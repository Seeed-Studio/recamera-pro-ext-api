"""
Host-side tests for the unified configurable output (OUTPUT_SINK_SPEC §9-P1).

No device, no real network: an in-process HTTP server, an injected UART FD, and
a hand-rolled fake MQTT broker (socket-level packet decode) exercise every
channel/formatter. Malformed templates/config must never raise into the caller.
"""
from __future__ import annotations

import json
import os
import socket
import struct
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
import sys  # noqa: E402
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from kit.adapters.output_sink import (  # noqa: E402
    ConfigurableSink,
    HaDiscoveryFormatter,
    HttpChannel,
    Jinja2Formatter,
    MqttChannel,
    RawJsonFormatter,
    UartChannel,
    WsChannel,
    assemble_output_sink,
    build_formatter,
    build_namespace,
    generate_mapping_templates,
    resolve_output_config,
)
from kit.adapters.result_sink import MultiSink, OutputChannel, OutputMessage  # noqa: E402


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
class RecordChannel(OutputChannel):
    """Records everything published; can be told to raise (isolation test)."""

    def __init__(self, name="rec", raises=False):
        self.name = name
        self.raises = raises
        self.msgs = []

    def publish(self, message):
        if self.raises:
            raise RuntimeError("boom")
        self.msgs.append(message)


def _frame_payload(results=None, events=None, **extra):
    p = {"results": results or [], "events": events or []}
    p.update(extra)
    return p


def _wait(pred, timeout=8.0, interval=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(interval)
    return pred()


# --------------------------------------------------------------------------- #
# 1. envelope / seq / time / frame size
# --------------------------------------------------------------------------- #
def test_envelope_seq_time_frame():
    rec = RecordChannel()
    sink = ConfigurableSink(app_id="yolo", channels=[rec],
                            formatter=RawJsonFormatter())
    sink.set_frame_size(1920, 1080)
    t0 = int(time.time() * 1000)
    sink.emit(_frame_payload(results=[{"box": [1, 2, 3, 4], "score": 0.9}]), 12.5)
    sink.emit(_frame_payload(results=[{"box": [0, 0, 1, 1]}]), 13.0)
    assert len(rec.msgs) == 2
    e0 = json.loads(rec.msgs[0].body)
    e1 = json.loads(rec.msgs[1].body)
    assert e0["app"] == "yolo"
    assert e0["seq"] == 1 and e1["seq"] == 2          # monotonic per process
    assert e0["frame"] == {"width": 1920, "height": 1080, "pts": 12.5}
    assert e0["timestamp"] >= t0 and isinstance(e0["timestamp"], int)
    assert e0["results"][0]["score"] == 0.9


def test_extra_payload_fields_preserved():
    rec = RecordChannel()
    sink = ConfigurableSink(app_id="a", channels=[rec], formatter=RawJsonFormatter())
    geometry = [{"type": "point", "points": [[1, 2]]}]
    sink.emit(_frame_payload(inference_time_ms=5.0, stream_id="camera-0",
                             geometry=geometry), 1.0)
    env = json.loads(rec.msgs[0].body)
    assert env["inference_time_ms"] == 5.0 and env["stream_id"] == "camera-0"
    assert env["geometry"] == geometry


# --------------------------------------------------------------------------- #
# 2. field projection (namespace)
# --------------------------------------------------------------------------- #
def test_namespace_projection():
    env = {
        "app": "x", "timestamp": 1, "seq": 3, "frame": {"width": 10, "height": 10},
        "results": [
            {"box": [0, 0, 2, 2], "score": 0.8, "cls": 0, "cls_name": "person"},
            {"label": "cat"},                                   # classification-like
            {"box": [1, 1, 2, 2], "keypoints": [[1, 1, 0.5]]},  # keypoint
            {"mask": [[1]]},                                    # segmentation
            {"box": [0, 0, 1, 1], "track_id": 7},               # tracking
        ],
        "events": [{"kind": "fall", "track_id": 7}, {"kind": "metrics", "fps": 30}],
        "geometry": [{"type": "point", "points": [[1, 2]]}],
        "summary": {"state": "alarm"}, "metrics": {"fps": 30},
        "stream_id": "main", "inference_time_ms": 12.5, "pipeline_ms": 20.0,
    }
    ns = build_namespace(env, app_id="x")
    assert ns["detection"].count == 3      # three results carry a box
    assert ns["detection"].entries[0]["label"] == "person"
    assert len(ns["keypoints"]) == 1
    assert len(ns["classification"]) == 1
    assert len(ns["segmentation"]) == 1
    assert len(ns["tracking"]) == 2        # one result + one event carry track_id
    assert [e["kind"] for e in ns["events"].fall] == ["fall"]
    assert len(ns["events"].all) == 2
    assert list(ns["events"]) == env["events"]   # top-level events iterates raw
    assert ns["geometry"] == env["geometry"]
    assert ns["summary"]["state"] == "alarm" and ns["metrics"]["fps"] == 30
    assert ns["stream_id"] == "main"
    assert ns["inference_time_ms"] == 12.5 and ns["pipeline_ms"] == 20.0


# --------------------------------------------------------------------------- #
# 3. raw + jinja snapshots
# --------------------------------------------------------------------------- #
def test_raw_formatter_snapshot():
    env = {"app": "a", "timestamp": 1, "seq": 1,
           "frame": {"width": 4, "height": 4, "pts": 0.0},
           "results": [{"box": [0, 0, 1, 1]}], "events": []}
    msgs = RawJsonFormatter().format(env, channel="mqtt")
    assert len(msgs) == 1 and msgs[0].topic is None
    assert json.loads(msgs[0].body) == env


def test_jinja_formatter_snapshot():
    specs = [{"topic": "recamera/{{ app }}/count",
              "body": "{{ {'count': detection.count} | tojson }}"}]
    fmt = Jinja2Formatter(specs, app_id="yolo", device_id="dev1")
    env = {"app": "yolo", "results": [{"box": [0, 0, 1, 1]}, {"box": [1, 1, 2, 2]}],
           "events": []}
    msgs = fmt.format(env, channel="mqtt")
    assert len(msgs) == 1
    assert msgs[0].topic == "recamera/yolo/count"
    assert json.loads(msgs[0].body) == {"count": 2}


def test_jinja_bad_template_drops_only_that_message():
    specs = [{"topic": "t/ok", "body": "{{ detection.count | tojson }}"},
             {"topic": "t/bad", "body": "{{ nope.explode() }}"}]
    fmt = Jinja2Formatter(specs, app_id="a")
    msgs = fmt.format({"results": [{"box": [0, 0, 1, 1]}], "events": []},
                      channel="mqtt")
    topics = [m.topic for m in msgs]
    assert "t/ok" in topics and "t/bad" not in topics


def test_jinja_rejects_wildcard_topics():
    fmt = Jinja2Formatter([{"topic": "a/#", "body": "{{ 1 | tojson }}"}], app_id="a")
    assert fmt.format({"results": [], "events": []}, channel="mqtt") == []


def test_jinja_resource_limits_block_repeat_power_and_nested_loops():
    started = time.monotonic()
    repeat = Jinja2Formatter(
        [{"body": "{{ 'x' * 20000000 }}"}], app_id="a")
    assert repeat.format({"results": [], "events": []}) == []
    assert time.monotonic() - started < 1.0

    power = Jinja2Formatter(
        [{"body": "{{ 2 ** 1000000 }}"}], app_id="a")
    assert power.format({"results": [], "events": []}) == []

    import pytest
    with pytest.raises(ValueError, match="nested template loops"):
        Jinja2Formatter([{
            "body": ("{% for outer in results %}"
                     "{% for inner in results %}x{% endfor %}"
                     "{% endfor %}"),
        }], app_id="a")
    with pytest.raises(ValueError, match="literal exceeds length"):
        Jinja2Formatter([{"body": "x" * 4097}], app_id="a")


def test_jinja_stream_limit_stops_large_loop_but_raw_is_unchanged():
    formatter = Jinja2Formatter([{
        "body": "{% for item in results %}{{ 'x' * 65536 }}{% endfor %}",
    }], app_id="a")
    assert formatter.format({"results": list(range(10)), "events": []}) == []

    raw = {"results": [{"text": "x" * 300000}], "events": []}
    messages = RawJsonFormatter().format(raw)
    assert json.loads(messages[0].body) == raw


def test_jinja_callable_attributes_cannot_bypass_allocation_limits():
    bodies = (
        "{{ 'x'.ljust(20000000) }}",
        "{{ 'x'.rjust(20000000) }}",
        "{{ 'x'.center(20000000) }}",
        "{{ '1'.zfill(20000000) }}",
        "{{ '{:20000000}'.format('x') }}",
        "{{ '{x:20000000}'.format_map({'x': 'x'}) }}",
        "{{ 'x'.replace('x', 'y' * 65536) }}",
    )
    started = time.monotonic()
    for body in bodies:
        formatter = Jinja2Formatter([{"body": body}], app_id="a")
        assert formatter.format({"results": [], "events": []}) == []
    # This is a deterministic fast-failure assertion, not an RSS heuristic:
    # no bound method is invoked, so none can allocate its requested width.
    assert time.monotonic() - started < 1.0


# --------------------------------------------------------------------------- #
# 4. mapping generator escaping + ordering
# --------------------------------------------------------------------------- #
def test_mapping_generator_escaping_and_grouping():
    rows = [
        {"source": "detection.count", "target": "count", "topic": "r/{{ app }}/c",
         "omit_if_none": False},
        {"source": "events.fall|length", "target": "fall\"s", "topic": "r/{{ app }}/c"},
        {"source": "seq", "target": "seq", "topic": "r/{{ app }}/s"},
    ]
    specs = generate_mapping_templates(rows)
    # grouped by topic, first-seen order preserved
    assert [s["topic"] for s in specs] == ["r/{{ app }}/c", "r/{{ app }}/s"]
    assert specs[0]["generated_from_mapping"] is True
    # render the grouped body and confirm valid JSON + a quoted target name
    # survives as a JSON-escaped key (the real contract, checked post-render)
    fmt = Jinja2Formatter(specs, app_id="app")
    env = {"app": "app", "seq": 9,
           "results": [{"box": [0, 0, 1, 1]}], "events": [{"kind": "fall"}]}
    msgs = {m.topic: json.loads(m.body) for m in fmt.format(env, channel="mqtt")}
    assert msgs["r/app/c"] == {"count": 1, 'fall"s': 1}
    assert msgs["r/app/s"] == {"seq": 9}


def test_mapping_optional_row_omitted_when_none():
    rows = [{"source": "missing_field", "target": "v", "topic": "t"}]  # omit default
    specs = generate_mapping_templates(rows)
    fmt = Jinja2Formatter(specs, app_id="a")
    msgs = fmt.format({"results": [], "events": []}, channel="mqtt")
    # body renders to "{}" -> non-empty, but the field is omitted
    assert json.loads(msgs[0].body) == {}


# --------------------------------------------------------------------------- #
# 5. filters / rate limit / edge preservation
# --------------------------------------------------------------------------- #
def test_only_on_detection_suppresses_empty():
    rec = RecordChannel()
    sink = ConfigurableSink(app_id="a", channels=[rec], formatter=RawJsonFormatter(),
                            filters={"only_on_detection": True})
    sink.emit(_frame_payload(), 0.0)                                # empty -> dropped
    sink.emit(_frame_payload(events=[{"kind": "metrics"}]), 0.0)    # metrics-only -> dropped
    sink.emit(_frame_payload(results=[{"box": [0, 0, 1, 1]}]), 0.0)  # kept
    assert len(rec.msgs) == 1


def test_class_allowlist_filters_results_keeps_unclassed_events():
    rec = RecordChannel()
    sink = ConfigurableSink(app_id="a", channels=[rec], formatter=RawJsonFormatter(),
                            filters={"classes": ["person"]})
    sink.emit(_frame_payload(
        results=[{"cls_name": "person", "box": [0, 0, 1, 1]},
                 {"cls_name": "car", "box": [1, 1, 2, 2]}],
        events=[{"kind": "fall"}, {"kind": "det", "cls_name": "car"}]), 0.0)
    env = json.loads(rec.msgs[0].body)
    assert [r["cls_name"] for r in env["results"]] == ["person"]
    kinds = [e["kind"] for e in env["events"]]
    assert "fall" in kinds and "det" not in kinds     # unclassed kept, car-event dropped


def test_rate_limit_and_edge_bypass():
    rec = RecordChannel()
    sink = ConfigurableSink(app_id="a", channels=[rec], formatter=RawJsonFormatter(),
                            filters={"rate_limit_hz": 1000.0, "preserve_edge_events": True})
    # first non-edge passes and arms the token
    sink.emit(_frame_payload(results=[{"box": [0, 0, 1, 1]}]), 0.0)
    # immediate second non-edge is throttled
    sink.emit(_frame_payload(results=[{"box": [0, 0, 1, 1]}]), 0.0)
    n_after_throttle = len(rec.msgs)
    # an edge event bypasses the throttle
    sink.emit(_frame_payload(events=[{"kind": "fall"}]), 0.0)
    assert n_after_throttle == 1
    assert len(rec.msgs) == 2


def test_all_bundled_application_edge_kinds_bypass_rate_limit():
    rec = RecordChannel()
    sink = ConfigurableSink(
        app_id="a", channels=[rec], formatter=RawJsonFormatter(),
        filters={"rate_limit_hz": 0.0001, "preserve_edge_events": True})
    sink.emit(_frame_payload(results=[{"box": [0, 0, 1, 1]}]), 0.0)
    kinds = (
        "fall", "line_cross", "blink", "yawn", "wake", "transcript",
        "listen_timeout", "rep_completed", "set_completed",
        "workout_complete",
    )
    for kind in kinds:
        sink.emit(_frame_payload(events=[{"kind": kind}]), 0.0)
    # The bundled fitness app emits kind=workout every frame and signals the
    # discrete transitions through booleans rather than changing kind.
    sink.emit(_frame_payload(events=[{
        "kind": "workout", "rep_completed": True,
        "set_completed": False, "workout_complete": False,
    }]), 0.0)
    assert len(rec.msgs) == 2 + len(kinds)
    # These bundled outputs are continuous observations, not transitions; they
    # remain rate-limited and Result Hub stores them as latest-wins state.
    for kind in ("qrcode", "text", "drowsiness"):
        sink.emit(_frame_payload(events=[{"kind": kind}]), 0.0)
    assert len(rec.msgs) == 2 + len(kinds)


# --------------------------------------------------------------------------- #
# 6. MultiSink isolation + hot swap
# --------------------------------------------------------------------------- #
def test_channel_failure_isolated():
    bad = RecordChannel(name="bad", raises=True)
    good = RecordChannel(name="good")
    sink = ConfigurableSink(app_id="a", channels=[bad, good],
                            formatter=RawJsonFormatter())
    sink.emit(_frame_payload(results=[{"box": [0, 0, 1, 1]}]), 0.0)  # must not raise
    assert len(good.msgs) == 1


def test_multisink_isolates_configurable_from_primary():
    class BoomSink:
        def emit(self, p, pts): raise RuntimeError("x")
        def emit_meta(self, p): pass
        def set_frame_size(self, w, h): pass
        def close(self): pass
    rec = RecordChannel()
    conf = ConfigurableSink(app_id="a", channels=[rec], formatter=RawJsonFormatter())
    multi = MultiSink([BoomSink(), conf])
    multi.emit(_frame_payload(results=[{"box": [0, 0, 1, 1]}]), 0.0)
    assert len(rec.msgs) == 1


def test_hot_swap_formatter():
    rec = RecordChannel()
    sink = ConfigurableSink(app_id="a", channels=[rec], formatter=RawJsonFormatter())
    sink.emit(_frame_payload(results=[{"box": [0, 0, 1, 1]}]), 0.0)
    assert json.loads(rec.msgs[0].body)["app"] == "a"   # raw envelope
    new = Jinja2Formatter([{"topic": "t", "body": "{{ detection.count | tojson }}"}],
                          app_id="a")
    sink.on_config_reload({"_formatter": new, "output_filters": {}})
    sink.emit(_frame_payload(results=[{"box": [0, 0, 1, 1]}]), 0.0)
    assert rec.msgs[1].body == b"1"                     # jinja output after swap


def test_hot_reload_rebuilds_formatter_from_ordinary_effective_config():
    rec = RecordChannel()

    def rebuild(config):
        template = (config.get("dTemplate") or {}).get("sDetection")
        return Jinja2Formatter([{"topic": "t", "body": template}], app_id="a")

    sink = ConfigurableSink(
        app_id="a", channels=[rec],
        formatter=Jinja2Formatter(
            [{"topic": "t", "body": "{{ detection.count | tojson }}"}],
            app_id="a"),
        formatter_builder=rebuild)
    sink.on_config_reload({
        "dTemplate": {"sDetection": "{{ (detection.count + 10) | tojson }}"},
        "output_filters": {},
    })
    sink.emit(_frame_payload(results=[{"box": [0, 0, 1, 1]}]), 0.0)
    assert rec.msgs[0].body == b"11"


def test_hot_reload_compile_failure_retains_last_good_formatter():
    rec = RecordChannel()
    original = Jinja2Formatter(
        [{"topic": "t", "body": "{{ detection.count | tojson }}"}], app_id="a")

    def rebuild(_config):
        raise ValueError("bad live template")

    sink = ConfigurableSink(app_id="a", channels=[rec], formatter=original,
                            formatter_builder=rebuild)
    sink.on_config_reload({"dTemplate": {"sDetection": "{{"},
                           "output_filters": {}})
    sink.emit(_frame_payload(results=[{"box": [0, 0, 1, 1]}]), 0.0)
    assert sink.formatter is original
    assert rec.msgs[0].body == b"1"


# --------------------------------------------------------------------------- #
# 7. WS channel reuse (preserve_envelope, no second seq)
# --------------------------------------------------------------------------- #
def test_ws_channel_preserves_envelope():
    from kit.adapters.result_sink import WsResultSink
    captured = []

    class FakeWs(WsResultSink):
        def __init__(self):        # bypass real socket server
            self.preserve_envelope = True
        def publish_envelope(self, obj):
            captured.append(obj)
        def client_count(self):
            return 0

    ch = WsChannel(FakeWs(), own=False)
    sink = ConfigurableSink(app_id="w", channels=[ch], formatter=RawJsonFormatter())
    sink.set_frame_size(8, 8)
    sink.emit(_frame_payload(results=[{"box": [0, 0, 1, 1]}]), 2.0)
    assert len(captured) == 1
    assert captured[0]["seq"] == 1 and captured[0]["app"] == "w"


# --------------------------------------------------------------------------- #
# 8. HTTP local test server
# --------------------------------------------------------------------------- #
def test_http_channel_posts_to_local_server(monkeypatch):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    received = {}

    # The host may define a corporate HTTP proxy without a NO_PROXY entry.
    # This test exercises a loopback transport and must never leave the host.
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence
            pass
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            received["body"] = self.rfile.read(n)
            received["auth"] = self.headers.get("Authorization")
            received["ctype"] = self.headers.get("Content-Type")
            self.send_response(200)
            self.end_headers()

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    ch = HttpChannel(url=f"http://127.0.0.1:{port}/ingest", token="secret")
    ch.publish(OutputMessage(body=b'{"k":1}', topic=None))
    assert _wait(lambda: "body" in received)
    ch.close()
    srv.shutdown()
    assert received["body"] == b'{"k":1}'
    assert received["auth"] == "Bearer secret"
    assert received["ctype"] == "application/json"


def test_http_channel_never_blocks_and_bounds_queue():
    ch = HttpChannel(url="http://127.0.0.1:1/none", queue_size=2, autostart=False)
    # publish never raises even with a dead worker / full queue
    for _ in range(50):
        ch.publish(OutputMessage(body=b"x"))
    assert ch._q.qsize() <= 2
    ch.close()


# --------------------------------------------------------------------------- #
# 9. UART injected FD
# --------------------------------------------------------------------------- #
def test_uart_channel_injected_fd():
    r, w = os.pipe()
    ch = UartChannel(fd=w)
    ch.publish(OutputMessage(body=b'{"a":1}'))
    ch.publish(OutputMessage(body=b'{"b":2}\n'))
    data = os.read(r, 1024)
    assert data == b'{"a":1}\n{"b":2}\n'    # newline-delimited, no double newline
    ch.close()
    os.close(r)
    try:
        os.close(w)
    except OSError:
        pass


def test_uart_rejects_non_allowlisted_path():
    import pytest
    with pytest.raises(ValueError):
        UartChannel(port_dev="/etc/passwd", enabled=True)


def test_uart_disabled_is_noop():
    ch = UartChannel(port_dev="/dev/ttyS9", enabled=False)   # gated off, no open
    ch.publish(OutputMessage(body=b"x"))                     # must not raise


# --------------------------------------------------------------------------- #
# 10. fake MQTT broker: packets / LWT / discovery / reconnect
# --------------------------------------------------------------------------- #
class FakeBroker:
    """Minimal MQTT 3.1.1 broker: decodes CONNECT (incl. will) + PUBLISH."""

    def __init__(self, drop_first=False):
        self.publishes = []          # (topic, payload_bytes, retain)
        self.connects = []           # {client_id, will_topic, will_payload}
        self._drop_first = drop_first
        self._conn_count = 0
        self._stop = threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.sock.settimeout(0.3)
        self.port = self.sock.getsockname()[1]
        self._t = threading.Thread(target=self._accept, daemon=True)
        self._t.start()

    def _accept(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self._conn_count += 1
            drop = self._drop_first and self._conn_count == 1
            threading.Thread(target=self._handle, args=(conn, drop),
                             daemon=True).start()

    @staticmethod
    def _recv(sock, n):
        buf = b""
        while len(buf) < n:
            c = sock.recv(n - len(buf))
            if not c:
                raise ConnectionError
            buf += c
        return buf

    def _remaining_length(self, sock):
        mult = 1
        val = 0
        while True:
            b = self._recv(sock, 1)[0]
            val += (b & 0x7F) * mult
            if not (b & 0x80):
                break
            mult *= 128
        return val

    def _handle(self, conn, drop):
        try:
            conn.settimeout(5)
            first = self._recv(conn, 1)[0]
            rl = self._remaining_length(conn)
            body = self._recv(conn, rl) if rl else b""
            if (first & 0xF0) == 0x10:            # CONNECT
                self._parse_connect(body)
            conn.sendall(b"\x20\x02\x00\x00")     # CONNACK ok
            if drop:
                conn.close()
                return
            while not self._stop.is_set():
                first = self._recv(conn, 1)[0]
                rl = self._remaining_length(conn)
                body = self._recv(conn, rl) if rl else b""
                op = first & 0xF0
                if op == 0x30:                    # PUBLISH (QoS0)
                    retain = bool(first & 0x01)
                    tlen = struct.unpack(">H", body[:2])[0]
                    topic = body[2:2 + tlen].decode()
                    payload = body[2 + tlen:]
                    self.publishes.append((topic, payload, retain))
                elif op == 0xC0:                  # PINGREQ
                    conn.sendall(b"\xd0\x00")
                elif op == 0xE0:                  # DISCONNECT
                    break
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _parse_connect(self, body):
        # variable header: protocol name(2+4) + level(1) + flags(1) + keepalive(2)
        i = 0
        plen = struct.unpack(">H", body[i:i + 2])[0]
        i += 2 + plen
        i += 1                                    # protocol level
        flags = body[i]
        i += 1
        i += 2                                    # keepalive
        # payload: client id
        cl = struct.unpack(">H", body[i:i + 2])[0]
        i += 2
        client_id = body[i:i + cl].decode()
        i += cl
        rec = {"client_id": client_id, "will_topic": None, "will_payload": None}
        if flags & 0x04:                          # will present
            wl = struct.unpack(">H", body[i:i + 2])[0]
            i += 2
            rec["will_topic"] = body[i:i + wl].decode()
            i += wl
            wpl = struct.unpack(">H", body[i:i + 2])[0]
            i += 2
            rec["will_payload"] = body[i:i + wpl]
            i += wpl
        self.connects.append(rec)

    def close(self):
        self._stop.set()
        try:
            self.sock.close()
        except Exception:
            pass


def test_mqtt_channel_lwt_and_publish():
    broker = FakeBroker()
    try:
        ch = MqttChannel(host="127.0.0.1", port=broker.port, client_id="c1",
                         default_topic="recamera/app/state",
                         will_topic="recamera/app/status", will_payload=b"offline")
        assert _wait(lambda: len(broker.connects) >= 1)
        # LWT registered from CONNECT
        assert broker.connects[0]["will_topic"] == "recamera/app/status"
        assert broker.connects[0]["will_payload"] == b"offline"
        ch.publish(OutputMessage(body=b'{"n":1}'))               # -> default topic
        ch.publish(OutputMessage(body=b"hi", topic="recamera/app/custom",
                                 retain=True))
        assert _wait(lambda: len(broker.publishes) >= 2)
        by_topic = {t: (p, r) for t, p, r in broker.publishes}
        assert by_topic["recamera/app/state"][0] == b'{"n":1}'
        assert by_topic["recamera/app/custom"] == (b"hi", True)
        ch.close()
        # graceful close publishes retained offline
        assert _wait(lambda: any(t == "recamera/app/status" and p == b"offline"
                                 for t, p, r in broker.publishes))
    finally:
        broker.close()


def test_mqtt_channel_ha_discovery_on_ready():
    broker = FakeBroker()
    try:
        fmt = HaDiscoveryFormatter(
            app_id="yolo", node="dev1", base_topic="recamera",
            entities=[{"object_id": "count", "name": "Count",
                       "value_template": "{{ value_json.results_count }}"}])
        ch = MqttChannel(host="127.0.0.1", port=broker.port, client_id="c",
                         default_topic=fmt.state_topic,
                         will_topic=fmt.status_topic,
                         on_ready=lambda: fmt.on_channel_ready(None))
        assert _wait(lambda: len(broker.publishes) >= 2)
        topics = [t for t, _, _ in broker.publishes]
        # retained online + retained discovery config
        assert "recamera/yolo/status" in topics
        disc = [t for t in topics if t.endswith("/config")]
        assert disc and "homeassistant/sensor/recamera_dev1_yolo/count/config" in disc
        # discovery payload carries availability + unique_id
        dp = next(p for t, p, _ in broker.publishes if t.endswith("/config"))
        cfg = json.loads(dp)
        assert cfg["availability_topic"] == "recamera/yolo/status"
        assert cfg["unique_id"] == "recamera_dev1_yolo_count"
        ch.close()
    finally:
        broker.close()


def test_mqtt_channel_reconnects_and_rearms_discovery():
    broker = FakeBroker(drop_first=True)
    try:
        ready_calls = {"n": 0}

        def on_ready():
            ready_calls["n"] += 1
            return [OutputMessage(body=b"online", topic="recamera/app/status",
                                  retain=True)]

        ch = MqttChannel(host="127.0.0.1", port=broker.port, client_id="c",
                         keepalive=10, default_topic="recamera/app/state",
                         on_ready=on_ready)
        # first connect dropped by broker; channel must reconnect (>=2 connects)
        assert _wait(lambda: len(broker.connects) >= 2, timeout=12.0)
        # on_ready re-fired on reconnect -> discovery/online re-armed
        assert ready_calls["n"] >= 2
        ch.close()
    finally:
        broker.close()


# --------------------------------------------------------------------------- #
# 11. assembly + legacy bypass
# --------------------------------------------------------------------------- #
class _App:
    id = "yolo"
    name = "YOLO"


def test_bypass_when_no_output_capability():
    manifest = {"name": "YOLO", "capabilities": [], "output": {"sink": "ws"}}
    sink, opted = assemble_output_sink(_App(), "/tmp", manifest, {})
    assert sink is None and opted is False


def test_assembly_builds_mqtt_channel_when_opted_in():
    manifest = {
        "name": "YOLO", "capabilities": ["output"],
        "output": {"default_channel": ["mqtt"], "default_mode": "raw"},
    }
    eff = {"dMqtt": {"sURL": "127.0.0.1", "iPort": 1883, "sTopic": "recamera"}}
    sink, opted = assemble_output_sink(_App(), "/tmp", manifest, eff)
    try:
        assert opted is True and sink is not None
        assert [c.name for c in sink.channels] == ["mqtt"]
        assert sink._formatter_builder is not None
    finally:
        if sink is not None:
            sink.close()


def test_assembly_arms_discovery_on_first_connect():
    """Regression (P3b init-time race): on_ready must be wired BEFORE the MQTT
    connect thread starts, so HA discovery config + retained `online` publish on
    the FIRST connect (spec §5) -- not only after a broker bounce/reconnect.

    The broker never drops, so a discovery `.../config` packet arriving while
    ``broker._conn_count == 1`` proves the config was armed on the first (and
    only) connection, no reconnect required."""
    broker = FakeBroker()               # accepts first connect, never drops
    try:
        manifest = {
            "name": "YOLO", "capabilities": ["output"],
            "output": {"default_channel": ["mqtt"], "default_mode": "ha"},
            "ha_entities": [{"object_id": "count", "name": "Count",
                             "value_template": "{{ value_json.results_count }}"}],
        }
        eff = {"dMqtt": {"sURL": "127.0.0.1", "iPort": broker.port,
                         "sTopic": "recamera"}}
        sink, opted = assemble_output_sink(_App(), "/tmp", manifest, eff)
        assert opted is True and sink is not None
        try:
            # discovery config must arrive on the first connection
            assert _wait(lambda: any(t.endswith("/config")
                                     for t, _, _ in broker.publishes))
            topics = [t for t, _, _ in broker.publishes]
            # retained `online` availability alongside the discovery config
            assert any(t == "recamera/yolo/status" for t in topics)
            cfgs = [t for t in topics if t.endswith("/config")]
            assert any(t.startswith("homeassistant/sensor/recamera_")
                       and t.endswith("/count/config") for t in cfgs)
            # armed on the FIRST connect: no reconnect happened
            assert broker._conn_count == 1
        finally:
            sink.close()
    finally:
        broker.close()


def test_assembly_ws_only_returns_none_but_opted_in():
    manifest = {"name": "A", "capabilities": ["output"],
                "output": {"default_channel": ["ws"]}}
    sink, opted = assemble_output_sink(_App(), "/tmp", manifest, {})
    assert sink is None and opted is True      # WS covered by primary overlay


def test_resolve_output_config_merges_manifest_and_user():
    manifest = {"output": {"default_channel": "ws", "default_mode": "custom",
                           "templates": {"detection": "{{ detection.count }}"}}}
    cfg = resolve_output_config(manifest, {"iMode": 2, "output_channels": ["mqtt"]})
    assert cfg["channels"] == ["mqtt"]        # user overrides manifest default
    assert cfg["mode"] == "raw"               # numeric iMode 2 -> raw
    assert cfg["templates"]["detection"] == "{{ detection.count }}"


def test_custom_format_source_is_explicit_not_mapping_first_ambiguity():
    mapping = [{"source": "detection.count", "target": "mapped",
                "topic": "mapped"}]
    common = {
        "mapping": mapping,
        "dTemplate": {"sDetection": "{{ (detection.count + 10) | tojson }}"},
        "templates": {},
    }
    envelope = _frame_payload(results=[{"box": [0, 0, 1, 1]}])

    mapping_formatter = build_formatter(
        "custom", {**common, "template_mode": "mapping"}, app_id="a",
        node="n", base_topic="base", entities=[], device_name="A")
    mapping_messages = mapping_formatter.format(envelope, channel="ws")
    assert mapping_messages[0].topic == "mapped"
    assert json.loads(mapping_messages[0].body) == {"mapped": 1}

    template_formatter = build_formatter(
        "custom", {**common, "template_mode": "template"}, app_id="a",
        node="n", base_topic="base", entities=[], device_name="A")
    template_messages = template_formatter.format(envelope, channel="ws")
    assert template_messages[0].topic == "base/a/detection"
    assert template_messages[0].body == b"11"


def test_malformed_filters_never_crash():
    rec = RecordChannel()
    sink = ConfigurableSink(app_id="a", channels=[rec], formatter=RawJsonFormatter(),
                            filters={"rate_limit_hz": "not-a-number", "classes": None})
    sink.emit(_frame_payload(results=[{"box": [0, 0, 1, 1]}]), 0.0)   # must not raise
    assert len(rec.msgs) == 1

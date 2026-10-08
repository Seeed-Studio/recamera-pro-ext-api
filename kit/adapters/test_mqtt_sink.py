"""Offline tests for MQTT state aggregation (no broker required)."""
from __future__ import annotations

import os
import sys


_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from kit.adapters.mqtt_sink import MqttSink  # noqa: E402


def test_build_state_reports_true_person_and_fallen_counts():
    results = [
        {"kind": "person", "track_id": 1, "state": "normal",
         "fall_detected": False, "score": 0.9},
        {"kind": "person", "track_id": 2, "state": "fallen",
         "fall_detected": True, "score": 0.8},
        {"kind": "car", "cls_name": "car", "score": 0.7},
    ]
    events = [
        # Put the edge event first to prove a later normal pose_state cannot
        # erase its event id in the compatibility summary.
        {"kind": "fall", "track_id": 2, "event_id": 4},
        {"kind": "pose_state", "track_id": 1, "state": "normal",
         "fall_detected": False},
        {"kind": "pose_state", "track_id": 2, "state": "fallen",
         "fall_detected": True},
    ]

    state = MqttSink._build_state("fall-detection", 7, 1.25, results, events)

    # Existing fields remain intact while the new counters reflect results,
    # rather than the number/kind of app events.
    assert state["results_count"] == 3
    assert state["person_count"] == 2
    assert state["fallen_count"] == 1
    assert state["summary"]["person_count"] == 2
    assert state["summary"]["fallen_count"] == 1
    assert state["summary"]["fall_detected"] is True
    assert state["summary"]["event_id"] == 4
    assert state["counts_by_kind"] == {"pose_state": 2, "fall": 1}


def test_build_state_keeps_legacy_person_fallbacks():
    # Older/other pose consumers may only provide cls_name or track/state.
    results = [
        {"cls_name": "person", "fall_detected": True},
        {"track_id": 9, "state": "normal", "fall_detected": False},
    ]
    state = MqttSink._build_state("legacy", 1, 0.0, results, [])
    assert state["person_count"] == 2
    assert state["fallen_count"] == 1


def test_fall_contract_matches_cross_platform_shape():
    sink = object.__new__(MqttSink)
    sink._seq = 9
    sink._fall_global_event_id = 2
    sink.app_id = "fall-detection"
    sink._frame_w = 1280
    sink._frame_h = 720
    payload = {
        "stream_id": "camera-0",
        "inference_time_ms": 12.5,
        "pipeline_ms": 18.0,
        "results": [{
            "kind": "person", "track_id": 3, "box": [128, 72, 640, 648],
            "score": 0.9, "keypoints": [[320, 180, 0.8]] * 17,
            "features": {"valid": True, "hip_drop_speed": 0.1,
                         "hip_drop_distance": 0.0, "torso_angle_deg": 10.0,
                         "bbox_aspect_ratio": 0.9},
        }],
        "events": [
            {"kind": "pose_state", "track_id": 3, "visible": True,
             "state": "fallen", "fall_detected": True, "event_id": 1,
             "missed_frames": 0},
            {"kind": "fall", "track_id": 3, "event_id": 1},
        ],
    }
    state = sink._fall_contract(payload, 1.0)
    required = {"timestamp", "frame_id", "inference_time_ms", "stream_id",
                "fall_detected", "fall_event", "event_id", "global_event_id",
                "event_id_scope", "state", "person_detected", "person_count",
                "fallen_count", "tracking", "features", "keypoints", "pose17",
                "persons"}
    assert required <= state.keys()
    assert state["event_id"] == state["global_event_id"] == 3
    assert state["event_id_scope"] == "stream_global_event_id"
    assert state["person_count"] == state["fallen_count"] == 1
    assert len(state["persons"][0]["pose17"]) == 17


# --------------------------------------------------------------------------- #
# MqttClient: SUBSCRIBE / inbound PUBLISH / QoS1 / keepalive / reconnect,
# against an in-process fake broker on a socketpair (no network).
# --------------------------------------------------------------------------- #
import queue  # noqa: E402
import socket  # noqa: E402
import struct  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

import pytest  # noqa: E402

from kit.adapters import mqtt_sink  # noqa: E402
from kit.adapters.mqtt_sink import MqttClient, _MqttConnection  # noqa: E402


def _read_pkt(sock):
    """Broker-side reader: one control packet -> (header0, body) or None."""
    def exact(n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf
    h = exact(1)
    if h is None:
        return None
    length, mult = 0, 1
    while True:
        b = exact(1)
        if b is None:
            return None
        length += (b[0] & 0x7F) * mult
        mult *= 128
        if not b[0] & 0x80:
            break
    body = exact(length) if length else b""
    return None if body is None else (h[0], body)


def _wire_str(s):
    d = s.encode()
    return struct.pack(">H", len(d)) + d


class FakeBroker:
    """Accepts each `socket.create_connection` as a socketpair session.

    Every packet the client sends lands in `self.rx` as
    (session_no, header0, body). The broker auto-answers CONNECT with
    CONNACK(rc), SUBSCRIBE with SUBACK(granted qos) and PINGREQ with PINGRESP.
    """

    def __init__(self, rc=0):
        self.rc = rc
        self.rx = queue.Queue()
        self.sessions = []
        self.log = []                 # every (session, header0) in arrival order
        self.answer_ping = True       # False: a broker that ignores PINGREQ
        self.connack_gates = {}       # session -> Event: hold CONNACK until set

    def create_connection(self, _addr, timeout=None):
        client, server = socket.socketpair()
        if timeout is not None:
            client.settimeout(timeout)
        n = len(self.sessions)
        self.sessions.append(server)
        threading.Thread(target=self._serve, args=(n, server), daemon=True).start()
        return client

    def _serve(self, n, sock):
        while True:
            try:
                pkt = _read_pkt(sock)
            except OSError:
                pkt = None
            if pkt is None:
                self.rx.put((n, None, b""))
                return
            h, body = pkt
            self.log.append((n, h))
            self.rx.put((n, h, body))
            ptype = h >> 4
            try:
                if ptype == 1:
                    gate = self.connack_gates.get(n)
                    if gate is not None:
                        gate.wait(5.0)
                    sock.sendall(bytes([0x20, 2, 0, self.rc]))
                elif ptype == 8:
                    pid = body[:2]
                    sock.sendall(bytes([0x90, 3]) + pid + bytes([body[-1]]))
                elif ptype == 12 and self.answer_ping:
                    sock.sendall(b"\xd0\x00")
            except OSError:
                return

    def send(self, n, data):
        self.sessions[n].sendall(data)

    def expect(self, ptype, timeout=3.0, session=None):
        """Next packet of `ptype` (others, and other sessions when `session`
        is given, are skipped) -> (session, h, body)."""
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise AssertionError(f"no packet type {ptype} within {timeout}s")
            n, h, body = self.rx.get(timeout=left)
            if (h is not None and h >> 4 == ptype
                    and (session is None or n == session)):
                return n, h, body


@pytest.fixture
def broker(monkeypatch):
    b = FakeBroker()
    monkeypatch.setattr(mqtt_sink.socket, "create_connection", b.create_connection)
    yield b
    for s in b.sessions:
        try:
            s.close()
        except OSError:
            pass


def _parse_connect(body):
    pos = 2 + struct.unpack(">H", body[:2])[0]           # protocol name
    level, flags = body[pos], body[pos + 1]
    keepalive = struct.unpack(">H", body[pos + 2:pos + 4])[0]
    return level, flags, keepalive


def test_connect_encodes_qos1_retained_will(broker):
    client = MqttClient("broker", 1883, client_id="cid", keepalive=30,
                        username="u", password="p", will_topic="a/status",
                        will_payload=b'{"online":false}', will_qos=1,
                        will_retain=True)
    client.connect()
    try:
        _, _, body = broker.expect(1)
        level, flags, keepalive = _parse_connect(body)
        assert level == 4 and keepalive == 30
        # username|password|will retain|will QoS1|will flag|clean session
        assert flags == 0x80 | 0x40 | 0x20 | 0x08 | 0x04 | 0x02
        assert b"a/status" in body and b'{"online":false}' in body
    finally:
        client.disconnect()


def test_subscribe_inbound_qos1_dispatch_and_puback(broker):
    got = []
    connected = []
    client = MqttClient("broker", client_id="cid",
                        on_connect=lambda c: connected.append(c),
                        on_message=lambda t, p: got.append((t, p)))
    client.subscribe("base/cmd/snapshot", qos=1)
    client.connect()
    try:
        _, h, body = broker.expect(8)
        assert h == 0x82                                 # SUBSCRIBE, reserved bits 0010
        assert body[2:] == _wire_str("base/cmd/snapshot") + b"\x01"
        assert connected == [client]
        # broker delivers a QoS1 PUBLISH, packet id 0x1234
        body = _wire_str("base/cmd/snapshot") + b"\x12\x34" + b'{"event_id":"e1"}'
        broker.send(0, bytes([0x32, len(body)]) + body)
        _, h, ack = broker.expect(4)
        assert (h, ack) == (0x40, b"\x12\x34")           # PUBACK echoes the id
        deadline = time.monotonic() + 2
        while not got and time.monotonic() < deadline:
            time.sleep(0.01)
        assert got == [("base/cmd/snapshot", b'{"event_id":"e1"}')]
        # QoS0 delivery: dispatched, no PUBACK
        body = _wire_str("base/cmd/snapshot") + b"x"
        broker.send(0, bytes([0x30, len(body)]) + body)
        deadline = time.monotonic() + 2
        while len(got) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert got[1] == ("base/cmd/snapshot", b"x")
    finally:
        client.disconnect()


def test_publish_qos_and_retain_flags(broker):
    client = MqttClient("broker", client_id="cid")
    client.connect()
    try:
        assert client.publish("t/status", '{"a":1}', qos=1, retain=True)
        _, h, body = broker.expect(3)
        assert h == 0x33                                 # PUBLISH|QoS1|retain
        assert body.startswith(_wire_str("t/status"))
        pid = struct.unpack(">H", body[10:12])[0]
        assert pid != 0 and body[12:] == b'{"a":1}'
        assert client.publish("t/det", b"d", qos=0, retain=False)
        _, h, body = broker.expect(3)
        assert h == 0x30 and body == _wire_str("t/det") + b"d"
        with pytest.raises(ValueError):
            client.publish("t", b"", qos=2)
    finally:
        client.disconnect()


def test_keepalive_sends_pingreq(broker):
    client = MqttClient("broker", client_id="cid", keepalive=1)
    client.connect()
    try:
        _, h, body = broker.expect(12, timeout=3.0)      # PINGREQ within keepalive
        assert (h, body) == (0xC0, b"")
        assert client.is_connected()
    finally:
        client.disconnect()


def test_reconnect_resubscribes_and_reruns_on_connect(broker):
    connects = []
    client = MqttClient("broker", client_id="cid",
                        on_connect=lambda c: connects.append(time.monotonic()))
    client.subscribe("x/cmd", qos=1)
    client.connect()
    try:
        n, _, _ = broker.expect(8)
        assert n == 0
        broker.sessions[0].shutdown(socket.SHUT_RDWR)    # broker drops the link
        n, _, body = broker.expect(8, timeout=5.0)       # SUBSCRIBE again
        assert n == 1 and body[2:] == _wire_str("x/cmd") + b"\x01"
        assert len(connects) == 2
        assert client.publish("x/y", b"1")
    finally:
        client.disconnect()


def test_publish_while_down_is_false_not_raise(broker):
    client = MqttClient("broker", client_id="cid")
    assert client.publish("t", b"x") is False             # never connected


def test_refused_connect_raises_and_leaves_no_thread(monkeypatch):
    b = FakeBroker(rc=5)                                  # not authorised
    monkeypatch.setattr(mqtt_sink.socket, "create_connection", b.create_connection)
    client = MqttClient("broker", client_id="cid")
    with pytest.raises(ConnectionError, match="rc=5"):
        client.connect()
    assert client._thread is None and not client.is_connected()


def test_disconnect_sends_disconnect_and_stops_thread(broker):
    client = MqttClient("broker", client_id="cid")
    client.connect()
    thread = client._thread
    client.disconnect()
    _, h, _ = broker.expect(14)
    assert h == 0xE0
    thread.join(2.0)
    assert not thread.is_alive() and not client.is_connected()


def test_connection_read_packet_parses_multibyte_length():
    a, b = socket.socketpair()
    try:
        conn = _MqttConnection("h", 1, "cid")
        conn._sock = a
        payload = b"z" * 300                              # remaining length > 127
        body = _wire_str("t") + payload
        b.sendall(bytes([0x30]) + mqtt_sink._remaining_length(len(body)) + body)
        h, got = conn.read_packet()
        assert h == 0x30
        assert mqtt_sink._parse_publish(h, got) == ("t", payload, 0, 0)
    finally:
        a.close()
        b.close()


# -- regression: lifecycle races (disconnect vs. in-flight reconnect) ------- #
def _reader_threads():
    return [t for t in threading.enumerate()
            if t.name == "mqtt-cid" and t.is_alive()]


def _drop_and_hold_reconnect(broker, client):
    """Kill session 0 and hold session 1's CONNACK; return its gate."""
    gate = threading.Event()
    broker.connack_gates[1] = gate
    broker.sessions[0].shutdown(socket.SHUT_RDWR)
    broker.expect(1, timeout=5.0, session=1)           # reconnect CONNECT
    return gate


def test_disconnect_cancels_in_flight_reconnect_handshake(broker):
    announced = []

    def on_connect(c):
        announced.append(1)
        c.publish("a/status", b"online", qos=1, retain=True)

    client = MqttClient("broker", client_id="cid", on_connect=on_connect)
    client.subscribe("a/cmd", qos=1)
    client.connect()
    broker.expect(3)                                     # first "online"
    gate = _drop_and_hold_reconnect(broker, client)
    client.disconnect(timeout=0.05)                      # returns mid-handshake
    gate.set()                                           # CONNACK arrives late
    deadline = time.monotonic() + 3
    while _reader_threads() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not _reader_threads()
    assert announced == [1]                              # no second on_connect
    session1 = [h >> 4 for n, h in broker.log if n == 1]
    assert 3 not in session1 and 8 not in session1       # no PUBLISH/SUBSCRIBE
    assert not client.is_connected()


def test_connect_right_after_disconnect_runs_one_reader(broker):
    client = MqttClient("broker", client_id="cid")
    client.connect()
    gate = _drop_and_hold_reconnect(broker, client)
    client.disconnect(timeout=0.05)
    threading.Timer(0.2, gate.set).start()               # old handshake ends late
    client.connect()                                     # waits for old reader
    try:
        time.sleep(0.2)
        assert len(_reader_threads()) == 1
        assert client.is_connected()
        assert client.publish("x", b"1")
    finally:
        client.disconnect()


# -- regression: liveness is judged on broker answers, not our own sends ---- #
def _publish_for(client, seconds, period=0.05):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        client.publish("t/det", b"d", qos=0)
        time.sleep(period)


def test_busy_qos0_publisher_on_healthy_link_is_not_dropped(broker):
    client = MqttClient("broker", client_id="cid", keepalive=1)
    client.connect()
    try:
        _publish_for(client, 2.6)                        # > 2 keepalive periods
        assert len(broker.sessions) == 1                 # never reconnected
        pings = [h for n, h in broker.log if n == 0 and h == 0xC0]
        assert len(pings) >= 2                           # PINGREQ not deferred
        assert client.is_connected()
    finally:
        client.disconnect()


def test_unanswered_pingreq_triggers_reconnect(broker):
    client = MqttClient("broker", client_id="cid", keepalive=1)
    client.connect()
    try:
        broker.answer_ping = False
        t = threading.Thread(target=_publish_for, args=(client, 3.0))
        t.start()
        broker.expect(1, timeout=5.0, session=1)         # reconnect CONNECT
        before = broker.log[:broker.log.index((1, 0x10))]
        # the drop followed an unanswered PINGREQ on the first session
        assert (0, 0xC0) in before
        t.join()
    finally:
        client.disconnect()

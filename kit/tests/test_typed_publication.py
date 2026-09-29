"""Typed publication + single shared sequence counter (overlay phase-2 V4-4/V5-3).

What these tests pin, in the order the spec argues for it:

  1. ★one counter per application★ -- result frames, metrics and status all draw
     from the same sink counter, and it is atomic, so two publishing threads
     cannot stamp two envelopes with the same `seq` (the Hub derives
     `app:generation:seq:type` ids, and a reused seq collides);
  2. a typed publication is recognisable as NOT a frame: top-level `type` is
     honoured, `results` is absent (not "an empty list" -- an explicit empty
     list is a frame snapshot to the consumer), and `geometry` is rejected;
  3. the results-only filter path (classes / only_on_detection / frame rate
     limit) does not touch typed publications;
  4. metrics are rate-limited independently, at <= 5 Hz.

Hardware-free: everything below the App is a recording sink.
"""
from __future__ import annotations

import json
import math
import os
import sys
import threading
import time

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import kit.app as kit_app                                              # noqa: E402
from kit.adapters.output_sink import (ConfigurableSink,                # noqa: E402
                                      RawJsonFormatter)
from kit.adapters.result_sink import (GatewayResultSink,               # noqa: E402
                                      MultiSink, WsResultSink)
from kit.errors import InputValidationError                            # noqa: E402

MANIFEST = {"id": "typed-test", "models": [], "render": {}}
APP_DIR = "/installed/typed-test"


# --------------------------------------------------------------------------- #
# doubles
# --------------------------------------------------------------------------- #
class RecordChannel:
    """OutputChannel that keeps every message it is handed."""

    def __init__(self, name="mqtt"):
        self.name = name
        self.messages = []
        self.lock = threading.Lock()

    def publish(self, message):
        with self.lock:
            self.messages.append(message)

    def client_count(self):
        return 0

    def close(self):
        pass

    def envelopes(self):
        with self.lock:
            return [json.loads(m.body.decode("utf-8")) for m in self.messages]


class RecordingSink:
    """ResultSink that records (kind, payload, pts) for every publication."""

    def __init__(self):
        self.records = []
        self.lock = threading.Lock()
        self._seq = 0
        self._seq_lock = threading.Lock()

    def _next_seq(self):
        with self._seq_lock:
            self._seq += 1
            return self._seq

    def emit(self, payload, pts):
        body = dict(payload)
        body.setdefault("type", "results")
        body["seq"] = self._next_seq()
        with self.lock:
            self.records.append(body)

    def emit_typed(self, payload, pts):
        if payload.get("geometry"):
            raise InputValidationError("typed geometry", operation="t",
                                       code="typed_geometry_forbidden")
        body = dict(payload)
        body["seq"] = self._next_seq()
        with self.lock:
            self.records.append(body)

    def emit_meta(self, payload):
        pass

    def set_frame_size(self, w, h):
        pass

    def close(self):
        pass

    def seqs(self):
        with self.lock:
            return [r["seq"] for r in self.records]

    def by_type(self, kind):
        with self.lock:
            return [r for r in self.records if r.get("type") == kind]


class PublisherApp(kit_app.App):
    """Loop-owning app that publishes nothing by itself."""

    id = "typed-test"
    owns_loop = True
    needs_frames = False
    needs_model = False

    def run(self):                       # pragma: no cover - never driven here
        pass


def _started_app(sink):
    app = PublisherApp()
    app.start(None, sink=sink, app_dir=APP_DIR, manifest=MANIFEST,
              config={}, verbose=False)
    return app


# --------------------------------------------------------------------------- #
# 1. one counter, atomic
# --------------------------------------------------------------------------- #
def test_two_threads_never_reuse_a_seq_across_frames_metrics_and_status():
    sink = RecordingSink()
    app = _started_app(sink)
    rounds = 200
    barrier = threading.Barrier(3)

    def pose_thread():
        barrier.wait()
        for i in range(rounds):
            app.emit([{"kind": "pose_state", "track_id": i}],
                     ts=float(i), results=[{"box": [0, 0, 1, 1], "state": "normal"}])

    def voice_thread():
        barrier.wait()
        for i in range(rounds):
            app.emit_status({"state": "listening", "text": f"line {i}"},
                            events=[{"kind": "transcript", "text": f"line {i}"}],
                            ts=float(i))
            app.emit_metrics({"audio_dbfs": -30.0}, ts=float(i))

    threads = [threading.Thread(target=pose_thread),
               threading.Thread(target=voice_thread)]
    for t in threads:
        t.start()
    barrier.wait()
    for t in threads:
        t.join()

    seqs = sink.seqs()
    assert len(seqs) == len(set(seqs)), "two publications reused a seq"
    # 200 frames + 200 status + (metrics admitted under the 5 Hz limiter).
    frames = sink.by_type("results")
    statuses = sink.by_type("status")
    metrics = sink.by_type("metrics")
    assert len(frames) == rounds
    assert len(statuses) == rounds
    assert len(metrics) >= 1
    assert len(seqs) == len(frames) + len(statuses) + len(metrics)
    print(f"\nseqs={len(seqs)} frames={len(frames)} status={len(statuses)} "
          f"metrics={len(metrics)}")


def test_sink_counter_is_atomic_without_the_app_lock():
    """The counter is safe even when the sink is called directly."""
    sink = RecordingSink()
    threads = [threading.Thread(target=lambda: [sink.emit({}, 0.0)
                                                for _ in range(500)])
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    seqs = sink.seqs()
    assert len(seqs) == 2000
    assert len(set(seqs)) == 2000


def test_configurable_sink_shares_one_counter_between_frames_and_typed():
    channel = RecordChannel()
    sink = ConfigurableSink(app_id="typed-test", channels=[channel],
                            formatter=RawJsonFormatter())
    sink.set_frame_size(640, 480)
    sink.emit({"results": [], "events": []}, 1.0)
    sink.emit_typed({"type": "metrics", "metrics": {"audio_dbfs": -20.0}}, 2.0)
    sink.emit_typed({"type": "status", "summary": {"state": "idle"}}, 3.0)
    sink.emit({"results": [{"box": [0, 0, 1, 1]}]}, 4.0)

    envelopes = channel.envelopes()
    assert [e["type"] for e in envelopes] == ["results", "metrics", "status",
                                             "results"]
    assert [e["seq"] for e in envelopes] == [1, 2, 3, 4]
    typed = [e for e in envelopes if e["type"] != "results"]
    for env in typed:
        assert "results" not in env, (
            "a typed envelope must omit `results` entirely; an explicit empty "
            "list would read as a frame snapshot to the Result Hub")
        assert "geometry" not in env


def test_ws_and_gateway_typed_envelopes_share_the_frame_counter():
    ws = WsResultSink(host="127.0.0.1", port=0, app_id="typed-test")
    seen = []
    ws._broadcast = seen.append                     # noqa: SLF001 - test seam
    ws.set_frame_size(320, 240)
    ws.emit({"results": [], "events": []}, 1.0)
    ws.emit_typed({"type": "status", "summary": {"state": "idle"}}, 2.0)
    ws.emit_typed({"type": "metrics", "metrics": {"audio_dbfs": -12.0}}, 3.0)
    try:
        assert [e["seq"] for e in seen] == [1, 2, 3]
        assert [e["type"] for e in seen] == ["results", "status", "metrics"]
        assert seen[0]["type"] == "results"
        assert "results" not in seen[1]
        ws.close()
    finally:
        ws.close()


def test_multi_sink_forwards_typed_publications_to_every_child():
    first, second = RecordingSink(), RecordingSink()
    MultiSink([first, second]).emit_typed(
        {"type": "metrics", "metrics": {"audio_dbfs": -3.0}}, 1.0)
    assert len(first.by_type("metrics")) == 1
    assert len(second.by_type("metrics")) == 1


# --------------------------------------------------------------------------- #
# 2. a typed publication is not a frame
# --------------------------------------------------------------------------- #
def test_emit_status_omits_results_and_carries_events():
    sink = RecordingSink()
    app = _started_app(sink)
    assert app.emit_status({"state": "listening", "text": "hello"},
                           events=[{"kind": "transcript", "text": "hello"}]) is True
    (status,) = sink.records
    assert status["type"] == "status"
    assert "results" not in status
    assert status["summary"] == {"state": "listening", "text": "hello"}
    assert status["events"] == [{"kind": "transcript", "text": "hello"}]


def test_emit_keeps_its_explicit_results_semantics():
    """`emit()` still publishes an explicit (possibly empty) results list."""
    sink = RecordingSink()
    app = _started_app(sink)
    app.emit([{"kind": "pose_state"}], ts=1.0)
    (frame,) = sink.records
    assert frame["type"] == "results"
    assert frame["results"] == []
    assert frame["events"] == [{"kind": "pose_state"}]


@pytest.mark.parametrize("method,args", [
    ("emit_metrics", ({"audio_dbfs": -1.0},)),
    ("emit_status", ({"state": "idle"},)),
])
def test_typed_publications_reject_geometry(method, args):
    sink = RecordingSink()
    app = _started_app(sink)
    with pytest.raises(InputValidationError) as caught:
        getattr(app, method)(*args, extra={"geometry": [{"type": "line"}]})
    assert caught.value.context.code == "typed_geometry_forbidden"
    assert sink.records == []


def test_sink_level_typed_emission_rejects_geometry():
    ws = WsResultSink(host="127.0.0.1", port=0, app_id="typed-test")
    ws._broadcast = lambda obj: None                # noqa: SLF001
    try:
        with pytest.raises(InputValidationError):
            ws.emit_typed({"type": "metrics", "geometry": [{"type": "point"}]}, 0.0)
    finally:
        ws.close()


# --------------------------------------------------------------------------- #
# 3. the results filter path does not see typed publications
# --------------------------------------------------------------------------- #
def test_only_on_detection_and_class_filter_do_not_drop_typed_publications():
    channel = RecordChannel()
    sink = ConfigurableSink(app_id="typed-test", channels=[channel],
                            formatter=RawJsonFormatter(),
                            filters={"only_on_detection": True,
                                     "classes": ["person"],
                                     "rate_limit_hz": 0.2})
    # A frame with neither results nor non-metric events is suppressed...
    sink.emit({"results": [], "events": []}, 1.0)
    # ...and so is any frame excluded by the class allow-list.
    sink.emit({"results": [{"label": "cat", "box": [0, 0, 1, 1]}],
               "events": []}, 2.0)
    # But metrics/status ride straight through, twice, past the frame rate gate.
    sink.emit_typed({"type": "metrics", "metrics": {"audio_dbfs": -30.0}}, 3.0)
    sink.emit_typed({"type": "metrics", "metrics": {"audio_dbfs": -31.0}}, 3.1)

    envelopes = channel.envelopes()
    assert [e["type"] for e in envelopes] == ["metrics", "metrics"]
    assert [e["seq"] for e in envelopes] == [3, 4], (
        "the suppressed frames still consumed their seq values; the counter is "
        "publication-order, not delivery-order")


def test_gateway_typed_publication_is_queued_without_a_results_key():
    sink = GatewayResultSink.__new__(GatewayResultSink)   # no socket needed
    sink.app_id = "typed-test"
    sink.preserve_envelope = False
    sink._seq = 0                                          # noqa: SLF001
    sink._seq_lock = threading.Lock()                      # noqa: SLF001
    sink._frame_w = 640                                    # noqa: SLF001
    sink._frame_h = 480                                    # noqa: SLF001
    queued = []
    sink._offer = lambda obj: queued.append(obj) or True   # noqa: SLF001
    sink.emit_typed({"type": "status", "summary": {"state": "idle"},
                     "events": [{"kind": "transcript", "text": "hi"}]}, 5.0)
    (env,) = queued
    assert env["type"] == "status"
    assert env["seq"] == 1
    assert "results" not in env


# --------------------------------------------------------------------------- #
# 4. metrics rate limit
# --------------------------------------------------------------------------- #
def test_metrics_are_limited_to_the_declared_rate():
    """At most `metrics_max_hz` admissions in ANY rolling one-second window.

    The budget is a rolling window rather than a token bucket precisely so the
    documented cap is the whole story: there is no burst allowance that lets a
    producer exceed `metrics_max_hz` per second even briefly.
    """
    sink = RecordingSink()
    app = _started_app(sink)
    app.metrics_max_hz = 5.0
    window = 1.5
    admitted = 0
    peak = 0
    started = time.monotonic()
    while time.monotonic() - started < window:
        if app.emit_metrics({"audio_dbfs": -20.0}):
            admitted += 1
            peak = max(peak, len(app._metrics_window))      # noqa: SLF001
        time.sleep(0.002)
    elapsed = time.monotonic() - started
    ceiling = app.metrics_max_hz * math.ceil(elapsed)
    print(f"\nadmitted={admitted} in {elapsed:.3f}s peak_in_window={peak} "
          f"ceiling={ceiling}")
    # The cap is per rolling second, so a half-open window of T seconds admits
    # at most ceil(T) full windows -- for the device's 30 s acceptance window
    # and hz=5 that is exactly 150, and never a burst on top of it.
    assert peak <= app.metrics_max_hz, "the rolling window overflowed"
    assert admitted <= ceiling, f"{admitted} > {ceiling}"
    assert admitted >= 2, "the limiter must still let metrics through"
    assert len(sink.by_type("metrics")) == admitted


def test_metrics_limiter_can_be_disabled():
    sink = RecordingSink()
    app = _started_app(sink)
    app.metrics_max_hz = 0.0
    for _ in range(20):
        assert app.emit_metrics({"audio_dbfs": -20.0}) is True
    assert len(sink.by_type("metrics")) == 20


# --------------------------------------------------------------------------- #
# 5. EVERY publication path shares that one counter (V5-3)
# --------------------------------------------------------------------------- #
def _bare_gateway_sink():
    """A real GatewayResultSink with no socket, recording what it queues."""
    sink = GatewayResultSink.__new__(GatewayResultSink)     # noqa: SLF001
    sink.app_id = "typed-test"
    sink.preserve_envelope = False
    sink._seq = 0                                           # noqa: SLF001
    sink._seq_lock = threading.Lock()                       # noqa: SLF001
    sink._frame_w, sink._frame_h = 640, 480                 # noqa: SLF001
    queued = []
    sink._offer = lambda obj: queued.append(obj) or True    # noqa: SLF001
    sink._queued = queued                                   # noqa: SLF001
    return sink


def test_emit_meta_draws_from_the_same_counter_as_frames_and_typed_paths():
    """The regression this pins: kit's own loop telemetry used to allocate
    nothing, so the consumer derived its seq -- and a derived seq can land on a
    published one, giving two different envelopes the same
    `app:generation:seq:metrics` identity."""
    sink = _bare_gateway_sink()
    app = PublisherApp()
    app.start(None, sink=sink, app_dir=APP_DIR, manifest=MANIFEST,
              config={}, verbose=False)

    app.emit([{"kind": "pose_state"}], ts=1.0,
             results=[{"box": [0, 0, 1, 1], "state": "normal"}])
    app.emit_metrics({"audio_dbfs": -20.0}, ts=2.0)
    app.emit_status({"state": "listening"}, ts=3.0)
    sink.emit_meta({"type": "metrics", "kind": "metrics", "app": "typed-test",
                    "fps": 12.0, "pts": 4.0})
    app.emit_metrics({"audio_dbfs": -21.0}, ts=5.0)

    envelopes = sink._queued                                  # noqa: SLF001
    seqs = [env["seq"] for env in envelopes]
    print(f"\npaths={[e['type'] for e in envelopes]} seqs={seqs}")
    assert seqs == [1, 2, 3, 4, 5], seqs
    assert len(seqs) == len(set(seqs)), "two publications reused a seq"

    metrics = [env for env in envelopes if env["type"] == "metrics"]
    assert len(metrics) == 3, "both metrics producers must be present"
    ids = [f"typed-test:1:{env['seq']}:metrics" for env in metrics]
    assert len(ids) == len(set(ids)), (
        "two metrics envelopes received the same Hub identity")


def test_two_publishers_one_sink_never_share_a_seq(tmp_path):
    """Two threads, three paths, one sink: the union has no duplicate seq.

    This is the shape that failed on device (an app-side metrics publisher next
    to kit's loop telemetry); the assertion is over the UNION, not per path.
    """
    sink = _bare_gateway_sink()
    app = PublisherApp()
    app.start(None, sink=sink, app_dir=APP_DIR, manifest=MANIFEST,
              config={}, verbose=False)
    rounds = 150
    barrier = threading.Barrier(3)

    def frames():
        barrier.wait()
        for i in range(rounds):
            app.emit([{"kind": "pose_state"}], ts=float(i),
                     results=[{"box": [0, 0, 1, 1]}])

    def metrics():
        barrier.wait()
        for i in range(rounds):
            app.emit_metrics({"audio_dbfs": -20.0})
            sink.emit_meta({"type": "metrics", "app": "typed-test",
                            "fps": 10.0, "pts": float(i)})

    threads = [threading.Thread(target=frames),
               threading.Thread(target=metrics)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    seqs = [env["seq"] for env in sink._queued]               # noqa: SLF001
    ordered = sorted(seqs)
    assert ordered == list(range(1, len(seqs) + 1)), (
        "the union of frame and metrics seqs is not a contiguous unique run")


def test_the_metrics_cap_is_exactly_partitioned_between_its_producers():
    """One cap for the `metrics` type, divided by reservation.

    The documented cap bounds `type == "metrics"` envelopes as a whole, so the
    reservation kit keeps for its own loop telemetry has to come OUT of it --
    at every configured value, including the sub-1 Hz ones where a one-second
    window cannot express the rate.
    """
    app = _started_app(RecordingSink())
    for hz in (0.5, 1, 2, 3, 5):
        app.metrics_max_hz = hz
        app_rate = (app._metrics_budget() / app._metrics_period()   # noqa: SLF001
                    if app._metrics_period() else 0)
        loop_rate = app._loop_metrics_budget()                      # noqa: SLF001
        print(f"\nhz={hz}: app={app_rate:g} loop={loop_rate:g} "
              f"aggregate={app_rate + loop_rate:g}")
        assert app_rate + loop_rate <= hz + 1e-9, (
            f"the partition exceeds the cap at hz={hz}")
    app.metrics_max_hz = 0.0
    assert app._metrics_rate_ok() and app._loop_metrics_rate_ok()   # noqa: SLF001


def test_both_metrics_producers_publish_inside_the_shared_cap():
    """Neither producer starves the other, and their union respects the cap.

    An unpartitioned window starved the loop telemetry to zero (the app-side
    publisher attempts an order of magnitude more often and always won the
    freed slot); this pins the reservation that fixes it.
    """
    app = _started_app(RecordingSink())
    app.metrics_max_hz = 5.0
    window = 2.2
    app_admitted = loop_admitted = 0
    started = time.monotonic()
    next_loop_attempt = started
    while time.monotonic() - started < window:
        if app._metrics_rate_ok():                    # noqa: SLF001
            app_admitted += 1
        now = time.monotonic()
        if now >= next_loop_attempt:
            next_loop_attempt = now + 0.5             # the loop's own cadence
            if app._loop_metrics_rate_ok():           # noqa: SLF001
                loop_admitted += 1
        time.sleep(0.01)
    elapsed = time.monotonic() - started
    print(f"\napp={app_admitted} loop={loop_admitted} in {elapsed:.2f}s "
          f"(cap {app.metrics_max_hz} Hz)")
    assert loop_admitted >= 2, "the loop telemetry was starved"
    assert app_admitted >= 4, "the application's metrics were starved"
    assert app_admitted + loop_admitted <= app.metrics_max_hz * math.ceil(elapsed)

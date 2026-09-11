"""Deterministic loop-budget measurements through the real App frame iterator."""
from __future__ import annotations

import copy

import numpy as np
import pytest

from kit import app as kit_app
from kit.adapters.frame_source import Frame
from kit.adapters.result_sink import ResultSink
from market.appmgr.result_hub import normalize_app_payload


class Clock:
    def __init__(self):
        self.seconds = 100.0

    def monotonic(self):
        return self.seconds

    def advance_ms(self, milliseconds):
        self.seconds += milliseconds / 1000


class TimedSource:
    def __init__(self, clock):
        self.clock = clock

    def frames(self):
        # Warm-up, then three application frames. Waiting is intentionally far
        # more expensive than processing, and must not enter latency_ms.loop.
        for index, delay in enumerate((0, 500, 500, 500)):
            self.clock.advance_ms(delay)
            yield Frame(
                data=np.arange(48, dtype=np.uint8).reshape(4, 4, 3),
                w=4, h=4, fmt="RGB", pts=float(index),
            )

    def close(self):
        pass


class TimedModel:
    def __init__(self, clock, factor):
        self.clock = clock
        self.factor = factor
        self.calls = 0

    def infer(self, _input):
        self.calls += 1
        self.clock.advance_ms(7 * self.factor())
        return []

    def release(self):
        pass


class RecordingSink(ResultSink):
    def __init__(self, clock, factor):
        self.clock = clock
        self.factor = factor
        self.results = []
        self.metrics = []

    def emit(self, payload, _pts):
        self.results.append(copy.deepcopy(payload))
        self.clock.advance_ms(5 * self.factor())

    def emit_meta(self, payload):
        self.metrics.append(copy.deepcopy(payload))
        # A slow telemetry sink cannot inflate this or the next loop budget.
        self.clock.advance_ms(2000)


class TimedApp(kit_app.App):
    id = "loop-metrics-test"
    owns_loop = True

    def __init__(self, clock, *, needs_model, skip_second=False):
        super().__init__()
        self.clock = clock
        self.needs_model = needs_model
        self.skip_second = skip_second
        self.factor = 1
        self.seen = []

    def run(self):
        for frame in self.frames():
            self.factor = int(frame.pts)
            self.seen.append(frame.pts)
            if self.skip_second and self.factor == 2:
                continue
            if self.needs_model:
                prepared = self.pre(frame)
                self.models[0].infer(prepared.data)
            self.clock.advance_ms(11 * self.factor)
            self.emit(results=[{"label": "test"}], ts=frame.pts)
            self.clock.advance_ms(13 * self.factor)  # business work AFTER emit


def run_timed_app(monkeypatch, *, needs_model=True, skip_second=False):
    clock = Clock()
    app = TimedApp(clock, needs_model=needs_model, skip_second=skip_second)
    model = TimedModel(clock, lambda: app.factor)
    sink = RecordingSink(clock, lambda: app.factor)
    monkeypatch.setattr(kit_app, "time", clock)
    monkeypatch.setattr(kit_app, "open_frame_source", lambda **_kw: TimedSource(clock))
    monkeypatch.setattr(app, "_load_model", lambda _path: model)

    def letterbox(data, _size):
        clock.advance_ms(3 * app.factor)
        return data, None

    monkeypatch.setattr(kit_app, "letterbox", letterbox)
    manifest = {
        "id": app.id,
        "models": [{"id": "det", "file": "models/mock.rknn"}] if needs_model else [],
    }
    app.start(app_dir="/test/loop-metrics", manifest=manifest, config={},
              sink=sink, n=3, verbose=False)
    try:
        app.run()
    finally:
        app.finish()
    return app, model, sink


@pytest.mark.parametrize("needs_model", [True, False])
def test_complete_loop_includes_post_emit_work_and_preserves_stage_fields(monkeypatch, needs_model):
    app, model, sink = run_timed_app(monkeypatch, needs_model=needs_model)

    assert app.seen == [1.0, 2.0, 3.0]
    assert model.calls == (4 if needs_model else 0)  # warm-up remains excluded
    assert len(sink.results) == 3
    assert len(sink.metrics) == 2
    for index, (factor, frames) in enumerate(((1.5, 2), (3, 3))):
        metrics = sink.metrics[index]
        assert metrics["type"] == metrics["kind"] == "metrics"
        assert metrics["app"] == app.id
        assert metrics["frames"] == frames
        assert metrics["latency_ms"] == {
            "loop": (39 if needs_model else 29) * factor,
            "pre": (3 if needs_model else 0) * factor,
            "infer": (7 if needs_model else 0) * factor,
            "post": 24 * factor,
            "app": 24 * factor,
            "emit": 5 * factor,
        }

    # The old per-result pipeline field intentionally still stops BEFORE emit:
    # model/pre + 11ms business work, excluding 5ms send and 13ms later work.
    assert [payload["pipeline_ms"] for payload in sink.results] == [
        (21 if needs_model else 11) * factor for factor in (1, 2, 3)
    ]
    assert [payload["inference_time_ms"] for payload in sink.results] == [
        (7 if needs_model else 0) * factor for factor in (1, 2, 3)
    ]
    # FPS keeps its wall-clock definition; it does not become 1000 / loop_ms.
    assert sink.metrics[0]["fps"] == 1.8
    assert sink.metrics[1]["fps"] == 0.4

    # Canonical Result Hub preserves the additive nested field without a new
    # schema, endpoint, or a change to the established latency stage keys.
    identity = {"app_id": app.id, "instance_id": "test-instance", "generation": 1}
    envelope, = normalize_app_payload(sink.metrics[0], identity)
    assert envelope["type"] == "metrics"
    assert envelope["metrics"]["latency_ms"] == sink.metrics[0]["latency_ms"]
    assert envelope["metrics"]["fps"] == sink.metrics[0]["fps"]


def test_app_continue_keeps_the_existing_loop_frame_denominator(monkeypatch):
    app, model, sink = run_timed_app(monkeypatch, skip_second=True)
    assert app.seen == [1.0, 2.0, 3.0]
    assert model.calls == 3  # warm-up plus frames 1 and 3
    assert len(sink.results) == 2
    assert sink.metrics[0]["frames"] == 2
    assert sink.metrics[0]["latency_ms"] == {
        "loop": 19.5, "pre": 1.5, "infer": 3.5,
        "post": 12.0, "app": 12.0, "emit": 2.5,
    }
    assert sink.metrics[1]["latency_ms"]["loop"] == 117.0

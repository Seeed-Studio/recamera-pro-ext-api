"""The run() loop over fake frames and a fake depth model -- no rknn, no camera.

`kit.app.open_frame_source` and `kit.app.App._load_model` are stubbed, the same
seams `apps/face-recognition/tests/test_app_loop.py` uses. The frames arrive the
way the `model_frame = "hw-direct"` source delivers them: `data` IS the 256
letterbox and `model_info` carries the transform back to 1280x720, so `pre()`
takes the no-copy branch and the app is exercised on the geometry it runs on.
"""
import base64
import importlib.util
import json
import os
import struct
import sys
import zlib

import numpy as np
import pytest

from kit import app as kit_app
from kit.adapters.frame_source import Frame
from kit.adapters.result_sink import ResultSink
from kit.runtime.preprocess import LetterboxInfo

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIZE = 256
FRAME_W, FRAME_H = 1280, 720
SCALE = SIZE / FRAME_W                       # 0.2 -> 256x144 content, 56 px bars
PAD_H = int(round((SIZE - FRAME_H * SCALE) / 2 - 0.1))
INFO = LetterboxInfo(scale=SCALE, pad_w=0, pad_h=PAD_H,
                     orig_w=FRAME_W, orig_h=FRAME_H)
N_GREY = 1
N_REAL = 5                  # frames() eats the first real frame as NPU warm-up
N_HANDED = N_REAL - 1
DT = 0.2
TAG = "rv1126b:midas_v21_small_256@fp16"


def _load_app_module():
    path = os.path.join(APP_DIR, "app.py")
    spec = importlib.util.spec_from_file_location("_depth_app_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _letterboxed(i):
    """A 256x256 letterbox: textured content band, grey 114 bars top and bottom."""
    canvas = np.full((SIZE, SIZE, 3), 114, np.uint8)
    yy, xx = np.mgrid[0:SIZE - 2 * PAD_H, 0:SIZE]
    band = np.stack([(xx + i * 7) % 256, yy % 256, (xx + yy) % 256], -1)
    canvas[PAD_H:SIZE - PAD_H] = band.astype(np.uint8)
    return canvas


def _frames():
    for i in range(N_GREY):
        yield Frame(data=np.full((SIZE, SIZE, 3), 128, np.uint8),
                    w=FRAME_W, h=FRAME_H, fmt="RGB", pts=i * DT,
                    model_info=INFO)
    for i in range(N_REAL):
        yield Frame(data=_letterboxed(i), w=FRAME_W, h=FRAME_H, fmt="RGB",
                    pts=(N_GREY + i) * DT, model_info=INFO)


def _ramp_depth():
    """Relative inverse depth growing left to right: the right edge is nearest.

    The grey letterbox bars are given a mid value that would visibly move the
    statistics if the app failed to exclude them -- that is what
    `test_bars_excluded` checks.
    """
    d = np.tile(np.linspace(0.0, 100.0, SIZE, dtype=np.float32), (SIZE, 1))
    d[:PAD_H] = 500.0
    d[SIZE - PAD_H:] = 500.0
    return d[None]


class _FakeDepthModel:
    """Returns whatever `map_fn(call_index)` produces, shaped [1,256,256]."""

    def __init__(self, path, map_fn=None):
        self.path = path
        self.calls = 0
        self.input_shapes = []
        self.released = False
        self.map_fn = map_fn or (lambda k: _ramp_depth())

    def infer(self, x):
        self.input_shapes.append(tuple(np.asarray(x).shape))
        k = self.calls
        self.calls += 1
        return [np.asarray(self.map_fn(k), dtype=np.float32)]

    def release(self):
        self.released = True


class _FakeSource:
    def __init__(self, *a, **kw):
        self.kw = kw
        self.closed = False

    def frames(self):
        return _frames()

    def close(self):
        self.closed = True


class _RecordingSink(ResultSink):
    def __init__(self):
        self.payloads = []
        self.metas = []
        self.frame_sizes = []

    def emit(self, payload, pts):
        # round-trip through JSON: the payload must be wire-serialisable
        self.payloads.append((json.loads(json.dumps(payload)), pts))

    def emit_meta(self, payload):
        self.metas.append(payload)

    def set_frame_size(self, w, h):
        self.frame_sizes.append((w, h))


class _Base:
    @pytest.fixture(autouse=True)
    def harness(self, monkeypatch):
        self.models = []
        self.map_fn = None
        self.sources = []

        def _fake_load(app_self, path):
            m = _FakeDepthModel(path, map_fn=self.map_fn)
            self.models.append(m)
            return m

        def _fake_source(*a, **kw):
            s = _FakeSource(*a, **kw)
            self.sources.append(s)
            return s

        monkeypatch.setattr(kit_app.App, "_load_model", _fake_load)
        monkeypatch.setattr(kit_app, "open_frame_source", _fake_source)
        self.mod = _load_app_module()

    def _run(self, **config):
        from kit import config as kitconfig
        man = kitconfig.load_manifest(APP_DIR)
        cfg = dict(kitconfig.flatten_schema(man))
        cfg.update(config)
        app = self.mod.DepthEstimationApp()
        sink = _RecordingSink()
        app.start(sink=sink, app_dir=APP_DIR, manifest=man, config=cfg,
                  verbose=False)
        try:
            app.run()
        finally:
            app.finish()
        self.sink = sink
        self.app = app
        return sink.payloads


# --------------------------------------------------------------------------- #
class TestLoop(_Base):

    def test_one_payload_per_handed_frame(self):
        payloads = self._run()
        assert len(payloads) == N_HANDED
        # warm-up frame ran the model too, and produced no payload
        assert self.models[0].calls == N_REAL
        assert self.sink.frame_sizes == [(FRAME_W, FRAME_H)] * N_HANDED

    def test_model_gets_raw_uint8_letterbox_pixels(self):
        """MiDaS carries its normalisation in-graph: feed uint8, never /255."""
        self._run()
        m = self.models[0]
        assert m.input_shapes == [(SIZE, SIZE, 3)] * N_REAL
        assert self.sources[0].kw["input_size"] == SIZE
        assert self.sources[0].kw["direct_preprocess"] is True

    def test_model_released_and_source_closed(self):
        self._run()
        assert self.models[0].released and self.sources[0].closed

    def test_payload_envelope(self):
        p, pts = self._run()[0]
        assert pts == pytest.approx((N_GREY + 1) * DT)
        assert p["stream_id"] == "camera-0"
        assert p["model_tag"] == TAG
        assert p["inference_time_ms"] >= 0.0
        assert p["render"] == {"boxes": {"color_by": "label", "line_width": 2}}


class TestDepthBlock(_Base):

    def test_stats_over_the_valid_region_only(self):
        p, _ = self._run()[0]
        d = p["depth"]
        assert d["unit"] == "relative"
        # MiDaS predicts INVERSE depth: larger = nearer. Stated, not inferred.
        assert d["smaller_is_nearer"] is False
        assert d["source_size"] == [FRAME_W, FRAME_H]
        assert d["valid_roi"] == [0.0, 0.0, FRAME_W, FRAME_H]
        # ramp is 0..100 over the content band; the 500.0 bars are excluded
        assert d["min"] == pytest.approx(0.0, abs=0.5)
        assert d["max"] == pytest.approx(100.0, abs=0.5)
        assert d["mean"] == pytest.approx(50.0, abs=0.5)
        assert d["p5"] == pytest.approx(5.0, abs=1.0)
        assert d["p95"] == pytest.approx(95.0, abs=1.0)

    def test_bars_excluded(self):
        """Including the grey bars would drag max/mean toward the 500.0 fill."""
        p, _ = self._run()[0]
        assert p["depth"]["max"] < 200.0
        assert p["depth"]["mean"] < 200.0

    def test_flat_map_is_degenerate_not_a_crash(self):
        self.map_fn = lambda k: np.full((1, SIZE, SIZE), 42.0, np.float32)
        p, _ = self._run()[0]
        assert p["depth"]["p5"] == p["depth"]["p95"] == 42.0
        assert all(v == 0.0 for row in p["grid"] for v in row)
        assert p["nearest"]["value"] == 0.0 and p["nearest"]["near"] == 0.0


class TestGrid(_Base):

    def test_default_grid_is_4x3_and_covers_the_frame(self):
        p, _ = self._run()[0]
        assert p["grid_size"] == [4, 3]
        assert len(p["grid"]) == 3 and all(len(r) == 4 for r in p["grid"])
        assert len(p["results"]) == 12
        xs = sorted({tuple(r["box"][0::2]) for r in p["results"]})
        ys = sorted({tuple(r["box"][1::2]) for r in p["results"]})
        assert xs[0][0] == 0.0 and xs[-1][1] == pytest.approx(FRAME_W, abs=5)
        assert ys[0][0] == 0.0 and ys[-1][1] == pytest.approx(FRAME_H, abs=5)

    def test_grid_values_follow_the_ramp_left_to_right(self):
        p, _ = self._run()[0]
        for row in p["grid"]:
            assert row == sorted(row)
            assert row[0] < row[-1]
        assert p["grid"][0] == p["grid"][1] == p["grid"][2]

    def test_results_carry_near_mid_far_labels_matching_the_score(self):
        p, _ = self._run()[0]
        for r in p["results"]:
            assert r["label"] == r["cls_name"]
            assert r["cls"] == ("far", "mid", "near").index(r["label"])
            expect = ("near" if r["score"] >= 0.66
                      else "mid" if r["score"] >= 0.33 else "far")
            assert r["label"] == expect
        assert {r["label"] for r in p["results"]} == {"far", "mid", "near"}

    def test_grid_shape_is_configurable(self):
        p, _ = self._run(grid_cols=2, grid_rows=5)[0]
        assert p["grid_size"] == [2, 5]
        assert len(p["grid"]) == 5 and all(len(r) == 2 for r in p["grid"])
        assert len(p["results"]) == 10

    def test_nearest_is_the_rightmost_cell_of_the_ramp(self):
        p, _ = self._run()[0]
        near = p["nearest"]
        assert (near["row"], near["col"]) in {(0, 3), (1, 3), (2, 3)}
        # rightmost column: box starts in the last quarter of the frame
        assert near["box"][0] > FRAME_W * 0.7
        assert near["box"][2] == pytest.approx(FRAME_W, abs=5)
        # the winner is picked on `near` (which saturates) but REPORTS its mean
        assert near["near"] == pytest.approx(1.0, abs=0.05)
        assert near["value"] == p["grid"][near["row"]][near["col"]]
        assert near["value"] < near["near"]

    def test_near_percentile_moves_the_selection_metric_not_the_grid(self):
        hi, _ = self._run(near_percentile=95)[0]
        lo, _ = self._run(near_percentile=50)[0]
        assert lo["nearest"]["near"] < hi["nearest"]["near"]
        assert lo["grid"] == hi["grid"]          # grid is the MEAN, unaffected


class TestRois(_Base):

    def test_absent_when_unconfigured(self):
        p, _ = self._run()[0]
        assert "rois" not in p

    def test_planar_ramp_scores_as_a_plane(self):
        """The ramp IS a plane, so planarity must sit at the top of its range."""
        p, _ = self._run(depth_roi='[[0.25,0.25,0.5,0.5]]')[0]
        roi = p["rois"][0]
        assert roi["roi"] == [0.25, 0.25, 0.5, 0.5]
        assert roi["planarity"] == pytest.approx(1.0, abs=1e-3)
        assert roi["score"] == pytest.approx(0.0, abs=1e-3)
        assert roi["relief"] < 1e-3
        assert roi["n_samples"] > 32

    def test_a_bump_inside_the_roi_breaks_planarity(self):
        def bumped(k):
            d = _ramp_depth().copy()
            yy, xx = np.mgrid[0:SIZE, 0:SIZE]
            r2 = (xx - SIZE / 2) ** 2 + (yy - SIZE / 2) ** 2
            d[0] += 60.0 * np.exp(-r2 / (2 * 40.0 ** 2))
            return d
        self.map_fn = bumped
        p, _ = self._run(depth_roi='[[0.25,0.25,0.5,0.5]]')[0]
        roi = p["rois"][0]
        assert roi["planarity"] < 0.5
        assert roi["score"] > 0.5
        assert roi["relief"] > 0.05

    def test_several_rois_are_reported_in_order(self):
        p, _ = self._run(depth_roi='[[0.0,0.0,0.4,0.4],[0.5,0.5,0.4,0.4]]')[0]
        assert [r["roi"][0] for r in p["rois"]] == [0.0, 0.5]

    def test_malformed_roi_config_is_ignored(self):
        p, _ = self._run(depth_roi="nonsense")[0]
        assert "rois" not in p
        assert p["grid"]                      # the frame still publishes


class TestPublishMap(_Base):

    def test_absent_by_default(self):
        p, _ = self._run()[0]
        assert "depth_map" not in p

    def test_png_is_decodable_and_matches_the_ramp(self):
        p, _ = self._run(publish_map=True)[0]
        m = p["depth_map"]
        assert (m["w"], m["h"], m["format"], m["encoding"]) == \
            (64, 48, "png", "base64")
        blob = base64.b64decode(m["data"])
        assert blob[:8] == b"\x89PNG\r\n\x1a\n"
        (w, h, depth, ctype) = struct.unpack(">IIBBBBB", blob[16:29])[:4]
        assert (w, h, depth, ctype) == (64, 48, 8, 0)
        # first IDAT starts right after the IHDR chunk (8+25 bytes)
        pos, idat = 8, b""
        while pos < len(blob):
            (ln,) = struct.unpack(">I", blob[pos:pos + 4])
            if blob[pos + 4:pos + 8] == b"IDAT":
                idat += blob[pos + 8:pos + 8 + ln]
            pos += 12 + ln
        raw = zlib.decompress(idat)
        row0 = list(raw[1:65])
        assert row0 == sorted(row0) and row0[0] == 0 and row0[-1] == 255


class TestEmitInterval(_Base):

    def test_interval_1_publishes_every_frame(self):
        assert len(self._run(emit_interval=1)) == N_HANDED

    def test_interval_2_skips_the_inference_too(self):
        payloads = self._run(emit_interval=2)
        assert len(payloads) == N_HANDED // 2
        # warm-up (1) + the frames actually processed -- the skipped frames
        # never reach the NPU at all
        assert self.models[0].calls == 1 + len(payloads)
        deltas = {round(b - a, 3)
                  for (_, a), (_, b) in zip(payloads, payloads[1:])}
        assert deltas == {round(2 * DT, 3)} or not deltas


class TestDepthEvent(_Base):
    """The `depth` event is what MQTT mappings and HA read (extra is WS-only)."""

    def test_one_depth_event_per_frame_restating_extra(self):
        p, _ = self._run()[0]
        assert len(p["events"]) == 1
        ev = p["events"][0]
        assert ev["kind"] == "depth"
        assert ev["nearest_value"] == p["nearest"]["value"]
        assert ev["nearest_near"] == p["nearest"]["near"]
        assert ev["nearest_box"] == p["nearest"]["box"]
        assert (ev["nearest_row"], ev["nearest_col"]) == (p["nearest"]["row"],
                                                          p["nearest"]["col"])
        assert ev["grid"] == p["grid"]
        assert ev["grid_size"] == p["grid_size"]
        for k in ("min", "max", "mean", "p5", "p95", "smaller_is_nearer"):
            assert ev[k] == p["depth"][k]

    def test_event_scalars_reach_the_home_assistant_state(self):
        from kit.adapters.mqtt_sink import MqttSink
        p, _ = self._run()[0]
        st = MqttSink._build_state("depth-estimation", 1, 1.5,
                                   p["results"], p["events"])
        # what apps/depth-estimation/manifest.json ha_entities template on
        assert st["summary"]["nearest_value"] == p["nearest"]["value"]
        assert st["summary"]["mean"] == p["depth"]["mean"]
        assert set(st["class_counts"]) <= {"near", "mid", "far"}

    def test_default_mapping_renders_valid_json(self):
        from kit.adapters.output_sink import (ConfigurableSink, Jinja2Formatter,
                                              generate_mapping_templates)
        from kit.adapters.result_sink import OutputChannel
        from kit import config as kitconfig

        class Rec(OutputChannel):
            name = "mqtt"

            def __init__(self):
                self.msgs = []

            def publish(self, msg):
                self.msgs.append(msg)

        man = kitconfig.load_manifest(APP_DIR)
        rows = man["output"]["default_mapping"]
        rec = Rec()
        sink = ConfigurableSink(
            app_id="depth-estimation", channels=[rec],
            formatter=Jinja2Formatter(generate_mapping_templates(rows),
                                      app_id="depth-estimation",
                                      device_id="dev-test"))
        sink.set_frame_size(FRAME_W, FRAME_H)
        p, _ = self._run()[0]
        sink.emit({"results": p["results"], "events": p["events"]}, 1.5)
        bodies = {m.topic: json.loads(m.body.decode()) for m in rec.msgs}
        assert set(bodies) == {"recamera/depth-estimation/depth",
                               "recamera/depth-estimation/zones"}
        d = bodies["recamera/depth-estimation/depth"]
        assert d["nearest_depth"] == p["nearest"]["value"]
        assert d["grid"] == p["grid"]
        assert len(bodies["recamera/depth-estimation/zones"]["zones"]) == 12

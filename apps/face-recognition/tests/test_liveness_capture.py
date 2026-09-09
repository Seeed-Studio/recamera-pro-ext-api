"""The `liveness_capture` calibration command: rows, auto-stop, validation.

Reuses the fake-model loop harness from test_app_loop, so a capture is exercised
end to end (HTTP-free) against scripted detections and scripted model outputs.
"""
import json
import os
import time
from concurrent.futures import Future

import pytest

from cmd_server import CmdServer
from test_app_loop import (N_EMITTED, _Base, _RecordingSink,  # noqa: E402
                           _load_app_module)

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIELDS = {"P_tex_v2", "P_tex_v1se", "motion_residual", "correlation", "EAR",
          "blink", "face_px_size", "track_id", "label", "ts"}


class _CaptureBase(_Base):
    def run_with_capture(self, label="real", seconds=30.0, **cfg):
        """Arm a capture between start() and run(), then drive the loop."""
        cfg.setdefault("cmd_port", 0)
        cfg.setdefault("min_face_px", 64)
        sink = _RecordingSink()
        app = _load_app_module().FaceRecognitionApp()
        app.start("models/scrfd500m_640_fp16.rknn", source="ffmpeg", sink=sink,
                  n=0, verbose=False, app_dir=APP_DIR, manifest=self.manifest,
                  config=dict(cfg))
        # The install dir is read-only in CI and is the repo here; a capture
        # must not litter it.
        app._capture_dir = str(self.tmp_path)
        self.arm = app.start_liveness_capture(label, seconds)
        self.fut: Future = self.arm["future"]
        try:
            app.run()
        finally:
            app.finish()
        self.rows = self.read_rows(self.arm["path"])
        return sink, app

    @staticmethod
    def read_rows(path):
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]


class TestCaptureRows(_CaptureBase):
    def test_one_row_per_eligible_face_per_frame(self):
        """The 32 px face is gated, so it is not a calibration sample."""
        self.enroll(alice=0)
        _sink, _app = self.run_with_capture()
        assert len(self.rows) == N_EMITTED
        assert {r["track_id"] for r in self.rows} == {self.rows[0]["track_id"]}

    def test_every_row_has_exactly_the_documented_fields(self):
        self.enroll(alice=0)
        self.run_with_capture(label="screen")
        for r in self.rows:
            assert set(r) == FIELDS
            assert r["label"] == "screen"
            assert r["face_px_size"] == pytest.approx(128.0)
            assert 0.0 <= r["P_tex_v2"] <= 1.0
            assert 0.0 <= r["P_tex_v1se"] <= 1.0
            assert isinstance(r["blink"], bool)

    def test_rows_carry_no_pixels_no_embedding_and_no_name(self):
        """★A calibration set must be safe to move off the device★ -- it holds
        model outputs and box geometry, nothing that identifies the person who
        recorded it."""
        self.enroll(alice=0)
        self.run_with_capture(liveness_enabled=True, liveness_timeout_sec=0.0,
                              embed_interval=1)
        blob = json.dumps(self.rows)
        assert "alice" not in blob
        for r in self.rows:
            assert not any(isinstance(v, list) for v in r.values())
            assert "name" not in r and "image" not in r and "embedding" not in r

    def test_capture_forces_both_texture_heads_every_frame(self):
        """★Capture bias★ -- reusing the cached value from the last embedding
        frame would fill the file with duplicated rows and overstate the ROC."""
        self.enroll(alice=0)
        self.run_with_capture(embed_interval=99)
        assert [m.calls for m in self.live] == [N_EMITTED, N_EMITTED]
        v2 = [r["P_tex_v2"] for r in self.rows]
        assert all(p is not None for p in v2)

    def test_the_two_heads_are_recorded_separately_not_averaged(self):
        self.p_fn = lambda k: 0.90
        self.p_v1se_fn = lambda k: 0.20
        self.enroll(alice=0)
        self.run_with_capture()
        for r in self.rows:
            assert r["P_tex_v2"] == pytest.approx(0.90, abs=1e-3)
            assert r["P_tex_v1se"] == pytest.approx(0.20, abs=1e-3)

    def test_ear_is_null_on_frames_where_facemesh_was_skipped(self):
        self.enroll(alice=0)
        self.run_with_capture(liveness_facemesh_interval=2)
        ears = [r["EAR"] for r in self.rows]
        assert None in ears
        assert any(e is not None for e in ears)

    def test_motion_fields_are_null_until_the_window_fills(self):
        self.enroll(alice=0)
        self.run_with_capture()
        assert self.rows[0]["motion_residual"] is None
        assert self.rows[0]["correlation"] is None

    def test_capture_runs_even_with_liveness_disabled(self):
        """Calibrating the thresholds is exactly what you do BEFORE turning the
        feature on."""
        self.enroll(alice=0)
        _sink, _app = self.run_with_capture(liveness_enabled=False)
        assert len(self.rows) == N_EMITTED

    def test_capture_does_not_publish_a_liveness_verdict_when_disabled(self):
        self.enroll(alice=0)
        sink, _app = self.run_with_capture(liveness_enabled=False)
        face = sink.payloads[-1][0]["faces"][0]
        assert face["live"] is None
        assert face["name"] == "alice"          # recognition is untouched


class TestCaptureLifecycle(_CaptureBase):
    def test_the_deadline_stops_the_capture_and_acks_the_row_count(self):
        self.enroll(alice=0)
        _sink, app = self.run_with_capture(seconds=1e-6)
        assert self.fut.done()
        res = self.fut.result(timeout=0)
        assert res["rows"] == 1                 # stopped after the first frame
        assert res["path"] == self.arm["path"]
        assert res["label"] == "real"
        assert app._capture is None
        assert len(self.rows) == 1

    def test_finishing_the_app_closes_an_unexpired_capture(self):
        self.enroll(alice=0)
        _sink, app = self.run_with_capture(seconds=60.0)
        assert app._capture is None
        assert self.fut.done()
        assert self.fut.result(timeout=0)["rows"] == N_EMITTED

    def test_rows_append_across_captures_rather_than_truncating(self):
        self.enroll(alice=0)
        self.run_with_capture(label="real")
        first = len(self.rows)
        self.run_with_capture(label="print")
        assert len(self.rows) == 2 * first
        assert {r["label"] for r in self.rows} == {"real", "print"}

    def test_a_second_capture_is_refused_while_one_is_running(self):
        app = _load_app_module().FaceRecognitionApp()
        app._capture_dir = str(self.tmp_path)
        app.liveness_capture_max_sec = 60
        app.start_liveness_capture("real", 5)
        with pytest.raises(ValueError):
            app.start_liveness_capture("real", 5)
        app._stop_capture()


class _Ops:
    """Minimal CmdServer.ops for the validation tests."""
    model_tag = "tag"

    def __init__(self, app):
        self.app = app

    def user_count(self):
        return 0

    def start_liveness_capture(self, label, seconds):
        return self.app.start_liveness_capture(label, seconds)


class TestCaptureCommand:
    """Validation of the command itself -- no frame loop, no models."""

    @pytest.fixture(autouse=True)
    def _app(self, tmp_path):
        self.tmp_path = tmp_path
        self.app = _load_app_module().FaceRecognitionApp()
        self.app._capture_dir = str(tmp_path)
        self.app.liveness_capture_max_sec = 60
        self.server = CmdServer(-1, _Ops(self.app))
        yield
        self.app._stop_capture()

    def test_the_op_is_accepted(self):
        assert "liveness_capture" in __import__("cmd_server").VALID_OPS

    @pytest.mark.parametrize("label", ["", "mask", None, 7])
    def test_a_bad_label_is_a_400_not_a_crash(self, label):
        status, body = self.server.handle({"op": "liveness_capture",
                                           "label": label, "seconds": 5})
        assert status == 400
        assert body["ok"] is False
        assert "label" in body["err"]

    @pytest.mark.parametrize("seconds", [0, -1, 61, 10 ** 9, "soon", None])
    def test_an_out_of_range_duration_is_a_400(self, seconds):
        status, body = self.server.handle({"op": "liveness_capture",
                                           "label": "real", "seconds": seconds})
        assert status == 400
        assert body["ok"] is False
        assert "seconds" in body["err"]

    def test_the_label_is_case_and_whitespace_normalised(self):
        arm = self.app.start_liveness_capture("  Print ", 5)
        assert arm["label"] == "print"

    def test_a_capture_nobody_runs_times_out_instead_of_hanging(self):
        """No frame loop is driving the app here, so the deadline never fires;
        the handler must give up rather than block a socket forever."""
        import cmd_server as cs
        old, cs.CAPTURE_GRACE = cs.CAPTURE_GRACE, 0.05
        try:
            t0 = time.monotonic()
            status, body = self.server.handle({"op": "liveness_capture",
                                               "label": "real", "seconds": 0.01})
            assert status == 504
            assert time.monotonic() - t0 < 5.0
            assert body["path"].endswith("liveness_capture.jsonl")
        finally:
            cs.CAPTURE_GRACE = old

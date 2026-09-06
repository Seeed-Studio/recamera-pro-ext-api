"""The run() loop over fake frames and fake models -- no rknn, no camera.

`kit.app.open_frame_source` and `kit.app.App._load_model` are stubbed, the same
seams `kit/tests/test_face_shape_equivalence.py` uses. The fixture scripts two
faces per frame: a 128 px one (above the 64 px gate) and a 32 px one (below it),
so gating, tracking and the emitted payload are all observable.
"""
import importlib.util
import json
import os
import signal
import sys

import numpy as np
import pytest

from kit import app as kit_app
from kit.adapters.frame_source import Frame
from kit.adapters.result_sink import ResultSink

import gallery as gallery_mod
from test_scrfd import blank_outputs, flatten, plant   # noqa: E402  (fixture reuse)

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRAME_W, FRAME_H = 640, 480
DET_SIZE = 640
N_GREY = 1
N_REAL = 4          # frames() eats the first real frame as the NPU warm-up
N_EMITTED = N_REAL - 1
DT = 0.2
TAG = "rv1126b:scrfd500m+mbf512@fp16"

# stride-32 grid cells for the two scripted faces (see test_scrfd.plant)
BIG = dict(col=4, row=6, half_cells=2.0)      # 128 px box -> above the gate
SMALL = dict(col=14, row=6, half_cells=0.5)   # 32 px box  -> below the gate


def _load_app_module():
    path = os.path.join(APP_DIR, "app.py")
    spec = importlib.util.spec_from_file_location("_face_reco_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _frames():
    """N_GREY flat grey warm-up placeholders, then N_REAL textured frames."""
    for i in range(N_GREY):
        yield Frame(data=np.full((FRAME_H, FRAME_W, 3), 128, np.uint8),
                    w=FRAME_W, h=FRAME_H, fmt="RGB", pts=i * DT)
    yy, xx = np.mgrid[0:FRAME_H, 0:FRAME_W]
    for i in range(N_REAL):
        img = np.stack([(xx + i * 7) % 256, yy % 256, (xx + yy) % 256], -1)
        yield Frame(data=img.astype(np.uint8), w=FRAME_W, h=FRAME_H,
                    fmt="RGB", pts=(N_GREY + i) * DT)


class _FakeSource:
    def __init__(self, *a, frames_fn=None, **kw):
        self.kw = kw
        self.closed = False
        self.frames_fn = frames_fn or _frames

    def frames(self):
        return self.frames_fn()

    def close(self):
        self.closed = True


class _FakeScrfd:
    """Nine SCRFD tensors per call; which faces fire comes from `faces_fn`."""

    def __init__(self, path, faces_fn=None):
        self.path = path
        self.calls = 0
        self.input_shapes = []
        self.released = False
        self.faces_fn = faces_fn or (lambda k: [(BIG, 0.9), (SMALL, 0.8)])

    def infer(self, x):
        self.input_shapes.append(tuple(np.asarray(x).shape))
        k = self.calls
        self.calls += 1
        blocks = blank_outputs()
        for cell, score in self.faces_fn(k):
            plant(blocks, 32, score=score, **cell)
        return flatten(blocks, seed=k)

    def release(self):
        self.released = True


class _FakeArcface:
    """(1,512) embedding per call; `vec_fn(k, chip)` picks which one."""

    def __init__(self, path, vec_fn=None):
        self.path = path
        self.calls = 0
        self.input_shapes = []
        self.released = False
        self.vec_fn = vec_fn or (lambda k, chip: 0)

    def infer(self, x):
        self.input_shapes.append(tuple(np.asarray(x).shape))
        k = self.calls
        self.calls += 1
        vec = np.zeros((1, 512), dtype=np.float32)
        vec[0, int(self.vec_fn(k, x)) % 512] = 4.0     # unnormalized on purpose
        return [vec]

    def release(self):
        self.released = True


class _FakeLiveness:
    """(1,3) logits per call; `p_fn(k)` is the intended P(real)."""

    def __init__(self, path, p_fn=None):
        self.path = path
        self.calls = 0
        self.input_shapes = []
        self.released = False
        self.p_fn = p_fn or (lambda k: 0.99)

    def infer(self, x):
        self.input_shapes.append(tuple(np.asarray(x).shape))
        k = self.calls
        self.calls += 1
        p = float(self.p_fn(k))
        # two-class in log space, third kept far away
        logits = np.array([[np.log(max(1e-6, 1.0 - p)), np.log(max(1e-6, p)),
                            -20.0]], dtype=np.float32)
        return [logits]

    def release(self):
        self.released = True


class _FakeFacemesh:
    """1404 landmark values + a presence logit; `ear_fn(k)` picks the EAR.

    Only the twelve eye indices matter to the app, so the rest of the mesh is a
    constant blob. The six-point EAR is scale-free and the ROI map only rescales
    the points, so writing them in ROI pixel space is enough.
    """

    def __init__(self, path, ear_fn=None):
        self.path = path
        self.calls = 0
        self.input_shapes = []
        self.released = False
        self.ear_fn = ear_fn or (lambda k: 0.30)

    @staticmethod
    def _eye(pts, idx, cx, cy, ear):
        # EAR = (|p1-p5| + |p2-p4|) / (2*|p0-p3|); with a 10 px width and both
        # vertical pairs at +-h, EAR == 0.2*h.
        h = 5.0 * float(ear)
        xy = [(-5.0, 0.0), (-2.0, h), (2.0, h), (5.0, 0.0), (2.0, -h),
              (-2.0, -h)]
        for k, (dx, dy) in zip(idx, xy):
            pts[k] = (cx + dx, cy + dy, 0.0)

    def infer(self, x):
        self.input_shapes.append(tuple(np.asarray(x).shape))
        k = self.calls
        self.calls += 1
        ear = float(self.ear_fn(k))
        pts = np.full((468, 3), 96.0, dtype=np.float32)
        self._eye(pts, (33, 160, 158, 133, 153, 144), 70.0, 80.0, ear)
        self._eye(pts, (362, 385, 387, 263, 373, 380), 120.0, 80.0, ear)
        return [pts.reshape(1, 1, 1, 1404),
                np.array([[5.0]], dtype=np.float32)]

    def release(self):
        self.released = True


class _RecordingSink(ResultSink):
    def __init__(self):
        self.payloads = []
        self.metas = []
        self.frame_sizes = []

    def emit(self, payload, pts):
        self.payloads.append((json.loads(json.dumps(payload)), pts))

    def emit_meta(self, payload):
        self.metas.append(payload)

    def set_frame_size(self, w, h):
        self.frame_sizes.append((w, h))


class _Base:
    @pytest.fixture(autouse=True)
    def harness(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FACE_GALLERY_DIR", str(tmp_path))
        self.tmp_path = tmp_path
        self.det, self.emb, self.live, self.mesh = [], [], [], []
        self.frames_fn = _frames
        self.faces_fn = None
        self.vec_fn = None
        self.p_fn = None
        self.p_v1se_fn = None
        self.ear_fn = None

        def _fake_load(app_self, path):
            base = os.path.basename(path)
            if "arcface" in base:
                m = _FakeArcface(path, vec_fn=self.vec_fn)
                self.emb.append(m)
            elif "v1se" in base:
                m = _FakeLiveness(path, p_fn=self.p_v1se_fn or self.p_fn)
                self.live.append(m)
            elif "liveness" in base:
                m = _FakeLiveness(path, p_fn=self.p_fn)
                self.live.append(m)
            elif "landmark" in base:
                m = _FakeFacemesh(path, ear_fn=self.ear_fn)
                self.mesh.append(m)
            else:
                m = _FakeScrfd(path, faces_fn=self.faces_fn)
                self.det.append(m)
            return m

        monkeypatch.setattr(kit_app, "open_frame_source",
                            lambda *a, **kw: _FakeSource(*a, frames_fn=self.frames_fn,
                                                         **kw))
        monkeypatch.setattr(kit_app.App, "_load_model", _fake_load)
        with open(os.path.join(APP_DIR, "manifest.json"), encoding="utf-8") as f:
            self.manifest = json.load(f)
        yield
        try:
            signal.signal(signal.SIGHUP, signal.SIG_DFL)
        except (ValueError, OSError):
            pass

    # -- helpers -------------------------------------------------------- #
    def gallery_path(self):
        return str(self.tmp_path / f"{gallery_mod.sanitize_tag(TAG)}.json")

    def enroll(self, **people):
        """people: name -> basis index the fake embedder will return."""
        g = gallery_mod.Gallery(self.gallery_path(), TAG, dim=512).load()
        for name, idx in people.items():
            v = np.zeros(512, dtype=np.float32)
            v[idx] = 1.0
            g.enroll(name, [v])
        return g

    def run_app(self, **cfg):
        cfg.setdefault("cmd_port", 0)          # do not bind a socket in tests
        sink = _RecordingSink()
        app = _load_app_module().FaceRecognitionApp()
        app.start("models/scrfd500m_640_fp16.rknn", source="ffmpeg", sink=sink,
                  n=0, verbose=False, app_dir=APP_DIR, manifest=self.manifest,
                  config=dict(cfg))
        try:
            app.run()
        finally:
            app.finish()
        return sink, app


class TestLoopContract(_Base):
    def test_three_frames_produce_three_payloads(self):
        sink, app = self.run_app()
        assert len(sink.payloads) == N_EMITTED
        assert [pts for _, pts in sink.payloads] == \
            pytest.approx([(N_GREY + i) * DT for i in range(1, N_REAL)])
        assert sink.frame_sizes == [(FRAME_W, FRAME_H)] * N_EMITTED

    def test_detector_sees_a_640_letterbox_and_runs_once_per_frame(self):
        _sink, _app = self.run_app()
        det = self.det[-1]
        assert det.calls == N_REAL          # warm-up frame included
        assert set(det.input_shapes) == {(DET_SIZE, DET_SIZE, 3)}

    def test_embedder_sees_a_112_chip_from_the_ORIGINAL_frame(self):
        """★model_frame='hw'★ alignment is a landmark-driven warp, so it must
        read full-resolution camera pixels, never the stage-1 letterbox."""
        _sink, _app = self.run_app()
        assert set(self.emb[-1].input_shapes) == {(112, 112, 3)}

    def test_cmd_server_is_not_bound_when_the_port_is_disabled(self):
        _sink, app = self.run_app(cmd_port=0)
        assert app._server is None          # finish() tore it down

    def test_models_are_released(self):
        self.run_app()
        assert self.det[-1].released and self.emb[-1].released


class TestPayload(_Base):
    def test_results_carry_pixel_boxes_label_score_and_cls(self):
        self.enroll(alice=0)
        sink, _app = self.run_app()
        payload, _ = sink.payloads[-1]
        res = payload["results"]
        assert len(res) == 2
        big = res[0]
        assert set(big) == {"box", "label", "score", "cls"}
        assert big["cls"] == 0
        # stride-32 cell (4,6), half-side 2*32 px, letterbox pad_h = 80
        assert big["box"] == pytest.approx([64.0, 48.0, 192.0, 176.0])
        assert big["label"] == "alice"
        assert big["score"] == pytest.approx(1.0, abs=1e-5)

    def test_extra_carries_the_model_tag_and_the_faces_table(self):
        self.enroll(alice=0)
        sink, _app = self.run_app()
        payload, _ = sink.payloads[-1]
        assert payload["model_tag"] == TAG
        assert payload["enrolled"] == 1
        faces = payload["faces"]
        assert len(faces) == 2
        assert set(faces[0]) == {"track_id", "bbox", "det_score", "name",
                                 "score", "live", "liveness_score", "stable",
                                 "gated", "reason", "liveness"}
        assert faces[0]["bbox"] == pytest.approx(
            [64.0 / 640, 48.0 / 480, 192.0 / 640, 176.0 / 480])
        assert faces[0]["det_score"] == pytest.approx(0.9)

    def test_an_unloadable_gallery_reports_the_reason_instead_of_crashing(self):
        """A model_tag mismatch must be visible in the stream, not silent."""
        foreign = gallery_mod.Gallery(self.gallery_path(),
                                      "rk3588:other@int8", dim=512).load()
        v = np.zeros(512, dtype=np.float32)
        v[0] = 1.0
        foreign.enroll("alice", [v])
        sink, _app = self.run_app()
        payload, _ = sink.payloads[-1]
        assert "gallery_error" in payload
        assert payload["enrolled"] == 0
        assert payload["results"][0]["label"] == "unknown"


class TestRecognitionBehaviour(_Base):
    def test_an_unenrolled_face_is_unknown_not_the_nearest_person(self):
        self.enroll(alice=7)                # embedder returns basis 0
        sink, _app = self.run_app()
        payload, _ = sink.payloads[-1]
        assert payload["results"][0]["label"] == "unknown"
        assert payload["faces"][0]["name"] is None

    def test_a_face_below_min_face_px_is_gated_and_costs_no_embedding(self):
        """★Gate before embed★ -- 32 px upsampled to 112 is artefact, and a
        garbage embedding does not fail, it matches somebody."""
        self.enroll(alice=0)
        sink, _app = self.run_app(min_face_px=64, embed_interval=1)
        small = sink.payloads[-1][0]["faces"][1]
        assert small["gated"] is True
        assert small["name"] is None
        assert small["stable"] is False
        # exactly one embedding per frame: the big face only
        assert self.emb[-1].calls == N_EMITTED

    def test_lowering_the_gate_lets_the_small_face_through(self):
        self.enroll(alice=0)
        sink, _app = self.run_app(min_face_px=8, embed_interval=1)
        assert sink.payloads[-1][0]["faces"][1]["gated"] is False
        assert self.emb[-1].calls == 2 * N_EMITTED

    def test_track_ids_are_stable_across_frames(self):
        self.enroll(alice=0)
        sink, _app = self.run_app()
        ids = [tuple(f["track_id"] for f in p["faces"])
               for p, _ in sink.payloads]
        assert len(set(ids)) == 1
        assert all(i is not None for i in ids[0])

    def test_embed_interval_staggers_the_re_embedding(self):
        """One embedding on the track's first frame, none while the interval
        has not elapsed -- and the verdict carries over in between."""
        self.enroll(alice=0)
        sink, _app = self.run_app(min_face_px=64, embed_interval=99)
        assert self.emb[-1].calls == 1
        assert [p["results"][0]["label"] for p, _ in sink.payloads] == \
            ["alice"] * N_EMITTED

    def test_stable_turns_true_only_after_min_track_frames_of_evidence(self):
        self.enroll(alice=0)
        sink, _app = self.run_app(min_face_px=64, embed_interval=1,
                                  min_track_frames=3)
        flags = [p["faces"][0]["stable"] for p, _ in sink.payloads]
        assert flags == [False, False, True]

    def test_the_verdict_is_a_vote_not_the_last_frame(self):
        """Two frames say alice, one says bob -> alice, even on the bob frame."""
        self.enroll(alice=0, bob=1)
        self.vec_fn = lambda k, chip: (1 if k == 2 else 0)   # 3rd embed = bob
        sink, _app = self.run_app(min_face_px=64, embed_interval=1)
        labels = [p["results"][0]["label"] for p, _ in sink.payloads]
        assert labels == ["alice", "alice", "alice"]


class TestLiveness(_Base):
    """Liveness v2: two-model texture ensemble, motion, blink, fusion.

    The scripted faces never move (the SCRFD fixture plants the same cell every
    frame), so the motion term is always at the noise floor and every test that
    wants a verdict inside three frames drives `liveness_timeout_sec=0` -- which
    is exactly the "still face at timeout" path.
    """

    LIVE = dict(min_face_px=64, embed_interval=1, liveness_enabled=True,
                liveness_timeout_sec=0.0)

    def test_disabled_by_default_and_no_liveness_model_is_run(self):
        self.enroll(alice=0)
        sink, _app = self.run_app(min_face_px=64, embed_interval=1)
        assert [m.calls for m in self.live] == [0, 0]
        assert self.mesh[-1].calls == 0
        face = sink.payloads[-1][0]["faces"][0]
        assert face["live"] is None
        assert face["liveness"] is None

    def test_enabled_runs_BOTH_texture_heads_on_80x80_bgr_crops(self):
        """★The ensemble is two crops, not one★ -- 2.7x and 4.0x of the same
        box, each cut from the frame, each seen by its own head."""
        self.enroll(alice=0)
        sink, _app = self.run_app(**self.LIVE)
        assert len(self.live) == 2
        assert [m.calls for m in self.live] == [N_EMITTED, N_EMITTED]
        for m in self.live:
            assert set(m.input_shapes) == {(80, 80, 3)}
        face = sink.payloads[-1][0]["faces"][0]
        assert face["live"] is True
        assert face["name"] == "alice"

    def test_the_two_heads_are_averaged_into_the_texture_score(self):
        self.p_fn = lambda k: 0.90
        self.p_v1se_fn = lambda k: 0.50
        self.enroll(alice=0)
        sink, _app = self.run_app(**self.LIVE)
        face = sink.payloads[-1][0]["faces"][0]
        assert face["liveness"]["texture"] == pytest.approx(0.70, abs=1e-3)

    def test_the_legacy_fields_survive_and_carry_the_texture_ema(self):
        """★Old consumers★ `live` stays a nullable bool and `liveness_score`
        stays a probability -- the TEXTURE EMA, not the fused score, which mixes
        in motion and would silently change meaning."""
        self.enroll(alice=0)
        sink, _app = self.run_app(**self.LIVE)
        face = sink.payloads[-1][0]["faces"][0]
        assert face["live"] is True
        assert face["liveness_score"] == pytest.approx(0.99, abs=1e-3)
        assert face["liveness"]["texture"] == pytest.approx(
            face["liveness_score"])

    def test_the_nested_liveness_object_has_the_documented_shape(self):
        self.enroll(alice=0)
        sink, _app = self.run_app(**self.LIVE)
        lv = sink.payloads[-1][0]["faces"][0]["liveness"]
        assert set(lv) == {"score", "texture", "motion", "blink", "decision",
                           "reason"}
        assert lv["decision"] == "live"
        assert lv["blink"] is False
        assert lv["motion"] is None          # a static face is not motion

    def test_a_pending_verdict_withholds_the_identity(self):
        """★Pitfall: identity leakage while pending★ a name published before the
        verdict settles cannot be un-published by the verdict."""
        self.enroll(alice=0)
        sink, _app = self.run_app(min_face_px=64, embed_interval=1,
                                  liveness_enabled=True,
                                  liveness_timeout_sec=99.0)
        for payload, _ in sink.payloads:
            face = payload["faces"][0]
            assert face["liveness"]["decision"] == "pending"
            assert face["name"] is None
            assert payload["results"][0]["label"] == "unknown"
        assert self.emb[-1].calls == 0       # and no embedding was spent

    def test_a_still_face_is_admitted_once_the_timeout_elapses(self):
        """Motion is DROPPED, not scored as zero: standing still is not a
        spoof."""
        self.enroll(alice=0)
        sink, _app = self.run_app(**self.LIVE)
        lv = sink.payloads[-1][0]["faces"][0]["liveness"]
        assert lv["decision"] == "live"
        assert lv["reason"] == "timeout_texture"

    def test_a_blink_overrides_a_bad_texture_score(self):
        self.enroll(alice=0)
        self.p_fn = lambda k: 0.10
        self.ear_fn = lambda k: (0.10 if k == 1 else 0.30)
        sink, _app = self.run_app(liveness_facemesh_interval=1, **self.LIVE)
        face = sink.payloads[-1][0]["faces"][0]
        assert self.mesh[-1].calls == N_EMITTED
        assert set(self.mesh[-1].input_shapes) == {(192, 192, 3)}
        assert face["liveness"]["blink"] is True
        assert face["liveness"]["reason"] == "blink"
        assert face["live"] is True
        assert face["name"] == "alice"

    def test_facemesh_is_sampled_not_run_every_frame(self):
        self.enroll(alice=0)
        self.run_app(liveness_facemesh_interval=2, **self.LIVE)
        # three tracked frames, every second one sampled
        assert self.mesh[-1].calls == 1

    def test_a_spoof_gets_no_name_and_no_embedding_is_wasted_on_it(self):
        """★A spoof must not vote★ -- annotating it while still counting the
        embedding would let a phone screen accumulate an identity."""
        self.enroll(alice=0)
        self.p_fn = lambda k: 0.01
        sink, _app = self.run_app(**self.LIVE)
        payload, _ = sink.payloads[-1]
        face = payload["faces"][0]
        assert face["live"] is False
        assert face["reason"] == "spoof"
        assert face["liveness"]["decision"] == "spoof"
        assert face["name"] is None
        assert payload["results"][0]["label"] == "unknown"
        assert self.emb[-1].calls == 0       # never reached the embedder

    def test_a_spoof_frame_wipes_the_evidence_a_live_frame_built(self):
        self.enroll(alice=0)
        self.p_fn = lambda k: (0.99 if k == 0 else 0.01)
        sink, _app = self.run_app(liveness_min_samples=1,
                                  liveness_texture_ema_alpha=1.0, **self.LIVE)
        labels = [p["results"][0]["label"] for p, _ in sink.payloads]
        assert labels == ["alice", "unknown", "unknown"]

    def test_track_retirement_drops_the_liveness_state(self):
        """A new person standing where the last one stood must not inherit a
        blink that was never theirs."""
        self.faces_fn = lambda k: ([(BIG, 0.9)] if k < 2 else [])
        sink, app = self.run_app(track_max_lost=1, **self.LIVE)
        assert app._states == {}


class TestEnrollmentThroughTheLoop(_Base):
    def test_camera_enrollment_collects_one_embedding_per_frame(self):
        """★Enrollment runs in the loop, not on the HTTP thread★: the NPU is
        single-core and 'camera' needs frames only the loop has."""
        sink, app = self.run_app_with_enroll(
            {"name": "carol", "source": "camera", "frames": 2})
        assert self.result["frames"] == 2
        assert self.result["name"] == "carol"
        assert [u["name"] for u in app.list_users()] == ["carol"]
        assert sink.payloads[-1][0]["enrolled"] == 1

    def test_camera_enrollment_ignores_embed_interval(self):
        _sink, _app = self.run_app_with_enroll(
            {"name": "carol", "source": "camera", "frames": 3},
            embed_interval=99)
        assert self.result["frames"] == 3

    def test_the_enrolled_template_is_what_recognition_then_matches(self):
        _sink, app = self.run_app_with_enroll(
            {"name": "carol", "source": "camera", "frames": 1})
        v = np.zeros(512, dtype=np.float32)
        v[0] = 1.0
        assert app.gallery.match(v)[0] == "carol"

    def test_enrollment_picks_the_big_centred_face_not_the_small_one(self):
        """The 32 px face at the frame edge must not become someone's template."""
        _sink, app = self.run_app_with_enroll(
            {"name": "carol", "source": "camera", "frames": 1})
        chip_shapes = set(self.emb[-1].input_shapes)
        assert chip_shapes == {(112, 112, 3)}
        assert self.emb[-1].calls >= 1
        assert len(app.gallery) == 1

    # -- driver ------------------------------------------------------- #
    def run_app_with_enroll(self, job, **cfg):
        """Queue an enroll job before the loop starts, then run it."""
        cfg.setdefault("cmd_port", 0)
        cfg.setdefault("min_face_px", 64)
        sink = _RecordingSink()
        app = _load_app_module().FaceRecognitionApp()
        app.start("models/scrfd500m_640_fp16.rknn", source="ffmpeg", sink=sink,
                  n=0, verbose=False, app_dir=APP_DIR, manifest=self.manifest,
                  config=dict(cfg))
        fut = app.submit_enroll({"name": job["name"], "source": job["source"],
                                 "frames": job.get("frames") or 0,
                                 "image_b64": job.get("image_b64")})
        try:
            app.run()
        finally:
            app.finish()
        assert fut.done(), "enrollment never completed inside the loop"
        self.result = fut.result()
        return sink, app

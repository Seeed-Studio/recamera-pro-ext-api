"""
Equivalence gate for the fall-detection app-shape migration (KIT_APP_SHAPE_SPEC §7).

fall-detection is the DEEPEST cross-frame case in the app set:

  * an IoU tracker that hands out identities and holds a lost-track grace,
  * one ported `FallDetector` state machine PER identity (normal -> suspected ->
    fallen -> recovering -> normal, plus a cooldown clock),
  * one `_TemporalClassifier` PER identity -- a 48-frame sliding window sampled
    at 15 fps and evaluated every 3rd frame, with a `positive_run` counter that
    only fires after `consecutive` positives.

A handful of frames would prove nothing here (the temporal window alone is
48 frames = 3.2 s), so the fixture drives a **140-frame, 9.3 s two-person
script** at exactly the profile's 15 fps:

  person A (cell 5,7): stands 2.0 s -> drops and lies down at call 30 ->
                       stays lying 3.0 s -> stands back up at call 75.
                       That walks the FULL state machine:
                       normal -> suspected -> fallen -> recovering -> normal.
  person B (cell 14,7): stands the whole time EXCEPT calls 50..79 (2.0 s), a gap
                       longer than the 0.75 s occlusion grace -- so its track is
                       fed invalid observations, then EXPIRES (detector +
                       classifier popped), then comes back with a NEW track id.

Hardware-free: the frame source (`kit.app.open_frame_source`) and the RKNN
engine (`kit.app.App._load_model`) are stubbed with deterministic fakes. The
fake pose model emits REAL rawhead tensors (64-ch DFL box + 1-ch class +
51-ch keypoint branches on one 20x20 / stride-32 level), so the genuine
`kit.runtime.postprocess.pose` decoder runs in both paths.

The SAME fixed frame sequence is pushed through

  OLD path : the pre-migration fall-detection (git 37c4ef9), reproduced
             verbatim below -- base `App.run()` loop + `setup()` +
             `on_config_reload()` + `run_postproc()` + `on_results()`;
  NEW path : the migrated `apps/fall-detection/app.py` -- `owns_loop = True`,
             `run()` + `for frame in self.frames()` + `on_params_changed()`,

and `results` / `events` / `pts` / `stream_id` are compared field for field.

The fake model is seeded BY CALL NUMBER, so the k-th inference of the old run
and of the new run are byte-identical -- any downstream difference is a
behaviour difference.

Run: `python3 -m pytest kit/tests/test_fall_shape_equivalence.py -q`
"""
import importlib.util
import json
import os
import signal
import sys
import unittest
from dataclasses import replace

import numpy as np

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from kit import app as kit_app                                       # noqa: E402
from kit.tests.legacy_loop import LegacyLoopApp              # noqa: E402
from kit.adapters.frame_source import Frame                          # noqa: E402
from kit.adapters.result_sink import ResultSink                      # noqa: E402
from kit.logic.geometry import make_observation                      # noqa: E402
from kit.logic.temporal import FallConfig, FallDetector              # noqa: E402
from kit.runtime.postprocess import pose as pose_post                # noqa: E402
from kit.runtime.preprocess import letterbox                         # noqa: E402

APP_DIR = os.path.join(_REPO, "apps", "fall-detection")

FRAME_W, FRAME_H = 640, 480     # camera frame: NOT square (letterbox pads y)
DET_SIZE = 640                  # manifest models[0].input
N_GREY = 2                      # camera warm-up placeholders, both paths skip
FPS = 15.0                      # == the frozen temporal profile's sample_fps
DT = 1.0 / FPS
POSE_MODEL = "models/yolo11n_pose_rawhead_int8.rknn"

GRID = 20                       # single FPN level: 20x20 @ stride 32
STRIDE = DET_SIZE // GRID
REG_MAX = 16
N_KPT = 17

# --- the two-person script, indexed by INFER CALL number --------------------- #
# call 0 is the kit warm-up frame (both paths discard it), so processed frame i
# corresponds to call i+1.
A_CELL = (5, 7)                 # (col, row) of person A
B_CELL = (14, 7)                # (col, row) of person B
A_LIE_FROM, A_LIE_TO = 30, 75   # person A is lying for calls [30, 75)
B_GONE_FROM, B_GONE_TO = 50, 80  # person B is absent for calls [50, 80)
N_REAL = 140                    # 140 / 15 fps = 9.3 s -- ~3 temporal windows

CONF_HI_RAW = 2.2               # sigmoid -> 0.900
CONF_LO_RAW = -2.2              # sigmoid -> 0.100  (below kpt_thres 0.5)
PERSON_SCORE = 0.91

# DFL bin -> half-side in letterbox px = bin * STRIDE.
STAND_BIN_X, STAND_BIN_Y = 1, 3   # w=64  h=192 -> aspect 0.33 (upright)
LIE_BIN_X, LIE_BIN_Y = 3, 1       # w=192 h=64  -> aspect 3.00 (lying)

# Effective config for the MAIN comparison = the manifest defaults, i.e. the
# learned 48-frame gate IS required. The scripted lying pose drives the frozen
# profile to p=1.0, so the fall fires through the real temporal path
# (3 consecutive stride-3 evaluations above threshold 0.8), not through a
# geometry shortcut.
EFF = {
    "temporal_confirmation_required": True,
    "confidence": 0.4,
    "keypoint_confidence": 0.5,
    "hip_drop_speed_threshold": 0.25,
    "hip_drop_distance_threshold": 0.02,
    "motion_window_sec": 0.75,
    "torso_angle_threshold_deg": 55,
    "bbox_aspect_ratio_threshold": 1.25,
    "min_suspected_features": 2,
    "confirmation_sec": 0.8,
    "suspected_timeout_sec": 1.5,
    "occlusion_grace_sec": 0.75,
    "recovery_torso_angle_deg": 35,
    "recovery_aspect_ratio": 1.1,
    "recovery_window_sec": 2.0,
    "cooldown_sec": 3.0,
}
# Same script with the learned gate switched OFF (`temporal_confirmation_required:
# false` is the documented geometry-only legacy mode) -- a second, independent
# equivalence run over the SAME 140 frames.
EFF_GEOM = dict(EFF, temporal_confirmation_required=False)
# Reverse control: a person threshold nothing in the fixture can pass. Used to
# prove the auto-bound `confidence` really reaches the pose post-processor (and
# that the assertions on the main run are therefore not vacuous).
EFF_BLIND = dict(EFF, confidence=0.95)

# The letterbox geometry the fixture's frames produce (640x480 -> 640).
_LB_INFO = letterbox(np.zeros((FRAME_H, FRAME_W, 3), dtype=np.uint8),
                     DET_SIZE)[1]


def _raw_kpt(ox, oy, cell):
    """Original-frame (x, y) -> the raw (x, y) the pose head must emit.

    Inverse of the decoder: original -> letterbox (scale + pad) -> ultralytics
    keypoint decode ``k = (raw * 2 + g) * stride``.
    """
    col, row = cell
    lx = ox * _LB_INFO.scale + _LB_INFO.pad_w
    ly = oy * _LB_INFO.scale + _LB_INFO.pad_h
    return ((lx / STRIDE - col) / 2.0,
            (ly / STRIDE - row) / 2.0)


def _skeleton(cx, cy, lying):
    """A full COCO-17 skeleton in ORIGINAL-frame pixels.

    Upright: shoulders 80 px ABOVE the hips (torso angle ~0 deg).
    Lying:   shoulders 80 px BESIDE the hips at the same height, and the hips
             dropped to `cy` -- torso angle atan2(|dx|, |dy|) ~= 90 deg.
    """
    if lying:
        hip = (cx, cy)
        sh = (cx - 80.0, cy)
        limb = ((60.0, 0.0), (120.0, 0.0))       # knee / ankle offsets
        head = (cx - 110.0, cy)
    else:
        hip = (cx, cy)
        sh = (cx, cy - 80.0)
        limb = ((0.0, 60.0), (0.0, 120.0))
        head = (cx, cy - 110.0)
    k = [list(head) for _ in range(N_KPT)]
    k[0] = list(head)                                        # nose
    for j, dx in ((1, -6.0), (2, 6.0), (3, -12.0), (4, 12.0)):
        k[j] = [head[0] + dx, head[1]]                       # eyes / ears
    for j, dx in ((5, -15.0), (6, 15.0)):
        k[j] = [sh[0], sh[1] + dx] if lying else [sh[0] + dx, sh[1]]
    for j, dx in ((7, -25.0), (8, 25.0)):                    # elbows
        k[j] = [sh[0], sh[1] + dx] if lying else [sh[0] + dx, sh[1] + 40.0]
    for j, dx in ((9, -30.0), (10, 30.0)):                   # wrists
        k[j] = [sh[0], sh[1] + dx] if lying else [sh[0] + dx, sh[1] + 80.0]
    for j, dx in ((11, -10.0), (12, 10.0)):                  # hips
        k[j] = [hip[0], hip[1] + dx] if lying else [hip[0] + dx, hip[1]]
    for (j, dx), off in zip(((13, -10.0), (14, 10.0)), (limb[0],) * 2):
        k[j] = [hip[0] + off[0], hip[1] + dx] if lying else \
               [hip[0] + dx, hip[1] + off[1]]
    for (j, dx), off in zip(((15, -10.0), (16, 10.0)), (limb[1],) * 2):
        k[j] = [hip[0] + off[0], hip[1] + dx] if lying else \
               [hip[0] + dx, hip[1] + off[1]]
    return k


# Person A: hips at y=200 while standing, y=400 while lying (hip_y 0.417 ->
# 0.833 in one frame = a 6.25 /s drop, far above the 0.25 threshold).
A_STAND = _skeleton(176.0, 200.0, lying=False)
A_LIE = _skeleton(176.0, 400.0, lying=True)
B_STAND = _skeleton(464.0, 200.0, lying=False)


def _script(call):
    """-> list of (cell, skeleton, lying) present at this infer call."""
    out = []
    lying_a = A_LIE_FROM <= call < A_LIE_TO
    out.append((A_CELL, A_LIE if lying_a else A_STAND, lying_a))
    if not (B_GONE_FROM <= call < B_GONE_TO):
        out.append((B_CELL, B_STAND, False))
    return out


def _fixed_frames():
    """N_GREY flat-grey warm-up frames, then N_REAL real ones, pts DT apart."""
    out = []
    for i in range(N_GREY):
        data = np.full((FRAME_H, FRAME_W, 3), 114, dtype=np.uint8)
        out.append(Frame(data=data, w=FRAME_W, h=FRAME_H, fmt="RGB",
                         pts=100.0 + i))
    for i in range(N_REAL):
        rng = np.random.default_rng(7700 + i)
        data = rng.integers(0, 256, (FRAME_H, FRAME_W, 3), dtype=np.uint8)
        out.append(Frame(data=data, w=FRAME_W, h=FRAME_H, fmt="RGB",
                         pts=400.0 + i * DT))
    return out


class _FakeSource:
    """Fake camera honouring the frame-source flags `start()` / `run()` pass.

    `direct_preprocess=True` (`model_frame = "hw-direct"`) is emulated
    faithfully: the letterboxed model image REPLACES `data` while `w`/`h` stay
    the original camera geometry. fall-detection never reads `frame.data`, so
    this is the correct mode for it.
    """

    def __init__(self, *a, **kw):
        self.closed = False
        self.kw = kw

    def frames(self):
        direct = bool(self.kw.get("direct_preprocess"))
        size = int(self.kw.get("input_size") or 0)
        for f in _fixed_frames():
            if direct and size:
                padded, info = letterbox(f.data, size)
                f = Frame(data=padded, w=f.w, h=f.h, fmt=f.fmt, pts=f.pts,
                          model_info=info)
            yield f

    def close(self):
        self.closed = True


class _FakePoseModel:
    """A scripted YOLO11n-pose rawhead: box / class / keypoint branch per call.

    Up to two people, each pinned to its own grid cell so the greedy IoU
    tracker has two well-separated identities to associate.
    """

    def __init__(self, path):
        self.path = path
        self.calls = 0
        self.input_shapes = []
        self.released = False

    def infer(self, x):
        self.input_shapes.append(tuple(np.asarray(x).shape))
        k = self.calls
        self.calls += 1

        box = np.zeros((1, 4 * REG_MAX, GRID, GRID), dtype=np.float32)
        cls = np.full((1, 1, GRID, GRID), 0.02, dtype=np.float32)
        kpt = np.zeros((1, N_KPT * 3, GRID, GRID), dtype=np.float32)
        for ch in range(N_KPT):
            kpt[0, ch * 3 + 2, :, :] = CONF_LO_RAW

        for cell, skel, lying in _script(k):
            col, row = cell
            cls[0, 0, row, col] = PERSON_SCORE
            bx = LIE_BIN_X if lying else STAND_BIN_X
            by = LIE_BIN_Y if lying else STAND_BIN_Y
            for side, b in ((0, bx), (1, by), (2, bx), (3, by)):
                box[0, side * REG_MAX + b, row, col] = 12.0
            for j, (px, py) in enumerate(skel):
                rx, ry = _raw_kpt(px, py, cell)
                kpt[0, j * 3 + 0, row, col] = rx
                kpt[0, j * 3 + 1, row, col] = ry
                kpt[0, j * 3 + 2, row, col] = CONF_HI_RAW
        return [box, cls, kpt]

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


def _load_new_app_module():
    path = os.path.join(APP_DIR, "app.py")
    spec = importlib.util.spec_from_file_location(
        "_fall_detection_app_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_NEW = _load_new_app_module()
# The app-local helpers the migration did NOT touch (tracker, frozen temporal
# profile/classifier, the 56-value pelvis-centred encoding). The legacy app
# below reuses them verbatim, exactly as the pre-migration file did.
IoUTracker = _NEW.IoUTracker
_TemporalProfile = _NEW._TemporalProfile
_TemporalClassifier = _NEW._TemporalClassifier
_make_temporal_frame = _NEW._make_temporal_frame


# ---- OLD shape: verbatim copy of the pre-migration fall-detection ---------- #
class _LegacyFallApp(LegacyLoopApp):
    """fall-detection exactly as it was before the migration (git 37c4ef9)."""

    id = "fall-detection"
    name = "Fall Detection"
    postproc = "pose"
    model_frame = "hw-direct"

    def setup(self, config):
        super().setup(config)
        params = {k: v for k, v in (config or {}).items() if v is not None}

        self.conf = float(params.get("confidence", 0.4))
        self.kpt_thres = float(params.get("keypoint_confidence", 0.5))

        cfg = FallConfig(
            temporal_confirmation_required=bool(
                params.get("temporal_confirmation_required", True)),
            hip_drop_speed_threshold=float(params.get("hip_drop_speed_threshold", 0.25)),
            hip_drop_distance_threshold=float(params.get("hip_drop_distance_threshold", 0.02)),
            motion_window_sec=float(params.get("motion_window_sec", 0.75)),
            torso_angle_threshold_deg=float(params.get("torso_angle_threshold_deg", 55.0)),
            bbox_aspect_ratio_threshold=float(params.get("bbox_aspect_ratio_threshold", 1.25)),
            min_suspected_features=int(params.get("min_suspected_features", 2)),
            confirmation_sec=float(params.get("confirmation_sec", 0.80)),
            suspected_timeout_sec=float(params.get("suspected_timeout_sec", 1.50)),
            occlusion_grace_sec=float(params.get("occlusion_grace_sec", 0.75)),
            recovery_torso_angle_deg=float(params.get("recovery_torso_angle_deg", 35.0)),
            recovery_aspect_ratio=float(params.get("recovery_aspect_ratio", 1.10)),
            recovery_window_sec=float(params.get("recovery_window_sec", 2.00)),
            cooldown_sec=float(params.get("cooldown_sec", 3.00)),
        )
        self._fall_config = cfg
        profile_file = str(params.get(
            "temporal_profile_file", "models/temporal_yolo11s_pose_v1.json.gz"))
        if not os.path.isabs(profile_file):
            profile_file = os.path.join(APP_DIR, profile_file)
        self._temporal_profile = _TemporalProfile(profile_file)
        self.temporal_classifiers = {}
        self.tracker = IoUTracker(
            iou_threshold=float(params.get("tracker_iou_threshold", 0.2)),
            max_lost_sec=cfg.occlusion_grace_sec,
        )
        self.detectors = {}

    def on_config_reload(self, config):
        params = {k: v for k, v in (config or {}).items() if v is not None}
        self.config = config or {}

        def _f(key, cur):
            try:
                return float(params.get(key, cur))
            except (TypeError, ValueError):
                return cur

        self.conf = _f("confidence", self.conf)
        self.kpt_thres = _f("keypoint_confidence", self.kpt_thres)

        cur = self._fall_config
        cfg = FallConfig(
            temporal_confirmation_required=bool(params.get(
                "temporal_confirmation_required",
                cur.temporal_confirmation_required)),
            hip_drop_speed_threshold=_f("hip_drop_speed_threshold",
                                        cur.hip_drop_speed_threshold),
            hip_drop_distance_threshold=_f("hip_drop_distance_threshold",
                                           cur.hip_drop_distance_threshold),
            motion_window_sec=_f("motion_window_sec", cur.motion_window_sec),
            torso_angle_threshold_deg=_f("torso_angle_threshold_deg",
                                         cur.torso_angle_threshold_deg),
            bbox_aspect_ratio_threshold=_f("bbox_aspect_ratio_threshold",
                                           cur.bbox_aspect_ratio_threshold),
            min_suspected_features=int(_f("min_suspected_features",
                                          cur.min_suspected_features)),
            confirmation_sec=_f("confirmation_sec", cur.confirmation_sec),
            suspected_timeout_sec=_f("suspected_timeout_sec", cur.suspected_timeout_sec),
            occlusion_grace_sec=_f("occlusion_grace_sec", cur.occlusion_grace_sec),
            recovery_torso_angle_deg=_f("recovery_torso_angle_deg",
                                        cur.recovery_torso_angle_deg),
            recovery_aspect_ratio=_f("recovery_aspect_ratio", cur.recovery_aspect_ratio),
            recovery_window_sec=_f("recovery_window_sec", cur.recovery_window_sec),
            cooldown_sec=_f("cooldown_sec", cur.cooldown_sec),
        )
        self._fall_config = cfg
        for detector in self.detectors.values():
            detector.set_config(replace(cfg))
        if getattr(self, "tracker", None) is not None:
            self.tracker.max_lost_sec = cfg.occlusion_grace_sec

    def _detector_for(self, track_id):
        detector = self.detectors.get(track_id)
        if detector is None:
            detector = FallDetector(replace(self._fall_config))
            self.detectors[track_id] = detector
        return detector

    def _temporal_for(self, track_id):
        classifier = self.temporal_classifiers.get(track_id)
        if classifier is None:
            classifier = _TemporalClassifier(self._temporal_profile)
            self.temporal_classifiers[track_id] = classifier
        return classifier

    def run_postproc(self, outs, info):
        return pose_post.postprocess(outs, info, conf_thres=self.conf,
                                     iou_thres=self.iou,
                                     kpt_thres=self.kpt_thres)

    def on_results(self, results, frame):
        results = list(results or [])
        events = []
        for result in results:
            if isinstance(result, dict):
                result.setdefault("kind", "person")

        tracks = self.tracker.update(results, frame.pts)
        for track in tracks:
            person = None
            if track.detection_index is not None:
                idx = track.detection_index
                if 0 <= idx < len(results):
                    person = results[idx]

            detector = self._detector_for(track.track_id)
            obs = make_observation(person, frame.pts, frame.h, self.kpt_thres)
            temporal = self._temporal_for(track.track_id)
            temporal_frame = _make_temporal_frame(
                person, obs, int(frame.w), int(frame.h))
            evaluated, temporal_positive, temporal_probability = temporal.update(
                temporal_frame, frame.pts)
            out = detector.update(
                obs,
                temporal_available=True,
                temporal_positive=temporal_positive,
                temporal_probability=temporal_probability,
            )

            events.append({
                "kind": "pose_state",
                "track_id": track.track_id,
                "visible": person is not None,
                "missed_frames": track.missing_frames,
                "box": list(track.box),
                "state": out.state,
                "fall_detected": out.fall_detected,
                "event_id": out.event_id,
                "person_score": round(obs.person_score, 3),
                "torso_angle_deg": round(out.diagnostics.get("torso_angle_deg", 0.0), 1),
                "bbox_aspect_ratio": round(out.diagnostics.get("bbox_aspect_ratio", 0.0), 3),
                "hip_drop_speed": round(out.diagnostics.get("hip_drop_speed", 0.0), 3),
                "evidence_features": int(out.diagnostics.get("evidence_features", 0)),
                "features": {
                    "valid": bool(obs.valid),
                    "hip_y": round(obs.hip_y, 4),
                    "person_score": round(obs.person_score, 4),
                    "hip_drop_speed": round(out.diagnostics.get("hip_drop_speed", 0.0), 4),
                    "hip_drop_distance": round(out.diagnostics.get("hip_drop_distance", 0.0), 4),
                    "torso_angle_deg": round(out.diagnostics.get("torso_angle_deg", 0.0), 2),
                    "bbox_aspect_ratio": round(out.diagnostics.get("bbox_aspect_ratio", 0.0), 4),
                    "temporal_evaluated": bool(evaluated),
                    "temporal_probability": round(out.diagnostics.get("temporal_probability", 0.0), 4),
                    "temporal_positive": bool(out.diagnostics.get("temporal_positive", False)),
                },
            })

            if person is not None:
                person["track_id"] = track.track_id
                person["state"] = out.state
                person["fall_detected"] = out.fall_detected
                person["event_id"] = out.event_id
                person["person_detected"] = True
                person["person_score"] = float(person.get("score", 0.0))
                person["tracking"] = True
                person["missed_frames"] = 0
                person["features"] = events[-1]["features"]

            if out.fall_event:
                events.append({
                    "kind": "fall",
                    "track_id": track.track_id,
                    "event_id": out.event_id,
                    "state": out.state,
                })

        for track_id in self.tracker.expired_ids:
            self.detectors.pop(track_id, None)
            self.temporal_classifiers.pop(track_id, None)

        return events


def _strip_timing(payloads):
    out = []
    for payload, pts in payloads:
        p = {k: v for k, v in payload.items()
             # `render` is a kit-injected display declaration (added after
             # this reference was frozen; RENDER_DECLARATION_SPEC §3) --
             # metadata about drawing, not app output. Its own suite is
             # kit/tests/test_render_declaration.py.
             if k not in ("inference_time_ms", "pipeline_ms", "render")}
        out.append((p, pts))
    return out


def _pose_events(payload):
    return [e for e in payload["events"] if e["kind"] == "pose_state"]


class _Base(unittest.TestCase):
    def setUp(self):
        self._orig_open = kit_app.open_frame_source
        self._orig_load = kit_app.App._load_model
        self.models = []

        def _fake_load(app_self, path):
            m = _FakePoseModel(path)
            self.models.append(m)
            return m

        kit_app.open_frame_source = lambda *a, **kw: _FakeSource(*a, **kw)
        kit_app.App._load_model = _fake_load

        with open(os.path.join(APP_DIR, "manifest.json")) as f:
            self.manifest = json.load(f)

    def tearDown(self):
        kit_app.open_frame_source = self._orig_open
        kit_app.App._load_model = self._orig_load
        try:
            signal.signal(signal.SIGHUP, signal.SIG_DFL)
        except (ValueError, OSError):
            pass

    def _run_old(self, eff):
        sink = _RecordingSink()
        app = _LegacyFallApp()
        app.setup(dict(eff))
        app.run(POSE_MODEL, source="ffmpeg", sink=sink, n=0, verbose=False)
        return sink, app

    def _run_new(self, eff):
        sink = _RecordingSink()
        app = _NEW.FallDetectionApp()
        app.start(POSE_MODEL, source="ffmpeg", sink=sink, n=0, verbose=False,
                  app_dir=APP_DIR, manifest=self.manifest, config=dict(eff))
        try:
            app.run()
        finally:
            app.finish()
        return sink, app


class FallEquivalenceTests(_Base):

    def _compare(self, eff, label):
        old, old_app = self._run_old(eff)
        old_calls = self.models[-1].calls
        new, new_app = self._run_new(eff)
        new_calls = self.models[-1].calls

        self.assertEqual(len(old.payloads), N_REAL - 1,
                         f"{label}: old path emitted an unexpected frame count")
        self.assertEqual(len(new.payloads), len(old.payloads),
                         f"{label}: new path emitted a different frame count")
        self.assertEqual(old_calls, new_calls,
                         f"{label}: infer call counts differ")

        old_s, new_s = _strip_timing(old.payloads), _strip_timing(new.payloads)
        for i, ((po, pts_o), (pn, pts_n)) in enumerate(zip(old_s, new_s)):
            self.assertEqual(pts_o, pts_n, f"{label} frame {i}: pts differs")
            self.assertEqual(po["stream_id"], pn["stream_id"],
                             f"{label} frame {i}: stream_id differs")
            self.assertEqual(po["results"], pn["results"],
                             f"{label} frame {i}: results differ")
            self.assertEqual(po["events"], pn["events"],
                             f"{label} frame {i}: events differ")
        self.assertEqual(old_s, new_s, f"{label}: payload streams differ")
        self.assertEqual(old.frame_sizes, new.frame_sizes)
        return old_s, new_s, old_app, new_app

    # -- 1. the headline deep comparison ---------------------------------- #
    def test_deep_equal_default_config(self):
        old_s, _new_s, old_app, new_app = self._compare(EFF, "default")

        evs = [e for p, _ in old_s for e in p["events"]]
        kinds = {k: sum(1 for e in evs if e["kind"] == k)
                 for k in sorted({e["kind"] for e in evs})}
        states = {}
        for e in evs:
            if e["kind"] == "pose_state":
                states[e["state"]] = states.get(e["state"], 0) + 1
        print("\n--- [default] event kind distribution (old path) ---")
        print(kinds)
        print("--- [default] pose_state distribution ---")
        print(states)
        print(f"--- [default] DEEP EQUAL over {len(old_s)} frames: "
              f"results/events/pts/stream_id all identical ---")

        # -- anti-vacuous-pass assertions --------------------------------- #
        self.assertGreater(len(evs), 0, "fixture produced no events")
        self.assertGreaterEqual(kinds.get("fall", 0), 1,
                                "no fall event -- the fixture is vacuous")
        self.assertGreater(kinds.get("pose_state", 0), len(old_s),
                           "fewer pose_state events than frames: the "
                           "multi-person path never ran")
        for st in ("normal", "suspected", "fallen", "recovering"):
            self.assertIn(st, states,
                          f"state {st!r} never reached -- the state machine "
                          f"did not walk the full cycle")
        # results carry the pose payload the overlay needs
        res = [r for p, _ in old_s for r in p["results"]]
        self.assertGreater(len(res), 0, "no person was ever detected")
        self.assertTrue(all(r["kind"] == "person" for r in res))
        self.assertTrue(all(len(r["keypoints"]) == N_KPT for r in res))
        self.assertTrue(any(r["fall_detected"] for r in res),
                        "no result was ever flagged fall_detected")
        # the two apps also agree on the surviving per-track state
        self.assertEqual(sorted(old_app.detectors), sorted(new_app.detectors))
        self.assertEqual(sorted(old_app.temporal_classifiers),
                         sorted(new_app.temporal_classifiers))

    def test_deep_equal_geometry_only_mode(self):
        """Same script with the learned gate switched OFF."""
        old_s, _new_s, _o, _n = self._compare(EFF_GEOM, "geometry-only")
        evs = [e for p, _ in old_s for e in p["events"]]
        kinds = {k: sum(1 for e in evs if e["kind"] == k)
                 for k in sorted({e["kind"] for e in evs})}
        print("\n--- [geometry-only] event kind distribution (old path) ---")
        print(kinds)
        print(f"--- [geometry-only] DEEP EQUAL over {len(old_s)} frames ---")
        self.assertGreater(kinds.get("pose_state", 0), 0)

    def test_deep_equal_when_nobody_passes_the_confidence_gate(self):
        """Same script at confidence=0.95: both paths must go silent together."""
        old_s, _new_s, _o, _n = self._compare(EFF_BLIND, "blind")
        res = [r for p, _ in old_s for r in p["results"]]
        evs = [e for p, _ in old_s for e in p["events"]]
        print(f"\n--- [blind] results={len(res)} events={len(evs)} "
              f"over {len(old_s)} frames (DEEP EQUAL) ---")
        self.assertEqual(res, [], "the confidence gate was not applied")
        self.assertEqual(evs, [], "events survived an empty detection list")

    # -- 2. per-frame cross-frame state, printed old vs new --------------- #
    def test_cross_frame_state_frame_by_frame(self):
        """pose_state / fall / temporal probability, per frame, old vs new."""
        old, _ = self._run_old(EFF)
        new, _ = self._run_new(EFF)

        def _table(sink):
            rows = []
            for p, pts in sink.payloads:
                row = []
                for e in _pose_events(p):
                    f = e["features"]
                    row.append((e["track_id"], e["state"], e["visible"],
                                e["missed_frames"], e["event_id"],
                                e["fall_detected"], f["temporal_evaluated"],
                                f["temporal_positive"],
                                f["temporal_probability"],
                                f["torso_angle_deg"], f["bbox_aspect_ratio"],
                                f["hip_drop_speed"]))
                falls = [(e["track_id"], e["event_id"])
                         for e in p["events"] if e["kind"] == "fall"]
                rows.append((round(pts, 4), tuple(row), tuple(falls)))
            return rows

        old_tbl, new_tbl = _table(old), _table(new)
        print("\n--- per-frame [(track, state, visible, missed, event_id, "
              "fall_detected, t_eval, t_pos, t_prob, torso, aspect, "
              "hip_speed)], falls ---")
        for i, (o, n) in enumerate(zip(old_tbl, new_tbl)):
            print(f"frame {i:3d}: OLD {o}")
            print(f"frame {i:3d}: NEW {n}")
            print(f"frame {i:3d}: EQUAL={o == n}")
        self.assertEqual(old_tbl, new_tbl, "cross-frame state diverged")

        # ★fall timing★: exactly where the confirmation fires, and through
        # which mechanism.
        fall_frames = [i for i, r in enumerate(old_tbl) if r[2]]
        print(f"\nfall events at frame indices: {fall_frames}")
        self.assertTrue(fall_frames, "the fixture never confirmed a fall")
        faller = old_tbl[fall_frames[0]][2][0][0]
        drop_frame = A_LIE_FROM - 1        # call 30 == processed frame 29
        print(f"faller track_id={faller}, drop at frame {drop_frame}, "
              f"confirmed at frame {fall_frames[0]} "
              f"(+{fall_frames[0] - drop_frame} frames = "
              f"{(fall_frames[0] - drop_frame) * DT:.3f} s)")
        self.assertGreater(fall_frames[0], drop_frame,
                           "a fall was confirmed before the person fell")
        self.assertLess(fall_frames[0], A_LIE_TO - 1,
                        "the fall was confirmed after the person stood up")
        # the learned gate needs `consecutive` positives, `stride` frames apart
        prof = _TemporalProfile(os.path.join(
            APP_DIR, "models/temporal_yolo11s_pose_v1.json.gz"))
        self.assertGreaterEqual(
            fall_frames[0] - drop_frame, prof.stride * (prof.consecutive - 1),
            "the fall fired sooner than `consecutive` stride-spaced "
            "evaluations allow")
        at_fall = [r for r in old_tbl[fall_frames[0]][1] if r[0] == faller][0]
        print(f"at the fall frame: temporal_evaluated={at_fall[6]} "
              f"temporal_positive={at_fall[7]} probability={at_fall[8]} "
              f"(threshold={prof.threshold}, consecutive={prof.consecutive})")
        self.assertTrue(at_fall[7], "the learned gate was not positive")
        self.assertGreaterEqual(at_fall[8], prof.threshold)

        # ★state progression★ for the faller, in order.
        seq = []
        for _pts, row, _f in old_tbl:
            for r in row:
                if r[0] == faller and (not seq or seq[-1] != r[1]):
                    seq.append(r[1])
        print(f"track {faller} state progression: {seq}")
        self.assertEqual(seq, ["normal", "suspected", "fallen", "recovering",
                               "normal"])

        # ★multi-person★: two identities live at once, and B's track EXPIRES
        # and is re-issued with a fresh id after the 2.0 s gap.
        ids = sorted({r[0] for _p, row, _f in old_tbl for r in row})
        print(f"track ids seen: {ids}")
        self.assertGreaterEqual(len(ids), 3,
                                "the expiry / re-issue path never ran")
        widths = {len(row) for _p, row, _f in old_tbl}
        self.assertIn(2, widths, "the two-person frames never happened")
        # the invalid-observation branch (track kept alive while unseen)
        self.assertTrue(any(r[2] is False for _p, row, _f in old_tbl for r in row),
                        "no track was ever carried through an occlusion")

    # -- 3. the temporal classifier really ran over a FULL window --------- #
    def test_temporal_window_is_fully_covered(self):
        _old, old_app = self._run_old(EFF)
        _new, new_app = self._run_new(EFF)
        prof = new_app._temporal_profile
        print(f"\ntemporal profile: window={prof.window} frames "
              f"@ {prof.sample_fps} fps, stride={prof.stride}, "
              f"threshold={prof.threshold}, consecutive={prof.consecutive}")
        span = (N_REAL - 1) / FPS
        print(f"fixture: {N_REAL - 1} processed frames at {FPS} fps = "
              f"{span:.2f} s = {span / (prof.window / prof.sample_fps):.2f} "
              f"full temporal windows")
        self.assertGreaterEqual(N_REAL - 1, prof.window * 2,
                                "the fixture is shorter than two temporal "
                                "windows; cross-frame behaviour untested")

        # the faller's track lives for the whole run: its classifier must hold a
        # FULL window's worth of samples and have been evaluated many times.
        tid = min(old_app.temporal_classifiers)
        self.assertEqual(tid, min(new_app.temporal_classifiers))
        for tag, app in (("old", old_app), ("new", new_app)):
            clf = app.temporal_classifiers[tid]
            print(f"[{tag}] track {tid} classifier: buffered={len(clf.frames)} "
                  f"last_prob={clf.last_probability:.6f} "
                  f"positive_run={clf.positive_run} "
                  f"last_positive={clf.last_positive} "
                  f"last_eval={clf.last_evaluation:.4f}")
            self.assertGreaterEqual(
                len(clf.frames), prof.window // 2,
                f"[{tag}] the sliding window never filled")
            self.assertGreater(clf.last_evaluation, 0.0,
                               f"[{tag}] the classifier never evaluated")
        old_clf = old_app.temporal_classifiers[tid]
        new_clf = new_app.temporal_classifiers[tid]
        self.assertEqual(len(old_clf.frames), len(new_clf.frames))
        self.assertEqual(old_clf.positive_run, new_clf.positive_run)
        self.assertEqual(old_clf.last_probability, new_clf.last_probability)
        self.assertEqual(old_clf.last_evaluation, new_clf.last_evaluation)
        np.testing.assert_array_equal(np.asarray(old_clf.frames[-1][1]),
                                      np.asarray(new_clf.frames[-1][1]))

    def test_temporal_evaluation_follows_stride_3(self):
        """The stride-3 cadence is visible in the emitted features."""
        old, old_app = self._run_old(EFF)
        tid = min(old_app.temporal_classifiers)
        flags = [f["temporal_evaluated"]
                 for p, _ in old.payloads
                 for e in _pose_events(p) if e["track_id"] == tid
                 for f in (e["features"],)]
        probs = [f["temporal_probability"]
                 for p, _ in old.payloads
                 for e in _pose_events(p) if e["track_id"] == tid
                 for f in (e["features"],)]
        n_eval = sum(1 for f in flags if f)
        print(f"\ntrack {tid}: {len(flags)} frames, {n_eval} temporal evaluations "
              f"(stride 3 -> expect ~{len(flags) // 3}), "
              f"{len(set(probs))} distinct probabilities "
              f"[{min(probs):.4f} .. {max(probs):.4f}]")
        self.assertGreater(n_eval, 0, "the classifier never evaluated")
        self.assertLess(n_eval, len(flags),
                        "every frame evaluated -- the stride is not in effect")
        self.assertGreater(len(set(probs)), 1,
                           "the probability never moved: the window is inert")

    def test_infer_call_counts_and_input_geometry(self):
        self._run_old(EFF)
        old_calls = self.models[-1].calls
        old_shapes = set(self.models[-1].input_shapes)
        self._run_new(EFF)
        new_calls = self.models[-1].calls
        new_shapes = set(self.models[-1].input_shapes)
        print(f"\npose infer calls: OLD {old_calls} NEW {new_calls}")
        print(f"pose infer input shapes: OLD {old_shapes} NEW {new_shapes}")
        self.assertEqual(old_calls, new_calls)
        self.assertEqual(old_calls, N_REAL,
                         "one infer per real frame (incl. the warm-up frame)")
        self.assertEqual(new_shapes, {(DET_SIZE, DET_SIZE, 3)})
        self.assertEqual(old_shapes, new_shapes)


class FallReverseControlTests(_Base):
    """Proof that the equivalence assertions are not tautologies."""

    def test_a_single_perturbed_field_is_caught(self):
        """Flip ONE nested feature in ONE frame -> the comparison must fail."""
        old, _ = self._run_old(EFF)
        new, _ = self._run_new(EFF)
        old_s, new_s = _strip_timing(old.payloads), _strip_timing(new.payloads)
        self.assertEqual(old_s, new_s)

        # perturb the deepest nested value we compare
        target = None
        for i, (p, _pts) in enumerate(new_s):
            evs = _pose_events(p)
            if evs:
                target = (i, evs[0])
                break
        self.assertIsNotNone(target, "no pose_state to perturb")
        i, ev = target
        before = ev["features"]["temporal_probability"]
        ev["features"]["temporal_probability"] = before + 1.0
        print(f"\n[reverse] perturbed frame {i} features.temporal_probability "
              f"{before} -> {before + 1.0}")
        self.assertNotEqual(old_s, new_s,
                            "the deep comparison ignores nested feature fields")

    def test_the_bound_confidence_really_drives_the_pipeline(self):
        """Raising the auto-bound `confidence` must silence the whole app.

        Without this control, "old == new" could hold simply because neither
        path ever looked at the config.
        """
        normal, _ = self._run_new(EFF)
        blind, _ = self._run_new(EFF_BLIND)
        n_res = sum(len(p["results"]) for p, _ in normal.payloads)
        n_evs = sum(len(p["events"]) for p, _ in normal.payloads)
        b_res = sum(len(p["results"]) for p, _ in blind.payloads)
        b_evs = sum(len(p["events"]) for p, _ in blind.payloads)
        print(f"\n[reverse] confidence=0.4 -> results={n_res} events={n_evs}; "
              f"confidence=0.95 -> results={b_res} events={b_evs}")
        self.assertGreater(n_res, 0)
        self.assertGreater(n_evs, 0)
        self.assertEqual(b_res, 0, "the bound confidence never reached "
                                   "pose_post.postprocess")
        self.assertEqual(b_evs, 0)

    def test_a_rebuilt_detector_would_be_visible(self):
        """Sanity: resetting a live FallDetector really does lose its state."""
        d = FallDetector(FallConfig())
        d._state = "fallen"
        d._event_id = 7
        d.set_config(FallConfig(confirmation_sec=1.23))
        self.assertEqual((d._state, d._event_id), ("fallen", 7),
                         "set_config must NOT reset the state machine")
        self.assertEqual(d.config.confirmation_sec, 1.23)
        d.reset()
        self.assertEqual((d._state, d._event_id), ("normal", 0),
                         "reset() is what a rebuild would look like")


class FallHotReloadTests(_Base):
    """★The load-bearing part★: what a SIGHUP does to the per-track state."""

    def _started(self, eff=None):
        app = _NEW.FallDetectionApp()
        app.start(POSE_MODEL, sink=_RecordingSink(), verbose=False,
                  app_dir=APP_DIR, manifest=self.manifest,
                  config=dict(eff or EFF))
        return app

    def _legacy(self, eff=None):
        app = _LegacyFallApp()
        app.setup(dict(eff or EFF))
        return app

    @staticmethod
    def _seed(app):
        """Put a live, mid-alarm identity on the app (track 1)."""
        det = app._detector_for(1)
        det._state = "fallen"
        det._event_id = 3
        det._initialized = True
        det._max_drop = 0.42
        clf = app._temporal_for(1)
        for i in range(20):
            clf.update(np.full(56, 0.5, dtype=np.float32), 100.0 + i * DT)
        return det, clf

    def test_params_auto_bound_from_manifest_schema(self):
        app = self._started(dict(EFF, confidence=0.6, keypoint_confidence=0.35,
                                 torso_angle_threshold_deg=61,
                                 min_suspected_features=3,
                                 cooldown_sec=9.5))
        try:
            self.assertEqual(app.confidence, 0.6)
            self.assertEqual(app.keypoint_confidence, 0.35)
            # `type: "integer"` -> the auto-bind hands over an INT, so the use
            # site no longer needs an int() band-aid ...
            self.assertIsInstance(app.min_suspected_features, int)
            self.assertNotIsInstance(app.min_suspected_features, bool)
            # ... while a genuinely-float knob (an angle: 55.5 is legal) stays
            # `type: "number"` and binds as float.
            self.assertIsInstance(app.torso_angle_threshold_deg, float)
            self.assertIsInstance(
                app._fall_config.min_suspected_features, int)
            self.assertEqual(app._fall_config.min_suspected_features, 3)
            self.assertEqual(app._fall_config.torso_angle_threshold_deg, 61.0)
            self.assertEqual(app._fall_config.cooldown_sec, 9.5)
            self.assertTrue(app._fall_config.temporal_confirmation_required)
            self.assertEqual(app.tracker.max_lost_sec,
                             app._fall_config.occlusion_grace_sec)
        finally:
            app.finish()

    def test_hot_reload_swaps_config_without_touching_state(self):
        """★existing detectors keep their state; only the config is replaced★."""
        new_app = self._started()
        old_app = self._legacy()
        try:
            new_det, new_clf = self._seed(new_app)
            old_det, old_clf = self._seed(old_app)
            before = (new_clf.positive_run, len(new_clf.frames),
                      new_clf.last_probability)

            cfg = dict(EFF, confirmation_sec=2.5, cooldown_sec=11.0,
                       torso_angle_threshold_deg=70, min_suspected_features=3)
            old_app.on_config_reload(cfg)
            changed = new_app._bind_params(cfg, live_only=True)
            self.assertEqual(
                changed, {"confirmation_sec", "cooldown_sec",
                          "torso_angle_threshold_deg", "min_suspected_features"})
            new_app.on_params_changed(changed)

            for tag, app, det, clf in (("old", old_app, old_det, old_clf),
                                       ("new", new_app, new_det, new_clf)):
                print(f"\n[hot-reload] {tag}: detector same_object="
                      f"{app.detectors[1] is det} state={det._state} "
                      f"event_id={det._event_id} max_drop={det._max_drop} "
                      f"confirm={det.config.confirmation_sec} "
                      f"cooldown={det.config.cooldown_sec} "
                      f"torso={det.config.torso_angle_threshold_deg} "
                      f"min_feat={det.config.min_suspected_features}")
                print(f"[hot-reload] {tag}: classifier same_object="
                      f"{app.temporal_classifiers[1] is clf} "
                      f"buffered={len(clf.frames)} "
                      f"positive_run={clf.positive_run}")
                # the detector object survives ...
                self.assertIs(app.detectors[1], det,
                              f"[{tag}] the detector was REBUILT")
                self.assertEqual(det._state, "fallen",
                                 f"[{tag}] the live alarm was cleared")
                self.assertEqual(det._event_id, 3)
                self.assertEqual(det._max_drop, 0.42)
                # ... with the NEW thresholds in it
                self.assertEqual(det.config.confirmation_sec, 2.5)
                self.assertEqual(det.config.cooldown_sec, 11.0)
                self.assertEqual(det.config.torso_angle_threshold_deg, 70.0)
                self.assertEqual(det.config.min_suspected_features, 3)
                # the temporal window is untouched by a config edit
                self.assertIs(app.temporal_classifiers[1], clf,
                              f"[{tag}] the classifier was REBUILT")
                self.assertEqual((clf.positive_run, len(clf.frames),
                                  clf.last_probability), before)

            # old vs new: the rebuilt shared template must match field for field
            self.assertEqual(old_app._fall_config, new_app._fall_config)
        finally:
            new_app.finish()

    def test_hot_reload_template_feeds_new_tracks(self):
        """A track created AFTER the reload inherits the new thresholds."""
        app = self._started()
        try:
            self._seed(app)
            cfg = dict(EFF, cooldown_sec=13.0)
            changed = app._bind_params(cfg, live_only=True)
            app.on_params_changed(changed)
            fresh = app._detector_for(99)
            print(f"\n[hot-reload] new track cooldown="
                  f"{fresh.config.cooldown_sec} "
                  f"(template={app._fall_config.cooldown_sec})")
            self.assertEqual(fresh.config.cooldown_sec, 13.0)
            # per-track copies: mutating one must not touch another
            self.assertIsNot(fresh.config, app.detectors[1].config)
            self.assertIsNot(fresh.config, app._fall_config)
        finally:
            app.finish()

    def test_hot_reload_mutates_tracker_grace_in_place(self):
        """occlusion_grace_sec -> tracker.max_lost_sec, same tracker object."""
        new_app = self._started()
        old_app = self._legacy()
        try:
            new_tracker, old_tracker = new_app.tracker, old_app.tracker
            new_tracker.update([{"box": [10, 10, 50, 90]}], 1.0)
            old_tracker.update([{"box": [10, 10, 50, 90]}], 1.0)
            cfg = dict(EFF, occlusion_grace_sec=2.25)
            old_app.on_config_reload(cfg)
            changed = new_app._bind_params(cfg, live_only=True)
            new_app.on_params_changed(changed)
            for tag, app, tracker in (("old", old_app, old_tracker),
                                      ("new", new_app, new_tracker)):
                print(f"[tracker] {tag}: same_object={app.tracker is tracker} "
                      f"max_lost={app.tracker.max_lost_sec} "
                      f"tracks={[t.track_id for t in app.tracker.active_tracks()]}")
                self.assertIs(app.tracker, tracker,
                              f"[{tag}] the tracker was REBUILT (ids would reset)")
                self.assertEqual(app.tracker.max_lost_sec, 2.25)
                self.assertEqual([t.track_id for t in
                                  app.tracker.active_tracks()], [1])
        finally:
            new_app.finish()

    def test_model_comes_from_the_registry(self):
        app = self._started()
        try:
            self.assertEqual(len(app.models), 1)
            self.assertIs(app.models.pose, app.models[0])
            self.assertEqual(os.path.basename(app.models.pose.path),
                             "yolo11n_pose_rawhead_int8.rknn")
            self.assertTrue(os.path.isabs(app.models.pose.path))
        finally:
            app.finish()


class FallFrameModeTests(_Base):
    """`model_frame = "hw-direct"` is correct here -- and the fixture proves it."""

    def test_new_app_keeps_hw_direct(self):
        self.assertEqual(_NEW.FallDetectionApp.model_frame, "hw-direct",
                         "fall-detection consumes keypoints only; keep hw-direct")
        self.assertTrue(_NEW.FallDetectionApp.owns_loop)

    def test_hw_direct_is_actually_in_effect(self):
        seen = {}
        orig = kit_app.open_frame_source

        def _spy(*a, **kw):
            seen.update(kw)
            return _FakeSource(*a, **kw)

        kit_app.open_frame_source = _spy
        try:
            self._run_new(EFF)
        finally:
            kit_app.open_frame_source = orig
        print("\nframe-source flags (new path):", seen)
        self.assertTrue(seen.get("direct_preprocess"),
                        "the source was not asked to letterbox into data")
        self.assertEqual(int(seen.get("input_size")), DET_SIZE)


if __name__ == "__main__":
    unittest.main()

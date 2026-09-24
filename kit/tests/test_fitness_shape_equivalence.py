"""
Equivalence gate for the fitness-trainer app-shape migration (KIT_APP_SHAPE_SPEC §7).

fitness-trainer is the CROSS-FRAME ACCUMULATOR case: `kit.logic.rep_counter`
holds an EMA-smoothed joint angle, a hysteresis phase, a rep debounce clock and
the reps/sets counters -- all of which live ACROSS frames. A per-frame
comparison would not catch a state machine that is silently rebuilt (and hence
zeroed), so the fixture drives a 41-frame squat script:

  * a scripted knee angle (175 deg standing / 85 deg bottom) that completes
    THREE reps with target_reps=2 -> rep, rep, SET ROLLOVER, rep;
  * a 7-frame no-person tail that triggers the idle reset
    (idle_reset_seconds = 1.0, frames 0.4 s apart).

Hardware-free: the frame source (`kit.app.open_frame_source`) and the RKNN
engine (`kit.app.App._load_model`) are stubbed with deterministic fakes. The
fake pose model emits REAL rawhead tensors (64-ch DFL box + 1-ch class +
51-ch keypoint branches on one 20x20 / stride-32 level), so the genuine
`kit.runtime.postprocess.pose` decoder runs in both paths -- the keypoints the
state machine sees really did come out of a decode.

The SAME fixed frame sequence is pushed through

  OLD path : the pre-migration fitness-trainer (git 75bd143), reproduced
             verbatim below -- base `App.run()` loop + `setup()` +
             `on_config_reload()` + `run_postproc()` + `on_results()`;
  NEW path : the migrated `apps/fitness-trainer/app.py` -- `owns_loop = True`,
             `run()` + `for frame in self.frames()` + `on_params_changed()`,

and `results` / `events` / `pts` / `stream_id` are compared field for field.

The fake model is seeded BY CALL NUMBER, so the k-th inference of the old run
and of the new run are byte-identical -- any downstream difference is a
behaviour difference.

Run: `python3 -m pytest kit/tests/test_fitness_shape_equivalence.py -q`
"""
import importlib.util
import json
import math
import os
import signal
import sys
import unittest

import numpy as np

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from kit import app as kit_app                                       # noqa: E402
from kit.tests.legacy_loop import LegacyLoopApp              # noqa: E402
from kit.adapters.frame_source import Frame                          # noqa: E402
from kit.adapters.result_sink import ResultSink                      # noqa: E402
from kit.logic.rep_counter import create_exercise, exercise_ids      # noqa: E402
from kit.runtime.postprocess import pose as pose_post                # noqa: E402
from kit.runtime.preprocess import letterbox                         # noqa: E402

APP_DIR = os.path.join(_REPO, "apps", "fitness-trainer")

FRAME_W, FRAME_H = 640, 480     # camera frame: NOT square (letterbox pads y)
DET_SIZE = 640                  # manifest models[0].input
N_GREY = 2                      # camera warm-up placeholders, both paths skip
DT = 0.4                        # seconds between frames
POSE_MODEL = "models/yolo11n_pose_rawhead_int8.rknn"

GRID = 20                       # single FPN level: 20x20 @ stride 32
STRIDE = DET_SIZE // GRID
REG_MAX = 16
HALF_BIN = 2                    # DFL bin -> box half-side = 2 * 32 = 64 px
N_KPT = 17
CELL_COL, CELL_ROW = 8, 7       # the grid cell that carries the person

# --- the squat script, indexed by INFER CALL number -------------------------- #
# call 0 is the kit warm-up frame (both paths discard it), so processed frame i
# corresponds to call i+1.
ANGLE_SCRIPT = (
    [175.0] * 5 +   # calls  0- 4  standing
    [85.0] * 5 +    # calls  5- 9  bottom      -> phase FLEXED
    [175.0] * 5 +   # calls 10-14  stand up    -> REP 1
    [85.0] * 5 +    # calls 15-19
    [175.0] * 5 +   # calls 20-24  REP 2 -> target_reps=2 -> SET ROLLOVER
    [85.0] * 5 +    # calls 25-29
    [175.0] * 5     # calls 30-34  REP 3 (set 2, reps 1)
)
NO_PERSON_FROM = len(ANGLE_SCRIPT)      # call 35 onwards: nobody in frame
N_NO_PERSON = 7                         # 7 * 0.4 s = 2.8 s > idle_reset 1.0 s
N_REAL = NO_PERSON_FROM + N_NO_PERSON   # 42 real frames offered by the source

KNEE = (300.0, 300.0)           # original-frame px
ANKLE = (300.0, 400.0)
LIMB = 100.0                    # knee->hip length, px
CONF_HI_RAW = 2.2               # sigmoid -> 0.900
CONF_LO_RAW = -2.2              # sigmoid -> 0.100  (below kpt_thres 0.5)

# Effective config: manifest defaults with small targets / idle window so the
# set rollover and the idle reset are both reachable inside the fixture.
EFF = {
    "mode": "squat",
    "target_reps": 2,
    "target_sets": 3,
    "idle_reset_seconds": 1.0,
    "confidence": 0.4,
    "keypoint_confidence": 0.5,
}

# The letterbox geometry the fixture's frames produce (640x480 -> 640).
_LB_INFO = letterbox(np.zeros((FRAME_H, FRAME_W, 3), dtype=np.uint8),
                     DET_SIZE)[1]


def _hip_for_angle(deg: float):
    """Hip position giving `deg` as the interior knee angle (hip-knee-ankle)."""
    th = math.radians(deg)
    return (KNEE[0] + LIMB * math.sin(th), KNEE[1] + LIMB * math.cos(th))


def _raw_kpt(ox: float, oy: float):
    """Original-frame (x, y) -> the raw (x, y) the pose head must emit.

    Inverse of the decoder: original -> letterbox (scale + pad) -> ultralytics
    keypoint decode ``k = (raw * 2 + g) * stride``.
    """
    lx = ox * _LB_INFO.scale + _LB_INFO.pad_w
    ly = oy * _LB_INFO.scale + _LB_INFO.pad_h
    return ((lx / STRIDE - CELL_COL) / 2.0,
            (ly / STRIDE - CELL_ROW) / 2.0)


def _fixed_frames():
    """N_GREY flat-grey warm-up frames, then N_REAL real ones, pts DT apart."""
    out = []
    for i in range(N_GREY):
        data = np.full((FRAME_H, FRAME_W, 3), 114, dtype=np.uint8)
        out.append(Frame(data=data, w=FRAME_W, h=FRAME_H, fmt="RGB",
                         pts=100.0 + i))
    for i in range(N_REAL):
        rng = np.random.default_rng(9100 + i)
        data = rng.integers(0, 256, (FRAME_H, FRAME_W, 3), dtype=np.uint8)
        out.append(Frame(data=data, w=FRAME_W, h=FRAME_H, fmt="RGB",
                         pts=400.0 + i * DT))
    return out


class _FakeSource:
    """Fake camera honouring the frame-source flags `start()` / `run()` pass.

    `direct_preprocess=True` (`model_frame = "hw-direct"`) is emulated
    faithfully: the letterboxed model image REPLACES `data` while `w`/`h` stay
    the original camera geometry. fitness-trainer never reads `frame.data`, so
    this is the correct mode for it -- `test_hw_direct_is_actually_in_effect`
    pins that the fixture really exercises it.
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

    One person (score 0.91) whose LEFT hip-knee-ankle triplet is confidently
    visible and traces ANGLE_SCRIPT; the right side sits below the keypoint
    threshold so the state machine always picks the left. From
    NO_PERSON_FROM on, the whole class branch is below threshold -> no person.
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
        # every keypoint starts invisible and parked at the knee
        kx, ky = _raw_kpt(*KNEE)
        for j in range(N_KPT):
            kpt[0, j * 3 + 0, CELL_ROW, CELL_COL] = kx
            kpt[0, j * 3 + 1, CELL_ROW, CELL_COL] = ky
            kpt[0, j * 3 + 2, CELL_ROW, CELL_COL] = CONF_LO_RAW

        if k < NO_PERSON_FROM:
            cls[0, 0, CELL_ROW, CELL_COL] = 0.91
            for side in range(4):
                box[0, side * REG_MAX + HALF_BIN, CELL_ROW, CELL_COL] = 12.0
            hip = _hip_for_angle(ANGLE_SCRIPT[k])
            for j, pt in ((11, hip), (13, KNEE), (15, ANKLE)):   # LEFT h/k/a
                rx, ry = _raw_kpt(*pt)
                kpt[0, j * 3 + 0, CELL_ROW, CELL_COL] = rx
                kpt[0, j * 3 + 1, CELL_ROW, CELL_COL] = ry
                kpt[0, j * 3 + 2, CELL_ROW, CELL_COL] = CONF_HI_RAW
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


# ---- OLD shape: verbatim copy of the pre-migration fitness-trainer -------- #
class _LegacyFitnessApp(LegacyLoopApp):
    """fitness-trainer exactly as it was before the migration (git 75bd143)."""

    id = "fitness-trainer"
    name = "Fitness Trainer"
    postproc = "pose"
    model_frame = "hw-direct"

    def setup(self, config):
        super().setup(config)
        params = {k: v for k, v in (config or {}).items() if v is not None}

        self.conf = float(params.get("confidence", 0.4))
        self.kpt_thres = float(params.get("keypoint_confidence", 0.5))

        mode = str(params.get("mode", "squat"))
        self.target_reps = int(params.get("target_reps", 12))
        self.target_sets = int(params.get("target_sets", 3))
        self.idle_reset_seconds = float(params.get("idle_reset_seconds", 60))

        self.exercise = create_exercise(mode, self.kpt_thres)
        if self.exercise is None:
            mode = "squat"
            self.exercise = create_exercise(mode, self.kpt_thres)
        self.mode = mode
        self.exercise.set_targets(self.target_reps, self.target_sets)

        self._last_person_pts = None
        self._last_workout_complete = False

    def on_config_reload(self, config):
        params = self._reload_params(config)
        self.config = config or {}
        self.conf = self._reload_float(params, "confidence", self.conf)
        self.kpt_thres = self._reload_float(params, "keypoint_confidence",
                                            self.kpt_thres)
        self.target_reps = self._reload_int(params, "target_reps", self.target_reps)
        self.target_sets = self._reload_int(params, "target_sets", self.target_sets)
        self.idle_reset_seconds = self._reload_float(
            params, "idle_reset_seconds", self.idle_reset_seconds)

        new_mode = str(params.get("mode", self.mode))
        if new_mode != self.mode:
            ex = create_exercise(new_mode, self.kpt_thres)
            if ex is None:
                pass                      # unknown mode: keep the current one
            else:
                self.exercise = ex
                self.mode = new_mode
        else:
            self.exercise.kpt_thres = self.kpt_thres
        self.exercise.set_targets(self.target_reps, self.target_sets)

    def run_postproc(self, outs, info):
        return pose_post.postprocess(outs, info, conf_thres=self.conf,
                                     iou_thres=self.iou,
                                     kpt_thres=self.kpt_thres)

    def on_results(self, results, frame):
        primary = results[0] if results else None
        now = frame.pts

        if primary is not None:
            self._last_person_pts = now
        elif (self.idle_reset_seconds > 0 and self._last_person_pts is not None
              and now - self._last_person_pts >= self.idle_reset_seconds):
            self.exercise.reset()
            self.exercise.set_targets(self.target_reps, self.target_sets)
            self._last_person_pts = None
            self._last_workout_complete = False

        st = self.exercise.update(primary, now)

        event = {
            "kind": "workout",
            "mode": self.mode,
            "target_reps": self.target_reps,
            "target_sets": self.target_sets,
            "person_score": round(float(primary["score"]), 3) if primary else 0.0,
        }
        event.update(st.as_dict())
        events = [event]
        self._last_workout_complete = st.workout_complete

        for r in results:
            r.setdefault("kind", "person")
        return events


def _load_new_app_module():
    path = os.path.join(APP_DIR, "app.py")
    spec = importlib.util.spec_from_file_location(
        "_fitness_trainer_app_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


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
        app = _LegacyFitnessApp()
        app.setup(dict(eff))
        app.run(POSE_MODEL, source="ffmpeg", sink=sink, n=0, verbose=False)
        return sink, app

    def _run_new(self, eff, cls=None):
        sink = _RecordingSink()
        app = (cls or _load_new_app_module().FitnessTrainerApp)()
        app.start(POSE_MODEL, source="ffmpeg", sink=sink, n=0, verbose=False,
                  app_dir=APP_DIR, manifest=self.manifest, config=dict(eff))
        try:
            app.run()
        finally:
            app.finish()
        return sink, app


class FitnessEquivalenceTests(_Base):

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

    def test_deep_equal(self):
        old_s, _new_s, old_app, new_app = self._compare(EFF, "default")

        evs = [e for p, _ in old_s for e in p["events"]]
        kinds = {k: sum(1 for e in evs if e["kind"] == k)
                 for k in sorted({e["kind"] for e in evs})}
        print("\n--- event kind distribution (old path) ---")
        print(kinds)
        print(f"final exercise state: old reps={old_app.exercise.state.reps} "
              f"set={old_app.exercise.state.set} | "
              f"new reps={new_app.exercise.state.reps} "
              f"set={new_app.exercise.state.set}")

        # -- anti-vacuous-pass assertions --------------------------------- #
        self.assertGreater(len(evs), 0, "fixture produced no events")
        self.assertEqual(kinds.get("workout", 0), len(old_s),
                         "one workout event per frame is the contract")
        self.assertGreater(sum(1 for e in evs if e["rep_completed"]), 0,
                           "no rep ever completed -- the fixture is vacuous")
        self.assertGreater(sum(1 for e in evs if e["set_completed"]), 0,
                           "no set ever completed")
        self.assertGreater(max(e["reps"] for e in evs), 0,
                           "the rep counter never left zero")
        self.assertGreater(max(e["set"] for e in evs), 1,
                           "the set counter never rolled over")
        self.assertGreater(len({e["stage"] for e in evs}), 1,
                           "the stage never changed")
        self.assertTrue(any(e["tracking"] for e in evs), "never tracked")
        self.assertTrue(any(not e["tracking"] for e in evs),
                        "the no-person branch never ran")
        # results must carry keypoints (the whole point of a pose app)
        res = [r for p, _ in old_s for r in p["results"]]
        self.assertGreater(len(res), 0, "no person was ever detected")
        self.assertTrue(all(r["kind"] == "person" for r in res))
        self.assertTrue(all(len(r["keypoints"]) == N_KPT for r in res))

    def test_cross_frame_state_frame_by_frame(self):
        """reps / set / stage / angle / tracking, printed and compared per frame."""
        old, _ = self._run_old(EFF)
        new, _ = self._run_new(EFF)

        def _table(sink):
            rows = []
            for p, pts in sink.payloads:
                e = [x for x in p["events"] if x["kind"] == "workout"][0]
                rows.append((round(pts, 2), e["mode"], e["reps"], e["set"],
                             e["stage"], e["angle"], e["tracking"],
                             e["rep_completed"], e["set_completed"],
                             e["workout_complete"], e["target_reps"],
                             e["target_sets"], e["person_score"]))
            return rows

        old_tbl, new_tbl = _table(old), _table(new)
        header = ("pts, mode, reps, set, stage, angle, tracking, rep_done, "
                  "set_done, workout_done, tgt_reps, tgt_sets, person_score")
        print(f"\n--- per-frame ({header}) ---")
        for i, (o, n) in enumerate(zip(old_tbl, new_tbl)):
            print(f"frame {i:2d}: OLD {o}")
            print(f"frame {i:2d}: NEW {n}")
            print(f"frame {i:2d}: EQUAL={o == n}")
        self.assertEqual(old_tbl, new_tbl, "cross-frame state diverged")

        # ★idle reset★: reps/set must be back at 0 / 1 on the last frame, and
        # they must have been HIGHER earlier -- otherwise the reset is vacuous.
        self.assertEqual((old_tbl[-1][2], old_tbl[-1][3]), (0, 1),
                         "the idle reset did not zero the workout")
        self.assertGreater(max(r[3] for r in old_tbl), 1,
                           "the set counter never rose, so the reset proves nothing")
        # and it really is the IDLE path: the last frames have no person
        self.assertEqual(old_tbl[-1][12], 0.0, "the tail frames still had a person")

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


class FitnessHotReloadTests(_Base):
    """★The load-bearing part★: what a SIGHUP does to the state machine."""

    def _started(self, eff=None):
        app = _load_new_app_module().FitnessTrainerApp()
        app.start(POSE_MODEL, sink=_RecordingSink(), verbose=False,
                  app_dir=APP_DIR, manifest=self.manifest,
                  config=dict(eff or EFF))
        return app

    def _legacy(self, eff=None):
        app = _LegacyFitnessApp()
        app.setup(dict(eff or EFF))
        return app

    @staticmethod
    def _seed(app, reps=5, sets=2):
        """Put some accumulated workout on the state machine."""
        app.exercise.state.reps = reps
        app.exercise.state.set = sets

    def test_params_auto_bound_from_manifest_schema(self):
        app = self._started({"mode": "push_up", "target_reps": 7,
                             "target_sets": 4, "idle_reset_seconds": 30,
                             "confidence": 0.6, "keypoint_confidence": 0.35})
        try:
            self.assertEqual(app.mode, "push_up")
            self.assertEqual(int(app.target_reps), 7)
            self.assertEqual(int(app.target_sets), 4)
            self.assertEqual(app.confidence, 0.6)
            self.assertEqual(app.keypoint_confidence, 0.35)
            self.assertEqual(app.exercise.id, "push_up")
            self.assertEqual(app.exercise.kpt_thres, 0.35)
            self.assertEqual(app.exercise.target_reps, 7)
            self.assertEqual(app.exercise.target_sets, 4)
        finally:
            app.finish()

    def test_mode_change_rebuilds_the_state_machine_and_zeroes_reps(self):
        """★mode changed -> NEW state machine, reps back to 0 (old + new)★."""
        new_app = self._started()
        old_app = self._legacy()
        try:
            self._seed(new_app), self._seed(old_app)
            before_new, before_old = new_app.exercise, old_app.exercise
            print(f"\n[mode CHANGE] before: old reps={old_app.exercise.state.reps} "
                  f"set={old_app.exercise.state.set} id={old_app.exercise.id} | "
                  f"new reps={new_app.exercise.state.reps} "
                  f"set={new_app.exercise.state.set} id={new_app.exercise.id}")

            cfg = dict(EFF, mode="push_up")
            old_app.on_config_reload(cfg)
            changed = new_app._bind_params(cfg, live_only=True)
            self.assertIn("mode", changed)
            new_app.on_params_changed(changed)

            print(f"[mode CHANGE] after : old reps={old_app.exercise.state.reps} "
                  f"set={old_app.exercise.state.set} id={old_app.exercise.id} | "
                  f"new reps={new_app.exercise.state.reps} "
                  f"set={new_app.exercise.state.set} id={new_app.exercise.id}")

            for app, before in ((new_app, before_new), (old_app, before_old)):
                self.assertIsNot(app.exercise, before,
                                 "the state machine was NOT rebuilt")
                self.assertEqual(app.exercise.id, "push_up")
                self.assertEqual(app.exercise.state.reps, 0)
                self.assertEqual(app.exercise.state.set, 1)
                self.assertEqual(app.mode, "push_up")
                self.assertEqual(app.exercise.target_reps, int(EFF["target_reps"]))
        finally:
            new_app.finish()

    def test_mode_unchanged_keeps_the_accumulator(self):
        """★mode NOT changed -> same object, reps/sets preserved (old + new)★."""
        new_app = self._started()
        old_app = self._legacy()
        try:
            self._seed(new_app), self._seed(old_app)
            before_new, before_old = new_app.exercise, old_app.exercise

            cfg = dict(EFF, target_reps=9, target_sets=5, confidence=0.7)
            old_app.on_config_reload(cfg)
            changed = new_app._bind_params(cfg, live_only=True)
            self.assertNotIn("mode", changed)
            new_app.on_params_changed(changed)

            print(f"\n[mode SAME] changed={sorted(changed)}")
            for tag, app, before in (("old", old_app, before_old),
                                     ("new", new_app, before_new)):
                print(f"[mode SAME] {tag}: same_object={app.exercise is before} "
                      f"reps={app.exercise.state.reps} "
                      f"set={app.exercise.state.set} "
                      f"targets={app.exercise.target_reps}x"
                      f"{app.exercise.target_sets}")
                self.assertIs(app.exercise, before,
                              "the state machine was REBUILT on an unchanged mode")
                self.assertEqual(app.exercise.state.reps, 5)
                self.assertEqual(app.exercise.state.set, 2)
                # set_targets replaces the targets WITHOUT resetting the counts
                self.assertEqual(app.exercise.target_reps, 9)
                self.assertEqual(app.exercise.target_sets, 5)
        finally:
            new_app.finish()

    def test_keypoint_confidence_mutates_in_place(self):
        """★kpt_thres is mutated on the LIVE object, never via a rebuild★."""
        new_app = self._started()
        old_app = self._legacy()
        try:
            self._seed(new_app), self._seed(old_app)
            before_new, before_old = new_app.exercise, old_app.exercise

            cfg = dict(EFF, keypoint_confidence=0.85)
            old_app.on_config_reload(cfg)
            changed = new_app._bind_params(cfg, live_only=True)
            self.assertEqual(changed, {"keypoint_confidence"})
            new_app.on_params_changed(changed)

            print(f"\n[kpt_thres] old: same_object={old_app.exercise is before_old} "
                  f"kpt_thres={old_app.exercise.kpt_thres} "
                  f"reps={old_app.exercise.state.reps}")
            print(f"[kpt_thres] new: same_object={new_app.exercise is before_new} "
                  f"kpt_thres={new_app.exercise.kpt_thres} "
                  f"reps={new_app.exercise.state.reps}")
            for app, before in ((new_app, before_new), (old_app, before_old)):
                self.assertIs(app.exercise, before, "the state machine was REBUILT")
                self.assertEqual(app.exercise.kpt_thres, 0.85)
                self.assertEqual(app.exercise.state.reps, 5)
                self.assertEqual(app.exercise.state.set, 2)
        finally:
            new_app.finish()

    def test_unknown_mode_is_refused_and_state_survives(self):
        new_app = self._started()
        old_app = self._legacy()
        try:
            self._seed(new_app), self._seed(old_app)
            before_new, before_old = new_app.exercise, old_app.exercise
            self.assertNotIn("burpee", exercise_ids())

            cfg = dict(EFF, mode="burpee")
            old_app.on_config_reload(cfg)
            changed = new_app._bind_params(cfg, live_only=True)
            new_app.on_params_changed(changed)

            print(f"\n[bad mode] old mode={old_app.mode} "
                  f"reps={old_app.exercise.state.reps} | "
                  f"new mode={new_app.mode} reps={new_app.exercise.state.reps}")
            for app, before in ((new_app, before_new), (old_app, before_old)):
                self.assertIs(app.exercise, before)
                self.assertEqual(app.mode, "squat", "the bad mode stuck")
                self.assertEqual(app.exercise.state.reps, 5)
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


class FitnessFrameModeTests(_Base):
    """`model_frame = "hw-direct"` is correct here -- and the fixture proves it."""

    def test_new_app_keeps_hw_direct(self):
        mod = _load_new_app_module()
        self.assertEqual(mod.FitnessTrainerApp.model_frame, "hw-direct",
                         "fitness-trainer consumes keypoints only; keep hw-direct")

    def test_hw_direct_is_actually_in_effect(self):
        """Reverse control: the CPU mode must reach the model differently.

        Under "hw-direct" the source hands `pre()` an already-letterboxed
        `frame.data` (+ `model_info`); under "cpu" the Python letterbox runs.
        Both produce a 640x640 model input, so the check that the fixture really
        exercises the hw path is that the source was ASKED for it.
        """
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

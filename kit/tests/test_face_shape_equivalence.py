"""
face-analysis regression gate: the stage-1 contract that must NOT move, plus the
four accuracy behaviours of 0.2.0 that must not silently move back.

This file started life as the app-shape migration equivalence gate
(KIT_APP_SHAPE_SPEC §7): run the pre-migration implementation and the migrated
one over the same fake frames and deep-equal `results` / `events`. That gate has
been retired on purpose. 0.2.0 changed what the app *means* by an attribute, so
"identical to the old output" is now the wrong answer -- keeping the deep-equal
assertion would have pinned four measured-wrong behaviours in place.

What is still compared against the old implementation
-----------------------------------------------------
Stage 1 did not change, so the legacy loop (`_LegacyFaceApp`, the pre-migration
app reproduced verbatim below) is still the reference for everything upstream of
the cascade, and only for that:

  * the detection boxes and scores in `results[]`,
  * `pts` / `stream_id` / the emitted frame count / the grey-skip behaviour,
  * the number of stage-1 detector inferences,
  * where stages 2/3 get their pixels from (the ORIGINAL camera frame, not the
    stage-1 letterbox) and at what size.

Deliberately NOT compared any more: event bodies, every attribute field, the
demographics aggregate, and the stage-2/3 inference counts. Each of those is
supposed to differ now. The stage-2/3 counts in particular happen to match on
this fixture (its faces are 128 px, comfortably over the default 64 px gate),
and no assertion in this file may lean on that coincidence -- change the fixture
face size and the gate legitimately changes the count.

What is pinned as new behaviour (`FaceAccuracyBehaviourTests`)
--------------------------------------------------------------
One test per accuracy fix, each written so that reverting the fix turns it red:

  1. `blur` / `kind` are stamped on EVERY detection, before the top-K slice --
     the previous version tagged only `results[:max_faces]`, so in exactly the
     crowded scene where blurring matters the extra faces went out unflagged.
  2. The demographic histogram counts PEOPLE: `faces` is the number of tracks
     that first became stable inside the window, and it is strictly below
     `face_frames` (the old, dwell-weighted number, kept alongside) whenever
     anyone stays for more than one frame.
  3. Emotion is remembered per `track_id`, not per list slot. The previous
     version cached the verdict under the face's index in the SCORE-SORTED list,
     so a score swap between two people handed one of them the other's emotion.
     The test drives exactly that swap and, as a negative control, shows the
     legacy implementation failing the same assertion on the same fixture --
     which is what proves the assertion has teeth.
  4. Faces below `min_face_px` are gated out before the crop: no stage-2/3
     inference is spent, the attributes are `None` rather than a confident-
     looking guess, and the detection still appears in `results[]` (gating is
     not dropping).
  5. Every reported label is the argmax of the evidence ACCUMULATED over that
     track's frames, so a minority frame does not flip the verdict, and
     `*_conf` is the winner's vote share rather than one frame's softmax.
  6. `track_id` is stable across frames and `stable` only turns true once
     `min_track_frames` of evidence have accumulated.

Hardware-free: the frame source (`kit.app.open_frame_source`) and the RKNN
engine (`kit.app.App._load_model`) are stubbed with deterministic fakes. The
default fakes are seeded BY CALL NUMBER so the k-th inference is reproducible;
tests that need to prove an identity claim swap in a fake that is seeded by ROI
CONTENT instead, because "did this face get its own emotion" is unanswerable
against a model whose output does not depend on which face it was shown.

Run: `python3 -m pytest kit/tests/test_face_shape_equivalence.py -q`
"""
import importlib.util
import json
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
from kit import pipeline                                             # noqa: E402
from kit.adapters.frame_source import Frame                          # noqa: E402
from kit.adapters.result_sink import ResultSink                      # noqa: E402
from kit.runtime.postprocess import classify as clf                  # noqa: E402
from kit.runtime.postprocess import face_detect as face_post         # noqa: E402
from kit.runtime.preprocess import letterbox                         # noqa: E402

APP_DIR = os.path.join(_REPO, "apps", "face-analysis")

FRAME_W, FRAME_H = 640, 480     # camera frame: NOT square, NOT the model size
DET_SIZE = 640                  # stage-1 input side (manifest models[0].input)
CLS_SIZE = 224                  # stage-2/3 classifier input side
N_GREY = 2                      # camera warm-up placeholders, both paths skip
N_REAL = 10                     # real frames offered by the fake source
DT = 0.2                        # seconds between frames

DET_MODEL = "models/yolov8n_face_rawhead_fp16.rknn"

GRID = 20                       # single FPN level: 20x20 @ stride 32
STRIDE = DET_SIZE // GRID
REG_MAX = 16
HALF_BIN = 2                    # DFL bin -> box half-side = 2 * 32 = 64 px

# The fake source hands out 640x480 frames, so `letterbox(.., 640)` scales by
# 1.0 and pads (640-480)/2 = 80 px top and bottom. A face scripted at grid cell
# (col,row) therefore lands at this ORIGINAL-frame centre, and is 2*HALF_BIN*
# STRIDE = 128 px on a side -- comfortably above the 64 px default gate, which
# is why the default fixture never trips it.
LB_PAD_Y = (DET_SIZE - FRAME_H) // 2
FACE_SIDE_PX = 2 * HALF_BIN * STRIDE


def _cell_center(col, row):
    """ORIGINAL-frame centre of a face scripted at detector grid cell (col,row)."""
    return ((col + 0.5) * STRIDE, (row + 0.5) * STRIDE - LB_PAD_Y)


# The two faces the identity fixtures use. A sits left-of-centre, B right.
CELL_A = (4, 6)                 # centre (144, 128)
CELL_B = (12, 5)                # centre (400,  96)
AX, _AY = _cell_center(*CELL_A)
BX, _BY = _cell_center(*CELL_B)
AB_SPLIT = (AX + BX) / 2.0      # x threshold telling an A box from a B box

# Effective config, as kit.config would hand it over (manifest defaults, except
# the three tuned so the fixture actually exercises the branches):
#   max_faces 2 (< the 3 faces the fake detector emits)  -> top-K slice is live
#   emotion_interval 3                                   -> stage 3 skips frames
#   aggregate_window_sec 0.5 (with DT=0.2)               -> the window fires
EFF = {
    "confidence": 0.4,
    "iou": 0.45,
    "max_faces": 2.0,
    "crop_pad": 0.15,
    "emotion_interval": 3.0,
    "aggregate_window_sec": 0.5,
    "privacy_blur": True,
}


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
                         pts=300.0 + i * DT))
    return out


def _painted_frames():
    """Like `_fixed_frames`, but face A's neighbourhood is painted DARK and face
    B's BRIGHT, so a content-seeded classifier fake can tell the two people apart
    from the ROI pixels alone. The painted rectangles are wider than the padded
    square ROI each face produces, so the whole crop is one flat value.
    """
    out = []
    for i in range(N_GREY):
        data = np.full((FRAME_H, FRAME_W, 3), 114, dtype=np.uint8)
        out.append(Frame(data=data, w=FRAME_W, h=FRAME_H, fmt="RGB",
                         pts=100.0 + i))
    for i in range(N_REAL):
        rng = np.random.default_rng(7700 + i)
        data = rng.integers(0, 256, (FRAME_H, FRAME_W, 3), dtype=np.uint8)
        data[24:232, 40:248] = 20        # around A (144,128): dark
        data[0:200, 296:504] = 220       # around B (400, 96): bright
        out.append(Frame(data=data, w=FRAME_W, h=FRAME_H, fmt="RGB",
                         pts=300.0 + i * DT))
    return out


# ---- scripted model behaviours (overridable per test) -------------------- #
def _default_det_faces(k):
    """Detector script: (col, row, score) per inference. det call 0 is the kit
    warm-up, so processed frame index == k - 1.

    Three faces, the first drifting sideways with the call number, scores
    0.91 / 0.85 / 0.62 so `max_faces = 2` really drops one; call 5 (processed
    frame 4) has ★no face at all★ -- empty top-K, no ROI, no classifier.
    """
    if k == 5:
        return []
    return [(4 + (k % 3), 6, 0.91), (12, 5, 0.85), (8, 12, 0.62)]


def _default_ff_vec(k):
    """FairFace script: (race_idx, gender_idx, age_idx) per inference."""
    return (k % 7, k % 2, k % 9)


def _default_emo_idx(k, roi):
    """Emotion script: class index per inference, seeded by call number."""
    return k % 8


class _FakeSource:
    """Fake camera that HONOURS the frame-source flags `start()` passes it.

    `direct_preprocess=True` (what `model_frame = "hw-direct"` asks for) is
    emulated faithfully: the letterboxed model image replaces `data` while
    `w`/`h` stay the original camera geometry -- exactly what would break the
    stage-2/3 ROI crop. That makes the crop-source assertion a real regression
    detector rather than a restatement of the class attribute.
    """

    def __init__(self, *a, frames_fn=None, **kw):
        self.closed = False
        self.kw = kw
        self.frames_fn = frames_fn or _fixed_frames

    def frames(self):
        direct = bool(self.kw.get("direct_preprocess"))
        size = int(self.kw.get("input_size") or 0)
        for f in self.frames_fn():
            if direct and size:
                padded, info = letterbox(f.data, size)
                f = Frame(data=padded, w=f.w, h=f.h, fmt=f.fmt, pts=f.pts,
                          model_info=info)
            yield f

    def close(self):
        self.closed = True


class _FakeDetModel:
    """A scripted yolov8n-face rawhead: 1 box branch + 1 class branch per call.

    One FPN level (20x20 @ stride 32) is enough for `_decode_dfl`: it pairs the
    64-channel DFL box branch with the 1-channel face-score branch. Which faces
    each call emits comes from `faces_fn` (see `_default_det_faces`).
    """

    def __init__(self, path, faces_fn=None):
        self.path = path
        self.calls = 0
        self.input_shapes = []
        self.released = False
        self.faces_fn = faces_fn or _default_det_faces

    def infer(self, x):
        self.input_shapes.append(tuple(np.asarray(x).shape))
        k = self.calls
        self.calls += 1
        box = np.zeros((1, 4 * REG_MAX, GRID, GRID), dtype=np.float32)
        cls = np.full((1, 1, GRID, GRID), 0.02, dtype=np.float32)
        for col, row, score in self.faces_fn(k):
            cls[0, 0, row, col] = score
            for side in range(4):
                box[0, side * REG_MAX + HALF_BIN, row, col] = 12.0
        return [box, cls]

    def release(self):
        self.released = True


class _FakeFairFaceModel:
    """A scripted FairFace head: one (1,18) logit vector per call.

    `vec_fn(k) -> (race_idx, gender_idx, age_idx)` picks which class each of the
    three heads votes for; the default moves all three with the call number so
    the k-th call of any two runs is byte-identical.
    """

    def __init__(self, path, vec_fn=None):
        self.path = path
        self.calls = 0
        self.input_shapes = []
        self.released = False
        self.vec_fn = vec_fn or _default_ff_vec

    def infer(self, x):
        self.input_shapes.append(tuple(np.asarray(x).shape))
        k = self.calls
        self.calls += 1
        race, gender, age = self.vec_fn(k)
        vec = np.full((1, 18), 0.1, dtype=np.float32)
        vec[0, 0 + (race % 7)] = 3.0       # race head  [0:7]
        vec[0, 7 + (gender % 2)] = 2.5     # gender head [7:9]
        vec[0, 9 + (age % 9)] = 4.0        # age head    [9:18]
        return [vec]

    def release(self):
        self.released = True


class _FakeEmotionModel:
    """A scripted emotion head: one (1,8) logit vector per call.

    `idx_fn(k, roi) -> class index`. The default is seeded by call number; the
    identity tests swap in one seeded by the ROI PIXELS, which is the only way
    to ask "did this face get its own emotion back".
    """

    def __init__(self, path, idx_fn=None):
        self.path = path
        self.calls = 0
        self.input_shapes = []
        self.released = False
        self.idx_fn = idx_fn or _default_emo_idx

    def infer(self, x):
        roi = np.asarray(x)
        self.input_shapes.append(tuple(roi.shape))
        k = self.calls
        self.calls += 1
        vec = np.full((1, 8), 0.1, dtype=np.float32)
        vec[0, int(self.idx_fn(k, roi)) % 8] = 3.0
        return [vec]

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


# ---- OLD shape: verbatim copy of the pre-migration face-analysis --------- #
class _LegacyFaceApp(LegacyLoopApp):
    """face-analysis exactly as it was before the migration (git d5a40d3).

    Kept for two jobs, and no others:
      * it is the reference for the STAGE-1 contract, which 0.2.0 did not touch;
      * it is the negative control for the per-slot emotion cache -- running it
        over the score-swap fixture is what demonstrates that the identity
        assertion in `FaceAccuracyBehaviourTests` can actually fail.

    Only three mechanical deviations, all test-harness plumbing:
      * the manifest is handed in rather than re-read off disk;
      * the stage-2/3 models are built through `self._load_model(path)` instead
        of `RknnModel(path)` -- `_load_model` *is* `RknnModel(path)`
        (kit/app.py), and routing through it lets one stub cover both paths
        (importing kit.runtime.engine off-device would need rknnlite);
      * `crop_square_roi` is reached through the `pipeline` module so the same
        spy sees both paths (it is the identical function object).
    """
    id = "face-analysis"
    name = "Face Analysis"
    postproc = "face_detect"

    def __init__(self, manifest):
        super().__init__()
        self._legacy_manifest = manifest

    def setup(self, config):
        super().setup(config)
        manifest = self._legacy_manifest
        params = {k: v for k, v in (config or {}).items() if v is not None}

        self.conf = float(params.get("confidence", 0.4))
        self.iou = float(params.get("iou", 0.45))
        self.max_faces = int(params.get("max_faces", 5))
        self.crop_pad = float(params.get("crop_pad", 0.15))

        self.emotion_interval = max(1, int(params.get("emotion_interval", 1)))
        self.agg_window = float(params.get("aggregate_window_sec", 30.0))
        self.privacy_blur = bool(params.get("privacy_blur", True))

        ff_file, ff_input = "models/fairface_fp16.rknn", 224
        emo_file, emo_input = "models/emotion_enet_b0_fp16.rknn", 224
        for m in manifest.get("models", []):
            role = m.get("role")
            inp = m.get("input")
            side = int(inp[1]) if isinstance(inp, list) and len(inp) == 4 else None
            if role == "stage2_fairface" or m.get("id", "").startswith("fairface"):
                ff_file = m.get("file", ff_file)
                if side:
                    ff_input = side
            elif role == "stage3_emotion" or m.get("id", "").startswith("emotion"):
                emo_file = m.get("file", emo_file)
                if side:
                    emo_input = side

        def _abs(p):
            return p if os.path.isabs(p) else os.path.join(APP_DIR, p)

        self.ff_input = ff_input
        self.emo_input = emo_input
        self.ff_model = self._load_model(_abs(ff_file))
        self.emo_model = self._load_model(_abs(emo_file))

        self._win_start = None
        self._win_faces = 0
        self._hist = {"gender": {}, "age": {}, "race": {}, "emotion": {}}
        self._frame_idx = 0
        self._emotion_cache = {}

    def run_postproc(self, outs, info):
        return face_post.postprocess(outs, info, conf_thres=self.conf,
                                     iou_thres=self.iou)

    def _bump(self, head, label):
        if label is None:
            return
        d = self._hist[head]
        d[label] = d.get(label, 0) + 1

    def _roll_window(self, t):
        if self._win_start is None:
            self._win_start = t
            return None
        if (t - self._win_start) < self.agg_window:
            return None
        event = {
            "kind": "demographics",
            "window_sec": round(float(t - self._win_start), 1),
            "faces": int(self._win_faces),
            "gender": dict(self._hist["gender"]),
            "age": dict(self._hist["age"]),
            "race": dict(self._hist["race"]),
            "emotion": dict(self._hist["emotion"]),
        }
        self._win_start = t
        self._win_faces = 0
        for k in self._hist:
            self._hist[k] = {}
        return event

    def on_results(self, results, frame):
        self._frame_idx += 1
        t = frame.pts
        run_emotion = (self._frame_idx % self.emotion_interval) == 0

        faces = results[: self.max_faces]
        for i, r in enumerate(faces):
            r["kind"] = "face"
            r["blur"] = self.privacy_blur

            roi, _roi_map = pipeline.crop_square_roi(frame.data, r["box"],
                                                     self.ff_input,
                                                     self.crop_pad)

            ff = clf.fairface_decode(self.ff_model.infer(roi))
            r["gender"] = ff["gender"]["label"]
            r["gender_conf"] = ff["gender"]["confidence"]
            r["age"] = ff["age"]["label"]
            r["age_conf"] = ff["age"]["confidence"]
            r["race"] = ff["race"]["label"]
            r["race_conf"] = ff["race"]["confidence"]

            if run_emotion:
                if self.emo_input != self.ff_input:
                    roi_e, _ = pipeline.crop_square_roi(frame.data, r["box"],
                                                        self.emo_input,
                                                        self.crop_pad)
                else:
                    roi_e = roi
                em = clf.emotion_decode(self.emo_model.infer(roi_e))
                self._emotion_cache[i] = em
            em = self._emotion_cache.get(i)
            if em is not None:
                r["emotion"] = em["label"]
                r["emotion_conf"] = em["confidence"]

            self._win_faces += 1
            self._bump("gender", r.get("gender"))
            self._bump("age", r.get("age"))
            self._bump("race", r.get("race"))
            if em is not None:
                self._bump("emotion", em["label"])

        events = []
        for r in faces:
            events.append({
                "kind": "face",
                "box": r["box"],
                "score": r.get("score"),
                "gender": r.get("gender"),
                "gender_conf": r.get("gender_conf"),
                "age": r.get("age"),
                "age_conf": r.get("age_conf"),
                "race": r.get("race"),
                "race_conf": r.get("race_conf"),
                "emotion": r.get("emotion"),
                "emotion_conf": r.get("emotion_conf"),
                "blur": bool(self.privacy_blur),
            })

        agg = self._roll_window(t)
        if agg is not None:
            events.append(agg)
        return events


def _load_new_app_module():
    path = os.path.join(APP_DIR, "app.py")
    spec = importlib.util.spec_from_file_location(
        "_face_analysis_app_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    # The app does `from kit.pipeline import crop_square_roi`, so re-point the
    # module-level name at whatever kit.pipeline currently exposes (the spy).
    mod.crop_square_roi = pipeline.crop_square_roi
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


def _stage1_only(payloads):
    """Reduce a payload stream to the STAGE-1 contract: per frame, the
    detection boxes and scores, plus pts and stream_id.

    Everything the cascade writes onto `results[]` after post-processing --
    kind / blur / track_id / gated / stable / the attribute fields -- is dropped
    here, because 0.2.0 deliberately changed all of it. What is left is what the
    detector produced, which did not change.
    """
    out = []
    for payload, pts in payloads:
        dets = [{"box": r["box"], "score": r.get("score")}
                for r in payload.get("results", [])]
        out.append((pts, payload.get("stream_id"), dets))
    return out


def _face_events(payload):
    return [e for e in payload["events"] if e["kind"] == "face"]


def _demographics(payload):
    return [e for e in payload["events"] if e["kind"] == "demographics"]


def _side(box):
    """Which scripted person is this box? 'A' (left) or 'B' (right)."""
    cx = (box[0] + box[2]) / 2.0
    return "A" if cx < AB_SPLIT else "B"


class _Base(unittest.TestCase):
    def setUp(self):
        self._orig_open = kit_app.open_frame_source
        self._orig_load = kit_app.App._load_model
        self._orig_crop = pipeline.crop_square_roi

        self.det_models = []
        self.ff_models = []
        self.emo_models = []
        self.crop_source_shapes = []
        self.crop_out_sizes = []

        # Scripted model / camera behaviour. A test overrides these BEFORE it
        # runs an app; the defaults reproduce the original fixture exactly.
        self.frames_fn = _fixed_frames
        self.det_faces_fn = _default_det_faces
        self.ff_vec_fn = _default_ff_vec
        self.emo_idx_fn = _default_emo_idx

        def _fake_load(app_self, path):
            base = os.path.basename(path)
            if "fairface" in base:
                m = _FakeFairFaceModel(path, vec_fn=self.ff_vec_fn)
                self.ff_models.append(m)
            elif "emotion" in base:
                m = _FakeEmotionModel(path, idx_fn=self.emo_idx_fn)
                self.emo_models.append(m)
            else:
                m = _FakeDetModel(path, faces_fn=self.det_faces_fn)
                self.det_models.append(m)
            return m

        def _spy_crop(frame, box, out_size, pad=0.25):
            # ★the load-bearing assertion source★: what pixels do stages 2/3 get?
            self.crop_source_shapes.append(tuple(np.asarray(frame).shape))
            self.crop_out_sizes.append(int(out_size))
            return self._orig_crop(frame, box, out_size, pad)

        kit_app.open_frame_source = (
            lambda *a, **kw: _FakeSource(*a, frames_fn=self.frames_fn, **kw))
        kit_app.App._load_model = _fake_load
        pipeline.crop_square_roi = _spy_crop

        with open(os.path.join(APP_DIR, "manifest.json")) as f:
            self.manifest = json.load(f)

    def tearDown(self):
        kit_app.open_frame_source = self._orig_open
        kit_app.App._load_model = self._orig_load
        pipeline.crop_square_roi = self._orig_crop
        try:
            signal.signal(signal.SIGHUP, signal.SIG_DFL)
        except (ValueError, OSError):
            pass

    def _reset_spies(self):
        self.crop_source_shapes = []
        self.crop_out_sizes = []

    def _run_old(self, eff):
        sink = _RecordingSink()
        app = _LegacyFaceApp(self.manifest)
        app.setup(dict(eff))
        app.run(DET_MODEL, source="ffmpeg", sink=sink, n=0, verbose=False)
        return sink, app

    def _run_new(self, eff, cls=None):
        sink = _RecordingSink()
        app = (cls or _load_new_app_module().FaceAnalysisApp)()
        app.start(DET_MODEL, source="ffmpeg", sink=sink, n=0, verbose=False,
                  app_dir=APP_DIR, manifest=self.manifest, config=dict(eff))
        try:
            app.run()
        finally:
            app.finish()
        return sink, app


class FaceStage1ContractTests(_Base):
    """Everything UPSTREAM of the cascade is still byte-identical to the
    pre-migration implementation. Attributes, events and demographics are not
    compared here -- 0.2.0 changed them on purpose; see
    `FaceAccuracyBehaviourTests` for what pins the new behaviour."""

    def _compare(self, eff, label):
        old, _old_app = self._run_old(eff)
        old_crops = set(self.crop_source_shapes)
        old_crop_sizes = set(self.crop_out_sizes)
        old_det_calls = self.det_models[-1].calls
        self._reset_spies()
        new, _new_app = self._run_new(eff)
        new_crops = set(self.crop_source_shapes)
        new_crop_sizes = set(self.crop_out_sizes)
        new_det_calls = self.det_models[-1].calls

        self.assertEqual(len(old.payloads), N_REAL - 1,
                         f"{label}: old path emitted an unexpected frame count")
        self.assertEqual(len(new.payloads), len(old.payloads),
                         f"{label}: new path emitted a different frame count "
                         "(grey-skip / warm-up behaviour moved)")

        old_s = _stage1_only(_strip_timing(old.payloads))
        new_s = _stage1_only(_strip_timing(new.payloads))
        for i, ((pts_o, sid_o, dets_o), (pts_n, sid_n, dets_n)) in enumerate(
                zip(old_s, new_s)):
            self.assertEqual(pts_o, pts_n, f"{label} frame {i}: pts differs")
            self.assertEqual(sid_o, sid_n,
                             f"{label} frame {i}: stream_id differs")
            self.assertEqual(dets_o, dets_n,
                             f"{label} frame {i}: stage-1 detections differ")
        self.assertEqual(old_s, new_s, f"{label}: stage-1 streams differ")
        self.assertEqual(old.frame_sizes, new.frame_sizes)

        self.assertEqual(old_det_calls, new_det_calls,
                         f"{label}: stage-1 detector inference count differs")

        # Crop CONTRACT, not crop count. The number of stage-2/3 crops is now a
        # function of the quality gate (a face under `min_face_px` is skipped),
        # so it is legitimately allowed to differ from the legacy path -- it
        # only happens to match on this fixture because its faces are 128 px.
        # What must NOT differ is WHERE the pixels come from and at what size.
        self.assertTrue(old_crops, f"{label}: old path never cropped an ROI")
        self.assertTrue(new_crops, f"{label}: new path never cropped an ROI")
        self.assertEqual(old_crops, new_crops,
                         f"{label}: stage-2/3 crop SOURCE image differs")
        self.assertEqual(old_crop_sizes, new_crop_sizes,
                         f"{label}: stage-2/3 crop output size differs")
        return old_s, new_s, new

    def test_stage1_contract_unchanged_vs_legacy(self):
        _old_s, new_s, new = self._compare(EFF, "default")

        # -- anti-vacuous-pass assertions --------------------------------- #
        # The stage-1 comparison above is only worth anything if the fixture
        # actually drove the branches the cascade cares about. All of these are
        # read off the NEW path, which is the implementation under test.
        evs = [e for p, _ in new.payloads for e in p["events"]]
        self.assertGreater(len(evs), 0, "fixture produced no events")
        faces = [e for e in evs if e["kind"] == "face"]
        demog = [e for e in evs if e["kind"] == "demographics"]
        self.assertGreater(len(faces), 0, "no face attribute events")
        self.assertGreater(len(demog), 0, "the aggregation window never fired")
        self.assertTrue(any(e["emotion"] is None for e in faces),
                        "every face carried an emotion: the interval never skipped")
        self.assertTrue(any(e["emotion"] is not None for e in faces),
                        "no face ever carried an emotion")
        self.assertTrue(any(len(dets) == 0 for _pts, _sid, dets in new_s),
                        "the no-face frame is missing from the fixture")
        self.assertTrue(
            any(len(p["results"]) > len(_face_events(p))
                for p, _ in new.payloads),
            "max_faces never dropped a detection: the top-K slice is untested")
        print("\n--- event kind distribution (new path) ---")
        print({k: sum(1 for e in evs if e["kind"] == k)
               for k in sorted({e["kind"] for e in evs})})

    def test_stage1_detections_frame_by_frame_vs_legacy(self):
        """Same detections, frame for frame -- printed side by side.

        The old per-frame table compared emotion labels and the demographics
        aggregate too; those are exactly what 0.2.0 changed, so the table now
        carries the stage-1 columns for comparison and prints the new path's
        cascade columns for diagnosis only.
        """
        old, _ = self._run_old(EFF)
        self._reset_spies()
        new, _ = self._run_new(EFF)

        old_tbl = [(round(pts, 2), sid, dets)
                   for pts, sid, dets in _stage1_only(old.payloads)]
        new_tbl = [(round(pts, 2), sid, dets)
                   for pts, sid, dets in _stage1_only(new.payloads)]

        print("\n--- per-frame stage-1 (pts, stream_id, [box/score]) ---")
        for i, (o, n) in enumerate(zip(old_tbl, new_tbl)):
            print(f"frame {i}: dets OLD={len(o[2])} NEW={len(n[2])} "
                  f"EQUAL={o == n}")
        print("--- new-path cascade columns (diagnostic, not compared) ---")
        for i, (p, pts) in enumerate(new.payloads):
            fe = _face_events(p)
            print(f"frame {i}: pts={round(pts, 2)} "
                  f"ids={[e['track_id'] for e in fe]} "
                  f"stable={[e['stable'] for e in fe]} "
                  f"emotion={[e['emotion'] for e in fe]} "
                  f"demographics={_demographics(p) or None}")
        self.assertEqual(old_tbl, new_tbl,
                         "stage-1 detections diverged from the legacy path")

    def test_emotion_interval_cadence(self):
        """Stage 3 must run on frames 3/6/9 only, and cache in between."""
        self._run_new(EFF)
        emo_calls = self.emo_models[-1].calls
        ff_calls = self.ff_models[-1].calls
        print(f"\nfairface calls={ff_calls} emotion calls={emo_calls}")
        self.assertGreater(emo_calls, 0, "stage 3 never ran")
        self.assertLess(emo_calls, ff_calls,
                        "stage 3 ran as often as stage 2: the interval is dead")

    def test_detector_infer_count_matches_legacy(self):
        """Stage 1 runs exactly as often as it used to -- and ONLY stage 1.

        Stage-2/3 counts are deliberately NOT compared. The quality gate skips
        faces below `min_face_px` before the classifiers run, so the new path is
        entitled to spend fewer classifier inferences than the legacy one on the
        same frames. This fixture's faces are 128 px against a 64 px gate, so
        the counts happen to coincide today; asserting on that coincidence would
        turn "someone made the gate stricter, or shrank the fixture's faces"
        into a failure of the wrong test. `FaceAccuracyBehaviourTests
        .test_small_faces_are_gated_before_any_classifier_runs` owns the gate.
        """
        self._run_old(EFF)
        old_det = self.det_models[-1].calls
        old_cls = (self.ff_models[-1].calls, self.emo_models[-1].calls)
        self._reset_spies()
        self._run_new(EFF)
        new_det = self.det_models[-1].calls
        new_cls = (self.ff_models[-1].calls, self.emo_models[-1].calls)
        print(f"\ndetector infer calls: OLD {old_det} NEW {new_det}")
        print(f"classifier calls (fairface, emotion) -- NOT compared: "
              f"OLD {old_cls} NEW {new_cls}")
        self.assertEqual(old_det, new_det)
        self.assertGreater(new_det, 0, "the detector never ran")
        # Anti-vacuous: the classifiers must still be doing work on this
        # fixture, otherwise the sibling gate test proves nothing by contrast.
        self.assertGreater(new_cls[0], 0, "stage 2 never ran")


class FaceFrameGeometryTests(_Base):
    """★The design point★: the model image and the crop source are two images."""

    def test_crop_source_is_the_original_frame_not_the_model_image(self):
        self._run_new(EFF)
        shapes = set(self.crop_source_shapes)
        self.assertTrue(self.crop_source_shapes, "stage 2/3 never cropped")
        print("\ncrop_square_roi input shapes (new path):", shapes)
        self.assertEqual(shapes, {(FRAME_H, FRAME_W, 3)},
                         "stage 2/3 was handed something other than the original "
                         f"{FRAME_H}x{FRAME_W} frame")
        self.assertNotIn((DET_SIZE, DET_SIZE, 3), shapes,
                         "stage 2/3 got the 640x640 model image")

    def test_stage1_input_is_640_and_classifier_input_is_224(self):
        self._run_new(EFF)
        det_shapes = set(self.det_models[-1].input_shapes)
        ff_shapes = set(self.ff_models[-1].input_shapes)
        emo_shapes = set(self.emo_models[-1].input_shapes)
        print("det infer input shapes:", det_shapes)
        print("fairface infer input shapes:", ff_shapes)
        print("emotion infer input shapes:", emo_shapes)
        print("crop out_size values:", set(self.crop_out_sizes))
        self.assertEqual(det_shapes, {(DET_SIZE, DET_SIZE, 3)},
                         "self.pre() did not letterbox to the stage-1 640")
        self.assertEqual(ff_shapes, {(CLS_SIZE, CLS_SIZE, 3)},
                         "FairFace did not get a 224x224 ROI")
        self.assertEqual(emo_shapes, {(CLS_SIZE, CLS_SIZE, 3)},
                         "the emotion model did not get a 224x224 ROI")
        self.assertEqual(set(self.crop_out_sizes), {CLS_SIZE})

    def test_new_app_uses_hw_roi_frame_mode(self):
        """face-analysis crops each face ROI off the dma-buf (hw-roi), never the
        letterbox: the mode must be "hw-roi" -- NOT "hw-direct"/"hw" (those put
        the model image in frame.data with no cropper, breaking the crop) and no
        longer "cpu" (which paid the full-res convert this path removes).

        Absent a hardware cropper -- e.g. this fixture's fake source --
        `crop_roi_hw` falls straight back to the identical numpy
        `crop_square_roi(frame.data, ...)`; that is what the crop-source tests in
        this module exercise.
        """
        mod = _load_new_app_module()
        self.assertEqual(mod.FaceAnalysisApp.model_frame, "hw-roi",
                         "face-analysis must crop ROIs off the dma-buf (hw-roi)")
        self.assertNotIn(mod.FaceAnalysisApp.model_frame, ("hw-direct", "hw"),
                         "a data-is-letterbox mode with no cropper breaks the "
                         "stage-2/3 crop")

    def test_negative_control_hw_direct_would_break_the_crop(self):
        """Proof the assertion above is load-bearing, not a tautology.

        Flip the app to "hw-direct" and the fake source (which honours the flag
        the way OfficialFrameSource does) hands stages 2/3 the 640x640 model
        image -- i.e. the previous test WOULD fail. If this control ever stops
        producing 640x640 crop sources, the guard has gone blind.
        """
        cls = _load_new_app_module().FaceAnalysisApp
        cls.model_frame = "hw-direct"
        self._run_new(EFF, cls=cls)
        shapes = set(self.crop_source_shapes)
        print("\nnegative control (hw-direct) crop source shapes:", shapes)
        self.assertEqual(shapes, {(DET_SIZE, DET_SIZE, 3)},
                         "the fixture no longer detects a model_frame change")
        self.assertNotIn((FRAME_H, FRAME_W, 3), shapes)


class FaceAccuracyBehaviourTests(_Base):
    """★The 0.2.0 accuracy fixes★ -- one test per behaviour, each written so
    that reverting the corresponding change in `apps/face-analysis/app.py`
    (or the module under it) makes the test fail."""

    # -- 1. blur is stamped before the top-K slice ------------------------- #
    def test_blur_is_stamped_on_every_detection_not_only_the_top_k(self):
        """`max_faces = 2` but the detector emits 3 faces. Every one of them --
        including the ones that never reach the cascade -- must carry the
        privacy flag, because `results[]` is what the sink publishes and what an
        overlay renders. Tagging only `results[:max_faces]`, as 0.1.0 did, left
        the extra faces unflagged in exactly the crowded scene where blurring
        matters."""
        sink, _ = self._run_new(EFF)

        crowded = 0
        checked = 0
        for p, _pts in sink.payloads:
            results = p["results"]
            if len(results) > int(EFF["max_faces"]):
                crowded += 1
            for i, r in enumerate(results):
                checked += 1
                self.assertIn("blur", r,
                              f"result #{i} of {len(results)} carries no blur "
                              "flag: the tag was applied after the top-K slice")
                self.assertIs(r["blur"], True)
                self.assertEqual(r.get("kind"), "face",
                                 f"result #{i} carries no kind")

        beyond = [r for p, _ in sink.payloads
                  for r in p["results"][int(EFF["max_faces"]):]]
        print(f"\nresults checked={checked} crowded frames={crowded} "
              f"beyond-top-K results={len(beyond)}")
        self.assertGreater(crowded, 0,
                           "no frame exceeded max_faces: the test is vacuous")
        self.assertGreater(len(beyond), 0,
                           "no detection past the top-K slice: test is vacuous")
        self.assertTrue(all(r.get("blur") is True for r in beyond))

    # -- 2. the demographic histogram counts people, not face-frames ------- #
    def test_demographics_counts_unique_people_not_face_frames(self):
        """`faces` must be the number of tracks that FIRST became stable inside
        the window, and `face_frames` the old dwell-weighted number. 0.1.0
        bumped the histogram once per face per frame, so one person standing
        still outvoted a crowd walking past."""
        sink, _ = self._run_new(EFF)
        payloads = [p for p, _ in sink.payloads]

        # First frame at which each track id was reported stable -- recomputed
        # independently from the event stream, not read back off the app.
        first_stable = {}
        for i, p in enumerate(payloads):
            for e in _face_events(p):
                tid = e.get("track_id")
                if tid is None or not e.get("stable"):
                    continue
                first_stable.setdefault(tid, i)

        # `_roll_window` resets on the first processed frame and again on every
        # frame that emits an aggregate, so a window covers (reset, emit].
        windows = []
        lo = 1
        for i, p in enumerate(payloads):
            for d in _demographics(p):
                windows.append((lo, i, d))
                lo = i + 1

        self.assertTrue(windows, "the aggregation window never fired")
        interesting = 0
        for lo, hi, d in windows:
            expected = sum(1 for idx in first_stable.values() if lo <= idx <= hi)
            print(f"\nwindow frames [{lo}..{hi}] faces={d['faces']} "
                  f"face_frames={d['face_frames']} expected_unique={expected} "
                  f"gender={d['gender']}")
            self.assertIn("face_frames", d,
                          "the raw face-frame count is gone from the aggregate")
            self.assertEqual(d["faces"], expected,
                             "demographics.faces is not the number of tracks "
                             "that became stable in this window")
            self.assertLessEqual(d["faces"], d["face_frames"])
            self.assertEqual(sum(d["gender"].values()), d["faces"],
                             "the gender histogram was bumped more than once "
                             "per person")
            if d["faces"] and d["face_frames"] > d["faces"]:
                interesting += 1
        self.assertGreater(
            interesting, 0,
            "no window had anyone stay for more than one frame, so "
            "faces < face_frames was never actually exercised")

    # -- 3. emotion is remembered per identity, not per list slot ---------- #
    #
    # Two people at fixed positions whose DETECTION SCORES swap every frame, so
    # the score-sorted `results` list reorders under the cascade. The emotion
    # fake is seeded by ROI PIXELS (A's neighbourhood is painted dark, B's
    # bright), so "which emotion came back" is a direct question about which
    # face the verdict belongs to.
    _SWAP_EFF = dict(EFF, max_faces=2.0, emotion_interval=2.0,
                     aggregate_window_sec=999.0)

    @staticmethod
    def _swap_det_faces(k):
        """det call k -> processed frame k-1. Scores swap every frame."""
        idx = k - 1
        a_score, b_score = (0.91, 0.85) if idx % 2 == 0 else (0.85, 0.91)
        return [(CELL_A[0], CELL_A[1], a_score),
                (CELL_B[0], CELL_B[1], b_score)]

    @staticmethod
    def _emotion_from_roi(k, roi):
        """Dark ROI (person A) -> Anger(0); bright ROI (person B) -> Happiness(4)."""
        return 0 if float(np.mean(roi)) < 128.0 else 4

    def _arm_swap_fixture(self):
        self.frames_fn = _painted_frames
        self.det_faces_fn = self._swap_det_faces
        self.emo_idx_fn = self._emotion_from_roi

    def test_emotion_follows_track_id_not_list_index(self):
        self._arm_swap_fixture()
        sink, _ = self._run_new(self._SWAP_EFF)
        payloads = [p for p, _ in sink.payloads]

        expected = {"A": "Anger", "B": "Happiness"}
        ids = {"A": set(), "B": set()}
        orderings = set()
        checked = 0
        for i, p in enumerate(payloads):
            fe = _face_events(p)
            self.assertEqual(len(fe), 2, f"frame {i}: expected 2 faces")
            orderings.add(tuple(_side(e["box"]) for e in fe))
            print(f"frame {i}: " + "  ".join(
                f"{_side(e['box'])}(id={e['track_id']},emo={e['emotion']})"
                for e in fe))
            for e in fe:
                who = _side(e["box"])
                ids[who].add(e["track_id"])
                if e["emotion"] is None:
                    continue          # no emotion evidence accumulated yet
                checked += 1
                self.assertEqual(
                    e["emotion"], expected[who],
                    f"frame {i}: person {who} was handed "
                    f"{e['emotion']!r} -- the other person's emotion")

        self.assertEqual(len(orderings), 2,
                         "the detection ordering never swapped: the fixture "
                         "cannot detect a slot-keyed cache")
        self.assertGreater(checked, 0, "no face ever carried an emotion")
        for who in ("A", "B"):
            self.assertEqual(len(ids[who]), 1,
                             f"person {who} was given more than one track_id: "
                             f"{sorted(ids[who])}")
        self.assertNotEqual(ids["A"], ids["B"],
                            "both people share a track_id")

    def test_negative_control_legacy_slot_cache_crosses_the_two_people(self):
        """Proof the assertion above can fail: the SAME fixture, run through the
        pre-migration implementation, hands one person the other's emotion,
        because 0.1.0 cached the verdict under the face's index in the
        score-sorted list."""
        self._arm_swap_fixture()
        sink, _ = self._run_old(self._SWAP_EFF)

        expected = {"A": "Anger", "B": "Happiness"}
        crossed = []
        for i, (p, _pts) in enumerate(sink.payloads):
            for e in _face_events(p):
                who = _side(e["box"])
                print(f"legacy frame {i}: {who} emo={e['emotion']}")
                if e["emotion"] is not None and e["emotion"] != expected[who]:
                    crossed.append((i, who, e["emotion"]))
        print("\nlegacy cross-contamination:", crossed)
        self.assertTrue(
            crossed,
            "the legacy slot-keyed cache did NOT cross the two people on this "
            "fixture -- the identity test above is not proving anything")

    # -- 4. the quality gate skips stage 2/3 entirely ---------------------- #
    def test_small_faces_are_gated_before_any_classifier_runs(self):
        """`min_face_px` above the fixture's 128 px faces: no crop, no stage-2/3
        inference, every attribute None -- but the detections are still
        published, because gating is not dropping."""
        eff = dict(EFF, max_faces=5.0, min_face_px=FACE_SIDE_PX * 2)
        sink, _ = self._run_new(eff)

        ff_calls = self.ff_models[-1].calls
        emo_calls = self.emo_models[-1].calls
        print(f"\nmin_face_px={eff['min_face_px']} (faces are {FACE_SIDE_PX}px) "
              f"-> fairface calls={ff_calls} emotion calls={emo_calls} "
              f"crops={len(self.crop_source_shapes)}")
        self.assertEqual(ff_calls, 0, "stage 2 ran on a gated face")
        self.assertEqual(emo_calls, 0, "stage 3 ran on a gated face")
        self.assertEqual(self.crop_source_shapes, [],
                         "a gated face was still cropped (inference budget "
                         "spent before the gate)")

        attrs = ("gender", "gender_conf", "age", "age_conf",
                 "race", "race_conf", "emotion", "emotion_conf")
        total = 0
        for i, (p, _pts) in enumerate(sink.payloads):
            for r in p["results"]:
                total += 1
                self.assertIs(r.get("gated"), True,
                              f"frame {i}: a sub-threshold face is not gated")
                for a in attrs:
                    self.assertIsNone(r.get(a),
                                      f"frame {i}: gated face carries {a}")
            for e in _face_events(p):
                self.assertTrue(e["gated"])
                self.assertFalse(e["stable"])
                for a in attrs:
                    self.assertIsNone(e[a], f"frame {i}: gated event carries {a}")
        self.assertGreater(total, 0,
                           "the gate dropped the detections instead of "
                           "flagging them -- results[] is empty")

        # Sanity: the very same fixture DOES classify with the default gate, so
        # the zero above is the gate's doing and not a broken fixture.
        self._reset_spies()
        self._run_new(dict(EFF, max_faces=5.0))
        self.assertGreater(self.ff_models[-1].calls, 0,
                           "the fixture never classifies anything even with the "
                           "default gate: the test is vacuous")

    # -- 5. the verdict is a vote, not a single frame's argmax ------------- #
    @staticmethod
    def _single_face(k):
        return [(CELL_A[0], CELL_A[1], 0.91)]

    # ff call index == processed frame index (exactly one face per frame).
    _MINORITY_FRAMES = (3, 6)

    @classmethod
    def _minority_gender(cls, k):
        return (0, 1 if k in cls._MINORITY_FRAMES else 0, 0)

    def test_label_is_the_argmax_of_accumulated_evidence_not_this_frame(self):
        """One face, one FairFace call per frame. The gender head votes Male on
        every frame except two, where it votes Female by a wide margin. A
        single-frame argmax reports Female on those two frames; the accumulated
        vote must still report Male -- and `gender_conf` must be the winner's
        share of the accumulated mass, not that frame's softmax."""
        self.frames_fn = _fixed_frames
        self.det_faces_fn = self._single_face
        self.ff_vec_fn = self._minority_gender
        eff = dict(EFF, max_faces=1.0, aggregate_window_sec=999.0)
        sink, _ = self._run_new(eff)

        # The single-frame softmax the minority frames would have produced.
        single = clf.classify_head([2.5 if i == 1 else 0.1 for i in range(2)],
                                   clf.GENDER_LABELS)
        self.assertEqual(single["label"], "Female",
                         "the fixture's minority frame is not actually a "
                         "Female frame -- the test would be vacuous")

        rows = []
        for i, (p, _pts) in enumerate(sink.payloads):
            fe = _face_events(p)
            self.assertEqual(len(fe), 1, f"frame {i}: expected exactly 1 face")
            rows.append((i, fe[0]["gender"], fe[0]["gender_conf"]))
        print("\n(frame, gender, gender_conf) -- minority (Female) frames "
              f"{self._MINORITY_FRAMES}:")
        for r in rows:
            print("  ", r, "<-- minority frame" if r[0] in
                  self._MINORITY_FRAMES else "")

        self.assertGreaterEqual(len(rows), max(self._MINORITY_FRAMES) + 1,
                                "the fixture is too short to contain both "
                                "minority frames")
        for i, label, conf in rows:
            self.assertEqual(label, "Male",
                             f"frame {i}: reported {label!r} -- the verdict "
                             "followed this frame's argmax, not the vote")
            self.assertIsNotNone(conf)
        for i in self._MINORITY_FRAMES:
            conf = rows[i][2]
            self.assertLess(conf, single["confidence"],
                            f"frame {i}: gender_conf {conf} equals the "
                            "single-frame softmax -- it is not a vote share")
            self.assertGreater(conf, 0.5,
                               f"frame {i}: the majority label lost its share")

    # -- 6. identity is stable and `stable` waits for the evidence --------- #
    def test_track_id_is_stable_and_stable_waits_for_min_track_frames(self):
        self.frames_fn = _fixed_frames
        self.det_faces_fn = self._single_face
        min_frames = 3
        eff = dict(EFF, max_faces=1.0, aggregate_window_sec=999.0,
                   min_track_frames=float(min_frames))
        sink, _ = self._run_new(eff)

        rows = []
        for i, (p, _pts) in enumerate(sink.payloads):
            fe = _face_events(p)
            self.assertEqual(len(fe), 1, f"frame {i}: expected exactly 1 face")
            e = fe[0]
            rows.append((i, e["track_id"], e["evidence_frames"], e["stable"]))
        print(f"\nmin_track_frames={min_frames} "
              "(frame, track_id, evidence_frames, stable):")
        for r in rows:
            print("  ", r)

        ids = {tid for _i, tid, _n, _s in rows}
        self.assertEqual(len(ids), 1,
                         f"the same face changed identity across frames: {ids}")
        self.assertNotIn(None, ids, "the face was never associated to a track")

        for i, _tid, n, stable in rows:
            self.assertEqual(n, i + 1,
                             f"frame {i}: evidence_frames={n}, expected {i + 1}")
            self.assertEqual(stable, n >= min_frames,
                             f"frame {i}: stable={stable} at {n} evidence "
                             f"frames (min_track_frames={min_frames})")
        self.assertFalse(rows[0][3], "stable was true on the first frame")
        self.assertTrue(rows[-1][3], "stable never became true")


class FaceNewShapeTests(_Base):
    """New-shape specifics: auto-binding, model registry, live re-bind."""

    def _started(self, eff=None):
        app = _load_new_app_module().FaceAnalysisApp()
        app.start(DET_MODEL, sink=_RecordingSink(), verbose=False,
                  app_dir=APP_DIR, manifest=self.manifest,
                  config=dict(eff or EFF))
        return app

    def test_params_auto_bound_from_manifest_schema(self):
        app = self._started({"confidence": 0.55, "iou": 0.5, "max_faces": 3.0,
                             "crop_pad": 0.3, "emotion_interval": 4.0,
                             "aggregate_window_sec": 12.0,
                             "privacy_blur": False})
        try:
            self.assertEqual(app.confidence, 0.55)
            self.assertEqual(app.iou, 0.5)
            self.assertEqual(app.max_faces, 3.0)
            self.assertEqual(app.crop_pad, 0.3)
            self.assertEqual(app.emotion_interval, 4.0)
            self.assertEqual(app.aggregate_window_sec, 12.0)
            self.assertIs(app.privacy_blur, False)
        finally:
            app.finish()

    def test_live_rebind_keeps_cross_frame_state(self):
        """★S1 hot-reload★ -- a config change must not cost the app its memory.

        The cross-frame state is no longer a per-slot emotion cache and a bare
        histogram: it is the tracker's identities, the per-track accumulated
        evidence, and the open demographic window. All three have to survive
        SIGHUP, AND the two knobs that feed DERIVED objects (`AttributeConfig`
        for the gate/vote, `TrackerConfig` for the occlusion budget) have to
        take effect IN PLACE -- every already-existing `TrackAttributes` holds a
        reference to the same config object, so swapping in a fresh one would
        leave the live tracks on the old policy.
        """
        app = self._started()
        try:
            # -- give the app some memory to lose ------------------------- #
            tr = app._tracker.update([{"box": [80, 64, 208, 192], "score": 0.9}],
                                     0.0, FRAME_W, FRAME_H)[0]
            tid = tr.track_id
            ta = app._agg.track(tid)
            ta.add("gender", [0.9, 0.1])
            ta.bump_frame(0.0)
            app._agg.unique_faces = 7
            app._agg.face_frames = 11
            app._agg.hist["gender"]["Male"] = 3
            app._agg.window_start = 42.0
            app._frame_idx = 5

            attr_cfg = app._agg.cfg
            trk_cfg = app._tracker.cfg
            self.assertIs(ta.cfg, attr_cfg,
                          "TrackAttributes does not share the Aggregator's "
                          "config object -- in-place mutation cannot work")

            changed = app._bind_params({"confidence": 0.7, "max_faces": 4.0,
                                        "min_face_px": 96.0,
                                        "min_track_frames": 7.0,
                                        "evidence_decay": 0.8,
                                        "track_max_lost": 40.0},
                                       live_only=True)
            self.assertEqual(changed, {"confidence", "max_faces", "min_face_px",
                                       "min_track_frames", "evidence_decay",
                                       "track_max_lost"})
            app.on_params_changed(changed)

            # -- plain scalars re-bound ----------------------------------- #
            self.assertEqual(app.confidence, 0.7)
            self.assertEqual(app.max_faces, 4.0)

            # -- derived policy mutated IN PLACE, not replaced ------------ #
            self.assertIs(app._agg.cfg, attr_cfg,
                          "the AttributeConfig object was replaced: live "
                          "TrackAttributes are stranded on the old policy")
            self.assertIs(ta.cfg, attr_cfg)
            self.assertIs(app._tracker.cfg, trk_cfg,
                          "the TrackerConfig object was replaced")
            self.assertEqual(attr_cfg.min_face_px, 96.0)
            self.assertEqual(attr_cfg.min_track_frames, 7)
            self.assertAlmostEqual(attr_cfg.decay, 0.8)
            self.assertEqual(trk_cfg.max_lost_frames_center, 40)
            self.assertEqual(trk_cfg.max_lost_frames_edge, 20)
            # the new min_track_frames is visible through the EXISTING accumulator
            self.assertFalse(ta.stable, "the live track kept the old gate")

            # -- ★the point★: no memory was thrown away ------------------- #
            self.assertIn(tid, [t.track_id for t in app._tracker.active_tracks()],
                          "the tracker lost the face's identity")
            self.assertIs(app._agg.track(tid), ta,
                          "the track's accumulated evidence was discarded")
            self.assertEqual(ta.frames, 1)
            self.assertEqual(list(ta.sums), ["gender"])
            self.assertEqual(app._agg.unique_faces, 7)
            self.assertEqual(app._agg.face_frames, 11)
            self.assertEqual(app._agg.hist["gender"], {"Male": 3})
            self.assertEqual(app._agg.window_start, 42.0)
            self.assertEqual(app._frame_idx, 5)
        finally:
            app.finish()

    def test_restart_param_not_rebound_live(self):
        app = self._started()
        try:
            changed = app._bind_params({"aggregate_window_sec": 999.0},
                                       live_only=True)
            self.assertEqual(changed, set())
            self.assertEqual(app.aggregate_window_sec,
                             EFF["aggregate_window_sec"])
        finally:
            app.finish()

    def test_all_three_models_come_from_the_registry(self):
        app = self._started()
        try:
            self.assertEqual(len(app.models), 3)
            self.assertEqual(os.path.basename(app.models.det.path),
                             "yolov8n_face_rawhead_fp16.rknn")
            self.assertEqual(os.path.basename(app.models["fairface_fp16"].path),
                             "fairface_fp16.rknn")
            self.assertEqual(
                os.path.basename(app.models["emotion_enet_b0_fp16"].path),
                "emotion_enet_b0_fp16.rknn")
            self.assertTrue(os.path.isabs(app.models["fairface_fp16"].path))
            self.assertFalse(hasattr(app, "ff_model"),
                             "the hand-rolled stage-2 loader should be gone")
            self.assertFalse(hasattr(app, "emo_model"),
                             "the hand-rolled stage-3 loader should be gone")
        finally:
            app.finish()

    def test_classify_alias_is_ambiguous_by_design(self):
        """Two models claim task `classify`, so the alias must NOT resolve."""
        app = self._started()
        try:
            with self.assertRaises(AttributeError):
                _ = app.models.cls
        finally:
            app.finish()


if __name__ == "__main__":
    unittest.main()

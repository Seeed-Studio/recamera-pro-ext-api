"""
Equivalence gate for the ppocr-reader app-shape migration (KIT_APP_SHAPE_SPEC §7).

ppocr-reader is the CASCADE case, and it is the app that pins down spec §3's
boundary: stage 1 (DB detection) is the ordinary pre/infer/post trunk, stage 2
(perspective crop + CTC recognition) is a PLAIN `for` loop inside `run()` --
nothing about it is declared anywhere. It is also the app that must NOT get the
hardware-letterboxed frame: `perspective_crop` warps out of the ORIGINAL camera
pixels, so `frame.data` has to stay full-res while `self.pre()` letterboxes to
480 (not the kit's default 640).

Hardware-free: the frame source (`kit.app.open_frame_source`), the RKNN engine
(`kit.app.App._load_model`) and the 26 KB character dictionary
(`ctc.load_dictionary`, gitignored with the models) are stubbed with
deterministic fakes, then the SAME fixed frame sequence is pushed through

  OLD path : the pre-migration ppocr-reader (git 3896acc), reproduced verbatim
             below -- base `App.run()` loop + `run_postproc()` + `on_results()`;
  NEW path : the migrated `apps/ppocr-reader/app.py` -- `owns_loop = True`,
             `run()` + `for frame in self.frames()` + a plain stage-2 loop,

and `results` / `events` / `pts` / `stream_id` are compared field for field.

Both fake models are seeded BY CALL NUMBER, so the k-th det (resp. rec)
inference of the old run and of the new run are byte-identical -- any downstream
difference is a behaviour difference, and a diverging number/order of stage-2
calls shows up immediately as differing text.

Run: `python3 -m pytest kit/tests/test_ppocr_shape_equivalence.py -q`
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
from kit.runtime.postprocess import ctc                              # noqa: E402
from kit.runtime.postprocess import db_ocr                           # noqa: E402
from kit.runtime.preprocess import letterbox                         # noqa: E402

APP_DIR = os.path.join(_REPO, "apps", "ppocr-reader")

FRAME_W, FRAME_H = 640, 480     # camera frame: NOT square, NOT the model size
DET_SIZE = 480                  # ppocr det input side (manifest models[0].input)
REC_H, REC_W = 48, 320          # ppocr rec input
N_GREY = 2                      # camera warm-up placeholders, both paths skip
N_REAL = 8                      # real frames offered by the fake source
DT = 0.1

DET_MODEL = "models/ppocr_det_fp16.rknn"

# Effective config, as kit.config would hand it over (manifest defaults; every
# item is type "number", so max_boxes arrives as a float -- deliberately kept).
EFF = {
    "det_thresh": 0.3,
    "box_thresh": 0.5,
    "unclip_ratio": 2.0,
    "max_boxes": 8.0,
    "min_rec_conf": 0.25,
}

# Stand-in for models/ppocr_keys_v1.txt (gitignored). Layout matches
# ctc.load_dictionary's contract: index 0 = CTC blank, then chars, then space.
FAKE_CHARS = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
FAKE_DICT = [""] + FAKE_CHARS + [" "]
WORDS = ["EXIT", "GATE7", "OPEN", "LANE2", "STOP"]

HIGH_CONF, LOW_CONF = 0.90, 0.10        # LOW_CONF < EFF["min_rec_conf"]


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
                         pts=300.0 + i * DT))
    return out


class _FakeSource:
    """Fake camera that HONOURS the frame-source flags `start()` passes it.

    `direct_preprocess=True` (what `model_frame = "hw-direct"` asks for) is
    emulated faithfully: the letterboxed model image replaces `data` while
    `w`/`h` stay the original camera geometry -- exactly what would break
    ppocr's stage-2 crop. That makes `test_crop_source_is_the_original_frame...`
    a real regression detector rather than a restatement of the class attribute.
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


class _FakeDetModel:
    """A scripted DBNet head: one (1,1,480,480) sigmoid prob map per call.

    Three filled rectangles are painted into the map, indexed by call number.
    Their scores are deliberately NOT in reading order (the middle band scores
    highest, the top band lowest), so `db_ocr.decode`'s score-descending order
    differs from the app's top-to-bottom reading order -- if the migration lost
    the sort, the text sequence changes.
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
        prob = np.zeros((1, 1, DET_SIZE, DET_SIZE), dtype=np.float32)
        dx = (k * 4) % 40
        #        (y0,  y1,  x0,       x1,       score)
        bands = [(100, 132,  60 + dx, 250 + dx, 0.55),   # top    (lowest score)
                 (200, 236,  90 + dx, 380 + dx, 0.95),   # middle (highest)
                 (300, 330, 140 + dx, 300 + dx, 0.75)]   # bottom
        for y0, y1, x0, x1, s in bands:
            prob[0, 0, y0:y1, x0:x1] = s
        return [prob]

    def release(self):
        self.released = True


class _FakeRecModel:
    """A scripted CTC recognizer: one (1,T,C) logit block per call.

    Seeded by call number: call k spells WORDS[k % len(WORDS)], and every third
    call comes back at LOW_CONF (below min_rec_conf) so the app's "blank out a
    low-confidence reading, keep the box and its raw confidence" business rule
    is actually exercised.
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
        word = WORDS[k % len(WORDS)]
        conf = LOW_CONF if (k % 3 == 2) else HIGH_CONF
        T, C = 12, len(FAKE_DICT)
        seq = np.zeros((1, T, C), dtype=np.float32)
        seq[0, :, 0] = 0.05                        # CTC blank floor
        for t, ch in enumerate(word):
            seq[0, t, FAKE_DICT.index(ch)] = conf
        return [seq]

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


# ---- OLD shape: verbatim copy of the pre-migration ppocr-reader --------- #
def _quad_bbox(quad):
    xs = [p[0] for p in quad]
    ys = [p[1] for p in quad]
    return [min(xs), min(ys), max(xs), max(ys)]


class _LegacyPpocrApp(LegacyLoopApp):
    """ppocr-reader exactly as it was before the migration (git 3896acc).

    Only two mechanical deviations, both test-harness plumbing:
      * the manifest is handed in rather than re-read off disk;
      * the stage-2 model is built through `self._load_model(path)` instead of
        `RknnModel(path)` -- `_load_model` *is* `RknnModel(path)` (kit/app.py),
        and routing through it lets one stub cover both paths (importing
        kit.runtime.engine off-device would need rknnlite).
    """
    id = "ppocr-reader"
    name = "PP-OCR Reader"
    postproc = "db_ocr"
    input_size = 480

    def __init__(self, manifest):
        super().__init__()
        self._legacy_manifest = manifest

    def setup(self, config):
        super().setup(config)
        manifest = self._legacy_manifest
        params = {k: v for k, v in (config or {}).items() if v is not None}

        self.det_thresh = float(params.get("det_thresh", 0.3))
        self.box_thresh = float(params.get("box_thresh", 0.5))
        self.unclip_ratio = float(params.get("unclip_ratio", 2.0))
        self.max_boxes = int(params.get("max_boxes", 8))
        self.min_rec_conf = float(params.get("min_rec_conf", 0.25))

        rec_file, dict_file = "models/ppocr_rec_fp16.rknn", "models/ppocr_keys_v1.txt"
        for m in manifest.get("models", []):
            if m.get("role") == "stage2_rec" or m.get("task") == "recognize":
                rec_file = m.get("file", rec_file)
                dict_file = m.get("dict", dict_file)

        def _abs(p):
            return p if os.path.isabs(p) else os.path.join(APP_DIR, p)

        self.rec_model = self._load_model(_abs(rec_file))
        self.dictionary = ctc.load_dictionary(_abs(dict_file))

    def on_config_reload(self, config):
        params = self._reload_params(config)
        self.config = config or {}
        self.det_thresh = self._reload_float(params, "det_thresh", self.det_thresh)
        self.box_thresh = self._reload_float(params, "box_thresh", self.box_thresh)
        self.unclip_ratio = self._reload_float(params, "unclip_ratio",
                                               self.unclip_ratio)
        self.max_boxes = self._reload_int(params, "max_boxes", self.max_boxes)
        self.min_rec_conf = self._reload_float(params, "min_rec_conf",
                                               self.min_rec_conf)

    def run_postproc(self, outs, info):
        boxes = db_ocr.decode(outs, info,
                              det_thresh=self.det_thresh,
                              box_thresh=self.box_thresh,
                              unclip_ratio=self.unclip_ratio,
                              max_boxes=self.max_boxes)

        def _key(b):
            q = b["quad"]
            top = min(p[1] for p in q)
            left = min(p[0] for p in q)
            return (round(top / 20.0), left)
        boxes.sort(key=_key)
        results = []
        for b in boxes:
            bbox = _quad_bbox(b["quad"])
            results.append({
                "kind": "text",
                "quad": b["quad"],
                "box": bbox,
                "score": float(b["score"]),
                "cls_name": "text",
                "text": "",
                "rec_conf": 0.0,
            })
        return results

    def on_results(self, results, frame):
        events = []
        for r in results:
            crop = pipeline.perspective_crop(frame.data, r["quad"])
            fit = pipeline.fit_rec_input(crop, out_h=48, out_w=320)
            outs = self.rec_model.infer(fit)
            text, conf = ctc.decode(outs, self.dictionary)
            if text and conf >= self.min_rec_conf:
                r["text"] = text
                r["rec_conf"] = round(float(conf), 4)
            else:
                r["text"] = ""
                r["rec_conf"] = round(float(conf), 4)
            events.append({
                "kind": "text",
                "box": r["box"],
                "quad": r["quad"],
                "text": r["text"],
                "score": r["score"],
                "rec_conf": r["rec_conf"],
            })
        return events


def _load_new_app_class():
    path = os.path.join(APP_DIR, "app.py")
    spec = importlib.util.spec_from_file_location(
        "_ppocr_reader_app_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.PpocrReaderApp


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
        self._orig_dict = ctc.load_dictionary
        self._orig_crop = pipeline.perspective_crop

        self.det_models = []
        self.rec_models = []
        self.dict_paths = []
        self.crop_source_shapes = []

        def _fake_load(app_self, path):
            if "rec" in os.path.basename(path):
                m = _FakeRecModel(path)
                self.rec_models.append(m)
            else:
                m = _FakeDetModel(path)
                self.det_models.append(m)
            return m

        def _fake_dict(path):
            self.dict_paths.append(path)
            return list(FAKE_DICT)

        def _spy_crop(frame, quad, *a, **kw):
            # ★the load-bearing assertion source★: what pixels does stage 2 get?
            self.crop_source_shapes.append(tuple(np.asarray(frame).shape))
            return self._orig_crop(frame, quad, *a, **kw)

        kit_app.open_frame_source = lambda *a, **kw: _FakeSource(*a, **kw)
        kit_app.App._load_model = _fake_load
        ctc.load_dictionary = _fake_dict
        pipeline.perspective_crop = _spy_crop

        with open(os.path.join(APP_DIR, "manifest.json")) as f:
            self.manifest = json.load(f)

    def tearDown(self):
        kit_app.open_frame_source = self._orig_open
        kit_app.App._load_model = self._orig_load
        ctc.load_dictionary = self._orig_dict
        pipeline.perspective_crop = self._orig_crop
        try:
            signal.signal(signal.SIGHUP, signal.SIG_DFL)
        except (ValueError, OSError):
            pass

    def _run_old(self, eff):
        sink = _RecordingSink()
        app = _LegacyPpocrApp(self.manifest)
        app.setup(dict(eff))
        app.run(DET_MODEL, source="ffmpeg", sink=sink, n=0, verbose=False)
        return sink, app

    def _run_new(self, eff, cls=None):
        sink = _RecordingSink()
        app = (cls or _load_new_app_class())()
        app.start(DET_MODEL, source="ffmpeg", sink=sink, n=0, verbose=False,
                  app_dir=APP_DIR, manifest=self.manifest, config=dict(eff))
        try:
            app.run()
        finally:
            app.finish()
        return sink, app


class PpocrEquivalenceTests(_Base):

    def _compare(self, eff, label):
        old, old_app = self._run_old(eff)
        old_crops = list(self.crop_source_shapes)
        self.crop_source_shapes = []
        new, new_app = self._run_new(eff)
        new_crops = list(self.crop_source_shapes)

        self.assertEqual(len(old.payloads), N_REAL - 1,
                         f"{label}: old path emitted an unexpected frame count")
        self.assertEqual(len(new.payloads), len(old.payloads),
                         f"{label}: new path emitted a different frame count")

        old_s, new_s = _strip_timing(old.payloads), _strip_timing(new.payloads)
        for i, ((po, pts_o), (pn, pts_n)) in enumerate(zip(old_s, new_s)):
            self.assertEqual(pts_o, pts_n, f"{label} frame {i}: pts differs")
            self.assertEqual(po["stream_id"], pn["stream_id"],
                             f"{label} frame {i}: stream_id differs")
            self.assertEqual(po["results"], pn["results"],
                             f"{label} frame {i}: results differ")
            self.assertEqual(po["events"], pn["events"],
                             f"{label} frame {i}: events differ")
        # whole-payload DEEP EQUAL (catches any added/removed key too)
        self.assertEqual(old_s, new_s, f"{label}: payload streams differ")
        self.assertEqual(old.frame_sizes, new.frame_sizes)
        self.assertEqual(old_crops, new_crops,
                         f"{label}: stage-2 crop sources differ")
        return old_s, new_s, old_crops, new_crops

    def test_deep_equal(self):
        old_s, _new_s, _oc, _nc = self._compare(EFF, "default")

        # -- anti-vacuous-pass assertions --------------------------------- #
        texts = [e for p, _ in old_s for e in p["events"]]
        self.assertGreater(len(texts), 0, "fixture produced no text events")
        self.assertTrue(all(e["kind"] == "text" for e in texts))
        self.assertGreater(sum(len(p["results"]) for p, _ in old_s), 0,
                           "fixture produced no text boxes")
        read = [e["text"] for e in texts if e["text"]]
        self.assertGreater(len(read), 0, "every reading came back empty")
        blanked = [e for e in texts if not e["text"]]
        self.assertGreater(len(blanked), 0,
                           "the min_rec_conf branch never fired")
        self.assertTrue(all(e["rec_conf"] > 0 for e in blanked),
                        "a blanked reading must still carry its raw confidence")

    def test_reading_order_preserved(self):
        """Boxes must arrive top-to-bottom, not score-descending."""
        old, _ = self._run_old(EFF)
        self.crop_source_shapes = []
        new, _ = self._run_new(EFF)
        for (po, _), (pn, _) in zip(old.payloads, new.payloads):
            tops_o = [min(p[1] for p in r["quad"]) for r in po["results"]]
            tops_n = [min(p[1] for p in r["quad"]) for r in pn["results"]]
            self.assertEqual(tops_o, tops_n)
            self.assertEqual(tops_o, sorted(tops_o),
                             "results are not in reading order")
            scores = [r["score"] for r in po["results"]]
            self.assertNotEqual(scores, sorted(scores, reverse=True),
                                "fixture is degenerate: reading order happens "
                                "to equal score order, so the sort is untested")

    def test_text_and_rec_conf_identical_per_box(self):
        old, _ = self._run_old(EFF)
        self.crop_source_shapes = []
        new, _ = self._run_new(EFF)
        old_tbl = [[(e["text"], e["rec_conf"], e["box"]) for e in p["events"]]
                   for p, _ in old.payloads]
        new_tbl = [[(e["text"], e["rec_conf"], e["box"]) for e in p["events"]]
                   for p, _ in new.payloads]
        self.assertEqual(old_tbl, new_tbl)
        print("\n--- per-frame text/rec_conf/box (old vs new) ---")
        for i, (o, n) in enumerate(zip(old_tbl, new_tbl)):
            print(f"frame {i}: OLD {o}")
            print(f"frame {i}: NEW {n}")
            print(f"frame {i}: EQUAL={o == n}")

    def test_infer_call_counts_match(self):
        self._run_old(EFF)
        old_det = self.det_models[-1].calls
        old_rec = self.rec_models[-1].calls
        self.crop_source_shapes = []
        self._run_new(EFF)
        self.assertEqual(old_det, self.det_models[-1].calls)
        self.assertEqual(old_rec, self.rec_models[-1].calls)
        self.assertGreater(old_rec, 0, "stage 2 never ran")


class PpocrFrameGeometryTests(_Base):
    """★The design point ppocr exists to prove★: two different images.

    `self.pre()` must letterbox to the app's 480 (not the kit default 640) AND
    `frame.data` must stay the ORIGINAL camera frame, because stage 2 crops
    source pixels out of it. `model_frame = "hw-direct"` would collapse the two.
    """

    def test_crop_source_is_the_original_frame_not_the_model_image(self):
        self._run_new(EFF)
        shapes = set(self.crop_source_shapes)
        self.assertTrue(self.crop_source_shapes, "stage 2 never cropped")
        print("\nperspective_crop input shapes (new path):", shapes)
        self.assertEqual(shapes, {(FRAME_H, FRAME_W, 3)},
                         "stage 2 was handed something other than the original "
                         f"{FRAME_H}x{FRAME_W} frame")
        self.assertNotIn((DET_SIZE, DET_SIZE, 3), shapes,
                         "stage 2 got the 480x480 model image")

    def test_model_input_is_480(self):
        self._run_new(EFF)
        det_shapes = set(self.det_models[-1].input_shapes)
        rec_shapes = set(self.rec_models[-1].input_shapes)
        print("det infer input shapes:", det_shapes)
        print("rec infer input shapes:", rec_shapes)
        self.assertEqual(det_shapes, {(DET_SIZE, DET_SIZE, 3)},
                         "the DB detector did not get a 480x480 input")
        self.assertEqual(rec_shapes, {(REC_H, REC_W, 3)},
                         "the recognizer did not get a 48x320 input")

    def test_new_app_keeps_cpu_frame_mode(self):
        cls = _load_new_app_class()
        self.assertEqual(cls.model_frame, "cpu",
                         "ppocr must not letterbox into frame.data")

    def test_negative_control_hw_direct_would_break_the_crop(self):
        """Proof the assertion above is load-bearing, not a tautology.

        Flip the app to "hw-direct" and the fake source (which honours the flag
        the way OfficialFrameSource does) hands stage 2 the 480x480 model image
        -- i.e. the previous test WOULD fail. If this control ever stops
        producing 480x480 crops, the guard has gone blind.
        """
        cls = _load_new_app_class()
        cls.model_frame = "hw-direct"
        self._run_new(EFF, cls=cls)
        shapes = set(self.crop_source_shapes)
        print("\nnegative control (hw-direct) crop shapes:", shapes)
        self.assertEqual(shapes, {(DET_SIZE, DET_SIZE, 3)},
                         "the fixture no longer detects a model_frame change")


class PpocrNewShapeTests(_Base):
    """New-shape specifics: auto-binding, model registry, dictionary loading."""

    def _started(self, eff=None):
        app = _load_new_app_class()()
        app.start(DET_MODEL, sink=_RecordingSink(), verbose=False,
                  app_dir=APP_DIR, manifest=self.manifest,
                  config=dict(eff or EFF))
        return app

    def test_params_auto_bound_from_manifest_schema(self):
        app = self._started({"det_thresh": 0.42, "box_thresh": 0.6,
                             "unclip_ratio": 2.5, "max_boxes": 3.0,
                             "min_rec_conf": 0.55})
        try:
            self.assertEqual(app.det_thresh, 0.42)
            self.assertEqual(app.box_thresh, 0.6)
            self.assertEqual(app.unclip_ratio, 2.5)
            self.assertEqual(app.max_boxes, 3.0)
            self.assertEqual(app.min_rec_conf, 0.55)
        finally:
            app.finish()

    def test_live_rebind_replaces_values(self):
        app = self._started()
        try:
            changed = app._bind_params({"det_thresh": 0.7, "min_rec_conf": 0.8},
                                       live_only=True)
            self.assertEqual(changed, {"det_thresh", "min_rec_conf"})
            self.assertEqual(app.det_thresh, 0.7)
            self.assertEqual(app.min_rec_conf, 0.8)
        finally:
            app.finish()

    def test_both_models_come_from_the_registry(self):
        app = self._started()
        try:
            self.assertEqual(len(app.models), 2)
            self.assertEqual(os.path.basename(app.models.det.path),
                             "ppocr_det_fp16.rknn")
            self.assertEqual(os.path.basename(app.models.rec.path),
                             "ppocr_rec_fp16.rknn")
            # paths absolutised against the install dir
            self.assertTrue(os.path.isabs(app.models.rec.path))
            self.assertFalse(hasattr(app, "rec_model"),
                             "the hand-rolled stage-2 loader should be gone")
        finally:
            app.finish()

    def test_dictionary_path_resolved_from_manifest(self):
        app = self._started()
        try:
            self.assertEqual(len(app.dictionary), len(FAKE_DICT))
            self.assertEqual(self.dict_paths[-1],
                             os.path.join(APP_DIR, "models/ppocr_keys_v1.txt"))
        finally:
            app.finish()


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------------------- #
# Long-strip windowing (kit.pipeline.split_windows / place_chars /
# merge_windows). The rec rknn is fixed at 48x320, so a strip wider than
# 6.67:1 is squashed and CTC's 40 steps cap it near 20 characters; these cover
# the split-and-merge that works around it. Pure geometry -- no model needed.
# --------------------------------------------------------------------------- #
from kit import pipeline as _pl                                    # noqa: E402


def test_split_windows_leaves_a_fitting_strip_alone():
    """A strip within the model's aspect ratio must stay ONE window.

    Short lines are the common case; they have to keep the single-inference
    path or every frame pays for a feature only long lines need.
    """
    assert _pl.split_windows(300, 48) == [(0, 300)]
    assert _pl.split_windows(320, 48) == [(0, 320)]


def test_split_windows_covers_the_strip_and_respects_the_aspect_cap():
    for crop_w, crop_h in [(984, 58), (2000, 58), (700, 30), (5000, 48)]:
        wins = _pl.split_windows(crop_w, crop_h)
        assert wins[0][0] == 0
        assert wins[-1][1] == crop_w, "the last window must reach the end"
        for x0, x1 in wins:
            assert (x1 - x0) <= _pl.REC_MAX_ASPECT * crop_h + 1
        for (a0, a1), (b0, b1) in zip(wins, wins[1:]):
            assert b0 < a1, "neighbouring windows must overlap, not just touch"


def test_place_chars_maps_time_steps_onto_the_strip():
    """Step t must land at (t+0.5)/T of the window's own width."""
    chars = [("A", 0, 0.9), ("B", 19, 0.9), ("C", 39, 0.9)]
    placed = _pl.place_chars(chars, 40, 100, 420, 48)   # 320 wide, 48 tall -> no padding
    xs = [p[0] for p in placed]
    assert abs(xs[0] - (100 + 0.5 / 40 * 320)) < 1e-6
    assert abs(xs[2] - (100 + 39.5 / 40 * 320)) < 1e-6
    assert xs[0] < xs[1] < xs[2]


def test_place_chars_drops_steps_inside_the_right_padding():
    """A narrow window is right-padded to 320; steps past the content are noise."""
    placed = _pl.place_chars([("A", 5, 0.9), ("Z", 38, 0.9)], 40, 0, 96, 48)
    assert [p[1] for p in placed] == ["A"], "the step inside the padding must go"


def _windowed_reads(text, pitch, windows, jitter=2.0, misreads=(), drops=()):
    """Build physically consistent per-window placements for `text`.

    Glyph i sits at x = i * pitch on the strip. Each window reports the glyphs
    inside its own span at that position give or take `jitter` px -- roughly one
    CTC step, which is the real placement error. `misreads` maps
    (window, glyph index) to the wrong character, `drops` is the set of
    (window, glyph index) a window failed to emit at all.
    """
    per = []
    for wi, (x0, x1) in enumerate(windows):
        placed = []
        for i, ch in enumerate(text):
            x = i * pitch
            if not (x0 <= x < x1) or (wi, i) in drops:
                continue
            got = dict(misreads).get((wi, i), ch)
            conf = 0.5 if (wi, i) in dict(misreads) else 0.9
            placed.append((x + (jitter if wi % 2 else -jitter), got, conf))
        per.append(placed)
    return per


def test_merge_windows_keeps_periodic_text_intact():
    """The reason this merges by position and not by string similarity.

    Longest-common-substring stitching aligns repeats of "0123456789" onto the
    wrong period and eats one; position cannot.
    """
    text = "0123456789" * 3
    wins = [(0, 200), (150, 350), (300, 500)]
    per = _windowed_reads(text, 16.0, wins)
    got, conf = _pl.merge_windows(wins, per, 500)
    assert got == text, got
    assert 0.0 < conf <= 1.0


def test_merge_windows_drops_a_glyph_emitted_by_both_windows():
    """A glyph inside the overlap is read twice and must be emitted once.

    This is the "...ABCDEFGHH12345678" duplication seen on device.
    """
    text = "ABCDEFGH12345678"
    wins = [(0, 200), (150, 350)]
    per = _windowed_reads(text, 20.0, wins)
    assert sum(len(p) for p in per) > len(text), "the fixture must overlap"
    got, _ = _pl.merge_windows(wins, per, 350)
    assert got == text, got


def test_merge_windows_keeps_a_glyph_only_one_window_read():
    """A glyph the neighbour missed must survive.

    Its window's edge is what cut through it, so the window that DID read it is
    the one to trust -- an ownership split by x would throw it away.
    """
    text = "ABCDEFGHIJKLMN"
    wins = [(0, 200), (150, 350)]
    # glyph 9 sits in the overlap; the left window never emitted it
    per = _windowed_reads(text, 20.0, wins, drops={(0, 9)})
    got, _ = _pl.merge_windows(wins, per, 350)
    assert got == text, got


def test_merge_windows_prefers_the_more_confident_read_of_a_shared_glyph():
    """When two windows disagree inside the overlap, confidence decides."""
    text = "ABCDEFGH12345678"
    wins = [(0, 200), (150, 350)]
    # the left window misreads glyph 8 near its right edge, with low confidence
    per = _windowed_reads(text, 20.0, wins, misreads={(0, 8): "X"})
    got, _ = _pl.merge_windows(wins, per, 350)
    assert got == text, got

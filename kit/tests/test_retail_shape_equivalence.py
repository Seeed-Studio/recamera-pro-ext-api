"""
Equivalence gate for the retail-vision app-shape migration (KIT_APP_SHAPE_SPEC §7).

retail-vision is the CROSS-FRAME-STATE case: `self.tracker` (id assignment),
`self.dwell` (per-track timers), `self.zone`, `self.line` (entry/exit counters)
and `self.window` (rolling metrics) all persist between frames. The migration
must keep them identical, not just the per-frame formatting.

Hardware-free: the frame source (`kit.app.open_frame_source`) and the RKNN
engine (`kit.app.App._load_model`) are stubbed with deterministic fakes, then
the SAME fixed multi-frame sequence is pushed through

  OLD path : the pre-migration retail-vision (git 3b64087), reproduced verbatim
             below -- base `App.run()` loop + `run_postproc()` + `on_results()`;
  NEW path : the migrated `apps/retail-vision/app.py` -- `owns_loop = True`,
             `run()` + `for frame in self.frames()`.

and `results` / `events` / `pts` / `stream_id` are compared field for field.

The synthetic scene has three people so every branch is live:
  * A walks left -> right  (crosses the entry line inbound, keeps moving)
  * B walks right -> left  (crosses the entry line outbound)
  * C stands still inside the counting zone (browsing -> engaged -> assistance)

Run: `python3 -m pytest kit/tests/test_retail_shape_equivalence.py -q`
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
from kit.adapters.frame_source import Frame                          # noqa: E402
from kit.adapters.result_sink import ResultSink                      # noqa: E402
from kit.logic.tracker import Tracker, TrackerConfig                 # noqa: E402
from kit.logic.zones import (                                        # noqa: E402
    ZoneCounter, LineCounter, Dwell, DwellConfig, RollingWindow,
    StateCount, ENGAGED, ASSISTANCE,
)

APP_DIR = os.path.join(_REPO, "apps", "retail-vision")

FRAME_W, FRAME_H = 160, 120
N_GREY = 2             # camera warm-up placeholders, both paths must skip these
N_REAL = 12            # real frames offered by the fake source
DT = 1.0               # seconds between frames (dwell timers are wall-clock)
N_ANCHORS = 100        # columns of the fake [1, 84, N] detection head

# Effective config used by both paths. dwell_assist is pulled in from the
# manifest default (20 s) to 4 s so the ASSISTANCE state is reachable inside a
# 12-frame fixture.
EFF = {
    "confidence": 0.30,
    "iou": 0.45,
    "dwell_speed": 10.0,
    "dwell_engaged": 1.5,
    "dwell_assist": 4.0,
    "window_duration": 60.0,
    "count_zone": [[0.10, 0.10], [0.90, 0.10], [0.90, 0.95], [0.10, 0.95]],
    "entry_line": {"a": [0.5, 0.0], "b": [0.5, 1.0], "in": "right"},
}
# Same scene, no entry line -> the appearance-based entry/exit fallback runs.
EFF_NO_LINE = {k: v for k, v in EFF.items() if k != "entry_line"}


def _fixed_frames():
    """N_GREY flat-grey warm-up frames, then N_REAL real ones, pts DT apart."""
    out = []
    for i in range(N_GREY):
        data = np.full((FRAME_H, FRAME_W, 3), 114, dtype=np.uint8)
        out.append(Frame(data=data, w=FRAME_W, h=FRAME_H, fmt="RGB",
                         pts=100.0 + i))
    for i in range(N_REAL):
        rng = np.random.default_rng(4200 + i)
        data = rng.integers(0, 256, (FRAME_H, FRAME_W, 3), dtype=np.uint8)
        out.append(Frame(data=data, w=FRAME_W, h=FRAME_H, fmt="RGB",
                         pts=200.0 + i * DT))
    return out


class _FakeSource:
    def __init__(self, *a, **kw):
        self.closed = False

    def frames(self):
        for f in _fixed_frames():
            yield f

    def close(self):
        self.closed = True


class _FakeModel:
    """A scripted 3-person YOLOv8 head, indexed by call number.

    Emits one `[1, 84, N_ANCHORS]` float32 tensor in 640-network space: rows 0-3
    are cx/cy/w/h, row 4 is the COCO 'person' score, rows 5-83 the other classes
    (kept low). Only the first three anchors carry a person; the rest sit below
    the confidence threshold. The k-th infer() of the old run and the k-th of
    the new run are byte-identical, so any difference downstream is a behaviour
    difference.
    """

    def __init__(self, path):
        self.path = path
        self.calls = 0
        self.released = False

    def infer(self, x):
        k = self.calls
        self.calls += 1
        head = np.zeros((1, 84, N_ANCHORS), dtype=np.float32)
        head[0, 4:, :] = 0.02                       # background anchors
        # The three walk in separate horizontal lanes (>0.15 normalised apart,
        # the tracker's distance threshold) so they never swap identities.
        # A: near lane, walks left -> right across the x=0.5 line
        head[0, 0:4, 0] = (110.0 + k * 40.0, 480.0, 60.0, 100.0)
        # B: far lane, walks right -> left across the same line
        head[0, 0:4, 1] = (570.0 - k * 40.0, 200.0, 60.0, 100.0)
        # C: middle lane, stands still inside the counting zone
        head[0, 0:4, 2] = (460.0, 340.0, 55.0, 100.0)
        head[0, 4, 0:3] = (0.91, 0.88, 0.77)        # class 0 = person
        return [head]

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


# ---- OLD shape: verbatim copy of the pre-migration retail-vision ------- #
PERSON_CLS = 0


class _LegacyRetailApp(LegacyLoopApp):
    """retail-vision exactly as it was before the migration (git 3b64087)."""
    id = "retail-vision"
    name = "Retail People Counting"
    postproc = "detect"
    model_frame = "hw-direct"

    def setup(self, config):
        super().setup(config)
        params = {k: v for k, v in (config or {}).items() if v is not None}

        spatial = {
            "count_zone": params.get("count_zone"),
            "entry_line": params.get("entry_line"),
        }

        self.conf = float(params.get("confidence", 0.4))
        self.iou = float(params.get("iou", 0.45))

        self.tracker = Tracker(TrackerConfig())

        self.dwell = Dwell(DwellConfig(
            speed_threshold=float(params.get("dwell_speed", 10.0)),
            engaged_sec=float(params.get("dwell_engaged", 1.5)),
            assistance_sec=float(params.get("dwell_assist", 20.0)),
        ))

        zone_poly = spatial.get("count_zone")
        self.zone = ZoneCounter(zone_poly)

        self.line = LineCounter()
        line_cfg = spatial.get("entry_line")
        if line_cfg and "a" in line_cfg and "b" in line_cfg:
            ab_in = str(line_cfg.get("in", "right")).lower() != "left"
            self.line.set_line(line_cfg["a"], line_cfg["b"], ab_in)

        self.window = RollingWindow(float(params.get("window_duration", 60.0)))

        self._entry = 0
        self._exit = 0

    def on_config_reload(self, config):
        params = self._reload_params(config)
        self.config = config or {}
        self.conf = self._reload_float(params, "confidence", self.conf)
        self.iou = self._reload_float(params, "iou", self.iou)
        try:
            self.dwell.cfg.speed_threshold = float(
                params.get("dwell_speed", self.dwell.cfg.speed_threshold))
            self.dwell.cfg.engaged_sec = float(
                params.get("dwell_engaged", self.dwell.cfg.engaged_sec))
            self.dwell.cfg.assistance_sec = float(
                params.get("dwell_assist", self.dwell.cfg.assistance_sec))
        except (TypeError, ValueError, AttributeError):
            pass

    def on_results(self, results, frame):
        persons = [d for d in results if d.get("cls") == PERSON_CLS]
        tracks = self.tracker.update(persons, frame.pts, frame.w, frame.h)

        events = []

        live_ids = [tr.track_id for tr in tracks]
        self.dwell.prune(live_ids)
        counts = StateCount()
        in_zone = self.zone.inside(tracks)
        in_zone_ids = {tr.track_id for tr in in_zone}
        for tr in tracks:
            state = self.dwell.update(tr, frame.pts)
            if tr.track_id in in_zone_ids:
                counts.total += 1
                if state == ENGAGED:
                    counts.engaged += 1
                elif state == ASSISTANCE:
                    counts.assistance += 1
                else:
                    counts.browsing += 1
            x1 = (tr.cx - tr.w / 2) * frame.w
            y1 = (tr.cy - tr.h / 2) * frame.h
            x2 = (tr.cx + tr.w / 2) * frame.w
            y2 = (tr.cy + tr.h / 2) * frame.h
            events.append({
                "kind": "track",
                "track_id": tr.track_id,
                "state": state,
                "in_zone": tr.track_id in in_zone_ids,
                "score": round(tr.score, 3),
                "speed_px_s": round(tr.speed_px_s, 1),
                "box": [round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)],
            })

        if self.line.enabled:
            for ev in self.line.update(tracks):
                events.append({"kind": "line_cross", **ev})
            entry, exit_ = self.line.entry_count, self.line.exit_count
        else:
            self._entry += len(self.tracker.new_ids)
            self._exit += len(self.tracker.removed_ids)
            for tid in self.tracker.new_ids:
                events.append({"kind": "line_cross", "track_id": tid, "dir": "in"})
            for tid in self.tracker.removed_ids:
                events.append({"kind": "line_cross", "track_id": tid, "dir": "out"})
            entry, exit_ = self._entry, self._exit

        self.window.update(counts, entry, exit_, frame.pts)
        snap = self.window.snapshot()
        events.append({
            "kind": "metrics",
            "occupancy": snap.occupancy,
            "browsing": snap.browsing,
            "engaged": snap.engaged,
            "assistance": snap.assistance,
            "peak": snap.peak,
            "entry_count": snap.entry_count,
            "exit_count": snap.exit_count,
        })

        for r in results:
            r.setdefault("kind", "person")
        return events


def _load_new_app_class():
    path = os.path.join(APP_DIR, "app.py")
    spec = importlib.util.spec_from_file_location(
        "_retail_vision_app_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.RetailVisionApp


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


def _of_kind(payloads, kind):
    return [[e for e in p["events"] if e["kind"] == kind] for p, _ in payloads]


class _Base(unittest.TestCase):
    def setUp(self):
        self._orig_open = kit_app.open_frame_source
        self._orig_load = kit_app.App._load_model
        self.models = []

        def _fake_load(app_self, path):
            m = _FakeModel(path)
            self.models.append(m)
            return m

        kit_app.open_frame_source = lambda *a, **kw: _FakeSource()
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
        app = _LegacyRetailApp()
        app.setup(dict(eff))
        app.run("models/yolo8n_rawhead_int8.rknn", source="ffmpeg",
                sink=sink, n=0, verbose=False)
        return sink, app

    def _run_new(self, eff):
        sink = _RecordingSink()
        app = _load_new_app_class()()
        app.start("models/yolo8n_rawhead_int8.rknn", source="ffmpeg",
                  sink=sink, n=0, verbose=False, app_dir=APP_DIR,
                  manifest=self.manifest, config=dict(eff))
        try:
            app.run()
        finally:
            app.finish()
        return sink, app


class RetailEquivalenceTests(_Base):

    def _compare(self, eff, label):
        old, old_app = self._run_old(eff)
        new, new_app = self._run_new(eff)

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
        return old_s, new_s, old_app, new_app

    # -- the gate -------------------------------------------------------- #
    def test_deep_equal_with_line_and_zone(self):
        old_s, new_s, old_app, new_app = self._compare(EFF, "line")

        # -- anti-vacuous-pass assertions --------------------------------- #
        total_events = sum(len(p["events"]) for p, _ in old_s)
        self.assertGreater(total_events, 0, "fixture produced no events")
        kinds = {}
        for p, _ in old_s:
            for e in p["events"]:
                kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
        self.assertGreater(kinds.get("track", 0), 0, "no track events")
        self.assertGreater(kinds.get("metrics", 0), 0, "no metrics events")
        self.assertGreater(kinds.get("line_cross", 0), 0, "no line crossings")
        self.assertGreater(sum(len(p["results"]) for p, _ in old_s), 0,
                           "fixture produced no detections")

    def test_deep_equal_appearance_fallback(self):
        """No entry_line configured -> appearance-based entry/exit counting."""
        old_s, _new_s, _o, _n = self._compare(EFF_NO_LINE, "appearance")
        crosses = [e for p, _ in old_s for e in p["events"]
                   if e["kind"] == "line_cross"]
        self.assertGreater(len(crosses), 0, "appearance fallback emitted nothing")
        self.assertTrue(all("dir" in e for e in crosses))

    # -- cross-frame state, checked explicitly ------------------------------ #
    def test_track_ids_identical_per_frame(self):
        old, _ = self._run_old(EFF)
        new, _ = self._run_new(EFF)
        old_ids = [[e["track_id"] for e in evs]
                   for evs in _of_kind(old.payloads, "track")]
        new_ids = [[e["track_id"] for e in evs]
                   for evs in _of_kind(new.payloads, "track")]
        self.assertEqual(old_ids, new_ids)
        # id assignment must actually be stable across frames (not re-issued)
        flat = {i for f in old_ids for i in f}
        self.assertEqual(len(flat), 3, f"expected 3 stable tracks, got {flat}")

    def test_dwell_state_transitions_identical(self):
        old, _ = self._run_old(EFF)
        new, _ = self._run_new(EFF)
        old_st = [[(e["track_id"], e["state"]) for e in evs]
                  for evs in _of_kind(old.payloads, "track")]
        new_st = [[(e["track_id"], e["state"]) for e in evs]
                  for evs in _of_kind(new.payloads, "track")]
        self.assertEqual(old_st, new_st)
        seen = {s for f in old_st for _, s in f}
        # the stationary person must actually progress past browsing
        self.assertIn(ENGAGED, seen, f"dwell never reached ENGAGED (saw {seen})")
        self.assertIn(ASSISTANCE, seen,
                      f"dwell never reached ASSISTANCE (saw {seen})")

    def test_entry_exit_counters_identical(self):
        old, _ = self._run_old(EFF)
        new, _ = self._run_new(EFF)
        old_m = [evs[0] for evs in _of_kind(old.payloads, "metrics")]
        new_m = [evs[0] for evs in _of_kind(new.payloads, "metrics")]
        self.assertEqual([(m["entry_count"], m["exit_count"]) for m in old_m],
                         [(m["entry_count"], m["exit_count"]) for m in new_m])
        self.assertGreater(old_m[-1]["entry_count"], 0, "no entry counted")
        self.assertGreater(old_m[-1]["exit_count"], 0, "no exit counted")

    def test_metrics_snapshots_identical(self):
        old, _ = self._run_old(EFF)
        new, _ = self._run_new(EFF)
        self.assertEqual(_of_kind(old.payloads, "metrics"),
                         _of_kind(new.payloads, "metrics"))
        occ = [evs[0]["occupancy"] for evs in _of_kind(old.payloads, "metrics")]
        self.assertGreater(max(occ), 0, "zone never occupied")

    def test_infer_call_counts_match(self):
        self._run_old(EFF)
        old_calls = self.models[-1].calls
        self._run_new(EFF)
        self.assertEqual(old_calls, self.models[-1].calls)


class RetailNewShapeTests(_Base):
    """New-shape specifics: auto-binding and the live-reload semantics."""

    def test_params_auto_bound_and_derived_objects_built(self):
        app = _load_new_app_class()()
        app.start(None, sink=_RecordingSink(), verbose=False, app_dir=APP_DIR,
                  manifest=self.manifest, config=dict(EFF))
        try:
            self.assertEqual(app.confidence, 0.30)
            self.assertEqual(app.iou, 0.45)
            self.assertEqual(app.dwell_assist, 4.0)
            self.assertTrue(app.zone.enabled)
            self.assertTrue(app.line.enabled)
            self.assertEqual(app.window.window_sec, 60.0)
            self.assertEqual(app.dwell.cfg.assistance_sec, 4.0)
        finally:
            app.finish()

    def test_live_reload_mutates_dwell_config_in_place(self):
        """dwell_* is apply:"live": the DwellConfig must be mutated, never
        rebuilt, or every live track's dwell timer would silently reset."""
        app = _load_new_app_class()()
        app.start(None, sink=_RecordingSink(), verbose=False, app_dir=APP_DIR,
                  manifest=self.manifest, config=dict(EFF))
        try:
            dwell_obj, cfg_obj = app.dwell, app.dwell.cfg
            tracker_obj, window_obj = app.tracker, app.window
            changed = app._bind_params(
                {"confidence": 0.6, "dwell_engaged": 3.0, "dwell_speed": 22.0,
                 "window_duration": 999.0},
                live_only=True)
            self.assertEqual(changed,
                             {"confidence", "dwell_engaged", "dwell_speed"})
            app.on_params_changed(changed)
            self.assertEqual(app.confidence, 0.6)
            self.assertIs(app.dwell, dwell_obj)      # object identity preserved
            self.assertIs(app.dwell.cfg, cfg_obj)    # mutated IN PLACE
            self.assertEqual(cfg_obj.engaged_sec, 3.0)
            self.assertEqual(cfg_obj.speed_threshold, 22.0)
            # apply:"restart" params are neither re-bound nor applied
            self.assertEqual(app.window_duration, 60.0)
            self.assertEqual(window_obj.window_sec, 60.0)
            self.assertIs(app.tracker, tracker_obj)
        finally:
            app.finish()

    def test_live_reload_replaces_spatial_geometry_at_frame_boundary(self):
        app = _load_new_app_class()()
        app.start(None, sink=_RecordingSink(), verbose=False, app_dir=APP_DIR,
                  manifest=self.manifest, config=dict(EFF))
        try:
            old_zone, old_line = app.zone, app.line
            changed = app._bind_params(
                {
                    "count_zone": [[0.2, 0.2], [0.8, 0.2],
                                   [0.8, 0.8], [0.2, 0.8]],
                    "entry_line": {"a": [0.1, 0.5], "b": [0.9, 0.5],
                                   "in": "left"},
                },
                live_only=True,
            )
            self.assertEqual(changed, {"count_zone", "entry_line"})
            app.on_params_changed(changed)
            self.assertIsNot(app.zone, old_zone)
            self.assertIsNot(app.line, old_line)
            self.assertTrue(app.zone.enabled)
            self.assertTrue(app.line.enabled)
            self.assertEqual(app.line.entry_count, 0)
            self.assertEqual(app.line.exit_count, 0)
        finally:
            app.finish()


class _MixedClassModel(_FakeModel):
    """One person + one chair, both above threshold, in every frame.

    The equivalence fixture is person-only, so it cannot see how non-person
    detections get tagged -- that blind spot is exactly why the old
    `setdefault("kind", "person")` bug survived review. This model exercises it.
    """

    CHAIR_CLS = 56          # COCO-80 'chair'

    def infer(self, x):
        head = np.zeros((1, 84, N_ANCHORS), dtype=np.float32)
        head[0, 4:, :] = 0.02
        head[0, 0:4, 0] = (300.0, 340.0, 60.0, 100.0)       # a person
        head[0, 4, 0] = 0.90
        head[0, 0:4, 1] = (150.0, 300.0, 50.0, 80.0)        # a chair
        head[0, 4 + self.CHAIR_CLS, 1] = 0.85
        self.calls += 1
        return [head]


class RetailResultKindTaggingTests(_Base):
    """`results[].kind` must reflect the ACTUAL class, not blanket 'person'."""

    def _run_mixed(self):
        sink = _RecordingSink()
        app = _load_new_app_class()()
        orig = kit_app.App._load_model
        kit_app.App._load_model = lambda s, path: _MixedClassModel(path)
        try:
            app.start("models/yolo8n_rawhead_int8.rknn", source="ffmpeg",
                      sink=sink, n=0, verbose=False, app_dir=APP_DIR,
                      manifest=self.manifest, config=dict(EFF))
            try:
                app.run()
            finally:
                app.finish()
        finally:
            kit_app.App._load_model = orig
        return sink

    def test_non_person_results_are_not_tagged_person(self):
        sink = self._run_mixed()
        by_kind = {}
        for payload, _ in sink.payloads:
            for r in payload["results"]:
                by_kind.setdefault(r["cls_name"], set()).add(r["kind"])

        self.assertTrue(by_kind, "fixture produced no results at all")
        self.assertIn("person", by_kind, "fixture lost the person detection")
        self.assertIn("chair", by_kind, "fixture lost the non-person detection")
        self.assertEqual(by_kind["person"], {"person"},
                         "person detections must be tagged kind='person'")
        self.assertEqual(by_kind["chair"], {"detection"},
                         "a chair must NOT be tagged kind='person' "
                         f"(got {by_kind['chair']})")

    def test_only_persons_reach_the_tracker(self):
        """The chair must never become a track (tracking filters on cls==0)."""
        sink = self._run_mixed()
        tracked = [e for payload, _ in sink.payloads
                   for e in payload["events"] if e["kind"] == "track"]
        self.assertTrue(tracked, "no track events produced")
        for payload, _ in sink.payloads:
            n_person = sum(1 for r in payload["results"] if r["cls"] == 0)
            n_track = sum(1 for e in payload["events"] if e["kind"] == "track")
            self.assertLessEqual(n_track, n_person,
                                 "more tracks than person detections")


if __name__ == "__main__":
    unittest.main()

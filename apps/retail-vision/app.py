#!/usr/bin/env python3
"""
retail-vision -- reCamera Pro retail people-counting app (port of the first-gen
SSCMA solution).

Migrated to the new kit shape (internal/KIT_APP_SHAPE_SPEC.md §1): `run()` owns
the loop and the pipeline reads top to bottom -- pre / infer / post, then the
retail business logic (person filter -> Tracker -> Dwell + Zone + Line counting
-> rolling-window metrics) -> emit. Frame grab/release, warm-up skipping, model
loading, config hot-reload and output fan-out stay with the kit.

Cross-frame state (`self.tracker`, `self.dwell`, `self.zone`, `self.line`,
`self.window`) lives on the instance and is built once in `setup()`; the loop
mutates it frame after frame.

All parameters (`confidence` / `iou` / `dwell_*` / `window_duration`) plus the
spatial `count_zone` / `entry_line` controls are auto-bound from the manifest's
config_schema onto `self` -- edited in the /appcenter panel, persisted to
<app_dir>/config.json, re-bound on SIGHUP for the apply:"live" ones. Points are
normalised [0,1]. With no line configured, entry/exit fall back to
appearance/disappearance counting (new track = entry, aged-out track = exit).

Run on device (inference requires root):
    python3 -m kit.run /userdata/local/apps/retail-vision \
        --model models/yolo8n_rawhead_int8.rknn --sink ws --port 8124
"""

from kit.app import App, run_app
from kit.runtime.postprocess.detect import postprocess
from kit import events as E
from kit.logic.tracker import Tracker, TrackerConfig
from kit.logic.zones import (
    ZoneCounter, LineCounter, Dwell, DwellConfig, RollingWindow,
    StateCount, ENGAGED, ASSISTANCE,
)

PERSON_CLS = 0  # COCO 'person'


class RetailVisionApp(App):
    id = "retail-vision"
    name = "Retail People Counting"
    owns_loop = True          # explicit new shape: run() drives self.frames()
    # Only boxes/tracks are consumed -- never frame.data pixels -- so the frame
    # source can letterbox on RGA into data itself (see App.model_frame).
    model_frame = "hw-direct"

    # Fallbacks for the auto-bound config_schema keys: these are what the app
    # runs with when a key is absent from the effective config (the manifest
    # supplies a default for every numeric one; the spatial controls have none
    # until the user draws them in the panel).
    confidence = 0.4
    dwell_speed = 10.0
    dwell_engaged = 1.5
    dwell_assist = 20.0
    window_duration = 60.0
    count_zone = None
    entry_line = None

    def setup(self, config):
        """Build the derived, cross-frame objects from the already-bound params.

        Called by `App.start()` AFTER the config_schema auto-bind, so every
        `self.<param>` below is already populated -- this hook only constructs
        what a plain attribute cannot express.
        """
        super().setup(config)

        self.tracker = Tracker(TrackerConfig())

        self.dwell = Dwell(DwellConfig(
            speed_threshold=float(self.dwell_speed),
            engaged_sec=float(self.dwell_engaged),
            assistance_sec=float(self.dwell_assist),
        ))

        self.zone = ZoneCounter(self.count_zone)
        self.line = self._new_line_counter(self.entry_line)

        self.window = RollingWindow(float(self.window_duration))

        # Appearance-based fallback counters (used only when no line configured).
        self._entry = 0
        self._exit = 0

        print(f"[retail] setup conf={self.confidence} iou={self.iou} "
              f"zone={'on' if self.zone.enabled else 'off'} "
              f"line={'on' if self.line.enabled else 'off'} "
              f"dwell(engaged={self.dwell.cfg.engaged_sec}s "
              f"assist={self.dwell.cfg.assistance_sec}s "
              f"speed={self.dwell.cfg.speed_threshold}px/s) "
              f"window={self.window.window_sec}s", flush=True)

    @staticmethod
    def _new_line_counter(line_cfg):
        """Build one line counter from a normalized manifest value.

        A spatial edit changes the meaning of every accumulated crossing, so a
        live update intentionally starts a fresh counter at the next frame
        boundary rather than carrying counts/state across different geometry.
        """

        line = LineCounter()
        if line_cfg and "a" in line_cfg and "b" in line_cfg:
            # "in" names which side of a->b counts as an entry: a left->right
            # crossing has cross-product sign +1, i.e. the point moves toward the
            # RIGHT side. ab_in True means that +1 (left->right) is an entry.
            ab_in = str(line_cfg.get("in", "right")).lower() != "left"
            line.set_line(line_cfg["a"], line_cfg["b"], ab_in)
        return line

    def on_params_changed(self, changed):
        """★S1 live hot-reload★ -- only what the auto-bind cannot do by itself.

        `confidence` / `iou` are already re-bound onto `self` by the time this
        runs; the dwell thresholds additionally live inside a DwellConfig owned
        by `self.dwell`, so mirror them by MUTATING that config IN PLACE. Never
        rebuild the Dwell object: it holds one timer per live track, and a fresh
        instance would silently reset every visitor's dwell clock.
        `window_duration` is apply:"restart" and never reaches here.
        """
        if changed & {"dwell_speed", "dwell_engaged", "dwell_assist"}:
            cfg = self.dwell.cfg
            cfg.speed_threshold = float(self.dwell_speed)
            cfg.engaged_sec = float(self.dwell_engaged)
            cfg.assistance_sec = float(self.dwell_assist)
        if "count_zone" in changed:
            # tick()/on_params_changed run at a frame boundary, so replacing
            # the geometry object cannot race the current frame's update.
            self.zone = ZoneCounter(self.count_zone)
        if "entry_line" in changed:
            self.line = self._new_line_counter(self.entry_line)
            self._entry = 0
            self._exit = 0
        print(f"[retail] hot-reload conf={self.confidence} iou={self.iou} "
              f"zone={'on' if self.zone.enabled else 'off'} "
              f"line={'on' if self.line.enabled else 'off'} "
              f"dwell(engaged={self.dwell.cfg.engaged_sec}s "
              f"assist={self.dwell.cfg.assistance_sec}s "
              f"speed={self.dwell.cfg.speed_threshold}px/s)", flush=True)

    def run(self):
        for frame in self.frames():
            # -- 1. pre / infer / post ---------------------------------- #
            x = self.pre(frame)
            outs = self.models.det.infer(x.data)
            results = postprocess(outs, x.info, conf_thres=self.confidence,
                                  iou_thres=self.iou,
                                  class_names=self.class_names)

            # -- 2. person filter -> tracking (cross-frame state) -------- #
            persons = [d for d in results if d.get("cls") == PERSON_CLS]
            tracks = self.tracker.update(persons, frame.pts, frame.w, frame.h)

            events = []

            # -- 3. per-track dwell state + one track event each --------- #
            live_ids = [tr.track_id for tr in tracks]
            self.dwell.prune(live_ids)
            counts = StateCount()
            in_zone_ids = {tr.track_id for tr in self.zone.inside(tracks)}
            for tr in tracks:
                state = self.dwell.update(tr, frame.pts)
                in_zone = tr.track_id in in_zone_ids
                if in_zone:
                    counts.total += 1
                    if state == ENGAGED:
                        counts.engaged += 1
                    elif state == ASSISTANCE:
                        counts.assistance += 1
                    else:
                        counts.browsing += 1
                events.append(E.track(tr, frame, state=state, in_zone=in_zone))

            # -- 4. entry/exit: line crossings if configured, else appearance #
            if self.line.enabled:
                for ev in self.line.update(tracks):
                    events.append({"kind": "line_cross", **ev})
                entry, exit_ = self.line.entry_count, self.line.exit_count
            else:
                self._entry += len(self.tracker.new_ids)
                self._exit += len(self.tracker.removed_ids)
                for tid in self.tracker.new_ids:
                    events.append({"kind": "line_cross", "track_id": tid,
                                   "dir": "in"})
                for tid in self.tracker.removed_ids:
                    events.append({"kind": "line_cross", "track_id": tid,
                                   "dir": "out"})
                entry, exit_ = self._entry, self._exit

            # -- 5. rolling-window metrics snapshot ---------------------- #
            self.window.update(counts, entry, exit_, frame.pts)
            events.append(E.metrics(self.window.snapshot()))

            # Tag by ACTUAL class: the detector is COCO-80, so a frame carries
            # chairs/suitcases too. Tagging every result "person" mis-labelled
            # them for any downstream consumer keying on `kind` (the manifest
            # even documented it as "kind may be mis-tagged"). Only cls==0 is a
            # person; everything else is a plain detection.
            for r in results:
                r.setdefault("kind", "person" if r.get("cls") == PERSON_CLS
                             else "detection")
            self.emit(events, frame.pts, results=results)


if __name__ == "__main__":
    run_app(RetailVisionApp())

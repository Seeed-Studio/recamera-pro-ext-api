#!/usr/bin/env python3
"""
fitness-trainer -- reCamera Pro pose app (port of the first-gen SSCMA solution).

Migrated to the new kit shape (internal/KIT_APP_SHAPE_SPEC.md §1): `run()` owns
the loop and the whole pipeline reads top to bottom as ordinary Python:

  frame -> self.pre()             letterbox to 640 (manifest models[0].input)
        -> self.models.pose       YOLO11n-pose rawhead RKNN
        -> pose_post.postprocess  person boxes + COCO-17 keypoints, score-desc
        -> self.exercise.update   ★business★ joint-angle hysteresis state
                                  machine (kit.logic.rep_counter) -- reps/sets
                                  accumulate ACROSS frames
        -> self.emit()            keypoints + one `workout` event per frame

`model_frame` stays "hw-direct" ON PURPOSE: this app only ever consumes the
post-processed keypoint COORDINATES and never reads `frame.data` pixels, so the
frame source may letterbox on RGA straight into `data` (the cheapest path -- it
also skips the full-res NV12->RGB convert). See docs/guide/hw-preprocess.md.

Cross-frame state that MUST survive a hot-reload (see `on_params_changed`):
  * `self.exercise`               -- rep/set accumulator + angle hysteresis,
  * `self._last_person_pts`       -- idle-reset clock,
  * `self._last_workout_complete` -- one-shot "workout complete" log edge.

Every knob is auto-bound from the manifest config_schema and re-bound on SIGHUP
for the apply:"live" ones, so there is no setup() param-copying and no
on_config_reload.

Run on device (inference requires root):
    python3 -m kit.run /userdata/local/apps/fitness-trainer \
        --model models/yolo11n_pose_rawhead_int8.rknn --sink ws --port 8124
"""
import sys

from kit.app import App, run_app
from kit.runtime.postprocess import pose as pose_post
from kit.logic.rep_counter import create_exercise, exercise_ids


class FitnessTrainerApp(App):
    id = "fitness-trainer"
    name = "Fitness Trainer"
    owns_loop = True          # explicit new shape: run() drives self.frames()
    # Only skeleton coordinates are consumed -- never frame.data pixels -- so the
    # frame source can letterbox on RGA into data itself (see App.model_frame).
    model_frame = "hw-direct"
    model_dma_input = True

    # Fallbacks for the auto-bound config_schema keys (used when a key is
    # missing from the effective config; the manifest supplies each default).
    # NOTE target_reps / target_sets are declared `type: "number"` in the
    # manifest, so the auto-bind coerces them to FLOAT. Every use site below
    # therefore goes through int() -- the published event fields stay integers.
    mode = "squat"
    target_reps = 12
    target_sets = 3
    idle_reset_seconds = 60.0
    confidence = 0.4
    keypoint_confidence = 0.5

    def setup(self, config):
        """Build the exercise state machine from the already-bound params.

        Called by `App.start()` AFTER the config_schema auto-bind, so every
        `self.<knob>` below is already populated.
        """
        super().setup(config)

        self.exercise = create_exercise(self.mode, float(self.keypoint_confidence))
        if self.exercise is None:
            print(f"[fitness] WARN unknown mode {self.mode!r}, falling back to "
                  f"squat (known: {exercise_ids()})", file=sys.stderr, flush=True)
            self.mode = "squat"
            self.exercise = create_exercise(self.mode,
                                            float(self.keypoint_confidence))
        # The mode the CURRENT self.exercise was built for. `self.mode` is
        # re-bound by the kit before on_params_changed() runs, so the state
        # machine needs its own record to tell "the mode really changed" from
        # "the same mode was re-applied".
        self._ex_mode = self.mode
        self.exercise.set_targets(self.target_reps, self.target_sets)

        self._last_person_pts = None   # last frame pts a subject was seen at
        self._last_workout_complete = False

        print(f"[fitness] setup mode={self.mode} target={self.target_reps}x"
              f"{self.target_sets} conf={self.confidence} "
              f"kpt_thres={self.keypoint_confidence} "
              f"idle_reset={self.idle_reset_seconds}s", flush=True)

    def on_params_changed(self, changed):
        """★S1 live hot-reload★ -- only what the auto-bind cannot do by itself.

        The scalars are already re-bound onto `self`; what is left is the
        `self.exercise` state machine, and the rule is the one the pre-migration
        `on_config_reload` implemented:

          * `mode` CHANGED   -> build a NEW exercise. A different exercise is a
            different state machine; reps counted for squats do not carry over
            to push-ups. An unknown mode is refused and BOTH `self.mode` and the
            state machine are left as they were.
          * `mode` UNCHANGED -> never rebuild (that would silently zero the
            running rep/set accumulator). The live keypoint threshold is applied
            IN PLACE instead.
          * `target_reps` / `target_sets` -> `set_targets()`, which replaces the
            targets WITHOUT resetting the reps already counted.

        `self._last_person_pts` / `self._last_workout_complete` are never
        touched here.
        """
        if "mode" in changed:
            new_mode = str(self.mode)
            ex = create_exercise(new_mode, float(self.keypoint_confidence))
            if ex is None:
                print(f"[fitness] hot-reload unknown mode {new_mode!r}, keeping "
                      f"{self._ex_mode!r}", file=sys.stderr, flush=True)
                self.mode = self._ex_mode      # refuse the bad value
            else:
                self.exercise = ex
                self._ex_mode = new_mode
        else:
            # same exercise: apply the live keypoint threshold in place.
            self.exercise.kpt_thres = float(self.keypoint_confidence)
        # targets are always safe to (re)apply -- set_targets keeps current reps.
        self.exercise.set_targets(self.target_reps, self.target_sets)
        print(f"[fitness] hot-reload changed={sorted(changed)} mode={self.mode} "
              f"target={self.target_reps}x{self.target_sets} "
              f"conf={self.confidence} kpt_thres={self.keypoint_confidence} "
              f"idle_reset={self.idle_reset_seconds}s", flush=True)

    def run(self):
        for frame in self.frames():
            # -- 1. pre / infer / post ----------------------------------- #
            x = self.pre(frame)
            outs = self.models.pose.infer(x)
            results = pose_post.postprocess(
                outs, x.info,
                conf_thres=self.confidence,
                iou_thres=self.iou,
                kpt_thres=self.keypoint_confidence)

            # Primary subject = highest-scoring person (post-process sorted).
            primary = results[0] if results else None
            now = frame.pts

            # -- 2. ★business★ idle reset ------------------------------- #
            # After idle_reset_seconds with nobody in view, zero the workout so
            # the next athlete starts fresh (0 = never, first-gen behaviour).
            if primary is not None:
                self._last_person_pts = now
            elif (self.idle_reset_seconds > 0
                  and self._last_person_pts is not None
                  and now - self._last_person_pts >= self.idle_reset_seconds):
                self.exercise.reset()
                self.exercise.set_targets(self.target_reps,
                                          self.target_sets)
                self._last_person_pts = None
                self._last_workout_complete = False
                print(f"[fitness] idle reset after "
                      f"{self.idle_reset_seconds:.0f}s with no subject",
                      flush=True)

            # -- 3. ★business★ rep/set state machine (cross-frame) ------- #
            st = self.exercise.update(primary, now)

            event = {
                "kind": "workout",
                "mode": self.mode,
                "target_reps": self.target_reps,
                "target_sets": self.target_sets,
                "person_score": (round(float(primary["score"]), 3)
                                 if primary else 0.0),
            }
            event.update(st.as_dict())
            events = [event]

            if st.rep_completed:
                print(f"[fitness] rep -> set {st.set} reps {st.reps}/"
                      f"{self.target_reps} (stage={st.stage} "
                      f"angle={st.angle:.0f})"
                      + (f" [{st.form_warning}]" if st.form_warning else ""),
                      flush=True)
            if st.workout_complete and not self._last_workout_complete:
                print(f"[fitness] *** WORKOUT COMPLETE at pts={now:.2f} ***",
                      flush=True)
            self._last_workout_complete = st.workout_complete

            for r in results:
                r.setdefault("kind", "person")

            self.emit(events, frame.pts, results=results)


if __name__ == "__main__":
    run_app(FitnessTrainerApp())

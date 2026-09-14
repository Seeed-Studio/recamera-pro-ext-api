#!/usr/bin/env python3
"""
facemesh-reader -- reCamera Pro two-stage face cascade app (port of the
first-gen SSCMA facemesh-reader / drowsiness solution).

Migrated to the new kit shape (internal/KIT_APP_SHAPE_SPEC.md §1/§3): `run()`
owns the loop and the two-stage cascade reads top to bottom as ordinary Python:

  frame -> self.pre()             letterbox to 640 (manifest models[0].input)
        -> self.models.det        YOLOv8n-face rawhead
        -> face_post.postprocess  face boxes in ORIGINAL pixels, score-desc
        -> self.cascade.process   <-- stage 2, an ordinary call in run():
                                  crop a padded SQUARE ROI out of the FULL-RES
                                  frame -> resize 192 -> self.models.lmk ->
                                  landmark.decode (468x3 mapped back to px)
        -> self.logic.update      ★business★ EAR / MAR + yawn + PERCLOS
                                  temporal state (cross-frame)
        -> self.emit()            metrics + blink / yawn / drowsiness events

`model_frame` stays "cpu" ON PURPOSE. Stage 2 crops SOURCE-RESOLUTION pixels out
of `frame.data`, so "hw-direct" -- which letterboxes into `frame.data` itself --
would silently feed the landmark model a 640x640 model image instead of the
camera frame. A 2026-08-14 device A/B also measured "hw" at +0.8% (noise)
because it still pays the full-res convert plus an extra RGA resize. See
docs/guide/hw-preprocess.md before touching this.

Both models are declared in the manifest `models[]` and preloaded by the kit,
so the hand-written "scan the manifest for role==stage2_landmark" loop is gone:
the stage-2 `CascadePipeline` ADOPTS `self.models.lmk` instead of loading a
second copy of the same rknn.

★Renamed★: the stage-2 pipeline used to live on `self.pipeline`, which now
reads as if it were a kit API (it never was -- the same-named `manifest.pipeline`
field was unrelated dead data). It is `self.cascade` here; `self.pipeline` no
longer exists anywhere in this app.

Every knob is auto-bound from the manifest config_schema and re-bound on SIGHUP
for the apply:"live" ones, so there is no setup() param-copying and no
on_config_reload; `on_params_changed` only mirrors the live values into the two
derived objects (`self.cascade`, `self.logic`) BY IN-PLACE MUTATION.

Run on device (inference requires root):
    python3 -m kit.run /userdata/local/apps/facemesh-reader \
        --model models/yolov8n_face_rawhead_fp16.rknn --sink ws --port 8124
"""

from kit.app import App, run_app
from kit.logic.recording import request_configured_recording
from kit import events as E
from kit.pipeline import CascadePipeline
from kit.runtime.postprocess import face_detect as face_post
from kit.runtime.postprocess import landmark as landmark_post
from kit.logic.drowsiness import DrowsinessLogic, DrowsinessConfig

LMK_INPUT = 192          # stage-2 landmark input side (manifest models[1].input)


class FacemeshReaderApp(App):
    id = "facemesh-reader"
    name = "Facemesh Reader"
    owns_loop = True          # explicit new shape: run() drives self.frames()
    # Stage 2 crops original-resolution pixels out of frame.data -- see the
    # module docstring for why this must not become "hw"/"hw-direct".
    model_frame = "cpu"

    # Fallbacks for the auto-bound config_schema keys (used when a key is
    # missing from the effective config; the manifest supplies each default).
    confidence = 0.4
    crop_pad = 0.25
    presence_threshold = 0.5
    ear_threshold = 0.21
    mar_threshold = 0.65
    yawn_consecutive_frames = 5
    ear_continuous_sec = 2.0
    perclos_window_sec = 60.0
    perclos_critical_pct = 20.0
    alert_cooldown_sec = 5.0
    yawn_count_threshold = 3

    def setup(self, config):
        """Build the derived, cross-frame objects from the already-bound params.

        Called by `App.start()` AFTER the config_schema auto-bind, so every
        `self.<knob>` below is already populated. The stage-2 landmark rknn is
        preloaded by the kit (`self.models.lmk`); the CascadePipeline adopts it.
        """
        super().setup(config)

        self.cascade = CascadePipeline(
            model=self.models.lmk,   # preloaded by the kit; never a 2nd copy
            input_size=LMK_INPUT,
            decode_fn=landmark_post.decode,
            pad=float(self.crop_pad),
            max_targets=1,          # primary face drives the drowsiness state
        )

        # --- CPU temporal logic (EAR/MAR + yawn + PERCLOS drowsiness) ---- #
        cfg = DrowsinessConfig(
            ear_threshold=float(self.ear_threshold),
            ear_continuous_sec=float(self.ear_continuous_sec),
            perclos_window_sec=float(self.perclos_window_sec),
            perclos_critical_pct=float(self.perclos_critical_pct),
            alert_cooldown_sec=float(self.alert_cooldown_sec),
            yawn_count_threshold=self.yawn_count_threshold,
        )
        self.logic = DrowsinessLogic(
            drowsy_cfg=cfg,
            mar_threshold=float(self.mar_threshold),
            yawn_consecutive_frames=self.yawn_consecutive_frames,
            ear_threshold=float(self.ear_threshold),
        )
        # blink edge-detect (eyes_closed rising edge, event-debounced)
        self._prev_closed = False
        self._blink_count = 0
        self._prev_yawn_count = 0

        print(f"[facemesh] setup conf={self.confidence} iou={self.iou} "
              f"crop_pad={self.crop_pad} landmark={self.models.lmk.id} "
              f"input={LMK_INPUT} ear_thr={cfg.ear_threshold} "
              f"mar_thr={self.logic.mar_threshold}", flush=True)

    def on_params_changed(self, changed):
        """★S1 live hot-reload★ -- only what the auto-bind cannot do by itself.

        The scalars are already re-bound onto `self` by the time this runs; what
        is left is mirroring them into the two DERIVED objects, and that is done
        by MUTATING THOSE OBJECTS IN PLACE -- never by rebuilding them:

          * `self.cascade.pad` -- rebuilding the CascadePipeline is what the old
            code explicitly avoided; today it would re-adopt the same handle
            rather than reload the rknn, but it would still drop the object the
            loop holds mid-frame. Assign the field.
          * `self.logic` (+ `logic.yawn`, `logic.drowsy.cfg`) -- these carry the
            PERCLOS deque, the continuous-closure timer, the 5-minute yawn
            window and the alert cooldown. A fresh instance would silently reset
            every one of them, so only the threshold FIELDS are replaced.

        Nothing here touches `self._prev_closed` / `self._blink_count` either.
        `perclos_window_sec` is apply:"restart" and never reaches here.
        """
        if "crop_pad" in changed:
            self.cascade.pad = float(self.crop_pad)

        logic = self.logic
        if changed & {"ear_threshold", "mar_threshold",
                      "yawn_consecutive_frames", "ear_continuous_sec",
                      "perclos_critical_pct", "alert_cooldown_sec",
                      "yawn_count_threshold"}:
            logic.ear_threshold = float(self.ear_threshold)
            logic.mar_threshold = float(self.mar_threshold)
            if getattr(logic, "yawn", None) is not None:
                logic.yawn.mar_threshold = logic.mar_threshold
                logic.yawn.consecutive_frames = self.yawn_consecutive_frames
            cfg = getattr(getattr(logic, "drowsy", None), "cfg", None)
            if cfg is not None:
                cfg.ear_threshold = logic.ear_threshold
                cfg.ear_continuous_sec = float(self.ear_continuous_sec)
                cfg.perclos_critical_pct = float(self.perclos_critical_pct)
                cfg.alert_cooldown_sec = float(self.alert_cooldown_sec)
                cfg.yawn_count_threshold = self.yawn_count_threshold

        print(f"[facemesh] hot-reload changed={sorted(changed)} "
              f"conf={self.confidence} iou={self.iou} "
              f"crop_pad={self.cascade.pad} presence={self.presence_threshold} "
              f"ear_thr={logic.ear_threshold} mar_thr={logic.mar_threshold}",
              flush=True)

    def run(self):
        for frame in self.frames():
            # -- 1. pre / infer / stage-1 post --------------------------- #
            x = self.pre(frame)
            outs = self.models.det.infer(x.data)
            results = face_post.postprocess(outs, x.info,
                                            conf_thres=self.confidence,
                                            iou_thres=self.iou)

            # tag every detected face for the overlay
            for r in results:
                r["kind"] = "face"

            t = frame.pts
            primary = results[0] if results else None

            # -- 2. stage 2: landmarks for the PRIMARY face -------------- #
            # `frame.data` is the ORIGINAL camera frame (model_frame="cpu"),
            # which is what crop_square_roi inside the cascade must cut from.
            landmarks = None
            presence = 0.0
            if primary is not None:
                stage2 = self.cascade.process(frame.data, [primary])
                if stage2:
                    lm_xyz, presence = stage2[0]["decoded"]
                    # ★business★ below the presence floor the landmarks are
                    # discarded and the temporal logic ticks with no face.
                    if presence >= self.presence_threshold:
                        landmarks = lm_xyz     # (468,3) original-frame px

            # -- 3. ★business★ CPU temporal logic (ticks with neutral input
            #       when no face) ---------------------------------------- #
            metrics, yawn_state, drowsy_state, yawn_event = \
                self.logic.update(landmarks, t)

            events = []

            # Attach per-face metrics + a small landmark summary to the primary.
            if primary is not None:
                primary["presence"] = round(float(presence), 3)
                primary["landmark_count"] = (int(len(landmarks))
                                             if landmarks is not None else 0)
                if metrics.valid:
                    primary["ear"] = round(metrics.avg_ear, 3)
                    primary["mar"] = round(metrics.mar, 3)
                    # keypoints as [x,y] pairs for overlay (rounded)
                    primary["keypoints"] = [
                        [round(float(p[0]), 1), round(float(p[1]), 1)]
                        for p in landmarks
                    ] if landmarks is not None else []

            # Always surface the current metrics/state so an overlay can render.
            events.append(E.drowsiness_metrics(metrics, yawn_state,
                                               drowsy_state))

            # ★business★ edge event: blink (eyes-closed rising edge, valid face)
            if metrics.valid:
                if metrics.eyes_closed and not self._prev_closed:
                    request_configured_recording(self, "blink", t)
                    self._blink_count += 1
                    events.append({"kind": "blink",
                                   "blink_count": self._blink_count,
                                   "avg_ear": round(metrics.avg_ear, 3)})
                self._prev_closed = metrics.eyes_closed
            else:
                self._prev_closed = False

            # ★business★ edge event: yawn onset.
            if yawn_event:
                request_configured_recording(self, "yawn", t)
                events.append({"kind": "yawn",
                               "yawn_count_5min": int(yawn_state.yawn_count_5min),
                               "mar": round(metrics.mar, 3)})
                print(f"[facemesh] *** YAWN #{yawn_state.yawn_count_5min} "
                      f"mar={metrics.mar:.2f} at t={t:.2f} ***", flush=True)

            # ★business★ edge event: drowsiness alert active (Drowsy/Danger).
            if drowsy_state.alert_active:
                pending = (getattr(self, "_recording_drowsy_pending", False)
                           or not getattr(self, "_recording_drowsy_active", False))
                self._recording_drowsy_pending = bool(pending and not
                    request_configured_recording(self, "drowsiness", t))
                events.append({
                    "kind": "drowsiness",
                    "state": drowsy_state.state,
                    "drowsiness_level": round(drowsy_state.drowsiness_level, 3),
                    "drowsy_by_ear": bool(drowsy_state.drowsy_by_ear),
                    "drowsy_by_perclos": bool(drowsy_state.drowsy_by_perclos),
                    "drowsy_by_yawn": bool(drowsy_state.drowsy_by_yawn),
                    "perclos_pct": round(drowsy_state.perclos_pct, 1),
                })

            if not drowsy_state.alert_active:
                self._recording_drowsy_pending = False
            self._recording_drowsy_active = bool(drowsy_state.alert_active)
            self.emit(events, frame.pts, results=results)


if __name__ == "__main__":
    run_app(FacemeshReaderApp())

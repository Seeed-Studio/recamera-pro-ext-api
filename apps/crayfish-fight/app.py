#!/usr/bin/env python3
"""
crayfish-fight -- three-stage crayfish behaviour funnel for reCamera Pro.

The customer's ask (docs/PLAN.md §3.2): per animal a box and a sex, and per
PAIR an event when one is fighting or harassing the other. Video never leaves
the tank room -- only events, and a quota-limited trickle of JPEGs that feeds
the next training round.

Three models, one loop, in cost order (PLAN §1.1 -- the funnel is the whole
point: behaviour is rare in time, so the expensive stage must not run per
frame):

  frame -> self.pre()                RGA letterbox to 640
        -> self.models.det          yolo11 raw head, single class "crayfish"
        -> detect.postprocess       boxes in ORIGINAL pixels, score-desc
        -> self.tracker.update      ★identity★ -- everything below is per TRACK,
                                    never per detection index
        -> ★stage A★ per NEW track only: sex_cls on the animal's own crop for
           its first `sex_vote_frames` frames, majority vote, then never again
        -> ★stage B★ proximity: every visible track PAIR is tested with
           `pair_is_close` (IoU>0 or centre distance < k x mean box diagonal)
           and pushed through a `min_hits`/`window` sliding window -- pure
           arithmetic, no inference
        -> ★stage C★ only for triggered pairs: behavior_cls on the padded UNION
           ROI, fed through `BehaviorStateMachine` (needs `behavior_streak`
           consecutive confirmations to raise, `behavior_release` misses to
           clear)
        -> capture: on every trigger, quota permitting, dump frame + ROI + json
           to /userdata/crayfish/<date>/ REGARDLESS of the verdict -- the
           misfires are exactly the samples the negative class needs
        -> self.emit()              per-animal `detection` events (with sex) +
                                    per-pair `behavior` events

Coordinates: everything inside is ORIGINAL-frame xyxy PIXELS, the space
`kit.runtime.postprocess.detect` returns. `emit()`'s sink de-normalises to
[0,1] against `frame.w/h` before injecting into the OSD (AGENTS.md's coordinate
contract; see `OfficialResultSink`), which is why yolo-detector and this app
both publish pixels and both declare `"coord": "pixel_xyxy"` in the manifest.
The one place normalisation happens here is the capture sidecar json, which is
written resolution-independent so a re-scaled dataset stays valid.

`model_frame = "hw-roi"`: stage A and stage C each need a per-object crop off
the CAMERA frame. That is the same shape as face-analysis, and the reason this
app is not "hw-direct": under hw-direct there is no ROI cropper and
`frame.data` holds the letterbox, so a crop would read the wrong pixels. ROIs
therefore MUST go through `self.crop_roi_hw`, never `frame.data`.

Run on device (inference requires root):
    python3 -m kit.run /userdata/local/apps/crayfish-fight --sink ws --port 8124
"""
from __future__ import annotations

import itertools
import json
import os
import time

from kit.app import App, run_app
from kit.logic.tracker import Tracker, TrackerConfig
from kit.runtime.postprocess import classify as clf
from kit.runtime.postprocess.detect import postprocess as detect_post

from logic import (BEHAVIOR_LABELS, SEX_LABELS, SEX_UNKNOWN, BehaviorStateMachine,
                   CaptureQuota, ProximityWindow, SexVoter, pair_is_close,
                   pair_key, to_norm, union_box)

# Manifest model ids and their input sides (manifest models[].input).
DET_ID = "crayfish_det"
SEX_ID = "sex_cls"
BEHAVIOR_ID = "behavior_cls"
SEX_INPUT = 128
BEHAVIOR_INPUT = 224

# Detector vocabulary. Single class -- declared here AND in the manifest
# (`models[].classes`), which is what the kit binds onto `self.class_names`.
CRAYFISH_CLASSES = ["crayfish"]


class CrayfishFightApp(App):
    id = "crayfish-fight"
    name = "Crayfish Fight Monitor"
    owns_loop = True
    # Stages A and C crop per-object ROIs off the camera dma-buf on RGA via
    # self.crop_roi_hw. NOT "hw-direct" (no cropper; frame.data is the
    # letterbox) -- see the module docstring.
    model_frame = "hw-roi"
    class_names = CRAYFISH_CLASSES

    # Fallbacks for the auto-bound config_schema keys (the manifest supplies
    # each default; these keep the app runnable with no config at all).
    conf = 0.35
    iou = 0.45
    max_animals = 8
    sex_vote_frames = 10
    sex_min_conf = 0.65
    sex_crop_pad = 0.10
    proximity_k = 1.2
    proximity_window = 8
    proximity_hits = 5
    roi_pad = 0.15
    behavior_streak = 3
    behavior_release = 3
    behavior_min_conf = 0.5
    track_max_lost = 30
    capture_enabled = True
    capture_per_minute = 6
    capture_per_day = 2000
    capture_dir = "/userdata/crayfish"
    capture_full_frame_px = 1280

    def setup(self, config):
        """Build the four stateful helpers from the already-bound params."""
        super().setup(config)
        self._tracker = Tracker(self._tracker_config())
        self._sex = SexVoter(self.sex_vote_frames, self.sex_min_conf)
        self._prox = ProximityWindow(self.proximity_window, self.proximity_hits)
        self._fsm = BehaviorStateMachine(self.behavior_streak,
                                         self.behavior_release,
                                         self.behavior_min_conf)
        self._quota = CaptureQuota(self.capture_per_minute, self.capture_per_day)
        self._prev_pairs = set()
        self._capture_warned = False
        print(f"[crayfish-fight] setup conf={self.conf} iou={self.iou} "
              f"max_animals={self.max_animals} "
              f"sex(vote={self.sex_vote_frames}, min_conf={self.sex_min_conf}) "
              f"prox(k={self.proximity_k}, {self.proximity_hits}/"
              f"{self.proximity_window}) roi_pad={self.roi_pad} "
              f"behavior(streak={self.behavior_streak}, "
              f"release={self.behavior_release}, "
              f"min_conf={self.behavior_min_conf}) "
              f"capture={self.capture_enabled}@{self.capture_dir} "
              f"({self.capture_per_minute}/min, {self.capture_per_day}/day)",
              flush=True)

    # -- derived-object builders ------------------------------------------ #
    def _tracker_config(self) -> TrackerConfig:
        """Tank-tuned tracker policy.

        The kit default (90 lost frames) is a retail person tracker's 3-second
        occlusion budget. In a tank the animals do occlude each other -- that is
        literally the `harass` label -- but they also do not leave and come back
        as a different individual, so a shorter budget only costs a new id on a
        long occlusion, while a long one risks welding two animals' sex votes
        together. `assumed_fps` matches the 10-15 fps this app plans for
        (PLAN §1.1), not the kit's 15 default reused blindly.
        """
        lost = max(1, int(self.track_max_lost))
        return TrackerConfig(max_lost_frames_center=lost,
                             max_lost_frames_edge=max(1, lost // 2),
                             assumed_fps=12.0)

    def on_params_changed(self, changed):
        """SIGHUP hot-reload for the apply:"live" knobs.

        Scalars (conf / iou / proximity_k / roi_pad / capture_enabled) are
        already re-bound on `self` and read fresh inside the loop. The ones that
        feed derived objects are mirrored IN PLACE so a config change never
        throws away the tracker's identities, the accumulated sex votes, or an
        open behaviour event.
        """
        if changed & {"proximity_window", "proximity_hits"}:
            self._prox.window = max(1, int(self.proximity_window))
            self._prox.min_hits = max(1, min(int(self.proximity_hits),
                                             self._prox.window))
        if changed & {"behavior_streak", "behavior_release", "behavior_min_conf"}:
            self._fsm.min_streak = max(1, int(self.behavior_streak))
            self._fsm.release_frames = max(1, int(self.behavior_release))
            self._fsm.min_conf = float(self.behavior_min_conf)
        if changed & {"sex_vote_frames", "sex_min_conf"}:
            self._sex.vote_frames = max(1, int(self.sex_vote_frames))
            self._sex.min_conf = float(self.sex_min_conf)
        if changed & {"capture_per_minute", "capture_per_day"}:
            self._quota.per_minute = max(0, int(self.capture_per_minute))
            self._quota.per_day = max(0, int(self.capture_per_day))
        if "track_max_lost" in changed:
            new = self._tracker_config().clamp()
            self._tracker.cfg.max_lost_frames_center = new.max_lost_frames_center
            self._tracker.cfg.max_lost_frames_edge = new.max_lost_frames_edge
        print(f"[crayfish-fight] hot-reload changed={sorted(changed)}", flush=True)

    # -- main loop --------------------------------------------------------- #
    def run(self):
        for frame in self.frames():
            t = frame.pts

            # -- 1. detect ------------------------------------------------ #
            x = self.pre(frame)
            outs = self.models[DET_ID].infer(x.data)
            results = detect_post(outs, x.info, conf_thres=self.conf,
                                  iou_thres=self.iou,
                                  class_names=self.class_names)
            results = results[: int(self.max_animals)]

            # -- 2. identity ---------------------------------------------- #
            tracks = self._tracker.update(results, t, frame.w, frame.h)
            self._sex.drop(self._tracker.removed_ids)
            for tid in self._tracker.removed_ids:
                self._prox.drop_track(tid)
            by_det = {tr.det_index: tr for tr in tracks if tr.det_index >= 0}

            # -- 3. stage A: sex, only while a track is still voting ------ #
            boxes = {}          # track_id -> box in ORIGINAL pixels
            for i, r in enumerate(results):
                tr = by_det.get(i)
                if tr is None:
                    r["track_id"] = None
                    r["sex"] = SEX_UNKNOWN
                    r["sex_conf"] = 0.0
                    continue
                r["track_id"] = tr.track_id
                boxes[tr.track_id] = r["box"]
                if self._sex.needs_vote(tr.track_id):
                    roi, _ = self.crop_roi_hw(frame, r["box"], SEX_INPUT,
                                              self.sex_crop_pad)
                    head = clf.classify_head(
                        clf.logits_from(self.models[SEX_ID].infer(roi),
                                        size=len(SEX_LABELS)), SEX_LABELS)
                    self._sex.add(tr.track_id, head["label"], head["confidence"])
                v = self._sex.verdict(tr.track_id)
                r["sex"] = v.label
                r["sex_conf"] = v.confidence
                r["sex_settled"] = v.settled

            # -- 4. stage B: proximity, arithmetic only ------------------- #
            obs = {}
            for a, b in itertools.combinations(sorted(boxes), 2):
                obs[pair_key(a, b)] = pair_is_close(boxes[a], boxes[b],
                                                    float(self.proximity_k))
            triggered = self._prox.update(obs)

            # -- 5. stage C: behaviour on the union ROI ------------------- #
            events = []
            for key in triggered:
                ub = union_box(boxes[key[0]], boxes[key[1]],
                               clip=(frame.w, frame.h))
                roi, _ = self.crop_roi_hw(frame, ub, BEHAVIOR_INPUT,
                                          float(self.roi_pad))
                head = clf.classify_head(
                    clf.logits_from(self.models[BEHAVIOR_ID].infer(roi),
                                    size=len(BEHAVIOR_LABELS)), BEHAVIOR_LABELS)
                ev = self._fsm.update(key, head["label"], head["confidence"], t)
                if ev is not None:
                    events.append(self._behavior_event(ev, key, ub, boxes))
                # ★flywheel★ every trigger is a sample, verdict or not.
                self._capture(frame, key, ub, head, t)

            # A pair that stopped being triggered must not leave an event open.
            for key in self._prev_pairs - set(triggered):
                ev = self._fsm.timeout(key, t)
                if ev is not None:
                    events.append(self._behavior_event(ev, key, None, boxes))
            self._prev_pairs = set(triggered)

            # -- 6. emit -------------------------------------------------- #
            events.extend(self._detection_event(r) for r in results)
            self.emit(events, t, results=results)

    # -- event builders (mechanical) --------------------------------------- #
    @staticmethod
    def _detection_event(r) -> dict:
        """One detection + its voted sex. Box stays ORIGINAL pixels (the sink
        normalises); `sex` is the accumulated vote, not this frame's argmax."""
        return {
            "kind": "detection",
            "label": r.get("cls_name", "crayfish"),
            "cls": r.get("cls", 0),
            "score": round(float(r.get("score", 0.0)), 4),
            "box": r["box"],
            "track_id": r.get("track_id"),
            "sex": r.get("sex", SEX_UNKNOWN),
            "sex_conf": r.get("sex_conf", 0.0),
        }

    @staticmethod
    def _behavior_event(ev, key, union, boxes) -> dict:
        """One state-machine transition -> the published `behavior` event."""
        out = {
            "kind": "behavior",
            "phase": ev["phase"],
            "label": ev["label"],
            "confidence": ev["confidence"],
            "track_ids": list(key),
            "frames": ev["frames"],
            "duration_sec": ev["duration_sec"],
        }
        if ev.get("previous"):
            out["previous"] = ev["previous"]
        if union is not None:
            out["box"] = [round(v, 1) for v in union]
        return out

    # -- capture (data flywheel) ------------------------------------------- #
    def _capture(self, frame, key, union, head, t) -> None:
        """Dump one triggered interaction: full frame + ROI + sidecar json.

        Written on EVERY trigger the quota allows, whatever the classifier said:
        a trigger the classifier called `none` is either a true negative worth
        keeping (perspective overlap -- the hard negative class per PLAN §3.2)
        or a miss worth relabelling. Filtering by verdict here would starve the
        retraining set of exactly the samples that fix it.

        Failures are swallowed after one warning: a full or read-only
        /userdata must never take the detector down.
        """
        if not self.capture_enabled:
            return
        now = time.time()
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        if not self._quota.allow(now, day):
            return
        try:
            import cv2

            out_dir = os.path.join(self.capture_dir, day)
            os.makedirs(out_dir, exist_ok=True)
            stem = (f"{time.strftime('%H%M%S', time.localtime(now))}"
                    f"_{int((now % 1) * 1000):03d}_t{key[0]}-{key[1]}")

            # Full frame straight off the camera: under hw-roi `frame.data` is
            # the 640 letterbox, so the full-FOV image is fetched through the
            # same RGA cropper with a whole-frame box.
            full, _ = self.crop_roi_hw(frame, [0, 0, frame.w, frame.h],
                                       int(self.capture_full_frame_px), 0.0)
            roi, _ = self.crop_roi_hw(frame, union, BEHAVIOR_INPUT,
                                      float(self.roi_pad))
            cv2.imwrite(os.path.join(out_dir, stem + "_frame.jpg"),
                        full[:, :, ::-1], [int(cv2.IMWRITE_JPEG_QUALITY), 88])
            cv2.imwrite(os.path.join(out_dir, stem + "_roi.jpg"),
                        roi[:, :, ::-1], [int(cv2.IMWRITE_JPEG_QUALITY), 92])

            meta = {
                "app": self.id,
                "ts": round(now, 3),
                "pts": round(float(t), 3),
                "frame": {"w": frame.w, "h": frame.h},
                "track_ids": list(key),
                "union_box_norm": [round(v, 6) for v in
                                   to_norm(union, frame.w, frame.h)],
                "behavior": {"label": head["label"],
                             "confidence": head["confidence"],
                             "probs": head["probs"],
                             "labels": BEHAVIOR_LABELS},
                "sex": {str(tid): self._sex.sex_of(tid) for tid in key},
                "quota": self._quota.stats(),
            }
            with open(os.path.join(out_dir, stem + ".json"), "w",
                      encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False)
        except Exception as e:                       # pragma: no cover - device
            if not self._capture_warned:
                self._capture_warned = True
                print(f"[crayfish-fight] capture disabled for this run ({e})",
                      flush=True)


if __name__ == "__main__":
    run_app(CrayfishFightApp())

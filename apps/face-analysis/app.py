#!/usr/bin/env python3
"""
face-analysis -- reCamera Pro three-stage face cascade (port of the first-gen
SSCMA face-analysis / audience-analytics solution).

`run()` owns the loop and the whole cascade reads top to bottom as ordinary
Python (internal/KIT_APP_SHAPE_SPEC.md §1/§3) -- no cascade framework, no
declarative stage list:

  frame -> self.pre()             letterbox to 640 (manifest models[0].input)
        -> self.models.det        YOLOv8n-face rawhead
        -> face_post.postprocess  face boxes in ORIGINAL pixels, score-desc
        -> results[:max_faces]    ★business★ top-K faces
        -> self.tracker.update    ★business★ stable identity per face
        -> for each tracked face: <-- stages 2+3 are a PLAIN `for`, spec §3
             attributes.passes_gate  skip ROIs too small to classify
             self.crop_roi_hw        padded SQUARE ROI, cropped on RGA straight
                                     from the camera NV12 dma-buf (hw-roi mode)
             self.models.fairface_fp16        (1,18) -> race/gender/age heads
             self.models.emotion_enet_b0_fp16 (1,8)  -> AffectNet emotion,
                                     ★business★ only every `emotion_interval`
                                     frames
             agg.track(id).add(...)  fold this frame's probabilities into the
                                     track's evidence; the reported label is the
                                     argmax of the ACCUMULATED evidence
        -> once-per-track demographic histogram (★business★, cross-frame)
        -> self.emit()            one `face` event per face + the periodic
                                  `demographics` aggregate + results[]

`model_frame = "hw-roi"`: stage-1 runs on the RGA letterbox and stages 2/3 crop
each face ROI off the camera NV12 dma-buf on RGA (`self.crop_roi_hw`), so the
per-frame full-resolution NV12->RGB convert the old "cpu" path paid is gone --
this is the cascade path added in docs/guide/hw-preprocess.md §7. It stays
correct if librga lacks the crop op (source degrades to "hw", crop_roi_hw falls
to the numpy crop) or the frame backend has no dma-buf (RTSP/snapshot). ROIs
MUST go through `self.crop_roi_hw`, never `frame.data` (which is the letterbox,
not the camera frame). "hw-direct" would be wrong here (no cropper), and plain
"hw" measured +0.8% (noise) because it still pays the full-res convert. See
docs/guide/hw-preprocess.md before touching this.

★Accuracy★ -- four things this app does NOT do naively, each of which was a
measured-wrong behaviour before (see the accuracy review in the 0.2.0 notes):

  * **Identity, not slot.** Faces are tracked (`kit.logic.tracker`), so stage-3
    results and accumulated evidence are keyed by `track_id`. The previous
    version cached the emotion verdict under the face's INDEX in the score-sorted
    list, so as soon as `emotion_interval > 1` and the ordering shifted, one
    person was handed another person's cached emotion.
  * **Vote, not single frame.** Every reported label is the argmax of the
    probability mass accumulated over that track's gate-passing frames
    (`kit.logic.attributes`), not one frame's argmax, which flickers.
  * **People, not face-frames.** The demographic histogram folds in each
    `track_id` exactly once. Bumping it per face per frame -- what the previous
    version did -- measures dwell time, not audience: one person standing still
    for a minute outvoted sixty people walking past.
  * **Gate before classify.** A face whose box is smaller than `min_face_px` is
    upsampling artefact by the time it reaches the 224 classifier input, so it
    is skipped entirely: no inference spent, no evidence contributed, and the
    result carries `gated: true` instead of a confident-looking coin toss.

`crop_pad` defaults to **-0.05** -- a NEGATIVE pad, i.e. the classifier ROI is
5% TIGHTER than the detector box before it is squared. That is a measured value,
not a guess: an offline sweep over 1942 FairFace val images through this exact
geometry (`square_roi_geometry`, imported by the harness rather than
reimplemented) is single-peaked at -0.05, and the curve falls off hard on the
positive side -- race 0.712 at -0.05 vs 0.551 at +0.25, a 16-point drop. The
reason the optimum is negative: yolov8n-face emits a looser box than the dlib
face rect FairFace was trained on, and squaring it widens the framing again, so
the crop has to be pulled back in to land on the training distribution. Reasoning
from "the checkpoint was trained at dlib padding=0.25" to "set crop_pad=0.25" is
exactly the mistake the sweep caught: the two paddings are measured against
different rectangles. See docs/guide/face-attribute-accuracy.md for the table.

At this padding the plain square crop is statistically level with feeding the
model FairFace's own aligned chips (gender/race/age all within the ±1.04 pt noise
floor), and similarity-transform ALIGNMENT measured 1-2.6 points WORSE than it.
So this app deliberately does not align: alignment only wins against a
mis-padded baseline.

All three models are declared in the manifest `models[]` and preloaded by the
kit, so both classifiers are reached by their manifest ids (both claim the
`classify` task, so the `.cls` alias is ambiguous and deliberately dropped --
see kit.app.ModelRegistry).

Every knob is auto-bound from the manifest config_schema and re-bound on SIGHUP
for the apply:"live" ones, so there is no setup() param-copying.

ImageNet normalization is baked into both classifier rknns, so we feed the raw
uint8 224x224 RGB ROI straight to the engine (no /255, no mean/std here).

Run on device (inference requires root):
    python3 -m kit.run /userdata/local/apps/face-analysis \
        --model models/yolov8n_face_rawhead_fp16.rknn --sink ws --port 8124
"""

from kit.app import App, run_app
from kit import events as E
from kit.logic.attributes import AttributeConfig, Aggregator, passes_gate
from kit.logic.tracker import Tracker, TrackerConfig
from kit.runtime.postprocess import face_detect as face_post
from kit.runtime.postprocess import classify as clf

# Classifier input sides (manifest models[1].input / models[2].input).
FF_INPUT = 224
EMO_INPUT = 224

# Manifest model ids. Both classifiers declare task "classify", so the `.cls`
# alias is ambiguous and deliberately dropped by the registry.
FF_ID = "fairface_fp16"
EMO_ID = "emotion_enet_b0_fp16"

# Head name -> label vocabulary, shared by the accumulator and the emitted
# fields. The three FairFace heads come off one 18-vector; emotion is its own
# model. Order is the model's, not ours (kit.runtime.postprocess.classify).
LABELS = {
    "race": clf.RACE_LABELS,
    "gender": clf.GENDER_LABELS,
    "age": clf.AGE_LABELS,
    "emotion": clf.EMOTION_LABELS,
}
FF_HEADS = ("race", "gender", "age")
ALL_HEADS = ("gender", "age", "race", "emotion")


class FaceAnalysisApp(App):
    id = "face-analysis"
    name = "Face Analysis"
    owns_loop = True          # explicit new shape: run() drives self.frames()
    # Stages 2/3 crop each face ROI off the camera dma-buf on RGA via
    # self.crop_roi_hw -- see the module docstring. Must NOT be "hw-direct"
    # (no cropper) or "cpu" (pays the full-res convert this path removes).
    model_frame = "hw-roi"

    # Fallbacks for the auto-bound config_schema keys (used when a key is
    # missing from the effective config; the manifest supplies each default).
    confidence = 0.4
    iou = 0.45
    max_faces = 5
    crop_pad = -0.05
    min_face_px = 64
    emotion_interval = 1
    min_track_frames = 3
    evidence_decay = 1.0
    track_max_lost = 15
    aggregate_window_sec = 30.0
    privacy_blur = True

    def setup(self, config):
        """Build the tracker and the evidence store from the already-bound params.

        Called by `App.start()` AFTER the config_schema auto-bind, so every
        `self.<knob>` below is already populated. All three RKNNs are preloaded
        by the kit from the manifest `models[]`.
        """
        super().setup(config)

        self._frame_idx = 0
        self._tracker = Tracker(self._tracker_config())
        self._agg = Aggregator(self._attr_config(), heads=ALL_HEADS)
        self._window_started = False

        print(f"[face-analysis] setup conf={self.confidence} iou={self.iou} "
              f"max_faces={self.max_faces} crop_pad={self.crop_pad} "
              f"min_face_px={self.min_face_px} "
              f"fairface={FF_ID}({FF_INPUT}) emotion={EMO_ID}({EMO_INPUT}) "
              f"emotion_interval={self.emotion_interval} "
              f"min_track_frames={self.min_track_frames} "
              f"evidence_decay={self.evidence_decay} "
              f"track_max_lost={self.track_max_lost} "
              f"agg_window={self.aggregate_window_sec}s "
              f"privacy_blur={self.privacy_blur}", flush=True)

    # -- derived-object builders ------------------------------------------ #
    def _tracker_config(self) -> TrackerConfig:
        """Face-tuned tracker policy.

        The kit defaults are tuned for the retail PERSON tracker, where a
        3-second occlusion budget (90 frames @30fps) is right. This cascade runs
        far slower than the capture rate -- every extra face costs two
        224-input classifier inferences -- so 90 frames can be 15+ wall-clock
        seconds, long enough for a track to be re-associated onto a DIFFERENT
        person who walked into the same spot, which then pollutes that track's
        accumulated evidence. `track_max_lost` caps it; the edge budget stays
        proportionally shorter because an edge loss is usually a real exit.
        """
        lost = max(1, int(self.track_max_lost))
        return TrackerConfig(max_lost_frames_center=lost,
                             max_lost_frames_edge=max(1, lost // 2))

    def _attr_config(self) -> AttributeConfig:
        """Gate / voting policy. Calibration (temperature, per-head confidence
        floors) is left at its no-op default on purpose: both need a measured
        held-out set behind them, and `AttributeConfig` only supplies the
        mechanism. See the accuracy notes before setting either."""
        return AttributeConfig(min_face_px=float(self.min_face_px),
                               min_track_frames=int(self.min_track_frames),
                               decay=float(self.evidence_decay))

    def on_params_changed(self, changed):
        """★S1 live hot-reload★ -- after SIGHUP re-bound the apply:"live" keys.

        Most live knobs (confidence / iou / max_faces / crop_pad /
        emotion_interval / privacy_blur) are plain scalars the auto-bind has
        already replaced on `self`, read fresh inside the loop. The two that
        feed DERIVED objects -- the gate/voting policy and the tracker budget --
        are mirrored in place, so neither the tracker's identities nor the
        accumulated per-track evidence nor the open demographic window is
        thrown away by a config change. `aggregate_window_sec` is
        apply:"restart" and never reaches here.
        """
        if changed & {"min_face_px", "min_track_frames", "evidence_decay"}:
            cfg = self._attr_config().clamp()
            # Mutate in place: every live TrackAttributes holds this same object.
            self._agg.cfg.min_face_px = cfg.min_face_px
            self._agg.cfg.min_track_frames = cfg.min_track_frames
            self._agg.cfg.decay = cfg.decay
        if "track_max_lost" in changed:
            new = self._tracker_config().clamp()
            self._tracker.cfg.max_lost_frames_center = new.max_lost_frames_center
            self._tracker.cfg.max_lost_frames_edge = new.max_lost_frames_edge

        print(f"[face-analysis] hot-reload changed={sorted(changed)} "
              f"conf={self.confidence} iou={self.iou} "
              f"max_faces={self.max_faces} crop_pad={self.crop_pad} "
              f"min_face_px={self.min_face_px} "
              f"emotion_interval={self.emotion_interval} "
              f"min_track_frames={self.min_track_frames} "
              f"evidence_decay={self.evidence_decay} "
              f"track_max_lost={self.track_max_lost} "
              f"privacy_blur={self.privacy_blur}", flush=True)

    # -- helpers (business: the demographic window) ------------------------ #
    def _roll_window(self, t: float):
        """Emit a demographics aggregate when the window elapses; reset it."""
        if not self._window_started:
            self._agg.reset_window(t)
            self._window_started = True
            return None
        if self._agg.elapsed(t) < self.aggregate_window_sec:
            return None
        event = self._agg.snapshot(t)
        self._agg.reset_window(t)
        return event

    def run(self):
        for frame in self.frames():
            # -- 1. pre / infer / stage-1 post --------------------------- #
            x = self.pre(frame)
            outs = self.models.det.infer(x.data)
            results = face_post.postprocess(outs, x.info,
                                            conf_thres=self.confidence,
                                            iou_thres=self.iou)

            # ★privacy★ kind and the blur flag are stamped on EVERY detection,
            # BEFORE the top-K slice. `results` is what the sink publishes and
            # what an overlay renders, so tagging only the top-K -- as the
            # previous version did -- silently left every face past `max_faces`
            # unflagged in exactly the crowded scene where blurring matters.
            for r in results:
                r["kind"] = "face"
                r["blur"] = self.privacy_blur

            self._frame_idx += 1
            t = frame.pts
            interval = max(1, int(self.emotion_interval))
            run_emotion = (self._frame_idx % interval) == 0

            # -- 2. identity: track the faces we will classify ----------- #
            # Only the top-K enter the tracker: they are the faces that can
            # accumulate evidence, and feeding it detections we never classify
            # would spawn ids that never produce a verdict.
            faces = results[: int(self.max_faces)]
            tracks = self._tracker.update(faces, t, frame.w, frame.h)
            self._agg.sweep(self._tracker.removed_ids)
            by_det = {tr.det_index: tr for tr in tracks if tr.det_index >= 0}

            # -- 3. stages 2+3: one padded square ROI per tracked face ---- #
            # A plain Python loop, not a declared pipeline stage. Each ROI is
            # cropped on RGA straight from the camera dma-buf (model_frame=
            # "hw-roi") via self.crop_roi_hw -- never from frame.data, which in
            # this mode holds the stage-1 letterbox, not the camera frame.
            for i, r in enumerate(faces):
                tr = by_det.get(i)
                r["track_id"] = tr.track_id if tr is not None else None

                # ★business★ quality gate, BEFORE the crop: too small to
                # classify means no inference spent and no evidence polluted.
                if tr is None or not passes_gate(r["box"], r.get("score", 0.0),
                                                 self._agg.cfg):
                    r["gated"] = True
                    r["stable"] = False
                    continue
                r["gated"] = False

                ta = self._agg.track(tr.track_id)
                roi, _roi_map = self.crop_roi_hw(frame, r["box"],
                                                 FF_INPUT, self.crop_pad)

                # stage 2: FairFace age / gender / race -> evidence
                ff = clf.fairface_decode(self.models[FF_ID].infer(roi),
                                         temperature=self._agg.cfg.temperature)
                for head in FF_HEADS:
                    ta.add(head, ff[head]["probs"])

                # stage 3: ★business★ emotion runs every `interval` frames. No
                # cache is needed: the accumulator IS the per-track memory, so
                # between runs `verdict("emotion")` keeps returning that track's
                # own accumulated verdict -- never a neighbour's.
                if run_emotion:
                    if EMO_INPUT != FF_INPUT:
                        roi_e, _ = self.crop_roi_hw(frame, r["box"],
                                                    EMO_INPUT, self.crop_pad)
                    else:
                        roi_e = roi
                    em = clf.emotion_decode(
                        self.models[EMO_ID].infer(roi_e),
                        temperature=self._agg.cfg.temp("emotion"))
                    ta.add("emotion", em["probs"])

                ta.bump_frame(t)
                self._agg.note_face_frame()

                # ★business★ report the VOTE over this track's evidence, not
                # this single frame's argmax.
                verdicts = {h: ta.verdict(h, LABELS[h]) for h in ALL_HEADS}
                for head in FF_HEADS:
                    r[head] = verdicts[head]["label"]
                    r[f"{head}_conf"] = verdicts[head]["confidence"]
                if verdicts["emotion"]["index"] >= 0:
                    r["emotion"] = verdicts["emotion"]["label"]
                    r["emotion_conf"] = verdicts["emotion"]["confidence"]
                r["stable"] = ta.stable
                r["evidence_frames"] = ta.frames

                # ★business★ fold this PERSON into the window histogram -- once,
                # the first time their evidence is stable.
                self._agg.maybe_count(tr.track_id,
                                      {h: verdicts[h]["label"] for h in ALL_HEADS})

            # -- 4. events: one attribute event per face ----------------- #
            events = [E.face_attributes(r, blur=self.privacy_blur)
                      for r in faces]

            agg = self._roll_window(t)
            if agg is not None:
                events.append(agg)
                print(f"[face-analysis] demographics window={agg['window_sec']}s "
                      f"faces={agg['faces']} face_frames={agg['face_frames']} "
                      f"gender={agg['gender']} age={agg['age']} "
                      f"race={agg['race']} emotion={agg['emotion']}", flush=True)

            self.emit(events, frame.pts, results=results)


if __name__ == "__main__":
    run_app(FaceAnalysisApp())

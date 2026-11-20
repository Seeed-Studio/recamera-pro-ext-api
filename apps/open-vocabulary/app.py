"""Live camera -> dynamic CLIP prompts -> scheduled YOLO-World -> Kit overlay."""
import os
import time

import numpy as np

from kit.app import App, run_app
from kit import config as kit_config, events

from clip_tokenizer import ClipTokenizer
from model_contracts import load_scheduled_model
from object_color import box_color
from vocabulary import Vocabulary
from world_postprocess import detection_result, postprocess


class OpenVocabularyApp(App):
    id = "open-vocabulary"
    name = "Open Vocabulary Detection"
    owns_loop = True
    input_size = 640
    model_frame = "hw-roi"
    vocabulary = "helmet, cup, person"
    max_fps = 5.0
    max_detections = 40

    def _load_model(self, path):
        # Kit retains the resulting session in self.models and releases it on
        # shutdown. Use the typed factory for mixed image/text inputs.
        return load_scheduled_model(path)

    def setup(self, config):
        super().setup(config)
        tokenizer = ClipTokenizer(os.path.join(kit_config.app_dir_of(self), "tokenizer"))
        self._vocab = Vocabulary(tokenizer)
        self._requested = None
        self._pending = None
        self._error = ""
        self._next_infer = 0.0
        self._refresh_vocabulary(immediate=True)
        if self._error:
            raise ValueError(self._error)

    def _refresh_vocabulary(self, immediate=False):
        if self.vocabulary == self._requested:
            return
        self._requested = self.vocabulary
        self._pending = None
        try:
            pending = self._vocab.begin(self.vocabulary)
        except ValueError as exc:
            # User input errors keep the previous working vocabulary. Model or
            # transport failures propagate so AppMgr can report/recover them.
            self._error = str(exc)
            print(f"[{self.id}] vocabulary rejected: {exc}", flush=True)
            return
        self._error = ""
        if pending.changed:
            self._pending = pending
        if immediate:
            while self._pending is not None:
                self._advance_vocabulary()

    def _advance_vocabulary(self):
        if self._pending is not None and self._pending.step(self.models.text_encoder):
            self._pending = None
            print(f"[{self.id}] vocabulary revision {self._vocab.revision}: "
                  f"{[t.label for t in self._vocab.terms]}", flush=True)

    def _infer(self, frame):
        prepared = self.pre(frame)
        image = np.ascontiguousarray(prepared.data, dtype=np.uint8)
        if image.ndim == 3:
            image = image[None]
        outputs = self.models.detector.infer([image, self._vocab.features])
        return outputs, prepared.info

    def _warm_up(self, frame):
        # Kit's default warm-up supplies only an image; this detector has TWO
        # inputs. Keep the same discarded first frame with the correct inputs.
        if self._vocab.features is not None:
            self._infer(frame)

    def _match_requests(self, frame, candidates):
        detections = []
        for candidate in candidates:
            requests = self._vocab.requests_by_slot[candidate["cls"]]
            plain = next((index for index, color in requests if color is None), None)
            actual = None
            if any(color is not None for _, color in requests):
                actual = box_color(frame, candidate["box"], self.crop_roi_hw)
            # One box per object. A matching color label is more specific than
            # the unfiltered noun, which still accepts uncertain colors.
            chosen = next((index for index, color in requests
                           if color is not None and color == actual), plain)
            if chosen is not None:
                row = detection_result(candidate["box"], chosen, candidate["score"], self._vocab.terms[chosen])
                if actual is not None:
                    row["object_color"] = actual
                detections.append(row)
                if len(detections) >= self.max_detections:
                    break
        return detections

    def run(self):
        for frame in self.frames():
            previous_revision = self._vocab.revision
            self._refresh_vocabulary()
            # Return the borrowed frame after at most one new text encoding,
            # instead of holding it across an entire 80-prompt update.
            self._advance_vocabulary()
            if previous_revision != self._vocab.revision:
                self._next_infer = 0.0
            now = time.monotonic()
            if now < self._next_infer:
                continue
            self._next_infer = now + 1.0 / max(0.1, min(30.0, float(self.max_fps)))
            dets = []
            if self._vocab.features is not None:
                outputs, info = self._infer(frame)
                # Keep room for matches when higher scoring candidates have
                # the wrong color. Bound the number of ROI checks per frame.
                candidate_limit = self.max_detections
                if self._vocab.has_color_queries:
                    candidate_limit = max(candidate_limit, 80)
                candidates = postprocess(outputs, info, self._vocab.model_terms,
                                         conf=self.conf, iou=self.iou,
                                         max_detections=candidate_limit)
                dets = self._match_requests(frame, candidates)
            self.emit([events.detection(d) for d in dets], frame.pts, results=dets,
                      extra={"vocabulary": [t.label for t in self._vocab.terms],
                             "vocabulary_revision": self._vocab.revision,
                             "vocabulary_updating": self._pending is not None,
                             "config_error": self._error})


if __name__ == "__main__":
    run_app(OpenVocabularyApp())

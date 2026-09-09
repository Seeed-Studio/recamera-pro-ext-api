#!/usr/bin/env python3
"""
ppocr-reader -- reCamera Pro on-device OCR (port of the first-gen SSCMA
solutions/ppocr-reader C++ pipeline to the Python kit).

Migrated to the new kit shape (internal/KIT_APP_SHAPE_SPEC.md §1/§3): `run()`
owns the loop and the whole two-stage cascade reads top to bottom as ordinary
Python -- there is no cascade framework and no declarative stage list:

  frame -> self.pre()             letterbox to 480 (NOT 640; the DB detector's
                                  input side comes from manifest models[0].input)
        -> self.models.det        DBNet, one (1,1,480,480) sigmoid prob map
        -> db_ocr.decode          threshold -> contours -> minAreaRect ->
                                  unclip -> map back to ORIGINAL pixels
        -> reading-order sort     top-to-bottom then left-to-right (app business)
        -> for each box:          <-- stage 2 is a PLAIN `for`, spec §3
             perspective_crop     warp the quad out of the FULL-RES frame
             fit_rec_input        resize/pad -> 48x320
             self.models.rec      SVTR-LCNet, (1,40,6625) CTC logits
             ctc.decode           greedy CTC -> string + confidence
        -> self.emit()            one `text` event per box + results[]

`model_frame` stays "cpu" ON PURPOSE. Stage 2 crops SOURCE-RESOLUTION pixels out
of `frame.data`, so "hw-direct" -- which letterboxes into `frame.data` itself --
would silently feed the recognizer a 480x480 model image instead of the camera
frame. A 2026-08-14 device A/B also measured "hw" at +0.8% (noise) because it
still pays the full-res convert plus an extra RGA resize. See
docs/guide/hw-preprocess.md before touching this.

Both models are declared in the manifest `models[]` and preloaded by the kit
(`self.models.det` / `self.models.rec`), so the hand-written "scan the manifest
for role==stage2_rec" loop is gone. The character dictionary is not a model --
the app still loads it, from the path the manifest hangs off the rec model.

All six knobs (det_thresh / box_thresh / unclip_ratio / min_size / max_boxes /
min_rec_conf) are auto-bound from the manifest config_schema and re-bound on
SIGHUP (every one is apply:"live"), so there is no setup() param-copying and no
on_config_reload: they are plain values read per frame.

Normalization is baked into both rknns (det: ImageNet, rec: [-1,1]); we feed raw
uint8 RGB.

Run on device (inference requires root):
    python3 -m kit.run /userdata/local/apps/ppocr-reader \
        --model models/ppocr_det_fp16.rknn --sink ws --port 8124
"""
import os

from kit.app import App, run_app
from kit import config as _cfg
from kit import pipeline
from kit import events as E
from kit.runtime.postprocess import db_ocr
from kit.runtime.postprocess import ctc

# App-local, NOT kit: an app package ships without the kit runtime, so anything
# the App Center install needs must travel with the app. See striptext.py.
import striptext

REC_H, REC_W = 48, 320          # rec model input (manifest models[1].input)


def _quad_bbox(quad):
    xs = [p[0] for p in quad]
    ys = [p[1] for p in quad]
    return [min(xs), min(ys), max(xs), max(ys)]


class PpocrReaderApp(App):
    id = "ppocr-reader"
    name = "PP-OCR Reader"
    owns_loop = True          # explicit new shape: run() drives self.frames()
    # Stage 2 crops original-resolution pixels out of frame.data -- see the
    # module docstring for why this must not become "hw"/"hw-direct".
    model_frame = "cpu"
    input_size = 480          # fallback only; manifest models[0].input wins

    # Fallbacks for the auto-bound config_schema keys (used when a key is
    # missing from the effective config; the manifest supplies each default).
    det_thresh = 0.3
    box_thresh = 0.5
    unclip_ratio = 2.0
    # Short side of the raw (pre-unclip, shrink-trained) DB blob, in detector
    # space. See db_ocr.DEFAULT_MIN_SIZE -- at 8.0 this dropped every line.
    min_size = 3.0
    max_boxes = 8
    min_rec_conf = 0.25

    def setup(self, config):
        """Load the character dictionary -- the one asset the kit cannot preload.

        Runs AFTER the config_schema auto-bind, so every `self.<knob>` below is
        already populated. Both RKNNs are preloaded by the kit from the manifest
        `models[]`; only the CTC keys file is left, and its path is declared on
        the rec model entry (`"dict": "models/ppocr_keys_v1.txt"`).
        """
        super().setup(config)

        dict_rel = "models/ppocr_keys_v1.txt"
        for m in (self._manifest.get("models") or []):
            if m.get("dict"):
                dict_rel = m["dict"]
        app_dir = _cfg.app_dir_of(self)
        dict_path = (dict_rel if os.path.isabs(dict_rel)
                     else os.path.join(app_dir, dict_rel))
        self.dictionary = ctc.load_dictionary(dict_path)

        print(f"[ppocr-reader] setup det_thresh={self.det_thresh} "
              f"box_thresh={self.box_thresh} unclip={self.unclip_ratio} "
              f"min_size={self.min_size} "
              f"max_boxes={self.max_boxes} min_rec_conf={self.min_rec_conf} "
              f"dict_classes={len(self.dictionary)} "
              f"dict={os.path.basename(dict_path)}", flush=True)

    def _read_strip(self, crop):
        """Recognize one upright text strip, windowing it if it is too wide.

        The rec rknn is fixed at 48x320, so a strip wider than 320/48 = 6.67:1
        gets squashed and CTC's 40 steps cap it at ~20 characters. Anything that
        long is read as overlapping windows and merged by character position --
        see kit.pipeline.merge_windows. A strip that already fits takes the
        single-inference path, so short lines cost exactly what they did before.
        """
        ch, cw = crop.shape[:2]
        wins = striptext.split_windows(cw, ch)
        if len(wins) == 1:
            fit = pipeline.fit_rec_input(crop, out_h=REC_H, out_w=REC_W)
            return ctc.decode(self.models.rec.infer(fit), self.dictionary)

        per_win = []
        for x0, x1 in wins:
            fit = pipeline.fit_rec_input(crop[:, x0:x1], out_h=REC_H, out_w=REC_W)
            chars, steps = striptext.decode_chars(self.models.rec.infer(fit),
                                                  self.dictionary)
            per_win.append(striptext.place_chars(chars, steps, x0, x1, ch,
                                                 out_h=REC_H, out_w=REC_W))
        return striptext.merge_windows(wins, per_win, cw)

    def run(self):
        for frame in self.frames():
            # -- 1. pre / infer / stage-1 post --------------------------- #
            x = self.pre(frame)                       # letterbox to 480
            outs = self.models.det.infer(x.data)
            boxes = db_ocr.decode(outs, x.info,
                                  det_thresh=self.det_thresh,
                                  box_thresh=self.box_thresh,
                                  unclip_ratio=self.unclip_ratio,
                                  min_size=self.min_size,
                                  max_boxes=self.max_boxes)

            # ★business★ reading order: top-to-bottom, then left-to-right,
            # with a 20 px row tolerance so a slightly tilted line stays one row.
            def _key(b):
                q = b["quad"]
                return (round(min(p[1] for p in q) / 20.0),
                        min(p[0] for p in q))
            boxes.sort(key=_key)

            results = [{
                "kind": "text",
                "quad": b["quad"],
                "box": _quad_bbox(b["quad"]),
                "score": float(b["score"]),
                "cls_name": "text",
                "text": "",
                "rec_conf": 0.0,
            } for b in boxes]

            # -- 2. stage 2: crop + recognize, one box at a time ---------- #
            # A plain Python loop, not a declared pipeline stage. `frame.data`
            # is the ORIGINAL camera frame (model_frame="cpu"), which is what
            # perspective_crop must warp from.
            events = []
            for r in results:
                crop = pipeline.perspective_crop(frame.data, r["quad"])
                text, conf = self._read_strip(crop)
                # ★business★ a reading below the confidence floor is reported
                # as an empty string -- the box still ships, carrying its raw
                # (unclamped) recognition confidence.
                if not (text and conf >= self.min_rec_conf):
                    text = ""
                ev = E.text(r, text=text, rec_conf=conf)
                r["text"], r["rec_conf"] = ev["text"], ev["rec_conf"]
                events.append(ev)

            read = [e["text"] for e in events if e["text"]]
            if read:
                print(f"[ppocr-reader] boxes={len(results)} "
                      f"read={' | '.join(read)}", flush=True)

            self.emit(events, frame.pts, results=results)


if __name__ == "__main__":
    run_app(PpocrReaderApp())

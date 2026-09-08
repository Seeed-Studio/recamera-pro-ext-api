"""
striptext.py -- read a text strip that is too wide for the rec model.

This lives in the APP, not the kit, on purpose. The fix has to reach users
through the App Center, and an app package carries the app's own files but NOT
the shared kit runtime (docs/guide/kit-design.md 0.6) -- so anything the app
needs from a kit newer than the device's would either be refused by the
`manifest.kit` gate or crash at the first call. Everything here is
ppocr-specific anyway: it exists solely because the rec rknn has a fixed
48x320 input.

The other half of the OCR fix needs nothing from here: app.py passes
`min_size` to `db_ocr.decode` explicitly, and that keyword has existed since
before the bug, so it works on any kit.
"""
from __future__ import annotations

import math
from typing import List, Optional, Tuple

import numpy as np

BLANK_INDEX = 0

def decode_chars(outputs, dictionary: List[str]) -> Tuple[List[tuple], int]:
    """Greedy CTC decode keeping each character's TIME STEP.

    Returns ([(char, t, conf), ...], T). `t` is the step that emitted the
    character, which is a position: step t covers model-input x in
    [t/T, (t+1)/T) of the crop's width. `place_chars` below uses that to place the
    characters of several overlapping windows on one axis and merge them, which
    is the only way to stitch a split-up long line without string heuristics --
    those align on the wrong repetition when the text is periodic.

    Same collapse rule as `kit.runtime.postprocess.ctc.decode`: skip blanks and
    repeats of the previous step.
    """
    o = outputs[0] if isinstance(outputs, (list, tuple)) else outputs
    seq = np.squeeze(np.asarray(o, dtype=np.float32))
    if seq.ndim == 1:
        seq = seq.reshape(1, -1)
    if seq.size == 0:
        return [], 0
    idx = np.argmax(seq, axis=1)
    best = seq[np.arange(seq.shape[0]), idx]
    out: List[tuple] = []
    prev = BLANK_INDEX
    dict_len = len(dictionary)
    for t in range(idx.shape[0]):
        c = int(idx[t])
        if c != BLANK_INDEX and c != prev and 0 <= c < dict_len:
            ch = dictionary[c]
            if ch != "":
                out.append((ch, t, float(best[t])))
        prev = c
    return out, int(seq.shape[0])



# -- long text strips ------------------------------------------------------- #
# The rec rknn has a FIXED 48x320 input, so `fit_rec_input` squashes anything
# wider than 320/48 = 6.67:1 and CTC only has T=40 steps (~20 characters).
# Upstream PaddleOCR widens imgW per batch; a fixed-shape rknn cannot. Measured
# on device (2026-09-04): a 20:1 English line read at 7.1% character similarity,
# a 15:1 digit run at 4.9%. Recognizing overlapping windows and merging them by
# position takes the same chart to 100%.
REC_MAX_ASPECT = 320.0 / 48.0
REC_WINDOW_OVERLAP = 0.20
# CTC steps the rec model emits for a full-width crop (out_w / 8 for the
# PP-OCRv3 rec backbone). One step is the finest position it can resolve,
# which is the pitch fallback when a window holds too few characters to
# measure one.
REC_TIME_STEPS = 40
# How close two reads must be, as a fraction of the character pitch, to count
# as the same glyph seen by both windows. Swept on device against a chart of
# genuine double letters (BOOKKEEPER / COFFEE / BALLOON / SUCCESS / ADDRESS):
#   0.40 -> 97.0%   0.50 -> 97.6%   0.60 -> 98.0%   0.70 -> 97.8%   0.80 -> 97.3%
# Below 0.6 seam duplicates survive ("CCOFFEE"); above it real characters start
# being eaten ("FOX" -> "FO" at 0.70, digits dropping at 0.80).
REC_DEDUPE_PITCH_FRAC = 0.60


def split_windows(crop_w: int, crop_h: int,
                  max_aspect: float = REC_MAX_ASPECT,
                  overlap: float = REC_WINDOW_OVERLAP) -> List[Tuple[int, int]]:
    """Cover `crop_w` with windows no wider than `max_aspect * crop_h`.

    Returns [(x0, x1), ...]; a strip that already fits gives a single window, so
    callers can use one code path and short lines pay nothing.
    """
    win = max_aspect * crop_h
    if crop_w <= win:
        return [(0, int(crop_w))]
    step = win * (1.0 - overlap)
    n = int(math.ceil((crop_w - win) / step)) + 1
    step = (crop_w - win) / (n - 1)
    return [(int(round(i * step)), int(round(i * step + win))) for i in range(n)]


def place_chars(chars, num_steps: int, x0: int, x1: int, crop_h: int,
                out_h: int = 48, out_w: int = 320) -> List[tuple]:
    """Map `ctc.decode_chars` output to absolute x in the strip.

    `fit_rec_input` scales the window to `out_h` tall keeping aspect, so its
    content occupies new_w of the out_w-wide input and the rest is right
    padding. Step t sits at model x = (t + 0.5) * out_w / T; anything past new_w
    is inside the padding and is dropped.
    """
    if not chars or num_steps <= 0:
        return []
    w = x1 - x0
    new_w = min(float(out_w), max(1.0, w * (out_h / float(crop_h))))
    out = []
    for ch, t, conf in chars:
        mx = (t + 0.5) * out_w / float(num_steps)
        if mx > new_w:
            continue
        out.append((x0 + mx / new_w * w, ch, conf))
    return out


def _char_pitch(placed) -> Optional[float]:
    """Median spacing between neighbouring characters of one window."""
    if len(placed) < 2:
        return None
    xs = sorted(p[0] for p in placed)
    d = [b - a for a, b in zip(xs, xs[1:])]
    return float(np.median(d)) if d else None


def merge_windows(windows, per_window, crop_w: int) -> Tuple[str, float]:
    """Merge the per-window placed characters into one reading.

    Union every window's characters on one x axis, then collapse the ones that
    describe the same glyph. Two windows overlap by design, so a glyph in the
    shared span is read twice; two reads are the same glyph when they sit closer
    than REC_DEDUPE_PITCH_FRAC of a character pitch, and the more confident read
    wins.

    Position, not string similarity. Aligning neighbouring reads by longest
    common substring lands on the wrong repetition when the text is periodic --
    measured on device, "0123456789"x4 came back as x2 and "超长中文行"x7 as x2.

    Nor does each window own a slice of the strip: that drops a glyph sitting in
    the overlap which only ONE window managed to read, and the window that
    missed it is typically the one whose edge cut through it.

    The pitch comes from each window's own characters. `REC_TIME_STEPS` is the
    floor for a window too sparse to measure -- below one CTC step the model
    cannot separate two positions anyway.
    """
    pitches = []
    for (x0, x1), placed in zip(windows, per_window):
        pitches.append(_char_pitch(placed) or (x1 - x0) / float(REC_TIME_STEPS))

    tagged = []
    for i, placed in enumerate(per_window):
        for x, ch, conf in placed:
            tagged.append((x, ch, conf, i))
    tagged.sort(key=lambda p: p[0])

    kept: List[tuple] = []
    for x, ch, conf, wi in tagged:
        if kept:
            px, pch, pconf, pwi = kept[-1]
            if wi != pwi and (x - px) < (REC_DEDUPE_PITCH_FRAC
                                        * min(pitches[wi], pitches[pwi])):
                if conf > pconf:                      # same glyph, better read
                    kept[-1] = (x, ch, conf, wi)
                continue
        kept.append((x, ch, conf, wi))

    text = "".join(c for _, c, _, _ in kept)
    conf = float(np.mean([c for _, _, c, _ in kept])) if kept else 0.0
    return text, conf

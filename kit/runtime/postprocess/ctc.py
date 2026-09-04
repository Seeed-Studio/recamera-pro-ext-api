"""
CTC greedy decoder for PP-OCR text recognition (reCamera Pro). Pure numpy.

Port of the first-gen C++ `TextRecognizer::ctcDecode` / `loadDictionary`
(sscma-example-sg200x/solutions/ppocr-reader/main/text_recognizer.cpp).

The rec rknn takes a 48x320 uint8 RGB text crop ([-1,1] normalization baked in)
and emits a (1, T, C) logit sequence (T=40, C=6625 for the PP-OCRv3 Chinese
model). The class layout is CTC-style:

    index 0            = CTC blank
    index 1 .. N       = characters from ppocr_keys_v1.txt (N = 6623)
    index N+1          = space   (PP-OCR use_space_char=True)

Greedy decode: per time-step argmax, then collapse repeats and drop blanks.
"""
from __future__ import annotations

from typing import List, Tuple

import numpy as np

BLANK_INDEX = 0


def load_dictionary(dict_path: str) -> List[str]:
    """Load a PP-OCR keys file into the full CTC class list.

    Returns [blank(''), char_1, ..., char_N, ' '(space)] so that the returned
    list length equals the model's number of output classes (6625 for the ch
    PP-OCRv3 rec model). Only \\r/\\n are stripped per line (spaces preserved),
    matching PaddleOCR.
    """
    chars: List[str] = [""]                       # index 0: CTC blank
    with open(dict_path, "r", encoding="utf-8") as f:
        for line in f:
            chars.append(line.rstrip("\r\n"))     # indices 1..N
    chars.append(" ")                             # index N+1: space
    return chars


def _as_seq(outputs) -> np.ndarray:
    """Extract the (T, C) logit matrix from raw rknn outputs."""
    o = outputs[0] if isinstance(outputs, (list, tuple)) else outputs
    a = np.asarray(o, dtype=np.float32)
    a = np.squeeze(a)                             # (1,T,C)/(1,T,C,1) -> (T,C)
    if a.ndim == 1:
        a = a.reshape(1, -1)
    if a.ndim != 2:
        a = a.reshape(-1, a.shape[-1])
    return a


def decode(outputs, dictionary: List[str]) -> Tuple[str, float]:
    """CTC greedy decode -> (text, mean confidence of emitted chars).

    outputs    : raw rknn outputs (list); outputs[0] is (1,T,C).
    dictionary : list from load_dictionary (index -> character).

    The PP-OCRv3 rec model ends in a softmax, so the raw output values are
    already per-class probabilities. Confidence is therefore the mean of the
    raw output value at each emitted (non-blank, non-repeat) time-step -- the
    same quantity the first-gen C++ ctcDecode averaged (best_val). We do NOT
    re-softmax (that would flatten a peaked 6625-way distribution to ~1/6625).
    """
    seq = _as_seq(outputs)                        # (T, C)
    if seq.size == 0:
        return "", 0.0
    idx = np.argmax(seq, axis=1)                  # (T,) best class per step
    best = seq[np.arange(seq.shape[0]), idx]      # (T,) value at argmax

    dict_len = len(dictionary)
    chars: List[str] = []
    confs: List[float] = []
    prev = BLANK_INDEX
    for t in range(idx.shape[0]):
        c = int(idx[t])
        if c != BLANK_INDEX and c != prev and 0 <= c < dict_len:
            ch = dictionary[c]
            if ch != "":
                chars.append(ch)
                confs.append(float(best[t]))
        prev = c

    text = "".join(chars)
    conf = float(np.mean(confs)) if confs else 0.0
    return text, conf

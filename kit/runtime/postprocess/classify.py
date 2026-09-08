"""
Generic classification post-processing for reCamera Pro. Pure numpy.

Second-stage classifiers in the cascade family (face-analysis: FairFace
age/gender/race + emotion) emit a flat logit vector per face ROI. This module
turns raw RKNN outputs into calibrated class picks:

    * softmax / argmax / topk               -- the numeric primitives
    * classify_head(logits, labels)         -- one softmax head -> pick + probs
    * split_heads(vec, segments)            -- slice a multi-head vector, one
                                               softmax+argmax per contiguous head
    * fairface_decode(outputs)              -- (1,18) -> race[0:7] / gender[7:9]
                                               / age[9:18], each its own head
    * emotion_decode(outputs)               -- (1,8) single 8-class head

The FairFace layout and label order are the ground truth from the model
conversion script (models/convert/fix_face_cls.py) and match the first-gen C++
runner (age_gender_race_runner.cpp): a single 18-vector split race(7) +
gender(2) + age(9). Normalization (ImageNet mean/std) is baked into the rknn,
so the caller feeds a raw uint8 224x224 RGB ROI to the engine and hands the raw
outputs here.
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

import numpy as np

# ---- canonical FairFace / emotion label vocabularies ---------------------- #
# order = model output order (see models/convert/fix_face_cls.py)
RACE_LABELS = ["White", "Black", "Latino_Hispanic", "East Asian",
               "Southeast Asian", "Indian", "Middle Eastern"]
GENDER_LABELS = ["Male", "Female"]                       # FairFace: 0=Male, 1=Female
AGE_LABELS = ["0-2", "3-9", "10-19", "20-29", "30-39",
              "40-49", "50-59", "60-69", "70+"]
# HSEmotion enet_b0_8 / AffectNet 8-class
EMOTION_LABELS = ["Anger", "Contempt", "Disgust", "Fear",
                  "Happiness", "Neutral", "Sadness", "Surprise"]

# FairFace single-vector head layout: (name, start, length)
FAIRFACE_SEGMENTS = [("race", 0, 7), ("gender", 7, 2), ("age", 9, 9)]


# ---- numeric primitives --------------------------------------------------- #
def softmax(logits: Sequence[float], temperature: float = 1.0) -> np.ndarray:
    """Numerically-stable 1-D softmax, optionally temperature-scaled.

    `temperature` divides the logits before exponentiating: >1 softens the
    distribution, <1 sharpens it, and 1.0 (the default) is the plain softmax.
    The calibration POLICY -- which temperature each head gets -- lives in
    `kit.logic.attributes.AttributeConfig`; this layer only does the arithmetic.
    """
    a = np.asarray(logits, dtype=np.float32).reshape(-1)
    if a.size == 0:
        return a
    t = max(1e-3, float(temperature))
    a = (a - float(np.max(a))) / t
    e = np.exp(a)
    s = float(np.sum(e))
    return e / s if s > 0.0 else np.full_like(e, 1.0 / e.size)


def argmax(logits: Sequence[float]) -> int:
    a = np.asarray(logits).reshape(-1)
    return int(np.argmax(a)) if a.size else -1


def topk(logits: Sequence[float], k: int = 3,
         labels: Optional[Sequence[str]] = None) -> List[Tuple]:
    """Return the top-k (label_or_index, probability) pairs, prob-descending."""
    probs = softmax(logits)
    if probs.size == 0:
        return []
    k = max(1, min(int(k), probs.size))
    order = np.argsort(probs)[::-1][:k]
    out = []
    for i in order:
        i = int(i)
        name = labels[i] if labels is not None and i < len(labels) else i
        out.append((name, round(float(probs[i]), 4)))
    return out


def classify_head(logits: Sequence[float],
                  labels: Optional[Sequence[str]] = None,
                  temperature: float = 1.0) -> dict:
    """Softmax + argmax one head. Returns index / label / confidence / probs.

    `temperature` divides the logits before the softmax (1.0 = plain softmax,
    the historical behaviour). It exists because a ResNet classification head is
    systematically overconfident, so a deployment that thresholds `confidence`
    needs the correction fitted on a held-out set; see `kit.logic.attributes`,
    which owns the per-head calibration policy.
    """
    probs = softmax(logits, temperature)
    if probs.size == 0:
        return {"index": -1, "label": None, "confidence": 0.0, "probs": []}
    idx = int(np.argmax(probs))
    label = labels[idx] if labels is not None and idx < len(labels) else idx
    return {
        "index": idx,
        "label": label,
        "confidence": round(float(probs[idx]), 4),
        "probs": [round(float(p), 4) for p in probs],
    }


def split_heads(vec: Sequence[float],
                segments: Sequence[Tuple[str, int, int]],
                labels_by_head: Optional[dict] = None,
                temperature: Optional[dict] = None) -> dict:
    """Slice a flat logit vector into contiguous heads and classify each.

    segments : [(name, start, length), ...]
    labels_by_head : optional {name: [labels]} to attach string labels.
    temperature : optional {name: float} per-head softmax temperature; a head
                  absent from the dict keeps 1.0 (plain softmax).
    Returns {name: classify_head(...)}.
    """
    v = np.asarray(vec, dtype=np.float32).reshape(-1)
    out = {}
    for name, start, length in segments:
        seg = v[start:start + length]
        labels = (labels_by_head or {}).get(name)
        t = float((temperature or {}).get(name, 1.0))
        out[name] = classify_head(seg, labels, temperature=t)
    return out


def logits_from(outputs, size: Optional[int] = None) -> np.ndarray:
    """Extract the classifier logit vector from a list of raw RKNN outputs.

    If `size` is given, prefer the tensor whose element count matches it;
    otherwise fall back to the largest tensor (single-output models).
    """
    tensors = [np.asarray(o).reshape(-1) for o in outputs]
    if not tensors:
        return np.asarray([], dtype=np.float32)
    if size is not None:
        for t in tensors:
            if t.size == size:
                return t.astype(np.float32)
    return max(tensors, key=lambda t: t.size).astype(np.float32)


# ---- task decoders (thin, label-aware wrappers) --------------------------- #
def fairface_decode(outputs, temperature: Optional[dict] = None) -> dict:
    """(1,18) FairFace head -> {race,gender,age} each a classify_head dict.

    `temperature` is an optional {"race"/"gender"/"age": float} calibration map;
    omitted or 1.0 reproduces the plain softmax exactly.
    """
    vec = logits_from(outputs, size=18)
    heads = split_heads(vec, FAIRFACE_SEGMENTS, labels_by_head={
        "race": RACE_LABELS, "gender": GENDER_LABELS, "age": AGE_LABELS,
    }, temperature=temperature)
    return heads


def emotion_decode(outputs, temperature: float = 1.0) -> dict:
    """(1,8) emotion head -> single classify_head dict."""
    vec = logits_from(outputs, size=len(EMOTION_LABELS))
    return classify_head(vec, EMOTION_LABELS, temperature=temperature)

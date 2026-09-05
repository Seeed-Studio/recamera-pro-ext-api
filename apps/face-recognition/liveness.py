"""MiniFASNet 2.7_80x80 input preparation.

Port of face_rec_api ``src/liveness.py`` :54-136. The crop geometry is the
Minivision ``CropImage._get_new_box`` behaviour: expand the detection box around
its centre by 2.7x, clamp the effective scale so the expansion fits the image,
and SHIFT a border-crossing box back inside rather than truncating it (which
would change the framing the model was trained on).

The model takes BGR and no normalization at all — kit hands the app RGB, so the
caller must flip channels (``crop[..., ::-1]``) before ``infer``.
"""
from __future__ import annotations

from typing import Tuple

import cv2
import numpy as np

LIVENESS_INPUT_SIZE = 80
LIVENESS_CROP_SCALE = 2.7
# Index of the "real" class in the softmax output (same for 2- and 3-class).
LIVENESS_REAL_INDEX = 1


def get_expanded_box(
    src_w: int,
    src_h: int,
    bbox_xywh: Tuple[float, float, float, float],
    scale: float = LIVENESS_CROP_SCALE,
) -> Tuple[int, int, int, int]:
    """Expand a detection bbox around its centre by `scale`.

    Returns inclusive corners ``(left, top, right, bottom)``, guaranteed inside
    ``[0, src_w-1] x [0, src_h-1]``.
    """
    x, y, box_w, box_h = bbox_xywh
    if box_w <= 0 or box_h <= 0:
        raise ValueError(f"Invalid bbox for liveness crop: w={box_w}, h={box_h}")

    scale = min((src_h - 1) / box_h, (src_w - 1) / box_w, scale)

    new_w = box_w * scale
    new_h = box_h * scale
    center_x = x + box_w / 2
    center_y = y + box_h / 2

    left = center_x - new_w / 2
    top = center_y - new_h / 2
    right = center_x + new_w / 2
    bottom = center_y + new_h / 2

    # Shift back inside the image instead of truncating (official behavior).
    if left < 0:
        right -= left
        left = 0
    if top < 0:
        bottom -= top
        top = 0
    if right > src_w - 1:
        left -= right - (src_w - 1)
        right = src_w - 1
    if bottom > src_h - 1:
        top -= bottom - (src_h - 1)
        bottom = src_h - 1

    left = max(0.0, left)
    top = max(0.0, top)
    return int(left), int(top), int(right), int(bottom)


def crop_minifas(
    frame_bgr: np.ndarray,
    bbox_xywh: Tuple[float, float, float, float],
    scale: float = LIVENESS_CROP_SCALE,
    out_size: int = LIVENESS_INPUT_SIZE,
) -> np.ndarray:
    """Crop + resize a face region into the canonical MiniFASNet input.

    `frame_bgr` is the full-resolution BGR uint8 image, `bbox_xywh` the raw
    detection box ``(x, y, w, h)``. Returns ``(out_size, out_size, 3)`` BGR
    uint8 — deliberately NOT normalized.
    """
    src_h, src_w = frame_bgr.shape[:2]
    left, top, right, bottom = get_expanded_box(src_w, src_h, bbox_xywh, scale)
    crop = frame_bgr[top:bottom + 1, left:right + 1]
    if crop.size == 0:
        raise ValueError(
            f"Empty liveness crop (bbox={bbox_xywh}, image={src_w}x{src_h})")
    return np.ascontiguousarray(cv2.resize(crop, (out_size, out_size)))


def softmax(logits: np.ndarray) -> np.ndarray:
    """Numerically-stable softmax over a 1-D logit vector."""
    v = np.asarray(logits, dtype=np.float32).reshape(-1)
    e = np.exp(v - v.max())
    return e / max(float(e.sum()), 1e-12)


def real_probability(outputs) -> float:
    """P(real) from the model's raw output list ``[(1,3) logits]``."""
    arr = outputs[0] if isinstance(outputs, (list, tuple)) else outputs
    probs = softmax(np.asarray(arr).reshape(-1))
    idx = LIVENESS_REAL_INDEX if probs.size > LIVENESS_REAL_INDEX else 0
    return float(probs[idx])

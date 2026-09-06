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

from typing import Optional, Tuple

import cv2
import numpy as np

LIVENESS_INPUT_SIZE = 80
LIVENESS_CROP_SCALE = 2.7
# MiniFASNetV1SE is trained on a WIDER 4.0x crop of the SAME box. The pair is an
# ensemble precisely because the two see different context: 2.7x sees the face,
# 4.0x sees the face plus whatever frames it — a phone bezel, a paper edge.
LIVENESS_CROP_SCALE_V1SE = 4.0
# Index of the "real" class in the softmax output (same for 2- and 3-class).
LIVENESS_REAL_INDEX = 1
N_LOGITS = 3


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


def real_probability_strict(outputs) -> float:
    """`real_probability` with the three-logit head asserted, not guessed.

    Both MiniFAS variants shipped here are 3-class (fake_2d / real / fake_3d).
    A silently-2-class or transposed export would still produce a plausible
    number through the lenient path, and an anti-spoofing head that is quietly
    reading the wrong index fails OPEN. So the ensemble path refuses instead.
    """
    arr = outputs[0] if isinstance(outputs, (list, tuple)) else outputs
    flat = np.asarray(arr).reshape(-1)
    if flat.size != N_LOGITS:
        raise ValueError(
            f"liveness head must emit exactly {N_LOGITS} logits, got {flat.size}")
    return float(softmax(flat)[LIVENESS_REAL_INDEX])


def infer_texture_ensemble(
    frame_bgr: np.ndarray,
    bbox_xywh: Tuple[float, float, float, float],
    model_v2,
    model_v1se,
    rgb_input: bool = False,
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Run the 2.7x and 4.0x MiniFAS heads once each on independent crops.

    Returns ``(P_v2_real, P_v1se_real, mean)``. The mean is the arithmetic mean
    of whichever heads are present — a missing `model_v1se` degrades to the
    single-model behaviour rather than failing, because `optional: true` models
    can legitimately be absent from an install.

    Each crop is cut fresh from `frame_bgr`: the 4.0x view is NOT a resize of
    the 2.7x one, which would hand V1SE upsampled pixels for the context it
    exists to look at.
    """
    ps: list = []
    p_v2: Optional[float] = None
    p_v1se: Optional[float] = None
    # With rgb_input=True the frame is the kit's native RGB and the channel
    # flip is applied to the 80x80 crop, not to the whole 1280x720 frame
    # (that full-frame copy alone cost ~40 ms per call on RV1126B).
    def _crop(scale):
        c = crop_minifas(frame_bgr, bbox_xywh, scale)
        return np.ascontiguousarray(c[..., ::-1]) if rgb_input else c
    if model_v2 is not None:
        p_v2 = real_probability_strict(model_v2.infer(_crop(LIVENESS_CROP_SCALE)))
        ps.append(p_v2)
    if model_v1se is not None:
        p_v1se = real_probability_strict(model_v1se.infer(_crop(LIVENESS_CROP_SCALE_V1SE)))
        ps.append(p_v1se)
    mean = (sum(ps) / len(ps)) if ps else None
    return p_v2, p_v1se, mean

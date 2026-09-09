"""
DBNet (PP-OCR text detection) post-processing for reCamera Pro.

Port of the first-gen C++ `TextDetector::postprocess` /
`unclipPolygon` / `orderBoxPointsTLTRBRBL`
(sscma-example-sg200x/solutions/ppocr-reader/main/text_detector.cpp) to Python.

The DB detector rknn takes a letterboxed uint8 RGB frame (ImageNet mean/std
baked in at convert time) and emits a single (1,1,H,W) sigmoid probability map
(H=W=480 for PP-OCRv3 det). We:

    1. threshold the map -> binary mask,
    2. findContours -> minAreaRect -> 4 corner points (map/input space),
    3. score each box by mean probability inside its contour,
    4. unclip (dilate) the quad outward (PaddleOCR db_unclip_ratio),
    5. map the quad from letterbox(480) space back to ORIGINAL-frame pixels
       using the kit LetterboxInfo,
    6. order the 4 points TL -> TR -> BR -> BL.

cv2 (findContours / minAreaRect) is used here and for the perspective crop --
it is already present on the device system python (opencv 4.6.0), so NO new
dependency is bundled. unclip uses the pure-numpy edge-offset method from the
reference (no pyclipper needed).
"""
from __future__ import annotations

from typing import List

import numpy as np


def _as_map(outputs) -> np.ndarray:
    """Extract the (H,W) float probability map from raw rknn outputs."""
    o = outputs[0] if isinstance(outputs, (list, tuple)) else outputs
    a = np.asarray(o, dtype=np.float32)
    a = np.squeeze(a)                      # (1,1,H,W)/(1,H,W,1) -> (H,W)
    if a.ndim != 2:
        # last resort: take the two largest dims
        a = a.reshape(a.shape[-2], a.shape[-1])
    return a


def _order_tl_tr_br_bl(pts: np.ndarray) -> np.ndarray:
    """Order 4 points as top-left, top-right, bottom-right, bottom-left.

    Mirrors orderBoxPointsTLTRBRBL: sort by polar angle around centroid,
    rotate so index 0 is the min(x+y) corner, then force clockwise winding.
    """
    pts = np.asarray(pts, dtype=np.float32).reshape(4, 2)
    c = pts.mean(axis=0)
    ang = np.arctan2(pts[:, 1] - c[1], pts[:, 0] - c[0])
    pts = pts[np.argsort(ang)]
    tl = int(np.argmin(pts[:, 0] + pts[:, 1]))
    pts = np.roll(pts, -tl, axis=0)
    v1 = pts[1] - pts[0]
    v2 = pts[2] - pts[1]
    if (v1[0] * v2[1] - v1[1] * v2[0]) < 0:   # counter-clockwise -> flip
        pts[[1, 3]] = pts[[3, 1]]
    return pts


def _unclip(points: np.ndarray, unclip_ratio: float) -> np.ndarray:
    """PaddleOCR-style polygon dilation (edge-offset method, pure numpy).

    Move each edge outward along its normal by distance =
    area * unclip_ratio / perimeter, then intersect adjacent offset edges to
    find the new corners. Direct port of TextDetector::unclipPolygon.
    """
    p = np.asarray(points, dtype=np.float64).reshape(4, 2)
    j = np.roll(p, -1, axis=0)             # next vertex per edge
    edge = j - p
    elen = np.hypot(edge[:, 0], edge[:, 1])
    perimeter = float(elen.sum())
    if perimeter < 1e-6:
        return p.astype(np.float32)
    signed_area = 0.5 * float(np.sum(p[:, 0] * j[:, 1] - j[:, 0] * p[:, 1]))
    area = abs(signed_area)
    distance = area * unclip_ratio / perimeter
    normal_sign = 1.0 if signed_area > 0 else -1.0

    elen_safe = np.where(elen < 1e-6, 1e-6, elen)
    nx = normal_sign * edge[:, 1] / elen_safe
    ny = normal_sign * (-edge[:, 0]) / elen_safe
    mid = (p + j) * 0.5
    off_p = np.stack([mid[:, 0] + nx * distance, mid[:, 1] + ny * distance], axis=1)

    out = np.zeros((4, 2), dtype=np.float64)
    for i in range(4):
        prev = (i + 3) % 4
        d1 = p[(prev + 1) % 4] - p[prev]          # prev edge direction
        d2 = j[i] - p[i]                          # edge i direction
        denom = d1[0] * d2[1] - d1[1] * d2[0]
        if abs(denom) < 1e-6:
            out[i] = [p[i][0] + nx[i] * distance, p[i][1] + ny[i] * distance]
        else:
            dp = off_p[i] - off_p[prev]
            t = (dp[0] * d2[1] - dp[1] * d2[0]) / denom
            out[i] = off_p[prev] + t * d1
    return out.astype(np.float32)


# Minimum short side (in DETECTOR/map space) a contour must have to survive.
#
# This is measured on the RAW DB blob, before unclip -- and DB is trained with
# shrunk labels, so that blob is only ~40% of the text's real height. On a
# 1280x720 frame letterboxed to 480 (scale 0.375) a 48px-tall line lands as a
# blob roughly 48 * 0.375 * 0.4 = 7px tall. The old default of 8.0 therefore
# discarded EVERY line: measured on device 2026-09-04 against a 6-size test
# chart, all 8 contours had a short side of 5-6px and 0 of 8 survived, 48px
# text included. Recognition was never the problem -- nothing reached it.
#
# 3.0 is PaddleOCR's own DBPostProcess default (`min_size=3`). With it the same
# chart detects 8 of 8 down to 12px text at 94.8% mean character similarity.
DEFAULT_MIN_SIZE = 3.0


def decode(outputs, info, *,
           det_thresh: float = 0.3,
           box_thresh: float = 0.5,
           unclip_ratio: float = 2.0,
           min_size: float = DEFAULT_MIN_SIZE,
           max_boxes: int = 32) -> List[dict]:
    """Decode the DB probability map into text-box quads in ORIGINAL pixels.

    outputs : raw rknn outputs (list); outputs[0] is the (1,1,H,W) prob map.
    info    : kit.runtime.preprocess.LetterboxInfo (scale/pad_w/pad_h/orig_w/h).
    Returns a list of {"quad": [[x,y]x4], "score": float}, score-descending,
    each quad ordered TL,TR,BR,BL and clipped to the original frame.
    """
    import cv2

    prob = _as_map(outputs)
    out_h, out_w = prob.shape
    binary = (prob > det_thresh).astype(np.uint8) * 255
    contours, _ = cv2.findContours(binary, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

    # letterbox(480) -> original mapping params
    scale = float(getattr(info, "scale", 1.0)) or 1.0
    pad_w = float(getattr(info, "pad_w", 0.0))
    pad_h = float(getattr(info, "pad_h", 0.0))
    orig_w = int(getattr(info, "orig_w", out_w))
    orig_h = int(getattr(info, "orig_h", out_h))
    # PP-OCR det: prob map size == detector input size, so map-space ==
    # letterbox-input space (1:1) as in the reference C++ (scale_x=scale_y=1).

    boxes: List[dict] = []
    for c in contours:
        if len(c) < 3:
            continue
        rect = cv2.minAreaRect(c)
        (rw, rh) = rect[1]
        # `max < min_size` would imply `min < min_size`, so only the short
        # side is worth testing -- this is the PaddleOCR `min_size` check.
        if min(rw, rh) < min_size:
            continue
        mask = np.zeros((out_h, out_w), dtype=np.uint8)
        cv2.fillPoly(mask, [c], 255)
        score = float(cv2.mean(prob, mask)[0])
        if score < box_thresh:
            continue

        quad = cv2.boxPoints(rect).astype(np.float32)   # map/input space (1:1)
        quad = _unclip(quad, unclip_ratio)

        # map letterbox(input) space -> original frame pixels
        quad[:, 0] = np.clip((quad[:, 0] - pad_w) / scale, 0.0, orig_w)
        quad[:, 1] = np.clip((quad[:, 1] - pad_h) / scale, 0.0, orig_h)
        quad = _order_tl_tr_br_bl(quad)
        boxes.append({"quad": quad.tolist(), "score": score})

    boxes.sort(key=lambda b: b["score"], reverse=True)
    if max_boxes and len(boxes) > max_boxes:
        boxes = boxes[:max_boxes]
    return boxes

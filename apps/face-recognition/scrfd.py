"""SCRFD-500M raw-head decode, pure numpy.

The RKNN graph emits nine 2-D tensors and the *order* is whatever the toolkit
happened to bake in, so nothing here may index by position. `map_outputs_by_shape`
recovers the (stride, kind) of every tensor from its shape alone:

    rows 12800 / 3200 / 800   -> stride 8 / 16 / 32   (640/8=80, 80*80*2 anchors)
    cols 1 / 4 / 10           -> score / bbox-delta / 5-point kps-delta

Decode geometry is a verbatim port of face_rec_api ``src/face_pipeline.py``
(anchors :45-56, NMS :58-82, decode :250-350) so an embedding enrolled through
that service and one produced here describe the same crop.
"""
from __future__ import annotations

from typing import Dict, List, Sequence, Tuple

import numpy as np

STRIDES: Tuple[int, int, int] = (8, 16, 32)
NUM_ANCHORS = 2

# columns -> what the tensor holds
_KIND_FOR_COLS = {1: "scores", 4: "bboxes", 10: "kps"}

_anchor_cache: Dict[Tuple[int, int], Dict[int, np.ndarray]] = {}


def generate_anchors(model_h: int, model_w: int) -> Dict[int, np.ndarray]:
    """SCRFD anchor centres per stride, shape ``(N, 2)`` of ``(cx, cy)``."""
    key = (int(model_h), int(model_w))
    cached = _anchor_cache.get(key)
    if cached is not None:
        return cached
    anchors: Dict[int, np.ndarray] = {}
    for stride in STRIDES:
        fh = model_h // stride
        fw = model_w // stride
        # InsightFace SCRFD convention: anchor centre = grid index * stride,
        # no half-cell offset (insightface/model_zoo/scrfd.py forward()).
        x_centers = np.arange(fw, dtype=np.float32) * stride
        y_centers = np.arange(fh, dtype=np.float32) * stride
        xv, yv = np.meshgrid(x_centers, y_centers)
        centers = np.stack([xv, yv], axis=-1).reshape(-1, 2)
        anchors[stride] = np.repeat(centers, NUM_ANCHORS, axis=0)
    _anchor_cache[key] = anchors
    return anchors


def nms(boxes: np.ndarray, scores: np.ndarray, thresh: float) -> List[int]:
    """Greedy IoU NMS; returns indices to keep, score-descending."""
    if boxes.shape[0] == 0:
        return []
    idxs = scores.argsort()[::-1]
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    area = (x2 - x1) * (y2 - y1)
    keep: List[int] = []
    while idxs.size > 0:
        i = idxs[0]
        keep.append(int(i))
        xx1 = np.maximum(x1[i], x1[idxs[1:]])
        yy1 = np.maximum(y1[i], y1[idxs[1:]])
        xx2 = np.minimum(x2[i], x2[idxs[1:]])
        yy2 = np.minimum(y2[i], y2[idxs[1:]])
        w = np.maximum(0.0, xx2 - xx1)
        h = np.maximum(0.0, yy2 - yy1)
        inter = w * h
        union = area[i] + area[idxs[1:]] - inter
        iou = inter / np.maximum(union, 1e-9)
        idxs = idxs[np.where(iou <= thresh)[0] + 1]
    return keep


def _normalize(buf: np.ndarray) -> Tuple[int, int]:
    """Return ``(rows, cols)`` for one raw output, whatever its rank."""
    a = np.asarray(buf)
    if a.ndim == 2:
        return int(a.shape[0]), int(a.shape[1])
    if a.ndim == 3:                       # (1, N, C)
        return int(a.shape[1]), int(a.shape[2])
    if a.ndim == 4:                       # (1, C, H, W) or (1, H, W, C)
        _, d1, d2, d3 = a.shape
        if int(d1) in _KIND_FOR_COLS:
            return int(d2) * int(d3) * NUM_ANCHORS, int(d1)
        if int(d3) in _KIND_FOR_COLS:
            return int(d1) * int(d2) * NUM_ANCHORS, int(d3)
    raise ValueError(f"Unrecognized SCRFD output shape {a.shape}")


def _as_rows(buf: np.ndarray, rows: int, cols: int) -> np.ndarray:
    return np.asarray(buf, dtype=np.float32).reshape(rows, cols)


def map_outputs_by_shape(
    outputs: Sequence[np.ndarray],
) -> Dict[int, Dict[str, np.ndarray]]:
    """Group the nine raw tensors into ``{stride: {"scores"|"bboxes"|"kps"}}``.

    Position-independent on purpose: the RKNN output order is not part of any
    contract, only the shapes are.
    """
    if len(outputs) != 9:
        raise ValueError(f"SCRFD expects 9 outputs, got {len(outputs)}")

    triples: List[Tuple[int, int, str, np.ndarray]] = []  # (rows, cols, kind, arr)
    for i, buf in enumerate(outputs):
        rows, cols = _normalize(buf)
        kind = _KIND_FOR_COLS.get(cols)
        if kind is None:
            raise ValueError(f"SCRFD output #{i}: unexpected column count {cols}")
        triples.append((rows, cols, kind, _as_rows(buf, rows, cols)))

    row_counts = sorted({r for r, _, _, _ in triples}, reverse=True)
    if len(row_counts) != 3:
        raise ValueError(
            f"Expected 3 distinct anchor counts from SCRFD, got {row_counts}")
    stride_for_rows = dict(zip(row_counts, STRIDES))

    by_stride: Dict[int, Dict[str, np.ndarray]] = {s: {} for s in STRIDES}
    for rows, _cols, kind, arr in triples:
        by_stride[stride_for_rows[rows]][kind] = arr

    for s in STRIDES:
        missing = {"scores", "bboxes", "kps"} - set(by_stride[s])
        if missing:
            raise ValueError(f"SCRFD stride {s} missing {sorted(missing)}")
    return by_stride


def decode(
    outputs: Sequence[np.ndarray],
    info,
    conf_thres: float = 0.5,
    iou_thres: float = 0.4,
    model_size: int = 640,
) -> List[dict]:
    """Decode raw SCRFD outputs to ORIGINAL-pixel detections.

    `info` is the kit ``LetterboxInfo`` from ``App.pre()`` (scale / pad_w /
    pad_h / orig_w / orig_h); every coordinate returned is in original camera
    pixels, clamped to the frame.

    Returns score-descending ``[{"box": [x1,y1,x2,y2], "score": float,
    "kps": [(x,y) x5]}]``.
    """
    by_stride = map_outputs_by_shape(outputs)
    anchors = generate_anchors(int(model_size), int(model_size))

    props: List[np.ndarray] = []
    for stride in STRIDES:
        blk = by_stride[stride]
        scores = blk["scores"][:, 0]
        keep_idx = np.where(scores >= conf_thres)[0]
        if keep_idx.size == 0:
            continue
        sc = scores[keep_idx]
        bd = blk["bboxes"][keep_idx]
        kd = blk["kps"][keep_idx]
        cur = anchors[stride][keep_idx]
        ax, ay = cur[:, 0], cur[:, 1]

        boxes = np.stack([ax - bd[:, 0] * stride,
                          ay - bd[:, 1] * stride,
                          ax + bd[:, 2] * stride,
                          ay + bd[:, 3] * stride], axis=-1)
        kps = np.zeros_like(kd)
        for i in range(5):
            kps[:, i * 2] = ax + kd[:, i * 2] * stride
            kps[:, i * 2 + 1] = ay + kd[:, i * 2 + 1] * stride
        props.append(np.concatenate([boxes, sc[:, None], kps], axis=1))

    if not props:
        return []
    p_all = np.concatenate(props, axis=0)
    keep = nms(p_all[:, :4], p_all[:, 4], iou_thres)
    p_all = p_all[keep]

    scale = float(getattr(info, "scale", 1.0))
    pad_w = float(getattr(info, "pad_w", 0.0))
    pad_h = float(getattr(info, "pad_h", 0.0))
    ow = float(getattr(info, "orig_w", model_size))
    oh = float(getattr(info, "orig_h", model_size))
    scale = scale if scale > 1e-9 else 1.0

    out: List[dict] = []
    for p in p_all:
        x1 = max(0.0, min(ow, (float(p[0]) - pad_w) / scale))
        y1 = max(0.0, min(oh, (float(p[1]) - pad_h) / scale))
        x2 = max(0.0, min(ow, (float(p[2]) - pad_w) / scale))
        y2 = max(0.0, min(oh, (float(p[3]) - pad_h) / scale))
        if x2 <= x1 or y2 <= y1:
            continue
        lm = [((float(p[5 + i * 2]) - pad_w) / scale,
               (float(p[5 + i * 2 + 1]) - pad_h) / scale) for i in range(5)]
        out.append({"box": [x1, y1, x2, y2], "score": float(p[4]), "kps": lm})
    out.sort(key=lambda d: -d["score"])
    return out

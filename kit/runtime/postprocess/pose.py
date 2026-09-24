"""
YOLO-pose (YOLOv8-pose / YOLO11-pose) post-processing for reCamera Pro. Pure numpy.

Mirrors the "filter first, decode second" style of detect.py. Handles the raw
multi-branch head produced by extracting the 9 leaf Conv outputs before the
in-graph concat/decode (see models/convert/export_pose.py):

    per FPN stride s in {8, 16, 32}:
        box branch  [1, 64, H, W]   -> DFL(reg_max=16) -> l,t,r,b distances
        cls branch  [1,  1, H, W]   -> person score (single class)
        kpt branch  [1, 51, H, W]   -> 17 keypoints, each (x, y, conf)

Keypoint decode (ultralytics convention, anchors = grid + 0.5, so
anchors - 0.5 is the integer grid index):
    kx = (raw_x * 2.0 + gx) * stride
    ky = (raw_y * 2.0 + gy) * stride
    kc = sigmoid(raw_conf)

Output: list of dicts
    {box:[x1,y1,x2,y2], score:float, keypoints:[[x,y,conf] * 17]}
with boxes AND keypoints mapped back to the ORIGINAL image via LetterboxInfo.
"""
from __future__ import annotations

from typing import List, Sequence

import numpy as np

from kit.runtime.postprocess.detect import _sigmoid, _softmax, nms

N_KPT = 17


def _decode_pose(outputs: Sequence[np.ndarray], conf_thres: float,
                 input_size: int = 640, reg_max: int = 16, n_kpt: int = N_KPT):
    """Decode raw pose head branches. Returns (xyxy, scores, keypoints).

    keypoints: (M, n_kpt, 3) in letterboxed 640 space (x, y, conf).
    Branches are paired per stride by channel count (64 box / n_kpt*3 kpt /
    1 cls), then sorted by feature-map size so the same stride lines up.
    """
    box_b, cls_b, kpt_b = [], [], []
    kpt_ch = n_kpt * 3
    for o in outputs:
        o = np.asarray(o)
        c = o.shape[1] if o.ndim == 4 else o.shape[-1]
        if c == 4 * reg_max:
            box_b.append(o)
        elif c == kpt_ch:
            kpt_b.append(o)
        else:                      # remaining = class branch (nc, usually 1)
            cls_b.append(o)
    box_b.sort(key=lambda t: -t.shape[-1])
    cls_b.sort(key=lambda t: -t.shape[-1])
    kpt_b.sort(key=lambda t: -t.shape[-1])

    all_xyxy, all_scores, all_kpts = [], [], []
    proj = np.arange(reg_max, dtype=np.float32)
    for bd, cl, kp in zip(box_b, cls_b, kpt_b):
        _, _, gh, gw = bd.shape
        stride = input_size // gh
        n = gh * gw

        # ---- 1. cheap person scoring + threshold ----
        cls = cl.reshape(-1, n)                 # (nc, N), nc usually 1
        best = cls.max(0)
        apply_sig = float(cls.min()) < 0.0 or float(best.max()) > 1.0
        scores = _sigmoid(best) if apply_sig else best
        keep = np.nonzero(scores >= conf_thres)[0]
        if keep.size == 0:
            continue

        gx = (keep % gw).astype(np.float32)
        gy = (keep // gw).astype(np.float32)

        # ---- 2. DFL box decode for survivors ----
        bd_sel = bd.reshape(4, reg_max, n)[:, :, keep].astype(np.float32)
        dist = (_softmax(bd_sel, axis=1) * proj[None, :, None]).sum(1)   # (4,k)
        l, t, r, b = dist[0], dist[1], dist[2], dist[3]
        x1 = (gx + 0.5 - l) * stride
        y1 = (gy + 0.5 - t) * stride
        x2 = (gx + 0.5 + r) * stride
        y2 = (gy + 0.5 + b) * stride

        # ---- 3. keypoint decode for survivors ----
        kp_sel = kp.reshape(n_kpt, 3, n)[:, :, keep].astype(np.float32)  # (17,3,k)
        kx = (kp_sel[:, 0, :] * 2.0 + gx[None, :]) * stride               # (17,k)
        ky = (kp_sel[:, 1, :] * 2.0 + gy[None, :]) * stride
        kc = _sigmoid(kp_sel[:, 2, :])
        kpts = np.stack([kx, ky, kc], axis=-1).transpose(1, 0, 2)         # (k,17,3)

        all_xyxy.append(np.stack([x1, y1, x2, y2], 1))
        all_scores.append(scores[keep])
        all_kpts.append(kpts)

    if not all_xyxy:
        return (np.zeros((0, 4)), np.zeros((0,)),
                np.zeros((0, n_kpt, 3)))
    return (np.concatenate(all_xyxy), np.concatenate(all_scores),
            np.concatenate(all_kpts))


def postprocess(outputs, info, conf_thres: float = 0.4, iou_thres: float = 0.45,
                input_size: int = 640, kpt_thres: float = 0.5):
    """
    outputs : list of raw RKNN output tensors (9 branches).
    info    : preprocess.LetterboxInfo (scale + padding for un-letterboxing).
    Returns list of person dicts sorted by score descending:
        {box:[x1,y1,x2,y2], score, keypoints:[[x,y,conf]*17]}
    Boxes and keypoints are in ORIGINAL-image pixel coordinates. Keypoints
    below `kpt_thres` keep their coordinates but the caller should treat their
    confidence as the visibility gate.
    """
    outputs = [np.asarray(o) for o in outputs]
    xyxy, scores, kpts = _decode_pose(outputs, conf_thres, input_size)

    # single-class (person) NMS
    keep = nms(xyxy, scores, iou_thres)

    sx = info.scale
    results = []
    for i in keep:
        x1, y1, x2, y2 = xyxy[i]
        bx1 = float(np.clip((x1 - info.pad_w) / sx, 0, info.orig_w))
        by1 = float(np.clip((y1 - info.pad_h) / sx, 0, info.orig_h))
        bx2 = float(np.clip((x2 - info.pad_w) / sx, 0, info.orig_w))
        by2 = float(np.clip((y2 - info.pad_h) / sx, 0, info.orig_h))

        kp_out = []
        for j in range(kpts.shape[1]):
            kx, ky, kc = kpts[i, j]
            ox = float(np.clip((kx - info.pad_w) / sx, 0, info.orig_w))
            oy = float(np.clip((ky - info.pad_h) / sx, 0, info.orig_h))
            kp_out.append([round(ox, 1), round(oy, 1), round(float(kc), 3)])

        results.append({
            "box": [round(bx1, 1), round(by1, 1), round(bx2, 1), round(by2, 1)],
            "score": round(float(scores[i]), 4),
            "keypoints": kp_out,
        })
    results.sort(key=lambda d: d["score"], reverse=True)
    return results

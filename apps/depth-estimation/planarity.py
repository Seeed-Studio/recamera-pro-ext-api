"""Plane detection over a monocular depth map — pure numpy.

Source: `apps/face-recognition/depth_liveness.py` (the liveness-facing half of
that module, `planarity_from_depth` and its helpers, verbatim). Copied rather
than imported: an app package ships its own directory tree only
(`market/packaging/build.py` packs `apps/<id>/` and nothing else), so a
cross-app import would work in the repo and fail on an installed device. The
model-handle wrapper `depth_flatness` is deliberately NOT copied — this app
already has the depth map in hand when it calls in.

A printed photo or a phone-screen replay is a PLANE: every pixel lies on one
flat surface, so the predicted relative depth over the region fits a single
plane almost perfectly. A real object leaves a residual. That is what
`planarity` measures, and it is what the `depth_roi` config exposes per ROI.
"""
from __future__ import annotations

from typing import Dict, Optional, Sequence, Tuple

import numpy as np

# Residual-to-frame-range ratio at which `planarity` reaches 0 (fully
# non-planar). Measured on MiDaS v2.1 small: a flat photo mount lands at 0.005,
# a face at 0.03-0.05. Recalibrate on device captures before using `score` as a
# hard gate.
RESIDUAL_FULL_SCALE = 0.03

# Below this many valid depth samples a plane fit is not meaningful.
MIN_SAMPLES = 32

# Fraction of the face box kept around its centre. Pulling the box in excludes
# the background behind the head, whose depth discontinuity would otherwise
# dominate the fit and make every face — live or not — look non-planar.
DEFAULT_SHRINK = 0.75

def _fit_plane_residual(patch: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Least-squares fit ``d = a*x + b*y + c`` over a 2-D patch.

    Returns ``(residual, values)``, both 1-D over the finite samples. x/y are
    normalised to [-1, 1] so conditioning does not depend on patch size.
    """
    h, w = patch.shape
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float64)
    if h > 1:
        yy = 2.0 * yy / (h - 1) - 1.0
    if w > 1:
        xx = 2.0 * xx / (w - 1) - 1.0

    d = patch.astype(np.float64)
    valid = np.isfinite(d)
    x, y, v = xx[valid], yy[valid], d[valid]
    if v.size < 3:
        return np.zeros(0, dtype=np.float64), v

    A = np.stack([x, y, np.ones_like(x)], axis=1)
    coef, *_ = np.linalg.lstsq(A, v, rcond=None)
    return v - A @ coef, v


def _box_on_depth(bbox_xywh: Sequence[float],
                  image_size: Tuple[int, int],
                  depth_shape: Tuple[int, int],
                  shrink: float) -> Tuple[int, int, int, int]:
    """Map an image-space bbox onto depth-map indices, shrunk toward its centre."""
    img_w, img_h = image_size
    dh, dw = depth_shape
    x, y, bw, bh = (float(v) for v in bbox_xywh)
    cx, cy = x + bw / 2.0, y + bh / 2.0
    bw *= shrink
    bh *= shrink

    sx, sy = dw / float(img_w), dh / float(img_h)
    left = int(np.floor((cx - bw / 2.0) * sx))
    right = int(np.ceil((cx + bw / 2.0) * sx))
    top = int(np.floor((cy - bh / 2.0) * sy))
    bottom = int(np.ceil((cy + bh / 2.0) * sy))

    left = max(0, min(left, dw - 1))
    top = max(0, min(top, dh - 1))
    right = max(left + 1, min(right, dw))
    bottom = max(top + 1, min(bottom, dh))
    return left, top, right, bottom


def planarity_from_depth(depth: np.ndarray,
                         bbox_xywh: Sequence[float],
                         image_size: Optional[Tuple[int, int]] = None,
                         shrink: float = DEFAULT_SHRINK,
                         full_scale: float = RESIDUAL_FULL_SCALE
                         ) -> Dict[str, float]:
    """Score how planar the face region of a depth map is.

    Parameters
    ----------
    depth:
        2-D relative-depth map (H, W); leading singleton axes are squeezed.
        Sign and scale are irrelevant — MiDaS emits inverse depth, Depth
        Anything emits a disparity-like value, and both work unchanged.
    bbox_xywh:
        Face box ``(x, y, w, h)`` in the coordinates of ``image_size``.
    image_size:
        ``(width, height)`` of the image the bbox was measured in. Defaults to
        the depth map's own ``(W, H)``.
    shrink:
        Fraction of the box kept around its centre.
    full_scale:
        Residual-to-frame-range ratio mapped to ``planarity == 0``.

    Returns
    -------
    ``{'planarity', 'relief', 'score', 'residual_ratio', 'n_samples', 'box'}``.
    The three scores are NaN when the box holds fewer than ``MIN_SAMPLES``
    finite depth values.
    """
    d = np.squeeze(np.asarray(depth))
    if d.ndim != 2:
        raise ValueError(f"depth must be 2-D after squeeze, got shape {d.shape}")
    if len(bbox_xywh) != 4:
        raise ValueError("bbox_xywh must be (x, y, w, h)")
    if float(bbox_xywh[2]) <= 0 or float(bbox_xywh[3]) <= 0:
        raise ValueError(f"bbox must have positive size, got {tuple(bbox_xywh)}")
    if not 0 < shrink <= 1:
        raise ValueError("shrink must be in (0, 1]")
    if full_scale <= 0:
        raise ValueError("full_scale must be positive")

    dh, dw = d.shape
    if image_size is None:
        image_size = (dw, dh)

    box = _box_on_depth(bbox_xywh, image_size, (dh, dw), shrink)
    left, top, right, bottom = box
    patch = d[top:bottom, left:right]

    n = int(np.count_nonzero(np.isfinite(patch)))
    out: Dict[str, float] = {
        "planarity": float("nan"),
        "relief": float("nan"),
        "score": float("nan"),
        "residual_ratio": float("nan"),
        "n_samples": n,
        "box": box,
    }
    if n < MIN_SAMPLES:
        return out

    residual, values = _fit_plane_residual(patch)

    # Frame dynamic range, robust to the few extreme pixels every relative-depth
    # head produces at object silhouettes. Falls back to the in-box spread when
    # the whole frame is one flat surface.
    finite = d[np.isfinite(d)]
    scale = 0.0
    if finite.size:
        scale = float(np.percentile(finite, 95) - np.percentile(finite, 5))
    if scale <= 0.0:
        scale = float(np.std(values))
    if scale <= 0.0:
        # Constant depth everywhere: planar, degenerately so.
        out.update(planarity=1.0, relief=0.0, score=0.0, residual_ratio=0.0)
        return out

    ratio = float(np.sqrt(np.mean(residual ** 2)) / scale)
    flat = float(np.clip(1.0 - ratio / full_scale, 0.0, 1.0))
    out.update(
        planarity=flat,
        relief=float((residual.max() - residual.min()) / scale),
        score=float(1.0 - flat),
        residual_ratio=ratio,
    )
    return out

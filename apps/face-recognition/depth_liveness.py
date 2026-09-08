"""Depth evidence for liveness — plane detection over a monocular depth map.

A printed photo or a phone-screen replay is a PLANE: every pixel of the
attacking face lies on one flat surface, so the predicted relative depth inside
the face box fits a single plane almost perfectly. A real face leaves a
nose-shaped residual. That is the `planarity` measurement this module is shaped
around.

Scale-free by construction. Monocular networks (MiDaS, Depth Anything) predict
relative depth / inverse depth with an arbitrary scale and offset, so every
number here is a ratio of two quantities carrying the same depth unit:

``planarity``  in [0, 1], 1.0 = perfectly flat = photo/screen.
    Derived from the RMS of the residual of a least-squares fit
    ``d ~ a*x + b*y + c`` over the face box, divided by the depth map's own
    dynamic range (p95 - p5 over the whole map), then mapped through
    ``clip(1 - ratio / RESIDUAL_FULL_SCALE, 0, 1)``.

``relief``  >= 0, unbounded.
    Peak-to-peak residual over the same box, in the same normalised units.
    Catches a single strong protrusion (a nose) that an RMS averages away.

The denominator is the *frame's* depth range rather than the in-box spread on
purpose. Normalising by the in-box spread is degenerate exactly where it matters
most: over a flat, textureless surface the residual IS the spread, so a perfect
plane scores the same as a face (measured — a grey photo mount came out at
residual/in-box-std = 0.38 on MiDaS, against 0.98 for a real face). Against the
frame range the same mount scores 0.005 versus 0.052 for the face, an order of
magnitude apart.

``score``  in [0, 1], the liveness-facing value, ``1 - planarity``.
    Higher is more live, so it mixes with texture and motion evidence without a
    sign flip.

The fit runs on the *residual*, never the raw depth, so a photo held at an angle
— a plane, but a tilted one with a large raw depth range — still scores as
planar: the tilt is absorbed by the ``a*x + b*y`` terms.

`planarity_from_depth` is pure numpy and takes a depth map that somebody else
produced, which is what the tests and the offline model comparison use.
`depth_flatness` is the app-facing wrapper: it runs the installed depth model on
a frame first. It returns None when no model is installed, which is the contract
for "no depth evidence available" — fusion renormalises its weights over the
terms that are present, so a None costs nothing and changes no verdict.
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

# Model handle installed by the app when a depth model is configured. Kept as a
# module global so the interface stays a plain function for the fusion code.
_MODEL = None


def set_model(model) -> None:
    """Install (or clear, with None) the depth model handle."""
    global _MODEL
    _MODEL = model


def available() -> bool:
    return _MODEL is not None


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


def depth_flatness(frame_rgb: np.ndarray,
                   bbox_xyxy: Sequence[float]) -> Optional[Dict[str, float]]:
    """Return ``{'planarity', 'relief', 'score', ...}`` for the face box, or None.

    Runs the installed depth model over the whole frame and scores the face box
    with `planarity_from_depth`. The model handle must expose the kit's
    ``RknnModel`` shape: ``infer(uint8 NHWC) -> [ndarray]`` with a single depth
    output, and ``input_size`` (int) or an inferrable square input.

    None means "no depth evidence" — no model installed, or a degenerate box.
    """
    if _MODEL is None:
        return None

    frame = np.asarray(frame_rgb)
    if frame.ndim != 3 or frame.shape[2] != 3:
        raise ValueError(f"frame_rgb must be HxWx3, got {frame.shape}")
    h, w = frame.shape[:2]

    x0, y0, x1, y1 = (float(v) for v in bbox_xyxy)
    bw, bh = x1 - x0, y1 - y0
    if bw <= 0 or bh <= 0:
        return None

    size = int(getattr(_MODEL, "input_size", 0) or 0)
    if size <= 0:
        describe = getattr(_MODEL, "describe", None)
        if callable(describe):
            shape = (describe() or {}).get("input_shape")
            if shape:
                size = int(shape[1])
    if size <= 0:
        raise ValueError("depth model does not expose an input size")

    # Nearest-neighbour resize: no OpenCV dependency in this module, and the
    # depth head is resilient to it. The app may pass an already-square frame.
    ys = (np.linspace(0, h - 1, size)).astype(np.int32)
    xs = (np.linspace(0, w - 1, size)).astype(np.int32)
    resized = frame[ys][:, xs]

    out = _MODEL.infer(resized[None].astype(np.uint8))
    depth = np.squeeze(np.asarray(out[0] if isinstance(out, (list, tuple)) else out))
    return planarity_from_depth(depth, (x0, y0, bw, bh), image_size=(w, h))

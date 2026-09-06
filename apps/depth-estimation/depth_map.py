"""Pure-numpy reductions over one monocular depth map.

Everything here is scale-free. MiDaS v2.1 small predicts *relative inverse
depth*: the value carries no unit, and **larger = nearer** (the opposite sign
of the FastDepth map the SG2002 solution reduces, which is why the payload
carries `smaller_is_nearer: false` explicitly rather than letting a consumer
guess). So the only numbers published are either raw model units (the `depth`
block, for a consumer that wants to do its own thing) or *proximity*, a
p5..p95-stabilised remap of the raw value into [0, 1] where 1 = nearest content
in frame.

Percentile stabilisation, not min/max: one hot pixel at either end would
otherwise rescale the whole frame and make the grid jump between two
consecutive frames that look identical (same reasoning as `depth_payload.cpp`
in the SG2002 solution, which uses p02/p98 over a histogram).

No OpenCV, no PIL: the app runs the model on the RGA letterbox the frame source
already produced, so nothing here needs to resize an image, and the debug map
is encoded by the minimal zlib PNG writer below.
"""
from __future__ import annotations

import base64
import struct
import zlib
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

# Proximity cuts for the coarse near/mid/far label attached to each grid cell.
NEAR_CUT = 0.66
MID_CUT = 0.33

# Debug map geometry (config `publish_map`). 64x48 is ~3 KB of PNG, small
# enough to ride on every frame of a 8124 WebSocket without starving the
# overlay, and still readable as a picture.
MAP_W, MAP_H = 64, 48


def valid_region(depth_shape: Tuple[int, int], info) -> Tuple[int, int, int, int]:
    """The letterbox's CONTENT rectangle inside the depth map, as (l, t, r, b).

    `info` is the kit `LetterboxInfo` that produced the model input, so the grey
    bars the letterbox padded on are excluded: they are not scene, and their
    (constant) predicted depth would otherwise drag every statistic.
    """
    dh, dw = depth_shape
    scale = float(getattr(info, "scale", 0.0) or 0.0)
    pw = int(round(float(getattr(info, "pad_w", 0.0) or 0.0)))
    ph = int(round(float(getattr(info, "pad_h", 0.0) or 0.0)))
    ow = int(getattr(info, "orig_w", 0) or 0)
    oh = int(getattr(info, "orig_h", 0) or 0)
    if scale <= 0 or ow <= 0 or oh <= 0:
        return 0, 0, dw, dh
    cw = int(round(ow * scale))
    ch = int(round(oh * scale))
    left = max(0, min(pw, dw - 1))
    top = max(0, min(ph, dh - 1))
    right = max(left + 1, min(left + cw, dw))
    bottom = max(top + 1, min(top + ch, dh))
    return left, top, right, bottom


def to_original(x: float, y: float, info) -> Tuple[float, float]:
    """Map a depth-map pixel back to ORIGINAL camera pixels."""
    scale = float(getattr(info, "scale", 0.0) or 0.0)
    if scale <= 0:
        return float(x), float(y)
    pw = float(getattr(info, "pad_w", 0.0) or 0.0)
    ph = float(getattr(info, "pad_h", 0.0) or 0.0)
    return (x - pw) / scale, (y - ph) / scale


def percentile(values: np.ndarray, q: float) -> float:
    """Nearest-rank percentile of a 1-D array, via `np.partition`.

    ★Why not np.percentile★ measured on the device (RV1126B, 256x144 map,
    apps/depth-estimation/tests/_bench.py): `np.percentile` costs ~4.5 ms per
    call from its own Python-side validation and interpolation, so the twelve
    per-zone calls alone were 54.6 ms/frame -- nearly as much as the 64 ms NPU
    inference they were meant to summarise. `np.partition` gives the same
    nearest-rank answer for ~0.2 ms. The interpolation `np.percentile` adds
    between the two straddling samples is below the noise of a relative depth
    map that is itself quantised by the network.
    """
    v = np.asarray(values).ravel()
    if v.size == 0:
        return 0.0
    k = int(np.clip(q, 0.0, 100.0) / 100.0 * (v.size - 1) + 0.5)
    k = max(0, min(k, v.size - 1))
    return float(np.partition(v, k)[k])


def frame_stats(values: np.ndarray) -> Dict[str, float]:
    """min / max / mean / p5 / p95 of the valid depth samples, raw model units."""
    v = np.asarray(values, dtype=np.float32).ravel()
    if not np.isfinite(v).all():
        v = v[np.isfinite(v)]
    if v.size == 0:
        return {"min": 0.0, "max": 0.0, "mean": 0.0, "p5": 0.0, "p95": 0.0}
    return {
        "min": float(v.min()),
        "max": float(v.max()),
        "mean": float(v.mean()),
        "p5": percentile(v, 5.0),
        "p95": percentile(v, 95.0),
    }


def proximity(depth: np.ndarray, p5: float, p95: float) -> np.ndarray:
    """Remap raw inverse depth into [0, 1], 1 = nearest.

    A flat map (p95 == p5) has no near and no far, so everything reads 0 -- the
    same degenerate answer the SG2002 `proximity_of` gives for a zero span.
    """
    span = float(p95) - float(p5)
    d = np.asarray(depth, dtype=np.float32)
    if not span > 0.0:
        return np.zeros(d.shape, dtype=np.float32)
    return np.clip((d - float(p5)) / span, 0.0, 1.0)


def grid_cells(prox: np.ndarray, rows: int, cols: int,
               near_percentile: float = 95.0) -> List[List[Dict[str, float]]]:
    """Split `prox` into rows x cols cells and reduce each one.

    Per cell: `mean` (the published grid value) and `near` (the
    `near_percentile`-th percentile, i.e. how near the cell's NEAREST content
    is -- what picks the winner for `nearest`). Cut points use integer division
    of the extent, so no row or column is dropped or double-counted.
    """
    h, w = prox.shape[:2]
    rows = max(1, int(rows))
    cols = max(1, int(cols))
    q = float(np.clip(near_percentile, 0.0, 100.0))
    out: List[List[Dict[str, float]]] = []
    for r in range(rows):
        y0, y1 = h * r // rows, h * (r + 1) // rows
        y1 = max(y1, y0 + 1)
        row: List[Dict[str, float]] = []
        for c in range(cols):
            x0, x1 = w * c // cols, w * (c + 1) // cols
            x1 = max(x1, x0 + 1)
            cell = prox[y0:y1, x0:x1]
            row.append({
                "mean": float(cell.mean()),
                "near": percentile(cell, q),
                "x0": x0, "y0": y0, "x1": x1, "y1": y1,
            })
        out.append(row)
    return out


def label_of(value: float) -> str:
    """Coarse near/mid/far bucket for a proximity value."""
    if value >= NEAR_CUT:
        return "near"
    if value >= MID_CUT:
        return "mid"
    return "far"


def parse_rois(spec) -> List[List[float]]:
    """Normalise a `depth_roi` config value into a list of [x, y, w, h] in 0..1.

    Accepts the already-parsed list form and the JSON-string form the config UI
    stores for a `string` control. Anything malformed yields [] -- a bad ROI
    must cost the frame nothing, the app keeps publishing depth.
    """
    if not spec:
        return []
    if isinstance(spec, str):
        import json
        try:
            spec = json.loads(spec)
        except ValueError:
            return []
    if not isinstance(spec, (list, tuple)):
        return []
    out: List[List[float]] = []
    for item in spec:
        if isinstance(item, dict):
            item = [item.get("x"), item.get("y"), item.get("w"), item.get("h")]
        if not isinstance(item, (list, tuple)) or len(item) != 4:
            continue
        try:
            x, y, w, h = (float(v) for v in item)
        except (TypeError, ValueError):
            continue
        if not (w > 0 and h > 0):
            continue
        out.append([x, y, w, h])
    return out


# --------------------------------------------------------------------------- #
# debug depth map (config `publish_map`)
# --------------------------------------------------------------------------- #
def downsample(prox: np.ndarray, w: int = MAP_W, h: int = MAP_H) -> np.ndarray:
    """Nearest-neighbour box pick down to w x h, returned as uint8 0..255."""
    sh, sw = prox.shape[:2]
    ys = np.linspace(0, sh - 1, int(h)).astype(np.int32)
    xs = np.linspace(0, sw - 1, int(w)).astype(np.int32)
    small = np.asarray(prox, dtype=np.float32)[ys][:, xs]
    return np.clip(small * 255.0 + 0.5, 0, 255).astype(np.uint8)


def encode_png_gray(gray: np.ndarray) -> bytes:
    """Minimal 8-bit greyscale PNG. stdlib zlib only -- no PIL on the hot path."""
    g = np.asarray(gray, dtype=np.uint8)
    if g.ndim != 2:
        raise ValueError(f"gray must be 2-D, got {g.shape}")
    h, w = g.shape
    # Filter type 0 (None) in front of every scanline.
    raw = np.concatenate(
        [np.zeros((h, 1), np.uint8), g], axis=1).tobytes()

    def chunk(tag: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + tag + body
                + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))


def depth_map_payload(prox: np.ndarray, w: int = MAP_W,
                      h: int = MAP_H) -> Dict[str, object]:
    """The `extra.depth_map` debug object: base64 PNG of the proximity map."""
    gray = downsample(prox, w, h)
    return {
        "w": int(w),
        "h": int(h),
        "format": "png",
        "encoding": "base64",
        "scale": "proximity 0..255, 255 = nearest",
        "data": base64.b64encode(encode_png_gray(gray)).decode("ascii"),
    }

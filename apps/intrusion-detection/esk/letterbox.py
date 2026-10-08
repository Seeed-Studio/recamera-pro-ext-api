"""Letterbox geometry and its exact inverse, shared by both decode paths.

The RKNN model input is square (640x640); the source is not. Stretching a
1280x720 frame into the square changes the aspect ratio and every published box
comes back horizontally compressed, so the frame is scaled with its aspect
preserved and the remainder padded.

What makes this module load-bearing on Rockchip -- more than on the CPU
platform -- is that the scaling happens in *two different places* depending on
which decoder is active:

* hardware path: ``mppvideodec`` is told to output ``scaled_w x scaled_h`` and
  the RGA-scaled rows are copied into a padded canvas;
* software path: OpenCV resizes and pads.

If those two disagree by even one pixel of pad, the same scene produces
different ``frame_norm`` coordinates depending on a decoder fallback nobody
noticed. So both call :func:`fit_geometry`, and the inverse is derived from the
integer geometry that was actually applied rather than from the ideal ratio.
``coordinate_space`` in the MQTT contract is always ``frame_norm``: normalized
against the ORIGINAL frame, never the padded canvas.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

PAD_VALUE = 114


def fit_geometry(src_w: int, src_h: int, dst: int) -> tuple[int, int, int, int]:
    """``(scaled_w, scaled_h, pad_left, pad_top)`` for an aspect-fit into ``dst``.

    Integer rounding is settled here, once, so the hardware scaler and the
    software fallback cannot drift apart.
    """
    if src_w <= 0 or src_h <= 0 or dst <= 0:
        raise ValueError("source dimensions and destination size must be positive")
    scale = min(dst / src_w, dst / src_h)
    scaled_w = max(1, int(round(src_w * scale)))
    scaled_h = max(1, int(round(src_h * scale)))
    return scaled_w, scaled_h, (dst - scaled_w) // 2, (dst - scaled_h) // 2


@dataclass(frozen=True)
class LetterboxTransform:
    """Forward parameters, enough to invert the mapping exactly.

    ``scale_x`` and ``scale_y`` are kept separately and derived from the integer
    scaled size rather than from the ideal ``min()`` ratio: after rounding, a
    1279-wide source does not scale by exactly the same factor in both axes, and
    a single shared scale would bias every box by up to half a source pixel.
    """

    scale_x: float
    scale_y: float
    pad_x: float
    pad_y: float
    src_w: int
    src_h: int
    dst: int

    @classmethod
    def for_source(cls, src_w: int, src_h: int, dst: int) -> "LetterboxTransform":
        scaled_w, scaled_h, pad_x, pad_y = fit_geometry(src_w, src_h, dst)
        return cls(
            scale_x=scaled_w / src_w,
            scale_y=scaled_h / src_h,
            pad_x=float(pad_x),
            pad_y=float(pad_y),
            src_w=int(src_w),
            src_h=int(src_h),
            dst=int(dst),
        )

    @property
    def scaled_w(self) -> int:
        return int(round(self.src_w * self.scale_x))

    @property
    def scaled_h(self) -> int:
        return int(round(self.src_h * self.scale_y))

    @property
    def active_region(self) -> tuple[int, int, int, int]:
        """``(left, top, w, h)`` of the canvas that holds real pixels."""
        return int(self.pad_x), int(self.pad_y), self.scaled_w, self.scaled_h

    def to_source(self, x: float, y: float) -> tuple[float, float]:
        """Map a point from model-input pixels back to source-frame pixels."""
        return (x - self.pad_x) / self.scale_x, (y - self.pad_y) / self.scale_y


def pad_into_canvas(
    scaled: np.ndarray, dst: int, color: int = PAD_VALUE
) -> np.ndarray:
    """Centre an already aspect-fitted image in a ``dst`` x ``dst`` canvas."""
    if scaled.ndim != 3 or scaled.shape[2] != 3 or scaled.dtype != np.uint8:
        raise ValueError("scaled image must be HWC uint8 with 3 channels")
    height, width = scaled.shape[:2]
    if width > dst or height > dst:
        raise ValueError("scaled image does not fit the letterbox canvas")
    canvas = np.full((dst, dst, 3), color, dtype=np.uint8)
    left, top = (dst - width) // 2, (dst - height) // 2
    canvas[top:top + height, left:left + width] = scaled
    return canvas


def copy_strided_to_canvas(
    data, width: int, height: int, stride: int, dst: int, color: int = PAD_VALUE
) -> np.ndarray:
    """Copy mapped, row-aligned RGB straight into an owned letterbox canvas.

    MPP/RGA aligns output rows, so the buffer stride is usually larger than
    ``width * 3``; reading it as tightly packed shears the image diagonally and
    the detector then finds nothing at all. The copy is also what lets the
    caller unmap the GStreamer buffer immediately -- no borrowed view escapes.
    """
    if width <= 0 or height <= 0 or stride < width * 3:
        raise ValueError("invalid mapped RGB dimensions or stride")
    if len(data) < stride * height:
        raise ValueError("mapped RGB buffer is shorter than its negotiated layout")
    borrowed = np.ndarray(
        (height, width, 3), dtype=np.uint8, buffer=data, strides=(stride, 3, 1)
    )
    return pad_into_canvas(borrowed, dst, color)


def xyxy_to_frame_norm(
    box: tuple[float, float, float, float], tf: LetterboxTransform
) -> list[float]:
    """Model-input xyxy -> ``frame_norm`` [cx, cy, w, h], clamped to [0, 1].

    Clamping happens in source-pixel space before the centre/size conversion:
    a clamped centre with an unclamped width would describe a box reaching
    outside the frame, which the schema rejects.
    """
    x1, y1 = tf.to_source(box[0], box[1])
    x2, y2 = tf.to_source(box[2], box[3])
    x1 = min(max(x1, 0.0), float(tf.src_w))
    x2 = min(max(x2, 0.0), float(tf.src_w))
    y1 = min(max(y1, 0.0), float(tf.src_h))
    y2 = min(max(y2, 0.0), float(tf.src_h))
    # Builtin floats, not numpy scalars: these values end up in json.dumps.
    return [
        float((x1 + x2) / 2.0) / tf.src_w,
        float((y1 + y2) / 2.0) / tf.src_h,
        float(x2 - x1) / tf.src_w,
        float(y2 - y1) / tf.src_h,
    ]


def frame_norm_to_pixels(
    bbox: list[float], width: int, height: int
) -> tuple[int, int, int, int]:
    """``frame_norm`` [cx, cy, w, h] -> pixel xyxy in the original frame."""
    cx, cy, w, h = bbox
    return (
        int(round((cx - w / 2) * width)),
        int(round((cy - h / 2) * height)),
        int(round((cx + w / 2) * width)),
        int(round((cy + h / 2) * height)),
    )

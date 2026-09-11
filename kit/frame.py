"""Canonical high-level frame object for reCamera AI workflows."""
from __future__ import annotations

import math
from typing import Any, Mapping, Optional

import numpy as np

from .buffer import ImageBuffer, PixelFormat
from .diagnostics import get_logger
from .errors import ConfigurationError


log = get_logger("frame")


class Frame:
    """One image and its capture metadata.

    The legacy constructor remains supported::

        Frame(data, width, height, "RGB", monotonic_seconds)

    New code may pass ``buffer=ImageBuffer(...)`` and an exact integer
    ``pts_us``.  ``w``/``h`` describe the original camera coordinate space;
    optimized preprocessors may place a model-sized image in ``data`` while
    retaining the original geometry for result mapping.

    ``model_info``, ``model_data``, and ``roi_cropper`` are compatibility fields
    used by the existing applications.  They will be superseded by a typed
    transform result, but are not removed in this compatibility release.
    """

    def __init__(
        self,
        data: Optional[np.ndarray] = None,
        w: Optional[int] = None,
        h: Optional[int] = None,
        fmt: Optional[str] = None,
        pts: Optional[float] = None,
        model_info: object = None,
        model_data: object = None,
        roi_cropper: object = None,
        *,
        buffer: Optional[ImageBuffer] = None,
        pts_us: Optional[int] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> None:
        if buffer is None:
            if data is None:
                raise TypeError("Frame requires either data or buffer")
            arr = np.asarray(data)
            if arr.ndim < 2:
                raise ValueError(f"frame data must be an image, got shape {arr.shape}")
            # The array's shape describes this image buffer.  Legacy w/h may
            # intentionally describe a different original-camera geometry.
            buffer = ImageBuffer.from_numpy(arr, format=fmt or "RGB")
        elif data is not None:
            raise TypeError("Frame accepts data or buffer, not both")

        self.buffer = buffer
        try:
            self.w = int(buffer.width if w is None else w)
            self.h = int(buffer.height if h is None else h)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(
                "frame dimensions must be integers",
                operation="frame.create",
                details={"width": repr(w), "height": repr(h)},
            ) from exc
        if self.w <= 0 or self.h <= 0:
            raise ConfigurationError(
                f"frame dimensions must be positive, got {self.w}x{self.h}",
                operation="frame.create",
                details={"width": self.w, "height": self.h},
            )
        self.fmt = str(fmt or buffer.format.value).upper()
        if pts_us is None:
            try:
                seconds = 0.0 if pts is None else float(pts)
            except (TypeError, ValueError) as exc:
                raise ConfigurationError(
                    f"frame pts must be a finite number, got {pts!r}",
                    operation="frame.create",
                    details={"pts": repr(pts)},
                ) from exc
            if not math.isfinite(seconds) or seconds < 0.0:
                raise ConfigurationError(
                    f"frame pts must be finite and non-negative, got {seconds!r}",
                    operation="frame.create",
                    details={"pts": seconds},
                )
            pts_us = int(round(seconds * 1_000_000.0))
        try:
            self.pts_us = int(pts_us)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(
                f"frame pts_us must be an integer, got {pts_us!r}",
                operation="frame.create",
                details={"pts_us": repr(pts_us)},
            ) from exc
        if self.pts_us < 0:
            raise ConfigurationError(
                "frame pts_us must be non-negative",
                operation="frame.create",
                details={"pts_us": self.pts_us},
            )
        if pts is not None:
            try:
                seconds = float(pts)
            except (TypeError, ValueError) as exc:
                raise ConfigurationError(
                    f"frame pts must be a finite number, got {pts!r}",
                    operation="frame.create",
                    details={"pts": repr(pts)},
                ) from exc
            if not math.isfinite(seconds) or seconds < 0.0:
                raise ConfigurationError(
                    "frame pts must be finite and non-negative",
                    operation="frame.create",
                    details={"pts": seconds},
                )
            supplied_us = int(round(seconds * 1_000_000.0))
            if supplied_us != self.pts_us:
                raise ConfigurationError(
                    "frame pts and pts_us describe different timestamps",
                    operation="frame.create",
                    details={"pts": seconds, "pts_us": self.pts_us},
                )
            self.pts = seconds
        else:
            self.pts = self.pts_us / 1_000_000.0
        self.model_info = model_info
        self.model_data = model_data
        self.roi_cropper = roi_cropper
        self.metadata = dict(metadata or {})

    @property
    def data(self) -> np.ndarray:
        """Return the current image as a NumPy view.

        Access after :meth:`release` raises ``BufferReleasedError``.  Call
        ``frame.copy()`` when pixels must outlive a borrowed source iteration.
        """

        return self.buffer.numpy(copy=False)

    @property
    def owned(self) -> bool:
        """Whether the frame owns its image storage."""

        return self.buffer.owned

    @property
    def released(self) -> bool:
        """Whether the underlying image buffer has been released."""

        return self.buffer.released

    def copy(self) -> "Frame":
        """Return an owned frame safe to retain after the source advances."""

        deferred = getattr(self, "_deferred_model_image", None)
        # In hw mode the owned RGB buffer is the full camera image, while the
        # model input still borrows the camera lease. Preserve both images and
        # the model transform before returning a frame safe to retain.
        copied_model_data = self.model_data
        if deferred is not None and self.buffer._backend is not deferred:
            copied_model_data = deferred.map()
        # Direct/ROI model pixels already live in buffer: its copy remains the
        # sole image so editing copied.data still changes that model's input.
        if isinstance(copied_model_data, np.ndarray):
            copied_model_data = np.array(copied_model_data, copy=True, order="C")
        return Frame(
            w=self.w,
            h=self.h,
            fmt=self.fmt,
            pts=self.pts,
            buffer=self.buffer.copy(),
            pts_us=self.pts_us,
            model_info=self.model_info,
            model_data=copied_model_data,
            # A cropper is tied to the producer's borrowed dma-buf and must not
            # be copied onto an independent CPU frame.
            roi_cropper=None,
            metadata=self.metadata,
        )

    def release(self) -> None:
        """Release/invalidate the underlying buffer; safe to call repeatedly."""

        self.roi_cropper = None
        deferred = getattr(self, "_deferred_model_image", None)
        if deferred is not None:
            deferred.release()
        self.buffer.release()

    def __enter__(self) -> "Frame":
        # Trigger the normal alive check without copying pixels.
        self.buffer._ensure_alive("frame.enter")
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            self.release()
        except BaseException as cleanup_error:
            if exc is None:
                raise
            log.error(
                "frame cleanup failed while preserving body exception",
                exc_info=(
                    type(cleanup_error),
                    cleanup_error,
                    cleanup_error.__traceback__,
                ),
            )
        return None

    def __repr__(self) -> str:
        return (
            f"Frame(w={self.w}, h={self.h}, fmt={self.fmt!r}, "
            f"pts_us={self.pts_us}, owned={self.owned}, "
            f"released={self.released})"
        )


__all__ = ["Frame", "ImageBuffer", "PixelFormat"]

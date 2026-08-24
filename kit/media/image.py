"""Stable public RGA image operations for borrowed NV12 dma-buf frames.

Only operations proven by the existing RV1126B librga shim are exposed:
NV12->RGB conversion, resize, letterbox, and crop+resize.  Rotation, blending,
drawing, fences, and destination dma-buf pools are intentionally absent until
their ABI and hardware behavior have device tests.

The API consumes a small structural protocol (public ``fd``, dimensions, and
producer-supplied plane descriptors), not a Rockchip ctypes structure.  The
returned buffers are owned CPU RGB arrays and remain valid after the source
frame lease is released.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol, Sequence, runtime_checkable

import numpy as np

from kit.buffer import ImageBuffer, PixelFormat
from kit.errors import CapabilityError, ImageOperationError, InputValidationError
from kit.diagnostics import get_logger


log = get_logger("media.rga")
NV12_FOURCC = 0x3231564E


@dataclass(frozen=True)
class Size:
    """Positive image dimensions in pixels."""

    width: int
    height: int

    def __post_init__(self) -> None:
        if int(self.width) <= 0 or int(self.height) <= 0:
            raise ValueError(f"size must be positive, got {self.width}x{self.height}")
        object.__setattr__(self, "width", int(self.width))
        object.__setattr__(self, "height", int(self.height))


@dataclass(frozen=True)
class Rect:
    """Half-open pixel rectangle ``[x1, y1, x2, y2)``."""

    x1: int
    y1: int
    x2: int
    y2: int

    def __post_init__(self) -> None:
        values = tuple(int(item) for item in (self.x1, self.y1, self.x2, self.y2))
        object.__setattr__(self, "x1", values[0])
        object.__setattr__(self, "y1", values[1])
        object.__setattr__(self, "x2", values[2])
        object.__setattr__(self, "y2", values[3])
        if self.x2 <= self.x1 or self.y2 <= self.y1:
            raise ValueError(f"rectangle has no area: {values!r}")

    @property
    def width(self) -> int:
        return self.x2 - self.x1

    @property
    def height(self) -> int:
        return self.y2 - self.y1

    def as_tuple(self) -> tuple[int, int, int, int]:
        """Return the tuple accepted by the native RGA shim."""

        return self.x1, self.y1, self.x2, self.y2


@dataclass(frozen=True)
class TransformMapping:
    """Exact affine mapping between a source rectangle and output window.

    Pixels outside ``output_rect`` are padding.  ``to_source`` is useful for
    mapping model detections back to camera coordinates after letterbox/crop.
    """

    source_size: Size
    output_size: Size
    source_rect: Rect
    output_rect: Rect

    def to_source(self, x: float, y: float) -> tuple[float, float]:
        """Map one output-space point to source-camera pixels."""

        sx = self.source_rect.width / float(self.output_rect.width)
        sy = self.source_rect.height / float(self.output_rect.height)
        return (
            self.source_rect.x1 + (float(x) - self.output_rect.x1) * sx,
            self.source_rect.y1 + (float(y) - self.output_rect.y1) * sy,
        )

    def box_to_source(self, box: Sequence[float]) -> tuple[float, float, float, float]:
        """Map an ``(x1,y1,x2,y2)`` output box to source-camera pixels."""

        if len(box) < 4:
            raise ValueError("box must contain at least four coordinates")
        x1, y1 = self.to_source(box[0], box[1])
        x2, y2 = self.to_source(box[2], box[3])
        return x1, y1, x2, y2


@dataclass(frozen=True)
class TransformResult:
    """Owned transform output plus its coordinate mapping."""

    image: ImageBuffer
    mapping: TransformMapping


@dataclass(frozen=True)
class RgaCapabilities:
    """Operations supported by the loaded librga build."""

    nv12_to_rgb: bool
    resize: bool
    crop: bool
    zero_copy_source: bool = True


@runtime_checkable
class DmaBufFrame(Protocol):
    """Public subset required from ``recamera_ext.FrameLease``."""

    fd: int
    width: int
    height: int
    fourcc: int
    planes: Sequence[Any]
    buf_size: int
    released: bool


def _plane_values(plane: Any) -> tuple[int, int, int]:
    try:
        return int(plane.offset), int(plane.stride), int(plane.vstride)
    except AttributeError:
        try:
            offset, stride, vstride = plane
            return int(offset), int(stride), int(vstride)
        except Exception as exc:
            raise InputValidationError(
                "invalid dma-buf plane descriptor",
                operation="rga.validate_frame",
            ) from exc


class RgaContext:
    """Bound librga context with checked, typed NV12 operations.

    Construct once per workflow and close it with a context manager.  The
    current native shim owns no persistent hardware handles, so ``close`` only
    invalidates this Python object; it is present now to keep the contract
    compatible with future pooled/fenced implementations.
    """

    def __init__(self, backend: Optional[Any] = None) -> None:
        if backend is None:
            try:
                from kit.adapters._rga import RgaNV12ToRGB
                backend = RgaNV12ToRGB()
            except Exception as exc:
                raise CapabilityError(
                    f"RGA is unavailable: {exc}",
                    operation="rga.open",
                    details={"backend": "librga"},
                ) from exc
        self._backend = backend
        self._closed = False
        try:
            crop = bool(getattr(backend, "can_crop", lambda: False)())
            resize_probe = getattr(backend, "can_resize", None)
            resize = (
                bool(resize_probe())
                if callable(resize_probe)
                else callable(getattr(backend, "resize_nv12_to_rgb", None))
            )
        except Exception as exc:
            raise CapabilityError(
                f"could not query RGA capabilities: {exc}",
                operation="rga.open",
                details={"backend": type(backend).__name__},
            ) from exc
        self.capabilities = RgaCapabilities(
            nv12_to_rgb=True,
            resize=resize,
            crop=crop,
        )

    def _frame(self, frame: DmaBufFrame, operation: str):
        if self._closed:
            raise ImageOperationError(
                "RGA context is closed",
                operation=operation,
                code="context_closed",
            )
        if bool(getattr(frame, "released", False)):
            raise InputValidationError(
                "source frame lease has already been released",
                operation=operation,
                code="source_released",
            )
        try:
            fd = int(frame.fd)
            width, height = int(frame.width), int(frame.height)
            fourcc = int(frame.fourcc)
            planes = frame.planes
            raw_size = getattr(frame, "buf_size", None)
            if raw_size is None:
                raw_size = getattr(getattr(frame, "buffer", None), "size", None)
            buffer_size = int(raw_size)
        except Exception as exc:
            raise InputValidationError(
                "source does not implement the dma-buf frame contract",
                operation=operation,
            ) from exc
        if (fd < 0 or width <= 0 or height <= 0 or buffer_size <= 0
                or len(planes) < 2):
            raise InputValidationError(
                "NV12 dma-buf requires valid geometry and Y/UV descriptors",
                operation=operation,
                details={"fd": fd, "width": width, "height": height,
                         "buffer_size": buffer_size,
                         "plane_count": len(planes)},
            )
        if fourcc not in (0, NV12_FOURCC):
            raise InputValidationError(
                f"RGA interface currently accepts NV12 only (fourcc=0x{fourcc:08x})",
                operation=operation,
                details={"fourcc": fourcc},
            )
        offset, stride, vstride = _plane_values(planes[0])
        uv_offset, uv_stride, uv_vstride = _plane_values(planes[1])
        if offset != 0:
            raise InputValidationError(
                "RGA fd wrapping requires the Y plane at dma-buf offset zero",
                operation=operation,
                details={"offset": offset},
            )
        if (width | height) & 1:
            raise InputValidationError(
                "NV12 source width and height must be even",
                operation=operation,
                details={"width": width, "height": height},
            )
        if stride <= 0 or vstride <= 0 or width > stride or height > vstride:
            raise InputValidationError(
                "NV12 geometry exceeds the producer Y-plane layout",
                operation=operation,
                details={
                    "width": width,
                    "height": height,
                    "stride": stride,
                    "vstride": vstride,
                },
            )
        required_uv_rows = (vstride + 1) // 2
        uv_end = uv_offset + uv_stride * uv_vstride
        if (width > uv_stride or required_uv_rows > uv_vstride
                or uv_stride != stride or uv_offset != stride * vstride
                or uv_end > buffer_size):
            raise InputValidationError(
                "NV12 UV layout cannot be represented by librga fd wrapping",
                operation=operation,
                details={
                    "y": {"offset": offset, "stride": stride,
                          "vstride": vstride},
                    "uv": {"offset": uv_offset, "stride": uv_stride,
                           "vstride": uv_vstride},
                    "required_uv_offset": stride * vstride,
                    "required_uv_vstride": required_uv_rows,
                    "buffer_size": buffer_size,
                    "uv_end": uv_end,
                },
            )
        return fd, width, height, stride, vstride

    @staticmethod
    def _size(value: Size | tuple[int, int], operation: str) -> Size:
        if isinstance(value, Size):
            return value
        try:
            raw = tuple(value)
            if len(raw) != 2 or any(isinstance(item, bool) for item in raw):
                raise ValueError("expected exactly two non-boolean dimensions")
            return Size(*raw)
        except (TypeError, ValueError, OverflowError) as exc:
            raise InputValidationError(
                "size must contain two positive integer dimensions",
                operation=operation,
                details={"size_type": type(value).__name__},
            ) from exc

    @staticmethod
    def _rect(value: Rect | Sequence[int], operation: str) -> Rect:
        if isinstance(value, Rect):
            return value
        try:
            raw = tuple(value)
            if len(raw) != 4 or any(isinstance(item, bool) for item in raw):
                raise ValueError("expected exactly four non-boolean coordinates")
            return Rect(*raw)
        except (TypeError, ValueError, OverflowError) as exc:
            raise InputValidationError(
                "rect must contain four ordered integer coordinates",
                operation=operation,
                details={"rect_type": type(value).__name__},
            ) from exc

    @staticmethod
    def _pad_value(value: int, operation: str) -> int:
        try:
            result = int(value)
        except (TypeError, ValueError) as exc:
            raise InputValidationError(
                f"pad_value must be an integer byte, got {value!r}",
                operation=operation,
            ) from exc
        if not 0 <= result <= 255:
            raise InputValidationError(
                "pad_value must be between 0 and 255",
                operation=operation,
                details={"pad_value": result},
            )
        return result

    @staticmethod
    def _owned_rgb(
        array: np.ndarray,
        *,
        width: int,
        height: int,
        operation: str,
    ) -> ImageBuffer:
        """Validate a native result before exposing it as owned RGB pixels."""

        arr = np.asarray(array)
        expected = (int(height), int(width), 3)
        if arr.dtype != np.uint8 or arr.shape != expected:
            raise ImageOperationError(
                "RGA returned an invalid RGB buffer",
                operation=operation,
                code="invalid_backend_output",
                details={
                    "expected_shape": list(expected),
                    "actual_shape": list(arr.shape),
                    "expected_dtype": "uint8",
                    "actual_dtype": str(arr.dtype),
                },
            )
        arr = np.ascontiguousarray(arr)
        return ImageBuffer.from_numpy(arr, format=PixelFormat.RGB)

    def convert_nv12(self, frame: DmaBufFrame) -> ImageBuffer:
        """Convert a borrowed NV12 dma-buf frame to owned RGB pixels."""

        operation = "rga.convert_nv12"
        fd, width, height, stride, vstride = self._frame(frame, operation)
        try:
            out = self._backend.convert(
                fd=fd,
                width=width,
                height=height,
                y_stride=stride,
                y_vstride=vstride,
            )
            return self._owned_rgb(
                out,
                width=width,
                height=height,
                operation=operation,
            )
        except ImageOperationError:
            raise
        except Exception as exc:
            log.exception("RGA NV12 conversion failed geometry=%dx%d", width, height)
            raise ImageOperationError(
                f"RGA NV12 conversion failed: {exc}",
                operation=operation,
                retryable=False,
                details={"width": width, "height": height},
            ) from exc

    def resize_nv12(self, frame: DmaBufFrame, size: Size | tuple[int, int]) -> TransformResult:
        """Resize a full NV12 frame to owned RGB, without preserving aspect."""

        operation = "rga.resize_nv12"
        target = self._size(size, operation)
        fd, width, height, stride, vstride = self._frame(frame, operation)
        if (target.width | target.height) & 1:
            raise InputValidationError(
                "NV12 resize destination width and height must be even",
                operation=operation,
                details={"destination": [target.width, target.height]},
            )
        if not self.capabilities.resize:
            raise CapabilityError(
                "loaded librga does not expose NV12 resize",
                operation=operation,
            )
        try:
            out = self._backend.resize_nv12_to_rgb(
                fd=fd,
                width=width,
                height=height,
                y_stride=stride,
                y_vstride=vstride,
                dst_width=target.width,
                dst_height=target.height,
            )
        except Exception as exc:
            log.exception("RGA resize failed %dx%d -> %dx%d",
                          width, height, target.width, target.height)
            raise ImageOperationError(
                f"RGA resize failed: {exc}",
                operation=operation,
                details={
                    "source": [width, height],
                    "destination": [target.width, target.height],
                },
            ) from exc
        mapping = TransformMapping(
            source_size=Size(width, height),
            output_size=target,
            source_rect=Rect(0, 0, width, height),
            output_rect=Rect(0, 0, target.width, target.height),
        )
        return TransformResult(
            self._owned_rgb(
                out,
                width=target.width,
                height=target.height,
                operation=operation,
            ),
            mapping,
        )

    def letterbox_nv12(
        self,
        frame: DmaBufFrame,
        size: Size | tuple[int, int],
        *,
        pad_value: int = 114,
    ) -> TransformResult:
        """Aspect-preserving NV12 resize into an owned padded RGB canvas."""

        operation = "rga.letterbox_nv12"
        target = self._size(size, operation)
        pad = self._pad_value(pad_value, operation)
        _fd, width, height, _stride, _vstride = self._frame(frame, operation)
        scale = min(target.width / float(width), target.height / float(height))
        resized_w = int(round(width * scale))
        resized_h = int(round(height * scale))
        # NV12 chroma requires even resize geometry.  Use the nearest lower even
        # size and report the exact resulting mapping.
        resized_w = max(2, resized_w & ~1)
        resized_h = max(2, resized_h & ~1)
        if resized_w > target.width or resized_h > target.height:
            raise InputValidationError(
                "letterbox destination is too small for valid NV12 geometry",
                operation=operation,
            )
        resized = self.resize_nv12(frame, Size(resized_w, resized_h)).image.numpy()
        left = (target.width - resized_w) // 2
        top = (target.height - resized_h) // 2
        canvas = np.full((target.height, target.width, 3),
                         pad, dtype=np.uint8)
        canvas[top:top + resized_h, left:left + resized_w] = resized
        mapping = TransformMapping(
            source_size=Size(width, height),
            output_size=target,
            source_rect=Rect(0, 0, width, height),
            output_rect=Rect(left, top, left + resized_w, top + resized_h),
        )
        return TransformResult(
            self._owned_rgb(
                canvas,
                width=target.width,
                height=target.height,
                operation=operation,
            ),
            mapping,
        )

    def crop_nv12(
        self,
        frame: DmaBufFrame,
        rect: Rect | Sequence[int],
        size: Size | tuple[int, int],
        *,
        pad_value: int = 114,
    ) -> TransformResult:
        """Crop an NV12 source rectangle and resize it to owned RGB.

        The current librga shim accepts a square destination.  A non-square
        target is rejected explicitly instead of stretching or silently using
        one dimension.
        """

        operation = "rga.crop_nv12"
        source_rect = self._rect(rect, operation)
        target = self._size(size, operation)
        pad = self._pad_value(pad_value, operation)
        if target.width != target.height:
            raise InputValidationError(
                "current RGA crop backend requires a square destination",
                operation=operation,
                details={"destination": [target.width, target.height]},
            )
        fd, width, height, stride, vstride = self._frame(frame, operation)
        if not self.capabilities.crop:
            raise CapabilityError(
                "loaded librga does not expose the crop operation",
                operation=operation,
            )
        clipped_values = (
            max(0, source_rect.x1),
            max(0, source_rect.y1),
            min(width, source_rect.x2),
            min(height, source_rect.y2),
        )
        if (clipped_values[2] <= clipped_values[0] or
                clipped_values[3] <= clipped_values[1]):
            raise InputValidationError(
                "crop rectangle does not intersect the source frame",
                operation=operation,
                details={"rect": list(source_rect.as_tuple()),
                         "source": [width, height]},
            )
        clipped = Rect(*clipped_values)
        # Mirror the native shim's NV12 even alignment so result coordinates map
        # to the pixels the hardware actually sampled.
        aligned_x1 = clipped.x1 & ~1
        aligned_y1 = clipped.y1 & ~1
        aligned_w = (clipped.x2 - aligned_x1) & ~1
        aligned_h = (clipped.y2 - aligned_y1) & ~1
        if aligned_w < 2 or aligned_h < 2:
            raise InputValidationError(
                "crop rectangle is too small after NV12 chroma alignment",
                operation=operation,
                details={"rect": list(source_rect.as_tuple())},
            )
        actual = Rect(aligned_x1, aligned_y1,
                      aligned_x1 + aligned_w, aligned_y1 + aligned_h)
        try:
            out = self._backend.crop_nv12_to_rgb(
                fd=fd,
                width=width,
                height=height,
                y_stride=stride,
                y_vstride=vstride,
                src_rect=actual.as_tuple(),
                dst_size=target.width,
                pad_value=pad,
            )
        except Exception as exc:
            log.exception("RGA crop failed rect=%s destination=%dx%d",
                          actual.as_tuple(), target.width, target.height)
            raise ImageOperationError(
                f"RGA crop failed: {exc}",
                operation=operation,
                details={
                    "rect": list(actual.as_tuple()),
                    "destination": [target.width, target.height],
                },
            ) from exc
        mapping = TransformMapping(
            source_size=Size(width, height),
            output_size=target,
            source_rect=actual,
            output_rect=Rect(0, 0, target.width, target.height),
        )
        return TransformResult(
            self._owned_rgb(
                out,
                width=target.width,
                height=target.height,
                operation=operation,
            ),
            mapping,
        )

    def close(self) -> None:
        """Invalidate the context.  Safe to call repeatedly."""

        self._closed = True
        self._backend = None

    def __enter__(self) -> "RgaContext":
        if self._closed:
            raise ImageOperationError(
                "RGA context is closed",
                operation="rga.enter",
                code="context_closed",
            )
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


# User-facing name for the image-operation service.  It is an alias today; a
# future backend selector may return a CPU implementation under this contract.
ImageOps = RgaContext


__all__ = [
    "DmaBufFrame",
    "ImageOps",
    "NV12_FOURCC",
    "Rect",
    "RgaCapabilities",
    "RgaContext",
    "Size",
    "TransformMapping",
    "TransformResult",
]

"""Backend-neutral image-buffer contracts for AI workflows.

``ImageBuffer`` intentionally does not expose Rockchip C structures.  A buffer
may be an owned NumPy array or a short-lived view backed by a native frame
lease, but users interact through the same checked methods.  Releasing a
borrowed buffer invalidates every future access instead of returning stale
memory that may already have been reused by VI/RGA.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Iterable, Optional, Protocol, runtime_checkable

import numpy as np

from .diagnostics import get_logger
from .errors import AdapterError, BufferReleasedError, ConfigurationError


log = get_logger("buffer")


class PixelFormat(str, Enum):
    """Pixel formats supported by the public Python buffer contract."""

    RGB = "RGB"
    BGR = "BGR"
    RGBA = "RGBA"
    BGRA = "BGRA"
    GRAY8 = "GRAY8"
    NV12 = "NV12"


class MemoryKind(str, Enum):
    """Where the pixels live."""

    CPU = "cpu"
    DMABUF = "dmabuf"
    BACKEND = "backend"


class Ownership(str, Enum):
    """Whether the workflow owns the storage or temporarily borrows it."""

    OWNED = "owned"
    BORROWED = "borrowed"


@dataclass(frozen=True)
class PlaneLayout:
    """Byte layout of one image plane.

    The values must come from the producer.  NV12 layout is never inferred from
    width/height because Rockchip buffers may contain aligned stride/vstride.
    """

    offset: int
    stride: int
    vstride: int

    def __post_init__(self) -> None:
        values = (int(self.offset), int(self.stride), int(self.vstride))
        object.__setattr__(self, "offset", values[0])
        object.__setattr__(self, "stride", values[1])
        object.__setattr__(self, "vstride", values[2])
        if values[0] < 0 or values[1] <= 0 or values[2] <= 0:
            raise ConfigurationError(
                "plane layout requires offset >= 0 and positive stride/vstride",
                operation="buffer.create",
                details={
                    "offset": values[0],
                    "stride": values[1],
                    "vstride": values[2],
                },
            )

    def __iter__(self):
        # Preserve the historical ``off, stride, vstride = plane`` idiom.
        return iter((self.offset, self.stride, self.vstride))


@runtime_checkable
class BufferBackend(Protocol):
    """Minimal protocol implemented by a native borrowed-buffer wrapper."""

    def map(self) -> Any:
        """Map the current lease and return a buffer-protocol object."""

    def release(self) -> None:
        """Release the lease; the operation must be idempotent."""

    @property
    def released(self) -> bool:
        """Whether the producer has invalidated this borrowed buffer."""


def _pixel_format(value: PixelFormat | str) -> PixelFormat:
    try:
        return value if isinstance(value, PixelFormat) else PixelFormat(str(value).upper())
    except ValueError as exc:
        raise ConfigurationError(
            f"unsupported pixel format: {value!r}",
            operation="buffer.create",
            details={"format": str(value)},
        ) from exc


def _enum_value(enum_type, value, field_name: str, operation: str):
    """Parse one public enum and turn raw ``ValueError`` into kit context."""

    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(str(value))
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(
            f"unsupported {field_name}: {value!r}",
            operation=operation,
            details={field_name: str(value)},
        ) from exc


class ImageBuffer:
    """Checked image storage used by frame, RGA, and inference interfaces.

    Construct CPU buffers with :meth:`from_numpy`.  Native adapters should use
    :meth:`from_backend` and supply the exact plane descriptors received from
    the producer.

    Access rules:
      * ``numpy(copy=False)`` may return a view whose lifetime is this object.
      * ``copy()`` always returns independent, owned CPU storage.
      * after ``release()``, every data-bearing operation raises
        :class:`~kit.errors.BufferReleasedError`.
    """

    def __init__(
        self,
        *,
        width: int,
        height: int,
        format: PixelFormat | str,
        memory: MemoryKind | str,
        ownership: Ownership | str,
        planes: Iterable[PlaneLayout | tuple[int, int, int]] = (),
        array: Optional[np.ndarray] = None,
        backend: Optional[BufferBackend] = None,
        release_callback: Optional[Callable[[], None]] = None,
    ) -> None:
        try:
            width_i, height_i = int(width), int(height)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(
                f"image dimensions must be integers, got {width!r}x{height!r}",
                operation="buffer.create",
                details={"width": repr(width), "height": repr(height)},
            ) from exc
        if width_i <= 0 or height_i <= 0:
            raise ConfigurationError(
                f"image dimensions must be positive, got {width}x{height}",
                operation="buffer.create",
                details={"width": width, "height": height},
            )
        self.width = width_i
        self.height = height_i
        self.format = _pixel_format(format)
        self.memory = _enum_value(
            MemoryKind, memory, "memory", "buffer.create")
        self.ownership = _enum_value(
            Ownership, ownership, "ownership", "buffer.create")
        self.planes = tuple(
            item if isinstance(item, PlaneLayout) else PlaneLayout(*map(int, item))
            for item in planes
        )
        self._array = array
        self._backend = backend
        self._release_callback = release_callback
        self._released = False
        # RLock permits a backend map callback to query/release this same
        # object without self-deadlock while still serializing map vs release.
        self._lock = threading.RLock()

        if self.memory is MemoryKind.CPU and array is None:
            raise ConfigurationError(
                "CPU ImageBuffer requires a numpy array",
                operation="buffer.create",
            )
        if self.memory is not MemoryKind.CPU and backend is None:
            raise ConfigurationError(
                "non-CPU ImageBuffer requires a backend",
                operation="buffer.create",
                details={"memory": self.memory.value},
            )

    @classmethod
    def from_numpy(
        cls,
        array: np.ndarray,
        *,
        format: PixelFormat | str = PixelFormat.RGB,
        width: Optional[int] = None,
        height: Optional[int] = None,
        copy: bool = False,
    ) -> "ImageBuffer":
        """Create an owned CPU buffer from an array.

        ``copy=False`` adopts the supplied array as application-owned storage;
        it does not imply a native borrowed lease.  Use ``copy=True`` when the
        producer may mutate or free the original array.
        """

        arr = np.asarray(array)
        if arr.ndim < 2:
            raise ConfigurationError(
                f"image array must have at least two dimensions, got {arr.shape}",
                operation="buffer.from_numpy",
                details={"shape": list(arr.shape)},
            )
        if copy:
            arr = np.array(arr, copy=True, order="C")
        inferred_h, inferred_w = int(arr.shape[0]), int(arr.shape[1])
        actual_w = inferred_w if width is None else int(width)
        actual_h = inferred_h if height is None else int(height)
        if (actual_w, actual_h) != (inferred_w, inferred_h):
            raise ConfigurationError(
                "declared image dimensions do not match the numpy array",
                operation="buffer.from_numpy",
                details={
                    "declared": [actual_w, actual_h],
                    "array": [inferred_w, inferred_h],
                },
            )
        stride = int(arr.strides[0])
        return cls(
            width=actual_w,
            height=actual_h,
            format=format,
            memory=MemoryKind.CPU,
            ownership=Ownership.OWNED,
            planes=(PlaneLayout(0, stride, actual_h),),
            array=arr,
        )

    @classmethod
    def from_backend(
        cls,
        backend: BufferBackend,
        *,
        width: int,
        height: int,
        format: PixelFormat | str,
        planes: Iterable[PlaneLayout | tuple[int, int, int]],
        memory: MemoryKind | str = MemoryKind.DMABUF,
        release_callback: Optional[Callable[[], None]] = None,
    ) -> "ImageBuffer":
        """Create a borrowed buffer around a native lease.

        ``planes`` is mandatory and is copied verbatim.  The high-level API
        never guesses an aligned plane layout.
        """

        plane_tuple = tuple(planes)
        if not plane_tuple:
            raise ConfigurationError(
                "borrowed image buffer requires producer plane descriptors",
                operation="buffer.from_backend",
            )
        return cls(
            width=width,
            height=height,
            format=format,
            memory=memory,
            ownership=Ownership.BORROWED,
            planes=plane_tuple,
            backend=backend,
            release_callback=release_callback,
        )

    @property
    def released(self) -> bool:
        """Whether this object or its native producer invalidated the lease."""

        with self._lock:
            local_released = self._released
            backend = self._backend
        if local_released:
            return True
        if backend is None or self.ownership is Ownership.OWNED:
            return False
        try:
            return bool(getattr(backend, "released", False))
        except Exception as exc:
            # A backend that can no longer report its lifetime cannot safely be
            # treated as alive.  Fail closed rather than expose a cached view.
            log.error("borrowed buffer lifetime probe failed: %s", exc,
                      exc_info=True)
            return True

    @property
    def owned(self) -> bool:
        """Whether this object owns its storage."""

        return self.ownership is Ownership.OWNED

    def _ensure_alive(self, operation: str) -> None:
        if self.released:
            raise BufferReleasedError(
                "image buffer has already been released",
                operation=operation,
                details={
                    "width": self.width,
                    "height": self.height,
                    "format": self.format.value,
                    "memory": self.memory.value,
                },
            )

    def numpy(self, *, copy: bool = False) -> np.ndarray:
        """Return pixels as a NumPy array.

        Native backends may expose a one-dimensional mapped byte view; format-
        specific reshaping remains the backend's responsibility.  Requesting a
        copy is the portable way to keep data after the lease is released.
        """

        with self._lock:
            self._ensure_alive("buffer.numpy")
            if self._array is None:
                backend = self._backend
                if backend is None:
                    # Never expose a raw AssertionError from a lifecycle race.
                    raise BufferReleasedError(
                        "image buffer backend is no longer available",
                        operation="buffer.numpy",
                    )
                mapped = backend.map()
                # A re-entrant backend callback or out-of-band producer may
                # invalidate the lease during map().  Recheck before caching or
                # returning the resulting view.
                self._ensure_alive("buffer.numpy")
                self._array = np.asarray(mapped)
            result = self._array
            return (np.array(result, copy=True, order="C")
                    if copy else result)

    def copy(self) -> "ImageBuffer":
        """Return independent, owned CPU storage with the same pixel format.

        A native backend is allowed to expose a flat raw mapping containing
        aligned planes.  Such a mapping cannot be reconstructed as HWC without
        guessing its format/stride, so the owned copy deliberately preserves
        the original dimensions and producer plane descriptors while keeping
        the copied NumPy array in its backend-provided shape.
        """

        array = self.numpy(copy=True)
        if (self.memory is MemoryKind.CPU and array.ndim >= 2
                and array.shape[:2] == (self.height, self.width)):
            return ImageBuffer.from_numpy(array, format=self.format, copy=False)
        return ImageBuffer(
            width=self.width,
            height=self.height,
            format=self.format,
            memory=MemoryKind.CPU,
            ownership=Ownership.OWNED,
            planes=self.planes,
            array=array,
        )

    def release(self) -> None:
        """Invalidate this object and release a native lease exactly once."""

        with self._lock:
            if self._released:
                return
            self._released = True
            backend, self._backend = self._backend, None
            callback, self._release_callback = self._release_callback, None
            # Drop our reference to a potentially large owned/mapped view.  A
            # caller that needs it after release must have requested copy().
            self._array = None
        failures: list[tuple[str, BaseException]] = []
        if callback is not None:
            try:
                callback()
            except BaseException as exc:  # release remains idempotent on error
                failures.append(("callback", exc))
                log.error("image buffer release callback failed: %s", exc,
                          exc_info=True)
        if backend is not None:
            try:
                backend.release()
            except BaseException as exc:
                failures.append(("backend", exc))
                log.error("image buffer backend release failed: %s", exc,
                          exc_info=True)
        if failures:
            control = next(
                (exc for _stage, exc in failures
                 if not isinstance(exc, Exception)),
                None,
            )
            if control is not None:
                raise control
            first = failures[0][1]
            raise AdapterError(
                f"{len(failures)} image buffer release operation(s) failed",
                operation="buffer.release",
                code="buffer_release_failed",
                details={
                    "failures": [
                        {"stage": stage, "type": type(exc).__name__,
                         "message": str(exc)}
                        for stage, exc in failures
                    ],
                },
            ) from first

    def __enter__(self) -> "ImageBuffer":
        self._ensure_alive("buffer.enter")
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        try:
            self.release()
        except BaseException:
            if exc is None:
                raise
            log.error(
                "image buffer release failed while preserving earlier error",
                exc_info=True,
            )
        return False

    def __del__(self) -> None:
        # Best-effort only.  Correct code uses a context manager or explicit
        # release; this guard prevents an otherwise unreferenced borrowed
        # backend from being retained until interpreter shutdown.
        try:
            self.release()
        except BaseException:
            pass


__all__ = [
    "BufferBackend",
    "ImageBuffer",
    "MemoryKind",
    "Ownership",
    "PixelFormat",
    "PlaneLayout",
]

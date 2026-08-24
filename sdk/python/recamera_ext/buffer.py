"""Safe views over a borrowed native image buffer.

``BorrowedBuffer`` deliberately does not know about ctypes or Rockchip structs.
Its owner supplies three small callbacks (alive check, fd lookup, and full-buffer
mapping), while this module owns the public plane-layout and NumPy-view rules.
That separation keeps the buffer contract unit-testable without loading the
aarch64 shared library.

The object never owns the dma-buf.  Calling :meth:`release` delegates to the
owning :class:`recamera_ext.FrameLease`; advancing/closing the source may also
release it.  Every data-bearing operation checks the lease first, so a stale
``frame.array`` property access fails deterministically instead of returning a
new view over unmapped memory.  A NumPy view already retained by user code cannot
be revoked by Python; callers that need data beyond the lease must call
``copy()`` while it is alive.
"""

from __future__ import annotations

import logging
import weakref
from typing import NamedTuple, Sequence

try:  # normal package import
    from .errors import BufferReleasedError, FormatError
except (ImportError, ModuleNotFoundError):  # standalone __init__.py loader compatibility
    # Some existing offline ABI tests deliberately execute this package's
    # ``__init__.py`` via ``spec_from_file_location`` under a private, non-package
    # module name.  The package bootstrap preloads this stable sibling module for
    # that case; keeping the fallback avoids breaking those tests/tools.
    from _recamera_ext_standalone_errors import BufferReleasedError, FormatError

__all__ = ["PlaneLayout", "BorrowedBuffer"]

_LOG = logging.getLogger(__name__)
_LOG.addHandler(logging.NullHandler())


class PlaneLayout(NamedTuple):
    """One image plane's byte layout inside a shared buffer.

    A ``NamedTuple`` is intentional: new code can use named attributes while old
    callers can still unpack or index it exactly like the historical
    ``(offset, stride, vstride)`` tuple.
    """

    offset: int
    stride: int
    vstride: int


class BorrowedBuffer:
    """A non-owning, lease-checked dma-buf view.

    Parameters are copied metadata.  ``owner`` is expected to implement the
    private callback surface used by :class:`recamera_ext.FrameLease`:
    ``_ensure_alive()``, ``_buffer_fd()``, ``_buffer_map()`` and ``release()``.
    Holding only a weak reference avoids a ``lease -> buffer -> lease`` finalizer
    cycle, so dropping the last frame reference can promptly return the native
    buffer.
    """

    def __init__(
        self,
        owner,
        *,
        size: int,
        width: int,
        height: int,
        fourcc: int,
        planes: Sequence[PlaneLayout],
    ) -> None:
        self._owner_ref = weakref.ref(owner)
        self._size = int(size)
        self._width = int(width)
        self._height = int(height)
        self._fourcc = int(fourcc)
        self._planes = tuple(PlaneLayout(*p) for p in planes)

    @property
    def size(self) -> int:
        """Total mapped byte length reported by the native producer."""

        return self._size

    @property
    def width(self) -> int:
        """Valid image width; plane stride may be larger."""

        return self._width

    @property
    def height(self) -> int:
        """Valid image height; plane vstride may be larger."""

        return self._height

    @property
    def fourcc(self) -> int:
        """Producer-supplied V4L2 fourcc value."""

        return self._fourcc

    @property
    def planes(self):
        """Immutable tuple of producer-supplied :class:`PlaneLayout` values."""

        return self._planes

    def _owner(self):
        owner = self._owner_ref()
        if owner is None:
            raise BufferReleasedError("the frame lease no longer exists")
        return owner

    def _alive_owner(self):
        owner = self._owner()
        owner._ensure_alive()
        return owner

    @property
    def released(self) -> bool:
        """Whether the owning frame lease has already been released."""

        owner = self._owner_ref()
        return owner is None or bool(owner.released)

    @property
    def fd(self) -> int:
        """Borrowed dma-buf fd, valid only until release.

        The fd must not be closed by Python code and must not be cached for use
        after the lease.  Use it synchronously with RGA/GStreamer while the frame
        is alive.
        """

        return int(self._alive_owner()._buffer_fd())

    def map(self):
        """Return a zero-copy 1-D ``uint8`` view of the complete buffer."""

        return self._alive_owner()._buffer_map()

    def plane_array(self, index: int):
        """Return plane ``index`` as a zero-copy ``(vstride, stride)`` view.

        Plane dimensions come exclusively from the server-provided descriptor;
        they are never inferred from image width/height.  The complete described
        byte range is validated before constructing the strided view.
        """

        if not isinstance(index, int):
            raise IndexError(f"plane index must be an integer: {index!r}")
        try:
            plane = self.planes[index]
        except IndexError:
            raise IndexError(f"plane index out of range: {index!r}") from None
        if plane.offset < 0 or plane.stride <= 0 or plane.vstride <= 0:
            raise FormatError(
                "invalid plane layout",
                detail=(f"plane={index} offset={plane.offset} "
                        f"stride={plane.stride} vstride={plane.vstride}"),
            )
        end = plane.offset + plane.stride * plane.vstride
        if end > self.size:
            raise FormatError(
                "plane layout exceeds borrowed buffer",
                detail=f"plane={index} end={end} size={self.size}",
            )

        import numpy as np

        buf = self.map()
        return np.lib.stride_tricks.as_strided(
            buf[plane.offset:end],
            shape=(plane.vstride, plane.stride),
            strides=(plane.stride, 1),
            writeable=False,
        )

    def copy(self):
        """Copy the entire mapped buffer into independently owned memory."""

        return self.map().copy()

    def release(self) -> bool:
        """Release the owning frame; returns ``True`` only on the first release."""

        owner = self._owner_ref()
        return False if owner is None else bool(owner.release())

    def __enter__(self):
        self._alive_owner()
        return self

    def __exit__(self, exc_type, exc_value, _traceback):
        if exc_type is None:
            self.release()
            return False
        try:
            self.release()
        except BaseException as cleanup_error:
            _LOG.error(
                "BorrowedBuffer cleanup failed while preserving the "
                "with-body exception",
                exc_info=True,
            )
            add_note = getattr(exc_value, "add_note", None)
            if callable(add_note):
                try:
                    add_note(
                        "BorrowedBuffer cleanup also failed: "
                        f"{type(cleanup_error).__name__}: {cleanup_error}"
                    )
                except Exception:
                    pass
        return False

    def __repr__(self) -> str:
        state = "released" if self.released else "borrowed"
        return (
            f"BorrowedBuffer({self.width}x{self.height}, fourcc=0x{self.fourcc:08x}, "
            f"size={self.size}, planes={len(self.planes)}, {state})"
        )

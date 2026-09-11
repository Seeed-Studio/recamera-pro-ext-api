"""
Optional RGA (Rockchip 2D raster graphic accelerator) NV12->RGB converter.

This module is a *thin, self-contained, OPTIONAL* ctypes shim over the device's
`librga.so` (im2d API). It exists so `OfficialFrameSource` can convert the
camera's NV12 dma-buf frames to packed RGB on the RV1126B's dedicated 2D
hardware -- near-zero CPU, reading the dma-buf fd directly -- instead of the
CPU/OpenCV path.

WHY A HAND-ROLLED CTYPES WRAPPER
--------------------------------
librga ships no official Python binding. Rather than pull a heavy third-party
package onto a constrained edge device, we bind the three im2d entry points we
actually need. Vendors copying this example get a complete, dependency-free
reference for talking to librga from Python.

ABI NOTE -- WHY WE USE wrapbuffer + AN OPAQUE STRUCT (read before editing)
-------------------------------------------------------------------------
`rga_buffer_t` grew across librga releases; on the device's librga (v1.10.5) it
is 96 bytes, and that library's `importbuffer_fd` takes an
`im_handle_param_t*`, NOT `(int fd, int size)` -- passing an int as the pointer
SIGSEGVs. It also forbids mixing an imported dma-buf *handle* with a virtual
address in the same im2d op. So this shim does NOT hand-build the struct or
import/release buffers itself. Instead it lets librga populate the struct via
the `wrapbuffer_*_t` constructors and treats `rga_buffer_t` as a 96-byte OPAQUE
blob (Python never reads its fields). The source is wrapped from the dma-buf
`fd` (`wrapbuffer_fd_t`) and the destination from a CPU virtual address
(`wrapbuffer_virtualaddr_t`) -- one consistent no-handles path.

Symbol names carry a `_t` suffix (`wrapbuffer_fd_t`,
`wrapbuffer_virtualaddr_t`, `imcvtcolor_t`): the non-suffixed spellings in the
public headers are macros expanding to these. Confirm on your device with
`strings librga.so | grep -iE 'wrapbuffer|imcvtcolor'`.

SAFETY / GRACEFUL DEGRADATION (read this before trusting the fast path)
----------------------------------------------------------------------
The RK_FORMAT_* enum values and the `_t` symbols below are transcribed from the
*public* Rockchip linux-rga headers (`im2d_api/im2d_type.h`, `include/rga.h`)
and verified against the device's librga. librga's ABI has drifted between
releases, so every failure mode is still defensive.

Therefore every failure mode is defensive:
  * librga not present / not loadable                -> `available()` is False.
  * a required symbol is missing                     -> `available()` is False.
  * dma-buf import returns a bad handle, or
    `imcvtcolor_t` returns anything but SUCCESS, or
    any exception is raised during a conversion      -> the CALLER latches to
                                                        the OpenCV path for the
                                                        rest of the run.
Set `RECAMERA_RGA=0` in the environment to force the OpenCV path regardless of
librga presence (belt-and-braces for a mis-detected/mis-versioned librga).

If you have verified your device's librga against these definitions, you can
rely on the fast path; otherwise the example still runs correctly (just warmer)
via the OpenCV fallback.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import operator
import os
import threading
from ctypes import (
    Structure,
    c_char,
    c_char_p,
    c_double,
    c_int,
    c_void_p,
)
from typing import Optional

import numpy as np

# --- RK_FORMAT_* (include/rga.h). VERIFY against your device's rga.h. -------- #
RK_FORMAT_RGB_888 = 0x2 << 8          # 0x200  packed 24-bit R,G,B
RK_FORMAT_YCbCr_420_SP = 0xE << 8     # 0xe00  NV12 (Y plane + interleaved CbCr)

# --- IM_STATUS (im2d_api/im2d_type.h). SUCCESS == 1. ------------------------- #
IM_STATUS_SUCCESS = 1
IM_SYNC = 1 << 19


# librga v1.10.5's `rga_buffer_t` is 96 bytes. We NEVER read/write its fields
# from Python -- librga's own `wrapbuffer_*_t` constructors populate it and we
# hand it straight back to `imcvtcolor_t`. Modelling it as a fixed-size opaque
# blob keeps the ctypes ABI (by-value arg and sret return) correct regardless of
# the exact field layout, which has drifted between librga releases.
_RGA_BUFFER_SIZE = 96
_RGA_INFORMATION_VERSION = 1
_VERIFIED_API_VERSION = "1.10.5_[11]"


class _rga_buffer_t(Structure):
    """Opaque 96-byte mirror of im2d `rga_buffer_t` (librga v1.10.5).

    Deliberately field-less: the struct is only ever produced by librga's
    `wrapbuffer_fd_t` / `wrapbuffer_virtualaddr_t` and consumed by
    `imcvtcolor_t`. Treating it as an opaque blob avoids depending on a field
    layout that changes across librga versions."""

    _fields_ = [("_opaque", c_char * _RGA_BUFFER_SIZE)]


class _im_rect(Structure):
    """im2d `im_rect` (im2d_api/im2d_type.h): four ints {x, y, width, height}.

    Unlike `rga_buffer_t`, this struct's layout has been stable across librga
    releases (four `int`s), so we model it explicitly and build it ourselves --
    it is what selects the crop sub-rectangle passed to `improcess_t`.
    """

    _fields_ = [("x", c_int), ("y", c_int), ("width", c_int), ("height", c_int)]


def _load_librga() -> Optional[ctypes.CDLL]:
    # The RV1126B firmware keeps Rockchip media libraries in the immutable OEM
    # tree.  A normal interactive/system Python process deliberately has no
    # LD_LIBRARY_PATH, so a bare ``ctypes.CDLL("librga.so")`` cannot discover
    # the shipped library even though the public RgaContext capability is
    # installed.  Probe the platform-owned absolute path first; keep the
    # soname/find_library fallbacks for host tests and alternate deployments.
    # The querystring ABI gate in RgaNV12ToRGB still runs before any unsafe
    # by-value rga_buffer_t signature is bound.
    candidates = [
        "/oem/usr/lib/librga.so",
        "/oem/usr/lib/librga.so.2",
        "/oem/usr/lib/librga.so.1",
        "librga.so.2",
        "librga.so",
        "librga.so.1",
    ]
    for cand in candidates:
        try:
            return ctypes.CDLL(cand)
        except OSError:
            continue
    found = ctypes.util.find_library("rga")
    if found:
        try:
            return ctypes.CDLL(found)
        except OSError:
            return None
    return None


class RgaNV12ToRGB:
    """Zero-copy NV12(dma-buf) -> packed RGB converter backed by librga.

    Construct it once (it binds the symbols); call `convert()` per frame. Any
    hardware/ABI problem is surfaced as an exception so the caller can latch to
    the software path -- this class NEVER silently returns wrong pixels: it
    checks the `imcvtcolor_t` IM_STATUS return code on every call.
    """

    def __init__(self) -> None:
        if os.environ.get("RECAMERA_RGA", "").strip() == "0":
            raise RuntimeError("RGA disabled via RECAMERA_RGA=0")
        lib = _load_librga()
        if lib is None:
            raise OSError("librga.so not found")
        # The functions below return/pass rga_buffer_t by value.  Binding them
        # against a library with a differently-sized struct can corrupt memory
        # before Python has an exception to catch.  querystring(int) is a
        # stable scalar/pointer call, so use it as a fail-closed ABI gate before
        # assigning any by-value signatures.  This SDK ships and verifies the
        # 1.10.5_[11], 96-byte aarch64 ABI only; a different build needs a
        # compiled C shim built against that build's own headers.
        query = getattr(lib, "querystring", None)
        if query is None:
            raise RuntimeError(
                "librga has no querystring ABI probe; refusing unsafe ctypes binding")
        query.restype = c_char_p
        query.argtypes = [c_int]
        raw_version = query(_RGA_INFORMATION_VERSION)
        try:
            version_info = (raw_version or b"").decode("utf-8", "replace")
        except AttributeError as exc:
            raise RuntimeError("librga returned an invalid version string") from exc
        expected = f"RGA_api version       : v{_VERIFIED_API_VERSION}"
        if expected not in version_info:
            compact_expected = f"v{_VERIFIED_API_VERSION}"
            if compact_expected not in version_info:
                raise RuntimeError(
                    "unsupported librga ctypes ABI; expected "
                    f"{_VERIFIED_API_VERSION}/rga_buffer_t={_RGA_BUFFER_SIZE}, "
                    f"probe returned {version_info!r}")
        self.version_info = version_info
        # NO importbuffer_fd / releasebuffer_handle: on this librga
        # importbuffer_fd takes an im_handle_param_t* (not (int fd, int size)),
        # and mixing an imported handle with a virtual address in one op is
        # rejected. We use the no-handles wrapbuffer path instead.
        #
        # wrapbuffer_fd_t(int fd, int w, int h, int wstride, int hstride,
        #                 int format) -> rga_buffer_t  (returned by value / sret)
        lib.wrapbuffer_fd_t.restype = _rga_buffer_t
        lib.wrapbuffer_fd_t.argtypes = [c_int, c_int, c_int, c_int, c_int, c_int]
        # wrapbuffer_virtualaddr_t(void* vir_addr, int w, int h, int wstride,
        #                          int hstride, int format) -> rga_buffer_t
        lib.wrapbuffer_virtualaddr_t.restype = _rga_buffer_t
        lib.wrapbuffer_virtualaddr_t.argtypes = [
            c_void_p, c_int, c_int, c_int, c_int, c_int,
        ]
        # imcvtcolor_t(src, dst, sfmt, dfmt, mode, sync) -> IM_STATUS
        # src/dst are the 96-byte rga_buffer_t passed BY VALUE.
        lib.imcvtcolor_t.restype = c_int
        lib.imcvtcolor_t.argtypes = [
            _rga_buffer_t, _rga_buffer_t, c_int, c_int, c_int, c_int,
        ]
        # imresize_t keeps the source and destination pixel format unchanged.
        # We use it for NV12->NV12 downscale, then imcvtcolor_t for the small
        # NV12->RGB conversion.  Keeping the formats equal here is important:
        # librga's resize helper is not a general cross-format blit on all
        # firmware builds.  Older librga releases may not export this helper;
        # ``resize_nv12_to_rgb`` then raises and the caller falls back to the
        # known-good full-resolution conversion path.
        self._resize = getattr(lib, "imresize_t", None)
        if self._resize is not None:
            self._resize.restype = c_int
            self._resize.argtypes = [
                _rga_buffer_t, _rga_buffer_t, c_double, c_double, c_int, c_int,
            ]
        # improcess_t is the general im2d entry that crops a source sub-rect
        # (srect), scales it into a destination sub-rect (drect) AND converts the
        # pixel format in ONE hardware op -- exactly "NV12 dma-buf ROI -> resized
        # RGB". It is OPTIONAL: some older RV1126B librga builds do not export it,
        # in which case `can_crop()` is False and the caller keeps the numpy crop.
        #   improcess(rga_buffer_t src, rga_buffer_t dst, rga_buffer_t pat,
        #             im_rect srect, im_rect drect, im_rect prect, int usage)
        # src/dst/pat are the 96-byte rga_buffer_t passed BY VALUE; the rects are
        # the 16-byte _im_rect passed BY VALUE.
        self._improcess = getattr(lib, "improcess_t", None)
        self._letterbox_process = self._improcess
        if self._letterbox_process is None:
            # The shipping 1.10.5_[11] library exports the C spelling below,
            # declared with these same seven arguments in im2d_single.h.
            # Never bind a mangled C++ overload or infer an importbuffer ABI.
            # This additional symbol is for the new letterbox path only:
            # enabling it must not change existing hw-roi capability/fallback.
            self._letterbox_process = getattr(lib, "improcess", None)
        for process in (self._improcess, self._letterbox_process):
            if process is not None:
                process.restype = c_int
                process.argtypes = [
                    _rga_buffer_t, _rga_buffer_t, _rga_buffer_t,
                    _im_rect, _im_rect, _im_rect, c_int,
                ]
        self._fill = getattr(lib, "imfill_t", None)
        if self._fill is not None:
            # im2d_single.h: imfill_t(rga_buffer_t, im_rect, int color, int sync)
            self._fill.restype = c_int
            self._fill.argtypes = [_rga_buffer_t, _im_rect, c_int, c_int]
        self._lib = lib
        # Bounded to one geometry, not a cache accumulating every source size.
        # Hold the lock until both synchronous operations finish: two callers
        # must never overwrite this scratch while the other is converting it.
        self._letterbox_lock = threading.Lock()
        self._letterbox_nv12 = None

    def can_crop(self) -> bool:
        """True when the librga build exports `improcess_t` (the ROI-crop path).

        Lets the caller probe ONCE whether hardware dma-buf ROI cropping is
        possible before committing an app to the ``hw-roi`` frame mode; a False
        keeps it on the numpy crop with no error."""
        return self._improcess is not None

    def can_resize(self) -> bool:
        """Return whether this librga build exports ``imresize_t``.

        ``resize_nv12_to_rgb`` remains present on the Python object for API
        compatibility even when an older device library lacks the native
        symbol.  Callers therefore must query this method instead of treating
        Python method presence as proof of hardware support.
        """

        return self._resize is not None

    def can_letterbox(self, *, dma_output: bool = False) -> bool:
        """Whether reusable RGB letterboxing is supported by this library.

        DMA output also needs hardware fill so the CPU never touches a DMA
        mapping without the owner's cache synchronization protocol.
        """
        return (self._resize is not None and self._letterbox_process is not None
                and (not dma_output or self._fill is not None))

    def letterbox_nv12_to_rgb(
        self, fd: int, width: int, height: int, y_stride: int, y_vstride: int,
        dst_width: int, dst_height: int, dst_window, *,
        out: Optional[np.ndarray] = None, dst_fd: Optional[int] = None,
        dst_w_stride: Optional[int] = None, dst_h_stride: Optional[int] = None,
        pad_value: int = 114,
    ) -> Optional[np.ndarray]:
        """Resize NV12 then convert directly into a caller-owned RGB canvas.

        ``dst_window=(left, top, right, bottom)`` is already rounded by the
        caller's letterbox geometry. No coordinate or color-order changes are
        made here. As in ``resize_nv12_to_rgb``, scaling happens in NV12 before
        converting to packed RGB. Only the model-sized NV12 scratch is cached.

        Supply either a writable, C-contiguous uint8 ``out[H,W,3]`` or a
        borrowed RGB ``dst_fd``. With neither, allocate a fresh output. DMA
        strides are in RGB *pixels* and rows, not bytes; the owner must provide
        at least ``dst_w_stride * dst_h_stride * 3`` backing bytes and keep the
        fd alive throughout this call. The destination begins at fd offset zero;
        a tensor suballocation at a nonzero byte offset requires a fallback.
        Array output must be tightly packed.
        DMA output returns None; array output returns the supplied/new array.
        Every RGA operation is synchronous. There is no fd ownership transfer,
        mapping, imported handle, or CPU read/write of the DMA destination.

        Reused outputs are overwritten on the next call. Consumers retaining
        them must copy or finish consuming them before reuse. A failure can
        leave an incomplete output; the caller must discard it and fall back.
        """
        dma_output = dst_fd is not None
        if not self.can_letterbox(dma_output=dma_output):
            raise RuntimeError("librga reusable letterbox symbols unavailable")
        if dma_output and out is not None:
            raise ValueError("supply either letterbox out or dst_fd, not both")

        def integer(value, name, minimum=1):
            try:
                result = operator.index(value)
            except TypeError as exc:
                raise ValueError(f"{name} must be an integer") from exc
            if isinstance(value, (bool, np.bool_)) or not minimum <= result <= 0x7fffffff:
                raise ValueError(f"invalid letterbox {name}")
            return result

        fd = integer(fd, "fd", 0)
        width, height = integer(width, "width"), integer(height, "height")
        y_stride = integer(y_stride, "y_stride")
        y_vstride = integer(y_vstride, "y_vstride")
        dst_width = integer(dst_width, "dst_width")
        dst_height = integer(dst_height, "dst_height")
        if y_stride < width or y_vstride < height:
            raise ValueError("NV12 strides must cover the visible source")
        if (width | height | y_stride | y_vstride) & 1:
            raise ValueError("NV12 geometry and strides must be even")
        try:
            left, top, right, bottom = dst_window
        except (TypeError, ValueError) as exc:
            raise ValueError("dst_window must contain four coordinates") from exc
        left, top, right, bottom = (
            integer(value, "dst_window coordinate", 0)
            for value in (left, top, right, bottom))
        if not (left < right <= dst_width and top < bottom <= dst_height):
            raise ValueError("dst_window must lie inside the RGB canvas")
        small_w, small_h = right - left, bottom - top
        if (small_w | small_h) & 1:
            raise ValueError("NV12 resize geometry must be even")
        pad_value = integer(pad_value, "pad_value", 0)
        if pad_value > 255:
            raise ValueError("pad_value must fit uint8")
        wstride = dst_width if dst_w_stride is None else integer(dst_w_stride, "dst_w_stride")
        hstride = dst_height if dst_h_stride is None else integer(dst_h_stride, "dst_h_stride")
        if wstride < dst_width or hstride < dst_height:
            raise ValueError("RGB strides must cover the visible destination")
        if not dma_output and (wstride != dst_width or hstride != dst_height):
            raise ValueError("array letterbox output must be tightly packed")
        if dma_output:
            dst_fd = integer(dst_fd, "dst_fd", 0)
            if dst_fd == fd:
                raise ValueError("source and destination DMA fds must differ")
        elif out is not None:
            if (not isinstance(out, np.ndarray)
                    or out.shape != (dst_height, dst_width, 3)
                    or out.dtype != np.uint8
                    or not out.flags.c_contiguous or not out.flags.writeable):
                raise ValueError("letterbox out must be writable contiguous uint8 [H,W,3]")

        with self._letterbox_lock:
            scratch_shape = (small_h * 3 // 2, small_w)
            if self._letterbox_nv12 is None or self._letterbox_nv12.shape != scratch_shape:
                self._letterbox_nv12 = np.empty(scratch_shape, dtype=np.uint8)
            nv12 = self._letterbox_nv12
            src = self._lib.wrapbuffer_fd_t(
                fd, width, height, y_stride, y_vstride, RK_FORMAT_YCbCr_420_SP)
            small = self._lib.wrapbuffer_virtualaddr_t(
                nv12.ctypes.data_as(c_void_p), small_w, small_h,
                small_w, small_h, RK_FORMAT_YCbCr_420_SP)
            rc = self._resize(src, small, 0.0, 0.0, 0, 1)
            if rc != IM_STATUS_SUCCESS:
                raise RuntimeError("RGA imresize_t failed: IM_STATUS=%d" % rc)

            if dma_output:
                dst = self._lib.wrapbuffer_fd_t(
                    dst_fd, dst_width, dst_height, wstride, hstride,
                    RK_FORMAT_RGB_888)
                # RGB888 drops the alpha byte; equal RGB components preserve
                # the established neutral padding regardless of byte naming.
                color = pad_value | (pad_value << 8) | (pad_value << 16)
                rc = self._fill(dst, _im_rect(0, 0, dst_width, dst_height), color, 1)
                if rc != IM_STATUS_SUCCESS:
                    raise RuntimeError("RGA imfill_t failed: IM_STATUS=%d" % rc)
            else:
                if out is None:
                    out = np.empty((dst_height, dst_width, 3), dtype=np.uint8)
                out.fill(pad_value)
                dst = self._lib.wrapbuffer_virtualaddr_t(
                    out.ctypes.data_as(c_void_p), dst_width, dst_height,
                    wstride, hstride, RK_FORMAT_RGB_888)
            rc = self._letterbox_process(
                small, dst, _rga_buffer_t(),
                _im_rect(0, 0, small_w, small_h),
                _im_rect(left, top, small_w, small_h), _im_rect(), IM_SYNC)
            if rc != IM_STATUS_SUCCESS:
                raise RuntimeError("RGA improcess letterbox failed: IM_STATUS=%d" % rc)
        return out

    def convert(self, fd: int, width: int, height: int,
                y_stride: int, y_vstride: int) -> np.ndarray:
        """NV12 dma-buf (`fd`) -> a fresh, contiguous [H, W, 3] uint8 RGB array.

        `y_stride`/`y_vstride` are plane[0]'s byte stride and padded row count
        (from the frame header -- never derive them from width/height). The
        returned array is a standalone copy, safe to hold after the source frame
        is released.
        """
        lib = self._lib
        # Destination: a tight CPU RGB buffer (wstride == width). RGA writes into
        # it via a virtual-address rga_buffer_t -- no dma-buf import for either
        # side, so there is nothing to release.
        out = np.empty((height, width, 3), dtype=np.uint8)

        # Source: wrap the borrowed NV12 dma-buf fd directly (no import). Y plane
        # stride/vstride come from the frame header (never derived from w/h).
        src = lib.wrapbuffer_fd_t(
            int(fd), width, height, y_stride, y_vstride,
            RK_FORMAT_YCbCr_420_SP,
        )
        # Destination: wrap the CPU RGB buffer's virtual address, tight (no pad).
        dst = lib.wrapbuffer_virtualaddr_t(
            out.ctypes.data_as(c_void_p), width, height, width, height,
            RK_FORMAT_RGB_888,
        )

        rc = lib.imcvtcolor_t(
            src, dst, RK_FORMAT_YCbCr_420_SP, RK_FORMAT_RGB_888, 0, 1,
        )
        if rc != IM_STATUS_SUCCESS:
            raise RuntimeError("RGA imcvtcolor_t failed: IM_STATUS=%d" % rc)
        return out

    def resize_nv12_to_rgb(self, fd: int, width: int, height: int,
                           y_stride: int, y_vstride: int,
                           dst_width: int, dst_height: int) -> np.ndarray:
        """Downscale NV12 in RGA, then convert the small image to RGB.

        The intermediate NV12 buffer is model-sized, so Python never receives
        a full-resolution RGB image.  ``imresize_t`` is deliberately used only
        for NV12->NV12 (same format on both sides); this is the portable subset
        supported by the RV1126B librga builds.  The returned RGB array is
        contiguous and owns its memory.
        """
        if self._resize is None:
            raise RuntimeError("librga imresize_t symbol unavailable")
        width, height = int(width), int(height)
        dst_width, dst_height = int(dst_width), int(dst_height)
        if min(width, height, dst_width, dst_height) <= 0:
            raise ValueError("invalid RGA resize geometry")
        # NV12 requires even dimensions.  Refuse rather than silently changing
        # the aspect ratio; the caller can use the conservative fallback.
        if (width | height | dst_width | dst_height) & 1:
            raise ValueError("NV12 resize geometry must be even")

        # Tight model-sized NV12 scratch (Y rows + interleaved UV rows).
        nv12 = np.empty((dst_height * 3 // 2, dst_width), dtype=np.uint8)
        src = self._lib.wrapbuffer_fd_t(
            int(fd), width, height, int(y_stride), int(y_vstride),
            RK_FORMAT_YCbCr_420_SP,
        )
        small = self._lib.wrapbuffer_virtualaddr_t(
            nv12.ctypes.data_as(c_void_p), dst_width, dst_height,
            dst_width, dst_height, RK_FORMAT_YCbCr_420_SP,
        )
        # Destination geometry is authoritative; librga computes the scale
        # when fx/fy are zero.  This avoids relying on interpolation enum values
        # that changed between im2d header revisions.  Mode 0 is the documented
        # default interpolation on the shipping RV1126B build.
        rc = self._resize(src, small, 0.0, 0.0, 0, 1)
        if rc != IM_STATUS_SUCCESS:
            raise RuntimeError("RGA imresize_t failed: IM_STATUS=%d" % rc)

        out = np.empty((dst_height, dst_width, 3), dtype=np.uint8)
        dst = self._lib.wrapbuffer_virtualaddr_t(
            out.ctypes.data_as(c_void_p), dst_width, dst_height,
            dst_width, dst_height, RK_FORMAT_RGB_888,
        )
        rc = self._lib.imcvtcolor_t(
            small, dst, RK_FORMAT_YCbCr_420_SP, RK_FORMAT_RGB_888, 0, 1,
        )
        if rc != IM_STATUS_SUCCESS:
            raise RuntimeError("RGA small imcvtcolor_t failed: IM_STATUS=%d" % rc)
        return out

    def crop_nv12_to_rgb(self, fd: int, width: int, height: int,
                         y_stride: int, y_vstride: int,
                         src_rect, dst_size: int, dst_window=None,
                         out: Optional[np.ndarray] = None,
                         pad_value: int = 114) -> np.ndarray:
        """Crop `src_rect` out of the NV12 dma-buf and scale it to RGB in ONE op.

        Reads the borrowed camera dma-buf directly (near-zero CPU), unlike the
        numpy `crop_square_roi` which needs a full-resolution RGB frame first.
        This is the per-ROI hot path for cascade apps (face/facemesh/ppocr) under
        the ``hw-roi`` frame mode.

        `src_rect`  = (sx1, sy1, sx2, sy2) in NV12 pixels (the square clipped to
                      the frame). It is aligned DOWN to even bounds because NV12
                      chroma is 2x2-subsampled -- an odd crop origin/size would
                      shift the color plane.
        `dst_size`  = side of the square RGB output canvas.
        `dst_window`= (dx1, dy1, dx2, dy2) sub-window of the output the crop is
                      scaled into; the rest of the canvas is filled `pad_value`
                      (gray, matching the CPU letterbox border). None -> fill the
                      whole canvas.
        `out`       = optional preallocated [dst_size, dst_size, 3] uint8 buffer
                      to reuse across calls (avoids per-ROI reallocation). It is
                      overwritten in place; hand back a COPY if you must retain it.

        Returns the `out` canvas (the caller owns the buffer it passed in, or a
        fresh one). Raises on a missing symbol or a non-SUCCESS IM_STATUS so the
        caller can fall back.
        """
        if self._improcess is None:
            raise RuntimeError("librga improcess_t symbol unavailable")
        dst_size = int(dst_size)
        if dst_size <= 0:
            raise ValueError("invalid RGA crop dst_size")
        if out is None:
            out = np.full((dst_size, dst_size, 3), int(pad_value), dtype=np.uint8)
        else:
            if out.shape != (dst_size, dst_size, 3) or out.dtype != np.uint8:
                raise ValueError("crop `out` buffer must be uint8 "
                                 "[dst_size, dst_size, 3]")
            out[:] = int(pad_value)

        sx1, sy1, sx2, sy2 = (int(src_rect[0]), int(src_rect[1]),
                              int(src_rect[2]), int(src_rect[3]))
        # Align the source rect to even bounds (NV12 chroma subsampling).
        sx1 &= ~1
        sy1 &= ~1
        sw = (sx2 - sx1) & ~1
        sh = (sy2 - sy1) & ~1
        if sw < 2 or sh < 2:
            raise ValueError("degenerate RGA crop source rect")

        src = self._lib.wrapbuffer_fd_t(
            int(fd), int(width), int(height), int(y_stride), int(y_vstride),
            RK_FORMAT_YCbCr_420_SP,
        )
        dst = self._lib.wrapbuffer_virtualaddr_t(
            out.ctypes.data_as(c_void_p), dst_size, dst_size,
            dst_size, dst_size, RK_FORMAT_RGB_888,
        )
        srect = _im_rect(sx1, sy1, sw, sh)
        if dst_window is None:
            drect = _im_rect(0, 0, dst_size, dst_size)
        else:
            dx1, dy1, dx2, dy2 = (int(dst_window[0]), int(dst_window[1]),
                                  int(dst_window[2]), int(dst_window[3]))
            drect = _im_rect(dx1, dy1, max(1, dx2 - dx1), max(1, dy2 - dy1))
        empty = _rga_buffer_t()
        prect = _im_rect(0, 0, 0, 0)
        rc = self._improcess(src, dst, empty, srect, drect, prect, 0)
        if rc != IM_STATUS_SUCCESS:
            raise RuntimeError("RGA improcess_t crop failed: IM_STATUS=%d" % rc)
        return out


def try_open() -> Optional[RgaNV12ToRGB]:
    """Return an `RgaNV12ToRGB` if librga is usable, else None (never raises)."""
    try:
        return RgaNV12ToRGB()
    except Exception:
        return None

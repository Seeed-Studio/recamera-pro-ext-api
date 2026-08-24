"""reCamera Pro extension SDK -- thin ctypes wrapper over librecamera_ext.so.1.

Core facilities mirror the C ABI v1 without reimplementing wire protocols
(spec §0: "Python is a thin wrapper of the C library"):

  ResultSink     -- inject inference results        (rc_ext_result_*)
  FrameSource    -- receive zero-copy camera frames (rc_ext_frame_*)
  ProbeSource    -- observe built-in pipeline data  (rc_ext_probe_*)
  InferenceLease -- arbitrate external RKNN ownership

Result injection:

    from recamera_ext import ResultSink
    with ResultSink(source_id="face-app") as sink:
        sink.send_detections(pts_us=0, boxes=[(x1, y1, x2, y2, score, "label")])

Frame receiving (spec §2.5 "5 lines to the first frame"):

    from recamera_ext import FrameSource
    with FrameSource() as src:
        for frame in src:            # frame.array: zero-copy np.ndarray (Y plane)
            infer(frame.array)        # released automatically on the next iteration
"""

import ctypes
import ctypes.util
import logging
import math
import operator
import os
import threading
import weakref
from collections.abc import Mapping
from dataclasses import dataclass
from enum import IntEnum
from ctypes import (
    POINTER,
    Structure,
    byref,
    c_char_p,
    c_float,
    c_int,
    c_int32,
    c_size_t,
    c_ubyte,
    c_uint8,
    c_uint16,
    c_uint32,
    c_uint64,
    c_void_p,
)

try:  # normal package import
    from .buffer import BorrowedBuffer, PlaneLayout
    from .errors import (
        AcquireTimeoutError,
        AuthError,
        AuthenticationError,
        BackpressureError,
        BufferReleasedError,
        BusyError,
        CapabilityUnavailableError,
        ErrorCode,
        FormatError,
        FrameTimeoutError,
        HandleClosedError,
        InternalError,
        LibraryLoadError,
        RateLimitError,
        RecameraError,
        RecameraRuntimeError,
        ResourceBusyError,
        ResultTooLarge,
        UnknownNativeError,
        VersionError,
        error_from_rc,
    )
except (ImportError, ModuleNotFoundError):
    # Backward compatibility for repository tools/tests that load this exact
    # ``__init__.py`` by path under a non-package name.  Normal users never take
    # this branch.  Stable private cache names ensure buffer.py and this module
    # share the same exception class identities.
    import importlib.util as _importlib_util
    import os as _os
    import sys as _sys

    def _standalone_sibling(name):
        cache_name = f"_recamera_ext_standalone_{name}"
        cached = _sys.modules.get(cache_name)
        if cached is not None:
            return cached
        filename = _os.path.join(_os.path.dirname(__file__), f"{name}.py")
        spec = _importlib_util.spec_from_file_location(cache_name, filename)
        module = _importlib_util.module_from_spec(spec)
        _sys.modules[cache_name] = module
        spec.loader.exec_module(module)
        return module

    _errors_mod = _standalone_sibling("errors")
    _buffer_mod = _standalone_sibling("buffer")
    BorrowedBuffer = _buffer_mod.BorrowedBuffer
    PlaneLayout = _buffer_mod.PlaneLayout
    AcquireTimeoutError = _errors_mod.AcquireTimeoutError
    AuthError = _errors_mod.AuthError
    AuthenticationError = _errors_mod.AuthenticationError
    BackpressureError = _errors_mod.BackpressureError
    BufferReleasedError = _errors_mod.BufferReleasedError
    BusyError = _errors_mod.BusyError
    CapabilityUnavailableError = _errors_mod.CapabilityUnavailableError
    ErrorCode = _errors_mod.ErrorCode
    FormatError = _errors_mod.FormatError
    FrameTimeoutError = _errors_mod.FrameTimeoutError
    HandleClosedError = _errors_mod.HandleClosedError
    InternalError = _errors_mod.InternalError
    LibraryLoadError = _errors_mod.LibraryLoadError
    RateLimitError = _errors_mod.RateLimitError
    RecameraError = _errors_mod.RecameraError
    RecameraRuntimeError = _errors_mod.RecameraRuntimeError
    ResourceBusyError = _errors_mod.ResourceBusyError
    ResultTooLarge = _errors_mod.ResultTooLarge
    UnknownNativeError = _errors_mod.UnknownNativeError
    VersionError = _errors_mod.VersionError
    error_from_rc = _errors_mod.error_from_rc

_LOG = logging.getLogger(__name__)
_LOG.addHandler(logging.NullHandler())

__all__ = [
    "ResultSink",
    "ResultTooLarge",
    "ErrorCode",
    "RecameraError",
    "RecameraRuntimeError",
    "VersionError",
    "AuthenticationError",
    "AuthError",
    "BusyError",
    "ResourceBusyError",
    "FormatError",
    "BackpressureError",
    "RateLimitError",
    "InternalError",
    "UnknownNativeError",
    "CapabilityUnavailableError",
    "BufferReleasedError",
    "HandleClosedError",
    "AcquireTimeoutError",
    "FrameTimeoutError",
    "LibraryLoadError",
    "Box",
    "Classification",
    "Segmentation",
    "Tracking",
    "Point",
    "KeypointInstance",
    "FrameSource",
    "FrameLease",
    "Frame",
    "FrameConfig",
    "BorrowedBuffer",
    "PlaneLayout",
    "ProbeSource",
    "ProbeSample",
    "InferenceLease",
    "InferenceState",
    "InferenceStatus",
    "MaskControl",
    "MaskRect",
]

# ProbeSubscribeAck.subscribed_mask bits (mirror of RC_EXT_PROBE_MASK_*).
PROBE_MASK_PREPROC = 0x1
PROBE_MASK_NPU = 0x2
PROBE_MASK_POSTPROC = 0x4
PROBE_MASK_METRICS = 0x8

# TensorMeta.dtype -> numpy dtype string.
_PROBE_DTYPES = {
    0: "uint8",
    1: "int8",
    2: "uint16",
    3: "int16",
    4: "float32",
    5: "float16",
}

FOURCC_NV12 = 0x3231564E  # 'N','V','1','2' little-endian

_numpy = None
_C_INT_MAX = (1 << 31) - 1
_INFERENCE_ID_MAX_BYTES = 64
_INFERENCE_MAX_TIMEOUT_MS = 30_000
_inference_lease_instances = weakref.WeakSet()


def _inference_after_fork_child():
    """Close inherited broker fds without sending the parent's RELEASE."""

    for lease in list(_inference_lease_instances):
        try:
            lease._abandon_after_fork_child()
        except BaseException:
            # after-fork hooks must not make the child unusable.  A library that
            # predates the abandon ABI keeps the fd until process exit rather
            # than incorrectly revoking its parent's generation.
            pass


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_inference_after_fork_child)


def _np():
    """Lazily import numpy on first use and cache it. numpy stays an optional
    dependency (the SDK core is pure ctypes); this raises ImportError at call
    time only if a frame/probe array is actually requested without numpy."""
    global _numpy
    if _numpy is None:
        import numpy as np

        _numpy = np
    return _numpy


def _validate_timeout_ms(value, operation):
    """Return a C ``int``-safe timeout without coercion or truncation."""

    if isinstance(value, bool) or type(value).__name__ == "bool_":
        raise FormatError(
            f"{operation}: timeout_ms must be an integer, not bool",
            operation=operation,
            detail=f"timeout_ms={value!r}",
        )
    try:
        timeout = operator.index(value)
    except (TypeError, ValueError, OverflowError):
        raise FormatError(
            f"{operation}: timeout_ms must be an integer",
            operation=operation,
            detail=f"timeout_ms={value!r}",
        ) from None
    if not 0 <= timeout <= _C_INT_MAX:
        raise FormatError(
            f"{operation}: timeout_ms must be between 0 and {_C_INT_MAX}",
            operation=operation,
            detail=f"timeout_ms={timeout}",
        )
    return timeout


def _validate_inference_timeout_ms(value):
    """Validate the broker drain timeout without allowing ctypes truncation."""

    timeout = _validate_timeout_ms(value, "InferenceLease.__init__")
    if timeout > _INFERENCE_MAX_TIMEOUT_MS:
        raise FormatError(
            "InferenceLease.__init__: timeout_ms exceeds the broker maximum",
            operation="InferenceLease.__init__",
            detail=(f"timeout_ms={timeout}; maximum="
                    f"{_INFERENCE_MAX_TIMEOUT_MS}"),
        )
    return timeout


def _encode_inference_id(value, field):
    """Return one non-empty, NUL-free UTF-8 broker diagnostic identifier."""

    if not isinstance(value, str):
        raise FormatError(
            f"InferenceLease.__init__: {field} must be a string",
            operation="InferenceLease.__init__",
            detail=f"{field}_type={type(value).__name__}",
        )
    if not value or "\x00" in value:
        raise FormatError(
            f"InferenceLease.__init__: {field} must be non-empty and NUL-free",
            operation="InferenceLease.__init__",
            detail=f"field={field}",
        )
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise FormatError(
            f"InferenceLease.__init__: {field} is not valid UTF-8 text",
            operation="InferenceLease.__init__",
            detail=f"field={field}",
        ) from exc
    if len(encoded) > _INFERENCE_ID_MAX_BYTES:
        raise FormatError(
            f"InferenceLease.__init__: {field} exceeds the 64-byte ABI limit",
            operation="InferenceLease.__init__",
            detail=f"field={field}; utf8_bytes={len(encoded)}",
        )
    return encoded


def _exit_with_cleanup(cleanup, exc_type, exc_value, owner):
    """Run context cleanup without replacing an active body exception."""

    if exc_type is None:
        cleanup()
        return False
    try:
        cleanup()
    except BaseException as cleanup_error:
        _LOG.error(
            "%s cleanup failed while preserving the with-body exception",
            owner,
            exc_info=True,
        )
        add_note = getattr(exc_value, "add_note", None)
        if callable(add_note):
            try:
                add_note(
                    f"{owner} cleanup also failed: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
            except Exception:
                pass
    return False


class Box(Structure):
    """Mirror of rc_ext_box_t."""

    _fields_ = [
        ("x1", c_float),
        ("y1", c_float),
        ("x2", c_float),
        ("y2", c_float),
        ("score", c_float),
        ("label", c_char_p),
        ("class_id", c_int),
    ]


class Classification(Structure):
    """Mirror of rc_ext_class_t."""

    _fields_ = [
        ("score", c_float),
        ("class_id", c_int),
        ("label", c_char_p),
        ("has_box", c_int),
        ("x1", c_float),
        ("y1", c_float),
        ("x2", c_float),
        ("y2", c_float),
    ]


class Segmentation(Structure):
    """Mirror of rc_ext_seg_t (ROI box + row-major mask)."""

    _fields_ = [
        ("x1", c_float),
        ("y1", c_float),
        ("x2", c_float),
        ("y2", c_float),
        ("score", c_float),
        ("class_id", c_int),
        ("label", c_char_p),
        ("mask", c_char_p),
        ("mask_w", c_int),
        ("mask_h", c_int),
    ]


class Tracking(Structure):
    """Mirror of rc_ext_track_t."""

    _fields_ = [
        ("x1", c_float),
        ("y1", c_float),
        ("x2", c_float),
        ("y2", c_float),
        ("score", c_float),
        ("class_id", c_int),
        ("label", c_char_p),
        ("track_id", c_int),
    ]


class Point(Structure):
    """Mirror of rc_ext_point_t (a single keypoint)."""

    _fields_ = [
        ("x", c_float),
        ("y", c_float),
        ("score", c_float),
        ("keypoint_id", c_int),
    ]


class KeypointInstance(Structure):
    """Mirror of rc_ext_kpinstance_t (one detected object + its keypoints)."""

    _fields_ = [
        ("has_box", c_int),
        ("x1", c_float),
        ("y1", c_float),
        ("x2", c_float),
        ("y2", c_float),
        ("score", c_float),
        ("class_id", c_int),
        ("label", c_char_p),
        ("points", POINTER(Point)),
        ("n_points", c_size_t),
    ]


class _MaskRect(Structure):
    """Mirror of rc_ext_mask_rect_t (normalized [0,1] hardware-cover rect)."""

    _fields_ = [
        ("id", c_int),
        ("x", c_float),
        ("y", c_float),
        ("w", c_float),
        ("h", c_float),
    ]


class MaskRect:
    """A normalized hardware privacy-mask rectangle. id selects the slot
    ([0,6)); x/y/w/h are [0,1] fractions of frame width/height."""

    def __init__(self, id, x, y, w, h):
        self.id = id
        self.x = x
        self.y = y
        self.w = w
        self.h = h


class _Plane(Structure):
    _fields_ = [("offset", c_uint32), ("stride", c_uint32), ("vstride", c_uint32)]


class _FrameBuf(Structure):
    """Mirror of rc_ext_frame_buf_t (natural alignment, matches the C ABI)."""

    _fields_ = [
        ("seq", c_uint64),
        ("pts_us", c_uint64),
        ("width", c_uint32),
        ("height", c_uint32),
        ("fourcc", c_uint32),
        ("buf_size", c_uint32),
        ("flags", c_uint16),
        ("chn_id", c_uint8),
        ("n_planes", c_uint8),
        ("plane", _Plane * 3),
        ("fd", c_int),
        ("_base", c_void_p),
        ("_map_len", c_size_t),
    ]


class _Cfg(Structure):
    _fields_ = [
        ("width", c_uint32),
        ("height", c_uint32),
        ("fourcc", c_uint32),
        ("fps_divisor", c_uint32),
    ]


class _ProbeSample(Structure):
    """Mirror of rc_ext_probe_sample_t (natural alignment, matches the C ABI)."""

    _fields_ = [
        ("stage_id", c_char_p),
        ("seq", c_uint64),
        ("pts_us", c_uint64),
        ("payload", c_void_p),
        ("payload_len", c_size_t),
        ("flags", c_uint32),
        ("has_meta", c_int),
        ("shape", c_uint32 * 8),
        ("n_shape", c_uint32),
        ("dtype", c_uint32),
        ("layout", c_uint32),
        ("fourcc", c_uint32),
        ("width", c_uint32),
        ("height", c_uint32),
        ("stride", c_uint32),
        ("scale", c_float),
        ("zero_point", c_int),
        ("_fd", c_int),
        ("_base", c_void_p),
        ("_map_len", c_size_t),
        ("_pb", c_void_p),
    ]


class InferenceState(IntEnum):
    """Authoritative rkipc NPU-owner state from the lease broker."""

    BUILTIN = 0
    PREPARING_EXTERNAL = 1
    EXTERNAL_ACQUIRED = 2
    EXTERNAL_READY = 3
    NONE = 4
    FAULT = 5


class _InferenceStatus(Structure):
    """Mirror of the versioned ``rc_ext_inference_status_t`` C structure."""

    _fields_ = [
        ("struct_size", c_uint32),
        ("state", c_uint32),
        ("lease_id", c_uint64),
        ("epoch", c_uint64),
        ("generation", c_uint64),
        ("actual_fps", c_uint32),
        ("peer_pid", c_int32),
        ("builtin_enabled", c_uint8),
        ("handle_present", c_uint8),
        ("fallback_builtin", c_uint8),
        ("reserved0", c_uint8),
        ("builtin_state", ctypes.c_char * 16),
        ("source_id", ctypes.c_char * 64),
    ]


@dataclass(frozen=True)
class InferenceStatus:
    """Immutable snapshot returned by :meth:`InferenceLease.status`.

    ``state`` is normally :class:`InferenceState`.  A newer server may add an
    enum value before this Python package is upgraded; in that case the raw
    integer is preserved so status inspection remains forward-compatible.
    """

    state: InferenceState | int
    lease_id: int
    epoch: int
    generation: int
    actual_fps: int
    peer_pid: int
    builtin_enabled: bool
    handle_present: bool
    fallback_builtin: bool
    builtin_state: str
    source_id: str


class FrameConfig:
    """Optional subscription config; omit for the NPU-matched defaults."""

    def __init__(self, width=0, height=0, fourcc=0, fps_divisor=0):
        self.width = width
        self.height = height
        self.fourcc = fourcc
        self.fps_divisor = fps_divisor


def _bind(lib):
    # Result sink.
    lib.rc_ext_result_open.restype = c_void_p
    lib.rc_ext_result_open.argtypes = [c_char_p, POINTER(c_int)]
    lib.rc_ext_result_send_detections.restype = c_int
    lib.rc_ext_result_send_detections.argtypes = [c_void_p, c_uint64, POINTER(Box), c_size_t]
    lib.rc_ext_result_send_classification.restype = c_int
    lib.rc_ext_result_send_classification.argtypes = [c_void_p, c_uint64, POINTER(Classification), c_size_t]
    lib.rc_ext_result_send_segmentation.restype = c_int
    lib.rc_ext_result_send_segmentation.argtypes = [c_void_p, c_uint64, POINTER(Segmentation), c_size_t]
    lib.rc_ext_result_send_tracking.restype = c_int
    lib.rc_ext_result_send_tracking.argtypes = [c_void_p, c_uint64, POINTER(Tracking), c_size_t]
    lib.rc_ext_result_send_keypoints.restype = c_int
    lib.rc_ext_result_send_keypoints.argtypes = [c_void_p, c_uint64, POINTER(KeypointInstance), c_size_t]
    lib.rc_ext_result_close.restype = None
    lib.rc_ext_result_close.argtypes = [c_void_p]
    # Frame source.
    lib.rc_ext_frame_open.restype = c_void_p
    lib.rc_ext_frame_open.argtypes = [POINTER(_Cfg), POINTER(c_int)]
    lib.rc_ext_frame_geometry.restype = c_int
    lib.rc_ext_frame_geometry.argtypes = [c_void_p] + [POINTER(c_uint32)] * 5
    lib.rc_ext_frame_next.restype = c_int
    lib.rc_ext_frame_next.argtypes = [c_void_p, POINTER(_FrameBuf), c_int]
    lib.rc_ext_frame_map.restype = c_void_p
    lib.rc_ext_frame_map.argtypes = [c_void_p, POINTER(_FrameBuf)]
    lib.rc_ext_frame_release.restype = None
    lib.rc_ext_frame_release.argtypes = [c_void_p, POINTER(_FrameBuf)]
    lib.rc_ext_frame_close.restype = None
    lib.rc_ext_frame_close.argtypes = [c_void_p]
    # Probe source (optional -- older libs may lack these symbols).
    if hasattr(lib, "rc_ext_probe_open"):
        lib.rc_ext_probe_open.restype = c_void_p
        lib.rc_ext_probe_open.argtypes = [POINTER(c_char_p), c_size_t, c_uint32, POINTER(c_int)]
        lib.rc_ext_probe_info.restype = c_int
        lib.rc_ext_probe_info.argtypes = [c_void_p, POINTER(c_uint32), POINTER(c_uint32)]
        lib.rc_ext_probe_next.restype = c_int
        lib.rc_ext_probe_next.argtypes = [c_void_p, POINTER(_ProbeSample), c_int]
        lib.rc_ext_probe_release.restype = None
        lib.rc_ext_probe_release.argtypes = [c_void_p, POINTER(_ProbeSample)]
        lib.rc_ext_probe_close.restype = None
        lib.rc_ext_probe_close.argtypes = [c_void_p]
    # Connection-lifetime NPU ownership (optional -- requires broker firmware).
    # Once the group marker exists every method is mandatory: accepting a
    # partially updated library would turn a safety boundary into a runtime
    # AttributeError after an RKNN context has already been created.
    if hasattr(lib, "rc_ext_inference_lease_open"):
        lib.rc_ext_inference_lease_open.restype = c_void_p
        lib.rc_ext_inference_lease_open.argtypes = [
            c_char_p,
            c_char_p,
            c_uint32,
            c_int,
            POINTER(c_int),
        ]
        lib.rc_ext_inference_lease_ready.restype = c_int
        lib.rc_ext_inference_lease_ready.argtypes = [c_void_p]
        lib.rc_ext_inference_lease_set_fallback.restype = c_int
        lib.rc_ext_inference_lease_set_fallback.argtypes = [c_void_p, c_int]
        lib.rc_ext_inference_lease_status.restype = c_int
        lib.rc_ext_inference_lease_status.argtypes = [
            c_void_p,
            POINTER(_InferenceStatus),
        ]
        lib.rc_ext_inference_lease_alive.restype = c_int
        lib.rc_ext_inference_lease_alive.argtypes = [c_void_p]
        lib.rc_ext_inference_lease_close.restype = None
        lib.rc_ext_inference_lease_close.argtypes = [c_void_p]
        lib.rc_ext_inference_lease_abandon_after_fork.restype = None
        lib.rc_ext_inference_lease_abandon_after_fork.argtypes = [c_void_p]
    # Hardware privacy-mask control (optional -- older libs may lack these).
    if hasattr(lib, "rc_ext_mask_open"):
        lib.rc_ext_mask_open.restype = c_void_p
        lib.rc_ext_mask_open.argtypes = [POINTER(c_int)]
        lib.rc_ext_mask_set.restype = c_int
        lib.rc_ext_mask_set.argtypes = [c_void_p, POINTER(_MaskRect), c_size_t, POINTER(c_int)]
        lib.rc_ext_mask_update.restype = c_int
        lib.rc_ext_mask_update.argtypes = [c_void_p, POINTER(_MaskRect)]
        lib.rc_ext_mask_clear.restype = c_int
        lib.rc_ext_mask_clear.argtypes = [c_void_p]
        lib.rc_ext_mask_query.restype = c_int
        lib.rc_ext_mask_query.argtypes = [c_void_p, POINTER(_MaskRect), c_size_t]
        lib.rc_ext_mask_close.restype = None
        lib.rc_ext_mask_close.argtypes = [c_void_p]
    return lib


def _load(path=None):
    """Load and bind the native library, retaining useful diagnostics.

    ``ctypes`` otherwise leaves users with only the final generic "not found"
    message even when a candidate existed but was ABI-incompatible.  Failed
    attempts are logged at DEBUG and summarized in ``LibraryLoadError.detail``;
    the default configuration emits no log output.
    """

    candidates = ([path] if path else []) + ["librecamera_ext.so.1", "librecamera_ext.so"]
    # Keep search order while avoiding a repeated explicit/default candidate.
    candidates = list(dict.fromkeys(candidates))
    failures = []
    for cand in candidates:
        try:
            return _bind(ctypes.CDLL(cand))
        except (OSError, AttributeError) as exc:
            failures.append((cand, exc))
            _LOG.debug("native library candidate %r rejected: %s", cand, exc)
    found = ctypes.util.find_library("recamera_ext")
    if found and found not in candidates:
        try:
            return _bind(ctypes.CDLL(found))
        except (OSError, AttributeError) as exc:
            failures.append((found, exc))
            _LOG.debug("find_library candidate %r rejected: %s", found, exc)
    detail = "; ".join(f"{cand}: {exc}" for cand, exc in failures)
    if not detail:
        detail = "no candidate found by ctypes.util.find_library"
    raise LibraryLoadError(
        operation="load librecamera_ext.so.1",
        detail=detail,
    )


class _Handle:
    """Lifecycle mixin for an object owning a C handle in ``self._h``.

    Subclasses set ``_close_cfn`` (the lib close-function attribute name) and
    may override ``_on_close()`` for teardown that must run before the handle
    is closed. Provides ``close()`` plus the context-manager / ``__del__``
    protocol."""

    _close_cfn = None

    def _on_close(self):
        pass

    def close(self):
        h = getattr(self, "_h", None)
        if not h:
            return False
        try:
            self._on_close()
        finally:
            try:
                getattr(self._lib, self._close_cfn)(h)
            finally:
                self._h = None
        _LOG.debug("closed native handle %s", type(self).__name__)
        return True

    @property
    def closed(self):
        """Whether the owning native handle has already been closed."""

        return not bool(getattr(self, "_h", None))

    def _ensure_open(self):
        if self.closed:
            raise HandleClosedError(
                f"{type(self).__name__} is closed",
                operation=type(self).__name__,
            )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, _traceback):
        return _exit_with_cleanup(
            self.close,
            exc_type,
            exc_value,
            type(self).__name__,
        )

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class InferenceLease(_Handle):
    """Own rkipc's crash-safe, connection-lifetime external NPU lease.

    Construction performs the broker ``ACQUIRE`` transaction and does not
    return until rkipc has drained its built-in RKNN handle.  Keep this object
    alive for the complete lifetime of every external RKNN context protected by
    it.  Call :meth:`ready` only after model/runtime initialization succeeds,
    and call :meth:`alive` immediately before each inference admission.

    The native connection is the lease.  :meth:`close`/:meth:`release` is
    idempotent; process death also closes the connection and lets rkipc reclaim
    ownership.  A lease inherited through ``fork()`` cannot safely be operated
    by the child and is rejected locally.

    Parameters:
        app_id: Non-empty diagnostic application id (UTF-8, at most 64 bytes).
        instance_id: Non-empty diagnostic instance id (same limit).  Defaults
            to the creating process id.
        timeout_ms: Built-in drain deadline.  Zero selects the server default;
            the current ABI accepts at most 30 seconds.
        fallback_builtin: Restore built-in inference when the connection is
            released or lost.
        lib_path: Optional explicit ``librecamera_ext`` path.
    """

    _close_cfn = "rc_ext_inference_lease_close"

    def __init__(
        self,
        app_id="python",
        instance_id=None,
        timeout_ms=0,
        fallback_builtin=True,
        lib_path=None,
    ):
        if instance_id is None:
            instance_id = f"pid-{os.getpid()}"
        app_id_bytes = _encode_inference_id(app_id, "app_id")
        instance_id_bytes = _encode_inference_id(instance_id, "instance_id")
        timeout = _validate_inference_timeout_ms(timeout_ms)
        self._lock = threading.RLock()
        self._owner_pid = os.getpid()
        self._ready = False
        self._h = None
        self._lib = _load(lib_path)
        if not hasattr(self._lib, "rc_ext_inference_lease_open"):
            raise CapabilityUnavailableError(
                "librecamera_ext lacks inference-lease broker support",
                operation="rc_ext_inference_lease_open",
            )
        err = c_int(0)
        self._h = self._lib.rc_ext_inference_lease_open(
            app_id_bytes,
            instance_id_bytes,
            c_uint32(timeout),
            c_int(1 if fallback_builtin else 0),
            byref(err),
        )
        if not self._h:
            raise error_from_rc(
                "rc_ext_inference_lease_open",
                err.value or -int(ErrorCode.EINTERNAL),
                detail=(f"app_id={app_id!r}; instance_id={instance_id!r}; "
                        f"timeout_ms={timeout}"),
            )
        self.app_id = app_id
        self.instance_id = instance_id
        self.timeout_ms = timeout
        self.fallback_builtin = bool(fallback_builtin)
        _inference_lease_instances.add(self)
        _LOG.info(
            "acquired inference lease app_id=%s instance_id=%s",
            self.app_id,
            self.instance_id,
        )

    @property
    def acquired(self):
        """Whether this process still owns an open local lease handle.

        This is local lifecycle state.  Use :meth:`alive` for an authoritative
        non-blocking connection check.
        """

        return not self.closed and self._owner_pid == os.getpid()

    def _ensure_owner_open(self, operation):
        if self._owner_pid != os.getpid():
            raise HandleClosedError(
                "an inference lease inherited through fork cannot be reused",
                operation=operation,
                detail=(f"owner_pid={self._owner_pid}; "
                        f"current_pid={os.getpid()}"),
            )
        self._ensure_open()

    def _checked_call(self, operation, *args):
        with self._lock:
            self._ensure_owner_open(operation)
            rc = int(getattr(self._lib, operation)(self._h, *args))
            if rc != 0:
                raise error_from_rc(operation, rc)
            return rc

    def ready(self):
        """Mark successful external model/runtime initialization at rkipc."""

        with self._lock:
            self._ensure_owner_open("rc_ext_inference_lease_ready")
            if self._ready:
                return False
            self._checked_call("rc_ext_inference_lease_ready")
            self._ready = True
        _LOG.debug(
            "marked inference lease ready app_id=%s instance_id=%s",
            self.app_id,
            self.instance_id,
        )
        return True

    def _abandon_after_fork_child(self):
        """Child-only close-without-RELEASE used by ``register_at_fork``.

        The native function is deliberately mutex-free because another thread
        may have owned both the Python and C locks when ``fork()`` occurred.
        Older broker libraries lack this symbol; fail-safe behavior then keeps
        the inherited descriptor until child exit instead of revoking the
        parent's generation.
        """

        h = getattr(self, "_h", None)
        if not h or getattr(self, "_owner_pid", os.getpid()) == os.getpid():
            return False
        abandon = getattr(
            getattr(self, "_lib", None),
            "rc_ext_inference_lease_abandon_after_fork",
            None,
        )
        if abandon is None:
            return False
        # Invalidate Python state first.  Even a malicious/fault-injected test
        # double cannot make __del__ follow up with RELEASE in this child.
        self._h = None
        self._ready = False
        abandon(h)
        return True

    def set_fallback(self, fallback_builtin):
        """Change whether built-in inference is restored on disconnect."""

        enabled = bool(fallback_builtin)
        self._checked_call(
            "rc_ext_inference_lease_set_fallback",
            c_int(1 if enabled else 0),
        )
        self.fallback_builtin = enabled

    def status(self):
        """Fetch an immutable authoritative broker status snapshot."""

        native = _InferenceStatus()
        native.struct_size = ctypes.sizeof(native)
        self._checked_call("rc_ext_inference_lease_status", byref(native))
        raw_state = int(native.state)
        try:
            state = InferenceState(raw_state)
        except ValueError:
            state = raw_state

        def text(field):
            return bytes(field).split(b"\0", 1)[0].decode(
                "utf-8", errors="replace")

        return InferenceStatus(
            state=state,
            lease_id=int(native.lease_id),
            epoch=int(native.epoch),
            generation=int(native.generation),
            actual_fps=int(native.actual_fps),
            peer_pid=int(native.peer_pid),
            builtin_enabled=bool(native.builtin_enabled),
            handle_present=bool(native.handle_present),
            fallback_builtin=bool(native.fallback_builtin),
            builtin_state=text(native.builtin_state),
            source_id=text(native.source_id),
        )

    def alive(self):
        """Return whether the live connection still fences this generation."""

        operation = "rc_ext_inference_lease_alive"
        with self._lock:
            self._ensure_owner_open(operation)
            rc = int(self._lib.rc_ext_inference_lease_alive(self._h))
        if rc == 1:
            return True
        if rc == 0:
            return False
        if rc < 0:
            raise error_from_rc(operation, rc)
        raise UnknownNativeError(
            "native inference liveness returned neither 0 nor 1",
            operation=operation,
            rc=rc,
            detail=f"alive={rc}",
        )

    def close(self):
        """Release broker ownership and close the native handle once."""

        lock = getattr(self, "_lock", None)
        if lock is None:
            return super().close()
        with lock:
            if (not self.closed
                    and getattr(self, "_owner_pid", os.getpid()) != os.getpid()):
                raise HandleClosedError(
                    "a fork child cannot release its parent's inference lease",
                    operation="rc_ext_inference_lease_close",
                )
            closed = super().close()
        if closed:
            _LOG.info(
                "released inference lease app_id=%s instance_id=%s",
                getattr(self, "app_id", ""),
                getattr(self, "instance_id", ""),
            )
        return closed

    def release(self):
        """Compatibility alias for :meth:`close`."""

        return self.close()


class _BorrowIterator(_Handle):
    """Borrow-iterator over a C ``*_next`` / ``*_release`` pair. Each
    ``__next__`` releases the previously borrowed record before fetching the
    next one, so the wrapper is valid only for the current loop step.
    Subclasses provide the ctypes record type (``_record``), the lib function
    names (``_next_cfn`` / ``_release_cfn``), and ``_wrap()``."""

    _record = None
    _next_cfn = None
    _release_cfn = None

    def _wrap(self, cbuf):
        raise NotImplementedError

    def _current_wrapper(self):
        ref = getattr(self, "_cur_wrapper_ref", None)
        return ref() if ref is not None else None

    def _release_cur(self, reason="source"):
        cbuf = getattr(self, "_cur", None)
        wrapper = self._current_wrapper()
        if cbuf is None:
            if wrapper is not None:
                wrapper._mark_released(reason)
            self._cur_wrapper_ref = None
            return False

        # Clear bookkeeping first so an idempotent/re-entrant close cannot send a
        # second release for the same native record.  The ctypes release function
        # is void; ``finally`` still invalidates Python views if a test double (or
        # a future binding) raises unexpectedly.
        self._cur = None
        self._cur_wrapper_ref = None
        try:
            h = getattr(self, "_h", None)
            if h:
                getattr(self._lib, self._release_cfn)(h, byref(cbuf))
        finally:
            if wrapper is not None:
                wrapper._mark_released(reason)
        _LOG.debug("released %s borrow reason=%s", type(self).__name__, reason)
        return True

    def _release_borrow(self, wrapper, reason="explicit"):
        """Release ``wrapper`` iff it is this source's current borrow."""

        if getattr(wrapper, "released", False):
            return False
        current = self._current_wrapper()
        if current is wrapper:
            return self._release_cur(reason)
        # A wrapper can reach here only if external code corrupted/overrode the
        # normal source bookkeeping.  It no longer has a safely releasable native
        # record, so fail closed and invalidate its data access.
        wrapper._mark_released(reason)
        _LOG.debug("invalidated detached %s borrow", type(wrapper).__name__)
        return False

    def _on_close(self):
        self._release_cur("source_close")

    def __iter__(self):
        return self

    def _acquire_once(self, timeout_ms):
        self._ensure_open()
        timeout = _validate_timeout_ms(timeout_ms, self._next_cfn)
        cbuf = self._record()
        rc = int(getattr(self._lib, self._next_cfn)(
            self._h, byref(cbuf), timeout))
        if rc > 0:
            raise AcquireTimeoutError(
                operation=self._next_cfn,
                detail=f"timeout_ms={timeout}",
            )
        if rc < 0:
            raise error_from_rc(self._next_cfn, rc)

        self._cur = cbuf
        try:
            wrapper = self._wrap(cbuf)
            wrapper_ref = weakref.ref(wrapper)
        except BaseException:
            self._release_cur("wrap_error")
            raise
        self._cur_wrapper_ref = wrapper_ref
        self.last_error = None
        return wrapper

    def acquire(self, timeout_ms=None):
        """Strictly acquire one record or raise a typed exception.

        Unlike iteration, this method performs exactly one native wait.  A
        timeout raises :class:`AcquireTimeoutError`; a negative native result is
        mapped to its typed SDK error.  Acquiring a new record first releases the
        previous single outstanding borrow, matching the historical iterator
        ownership model.
        """

        self._ensure_open()
        candidate = self._timeout if timeout_ms is None else timeout_ms
        timeout = _validate_timeout_ms(
            candidate,
            f"{type(self).__name__}.acquire",
        )
        # Validate the override before returning the currently borrowed record.
        self._release_cur("next_acquire")
        return self._acquire_once(timeout)

    def __next__(self):
        """Compatibility iterator: retry timeouts and end on native errors.

        Existing ``for frame in source`` applications historically observed a
        plain end-of-stream for every negative return code.  Preserve that shape
        while recording the typed cause in ``source.last_error``.  New code that
        needs to distinguish protocol/backpressure/internal failures should use
        :meth:`acquire`.
        """

        self._release_cur("iterator_advance")
        while True:
            try:
                return self._acquire_once(self._timeout)
            except AcquireTimeoutError:
                continue
            except RecameraError as exc:
                self.last_error = exc
                _LOG.debug("compatibility iterator stopped after %s", exc)
                raise StopIteration from None


class ResultSink(_Handle):
    """Injects detection results into rkipc via /run/recamera/result-in.sock."""

    _close_cfn = "rc_ext_result_close"

    # Conservative local wire budget for one send_* datagram. 64 KiB is the
    # documented per-message datagram limit; the authoritative value is the
    # server's (a future server-ACK protocol will report it -- 需核实 against the
    # firmware socket's SO_SNDBUF). Overridable per instance if a build differs.
    MAX_MESSAGE_BYTES = 64 * 1024
    _C_INT_MIN = -(1 << 31)
    _C_INT_MAX = (1 << 31) - 1
    _C_UINT64_MAX = (1 << 64) - 1

    def __init__(self, source_id, lib_path=None):
        self._lib = _load(lib_path)
        err = c_int(0)
        self._h = self._lib.rc_ext_result_open(source_id.encode(), byref(err))
        if not self._h:
            raise error_from_rc(
                "rc_ext_result_open",
                err.value or -int(ErrorCode.EINTERNAL),
                detail=f"source_id={source_id!r}",
            )
        self.source_id = source_id
        self._labels = []
        self._masks = []
        self._pt_arrays = []
        # Local send counters (best-effort visibility until server-ACK lands).
        self._sent = 0
        self._oversize = 0
        self._send_error = 0

    @staticmethod
    def _enc(label):
        if isinstance(label, str):
            return label.encode()
        return label or b""

    @staticmethod
    def _format_error(operation, detail):
        raise FormatError(
            f"{operation}: {detail}",
            operation=operation,
            detail=detail,
        )

    @classmethod
    def _require_sequence(cls, value, lengths, operation, field):
        try:
            actual = len(value)
        except TypeError:
            cls._format_error(operation, f"{field} must be a sized sequence")
        accepted = tuple(sorted(set(int(length) for length in lengths)))
        if actual not in accepted:
            choices = " or ".join(str(length) for length in accepted)
            cls._format_error(
                operation,
                f"{field} must contain {choices} element(s), got {actual}",
            )
        return value

    @classmethod
    def _require_int(
        cls,
        value,
        operation,
        field,
        *,
        minimum=None,
        maximum=None,
    ):
        if isinstance(value, bool) or type(value).__name__ == "bool_":
            cls._format_error(operation, f"{field} must be an integer, not bool")
        try:
            converted = operator.index(value)
        except TypeError:
            cls._format_error(operation, f"{field} must be an integer")
        if minimum is not None and converted < minimum:
            cls._format_error(operation, f"{field} must be >= {minimum}")
        if maximum is not None and converted > maximum:
            cls._format_error(operation, f"{field} must be <= {maximum}")
        return converted

    @classmethod
    def _require_c_int(cls, value, operation, field):
        return cls._require_int(
            value,
            operation,
            field,
            minimum=cls._C_INT_MIN,
            maximum=cls._C_INT_MAX,
        )

    @classmethod
    def _require_unit_float(cls, value, operation, field):
        if isinstance(value, bool) or type(value).__name__ == "bool_":
            cls._format_error(operation, f"{field} must be a finite number, not bool")
        try:
            converted = float(value)
        except (TypeError, ValueError, OverflowError):
            cls._format_error(operation, f"{field} must be a finite number")
        if not math.isfinite(converted):
            cls._format_error(operation, f"{field} must be finite")
        if not 0.0 <= converted <= 1.0:
            cls._format_error(operation, f"{field} must be normalized to [0,1]")
        return converted

    @classmethod
    def _require_box(cls, value, operation, field):
        value = cls._require_sequence(value, (4,), operation, field)
        x1, y1, x2, y2 = (
            cls._require_unit_float(value[index], operation, f"{field}[{index}]")
            for index in range(4)
        )
        if x1 > x2 or y1 > y2:
            cls._format_error(
                operation,
                f"{field} must satisfy x1 <= x2 and y1 <= y2",
            )
        return x1, y1, x2, y2

    @classmethod
    def _require_label(cls, label, operation, field):
        if label is None:
            encoded = b""
        elif isinstance(label, str):
            try:
                encoded = label.encode()
            except UnicodeError:
                cls._format_error(operation, f"{field} is not valid UTF-8 text")
        elif isinstance(label, (bytes, bytearray, memoryview)):
            encoded = bytes(label)
        else:
            cls._format_error(operation, f"{field} must be str, bytes, or None")
        if b"\0" in encoded:
            cls._format_error(operation, f"{field} must not contain NUL bytes")
        return encoded

    def _raise_oversize(self, name, estimated, item_count):
        self._oversize += 1
        raise ResultTooLarge(
            "%s: estimated wire size %d bytes for %d item(s) exceeds the "
            "%d-byte datagram limit; split the results or drop "
            "mask/keypoint detail"
            % (name, estimated, item_count, self.MAX_MESSAGE_BYTES)
        )

    def _require_mask_bytes(self, mask, width, height, operation, field):
        if (width == 0) != (height == 0):
            self._format_error(
                operation,
                f"{field} dimensions must both be zero or both be positive",
            )
        expected = width * height
        if mask is None:
            actual = 0
            encoded = b""
        elif isinstance(mask, str):
            self._format_error(operation, f"{field} must be raw bytes, not str")
        else:
            try:
                view = memoryview(mask)
            except TypeError:
                try:
                    actual = len(mask)
                except TypeError:
                    self._format_error(operation, f"{field} must be bytes-like")
                if actual != expected:
                    self._format_error(
                        operation,
                        f"len({field}) must equal mask_w * mask_h "
                        f"({expected}), got {actual}",
                    )
                estimated = ctypes.sizeof(Segmentation) + actual + 64
                if estimated > self.MAX_MESSAGE_BYTES:
                    self._raise_oversize(operation, estimated, 1)
                try:
                    encoded = bytes(mask)
                except (TypeError, ValueError, OverflowError):
                    self._format_error(operation, f"{field} must be bytes-like")
            else:
                actual = view.nbytes
                if actual != expected:
                    self._format_error(
                        operation,
                        f"len({field}) must equal mask_w * mask_h "
                        f"({expected}), got {actual}",
                    )
                estimated = ctypes.sizeof(Segmentation) + actual + 64
                if estimated > self.MAX_MESSAGE_BYTES:
                    self._raise_oversize(operation, estimated, 1)
                try:
                    encoded = view.tobytes()
                except (TypeError, ValueError, BufferError):
                    self._format_error(operation, f"{field} must be bytes-like")
        actual = len(encoded)
        if actual != expected:
            self._format_error(
                operation,
                f"len({field}) must equal mask_w * mask_h "
                f"({expected}), got {actual}",
            )
        return encoded

    def _send(self, cfn, ArrayT, name, pts_us, items, fill_item):
        """Shared send scaffold: materialise ``items`` into a ``(ArrayT * n)``
        C array via ``fill_item(i, item, arr)``, call ``self._lib.<cfn>``, and
        raise on a non-zero rc (``name`` labels the error). ``self._labels`` is
        reset here as the common keepalive; callers with extra keepalives (e.g.
        masks, point arrays) reset those before invoking ``_send``."""
        self._ensure_open()
        pts_us = self._require_int(
            pts_us,
            name,
            "pts_us",
            minimum=0,
            maximum=self._C_UINT64_MAX,
        )
        try:
            items = list(items)
        except TypeError:
            self._format_error(name, "items must be iterable")
        n = len(items)
        # Reset EVERY keepalive here so the size estimate below counts only this
        # message's variable-length bytes (labels/masks/point arrays), never
        # stale ones left over from a prior send of a different task type.
        self._labels = []
        self._masks = []
        self._pt_arrays = []
        fixed_estimate = ctypes.sizeof(ArrayT) * n + 64
        if fixed_estimate > self.MAX_MESSAGE_BYTES:
            self._raise_oversize(name, fixed_estimate, n)
        arr = (ArrayT * n)()
        for i, it in enumerate(items):
            fill_item(i, it, arr)
        # Local oversize guard, BEFORE the C call: each send_* packs one datagram
        # (recamera_ext.h §M1); a datagram past the socket's wire budget is
        # dropped/truncated by the kernel while the C ABI may still return local
        # success -- the result then silently never reaches rkipc. Reject early
        # with a clear error rather than emitting into the void.
        est = self._estimate_size(arr)
        if est > self.MAX_MESSAGE_BYTES:
            self._raise_oversize(name, est, n)
        rc = getattr(self._lib, cfn)(self._h, c_uint64(pts_us), arr, c_size_t(n))
        if rc != 0:
            self._send_error += 1
            raise error_from_rc(cfn, rc, detail=f"method={name}")
        self._sent += 1
        return rc

    def _estimate_size(self, arr):
        """Conservative estimate of the packed datagram size: the fixed struct
        array bytes plus the variable-length label/mask/keypoint bytes it points
        at, plus a small envelope allowance. Protobuf packing is of the same
        order (varints vs the C floats/ints roughly offset the field tags), so
        this is an adequate LOCAL guard; the authoritative limit is the
        server's, surfaced by the future server-ACK protocol."""
        total = ctypes.sizeof(arr) + 64  # struct payload + envelope/header slack
        total += sum(len(lb) for lb in self._labels)
        total += sum(len(m) for m in self._masks)
        total += sum(ctypes.sizeof(pa) for pa in self._pt_arrays)
        return total

    def stats(self):
        """Local send counters (best-effort visibility). `sent` = the C send
        returned success; `oversize_rejected` = refused locally by the wire-size
        guard before the C call; `send_error` = the C send returned a negative
        rc. These are LOCAL only: a `sent` frame the server later drops
        (rate-limit / auth / decode) is not visible until the server-ACK
        protocol lands (docs/guide/result-push.md)."""
        return {"sent": self._sent,
                "oversize_rejected": self._oversize,
                "send_error": self._send_error}

    def send_detections(self, pts_us, boxes):
        """boxes: iterable of (x1, y1, x2, y2, score, label[, class_id]).

        Coordinates are normalized [0,1] (top-left x1/y1, bottom-right x2/y2, as
        a fraction of frame width/height). The OSD renderer clamps to [0,1] and
        multiplies by frame size, so pixel values collapse to a 1px box -- always
        send fractions, e.g. (0.05, 0.07, 0.62, 0.94, 0.92, "person")."""

        operation = "send_detections"

        def fill(i, b, arr):
            field = f"boxes[{i}]"
            b = self._require_sequence(b, (6, 7), operation, field)
            x1, y1, x2, y2 = self._require_box(
                (b[0], b[1], b[2], b[3]), operation, f"{field}.box"
            )
            score = self._require_unit_float(b[4], operation, f"{field}.score")
            lb = self._require_label(b[5], operation, f"{field}.label")
            class_id = self._require_c_int(
                b[6] if len(b) > 6 else 0,
                operation,
                f"{field}.class_id",
            )
            self._labels.append(lb)
            arr[i] = Box(x1, y1, x2, y2, score, lb, class_id)

        return self._send("rc_ext_result_send_detections", Box, operation,
                          pts_us, boxes, fill)

    def send_classification(self, pts_us, items):
        """items: iterable of (score, class_id, label[, box]).

        The optional 4th element is a box (x1, y1, x2, y2); omit it or pass
        None to leave the entry box-less (original behaviour). A box attaches
        a source ROI to the entry (e.g. per-face attributes). When present, the
        box coordinates are normalized [0,1] (fraction of frame width/height),
        e.g. (0.30, 0.20, 0.55, 0.60)."""

        operation = "send_classification"

        def fill(i, it, arr):
            field = f"items[{i}]"
            it = self._require_sequence(it, (3, 4), operation, field)
            score = self._require_unit_float(it[0], operation, f"{field}.score")
            class_id = self._require_c_int(it[1], operation, f"{field}.class_id")
            lb = self._require_label(it[2], operation, f"{field}.label")
            self._labels.append(lb)
            c = Classification(score, class_id, lb)
            box = it[3] if len(it) > 3 else None
            if box is not None:
                c.has_box = 1
                c.x1, c.y1, c.x2, c.y2 = self._require_box(
                    box, operation, f"{field}.box"
                )
            else:
                c.has_box = 0
            arr[i] = c

        return self._send("rc_ext_result_send_classification", Classification,
                          operation, pts_us, items, fill)

    def send_segmentation(self, pts_us, items):
        """items: iterable of
        (x1, y1, x2, y2, score, class_id, label, mask_bytes, mask_w, mask_h).
        The ROI box x1/y1/x2/y2 is normalized [0,1] (fraction of frame
        width/height), e.g. (0.05, 0.07, 0.62, 0.94). mask_bytes is raw
        row-major bytes (not coordinates) and may be None/empty (with
        mask_w=mask_h=0)."""
        operation = "send_segmentation"

        def fill(i, it, arr):
            field = f"items[{i}]"
            it = self._require_sequence(it, (7, 8, 9, 10), operation, field)
            x1, y1, x2, y2 = self._require_box(
                (it[0], it[1], it[2], it[3]), operation, f"{field}.box"
            )
            score = self._require_unit_float(it[4], operation, f"{field}.score")
            class_id = self._require_c_int(it[5], operation, f"{field}.class_id")
            lb = self._require_label(it[6], operation, f"{field}.label")
            mask = it[7] if len(it) > 7 else None
            mask_w = self._require_int(
                it[8] if len(it) > 8 else 0,
                operation,
                f"{field}.mask_w",
                minimum=0,
                maximum=self._C_INT_MAX,
            )
            mask_h = self._require_int(
                it[9] if len(it) > 9 else 0,
                operation,
                f"{field}.mask_h",
                minimum=0,
                maximum=self._C_INT_MAX,
            )
            mb = self._require_mask_bytes(
                mask, mask_w, mask_h, operation, f"{field}.mask"
            )
            self._labels.append(lb)
            self._masks.append(mb)
            arr[i] = Segmentation(
                x1, y1, x2, y2, score,
                class_id, lb, (mb if mb else None), mask_w, mask_h,
            )

        return self._send("rc_ext_result_send_segmentation", Segmentation,
                          operation, pts_us, items, fill)

    def send_tracking(self, pts_us, items):
        """items: iterable of (x1, y1, x2, y2, score, class_id, label, track_id).

        Coordinates are normalized [0,1] (fraction of frame width/height), same
        contract as send_detections, e.g. (0.05, 0.07, 0.62, 0.94, 0.92, 0,
        "person", 7)."""

        operation = "send_tracking"

        def fill(i, it, arr):
            field = f"items[{i}]"
            it = self._require_sequence(it, (8,), operation, field)
            x1, y1, x2, y2 = self._require_box(
                (it[0], it[1], it[2], it[3]), operation, f"{field}.box"
            )
            score = self._require_unit_float(it[4], operation, f"{field}.score")
            class_id = self._require_c_int(it[5], operation, f"{field}.class_id")
            lb = self._require_label(it[6], operation, f"{field}.label")
            track_id = self._require_c_int(it[7], operation, f"{field}.track_id")
            self._labels.append(lb)
            arr[i] = Tracking(
                x1, y1, x2, y2, score,
                class_id, lb, track_id,
            )

        return self._send("rc_ext_result_send_tracking", Tracking, operation,
                          pts_us, items, fill)

    def send_keypoints(self, pts_us, instances):
        """instances: iterable of dicts (or tuples) describing one object each:
            {
              "points": [(x, y, score, keypoint_id), ...],   # required
              "box": (x1, y1, x2, y2),   # optional; omit -> no object box
              "score": float, "class_id": int, "label": str,  # object-level
            }
        Both the point x/y and the optional object box x1/y1/x2/y2 are
        normalized [0,1] (fraction of frame width/height), same contract as
        send_detections. A missing "box" leaves the whole object_info group
        unset on the wire."""
        operation = "send_keypoints"

        def fill(i, inst, arr):
            field = f"instances[{i}]"
            if not isinstance(inst, Mapping):
                self._format_error(operation, f"{field} must be a mapping")
            try:
                pts = list(inst.get("points", []))
            except TypeError:
                self._format_error(operation, f"{field}.points must be iterable")
            np_ = len(pts)
            point_estimate = ctypes.sizeof(KeypointInstance) + ctypes.sizeof(Point) * np_ + 64
            if point_estimate > self.MAX_MESSAGE_BYTES:
                self._raise_oversize(operation, point_estimate, 1)
            parr = (Point * np_)()
            for j, p in enumerate(pts):
                point_field = f"{field}.points[{j}]"
                p = self._require_sequence(p, (3, 4), operation, point_field)
                px = self._require_unit_float(p[0], operation, f"{point_field}.x")
                py = self._require_unit_float(p[1], operation, f"{point_field}.y")
                pscore = self._require_unit_float(
                    p[2], operation, f"{point_field}.score"
                )
                kid = self._require_c_int(
                    p[3] if len(p) > 3 else 0,
                    operation,
                    f"{point_field}.keypoint_id",
                )
                parr[j] = Point(px, py, pscore, kid)
            self._pt_arrays.append(parr)

            box = inst.get("box")
            lb = self._require_label(
                inst.get("label", ""), operation, f"{field}.label"
            )
            self._labels.append(lb)
            ke = KeypointInstance()
            if box is not None:
                ke.has_box = 1
                ke.x1, ke.y1, ke.x2, ke.y2 = self._require_box(
                    box, operation, f"{field}.box"
                )
                ke.score = self._require_unit_float(
                    inst.get("score", 0.0), operation, f"{field}.score"
                )
                ke.class_id = self._require_c_int(
                    inst.get("class_id", 0), operation, f"{field}.class_id"
                )
                ke.label = lb
            else:
                ke.has_box = 0
                ke.label = None
            ke.points = ctypes.cast(parr, POINTER(Point)) if np_ else ctypes.cast(None, POINTER(Point))
            ke.n_points = np_
            arr[i] = ke

        return self._send("rc_ext_result_send_keypoints", KeypointInstance,
                          operation, pts_us, instances, fill)


class FrameLease:
    """An explicit lease on one borrowed camera dma-buf.

    The lease is valid until any of these events occurs:

    * :meth:`release` is called;
    * its source advances/acquires another frame;
    * its source is closed or leaves a context manager; or
    * the lease is garbage-collected while still current.

    ``release()`` is idempotent and returns whether it performed the native
    release.  Metadata such as ``seq`` and ``width`` remains readable afterwards,
    but fd/mapping/array access raises :class:`BufferReleasedError`.  A NumPy view
    retained before release cannot be revoked by Python; call :meth:`copy` while
    the lease is alive when data must outlive this scope.

    ``Frame`` below is a compatibility subclass, so every historical object is
    also a ``FrameLease`` without changing ``from recamera_ext import Frame``.
    """

    def __init__(self, src, cbuf):
        self._src = src
        # Kept for native calls and for compatibility with older adapter code.
        # New callers should use the public ``fd``/``buffer`` properties rather
        # than reaching through ``frame._c.fd``.
        self._c = cbuf
        self.seq = int(cbuf.seq)
        self.pts_us = int(cbuf.pts_us)
        self.width = int(cbuf.width)
        self.height = int(cbuf.height)
        self.fourcc = int(cbuf.fourcc)
        self.buf_size = int(cbuf.buf_size)
        self.flags = int(cbuf.flags)
        self.chn_id = int(cbuf.chn_id)
        self.n_planes = int(cbuf.n_planes)
        self.dropped = bool(cbuf.flags & 1)
        if self.width <= 0 or self.height <= 0 or self.buf_size <= 0:
            raise FormatError(
                "native frame has invalid image geometry",
                operation="rc_ext_frame_next",
                detail=(f"seq={self.seq} width={self.width} height={self.height} "
                        f"buf_size={self.buf_size}"),
            )
        if int(cbuf.fd) < 0:
            raise FormatError(
                "native frame has no live dma-buf fd",
                operation="rc_ext_frame_next",
                detail=f"seq={self.seq} fd={int(cbuf.fd)}",
            )
        if not 1 <= self.n_planes <= len(cbuf.plane):
            raise FormatError(
                "native frame has an invalid plane count",
                operation="rc_ext_frame_next",
                detail=f"seq={self.seq} n_planes={self.n_planes}",
            )
        # Keep the historical list shape while upgrading each entry from a raw
        # tuple to the tuple-compatible, named ``PlaneLayout`` type.
        self.planes = [
            PlaneLayout(
                int(cbuf.plane[i].offset),
                int(cbuf.plane[i].stride),
                int(cbuf.plane[i].vstride),
            )
            for i in range(self.n_planes)
        ]
        for index, plane in enumerate(self.planes):
            end = plane.offset + plane.stride * plane.vstride
            if (plane.offset < 0 or plane.stride <= 0 or plane.vstride <= 0
                    or end > self.buf_size):
                raise FormatError(
                    "native frame has an invalid plane layout",
                    operation="rc_ext_frame_next",
                    detail=(f"seq={self.seq} plane={index} offset={plane.offset} "
                            f"stride={plane.stride} vstride={plane.vstride} "
                            f"end={end} buf_size={self.buf_size}"),
                )
        if self.fourcc == FOURCC_NV12:
            if self.n_planes < 2:
                raise FormatError(
                    "NV12 frame requires Y and UV plane descriptors",
                    operation="rc_ext_frame_next",
                    detail=f"seq={self.seq} n_planes={self.n_planes}",
                )
            y_plane, uv_plane = self.planes[:2]
            if (self.width > y_plane.stride or self.height > y_plane.vstride
                    or self.width > uv_plane.stride
                    or (self.height + 1) // 2 > uv_plane.vstride):
                raise FormatError(
                    "NV12 valid geometry exceeds its plane descriptors",
                    operation="rc_ext_frame_next",
                    detail=f"seq={self.seq} frame={self.width}x{self.height}",
                )
        self._buf = None
        self._array = None
        self._released = False
        self._release_reason = None
        self.buffer = BorrowedBuffer(
            self,
            size=self.buf_size,
            width=self.width,
            height=self.height,
            fourcc=self.fourcc,
            planes=self.planes,
        )

    @property
    def released(self):
        """Whether the source has returned this native frame buffer."""

        return self._released

    @property
    def release_reason(self):
        """Diagnostic reason for release (explicit, source close, next frame…)."""

        return self._release_reason

    def _ensure_alive(self):
        if self._released:
            reason = f"; reason={self._release_reason}" if self._release_reason else ""
            raise BufferReleasedError(
                f"frame seq={self.seq} has been released{reason}",
                operation="frame buffer access",
            )

    def _mark_released(self, reason):
        if self._released:
            return False
        self._released = True
        self._release_reason = str(reason) if reason else "released"
        # Do not retain Python aliases as an invitation to reuse them.  Existing
        # external ndarray references still require the documented caller copy.
        self._buf = None
        self._array = None
        return True

    def _buffer_fd(self):
        self._ensure_alive()
        fd = int(self._c.fd)
        if fd < 0:
            # The C ABI uses fd=-1 as its idempotent released sentinel.  Mirror it
            # into the Python lease even if an out-of-band native action set it.
            self._mark_released("native_fd_closed")
            raise BufferReleasedError(
                f"frame seq={self.seq} native fd is closed",
                operation="frame.fd",
            )
        return fd

    @property
    def fd(self):
        """Borrowed dma-buf fd; never close or retain it beyond this lease."""

        return self.buffer.fd

    def _buffer_map(self):
        """Native mmap + DMA_BUF sync, cached for this live lease."""

        self._ensure_alive()
        if self._buf is not None:
            return self._buf
        np = _np()

        self._src._ensure_open()
        yptr = self._src._lib.rc_ext_frame_map(self._src._h, byref(self._c))
        if not yptr:
            raise InternalError(
                operation="rc_ext_frame_map",
                code=ErrorCode.EINTERNAL,
                rc=-int(ErrorCode.EINTERNAL),
                detail=f"seq={self.seq}",
            )
        base = int(self._c._base or 0)
        mapped_size = self.buffer.size
        native_map_len = int(self._c._map_len)
        if base == 0 or mapped_size <= 0 or native_map_len < mapped_size:
            raise FormatError(
                "native frame map returned invalid buffer metadata",
                operation="rc_ext_frame_map",
                detail=(f"seq={self.seq} base={base} size={mapped_size} "
                        f"map_len={native_map_len}"),
            )
        # Use the immutable BorrowedBuffer metadata for the memory boundary.  A
        # caller mutating the legacy public ``frame.buf_size`` field cannot make
        # ctypes construct a view past the producer-declared mapping.
        carr = (c_ubyte * mapped_size).from_address(base)
        self._buf = np.ctypeslib.as_array(carr)
        try:
            self._buf.flags.writeable = False
        except ValueError:
            pass
        return self._buf

    # Historical private name retained for adapter compatibility.
    def _map(self):
        return self._buffer_map()

    def plane_array(self, i):
        """Plane ``i`` as a checked zero-copy ``(vstride, stride)`` view."""

        return self.buffer.plane_array(i)

    @property
    def array(self):
        """Zero-copy valid Y pixels as ``(height, width)`` uint8.

        The property rechecks the lease even when the view was previously cached,
        so accessing ``frame.array`` after automatic/explicit release is rejected.
        """

        self._ensure_alive()
        if self._array is not None:
            return self._array
        y = self.plane_array(0)
        if self.height > y.shape[0] or self.width > y.shape[1]:
            raise FormatError(
                "valid frame geometry exceeds Y-plane layout",
                detail=(f"frame={self.width}x{self.height} "
                        f"plane={y.shape[1]}x{y.shape[0]}"),
            )
        self._array = y[: self.height, : self.width]
        return self._array

    def copy(self):
        """Return an owned copy of the valid Y plane that survives release."""

        return self.array.copy()

    def to_bgr(self):
        """Return an owned contiguous BGR image using OpenCV NV12 conversion."""

        self._ensure_alive()
        if self.fourcc != FOURCC_NV12:
            raise FormatError(
                "BGR conversion currently supports NV12 frames only",
                detail=f"seq={self.seq} fourcc=0x{self.fourcc:08x}",
            )
        if self.n_planes < 2:
            raise FormatError(
                "NV12 conversion requires two planes",
                detail=f"seq={self.seq} n_planes={self.n_planes}",
            )
        import cv2

        np = _np()
        w, h = self.width, self.height
        if (w | h) & 1:
            raise FormatError(
                "NV12 conversion requires even width and height",
                detail=f"seq={self.seq} frame={w}x{h}",
            )
        ysrc = self.plane_array(0)
        uvsrc = self.plane_array(1)
        if h > ysrc.shape[0] or w > ysrc.shape[1] or \
                h // 2 > uvsrc.shape[0] or w > uvsrc.shape[1]:
            raise FormatError(
                "valid NV12 geometry exceeds plane layout",
                detail=f"seq={self.seq} frame={w}x{h}",
            )
        nv12 = np.empty((h * 3 // 2, w), dtype=np.uint8)
        nv12[:h] = ysrc[:h, :w]
        nv12[h:] = uvsrc[:h // 2, :w]
        return cv2.cvtColor(nv12, cv2.COLOR_YUV2BGR_NV12)

    def release(self):
        """Return this frame to its source; idempotent and exception-safe."""

        if self._released:
            return False
        return bool(self._src._release_borrow(self, "explicit"))

    def __enter__(self):
        self._ensure_alive()
        return self

    def __exit__(self, exc_type, exc_value, _traceback):
        return _exit_with_cleanup(
            self.release,
            exc_type,
            exc_value,
            type(self).__name__,
        )

    def __del__(self):
        try:
            if not self._released:
                _LOG.debug("garbage-collected live frame lease seq=%s; releasing", self.seq)
                self.release()
        except Exception:
            # Finalizers must never turn interpreter shutdown into noisy stderr.
            pass

    def __repr__(self):
        state = "released" if self.released else "borrowed"
        return (f"{type(self).__name__}(seq={self.seq}, pts_us={self.pts_us}, "
                f"size={self.width}x{self.height}, fourcc=0x{self.fourcc:08x}, "
                f"{state})")


class Frame(FrameLease):
    """Backward-compatible name for :class:`FrameLease`.

    ``FrameSource`` continues to yield this exact class, preserving existing
    imports and ``isinstance(frame, Frame)`` checks while exposing the new lease
    API through inheritance.
    """


class FrameSource(_BorrowIterator):
    """Zero-copy frame receiver over /run/recamera/frame.sock (spec §2.5).

    Iterating yields Frame objects; each is released automatically when the loop
    advances to the next frame or the context exits."""

    _record = _FrameBuf
    _next_cfn = "rc_ext_frame_next"
    _release_cfn = "rc_ext_frame_release"
    _close_cfn = "rc_ext_frame_close"

    def __init__(self, config=None, timeout_ms=1000, lib_path=None):
        timeout = _validate_timeout_ms(timeout_ms, "FrameSource.__init__")
        self._lib = _load(lib_path)
        cfgp = None
        if config is not None:
            self._cfg = _Cfg(config.width, config.height, config.fourcc, config.fps_divisor)
            cfgp = byref(self._cfg)
        err = c_int(0)
        self._h = self._lib.rc_ext_frame_open(cfgp, byref(err))
        if not self._h:
            raise error_from_rc(
                "rc_ext_frame_open",
                err.value or -int(ErrorCode.EINTERNAL),
            )
        w, h, fcc, pd, mo = (c_uint32() for _ in range(5))
        self._lib.rc_ext_frame_geometry(self._h, byref(w), byref(h), byref(fcc), byref(pd), byref(mo))
        self.width, self.height = w.value, h.value
        self.fourcc, self.pool_depth, self.max_outstanding = fcc.value, pd.value, mo.value
        self._timeout = timeout
        self._cur = None  # _FrameBuf currently borrowed
        self._cur_wrapper_ref = None
        self.last_error = None

    def _wrap(self, cbuf):
        return Frame(self, cbuf)


class ProbeSample:
    """A borrowed probe sample. Valid only inside the current iteration step;
    the underlying buffer (inline copy or memfd mmap) is released when the loop
    advances or exits."""

    def __init__(self, src, csample):
        self._src = src
        self._c = csample
        sid = csample.stage_id
        self.stage_id = sid.decode() if sid else ""
        self.seq = csample.seq
        self.pts_us = csample.pts_us
        self.flags = csample.flags
        self.dropped = bool(csample.flags & 1)
        self.payload_len = int(csample.payload_len)
        self._ptr = csample.payload
        self._released = False
        self._release_reason = None
        if csample.has_meta:
            self.meta = {
                "shape": [int(csample.shape[i]) for i in range(csample.n_shape)],
                "dtype": int(csample.dtype),
                "layout": int(csample.layout),
                "fourcc": int(csample.fourcc),
                "width": int(csample.width),
                "height": int(csample.height),
                "stride": int(csample.stride),
                "scale": float(csample.scale),
                "zero_point": int(csample.zero_point),
            }
        else:
            self.meta = None

    @property
    def released(self):
        return self._released

    @property
    def release_reason(self):
        return self._release_reason

    def _ensure_alive(self):
        if self._released:
            raise BufferReleasedError(
                f"probe sample seq={self.seq} has been released; "
                f"reason={self._release_reason}",
                operation="probe payload access",
            )

    def _mark_released(self, reason):
        if self._released:
            return False
        self._released = True
        self._release_reason = str(reason) if reason else "released"
        self._ptr = None
        return True

    def release(self):
        """Release this sample early; idempotent like :class:`FrameLease`."""

        if self._released:
            return False
        return bool(self._src._release_borrow(self, "explicit"))

    def __enter__(self):
        self._ensure_alive()
        return self

    def __exit__(self, *_exc):
        self.release()

    def __del__(self):
        try:
            if not self._released:
                self.release()
        except Exception:
            pass

    @property
    def payload(self):
        """The sample bytes (a copy). Valid only for this iteration step."""
        self._ensure_alive()
        if not self._ptr or self.payload_len == 0:
            return b""
        return ctypes.string_at(self._ptr, self.payload_len)

    @property
    def array(self):
        """Zero-copy numpy view over the payload. When meta is present the view
        is typed/shaped by the TensorMeta (dtype + shape); otherwise a flat
        uint8 array. The view is valid only until the loop advances."""
        self._ensure_alive()
        np = _np()

        if not self._ptr or self.payload_len == 0:
            return np.empty((0,), dtype=np.uint8)
        if self.meta is not None:
            npdt = np.dtype(_PROBE_DTYPES.get(self.meta["dtype"], "uint8"))
            count = self.payload_len // npdt.itemsize
            carr = (c_ubyte * self.payload_len).from_address(self._ptr)
            flat = np.frombuffer(carr, dtype=npdt, count=count)
            shape = self.meta["shape"]
            if shape and int(np.prod(shape)) == count:
                return flat.reshape(shape)
            return flat
        carr = (c_ubyte * self.payload_len).from_address(self._ptr)
        return np.frombuffer(carr, dtype=np.uint8)


class ProbeSource(_BorrowIterator):
    """Probe observability tap over /run/recamera/probe.sock (spec §4).

    Iterating yields ProbeSample objects; each is released automatically when
    the loop advances to the next sample or the context exits.

        from recamera_ext import ProbeSource
        with ProbeSource(stages=["metrics"]) as probe:
            for s in probe:
                print(s.stage_id, s.seq, s.payload_len)
    """

    _record = _ProbeSample
    _next_cfn = "rc_ext_probe_next"
    _release_cfn = "rc_ext_probe_release"
    _close_cfn = "rc_ext_probe_close"

    def __init__(self, stages, sample_every=1, timeout_ms=1000, lib_path=None):
        timeout = _validate_timeout_ms(timeout_ms, "ProbeSource.__init__")
        self._lib = _load(lib_path)
        if not hasattr(self._lib, "rc_ext_probe_open"):
            raise CapabilityUnavailableError(
                "librecamera_ext lacks probe support (requires SDK >= 1.2.0)",
                operation="rc_ext_probe_open",
            )
        stages = list(stages)
        if not stages:
            raise ValueError("stages must be a non-empty list of stage ids")
        arr = (c_char_p * len(stages))()
        for i, s in enumerate(stages):
            arr[i] = s.encode() if isinstance(s, str) else s
        err = c_int(0)
        self._h = self._lib.rc_ext_probe_open(arr, c_size_t(len(stages)),
                                              c_uint32(sample_every), byref(err))
        if not self._h:
            raise error_from_rc(
                "rc_ext_probe_open",
                err.value or -int(ErrorCode.EINTERNAL),
                detail=f"stages={stages!r}",
            )
        se, mask = c_uint32(), c_uint32()
        self._lib.rc_ext_probe_info(self._h, byref(se), byref(mask))
        self.sample_every = se.value
        self.subscribed_mask = mask.value
        self._timeout = timeout
        self._cur = None  # _ProbeSample currently borrowed
        self._cur_wrapper_ref = None
        self.last_error = None

    def _wrap(self, csample):
        return ProbeSample(self, csample)


class MaskControl(_Handle):
    """Hardware privacy-mask control -- a thin wrapper over rc_ext_mask_* (no
    logic of its own). Talks to rkipc's /var/tmp/rkipc control socket.

        from recamera_ext import MaskControl, MaskRect
        with MaskControl() as mc:
            mc.set([MaskRect(0, 0.1, 0.1, 0.3, 0.2)])   # create one block
            for x in drift():                            # incremental move, no flicker
                mc.update(MaskRect(0, x, 0.1, 0.3, 0.2))
    """

    _close_cfn = "rc_ext_mask_close"

    def __init__(self, lib_path=None):
        self._lib = _load(lib_path)
        if not hasattr(self._lib, "rc_ext_mask_open"):
            raise CapabilityUnavailableError(
                "librecamera_ext lacks privacy-mask support",
                operation="rc_ext_mask_open",
            )
        err = c_int(0)
        self._h = self._lib.rc_ext_mask_open(byref(err))
        if not self._h:
            raise error_from_rc(
                "rc_ext_mask_open",
                err.value or -int(ErrorCode.EINTERNAL),
            )

    @staticmethod
    def _to_c(rect):
        return _MaskRect(int(rect.id), float(rect.x), float(rect.y),
                         float(rect.w), float(rect.h))

    def set(self, rects):
        """Full set of active blocks (list[MaskRect], <=6). Persisted. Returns
        the number of blocks actually applied."""
        self._ensure_open()
        rects = list(rects)
        n = len(rects)
        arr = (_MaskRect * n)() if n else None
        for i, r in enumerate(rects):
            arr[i] = self._to_c(r)
        applied = c_int(0)
        rc = self._lib.rc_ext_mask_set(self._h, arr, c_size_t(n), byref(applied))
        if rc < 0:
            raise error_from_rc("rc_ext_mask_set", rc)
        return applied.value

    def update(self, rect):
        """Incrementally move a single block (no flicker, not persisted). The
        block must already exist. Returns 0; raises on error (caller may fall
        back to set())."""
        self._ensure_open()
        c = self._to_c(rect)
        rc = self._lib.rc_ext_mask_update(self._h, byref(c))
        if rc < 0:
            raise error_from_rc("rc_ext_mask_update", rc)
        return rc

    def clear(self):
        """Clear all masks (persisted)."""
        self._ensure_open()
        rc = self._lib.rc_ext_mask_clear(self._h)
        if rc < 0:
            raise error_from_rc("rc_ext_mask_clear", rc)
        return rc

    def query(self):
        """Return the current active masks as list[MaskRect]."""
        self._ensure_open()
        out = (_MaskRect * 6)()
        rc = self._lib.rc_ext_mask_query(self._h, out, c_size_t(6))
        if rc < 0:
            raise error_from_rc("rc_ext_mask_query", rc)
        n = min(rc, 6)
        return [MaskRect(out[i].id, out[i].x, out[i].y, out[i].w, out[i].h)
                for i in range(n)]

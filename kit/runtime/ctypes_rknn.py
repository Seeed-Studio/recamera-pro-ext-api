"""Direct ``librknnrt.so`` bindings for a single uint8 NHWC caller input.

The August 19 legacy-path investigation observed reference cycles retained by
RKNNLite 2.3.2 and compared direct bindings against that wrapper. Those historical
RSS and latency measurements do not establish the behavior of the new inference
daemon; its managed lifecycle needs separate device validation.

Inputs use pass_through=0 so librknnrt converts to the graph's native layout and
dtype. Static image models can reuse bound DMA input/output buffers; float32
outputs retain the graph's declared shape. Unsupported binding configurations
fall back to the general API in a fresh context. ESK_RKNN_IO_MODE=legacy disables
binding; ESK_RKNN_IO_MODE=bound requires it. Unsupported caller contracts remain
on the RKNNLite backend in auto mode.

All native entry points have ctypes prototypes to avoid pointer truncation on
AArch64. Device access is subject to the installed /dev/rknpu permissions.
"""

from __future__ import annotations

import ctypes
import math
import os
import weakref
from typing import List, Optional

import numpy as np

# The runtime lives in the read-only firmware partition on reCamera Pro and is
# symlinked into /usr/lib by the installer; a board where only one of the two
# exists still has to work, so both are tried in order.
LIB_CANDIDATES = (
    "/usr/lib/librknnrt.so",
    "/oem/usr/lib/librknnrt.so",
    "/userdata/sdk/lib/librknnrt.so",
)

RKNN_SUCC = 0
RKNN_MAX_DIMS = 16
RKNN_MAX_NAME_LEN = 256

# rknn_query_cmd
RKNN_QUERY_IN_OUT_NUM = 0
RKNN_QUERY_INPUT_ATTR = 1
RKNN_QUERY_OUTPUT_ATTR = 2
RKNN_QUERY_PERF_DETAIL = 3
# 5, not 3. 3 is PERF_DETAIL, whose struct is a pointer plus a length --
# querying it into an RknnSdkVersion returns RKNN_SUCC and fills the
# buffer with garbage rather than failing, so the wrong constant reads as
# a working call.
RKNN_QUERY_SDK_VERSION = 5
RKNN_QUERY_NATIVE_INPUT_ATTR = 8
RKNN_QUERY_NATIVE_OUTPUT_ATTR = 9
RKNN_QUERY_NATIVE_NHWC_OUTPUT_ATTR = 11

# rknn_tensor_type / rknn_tensor_format
RKNN_TENSOR_UINT8 = 3
RKNN_TENSOR_FLOAT32 = 0
RKNN_TENSOR_FLOAT16 = 1
RKNN_TENSOR_INT8 = 2
RKNN_TENSOR_NHWC = 1
RKNN_TENSOR_NCHW = 0
RKNN_TENSOR_NC1HWC2 = 2
RKNN_TENSOR_UNDEFINED = 3
RKNN_MEMORY_SYNC_TO_DEVICE = 1
RKNN_MEMORY_SYNC_FROM_DEVICE = 2

# rknn_core_mask values are a bitmask; 0 means "runtime decides".
RKNN_NPU_CORE_AUTO = 0

# aarch64 is LP64, so the header's non-``__arm__`` branch applies.
rknn_context = ctypes.c_uint64


class RknnTensorAttr(ctypes.Structure):
    """``rknn_tensor_attr``, field order and types verbatim from the header.

    ``fl`` (int8) sitting in front of ``zp`` (int32) is the one place a hand-
    packed layout would go wrong; ctypes inserts the same three padding bytes
    the C compiler does, so this must NOT be declared ``_pack_``-ed.
    """

    _fields_ = [
        ("index", ctypes.c_uint32),
        ("n_dims", ctypes.c_uint32),
        ("dims", ctypes.c_uint32 * RKNN_MAX_DIMS),
        ("name", ctypes.c_char * RKNN_MAX_NAME_LEN),
        ("n_elems", ctypes.c_uint32),
        ("size", ctypes.c_uint32),
        ("fmt", ctypes.c_int),
        ("type", ctypes.c_int),
        ("qnt_type", ctypes.c_int),
        ("fl", ctypes.c_int8),
        ("zp", ctypes.c_int32),
        ("scale", ctypes.c_float),
        ("w_stride", ctypes.c_uint32),
        ("size_with_stride", ctypes.c_uint32),
        ("pass_through", ctypes.c_uint8),
        ("h_stride", ctypes.c_uint32),
    ]


class RknnInputOutputNum(ctypes.Structure):
    _fields_ = [("n_input", ctypes.c_uint32), ("n_output", ctypes.c_uint32)]


class RknnInput(ctypes.Structure):
    _fields_ = [
        ("index", ctypes.c_uint32),
        ("buf", ctypes.c_void_p),
        ("size", ctypes.c_uint32),
        ("pass_through", ctypes.c_uint8),
        ("type", ctypes.c_int),
        ("fmt", ctypes.c_int),
    ]


class RknnOutput(ctypes.Structure):
    _fields_ = [
        ("want_float", ctypes.c_uint8),
        ("is_prealloc", ctypes.c_uint8),
        ("index", ctypes.c_uint32),
        ("buf", ctypes.c_void_p),
        ("size", ctypes.c_uint32),
    ]


class RknnSdkVersion(ctypes.Structure):
    _fields_ = [
        ("api_version", ctypes.c_char * 256),
        ("drv_version", ctypes.c_char * 256),
    ]


class RknnTensorMem(ctypes.Structure):
    """LP64 ``rknn_tensor_mem`` from the installed RKNN 2.3.2 header."""

    _fields_ = [
        ("virt_addr", ctypes.c_void_p), ("phys_addr", ctypes.c_uint64),
        ("fd", ctypes.c_int32), ("offset", ctypes.c_int32),
        ("size", ctypes.c_uint32), ("flags", ctypes.c_uint32),
        ("priv_data", ctypes.c_void_p),
    ]


class _BoundIOUnavailable(RuntimeError):
    pass


class RknnIOBuffer:
    """Model-owned DMA allocation; descriptors are borrowed until release.

    Do not close its fd. Allocate separate buffers per client/model alias; the
    model's default buffers are private scratch space. The caller must serialize
    allocation, binding, inference and release with its existing driver lock,
    and wait for all external writers/readers before reuse or release.
    """

    def __init__(self, owner, mem, *, kind, index, shape, strides, dtype):
        self._owner = weakref.ref(owner)
        self._mem = mem
        self.kind = kind
        self.index = index
        self.shape = tuple(shape)
        self.strides = tuple(strides)
        self.dtype = np.dtype(dtype).name
        self.released = False

    def _check(self):
        if self.released or self._owner() is None:
            raise RuntimeError("RKNN IO buffer has been released")

    @property
    def fd(self):
        self._check()
        return int(self._mem.contents.fd)

    @property
    def offset(self):
        self._check()
        return int(self._mem.contents.offset)

    @property
    def size(self):
        self._check()
        return int(self._mem.contents.size)

    def describe(self):
        self._check()
        return {"fd": self.fd, "offset": self.offset, "size": self.size,
                "shape": self.shape, "strides": self.strides,
                "dtype": self.dtype}

    def _array(self):
        self._check()
        # virt_addr already addresses this tensor; offset describes fd mappings.
        storage = (ctypes.c_ubyte * self.size).from_address(
            self._mem.contents.virt_addr)
        return np.ndarray(self.shape, dtype=self.dtype, buffer=storage,
                          strides=self.strides)


_TYPE_NAMES = {
    0: "float32", 1: "float16", 2: "int8", 3: "uint8", 4: "int16",
    5: "uint16", 6: "int32", 7: "uint32", 8: "int64", 9: "bool",
    10: "int4", 11: "bfloat16",
}
_FMT_NAMES = {0: "NCHW", 1: "NHWC", 2: "NC1HWC2", 3: "UNDEFINED"}

_lib = None
_lib_path = ""


def library_path() -> str:
    """First existing candidate, or "" -- used to decide whether to even try."""
    for name in LIB_CANDIDATES:
        if os.path.exists(name):
            return name
    return ""


def _load():
    """dlopen ``librknnrt`` once and prototype every entry point used here."""
    global _lib, _lib_path
    if _lib is not None:
        return _lib
    path = library_path()
    if not path:
        raise OSError(
            "librknnrt.so not found in " + ", ".join(LIB_CANDIDATES)
        )
    lib = ctypes.CDLL(path, mode=ctypes.RTLD_GLOBAL)

    lib.rknn_init.restype = ctypes.c_int
    lib.rknn_init.argtypes = [
        ctypes.POINTER(rknn_context), ctypes.c_void_p, ctypes.c_uint32,
        ctypes.c_uint32, ctypes.c_void_p,
    ]
    lib.rknn_destroy.restype = ctypes.c_int
    lib.rknn_destroy.argtypes = [rknn_context]

    lib.rknn_query.restype = ctypes.c_int
    lib.rknn_query.argtypes = [rknn_context, ctypes.c_int, ctypes.c_void_p,
                               ctypes.c_uint32]

    lib.rknn_inputs_set.restype = ctypes.c_int
    lib.rknn_inputs_set.argtypes = [rknn_context, ctypes.c_uint32,
                                    ctypes.POINTER(RknnInput)]

    lib.rknn_run.restype = ctypes.c_int
    lib.rknn_run.argtypes = [rknn_context, ctypes.c_void_p]

    lib.rknn_outputs_get.restype = ctypes.c_int
    lib.rknn_outputs_get.argtypes = [rknn_context, ctypes.c_uint32,
                                     ctypes.POINTER(RknnOutput),
                                     ctypes.c_void_p]
    lib.rknn_outputs_release.restype = ctypes.c_int
    lib.rknn_outputs_release.argtypes = [rknn_context, ctypes.c_uint32,
                                         ctypes.POINTER(RknnOutput)]

    # Optional when no explicit core mask was requested.
    try:
        lib.rknn_set_core_mask.restype = ctypes.c_int
        lib.rknn_set_core_mask.argtypes = [rknn_context, ctypes.c_int]
    except AttributeError:
        pass

    # Older runtimes may expose only the general API. Never partially use these
    # functions without the complete allocation/binding/synchronization surface.
    optional = {
        "rknn_create_mem": (ctypes.POINTER(RknnTensorMem),
                            [rknn_context, ctypes.c_uint32]),
        "rknn_destroy_mem": (ctypes.c_int,
                             [rknn_context, ctypes.POINTER(RknnTensorMem)]),
        "rknn_set_io_mem": (ctypes.c_int,
                            [rknn_context, ctypes.POINTER(RknnTensorMem),
                             ctypes.POINTER(RknnTensorAttr)]),
        "rknn_mem_sync": (ctypes.c_int,
                          [rknn_context, ctypes.POINTER(RknnTensorMem), ctypes.c_int]),
    }
    for name, (restype, argtypes) in optional.items():
        try:
            fn = getattr(lib, name)
        except AttributeError:
            continue
        fn.restype, fn.argtypes = restype, argtypes

    _lib = lib
    _lib_path = path
    return lib


def _attr_dict(attr: RknnTensorAttr) -> dict:
    return {
        "index": attr.index,
        "name": attr.name.decode("utf-8", "replace"),
        "dims": [attr.dims[i] for i in range(attr.n_dims)],
        "n_elems": attr.n_elems,
        "size": attr.size,
        "fmt": _FMT_NAMES.get(attr.fmt, attr.fmt),
        "type": _TYPE_NAMES.get(attr.type, attr.type),
        "zp": attr.zp,
        "scale": attr.scale,
    }


class CtypesRknnModel:
    """One ``rknn_context``, same surface as ``RknnLiteModel``.

    ``infer(uint8_NHWC) -> list[np.ndarray]`` of dequantized float32 tensors
    shaped by the graph's declared output dims.

    One ``rknn_input`` array and one ``rknn_output`` array are allocated at
    runtime initialization and reused for every call. Native work is deferred
    until init_runtime() so callers can acquire their driver lock first.
    """

    backend = "ctypes"

    def __init__(self, path: str = "", core_mask: Optional[int] = None,
                 io_mode: Optional[str] = None):
        # Match RKNNLite's two phase API.  The daemon constructs backends
        # outside its driver lock; no native call is allowed here.
        self.path = str(path)
        self.core_mask = core_mask
        self.lib = None
        self.lib_path = ""
        self.ctx = rknn_context(0)
        self._released = False
        # Set before the first failure can happen, so ``release`` (and
        # ``__del__`` via it) never trips over a half-built object.
        self._inputs = None
        self._outputs = None

        self._blob = None
        self.sdk = {}
        self.input_attrs = ()
        self.output_attrs = ()
        self.n_input = 0
        self.n_output = 0
        self._out_shapes = []
        self._input_shape = ()
        self._ready = False
        self._output_release_failed = False
        self._native_cleanup_failed = False
        self.requested_io_mode = (io_mode if io_mode is not None else
                                  os.getenv("ESK_RKNN_IO_MODE", "auto"))
        if self.requested_io_mode not in ("auto", "bound", "legacy"):
            raise ValueError("io_mode must be auto, bound or legacy")
        self.io_mode = "legacy"
        self.io_fallback_reason = ""
        self._io_buffers = []
        self._default_input = None
        self._default_outputs = []
        self._active_input = None
        self._active_outputs = []
        self._bound_input_attr = None
        self._bound_output_attrs = []

    def load_rknn(self, path: str) -> int:
        """Record the model path; defer all native work to init_runtime."""
        if self._released:
            raise RuntimeError("load_rknn() on a released model")
        if self.ctx.value:
            raise RuntimeError("cannot replace a live native model")
        self.path = os.fspath(path)
        return RKNN_SUCC

    def init_runtime(self, *, core_mask: Optional[int] = None, **kwargs) -> int:
        self._initialize_context(core_mask=core_mask, **kwargs)
        if self.requested_io_mode == "legacy":
            return RKNN_SUCC
        required = ("rknn_create_mem", "rknn_destroy_mem", "rknn_set_io_mem",
                    "rknn_mem_sync")
        if not all(callable(getattr(self.lib, name, None)) for name in required):
            self.io_fallback_reason = "runtime lacks the complete bound IO API"
            if self.requested_io_mode == "bound":
                self._ready = False
                raise _BoundIOUnavailable(self.io_fallback_reason)
            return RKNN_SUCC
        self._ready = False
        try:
            self._initialize_bound_io()
        except _BoundIOUnavailable as exc:
            self.io_fallback_reason = str(exc)
            # A rejected set_io_mem may have changed native state. General and
            # zero-copy calls must NEVER share that context (RKNN guide §5.2).
            self._destroy_context()
            if self.requested_io_mode == "bound":
                raise
            self._initialize_context(core_mask=core_mask, **kwargs)
            return RKNN_SUCC
        self.io_mode = "bound"
        self._ready = True
        return RKNN_SUCC

    def _initialize_context(self, *, core_mask: Optional[int] = None, **kwargs) -> int:
        """Create/query the native context lazily, using the RKNNLite contract."""
        if self._released:
            raise RuntimeError("init_runtime() on a released model")
        if self.ctx.value:
            raise RuntimeError("native context already exists")
        if not self.path:
            raise RuntimeError("init_runtime() requires load_rknn() first")
        self.lib = _load()
        self.lib_path = _lib_path
        self.core_mask = self.core_mask if core_mask is None else core_mask
        with open(self.path, "rb") as fh:
            blob = fh.read()
        # Held for the object's life. The vendor frees its own copy right after
        # rknn_init, so this is belt-and-braces -- but a freed buffer the runtime
        # did retain a pointer into is not a failure mode worth discovering
        # during a multi-hour run.
        self._blob = ctypes.create_string_buffer(blob, len(blob))
        ret = self.lib.rknn_init(ctypes.byref(self.ctx), self._blob,
                                 len(blob), 0, None)
        if ret != RKNN_SUCC:
            raise RuntimeError(f"rknn_init failed for {self.path!r}: ret={ret}")

        if self.core_mask is not None:
            if not hasattr(self.lib, "rknn_set_core_mask"):
                raise RuntimeError("runtime does not support an explicit core mask")
            ret = self.lib.rknn_set_core_mask(self.ctx, int(self.core_mask))
            if ret != RKNN_SUCC:
                raise RuntimeError(
                    f"rknn_set_core_mask({self.core_mask}) failed: ret={ret}"
                )

        ver = RknnSdkVersion()
        if self.lib.rknn_query(self.ctx, RKNN_QUERY_SDK_VERSION,
                               ctypes.byref(ver),
                               ctypes.sizeof(ver)) == RKNN_SUCC:
            self.sdk = {"api": ver.api_version.decode("utf-8", "replace"),
                        "drv": ver.drv_version.decode("utf-8", "replace")}
        else:
            self.sdk = {}

        io = RknnInputOutputNum()
        ret = self.lib.rknn_query(self.ctx, RKNN_QUERY_IN_OUT_NUM,
                                  ctypes.byref(io), ctypes.sizeof(io))
        if ret != RKNN_SUCC:
            raise RuntimeError(f"rknn_query(IN_OUT_NUM) failed: ret={ret}")
        self.n_input = int(io.n_input)
        self.n_output = int(io.n_output)

        self.input_attrs = (RknnTensorAttr * self.n_input)()
        for i in range(self.n_input):
            self.input_attrs[i].index = i
            ret = self.lib.rknn_query(
                self.ctx, RKNN_QUERY_INPUT_ATTR,
                ctypes.byref(self.input_attrs[i]),
                ctypes.sizeof(RknnTensorAttr),
            )
            if ret != RKNN_SUCC:
                raise RuntimeError(
                    f"rknn_query(INPUT_ATTR {i}) failed: ret={ret}")

        self.output_attrs = (RknnTensorAttr * self.n_output)()
        for i in range(self.n_output):
            self.output_attrs[i].index = i
            ret = self.lib.rknn_query(
                self.ctx, RKNN_QUERY_OUTPUT_ATTR,
                ctypes.byref(self.output_attrs[i]),
                ctypes.sizeof(RknnTensorAttr),
            )
            if ret != RKNN_SUCC:
                raise RuntimeError(
                    f"rknn_query(OUTPUT_ATTR {i}) failed: ret={ret}")

        self._inputs = (RknnInput * self.n_input)()
        self._outputs = (RknnOutput * self.n_output)()
        # Declared output dims, cached once. rknnlite reshapes to exactly these,
        # and every head decode in this repo is written against that shape.
        self._out_shapes = [
            tuple(self.output_attrs[i].dims[d]
                  for d in range(self.output_attrs[i].n_dims))
            for i in range(self.n_output)
        ]
        if self.n_input != 1:
            raise RuntimeError(f"ctypes backend requires one input, got {self.n_input}")
        attr = self.input_attrs[0]
        if attr.n_dims != 4 or attr.fmt not in (RKNN_TENSOR_NHWC, RKNN_TENSOR_NCHW):
            raise RuntimeError("ctypes backend requires a static 4D image model")
        shape = tuple(int(attr.dims[i]) for i in range(4))
        if not all(shape):
            raise RuntimeError("ctypes backend does not support dynamic input shapes")
        # Graph storage may be int8/float16 and NCHW. pass_through=0 asks RKNN
        # to convert the caller's uint8 NHWC pixels to that internal format.
        self._input_shape = (shape if attr.fmt == RKNN_TENSOR_NHWC
                             else (shape[0], shape[2], shape[3], shape[1]))
        self._ready = True
        return RKNN_SUCC

    @property
    def native_cleanup_failed(self):
        """A native memory/context destruction failed; retain driver quarantine.

        This lifetime latch is distinct from a failed run/bind/sync, which fences
        this model but does not by itself prove that cleanup is uncertain. It
        also covers failures inside allocation rollback, before a buffer can be
        returned to the caller. A later successful release does not clear it.
        """
        return self._native_cleanup_failed

    @property
    def shared_io_size_bytes(self):
        """One private IO set's observed size; recheck actual new allocations."""
        if self.io_mode != "bound" or not self._ready:
            return 0
        return self._default_input.size + sum(buf.size for buf in self._default_outputs)

    @property
    def output_specs(self):
        """Stable caller output contract, including for legacy runtimes."""
        return [{"shape": shape, "dtype": "float32",
                 "size": math.prod(shape) * 4,
                 "strides": self._compact_strides(shape, 4)}
                for shape in self._out_shapes]

    @staticmethod
    def _compact_strides(shape, itemsize):
        strides = []
        for extent in reversed(shape):
            strides.append(itemsize)
            itemsize *= extent
        return tuple(reversed(strides))

    def _query_native(self, command, index):
        attr = RknnTensorAttr(index=index)
        ret = self.lib.rknn_query(self.ctx, command, ctypes.byref(attr),
                                  ctypes.sizeof(attr))
        if ret != RKNN_SUCC:
            raise _BoundIOUnavailable(f"native tensor query {command}/{index}: ret={ret}")
        return attr

    def _initialize_bound_io(self):
        # Rockchip User Guide 2.3.2 §5.2.3: query NATIVE attrs before binding.
        # Image input UINT8 is supported for native INT8/FLOAT16 graphs. Keep
        # pass_through=0 so the runtime retains the model's normalization.
        native = self._query_native(RKNN_QUERY_NATIVE_INPUT_ATTR, 0)
        n, h, w, c = self._input_shape
        if (n != 1 or c not in (1, 3, 4) or native.n_dims != 4
                or native.fmt != RKNN_TENSOR_NHWC
                or tuple(native.dims[:4]) != self._input_shape
                or native.type not in (RKNN_TENSOR_UINT8, RKNN_TENSOR_INT8,
                                       RKNN_TENSOR_FLOAT16)):
            raise _BoundIOUnavailable("native input is not a supported static NHWC image")
        w_stride = int(native.w_stride) or w
        h_stride = int(native.h_stride) or h
        if w_stride < w or h_stride < h or (w_stride * c) % 16:
            raise _BoundIOUnavailable("native input stride violates RV1126B alignment")
        if n * h_stride * w_stride * c >= 2**32:
            raise _BoundIOUnavailable("native input allocation exceeds RKNN size limit")
        native.type = RKNN_TENSOR_UINT8
        native.pass_through = 0
        native.size = n * h * w * c
        native.size_with_stride = n * h_stride * w_stride * c
        native.h_stride = h_stride
        self._bound_input_attr = native
        self._input_strides = (h_stride * w_stride * c, w_stride * c, c, 1)

        attrs = []
        for i, shape in enumerate(self._out_shapes):
            logical = self.output_attrs[i]
            if (not shape or len(shape) > RKNN_MAX_DIMS or not all(shape)
                    or math.prod(shape) != logical.n_elems
                    or math.prod(shape) * 4 >= 2**32):
                raise _BoundIOUnavailable("invalid static output dimensions")
            native_out = self._query_native(RKNN_QUERY_NATIVE_OUTPUT_ATTR, i)
            if native_out.type not in (RKNN_TENSOR_INT8, RKNN_TENSOR_FLOAT16,
                                       RKNN_TENSOR_FLOAT32):
                raise _BoundIOUnavailable("unsupported native output dtype")
            if len(shape) == 4:
                if logical.fmt not in (RKNN_TENSOR_NCHW, RKNN_TENSOR_NHWC):
                    raise _BoundIOUnavailable("unsupported declared output layout")
                if logical.fmt == RKNN_TENSOR_NHWC and native_out.fmt != logical.fmt:
                    native_out = self._query_native(RKNN_QUERY_NATIVE_NHWC_OUTPUT_ATTR, i)
                    if native_out.type not in (RKNN_TENSOR_INT8, RKNN_TENSOR_FLOAT16,
                                               RKNN_TENSOR_FLOAT32):
                        raise _BoundIOUnavailable("unsupported native NHWC output dtype")
                if native_out.fmt == RKNN_TENSOR_NC1HWC2:
                    # The runtime supports conversion to NCHW (guide table 5-5).
                    if logical.fmt != RKNN_TENSOR_NCHW or native_out.n_dims != 5:
                        raise _BoundIOUnavailable("unsupported blocked output layout")
                    bn, c1, bh, bw, c2 = tuple(native_out.dims[:5])
                    if (bn, bh, bw) != (shape[0], shape[2], shape[3]) or c1 * c2 < shape[1]:
                        raise _BoundIOUnavailable("native output dimensions disagree")
                elif (native_out.fmt != logical.fmt or native_out.n_dims != 4
                      or tuple(native_out.dims[:4]) != shape):
                    raise _BoundIOUnavailable("native output layout/dimensions disagree")
            elif (native_out.fmt != RKNN_TENSOR_UNDEFINED
                  or native_out.n_dims != len(shape)
                  or tuple(native_out.dims[:len(shape)]) != shape):
                raise _BoundIOUnavailable("unsupported non-image output layout")
            # RV1126B explicitly supports FLOAT32 bound outputs for INT8/FP16
            # models (guide tables 5-4/5-6). After dtype/layout changes use the
            # NEW compact size, not the padded native INT8/NC1HWC2 size.
            attr = RknnTensorAttr.from_buffer_copy(logical)
            attr.type = RKNN_TENSOR_FLOAT32
            attr.fmt = logical.fmt if len(shape) == 4 else RKNN_TENSOR_UNDEFINED
            attr.size = math.prod(shape) * 4
            attr.size_with_stride = attr.size
            attr.w_stride = attr.h_stride = 0
            attr.pass_through = 0
            attrs.append(attr)
        self._bound_output_attrs = attrs
        self._default_input = self._new_input_buffer()
        self._default_outputs = self._new_output_buffers()
        try:
            self._bind_buffers(self._default_input, self._default_outputs)
        except RuntimeError as exc:
            raise _BoundIOUnavailable(str(exc)) from exc

    def _allocate_buffer(self, attr, *, kind, shape, strides, dtype):
        size = int(attr.size_with_stride or attr.size)
        if size <= 0:
            raise _BoundIOUnavailable("invalid bound tensor allocation size")
        mem = self.lib.rknn_create_mem(self.ctx, size)
        if not mem:
            raise _BoundIOUnavailable("rknn_create_mem failed")
        buf = RknnIOBuffer(self, mem, kind=kind, index=int(attr.index),
                           shape=shape, strides=strides, dtype=dtype)
        # Track even a malformed allocation so cleanup never leaks it.
        self._io_buffers.append(buf)
        if not mem.contents.virt_addr or buf.fd < 0 or buf.offset < 0 or buf.size < size:
            raise _BoundIOUnavailable("rknn_create_mem returned unusable DMA memory")
        return buf

    def _new_input_buffer(self):
        return self._allocate_buffer(self._bound_input_attr, kind="input",
                                     shape=self._input_shape,
                                     strides=self._input_strides, dtype="uint8")

    def _new_output_buffers(self):
        return [self._allocate_buffer(attr, kind="output", shape=spec["shape"],
                                      strides=spec["strides"], dtype="float32")
                for attr, spec in zip(self._bound_output_attrs, self.output_specs)]

    def _require_ready(self, *, bound=False):
        if self._released or not self._ready or self._output_release_failed:
            raise RuntimeError("native model is not ready or requires teardown")
        if bound and self.io_mode != "bound":
            raise RuntimeError("native model does not support bound IO")

    def _validate_buffer(self, buf, kind, index=0):
        if (not isinstance(buf, RknnIOBuffer) or buf._owner() is not self
                or buf.released or buf not in self._io_buffers
                or buf.kind != kind or buf.index != index):
            raise ValueError("invalid, foreign or released RKNN IO buffer")

    def allocate_input_buffer(self):
        """Allocate private DMA input for one client; caller holds driver lock."""
        self._require_ready(bound=True)
        before = len(self._io_buffers)
        try:
            return self._new_input_buffer()
        except Exception:
            for buf in list(reversed(self._io_buffers[before:])):
                self._destroy_buffer(buf)
            raise

    def allocate_output_buffers(self):
        """Allocate all float32 outputs for one client; caller holds driver lock."""
        self._require_ready(bound=True)
        before = len(self._io_buffers)
        try:
            return self._new_output_buffers()
        except Exception:
            # Preserve pointers on destroy failure so release() can retry.
            for buf in list(reversed(self._io_buffers[before:])):
                self._destroy_buffer(buf)
            raise

    def export_input_buffer(self):
        """Borrow the private default input descriptor for in-process use only."""
        self._require_ready(bound=True)
        return self._default_input.describe()

    def _bind_buffers(self, input_buffer, output_buffers):
        if self._active_input is not input_buffer:
            ret = self.lib.rknn_set_io_mem(self.ctx, input_buffer._mem,
                                           ctypes.byref(self._bound_input_attr))
            if ret != RKNN_SUCC:
                self._ready = False
                raise RuntimeError(f"rknn_set_io_mem input failed: ret={ret}")
            self._active_input = input_buffer
        while len(self._active_outputs) < self.n_output:
            self._active_outputs.append(None)
        for i, buf in enumerate(output_buffers):
            if self._active_outputs[i] is buf:
                continue
            ret = self.lib.rknn_set_io_mem(self.ctx, buf._mem,
                                           ctypes.byref(self._bound_output_attrs[i]))
            if ret != RKNN_SUCC:
                self._ready = False
                raise RuntimeError(f"rknn_set_io_mem output {i} failed: ret={ret}")
            self._active_outputs[i] = buf

    def _destroy_buffer(self, buf):
        try:
            ret = self.lib.rknn_destroy_mem(self.ctx, buf._mem)
        except BaseException:
            self._ready = False
            self._native_cleanup_failed = True
            raise
        if ret != RKNN_SUCC:
            self._ready = False
            self._native_cleanup_failed = True
            raise RuntimeError(f"rknn_destroy_mem failed: ret={ret}")
        buf.released = True
        self._io_buffers.remove(buf)

    def release_input_buffer(self, buf):
        if isinstance(buf, RknnIOBuffer) and buf._owner() is self and buf.released:
            return
        self._validate_buffer(buf, "input")
        if buf is self._default_input:
            raise ValueError("default IO buffers are released with the model")
        if self._active_input is buf:
            self._bind_buffers(self._default_input, self._active_outputs)
        self._destroy_buffer(buf)

    def release_output_buffers(self, buffers):
        if not isinstance(buffers, (list, tuple)) or len(buffers) != self.n_output:
            raise ValueError("output buffer count does not match model")
        for i, buf in enumerate(buffers):
            if isinstance(buf, RknnIOBuffer) and buf._owner() is self and buf.released:
                continue
            self._validate_buffer(buf, "output", i)
            if buf is self._default_outputs[i]:
                raise ValueError("default IO buffers are released with the model")
        if any(buf is active for buf, active in zip(buffers, self._active_outputs)):
            self._bind_buffers(self._active_input, self._default_outputs)
        for buf in buffers:
            if not buf.released:
                self._destroy_buffer(buf)

    def _validate_destinations(self, outputs):
        if not isinstance(outputs, (list, tuple)) or len(outputs) != self.n_output:
            raise ValueError("output destination count does not match model")
        for target, shape in zip(outputs, self._out_shapes):
            if (not isinstance(target, np.ndarray) or target.dtype != np.float32
                    or target.shape != shape or not target.flags.c_contiguous
                    or not target.flags.writeable):
                raise ValueError("output destinations must be writable compact float32 model tensors")

    def infer_dma_buffers(self, input_buffer, output_buffers):
        """Run into caller-private DMA outputs, under the caller's driver lock.

        External RGA/CPU input writes must have completed and flushed before this
        call. Outputs are cache-synchronized before return. Clients must finish
        reading before reuse; the API never transfers allocation ownership.
        """
        self._require_ready(bound=True)
        self._validate_buffer(input_buffer, "input")
        if not isinstance(output_buffers, (list, tuple)) or len(output_buffers) != self.n_output:
            raise ValueError("output buffer count does not match model")
        for i, buf in enumerate(output_buffers):
            self._validate_buffer(buf, "output", i)
        self._bind_buffers(input_buffer, output_buffers)
        try:
            ret = self.lib.rknn_mem_sync(self.ctx, input_buffer._mem,
                                         RKNN_MEMORY_SYNC_TO_DEVICE)
            if ret != RKNN_SUCC:
                raise RuntimeError(f"rknn_mem_sync input failed: ret={ret}")
            ret = self.lib.rknn_run(self.ctx, None)
            if ret != RKNN_SUCC:
                raise RuntimeError(f"rknn_run failed: ret={ret}")
            for buf in output_buffers:
                ret = self.lib.rknn_mem_sync(self.ctx, buf._mem,
                                             RKNN_MEMORY_SYNC_FROM_DEVICE)
                if ret != RKNN_SUCC:
                    raise RuntimeError(f"rknn_mem_sync output failed: ret={ret}")
        except Exception:
            self._ready = False
            raise

    def infer_dma_into(self, input_buffer, outputs):
        self._require_ready(bound=True)
        self._validate_destinations(outputs)
        self.infer_dma_buffers(input_buffer, self._default_outputs)
        for buf, target in zip(self._default_outputs, outputs):
            np.copyto(target, buf._array())
        return outputs

    def infer_bound_into(self, outputs):
        """Run after filling the private default input; synchronous use only."""
        return self.infer_dma_into(self._default_input, outputs)

    def infer_into(self, input_uint8, outputs):
        """Fill caller-owned float32 destinations (including shared memory)."""
        self._require_ready()
        self._validate_destinations(outputs)
        if self.io_mode == "bound":
            arr = self._prepare_input(input_uint8)
            np.copyto(self._default_input._array(), arr)
            return self.infer_dma_into(self._default_input, outputs)
        for value, target in zip(self.infer(input_uint8), outputs):
            np.copyto(target, value)
        return outputs

    def _prepare_input(self, input_uint8):
        arr = np.asarray(input_uint8)
        if arr.ndim == 3:
            arr = np.expand_dims(arr, 0)
        if arr.dtype != np.uint8:
            raise TypeError(f"ctypes backend requires uint8 input, got {arr.dtype}")
        if arr.shape != self._input_shape:
            raise ValueError(f"input shape {arr.shape} != model shape {self._input_shape}")
        return arr

    # ---------------------------------------------------------------- infer

    def infer(self, input_uint8) -> List[np.ndarray]:
        """One forward pass. Input is uint8 NHWC; outputs are float32."""
        self._require_ready()
        if self.io_mode == "bound":
            out = [np.empty(shape, dtype=np.float32) for shape in self._out_shapes]
            return self.infer_into(input_uint8, out)
        lib = self.lib
        arr = np.ascontiguousarray(self._prepare_input(input_uint8))

        inp = self._inputs[0]
        inp.index = 0
        inp.buf = arr.ctypes.data_as(ctypes.c_void_p)
        inp.size = arr.nbytes
        inp.pass_through = 0
        inp.type = RKNN_TENSOR_UINT8
        inp.fmt = RKNN_TENSOR_NHWC
        ret = lib.rknn_inputs_set(self.ctx, self.n_input, self._inputs)
        if ret != RKNN_SUCC:
            raise RuntimeError(f"rknn_inputs_set failed: ret={ret}")

        ret = lib.rknn_run(self.ctx, None)
        if ret != RKNN_SUCC:
            raise RuntimeError(f"rknn_run failed: ret={ret}")

        for i in range(self.n_output):
            self._outputs[i].want_float = 1
            self._outputs[i].is_prealloc = 0
            self._outputs[i].index = i
            self._outputs[i].buf = None
            self._outputs[i].size = 0
        ret = lib.rknn_outputs_get(self.ctx, self.n_output, self._outputs, None)
        if ret != RKNN_SUCC:
            raise RuntimeError(f"rknn_outputs_get failed: ret={ret}")

        try:
            out = []
            for i in range(self.n_output):
                o = self._outputs[i]
                # Copy before release: the header is explicit that the buffer is
                # freed by rknn_outputs_release, so a np.frombuffer view over
                # the raw pointer would alias freed memory.
                a = np.frombuffer(ctypes.string_at(o.buf, o.size),
                                  dtype=np.float32)
                shape = self._out_shapes[i]
                if not shape or a.size != int(np.prod(shape)):
                    raise RuntimeError("native output size does not match its declared shape")
                a = a.reshape(shape)
                out.append(a)
        finally:
            # In the finally block on purpose: an exception between _get and
            # _release would skip the release, which is an actual C-level leak
            # -- worse than the cyclic garbage this class exists to avoid.
            self._output_release_failed = True
            rel = lib.rknn_outputs_release(self.ctx, self.n_output, self._outputs)
            if rel != RKNN_SUCC:
                raise RuntimeError(f"rknn_outputs_release failed: ret={rel}")
            self._output_release_failed = False
        return out

    def inference(self, inputs) -> List[np.ndarray]:
        """RKNNLite-compatible sequence API used by sessions and the daemon."""
        if not isinstance(inputs, (list, tuple)) or len(inputs) != 1:
            raise ValueError("ctypes inference requires exactly one input array")
        return self.infer(inputs[0])

    # -------------------------------------------------------------- teardown

    def describe(self) -> dict:
        return {
            "backend": "ctypes",
            "io_mode": self.io_mode,
            "io_fallback_reason": self.io_fallback_reason,
            "lib": self.lib_path,
            "path": self.path,
            "sdk": self.sdk,
            "n_input": self.n_input,
            "n_output": self.n_output,
            "inputs": [_attr_dict(self.input_attrs[i])
                       for i in range(self.n_input)],
            "outputs": [_attr_dict(self.output_attrs[i])
                        for i in range(self.n_output)],
            "pid": os.getpid(),
        }

    def _destroy_context(self):
        self._ready = False
        if not self.ctx.value:
            return
        if self.lib is None:
            self._native_cleanup_failed = True
            raise RuntimeError("live native context has no runtime library")
        # Remove each pointer only after confirmed destruction. If native
        # cleanup fails, no inference/fallback is allowed and release can retry
        # without double-freeing the allocations already destroyed.
        for buf in list(reversed(self._io_buffers)):
            self._destroy_buffer(buf)
        self._default_input = self._active_input = None
        self._default_outputs = []
        self._active_outputs = []
        try:
            ret = self.lib.rknn_destroy(self.ctx)
        except BaseException:
            self._native_cleanup_failed = True
            raise
        if ret != RKNN_SUCC:
            self._native_cleanup_failed = True
            # Keep ctx and _released false so the caller can retry safely.
            raise RuntimeError(f"rknn_destroy failed: ret={ret}")
        self.ctx = rknn_context(0)
        self.io_mode = "legacy"

    def release(self) -> None:
        """Destroy all IO and the context; failed native cleanup is retryable."""
        if self._released:
            return
        self._destroy_context()
        self._released = True
        self._ready = False
        self._blob = None
        return RKNN_SUCC

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()

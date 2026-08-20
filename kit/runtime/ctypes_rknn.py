"""``librknnrt.so`` driven directly from Python, without rknn_toolkit_lite2.

Why this exists: ``RKNNLite.inference()`` leaves one set of reference cycles
per call, holding that call's ctypes buffers (the ``c_char_Array`` input copy,
one ctypes array per output tensor, ``RKNNRtTensorAttr`` instances). Reference
cycles are invisible to refcounting, so nothing is reclaimed until a cyclic
collection runs, and at default ``gc`` thresholds the collector does not keep
up. On the nine-output YOLOv8 graph the intrusion-detection app runs, RSS grows
**43.8 kB per call** -- ~2.2 MB/min at 18.8 fps, an OOM in hours on a 2 GB
board, and the reason a long-running vision app on this device had to be
restarted periodically.

That is cyclic garbage, not a C-level leak: a diagnostic calling
``gc.collect()`` every 100 inferences returns RSS to exactly 64632 kB on every
collection, and running the repro with ``--gc-every 5`` drops the rate 269x
(42.4321 -> 0.1578 kB/inference). ``librknnrt`` itself is clean -- this module
performs exactly the same ``librknnrt`` sequence the Cython extension performs
(``rknn_inputs_set`` + ``rknn_run`` + ``rknn_outputs_get`` +
``rknn_outputs_release`` with ``want_float=1``, precisely the entry points
named in the extension's ``.dynstr``) against the same graph, with the same
input, sampled by the same harness, and 6465 iterations moved the ``[heap]``
VMA by zero bytes with no ``gc`` involved.

So there are two ways out, and this module is one of them. Periodic
``gc.collect()`` in every inference loop works too, at ~23% on inference
latency (p50 19.8 -> 24.3 ms). Bypassing the wrapper avoids the cycles at the
source, needs no ``gc`` call in caller code, and measures a *lower* p50 than
rknnlite; the cost is maintaining these bindings and the equivalence test
below.

Numerically this is a no-op by construction: ``want_float=1`` is what makes the
runtime dequantize into float32, which is the same thing ``RKNNLite.inference()``
asks for, and the returned buffer is reshaped to the graph's declared output
dims. That was verified element-by-element against rknnlite before this became
the default -- see ``kit/runtime/engine.py`` for the switch and
``ESK_RKNN_BACKEND`` for how to go back.

Every ``restype``/``argtypes`` below is load-bearing. ctypes types an
unprototyped return as C ``int``, which on aarch64 truncates a returned pointer
to 32 bits; a truncated ``rknn_tensor_mem *`` dereference takes the interpreter
down rather than returning an error.

``/dev/rknpu`` is mode 0600 root:root, so anything using this has to run as
root -- the same constraint rknnlite has, since the permission denial surfaces
from ``rknn_init`` either way ("failed to open rknpu module, need to insmod
rknpu driver", which reads like a missing kernel module and is not).
"""

from __future__ import annotations

import ctypes
import os
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

# rknn_tensor_type / rknn_tensor_format
RKNN_TENSOR_UINT8 = 3
RKNN_TENSOR_NHWC = 1

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

    # Optional on some runtime builds -- probed, not assumed, because a missing
    # symbol here must degrade to "no core mask" rather than kill the load.
    try:
        lib.rknn_set_core_mask.restype = ctypes.c_int
        lib.rknn_set_core_mask.argtypes = [rknn_context, ctypes.c_int]
    except AttributeError:
        pass

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
    shaped by the graph's declared output dims -- which is what
    ``RKNNLite.inference()`` returns, verified element-by-element.

    One ``rknn_input`` array and one ``rknn_output`` array are allocated at
    construction and reused for every call, so the wrapper itself allocates
    nothing per inference beyond the output copies it hands back.
    """

    backend = "ctypes"

    def __init__(self, path: str, core_mask: Optional[int] = None):
        self.path = path
        self.lib = _load()
        self.lib_path = _lib_path
        self.ctx = rknn_context(0)
        self._released = False
        # Set before the first failure can happen, so ``release`` (and
        # ``__del__`` via it) never trips over a half-built object.
        self._inputs = None
        self._outputs = None

        with open(path, "rb") as fh:
            blob = fh.read()
        # Held for the object's life. The vendor frees its own copy right after
        # rknn_init, so this is belt-and-braces -- but a freed buffer the runtime
        # did retain a pointer into is not a failure mode worth discovering
        # during a multi-hour run.
        self._blob = ctypes.create_string_buffer(blob, len(blob))
        ret = self.lib.rknn_init(ctypes.byref(self.ctx), self._blob,
                                 len(blob), 0, None)
        if ret != RKNN_SUCC:
            raise RuntimeError(f"rknn_init failed for {path!r}: ret={ret}")

        if core_mask is not None and hasattr(self.lib, "rknn_set_core_mask"):
            ret = self.lib.rknn_set_core_mask(self.ctx, int(core_mask))
            if ret != RKNN_SUCC:
                self.release()
                raise RuntimeError(
                    f"rknn_set_core_mask({core_mask}) failed: ret={ret}"
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
            self.release()
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
                self.release()
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
                self.release()
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

    # ---------------------------------------------------------------- infer

    def infer(self, input_uint8) -> List[np.ndarray]:
        """One forward pass. Input is uint8 NHWC; outputs are float32."""
        if self._released:
            raise RuntimeError("infer() on a released model")
        lib = self.lib
        arr = np.asarray(input_uint8)
        if arr.ndim == 3:
            arr = np.expand_dims(arr, 0)
        if arr.dtype != np.uint8:
            arr = arr.astype(np.uint8)
        arr = np.ascontiguousarray(arr)

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
                if shape and a.size == int(np.prod(shape)):
                    a = a.reshape(shape)
                out.append(a)
        finally:
            # In the finally block on purpose: an exception between _get and
            # _release would skip the release, which is an actual C-level leak
            # -- worse than the cyclic garbage this class exists to avoid.
            rel = lib.rknn_outputs_release(self.ctx, self.n_output,
                                           self._outputs)
            if rel != RKNN_SUCC:
                raise RuntimeError(f"rknn_outputs_release failed: ret={rel}")
        return out

    # -------------------------------------------------------------- teardown

    def describe(self) -> dict:
        return {
            "backend": "ctypes",
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

    def release(self) -> None:
        """Destroy the context. Idempotent; safe on a half-built object."""
        if self._released:
            return
        self._released = True
        try:
            if self.ctx.value:
                self.lib.rknn_destroy(self.ctx)
                self.ctx = rknn_context(0)
        except Exception:
            pass

    def __del__(self):
        # An app that forgets to release() would otherwise hold the NPU context
        # until process exit, which on a single-core NPU blocks the next app.
        try:
            self.release()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()

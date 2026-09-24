"""Exercise real ctypes cycles without requiring the target vendor binary."""

import ctypes
import gc
import sys
import types
import weakref

import numpy as np
import pytest

from kit.runtime.engine import ModelSpec, TensorSpec, _runtime_for_spec
from kit.runtime.rknnlite import RknnLiteRuntime


class CyclicVendor:
    def __init__(self, *args, **kwargs):
        self.options = (args, kwargs)
        self.buffers = []
        self.rknn_data = object()
        self.infer_mode = "ok"
        self.init_error = None
        self.release_result = None

    def _buffer(self):
        buf = (ctypes.c_float * 6)(*range(6))
        buf.cycle = buf
        self.buffers.append(weakref.ref(buf))
        return buf

    def init_runtime(self, **kwargs):
        self._buffer()
        if self.init_error is not None:
            raise self.init_error
        return 0

    def load_rknn(self, path):
        self.path = path
        return 0

    def inference(self, inputs, **kwargs):
        if self.infer_mode != "ok":
            self._buffer()
            if self.infer_mode == "raise":
                raise RuntimeError("vendor failed")
            return None
        self.last_inputs = inputs
        out = np.ctypeslib.as_array(self._buffer()).reshape(2, 3)
        # Model the actual vendor-owned cyclic backing store, plus other
        # tensor dtypes and optional frame-id metadata.
        return [out, np.array([2, 7], dtype=np.int64), 42]

    def release(self):
        self._buffer()
        if isinstance(self.release_result, BaseException):
            raise self.release_result
        return self.release_result


@pytest.fixture
def vendor(monkeypatch):
    api = types.ModuleType("rknnlite.api")
    api.RKNNLite = CyclicVendor
    monkeypatch.setitem(sys.modules, "rknnlite.api", api)
    was_enabled = gc.isenabled()
    gc.disable()  # Explicit boundary collection must work without auto GC.
    try:
        yield
    finally:
        gc.collect()
        if was_enabled:
            gc.enable()


def test_returns_owned_unchanged_outputs_and_collects_vendor_cycles(vendor):
    runtime = RknnLiteRuntime(verbose=False)
    assert runtime.load_rknn("voice.rknn") == 0
    assert runtime.init_runtime() == 0
    assert runtime.path == "voice.rknn"
    assert runtime.options == ((), {"verbose": False})
    inputs = [np.ones((1, 344, 560), dtype=np.float32)]
    retained = []
    for _ in range(20):
        out = runtime.inference(inputs=inputs)
        assert runtime.last_inputs is inputs
        assert all(ref() is None for ref in runtime.buffers)
        assert out[0].flags.owndata
        assert out[0].dtype == np.float32
        assert out[1].dtype == np.int64
        assert out[2] == 42
        retained.append(out[0])
    runtime.release()
    assert runtime.rknn_data is None
    assert all(ref() is None for ref in runtime.buffers)
    for out in retained:
        np.testing.assert_array_equal(out, np.arange(6).reshape(2, 3))


@pytest.mark.parametrize("mode", ["none", "raise"])
def test_failed_inference_also_collects_unreachable_buffers(vendor, mode):
    runtime = RknnLiteRuntime()
    runtime._runtime.infer_mode = mode
    if mode == "raise":
        with pytest.raises(RuntimeError, match="vendor failed"):
            runtime.inference(inputs=[])
    else:
        assert runtime.inference(inputs=[]) is None
    assert all(ref() is None for ref in runtime.buffers)


def test_failed_initialization_also_collects_and_can_be_released(vendor):
    runtime = RknnLiteRuntime()
    runtime._runtime.init_error = RuntimeError("init failed")
    with pytest.raises(RuntimeError, match="init failed"):
        runtime.init_runtime()
    assert all(ref() is None for ref in runtime.buffers)
    runtime.release()
    assert runtime.rknn_data is None


@pytest.mark.parametrize("result", [9, RuntimeError("destroy failed")])
def test_failed_release_preserves_model_for_quarantine_and_retry(vendor, result):
    runtime = RknnLiteRuntime()
    data = runtime.rknn_data
    runtime._runtime.release_result = result
    if isinstance(result, BaseException):
        with pytest.raises(RuntimeError, match="destroy failed"):
            runtime.release()
    else:
        assert runtime.release() == 9
    assert runtime.rknn_data is data
    assert all(ref() is None for ref in runtime.buffers)
    runtime._runtime.release_result = 0
    assert runtime.release() == 0
    assert runtime.rknn_data is None


@pytest.mark.parametrize("inputs", [
    (),
    (TensorSpec("image", (1, 8, 8, 3)),
     TensorSpec("aux", (1, 2), "float32", "NC")),
])
def test_auto_fallback_contracts_use_collected_backend(vendor, monkeypatch, inputs):
    monkeypatch.setenv("ESK_RKNN_BACKEND", "auto")
    runtime = _runtime_for_spec(ModelSpec("model.rknn", inputs=inputs))
    assert isinstance(runtime, RknnLiteRuntime)


@pytest.mark.parametrize("spec", [
    ModelSpec("image.rknn", inputs=(TensorSpec("image", (1, 8, 8, 3)),)),
    ModelSpec("speech.rknn", inputs=(TensorSpec("speech", (1, 344, 560), "float32", "NTF"),)),
])
def test_ctypes_selection_and_explicit_lite_is_protected(vendor, monkeypatch, spec):
    from kit.runtime.ctypes_rknn import CtypesRknnModel

    monkeypatch.setenv("ESK_RKNN_BACKEND", "auto")
    assert isinstance(_runtime_for_spec(spec), CtypesRknnModel)
    monkeypatch.setenv("ESK_RKNN_BACKEND", "rknnlite")
    assert isinstance(_runtime_for_spec(spec), RknnLiteRuntime)


def test_service_does_not_collect_twice_for_protected_runtime(vendor, monkeypatch):
    from contextlib import nullcontext
    from market.inferenced.server import RknnBackend

    runtime = RknnLiteRuntime()
    collect = gc.collect
    calls = []

    def counted_collect(*args):
        calls.append(args)
        return collect(*args)

    monkeypatch.setattr(gc, "collect", counted_collect)
    backend = RknnBackend(coordinator=types.SimpleNamespace(hold=nullcontext))
    out = backend.infer(runtime, [np.ones((1, 344, 560), dtype=np.float32)])
    assert len(calls) == 1
    assert all(ref() is None for ref in runtime.buffers)
    np.testing.assert_array_equal(out[0], np.arange(6).reshape(2, 3))

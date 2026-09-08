import ctypes

import numpy as np
import pytest

from kit.runtime import ctypes_rknn
from kit.runtime import engine
from kit.errors import ConfigurationError, ModelLoadError


class FakeLib:
    def __init__(self, *, destroy_results=(0,), output_release=0):
        self.destroy_results = list(destroy_results)
        self.output_release = output_release
        self.destroy_calls = 0
        self.output_release_calls = 0
        self._output = (ctypes.c_float * 2)(1.0, 2.0)

    def rknn_init(self, ctx, blob, size, flags, cfg):
        ctx._obj.value = 7
        return 0

    def rknn_destroy(self, ctx):
        self.destroy_calls += 1
        return self.destroy_results.pop(0) if self.destroy_results else 0

    def rknn_query(self, ctx, cmd, value, size):
        if cmd == ctypes_rknn.RKNN_QUERY_IN_OUT_NUM:
            value._obj.n_input = 1
            value._obj.n_output = 1
        elif cmd == ctypes_rknn.RKNN_QUERY_INPUT_ATTR:
            value._obj.n_dims = 4
            value._obj.dims[0:4] = (1, 1, 1, 1)
            value._obj.type = ctypes_rknn.RKNN_TENSOR_UINT8
            value._obj.fmt = ctypes_rknn.RKNN_TENSOR_NHWC
        elif cmd == ctypes_rknn.RKNN_QUERY_OUTPUT_ATTR:
            value._obj.n_dims = 1
            value._obj.dims[0] = 2
        return 0

    def rknn_inputs_set(self, *args):
        self.input_args = args
        return 0

    def rknn_run(self, *args):
        return 0

    def rknn_outputs_get(self, ctx, count, outputs, userdata):
        outputs[0].buf = ctypes.cast(self._output, ctypes.c_void_p)
        outputs[0].size = ctypes.sizeof(self._output)
        return 0

    def rknn_outputs_release(self, *args):
        self.output_release_calls += 1
        return self.output_release


class ImageFakeLib(FakeLib):
    """Native-shaped fake that records the ABI input descriptor."""

    def __init__(self, *, input_dims=(1, 3, 2, 2), input_fmt=None,
                 query_error=None, output_size=None, destroy_results=(0,),
                 output_release=0):
        super().__init__(destroy_results=destroy_results,
                         output_release=output_release)
        self.input_dims = input_dims
        self.input_fmt = (ctypes_rknn.RKNN_TENSOR_NCHW
                          if input_fmt is None else input_fmt)
        self.query_error = query_error
        self.output_size = output_size
        self.input_descriptor = None

    def rknn_query(self, ctx, cmd, value, size):
        if self.query_error == cmd:
            return 77
        if cmd == ctypes_rknn.RKNN_QUERY_IN_OUT_NUM:
            value._obj.n_input = 1
            value._obj.n_output = 1
        elif cmd == ctypes_rknn.RKNN_QUERY_INPUT_ATTR:
            value._obj.n_dims = 4
            value._obj.dims[0:4] = self.input_dims
            value._obj.type = 2  # RKNN_TENSOR_INT8
            value._obj.fmt = self.input_fmt
        elif cmd == ctypes_rknn.RKNN_QUERY_OUTPUT_ATTR:
            value._obj.n_dims = 1
            value._obj.dims[0] = 2
        return 0

    def rknn_inputs_set(self, ctx, count, inputs):
        self.input_descriptor = inputs[0]
        return 0

    def rknn_outputs_get(self, ctx, count, outputs, userdata):
        outputs[0].buf = ctypes.cast(self._output, ctypes.c_void_p)
        outputs[0].size = (ctypes.sizeof(self._output) if self.output_size is None
                           else self.output_size)
        return 0


def test_ctypes_model_is_lazy_and_destroy_failure_is_retryable(tmp_path, monkeypatch):
    model_path = tmp_path / "model.rknn"
    model_path.write_bytes(b"rknn")
    lib = FakeLib(destroy_results=(9, 0))
    monkeypatch.setattr(ctypes_rknn, "_load", lambda: lib)
    monkeypatch.setattr(ctypes_rknn, "_lib_path", "/fake/librknnrt.so")

    model = ctypes_rknn.CtypesRknnModel()
    assert model.ctx.value == 0
    assert model.load_rknn(str(model_path)) == 0
    assert model.ctx.value == 0
    assert model.init_runtime() == 0
    assert np.allclose(model.inference(inputs=[np.zeros((1, 1, 1, 1), np.uint8)])[0], [1, 2])
    with pytest.raises(RuntimeError, match="destroy"):
        model.release()
    assert model.ctx.value == 7
    assert not model._released
    assert model.release() == 0
    assert model.ctx.value == 0
    assert lib.destroy_calls == 2


def test_ctypes_model_rejects_non_uint8_without_conversion(tmp_path, monkeypatch):
    model_path = tmp_path / "model.rknn"
    model_path.write_bytes(b"rknn")
    lib = FakeLib()
    monkeypatch.setattr(ctypes_rknn, "_load", lambda: lib)
    model = ctypes_rknn.CtypesRknnModel()
    model.load_rknn(str(model_path))
    model.init_runtime()
    with pytest.raises(TypeError, match="uint8"):
        model.inference(inputs=[np.zeros((1, 1, 1, 1), np.float32)])


def test_output_release_failure_is_propagated(tmp_path, monkeypatch):
    model_path = tmp_path / "model.rknn"
    model_path.write_bytes(b"rknn")
    lib = FakeLib(output_release=12)
    monkeypatch.setattr(ctypes_rknn, "_load", lambda: lib)
    model = ctypes_rknn.CtypesRknnModel()
    model.load_rknn(str(model_path))
    model.init_runtime()
    with pytest.raises(RuntimeError, match="outputs_release"):
        model.inference(inputs=[np.zeros((1, 1, 1, 1), np.uint8)])
    assert lib.output_release_calls == 1


def _loaded_ctypes_model(tmp_path, monkeypatch, lib):
    path = tmp_path / "image.rknn"
    path.write_bytes(b"rknn")
    monkeypatch.setattr(ctypes_rknn, "_load", lambda: lib)
    model = ctypes_rknn.CtypesRknnModel()
    model.load_rknn(path)
    assert model.init_runtime() == 0
    return model


def test_nchw_graph_accepts_uint8_nhwc_and_sets_native_descriptor(tmp_path, monkeypatch):
    lib = ImageFakeLib()
    model = _loaded_ctypes_model(tmp_path, monkeypatch, lib)
    try:
        result = model.inference(inputs=[np.zeros((1, 2, 2, 3), np.uint8)])
        assert result[0].shape == (2,)
        descriptor = lib.input_descriptor
        assert descriptor.pass_through == 0
        assert descriptor.type == ctypes_rknn.RKNN_TENSOR_UINT8
        assert descriptor.fmt == ctypes_rknn.RKNN_TENSOR_NHWC
        assert descriptor.size == 1 * 2 * 2 * 3
    finally:
        model.release()


@pytest.mark.parametrize(
    "spec",
    [
        engine.ModelSpec("image.rknn", inputs=(engine.TensorSpec(
            "image", (1, 2, 2, 3), "uint8", "NHWC"),)),
    ],
)
def test_auto_selects_ctypes_for_static_uint8_nhwc(monkeypatch, spec):
    monkeypatch.setenv("ESK_RKNN_BACKEND", "auto")
    monkeypatch.setattr(ctypes_rknn, "library_path", lambda: "/fake/rt.so")
    runtime = engine._runtime_for_spec(spec)
    assert isinstance(runtime, ctypes_rknn.CtypesRknnModel)


def test_backend_selection_explicit_rknnlite_and_unsupported_contracts(monkeypatch):
    sentinel = object()
    monkeypatch.setenv("ESK_RKNN_BACKEND", "rknnlite")
    monkeypatch.setattr(engine, "_default_runtime_factory", lambda: sentinel)
    assert engine._runtime_for_spec(engine.ModelSpec("x.rknn")) is sentinel

    float_spec = engine.ModelSpec("x.rknn", inputs=(engine.TensorSpec(
        "image", (1, 2, 2, 3), "float32", "NHWC"),))
    monkeypatch.setenv("ESK_RKNN_BACKEND", "auto")
    monkeypatch.setattr(engine, "_default_runtime_factory", lambda: sentinel)
    assert engine._runtime_for_spec(float_spec) is sentinel
    multi = engine.ModelSpec("x.rknn", inputs=(
        engine.TensorSpec("a", (1, 2, 2, 3)),
        engine.TensorSpec("b", (1, 2, 2, 3)),
    ))
    with pytest.raises(ConfigurationError, match="one static uint8 NHWC"):
        monkeypatch.setenv("ESK_RKNN_BACKEND", "ctypes")
        engine._runtime_for_spec(multi)


def test_init_runtime_cannot_replace_or_reinitialize_live_context(tmp_path, monkeypatch):
    model = _loaded_ctypes_model(tmp_path, monkeypatch, FakeLib())
    try:
        with pytest.raises(RuntimeError, match="already exists"):
            model.init_runtime()
        with pytest.raises(RuntimeError, match="replace a live"):
            model.load_rknn(tmp_path / "other.rknn")
    finally:
        model.release()


def test_bad_output_shape_releases_native_outputs(tmp_path, monkeypatch):
    lib = ImageFakeLib(output_size=ctypes.sizeof(ctypes.c_float))
    model = _loaded_ctypes_model(tmp_path, monkeypatch, lib)
    try:
        with pytest.raises(RuntimeError, match="output size"):
            model.inference(inputs=[np.zeros((1, 2, 2, 3), np.uint8)])
        assert lib.output_release_calls == 1
    finally:
        model.release()


def test_output_release_failure_fences_followup_inference(tmp_path, monkeypatch):
    lib = ImageFakeLib(output_release=12)
    model = _loaded_ctypes_model(tmp_path, monkeypatch, lib)
    try:
        image = np.zeros((1, 2, 2, 3), np.uint8)
        with pytest.raises(RuntimeError, match="outputs_release"):
            model.inference(inputs=[image])
        with pytest.raises(RuntimeError, match="not ready"):
            model.inference(inputs=[image])
    finally:
        # output release failure does not imply destroy failure.
        model.release()


def test_explicit_core_mask_requires_native_symbol(tmp_path, monkeypatch):
    model = ctypes_rknn.CtypesRknnModel(core_mask=3)
    path = tmp_path / "mask.rknn"
    path.write_bytes(b"rknn")
    lib = FakeLib()
    monkeypatch.setattr(ctypes_rknn, "_load", lambda: lib)
    model.load_rknn(path)
    with pytest.raises(RuntimeError, match="explicit core mask"):
        model.init_runtime()
    assert model.ctx.value == 7
    model.release()


def test_native_input_shape_is_checked_before_driver(tmp_path, monkeypatch):
    lib = ImageFakeLib()
    model = _loaded_ctypes_model(tmp_path, monkeypatch, lib)
    try:
        with pytest.raises(ValueError, match="input shape"):
            model.inference(inputs=[np.zeros((1, 3, 2, 2), np.uint8)])
        assert not hasattr(lib, "input_descriptor") or lib.input_descriptor is None
    finally:
        model.release()

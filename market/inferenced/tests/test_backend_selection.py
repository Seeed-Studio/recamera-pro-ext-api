"""Daemon backend choice for authorized models, with host-only native fakes."""

import ctypes
import gc
import importlib.util
import pathlib
from contextlib import contextmanager

import numpy as np
import pytest

from kit.runtime import ctypes_rknn as r
from kit.runtime import engine
from kit.errors import InputValidationError
from kit.runtime.engine import ModelSpec, TensorSpec
from kit.runtime.remote import RemoteRknnModel, RemoteRknnSession
from market.inferenced import server
from market.inferenced.server import RknnBackend
from market.inferenced.tests.test_service import RunningService, model

_BOUND_IO_TESTS = (pathlib.Path(__file__).resolve().parents[3]
                   / "kit" / "tests" / "test_ctypes_bound_io.py")
_spec = importlib.util.spec_from_file_location("_bound_io_fakes", _BOUND_IO_TESTS)
_bound_io = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bound_io)
DmaLib = _bound_io.DmaLib


class Coordinator:
    def __init__(self):
        self.retained = []

    @contextmanager
    def hold(self, timeout=30.0):
        coordinator = self

        class Token:
            def retain_fail_closed(self, reason):
                coordinator.retained.append(reason)

        yield Token()


class GraphLib:
    """General-API fake whose graph inputs are read back by rknn_query."""

    def __init__(self, inputs=((1, 344, 560),), *, fmt=r.RKNN_TENSOR_UNDEFINED,
                 dtype=r.RKNN_TENSOR_FLOAT16, destroy_result=0):
        self.inputs = inputs
        self.fmt = fmt
        self.dtype = dtype
        self.destroy_result = destroy_result
        self.destroyed = 0
        self.inputs_set = []
        self._output = (ctypes.c_float * 2)(1.0, 2.0)

    def rknn_init(self, ctx, blob, size, flags, cfg):
        ctx._obj.value = 5
        return 0

    def rknn_destroy(self, ctx):
        self.destroyed += 1
        return self.destroy_result

    def rknn_query(self, ctx, cmd, value, size):
        obj = value._obj
        if cmd == r.RKNN_QUERY_IN_OUT_NUM:
            obj.n_input, obj.n_output = len(self.inputs), 1
        elif cmd == r.RKNN_QUERY_INPUT_ATTR:
            dims = self.inputs[obj.index]
            obj.n_dims = len(dims)
            obj.dims[:len(dims)] = dims
            obj.fmt, obj.type = self.fmt, self.dtype
        elif cmd == r.RKNN_QUERY_OUTPUT_ATTR:
            obj.n_dims = 1
            obj.dims[0] = 2
        return 0

    def rknn_inputs_set(self, ctx, count, inputs):
        self.inputs_set.append((inputs[0].type, inputs[0].size))
        return 0

    def rknn_run(self, *args):
        return 0

    def rknn_outputs_get(self, ctx, count, outputs, userdata):
        outputs[0].buf = ctypes.cast(self._output, ctypes.c_void_p)
        outputs[0].size = ctypes.sizeof(self._output)
        return 0

    def rknn_outputs_release(self, *args):
        return 0


class FakeRknnLite:
    def __init__(self):
        self.loaded = None

    def load_rknn(self, path):
        self.loaded = path
        return 0

    def init_runtime(self, **kwargs):
        return 0

    def inference(self, *, inputs):
        return [np.zeros((1, 2), np.float32)]

    def release(self):
        return 0


@pytest.fixture
def native(monkeypatch, tmp_path):
    monkeypatch.delenv("ESK_RKNN_BACKEND", raising=False)
    monkeypatch.delenv("ESK_RKNN_IO_MODE", raising=False)
    monkeypatch.setattr(r, "library_path", lambda: "/fake/librknnrt.so")
    lites = []

    def factory():
        lites.append(FakeRknnLite())
        return lites[-1]

    monkeypatch.setattr(server, "_default_runtime_factory", factory)
    monkeypatch.setattr(engine, "_default_runtime_factory", factory)
    path = tmp_path / "m.rknn"
    path.write_bytes(b"rknn")

    def install(lib):
        monkeypatch.setattr(r, "_load", lambda: lib)
        return lib

    return install, lites, str(path)


UNDECLARED = {"name": "m", "inputs": [], "outputs": []}


def test_undeclared_single_float_input_uses_ctypes_general_io(native):
    install, lites, path = native
    lib = install(GraphLib())
    backend = RknnBackend(coordinator=Coordinator())
    handle = backend.load(path, UNDECLARED)
    try:
        assert isinstance(handle, r.CtypesRknnModel)
        assert handle.io_mode == "legacy"
        assert backend.backend_name(handle) == "ctypes"
        assert backend.shared_io_size(handle) == 0
        out = backend.infer(handle, [np.zeros((1, 344, 560), np.float32)])
        assert out[0].shape == (2,)
        assert lib.inputs_set == [(r.RKNN_TENSOR_FLOAT32, 344 * 560 * 4)]
        assert lites == []
    finally:
        backend.release(handle)


@pytest.mark.parametrize("graph", [
    dict(inputs=((1, 3), (1, 4))),                  # two inputs
    dict(inputs=((1, 0, 560),)),                    # dynamic dimension
    dict(inputs=((1, 8),), fmt=r.RKNN_TENSOR_NC1HWC2),
    dict(inputs=((1, 8),), dtype=6),                # RKNN_TENSOR_INT32
])
def test_undeclared_unsupported_graph_releases_ctypes_and_uses_rknnlite(native, graph):
    install, lites, path = native
    lib = install(GraphLib(**graph))
    coordinator = Coordinator()
    backend = RknnBackend(coordinator=coordinator)
    handle = backend.load(path, UNDECLARED)
    assert lib.destroyed == 1
    assert handle is lites[0] and handle.loaded == path
    assert backend.backend_name(handle) == "rknnlite"
    assert coordinator.retained == []


def test_failed_ctypes_teardown_does_not_fall_back(native):
    install, lites, path = native
    install(GraphLib(inputs=((1, 3), (1, 4)), destroy_result=9))
    coordinator = Coordinator()
    with pytest.raises(r.CtypesUnsupportedModel):
        RknnBackend(coordinator=coordinator).load(path, UNDECLARED)
    assert lites == []
    assert coordinator.retained


def test_undeclared_non_capability_failure_stays_fail_closed(native):
    install, lites, path = native
    lib = install(GraphLib())
    lib.rknn_init = lambda *args: 3
    with pytest.raises(RuntimeError, match="rknn_init failed"):
        RknnBackend(coordinator=Coordinator()).load(path, UNDECLARED)
    assert lites == []


def test_rknnlite_fallback_handles_are_still_collected(native, monkeypatch):
    install, lites, path = native
    install(GraphLib(inputs=((1, 3), (1, 4))))
    backend = RknnBackend(coordinator=Coordinator())
    handle = backend.load(path, UNDECLARED)
    calls = []
    monkeypatch.setattr(gc, "collect", lambda *args: calls.append(args) or 0)
    backend.infer(handle, [np.zeros((1, 3), np.float32), np.zeros((1, 4), np.float32)])
    assert calls == [()]


def test_declared_uint8_nhwc_keeps_bound_io(native):
    install, lites, path = native
    install(DmaLib())
    backend = RknnBackend(coordinator=Coordinator())
    spec = {"name": "det", "inputs": [
        {"name": "images", "shape": [1, 2, 5, 3], "dtype": "uint8", "layout": "NHWC"}]}
    handle = backend.load(path, spec)
    try:
        assert handle.io_mode == "bound"
        assert backend.shared_io_size(handle) > 0
        assert backend.backend_name(handle) == "ctypes"
    finally:
        backend.release(handle)


def test_undeclared_image_model_stays_on_rknnlite(native):
    # Without a declared uint8 contract the caller may send float pixels,
    # which ctypes rejects for image graphs; RKNNLite accepted them before.
    install, lites, path = native
    lib = install(DmaLib())
    coordinator = Coordinator()
    backend = RknnBackend(coordinator=coordinator)
    handle = backend.load(path, UNDECLARED)
    assert handle is lites[0]
    assert backend.backend_name(handle) == "rknnlite"
    assert lib.allocations == 0
    assert backend.shared_io_size(handle) == 0
    assert coordinator.retained == []
    backend.infer(handle, [np.zeros((1, 2, 5, 3), np.float32)])


def test_env_rknnlite_forces_rknnlite_for_undeclared(native, monkeypatch):
    install, lites, path = native
    lib = install(GraphLib())
    monkeypatch.setenv("ESK_RKNN_BACKEND", "rknnlite")
    handle = RknnBackend(coordinator=Coordinator()).load(path, UNDECLARED)
    assert handle is lites[0]
    assert lib.destroyed == 0


def test_env_ctypes_forces_ctypes_without_fallback(native, monkeypatch):
    install, lites, path = native
    install(GraphLib())
    monkeypatch.setenv("ESK_RKNN_BACKEND", "ctypes")
    backend = RknnBackend(coordinator=Coordinator())
    handle = backend.load(path, UNDECLARED)
    assert backend.backend_name(handle) == "ctypes"
    backend.release(handle)

    lib = install(GraphLib(inputs=((1, 3), (1, 4))))
    with pytest.raises(r.CtypesUnsupportedModel):
        backend.load(path, UNDECLARED)
    assert lib.destroyed == 1
    assert lites == []


def test_missing_runtime_library_keeps_rknnlite(native, monkeypatch):
    install, lites, path = native
    install(GraphLib())
    monkeypatch.setattr(r, "library_path", lambda: "")
    handle = RknnBackend(coordinator=Coordinator()).load(path, UNDECLARED)
    assert handle is lites[0]


@pytest.mark.parametrize("value", [
    np.zeros((1, 100, 560), np.float32),
    np.zeros((344, 560), np.float32),
    np.zeros((1, 344, 560), np.float64),
    np.zeros((1, 344, 560), np.int32),
])
def test_undeclared_ctypes_rejects_mismatched_tensor_before_driver(native, value):
    install, lites, path = native
    lib = install(GraphLib())
    backend = RknnBackend(coordinator=Coordinator())
    handle = backend.load(path, UNDECLARED)
    try:
        with pytest.raises(ValueError):
            backend.infer(handle, [value])
        with pytest.raises(ValueError):
            backend.infer(handle, [np.zeros((1, 344, 560), np.float32)] * 2)
        assert lib.inputs_set == []
    finally:
        backend.release(handle)


def test_service_reports_backend_and_maps_contract_mismatch(native, tmp_path):
    install, lites, _ = native
    lib = install(GraphLib())
    running = RunningService(tmp_path, backend=RknnBackend(coordinator=Coordinator()))
    path, digest = model(tmp_path, "asr.rknn")
    sessions = []

    def session(frames):
        # The client-side spec only shapes the request; the daemon checks
        # the undeclared model against the graph itself.
        spec = ModelSpec(str(path), name="asr", inputs=(
            TensorSpec("speech", (1, frames, 560), "float32", "NTF"),))
        sessions.append(RemoteRknnSession(spec, socket_path=running.socket,
                                          model_sha256=digest, memory_mb=32))
        return sessions[-1]

    try:
        good = session(344)
        assert good.infer(np.zeros((1, 344, 560), np.float32))[0].shape == (2,)
        status = running.service.status()
        assert [item["backend"] for item in status["models"]] == ["ctypes"]
        with pytest.raises(InputValidationError, match="model shape"):
            session(3).infer(np.zeros((1, 3, 560), np.float32))
        assert len(lib.inputs_set) == 1
    finally:
        for item in sessions:
            item.release()
        running.close()


def test_fake_backend_status_names_backend(tmp_path):
    running = RunningService(tmp_path)
    path, digest = model(tmp_path)
    try:
        remote = RemoteRknnModel(str(path), socket_path=running.socket,
                                 model_sha256=digest, memory_mb=32)
        assert running.service.status()["models"][0]["backend"] == "fake"
        remote.release()
    finally:
        running.close()


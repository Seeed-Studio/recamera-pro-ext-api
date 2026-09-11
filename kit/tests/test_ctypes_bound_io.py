"""Host checks of the actual RKNN IO ABI/lifecycle, without an NPU runtime."""

import ctypes
import math

import numpy as np
import pytest

from kit.runtime import ctypes_rknn as r


class DmaLib:
    """Real ctypes-backed allocations; errors can occur at native boundaries."""

    def __init__(self, *, fail_bind=0, fail_alloc=0, bad_native=False,
                 fail_mem_destroy=0, fail_destroy=0):
        self.fail_bind = fail_bind
        self.fail_alloc = fail_alloc
        self.fail_mem_destroy = fail_mem_destroy
        self.fail_destroy = fail_destroy
        self.bad_native = bad_native
        self.fail_sync = 0
        self.fail_run = False
        self.events = []
        self.contexts = 0
        self.allocations = 0
        self.bindings = 0
        self.mem_destroys = 0
        self.live = {}
        self.bound = {}
        self.output_shapes = ((1, 2, 2, 5), (3,))
        self.output_float = [np.arange(math.prod(s), dtype=np.float32).reshape(s)
                             for s in self.output_shapes]

    def rknn_init(self, ctx, *args):
        self.contexts += 1
        ctx._obj.value = self.contexts
        self.bound[self.contexts] = {}
        self.events.append(("init", self.contexts))
        return 0

    def rknn_destroy(self, ctx):
        self.events.append(("destroy", ctx.value))
        if self.fail_destroy:
            self.fail_destroy -= 1
            return 9
        return 0

    @staticmethod
    def _attr(attr, shape, fmt, dtype):
        attr.n_dims = len(shape)
        attr.dims[:len(shape)] = shape
        attr.fmt, attr.type = fmt, dtype
        attr.n_elems = math.prod(shape)
        attr.size = attr.n_elems * (2 if dtype == r.RKNN_TENSOR_FLOAT16 else 1)
        attr.size_with_stride = attr.size

    def rknn_query(self, ctx, cmd, value, size):
        attr = value._obj
        if cmd == r.RKNN_QUERY_SDK_VERSION:
            attr.api_version = b"2.3.2"
        elif cmd == r.RKNN_QUERY_IN_OUT_NUM:
            attr.n_input, attr.n_output = 1, len(self.output_shapes)
        elif cmd == r.RKNN_QUERY_INPUT_ATTR:
            self._attr(attr, (1, 3, 2, 5), r.RKNN_TENSOR_NCHW, r.RKNN_TENSOR_INT8)
        elif cmd == r.RKNN_QUERY_NATIVE_INPUT_ATTR:
            self._attr(attr, (1, 2, 5, 3), r.RKNN_TENSOR_NHWC, r.RKNN_TENSOR_INT8)
            attr.w_stride, attr.h_stride = 16, 3
            attr.size_with_stride = 16 * 3 * 3
            if self.bad_native:
                attr.type = 6  # integer tensor, not normalized RGB input
        elif cmd == r.RKNN_QUERY_OUTPUT_ATTR:
            i = attr.index
            self._attr(attr, self.output_shapes[i],
                       r.RKNN_TENSOR_NCHW if i == 0 else r.RKNN_TENSOR_UNDEFINED,
                       r.RKNN_TENSOR_INT8 if i == 0 else r.RKNN_TENSOR_FLOAT16)
            attr.zp, attr.scale, attr.qnt_type = -10, 0.125, 2
        elif cmd == r.RKNN_QUERY_NATIVE_OUTPUT_ATTR:
            i = attr.index
            self._attr(attr, (1, 1, 2, 5, 4) if i == 0 else (3,),
                       r.RKNN_TENSOR_NC1HWC2 if i == 0 else r.RKNN_TENSOR_UNDEFINED,
                       r.RKNN_TENSOR_INT8 if i == 0 else r.RKNN_TENSOR_FLOAT16)
        else:
            return -1
        return 0

    def rknn_create_mem(self, ctx, size):
        self.allocations += 1
        self.events.append(("alloc", ctx.value, size))
        if self.allocations == self.fail_alloc:
            return None
        storage = (ctypes.c_ubyte * size)(*([0xA5] * size))
        mem = r.RknnTensorMem(virt_addr=ctypes.addressof(storage),
                              fd=100 + self.allocations, size=size)
        pointer = ctypes.pointer(mem)
        self.live[mem.fd] = (storage, pointer)
        return pointer

    def rknn_destroy_mem(self, ctx, pointer):
        self.mem_destroys += 1
        fd = pointer.contents.fd
        self.events.append(("free", ctx.value, fd))
        if self.mem_destroys == self.fail_mem_destroy:
            return 7
        storage, _ = self.live.pop(fd)
        ctypes.memset(ctypes.addressof(storage), 0xCC, len(storage))
        return 0

    def rknn_set_io_mem(self, ctx, pointer, attr_pointer):
        self.bindings += 1
        attr = r.RknnTensorAttr.from_buffer_copy(attr_pointer._obj)
        self.events.append(("bind", ctx.value, pointer.contents.fd, attr))
        if self.bindings == self.fail_bind:
            return 8
        key = "input" if attr.type == r.RKNN_TENSOR_UINT8 else attr.index
        self.bound[ctx.value][key] = pointer, attr
        return 0

    def rknn_mem_sync(self, ctx, pointer, direction):
        self.events.append(("sync", ctx.value, pointer.contents.fd, direction))
        return 5 if direction == self.fail_sync else 0

    def rknn_run(self, ctx, _):
        self.events.append(("run", ctx.value))
        if self.fail_run:
            return 6
        entries = self.bound[ctx.value]
        if not entries:
            return 0
        pointer, attr = entries["input"]
        image = np.ndarray((1, 2, 5, 3), dtype=np.uint8,
                           buffer=self.live[pointer.contents.fd][0],
                           strides=(attr.h_stride * attr.w_stride * 3,
                                    attr.w_stride * 3, 3, 1))
        self.last_image = image.copy()
        for i, shape in enumerate(self.output_shapes):
            pointer, attr = entries[i]
            assert attr.type == r.RKNN_TENSOR_FLOAT32
            assert tuple(attr.dims[:attr.n_dims]) == shape
            assert attr.size == math.prod(shape) * 4
            assert attr.w_stride == attr.h_stride == 0
            target = np.ndarray(shape, dtype=np.float32,
                                buffer=self.live[pointer.contents.fd][0])
            # Stand-in for the runtime's documented FLOAT32/layout conversion.
            target[:] = self.output_float[i] + image[0, 0, 0, 0]
        return 0

    def rknn_inputs_set(self, ctx, *args):
        self.events.append(("inputs_set", ctx.value))
        return 0

    def rknn_outputs_get(self, ctx, count, outputs, _):
        self.events.append(("outputs_get", ctx.value))
        for i, arr in enumerate(self.output_float):
            outputs[i].buf = arr.ctypes.data
            outputs[i].size = arr.nbytes
        return 0

    def rknn_outputs_release(self, ctx, *args):
        self.events.append(("outputs_release", ctx.value))
        return 0


def model_for(tmp_path, monkeypatch, lib=None, mode="auto"):
    lib = lib or DmaLib()
    monkeypatch.setattr(r, "_load", lambda: lib)
    path = tmp_path / "bound.rknn"
    path.write_bytes(b"rknn")
    model = r.CtypesRknnModel(str(path), io_mode=mode)
    model.init_runtime()
    return model, lib


def image(value=7):
    return np.full((1, 2, 5, 3), value, dtype=np.uint8)


def test_bound_reuses_allocations_preserves_float_shape_and_owned_results(tmp_path, monkeypatch):
    model, lib = model_for(tmp_path, monkeypatch)
    assert model.io_mode == "bound"
    assert model.describe()["io_mode"] == "bound"
    assert lib.allocations == 3
    first = model.infer(image(7))
    second = model.inference([image(9)])
    assert lib.allocations == 3
    for i, arr in enumerate(first):
        assert arr.dtype == np.float32
        assert arr.shape == lib.output_shapes[i]
        np.testing.assert_array_equal(arr, lib.output_float[i] + 7)
        np.testing.assert_array_equal(second[i], lib.output_float[i] + 9)
        assert not np.shares_memory(arr, second[i])
    assert not any(event[0] in ("inputs_set", "outputs_get", "outputs_release")
                   for event in lib.events)
    model.release()
    np.testing.assert_array_equal(first[0], lib.output_float[0] + 7)
    assert not lib.live


def test_input_copy_honors_horizontal_and_vertical_stride_and_noncontiguous_input(tmp_path, monkeypatch):
    model, lib = model_for(tmp_path, monkeypatch)
    src = np.arange(60, dtype=np.uint8).reshape(1, 2, 10, 3)[:, :, ::2]
    assert not src.flags.c_contiguous
    model.infer(src)
    np.testing.assert_array_equal(lib.last_image, src)
    buf = model._default_input
    storage = np.frombuffer(lib.live[buf.fd][0], dtype=np.uint8).reshape(3, 48)
    assert np.all(storage[:2, 15:] == 0xA5)
    assert np.all(storage[2] == 0xA5)
    assert buf.describe() == {"fd": buf.fd, "offset": 0, "size": 144,
                              "shape": (1, 2, 5, 3), "strides": (144, 48, 3, 1),
                              "dtype": "uint8"}
    model.release()


def test_private_dma_clients_do_not_overwrite_each_other_and_sync_before_read(tmp_path, monkeypatch):
    model, lib = model_for(tmp_path, monkeypatch)
    in_a, in_b = model.allocate_input_buffer(), model.allocate_input_buffer()
    out_a, out_b = model.allocate_output_buffers(), model.allocate_output_buffers()
    assert model.shared_io_size_bytes == 144 + 80 + 12
    assert sum(x.size for x in [in_a, *out_a]) == model.shared_io_size_bytes
    assert model.output_specs[0] == {"shape": (1, 2, 2, 5), "dtype": "float32",
                                     "size": 80, "strides": (80, 40, 20, 4)}
    in_a._array()[:] = 4
    in_b._array()[:] = 8
    count = lib.allocations
    model.infer_dma_buffers(in_a, out_a)
    model.infer_dma_buffers(in_b, out_b)
    model.infer(image(12))  # legacy caller surface restores private defaults
    assert lib.allocations == count
    for i in range(2):
        np.testing.assert_array_equal(out_a[i]._array(), lib.output_float[i] + 4)
        np.testing.assert_array_equal(out_b[i]._array(), lib.output_float[i] + 8)
    for i, event in enumerate(lib.events):
        if event[0] == "run":
            assert lib.events[i - 1][0] == "sync"
            assert lib.events[i - 1][-1] == r.RKNN_MEMORY_SYNC_TO_DEVICE
            assert [e[-1] for e in lib.events[i + 1:i + 3]] == [2, 2]
    model.release_input_buffer(in_a)
    model.release_output_buffers(out_a)
    model.release_input_buffer(in_a)  # idempotent
    model.release_output_buffers(out_a)
    with pytest.raises(RuntimeError, match="released"):
        in_a.describe()
    with pytest.raises(ValueError, match="released"):
        model.infer_dma_buffers(in_a, out_b)
    model.release()
    assert in_b.released and all(x.released for x in out_b)
    assert not lib.live


def test_shared_numpy_destinations_and_dma_input_use_same_float_contract(tmp_path, monkeypatch):
    model, lib = model_for(tmp_path, monkeypatch)
    target = [np.empty(s, dtype=np.float32) for s in lib.output_shapes]
    assert model.infer_into(image(3), target) is target
    np.testing.assert_array_equal(target[0], lib.output_float[0] + 3)
    buf = model.allocate_input_buffer()
    buf._array()[:] = 5
    assert model.infer_dma_into(buf, target) is target
    np.testing.assert_array_equal(target[0], lib.output_float[0] + 5)
    model.release()


@pytest.mark.parametrize("kwargs", [{"fail_bind": 2}, {"fail_alloc": 2}, {"bad_native": True}])
def test_auto_fallback_uses_fresh_context_and_frees_partial_allocations(tmp_path, monkeypatch, kwargs):
    model, lib = model_for(tmp_path, monkeypatch, DmaLib(**kwargs))
    assert model.io_mode == "legacy"
    assert model.io_fallback_reason
    assert lib.contexts == 2
    assert not lib.live
    result = model.infer(image())
    np.testing.assert_array_equal(result[0], lib.output_float[0])
    assert all(e[1] == 2 for e in lib.events if e[0] in ("inputs_set", "outputs_get", "outputs_release"))
    assert ("destroy", 1) in lib.events
    model.release()


def test_legacy_explicitly_avoids_bound_allocation_and_accepts_into(tmp_path, monkeypatch):
    model, lib = model_for(tmp_path, monkeypatch, mode="legacy")
    target = [np.empty(s, dtype=np.float32) for s in lib.output_shapes]
    model.infer_into(image(), target)
    assert lib.allocations == 0
    assert model.shared_io_size_bytes == 0
    np.testing.assert_array_equal(target[1], lib.output_float[1])
    model.release()


@pytest.mark.parametrize("stage", ["bind", "input_sync", "run", "output_sync"])
def test_bound_native_failure_fences_retries_until_teardown(tmp_path, monkeypatch, stage):
    model, lib = model_for(tmp_path, monkeypatch)
    buf = model.allocate_input_buffer()
    outputs = model.allocate_output_buffers()
    if stage == "bind":
        lib.fail_bind = lib.bindings + 1
    elif stage == "run":
        lib.fail_run = True
    else:
        lib.fail_sync = 1 if stage == "input_sync" else 2
    with pytest.raises(RuntimeError, match="failed"):
        model.infer_dma_buffers(buf, outputs)
    assert model.native_cleanup_failed is False
    count = len(lib.events)
    with pytest.raises(RuntimeError, match="not ready"):
        model.infer(image())
    assert len(lib.events) == count
    model.release()
    assert not lib.live


def test_memory_destroy_failure_preserves_retry_and_never_falls_back(tmp_path, monkeypatch):
    model, lib = model_for(tmp_path, monkeypatch)
    lib.fail_mem_destroy = 2
    with pytest.raises(RuntimeError, match="destroy_mem"):
        model.release()
    assert model.ctx.value == 1 and not model._released
    assert model.native_cleanup_failed is True
    assert len(lib.live) == 2
    assert not any(e[0] == "destroy" for e in lib.events)
    with pytest.raises(RuntimeError, match="not ready"):
        model.infer(image())
    model.release()
    assert model.native_cleanup_failed is True  # lifetime quarantine latch
    successful_frees = [e[2] for e in lib.events if e[0] == "free"]
    assert successful_frees.count(103) == 1  # first freed buffer never freed again
    assert not lib.live


def test_failed_fallback_cleanup_does_not_initialize_second_context(tmp_path, monkeypatch):
    lib = DmaLib(fail_bind=2, fail_destroy=1)
    monkeypatch.setattr(r, "_load", lambda: lib)
    path = tmp_path / "fail.rknn"
    path.write_bytes(b"rknn")
    model = r.CtypesRknnModel(str(path))
    with pytest.raises(RuntimeError, match="rknn_destroy failed"):
        model.init_runtime()
    assert lib.contexts == 1
    assert model.native_cleanup_failed is True
    assert not lib.live
    with pytest.raises(RuntimeError, match="not ready"):
        model.infer(image())
    model.release()


def test_release_active_client_buffers_rebinds_defaults_before_free(tmp_path, monkeypatch):
    model, lib = model_for(tmp_path, monkeypatch)
    buf, outputs = model.allocate_input_buffer(), model.allocate_output_buffers()
    model.infer_dma_buffers(buf, outputs)
    lib.events.clear()
    model.release_input_buffer(buf)
    assert [e[0] for e in lib.events] == ["bind", "free"]
    lib.events.clear()
    model.release_output_buffers(outputs)
    assert [e[0] for e in lib.events] == ["bind", "bind", "free", "free"]
    model.infer(image())
    model.release()


def test_invalid_destinations_and_foreign_buffers_do_not_reach_driver(tmp_path, monkeypatch):
    model, lib = model_for(tmp_path, monkeypatch)
    other, _ = model_for(tmp_path, monkeypatch, DmaLib())
    wrong = other.allocate_input_buffer()
    before = len(lib.events)
    with pytest.raises(ValueError, match="foreign"):
        model.infer_dma_buffers(wrong, model._default_outputs)
    with pytest.raises(ValueError, match="float32"):
        model.infer_into(image(), [np.empty((1, 2, 2, 5), dtype=np.int8),
                                  np.empty((3,), dtype=np.float32)])
    with pytest.raises(TypeError, match="uint8"):
        model.infer(image().astype(np.float32))
    assert len(lib.events) == before
    model.release()
    other.release()


def test_partial_client_output_allocation_failure_frees_only_new_buffers(tmp_path, monkeypatch):
    model, lib = model_for(tmp_path, monkeypatch)
    lib.fail_alloc = lib.allocations + 2
    before = set(lib.live)
    with pytest.raises(RuntimeError, match="create_mem"):
        model.allocate_output_buffers()
    assert set(lib.live) == before
    assert model.native_cleanup_failed is False
    model.infer(image())
    model.release()


def test_forced_bound_does_not_silently_fallback(tmp_path, monkeypatch):
    lib = DmaLib(fail_bind=2)
    monkeypatch.setattr(r, "_load", lambda: lib)
    path = tmp_path / "forced.rknn"
    path.write_bytes(b"rknn")
    model = r.CtypesRknnModel(str(path), io_mode="bound")
    with pytest.raises(RuntimeError, match="set_io_mem"):
        model.init_runtime()
    assert lib.contexts == 1
    assert model.ctx.value == 0
    assert not lib.live
    model.release()


@pytest.mark.parametrize("mode", ["auto", "bound"])
def test_partial_runtime_symbol_set_never_attempts_binding(tmp_path, monkeypatch, mode):
    lib = DmaLib()
    lib.rknn_mem_sync = None
    monkeypatch.setattr(r, "_load", lambda: lib)
    path = tmp_path / "old.rknn"
    path.write_bytes(b"rknn")
    model = r.CtypesRknnModel(str(path), io_mode=mode)
    if mode == "bound":
        with pytest.raises(RuntimeError, match="complete bound IO API"):
            model.init_runtime()
    else:
        model.init_runtime()
        assert model.io_mode == "legacy"
        model.infer(image())
    assert lib.allocations == 0
    model.release()


def test_nhwc_output_uses_explicit_native_nhwc_query(tmp_path, monkeypatch):
    lib = DmaLib()
    lib.output_shapes = ((1, 2, 5, 2), (3,))
    lib.output_float = [np.arange(math.prod(s), dtype=np.float32).reshape(s)
                        for s in lib.output_shapes]
    query = lib.rknn_query
    commands = []

    def nhwc_query(ctx, cmd, value, size):
        commands.append(cmd)
        if cmd == r.RKNN_QUERY_NATIVE_NHWC_OUTPUT_ATTR:
            lib._attr(value._obj, lib.output_shapes[0], r.RKNN_TENSOR_NHWC,
                      r.RKNN_TENSOR_FLOAT16)
            return 0
        ret = query(ctx, cmd, value, size)
        if cmd == r.RKNN_QUERY_OUTPUT_ATTR and value._obj.index == 0:
            value._obj.fmt = r.RKNN_TENSOR_NHWC
        return ret

    lib.rknn_query = nhwc_query
    model, _ = model_for(tmp_path, monkeypatch, lib)
    assert model.io_mode == "bound"
    assert r.RKNN_QUERY_NATIVE_NHWC_OUTPUT_ATTR in commands
    np.testing.assert_array_equal(model.infer(image())[0], lib.output_float[0] + 7)
    assert model._bound_output_attrs[0].fmt == r.RKNN_TENSOR_NHWC
    model.release()


@pytest.mark.parametrize("invalid", ["stride", "output_dtype", "output_shape", "output_layout"])
def test_unsupported_native_metadata_falls_back_before_allocating(tmp_path, monkeypatch, invalid):
    lib = DmaLib()
    query = lib.rknn_query

    def bad_query(ctx, cmd, value, size):
        ret = query(ctx, cmd, value, size)
        attr = value._obj
        if cmd == r.RKNN_QUERY_NATIVE_INPUT_ATTR and invalid == "stride":
            attr.w_stride = 4
        if cmd == r.RKNN_QUERY_NATIVE_OUTPUT_ATTR and attr.index == 0:
            if invalid == "output_dtype":
                attr.type = 4
            elif invalid == "output_shape":
                attr.dims[3] = 6
            elif invalid == "output_layout":
                attr.fmt = 99
        return ret

    lib.rknn_query = bad_query
    model, _ = model_for(tmp_path, monkeypatch, lib)
    assert model.io_mode == "legacy"
    assert lib.allocations == 0
    model.infer(image())
    model.release()


def test_rebind_failure_on_release_keeps_client_allocation_for_context_teardown(tmp_path, monkeypatch):
    model, lib = model_for(tmp_path, monkeypatch)
    buf = model.allocate_input_buffer()
    model.infer_dma_buffers(buf, model._default_outputs)
    lib.fail_bind = lib.bindings + 1
    with pytest.raises(RuntimeError, match="set_io_mem"):
        model.release_input_buffer(buf)
    assert not buf.released and buf.fd in lib.live
    with pytest.raises(RuntimeError, match="not ready"):
        model.infer(image())
    model.release()
    assert buf.released and not lib.live


def test_load_prototypes_zero_copy_pointers_and_lp64_tensor_memory(monkeypatch):
    class Symbol:
        pass

    class Library:
        def __getattr__(self, name):
            symbol = Symbol()
            setattr(self, name, symbol)
            return symbol

    lib = Library()
    monkeypatch.setattr(r, "_lib", None)
    monkeypatch.setattr(r, "_lib_path", "")
    monkeypatch.setattr(r, "library_path", lambda: "/fake/librknnrt.so")
    monkeypatch.setattr(r.ctypes, "CDLL", lambda *args, **kwargs: lib)
    assert r._load() is lib
    pointer = ctypes.POINTER(r.RknnTensorMem)
    assert lib.rknn_create_mem.restype is pointer
    assert lib.rknn_create_mem.argtypes == [r.rknn_context, ctypes.c_uint32]
    assert lib.rknn_set_io_mem.argtypes == [r.rknn_context, pointer,
                                          ctypes.POINTER(r.RknnTensorAttr)]
    assert lib.rknn_mem_sync.argtypes == [r.rknn_context, pointer, ctypes.c_int]
    assert lib.rknn_destroy_mem.argtypes == [r.rknn_context, pointer]
    assert ctypes.sizeof(r.RknnTensorMem) == 40
    assert r.RknnTensorMem.fd.offset == 16
    assert r.RknnTensorMem.offset.offset == 20
    assert r.RknnTensorMem.priv_data.offset == 32


@pytest.mark.parametrize("kind", ["input", "outputs"])
def test_client_allocation_rollback_failure_latches_native_cleanup_fault(tmp_path, monkeypatch, kind):
    model, lib = model_for(tmp_path, monkeypatch)
    lib.fail_mem_destroy = lib.mem_destroys + 1
    if kind == "outputs":
        lib.fail_alloc = lib.allocations + 2
        allocate = model.allocate_output_buffers
    else:
        create = lib.rknn_create_mem

        def malformed_input(ctx, size):
            mem = create(ctx, size)
            mem.contents.virt_addr = None
            return mem

        lib.rknn_create_mem = malformed_input
        allocate = model.allocate_input_buffer
    with pytest.raises(RuntimeError, match="destroy_mem"):
        allocate()
    assert model.native_cleanup_failed is True
    assert len(lib.live) == 4  # private IO plus unreleased partial allocation
    with pytest.raises(RuntimeError, match="not ready"):
        model.infer(image())
    model.release()
    assert not lib.live


@pytest.mark.parametrize("symbol", ["rknn_destroy_mem", "rknn_destroy"])
def test_native_destroy_exception_also_latches_cleanup_fault(tmp_path, monkeypatch, symbol):
    model, lib = model_for(tmp_path, monkeypatch)
    original = getattr(lib, symbol)

    def broken(*args):
        raise RuntimeError("native call raised")

    setattr(lib, symbol, broken)
    with pytest.raises(RuntimeError, match="native call raised"):
        model.release()
    assert model.native_cleanup_failed is True
    setattr(lib, symbol, original)
    model.release()
    assert not lib.live

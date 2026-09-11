"""Deferred camera-to-model preprocessing with real Kit frame/lifetime APIs.

Only hardware and native camera acquisition are replaced. The fake RGA writes
into a preallocated destination and records borrowed-frame access, making an
eager RGB allocation, lost application edit or premature frame ACK observable.
"""
from __future__ import annotations

import sys
import threading
from types import SimpleNamespace

import numpy as np
import pytest

import kit.app as app_module
from kit.adapters.official import OfficialFrameSource
from kit.app import App, PreparedInput, _ModelHandle
from kit.errors import BufferReleasedError


class Clock:
    now = 100.0

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class CameraFrame:
    width, height = 16, 8
    pts_us = 123456
    fd = 7
    planes = ((0, 32, 8), (256, 32, 4))

    def __init__(self, trace):
        self.active = True
        self.trace = trace

    def to_bgr(self):
        assert self.active, "CPU conversion read an expired camera lease"
        self.trace.append("cpu-convert")
        out = np.empty((self.height, self.width, 3), dtype=np.uint8)
        out[:] = (33, 22, 11)
        return out


class FakeRga:
    def __init__(self, camera, clock, trace):
        self.camera, self.clock, self.trace = camera, clock, trace
        self.canvas = np.full((8, 12, 3), 71, dtype=np.uint8)
        self.fail_dma = False
        self.supported = True
        self.entered = self.proceed = None
        self.last_target = None

    def can_letterbox(self, *, dma_output=False):
        assert dma_output
        return self.supported

    def resize_nv12_to_rgb(self, **kw):
        assert self.camera.active, "RGB materialization read an expired lease"
        self.trace.append("rgb-materialize")
        self.clock.advance(0.003)
        out = np.empty((kw["dst_height"], kw["dst_width"], 3), dtype=np.uint8)
        out[:] = (11, 22, 33)
        return out

    def letterbox_nv12_to_rgb(self, **kw):
        assert self.camera.active, "RGA read an expired camera lease"
        self.trace.append("dma-start")
        self.last_target = kw
        if self.entered is not None:
            self.entered.set()
            assert self.proceed.wait(3), "test did not release the RGA operation"
        assert self.camera.active, "camera was ACKed during synchronous RGA"
        self.clock.advance(0.002)
        if self.fail_dma:
            raise RuntimeError("simulated RGA DMA failure")
        assert kw["dst_fd"] == 70
        assert (kw["dst_w_stride"], kw["dst_h_stride"]) == (12, 8)
        self.canvas[:8, :8] = kw["pad_value"]
        left, top, right, bottom = kw["dst_window"]
        self.canvas[top:bottom, left:right] = (11, 22, 33)
        self.trace.append("dma-end")


def descriptor(**changes):
    result = dict(fd=70, dtype="uint8", shape=[1, 8, 8, 3],
                  strides=[288, 36, 3, 1], size=288, offset=0)
    result.update(changes)
    return result


class Model:
    def __init__(self, rga, clock, target=None):
        self.rga, self.clock = rga, clock
        self.target = descriptor() if target is None else target
        self.inputs = []
        self.used_dma = []

    def infer_prepared(self, prepare, fallback):
        ready = prepare(self.target)
        self.used_dma.append(ready)
        pixels = self.rga.canvas[:8, :8] if ready else fallback()
        return self.infer(pixels)

    def infer(self, pixels):
        self.inputs.append(np.array(pixels, copy=True))
        self.clock.advance(0.004)
        return [np.array([42])]


@pytest.fixture
def pipeline(monkeypatch):
    trace, clock = [], Clock()
    camera = CameraFrame(trace)
    rga = FakeRga(camera, clock, trace)
    source = OfficialFrameSource(input_size=8, direct_preprocess=True,
                                 deferred_preprocess=True, verbose=False)
    source._rga, source._rga_decided = rga, True
    monkeypatch.setattr(app_module.time, "monotonic", clock.monotonic)
    owner = App()
    model = Model(rga, clock)
    handle = _ModelHandle("det", "fake.rknn", owner, model)
    return SimpleNamespace(source=source, camera=camera, rga=rga, owner=owner,
                           model=model, handle=handle, clock=clock, trace=trace)


def prepared(pipeline):
    frame = pipeline.source._deferred_frame(pipeline.camera)
    assert frame is not None
    return frame, pipeline.owner.pre(frame)


def expected_pixels():
    pixels = np.full((8, 8, 3), 114, dtype=np.uint8)
    pixels[2:6] = (11, 22, 33)
    return pixels


def test_lazy_frame_and_pre_do_not_materialize_rgb_and_infer_fills_dma(pipeline, monkeypatch):
    p = pipeline
    frame, value = prepared(p)
    assert isinstance(value, PreparedInput)
    assert p.trace == []
    assert (frame.w, frame.h, frame.pts_us) == (16, 8, 123456)
    assert (value.info.orig_w, value.info.orig_h, value.info.scale,
            value.info.pad_w, value.info.pad_h) == (16, 8, 0.5, 0, 2)
    # After creating the persistent fake DMA buffer, no CPU canvas is needed.
    monkeypatch.setattr(np, "empty", lambda *a, **kw: pytest.fail("eager RGB allocation"))
    monkeypatch.setattr(np, "full", lambda *a, **kw: pytest.fail("CPU padding allocation"))
    assert p.handle.infer(value)[0].item() == 42
    assert p.model.used_dma == [True]
    assert p.trace == ["dma-start", "dma-end"]
    assert p.rga.last_target["y_stride"] == 32
    assert p.rga.last_target["y_vstride"] == 8
    assert np.all(p.model.inputs[0][:2] == 114)
    assert np.all(p.model.inputs[0][2:6] == (11, 22, 33))
    assert np.all(p.rga.canvas[:, 8:] == 71)  # padded physical rows preserved
    assert p.owner._t_pre == pytest.approx(0.002)
    assert p.owner._t_infer == pytest.approx(0.004)


@pytest.mark.parametrize("access", ["frame", "prepared", "unpack"])
def test_reading_or_editing_data_uses_copy_and_preserves_edits(pipeline, access):
    p = pipeline
    frame, value = prepared(p)
    array = frame.data if access == "frame" else (
        tuple(value)[0] if access == "unpack" else value.data)
    np.testing.assert_array_equal(array, expected_pixels())
    array[3, 4] = (211, 199, 173)
    assert np.shares_memory(value.data, frame.data)
    p.handle.infer(value)
    assert p.model.used_dma == [False]
    assert p.trace == ["rgb-materialize"]
    np.testing.assert_array_equal(p.model.inputs[0], array)
    assert p.owner._t_infer == pytest.approx(0.004)


def test_replacing_prepared_data_does_not_map_or_overwrite_replacement(pipeline):
    p = pipeline
    frame, value = prepared(p)
    replacement = np.full((8, 8, 3), 203, dtype=np.uint8)
    value.data = replacement
    p.handle.infer(value)
    assert value.data is replacement
    assert p.model.used_dma == [False]
    assert p.trace == []
    np.testing.assert_array_equal(p.model.inputs[0], replacement)
    # The replacement belongs to this PreparedInput, not the camera frame.
    np.testing.assert_array_equal(frame.data, expected_pixels())


@pytest.mark.parametrize("changes", [
    {"shape": [1, 16, 16, 3]}, {"shape": [1, 3, 8, 8]},
    {"strides": [288, 36, 1, 8]}, {"strides": [288, 35, 3, 1]},
    {"strides": [36, 3, 1]}, {"offset": 16}, {"size": 287},
])
def test_incompatible_model_descriptor_falls_back_without_disabling_dma(pipeline, changes):
    p = pipeline
    p.model.target = descriptor(**changes)
    _, value = prepared(p)
    p.handle.infer(value)
    assert p.model.used_dma == [False]
    assert p.trace == ["rgb-materialize"]
    assert p.source.deferred_preprocess
    np.testing.assert_array_equal(p.model.inputs[0], expected_pixels())
    assert p.owner._t_pre == pytest.approx(0.003)
    assert p.owner._t_infer == pytest.approx(0.004)


def test_offset_camera_uses_established_rgb_fallback_without_fd_wrap(pipeline):
    p = pipeline
    p.camera.planes = ((16, 32, 8), (272, 32, 4))
    _, value = prepared(p)
    p.handle.infer(value)
    assert p.model.used_dma == [False]
    assert p.trace == ["cpu-convert"]
    np.testing.assert_array_equal(p.model.inputs[0], expected_pixels())


@pytest.mark.parametrize("reason", ["odd-geometry", "unsupported-rga", "disabled"])
def test_ineligible_source_keeps_existing_eager_contract(pipeline, reason):
    p = pipeline
    if reason == "odd-geometry":
        p.camera.width, p.camera.height = 12, 8  # resize becomes 8x5 NV12
    elif reason == "unsupported-rga":
        p.rga.supported = False
    else:
        p.source.deferred_preprocess = False
    assert p.source._deferred_frame(p.camera) is None
    assert p.trace == []


def test_dma_rga_exception_disables_only_dma_optimization_and_accounts_fallback(pipeline):
    p = pipeline
    _, value = prepared(p)
    p.rga.fail_dma = True
    p.handle.infer(value)
    assert p.model.used_dma == [False]
    assert p.trace == ["dma-start", "rgb-materialize"]
    assert not p.source.deferred_preprocess
    assert p.source.direct_preprocess and p.source._rga is p.rga
    np.testing.assert_array_equal(p.model.inputs[0], expected_pixels())
    assert p.owner._t_pre == pytest.approx(0.005)
    assert p.owner._t_infer == pytest.approx(0.004)
    assert p.owner._t_pre + p.owner._t_infer == pytest.approx(p.clock.now - 100.0)
    assert p.source._deferred_frame(p.camera) is None


def test_model_without_prepared_api_materializes_once_and_accounts_pre_separately(pipeline):
    p = pipeline
    p.model.infer_prepared = None
    _, value = prepared(p)
    p.handle.infer(value)
    assert p.trace == ["rgb-materialize"]
    np.testing.assert_array_equal(p.model.inputs[0], expected_pixels())
    assert p.owner._t_pre == pytest.approx(0.003)
    assert p.owner._t_infer == pytest.approx(0.004)


class NativeSource:
    width, height, fourcc = 16, 8, 0x3231564E
    pool_depth, max_outstanding = 2, 1

    def __init__(self, camera, trace):
        self.camera, self.trace = camera, trace
        self.calls = 0
        self.closed = False

    def acquire(self):
        self.calls += 1
        if self.calls == 1:
            return self.camera
        self.camera.active = False
        self.trace.append("camera-ack")
        raise StopIteration

    def close(self):
        self.closed = True
        self.camera.active = False
        self.trace.append("camera-close")


def open_stream(pipeline, monkeypatch):
    p = pipeline
    native = NativeSource(p.camera, p.trace)
    fake_sdk = SimpleNamespace(FrameSource=lambda **kw: native,
                               FrameConfig=lambda **kw: kw,
                               AcquireTimeoutError=TimeoutError,
                               RecameraError=RuntimeError)
    monkeypatch.setitem(sys.modules, "recamera_ext", fake_sdk)
    stream = p.source.frames()
    return stream, next(stream), native


def test_unmapped_previous_frame_is_rejected_after_source_advances(pipeline, monkeypatch):
    p = pipeline
    stream, frame, native = open_stream(p, monkeypatch)
    value = p.owner.pre(frame)
    with pytest.raises(StopIteration):
        next(stream)
    assert native.closed and frame.released
    with pytest.raises(BufferReleasedError):
        _ = frame.data
    with pytest.raises(BufferReleasedError):
        p.handle.infer(value)
    assert p.model.inputs == []
    assert "dma-start" not in p.trace and "rgb-materialize" not in p.trace


def test_owned_copy_survives_advance_and_release_and_uses_normal_model_input(pipeline, monkeypatch):
    p = pipeline
    stream, frame, _ = open_stream(p, monkeypatch)
    copy = frame.copy()
    saved = copy.data.copy()
    assert copy.owned
    assert not hasattr(copy, "_deferred_model_image")
    with pytest.raises(StopIteration):
        next(stream)
    frame.release()
    np.testing.assert_array_equal(copy.data, saved)
    p.handle.infer(p.owner.pre(copy))
    assert p.model.used_dma == []
    np.testing.assert_array_equal(p.model.inputs[0], expected_pixels())


@pytest.mark.parametrize("stop", ["advance", "source-close", "frame-release"])
def test_frame_expiration_waits_for_synchronous_rga(pipeline, monkeypatch, stop):
    p = pipeline
    stream, frame, native = open_stream(p, monkeypatch)
    image = frame._deferred_model_image
    entered, proceed, stopping, stopped = (threading.Event() for _ in range(4))
    p.rga.entered, p.rga.proceed = entered, proceed
    failures, prepared_results = [], []

    def run_rga():
        try:
            prepared_results.append(image.prepare(descriptor()))
        except BaseException as exc:
            failures.append(exc)

    def stop_frame():
        stopping.set()
        try:
            if stop == "advance":
                with pytest.raises(StopIteration):
                    next(stream)
            elif stop == "source-close":
                p.source.close()
            else:
                frame.release()
        except BaseException as exc:
            failures.append(exc)
        finally:
            stopped.set()

    runner = threading.Thread(target=run_rga, daemon=True)
    stopper = threading.Thread(target=stop_frame, daemon=True)
    runner.start()
    assert entered.wait(3)
    stopper.start()
    try:
        assert stopping.wait(3)
        assert not stopped.wait(0.03), "frame invalidated while RGA still reads it"
        assert p.camera.active and not native.closed
    finally:
        proceed.set()
        runner.join(3)
        stopper.join(3)
    assert not runner.is_alive() and not stopper.is_alive()
    assert failures == []
    assert prepared_results == [True]
    assert p.trace.index("dma-start") < p.trace.index("dma-end")
    if stop != "frame-release":
        final_event = "camera-ack" if stop == "advance" else "camera-close"
        assert p.trace.index("dma-end") < p.trace.index(final_event)
    with pytest.raises(BufferReleasedError):
        image.prepare(descriptor())
    stream.close()


def test_mapped_array_can_outlive_lease_but_explicit_release_invalidates_it(pipeline):
    p = pipeline
    frame, value = prepared(p)
    old_array = value.data
    image = frame._deferred_model_image
    image.expire()
    p.camera.active = False
    old_array[0, 0] = (1, 2, 3)
    p.handle.infer(value)
    assert p.model.used_dma == [False]
    np.testing.assert_array_equal(p.model.inputs[0], old_array)
    frame.release()
    with pytest.raises(BufferReleasedError):
        _ = value.data
    with pytest.raises(BufferReleasedError):
        _ = frame.data


@pytest.mark.parametrize("opt_in,mode,transports,expected", [
    pytest.param("default", "hw-direct", ["rknn-dma-v1"], False, id="old-app-default"),
    pytest.param(True, "hw-direct", ["rknn-dma-v1"], True, id="explicit-opt-in"),
    pytest.param(False, "hw-direct", ["rknn-dma-v1"], False, id="explicit-opt-out"),
    pytest.param(1, "hw-direct", ["rknn-dma-v1"], False, id="truthy-int-is-not-opt-in"),
    pytest.param("true", "hw-direct", ["rknn-dma-v1"], False, id="truthy-string-is-not-opt-in"),
    pytest.param(True, "hw-direct", ["memfd-v1"], False, id="shared-copy-transport"),
    pytest.param(True, "hw-direct", ["socket"], False, id="socket-copy-transport"),
    pytest.param(True, "hw-direct", [None], False, id="old-model-without-transport"),
    pytest.param(True, "cpu", ["rknn-dma-v1"], False, id="cpu-model-frame"),
    pytest.param(True, "hw", ["rknn-dma-v1"], True, id="separate-hardware-model-frame"),
    pytest.param("default", "hw", ["rknn-dma-v1"], False, id="old-hw-app-default"),
    pytest.param(False, "hw", ["rknn-dma-v1"], False, id="hw-explicit-opt-out"),
    pytest.param(True, "hw", ["tensor-v1"], False, id="hw-legacy-transport"),
    pytest.param(True, "hw-roi", ["rknn-dma-v1"], True, id="hardware-roi-frame"),
    pytest.param("default", "hw-roi", ["rknn-dma-v1"], False, id="old-roi-app-default"),
    pytest.param(False, "hw-roi", ["rknn-dma-v1"], False, id="roi-explicit-opt-out"),
    pytest.param(True, "hw-roi", ["tensor-v1"], False, id="roi-legacy-transport"),
    pytest.param(True, "hw-direct", [], False, id="no-primary-model"),
    pytest.param(True, "hw-direct", [None, "rknn-dma-v1"], False,
                 id="dma-secondary-cannot-enable-primary"),
    pytest.param(True, "hw-direct", ["rknn-dma-v1", None], True,
                 id="dma-primary-with-legacy-secondary"),
])
def test_start_requires_explicit_opt_in_hardware_mode_and_primary_dma_transport(
        monkeypatch, opt_in, mode, transports, expected):
    class StartupApp(App):
        id = "dma-startup-test"
        owns_loop = True

        def run(self):
            pass

    if opt_in != "default":
        StartupApp.model_dma_input = opt_in
    StartupApp.model_frame = mode
    app = StartupApp()
    if opt_in == "default":
        assert app.model_dma_input is False

    opened, cleaned = [], []
    source = SimpleNamespace(close=lambda: cleaned.append("source"))

    def open_source(**kwargs):
        opened.append(kwargs)
        return source

    implementations = []
    for index, transport in enumerate(transports):
        impl = SimpleNamespace(release=lambda index=index: cleaned.append(index))
        if transport is not None:
            impl.io_transport = transport
        implementations.append(impl)
    pending_models = iter(implementations)
    monkeypatch.setattr(app, "_load_model", lambda _path: next(pending_models))
    monkeypatch.setattr(app_module, "open_frame_source", open_source)
    # Startup selection is the subject; do not replace process signal handlers.
    monkeypatch.setattr(app, "_install_reload_handler", lambda: None)
    monkeypatch.setattr(app, "_install_stop_handlers", lambda: None)
    manifest = {"id": app.id, "models": [
        {"id": f"model{index}", "file": f"model{index}.rknn", "input": [1, 8, 8, 3]}
        for index in range(len(transports))
    ]}
    try:
        app.start(app_dir="/test/dma-startup", manifest=manifest, config={},
                  source="official", sink=SimpleNamespace(), verbose=False)
        assert len(opened) == 1
        options = opened[0]
        assert options["deferred_preprocess"] is expected
        assert options["direct_preprocess"] is (mode == "hw-direct")
        assert options["hw_roi"] is (mode == "hw-roi")
        assert options["hw_letterbox"] is (mode == "hw")
        assert options["input_size"] == (0 if mode == "cpu" else app._pre_size)
    finally:
        app.finish()
    assert cleaned == ["source", *range(len(transports))]

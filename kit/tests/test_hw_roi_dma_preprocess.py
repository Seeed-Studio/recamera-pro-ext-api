"""Opt-in DMA primary input keeps the original-camera hw-roi contract."""
from __future__ import annotations

import threading
from types import SimpleNamespace

import numpy as np
import pytest

from kit.adapters import _rga
from kit.adapters.official import OfficialFrameSource
from kit.app import App, _ModelHandle
from kit.errors import BufferReleasedError
from kit.pipeline import crop_square_roi, square_roi_geometry
from kit.tests.test_app_dma_preprocess import (
    CameraFrame, Clock, FakeRga, Model, descriptor, expected_pixels, open_stream,
)


class RoiRga(FakeRga):
    def __init__(self, camera, clock, trace):
        super().__init__(camera, clock, trace)
        self.crop_supported = True
        self.fail_rgb = self.fail_convert = self.fail_crop = False
        self.crop_entered = self.crop_proceed = None
        self.last_crop = None
        self.roi_color = (201, 151, 101)

    def can_crop(self):
        return self.crop_supported

    def resize_nv12_to_rgb(self, **kwargs):
        if self.fail_rgb:
            raise RuntimeError("simulated model RGB conversion failure")
        return super().resize_nv12_to_rgb(**kwargs)

    def convert(self, **kwargs):
        assert self.camera.active
        self.trace.append("full-rgb")
        if self.fail_convert:
            raise RuntimeError("simulated full RGB conversion failure")
        out = np.empty((kwargs["height"], kwargs["width"], 3), dtype=np.uint8)
        out[:] = (11, 22, 33)
        return out

    def crop_nv12_to_rgb(self, **kwargs):
        assert self.camera.active, "ROI read an expired camera lease"
        self.trace.append("crop-start")
        self.last_crop = kwargs
        if self.crop_entered is not None:
            self.crop_entered.set()
            assert self.crop_proceed.wait(3), "test did not release ROI operation"
        assert self.camera.active, "camera ACKed while ROI still reads it"
        if self.fail_crop:
            raise RuntimeError("simulated hardware ROI failure")
        out = kwargs["out"]
        out[:] = kwargs["pad_value"]
        left, top, right, bottom = kwargs["dst_window"]
        out[top:bottom, left:right] = self.roi_color
        self.trace.append("crop-end")


@pytest.fixture
def pipeline(monkeypatch):
    trace, clock = [], Clock()
    camera = CameraFrame(trace)
    rga = RoiRga(camera, clock, trace)
    source = OfficialFrameSource(input_size=8, hw_roi=True,
                                 deferred_preprocess=True, verbose=False)
    # Exercise the real first-frame capability probe, including its downgrade.
    monkeypatch.setattr(_rga, "try_open", lambda: rga)
    owner = App()
    model = Model(rga, clock)
    handle = _ModelHandle("det", "fake.rknn", owner, model)
    return SimpleNamespace(source=source, camera=camera, rga=rga, owner=owner,
                           model=model, handle=handle, clock=clock, trace=trace)


@pytest.mark.parametrize("box,pad", [
    ([8, 2, 14, 6], 0.0), ([-2, -1, 6, 5], 0.25), ([25, 20, 30, 25], 0.5),
])
def test_primary_dma_then_roi_uses_original_geometry_padding_and_owned_copy(
        pipeline, monkeypatch, box, pad):
    p = pipeline
    stream, frame, _ = open_stream(p, monkeypatch)
    try:
        assert frame.roi_cropper is not None
        assert (frame.w, frame.h) == (16, 8)
        assert p.trace == []
        p.handle.infer(p.owner.pre(frame))
        assert p.model.used_dma == [True]
        np.testing.assert_array_equal(p.model.inputs[0], expected_pixels())
        roi, roi_map = p.owner.crop_roi_hw(frame, box, 6, pad)
        expected_map, source_rect, window, _ = square_roi_geometry(8, 16, box, 6, pad)
        assert roi_map == expected_map
        assert roi.shape == (6, 6, 3)
        if source_rect is None:
            assert np.all(roi == 0)
            assert p.rga.last_crop is None
        else:
            assert p.rga.last_crop["src_rect"] == source_rect
            assert p.rga.last_crop["dst_window"] == window
            assert (p.rga.last_crop["width"], p.rga.last_crop["height"]) == (16, 8)
            assert (p.rga.last_crop["y_stride"], p.rga.last_crop["y_vstride"]) == (32, 8)
            expected = np.full((6, 6, 3), 114, dtype=np.uint8)
            left, top, right, bottom = window
            expected[top:bottom, left:right] = p.rga.roi_color
            np.testing.assert_array_equal(roi, expected)
        saved = roi.copy()
        p.rga.roi_color = (7, 8, 9)
        newer, _ = p.owner.crop_roi_hw(frame, [8, 2, 14, 6], 6, 0)
        assert not np.shares_memory(roi, newer)
        np.testing.assert_array_equal(roi, saved)
        assert "rgb-materialize" not in p.trace and "full-rgb" not in p.trace
    finally:
        stream.close()


@pytest.mark.parametrize("capability", ["crop", "all-rga", "dma-letterbox"])
def test_missing_crop_keeps_full_rgb_and_missing_dma_keeps_eager_path(pipeline, monkeypatch, capability):
    p = pipeline
    if capability == "crop":
        p.rga.crop_supported = False
    elif capability == "all-rga":
        monkeypatch.setattr(_rga, "try_open", lambda: None)
    else:
        p.rga.supported = False
    stream, frame, _ = open_stream(p, monkeypatch)
    try:
        assert hasattr(frame, "_deferred_model_image") is (capability == "crop")
        if capability == "dma-letterbox":
            assert p.source.hw_roi and not p.source.hw_letterbox
            assert frame.data.shape == (8, 8, 3)
            assert frame.roi_cropper is not None
        else:
            assert not p.source.hw_roi and p.source.hw_letterbox
            assert frame.data.shape == (8, 16, 3)
            assert frame.roi_cropper is None
            if capability == "crop":
                # Even on firmware without the old crop ABI, the model-only
                # DMA path remains available while numpy ROI uses full RGB.
                assert p.trace == ["full-rgb"]
                p.handle.infer(p.owner.pre(frame))
                assert p.model.used_dma == [True]
                assert "rgb-materialize" not in p.trace
            box = [8, 2, 14, 6]
            expected, expected_map = crop_square_roi(frame.data, box, 6, 0)
            actual, actual_map = p.owner.crop_roi_hw(frame, box, 6, 0)
            np.testing.assert_array_equal(actual, expected)
            assert actual_map == expected_map
    finally:
        stream.close()


@pytest.mark.parametrize("failure", ["dma", "dma-and-resize", "all-rga", "descriptor"])
def test_primary_fallback_keeps_cropper_and_never_numpy_crops_model_letterbox(
        pipeline, monkeypatch, failure):
    p = pipeline
    stream, frame, _ = open_stream(p, monkeypatch)
    cropper = frame.roi_cropper
    p.rga.fail_dma = failure != "descriptor"
    p.rga.fail_rgb = failure in ("dma-and-resize", "all-rga")
    p.rga.fail_convert = failure == "all-rga"
    if failure == "descriptor":
        p.model.target = descriptor(offset=16)
    try:
        p.handle.infer(p.owner.pre(frame))
        assert p.model.used_dma == [False]
        assert frame.roi_cropper is cropper
        assert frame.data.shape == (8, 8, 3)
        np.testing.assert_array_equal(p.model.inputs[0], expected_pixels())
        # If the cropper disappears, App would silently crop this model-space
        # image using original-camera coordinates. That fallback must not run.
        import kit.pipeline as pipeline_module
        monkeypatch.setattr(pipeline_module, "crop_square_roi",
                            lambda *a, **kw: pytest.fail("cropped the model letterbox"))
        roi, _ = p.owner.crop_roi_hw(frame, [8, 2, 14, 6], 6, 0)
        assert np.all(roi == (114 if failure == "all-rga" else p.rga.roi_color))
        assert p.source.deferred_preprocess is (failure == "descriptor")
    finally:
        stream.close()


def test_dynamic_roi_failure_retains_existing_gray_result(pipeline, monkeypatch):
    p = pipeline
    stream, frame, _ = open_stream(p, monkeypatch)
    try:
        p.handle.infer(p.owner.pre(frame))
        p.rga.fail_crop = True
        roi, roi_map = p.owner.crop_roi_hw(frame, [8, 2, 14, 6], 6, 0)
        assert np.all(roi == 114)
        assert roi_map == (8.0, 2.0, 1.0, 1.0)
        assert frame.roi_cropper is not None
        assert "rgb-materialize" not in p.trace
    finally:
        stream.close()


@pytest.mark.parametrize("stop", ["advance", "close", "release"])
@pytest.mark.parametrize("mapped", [False, True])
def test_model_rgb_copy_does_not_extend_original_roi_lease(pipeline, monkeypatch, stop, mapped):
    p = pipeline
    stream, frame, native = open_stream(p, monkeypatch)
    cropper = frame.roi_cropper
    if mapped:
        _ = frame.data
    if stop == "advance":
        with pytest.raises(StopIteration):
            next(stream)
    elif stop == "close":
        p.source.close()
    else:
        frame.release()
    before = list(p.trace)
    with pytest.raises(BufferReleasedError):
        cropper.crop_square([8, 2, 14, 6], 6, 0)
    assert p.trace == before  # no access to the expired NV12 lease
    if mapped and stop != "release":
        np.testing.assert_array_equal(frame.data, expected_pixels())
    else:
        with pytest.raises(BufferReleasedError):
            _ = frame.data
    stream.close()
    assert native.closed


@pytest.mark.parametrize("operation", ["primary-rga", "roi-rga"])
@pytest.mark.parametrize("stop", ["advance", "close"])
def test_expiry_waits_for_primary_or_roi_rga(pipeline, monkeypatch, operation, stop):
    p = pipeline
    stream, frame, native = open_stream(p, monkeypatch)
    entered, proceed, stopping, stopped = (threading.Event() for _ in range(4))
    if operation == "primary-rga":
        p.rga.entered, p.rga.proceed = entered, proceed
        action = lambda: frame._deferred_model_image.prepare(descriptor())
        last_event = "dma-end"
    else:
        p.rga.crop_entered, p.rga.crop_proceed = entered, proceed
        action = lambda: frame.roi_cropper.crop_square([8, 2, 14, 6], 6, 0)
        last_event = "crop-end"
    failures = []

    def worker():
        try:
            action()
        except BaseException as exc:
            failures.append(exc)

    def expire():
        stopping.set()
        try:
            if stop == "advance":
                with pytest.raises(StopIteration):
                    next(stream)
            else:
                p.source.close()
        except BaseException as exc:
            failures.append(exc)
        finally:
            stopped.set()

    runner = threading.Thread(target=worker, daemon=True)
    stopper = threading.Thread(target=expire, daemon=True)
    runner.start()
    assert entered.wait(3)
    stopper.start()
    try:
        assert stopping.wait(3)
        assert not stopped.wait(0.03)
        assert p.camera.active and not native.closed
    finally:
        proceed.set()
        runner.join(3)
        stopper.join(3)
        stream.close()
    assert not runner.is_alive() and not stopper.is_alive()
    assert failures == []
    event = "camera-ack" if stop == "advance" else "camera-close"
    assert p.trace.index(last_event) < p.trace.index(event)


def use_hw_source(pipeline):
    pipeline.source = OfficialFrameSource(
        input_size=8, hw_letterbox=True, deferred_preprocess=True, verbose=False)


@pytest.mark.parametrize("source_mode", ["hw", "hw-roi-downgrade"])
def test_full_rgb_access_and_edits_do_not_materialize_or_change_dma_model_image(
        pipeline, monkeypatch, source_mode):
    p = pipeline
    if source_mode == "hw":
        use_hw_source(p)
    else:
        p.rga.crop_supported = False
    stream, frame, _ = open_stream(p, monkeypatch)
    try:
        assert frame.owned and frame.data.shape == (8, 16, 3)
        assert p.trace == ["full-rgb"]
        frame.data[:] = (55, 77, 99)
        p.handle.infer(p.owner.pre(frame))
        assert p.model.used_dma == [True]
        np.testing.assert_array_equal(p.model.inputs[0], expected_pixels())
        assert p.trace == ["full-rgb", "dma-start", "dma-end"]
        assert np.all(frame.data == (55, 77, 99))
        roi, _ = p.owner.crop_roi_hw(frame, [8, 2, 14, 6], 6, 0)
        assert np.all(roi == (55, 77, 99))  # original-RGB application edits survive
    finally:
        stream.close()


@pytest.mark.parametrize("failure", ["descriptor", "dma", "dma-and-resize", "full-convert"])
def test_hw_model_fallback_never_consumes_application_edits_to_full_rgb(
        pipeline, monkeypatch, failure):
    p = pipeline
    use_hw_source(p)
    p.rga.fail_convert = failure == "full-convert"
    stream, frame, _ = open_stream(p, monkeypatch)
    p.rga.fail_dma = failure != "descriptor"
    p.rga.fail_rgb = failure == "dma-and-resize"
    if failure == "descriptor":
        p.model.target = descriptor(offset=16)
    try:
        frame.data[:] = 222
        p.handle.infer(p.owner.pre(frame))
        assert p.model.used_dma == [False]
        np.testing.assert_array_equal(p.model.inputs[0], expected_pixels())
        assert np.all(frame.data == 222)
        assert frame.roi_cropper is None
        if failure == "dma-and-resize":
            assert p.trace.count("full-rgb") == 2  # fresh input only on failure
        elif failure == "full-convert":
            assert p.trace.count("cpu-convert") == 2
        else:
            assert p.trace.count("full-rgb") == 1
            assert p.trace.count("rgb-materialize") == 1
    finally:
        stream.close()


@pytest.mark.parametrize("stop", ["advance", "close", "release"])
def test_hw_expiry_invalidates_unmapped_model_but_preserves_owned_original_pixels(
        pipeline, monkeypatch, stop):
    p = pipeline
    use_hw_source(p)
    stream, frame, native = open_stream(p, monkeypatch)
    value = p.owner.pre(frame)
    if stop == "advance":
        with pytest.raises(StopIteration):
            next(stream)
    elif stop == "close":
        p.source.close()
    else:
        frame.release()
    before = list(p.trace)
    with pytest.raises(BufferReleasedError):
        p.handle.infer(value)
    assert p.trace == before and p.model.inputs == []
    if stop == "release":
        with pytest.raises(BufferReleasedError):
            _ = frame.data
    else:
        assert frame.data.shape == (8, 16, 3)
        assert np.all(frame.data == (11, 22, 33))
    stream.close()
    assert native.closed

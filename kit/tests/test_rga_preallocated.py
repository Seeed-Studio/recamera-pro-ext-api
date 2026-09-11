"""Reusable RGA outputs: host tests of buffers, geometry and native contracts.

The fake library writes through the actual supplied CPU pointers or a fake DMA
allocation, so an accidental temporary destination/copy or row-stride error is
observable. Pixel kernels themselves still require hardware validation.
"""
import ctypes
from types import SimpleNamespace

import numpy as np
import pytest

from kit.adapters import _rga


class Function:
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


class Library:
    def __init__(self):
        self.calls = []
        self.fail = None
        self.rgb = (211, 73, 29)
        self.dma = {7: np.zeros((12, 16), dtype=np.uint8)}
        self.querystring = Function(lambda _: b"RGA_api version       : v1.10.5_[11]")
        self.wrapbuffer_fd_t = Function(self.wrap_fd)
        self.wrapbuffer_virtualaddr_t = Function(self.wrap_array)
        self.imresize_t = Function(self.resize)
        self.imcvtcolor_t = Function(self.convert)
        # Use the actual firmware symbol, not the historical _t spelling.
        self.improcess = Function(self.process)
        self.imfill_t = Function(self.fill)

    def wrap_fd(self, fd, w, h, ws, hs, fmt):
        self.calls.append(("fd", fd, w, h, ws, hs, fmt))
        return SimpleNamespace(data=self.dma[fd], w=w, h=h, fmt=fmt)

    def wrap_array(self, pointer, w, h, ws, hs, fmt):
        count = hs * ws * 3 if fmt == _rga.RK_FORMAT_RGB_888 else hs * ws * 3 // 2
        view = np.ctypeslib.as_array((ctypes.c_uint8 * count).from_address(pointer.value))
        shape = (hs, ws, 3) if fmt == _rga.RK_FORMAT_RGB_888 else (hs * 3 // 2, ws)
        return SimpleNamespace(data=view.reshape(shape), w=w, h=h, fmt=fmt)

    def resize(self, src, dst, fx, fy, interpolation, sync):
        assert src.fmt == dst.fmt == _rga.RK_FORMAT_YCbCr_420_SP
        assert (fx, fy, interpolation, sync) == (0.0, 0.0, 0, 1)
        self.calls.append(("resize", dst.w, dst.h))
        dst.data.fill(85)
        return -5 if self.fail == "resize" else _rga.IM_STATUS_SUCCESS

    def paint(self, dst, x, y, w, h):
        for channel, value in enumerate(self.rgb):
            dst.data[y:y + h, x:x + w, channel] = value

    def convert(self, src, dst, sfmt, dfmt, mode, sync):
        assert (sfmt, dfmt, mode, sync) == (
            _rga.RK_FORMAT_YCbCr_420_SP, _rga.RK_FORMAT_RGB_888, 0, 1)
        self.paint(dst, 0, 0, dst.w, dst.h)
        return _rga.IM_STATUS_SUCCESS

    def process(self, src, dst, _pat, source, dest, _prect, usage):
        assert src.fmt == _rga.RK_FORMAT_YCbCr_420_SP
        assert dst.fmt == _rga.RK_FORMAT_RGB_888
        assert (source.x, source.y, source.width, source.height) == (0, 0, src.w, src.h)
        assert (dest.width, dest.height) == (src.w, src.h)  # no second resize
        assert usage == _rga.IM_SYNC
        assert np.all(src.data == 85)  # consumes the preceding resized NV12
        self.calls.append(("process", dest.x, dest.y, dest.width, dest.height))
        self.paint(dst, dest.x, dest.y, dest.width, dest.height)
        return -5 if self.fail == "process" else _rga.IM_STATUS_SUCCESS

    def fill(self, dst, rect, color, sync):
        assert sync == 1
        assert (rect.x, rect.y, rect.width, rect.height) == (0, 0, dst.w, dst.h)
        assert color == 0x727272
        self.calls.append(("fill",))
        dst.data[:dst.h, :dst.w].fill(color & 255)
        return -5 if self.fail == "fill" else _rga.IM_STATUS_SUCCESS


@pytest.fixture
def rga(monkeypatch):
    lib = Library()
    monkeypatch.delenv("RECAMERA_RGA", raising=False)
    monkeypatch.setattr(_rga, "_load_librga", lambda: lib)
    return _rga.RgaNV12ToRGB(), lib


def geometry(**changes):
    # 12x8 visible camera in 16x8 NV12; 6x4 resized content in an 8x8 canvas.
    # The odd left=1 intentionally preserves caller rounding, not NV12 crop alignment.
    values = dict(fd=7, width=12, height=8, y_stride=16, y_vstride=8,
                  dst_width=8, dst_height=8, dst_window=(1, 2, 7, 6))
    values.update(changes)
    return values


def assert_pixels(canvas, rgb):
    for channel, value in enumerate(rgb):
        assert np.all(canvas[2:6, 1:7, channel] == value)
    assert np.all(canvas[:2] == 114)
    assert np.all(canvas[6:] == 114)
    assert np.all(canvas[2:6, :1] == 114)
    assert np.all(canvas[2:6, 7:] == 114)


def test_preallocated_rgb_matches_existing_two_stage_resize_and_padding(rga):
    backend, lib = rga
    small = backend.resize_nv12_to_rgb(7, 12, 8, 16, 8, 6, 4)
    expected = np.full((8, 8, 3), 114, dtype=np.uint8)
    expected[2:6, 1:7] = small
    out = np.zeros_like(expected)
    result = backend.letterbox_nv12_to_rgb(**geometry(out=out))
    assert result is out
    np.testing.assert_array_equal(result, expected)
    assert_pixels(result, lib.rgb)
    assert ("fd", 7, 12, 8, 16, 8, _rga.RK_FORMAT_YCbCr_420_SP) in lib.calls


def test_same_geometry_reuses_scratch_and_caller_output_without_numpy_allocation(rga, monkeypatch):
    backend, lib = rga
    out = np.empty((8, 8, 3), dtype=np.uint8)
    backend.letterbox_nv12_to_rgb(**geometry(out=out))
    scratch = backend._letterbox_nv12
    lib.rgb = (29, 101, 212)
    monkeypatch.setattr(_rga.np, "empty", lambda *a, **kw: pytest.fail("per-frame allocation"))
    result = backend.letterbox_nv12_to_rgb(**geometry(out=out))
    assert result is out
    assert backend._letterbox_nv12 is scratch
    assert_pixels(result, lib.rgb)


def test_new_geometry_replaces_single_scratch_and_allocating_api_keeps_old_output(rga):
    backend, lib = rga
    previous = backend.letterbox_nv12_to_rgb(**geometry())
    old_scratch = backend._letterbox_nv12
    saved = previous.copy()
    lib.rgb = (1, 2, 3)
    newer = backend.letterbox_nv12_to_rgb(**geometry(dst_window=(0, 0, 8, 8)))
    assert backend._letterbox_nv12.shape == (12, 8)
    assert backend._letterbox_nv12 is not old_scratch
    assert not np.shares_memory(newer, previous)
    np.testing.assert_array_equal(previous, saved)


def test_dma_output_uses_fd_stride_hardware_fill_and_no_cpu_canvas(rga, monkeypatch):
    backend, lib = rga
    lib.dma[9] = np.full((10, 12, 3), 7, dtype=np.uint8)
    assert backend.can_letterbox(dma_output=True)
    backend.letterbox_nv12_to_rgb(**geometry(dst_fd=9, dst_w_stride=12, dst_h_stride=10))
    monkeypatch.setattr(_rga.np, "empty", lambda *a, **kw: pytest.fail("DMA canvas allocation"))
    lib.calls.clear()
    result = backend.letterbox_nv12_to_rgb(**geometry(dst_fd=9, dst_w_stride=12, dst_h_stride=10))
    assert result is None
    assert ("fd", 9, 8, 8, 12, 10, _rga.RK_FORMAT_RGB_888) in lib.calls
    assert [c[0] for c in lib.calls if c[0] != "fd"] == ["resize", "fill", "process"]
    assert_pixels(lib.dma[9][:8, :8], lib.rgb)
    assert np.all(lib.dma[9][8:] == 7)
    assert np.all(lib.dma[9][:8, 8:] == 7)


@pytest.mark.parametrize("changes", [
    {"width": 11}, {"y_stride": 10}, {"y_vstride": 6}, {"y_stride": 17},
    {"dst_window": (-1, 2, 7, 6)}, {"dst_window": (1, 2, 8, 6)},
    {"dst_window": (1, 2, 9, 6)}, {"dst_window": (1, 2, 1, 6)},
    {"dst_window": (1, 2, 7)}, {"dst_window": None},
    {"dst_width": 8.5}, {"fd": -1}, {"dst_fd": -1}, {"dst_fd": 7},
    {"dst_width": True}, {"pad_value": 256}, {"pad_value": -1},
    {"dst_w_stride": 7}, {"dst_h_stride": 7}, {"dst_w_stride": 12},
    {"dst_width": 1 << 40},
])
def test_invalid_geometry_does_not_reach_native_code(rga, changes):
    backend, lib = rga
    with pytest.raises(ValueError):
        backend.letterbox_nv12_to_rgb(**geometry(**changes))
    assert lib.calls == []


@pytest.mark.parametrize("kind", ["dtype", "shape", "noncontiguous", "readonly", "list", "both"])
def test_invalid_output_does_not_reach_native_code(rga, kind):
    backend, lib = rga
    out = np.empty((8, 8, 3), dtype=np.uint8)
    kwargs = {}
    if kind == "dtype":
        out = out.astype(np.float32)
    elif kind == "shape":
        out = out[:4]
    elif kind == "noncontiguous":
        out = out[:, ::-1]
    elif kind == "readonly":
        out.flags.writeable = False
    elif kind == "list":
        out = []
    elif kind == "both":
        kwargs["dst_fd"] = 9
    with pytest.raises(ValueError):
        backend.letterbox_nv12_to_rgb(**geometry(out=out, **kwargs))
    assert lib.calls == []


@pytest.mark.parametrize("stage", ["resize", "fill", "process"])
def test_native_failure_is_surfaced_and_later_stages_do_not_run(rga, stage):
    backend, lib = rga
    lib.dma[9] = np.zeros((8, 8, 3), dtype=np.uint8)
    lib.fail = stage
    with pytest.raises(RuntimeError, match="IM_STATUS=-5"):
        backend.letterbox_nv12_to_rgb(**geometry(dst_fd=9))
    stages = [c[0] for c in lib.calls if c[0] != "fd"]
    assert stages[-1] == stage


@pytest.mark.parametrize("missing", ["imresize_t", "improcess", "imfill_t"])
def test_optional_symbols_control_reusable_cpu_and_dma_capabilities(monkeypatch, missing):
    lib = Library()
    delattr(lib, missing)
    monkeypatch.delenv("RECAMERA_RGA", raising=False)
    monkeypatch.setattr(_rga, "_load_librga", lambda: lib)
    backend = _rga.RgaNV12ToRGB()
    assert backend.can_letterbox() is (missing == "imfill_t")
    assert backend.can_letterbox(dma_output=True) is False
    with pytest.raises(RuntimeError, match="symbols unavailable"):
        backend.letterbox_nv12_to_rgb(**geometry(dst_fd=9))
    assert lib.calls == []


def test_c_improcess_enables_new_dma_letterbox_without_changing_legacy_roi(rga):
    backend, lib = rga
    assert backend.can_crop() is False
    assert backend.can_letterbox(dma_output=True) is True
    with pytest.raises(RuntimeError, match="improcess_t symbol unavailable"):
        backend.crop_nv12_to_rgb(7, 12, 8, 16, 8, (0, 0, 12, 8), 8)
    assert lib.calls == []  # existing ROI callers still take their fallback
    lib.dma[9] = np.zeros((8, 8, 3), dtype=np.uint8)
    backend.letterbox_nv12_to_rgb(**geometry(dst_fd=9))
    assert_pixels(lib.dma[9], lib.rgb)


def test_legacy_improcess_t_remains_available_to_both_paths(monkeypatch):
    lib = Library()
    lib.improcess_t = lib.improcess
    del lib.improcess
    monkeypatch.delenv("RECAMERA_RGA", raising=False)
    monkeypatch.setattr(_rga, "_load_librga", lambda: lib)
    backend = _rga.RgaNV12ToRGB()
    assert backend.can_crop() is True
    assert backend.can_letterbox() is True
    assert_pixels(backend.letterbox_nv12_to_rgb(**geometry()), lib.rgb)

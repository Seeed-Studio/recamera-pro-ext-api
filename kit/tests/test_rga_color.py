"""Check the librga format contract with an independent CPU color decoder.

Unlike a mock that simply paints RGB, this double interprets the *numeric*
source format and the actual NV12 bytes. An NV21 enum cannot pass as NV12 just
because the test imports the same erroneous constant as the implementation.
Hardware conversion and DMA coherency still require the on-device check.
"""
import ctypes
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from kit.adapters import _rga


class Function:
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


class ColorLibrary:
    def __init__(self, source):
        self.source = source
        self.querystring = Function(lambda _: b"RGA_api version       : v1.10.5_[11]")
        self.wrapbuffer_fd_t = Function(self.wrap_fd)
        self.wrapbuffer_virtualaddr_t = Function(self.wrap_array)
        self.imcvtcolor_t = Function(self.convert)
        self.imresize_t = Function(self.resize)

    def wrap_fd(self, fd, w, h, ws, hs, fmt):
        assert fd == 7
        return SimpleNamespace(data=self.source, w=w, h=h, hs=hs, fmt=fmt)

    def wrap_array(self, pointer, w, h, ws, hs, fmt):
        # Numeric values from Rockchip's rga.h, independent of _rga constants.
        assert fmt in (0x0200, 0x0A00, 0x0E00)
        shape = (hs, ws, 3) if fmt == 0x0200 else (hs * 3 // 2, ws)
        view = np.ctypeslib.as_array(
            (ctypes.c_uint8 * int(np.prod(shape))).from_address(pointer.value))
        return SimpleNamespace(data=view.reshape(shape), w=w, h=h, hs=hs, fmt=fmt)

    def resize(self, src, dst, fx, fy, interpolation, sync):
        assert src.fmt == dst.fmt
        assert (fx, fy, interpolation, sync) == (0.0, 0.0, 0, 1)
        dst.data[:dst.h, :dst.w] = cv2.resize(
            src.data[:src.h, :src.w], (dst.w, dst.h), interpolation=cv2.INTER_NEAREST)
        uv = src.data[src.hs:src.hs + src.h // 2, :src.w].reshape(src.h // 2, src.w // 2, 2)
        dst.data[dst.hs:dst.hs + dst.h // 2, :dst.w] = cv2.resize(
            uv, (dst.w // 2, dst.h // 2), interpolation=cv2.INTER_NEAREST).reshape(dst.h // 2, dst.w)
        return 1

    def convert(self, src, dst, sfmt, dfmt, mode, sync):
        assert sfmt == src.fmt and dfmt == dst.fmt == 0x0200
        assert (mode, sync) == (0, 1)
        packed = np.concatenate((src.data[:src.h, :src.w],
                                 src.data[src.hs:src.hs + src.h // 2, :src.w]))
        conversion = {0x0A00: cv2.COLOR_YUV2RGB_NV12,
                      0x0E00: cv2.COLOR_YUV2RGB_NV21}[sfmt]
        dst.data[:dst.h, :dst.w] = cv2.cvtColor(packed, conversion)
        return 1


@pytest.mark.parametrize("yuv,rgb", [
    pytest.param((81, 90, 240), (254, 0, 0), id="red"),
    pytest.param((41, 240, 110), (0, 0, 255), id="blue"),
    pytest.param((128, 128, 128), (130, 130, 130), id="gray"),
])
@pytest.mark.parametrize("stride,vstride", [(32, 16), (48, 24)], ids=["tight", "padded"])
@pytest.mark.parametrize("resize", [False, True], ids=["convert", "resize"])
def test_nv12_colors_follow_native_uv_order(monkeypatch, yuv, rgb, stride, vstride, resize):
    w, h = 32, 16
    source = np.full((vstride * 3 // 2, stride), 17, dtype=np.uint8)
    source[:h, :w] = yuv[0]
    source[vstride:vstride + h // 2, :w:2] = yuv[1]
    source[vstride:vstride + h // 2, 1:w:2] = yuv[2]
    monkeypatch.delenv("RECAMERA_RGA", raising=False)
    monkeypatch.setattr(_rga, "_load_librga", lambda: ColorLibrary(source))
    backend = _rga.RgaNV12ToRGB()
    if resize:
        actual = backend.resize_nv12_to_rgb(7, w, h, stride, vstride, 16, 8)
        expected_shape = (8, 16, 3)
    else:
        actual = backend.convert(7, w, h, stride, vstride)
        expected_shape = (16, 32, 3)
    expected = np.empty(expected_shape, dtype=np.uint8)
    expected[:] = rgb
    np.testing.assert_array_equal(actual, expected)

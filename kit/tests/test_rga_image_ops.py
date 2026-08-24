from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

import kit
from kit.errors import CapabilityError, ImageOperationError, InputValidationError
from kit.media import Rect, RgaContext, Size
from kit.media.image import NV12_FOURCC


class FakeFrame:
    def __init__(self, *, width=640, height=480, released=False, fourcc=NV12_FOURCC):
        self.fd = 9
        self.width = width
        self.height = height
        self.fourcc = fourcc
        self.planes = [
            SimpleNamespace(offset=0, stride=672, vstride=496),
            SimpleNamespace(offset=672 * 496, stride=672, vstride=248),
        ]
        self.buf_size = 672 * (496 + 248)
        self.released = released


class FakeRga:
    def __init__(self, crop=True, resize=True, fail=None):
        self.calls = []
        self._crop = crop
        self._resize = resize
        self.fail = fail

    def can_crop(self):
        return self._crop

    def can_resize(self):
        return self._resize

    def convert(self, **kwargs):
        self.calls.append(("convert", kwargs))
        if self.fail:
            raise self.fail
        return np.full((kwargs["height"], kwargs["width"], 3), 4, dtype=np.uint8)

    def resize_nv12_to_rgb(self, **kwargs):
        self.calls.append(("resize", kwargs))
        if self.fail:
            raise self.fail
        return np.full((kwargs["dst_height"], kwargs["dst_width"], 3),
                       5, dtype=np.uint8)

    def crop_nv12_to_rgb(self, **kwargs):
        self.calls.append(("crop", kwargs))
        if self.fail:
            raise self.fail
        size = kwargs["dst_size"]
        return np.full((size, size, 3), 6, dtype=np.uint8)


def test_convert_uses_public_fd_and_exact_producer_stride():
    assert kit.RgaContext is RgaContext
    backend = FakeRga()
    image = RgaContext(backend).convert_nv12(FakeFrame())
    method, args = backend.calls[0]
    assert method == "convert"
    assert args["fd"] == 9
    assert args["y_stride"] == 672
    assert args["y_vstride"] == 496
    assert image.numpy().shape == (480, 640, 3)
    assert image.owned is True


def test_resize_returns_exact_coordinate_mapping():
    result = RgaContext(FakeRga()).resize_nv12(FakeFrame(), Size(320, 240))
    assert result.image.numpy().shape == (240, 320, 3)
    assert result.mapping.to_source(160, 120) == (320.0, 240.0)
    assert result.mapping.box_to_source((0, 0, 320, 240)) == \
        (0.0, 0.0, 640.0, 480.0)


def test_letterbox_mapping_excludes_padding():
    result = RgaContext(FakeRga()).letterbox_nv12(
        FakeFrame(width=640, height=360), Size(640, 640))
    assert result.image.numpy().shape == (640, 640, 3)
    assert result.mapping.output_rect == Rect(0, 140, 640, 500)
    assert result.mapping.to_source(320, 320) == (320.0, 180.0)
    assert np.all(result.image.numpy()[0] == 114)


def test_crop_reports_even_aligned_actual_source_mapping():
    backend = FakeRga()
    result = RgaContext(backend).crop_nv12(
        FakeFrame(), Rect(11, 13, 111, 215), Size(224, 224))
    method, args = backend.calls[0]
    assert method == "crop"
    assert args["src_rect"] == (10, 12, 110, 214)
    assert result.mapping.source_rect == Rect(10, 12, 110, 214)
    assert result.mapping.to_source(224, 224) == (110.0, 214.0)


def test_released_wrong_format_and_nonintersecting_frames_fail_before_backend():
    ctx = RgaContext(FakeRga())
    with pytest.raises(InputValidationError) as released:
        ctx.convert_nv12(FakeFrame(released=True))
    assert released.value.code == "source_released"
    with pytest.raises(InputValidationError):
        ctx.convert_nv12(FakeFrame(fourcc=0x12345678))
    with pytest.raises(InputValidationError):
        ctx.crop_nv12(FakeFrame(), Rect(700, 500, 800, 600), Size(32, 32))


def test_invalid_nv12_layout_resize_geometry_and_pad_fail_before_backend():
    backend = FakeRga()
    ctx = RgaContext(backend)
    bad_stride = FakeFrame()
    bad_stride.planes = [
        SimpleNamespace(offset=0, stride=320, vstride=480),
        SimpleNamespace(offset=320 * 480, stride=320, vstride=240),
    ]
    with pytest.raises(InputValidationError):
        ctx.convert_nv12(bad_stride)
    with pytest.raises(InputValidationError):
        ctx.convert_nv12(FakeFrame(width=639))
    with pytest.raises(InputValidationError):
        ctx.resize_nv12(FakeFrame(), Size(319, 240))
    with pytest.raises(InputValidationError):
        ctx.letterbox_nv12(FakeFrame(), Size(320, 320), pad_value=300)
    assert backend.calls == []


def test_missing_or_noncontiguous_uv_plane_fails_before_backend():
    backend = FakeRga()
    ctx = RgaContext(backend)
    missing = FakeFrame()
    missing.planes = missing.planes[:1]
    with pytest.raises(InputValidationError):
        ctx.convert_nv12(missing)
    displaced = FakeFrame()
    displaced.planes[1] = SimpleNamespace(
        offset=123, stride=672, vstride=248)
    with pytest.raises(InputValidationError):
        ctx.convert_nv12(displaced)
    assert backend.calls == []


def test_fd_wrap_rejects_short_uv_padding_and_buffer_bounds():
    backend = FakeRga()
    ctx = RgaContext(backend)
    short_uv = FakeFrame()
    short_uv.planes[1] = SimpleNamespace(
        offset=672 * 496, stride=672, vstride=240)
    with pytest.raises(InputValidationError) as caught:
        ctx.convert_nv12(short_uv)
    assert caught.value.details["required_uv_vstride"] == 248

    too_small = FakeFrame()
    too_small.buf_size -= 1
    with pytest.raises(InputValidationError):
        ctx.convert_nv12(too_small)
    assert backend.calls == []


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("resize_nv12", ((320,),)),
        ("letterbox_nv12", (None,)),
        ("crop_nv12", ((0, 0, 10), (32, 32))),
        ("crop_nv12", ((0, 0, 10, 10), (32,))),
    ],
)
def test_public_geometry_errors_are_typed(method, args):
    with pytest.raises(InputValidationError):
        getattr(RgaContext(FakeRga()), method)(FakeFrame(), *args)


def test_missing_crop_capability_and_backend_error_are_typed():
    with pytest.raises(CapabilityError):
        RgaContext(FakeRga(crop=False)).crop_nv12(
            FakeFrame(), Rect(0, 0, 20, 20), Size(32, 32))
    backend_error = RuntimeError("iommu fault")
    with pytest.raises(ImageOperationError) as caught:
        RgaContext(FakeRga(fail=backend_error)).convert_nv12(FakeFrame())
    assert caught.value.__cause__ is backend_error
    assert caught.value.operation == "rga.convert_nv12"


def test_missing_native_resize_symbol_is_reported_before_call():
    backend = FakeRga(resize=False)
    ctx = RgaContext(backend)
    assert ctx.capabilities.resize is False
    with pytest.raises(CapabilityError) as caught:
        ctx.resize_nv12(FakeFrame(), Size(320, 240))
    assert caught.value.operation == "rga.resize_nv12"
    assert backend.calls == []


@pytest.mark.parametrize(
    "bad",
    [
        np.zeros((480, 640), dtype=np.uint8),
        np.zeros((480, 640, 3), dtype=np.float32),
        np.zeros((1, 1, 3), dtype=np.uint8),
    ],
)
def test_invalid_backend_output_is_rejected(bad):
    backend = FakeRga()
    backend.convert = lambda **_kwargs: bad
    with pytest.raises(ImageOperationError) as caught:
        RgaContext(backend).convert_nv12(FakeFrame())
    assert caught.value.code == "invalid_backend_output"


def test_control_flow_base_exception_is_not_wrapped():
    interrupt = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        RgaContext(FakeRga(fail=interrupt)).convert_nv12(FakeFrame())


def test_closed_context_rejects_work():
    ctx = RgaContext(FakeRga())
    ctx.close()
    with pytest.raises(ImageOperationError) as caught:
        ctx.convert_nv12(FakeFrame())
    assert caught.value.code == "context_closed"


def test_ctypes_rga_binding_rejects_unverified_struct_abi(monkeypatch):
    from kit.adapters import _rga

    class Function:
        def __init__(self, result=None):
            self.result = result
            self.restype = None
            self.argtypes = None

        def __call__(self, *_args):
            return self.result

    class FakeLibrary:
        querystring = Function(b"RGA_api version       : v2.0.0_[1]\n")
        wrapbuffer_fd_t = Function()
        wrapbuffer_virtualaddr_t = Function()
        imcvtcolor_t = Function()

    monkeypatch.setattr(_rga, "_load_librga", lambda: FakeLibrary())
    with pytest.raises(RuntimeError, match="unsupported librga ctypes ABI"):
        _rga.RgaNV12ToRGB()


def test_librga_loader_probes_firmware_oem_path_before_sonames(monkeypatch):
    from kit.adapters import _rga

    loaded = object()
    attempts = []

    def fake_cdll(candidate):
        attempts.append(candidate)
        if candidate == "/oem/usr/lib/librga.so":
            return loaded
        raise OSError(candidate)

    monkeypatch.setattr(_rga.ctypes, "CDLL", fake_cdll)
    monkeypatch.setattr(
        _rga.ctypes.util, "find_library",
        lambda _name: pytest.fail("find_library must not run after OEM load"),
    )

    assert _rga._load_librga() is loaded
    assert attempts == ["/oem/usr/lib/librga.so"]


def test_librga_loader_keeps_soname_and_find_library_fallbacks(monkeypatch):
    from kit.adapters import _rga

    loaded = object()
    attempts = []

    def fake_cdll(candidate):
        attempts.append(candidate)
        if candidate == "resolved-rga.so":
            return loaded
        raise OSError(candidate)

    monkeypatch.setattr(_rga.ctypes, "CDLL", fake_cdll)
    monkeypatch.setattr(_rga.ctypes.util, "find_library", lambda _name: "resolved-rga.so")

    assert _rga._load_librga() is loaded
    assert attempts == [
        "/oem/usr/lib/librga.so",
        "/oem/usr/lib/librga.so.2",
        "/oem/usr/lib/librga.so.1",
        "librga.so.2",
        "librga.so",
        "librga.so.1",
        "resolved-rga.so",
    ]


def test_ctypes_rga_binding_accepts_only_verified_1105_build(monkeypatch):
    from kit.adapters import _rga

    class Function:
        def __init__(self, result=None):
            self.result = result
            self.restype = None
            self.argtypes = None

        def __call__(self, *_args):
            return self.result

    class FakeLibrary:
        querystring = Function(
            b"RGA_api version       : v1.10.5_[11]\nRGA version: RGA_2")
        wrapbuffer_fd_t = Function()
        wrapbuffer_virtualaddr_t = Function()
        imcvtcolor_t = Function()

    monkeypatch.setattr(_rga, "_load_librga", lambda: FakeLibrary())
    backend = _rga.RgaNV12ToRGB()
    assert "v1.10.5_[11]" in backend.version_info

"""Strict official-adapter error and lifetime behavior."""

from __future__ import annotations

import importlib.util
import os
import sys

import numpy as np
import pytest

from kit.adapters.official import OfficialFrameSource, OfficialResultSink
from kit.errors import InputValidationError, TransportError


_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
_SDK_INIT = os.path.join(_ROOT, "sdk", "python", "recamera_ext", "__init__.py")


def _load_real_recamera_ext():
    spec = importlib.util.spec_from_file_location(
        "recamera_ext_real_strict", _SDK_INIT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _real_recamera_ext(monkeypatch):
    """Keep this module isolated from tests that replace sys.modules['recamera_ext']."""
    module = _load_real_recamera_ext()
    monkeypatch.setitem(sys.modules, "recamera_ext", module)
    return module


class _Frame:
    width = 8
    height = 4
    pts_us = 123
    fourcc = 0x3231564E
    fd = 7
    planes = ((0, 8, 4), (32, 8, 2))

    def to_bgr(self):
        return np.zeros((self.height, self.width, 3), dtype=np.uint8)


class _StrictSource:
    width = 8
    height = 4
    fourcc = 0x3231564E
    pool_depth = 2
    max_outstanding = 1

    instances = []

    def __init__(self, **_kwargs):
        self.calls = 0
        self.closed = 0
        self.fail_close = False
        self.__class__.instances.append(self)

    def acquire(self):
        recamera_ext = sys.modules["recamera_ext"]
        self.calls += 1
        if self.calls == 1:
            raise recamera_ext.AcquireTimeoutError(operation="frame.next")
        if self.calls == 2:
            return _Frame()
        raise recamera_ext.FormatError(
            operation="frame.next", detail="malformed producer layout")

    def close(self):
        self.closed += 1
        if self.fail_close:
            raise OSError("close failed")


def test_official_source_retries_timeout_but_surfaces_terminal_native_error(
        monkeypatch, _real_recamera_ext):
    _StrictSource.instances.clear()
    monkeypatch.setattr(_real_recamera_ext, "FrameSource", _StrictSource)
    source = OfficialFrameSource(prefer_rga=False, verbose=False)
    stream = source.frames()
    frame = next(stream)
    assert frame.data.shape == (4, 8, 3)
    with pytest.raises(TransportError) as caught:
        next(stream)
    assert isinstance(caught.value.__cause__, _real_recamera_ext.FormatError)
    assert caught.value.operation == "frame.acquire"
    assert _StrictSource.instances[0].closed == 1


def test_stream_error_is_not_replaced_by_close_failure(monkeypatch, _real_recamera_ext):
    class CloseFailingSource(_StrictSource):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.fail_close = True

    monkeypatch.setattr(_real_recamera_ext, "FrameSource", CloseFailingSource)
    stream = OfficialFrameSource(prefer_rga=False, verbose=False).frames()
    next(stream)
    with pytest.raises(TransportError) as caught:
        next(stream)
    assert isinstance(caught.value.__cause__, _real_recamera_ext.FormatError)


def test_checked_result_publish_requires_frame_geometry_before_counting_success():
    sink = OfficialResultSink(verbose=False)
    sink._sink = object()  # skip native open; geometry check must happen first
    with pytest.raises(InputValidationError) as caught:
        sink.emit_checked({"results": []}, 0.0)
    assert caught.value.operation == "result.emit"
    assert sink.stats()["send_calls"] == 0


def test_official_close_errors_are_typed():
    class Broken:
        def close(self):
            raise OSError("socket close failed")

    source = OfficialFrameSource(prefer_rga=False, verbose=False)
    source._src = Broken()
    with pytest.raises(TransportError) as caught:
        source.close()
    assert caught.value.operation == "frame.close"

    sink = OfficialResultSink(verbose=False)
    sink._sink = Broken()
    with pytest.raises(TransportError) as caught:
        sink.close()
    assert caught.value.operation == "result.close"

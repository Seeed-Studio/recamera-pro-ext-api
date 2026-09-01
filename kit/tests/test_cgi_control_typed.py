from __future__ import annotations

import sys
from types import SimpleNamespace

import numpy as np
import pytest

from kit.adapters.cgi_control import CgiControl
from kit.errors import (
    AdapterError,
    ConfigurationError,
    InputValidationError,
    TransportError,
)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"host": ""},
        {"port": 0},
        {"port": 65536},
        {"model_id": -1},
        {"timeout": 0},
        {"timeout": float("inf")},
    ],
)
def test_constructor_rejects_invalid_control_endpoint(kwargs):
    with pytest.raises(ConfigurationError) as caught:
        CgiControl(**kwargs)
    assert caught.value.operation == "control.open"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"enable": 1},
        {"enable": True, "model": ""},
        {"enable": True, "fps": -1},
        {"enable": True, "fps": "5"},
        {"enable": True, "fps": True},
    ],
)
def test_set_inference_validates_before_http(monkeypatch, kwargs):
    ctl = CgiControl()
    monkeypatch.setattr(ctl, "_request", lambda *_a, **_kw: pytest.fail(
        "invalid input reached HTTP"))
    with pytest.raises(InputValidationError) as caught:
        ctl.set_inference(**kwargs)
    assert caught.value.operation == "control.set_inference"


def test_set_inference_returns_device_envelope(monkeypatch):
    ctl = CgiControl()
    expected = {"code": 0, "message": "success", "generation": 7}
    monkeypatch.setattr(ctl, "_request", lambda *_a, **_kw: expected)
    assert ctl.set_inference(enable=False) is expected


class _Frame:
    def __init__(self, source, fmt="RGB"):
        self._source = source
        self._data = np.arange(18, dtype=np.uint8).reshape(2, 3, 3)
        self.fmt = fmt
        self.released = False

    @property
    def data(self):
        assert not self._source.closed
        assert not self.released
        return self._data

    def release(self):
        self.released = True


class _Iterator:
    def __init__(self, values):
        self.values = iter(values)
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self.values)

    def close(self):
        self.closed = True


class _Source:
    def __init__(self, with_frame=True, fmt="RGB"):
        self.closed = False
        self.frame = _Frame(self, fmt=fmt)
        self.iterator = _Iterator([self.frame] if with_frame else [])

    def frames(self):
        return self.iterator

    def close(self):
        self.closed = True


def _install_source(monkeypatch, source):
    from kit.adapters import registry

    monkeypatch.setattr(
        registry,
        "select_frame_source",
        lambda **_kwargs: source,
    )


def test_snapshot_encodes_before_releasing_borrowed_frame(monkeypatch):
    source = _Source()
    _install_source(monkeypatch, source)

    def imencode(extension, pixels):
        assert extension == ".jpg"
        assert not source.closed
        assert not source.frame.released
        assert np.array_equal(pixels, source.frame._data[:, :, ::-1])
        return True, np.asarray([1, 2, 3], dtype=np.uint8)

    monkeypatch.setitem(sys.modules, "cv2", SimpleNamespace(imencode=imencode))
    assert CgiControl().snapshot() == b"\x01\x02\x03"
    assert source.frame.released is True
    assert source.iterator.closed is True
    assert source.closed is True


def test_snapshot_empty_source_is_retryable_and_always_closed(monkeypatch):
    source = _Source(with_frame=False)
    _install_source(monkeypatch, source)
    monkeypatch.setitem(
        sys.modules,
        "cv2",
        SimpleNamespace(imencode=lambda *_args: pytest.fail("must not encode")),
    )
    with pytest.raises(TransportError) as caught:
        CgiControl().snapshot()
    assert caught.value.operation == "control.snapshot.acquire"
    assert caught.value.retryable is True
    assert source.iterator.closed is True
    assert source.closed is True


def test_snapshot_rejects_unknown_format_and_releases_frame(monkeypatch):
    source = _Source(fmt="NV12")
    _install_source(monkeypatch, source)
    monkeypatch.setitem(
        sys.modules,
        "cv2",
        SimpleNamespace(imencode=lambda *_args: pytest.fail("must not encode")),
    )
    with pytest.raises(AdapterError) as caught:
        CgiControl().snapshot()
    assert caught.value.code == "unsupported_format"
    assert source.frame.released is True
    assert source.closed is True


def test_snapshot_open_error_is_typed_and_redacts_url(monkeypatch):
    from kit.adapters import registry

    def unavailable(**_kwargs):
        raise ConnectionError("camera offline")

    monkeypatch.setattr(registry, "select_frame_source", unavailable)
    monkeypatch.setitem(
        sys.modules,
        "cv2",
        SimpleNamespace(imencode=lambda *_args: None),
    )
    ctl = CgiControl(frame_url="rtsp://admin:secret@127.0.0.1/live/1")
    with pytest.raises(TransportError) as caught:
        ctl.snapshot()
    assert caught.value.operation == "control.snapshot.open"
    assert caught.value.details["url"] == "rtsp://***@127.0.0.1/live/1"
    assert "secret" not in str(caught.value.details)

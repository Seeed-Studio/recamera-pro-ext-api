"""Typed-error and native-loader tests that require no device or shared lib."""

from __future__ import annotations

import ctypes
import logging

import pytest


@pytest.mark.parametrize(
    ("code", "exception_name"),
    [
        (1, "VersionError"),
        (2, "AuthenticationError"),
        (3, "BusyError"),
        (4, "FormatError"),
        (5, "BackpressureError"),
        (6, "RateLimitError"),
        (7, "InternalError"),
    ],
)
@pytest.mark.parametrize("sign", [1, -1])
def test_error_from_rc_maps_every_frozen_code(sdk_module, code, exception_name, sign):
    exc = sdk_module.error_from_rc("native_op", sign * code, detail="context")

    assert type(exc) is getattr(sdk_module, exception_name)
    assert isinstance(exc, sdk_module.RecameraError)
    assert isinstance(exc, RuntimeError)  # compatibility with the old wrapper
    assert exc.operation == "native_op"
    assert exc.code is sdk_module.ErrorCode(code)
    assert exc.code_value == code
    assert exc.rc == sign * code
    assert exc.detail == "context"
    assert exception_name in type(exc).__name__
    assert sdk_module.ErrorCode(code).name in str(exc)


def test_error_from_rc_preserves_unknown_code_and_rejects_success(sdk_module):
    exc = sdk_module.error_from_rc("future_op", ctypes.c_int(-41))
    assert isinstance(exc, sdk_module.UnknownNativeError)
    assert exc.code is None
    assert exc.rc == -41
    assert exc.code_value == 41
    assert "unknown native error 41" in str(exc)

    with pytest.raises(ValueError, match="non-zero"):
        sdk_module.error_from_rc("not_an_error", 0)


def test_error_family_compatibility_and_aliases(sdk_module):
    timeout = sdk_module.AcquireTimeoutError(operation="next")
    assert isinstance(timeout, TimeoutError)
    assert isinstance(timeout, sdk_module.RecameraError)
    assert timeout.retryable
    assert sdk_module.FrameTimeoutError is sdk_module.AcquireTimeoutError

    assert issubclass(sdk_module.LibraryLoadError, OSError)
    assert issubclass(sdk_module.ResultTooLarge, ValueError)
    assert sdk_module.AuthError is sdk_module.AuthenticationError
    assert sdk_module.ResourceBusyError is sdk_module.BusyError

    assert sdk_module.error_from_rc("busy", -3).retryable
    assert not sdk_module.error_from_rc("format", -4).retryable


def test_public_exports_include_lease_buffer_and_typed_errors(sdk_module):
    expected = {
        "FrameLease",
        "Frame",
        "BorrowedBuffer",
        "PlaneLayout",
        "RecameraError",
        "BufferReleasedError",
        "AcquireTimeoutError",
        "CapabilityUnavailableError",
        "InferenceLease",
        "InferenceState",
        "InferenceStatus",
    }
    assert expected <= set(sdk_module.__all__)
    for name in expected:
        assert getattr(sdk_module, name) is not None


def test_native_loader_raises_typed_oserror_and_logs_candidates(
    sdk_module, monkeypatch, caplog
):
    attempts = []

    def fail_cdll(candidate):
        attempts.append(candidate)
        raise OSError(f"cannot load {candidate}")

    monkeypatch.setattr(sdk_module.ctypes, "CDLL", fail_cdll)
    monkeypatch.setattr(sdk_module.ctypes.util, "find_library", lambda _name: None)
    caplog.set_level(logging.DEBUG, logger=sdk_module.__name__)

    with pytest.raises(sdk_module.LibraryLoadError) as raised:
        sdk_module._load("/opt/test/librecamera_ext.so.1")

    assert isinstance(raised.value, OSError)
    assert attempts == [
        "/opt/test/librecamera_ext.so.1",
        "librecamera_ext.so.1",
        "librecamera_ext.so",
    ]
    assert "/opt/test/librecamera_ext.so.1" in raised.value.detail
    assert "candidate" in caplog.text and "rejected" in caplog.text


class _FakeResultLib:
    def __init__(self, *, open_error=0, send_rc=0):
        self.open_error = open_error
        self.send_rc = send_rc
        self.closed = 0

    def rc_ext_result_open(self, _source_id, err_ptr):
        ctypes.cast(err_ptr, ctypes.POINTER(ctypes.c_int))[0] = self.open_error
        return 0 if self.open_error else 0xCAFE

    def rc_ext_result_send_detections(self, _handle, _pts, _items, _count):
        return self.send_rc

    def rc_ext_result_close(self, _handle):
        self.closed += 1


def test_result_open_and_send_use_typed_native_errors(sdk_module, monkeypatch):
    open_failure = _FakeResultLib(open_error=3)
    monkeypatch.setattr(sdk_module, "_load", lambda _path=None: open_failure)
    with pytest.raises(sdk_module.BusyError) as raised:
        sdk_module.ResultSink("test-app")
    assert raised.value.rc == 3

    send_failure = _FakeResultLib(send_rc=-6)
    monkeypatch.setattr(sdk_module, "_load", lambda _path=None: send_failure)
    sink = sdk_module.ResultSink("test-app")
    with pytest.raises(sdk_module.RateLimitError) as raised:
        sink.send_detections(123, [(0.1, 0.2, 0.3, 0.4, 0.9, "person")])
    assert raised.value.rc == -6
    assert sink.stats()["send_error"] == 1

    assert sink.close() is True
    assert sink.close() is False
    assert send_failure.closed == 1
    with pytest.raises(sdk_module.HandleClosedError):
        sink.send_detections(0, [])


def test_handle_context_preserves_body_error_when_close_fails(
    sdk_module, caplog
):
    class BodyError(Exception):
        pass

    class CleanupAbort(BaseException):
        pass

    class FailingLib:
        close_calls = 0

        def close(self, _handle):
            self.close_calls += 1
            raise CleanupAbort("native close failed")

    class TestHandle(sdk_module._Handle):
        _close_cfn = "close"

        def __init__(self):
            self._lib = FailingLib()
            self._h = 1

    handle = TestHandle()
    caplog.set_level(logging.ERROR, logger=sdk_module.__name__)
    original = BodyError("body failure")
    with pytest.raises(BodyError) as raised:
        with handle:
            raise original

    assert raised.value is original
    assert any("CleanupAbort" in note for note in raised.value.__notes__)
    assert "cleanup failed" in caplog.text
    assert handle._lib.close_calls == 1
    assert handle.closed


def test_handle_context_propagates_close_error_without_body_error(sdk_module):
    class CleanupError(Exception):
        pass

    class FailingLib:
        def close(self, _handle):
            raise CleanupError("native close failed")

    class TestHandle(sdk_module._Handle):
        _close_cfn = "close"

        def __init__(self):
            self._lib = FailingLib()
            self._h = 1

    with pytest.raises(CleanupError):
        with TestHandle():
            pass


def test_missing_optional_native_surfaces_are_capability_errors(
    sdk_module, monkeypatch
):
    monkeypatch.setattr(sdk_module, "_load", lambda _path=None: object())

    with pytest.raises(sdk_module.CapabilityUnavailableError):
        sdk_module.ProbeSource(["metrics"])
    with pytest.raises(sdk_module.CapabilityUnavailableError):
        sdk_module.MaskControl()


def test_ctypes_abi_layouts_remain_compatible(sdk_module):
    assert ctypes.sizeof(sdk_module.Box) == 40
    assert ctypes.sizeof(sdk_module.Classification) == 40
    assert ctypes.sizeof(sdk_module.Segmentation) == 48
    assert ctypes.sizeof(sdk_module.Tracking) == 40
    assert ctypes.sizeof(sdk_module.Point) == 16
    assert ctypes.sizeof(sdk_module.KeypointInstance) == 56
    assert ctypes.sizeof(sdk_module._Plane) == 12
    assert ctypes.sizeof(sdk_module._FrameBuf) == 96
    assert ctypes.sizeof(sdk_module._Cfg) == 16

    offsets = {
        name: getattr(sdk_module._FrameBuf, name).offset
        for name, *_rest in sdk_module._FrameBuf._fields_
    }
    assert offsets == {
        "seq": 0,
        "pts_us": 8,
        "width": 16,
        "height": 20,
        "fourcc": 24,
        "buf_size": 28,
        "flags": 32,
        "chn_id": 34,
        "n_planes": 35,
        "plane": 36,
        "fd": 72,
        "_base": 80,
        "_map_len": 88,
    }

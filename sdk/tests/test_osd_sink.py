"""Dedicated OSD-only Python ABI tests with a fake native library."""

from __future__ import annotations

import ctypes

import pytest


class FakeOsdLib:
    def __init__(self, open_error=0, send_rc=0):
        self.open_error = open_error
        self.send_rc = send_rc
        self.calls = []
        self.closed = 0

    def rc_ext_osd_open(self, error):
        ctypes.cast(error, ctypes.POINTER(ctypes.c_int))[0] = self.open_error
        return 0 if self.open_error else 0x0D50

    def rc_ext_osd_send_detections(self, handle, pts_us, boxes, count):
        self.calls.append((handle, int(pts_us.value), int(count.value)))
        return self.send_rc

    def rc_ext_osd_close(self, handle):
        self.closed += 1


def test_osd_sink_uses_only_dedicated_native_symbols(sdk_module, monkeypatch):
    fake = FakeOsdLib()
    monkeypatch.setattr(sdk_module, "_load", lambda _path=None: fake)
    sink = sdk_module.OsdSink()
    assert sink.send_detections(
        123, [(0.1, 0.2, 0.3, 0.4, 0.9, "person", 7)]
    ) == 0
    assert fake.calls == [(0x0D50, 123, 1)]
    assert sink.stats()["sent"] == 1
    assert sink.close() is True
    assert fake.closed == 1


def test_osd_sink_rejects_non_detection_and_box_overflow_locally(
    sdk_module, monkeypatch
):
    fake = FakeOsdLib()
    monkeypatch.setattr(sdk_module, "_load", lambda _path=None: fake)
    sink = sdk_module.OsdSink()
    with pytest.raises(sdk_module.FormatError, match="at most 64"):
        sink.send_detections(0, [(0, 0, 1, 1, 1, "x")] * 65)
    with pytest.raises(sdk_module.FormatError, match="detection.*only"):
        sink.send_classification(0, [])
    assert fake.calls == []
    sink.close()


def test_osd_sink_reports_missing_or_rejected_capability(sdk_module, monkeypatch):
    monkeypatch.setattr(sdk_module, "_load", lambda _path=None: object())
    with pytest.raises(sdk_module.CapabilityUnavailableError):
        sdk_module.OsdSink()

    failure = FakeOsdLib(open_error=int(sdk_module.ErrorCode.EAUTH))
    monkeypatch.setattr(sdk_module, "_load", lambda _path=None: failure)
    with pytest.raises(sdk_module.AuthenticationError):
        sdk_module.OsdSink()

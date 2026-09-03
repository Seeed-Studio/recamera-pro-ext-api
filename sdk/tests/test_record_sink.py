"""Recording-only Python ABI tests backed by a fake native library."""

from __future__ import annotations

import ctypes
import threading
import time

import pytest


class FakeRecordLib:
    def __init__(
        self, *, open_error=0, send_rc=0, reset_rc=0, call_delay=0.002
    ):
        self.open_error = open_error
        self.send_rc = send_rc
        self.reset_rc = reset_rc
        self.call_delay = call_delay
        self.calls = []
        self.closed = 0
        self.closed_while_active = False
        self.call_entered = threading.Event()
        self._call_lock = threading.Lock()
        self._active_calls = 0
        self.max_active_calls = 0

    @staticmethod
    def _value(value):
        return int(getattr(value, "value", value))

    @staticmethod
    def _app_id(value):
        value = getattr(value, "value", value)
        return value.decode("ascii")

    def rc_ext_record_open(self, error):
        ctypes.cast(error, ctypes.POINTER(ctypes.c_int))[0] = self.open_error
        return 0 if self.open_error else 0xA11CE

    def _record(self, name, handle, app_id, pts_us, items, count):
        with self._call_lock:
            self._active_calls += 1
            self.max_active_calls = max(self.max_active_calls, self._active_calls)
            self.call_entered.set()
        try:
            # Release the GIL long enough for a competing Python thread. The
            # RecordSink lock must still serialize the ordered native stream.
            time.sleep(self.call_delay)
            size = self._value(count)
            detail = None
            if name in ("classification", "events") and size:
                detail = [
                    (
                        float(items[index].score),
                        int(items[index].class_id),
                        items[index].label.decode(),
                        int(items[index].has_box),
                    )
                    for index in range(size)
                ]
            self.calls.append(
                (
                    name,
                    self._value(handle),
                    self._app_id(app_id),
                    self._value(pts_us),
                    size,
                    detail,
                )
            )
            return self.send_rc
        finally:
            with self._call_lock:
                self._active_calls -= 1

    def rc_ext_record_send_detections(
        self, handle, app_id, pts_us, items, count
    ):
        return self._record(
            "detections", handle, app_id, pts_us, items, count
        )

    def rc_ext_record_send_classification(
        self, handle, app_id, pts_us, items, count
    ):
        return self._record(
            "classification", handle, app_id, pts_us, items, count
        )

    def rc_ext_record_send_tracking(
        self, handle, app_id, pts_us, items, count
    ):
        return self._record("tracking", handle, app_id, pts_us, items, count)

    def rc_ext_record_send_events(
        self, handle, app_id, pts_us, items, count
    ):
        return self._record("events", handle, app_id, pts_us, items, count)

    def rc_ext_record_send_keypoints(
        self, handle, app_id, pts_us, items, count
    ):
        return self._record("keypoints", handle, app_id, pts_us, items, count)

    def rc_ext_record_reset(self, handle, app_id):
        self.calls.append(
            ("reset", self._value(handle), self._app_id(app_id), 0, 0, None)
        )
        return self.reset_rc

    def rc_ext_record_close(self, _handle):
        with self._call_lock:
            self.closed_while_active = self._active_calls != 0
        self.closed += 1


def _sink(sdk_module, monkeypatch, **kwargs):
    fake = FakeRecordLib(**kwargs)
    monkeypatch.setattr(sdk_module, "_load", lambda _path=None: fake)
    return sdk_module.RecordSink(), fake


def test_record_sink_routes_all_supported_types_with_ordered_app_id(
    sdk_module, monkeypatch
):
    sink, fake = _sink(sdk_module, monkeypatch)

    assert sink.reset("fall-detection") == 0
    assert sink.send_detections(
        "fall-detection",
        101,
        [(0.1, 0.2, 0.8, 0.9, 0.75, "person", 3)],
    ) == 0
    assert sink.send_classifications(
        "fall-detection",
        102,
        [(1.0, "fall"), (0.8, "person", 7, (0.1, 0.2, 0.8, 0.9))],
    ) == 0
    assert sink.send_classification(
        "second-app", 103, [(0.5, "standing", -1)]
    ) == 0
    assert sink.send_events("second-app", 103, [(1.0, "motion", 0)]) == 0
    assert sink.send_tracking(
        "second-app",
        104,
        [(0.1, 0.2, 0.8, 0.9, 0.75, 3, "person", 42)],
    ) == 0
    assert sink.send_keypoints(
        "second-app",
        105,
        [{"points": [(0.3, 0.4, 0.9, 1)]}],
    ) == 0

    assert [call[:5] for call in fake.calls] == [
        ("reset", 0xA11CE, "fall-detection", 0, 0),
        ("detections", 0xA11CE, "fall-detection", 101, 1),
        ("classification", 0xA11CE, "fall-detection", 102, 2),
        ("classification", 0xA11CE, "second-app", 103, 1),
        ("events", 0xA11CE, "second-app", 103, 1),
        ("tracking", 0xA11CE, "second-app", 104, 1),
        ("keypoints", 0xA11CE, "second-app", 105, 1),
    ]
    assert fake.calls[2][5][0][1:] == (0, "fall", 0)
    assert fake.calls[2][5][1][1:] == (7, "person", 1)
    assert sink.stats() == {
        "sent": 6,
        "oversize_rejected": 0,
        "send_error": 0,
    }
    assert sink.close() is True
    assert sink.close() is False
    assert fake.closed == 1


@pytest.mark.parametrize(
    "app_id",
    [None, b"bytes", "", "builtin", "Bad-App", "bad_app", "a" * 65],
)
def test_record_sink_rejects_invalid_app_ids_before_native_call(
    sdk_module, monkeypatch, app_id
):
    sink, fake = _sink(sdk_module, monkeypatch)

    with pytest.raises(sdk_module.FormatError, match="app_id must match"):
        sink.send_detections(app_id, 0, [])
    with pytest.raises(sdk_module.FormatError, match="app_id must match"):
        sink.reset(app_id)

    assert fake.calls == []
    sink.close()


def test_record_sink_accepts_maximum_app_id_and_rejects_segmentation(
    sdk_module, monkeypatch
):
    sink, fake = _sink(sdk_module, monkeypatch)
    app_id = "a" * 64

    assert sink.send_detections(app_id, 0, []) == 0
    with pytest.raises(sdk_module.FormatError, match="does not support segmentation"):
        sink.send_segmentation(app_id, 0, [])

    assert [call[2] for call in fake.calls] == [app_id]
    sink.close()


def test_record_sink_uses_typed_capability_open_and_operation_errors(
    sdk_module, monkeypatch
):
    monkeypatch.setattr(sdk_module, "_load", lambda _path=None: object())
    with pytest.raises(sdk_module.CapabilityUnavailableError) as unavailable:
        sdk_module.RecordSink()
    assert unavailable.value.operation == "rc_ext_record_open"

    open_failure = FakeRecordLib(
        open_error=int(sdk_module.ErrorCode.EAUTH)
    )
    monkeypatch.setattr(sdk_module, "_load", lambda _path=None: open_failure)
    with pytest.raises(sdk_module.AuthenticationError) as rejected:
        sdk_module.RecordSink()
    assert rejected.value.rc == int(sdk_module.ErrorCode.EAUTH)

    sink, fake = _sink(
        sdk_module,
        monkeypatch,
        send_rc=-int(sdk_module.ErrorCode.ERATELIMIT),
    )
    with pytest.raises(sdk_module.RateLimitError) as send_error:
        sink.send_detections("demo-app", 0, [])
    assert send_error.value.operation == "rc_ext_record_send_detections"
    assert sink.stats()["send_error"] == 1
    sink.close()

    sink, fake = _sink(
        sdk_module,
        monkeypatch,
        reset_rc=-int(sdk_module.ErrorCode.EAUTH),
    )
    with pytest.raises(sdk_module.AuthenticationError) as reset_error:
        sink.reset("demo-app")
    assert reset_error.value.operation == "rc_ext_record_reset"
    sink.close()


def test_record_sink_serializes_concurrent_sources_and_closed_operations(
    sdk_module, monkeypatch
):
    sink, fake = _sink(sdk_module, monkeypatch)
    barrier = threading.Barrier(3)
    errors = []

    def send(app_id):
        try:
            barrier.wait()
            sink.send_detections(app_id, 1, [])
        except Exception as exc:  # pragma: no cover - assertion reports detail
            errors.append(exc)

    threads = [
        threading.Thread(target=send, args=("first-app",)),
        threading.Thread(target=send, args=("second-app",)),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=2)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert fake.max_active_calls == 1
    assert {call[2] for call in fake.calls} == {"first-app", "second-app"}

    sink.close()
    with pytest.raises(sdk_module.HandleClosedError):
        sink.send_detections("first-app", 0, [])
    with pytest.raises(sdk_module.HandleClosedError):
        sink.reset("first-app")


def test_record_sink_close_serializes_with_bounded_native_send(
    sdk_module, monkeypatch
):
    sink, fake = _sink(sdk_module, monkeypatch, call_delay=0.05)
    errors = []

    def send():
        try:
            sink.send_detections("first-app", 1, [])
        except Exception as exc:  # pragma: no cover - assertion reports detail
            errors.append(exc)

    sender = threading.Thread(target=send)
    sender.start()
    assert fake.call_entered.wait(timeout=1)

    closer = threading.Thread(target=sink.close)
    closer.start()
    sender.join(timeout=1)
    closer.join(timeout=1)

    assert not errors
    assert not sender.is_alive()
    assert not closer.is_alive()
    assert fake.closed == 1
    assert not fake.closed_while_active
    with pytest.raises(sdk_module.HandleClosedError):
        sink.send_detections("first-app", 2, [])

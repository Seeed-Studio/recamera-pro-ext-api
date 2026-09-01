"""Strict-vs-legacy result fan-out behavior."""

from __future__ import annotations

import pytest

from kit.adapters.result_sink import MultiSink, ResultSink
from kit.errors import AdapterError


class RecordingSink(ResultSink):
    def __init__(self, name: str, events: list, *, fail_emit=False,
                 fail_size=False, fail_close=False):
        self.name = name
        self.events = events
        self.fail_emit = fail_emit
        self.fail_size = fail_size
        self.fail_close = fail_close

    def emit(self, payload: dict, pts: float) -> None:
        self.events.append((self.name, "emit", payload, pts))
        if self.fail_emit:
            raise RuntimeError(f"{self.name} emit failed")

    def set_frame_size(self, w: int, h: int) -> None:
        self.events.append((self.name, "size", w, h))
        if self.fail_size:
            raise RuntimeError(f"{self.name} size failed")

    def close(self) -> None:
        self.events.append((self.name, "close"))
        if self.fail_close:
            raise RuntimeError(f"{self.name} close failed")


def test_multisink_legacy_emit_stays_best_effort():
    events = []
    sink = MultiSink([
        RecordingSink("bad", events, fail_emit=True),
        RecordingSink("good", events),
    ])

    sink.emit({"results": []}, 1.0)

    assert [item[0] for item in events] == ["bad", "good"]


def test_multisink_checked_emit_attempts_all_then_reports_children():
    events = []
    sink = MultiSink([
        RecordingSink("bad", events, fail_emit=True),
        RecordingSink("good", events),
    ])

    with pytest.raises(AdapterError) as caught:
        sink.emit_checked({"results": []}, 1.0)

    assert [item[0] for item in events] == ["bad", "good"]
    assert caught.value.operation == "result.multi.emit"
    assert caught.value.details["failed_children"][0]["sink"] == "RecordingSink"
    assert isinstance(caught.value.__cause__, RuntimeError)


def test_multisink_checked_frame_size_attempts_all_then_reports():
    events = []
    sink = MultiSink([
        RecordingSink("bad", events, fail_size=True),
        RecordingSink("good", events),
    ])

    with pytest.raises(AdapterError) as caught:
        sink.set_frame_size_checked(640, 480)

    assert [item[0] for item in events] == ["bad", "good"]
    assert caught.value.operation == "result.multi.set_frame_size"


def test_multisink_close_attempts_all_then_reports_cleanup_failure():
    events = []
    sink = MultiSink([
        RecordingSink("bad", events, fail_close=True),
        RecordingSink("good", events),
    ])

    with pytest.raises(AdapterError) as caught:
        sink.close()

    assert events == [("bad", "close"), ("good", "close")]
    assert caught.value.operation == "result.multi.close"

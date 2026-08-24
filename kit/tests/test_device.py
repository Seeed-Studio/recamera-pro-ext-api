from __future__ import annotations

import threading

import pytest

from kit.capabilities import Capabilities
from kit.device import Device
from kit.errors import AdapterError, CapabilityError


class Resource:
    def __init__(self, name, events, fail=False):
        self.name = name
        self.events = events
        self.fail = fail

    def close(self):
        self.events.append(self.name)
        if self.fail:
            raise RuntimeError("close failed")


def test_device_uses_injected_services_and_closes_in_reverse_order():
    events = []
    device = Device(Capabilities(), factories={
        "frame_source": lambda **kw: Resource("frame", events),
        "result_publisher": lambda **kw: Resource("results", events, fail=True),
        "control": lambda **kw: Resource("control", events),
    })
    device.frame_source()
    device.result_publisher()
    device.control()
    report = device.close()
    assert events == ["control", "results", "frame"]
    assert report.closed == 2
    assert report.ok is False
    assert "results" not in report.errors[0]  # class + diagnosis, no secret state
    assert device.close().closed == 0


def test_closed_device_rejects_new_resources():
    device = Device(Capabilities())
    device.close()
    with pytest.raises(AdapterError) as caught:
        device.frame_source()
    assert caught.value.code == "device_closed"


def test_open_require_verified_fails_for_filesystem_only_probe(tmp_path, monkeypatch):
    endpoint = tmp_path / "frame.sock"
    endpoint.touch()
    monkeypatch.setenv("RECAMERA_FRAME_SOCK", str(endpoint))
    with pytest.raises(CapabilityError):
        Device.open(require_verified=("frame",))


def test_device_builds_typed_result_publisher_and_owns_legacy_sink():
    events = []
    sink = Resource("results", events)
    sink.emit = lambda payload, pts: events.append(("emit", payload, pts))
    sink.set_frame_size = lambda w, h: events.append(("size", w, h))
    device = Device(Capabilities(), factories={
        "result_publisher": lambda **_kwargs: sink,
    })

    publisher = device.result_batch_publisher()

    from kit.ai import ResultBatch
    report = publisher.publish(ResultBatch((), pts_us=10, frame_size=(4, 3)))
    assert report.locally_accepted is True
    assert events[:2] == [("size", 4, 3), ("emit", {
        "results": [],
        "events": [],
        "pts_us": 10,
        "source_id": "",
        "coordinate_space": "pixel",
        "frame": {"width": 4, "height": 3},
    }, 0.00001)]
    assert device.close().closed == 1
    assert events[-1] == "results"


def test_factory_control_flow_is_not_wrapped():
    def interrupted(**_kwargs):
        raise KeyboardInterrupt

    device = Device(Capabilities(), factories={"frame_source": interrupted})
    with pytest.raises(KeyboardInterrupt):
        device.frame_source()


def test_close_finishes_other_resources_then_re_raises_control_flow():
    events = []

    class Interrupted(Resource):
        def close(self):
            self.events.append(self.name)
            raise KeyboardInterrupt

    device = Device(Capabilities())
    device._owned = [Resource("first", events), Interrupted("second", events)]
    with pytest.raises(KeyboardInterrupt):
        device.close()
    assert events == ["second", "first"]
    assert device.close_report is not None
    assert device.close_report.ok is False


def test_context_manager_retains_close_report_for_inspection():
    events = []
    device = Device(Capabilities(), factories={
        "control": lambda **_kwargs: Resource("control", events, fail=True),
    })
    with device:
        device.control()
    assert device.close_report is not None
    assert device.close_report.closed == 0
    assert device.close_report.ok is False
    assert events == ["control"]


def test_close_drains_inflight_factory_and_cannot_leak_its_resource():
    events = []
    factory_started = threading.Event()
    allow_factory = threading.Event()

    def factory(**_kwargs):
        factory_started.set()
        assert allow_factory.wait(2.0)
        return Resource("frame", events)

    device = Device(Capabilities(), factories={"frame_source": factory})
    created: list[Resource] = []
    creator = threading.Thread(target=lambda: created.append(device.frame_source()))
    reports = []
    closer = threading.Thread(target=lambda: reports.append(device.close()))
    creator.start()
    assert factory_started.wait(1.0)
    closer.start()
    assert closer.is_alive()
    allow_factory.set()
    creator.join(2.0)
    closer.join(2.0)
    assert len(created) == 1
    assert events == ["frame"]
    assert reports[0].closed == 1


def test_factory_cannot_deadlock_by_closing_its_own_device():
    holder = {}

    def factory(**_kwargs):
        with pytest.raises(AdapterError) as caught:
            holder["device"].close()
        assert caught.value.code == "reentrant_close"
        return Resource("frame", [])

    device = Device(Capabilities(), factories={"frame_source": factory})
    holder["device"] = device
    device.frame_source()
    assert device.close().closed == 1

import importlib
import threading
import time


def _module():
    import appmgr.recording as recording
    return importlib.reload(recording)


def _capability(*signals):
    return {
        "valid": True,
        "record_trigger": {"version": 1, "signals": list(signals)},
    }


def _manifest_with_trigger(*signals):
    fields = [
        {"name": "kind", "from": "results[].kind", "type": "string"},
        {"name": "score", "from": "results[].score", "type": "float"},
        {"name": "box", "from": "results[].box", "type": "bbox<float>[4]",
         "coord": "pixel_xyxy"},
        {"name": "fall_kind", "from": "events[kind=fall].kind",
         "type": "string", "event_kind": "fall"},
    ]
    return {
        "manifest_version": 2,
        "id": "demo-app",
        "output": {"contract_version": 2, "fields": fields},
        "record_trigger": {"version": 1, "signals": list(signals)},
    }


def _identity(generation=1):
    return {
        "kind": "app", "id": "demo-app", "app_id": "demo-app",
        "instance": "demo-instance", "generation": generation,
    }


def _envelope(message_type, *, generation=1, results=None, events=None,
              message_id="message-1"):
    return {
        "schema": "recamera.ai.result", "schema_version": 2,
        "type": message_type, "id": message_id,
        "source": _identity(generation),
        "time": {"pts_us": 1234},
        "stream": {"width": 200, "height": 100,
                   "coordinate_space": "pixel_xyxy"},
        "results": list(results or []), "events": list(events or []),
    }


class _Sink:
    def __init__(self):
        self.lock = threading.Lock()
        self.calls = []
        self.closed = False

    def reset(self, app_id):
        with self.lock:
            self.calls.append(("reset", app_id))

    def send_detections(self, app_id, pts_us, boxes):
        with self.lock:
            self.calls.append(("detections", app_id, pts_us, list(boxes)))

    def send_classifications(self, app_id, pts_us, classes):
        with self.lock:
            self.calls.append(("classifications", app_id, pts_us,
                               list(classes)))

    def send_events(self, app_id, pts_us, events):
        with self.lock:
            self.calls.append(("events", app_id, pts_us, list(events)))

    def close(self):
        self.closed = True


def _wait_for(sink, predicate):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with sink.lock:
            calls = list(sink.calls)
        if predicate(calls):
            return calls
        time.sleep(0.01)
    raise AssertionError("timed out waiting for bridge calls: %r" % calls)


def test_bridge_projects_only_manifest_authorized_detection_classes():
    recording = _module()
    sink = _Sink()
    bridge = recording.RecordingTriggerBridge(sink_factory=lambda: sink).start()
    signal = {"id": "people", "type": "detection", "classes": ["person"],
              "supports_roi": True}
    try:
        bridge.invalidate_source(
            "demo-app", identity=_identity(), capability=_capability(signal))
        bridge.observe(_envelope("frame", results=[
            {"box": [20, 10, 100, 60], "score": .9,
             "cls_name": "person", "cls": 0,
             "spaces": {"box": "pixel_xyxy"}},
            {"box": [0, 0, 20, 20], "score": .8,
             "cls_name": "car", "cls": 2,
             "spaces": {"box": "pixel_xyxy"}},
        ]))
        calls = _wait_for(
            sink, lambda values: any(value[0] == "detections" for value in values))
        sent = next(value for value in calls if value[0] == "detections")
        assert sent[:3] == ("detections", "demo-app", 1234)
        assert sent[3] == [(.1, .1, .5, .6, .9, "person", 0)]
        assert bridge.status()["frames"] == 1
    finally:
        bridge.close()
    assert sink.closed


def test_hub_preserves_empty_detection_frames_alongside_business_events():
    from appmgr.result_hub import normalize_app_payload

    recording = _module()
    sink = _Sink()
    bridge = recording.RecordingTriggerBridge(sink_factory=lambda: sink).start()
    signal = {"id": "people", "type": "detection", "classes": ["person"],
              "supports_roi": True}
    identity = {"app_id": "demo-app", "instance_id": "demo-instance",
                "generation": 1}
    person = {"box": [20, 10, 100, 60], "score": .9, "cls_name": "person"}
    try:
        token = bridge.invalidate_source(
            "demo-app", identity=_identity(), capability=_capability(signal))
        assert bridge.wait_invalidation(token)
        for seq, results in enumerate(([person], [], [person]), 1):
            raw = {"type": "results", "seq": seq,
                   "frame": {"width": 200, "height": 100},
                   "results": results,
                   "events": [{"kind": "metrics", "occupancy": len(results)}]}
            envelopes = normalize_app_payload(
                raw, identity, trusted_geometry={"results": {"box": "pixel_xyxy"}})
            assert [item["type"] for item in envelopes] == ["frame", "event"]
            for envelope in envelopes:
                bridge.observe(envelope)
            calls = _wait_for(sink, lambda values: sum(
                value[0] == "detections" for value in values) == seq)
        assert [len(value[3]) for value in calls
                if value[0] == "detections"] == [1, 0, 1]

        event_only = normalize_app_payload(
            {"type": "results", "events": [{"kind": "fall"}]}, identity)
        assert [item["type"] for item in event_only] == ["event"]
    finally:
        assert bridge.close()


def test_event_signal_uses_one_shot_delivery_and_generation_fence_resets_state():
    recording = _module()
    sink = _Sink()
    bridge = recording.RecordingTriggerBridge(sink_factory=lambda: sink).start()
    signal = {"id": "fall", "type": "event", "event_kind": "fall",
              "supports_roi": False}
    try:
        bridge.invalidate_source(
            "demo-app", identity=_identity(), capability=_capability(signal))
        bridge.observe(_envelope(
            "event", events=[{"kind": "fall", "event_id": "fall-1"}]))
        # Exact duplicate ids cannot increment Vigil debounce twice.
        bridge.observe(_envelope(
            "event", events=[{"kind": "fall", "event_id": "fall-1"}]))
        calls = _wait_for(
            sink, lambda values: any(
                value[0] == "events" for value in values))
        sent = next(value for value in calls if value[0] == "events")
        assert sent[3] == [(1.0, "fall", 0)]
        assert bridge.status()["duplicates"] == 1

        bridge.invalidate_source("demo-app", identity=_identity(2),
                                 capability=_capability(signal))
        bridge.observe(_envelope(
            "event", generation=1, message_id="stale",
            events=[{"kind": "fall"}]))
        _wait_for(sink, lambda values: sum(
            value[0] == "reset" for value in values) >= 2)
        with sink.lock:
            assert sum(value[0] == "events"
                       for value in sink.calls) == 1
        assert bridge.status()["active_sources"] == ["demo-app"]
    finally:
        bridge.close()


def test_detection_wire_fields_cannot_escape_the_authorized_label():
    recording = _module()
    sink = _Sink()
    bridge = recording.RecordingTriggerBridge(sink_factory=lambda: sink).start()
    signal = {"id": "people", "type": "detection", "classes": ["person"],
              "supports_roi": True}
    try:
        bridge.invalidate_source(
            "demo-app", identity=_identity(), capability=_capability(signal))
        bridge.observe(_envelope("frame", results=[{
            "box": [20, 10, 100, 60],
            "confidence": .91,
            "kind": "person",
            # OSD legitimately treats text as a presentation label, but it may
            # never replace the class value authorized for recording.
            "text": "UNDECLARED-LABEL",
            "class_id": 7,
            "spaces": {"box": "pixel_xyxy"},
        }]))
        calls = _wait_for(
            sink, lambda values: any(value[0] == "detections" for value in values))
        sent = next(value for value in calls if value[0] == "detections")
        assert sent[3] == [(.1, .1, .5, .6, .91, "person", 7)]
    finally:
        assert bridge.close()


def test_source_view_hides_apps_without_signed_recording_declaration():
    recording = _module()
    base = {
        "id": "demo-app", "name": "Demo", "version": "1.0.0",
        "running": False, "status": "stopped",
        "manifest": {"manifest_version": 2, "id": "demo-app"},
    }
    assert recording.source_view(base) is None
    signal = {"id": "fall", "type": "event", "event_kind": "fall",
              "supports_roi": False}
    base["manifest"] = _manifest_with_trigger(signal)
    assert recording.source_view(base) == {
        "id": "demo-app", "kind": "app", "name": "Demo", "name_zh": None,
        "version": "1.0.0", "installed": True, "running": False,
        "status": "stopped", "supports_roi": False,
        "signals": [signal],
    }


def test_source_view_revalidates_the_complete_installed_manifest():
    recording = _module()
    signal = {"id": "person", "type": "detection", "classes": ["person"],
              "supports_roi": True}
    app = {"id": "demo-app", "manifest": _manifest_with_trigger(signal)}
    assert recording.source_view(app) is not None

    # A disk-edited declaration must not be advertised merely because the
    # recording subsection happens to look structurally plausible.
    app["manifest"]["output"]["fields"] = []
    assert recording.source_view(app) is None

    app["manifest"] = _manifest_with_trigger(signal)
    app["manifest"]["id"] = "other-app"
    assert recording.source_view(app) is None


def test_invalidation_is_nonblocking_and_reset_ack_fences_inflight_send():
    recording = _module()
    entered = threading.Event()
    release = threading.Event()
    invalidated = threading.Event()

    class BlockingSink(_Sink):
        def send_detections(self, app_id, pts_us, boxes):
            entered.set()
            assert release.wait(2)
            super().send_detections(app_id, pts_us, boxes)

    sink = BlockingSink()
    bridge = recording.RecordingTriggerBridge(sink_factory=lambda: sink).start()
    signal = {"id": "people", "type": "detection", "classes": ["person"],
              "supports_roi": True}
    try:
        bridge.invalidate_source(
            "demo-app", identity=_identity(), capability=_capability(signal))
        _wait_for(sink, lambda values: any(value[0] == "reset" for value in values))
        bridge.observe(_envelope("frame", results=[
            {"box": [20, 10, 100, 60], "score": .9,
             "cls_name": "person", "spaces": {"box": "pixel_xyxy"}},
        ]))
        assert entered.wait(2)

        token_box = []

        def invalidate():
            token_box.append(bridge.invalidate_source(
                "demo-app", identity=_identity(2), capability=_capability(signal))
            )
            invalidated.set()

        thread = threading.Thread(target=invalidate)
        thread.start()
        # The Result Hub invalidator contract is memory-only and cannot block
        # its publish fence on native I/O.
        assert invalidated.wait(.05)
        assert not token_box[0].event.is_set()
        release.set()
        assert bridge.wait_invalidation(token_box[0], timeout=2)
        thread.join(timeout=2)
        with sink.lock:
            detections = [value for value in sink.calls
                          if value[0] == "detections"]
            call_kinds = [value[0] for value in sink.calls]
        assert len(detections) == 1
        assert call_kinds[-1] == "reset"
    finally:
        release.set()
        assert bridge.close()


def test_invalidation_during_sink_open_drops_the_prepared_old_action():
    recording = _module()
    opening = threading.Event()
    release = threading.Event()
    sink = _Sink()

    def factory():
        opening.set()
        assert release.wait(2)
        return sink

    bridge = recording.RecordingTriggerBridge(sink_factory=factory).start()
    signal = {"id": "people", "type": "detection", "classes": ["person"],
              "supports_roi": True}
    first_token = bridge.invalidate_source(
        "demo-app", identity=_identity(), capability=_capability(signal))
    assert opening.wait(2)
    # Replace the initial reset/action epoch while native open is outside the
    # dispatch gate.  Its final current-epoch check must reject that stale work.
    second_token = bridge.invalidate_source(
        "demo-app", identity=_identity(2), capability=_capability(signal))
    release.set()
    try:
        _wait_for(sink, lambda values: any(value[0] == "reset" for value in values))
        assert bridge.wait_invalidation(second_token, timeout=2)
        assert bridge.wait_invalidation(first_token, timeout=0) is False
        with sink.lock:
            assert not any(value[0] == "detections" for value in sink.calls)
        assert bridge.status()["dropped"] >= 1
    finally:
        assert bridge.close()


def test_edge_survives_frame_pressure_and_is_dispatched_first():
    recording = _module()
    entered = threading.Event()
    release = threading.Event()

    class BlockingSink(_Sink):
        def send_detections(self, app_id, pts_us, boxes):
            if not entered.is_set():
                entered.set()
                assert release.wait(2)
            super().send_detections(app_id, pts_us, boxes)

    sink = BlockingSink()
    bridge = recording.RecordingTriggerBridge(
        sink_factory=lambda: sink, max_queue=2).start()
    detection = {"id": "people", "type": "detection", "classes": ["person"],
                 "supports_roi": True}
    event = {"id": "fall", "type": "event", "event_kind": "fall",
             "supports_roi": False}
    try:
        token = bridge.invalidate_source(
            "demo-app", identity=_identity(),
            capability=_capability(detection, event))
        assert bridge.wait_invalidation(token, timeout=2)
        box = {"box": [20, 10, 100, 60], "score": .9,
               "cls_name": "person", "spaces": {"box": "pixel_xyxy"}}
        bridge.observe(_envelope("frame", results=[box], message_id="frame-0"))
        assert entered.wait(2)
        bridge.observe(_envelope("frame", results=[box], message_id="frame-1"))
        bridge.observe(_envelope(
            "event", events=[{"kind": "fall", "event_id": "fall-1"}],
            message_id="edge-1"))
        for index in range(2, 12):
            bridge.observe(_envelope(
                "frame", results=[box], message_id=f"frame-{index}"))
        release.set()
        calls = _wait_for(
            sink, lambda values: sum(value[0] == "detections" for value in values)
            >= 2 and any(value[0] == "events" for value in values))
        after_first_frame = [value[0] for value in calls
                             if value[0] in ("detections", "events")][1:]
        assert after_first_frame[0] == "events"
        status = bridge.status()
        assert status["event_dropped"] == 0
        assert status["frame_coalesced"] >= 1
    finally:
        release.set()
        assert bridge.close()


def test_budget_wait_preserves_empty_boundary_and_prioritizes_events(monkeypatch):
    recording = _module()
    monkeypatch.setattr(recording, "DATA_INTERVAL_SECONDS", .15)
    sink = _Sink()
    bridge = recording.RecordingTriggerBridge(sink_factory=lambda: sink).start()
    detection = {"id": "people", "type": "detection", "classes": ["person"],
                 "supports_roi": True}
    event = {"id": "fall", "type": "event", "event_kind": "fall",
             "supports_roi": False}
    box = {"box": [20, 10, 100, 60], "score": .9, "cls_name": "person",
           "spaces": {"box": "pixel_xyxy"}}
    try:
        token = bridge.invalidate_source(
            "demo-app", identity=_identity(), capability=_capability(detection, event))
        assert bridge.wait_invalidation(token)
        bridge.observe(_envelope("frame", results=[box], message_id="positive-1"))
        _wait_for(sink, lambda values: any(value[0] == "detections" for value in values))
        bridge.observe(_envelope("frame", results=[], message_id="empty"))
        bridge.observe(_envelope("frame", results=[box], message_id="positive-2"))
        bridge.observe(_envelope("event", events=[{"kind": "fall"}], message_id="edge"))
        calls = _wait_for(sink, lambda values: sum(
            value[0] == "detections" for value in values) == 3)
        data = [value for value in calls if value[0] in ("detections", "events")]
        assert [value[0] for value in data] == ["detections", "events", "detections", "detections"]
        assert [len(value[3]) for value in data if value[0] == "detections"] == [1, 0, 1]
    finally:
        assert bridge.close()


def test_lifecycle_reset_and_close_interrupt_data_budget_wait(monkeypatch):
    recording = _module()
    monkeypatch.setattr(recording, "DATA_INTERVAL_SECONDS", 10.0)
    sink = _Sink()
    bridge = recording.RecordingTriggerBridge(sink_factory=lambda: sink).start()
    signal = {"id": "people", "type": "detection", "classes": ["person"],
              "supports_roi": True}
    try:
        token = bridge.invalidate_source(
            "demo-app", identity=_identity(), capability=_capability(signal))
        assert bridge.wait_invalidation(token)
        bridge.observe(_envelope("frame", message_id="first"))
        _wait_for(sink, lambda values: any(value[0] == "detections" for value in values))
        bridge.observe(_envelope("frame", message_id="must-not-send"))
        started = time.monotonic()
        token = bridge.invalidate_source("demo-app")
        assert bridge.wait_invalidation(token, timeout=.5)
        assert bridge.close()
        assert time.monotonic() - started < .5
        assert sum(value[0] == "detections" for value in sink.calls) == 1
    finally:
        assert bridge.close()


def test_cross_source_lifecycle_reset_precedes_queued_events():
    recording = _module()
    entered = threading.Event()
    release = threading.Event()

    class BlockingSink(_Sink):
        def send_events(self, app_id, pts_us, events):
            if app_id == "demo-app" and not entered.is_set():
                entered.set()
                assert release.wait(2)
            super().send_events(app_id, pts_us, events)

    sink = BlockingSink()
    bridge = recording.RecordingTriggerBridge(
        sink_factory=lambda: sink, max_queue=4).start()
    signal = {"id": "fall", "type": "event", "event_kind": "fall",
              "supports_roi": False}
    other_identity = {
        "kind": "app", "id": "other-app", "app_id": "other-app",
        "instance": "other-instance", "generation": 1,
    }
    try:
        first = bridge.invalidate_source(
            "demo-app", identity=_identity(), capability=_capability(signal))
        other = bridge.invalidate_source(
            "other-app", identity=other_identity, capability=_capability(signal))
        assert bridge.wait_invalidation(first, timeout=2)
        assert bridge.wait_invalidation(other, timeout=2)

        bridge.observe(_envelope(
            "event", events=[{"kind": "fall"}], message_id="edge-1"))
        assert entered.wait(2)
        second = _envelope(
            "event", events=[{"kind": "fall"}], message_id="edge-2")
        second["time"]["pts_us"] = 2000
        third = _envelope(
            "event", events=[{"kind": "fall"}], message_id="edge-3")
        third["time"]["pts_us"] = 3000
        bridge.observe(second)
        bridge.observe(third)
        reset = bridge.invalidate_source("other-app")
        release.set()
        assert bridge.wait_invalidation(reset, timeout=2)
        calls = _wait_for(sink, lambda values: sum(
            value[0] == "events" for value in values) >= 3)
        tail = [value for value in calls if value[0] in ("events", "reset")][-4:]
        assert tail == [
            ("events", "demo-app", 1234, [(1.0, "fall", 0)]),
            ("reset", "other-app"),
            ("events", "demo-app", 2000, [(1.0, "fall", 0)]),
            ("events", "demo-app", 3000, [(1.0, "fall", 0)]),
        ]
    finally:
        release.set()
        assert bridge.close()


def test_recording_observer_filters_unrelated_records_before_its_queue():
    recording = _module()
    sink = _Sink()
    bridge = recording.RecordingTriggerBridge(sink_factory=lambda: sink).start()
    signal = {"id": "fall", "type": "event", "event_kind": "fall",
              "supports_roi": False}
    token = bridge.invalidate_source(
        "demo-app", identity=_identity(), capability=_capability(signal))
    assert bridge.wait_invalidation(token, timeout=2)

    assert bridge.observer_accepts(_envelope(
        "event", events=[{"kind": "fall"}], message_id="authorized")) is True
    assert bridge.observer_accepts(_envelope(
        "event", events=[{"kind": "metrics"}], message_id="unrelated")) is False
    assert bridge.observer_accepts(_envelope("frame", results=[])) is False
    unrelated = _envelope(
        "event", events=[{"kind": "metrics"}], message_id="unrelated")
    bridge.observe(unrelated)
    with bridge._condition:
        assert not any(item[0] == "events" for item in bridge._queue)
    assert bridge.close()


def test_result_hub_capability_refresh_drives_recording_bridge(tmp_path):
    recording = _module()
    from appmgr.result_hub import ResultHub

    class NoopFormatter:
        def format(self, _raw):
            return []

    sink = _Sink()
    bridge = recording.RecordingTriggerBridge(sink_factory=lambda: sink).start()
    hub = ResultHub(ws_port=0, system_uds_path=str(tmp_path / "system.sock"),
                    formatter=NoopFormatter())
    registration = hub.add_observer(bridge.observe)
    signal = {"id": "fall", "type": "event", "event_kind": "fall",
              "supports_roi": False}
    identity = {
        "app_id": "demo-app", "instance_id": "demo-instance",
        "generation": 1,
    }
    try:
        assert hub.refresh_app_manifest(
            identity, _manifest_with_trigger(signal))
        assert hub.publish_app({
            "type": "results", "seq": 1, "pts": 1.25,
            "events": [{"kind": "fall", "event_id": "fall-1"}],
        }, identity)
        calls = _wait_for(
            sink, lambda values: any(value[0] == "events" for value in values))
        assert next(value for value in calls if value[0] == "events")[3] == [
            (1.0, "fall", 0)]

        assert hub.invalidate_app_manifest("demo-app", identity=identity)
        with sink.lock:
            assert sink.calls[-1] == ("reset", "demo-app")
    finally:
        hub.remove_observer(registration)
        assert bridge.close()

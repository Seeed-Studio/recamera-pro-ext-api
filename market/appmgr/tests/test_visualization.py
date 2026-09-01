import importlib
import json
import os
import stat
import sys
import threading
import time
import types


def _module(tmp_path, monkeypatch):
    monkeypatch.setenv("APPMGR_DIR", str(tmp_path / "appmgr"))
    monkeypatch.setenv(
        "APPMGR_VISUALIZATION_CONFIG", str(tmp_path / "visualization.json"))
    import appmgr.paths as paths
    import appmgr.visualization as visualization
    importlib.reload(paths)
    return importlib.reload(visualization)


def test_policy_defaults_validation_and_atomic_mode(tmp_path, monkeypatch):
    v = _module(tmp_path, monkeypatch)
    assert v.load() == {"osd": {"enabled": False, "sources": []}}
    saved = v.save({"osd": {"enabled": True,
                             "sources": ["yolo-detector", "yolo-detector"]}})
    assert saved == {"osd": {"enabled": True, "sources": ["yolo-detector"]}}
    assert v.load() == saved
    mode = stat.S_IMODE(os.stat(os.environ["APPMGR_VISUALIZATION_CONFIG"]).st_mode)
    assert mode == 0o600
    with open(os.environ["APPMGR_VISUALIZATION_CONFIG"], encoding="utf-8") as f:
        assert json.load(f) == saved


def test_policy_rejects_implicit_builtin_and_invalid_shape(tmp_path, monkeypatch):
    v = _module(tmp_path, monkeypatch)
    for value in (
        [],
        {"osd": True},
        {"osd": {"enabled": 1}},
        {"osd": {"enabled": True, "sources": []}},
        {"osd": {"sources": ["builtin"]}},
        {"osd": {"sources": ["../bad"]}},
        {"extra": {}},
    ):
        try:
            v.validate(value, current=v.defaults())
        except v.VisualizationError:
            pass
        else:
            raise AssertionError("invalid visualization policy accepted: %r" % value)


def test_old_empty_enabled_policy_is_disabled_across_read_bridge_and_api(
        tmp_path, monkeypatch):
    v = _module(tmp_path, monkeypatch)
    path = os.environ["APPMGR_VISUALIZATION_CONFIG"]
    with open(path, "w", encoding="utf-8") as stream:
        json.dump({"osd": {"enabled": True, "sources": []}}, stream)
    expected = {"osd": {"enabled": False, "sources": []}}
    assert v.load() == expected

    bridge = v.DetectionOsdBridge(sink_factory=_Sink)
    assert bridge.status()["enabled"] is False
    assert bridge.status()["sources"] == []

    public = v.public_view(bridge)
    assert public["osd"]["enabled"] is False
    assert public["osd"]["sources"] == []
    assert public["osd"]["status"]["enabled"] is False
    assert public["osd"]["status"]["sources"] == []


def test_effective_box_osd_capability_is_strict_and_non_mutating(
        tmp_path, monkeypatch):
    v = _module(tmp_path, monkeypatch)
    legacy = {
        "manifest_version": 2,
        "id": "legacy-detector",
        "render": {"schema_version": 1, "boxes": {"line_width": 2}},
        "output": {"contract_version": 2, "fields": [{
            "name": "box", "from": "results[].box", "coord": "pixel_xyxy",
        }]},
    }
    assert v.supports_detection_stream_osd(legacy) is True
    projected = v.effective_render(legacy)
    assert projected == {
        "schema_version": 1,
        "boxes": {"line_width": 2},
        "stream_osd": {"supported": ["boxes"], "default": False},
    }
    assert "stream_osd" not in legacy["render"]

    explicit = json.loads(json.dumps(legacy))
    explicit["render"]["stream_osd"] = {
        "supported": ["boxes"], "default": False,
    }
    assert v.supports_detection_stream_osd(explicit) is True
    assert v.effective_render(explicit) == explicit["render"]

    explicit_osd_only = json.loads(json.dumps(explicit))
    explicit_osd_only["render"].pop("boxes")
    assert v.supports_detection_stream_osd(explicit_osd_only) is True
    assert v.effective_render(explicit_osd_only) == \
        explicit_osd_only["render"]

    same_space_alias = json.loads(json.dumps(explicit_osd_only))
    same_space_alias["output"]["fields"].append({
        "name": "box_copy", "from": "results[].box",
        "coord": "pixel_xyxy",
    })
    assert v.supports_detection_stream_osd(same_space_alias) is True

    conflicting_alias = json.loads(json.dumps(same_space_alias))
    conflicting_alias["output"]["fields"][1]["coord"] = "normalized_xyxy"
    assert v.supports_detection_stream_osd(conflicting_alias) is False
    assert "stream_osd" not in v.effective_render(conflicting_alias)

    derived_only = json.loads(json.dumps(explicit_osd_only))
    derived_only["output"]["fields"][0]["derived"] = True
    assert v.supports_detection_stream_osd(derived_only) is False
    assert "stream_osd" not in v.effective_render(derived_only)

    variants = []
    no_render_boxes = json.loads(json.dumps(legacy))
    no_render_boxes["render"] = {"schema_version": 1, "keypoints": {}}
    variants.append(no_render_boxes)
    loose_output = json.loads(json.dumps(legacy))
    loose_output["output"].pop("contract_version")
    variants.append(loose_output)
    loose_render = json.loads(json.dumps(legacy))
    loose_render["render"].pop("schema_version")
    variants.append(loose_render)
    derived = json.loads(json.dumps(legacy))
    derived["output"]["fields"][0]["derived"] = True
    variants.append(derived)
    unknown_space = json.loads(json.dumps(legacy))
    unknown_space["output"]["fields"][0]["coord"] = "unknown"
    variants.append(unknown_space)
    unhashable_space = json.loads(json.dumps(legacy))
    unhashable_space["output"]["fields"][0]["coord"] = ["pixel_xyxy"]
    variants.append(unhashable_space)
    duplicate = json.loads(json.dumps(legacy))
    duplicate["output"]["fields"].append(dict(
        duplicate["output"]["fields"][0]))
    variants.append(duplicate)
    explicit_deny = json.loads(json.dumps(legacy))
    explicit_deny["render"]["stream_osd"] = {
        "supported": [], "default": False,
    }
    variants.append(explicit_deny)
    for value in variants:
        assert v.supports_detection_stream_osd(value) is False
        assert "stream_osd" not in v.effective_render(value) or value is explicit_deny

    loose_positive_output = json.loads(json.dumps(explicit))
    loose_positive_output["output"].pop("contract_version")
    loose_positive_render = json.loads(json.dumps(explicit))
    loose_positive_render["render"].pop("schema_version")
    boolean_positive_render = json.loads(json.dumps(explicit))
    boolean_positive_render["render"]["schema_version"] = True
    for loose_positive in (
            loose_positive_output, loose_positive_render,
            boolean_positive_render):
        assert v.supports_detection_stream_osd(loose_positive) is False
        assert "stream_osd" not in v.effective_render(loose_positive)


class _Sink:
    def __init__(self):
        self.calls = []
        self.closed = False

    def send_detections(self, pts_us, boxes):
        self.calls.append((pts_us, list(boxes)))

    def close(self):
        self.closed = True


def test_default_factory_uses_dedicated_osd_sink(tmp_path, monkeypatch):
    v = _module(tmp_path, monkeypatch)
    sink = _Sink()
    fake_sdk = types.SimpleNamespace(OsdSink=lambda: sink)
    monkeypatch.setitem(sys.modules, "recamera_ext", fake_sdk)
    assert v._default_sink_factory() is sink


def _envelope(app, box, *, width=100, height=50, generation=1, pts_us=9):
    return {
        "schema": "recamera.ai.result", "schema_version": 2,
        "type": "frame",
        "source": {"kind": "app", "id": app, "app_id": app,
                   "instance": app + "-i", "generation": generation},
        "time": {"wall_ms": 1, "pts_us": pts_us},
        "stream": {"id": "sub", "width": width, "height": height,
                   "coordinate_space": "pixel_xyxy"},
        "render": {"schema_version": 1,
                   "stream_osd": {"supported": ["boxes"]}},
        "results": [{"box": box, "score": .75, "cls": 2,
                     "cls_name": "person"}],
        "events": [],
    }


def test_bridge_unions_selected_sources_and_normalises_coordinates(
        tmp_path, monkeypatch):
    v = _module(tmp_path, monkeypatch)
    sink = _Sink()
    bridge = v.DetectionOsdBridge(
        sink_factory=lambda: sink,
        config_loader=lambda: {"osd": {"enabled": True,
                                        "sources": ["a", "b"]}},
        ttl_sec=2.0).start()
    try:
        bridge.observe(_envelope("a", [10, 5, 50, 25], pts_us=10))
        bridge.observe(_envelope("b", [20, 10, 80, 40], pts_us=20))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if sink.calls and len(sink.calls[-1][1]) == 2:
                break
            time.sleep(.01)
        assert len(sink.calls[-1][1]) == 2
        assert sink.calls[-1][0] == 20
        assert sink.calls[-1][1][0][:4] == (.1, .1, .5, .5)
        assert sink.calls[-1][1][1][:4] == (.2, .2, .8, .8)
    finally:
        bridge.close()


def test_bridge_rejects_unknown_coordinate_space_and_unselected_sources(
        tmp_path, monkeypatch):
    v = _module(tmp_path, monkeypatch)
    sink = _Sink()
    bridge = v.DetectionOsdBridge(
        sink_factory=lambda: sink,
        config_loader=lambda: {"osd": {"enabled": True, "sources": ["a"]}},
        ttl_sec=2.0).start()
    try:
        unknown = _envelope("a", [0, 0, 1, 1])
        unknown["stream"].pop("coordinate_space")
        bridge.observe(unknown)
        bridge.observe(_envelope("b", [1, 1, 2, 2]))
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and not sink.calls:
            time.sleep(.01)
        assert sink.calls and sink.calls[-1][1] == []
    finally:
        bridge.close()


def test_event_record_does_not_erase_frame_snapshot(tmp_path, monkeypatch):
    v = _module(tmp_path, monkeypatch)
    sink = _Sink()
    bridge = v.DetectionOsdBridge(
        sink_factory=lambda: sink,
        config_loader=lambda: {"osd": {"enabled": True, "sources": ["a"]}},
        ttl_sec=2.0).start()
    try:
        frame = _envelope("a", [1, 1, 20, 20])
        bridge.observe(frame)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and not any(call[1] for call in sink.calls):
            time.sleep(.01)
        assert sink.calls[-1][1]
        event = dict(frame)
        event["type"] = "event"
        event["results"] = []
        event["events"] = [{"kind": "fall", "event_id": "fall:1"}]
        bridge.observe(event)
        time.sleep(.1)
        assert sink.calls[-1][1]
    finally:
        bridge.close()


def test_new_generation_without_manifest_osd_capability_clears_old_boxes(
        tmp_path, monkeypatch):
    v = _module(tmp_path, monkeypatch)
    sink = _Sink()
    bridge = v.DetectionOsdBridge(
        sink_factory=lambda: sink,
        config_loader=lambda: {"osd": {"enabled": True, "sources": ["a"]}},
        ttl_sec=2.0).start()
    try:
        supported = _envelope("a", [1, 1, 20, 20], generation=1)
        bridge.observe(supported)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and not any(call[1] for call in sink.calls):
            time.sleep(.01)
        assert sink.calls[-1][1]

        unsupported = _envelope("a", [2, 2, 30, 30], generation=2)
        unsupported["render"] = {"schema_version": 1}
        bridge.observe(unsupported)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and sink.calls[-1][1]:
            time.sleep(.01)
        assert sink.calls[-1][1] == []
        assert bridge.status()["active_sources"] == []
    finally:
        bridge.close()


def test_box_validation_rejects_boolean_coordinates_and_sanitizes_metadata(
        tmp_path, monkeypatch):
    v = _module(tmp_path, monkeypatch)
    stream = {"width": 100, "height": 50,
              "coordinate_space": "pixel_xyxy"}
    assert v._normalise_box({"box": [False, 0, 10, 10]}, stream) is None
    value = v._normalise_box({
        "box": [0, 0, 100, 50], "score": 2,
        "cls": 1 << 50, "cls_name": "per\x00son\n",
    }, stream)
    assert value == (0.0, 0.0, 1.0, 1.0, 1.0, "person", (1 << 31) - 1)


def test_app_item_cannot_spoof_authoritative_coordinate_space(tmp_path, monkeypatch):
    v = _module(tmp_path, monkeypatch)
    stream = {"width": 100, "height": 50,
              "coordinate_space": "pixel_xyxy"}
    item = {"box": [10, 5, 50, 25], "space": "normalized_xyxy"}
    assert v._normalise_box(item, stream)[:4] == (.1, .1, .5, .5)
    item["spaces"] = {"box": "normalized_xyxy"}
    item["box"] = [.1, .2, .3, .4]
    assert v._normalise_box(item, stream)[:4] == (.1, .2, .3, .4)


def test_disable_discards_cached_boxes_before_reenable(tmp_path, monkeypatch):
    v = _module(tmp_path, monkeypatch)
    bridge = v.DetectionOsdBridge(
        sink_factory=_Sink,
        config_loader=lambda: {"osd": {"enabled": True, "sources": ["a"]}},
        ttl_sec=2.0)
    bridge.observe(_envelope("a", [1, 1, 20, 20]))
    assert bridge._snapshot()[1]
    bridge.reload({"osd": {"enabled": False, "sources": ["a"]}})
    bridge.reload({"osd": {"enabled": True, "sources": ["a"]}})
    assert bridge._snapshot()[1] == []


def test_policy_epoch_rejects_inflight_frame_across_disable_enable_aba(
        tmp_path, monkeypatch):
    v = _module(tmp_path, monkeypatch)
    entered = threading.Event()
    release = threading.Event()
    original = v._normalise_box

    def blocking_normalise(item, stream):
        entered.set()
        assert release.wait(2)
        return original(item, stream)

    monkeypatch.setattr(v, "_normalise_box", blocking_normalise)
    bridge = v.DetectionOsdBridge(
        sink_factory=_Sink,
        config_loader=lambda: {"osd": {"enabled": True, "sources": ["a"]}},
        ttl_sec=2.0)
    observer = threading.Thread(
        target=bridge.observe, args=(_envelope("a", [1, 1, 20, 20]),))
    observer.start()
    assert entered.wait(1)
    bridge.reload({"osd": {"enabled": False, "sources": ["a"]}})
    bridge.reload({"osd": {"enabled": True, "sources": ["a"]}})
    release.set()
    observer.join(1)
    assert not observer.is_alive()
    assert bridge._snapshot()[1] == []
    assert bridge.status()["active_sources"] == []


def test_source_invalidation_rejects_inflight_old_generation(
        tmp_path, monkeypatch):
    v = _module(tmp_path, monkeypatch)
    entered = threading.Event()
    release = threading.Event()
    original = v._normalise_box

    def blocking_normalise(item, stream):
        entered.set()
        assert release.wait(2)
        return original(item, stream)

    monkeypatch.setattr(v, "_normalise_box", blocking_normalise)
    bridge = v.DetectionOsdBridge(
        sink_factory=_Sink,
        config_loader=lambda: {"osd": {"enabled": True, "sources": ["a"]}},
        ttl_sec=2.0)
    observer = threading.Thread(
        target=bridge.observe,
        args=(_envelope("a", [1, 1, 20, 20], generation=1),))
    observer.start()
    assert entered.wait(1)
    bridge.invalidate_source("a", instance="a-new", generation=2)
    release.set()
    observer.join(1)
    assert not observer.is_alive()
    assert bridge._snapshot()[1] == []
    assert bridge.status()["active_sources"] == []


def test_bridge_ttl_clears_stale_boxes(tmp_path, monkeypatch):
    v = _module(tmp_path, monkeypatch)
    sink = _Sink()
    now = [10.0]
    bridge = v.DetectionOsdBridge(
        sink_factory=lambda: sink,
        config_loader=lambda: {"osd": {"enabled": True, "sources": ["a"]}},
        ttl_sec=.1, clock=lambda: now[0]).start()
    try:
        bridge.observe(_envelope("a", [1, 1, 20, 20]))
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and not any(call[1] for call in sink.calls):
            time.sleep(.01)
        assert any(call[1] for call in sink.calls)
        now[0] += 1.0
        bridge._wake.set()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and sink.calls[-1][1]:
            time.sleep(.01)
        assert sink.calls[-1][1] == []
    finally:
        bridge.close()


def test_observe_is_non_blocking_while_native_send_runs(tmp_path, monkeypatch):
    v = _module(tmp_path, monkeypatch)
    entered = threading.Event()
    release = threading.Event()

    class BlockingSink(_Sink):
        def send_detections(self, pts_us, boxes):
            entered.set()
            release.wait(2)
            super().send_detections(pts_us, boxes)

    sink = BlockingSink()
    bridge = v.DetectionOsdBridge(
        sink_factory=lambda: sink,
        config_loader=lambda: {"osd": {"enabled": True, "sources": ["a"]}},
        ttl_sec=2.0).start()
    try:
        bridge.observe(_envelope("a", [1, 1, 20, 20]))
        assert entered.wait(1)
        started = time.monotonic()
        bridge.observe(_envelope("a", [2, 2, 30, 30], generation=2))
        assert time.monotonic() - started < .1
    finally:
        release.set()
        bridge.close()


def test_close_never_closes_native_handle_under_inflight_send(tmp_path, monkeypatch):
    v = _module(tmp_path, monkeypatch)
    entered = threading.Event()
    release = threading.Event()

    class BlockingSink(_Sink):
        def send_detections(self, pts_us, boxes):
            entered.set()
            release.wait(3)
            assert not self.closed
            super().send_detections(pts_us, boxes)

    sink = BlockingSink()
    bridge = v.DetectionOsdBridge(
        sink_factory=lambda: sink,
        config_loader=lambda: {"osd": {"enabled": True, "sources": ["a"]}},
        ttl_sec=2.0).start()
    bridge.observe(_envelope("a", [1, 1, 20, 20]))
    assert entered.wait(1)
    closer = threading.Thread(target=bridge.close)
    closer.start()
    time.sleep(.05)
    assert not sink.closed
    release.set()
    closer.join(1)
    assert not closer.is_alive()
    assert sink.closed

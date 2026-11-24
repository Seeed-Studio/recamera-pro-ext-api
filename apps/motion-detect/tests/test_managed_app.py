"""Pin the HA wire contract and the managed application's resource lifecycle."""
import json
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
import numpy as np

from ha_mqtt import HaMqttBridge
from kit.app import _maybe_open_mqtt_sink
from kit.adapters.mqtt_sink import MqttSink


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "manifest.json").read_text())
SPEC = importlib.util.spec_from_file_location("motion_detect_managed_app", ROOT / "app.py")
APP_MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(APP_MODULE)
MotionDetectApp = APP_MODULE.MotionDetectApp


def test_manifest_uses_the_shared_schema():
    schema = json.loads((ROOT / "config_schema.json").read_text())
    assert MANIFEST["config_schema"]["groups"] == schema["groups"]
    assert "output" not in MANIFEST["capabilities"]  # HA owns its fixed protocol.


def test_ha_topics_fields_and_discovery_identity_are_unchanged(monkeypatch):
    monkeypatch.setenv("RECAMERA_SN", "Test_SN")
    monkeypatch.setattr(MqttSink, "_run", lambda self: None)
    sent = []
    monkeypatch.setattr(MqttSink, "_safe_publish",
                        lambda self, topic, body, retain=False: sent.append(
                            (topic, body, retain)))
    bridge = HaMqttBridge({"mqtt_host": "broker"})
    sink = bridge._sink
    try:
        assert sink.state_topic == "recamera/motion-detect/state"
        assert sink.status_topic == "recamera/motion-detect/status"
        assert sink.device_name == "reCamera Pro"
        templates = {
            "motion": "{{ 'ON' if value_json.summary.motion | default(false) else 'OFF' }}",
            "motion_score": "{{ value_json.summary.score | default(0) }}",
            "motion_changed_pixels": "{{ value_json.summary.changed_pixels | default(0) }}",
        }
        for entity in sink.entities:
            key = entity["object_id"]
            assert sink._discovery_topic(entity) == (
                f"homeassistant/{entity['component']}/recamera_test_sn_motion-detect/{key}/config")
            declaration = sink._discovery_payload(entity)
            assert declaration["unique_id"] == f"recamera_test_sn_motion-detect_{key}"
            assert declaration["device"]["identifiers"] == ["recamera_test_sn"]
            assert declaration["value_template"] == templates[key]
            assert declaration["availability_topic"] == sink.status_topic
        events = [
            {"kind": "motion", "score": 0.5, "changed_ratio": 0.25, "changed_pixels": 10},
            {"kind": "motion_state", "motion": True, "score": 0.5,
             "changed_ratio": 0.25, "changed_pixels": 10},
        ]
        bridge.emit(events, 12.5)
        assert json.loads(sent[0][1]) == {
            "app": "motion-detect", "seq": 1, "pts": 12.5,
            "results_count": 0, "person_count": 0, "fallen_count": 0,
            "counts_by_kind": {"motion": 1, "motion_state": 1}, "class_counts": {},
            "summary": {"motion": True, "score": 0.5, "changed_ratio": 0.25,
                        "changed_pixels": 10, "person_count": 0, "fallen_count": 0},
            "events": events,
        }
        assert sent[0][0] == sink.state_topic and sent[0][2] is False
    finally:
        bridge.close()
    assert sent[-1] == (sink.status_topic, b"offline", True)


def test_dedicated_mqtt_never_adds_a_global_or_cli_publisher(monkeypatch):
    monkeypatch.setenv("RECAMERA_MQTT_ENABLED", "1")
    monkeypatch.setenv("RECAMERA_MQTT_HOST", "another-broker")
    # No CLI/manifest access or second MqttSink construction should occur.
    assert _maybe_open_mqtt_sink(MotionDetectApp(), str(ROOT), None, verbose=False) is None


def test_motion_edges_hold_and_heartbeat_match_the_legacy_protocol():
    app = MotionDetectApp()
    app.setup({})
    published, mqtt = [], []
    app._mqtt = SimpleNamespace(emit=lambda events, pts: mqtt.append((events, pts)))
    app.emit_status = lambda summary, **kw: published.append((summary, kw))
    active = {"active": True, "score": 0.6, "changed_ratio": 0.1, "changed_pixels": 1440}
    idle = {**active, "active": False, "score": 0.0}
    app._step(active, 10)
    app._step(idle, 11)  # Still inside motion_hold_ms.
    assert len(mqtt) == 1
    app._step(idle, 14)
    app._step(idle, 44)
    assert [row[0][-1]["motion"] for row in mqtt] == [True, False, False]
    assert [row[0][0]["kind"] for row in mqtt] == ["motion", "motion_state", "motion_state"]
    assert [row[0]["motion"] for row in published] == [True, False, False]


def test_start_failure_closes_raw_source_before_ready(monkeypatch):
    import recamera_ext

    closed = []
    source = SimpleNamespace(close=lambda: closed.append("source"))
    monkeypatch.setattr(recamera_ext, "FrameSource", lambda *a, **kw: source)
    def failed_bridge(*args):
        raise RuntimeError("bridge startup failed")
    monkeypatch.setattr(APP_MODULE, "HaMqttBridge", failed_bridge)
    app = MotionDetectApp()
    with pytest.raises(RuntimeError, match="bridge startup failed"):
        app.start(app_dir=str(ROOT), manifest=MANIFEST, config={}, verbose=False)
    assert closed == ["source"]
    assert app._source is None and app._rt is None


def test_platform_heartbeat_does_not_change_the_ha_reporting_interval():
    app = MotionDetectApp()
    app.setup({})
    mqtt, hub = [], []
    app._mqtt = SimpleNamespace(emit=lambda *args: mqtt.append(args))
    app.emit_status = lambda *args, **kw: hub.append(kw["ts"])
    idle = {"active": False, "score": 0.0, "changed_ratio": 0.0, "changed_pixels": 0}
    for pts in (1, 11, 21, 31):
        app._step(idle, pts)
    assert len(mqtt) == 1 and mqtt[0][1] == 31
    assert hub == [1, 11, 21, 31]


def test_hot_reload_rebuilds_only_changed_resources(monkeypatch):

    app = MotionDetectApp()
    app.setup({})
    core = app._core
    closed = []
    app._mqtt = SimpleNamespace(close=lambda: closed.append("mqtt"))
    replacement = object()
    monkeypatch.setattr(APP_MODULE, "HaMqttBridge", lambda cfg: replacement)
    app.config = {"mqtt_host": "new-broker"}
    app.analysis_width = 320
    app.on_params_changed({"analysis_width", "mqtt_host"})
    assert app._core is not core and app._core.analysis_width == 320
    assert app._mqtt is replacement and closed == ["mqtt"]


def test_managed_loop_uses_raw_frames_and_closes_all_resources(monkeypatch):
    import recamera_ext
    import kit.app as kit_app
    from market.appmgr.result_hub import ResultHub

    calls, packets, messages, overlays = [], [], [], []
    frames = [SimpleNamespace(
        pts_us=1_000_000 + index * 200_000,
        width=32, height=32,
        array=np.full((32, 32), 255 if 6 <= index < 12 else 0, dtype=np.uint8),
    ) for index in range(20)]
    class Source:
        def __init__(self, config, timeout_ms):
            calls.append(("open", config.fps_divisor, timeout_ms))
        def __iter__(self):
            return iter(frames)
        def close(self):
            calls.append("source closed")
    class Bridge:
        def __init__(self, config):
            calls.append("mqtt opened")
        def emit(self, events, pts):
            messages.append((events, pts))
        def close(self):
            calls.append("mqtt closed")
    monkeypatch.setattr(recamera_ext, "FrameSource", Source)
    monkeypatch.setattr(APP_MODULE, "HaMqttBridge", Bridge)
    monkeypatch.setattr(kit_app, "open_frame_source",
                        lambda **kw: pytest.fail("opened an RGB frame adapter"))
    app = MotionDetectApp()
    monkeypatch.setattr(app, "_load_model", lambda *a: pytest.fail("loaded an NPU model"))
    sink = SimpleNamespace(emit_typed=lambda payload, pts: packets.append(payload),
                           emit=lambda payload, pts: overlays.append((payload, pts)))
    try:
        app.start(app_dir=str(ROOT), manifest=MANIFEST, sink=sink, verbose=False,
                  config={"baseline_frames": 1, "current_frames": 1,
                          "baseline_gap_ms": 200, "analysis_width": 32,
                          "motion_hold_ms": 0})
        app.run()
    finally:
        app.finish()
    assert calls == [("open", 6, 5000), "mqtt opened", "source closed", "mqtt closed"]
    assert any(packet["summary"]["motion"] for packet in packets)
    assert packets[-1]["summary"]["daemon_running"] is False
    # Both the upward and downward luminance transitions are motion.
    assert [events[-1]["motion"] for events, _ in messages] == [True, False, True, False]

    hub = ResultHub()
    identity = {"app_id": app.id, "instance_id": "test", "generation": 1}
    assert hub.refresh_app_manifest(identity, MANIFEST, stream_contract={
        "id": "main", "kind": "frame.sock", "path": "/live/0",
    })
    states = {pts: hub.publish_app({**payload, "pts": pts, "seq": seq}, identity)[0]
              for seq, (payload, pts) in enumerate(overlays, 1)}
    motion = states[2.2]
    assert motion["type"] == "frame"
    assert (motion["stream"]["id"], motion["stream"]["width"],
            motion["stream"]["height"]) == ("main", 32, 32)
    assert motion["geometry"][0] == {
        "id": "motion-status", "type": "point", "points": [[0.02, 0.09]],
        "space": "normalized_points", "style": {"color": "#ffda73", "opacity": 0.0},
        "label": "Motion: detected | Score 1.0000 | Change 100.00% (1,024 px)",
    }
    assert states[1]["geometry"][0]["label"] == "Motion: warming up"
    for events, pts in messages:
        state = "detected" if events[-1]["motion"] else "clear"
        assert states[pts]["geometry"][0]["label"].startswith(f"Motion: {state}")


def test_overlay_refreshes_between_slow_detection_samples(monkeypatch):
    import recamera_ext

    frames = [SimpleNamespace(pts_us=index * 500_000, width=32, height=32)
              for index in range(8)]
    frames[0].array = np.zeros((32, 32), dtype=np.uint8)
    opened, overlay_times = [], []

    def open_source(config, **kwargs):
        opened.append(config.fps_divisor)
        return frames

    monkeypatch.setattr(recamera_ext, "FrameSource", open_source)
    monkeypatch.setattr(APP_MODULE, "HaMqttBridge", lambda config: None)
    app = MotionDetectApp()
    app.setup({"sample_fps": 0.2})
    app.emit_status = lambda *args, **kwargs: None
    app.emit = lambda *, ts, **kwargs: overlay_times.append(ts)
    app.prepare_runtime()
    app.run()

    assert opened == [15]
    assert overlay_times == [0.0, 1.0, 2.0, 3.0]

"""
Unit tests for the per-app `output` capability blocks (OUTPUT_SINK_SPEC §7, P3).

These prove the nine apps that opted into `capabilities:["output"]` declare a
well-formed `output` block and that their `default_mapping` rows generate
templates that render valid, loss-free JSON through the real kit pipeline
(`generate_mapping_templates` -> `Jinja2Formatter` -> `ConfigurableSink`).

Covered per §7's checklist:
  * manifest validation: capabilities, field name / source-path uniqueness,
    required descriptor keys, normalizable default_channel / default_mode.
  * representative fixture + template render snapshots (exact rendered JSON).
  * empty-result behaviour (no crash, still valid JSON / suppressed).
  * filters (only_on_detection, class allow-list).
  * legacy bypass (an app WITHOUT the capability keeps the old sink path).
  * the documented per-app pitfalls (ppocr label priority, retail cls==0,
    face demographics not scalar-flattened, fall multi-person aggregation,
    voice persistent summary).
  * voice-transcribe's self-paced (owns_loop + needs_frames=False) integration:
    SIGHUP re-binds the app knob AND kit routes the same reload down the sink
    tree into the ConfigurableSink; one voice event publishes through it.

Run:  python3 apps/test_output_manifests.py     (from repo root, or via pytest)
"""
import importlib.util
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from kit.adapters.output_sink import (  # noqa: E402
    ConfigurableSink,
    Jinja2Formatter,
    RawJsonFormatter,
    assemble_output_sink,
    generate_mapping_templates,
    resolve_output_config,
)
from kit.adapters.result_sink import MultiSink, OutputChannel  # noqa: E402

APPS = [
    "face-analysis", "facemesh-reader", "fall-detection", "fitness-trainer",
    "ppocr-reader", "qrcode-reader", "retail-vision", "voice-transcribe",
    "yolo-detector",
]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
class RecordChannel(OutputChannel):
    """Records every published message. `name` picks the channel identity so a
    non-'ws' name is used (ws-only assembly yields no external channel)."""

    def __init__(self, name="mqtt"):
        self.name = name
        self.msgs = []

    def publish(self, message):
        self.msgs.append(message)


def _manifest(app):
    with open(os.path.join(_ROOT, "apps", app, "manifest.json")) as fh:
        return json.load(fh)


def _render(app, results, events, *, filters=None):
    """Run one frame through a real ConfigurableSink using the app's declared
    default_mapping. Returns {topic: parsed_json_dict}."""
    m = _manifest(app)
    specs = generate_mapping_templates(m["output"]["default_mapping"])
    fmt = Jinja2Formatter(specs, app_id=app, device_id="dev-test")
    rec = RecordChannel()
    sink = ConfigurableSink(app_id=app, channels=[rec], formatter=fmt,
                            filters=filters)
    sink.set_frame_size(1920, 1080)
    sink.emit({"results": results, "events": events}, 1.5)
    out = {}
    for msg in rec.msgs:
        assert msg.topic and "+" not in msg.topic and "#" not in msg.topic, msg.topic
        out[msg.topic] = json.loads(msg.body.decode("utf-8"))  # must be valid JSON
    return out


# representative fixtures (results, events) per app -- mirror the §7 observed
# field lists closely enough to exercise every default_mapping row.
FIXTURES = {
    "yolo-detector": (
        [{"box": [1, 2, 3, 4], "cls": 0, "cls_name": "person", "score": 0.9},
         {"box": [5, 6, 7, 8], "cls": 2, "cls_name": "car", "score": 0.7}],
        [{"kind": "detection", "label": "person", "cls": 0, "score": 0.9,
          "box": [1, 2, 3, 4]}],
    ),
    "qrcode-reader": (
        [{"text": "hi", "quad": [[0, 0], [1, 0], [1, 1], [0, 1]]}],
        [{"kind": "qrcode", "text": "hi", "quad": [[0, 0], [1, 0], [1, 1], [0, 1]]},
         {"kind": "qrcode", "text": "", "quad": [[0, 0], [1, 0], [1, 1], [0, 1]]}],
    ),
    "ppocr-reader": (
        [{"kind": "text", "box": [1, 2, 3, 4], "quad": [[0, 0]], "score": 0.8,
          "cls_name": "text", "text": "Hello", "rec_conf": 0.95}],
        [{"kind": "text", "box": [1, 2, 3, 4], "quad": [[0, 0]], "text": "Hello",
          "score": 0.8, "rec_conf": 0.95},
         {"kind": "text", "text": "", "score": 0.5, "rec_conf": 0.1}],
    ),
    "retail-vision": (
        [{"box": [1, 2, 3, 4], "cls": 0, "cls_name": "person", "score": 0.9, "kind": "person"},
         {"box": [5, 6, 7, 8], "cls": 0, "cls_name": "person", "score": 0.8, "kind": "person"}],
        [{"kind": "track", "track_id": 3, "state": "browsing", "in_zone": True,
          "score": 0.9, "speed_px_s": 12.0, "box": [1, 2, 3, 4]},
         {"kind": "line_cross", "track_id": 3, "dir": "in"},
         {"kind": "metrics", "occupancy": 2, "browsing": 1, "engaged": 1,
          "assistance": 0, "peak": 5, "entry_count": 10, "exit_count": 8}],
    ),
    "face-analysis": (
        [{"box": [1, 2, 3, 4], "score": 0.9, "kind": "face", "blur": False,
          "gender": "female", "gender_conf": 0.8, "age": "20-29", "age_conf": 0.6,
          "race": "asian", "race_conf": 0.7, "emotion": "Happiness", "emotion_conf": 0.5}],
        [{"kind": "face", "box": [1, 2, 3, 4], "gender": "female"},
         {"kind": "demographics", "window_sec": 30.0, "faces": 5,
          "gender": {"female": 3, "male": 2}, "age": {"20-29": 4},
          "race": {"asian": 5}, "emotion": {"Happiness": 3}}],
    ),
    "facemesh-reader": (
        [{"box": [1, 2, 3, 4], "score": 0.9, "kind": "face", "presence": 0.9,
          "landmark_count": 468, "ear": 0.3, "mar": 0.2, "keypoints": [[1, 2]]}],
        [{"kind": "metrics", "face_valid": True, "avg_ear": 0.28, "left_ear": 0.27,
          "right_ear": 0.29, "mar": 0.2, "eyes_closed": False, "mouth_open": False,
          "state": "alert", "drowsiness_level": "none", "perclos_pct": 5.0,
          "continuous_closure_sec": 0.0, "is_yawning": False, "yawn_count_5min": 0,
          "alert_active": False},
         {"kind": "blink"}, {"kind": "blink"}, {"kind": "yawn"}],
    ),
    "fall-detection": (
        [{"box": [1, 2, 3, 4], "score": 0.9, "keypoints": [[1, 2]], "kind": "person",
          "track_id": 1, "state": "fallen", "fall_detected": True, "event_id": 7,
          "person_detected": True, "person_score": 0.9, "tracking": {},
          "missed_frames": 0, "features": {}},
         {"box": [5, 6, 7, 8], "score": 0.8, "keypoints": [[3, 4]], "kind": "person",
          "track_id": 2, "state": "standing", "fall_detected": False, "event_id": 0}],
        [{"kind": "pose_state", "track_id": 1, "state": "fallen",
          "fall_detected": True, "event_id": 7},
         {"kind": "fall", "track_id": 1, "event_id": 7, "state": "fallen"}],
    ),
    "fitness-trainer": (
        [{"box": [1, 2, 3, 4], "score": 0.9, "keypoints": [[1, 2]], "kind": "person"}],
        [{"kind": "workout", "mode": "squat", "reps": 5, "set": 1, "target_reps": 12,
          "target_sets": 3, "stage": "down", "angle": 95.0, "has_angle": True,
          "tracking": True, "rep_completed": False, "set_completed": False,
          "workout_complete": False, "form_warning": "keep back straight"}],
    ),
    "voice-transcribe": (
        [],
        [{"kind": "state", "type": "state", "t": 1.0, "state": "transcribing"},
         {"kind": "transcript", "type": "transcript", "t": 2.0, "text": "hello world",
          "transcript": "hello world", "audio_sec": 1.2, "rtf": 0.3, "language": "en"},
         {"kind": "wake", "type": "wake", "t": 0.5, "keyword": "hello camera",
          "backend": "kws", "score": 1.5}],
    ),
}

_VALID_MODES = {"ha", "custom", "raw"}
_REQUIRED_FIELD_KEYS = {"name", "from", "type", "description"}


# --------------------------------------------------------------------------- #
# 1. manifest validation
# --------------------------------------------------------------------------- #
def test_manifests_declare_output_and_valid_fields():
    for app in APPS:
        m = _manifest(app)
        assert "output" in (m.get("capabilities") or []), f"{app} missing capability"
        o = m.get("output") or {}

        # default_channel normalizes to a non-empty list of channel strings.
        dc = o.get("default_channel")
        chans = [dc] if isinstance(dc, str) else list(dc or [])
        assert chans and all(isinstance(c, str) for c in chans), f"{app} default_channel"

        assert o.get("default_mode") in _VALID_MODES, f"{app} default_mode"

        fields = o.get("fields") or []
        assert fields, f"{app} has no fields"
        names, froms = [], []
        for f in fields:
            missing = _REQUIRED_FIELD_KEYS - set(f)
            assert not missing, f"{app} field {f.get('name')} missing {missing}"
            names.append(f["name"])
            froms.append(f["from"])
        assert len(names) == len(set(names)), \
            f"{app} duplicate field names: {sorted(n for n in names if names.count(n) > 1)}"
        assert len(froms) == len(set(froms)), \
            f"{app} duplicate source paths: {sorted(s for s in froms if froms.count(s) > 1)}"
    print("PASS test_manifests_declare_output_and_valid_fields")


def test_ha_entities_preserved():
    # §7: do not delete existing ha_entities until HA-mode parity is proven.
    for app in APPS:
        m = _manifest(app)
        assert m.get("ha_entities"), f"{app} lost its ha_entities"
    print("PASS test_ha_entities_preserved")


def test_resolve_output_config_roundtrips_manifest_defaults():
    # With no persisted overrides, resolve_output_config must surface the
    # manifest defaults (channels, mode, mapping) that the fields describe.
    for app in APPS:
        m = _manifest(app)
        cfg = resolve_output_config(m, {})
        assert cfg["channels"], f"{app} resolved channels empty"
        assert cfg["mode"] in _VALID_MODES, f"{app} resolved mode"
        assert cfg["mapping"] == (m["output"].get("default_mapping") or []), app
    print("PASS test_resolve_output_config_roundtrips_manifest_defaults")


# --------------------------------------------------------------------------- #
# 2. representative fixture render snapshots
# --------------------------------------------------------------------------- #
def test_render_snapshots():
    out = _render("yolo-detector", *FIXTURES["yolo-detector"])
    assert out["recamera/yolo-detector/count"] == {"count": 2, "person_count": 1}
    assert out["recamera/yolo-detector/detections"]["detections"][0]["label"] == "person"

    out = _render("qrcode-reader", *FIXTURES["qrcode-reader"])
    assert out["recamera/qrcode-reader/count"] == {"count": 2}
    assert out["recamera/qrcode-reader/text"] == {"last_text": "hi", "texts": ["hi"]}

    out = _render("retail-vision", *FIXTURES["retail-vision"])
    assert out["recamera/retail-vision/count"] == {"person_count": 2}
    assert out["recamera/retail-vision/metrics"] == \
        {"occupancy": 2, "entry_count": 10, "exit_count": 8, "peak": 5}

    out = _render("facemesh-reader", *FIXTURES["facemesh-reader"])
    assert out["recamera/facemesh-reader/count"] == {"face_count": 1}
    assert out["recamera/facemesh-reader/edges"] == {"blink_count": 2, "yawn_count": 1}
    assert out["recamera/facemesh-reader/metrics"]["state"] == "alert"

    out = _render("fall-detection", *FIXTURES["fall-detection"])
    assert out["recamera/fall-detection/summary"] == {"person_count": 2, "fallen_count": 1}
    assert out["recamera/fall-detection/fall"]["fall_event"]["event_id"] == 7

    out = _render("fitness-trainer", *FIXTURES["fitness-trainer"])
    w = out["recamera/fitness-trainer/workout"]
    assert w["mode"] == "squat" and w["reps"] == 5 and w["stage"] == "down"

    out = _render("voice-transcribe", *FIXTURES["voice-transcribe"])
    assert out["recamera/voice-transcribe/state"] == {"state": "transcribing"}
    assert out["recamera/voice-transcribe/transcript"] == {"transcript": "hello world"}
    assert out["recamera/voice-transcribe/wake"] == {"wake": "hello camera"}
    print("PASS test_render_snapshots")


# --------------------------------------------------------------------------- #
# 3. documented per-app pitfalls (§7)
# --------------------------------------------------------------------------- #
def test_pitfall_ppocr_label_prefers_text_over_cls_name():
    # §7: ConfigurableSink must NOT copy Official's cls_name-first priority.
    m = _manifest("ppocr-reader")
    body = " ".join(s["body"] for s in
                    generate_mapping_templates(m["output"]["default_mapping"]))
    assert "attribute='text'" in body and "cls_name" not in body
    out = _render("ppocr-reader", *FIXTURES["ppocr-reader"])
    # the recognized string, never the literal "text" tag
    assert out["recamera/ppocr-reader/text"]["last_text"] == "Hello"
    print("PASS test_pitfall_ppocr_label_prefers_text_over_cls_name")


def test_pitfall_retail_uses_cls_zero_not_kind_person():
    m = _manifest("retail-vision")
    body = " ".join(s["body"] for s in
                    generate_mapping_templates(m["output"]["default_mapping"]))
    assert "selectattr('cls','equalto',0)" in body
    out = _render("retail-vision", *FIXTURES["retail-vision"])
    assert out["recamera/retail-vision/count"] == {"person_count": 2}
    print("PASS test_pitfall_retail_uses_cls_zero_not_kind_person")


def test_pitfall_face_demographics_published_as_nested_object():
    # §7: publish demographics as a nested JSON object -- NOT flattened through a
    # scalar summary, and NOT routed through the detection.count scalar.
    out = _render("face-analysis", *FIXTURES["face-analysis"])
    demo = out["recamera/face-analysis/demographics"]["demographics"]
    assert isinstance(demo, dict) and isinstance(demo["gender"], dict)
    assert demo["gender"] == {"female": 3, "male": 2}
    assert out["recamera/face-analysis/count"] == {"face_count": 1}
    print("PASS test_pitfall_face_demographics_published_as_nested_object")


def test_pitfall_fall_reuses_multiperson_aggregation():
    # §7: aggregate person/fallen counts + publish the fall edge immediately.
    out = _render("fall-detection", *FIXTURES["fall-detection"])
    assert out["recamera/fall-detection/summary"]["person_count"] == 2
    assert out["recamera/fall-detection/summary"]["fallen_count"] == 1
    assert "recamera/fall-detection/fall" in out  # edge published on its own topic
    print("PASS test_pitfall_fall_reuses_multiperson_aggregation")


def test_pitfall_voice_persistent_summary_survives_state_event():
    # §7: the canonical persistent summary/last_text must NOT be cleared by a
    # later non-transcript event. Default_mode is raw, which serializes the full
    # envelope including the app's top-level state + summary.
    m = _manifest("voice-transcribe")
    assert m["output"]["default_mode"] == "raw"
    rec = RecordChannel()
    sink = ConfigurableSink(app_id="voice-transcribe", channels=[rec],
                            formatter=RawJsonFormatter())
    # a transcript, then a bare state event -- summary.text must persist.
    sink.emit({"results": [], "events": [{"kind": "transcript", "text": "hi"}],
               "state": "transcribing",
               "summary": {"state": "transcribing", "text": "hi"}}, 1.0)
    sink.emit({"results": [], "events": [{"kind": "state", "state": "idle"}],
               "state": "idle", "summary": {"state": "idle", "text": "hi"}}, 2.0)
    last = json.loads(rec.msgs[-1].body.decode("utf-8"))
    assert last["summary"]["text"] == "hi", "persistent last transcript was cleared"
    print("PASS test_pitfall_voice_persistent_summary_survives_state_event")


# --------------------------------------------------------------------------- #
# 4. empty-result behaviour
# --------------------------------------------------------------------------- #
def test_empty_results_never_crash_and_stay_valid_json():
    for app in APPS:
        out = _render(app, [], [])   # must not raise; any output stays valid JSON
        for topic, body in out.items():
            assert isinstance(body, dict), (app, topic)
    print("PASS test_empty_results_never_crash_and_stay_valid_json")


# --------------------------------------------------------------------------- #
# 5. filters
# --------------------------------------------------------------------------- #
def test_only_on_detection_suppresses_empty_frames():
    # yolo with only_on_detection: an empty frame yields no publish at all.
    out = _render("yolo-detector", [], [], filters={"only_on_detection": True})
    assert out == {}
    out = _render("yolo-detector", *FIXTURES["yolo-detector"],
                  filters={"only_on_detection": True})
    assert out  # a real detection still publishes
    print("PASS test_only_on_detection_suppresses_empty_frames")


def test_class_allowlist_filters_results():
    # restrict to 'car': person_count collapses to 0, count to the car only.
    out = _render("yolo-detector", *FIXTURES["yolo-detector"],
                  filters={"classes": ["car"]})
    assert out["recamera/yolo-detector/count"] == {"count": 1, "person_count": 0}
    print("PASS test_class_allowlist_filters_results")


# --------------------------------------------------------------------------- #
# 6. legacy bypass
# --------------------------------------------------------------------------- #
class _FakeApp:
    id = "legacy-app"
    name = "Legacy App"


def test_legacy_bypass_when_capability_absent():
    # No `capabilities:["output"]` -> (None, False): caller keeps the old path.
    manifest = {"name": "Legacy", "output": {"sink": "ws"}}
    sink, opted = assemble_output_sink(_FakeApp(), _ROOT, manifest, {})
    assert sink is None and opted is False
    print("PASS test_legacy_bypass_when_capability_absent")


def test_ws_only_optin_engages_no_external_channel():
    # Opted in but WS-only (the manifest default) -> (None, True): the primary
    # overlay carries output, and the legacy MQTT path stays disengaged.
    m = _manifest("yolo-detector")
    sink, opted = assemble_output_sink(_FakeApp(), _ROOT, m, {})
    assert sink is None and opted is True
    print("PASS test_ws_only_optin_engages_no_external_channel")


def test_mqtt_channel_optin_builds_configurable_sink():
    # Selecting mqtt with a broker host builds a real ConfigurableSink.
    m = _manifest("yolo-detector")
    eff = {"output_channels": ["mqtt"], "dMqtt": {"sURL": "127.0.0.1", "iPort": 1883}}
    sink, opted = assemble_output_sink(_FakeApp(), _ROOT, m, eff)
    try:
        assert opted is True and isinstance(sink, ConfigurableSink)
        assert any(c.name == "mqtt" for c in sink.channels)
    finally:
        if sink is not None:
            sink.close()
    print("PASS test_mqtt_channel_optin_builds_configurable_sink")


# --------------------------------------------------------------------------- #
# 7. voice-transcribe run()-override integration (§7 integration exception)
# --------------------------------------------------------------------------- #
def _load_voice_app():
    path = os.path.join(_ROOT, "apps", "voice-transcribe", "app.py")
    spec = importlib.util.spec_from_file_location("voice_transcribe_app", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _start_voice_app(sink, config=None):
    """Start the (loop-owning, frameless) voice app on `sink` without running it."""
    class _NoopAsr:
        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1

    mod = _load_voice_app()
    app = mod.VoiceTranscribeApp()
    fake_asr = _NoopAsr()
    app._build_asr = lambda: fake_asr
    # These tests exercise output/reload plumbing only.  The real Voice app now
    # prepares VAD/KWS/audio (including a live PCM probe) inside start(), which
    # has dedicated lifecycle tests and must not reach host sherpa/ALSA here.
    app.prepare_runtime = lambda: None
    app._test_asr = fake_asr
    app_dir = os.path.join(_ROOT, "apps", "voice-transcribe")
    with open(os.path.join(app_dir, "manifest.json")) as f:
        manifest = json.load(f)
    app.start(None, sink=sink, verbose=False, app_dir=app_dir,
              manifest=manifest, config=config or {})
    return app


def test_sink_reload_reaches_configurable_sink_through_nested_multisink():
    """The generic replacement for the deleted `_find_configurable_sink`.

    Hot-reload now travels DOWN the sink tree (ResultSink.on_config_reload ->
    MultiSink fan-out), so no app has to walk it looking for its own sink.
    """
    cs = ConfigurableSink(app_id="voice-transcribe", channels=[RecordChannel()],
                          formatter=RawJsonFormatter())
    nested = MultiSink([MultiSink([cs])])
    nested.on_config_reload({"output_filters": {"only_on_detection": True}})
    assert cs._only_on_detection is True, "filter not routed to ConfigurableSink"
    MultiSink([]).on_config_reload({})          # empty tree must not raise
    print("PASS test_sink_reload_reaches_configurable_sink_through_nested_multisink")


def test_voice_reload_keeps_captured_knob_and_routes_filter_to_sink():
    """Output reload stays live; the already-open Voice graph needs restart."""
    cs = ConfigurableSink(app_id="voice-transcribe", channels=[RecordChannel()],
                          formatter=RawJsonFormatter())
    app = _start_voice_app(cs)
    assert app.wakeword == "hello camera", "manifest default not auto-bound"

    from kit import config as _cfg
    orig = _cfg.effective_config
    _cfg.effective_config = lambda *a, **kw: {
        "wakeword": "hey cam", "output_filters": {"only_on_detection": True}}
    try:
        app._reload_flag = True
        app.tick()                              # what _on_voice_event calls
    finally:
        _cfg.effective_config = orig
        app.finish()
    assert app.wakeword == "hello camera", "captured knob changed without restart"
    assert cs._only_on_detection is True, "filter not routed to ConfigurableSink"
    print("PASS test_voice_reload_keeps_captured_knob_and_routes_filter_to_sink")


def test_voice_event_polls_reload_and_emits_through_sink():
    rec = RecordChannel()
    cs = ConfigurableSink(app_id="voice-transcribe", channels=[rec],
                          formatter=RawJsonFormatter())
    app = _start_voice_app(cs)
    try:
        # tick() must be safe to poll even with no pending SIGHUP.
        app._reload_flag = False
        app._on_voice_event({"type": "state", "state": "listening", "t": 1.0})
    finally:
        app.finish()
    assert rec.msgs, "voice event did not emit through the ConfigurableSink"
    body = json.loads(rec.msgs[-1].body.decode("utf-8"))
    # persistent top-level state is present in the raw envelope
    assert body["state"] == "listening" and body["summary"]["state"] == "listening"
    print("PASS test_voice_event_polls_reload_and_emits_through_sink")


_TESTS = [
    test_manifests_declare_output_and_valid_fields,
    test_ha_entities_preserved,
    test_resolve_output_config_roundtrips_manifest_defaults,
    test_render_snapshots,
    test_pitfall_ppocr_label_prefers_text_over_cls_name,
    test_pitfall_retail_uses_cls_zero_not_kind_person,
    test_pitfall_face_demographics_published_as_nested_object,
    test_pitfall_fall_reuses_multiperson_aggregation,
    test_pitfall_voice_persistent_summary_survives_state_event,
    test_empty_results_never_crash_and_stay_valid_json,
    test_only_on_detection_suppresses_empty_frames,
    test_class_allowlist_filters_results,
    test_legacy_bypass_when_capability_absent,
    test_ws_only_optin_engages_no_external_channel,
    test_mqtt_channel_optin_builds_configurable_sink,
    test_sink_reload_reaches_configurable_sink_through_nested_multisink,
    test_voice_reload_keeps_captured_knob_and_routes_filter_to_sink,
    test_voice_event_polls_reload_and_emits_through_sink,
]


if __name__ == "__main__":
    for t in _TESTS:
        t()
    print(f"\nALL {len(_TESTS)} OUTPUT-MANIFEST TESTS PASSED")

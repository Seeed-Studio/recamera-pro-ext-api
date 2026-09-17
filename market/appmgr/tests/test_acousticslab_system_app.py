"""The AcousticsLab system card adapts the firmware daemon without owning it."""
import json
import os
import sys
import tempfile
import time
from types import SimpleNamespace

import pytest

# paths.py snapshots the layout from the env at IMPORT time, and pytest imports
# every test module before running any of them -- so whichever module sorts
# first decides where the whole package thinks /userdata is. This file sorts
# before test_assets.py, so it must carry the same redirection.
_BASE = tempfile.mkdtemp(prefix="appmgr-acousticslab.")
os.environ.setdefault("APPMGR_APPS_DIR", os.path.join(_BASE, "apps"))
os.environ.setdefault("APPMGR_DIR", os.path.join(_BASE, "appmgr"))
os.environ.setdefault("APPMGR_VENVS_DIR", os.path.join(_BASE, "venvs"))
os.environ.setdefault("APPMGR_APPSTAGE_DIR", os.path.join(_BASE, "appstage"))
os.environ.setdefault("APPMGR_MODEL_ROOTS", os.path.join(_BASE, "models"))

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from appmgr import acousticslab, paths, server  # noqa: E402


@pytest.fixture(autouse=True)
def isolate_acousticslab_cache():
    server._acousticslab_invalidate()
    yield
    server._acousticslab_invalidate()


def _snapshot(**updates):
    value = {"available": True, "running": True, "enabled": True,
             "state": "running",
             "head": {"id": "00000000-0000-4000-8000-000000000000",
                      "origin": "default", "n_classes": 2,
                      "labels": ["_background_noise_", "Yes"],
                      "activated_at": "2026-09-08T07:14:40Z"},
             "uptime_s": 60,
             "inference": {"healthy": True, "detail": "waiting", "stale": False},
             "reason": None, "pid": 992}
    value.update(updates)
    return value


def _view(monkeypatch, snap, operation=None):
    monkeypatch.setattr(server, "_acousticslab_status", lambda: snap)
    return server._acousticslab_app_view(
        SimpleNamespace(active_for=lambda _: operation))


def test_system_card_identity_and_console_without_instance(monkeypatch):
    item = _view(monkeypatch, _snapshot())
    assert item["id"] == "acousticslab"
    assert item["system"] is True and item["type"] == "system"
    assert item["source"] == {"kind": "system", "id": "acousticslab"}
    assert item["console"] == {"path": "/extension/acousticslab/"}
    assert item["running"] is True and item["status"] == "running"
    assert item["pid"] is None and item["instance"] is None
    assert item["runtime"]["pid"] is None
    assert item["runtime"]["desired_state"] == "running"
    assert item["acousticslab"]["head"]["labels"] == ["_background_noise_", "Yes"]
    assert item["actions"] == {"start": False, "stop": True, "restart": True,
                               "configure": False, "uninstall": False}


def test_stopped_daemon_can_start_but_not_stop_or_restart(monkeypatch):
    item = _view(monkeypatch, _snapshot(available=False, running=False,
                                        enabled=True, state="stopped",
                                        head=None, uptime_s=None,
                                        inference=None,
                                        reason="enabled_but_stopped", pid=None))
    assert item["running"] is False and item["status"] == "stopped"
    assert item["reason"] == "enabled_but_stopped"
    assert item["actions"]["start"] is True
    assert item["actions"]["stop"] is True     # enabled intent still cancelable
    assert item["actions"]["restart"] is False


def test_desired_stopped_reports_no_reason_and_no_start_block(monkeypatch):
    item = _view(monkeypatch, _snapshot(available=False, running=False,
                                        enabled=False, state="stopped",
                                        reason=None, pid=None))
    assert item["status"] == "stopped" and item["reason"] is None
    assert item["runtime"]["desired_state"] == "stopped"
    assert item["actions"]["start"] is True
    assert item["actions"]["stop"] is False


def test_probe_failure_never_fakes_state(monkeypatch):
    item = _view(monkeypatch, _snapshot(available=False, running=True,
                                        state="unknown",
                                        reason="acousticslabd api GET "
                                               "/api/v1/status -> transport error: x"))
    assert item["running"] is False
    assert item["status"] == "unknown"
    assert item["error"] is not None
    assert item["actions"]["start"] is False
    assert item["actions"]["stop"] is False


@pytest.mark.parametrize("operation,expected", [
    ({"type": "stop"}, "stopping"),
    ({"type": "start"}, "starting"),
    ({"type": "restart"}, "starting"),
])
def test_active_operation_overrides_observed_status(monkeypatch, operation, expected):
    item = _view(monkeypatch, _snapshot(), operation=operation)
    assert item["status"] == expected
    assert item["actions"]["start"] is False
    assert item["actions"]["stop"] is False
    assert item["actions"]["restart"] is False


def test_v1_apps_list_carries_both_system_apps(monkeypatch):
    monkeypatch.setattr(server, "do_list",
                        lambda: {"apps": [], "active_app": None,
                                 "running_apps": [], "state_revision": 7})
    monkeypatch.setattr(server, "_builtin_status", lambda: {
        "available": True, "enabled": True, "state": "running",
        "model": "m.rknn", "fps": 20, "actual_fps": 18,
        "external_hold": None, "reason": None})
    monkeypatch.setattr(server, "_acousticslab_status", lambda: _snapshot())
    monkeypatch.setattr(server, "_operation_manager",
                        lambda: SimpleNamespace(active_for=lambda _: None))
    listing = server.do_v1_apps()
    ids = [app["id"] for app in listing["apps"]]
    assert ids[:2] == ["builtin", "acousticslab"]
    assert "acousticslab" in listing["running_apps"]


def test_system_apps_cannot_be_uninstalled_or_configured():
    with pytest.raises(ValueError, match="system application"):
        server.do_v1_delete(acousticslab.AL_ID)
    with pytest.raises(ValueError, match="system application"):
        server.do_uninstall(acousticslab.AL_ID)
    with pytest.raises(ValueError, match="console"):
        server.do_get_config(acousticslab.AL_ID)
    with pytest.raises(ValueError, match="console"):
        server.do_set_config(acousticslab.AL_ID, {"anything": 1})


def test_lifecycle_gate_skips_installed_dir_check(monkeypatch):
    """do_v1_lifecycle must reach do_start for the synthetic system app."""
    seen = {}

    def fake_start(app_id, **kwargs):
        seen["id"] = app_id
        return {"id": app_id, "started": True}

    monkeypatch.setattr(server, "do_start", fake_start)
    monkeypatch.setattr(server, "_operation_manager",
                        lambda: SimpleNamespace(
                            submit=lambda action, app_id, job:
                                {"type": action, "app_id": app_id,
                                 "result": job()},
                            events=SimpleNamespace(
                                publish=lambda *a, **k: None)))
    receipt = server.do_v1_lifecycle(acousticslab.AL_ID, "start")
    assert receipt["operation"]["app_id"] == "acousticslab"
    assert seen["id"] == "acousticslab"


def test_do_list_never_surfaces_reserved_directory(monkeypatch, tmp_path):
    stray = tmp_path / "acousticslab"
    stray.mkdir()
    (stray / "manifest.json").write_text(json.dumps(
        {"id": "acousticslab", "version": "9.9.9", "entry": "app.py"}))
    monkeypatch.setattr(paths, "APPS_DIR", str(tmp_path))
    monkeypatch.setattr(server, "_read_manifest", lambda app_id: None)
    listing = server.do_list()
    assert all(app.get("id") != "acousticslab"
               for app in listing.get("apps") or [])


# --------------------------------------------------------------------------- #
# adapter unit tests (UDS/init-script seam mocked)
# --------------------------------------------------------------------------- #
def test_adapter_desired_state_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr(acousticslab, "_DESIRED_PATH",
                        str(tmp_path / "acousticslab.json"))
    assert acousticslab.desired_state() == "running"      # boot default
    acousticslab._write_desired("stopped")
    assert acousticslab.desired_state() == "stopped"
    acousticslab._write_desired("running")
    assert acousticslab.desired_state() == "running"
    (tmp_path / "acousticslab.json").write_text("not json")
    assert acousticslab.desired_state() == "running"      # corrupt -> default


def test_adapter_process_pid_requires_matching_comm(monkeypatch, tmp_path):
    pidfile = tmp_path / "acousticslabd.pid"
    pidfile.write_text(str(os.getpid()))
    monkeypatch.setattr(acousticslab, "_PIDFILE", str(pidfile))
    # This test process is not acousticslabd -> comm mismatch -> not running.
    assert acousticslab.process_pid() is None
    assert acousticslab.is_running() is False
    pidfile.unlink()
    assert acousticslab.process_pid() is None


def test_adapter_snapshot_never_fabricates_when_stopped(monkeypatch, tmp_path):
    monkeypatch.setattr(acousticslab, "_PIDFILE",
                        str(tmp_path / "absent.pid"))
    monkeypatch.setattr(acousticslab, "_DESIRED_PATH",
                        str(tmp_path / "absent.json"))
    snap = acousticslab.snapshot()
    assert snap["running"] is False and snap["available"] is False
    assert snap["state"] == "stopped"
    assert snap["reason"] == "enabled_but_stopped"
    assert snap["head"] is None


def _running_snapshot_fakes(monkeypatch, tmp_path, active, listing=None):
    """Pin a running daemon whose /active returns `active`; `listing` is the
    /workspaces payload (None = endpoint raises like an unreachable daemon)."""
    monkeypatch.setattr(acousticslab, "_DESIRED_PATH", str(tmp_path / "d.json"))
    monkeypatch.setattr(acousticslab, "process_pid", lambda: 4242)
    monkeypatch.setattr(acousticslab, "status",
                        lambda: {"uptime_s": 5, "subsystems": {}})
    monkeypatch.setattr(acousticslab, "active_head", lambda: active)

    def fake_workspaces():
        if listing is None:
            raise acousticslab.AcousticsLabError("unavailable")
        return listing

    monkeypatch.setattr(acousticslab, "workspaces", fake_workspaces)
    acousticslab._WORKSPACE_NAME_CACHE.clear()


def test_snapshot_default_head_never_touches_workspaces(monkeypatch, tmp_path):
    def forbidden():
        raise AssertionError("default origin must not query /workspaces")

    _running_snapshot_fakes(monkeypatch, tmp_path, {
        "runtime_head_id": "h0", "origin": "default",
        "n_classes": 20, "labels": [], "activated_at": "t0"})
    monkeypatch.setattr(acousticslab, "workspaces", forbidden)
    head = acousticslab.snapshot()["head"]
    assert head["origin"] == "default"
    assert head["source_workspace_id"] is None
    assert head["source_workspace_alive"] is None
    assert head["workspace_name"] is None


def test_snapshot_live_head_resolves_workspace_name(monkeypatch, tmp_path):
    _running_snapshot_fakes(monkeypatch, tmp_path, {
        "runtime_head_id": "h1", "origin": "head",
        "n_classes": 2, "labels": ["_background_noise_", "cat"],
        "activated_at": "t1", "source_workspace_id": "ws-1",
        "source_workspace_alive": True},
        {"workspaces": [{"id": "ws-1", "name": "Cats"}]})
    head = acousticslab.snapshot()["head"]
    assert head["workspace_name"] == "Cats"
    assert head["source_workspace_alive"] is True
    # The cache was seeded for a later detached snapshot.
    assert acousticslab._WORKSPACE_NAME_CACHE == {"ws-1": "Cats"}


def test_snapshot_detached_head_uses_cached_name_or_none(monkeypatch, tmp_path):
    active = {
        "runtime_head_id": "h1", "origin": "head", "n_classes": 2,
        "labels": [], "activated_at": "t1",
        "source_workspace_id": "ws-1", "source_workspace_alive": False}
    _running_snapshot_fakes(monkeypatch, tmp_path, active,
                            {"workspaces": []})
    assert acousticslab.snapshot()["head"]["workspace_name"] is None

    acousticslab._WORKSPACE_NAME_CACHE["ws-1"] = "Cats"
    assert acousticslab.snapshot()["head"]["workspace_name"] == "Cats"


def test_snapshot_workspace_lookup_failure_keeps_status(monkeypatch, tmp_path):
    _running_snapshot_fakes(monkeypatch, tmp_path, {
        "runtime_head_id": "h1", "origin": "head", "n_classes": 2,
        "labels": [], "activated_at": "t1",
        "source_workspace_id": "ws-1", "source_workspace_alive": True},
        None)  # /workspaces raises
    snap = acousticslab.snapshot()
    assert snap["state"] == "running"
    assert snap["head"]["workspace_name"] is None


def test_adapter_start_stop_use_init_script_and_persist_desired(
        monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(acousticslab, "_DESIRED_PATH",
                        str(tmp_path / "desired.json"))

    def fake_initctl(action):
        # The init script gates on the desired file, so the adapter must
        # persist intent BEFORE invoking it.
        calls.append((action, acousticslab.desired_state()))
        return "ok"

    monkeypatch.setattr(acousticslab, "_initctl", fake_initctl)
    monkeypatch.setattr(acousticslab, "_CONFIRM_TIMEOUT", 0.01)
    monkeypatch.setattr(acousticslab, "_CONFIRM_POLL", 0.001)

    monkeypatch.setattr(acousticslab, "is_running", lambda: True)
    monkeypatch.setattr(acousticslab, "health", lambda: True)
    result = acousticslab.start()
    assert result["started"] is True and calls == [("start", "running")]
    assert acousticslab.desired_state() == "running"

    monkeypatch.setattr(acousticslab, "is_running", lambda: False)
    result = acousticslab.stop()
    assert result["stop_confirmed"] is True
    assert calls == [("start", "running"), ("stop", "stopped")]
    assert acousticslab.desired_state() == "stopped"


def test_adapter_reconcile_enforces_only_the_persisted_intent(
        monkeypatch, tmp_path):
    init_script = tmp_path / "S40acousticslabd"
    init_script.write_text("#!/bin/sh\n")
    monkeypatch.setattr(acousticslab, "_INIT_SCRIPT", str(init_script))
    stopped = []
    started = []
    monkeypatch.setattr(acousticslab, "stop", lambda: stopped.append(1) or {})
    monkeypatch.setattr(acousticslab, "start", lambda: started.append(1) or {})

    monkeypatch.setattr(acousticslab, "desired_state", lambda: "stopped")
    monkeypatch.setattr(acousticslab, "is_running", lambda: True)
    acousticslab.reconcile()
    assert stopped and not started

    stopped.clear()
    monkeypatch.setattr(acousticslab, "desired_state", lambda: "running")
    acousticslab.reconcile()
    assert not stopped and not started

    monkeypatch.setattr(acousticslab, "is_running", lambda: False)
    acousticslab.reconcile()
    assert started and not stopped


def test_adapter_manifest_has_no_config_schema():
    man = acousticslab.manifest()
    assert man["id"] == "acousticslab" and man["type"] == "system"
    assert "config_schema" not in man
    assert man["console"]["path"] == "/extension/acousticslab/"


def test_v1_logs_reads_daemon_daily_logs(monkeypatch, tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    old = log_dir / "acousticslabd.2026-09-08.log"
    old.write_text("old-day line\n" + "\n".join("old %d" % i for i in range(10)))
    today = log_dir / "acousticslabd.2026-09-13.log"
    today.write_text("\n".join("today %d" % i for i in range(50)))
    (log_dir / "unrelated.txt").write_text("skip me")
    monkeypatch.setattr(acousticslab, "_LOG_DIR", str(log_dir))
    payload = server.do_v1_logs(acousticslab.AL_ID, tail=5)
    assert payload["id"] == "acousticslab"
    assert payload["lines"] == ["today 45", "today 46", "today 47",
                                "today 48", "today 49"]
    payload = server.do_v1_logs(acousticslab.AL_ID, tail=60)
    assert "old 9" in payload["lines"]        # spans the previous day's file
    assert "skip me" not in payload["text"]


def test_v1_logs_handles_missing_log_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(acousticslab, "_LOG_DIR", str(tmp_path / "absent"))
    payload = server.do_v1_logs(acousticslab.AL_ID, tail=10)
    assert payload["lines"] == [] and payload["text"] == ""


# --------------------------------------------------------------------------- #
# result forwarder: protobuf decode + head-aware capability registration
# --------------------------------------------------------------------------- #
def _enc_varint(value):
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _enc_field_varint(number, value):
    return _enc_varint(number << 3 | 0) + _enc_varint(value)


def _enc_field_bytes(number, payload):
    return _enc_varint(number << 3 | 2) + _enc_varint(len(payload)) + payload


def _enc_field_float(number, value):
    import struct as _struct
    return _enc_varint(number << 3 | 5) + _struct.pack("<f", value)


def _infer_frame(seq, top_k, head_id="head-1", head_version=7,
                 capture_us=555_000_000, publish_us=1_789_000_000_000_000):
    frame = _enc_field_varint(1, seq)
    frame += _enc_field_varint(2, capture_us)
    for class_idx, label, prob in top_k:
        frame += _enc_field_bytes(4, _enc_field_varint(1, class_idx)
                                  + _enc_field_bytes(2, label.encode())
                                  + _enc_field_float(3, prob))
    if head_id is not None:
        frame += _enc_field_bytes(5, head_id.encode())
    frame += _enc_field_varint(7, publish_us)
    if head_version is not None:
        frame += _enc_field_varint(9, head_version)
    return _enc_field_bytes(11, frame)


def test_decode_envelope_reads_inference_and_ignores_audio():
    # Envelope variant 10 (audio) must be skipped, not misparsed.
    audio_only = _enc_field_bytes(10, b"\x00\x01")
    assert acousticslab.decode_envelope(audio_only) is None

    frame = acousticslab.decode_envelope(_infer_frame(
        42, [(3, "Yes", 0.875), (0, "_background_noise_", 0.125)]))
    assert frame["seq"] == 42
    assert frame["t_us_capture_monotonic"] == 555_000_000
    assert frame["t_us_publish_unix"] == 1_789_000_000_000_000
    assert frame["head_id"] == "head-1" and frame["head_version"] == 7
    assert frame["top_k"] == [{"class_idx": 3, "label": "Yes", "prob": 0.875},
                              {"class_idx": 0, "label": "_background_noise_",
                               "prob": 0.125}]


def test_decode_envelope_skips_unknown_fields_and_rejects_garbage():
    # Unknown fields at 16+ (the additive-evolution range) must be skipped.
    body = _enc_field_bytes(11, _enc_field_varint(1, 7)
                            + _enc_field_varint(16, 999))
    frame = acousticslab.decode_envelope(body)
    assert frame is not None and frame["seq"] == 7 and frame["top_k"] == []
    with pytest.raises(ValueError):
        acousticslab.decode_envelope(b"\xff\xff\xff")


def test_frame_to_payload_maps_classification_contract():
    frame = {"seq": 9, "t_us_capture_monotonic": 555_000_000,
             "t_us_publish_unix": 1_789_000_000_000_000,
             "head_id": "h", "head_version": 3,
             "top_k": [{"class_idx": 3, "label": "Yes", "prob": 0.9},
                       {"class_idx": 0, "label": "bg", "prob": 1.4}]}
    payload = acousticslab.frame_to_payload(frame)
    assert payload["task_type_name"] == "classification"
    assert payload["seq"] == 9 and payload["pts_us"] == 555_000_000
    assert payload["timestamp_ms"] == 1_789_000_000_000
    assert payload["classification"]["entries"][0] == {
        "score": 0.9, "class_id": 3, "class_name": "Yes"}
    # Out-of-range probabilities are clamped into the contract's [0, 1].
    assert payload["classification"]["entries"][1]["score"] == 1.0
    assert payload["summary"] == {"head_id": "h", "head_version": 3}


class _FakeConn:
    def __init__(self, chunks):
        self._buf = bytearray(b"".join(chunks))
        self.closed = False

    def recv(self, size):
        if not self._buf:
            return b""
        out = bytes(self._buf[:size])
        del self._buf[:size]
        return out

    def close(self):
        self.closed = True


class _FakeNotifySink:
    def __init__(self, fail=False):
        self.frames = []
        self.fail = fail
        self.closed = False

    def publish(self, frame):
        if self.fail:
            return False
        self.frames.append(frame)
        return True

    def close(self):
        self.closed = True

    def status(self):
        return {"enabled": True, "injected": len(self.frames), "errors": 0}


def _forwarder_harness(frames, labels=("Yes", "No"), notify_sink=None):
    import struct as _struct
    published = []
    capabilities = []
    payload = b"".join(
        _struct.pack("<I", len(frame)) + frame for frame in frames)
    forwarder = acousticslab.ResultForwarder(
        publish=lambda body, identity: published.append((body, identity)),
        capability_changed=capabilities.append,
        labels_provider=lambda: list(labels),
        connector=lambda path: _FakeConn([payload]),
        notify_sink=notify_sink or _FakeNotifySink())
    return forwarder, published, capabilities


def test_forwarder_registers_capability_and_publishes_every_frame():
    frames = [_infer_frame(1, [(3, "Yes", 0.9)]),
              _infer_frame(2, [(0, "_background_noise_", 0.7)])]
    forwarder, published, capabilities = _forwarder_harness(frames)
    forwarder._consume(forwarder._connector("ignored"))
    # The first frame registered the capability from the active head's labels.
    assert capabilities == [{
        "version": 1,
        "signals": [{"id": "classification", "type": "classification",
                     "classes": ["Yes", "No"], "supports_roi": False}],
    }]
    # BOTH frames published: the negative frame drives vigil's debounce decay.
    assert [body["seq"] for body, _ in published] == [1, 2]
    assert all(identity == {"id": "acousticslab", "trust": "in-process"}
               for _, identity in published)


def test_forwarder_head_swap_reregisters_capability():
    frames = [_infer_frame(1, [(3, "Yes", 0.9)], head_version=7),
              _infer_frame(2, [(3, "Yes", 0.8)], head_version=8)]
    forwarder, published, capabilities = _forwarder_harness(frames)
    forwarder._consume(forwarder._connector("ignored"))
    # One registration on the first frame plus one on the mid-stream head change.
    assert len(capabilities) == 2
    assert forwarder._head_marker == ("head-1", 8)


def test_forwarder_empty_labels_fail_closed():
    forwarder, published, capabilities = _forwarder_harness(
        [_infer_frame(1, [(3, "Yes", 0.9)])], labels=())
    forwarder._register()
    assert capabilities == [None]
    assert forwarder._registered is False


def test_forwarder_run_loop_recovers_from_connect_failure():
    import struct as _struct
    published = []
    calls = []
    blob = _infer_frame(5, [(1, "No", 0.6)])

    def connector(path):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("missing socket")
        return _FakeConn([_struct.pack("<I", len(blob)) + blob])

    forwarder = acousticslab.ResultForwarder(
        publish=lambda body, identity: published.append(body),
        capability_changed=lambda declaration: None,
        labels_provider=lambda: ["No"],
        connector=connector,
        notify_sink=_FakeNotifySink())
    forwarder._INITIAL_BACKOFF = 0.01
    forwarder._MAX_BACKOFF = 0.02
    forwarder.start()
    try:
        deadline = time.monotonic() + 2
        while not published and time.monotonic() < deadline:
            time.sleep(0.01)
        assert published and published[0]["seq"] == 5
    finally:
        forwarder.stop()
    assert len(calls) >= 2


# --------------------------------------------------------------------------- #
# legacy notify injection (system notify-server distribution reuse)
# --------------------------------------------------------------------------- #
def test_encode_legacy_classification_exact_wire_bytes():
    body = acousticslab.encode_legacy_classification(
        timestamp_ms=1000, pts_us=0, source_id="acousticslab",
        entries=[{"score": 0.5, "class_id": 2, "class_name": "cat"}])
    # InferenceClassificationEntry: score(fixed32) class_id(varint) class_name.
    entry = b"\x0d\x00\x00\x00\x3f" + b"\x10\x02" + b"\x1a\x03cat"
    classification = b"\x0a" + bytes([len(entry)]) + entry
    expected = (b"\x10\xe8\x07"                      # timestamp_ms = 1000
                + b"\x22\x0cacousticslab"            # source_id
                + b"\x5a" + bytes([len(classification)]) + classification)
    assert body == expected
    # task_type (CLASSIFICATION=0) and model_id(0) stay off the wire.
    assert b"\x08" not in body[:1] and b"\x18" not in body


def test_encode_legacy_classification_omits_defaults_and_skips_empty_entries():
    body = acousticslab.encode_legacy_classification(
        timestamp_ms=0, pts_us=0, source_id="",
        entries=[{"score": 0.25}, {}, {"class_name": ""}])
    # Only the scored entry survives; no timestamp/source/pts fields.
    entry = b"\x0d\x00\x00\x80\x3e"
    assert body == b"\x5a" + bytes([2 + len(entry)]) + b"\x0a" + \
        bytes([len(entry)]) + entry


class _FakeSendConn:
    def __init__(self, fail=False):
        self.sent = bytearray()
        self.closed = False
        self.fail = fail

    def sendall(self, data):
        if self.fail:
            raise OSError("broken pipe")
        self.sent.extend(data)

    def close(self):
        self.closed = True


def test_legacy_notify_sink_frames_le32_and_counts():
    conn = _FakeSendConn()
    sink = acousticslab._LegacyNotifySink(path="p", connector=lambda path: conn)
    assert sink.publish(b"\x01\x02") is True
    assert bytes(conn.sent) == b"\x02\x00\x00\x00\x01\x02"
    assert sink.status()["injected"] == 1


def test_legacy_notify_sink_connect_failure_backs_off_then_recovers():
    attempts = []

    def connector(path):
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("missing")
        return _FakeSendConn()

    sink = acousticslab._LegacyNotifySink(path="p", connector=connector)
    assert sink.publish(b"x") is False
    assert sink.publish(b"x") is False  # still inside the retry window
    assert len(attempts) == 1 and sink.errors == 1
    sink._next_try = 0.0
    assert sink.publish(b"x") is True
    assert len(attempts) == 2 and sink.injected == 1


def test_legacy_notify_sink_send_failure_drops_and_reconnects():
    conns = [_FakeSendConn(fail=True), _FakeSendConn()]
    sink = acousticslab._LegacyNotifySink(
        path="p", connector=lambda path: conns.pop(0))
    assert sink.publish(b"x") is False
    assert sink.errors == 1
    sink._next_try = 0.0
    assert sink.publish(b"y") is True
    assert sink.injected == 1


def test_forwarder_notify_channel_survives_hub_publish_failure():
    sink = _FakeNotifySink()

    def failing_publish(body, identity):
        raise RuntimeError("hub down")

    blob = _infer_frame(1, [(3, "Yes", 0.9)])
    import struct as _struct
    forwarder = acousticslab.ResultForwarder(
        publish=failing_publish,
        capability_changed=lambda declaration: None,
        labels_provider=lambda: ["Yes"],
        connector=lambda path: _FakeConn(
            [_struct.pack("<I", len(blob)) + blob]),
        notify_sink=sink)
    forwarder._consume(forwarder._connector("ignored"))
    assert len(sink.frames) == 1
    frame = sink.frames[0]
    assert frame.startswith(b"\x10")  # timestamp_ms leads the wire body
    assert b"acousticslab" in frame and b"Yes" in frame
    assert forwarder.status()["published"] == 0


def test_forwarder_hub_publish_survives_notify_failure():
    sink = _FakeNotifySink(fail=True)
    forwarder, published, _ = _forwarder_harness(
        [_infer_frame(1, [(3, "Yes", 0.9)])], notify_sink=sink)
    forwarder._consume(forwarder._connector("ignored"))
    assert [body["seq"] for body, _ in published] == [1]
    status = forwarder.status()
    assert status["published"] == 1
    assert status["notify"]["injected"] == 0

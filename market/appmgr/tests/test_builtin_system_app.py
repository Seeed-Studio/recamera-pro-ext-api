"""The firmware system card observes the engine without claiming its resources."""
import io
import json
import os
import sys
import tarfile
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from appmgr import builtin, installer, paths, server


def _engine(**updates):
    value = {"iEnable": 1, "sStatus": "running", "sModel": "detector.rknn",
             "iFPS": 20, "iActualFPS": 18}
    value.update(updates)
    return value


@pytest.fixture(autouse=True)
def isolate_builtin_cache():
    server._builtin_invalidate()
    yield
    server._builtin_invalidate()


def _view(monkeypatch, engine, operation=None):
    monkeypatch.setattr(builtin, "get_inference", lambda **kw: engine)
    return server._builtin_app_view(SimpleNamespace(active_for=lambda _: operation))


def test_system_card_uses_observed_engine_without_pid_or_result_identity_change(monkeypatch):
    item = _view(monkeypatch, _engine())
    assert item["id"] == "builtin" and item["system"] is True
    assert item["source"] == {"kind": "builtin", "id": "builtin"}
    assert item["running"] is True and item["status"] == "running"
    assert item["pid"] is None and item["instance"] is None
    assert item["runtime"]["pid"] is None
    assert item["builtin_inference"]["model"] == "detector.rknn"
    assert item["builtin_inference"]["fps"] == 20
    assert item["builtin_inference"]["actual_fps"] == 18
    assert item["actions"] == {"start": False, "stop": True, "restart": True,
                               "configure": True, "uninstall": False}


@pytest.mark.parametrize("engine,status,reason", [
    (_engine(sStatus="stopped", iActualFPS=0), "stopped", "enabled_but_stopped"),
    (_engine(sStatus="stopped", iActualFPS=0, bExternalHold=True),
     "waiting_resource", "external_npu_hold"),
    (_engine(sStatus="stopped", iActualFPS=0, bExternalHold=False),
     "stopped", "enabled_but_stopped"),
    (_engine(sStatus="stopped", iActualFPS=0, bExternalHold="true"),
     "stopped", "enabled_but_stopped"),
    (_engine(sStatus="error", iActualFPS=0), "failed", "inference_error"),
])
def test_enabled_bit_never_fakes_running_or_guesses_broker_hold(monkeypatch, engine, status, reason):
    item = _view(monkeypatch, engine)
    assert item["running"] is False
    assert item["status"] == status and item["reason"] == reason
    assert item["runtime"]["desired_state"] == "running"
    assert item["actions"]["stop"] is True
    assert item["actions"]["start"] is False


@pytest.mark.parametrize("engine_state", ["stopped", "error"])
def test_disabled_engine_can_start_even_if_external_hold_persists(monkeypatch, engine_state):
    item = _view(monkeypatch, _engine(iEnable=0, sStatus=engine_state, iActualFPS=0,
                                    bExternalHold=True))
    assert item["status"] == ("failed" if engine_state == "error" else "stopped")
    assert item["actions"]["start"] is True
    assert item["actions"]["stop"] is False
    assert item["actions"]["restart"] is False


@pytest.mark.parametrize("observed,operation,expected", [
    ("starting", None, "starting"), ("stopping", None, "stopping"),
    ("running", {"type": "restart"}, "starting"),
    ("running", {"type": "stop"}, "stopping"),
])
def test_engine_or_operation_transitions_disable_lifecycle_actions(monkeypatch, observed, operation, expected):
    item = _view(monkeypatch, _engine(sStatus=observed), operation)
    assert item["status"] == expected
    assert all(item["actions"][key] is False for key in ("start", "stop", "restart"))
    assert item["actions"]["configure"] is True


def test_offline_builtin_does_not_hide_installed_apps_or_take_lifecycle_lock(monkeypatch):
    reads = []
    def offline(**kwargs):
        reads.append(kwargs)
        raise builtin.BuiltinError("CGI unavailable")
    monkeypatch.setattr(builtin, "get_inference", offline)
    monkeypatch.setattr(server, "do_list", lambda: {
        "apps": [{"id": "cpu-app", "running": True, "pid": 123,
                  "observed_state": "ready"}], "running_apps": ["cpu-app"]})
    monkeypatch.setattr(server, "_operation_manager", lambda: SimpleNamespace(active_for=lambda _: None))
    monkeypatch.setattr(server, "_read_manifest", lambda _: {})
    monkeypatch.setattr(server, "busy_gate", lambda **kw: pytest.fail("list acquired mutation gate"))
    for _ in range(2):
        listing = server.do_v1_apps()
        assert [item["id"] for item in listing["apps"]] == ["builtin", "cpu-app"]
        assert listing["running_apps"] == ["cpu-app"]
        assert listing["apps"][0]["status"] == "unknown"
        assert listing["apps"][0]["builtin_inference"]["available"] is False
        assert listing["apps"][0]["actions"]["stop"] is False
    assert reads == [{"timeout": 1.0}]


@pytest.mark.parametrize("data", [{}, _engine(iEnable=None), _engine(sStatus="unknown")])
def test_incomplete_firmware_status_is_unknown(monkeypatch, data):
    item = _view(monkeypatch, data)
    assert item["status"] == "unknown" and item["running"] is False
    assert item["runtime"]["desired_state"] is None


def test_read_started_before_mutation_cannot_repopulate_status_cache(monkeypatch):
    calls = []
    def sample():
        calls.append(1)
        if len(calls) == 1:
            server._builtin_invalidate()
        return {"state": "running"}
    monkeypatch.setattr(builtin, "inference_status", sample)
    server._builtin_status()
    server._builtin_status()
    server._builtin_status()
    assert len(calls) == 2


def _config_firmware(monkeypatch, **updates):
    current = _engine(**updates)
    calls = []
    info = {"algorithm": "yolov5", "classes": ["person"],
            "metrics": {"confidence": 0.25, "iou": 0.45, "max_obj": 100}}
    def request(method, path, body=None):
        calls.append((method, path, json.loads(body) if body else None))
        if method == "GET" and path.startswith("/model/inference?"):
            return dict(current)
        if method == "GET" and path.startswith("/model/info?"):
            return info
        return {"code": 0}
    monkeypatch.setattr(builtin, "_request", request)
    return calls


def test_threshold_only_change_uses_explicit_restart_after_full_info_write(monkeypatch):
    calls = _config_firmware(monkeypatch)
    result = builtin.set_config({"confidence": 0.5, "fps": 20})
    writes = [call for call in calls if call[0] == "POST"]
    assert writes == [
        ("POST", "/model/info?File-name=detector.rknn",
         {"algorithm": "yolov5", "classes": ["person"],
          "metrics": {"confidence": 0.5, "iou": 0.45, "max_obj": 100}}),
        ("POST", "/model/inference-restart?id=0", None),
    ]
    assert result["restarted"] is True


def test_model_switch_updates_target_metrics_and_queues_only_one_reload(monkeypatch):
    calls = _config_firmware(monkeypatch, bExternalHold=True)
    result = builtin.set_config({"model": "new.rknn", "confidence": 0.5, "fps": 15})
    writes = [call for call in calls if call[0] == "POST"]
    assert writes[0][1] == "/model/info?File-name=new.rknn"
    assert writes[1:] == [("POST", "/model/inference?id=0", {"sModel": "new.rknn", "iFPS": 15})]
    assert result["restarted"] is True


def test_disabled_threshold_change_never_enables_or_restarts(monkeypatch):
    calls = _config_firmware(monkeypatch, iEnable=0, sStatus="stopped")
    result = builtin.set_config({"confidence": 0.5})
    assert len([call for call in calls if call[0] == "POST"]) == 1
    assert result["restarted"] is False


def test_config_does_not_write_after_status_read_failure(monkeypatch):
    calls = []
    def offline(method, path, body=None):
        calls.append(method)
        raise builtin.BuiltinError("CGI unavailable")
    monkeypatch.setattr(builtin, "_request", offline)
    with pytest.raises(builtin.BuiltinError):
        builtin.set_config({"confidence": 0.5})
    assert calls == ["GET"]


def test_partial_config_failure_invalidates_observation_cache(monkeypatch):
    monkeypatch.setattr(server, "busy_gate", lambda **kwargs: nullcontext())
    def failed(_):
        raise builtin.BuiltinError("reload response lost after write")
    monkeypatch.setattr(builtin, "set_config", failed)
    server._builtin_status_probe = (0, {"state": "running"})
    with pytest.raises(builtin.BuiltinError):
        server.do_set_config("builtin", {"confidence": 0.5})
    assert server._builtin_status_probe is None


def test_reserved_builtin_package_is_rejected_before_extraction():
    data = json.dumps({"id": "builtin", "version": "1.0.0"}).encode()
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as tar:
        member = tarfile.TarInfo("manifest.json")
        member.size = len(data)
        tar.addfile(member, io.BytesIO(data))
    archive.seek(0)
    with tarfile.open(fileobj=archive, mode="r:gz") as tar:
        with pytest.raises(installer.InstallError, match="reserved"):
            installer._read_manifest_from_tar(tar)


@pytest.mark.parametrize("remove", [installer.uninstall, server.do_uninstall, server.do_v1_delete])
def test_system_uninstall_rejected_even_with_stale_package_directory(tmp_path, monkeypatch, remove):
    monkeypatch.setattr(paths, "APPS_DIR", str(tmp_path))
    retained = tmp_path / "builtin" / "sentinel"
    retained.parent.mkdir()
    retained.write_text("must remain")
    monkeypatch.setattr(server, "busy_gate", lambda **kw: pytest.fail("system uninstall acquired gate"))
    with pytest.raises((ValueError, installer.InstallError), match="cannot be uninstalled"):
        remove("builtin")
    assert retained.read_text() == "must remain"

"""Firmware defaults, ordinary store upgrades, and persistent user choices."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from appmgr import (config, default_apps, installer, paths, pythonenv,
                    server, state, supervisor)
from market.packaging.build import build


REPO = Path(__file__).resolve().parents[3]


@pytest.fixture
def layout(tmp_path, monkeypatch):
    for name, leaf in (("APPS_DIR", "apps"), ("APPMGR_DIR", "manager"),
                       ("APPDATA_DIR", "appdata"), ("VENVS_DIR", "venvs")):
        directory = tmp_path / leaf
        directory.mkdir()
        monkeypatch.setattr(paths, name, str(directory))
    monkeypatch.setattr(paths, "STATE_FILE", str(tmp_path / "apps/state.json"))
    monkeypatch.setattr(paths, "KIT_DIR", str(REPO / "kit"))
    monkeypatch.setattr(paths, "ALLOWED_PKG_ROOTS", (str(tmp_path / "uploads"),))
    (tmp_path / "uploads").mkdir()
    monkeypatch.setattr(paths, "REQUIRE_SIGNATURE", True)
    monkeypatch.setattr(paths, "OWNER_KEYS_DIR", str(tmp_path / "no-owner-keys"))
    monkeypatch.setenv("APPMGR_PLATFORM_PYTHON", sys.executable)
    monkeypatch.setenv("APPMGR_PLATFORM_ARCH", "aarch64")
    # Host lacks the target's SDK library; application logic is tested separately.
    monkeypatch.setattr(pythonenv, "_probe_imports", lambda *args: None)
    for name in ("_result_hub_instance", "_coordinator_instance", "_coordinator_layout"):
        monkeypatch.setattr(server, name, None)
    monkeypatch.setattr(server, "_service_stopping", False)
    firmware = tmp_path / "firmware"
    firmware.mkdir()
    firmware.chmod(0o755)
    monkeypatch.setattr(default_apps, "FIRMWARE_DIR", str(firmware))
    legacy = tmp_path / "legacy-motion"
    legacy.mkdir()
    monkeypatch.setattr(default_apps, "LEGACY_MOTION_DIR", str(legacy))
    monkeypatch.setattr(default_apps, "LEGACY_MOTION_PID", str(legacy / "missing.pid"))
    package = Path(build(str(REPO / "apps/motion-detect"), str(firmware)))
    (firmware / "index.json").write_text(json.dumps({
        "version": 1, "apps": [{"id": "motion-detect", "package": package.name,
                                  "sha256": hashlib.sha256(package.read_bytes()).hexdigest()}],
    }))
    for member in firmware.iterdir():
        member.chmod(0o644)
    server.cache_clear()
    return tmp_path, package


def _seed():
    events = []
    default_apps.reconcile(install=server.do_install,
                           audit=lambda action, **kw: events.append((action, kw)))
    assert not [event for event in events if "failed" in event[0]], events
    return events


def test_first_preinstall_is_a_normal_installed_stopped_application(layout, monkeypatch):
    _, package = layout
    monkeypatch.setattr(supervisor, "start", lambda *a, **k: pytest.fail("preinstall started app"))
    info = installer.inspect(str(package))
    assert info["signature"] == {"signed": False, "verified": True,
                                 "signer_kind": "firmware", "firmware_app_id": "motion-detect"}
    assert info["preflight"]["requires_developer_mode"] is False
    _seed()
    assert state.get_app("motion-detect")["desired_state"] == "stopped"
    assert state.get_app("motion-detect")["observed_state"] == "stopped"
    assert installer.installed_release_id("motion-detect")
    listing = server.do_v1_apps()["apps"]
    motion = [item for item in listing if item["id"] == "motion-detect"]
    assert len(motion) == 1
    assert motion[0]["version"] == "1.1.0"
    assert motion[0]["status"] == "stopped"
    assert motion[0].get("system") is not True
    settings = server.do_get_config("motion-detect")
    assert settings["id"] == "motion-detect"
    assert settings["config_schema"]["groups"]
    visualization = server.do_get_app_visualization("motion-detect")
    assert visualization["stream_burn_in"]["supported"] is False
    assert visualization["stream_burn_in"]["status"] == "unsupported"


def test_firmware_trust_does_not_follow_a_copied_archive(layout):
    root, package = layout
    copied = root / "uploads" / package.name
    shutil.copyfile(package, copied)
    with pytest.raises(installer.InstallError, match="[Ss]ignature|unsigned"):
        installer.inspect(str(copied))


@pytest.mark.parametrize("target", ["package", "index", "symlink", "writable"])
def test_tampered_firmware_defaults_are_rejected(layout, target):
    root, package = layout
    if target == "package":
        with package.open("ab") as stream:
            stream.write(b"changed")
    elif target == "index":
        index = root / "firmware/index.json"
        data = json.loads(index.read_text())
        data["apps"][0]["id"] = "impostor"
        index.write_text(json.dumps(data))
    elif target == "symlink":
        original = root / "archive.tar.gz"
        package.rename(original)
        package.symlink_to(original)
    else:
        package.chmod(0o666)
    with pytest.raises((installer.InstallError, ValueError)):
        installer.inspect(str(package))


@pytest.mark.parametrize("desired", ["stopped", "running"])
def test_reboot_retains_the_user_choice_without_reinstall(layout, desired):
    _seed()
    state.set_desired("motion-detect", desired, launch_mode="managed")
    config.write_user_config("motion-detect", {"sq_diff_threshold": 600})
    default_apps.reconcile(install=lambda *a: pytest.fail("reinstalled default"),
                           audit=lambda *a, **kw: None)
    assert state.get_app("motion-detect")["desired_state"] == desired
    assert config.load_user_config("motion-detect")["sq_diff_threshold"] == 600


def test_uninstall_is_not_undone_by_preinstall_on_reboot(layout):
    _seed()
    server.do_uninstall("motion-detect")
    _seed()
    assert not Path(paths.app_dir("motion-detect")).exists()


@pytest.mark.parametrize("desired", ["stopped", "running"])
def test_legacy_configuration_and_explicit_choice_migrate(layout, desired):
    root, _ = layout
    (root / "legacy-motion/config.json").write_text(json.dumps({
        "sq_diff_threshold": 800, "mqtt_host": "existing-broker",
        "mqtt_user": "ha", "mqtt_pass": "secret", "http_port": 8099,
    }))
    (root / "manager/motion-detect.json").write_text(json.dumps({"desired": desired}))
    _seed()
    values = config.load_user_config("motion-detect")
    assert values == {"sq_diff_threshold": 800.0, "mqtt_host": "existing-broker",
                      "mqtt_user": "ha", "mqtt_pass": "secret"}
    assert state.get_app("motion-detect")["desired_state"] == desired


def test_failed_migration_retries_after_code_was_already_installed(layout, monkeypatch):
    root, _ = layout
    (root / "legacy-motion/config.json").write_text('{"mqtt_host":"existing-broker"}')
    migrate = default_apps._migrate_motion_config
    def fail_once(*args):
        raise OSError("power cut during migration")
    monkeypatch.setattr(default_apps, "_migrate_motion_config", fail_once)
    default_apps.reconcile(install=server.do_install, audit=lambda *a, **kw: None)
    assert Path(paths.app_dir("motion-detect")).is_dir()
    monkeypatch.setattr(default_apps, "_migrate_motion_config", migrate)
    _seed()
    assert config.load_user_config("motion-detect")["mqtt_host"] == "existing-broker"


def test_a_stale_legacy_pidfile_cannot_stop_an_unrelated_process(layout, monkeypatch):
    root, _ = layout
    pidfile = root / "legacy-motion/stale.pid"
    pidfile.write_text(str(os.getpid()))
    monkeypatch.setattr(default_apps, "LEGACY_MOTION_PID", str(pidfile))
    monkeypatch.setattr(default_apps.os, "kill",
                        lambda *args: pytest.fail("signaled an unrelated process"))
    default_apps._retire_legacy_motion()


def test_verified_legacy_daemon_is_stopped(layout, monkeypatch):
    root, _ = layout
    code = root / "legacy-code"
    code.mkdir()
    script = code / "motion_daemon.py"
    script.write_text("import time\nprint('ready', flush=True)\ntime.sleep(30)\n")
    process = subprocess.Popen([sys.executable, "motion_daemon.py"], cwd=code,
                               stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "ready"
        pidfile = root / "legacy-motion/run.pid"
        pidfile.write_text(str(process.pid))
        monkeypatch.setattr(default_apps, "LEGACY_MOTION_PID", str(pidfile))
        monkeypatch.setattr(default_apps, "LEGACY_MOTION_CODE_DIR", str(code))
        default_apps._retire_legacy_motion()
        assert process.wait(timeout=2) < 0
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=2)
        process.stdout.close()


def test_store_signed_upgrade_preserves_config_and_ignores_older_firmware(layout, monkeypatch):
    root, _ = layout
    _seed()
    config.write_user_config("motion-detect", {"sq_diff_threshold": 700})
    source = root / "new-source"
    shutil.copytree(REPO / "apps/motion-detect", source)
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["version"] = "1.2.0"
    manifest["release"]["sequence"] = 3
    manifest_path.write_text(json.dumps(manifest))
    package = build(str(source), str(root / "uploads"))
    private, public = root / "private.pem", root / "public.pem"
    subprocess.run(["openssl", "ecparam", "-name", "prime256v1", "-genkey",
                    "-noout", "-out", str(private)], check=True, capture_output=True)
    subprocess.run(["openssl", "pkey", "-in", str(private), "-pubout", "-out", str(public)],
                   check=True, capture_output=True)
    public.chmod(0o644)
    signature = base64.b64encode(subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(private), package],
        check=True, capture_output=True).stdout).decode()
    monkeypatch.setattr(paths, "RELEASE_PUBKEY", str(public))
    result = server.do_install(package, signature)
    assert result["signature"]["verified"] is True
    _seed()
    current = json.loads(Path(paths.app_dir("motion-detect"), "manifest.json").read_text())
    assert current["version"] == "1.2.0"
    assert config.load_user_config("motion-detect")["sq_diff_threshold"] == 700
    assert state.get_app("motion-detect")["desired_state"] == "stopped"

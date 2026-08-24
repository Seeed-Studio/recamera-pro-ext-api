"""Installer integration tests for strict v2 metadata and code/env rollback."""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tarfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from appmgr import installer, manifest as contract, paths, pythonenv  # noqa: E402


def manifest_v2(version="1.0.0", sequence=1):
    return {
        "manifest_version": 2,
        "id": "v2-app", "name": "V2 App", "version": version,
        "type": "self-hosted", "entry": "src/main.py",
        "release": {"sequence": sequence, "channel": "stable"},
        "compatibility": {
            "platform_profile": contract.DEFAULT_PLATFORM_PROFILE,
            "arch": "aarch64", "python": "==3.11.*",
        },
        "python": {
            "runtime_profile": "system-cp311-rknn232", "isolation": "per-release",
            "wheels": [], "imports": [],
        },
        "artifacts": [], "config_schema": {"revision": 1, "groups": []},
        "resources": {"claims": [
            {"name": "camera.frames", "mode": "shared", "required": True},
            {"name": "npu.rknn", "mode": "scheduled", "required": True},
        ]},
        "permissions": {
            "sdk": ["frame.read", "npu.infer"],
            "filesystem": {"read": ["app"], "write": ["appdata", "tmp"]},
            "network": {"listen": [], "outbound": []},
        },
        "health": {
            "protocol": "kit-health-v1", "startup_timeout_sec": 30,
            "stabilization_sec": 1, "liveness_interval_sec": 10,
            "liveness_failures": 3,
            "restart": {"policy": "on-failure", "max_attempts": 3,
                        "window_sec": 60, "backoff_sec": [1, 2]},
        },
        "instances": {"max": 1, "config_scope": "app", "data_scope": "app",
                      "endpoint_mode": "allocated"},
        "capabilities": ["video", "detection"],
    }


def package_v2(path, *, version="1.0.0", sequence=1, marker="ONE",
               tamper_payload=False):
    manifest = manifest_v2(version, sequence)
    manifest_bytes = contract.canonical_json(manifest)
    original_entry = f"# {marker}\n".encode()
    files = {"manifest.json": manifest_bytes, "src/main.py": original_entry,
             "marker.txt": marker.encode()}
    records = {
        name: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        for name, data in files.items()
    }
    lock, bom = contract.make_release_metadata(manifest, records)
    if tamper_payload:
        files["src/main.py"] += b"# changed after lock\n"
    files[contract.BOM_PATH] = bom
    files[contract.RELEASE_LOCK_PATH] = contract.canonical_json(lock)
    with tarfile.open(path, "w:gz") as archive:
        for name, data in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(data))
    return path, manifest, lock


def invalid_v2_package(path):
    value = manifest_v2()
    value.pop("permissions")
    files = {"manifest.json": json.dumps(value).encode(), "src/main.py": b"# bad\n"}
    with tarfile.open(path, "w:gz") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return path


def package_v1(path, *, version="legacy-2"):
    files = {
        "manifest.json": json.dumps({
            "id": "v2-app", "name": "Legacy", "version": version, "entry": "app.py",
        }).encode(),
        "app.py": b"# legacy\n",
    }
    with tarfile.open(path, "w:gz") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return path


@pytest.fixture
def layout(tmp_path, monkeypatch):
    apps = tmp_path / "apps"
    state = tmp_path / "state"
    apps.mkdir()
    state.mkdir()
    monkeypatch.setattr(paths, "APPS_DIR", str(apps))
    monkeypatch.setattr(paths, "APPMGR_DIR", str(state))
    monkeypatch.setattr(paths, "VENVS_DIR", str(tmp_path / "venvs"))
    monkeypatch.setattr(paths, "ALLOWED_PKG_ROOTS", (str(tmp_path),))
    monkeypatch.setattr(paths, "REQUIRE_SIGNATURE", True)
    monkeypatch.setenv("APPMGR_PLATFORM_ARCH", "aarch64")
    monkeypatch.setenv("APPMGR_PLATFORM_PYTHON", sys.executable)
    monkeypatch.setenv("APPMGR_WHEELHOUSE_DIR", str(tmp_path / "wheelhouse"))
    return tmp_path


def installed_version(base):
    with open(base / "apps" / "v2-app" / "manifest.json") as source:
        return json.load(source)["version"]


def test_valid_v2_install_builds_and_activates_matching_environment(layout):
    package, _, lock = package_v2(str(layout / "one.tar.gz"))
    info = installer.inspect(package, allow_unsigned=True)
    assert info["preflight"]["release_id"] == lock["release_id"]
    assert info["preflight"]["resources"]["claims"][1]["mode"] == "scheduled"
    assert info["preflight"]["developer_mode_allowed"] is True

    app_id, manifest = installer.install(package, allow_unsigned=True)
    assert (app_id, manifest["version"]) == ("v2-app", "1.0.0")
    assert installed_version(layout) == "1.0.0"
    assert pythonenv.current_release_id(app_id) == lock["release_id"]
    assert os.path.isfile(pythonenv.current_python(app_id))


def test_schema_and_release_bom_fail_before_existing_release_changes(layout):
    installed = layout / "apps" / "v2-app"
    installed.mkdir()
    (installed / "marker.txt").write_text("OLD")

    invalid = invalid_v2_package(str(layout / "invalid.tar.gz"))
    with pytest.raises(installer.InstallError, match="missing required.*permissions"):
        installer.install(invalid, allow_unsigned=True)
    assert (installed / "marker.txt").read_text() == "OLD"

    tampered, _, _ = package_v2(str(layout / "tampered.tar.gz"), tamper_payload=True)
    with pytest.raises(installer.InstallError, match="does not match payload"):
        installer.install(tampered, allow_unsigned=True)
    assert (installed / "marker.txt").read_text() == "OLD"
    assert not any(".stage." in name for name in os.listdir(layout / "apps"))


def test_incompatible_arch_is_rejected_before_env_or_code_staging(layout, monkeypatch):
    package, _, _ = package_v2(str(layout / "one.tar.gz"))
    monkeypatch.setenv("APPMGR_PLATFORM_ARCH", "x86_64")
    with pytest.raises(installer.InstallError, match="compatibility.arch"):
        installer.install(package, allow_unsigned=True)
    assert list((layout / "apps").iterdir()) == []
    assert not (layout / "venvs").exists()


def test_v2_release_sequence_prevents_downgrade_and_equivocation(layout):
    current, _, _ = package_v2(
        str(layout / "current.tar.gz"), version="2.0.0", sequence=2, marker="CURRENT")
    installer.install(current, allow_unsigned=True)
    active_before = pythonenv.current_release_id("v2-app")

    downgrade, _, _ = package_v2(
        str(layout / "downgrade.tar.gz"), version="1.0.0", sequence=1, marker="OLD")
    with pytest.raises(installer.InstallError, match="release downgrade refused"):
        installer.install(downgrade, allow_unsigned=True)

    equivocation, _, _ = package_v2(
        str(layout / "equivocation.tar.gz"), version="2.0.1", sequence=2, marker="OTHER")
    with pytest.raises(installer.InstallError, match="sequence equivocation refused"):
        installer.install(equivocation, allow_unsigned=True)
    assert installed_version(layout) == "2.0.0"
    assert pythonenv.current_release_id("v2-app") == active_before


def test_post_switch_baseexception_restores_code_and_environment(layout, monkeypatch):
    first_package, _, first_lock = package_v2(str(layout / "one.tar.gz"))
    installer.install(first_package, allow_unsigned=True)
    second_package, _, second_lock = package_v2(
        str(layout / "two.tar.gz"), version="2.0.0", sequence=2, marker="TWO")
    real_activate = pythonenv.activate_environment

    def activate_then_fail(candidate):
        real_activate(candidate)
        if candidate.release_id == second_lock["release_id"]:
            raise KeyboardInterrupt("injected after current switch")

    monkeypatch.setattr(pythonenv, "activate_environment", activate_then_fail)
    with pytest.raises(KeyboardInterrupt, match="injected"):
        installer.install(second_package, allow_unsigned=True)
    assert installed_version(layout) == "1.0.0"
    assert pythonenv.current_release_id("v2-app") == first_lock["release_id"]
    assert not os.path.exists(os.path.join(
        paths.venv_dir("v2-app"), "releases", second_lock["release_id"]))


def test_restore_prev_rolls_code_and_interpreter_together(layout):
    first_package, _, first_lock = package_v2(str(layout / "one.tar.gz"))
    installer.install(first_package, allow_unsigned=True)
    second_package, _, _ = package_v2(
        str(layout / "two.tar.gz"), version="2.0.0", sequence=2, marker="TWO")
    installer.install(second_package, allow_unsigned=True)
    assert installed_version(layout) == "2.0.0"

    assert installer.restore_prev("v2-app") is True
    assert installed_version(layout) == "1.0.0"
    assert pythonenv.current_release_id("v2-app") == first_lock["release_id"]
    assert installer.restore_prev("v2-app") is False


def test_v1_upgrade_rollback_does_not_consume_stale_v2_env_record(layout):
    first_package, _, first_lock = package_v2(str(layout / "one.tar.gz"))
    installer.install(first_package, allow_unsigned=True)
    legacy = package_v1(str(layout / "legacy.tar.gz"))
    installer.install(legacy, allow_unsigned=True)
    assert installed_version(layout) == "legacy-2"
    assert pythonenv.current_release_id("v2-app") == first_lock["release_id"]

    assert installer.restore_prev("v2-app") is True
    assert installed_version(layout) == "1.0.0"
    assert pythonenv.current_release_id("v2-app") == first_lock["release_id"]

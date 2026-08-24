"""Offline, immutable per-release Python environment tests."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import zipfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from appmgr import manifest as contract, paths, pythonenv  # noqa: E402


def make_wheel(path, *, project="demo-dep", version="1.0.0", value="one",
               extra=None):
    dist = project.replace("-", "_")
    dist_info = f"{dist}-{version}.dist-info"
    members = {
        f"{dist}/__init__.py": f"VALUE = {value!r}\n".encode(),
        f"{dist_info}/METADATA": (
            f"Metadata-Version: 2.1\nName: {project}\nVersion: {version}\n\n").encode(),
        f"{dist_info}/WHEEL": b"Wheel-Version: 1.0\nTag: py3-none-any\n",
        f"{dist_info}/RECORD": b"",
    }
    members.update(extra or {})
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    raw = path.read_bytes()
    return {
        "name": project,
        "version": version,
        "filename": path.name,
        "file": "wheels/" + path.name,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size": len(raw),
        "tags": ["py3-none-any"],
        "source": "bundled",
    }


def app_manifest(app_id, version, wheel):
    return {
        "manifest_version": 2,
        "id": app_id, "name": app_id, "version": version,
        "type": "self-hosted", "entry": "app.py",
        "release": {"sequence": int(version.split(".")[0]), "channel": "stable"},
        "compatibility": {
            "platform_profile": contract.DEFAULT_PLATFORM_PROFILE,
            "arch": "aarch64", "python": "==3.11.*",
        },
        "python": {
            "runtime_profile": "system-cp311-rknn232", "isolation": "per-release",
            "wheels": [wheel], "imports": [wheel["name"].replace("-", "_")],
        },
        "artifacts": [], "config_schema": {"revision": 1, "groups": []},
        "resources": {"claims": []},
        "permissions": {
            "sdk": [], "filesystem": {"read": ["app"], "write": ["appdata"]},
            "network": {"listen": [], "outbound": []},
        },
        "health": {
            "protocol": "kit-health-v1", "startup_timeout_sec": 30,
            "stabilization_sec": 0, "liveness_interval_sec": 10,
            "liveness_failures": 3,
            "restart": {"policy": "on-failure", "max_attempts": 2,
                        "window_sec": 60, "backoff_sec": [1]},
        },
        "instances": {"max": 1, "config_scope": "app", "data_scope": "app",
                      "endpoint_mode": "allocated"},
        "capabilities": [],
    }


def prepare_app(root, manifest, wheel_source):
    root.mkdir(parents=True)
    (root / "wheels").mkdir()
    destination = root / manifest["python"]["wheels"][0]["file"]
    destination.write_bytes(wheel_source.read_bytes())
    raw_manifest = contract.canonical_json(manifest)
    (root / "manifest.json").write_bytes(raw_manifest)
    (root / "app.py").write_text("# entry\n")
    records = {}
    for path in (root / "manifest.json", root / "app.py", destination):
        raw = path.read_bytes()
        relative = path.relative_to(root).as_posix()
        records[relative] = {"sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}
    lock, _ = contract.make_release_metadata(manifest, records)
    return lock


@pytest.fixture
def env_layout(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "VENVS_DIR", str(tmp_path / "venvs"))
    wheelhouse = tmp_path / "wheelhouse"
    return tmp_path, wheelhouse


def stage_release(base, wheelhouse, *, app_id="env-app", app_version="1.0.0",
                  dep_version="1.0.0", value="one", extra=None):
    wheel_path = base / f"demo_dep-{dep_version}-py3-none-any.whl"
    wheel = make_wheel(
        wheel_path, version=dep_version, value=value, extra=extra)
    manifest = app_manifest(app_id, app_version, wheel)
    app_dir = base / f"src-{app_id}-{app_version}"
    lock = prepare_app(app_dir, manifest, wheel_path)
    candidate = pythonenv.stage_environment(
        str(app_dir), manifest, lock, base_python=sys.executable,
        wheelhouse_root=str(wheelhouse))
    return manifest, lock, candidate


def read_value(interpreter):
    result = subprocess.run(
        [interpreter, "-I", "-c", "import demo_dep; print(demo_dep.VALUE)"],
        check=True, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1"})
    return result.stdout.strip()


def test_two_release_envs_coexist_activate_and_restore_exactly_once(env_layout):
    base, wheelhouse = env_layout
    _, lock1, first = stage_release(base, wheelhouse)
    assert pythonenv.current_python("env-app") is None
    activation1 = pythonenv.activate_environment(first)
    first_python = pythonenv.current_python("env-app")
    assert activation1.previous_release_id is None
    assert read_value(first_python) == "one"

    _, lock2, second = stage_release(
        base, wheelhouse, app_version="2.0.0", dep_version="2.0.0", value="two")
    activation2 = pythonenv.activate_environment(second)
    second_python = pythonenv.current_python("env-app")
    assert activation2.previous_release_id == lock1["release_id"]
    assert read_value(second_python) == "two"
    assert read_value(first_python) == "one"
    assert lock1["release_id"] != lock2["release_id"]

    assert pythonenv.restore_previous("env-app") is True
    assert pythonenv.current_python("env-app") == first_python
    assert pythonenv.restore_previous("env-app") is False


def test_bad_bundled_digest_fails_without_changing_current(env_layout):
    base, wheelhouse = env_layout
    _, first_lock, first = stage_release(base, wheelhouse)
    pythonenv.activate_environment(first)
    active_before = pythonenv.current_release_id("env-app")

    wheel_path = base / "demo_dep-2.0.0-py3-none-any.whl"
    wheel = make_wheel(wheel_path, version="2.0.0", value="two")
    manifest = app_manifest("env-app", "2.0.0", wheel)
    app_dir = base / "corrupt-app"
    lock = prepare_app(app_dir, manifest, wheel_path)
    bundled = app_dir / wheel["file"]
    bundled.write_bytes(bundled.read_bytes() + b"tamper")
    with pytest.raises(pythonenv.PythonEnvError, match="size mismatch|admission mismatch"):
        pythonenv.stage_environment(
            str(app_dir), manifest, lock, base_python=sys.executable,
            wheelhouse_root=str(wheelhouse))
    assert pythonenv.current_release_id("env-app") == active_before == first_lock["release_id"]
    assert not any(".stage." in name for name in os.listdir(
        os.path.join(paths.venv_dir("env-app"), "releases")))


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"../escape.py": b"bad"}, "unsafe wheel member"),
        ({"demo_dep/native.so": b"\x7fELF\x02\x01" + b"\x00" * 12 + b"\x3e\x00"},
         "non-AArch64 ELF"),
    ],
)
def test_unsafe_wheel_payload_rolls_back_candidate(env_layout, extra, message):
    base, wheelhouse = env_layout
    with pytest.raises(pythonenv.PythonEnvError, match=message):
        stage_release(base, wheelhouse, extra=extra)
    releases = os.path.join(paths.venv_dir("env-app"), "releases")
    assert not os.path.exists(releases) or not os.listdir(releases)


def test_platform_owned_project_cannot_be_shadowed(env_layout):
    base, wheelhouse = env_layout
    wheel_path = base / "numpy-9.0.0-py3-none-any.whl"
    wheel = make_wheel(wheel_path, project="numpy", version="9.0.0")
    manifest = app_manifest("env-app", "1.0.0", wheel)
    app_dir = base / "numpy-app"
    lock = prepare_app(app_dir, manifest, wheel_path)
    with pytest.raises(pythonenv.PythonEnvError, match="platform-owned"):
        pythonenv.stage_environment(
            str(app_dir), manifest, lock, base_python=sys.executable,
            wheelhouse_root=str(wheelhouse))


def test_activation_failure_keeps_old_pointer_and_candidate_is_discardable(
        env_layout, monkeypatch):
    base, wheelhouse = env_layout
    _, first_lock, first = stage_release(base, wheelhouse)
    pythonenv.activate_environment(first)
    _, _, second = stage_release(
        base, wheelhouse, app_version="2.0.0", dep_version="2.0.0", value="two")
    real_switch = pythonenv._switch_current

    def fail_new(root, release_id):
        if release_id == second.release_id:
            raise OSError("injected switch failure")
        return real_switch(root, release_id)

    monkeypatch.setattr(pythonenv, "_switch_current", fail_new)
    with pytest.raises(OSError, match="injected"):
        pythonenv.activate_environment(second)
    assert pythonenv.current_release_id("env-app") == first_lock["release_id"]
    pythonenv.discard_candidate(second)
    assert not os.path.exists(second.final_dir)


def test_subprocess_environment_drops_host_pythonpath(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/host/must/not/leak")
    clean = pythonenv._clean_subprocess_env()
    assert "PYTHONPATH" not in clean
    assert clean["PYTHONNOUSERSITE"] == "1"


def test_wheelhouse_digest_directory_symlink_cannot_escape(env_layout):
    base, wheelhouse = env_layout
    wheel_path = base / "demo_dep-1.0.0-py3-none-any.whl"
    wheel = make_wheel(wheel_path)
    wheelhouse.mkdir()
    outside = base / "outside"
    outside.mkdir()
    os.symlink(outside, wheelhouse / wheel["sha256"])
    with pytest.raises(pythonenv.PythonEnvError, match="escapes configured root"):
        pythonenv.wheelhouse_path(wheel, wheelhouse_root=str(wheelhouse))

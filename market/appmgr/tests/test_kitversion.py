"""Compatibility metadata and preflight ordering tests."""
from __future__ import annotations

import io
import json
import os
import sys
import tarfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

from appmgr import installer, kitversion, manifest as contract, paths, server, supervisor  # noqa: E402


def _kit(tmp_path, source: str) -> str:
    root = tmp_path / "kit"
    root.mkdir(exist_ok=True)
    (root / "__init__.py").write_text(source, encoding="utf-8")
    return str(root)


def test_dual_track_metadata_and_version_boundaries(tmp_path):
    dual = _kit(tmp_path, '__version__ = "1.6.5"\n__legacy_version__ = "1.6.5"\n__api_version__ = "0.2"\n')
    assert kitversion.installed_version(dual) == "1.6.5"
    assert kitversion.installed_version(dual, api=True) == "0.2"
    assert kitversion.check({"kit": ">=1.6.4"}, dual) == "1.6.5"
    assert kitversion.check({"manifest_version": 2, "compatibility": {"kit_api": ">=0.2,<1"}}, dual) == "0.2"

    v1 = _kit(tmp_path, '__version__ = "1.6.5"\n')
    assert kitversion.check({"kit": ">=1.6.4"}, v1) == "1.6.5"
    v2 = _kit(tmp_path, '__api_version__ = "0.2"\n')
    assert kitversion.check({"manifest_version": 2, "compatibility": {"kit_api": ">=0.2,<1"}}, v2) == "0.2"

    legacy_unknown = _kit(tmp_path, '__version__ = "0.1"\n')
    assert kitversion.check({"kit": ">=0.1"}, legacy_unknown) == "0.1"
    with pytest.raises(kitversion.KitIncompatible):
        kitversion.check({"kit": ">=1.6.4"}, legacy_unknown)
    with pytest.raises(kitversion.KitIncompatible):
        kitversion.check({"manifest_version": 2, "compatibility": {"kit_api": ">=0.1"}}, legacy_unknown)


@pytest.mark.parametrize("requirement", ["", "wat", ">=1.2,wat", ">=1.2,<1.0", "==1.*", "!=1.*"])
def test_requirement_parser_rejects_bad_strings_and_supports_combinations(requirement):
    if requirement in ("==1.*", "!=1.*", ">=1.2,<1.0"):
        assert kitversion.parse_requirement(requirement)
    else:
        assert kitversion.parse_requirement(requirement) is None


@pytest.mark.parametrize(
    "source",
    [
        '__version__ = "1.6.5"\n__version__ = "1.6.6"\n',
        'value = "1.6.5"\n__version__ = value\n',
        'if True:\n    __version__ = "1.6.5"\n',
        '__version__, = ("1.6.5",)\n',
        '__version__ = "1.6.5"\n__version__ += ".1"\n',
        'from other import __version__\n',
    ],
)
def test_metadata_is_static_literal_only_and_never_executes(tmp_path, source):
    with pytest.raises(kitversion.KitIncompatible):
        kitversion.metadata(_kit(tmp_path, source))


def _tar_with_manifest(manifest: dict):
    payload = json.dumps(manifest).encode()
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        info = tarfile.TarInfo("manifest.json")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    stream.seek(0)
    return tarfile.open(fileobj=stream, mode="r")


def test_installer_rejects_kit_before_hashing_package_or_building_venv(monkeypatch):
    tar = _tar_with_manifest({"id": "demo", "kit": ">=99.0"})
    calls = []
    monkeypatch.setattr(installer.manifest_contract, "check_platform_compatibility", lambda _m: None)
    monkeypatch.setattr(installer, "_read_manifest_from_tar", lambda _t: {"id": "demo", "kit": ">=99.0"})
    monkeypatch.setattr(installer, "_package_records", lambda *_a: calls.append("records") or {})
    monkeypatch.setattr(installer.kitversion, "check", lambda _m: (_ for _ in ()).throw(kitversion.KitIncompatible("old kit")))
    with pytest.raises(installer.InstallError, match="kit compatibility"):
        installer._inspect_open_tar(tar, {})
    assert calls == []


def test_supervisor_rejects_kit_before_stop_build_or_spawn(tmp_path, monkeypatch):
    app_id = "demo"
    app_dir = tmp_path / app_id
    app_dir.mkdir()
    events = []
    monkeypatch.setattr(paths, "app_dir", lambda _id: str(app_dir))
    monkeypatch.setattr(supervisor, "is_running", lambda _id: None)
    monkeypatch.setattr(supervisor, "_load_manifest", lambda _id: {"id": app_id, "kit": ">=99.0"})
    monkeypatch.setattr(supervisor.kitversion, "check", lambda _m: (_ for _ in ()).throw(kitversion.KitIncompatible("old kit")))
    monkeypatch.setattr(supervisor, "stop", lambda *_a, **_k: events.append("stop"))
    monkeypatch.setattr(supervisor, "_build_cmd", lambda *_a: events.append("build") or [])
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda *_a, **_k: events.append("spawn"))
    with pytest.raises(supervisor.SupervisorError, match="kit compatibility"):
        supervisor.start(app_id)
    assert events == []


@pytest.mark.parametrize("operation", ["do_start", "do_restart", "do_switch", "do_activate"])
def test_server_rejects_incompatible_target_before_any_teardown_or_reservation(
    tmp_path, monkeypatch, operation,
):
    app_id = "demo"
    app_dir = tmp_path / app_id
    app_dir.mkdir()
    monkeypatch.setattr(paths, "APPS_DIR", str(tmp_path / "apps"))
    monkeypatch.setattr(paths, "APPMGR_DIR", str(tmp_path / "appmgr"))
    events = []
    monkeypatch.setattr(paths, "app_dir", lambda _id: str(app_dir))
    monkeypatch.setattr(server, "_read_manifest", lambda _id: {"id": app_id, "kit": ">=99.0"})
    monkeypatch.setattr(server.kitversion, "check", lambda _m: (_ for _ in ()).throw(kitversion.KitIncompatible("old kit")))
    monkeypatch.setattr(server, "_prepare_external_start", lambda *_a: events.append("prepare"))
    monkeypatch.setattr(server, "_stop_external", lambda *_a: events.append("stop"))
    monkeypatch.setattr(server, "_coordinator", lambda: events.append("coordinator"))
    monkeypatch.setattr(server.state, "get_active", lambda: events.append("active") or app_id)
    with pytest.raises(kitversion.KitIncompatible, match="old kit"):
        getattr(server, operation)(app_id)
    assert events == []

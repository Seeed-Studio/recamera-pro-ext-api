"""Model-path contract for the manifest-v2 Voice application.

The scheduled inference daemon authorizes the exact installed bundled RKNN
path.  These tests deliberately run with an unrelated cwd so a regression
cannot turn ``models/asr`` into ``/models/asr`` again.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import uuid
from pathlib import Path

import pytest

from kit import config as kit_config
from kit.errors import ConfigurationError


ROOT = Path(__file__).resolve().parents[2]
VOICE_DIR = ROOT / "apps" / "voice-transcribe"


def _load_voice_module():
    path = VOICE_DIR / "app.py"
    name = f"_voice_model_paths_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _manifest() -> dict:
    with (VOICE_DIR / "manifest.json").open(encoding="utf-8") as stream:
        return json.load(stream)


def _new_app(monkeypatch, app_root: Path):
    module = _load_voice_module()
    app = module.VoiceTranscribeApp()
    app._manifest = _manifest()
    app.asr_backend = "rk"
    monkeypatch.setattr(kit_config, "app_dir_of", lambda _app: os.fspath(app_root))
    for name in module._INFERENCE_SERVICE_ENVS:
        monkeypatch.delenv(name, raising=False)
    return module, app


def test_manifest_default_names_the_bundled_asset_directory():
    manifest = _manifest()
    items = {
        item["key"]: item
        for group in manifest["config_schema"]["groups"]
        for item in group["items"]
    }
    assert items["model_dir"]["default"] == "models/asr"
    assert {
        os.path.dirname(item["file"])
        for item in manifest["artifacts"]
        if item["kind"] == "rknn" and item["source"] == "bundled"
    } == {"models/asr"}


def test_managed_v2_model_dir_is_app_relative_not_cwd_relative(
        tmp_path, monkeypatch):
    app_root = tmp_path / "installed" / "voice-transcribe"
    bundled = app_root / "models" / "asr"
    bundled.mkdir(parents=True)
    unrelated = tmp_path / "unrelated-cwd"
    unrelated.mkdir()
    _module, app = _new_app(monkeypatch, app_root)
    monkeypatch.setenv("RECAMERA_INFERENCE_SERVICE_SOCK",
                       "/run/recamera/inferenced.sock")
    monkeypatch.chdir(unrelated)

    resolved = app._resolve_model_dir("models/asr")

    assert resolved == os.path.realpath(bundled)
    assert resolved != os.path.realpath(unrelated / "models" / "asr")


def test_managed_v2_rejects_an_unauthorized_model_override(
        tmp_path, monkeypatch):
    app_root = tmp_path / "installed" / "voice-transcribe"
    (app_root / "models" / "asr").mkdir(parents=True)
    external = tmp_path / "shared" / "asr"
    external.mkdir(parents=True)
    _module, app = _new_app(monkeypatch, app_root)
    monkeypatch.setenv("RECAMERA_INFERENCE_SERVICE_SOCK",
                       "/run/recamera/inferenced.sock")

    with pytest.raises(ConfigurationError) as caught:
        app._resolve_model_dir(os.fspath(external))

    assert caught.value.code == "unauthorized_model_path"
    assert caught.value.operation == "voice.configure"
    assert caught.value.details["configured"] == os.path.realpath(external)
    assert caught.value.details["authorized"] == os.path.realpath(
        app_root / "models" / "asr")
    assert "bundled artifact directory" in str(caught.value)


def test_standalone_launch_keeps_legacy_shared_directory_fallback(
        tmp_path, monkeypatch):
    app_root = tmp_path / "standalone" / "voice-transcribe"
    app_root.mkdir(parents=True)
    module, app = _new_app(monkeypatch, app_root)
    shared = tmp_path / "legacy-shared" / "asr"
    shared.mkdir(parents=True)
    module.SHARED_MODEL_DIR = os.fspath(shared)
    module.STAGING_MODEL_DIR = os.fspath(tmp_path / "absent-staging")

    assert app._resolve_model_dir(None) == os.path.realpath(shared)


def test_standalone_package_also_resolves_bundle_without_cwd_dependency(
        tmp_path, monkeypatch):
    app_root = tmp_path / "standalone" / "voice-transcribe"
    bundled = app_root / "models" / "asr"
    bundled.mkdir(parents=True)
    unrelated = tmp_path / "elsewhere"
    unrelated.mkdir()
    _module, app = _new_app(monkeypatch, app_root)
    monkeypatch.chdir(unrelated)

    assert app._resolve_model_dir("models/asr") == os.path.realpath(bundled)

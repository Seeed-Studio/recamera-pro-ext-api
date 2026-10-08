import importlib.util
import json
import os
import socket
import subprocess
import sys
import types
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("INFERENCE_ENGINE_ROOT"),
    reason="Set INFERENCE_ENGINE_ROOT to the pinned engine checkout",
)

APP_DIR = Path(__file__).resolve().parents[1]
SDK_ROOT = APP_DIR.parents[1]
ENGINE_ROOT = Path(os.environ.get("INFERENCE_ENGINE_ROOT", "/__unconfigured_engine__"))


def test_real_appmgr_config_is_read_by_kit_and_survives_app_directory_change(tmp_path):
    sdk = SDK_ROOT
    env = dict(os.environ)
    env.update(
        PYTHONPATH=os.pathsep.join((str(sdk / "market"), str(sdk), str(ENGINE_ROOT))),
        APPMGR_APPS_DIR=str(tmp_path / "apps"),
        APPMGR_APPDATA_DIR=str(tmp_path / "appdata"),
        INFERENCE_RUNTIME_PROFILE="rv1126b",
    )
    script = r"""
import json
from pathlib import Path
from appmgr import config, manifest, paths
from kit import config as kit_config
from inference.edge.settings import EdgeSettings

template = json.loads(Path(TEMPLATE).read_text())
manifest.validate_manifest(template, allow_v1=False)
items = config.schema_specs(template)
assert items["api_token"]["type"] == "password"
assert all(item["apply"] == "restart" for item in items.values())
defaults = config.schema_defaults(template)
assert defaults["host"] == "0.0.0.0" and defaults["port"] == 9001
clean, errors = config.validate_config(template, {
    "host": "0.0.0.0", "port": 19001, "api_token": "test-secret", "max_fps": 4,
    "workflow_id": "saved-camera", "video_source": "rtsp",
    "rtsp_url": "rtsp://user:private@camera.invalid/live", "workflow_fps": 2,
    "workflow_parameters": '{"confidence": 0.6, "classes": ["person"]}',
})
assert not errors, errors
config.write_user_config(template["id"], clean)
for name in ("old-release", "new-release"):
    app_dir = Path(paths.APPS_DIR) / name
    app_dir.mkdir(parents=True)
    (app_dir / "manifest.json").write_text(json.dumps(template))
    effective = kit_config.effective_config(str(app_dir))
    storage = Path(kit_config.appdata_root()) / template["id"]
    settings = EdgeSettings.from_app_config(effective, model_root=app_dir / "models", storage_root=storage)
    assert settings.host == "0.0.0.0" and settings.port == 19001
    assert settings.api_token == "test-secret" and settings.max_fps == 4
    assert settings.workflow_id == "saved-camera" and settings.workflow_autostart is True
    assert settings.video_source == "rtsp" and settings.workflow_fps == 2
    assert "private@camera" not in repr(settings)
    assert settings.workflow_parameters == {"confidence": 0.6, "classes": ["person"]}
    assert settings.storage_root == Path(paths.appdata_dir(template["id"]))
    assert Path(kit_config.user_config_path(str(app_dir))) == storage / "config.json"
for bad in ({"max_fps": 61}, {"port": False}, {"storage_root": "/system"}):
    _, errors = config.validate_config(template, bad)
    assert errors, bad
print("persisted schema and Kit mapping verified")
"""
    script = (
        "TEMPLATE = "
        + repr(str(APP_DIR / "manifest.json"))
        + "\n"
        + script
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr

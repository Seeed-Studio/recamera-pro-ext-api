import ast
import os
import secrets
from pathlib import Path

import pytest


@pytest.fixture
def access(tmp_path, monkeypatch):
    sdk = Path(__file__).resolve().parents[3]
    monkeypatch.syspath_prepend(str(sdk / "market"))
    monkeypatch.syspath_prepend(str(sdk))
    from appmgr import config, paths

    monkeypatch.setattr(paths, "APPS_DIR", str(tmp_path / "apps"))
    monkeypatch.setattr(paths, "APPDATA_DIR", str(tmp_path / "appdata"))
    path = Path(__file__).resolve().parents[1] / "app.py"
    # Only the stdlib access helper is needed here. Importing the application
    # entry also requires its private venv/Kit; lifecycle tests cover that path.
    tree = ast.parse(path.read_text())
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name == "managed_access_config")
    namespace = {"secrets": secrets, "InferenceEdgeApp": type("App", (), {"id": "inference-rv1126b"})}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), namespace)
    return namespace["managed_access_config"], config, "inference-rv1126b"


def test_generated_token_is_reused_and_does_not_replace_other_settings(access):
    configure, config, app_id = access
    config.write_user_config(app_id, {"workflow_id": "saved", "port": 19001})
    first = configure({})
    second = configure({})
    assert first["host"] == "0.0.0.0"
    assert len(first["api_token"]) >= 32
    assert first["api_token"] == second["api_token"]
    assert config.load_user_config(app_id) == {
        "workflow_id": "saved", "port": 19001, "api_token": first["api_token"]}
    assert os.stat(config.config_path(app_id)).st_mode & 0o077 == 0


def test_existing_credentials_and_explicit_loopback_remain_user_controlled(access):
    configure, config, app_id = access
    config.write_user_config(app_id, {"api_token": "user-secret"})
    assert configure({"host": "0.0.0.0"})["api_token"] == "user-secret"
    assert configure({"api_token": "replacement"})["api_token"] == "replacement"
    assert configure({"host": "127.0.0.1", "api_token": ""})["api_token"] == ""
    assert config.load_user_config(app_id)["api_token"] == "user-secret"


def test_persistence_failure_prevents_lan_startup(access, monkeypatch):
    configure, config, _ = access
    def denied(*args):
        raise PermissionError("read-only config")
    monkeypatch.setattr(config, "write_user_config", denied)
    with pytest.raises(PermissionError, match="read-only config"):
        configure({})

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


def test_kit_app_wrapper_starts_health_endpoint_and_finishes(monkeypatch, tmp_path):
    import urllib.request

    monkeypatch.setitem(sys.modules, "appmgr", types.ModuleType("appmgr"))
    monkeypatch.setitem(sys.modules, "appmgr.workflow_model_contract", types.SimpleNamespace(bindings=lambda *a, **k: []))

    class App:
        def setup(self, config):
            self.config = config
            self._stop_flag = False

        def finish(self):
            self.finished = True

    monkeypatch.setitem(
        sys.modules,
        "kit.app",
        types.SimpleNamespace(
            App=App, run_app=lambda app: None, open_result_sink=lambda *a, **kw: None
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "kit.config",
        types.SimpleNamespace(appdata_root=lambda: str(tmp_path), app_dir_of=lambda app: str(APP_DIR)),
    )
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    monkeypatch.setenv("INFERENCE_EDGE_PORT", str(port))
    path = APP_DIR / "app.py"
    spec = importlib.util.spec_from_file_location("edge_deployment_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = module.InferenceEdgeApp()
    app.setup({"host": "127.0.0.1", "port": port})
    try:
        app.prepare_runtime()
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/healthz", timeout=3
        ) as response:
            assert json.load(response)["profile"] == "rv1126b"
    finally:
        app.finish()
    assert not app._http_thread.is_alive()
    assert app.finished

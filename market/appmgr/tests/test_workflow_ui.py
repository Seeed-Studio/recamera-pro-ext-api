import http.server
import hashlib
import json
import os
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from appmgr import workflow_ui, paths

MANIFEST = {"x-workflow-ui": {"version": 1}}


def test_stopped_app_workflow_selection_and_symlink_isolation(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "APPDATA_DIR", str(tmp_path))
    root = tmp_path / "demo" / "workflows"
    root.mkdir(parents=True)
    filename = hashlib.sha256(b"saved").hexdigest() + ".json"
    (root / filename).write_text(json.dumps({"id": "saved", "name": "Saved on device", "config": "{}"}))
    (root / "mismatch.json").write_text(json.dumps({"id": "saved"}))
    secret = tmp_path / "private.json"
    secret.write_text('{"name":"private-credential"}')
    (root / "linked.json").symlink_to(secret)
    os.mkfifo(root / "pipe.json")
    listing = workflow_ui.workflows(MANIFEST, "demo")
    assert listing == {"workflows": [{"id": "saved", "name": "Saved on device"}], "invalid_documents": 3}
    original = {"values": {"workflow_id": "deleted"}, "config_schema": {"groups": [
        {"items": [{"key": "workflow_id", "type": "string"}]}]}}
    payload = workflow_ui.decorate_config(MANIFEST, "demo", original)
    field = payload["config_schema"]["groups"][0]["items"][0]
    assert [option["value"] for option in field["options"]] == ["", "saved", "deleted"]
    assert original["config_schema"]["groups"][0]["items"][0]["type"] == "string"
    assert "private-credential" not in json.dumps(payload)
    with pytest.raises(ValueError):
        workflow_ui.workflows(MANIFEST, "../demo")
    assert not workflow_ui.supported({"x-workflow-ui": {"version": True}})


def test_loopback_bridge_uses_real_app_credential_and_rejects_redirects():
    observed = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            observed.append((self.path, self.headers.get("X-Inference-Token")))
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "https://external.invalid/")
            else:
                self.send_response(200 if self.headers.get("X-Inference-Token") == "private-device" else 401)
            self.end_headers()
            self.wfile.write(b'{"deployment":{"status":"running"}}')

        def log_message(self, *args):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        config = {"port": httpd.server_port, "api_token": "private-device"}
        result, _ = workflow_ui._request(config, "/app-center/workflow-runtime")
        assert result["deployment"]["status"] == "running"
        with pytest.raises(RuntimeError, match="HTTP 302"):
            workflow_ui._request(config, "/redirect")
        assert observed == [("/app-center/workflow-runtime", "private-device"), ("/redirect", "private-device")]
    finally:
        httpd.shutdown(); httpd.server_close(); thread.join(timeout=2)


def test_editor_exchanges_device_session_without_exposing_permanent_key(monkeypatch):
    monkeypatch.setattr(workflow_ui.config, "effective_values", lambda *args: {
        "host": "0.0.0.0", "port": 9001, "api_token": "permanent-private", "workflow_id": "saved"})
    calls = []

    def request(values, path, **kwargs):
        calls.append((path, kwargs))
        if path == "/ui/session":
            return {"csrf": "session-csrf"}, "edge_session=real-session"
        return {"origin": "https://app.roboflow.com", "csrf": "temporary-grant", "runtime_path": "/ui/runtime/temporary-grant"}, ""

    monkeypatch.setattr(workflow_ui, "_request", request)
    result = workflow_ui.editor_session(MANIFEST, "demo")
    assert "permanent-private" not in json.dumps(result)
    assert calls[1][1]["cookie"] == "edge_session=real-session"
    assert calls[1][1]["csrf"] == "session-csrf"

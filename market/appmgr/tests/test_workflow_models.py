import copy
import hashlib
import io
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from appmgr import paths, workflow_models as models, workflow_model_contract as contract

MANIFEST = {"x-workflow-ui": {"version": 1}}


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "APPMGR_DIR", str(tmp_path / "system"))
    monkeypatch.setattr(paths, "APPS_DIR", str(tmp_path / "apps"))
    monkeypatch.setattr(paths, "APPDATA_DIR", str(tmp_path / "appdata"))
    monkeypatch.setattr(models.config, "effective_values", lambda *args: {"workflow_id": "saved", "workflow_parameters": '{"model":"detector/2"}'})
    monkeypatch.setattr(models.state, "get_app", lambda *args: {})
    installed = Path(paths.app_dir("demo"))
    installed.mkdir(parents=True)
    (installed / "manifest.json").write_text(json.dumps(MANIFEST))
    doc = {"schema_version": 1, "model_id": "detector/2", "platform": "rv1126b", "format": "rknn",
           "task": "object-detection", "labels": ["person"],
           "input": {"name": "images", "shape": [1, 32, 32, 3], "dtype": "uint8", "layout": "NHWC", "color_format": "RGB", "normalization": "baked"},
           "outputs": [{"name": "detections", "shape": [1, 5, 8], "dtype": "float32", "layout": "BCN"}],
           "postprocess": {"kind": "yolo-decoded", "scores": "probabilities", "box_format": "xywh"}}
    manager = models.Manager()
    return manager, doc


def staged(service, mode="rknn"):
    manager, doc = service
    task = manager.create(MANIFEST, "demo", {"mode": mode, "metadata": doc})
    blob = b"RKNN" + b"model" * 32
    manager.upload("demo", task["id"], "source", io.BytesIO(blob), len(blob))
    manager.action("demo", task["id"], "resume", {})
    return contract.read_json(manager._path("demo", task["id"]) / "task.json"), blob


def test_offline_registration_persists_and_is_not_ready_until_runtime_validation(service):
    manager, _ = service
    task, blob = staged(service)
    manager._step(task)
    snapshot = models.Manager().snapshot(MANIFEST, "demo")
    assert snapshot["tasks"][0]["state"] == "registered"
    assert snapshot["models"][0]["status"] == "restart_required"
    binding, = contract.bindings("demo", verify=True)
    assert binding["sha256"] == hashlib.sha256(blob).hexdigest()
    assert not (manager._path("demo", task["id"]) / "source").exists()
    with pytest.raises(models.ModelConflict):
        manager.create(MANIFEST, "demo", {"mode": "rknn", "metadata": task["metadata"]})
    Path(binding["path"]).write_bytes(b"RKNNtampered!")
    with pytest.raises(ValueError, match="digest mismatch"):
        contract.bindings("demo", verify=True)


def test_login_required_preserves_source_and_resumes_same_cloud_task(service, monkeypatch):
    manager, _ = service
    task, blob = staged(service, "onnx")
    def denied():
        raise models.cloud.LoginRequired()
    monkeypatch.setattr(models.cloud, "credentials", denied)
    manager._step(task)
    assert task["state"] == "awaiting_login"
    assert (manager._path("demo", task["id"]) / "source").read_bytes() == blob
    submissions = []
    monkeypatch.setattr(models.cloud, "credentials", lambda: {"user_id": "account", "access_token": "secret"})
    monkeypatch.setattr(models.cloud, "submit", lambda *a: submissions.append(1) or "cloud-123")
    monkeypatch.setattr(models.cloud, "query", lambda *a, **k: {"status": "20"})
    manager.action("demo", task["id"], "resume", {})
    task = contract.read_json(manager._path("demo", task["id"]) / "task.json")
    manager._step(task)
    assert task["cloud_id"] == "cloud-123"
    fresh = models.Manager()
    monkeypatch.setattr(models.cloud, "query", lambda *a, **k: {"status": "done"})
    monkeypatch.setattr(models.cloud, "download", lambda s, i, p: Path(p).write_bytes(blob))
    fresh._step(contract.read_json(manager._path("demo", task["id"]) / "task.json"))
    assert len(submissions) == 1
    public = fresh.snapshot(MANIFEST, "demo")
    assert public["tasks"][0]["state"] == "registered"
    assert "secret" not in json.dumps(public) and "account" not in json.dumps(public)


def test_interrupted_submit_is_not_replayed(service, monkeypatch):
    manager, _ = service
    task, _ = staged(service, "onnx")
    manager._save(task, state="uploading", user_id="account")
    monkeypatch.setattr(models.cloud, "submit", lambda *a: pytest.fail("duplicate POST"))
    manager.start()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        recovered = contract.read_json(manager._path("demo", task["id"]) / "task.json")
        if recovered["state"] == "submission_uncertain":
            break
        time.sleep(.01)
    manager.close()
    assert recovered["state"] == "submission_uncertain"
    with pytest.raises(ValueError, match="existing SenseCraft"):
        manager.action("demo", task["id"], "resume", {})


def test_reject_traversal_symlink_and_truncated_upload(service, tmp_path):
    manager, doc = service
    for identifier in ("../x", "a/../x", "/tmp/x", "a//x"):
        with pytest.raises(ValueError):
            manager.create(MANIFEST, "demo", {"mode": "rknn", "metadata": {**doc, "model_id": identifier}})
    task = manager.create(MANIFEST, "demo", {"mode": "rknn", "metadata": doc})
    with pytest.raises(ValueError, match="Incomplete"):
        manager.upload("demo", task["id"], "source", io.BytesIO(b"short"), 20)
    assert not (manager._path("demo", task["id"]) / "source").exists()
    manager.action("demo", task["id"], "cancel", {})
    task, _ = staged(service)
    manager._step(task)
    item, = contract.bindings("demo")
    asset = Path(item["path"])
    outside = tmp_path / "outside"
    asset.rename(outside)
    asset.symlink_to(outside)
    with pytest.raises(OSError):
        contract.bindings("demo", verify=True)


def test_dynamic_workflow_models_are_reported_without_blocking_save(service):
    manager, _ = service
    directory = Path(paths.APPDATA_DIR) / "demo" / "workflows"
    directory.mkdir(parents=True)
    definition = {"inputs": [{"name": "model", "default_value": "default/1"}],
                  "steps": [{"model_id": "$inputs.model"}, {"model_id": "$inputs.unset"}]}
    doc = {"id": "saved", "config": json.dumps({"specification": definition})}
    contract.atomic_json(directory / (hashlib.sha256(b"saved").hexdigest()+".json"), doc)
    workflow, = manager.snapshot(MANIFEST, "demo")["workflows"]
    assert workflow["missing"] == ["detector/2"]
    assert workflow["unresolved"] == ["unset"]
    assert contract.read_json(directory / (hashlib.sha256(b"saved").hexdigest()+".json")) == doc


def test_invalid_decoder_contract_is_rejected_before_upload(service):
    manager, doc = service
    bad = copy.deepcopy(doc)
    bad["outputs"][0]["shape"] = [1, 84, 8]
    with pytest.raises(ValueError, match="Decoded YOLO"):
        manager.create(MANIFEST, "demo", {"mode": "rknn", "metadata": bad})


def test_remove_requires_stopped_app_and_no_workflow_reference(service, monkeypatch):
    manager, _ = service
    task, _ = staged(service)
    manager._step(task)
    monkeypatch.setattr(models.state, "get_app", lambda *a: {"pid": 123, "desired_state": "running"})
    with pytest.raises(models.ModelConflict, match="Stop"):
        manager.remove(MANIFEST, "demo", "detector/2")
    monkeypatch.setattr(models.state, "get_app", lambda *a: {})
    assert manager.remove(MANIFEST, "demo", "detector/2") == {"removed": "detector/2"}
    assert contract.bindings("demo") == []


def test_cloud_submission_streams_bounded_chunks_and_never_retries_unknown_result(tmp_path, monkeypatch):
    from appmgr import workflow_cloud
    source = tmp_path / "source"
    source.write_bytes(b"onnx" * 50000)
    sent, headers = [], {}
    class Connection:
        def putrequest(self, method, path):
            assert (method, path) == ("POST", "/v1/api/create_task")
        def putheader(self, key, value):
            headers[key] = value
        def endheaders(self):
            pass
        def send(self, data):
            sent.append(data)
        def getresponse(self):
            raise TimeoutError("lost response")
        def close(self):
            pass
    monkeypatch.setattr(workflow_cloud, "_connection", Connection)
    with pytest.raises(workflow_cloud.SubmissionUncertain):
        workflow_cloud.submit({"user_id": "account", "access_token": "private"}, source, model_name="example/1")
    assert max(map(len, sent)) <= 65536
    assert sum(map(len, sent)) == int(headers["Content-Length"])
    assert b'example-1-' in b"".join(sent)
    assert b"private" not in b"".join(sent)


def test_cloud_login_required_is_not_native_web_logout_and_mutations_reject_foreign_origin(monkeypatch):
    import http.client
    import http.server
    import threading
    from appmgr import server
    monkeypatch.setattr(server, "_require_installed", lambda *a: None)
    monkeypatch.setattr(server, "_read_manifest", lambda *a: MANIFEST)
    def login_required():
        raise models.cloud.LoginRequired()
    monkeypatch.setattr(models.cloud, "credentials", login_required)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server._Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=3)
        connection.request("GET", "/api/app-center/v1/apps/demo/workflow-models/cloud-records")
        response = connection.getresponse()
        assert response.status == 409
        assert json.loads(response.read())["code"] == "sensecraft_login_required"
        connection.request("POST", "/api/app-center/v1/apps/demo/workflow-models/tasks", body=b"{}",
                           headers={"Content-Type": "application/json", "Origin": "https://untrusted.invalid"})
        response = connection.getresponse()
        assert response.status == 403
        response.read()
        connection.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)

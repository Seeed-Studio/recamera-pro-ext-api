import ast
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from .conftest import REPO, SKILL
from dependency_contract import check_imports
from package_app import PackagingError, build
from smoke_app import smoke
from source_contract import detector_contract_issues, inspect_sources
from sdk_contract import LOCK
import deploy_app
import device_app


def test_sdk_import_name_is_not_distribution_name(manifest, make_app, tmp_path):
    manifest["python"]["imports"] = ["recamera_ext"]
    archive, _ = build(make_app(), tmp_path / "out")
    assert archive.exists()


def test_missing_dependency_cannot_hide_behind_empty_imports(make_app, tmp_path):
    app = make_app()
    source = app / "app.py"
    source.write_text("import unavailable_skill_dependency\n" + source.read_text())
    with pytest.raises(PackagingError, match="unresolved source imports.*unavailable_skill_dependency"):
        build(app, tmp_path / "out")


def test_final_archive_imports_rechecked_after_overlay(make_app, tmp_path, monkeypatch):
    import package_app
    original = package_app.update_manifest
    def tamper(*args, **kwargs):
        # Inject between staged validation and official archive construction;
        # the official builder will generate a valid BOM for these bad bytes.
        result = original(*args, **kwargs)
        staging = args[0]
        path = staging / "app.py"
        path.write_text("import missing_from_overlay\n" + path.read_text())
        return result
    monkeypatch.setattr(package_app, "update_manifest", tamper)
    with pytest.raises(PackagingError, match="final archive unresolved source imports"):
        build(make_app(), tmp_path / "out")


def test_import_check_handles_local_lazy_optional_and_type_checking():
    trees = [("app.py", ast.parse('''
import os
import recamera_ext
from helpers import VALUE
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    import typing_only_missing
try:
    import optional_missing
except ImportError:
    pass
def lazy():
    import private_demo.feature
    import required_missing
''')), ("helpers.py", ast.parse("VALUE=1"))]
    result = check_imports(trees, "app.py", {"private_demo"})
    assert [x["module"] for x in result["missing"]] == ["required_missing"]
    assert [x["module"] for x in result["optional_unverified"]] == ["optional_missing"]
    assert result["private_imports"] == ["private_demo.feature"]


def analyze(body):
    return inspect_sources([("app.py", ast.parse("from kit.app import App\n" + body))],
                           "app.py", LOCK["kit_api"])


def test_missing_kit_method_is_error_and_real_method_arguments_are_checked():
    report = analyze("class Demo(App):\n owns_loop=True\n def run(self):\n  self.unavailable_inference_api()\n  self.tick(unsupported=True)\n")
    assert {i["code"] for i in report["issues"]} >= {"unknown_app_method", "invalid_kit_api_call"}


@pytest.mark.parametrize("body", [
    "class Base(App):\n def custom(self): pass\nclass Demo(Base):\n owns_loop=True\n def run(self): self.custom()\n",
    "class Demo(App):\n owns_loop=True\n def run(self):\n  self.callback=lambda:None\n  self.callback()\n",
    "class Demo(App):\n owns_loop=True\n def __getattr__(self, name): return lambda:None\n def run(self): self.dynamic()\n",
    "class Mixin:\n def helper(self): pass\nclass Demo(Mixin, App):\n owns_loop=True\n def run(self): self.helper()\n",
])
def test_custom_helpers_not_rejected_as_unknown_sdk(body):
    assert not [i for i in analyze(body)["issues"] if i["severity"] == "error"]


def test_custom_detector_requires_model_parameters():
    source = ast.parse("from kit.runtime.postprocess.detect import postprocess as pp\npp(outs, info)\n")
    manifest = {"entry": "app.py", "models": [{"input": [1, 320, 320, 3], "classes": ["helmet", "head"]}]}
    issues = detector_contract_issues([("app.py", source)], manifest)
    assert len(issues) == 2
    assert all(i["code"] == "missing_detector_model_parameter" for i in issues)


@pytest.mark.parametrize("nc", [2, 80])
def test_documented_detector_320_has_correct_classes_and_geometry(nc):
    np = pytest.importorskip("numpy")
    from kit.runtime.preprocess import LetterboxInfo
    from kit.runtime.postprocess.detect import COCO80
    code = re.search(r"```python\n(.*?)\n```", (SKILL / "references/kit-app-patterns.md").read_text(), re.S)[1]
    namespace = {"__name__": "regression_detector"}
    exec(compile(code, "documented_detector", "exec"), namespace)
    boxes = np.full((1, 64, 40, 40), -30., np.float32)
    for side in range(4):
        boxes[:, side * 16 + 1, :, :] = 30.
    classes = np.zeros((1, nc, 40, 40), np.float32)
    classes[0, 0, 10, 10] = .9
    app = namespace["MyApp"].__new__(namespace["MyApp"])
    app.frames = lambda: iter([SimpleNamespace(pts=1.25)])
    info = LetterboxInfo(scale=1., pad_w=0, pad_h=0, orig_w=320, orig_h=320)
    prepared = SimpleNamespace(info=info)
    app.pre = lambda frame: prepared
    app.models = SimpleNamespace(det=SimpleNamespace(infer=lambda value: [boxes, classes]))
    app.conf, app.iou, app._pre_size = .25, .45, 320
    app.class_names = ["helmet", "head"] if nc == 2 else COCO80
    emitted = []
    app.emit = lambda *args, **kwargs: emitted.extend(kwargs["results"])
    app.run()
    assert len(emitted) == 1
    assert emitted[0]["box"] == pytest.approx([76, 76, 92, 92])
    assert emitted[0]["cls_name"] == ("helmet" if nc == 2 else "person")


def test_archive_loader_and_controlled_loop(make_app, tmp_path):
    archive, _ = build(make_app(), tmp_path / "out")
    hook = tmp_path / "smoke.py"
    hook.write_text("def smoke(app):\n    assert app.owns_loop\n    app.run()\n")
    result = smoke(archive, sdk_root=REPO, hook=hook)
    assert result["loader"] == result["mock_loop"] == "passed", result
    assert result["device"] == "not_run"


def test_loader_detects_constructor_failure_and_bounds_loop(make_app, tmp_path):
    app = make_app("from kit.app import App\nclass Demo(App):\n owns_loop=True\n def __init__(self): raise ValueError('bad config')\n def run(self): pass\n")
    archive, _ = build(app, tmp_path / "out")
    result = smoke(archive, sdk_root=REPO)
    assert result["loader"] == "failed" and "bad config" in result["error"]
    archive, _ = build(make_app(), tmp_path / "out")
    hook = tmp_path / "smoke.py"
    hook.write_text("def smoke(app):\n    while True: pass\n")
    result = smoke(archive, sdk_root=REPO, hook=hook, timeout=1)
    assert result["loader"] == "failed" and "deadline" in result["error"]


def test_staging_integrity_permissions_and_partial_transfer(tmp_path):
    content = b"verified bytes"
    config = {"size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    target = device_app.receive(io.BytesIO(content), config, tmp_path)
    assert target.read_bytes() == content
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.parent.stat().st_mode & 0o777 == 0o700
    for bad in (content[:-1], b"x" * len(content)):
        with pytest.raises(device_app.DeviceError):
            device_app.receive(io.BytesIO(bad), config, tmp_path)
        assert list(tmp_path.iterdir()) == [target.parent]


def test_password_uses_fd_and_verified_bytes_are_transferred(make_app, tmp_path, monkeypatch):
    archive, _ = build(make_app(), tmp_path / "out")
    secret = "test-only-credential"
    monkeypatch.setattr(deploy_app.shutil, "which", lambda _: "/usr/bin/sshpass")
    def transport(command, **kwargs):
        assert secret not in " ".join(command)
        assert "StrictHostKeyChecking=yes" in command
        assert os.read(kwargs["pass_fds"][0], 1024) == secret.encode() + b"\n"
        stream = kwargs["stdin"]
        config = json.loads(stream.readline())
        assert config["action"] == "upload"
        assert hashlib.sha256(stream.read()).hexdigest() == config["sha256"]
        report = {"upload": "passed", "sha256": config["sha256"], "app_id": config["manifest"]["id"]}
        return SimpleNamespace(returncode=0, stdout=("banner\nRECAMERA_SKILL_RESULT=" + json.dumps(report)).encode(), stderr=b"")
    monkeypatch.setattr(deploy_app, "run_ssh", transport)
    result = deploy_app.deploy(archive, host="192.0.2.5", user="root", password=secret)
    assert result["upload"] == "passed"
    assert result["install"] == result["lifecycle"] == "not_run"
    assert secret not in json.dumps(result)


def test_transport_failure_does_not_claim_no_device_mutation(make_app, tmp_path, monkeypatch):
    archive, _ = build(make_app(), tmp_path / "out")
    monkeypatch.setattr(deploy_app, "run_ssh", lambda *a, **kw: SimpleNamespace(returncode=255, stdout=b"", stderr=b"REMOTE HOST IDENTIFICATION HAS CHANGED"))
    result = deploy_app.deploy(archive, host="192.0.2.5", user="root", action="verify")
    assert result["install"] == result["lifecycle"] == "unknown"
    assert "HOST IDENTIFICATION HAS CHANGED" in result["error"]


def test_operation_rejects_wrong_identity_failure_and_pending(monkeypatch):
    api = SimpleNamespace(request=lambda *args: {"operations": []})
    op = {"id": "a" * 32, "type": "install", "app_id": "test", "status": "succeeded"}
    with pytest.raises(device_app.DeviceError, match="identity"):
        device_app.wait_operation(api, {"operation": op}, "install", "other", 1, {})
    with pytest.raises(device_app.DeviceError, match="failed"):
        device_app.wait_operation(api, {"operation": dict(op, status="failed", error="no space")}, "install", "test", 1, {})
    report = {}
    with pytest.raises(device_app.PendingOperation, match="pending"):
        device_app.wait_operation(api, {"operation": dict(op, status="running")}, "install", "test", 0, report)
    assert report["operations"][0]["id"] == op["id"]


def test_old_or_other_app_result_cannot_pass_acceptance():
    message = {"type": "frame", "seq": 1, "source": {"kind": "app", "id": "demo", "instance": "new", "generation": 3}}
    assert device_app.matching_result(message, "demo", ("new", 3))
    assert not device_app.matching_result(message, "another", ("new", 3))
    assert not device_app.matching_result(message, "demo", ("old", 2))
    assert not device_app.matching_result(dict(message, type="status"), "demo", ("new", 3))


def test_existing_app_refused_before_api_upload(tmp_path):
    package = tmp_path / "app.tar.gz"
    package.write_bytes(b"package")
    requests = []
    def request(method, path, **kw):
        requests.append((method, path))
        return {"apps": [{"id": "demo"}]} if path == "/apps" else {}
    with pytest.raises(device_app.DeviceError, match="already installed"):
        device_app.install(SimpleNamespace(request=request), package,
                           {"manifest": {"id": "demo"}, "replace": False}, {})
    assert requests == [("GET", "/policy"), ("GET", "/apps")]


def test_actual_private_import_is_added_and_loadable(make_app, tmp_path):
    wheelhouse = tmp_path / "wheels"
    wheelhouse.mkdir()
    wheel = wheelhouse / "private_demo-1.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as output:
        output.writestr("private_demo/__init__.py", "VALUE = 42\n")
        output.writestr("private_demo-1.0.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: private-demo\nVersion: 1.0.0\n")
        output.writestr("private_demo-1.0.0.dist-info/WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        output.writestr("private_demo-1.0.0.dist-info/RECORD", "")
    app = make_app()
    path = app / "app.py"
    path.write_text("from private_demo import VALUE\nassert VALUE == 42\n" + path.read_text())
    archive, _ = build(app, tmp_path / "out", wheelhouse=wheelhouse)
    with tarfile.open(archive) as package:
        manifest = json.load(package.extractfile("manifest.json"))
    assert manifest["python"]["imports"] == ["private_demo"]
    assert smoke(archive, sdk_root=REPO)["loader"] == "passed"


def test_install_checks_preflight_and_preserves_permission_contract(tmp_path, monkeypatch):
    package = tmp_path / "app.tar.gz"
    package.write_bytes(b"package")
    manifest = {"id": "demo", "version": "1.0.0", "permissions": {"sdk": ["result.publish"]}}
    preflight = {"manifest": manifest, "release_id": "release", "checks": [{"passed": True}],
                 "install_context": {"mode": "new", "confirmation_required": []}}
    requests = []
    def request(method, path, body=None, **kw):
        requests.append((method, path, body))
        if path == "/policy":
            return {}
        if method == "POST" and path == "/uploads":
            assert kw["package"] == package
            return {"upload_id": "a" * 32, "preflight": preflight}
        if method == "POST" and path == "/apps":
            assert body == {"upload_id": "a" * 32, "permissions_confirmed": True, "permissions": manifest["permissions"]}
            return {"operation": {"id": "b" * 32, "type": "install", "app_id": "demo", "status": "succeeded"}}
        if method == "DELETE":
            return {}
        return {"apps": [{"id": "demo", "version": "1.0.0"}] if any(r[:2] == ("POST", "/apps") for r in requests) else []}
    monkeypatch.setattr(device_app, "installed_identity", lambda *args: None)
    config = {"manifest": manifest, "release_id": "release", "replace": False, "timeout": 2}
    report = {}
    device_app.install(SimpleNamespace(request=request), package, config, report)
    assert report["install"] == "passed"
    requests.clear()
    preflight["release_id"] = "substituted"
    with pytest.raises(device_app.DeviceError, match="identity differs"):
        device_app.install(SimpleNamespace(request=request), package, config, {})
    assert not any(r[:2] == ("POST", "/apps") for r in requests)
    assert requests[-1][:2] == ("DELETE", "/uploads/" + "a" * 32)


def test_lifecycle_uses_new_instances_and_does_not_touch_other_apps(monkeypatch):
    generation, running, actions = 0, False, []
    def state():
        return {"id": "demo", "running": running,
                "runtime": {"observed_state": "running" if running else "stopped"},
                "instance": {"id": f"i{generation}", "generation": generation}}
    def request(method, path, body=None):
        nonlocal generation, running
        if method == "GET":
            assert path == "/apps"
            return {"apps": [state()]}
        assert path.startswith("/apps/demo/")
        action = path.rsplit("/", 1)[1]
        actions.append(action)
        running = action != "stop"
        if running:
            generation += 1
        return {"operation": {"id": f"{len(actions):032x}", "type": action, "app_id": "demo", "status": "succeeded"}}
    def observe(api, app_id, seconds, report, label):
        report.setdefault("observations", {})[label] = {"state": "passed"}
        return device_app.identity(state())
    monkeypatch.setattr(device_app, "observe", observe)
    report = {}
    device_app.verify(SimpleNamespace(request=request), {"manifest": {"id": "demo"}, "timeout": 2,
                      "observe_seconds": 2, "leave_running": False}, report)
    assert actions == ["start", "stop", "start", "restart", "stop"]
    assert not running
    assert report["lifecycle"] == report["results"] == "passed"


def test_observation_rejects_replay_and_detects_crash(monkeypatch):
    app = {"id": "demo", "running": True, "runtime": {"observed_state": "running"},
           "instance": {"id": "current", "generation": 1}}
    api = SimpleNamespace(request=lambda *args: {"apps": [app]})
    class ReplayedResult:
        def __init__(self, app_id):
            pass
        def close(self):
            pass
        def message(self, deadline):
            time.sleep(.005)
            return {"type": "frame", "seq": 1, "source": {"kind": "app", "id": "demo", "instance": "current", "generation": 1}}
    monkeypatch.setattr(device_app, "Results", ReplayedResult)
    report = {}
    device_app.observe(api, "demo", .03, report, "start")
    assert report["observations"]["start"]["state"] == "unverified"
    app["runtime"]["observed_state"] = "failed"
    with pytest.raises(device_app.DeviceError, match="exited"):
        device_app.observe(api, "demo", .03, {}, "start")


def test_http_stream_is_accepted_by_official_upload_parser(tmp_path, monkeypatch):
    from market.appmgr import uploads, paths
    monkeypatch.setattr(paths, "APPSTAGE_DIR", str(tmp_path / "appstage"))
    payload = b"binary package\x00\xff" * 10000
    package = tmp_path / "app.tar.gz"
    package.write_bytes(payload)
    received = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/api/app-center/v1/uploads"
            assert self.headers["Origin"] == "http://127.0.0.1"
            assert "X-ReCamera-App-Center-Route" not in self.headers
            received.append(uploads.receive(self.rfile, int(self.headers["Content-Length"]), self.headers["Content-Type"]))
            self.send_response(201)
            self.end_headers()
            self.wfile.write(json.dumps({"upload_id": received[0]["upload_id"]}).encode())
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original = device_app.http.client.HTTPConnection
    monkeypatch.setattr(device_app.http.client, "HTTPConnection", lambda *args, **kwargs: original("127.0.0.1", server.server_port, timeout=3))
    try:
        result = device_app.API().request("POST", "/uploads", package=package)
        receipt = uploads.verify(result["upload_id"])
        assert receipt["sha256"] == hashlib.sha256(payload).hexdigest()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_result_client_with_actual_canonical_hub(tmp_path, monkeypatch):
    from market.appmgr.result_hub import ResultHub
    hub = ResultHub(ws_port=0, system_uds_path=str(tmp_path / "results.sock"),
                    formatter=SimpleNamespace(format=lambda raw: [])).start()
    owner = {"app_id": "demo", "instance_id": "i1", "generation": 1, "pid": os.getpid()}
    manifest = {"manifest_version": 2, "id": "demo", "resources": {"claims": []},
                "output": {"fields": []}, "render": {}}
    assert hub.refresh_app_manifest(owner, manifest, stream_contract=None)
    original = device_app.socket.create_connection
    monkeypatch.setattr(device_app.socket, "create_connection", lambda *args, **kw: original(("127.0.0.1", hub.ws_port), timeout=2))
    subscriber = None
    try:
        subscriber = device_app.Results("demo")
        deadline = time.monotonic() + 3
        message = subscriber.message(deadline)
        assert message["type"] == "hello"
        hub.publish_app({"type": "results", "seq": 5, "pts": 1.25, "results": []}, owner)
        while time.monotonic() < deadline:
            message = subscriber.message(deadline)
            if device_app.matching_result(message, "demo", ("i1", 1)):
                break
        assert message["seq"] == 5
    finally:
        if subscriber:
            subscriber.close()
        hub.stop()


def test_password_redaction_does_not_break_numeric_report_values():
    assert deploy_app.redact({"count": 1, "log": "password is 1"}, "1") == {
        "count": 1, "log": "password is [REDACTED]"}


def test_ssh_runner_timeout_reaps_its_process_group():
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        deploy_app.run_ssh([sys.executable, "-c", "import time; time.sleep(30)"],
                           stdin=subprocess.DEVNULL, timeout=.05, pass_fds=())
    assert time.monotonic() - started < 3


def test_smoke_failure_is_separate_from_loader_success(make_app, tmp_path):
    archive, _ = build(make_app(), tmp_path / "out")
    hook = tmp_path / "assert_output.py"
    hook.write_text("def smoke(app):\n    app.run()\n    raise AssertionError('unexpected output')\n")
    result = smoke(archive, sdk_root=REPO, hook=hook)
    assert result["loader"] == "passed"
    assert result["mock_loop"] == "failed"
    assert result["device"] == "not_run"

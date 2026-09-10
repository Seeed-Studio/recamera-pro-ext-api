"""TERM finishes lifecycle transactions, contains children and preserves intent."""

import http.client
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from appmgr import coordinator, paths, server, state


@pytest.fixture
def shutdown_layout(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "APPS_DIR", str(tmp_path / "apps"))
    monkeypatch.setattr(paths, "APPMGR_DIR", str(tmp_path / "appmgr"))
    monkeypatch.setattr(paths, "STATE_FILE", str(tmp_path / "apps/state.json"))
    monkeypatch.setattr(server, "_service_stopping", False)
    monkeypatch.setattr(server, "_audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_invalidate_result_if_inactive", lambda app_id: None)
    paths.ensure_dirs()


def test_shutdown_preserves_failed_fence_and_stops_remaining_apps(
        shutdown_layout, monkeypatch):
    for app_id, desired in (("a-stuck", "running"), ("b-running", "running"),
                            ("c-stopped-intent", "stopped")):
        state.begin_start(app_id, "instance-" + app_id)
        state.set_desired(app_id, desired)
        state.transition(app_id, "running", pid=123, pgid=123)
    calls = []

    def stop(app_id, **kwargs):
        calls.append(app_id)
        if app_id == "a-stuck":
            raise RuntimeError("process group fence remains alive")
        return {"stopped": app_id}

    supervisor = SimpleNamespace(stop=stop, is_running=lambda app_id: 123)
    coord = coordinator.AppCoordinator(
        supervisor_module=supervisor,
        resource_manager=SimpleNamespace(release=lambda *args: None),
        inference_registry=SimpleNamespace(revoke=lambda *args, **kwargs: None))
    monkeypatch.setattr(server, "_coordinator", lambda: coord)
    monkeypatch.setattr(server.supervisor, "is_running", lambda app_id: 123)
    monkeypatch.setattr(server.supervisor, "has_run_record", lambda app_id: True)
    monkeypatch.setattr(server, "_service_stopping", True)

    with pytest.raises(RuntimeError, match="application shutdown incomplete.*a-stuck"):
        server._shutdown_apps()
    assert calls == ["a-stuck", "b-running", "c-stopped-intent"]
    failed = state.get_app("a-stuck")
    assert failed["desired_state"] == "running"
    assert failed["teardown_pending"] is True
    assert "fence remains alive" in failed["reason"]
    assert failed["pid"] == 123
    assert state.get_app("b-running")["desired_state"] == "running"
    assert state.get_app("b-running")["observed_state"] == "stopped"
    assert state.get_app("c-stopped-intent")["desired_state"] == "stopped"


def test_shutdown_refuses_new_mutations_but_keeps_current_transaction(
        shutdown_layout, monkeypatch):
    completed = []
    with server.busy_gate():
        monkeypatch.setattr(server, "_service_stopping", True)
        completed.append("durable transaction completed")
    with pytest.raises(server.BusyError, match="stopping"):
        with server.busy_gate():
            pytest.fail("new mutation entered after shutdown")
    with server.busy_gate(allow_stopping=True):
        completed.append("shutdown cleanup")
    assert len(completed) == 2


def test_regular_reconcile_cancels_between_apps(shutdown_layout, monkeypatch):
    stop = threading.Event()
    calls = []
    for app_id in ("first", "second"):
        Path(paths.app_dir(app_id)).mkdir(parents=True)
        state.set_desired(app_id, "running")

    def reconcile(app_id, **kwargs):
        calls.append(app_id)
        stop.set()
        return {"observed_state": "waiting_dependency"}

    monkeypatch.setattr(server, "_coordinator", lambda: SimpleNamespace(
        reconcile_allocations=lambda: [], reconcile_one=reconcile))
    monkeypatch.setattr(server.supervisor, "sweep_stale", lambda: [])
    monkeypatch.setattr(server, "_recover_incomplete_teardowns", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_managed_launch", lambda *args: None)
    monkeypatch.setattr(server, "_read_manifest", lambda app_id: {"id": app_id})
    monkeypatch.setattr(server, "_operation_manager", lambda: SimpleNamespace(
        events=SimpleNamespace(publish=lambda *args, **kwargs: None)))
    server._reconcile_once(stop)
    assert calls == ["first"]


def test_term_during_startup_recovery_still_runs_owned_cleanup(
        shutdown_layout, monkeypatch):
    cleaned = []

    def startup(host, port, *, lifecycle):
        lifecycle["owned"] = True
        server._service_stopping = True
        with server.busy_gate():
            pytest.fail("startup opened another transaction after TERM")

    monkeypatch.setattr(server, "_serve", startup)
    monkeypatch.setattr(server, "_stop_reconciler", lambda: cleaned.append("worker"))
    monkeypatch.setattr(server, "_shutdown_apps", lambda: cleaned.append("apps"))
    previous = signal.getsignal(signal.SIGTERM)
    server.serve()
    assert cleaned == ["worker", "apps"]
    assert signal.getsignal(signal.SIGTERM) == previous
    assert server._service_stopping is True
    with pytest.raises(server.BusyError):
        with server.busy_gate():
            pytest.fail("late request started after serve returned")


def test_stopping_health_is_unready_even_with_live_listeners(
        shutdown_layout, monkeypatch):
    monkeypatch.setattr(server, "_result_hub_instance", object())
    monkeypatch.setattr(server, "_result_gateway_instance", object())
    monkeypatch.setattr(server, "_service_stopping", True)
    httpd = server._AppHTTPServer(("127.0.0.1", 0), server._Handler)
    httpd.timeout = 2
    thread = threading.Thread(target=httpd.handle_request, daemon=True)
    thread.start()
    conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=2)
    try:
        conn.request("GET", "/health")
        response = conn.getresponse()
        assert response.status == 503
        assert json.loads(response.read()) == {"service": "appmgr", "ready": False}
    finally:
        conn.close()
        thread.join(3)
        httpd.server_close()


_SERVICE = r'''
import json, os, subprocess, sys, time
from pathlib import Path
from appmgr import coordinator, paths, server, state, supervisor
root = Path(sys.argv[1])
paths.APPS_DIR = str(root / "apps")
paths.APPMGR_DIR = str(root / "appmgr")
paths.APPDATA_DIR = str(root / "appdata")
paths.STATE_FILE = str(root / "apps/state.json")
paths.ensure_dirs()
events = root / "events"
def event(name):
    with events.open("a") as out: out.write(name + "\n")
class Listener:
    ws_port = 0
    def __init__(self, *args, **kwargs): pass
    def start(self): return self
    def stop(self): event("endpoint-stop")
    def close(self): return True
    def add_observer(self, callback): pass
    def remove_observer(self, callback): pass
    def observe(self, value): pass
    def submit_app(self, *args, **kwargs): pass
server.canonical_results.ResultHub = Listener
server.resultgateway.ResultGateway = Listener
server.appvisualization.DetectionOsdBridge = Listener
server.apprecording.RecordingTriggerBridge = Listener
server.appuploads.recover_startup = lambda: {}
server.appuploads.gc_expired = lambda: []
server.installer.reconcile_interrupted_installs = lambda: []
server._reconcile_install_transaction = lambda: None
server._audit = lambda name, **kwargs: event(name)
server._invalidate_result_if_inactive = lambda app_id: None
supervisor.kitversion.check = lambda manifest: None
supervisor._build_cmd = lambda app_id, manifest: [sys.executable, str(Path(paths.app_dir(app_id)) / "app.py")]
supervisor._build_env = lambda *args, **kwargs: dict(os.environ)
supervisor.READY_TIMEOUT = 5
supervisor._READY_POLL = .01
coord = coordinator.AppCoordinator(
    inference_registry=type("Registry", (), {"revoke": lambda *args, **kwargs: None})())
server._coordinator = lambda: coord
for app_id in ("a-running", "b-pending"):
    directory = Path(paths.app_dir(app_id))
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text(json.dumps({"id": app_id, "version": "1.0.0"}))
    state.set_desired(app_id, "running")
    (directory / "app.py").write_text("""
import os, subprocess, sys, time
from pathlib import Path
root = Path(%r)
app_id = %r
(root / (app_id + ".pid")).write_text(str(os.getpid()))
helper = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
(root / (app_id + ".helper.pid")).write_text(str(helper.pid))
while not (root / 'release-ready').exists(): time.sleep(.01)
Path(os.environ['APPMGR_READY_FILE']).write_text('ready')
time.sleep(60)
""" % (str(root), app_id))
http_server = server._AppHTTPServer
class HTTP(http_server):
    def __init__(self, address, handler):
        super().__init__(("127.0.0.1", 0), handler)
        (root / "port").write_text(str(self.server_port))
server._AppHTTPServer = HTTP
server.serve(host="127.0.0.1", port=1)
event("service-exit")
'''


def _wait_for_file(path, process, log, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return path.read_text()
        assert process.poll() is None, log.read_text()
        time.sleep(.02)
    pytest.fail("timed out waiting for %s: %s" % (path.name, log.read_text()))


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="process-group ownership uses procfs")
def test_sigterm_finishes_current_launch_then_stops_owned_group(tmp_path):
    service = tmp_path / "service.py"
    service.write_text(_SERVICE)
    log = tmp_path / "service.log"
    repo = Path(__file__).resolve().parents[3]
    env = dict(os.environ, PYTHONPATH=os.pathsep.join((
        str(repo / "market"), str(repo), str(repo / "sdk/python"))))
    env["APPMGR_RECONCILE_INTERVAL"] = ".05"
    process = None
    try:
        with log.open("w") as output:
            process = subprocess.Popen([sys.executable, str(service), str(tmp_path)],
                                       env=env, stdout=output, stderr=subprocess.STDOUT,
                                       start_new_session=True)
        app_pid = int(_wait_for_file(tmp_path / "a-running.pid", process, log))
        helper_pid = int(_wait_for_file(tmp_path / "a-running.helper.pid", process, log))
        _wait_for_file(tmp_path / "port", process, log)
        # TERM arrives while supervisor.start still waits for this app's READY.
        # It must finish that transaction, then contain the committed group.
        process.send_signal(signal.SIGTERM)
        time.sleep(.1)
        assert process.poll() is None, log.read_text()
        (tmp_path / "release-ready").touch()
        assert process.wait(timeout=8) == 0, log.read_text()
        snapshot = json.loads((tmp_path / "apps/state.json").read_text())
        assert snapshot["apps"]["a-running"]["desired_state"] == "running"
        assert snapshot["apps"]["a-running"]["observed_state"] == "stopped"
        assert snapshot["apps"]["a-running"]["pid"] is None
        assert snapshot["apps"]["b-pending"]["desired_state"] == "running"
        assert not (tmp_path / "b-pending.pid").exists()
        for pid in (app_pid, helper_pid):
            proc = Path("/proc/%d/stat" % pid)
            assert not proc.exists() or proc.read_text().rsplit(")", 1)[1].split()[0] == "Z"
        events = (tmp_path / "events").read_text().splitlines()
        assert events.index("service_shutdown_stopped") < events.index("endpoint-stop")
        assert events[-1] == "service-exit"
    finally:
        # Cleanup targets only PIDs created by this fixture, each own session.
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        for pidfile in tmp_path.glob("*.pid"):
            try:
                pid = int(pidfile.read_text())
                owned_cwd = os.readlink("/proc/%d/cwd" % pid)
                if owned_cwd.startswith(str(tmp_path / "apps") + os.sep):
                    os.kill(pid, signal.SIGKILL)
            except (OSError, ValueError):
                pass

"""Management remains reachable while boot applications wait for services."""

import http.client
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from appmgr import paths, server


@pytest.fixture
def lifecycle_layout(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "APPS_DIR", str(tmp_path / "apps"))
    monkeypatch.setattr(paths, "APPMGR_DIR", str(tmp_path / "appmgr"))
    monkeypatch.setattr(paths, "STATE_FILE", str(tmp_path / "apps/state.json"))
    monkeypatch.setattr(server, "_reconcile_thread", None)
    monkeypatch.setattr(server, "_reconcile_stop", None)
    monkeypatch.setattr(server, "_service_stopping", False)
    monkeypatch.setattr(server, "_audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_reconcile_once", lambda stop_event=None: [])
    yield
    server._stop_reconciler()


def _get_health(httpd):
    conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=1)
    try:
        conn.request("GET", "/health")
        response = conn.getresponse()
        return response.status, json.loads(response.read())
    finally:
        conn.close()


def test_serve_answers_health_before_boot_restore_finishes(
        lifecycle_layout, monkeypatch):
    entered_restore = threading.Event()
    release_restore = threading.Event()
    listeners = []
    errors = []

    class Listener:
        ws_port = 0

        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return self

        def stop(self):
            pass

        def close(self):
            return True

        def add_observer(self, callback):
            pass

        def remove_observer(self, callback):
            pass

        def observe(self, envelope):
            pass

        def submit_app(self, *args, **kwargs):
            pass

    def restore(stop_event):
        entered_restore.set()
        assert release_restore.wait(5), "test did not release boot restoration"

    http_server = server._AppHTTPServer

    def listen(address, handler):
        listener = http_server(("127.0.0.1", 0), handler)
        listeners.append(listener)
        return listener

    monkeypatch.setattr(server, "_AppHTTPServer", listen)
    monkeypatch.setattr(server, "_acquire_single_instance", lambda: True)
    monkeypatch.setattr(server, "_reconcile_startup_state", lambda: None)
    monkeypatch.setattr(server, "_reconcile_install_transaction", lambda: None)
    monkeypatch.setattr(server.appuploads, "recover_startup", lambda: {})
    monkeypatch.setattr(server.appuploads, "gc_expired", lambda: [])
    monkeypatch.setattr(server.supervisor, "install_sigchld", lambda: True)
    monkeypatch.setattr(server.installer, "reconcile_interrupted_installs", lambda: [])
    monkeypatch.setattr(server, "_coordinator", lambda: object())
    monkeypatch.setattr(server.canonical_results, "ResultHub", Listener)
    monkeypatch.setattr(server.appvisualization, "DetectionOsdBridge", Listener)
    monkeypatch.setattr(server.apprecording, "RecordingTriggerBridge", Listener)
    monkeypatch.setattr(server.resultgateway, "ResultGateway", Listener)
    monkeypatch.setattr(server, "_operation_manager_instance", None)
    monkeypatch.setattr(server, "_boot_restore_locked", restore)

    def serve():
        try:
            server.serve(host="127.0.0.1", port=1)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        assert entered_restore.wait(3), errors
        assert _get_health(listeners[0]) == (
            200, {"service": "appmgr", "ready": True})
        assert not release_restore.is_set()
    finally:
        release_restore.set()
        if listeners:
            listeners[0].shutdown()
        thread.join(5)
    assert not thread.is_alive()
    assert not errors
    assert server._reconcile_thread is None


def test_shutdown_cancels_restore_waiting_for_mutation_gate(
        lifecycle_layout, monkeypatch):
    contended = threading.Event()
    restored = []
    monkeypatch.setattr(server, "_audit", lambda *args, **kwargs: contended.set())
    monkeypatch.setattr(server, "_boot_restore_locked", lambda *args: restored.append(True))
    with server.busy_gate():
        server._start_reconciler(restore=True)
        assert contended.wait(2)
        worker = server._reconcile_thread
        # Cancellation must not wait for the other process's mutation gate.
        server._stop_reconciler()
        assert not worker.is_alive()
    assert not restored


def test_shutdown_between_apps_prevents_another_boot_launch(
        lifecycle_layout, monkeypatch):
    stopped = threading.Event()
    launched = []
    for app_id in ("first", "second"):
        Path(paths.app_dir(app_id)).mkdir(parents=True)

    def start(app_id, **kwargs):
        launched.append(app_id)
        stopped.set()
        return {"pid": 123, "observed_state": "running"}

    monkeypatch.setattr(server, "_coordinator", lambda: SimpleNamespace(
        reconcile_allocations=lambda: [], start=start))
    monkeypatch.setattr(server.supervisor, "sweep_stale", lambda: [])
    monkeypatch.setattr(server.supervisor, "is_running", lambda app_id: None)
    monkeypatch.setattr(server, "_recover_incomplete_teardowns", lambda *args, **kwargs: None)
    monkeypatch.setattr(server.state, "get_active", lambda: None)
    monkeypatch.setattr(server.state, "desired_apps", lambda: ["first", "second"])
    monkeypatch.setattr(server.state, "get_app", lambda app_id: {"launch_mode": "managed"})
    monkeypatch.setattr(server, "_read_manifest", lambda app_id: {"id": app_id})
    monkeypatch.setattr(server, "_managed_launch", lambda *args: None)
    server._boot_restore_locked(stopped)
    assert launched == ["first"]


def test_health_rejects_incomplete_service_listeners(monkeypatch):
    monkeypatch.setattr(server, "_result_hub_instance", None)
    monkeypatch.setattr(server, "_result_gateway_instance", object())
    httpd = server._AppHTTPServer(("127.0.0.1", 0), server._Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        assert _get_health(httpd) == (
            503, {"service": "appmgr", "ready": False})
    finally:
        httpd.shutdown()
        thread.join(2)
        httpd.server_close()

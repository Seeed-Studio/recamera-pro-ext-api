import io
import json
import os
import socket
import sys
import threading
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
from appmgr import operations, paths, server, uploads


def _multipart(package: bytes, boundary: str = "upload-pipeline-test"):
    body = b"".join((
        b"--" + boundary.encode("ascii") + b"\r\n",
        b'Content-Disposition: form-data; name="package"; '
        b'filename="demo.tar.gz"\r\n',
        b"Content-Type: application/gzip\r\n\r\n",
        package,
        b"\r\n--" + boundary.encode("ascii") + b"--\r\n",
    ))
    return body, "multipart/form-data; boundary=" + boundary


@pytest.fixture
def upload_layout(tmp_path, monkeypatch):
    appmgr = tmp_path / "appmgr"
    stage = tmp_path / "stage"
    apps = tmp_path / "apps"
    appmgr.mkdir()
    stage.mkdir()
    apps.mkdir()
    monkeypatch.setattr(paths, "APPMGR_DIR", str(appmgr))
    monkeypatch.setattr(paths, "APPSTAGE_DIR", str(stage))
    monkeypatch.setattr(paths, "APPS_DIR", str(apps))
    monkeypatch.setattr(paths, "STATE_FILE", str(apps / "state.json"))
    monkeypatch.setattr(paths, "MAX_PKG_BYTES", 1024 * 1024)
    monkeypatch.setattr(paths, "MAX_UPLOAD_STAGING_BYTES", 4 * 1024 * 1024)
    monkeypatch.setattr(paths, "MAX_STAGED_UPLOADS", 8)
    monkeypatch.setattr(paths, "UPLOAD_TTL_SEC", 3600)
    monkeypatch.setattr(paths, "MIN_UPLOAD_FREE_BYTES", 0)
    monkeypatch.setattr(paths, "UPLOAD_TOTAL_TIMEOUT_SEC", 10.0)
    with uploads._UPLOAD_LOCK:
        uploads._RECORD_LOCKS.clear()
        uploads._LIVE_INCOMING.clear()
        uploads._ORPHANED_ACTIVE.clear()
    if server._operation_manager_instance is not None:
        server._operation_manager_instance.close()
    monkeypatch.setattr(server, "_operation_manager_instance", None)
    monkeypatch.setattr(server, "_operation_manager_layout", None)
    yield tmp_path
    if server._operation_manager_instance is not None:
        server._operation_manager_instance.close()
        server._operation_manager_instance = None


class _BlockedStream(io.BytesIO):
    def __init__(self, value: bytes, entered: threading.Event,
                 release: threading.Event):
        super().__init__(value)
        self._entered = entered
        self._release = release
        self._blocked = False

    def read(self, size=-1):
        if not self._blocked:
            self._blocked = True
            self._entered.set()
            if not self._release.wait(timeout=3):
                raise RuntimeError("test upload was not released")
        return super().read(size)


def test_slow_receive_does_not_block_another_upload_cancel(upload_layout):
    body, content_type = _multipart(b"first")
    existing = uploads.receive(io.BytesIO(body), len(body), content_type)
    slow_body, slow_type = _multipart(b"slow" * 1024)
    entered = threading.Event()
    release = threading.Event()
    errors = []

    def receive_slowly():
        try:
            uploads.receive(
                _BlockedStream(slow_body, entered, release),
                len(slow_body), slow_type)
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    receiver = threading.Thread(target=receive_slowly)
    receiver.start()
    assert entered.wait(timeout=1)

    completed = threading.Event()
    result = []

    def cancel_existing():
        result.append(uploads.cancel(existing["upload_id"]))
        completed.set()

    canceller = threading.Thread(target=cancel_existing)
    canceller.start()
    try:
        assert completed.wait(timeout=0.5)
        assert result[0]["deleted"] is True
    finally:
        release.set()
        receiver.join(timeout=3)
        canceller.join(timeout=3)
    assert not errors


def test_concurrent_receive_reserves_declared_bytes_before_read(
        upload_layout, monkeypatch):
    body, content_type = _multipart(b"a" * 4096)
    monkeypatch.setattr(paths, "MAX_UPLOAD_STAGING_BYTES", len(body) * 2 - 1)
    entered = threading.Event()
    release = threading.Event()
    errors = []

    def receive_first():
        try:
            uploads.receive(
                _BlockedStream(body, entered, release), len(body), content_type)
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    first = threading.Thread(target=receive_first)
    first.start()
    assert entered.wait(timeout=1)
    try:
        with pytest.raises(uploads.StagingQuotaError, match="byte limit"):
            uploads.receive(io.BytesIO(body), len(body), content_type)
    finally:
        release.set()
        first.join(timeout=3)
    assert not errors
    assert len([name for name in os.listdir(paths.uploads_dir())
                if not name.startswith(".incoming-")]) == 1


def test_concurrent_reservation_preserves_unwritten_free_space_balance(
        upload_layout, monkeypatch):
    body, content_type = _multipart(b"a" * 4096)
    reserve = len(body)
    baseline_free = len(body) * 3 - 1
    monkeypatch.setattr(paths, "MIN_UPLOAD_FREE_BYTES", reserve)

    def disk_usage(_root):
        written = uploads._tree_size(paths.uploads_dir())
        return SimpleNamespace(free=baseline_free - written)

    monkeypatch.setattr(uploads.shutil, "disk_usage", disk_usage)
    entered = threading.Event()
    release = threading.Event()
    errors = []

    def receive_first():
        try:
            uploads.receive(
                _BlockedStream(body, entered, release), len(body), content_type)
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    first = threading.Thread(target=receive_first)
    first.start()
    assert entered.wait(timeout=1)
    try:
        with pytest.raises(
                uploads.StagingQuotaError, match="preserving device reserve"):
            uploads.receive(io.BytesIO(body), len(body), content_type)
    finally:
        release.set()
        first.join(timeout=3)
    assert not errors


def test_preflight_metadata_growth_is_rechecked_against_staging_cap(
        upload_layout, monkeypatch):
    body, content_type = _multipart(b"small-compressed-package")
    upload = uploads.receive(io.BytesIO(body), len(body), content_type)
    used = uploads._tree_size(paths.uploads_dir())
    monkeypatch.setattr(paths, "MAX_UPLOAD_STAGING_BYTES", used + 256)

    with pytest.raises(
            uploads.StagingQuotaError, match="after metadata update"):
        uploads.update(
            upload["upload_id"], status="preflighted",
            preflight={"manifest": {"description": "x" * 4096}})

    unchanged = uploads.load(upload["upload_id"])
    assert unchanged["status"] == "uploaded"
    assert "preflight" not in unchanged


def test_failed_and_timed_out_receive_remove_private_reservation(upload_layout):
    malformed = b"--wrong\r\n"
    with pytest.raises(uploads.MultipartError, match="does not start"):
        uploads.receive(
            io.BytesIO(malformed), len(malformed),
            "multipart/form-data; boundary=expected")
    assert os.listdir(paths.uploads_dir()) == []

    body, content_type = _multipart(b"timeout")

    class Delayed(io.BytesIO):
        def read(self, size=-1):
            time.sleep(0.02)
            return super().read(size)

    with pytest.raises(uploads.MultipartError, match="timed out"):
        uploads.receive(
            Delayed(body), len(body), content_type, total_timeout=0.001)
    assert os.listdir(paths.uploads_dir()) == []


def test_http_socket_timeout_interrupts_stalled_body_and_cleans_reservation(
        upload_layout, monkeypatch):
    monkeypatch.setattr(paths, "UPLOAD_SOCKET_TIMEOUT_SEC", 0.05)
    body, content_type = _multipart(b"never-sent")
    httpd = server._AppHTTPServer(("127.0.0.1", 0), server._Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    client = socket.create_connection(
        ("127.0.0.1", httpd.server_port), timeout=2)
    try:
        request = (
            "POST /api/app-center/v1/uploads HTTP/1.1\r\n"
            "Host: camera.local\r\n"
            "Origin: https://camera.local\r\n"
            "X-Forwarded-Proto: https\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"{server.V1_TRUSTED_EDGE_HEADER}: "
            f"{server.V1_TRUSTED_EDGE_VALUE}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        client.sendall(request)
        response = bytearray()
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            response.extend(chunk)
        assert b" 400 " in bytes(response).split(b"\r\n", 1)[0]
        assert b"multipart upload socket timed out" in response
    finally:
        client.close()
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
    assert os.listdir(paths.uploads_dir()) == []


def test_startup_recovery_removes_young_incoming_and_active_orphan(
        upload_layout):
    root = paths.ensure_uploads()
    incoming = os.path.join(root, ".incoming-dead-process")
    os.mkdir(incoming)
    with open(os.path.join(incoming, "partial"), "wb") as output:
        output.write(b"partial")
    corrupt_id = "d" * 32
    corrupt = os.path.join(root, corrupt_id)
    os.mkdir(corrupt)
    with open(os.path.join(corrupt, "upload.json"), "w") as output:
        output.write("{not-json")

    body, content_type = _multipart(b"installing")
    active = uploads.receive(io.BytesIO(body), len(body), content_type)
    uploads.update(active["upload_id"], status="installing")

    recovered = uploads.recover_startup()

    assert ".incoming-dead-process" in recovered["removed"]
    assert corrupt_id in recovered["removed"]
    assert active["upload_id"] in recovered["interrupted"]
    assert active["upload_id"] in recovered["removed"]
    assert os.listdir(root) == []


def test_failed_startup_delete_leaves_interrupted_record_for_runtime_gc(
        upload_layout, monkeypatch):
    body, content_type = _multipart(b"installing")
    active = uploads.receive(io.BytesIO(body), len(body), content_type)
    uploads.update(active["upload_id"], status="installing")
    directory = os.path.dirname(active["package_path"])
    real_remove = uploads._remove_entry
    failed_once = False

    def fail_once(path):
        nonlocal failed_once
        if path == directory and not failed_once:
            failed_once = True
            return False
        return real_remove(path)

    monkeypatch.setattr(uploads, "_remove_entry", fail_once)
    recovered = uploads.recover_startup()
    assert active["upload_id"] in recovered["interrupted"]
    assert active["upload_id"] not in recovered["removed"]
    assert uploads.load(active["upload_id"])["status"] == "interrupted"

    monkeypatch.setattr(paths, "UPLOAD_TTL_SEC", 1)
    assert active["upload_id"] in uploads.gc_expired(now=time.time() + 2)
    assert not os.path.lexists(directory)


def _wait_terminal(manager, operation_id, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = next(item for item in manager.list()
                       if item["id"] == operation_id)
        if current["status"] in operations.TERMINAL:
            return current
        time.sleep(0.005)
    pytest.fail("operation did not finish")


def test_upload_operation_idempotency_is_persisted_and_request_bound(
        upload_layout):
    upload_id = "a" * 32
    fingerprint = "b" * 64
    journal = paths.operation_state_file()
    callbacks = []
    manager = operations.OperationManager(journal)
    try:
        original = manager.submit(
            "install", "demo", lambda: callbacks.append("original"),
            upload_id=upload_id, request_fingerprint=fingerprint)
        finished = _wait_terminal(manager, original["id"])
        replay = manager.submit(
            "install", "demo", lambda: callbacks.append("replay"),
            upload_id=upload_id, request_fingerprint=fingerprint)
        assert replay["id"] == original["id"]
        assert callbacks == ["original"]
        assert finished["upload_id"] == upload_id
        assert finished["request_fingerprint"] == fingerprint

        with pytest.raises(operations.OperationBusyError, match="different"):
            manager.submit(
                "restart", "demo", lambda: None, upload_id=upload_id,
                request_fingerprint=fingerprint)
        with pytest.raises(operations.OperationBusyError, match="does not match"):
            manager.submit(
                "install", "demo", lambda: None, upload_id=upload_id,
                request_fingerprint="c" * 64)
    finally:
        manager.close()

    replay_callbacks = []
    restarted = operations.OperationManager(journal)
    try:
        assert restarted.for_upload(upload_id)["id"] == original["id"]
        replay = restarted.submit(
            "install", "demo", lambda: replay_callbacks.append(True),
            upload_id=upload_id, request_fingerprint=fingerprint)
        assert replay["id"] == original["id"]
        assert replay_callbacks == []
        with open(journal) as source:
            persisted = json.load(source)["operations"]
        assert persisted[-1]["upload_id"] == upload_id
    finally:
        restarted.close()


def test_operation_manager_lazy_initialization_is_singleton_under_concurrency(
        upload_layout):
    barrier = threading.Barrier(9)
    managers = []
    errors = []

    def get_manager():
        try:
            barrier.wait(timeout=2)
            managers.append(server._operation_manager())
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=get_manager) for _ in range(8)]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=2)
    for thread in threads:
        thread.join(timeout=3)

    assert not errors
    assert len(managers) == 8
    assert len({id(manager) for manager in managers}) == 1


def test_worker_continues_after_running_journal_persistence_failure(
        upload_layout, monkeypatch):
    manager = operations.OperationManager(paths.operation_state_file())
    real_save = manager._save
    save_calls = 0
    callbacks = []

    def fail_running_save_once():
        nonlocal save_calls
        save_calls += 1
        if save_calls == 2:
            raise OSError("injected journal fsync failure")
        return real_save()

    monkeypatch.setattr(manager, "_save", fail_running_save_once)
    try:
        first = manager.submit(
            "install", "one", lambda: callbacks.append("one"))
        assert _wait_terminal(manager, first["id"])["status"] == "succeeded"
        second = manager.submit(
            "install", "two", lambda: callbacks.append("two"))
        assert _wait_terminal(manager, second["id"])["status"] == "succeeded"
        assert callbacks == ["one", "two"]
    finally:
        manager.close()


def test_finalize_replay_after_upload_cleanup_returns_original_operation(
        upload_layout, monkeypatch):
    permissions = {
        "sdk": [],
        "filesystem": {"read": [], "write": []},
        "network": {"listen": [], "outbound": []},
        "devices": [],
    }
    body, content_type = _multipart(b"single-use-install")
    upload = uploads.receive(io.BytesIO(body), len(body), content_type)
    uploads.update(upload["upload_id"], status="preflighted", preflight={
        "manifest": {
            "manifest_version": 2,
            "id": "demo",
            "name": "Demo",
            "version": "1.0.0",
            "permissions": permissions,
        },
        "release_id": "demo-release-1",
        "signature": {"status": "verified"},
        "install_context": {
            "mode": "new",
            "target_version": "1.0.0",
            "target_release_id": "demo-release-1",
            "confirmation_required": [],
        },
        "checks": [{"id": "resources", "passed": True}],
    })
    installs = []

    def install_once(*args, **kwargs):
        installs.append((args, kwargs))
        return {"id": "demo", "installed": True}

    monkeypatch.setattr(server, "do_install", install_once)
    request = {
        "upload_id": upload["upload_id"],
        "permissions_confirmed": True,
        "permissions": permissions,
        "running_upgrade_confirmed": False,
        "force_reinstall_confirmed": False,
    }
    original = server.do_v1_install(request)["operation"]
    terminal = _wait_terminal(server._operation_manager(), original["id"])
    assert terminal["status"] == "succeeded"
    assert not os.path.lexists(os.path.dirname(upload["package_path"]))

    replay = server.do_v1_install(dict(request))
    assert replay["idempotent_replay"] is True
    assert replay["operation"]["id"] == original["id"]
    assert len(installs) == 1

    with pytest.raises(
            operations.OperationBusyError, match="does not match"):
        server.do_v1_install({**request, "force_reinstall_confirmed": True})

    # The upload bytes are gone and the daemon-local manager can be recreated;
    # the journal correlation still recovers the response without reinstalling.
    server._operation_manager_instance.close()
    server._operation_manager_instance = None
    server._operation_manager_layout = None
    restarted_replay = server.do_v1_install(dict(request))
    assert restarted_replay["idempotent_replay"] is True
    assert restarted_replay["operation"]["id"] == original["id"]
    assert len(installs) == 1

"""Device-owned store tasks: real signed archives, mocked network, no hardware."""
import base64
import copy
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from appmgr import installer, manifest as manifest_contract, operations, paths, server, store_download, store_tasks, uploads
from appmgr.tests.test_installer_v2 import package_v2


@pytest.fixture
def layout(tmp_path, monkeypatch):
    for name in ("apps", "appmgr", "stage"):
        (tmp_path / name).mkdir()
    for key, value in {
        "APPS_DIR": tmp_path / "apps", "APPMGR_DIR": tmp_path / "appmgr",
        "APPSTAGE_DIR": tmp_path / "stage", "STATE_FILE": tmp_path / "apps/state.json",
        "VENVS_DIR": tmp_path / "venvs", "RELEASE_PUBKEY": tmp_path / "public.pem",
        "OWNER_KEYS_DIR": tmp_path / "owners",
    }.items():
        monkeypatch.setattr(paths, key, str(value))
    monkeypatch.setattr(paths, "ALLOWED_PKG_ROOTS", (str(tmp_path),))
    monkeypatch.setattr(paths, "MAX_PKG_BYTES", 1024 * 1024)
    monkeypatch.setattr(paths, "MAX_UPLOAD_STAGING_BYTES", 4 * 1024 * 1024)
    monkeypatch.setattr(paths, "MAX_STAGED_UPLOADS", 8)
    monkeypatch.setattr(paths, "MIN_UPLOAD_FREE_BYTES", 0)
    monkeypatch.setattr(paths, "REQUIRE_SIGNATURE", True)
    monkeypatch.setenv("APPMGR_PLATFORM_ARCH", "aarch64")
    monkeypatch.setenv("APPMGR_PLATFORM_PYTHON", sys.executable)
    monkeypatch.setenv("APPMGR_WHEELHOUSE_DIR", str(tmp_path / "wheelhouse"))
    monkeypatch.setattr(server, "_service_stopping", False)
    monkeypatch.setattr(server, "_store_manager_instance", None)
    monkeypatch.setattr(server, "_store_manager_layout", None)
    monkeypatch.setattr(server, "_operation_manager_instance", None)
    monkeypatch.setattr(server, "_operation_manager_layout", None)
    monkeypatch.setattr(server, "_coordinator_instance", None)
    monkeypatch.setattr(server, "_coordinator_layout", None)
    monkeypatch.setattr(server, "_audit", lambda *_args, **_kwargs: None)
    with uploads._UPLOAD_LOCK:
        uploads._LIVE_INCOMING.clear()
        uploads._ORPHANED_ACTIVE.clear()
    yield tmp_path
    if server._store_manager_instance is not None:
        server._store_manager_instance.close()
    if server._operation_manager_instance is not None:
        server._operation_manager_instance.close()


@pytest.fixture
def app(layout, monkeypatch):
    archive, manifest, lock = package_v2(str(layout / "fixture.tar.gz"))
    key = layout / "private.pem"
    subprocess.run(["openssl", "ecparam", "-genkey", "-name", "prime256v1", "-noout", "-out", str(key)],
                   check=True, capture_output=True)
    subprocess.run(["openssl", "pkey", "-in", str(key), "-pubout", "-out", paths.RELEASE_PUBKEY],
                   check=True, capture_output=True)
    os.chmod(paths.RELEASE_PUBKEY, 0o644)
    signature = base64.b64encode(subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(key), archive], check=True, capture_output=True).stdout).decode()
    payload = Path(archive).read_bytes()
    result = {"id": manifest["id"], "version": manifest["version"], "package": {
        "filename": "v2-app-1.0.0-arm64.tar.gz", "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(), "signature": signature,
    }, "manifest": manifest, "lock": lock, "payload": payload}

    def resolve(app_id, version, *, package_sha256, cancelled=None):
        assert (app_id, version, package_sha256) == (
            result["id"], result["version"], result["package"]["sha256"])
        return result

    def download(candidate, package_path, signature_path, *, cancelled, on_progress, **kwargs):
        assert candidate is result
        with open(package_path, "xb") as output:
            output.write(payload)
        with open(signature_path, "x") as output:
            output.write(signature)
        on_progress(len(payload), len(payload))

    monkeypatch.setattr(store_download, "resolve_app", resolve)
    monkeypatch.setattr(store_download, "download_package", download)
    return result


def body(app):
    return {"app_id": app["id"], "version": app["version"], "package_sha256": app["package"]["sha256"]}


def wait_task(manager, task_id, states=("awaiting_confirmation",), timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = manager.get(task_id)
        if task["status"] in states:
            return task
        if task["status"] == "failed" and "failed" not in states:
            pytest.fail(f"task failed: {task}")
        time.sleep(0.005)
    pytest.fail(f"task did not reach {states}: {manager.get(task_id)}")


def wait_operation(upload_id):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = server._operation_manager().for_upload(upload_id)
        if result and result["status"] in operations.TERMINAL:
            return result
        time.sleep(0.005)
    pytest.fail("installation operation did not finish")


def test_download_is_device_owned_signed_preflight_and_survives_manager_reopen(layout, app):
    manager = server._store_manager()
    submitted = manager.create(body(app))
    task = wait_task(manager, submitted["task"]["id"])
    assert task["package_sha256"] == app["package"]["sha256"]
    assert task["progress"]["percent"] == 100
    preview = task["inspection"]["preflight"]
    assert preview["source"] == "app-store"
    assert preview["channel"] == "app-center-v1-store"
    assert preview["signature"]["status"] == "verified"
    assert preview["signature"]["unsigned_install_allowed"] is False
    assert preview["permissions"] == app["manifest"]["permissions"]
    assert not Path(paths.app_dir(app["id"])).exists()
    manager.close()
    reopened = store_tasks.StoreTaskManager(preflight=server._preflight_v1_upload_record,
        finalize=server.do_v1_install, operation_manager=server._operation_manager(),
        snapshot=server._store_upload_snapshot)
    try:
        recovered = reopened.get(task["id"])
        assert recovered["status"] == "awaiting_confirmation"
        assert recovered["inspection"] == task["inspection"]
        assert reopened.create(body(app))["idempotent_replay"] is True
        cancelled = reopened.cancel(task["id"])
        assert cancelled["status"] == "cancelled"
        assert not Path(uploads._directory(task["upload_id"])).exists()
    finally:
        reopened.close()


def test_blocked_download_does_not_block_lifecycle_and_cancel_cleans_partial(layout, app, monkeypatch):
    entered = threading.Event()

    def download(_app, package_path, signature_path, *, cancelled, **kwargs):
        Path(package_path).write_bytes(b"partial")
        entered.set()
        deadline = time.monotonic() + 3
        while not cancelled() and time.monotonic() < deadline:
            time.sleep(0.005)
        raise store_download.StoreDownloadError("store_cancelled", "cancelled")

    monkeypatch.setattr(store_download, "download_package", download)
    manager = server._store_manager()
    task = manager.create(body(app))["task"]
    assert entered.wait(1)
    duplicate = manager.create(body(app))
    assert duplicate["task"]["id"] == task["id"]
    assert duplicate["idempotent_replay"] is True
    with pytest.raises(store_tasks.StoreTaskConflict):
        manager.create({**body(app), "version": "2.0.0"})
    lifecycle_finished = threading.Event()
    server._operation_manager().submit("stop", "other-app", lifecycle_finished.set)
    assert lifecycle_finished.wait(0.5), "download occupied the lifecycle worker"
    assert manager.cancel(task["id"])["status"] == "cancelled"
    manager._queue.join()
    assert os.listdir(paths.uploads_dir()) == []
    assert not manager._cancel


def test_download_and_browser_uploads_share_disk_quota(layout, app, monkeypatch):
    monkeypatch.setattr(paths, "MAX_UPLOAD_STAGING_BYTES", app["package"]["size"])
    calls = []
    monkeypatch.setattr(store_download, "download_package", lambda *_args, **_kwargs: calls.append(1))
    manager = server._store_manager()
    task = wait_task(manager, manager.create(body(app))["task"]["id"], ("failed",))
    assert "staging byte limit" in task["error"]["message"]
    assert calls == []
    assert os.listdir(paths.uploads_dir()) == []


@pytest.mark.parametrize("change,code", [
    ("signature", "storeSignatureUnverified"),
    ("manifest-id", "storeManifestMismatch"),
    ("manifest-version", "storeManifestUnsupported"),
    ("external", "storeExternalModelsUnsupported"),
])
def test_preflight_failures_never_expose_installable_task(layout, app, monkeypatch, change, code):
    manager = server._store_manager()
    if change == "signature":
        def download(_app, package_path, signature_path, *, on_progress, **kwargs):
            Path(package_path).write_bytes(app["payload"])
            Path(signature_path).write_text("MAYCAQECAQE=")
            on_progress(len(app["payload"]), len(app["payload"]))
        monkeypatch.setattr(store_download, "download_package", download)
    else:
        real_preflight = manager.preflight
        def changed(upload):
            result = copy.deepcopy(real_preflight(upload))
            manifest = result["preflight"]["manifest"]
            if change == "manifest-id":
                manifest["id"] = "wrong-app"
            elif change == "manifest-version":
                manifest["manifest_version"] = 1
            elif change == "external":
                manifest["artifacts"] = [{"source": "catalog"}]
            return result
        manager.preflight = changed
    task = wait_task(manager, manager.create(body(app))["task"]["id"], ("failed",))
    assert task["error"]["code"] == code
    assert "inspection" not in task
    assert os.listdir(paths.uploads_dir()) == []


def test_confirmation_reuses_v1_permissions_and_install_binding(layout, app, monkeypatch):
    manager = server._store_manager()
    task = wait_task(manager, manager.create(body(app))["task"]["id"])
    permissions = task["inspection"]["preflight"]["permissions"]
    installs = []
    def install(package_path, signature, **kwargs):
        info = installer.inspect(package_path, signature, allow_unsigned=kwargs["allow_unsigned"])
        server._assert_v1_preflight_binding(kwargs["expected_preflight"], info)
        installs.append(kwargs)
        return {"installed": True}
    monkeypatch.setattr(server, "do_install", install)
    with pytest.raises(ValueError, match="explicitly confirmed"):
        manager.install(task["id"], {"permissions": permissions})
    with pytest.raises(ValueError, match="do not match"):
        manager.install(task["id"], {"permissions_confirmed": True, "permissions": {}})
    assert manager.get(task["id"])["status"] == "awaiting_confirmation"
    assert installs == []
    confirmation = {"permissions_confirmed": True, "permissions": permissions}
    accepted = manager.install(task["id"], confirmation)
    final = wait_task(manager, task["id"], ("succeeded",))
    assert final["operation"]["id"] == accepted["operation"]["id"]
    assert len(installs) == 1
    assert installs[0]["allow_unsigned"] is False
    assert installs[0]["_enforce_v1_confirmations"] is True
    assert manager.install(task["id"], confirmation)["idempotent_replay"] is True
    assert len(installs) == 1


def test_tampered_staging_after_preflight_is_rejected_before_install(layout, app):
    manager = server._store_manager()
    task = wait_task(manager, manager.create(body(app))["task"]["id"])
    package = uploads.load(task["upload_id"])["package_path"]
    with open(package, "ab") as output:
        output.write(b"tampered")
    with pytest.raises(ValueError, match="changed after preflight"):
        manager.install(task["id"], {"permissions_confirmed": True,
            "permissions": task["inspection"]["preflight"]["permissions"]})
    assert server._operation_manager().list() == []


def test_interrupted_download_recovery_marks_failed_and_removes_staging(layout, app):
    manager = server._store_manager()
    task = wait_task(manager, manager.create(body(app))["task"]["id"])
    manager.close()
    journal = json.loads(Path(manager.journal).read_text())
    journal["tasks"][0]["status"] = "preflighting"
    Path(manager.journal).write_text(json.dumps(journal))
    reopened = store_tasks.StoreTaskManager(preflight=manager.preflight, finalize=manager.finalize,
        operation_manager=server._operation_manager(), snapshot=server._store_upload_snapshot)
    try:
        assert reopened.get(task["id"])["error"]["code"] == "storeInterrupted"
        assert os.listdir(paths.uploads_dir()) == []
    finally:
        reopened.close()


def test_store_http_endpoints_and_same_origin_guard(layout, app, monkeypatch):
    monkeypatch.setattr(store_download, "fetch_catalog", lambda: {"schema": 1, "apps": []})
    httpd = server._AppHTTPServer(("127.0.0.1", 0), server._Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    def request(method, path, value=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=3)
        try:
            connection.request(method, "/api/app-center/v1/store/" + path,
                body=json.dumps(value) if value is not None else None,
                headers={"Content-Type": "application/json", **(headers or {})})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()
    try:
        assert request("GET", "catalog") == (200, {"schema": 1, "apps": []})
        assert request("POST", "tasks", body(app), {"Origin": "https://attacker.invalid"})[0] == 403
        status, submitted = request("POST", "tasks", body(app))
        assert status == 202
        task_id = submitted["task"]["id"]
        wait_task(server._store_manager(), task_id)
        assert request("GET", "tasks")[1]["tasks"][0]["id"] == task_id
        assert request("GET", "tasks/" + task_id)[1]["task"]["status"] == "awaiting_confirmation"
        assert request("POST", f"tasks/{task_id}/install", {})[0] == 400
        assert request("DELETE", "tasks/" + task_id)[1]["task"]["status"] == "cancelled"
        assert request("GET", "tasks/" + "f" * 32)[0] == 404
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(2)


def test_bounded_queue_includes_cancelled_downloads_until_worker_releases_them(layout, app, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def resolve(*_args, cancelled=None, **_kwargs):
        entered.set()
        assert release.wait(2)
        if cancelled():
            raise store_download.StoreDownloadError("store_cancelled", "cancelled")
        return app
    monkeypatch.setattr(store_download, "resolve_app", resolve)
    manager = server._store_manager()
    try:
        first = manager.create(body(app))["task"]
        assert entered.wait(1)
        others = [manager.create({**body(app), "app_id": f"other-{index}"})["task"]
                  for index in range(3)]
        with pytest.raises(store_tasks.StoreTaskConflict, match="limit") as error:
            manager.create({**body(app), "app_id": "overflow"})
        assert error.value.code == "storeTaskLimitReached"
        for task in [first, *others]:
            manager.cancel(task["id"])
        with pytest.raises(store_tasks.StoreTaskConflict, match="limit"):
            manager.create({**body(app), "app_id": "still-full"})
    finally:
        release.set()
        manager._queue.join()
    assert not manager._cancel
    assert all(task["status"] == "cancelled" for task in manager.list())


def test_full_v2_install_uses_real_installer_in_temporary_device_layout(layout, app):
    manager = server._store_manager()
    task = wait_task(manager, manager.create(body(app))["task"]["id"])
    manager.install(task["id"], {"permissions_confirmed": True,
        "permissions": task["inspection"]["preflight"]["permissions"]})
    final = wait_task(manager, task["id"], ("succeeded",), timeout=15)
    installed_manifest = json.loads(Path(paths.app_dir(app["id"]), "manifest.json").read_text())
    assert installed_manifest == app["manifest"]
    assert final["operation"]["result"]["id"] == app["id"]
    assert os.listdir(paths.uploads_dir()) == []


def test_recovery_correlates_install_accepted_before_task_journal_was_updated(layout, app, monkeypatch):
    manager = server._store_manager()
    task = wait_task(manager, manager.create(body(app))["task"]["id"])
    monkeypatch.setattr(server, "do_install", lambda *_args, **_kwargs: {"installed": True})
    result = server.do_v1_install({"upload_id": task["upload_id"], "permissions_confirmed": True,
        "permissions": task["inspection"]["preflight"]["permissions"]})
    wait_operation(task["upload_id"])
    manager.close()
    reopened = store_tasks.StoreTaskManager(preflight=manager.preflight, finalize=manager.finalize,
        operation_manager=server._operation_manager(), snapshot=server._store_upload_snapshot)
    try:
        recovered = reopened.get(task["id"])
        assert recovered["status"] == "succeeded"
        assert recovered["operation"]["id"] == result["operation"]["id"]
    finally:
        reopened.close()


def test_external_finalize_prevents_store_cancellation_from_deleting_active_upload(layout, app, monkeypatch):
    manager = server._store_manager()
    task = wait_task(manager, manager.create(body(app))["task"]["id"])
    entered, release = threading.Event(), threading.Event()
    def install(*_args, **_kwargs):
        entered.set()
        assert release.wait(2)
        return {"installed": True}
    monkeypatch.setattr(server, "do_install", install)
    try:
        server.do_v1_install({"upload_id": task["upload_id"], "permissions_confirmed": True,
            "permissions": task["inspection"]["preflight"]["permissions"]})
        assert entered.wait(1)
        with pytest.raises(store_tasks.StoreTaskConflict, match="already been submitted"):
            manager.cancel(task["id"])
        assert Path(uploads.load(task["upload_id"])["package_path"]).exists()
    finally:
        release.set()
        wait_operation(task["upload_id"])


def test_fast_external_install_winning_cancel_race_cannot_be_reported_cancelled(layout, app, monkeypatch):
    manager = server._store_manager()
    task = wait_task(manager, manager.create(body(app))["task"]["id"])
    monkeypatch.setattr(server, "do_install", lambda *_args, **_kwargs: {"installed": True})
    original_discard = manager.discard
    def competing_finalize_then_discard(upload_id):
        server.do_v1_install({"upload_id": upload_id, "permissions_confirmed": True,
            "permissions": task["inspection"]["preflight"]["permissions"]})
        assert wait_operation(upload_id)["status"] == "succeeded"
        assert not os.path.exists(uploads._directory(upload_id))
        return original_discard(upload_id)
    manager.discard = competing_finalize_then_discard
    with pytest.raises(store_tasks.StoreTaskConflict, match="already been submitted"):
        manager.cancel(task["id"])
    assert manager.get(task["id"])["status"] == "succeeded"


def test_task_snapshot_serializes_external_finalize_with_upload_inspection(layout, app, monkeypatch):
    manager = server._store_manager()
    task = wait_task(manager, manager.create(body(app))["task"]["id"])
    monkeypatch.setattr(server, "do_install", lambda *_args, **_kwargs: {"installed": True})
    entered, finished = threading.Event(), threading.Event()
    errors = []
    def finalize():
        entered.set()
        try:
            server.do_v1_install({"upload_id": task["upload_id"], "permissions_confirmed": True,
                "permissions": task["inspection"]["preflight"]["permissions"]})
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()
    contender = threading.Thread(target=finalize)
    original_load = uploads.load
    triggered = False
    def load(upload_id):
        nonlocal triggered
        if not triggered and threading.current_thread() is threading.main_thread():
            triggered = True
            contender.start()
            assert entered.wait(1)
            assert not finished.wait(0.03), "finalize entered midway through the task snapshot"
        return original_load(upload_id)
    monkeypatch.setattr(uploads, "load", load)
    current = manager.get(task["id"])
    contender.join(2)
    assert finished.is_set()
    assert not errors
    assert current["status"] != "failed"
    wait_operation(task["upload_id"])
    assert manager.get(task["id"])["status"] == "succeeded"


def test_upload_removal_before_public_snapshot_returns_expired_task_instead_of_get_error(layout, app, monkeypatch):
    manager = server._store_manager()
    task = wait_task(manager, manager.create(body(app))["task"]["id"])
    original_public = manager._public
    def public(record):
        server.do_v1_cancel_upload(record["upload_id"])
        return original_public(record)
    monkeypatch.setattr(manager, "_public", public)
    current = manager.get(task["id"])
    assert current["status"] == "failed"
    assert current["error"]["code"] == "storeTaskExpired"
    assert "inspection" not in current


def test_cancel_during_preflight_discards_package_without_install_submission(layout, app):
    manager = server._store_manager()
    entered, release = threading.Event(), threading.Event()
    original_preflight = manager.preflight
    def preflight(upload):
        result = original_preflight(upload)
        entered.set()
        assert release.wait(2)
        return result
    manager.preflight = preflight
    task = manager.create(body(app))["task"]
    assert entered.wait(1)
    try:
        assert manager.get(task["id"])["status"] == "preflighting"
        assert manager.cancel(task["id"])["status"] == "cancelled"
    finally:
        release.set()
        manager._queue.join()
    assert os.listdir(paths.uploads_dir()) == []
    assert server._operation_manager().list() == []


@pytest.mark.parametrize("first_fails", [False, True])
def test_other_app_downloads_and_queues_install_while_first_install_is_running(
        layout, app, monkeypatch, first_fails):
    """A slow (or failed) install must not monopolize store retrieval or lose B."""
    second = copy.deepcopy(app)
    second["id"] = "second-app"
    second["manifest"]["id"] = second["id"]
    with tarfile.open(fileobj=io.BytesIO(app["payload"]), mode="r:gz") as archive:
        files = {member.name: archive.extractfile(member).read()
                 for member in archive.getmembers() if member.isfile()
                 and member.name not in (manifest_contract.BOM_PATH, manifest_contract.RELEASE_LOCK_PATH)}
    files["manifest.json"] = manifest_contract.canonical_json(second["manifest"])
    records = {name: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
               for name, data in files.items()}
    second["lock"], bom = manifest_contract.make_release_metadata(second["manifest"], records)
    files[manifest_contract.BOM_PATH] = bom
    files[manifest_contract.RELEASE_LOCK_PATH] = manifest_contract.canonical_json(second["lock"])
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, data in sorted(files.items()):
            member = tarfile.TarInfo(name)
            member.size, member.mode = len(data), 0o644
            archive.addfile(member, io.BytesIO(data))
    second["payload"] = output.getvalue()
    second["package"] = {
        "filename": "second-app-1.0.0-arm64.tar.gz", "size": len(second["payload"]),
        "sha256": hashlib.sha256(second["payload"]).hexdigest(),
        "signature": base64.b64encode(subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", str(layout / "private.pem")],
            input=second["payload"], check=True, capture_output=True).stdout).decode(),
    }
    candidates = {candidate["id"]: candidate for candidate in (app, second)}
    def resolve(app_id, version, *, package_sha256, **_kwargs):
        candidate = candidates[app_id]
        assert (version, package_sha256) == (candidate["version"], candidate["package"]["sha256"])
        return candidate
    downloaded = []
    def download(candidate, package_path, signature_path, *, on_progress, **_kwargs):
        Path(package_path).write_bytes(candidate["payload"])
        Path(signature_path).write_text(candidate["package"]["signature"])
        on_progress(candidate["package"]["size"], candidate["package"]["size"])
        downloaded.append(candidate["id"])
    monkeypatch.setattr(store_download, "resolve_app", resolve)
    monkeypatch.setattr(store_download, "download_package", download)
    first_entered, release_first, second_entered = (threading.Event() for _ in range(3))
    execution_order = []
    def install(package_path, signature, **kwargs):
        # Keep the real mutation gate and package/confirmation binding while
        # replacing only publication, so no application or hardware is started.
        with server.busy_gate(wait_timeout=kwargs["_busy_timeout"]):
            assert kwargs["allow_unsigned"] is False
            assert kwargs["_enforce_v1_confirmations"] is True
            inspected = installer.inspect(package_path, signature)
            server._assert_v1_preflight_binding(kwargs["expected_preflight"], inspected)
            app_id = inspected["id"]
            execution_order.append((app_id, "entered"))
            if app_id == app["id"]:
                first_entered.set()
                assert release_first.wait(5)
                execution_order.append((app_id, "finished"))
                if first_fails:
                    raise installer.InstallError("simulated first application install failure")
            else:
                second_entered.set()
                execution_order.append((app_id, "finished"))
            return {"id": app_id, "installed": True}
    monkeypatch.setattr(server, "do_install", install)
    manager = server._store_manager()
    first_task = wait_task(manager, manager.create(body(app))["task"]["id"])
    manager.install(first_task["id"], {"permissions_confirmed": True,
        "permissions": first_task["inspection"]["preflight"]["permissions"]})
    assert first_entered.wait(1)
    second_task = None
    try:
        second_task = wait_task(manager, manager.create(body(second))["task"]["id"])
        assert downloaded == [app["id"], second["id"]]
        assert manager.get(first_task["id"])["operation"]["status"] == "running"
        confirmation = {"permissions_confirmed": True,
            "permissions": second_task["inspection"]["preflight"]["permissions"]}
        submitted = manager.install(second_task["id"], confirmation)
        assert submitted["operation"]["status"] == "queued"
        assert submitted["task"]["status"] == "installing"
        assert not second_entered.is_set()
        assert manager.create(body(second))["idempotent_replay"] is True
        assert manager.install(second_task["id"], confirmation)["idempotent_replay"] is True
        assert len(server._operation_manager().list()) == 2
    finally:
        release_first.set()
    first_result = wait_operation(first_task["upload_id"])
    assert first_result["status"] == ("failed" if first_fails else "succeeded")
    assert second_task is not None
    assert wait_task(manager, second_task["id"], ("succeeded",))["operation"]["status"] == "succeeded"
    assert execution_order == [(app["id"], "entered"), (app["id"], "finished"),
                               (second["id"], "entered"), (second["id"], "finished")]

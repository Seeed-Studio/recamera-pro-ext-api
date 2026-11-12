"""Browser display defaults: real HTTP, concurrency, persistence and lifecycle."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import http.client
import importlib
import json
import os
from pathlib import Path
import stat
import sys
import tarfile
import threading
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from appmgr import (browser_overlay, installer, operations, paths, server,
                    state, supervisor, visualization)


APP = "overlay-app"
BASE = "/api/app-center/v1"


@pytest.fixture
def layout(tmp_path, monkeypatch):
    for key, name in (("APPS_DIR", "apps"), ("APPMGR_DIR", "appmgr"),
                      ("APPDATA_DIR", "appdata"), ("VENVS_DIR", "venvs"),
                      ("APPSTAGE_DIR", "stage")):
        directory = tmp_path / name
        directory.mkdir()
        monkeypatch.setattr(paths, key, str(directory))
    monkeypatch.setattr(paths, "STATE_FILE", str(tmp_path / "apps/state.json"))
    monkeypatch.setattr(paths, "ALLOWED_PKG_ROOTS", (str(tmp_path),))
    monkeypatch.setattr(paths, "REQUIRE_SIGNATURE", False)
    monkeypatch.setattr(server, "_result_hub_instance", None)
    monkeypatch.setattr(server, "_coordinator_instance", None)
    monkeypatch.setattr(server, "_coordinator_layout", None)
    monkeypatch.setattr(server, "_service_stopping", False)
    if server._operation_manager_instance is not None:
        server._operation_manager_instance.close()
    monkeypatch.setattr(server, "_operation_manager_instance", None)
    monkeypatch.setattr(server, "_operation_manager_layout", None)
    server.cache_clear()
    yield tmp_path
    if server._operation_manager_instance is not None:
        server._operation_manager_instance.close()
        server._operation_manager_instance = None


@pytest.fixture
def httpd(layout):
    httpd = server._AppHTTPServer(("127.0.0.1", 0), server._Handler)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()
    worker.join(timeout=3)


def install_dir(app_id=APP, **extra):
    directory = Path(paths.app_dir(app_id))
    directory.mkdir(exist_ok=True)
    (directory / "manifest.json").write_text(json.dumps({
        "id": app_id, "version": "1.0.0", "entry": "app.py", **extra}))
    server.cache_clear()
    return directory


def request(httpd, method, route, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
    try:
        connection.request(method, BASE + route,
                           body=json.dumps(body) if body is not None else None,
                           headers={"Content-Type": "application/json", **(headers or {})})
        response = connection.getresponse()
        return response.status, json.loads(response.read()), dict(response.getheaders())
    finally:
        connection.close()


def put(httpd, app_id=APP, enabled=True, revision=0, **kwargs):
    return request(httpd, "PUT", "/apps/%s/browser-overlay" % app_id,
                   {"enabled": enabled, "revision": revision}, **kwargs)


def document():
    return server.do_get_browser_overlays()


def save(app_id=APP, enabled=True, revision=0):
    return server.do_set_browser_overlay(app_id, {"enabled": enabled, "revision": revision})


def package(root, version):
    source = root / ("source-" + version)
    source.mkdir()
    (source / "manifest.json").write_text(json.dumps({
        "id": APP, "name": "Overlay", "version": version,
        "entry": "app.py", "config_schema": {}}))
    (source / "app.py").write_text("# test package\n")
    target = root / (version + ".tar.gz")
    with tarfile.open(target, "w:gz") as archive:
        for name in ("manifest.json", "app.py"):
            archive.add(source / name, arcname=name)
    return str(target)


def test_http_snapshot_save_cas_and_durable_restart(httpd):
    install_dir()
    status, body, headers = request(httpd, "GET", "/browser-overlays")
    assert status == 200 and headers["Cache-Control"] == "no-store"
    assert body == {"schema_version": 1, "revision": 0, "preferences": {}}
    assert not Path(browser_overlay.config_path()).exists()
    status, saved, _ = put(httpd)
    assert status == 200
    assert saved == {"schema_version": 1, "revision": 1,
                     "preferences": {APP: {"enabled": True, "revision": 1}}}
    assert stat.S_IMODE(Path(browser_overlay.config_path()).stat().st_mode) == 0o600
    # No cached process state supplies the persisted preference.
    importlib.reload(browser_overlay)
    assert document() == saved
    status, conflict, _ = put(httpd, enabled=False)
    assert (status, conflict) == (409, {"error": "revision_conflict", "current_revision": 1})
    status, updated, _ = put(httpd, enabled=False, revision=1)
    assert status == 200 and updated["preferences"][APP] == {"enabled": False, "revision": 2}
    assert request(httpd, "GET", "/browser-overlays")[1] == updated


@pytest.mark.parametrize("payload,field", [
    ({}, "enabled"), ({"enabled": None}, "enabled"),
    ({"enabled": 1}, "enabled"), ({"enabled": "true"}, "enabled"),
    ({"enabled": []}, "enabled"), ({"enabled": True, "revision": True}, "revision"),
    ({"enabled": True, "revision": -1}, "revision"),
    ({"enabled": True, "revision": 1.0}, "revision"),
    ({"enabled": True, "revision": "0"}, "revision"),
    ({"enabled": True, "revision": None}, "revision"),
    ({"enabled": True}, "revision"),
    ({"enabled": True, "revision": 2**53}, "revision"),
    ({"enabled": True, "stream_burn_in": {}}, "stream_burn_in"),
    ({"enabled": True, "preferences": {}}, "preferences"),
])
def test_http_validation_has_no_write(httpd, payload, field):
    install_dir()
    status, body, _ = request(httpd, "PUT", "/apps/%s/browser-overlay" % APP, payload)
    assert status == 400 and body["field"] == field
    assert document()["revision"] == 0


@pytest.mark.parametrize("payload", [[], True, "enabled"])
def test_non_object_http_body_rejected(httpd, payload):
    install_dir()
    status, _, _ = request(httpd, "PUT", "/apps/%s/browser-overlay" % APP, payload)
    assert status == 400 and document()["revision"] == 0


def test_missing_revision_never_mutates_even_an_absent_entry(httpd):
    install_dir()
    assert request(httpd, "PUT", "/apps/%s/browser-overlay" % APP, {"enabled": True})[0] == 400
    assert document()["revision"] == 0
    save()
    assert request(httpd, "PUT", "/apps/%s/browser-overlay" % APP, {"enabled": False})[0] == 400
    assert document()["preferences"][APP] == {"enabled": True, "revision": 1}


@pytest.mark.parametrize("app_id,installed", [
    ("builtin", False), ("acousticslab", False),
    ("legacy-app", True), ("workflow-app", True), ("manifest-v2-app", True),
])
def test_system_legacy_workflow_and_v2_share_only_display_preference(httpd, app_id, installed, monkeypatch):
    if installed:
        install_dir(app_id, manifest_version=2 if app_id == "manifest-v2-app" else 1)

    def forbidden(*_args, **_kwargs):
        pytest.fail("browser preference touched device OSD, inference or app config")

    monkeypatch.setattr(visualization, "save", forbidden)
    monkeypatch.setattr(server, "do_set_config", forbidden)
    monkeypatch.setattr(server, "do_start", forbidden)
    monkeypatch.setattr(server, "do_stop", forbidden)
    assert put(httpd, app_id)[0] == 200
    assert document()["preferences"][app_id]["enabled"] is True


def test_unknown_and_path_traversal_cannot_create_entries(httpd):
    assert put(httpd, "unknown")[0] == 404
    for app_id in ("..", "%2e%2e", "Bad-ID", "a" * 65, "a/b"):
        assert put(httpd, app_id)[0] == 404
    assert document()["preferences"] == {}


def test_mutation_reuses_origin_guard(httpd):
    install_dir()
    assert put(httpd, headers={"Origin": "https://attacker.invalid"})[0] == 403
    assert document()["revision"] == 0


@pytest.mark.parametrize("same_app", [False, True])
def test_real_parallel_http_writes_preserve_independent_entries(httpd, same_app):
    install_dir()
    install_dir("other-app")
    barrier = threading.Barrier(2)

    def write(app_id):
        barrier.wait(timeout=3)
        return put(httpd, app_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, [APP, APP if same_app else "other-app"]))
    assert sorted(result[0] for result in results) == ([200, 409] if same_app else [200, 200])
    final = document()
    assert final["revision"] == (1 if same_app else 2)
    assert set(final["preferences"]) == ({APP} if same_app else {APP, "other-app"})
    assert all(entry["enabled"] for entry in final["preferences"].values())


def test_busy_gate_failure_does_not_write(layout):
    install_dir()
    with server.busy_gate():
        with pytest.raises(server.BusyError):
            save()
    assert document()["revision"] == 0


def test_publish_happens_after_durable_save_and_audits(layout, monkeypatch):
    install_dir()
    seen = []

    def publish(kind, **payload):
        seen.append((kind, payload, document()))

    monkeypatch.setattr(server, "_operation_manager", lambda: SimpleNamespace(events=SimpleNamespace(publish=publish)))
    saved = save()
    assert seen == [("browser_overlay", {"action": "updated", "app_id": APP, "revision": 1}, saved)]
    assert json.loads(Path(paths.audit_log()).read_text().splitlines()[-1])["action"] == "v1_browser_overlay"


def test_real_event_bus_delivers_change_invalidation(layout):
    install_dir()
    events = server._operation_manager().events
    queue = events.subscribe()
    try:
        save()
        event = queue.get(timeout=1)
        assert (event["type"], event["app_id"], event["revision"]) == ("browser_overlay", APP, 1)
    finally:
        events.unsubscribe(queue)


def test_notification_failure_does_not_turn_saved_setting_into_error(httpd, monkeypatch):
    install_dir()

    def fail(*_args, **_kwargs):
        raise RuntimeError("event bus unavailable")

    monkeypatch.setattr(server, "_operation_manager", fail)
    status, saved, _ = put(httpd)
    assert status == 200 and document() == saved


@pytest.mark.parametrize("phase", ["mkstemp", "fsync", "replace"])
def test_write_failure_preserves_old_snapshot_without_event(httpd, monkeypatch, phase):
    install_dir()
    old = save()
    before = Path(browser_overlay.config_path()).read_bytes()
    events = server._operation_manager().events
    queue = events.subscribe()

    def fail(*_args, **_kwargs):
        raise OSError("injected disk failure")

    target = browser_overlay.tempfile if phase == "mkstemp" else browser_overlay.os
    monkeypatch.setattr(target, phase, fail)
    try:
        status, body, _ = put(httpd, enabled=False, revision=1)
        assert status == 503 and body["error"] == "browser_overlay_storage_unavailable"
        assert document() == old
        assert Path(browser_overlay.config_path()).read_bytes() == before
        assert not list(Path(paths.APPMGR_DIR).glob(".browser-overlays.*.tmp"))
        assert queue.empty()
    finally:
        events.unsubscribe(queue)


def test_directory_sync_failure_reports_uncertain_commit_without_notification(httpd, monkeypatch):
    install_dir()
    save()
    queue = server._operation_manager().events.subscribe()

    def fail(_directory):
        raise OSError("injected directory sync failure after rename")

    monkeypatch.setattr(operations, "_fsync_dir", fail)
    assert put(httpd, enabled=False, revision=1)[0] == 503
    # Rename already happened. Refetch, never blindly retry revision 1.
    assert document()["preferences"][APP] == {"enabled": False, "revision": 2}
    assert queue.empty()


def test_read_during_atomic_publish_sees_complete_old_snapshot(layout, monkeypatch):
    install_dir()
    old = save()
    real_replace = browser_overlay.os.replace
    seen = []

    def observe(source, target):
        seen.append(document())
        assert json.loads(Path(source).read_text())["revision"] == 2
        real_replace(source, target)

    monkeypatch.setattr(browser_overlay.os, "replace", observe)
    save(enabled=False, revision=1)
    assert seen == [old]
    assert document()["revision"] == 2


@pytest.mark.parametrize("raw", [
    "{", "[]", '{"schema_version":2,"revision":9,"preferences":{}}',
    '{"schema_version":1,"revision":true,"preferences":{}}',
    '{"schema_version":1,"revision":3,"preferences":{"bad":{"enabled":true,"revision":4}}}',
    '{"schema_version":1,"revision":3,"preferences":{"bad":{"enabled":1,"revision":3}}}',
    '{"schema_version":1,"revision":3,"preferences":{"../bad":{"enabled":true,"revision":3}}}',
])
def test_corruption_does_not_reset_revisions_or_overwrite_data(httpd, raw):
    install_dir()
    path = Path(browser_overlay.config_path())
    path.write_text(raw)
    assert request(httpd, "GET", "/browser-overlays")[0] == 503
    assert put(httpd)[0] == 503
    assert path.read_text() == raw


def test_revision_exhaustion_does_not_wrap(layout):
    install_dir()
    Path(browser_overlay.config_path()).write_text(json.dumps({
        "schema_version": 1, "revision": browser_overlay.MAX_REVISION,
        "preferences": {APP: {"enabled": False, "revision": browser_overlay.MAX_REVISION}}}))
    with pytest.raises(browser_overlay.StorageError, match="exhausted"):
        save(revision=browser_overlay.MAX_REVISION)
    assert document()["revision"] == browser_overlay.MAX_REVISION


def test_uninstall_reinstall_keeps_false_tombstone_and_blocks_old_browser(layout, monkeypatch):
    server.do_install(package(layout, "1.0.0"))
    first = document()["preferences"][APP]["revision"]
    selected = save(revision=first)["preferences"][APP]["revision"]
    monkeypatch.setattr(supervisor, "is_running", lambda _app: None)
    server.do_uninstall(APP)
    removed = document()["preferences"][APP]
    assert removed == {"enabled": False, "revision": selected + 1}
    with pytest.raises(FileNotFoundError):
        save(revision=removed["revision"])
    server.do_install(package(layout, "2.0.0"))
    reinstalled = document()["preferences"][APP]
    assert reinstalled == {"enabled": False, "revision": removed["revision"] + 1}
    for stale in (0, first, selected, removed["revision"]):
        with pytest.raises(browser_overlay.RevisionConflict):
            save(revision=stale)


def test_fresh_install_clears_stale_choice_before_package_publish(layout, monkeypatch):
    browser_overlay.replace(APP, True, 0)
    real_commit = installer.commit_prepared
    seen = []

    def observe(candidate):
        seen.append(document()["preferences"][APP])
        return real_commit(candidate)

    monkeypatch.setattr(installer, "commit_prepared", observe)
    server.do_install(package(layout, "1.0.0"))
    assert seen == [{"enabled": False, "revision": 2}]


def test_upgrade_preserves_choice_and_revision(layout):
    server.do_install(package(layout, "1.0.0"))
    old = save(revision=document()["preferences"][APP]["revision"])
    server.do_install(package(layout, "2.0.0"))
    assert document() == old


def test_upgrade_publish_failure_preserves_choice_and_revision(layout, monkeypatch):
    server.do_install(package(layout, "1.0.0"))
    old = save(revision=document()["preferences"][APP]["revision"])

    def fail(_candidate):
        raise installer.InstallError("injected publish failure")

    monkeypatch.setattr(installer, "commit_prepared", fail)
    with pytest.raises(installer.InstallError, match="publish failure"):
        server.do_install(package(layout, "2.0.0"))
    assert document() == old


def test_upgrade_ready_failure_rolls_back_without_changing_preference(layout, monkeypatch):
    server.do_install(package(layout, "1.0.0"))
    old = save(revision=document()["preferences"][APP]["revision"])
    state.set_active(APP, "1.0.0")
    running = {APP: 4242}
    monkeypatch.setattr(supervisor, "is_running", lambda app_id: running.get(app_id))
    monkeypatch.setattr(supervisor, "has_run_record", lambda _app: False)
    monkeypatch.setattr(supervisor, "owned_pid_is_running", lambda *_args: False)

    def stop(app_id):
        running.pop(app_id, None)
        return {"stopped": app_id}

    def start(app_id, operation, proof=None, **_kwargs):
        if operation == "upgrade_restart":
            raise supervisor.SupervisorError("injected READY failure")
        running[app_id] = 5252
        return 5252

    monkeypatch.setattr(supervisor, "stop", stop)
    monkeypatch.setattr(server, "_prepare_external_start", lambda *_args: {})
    monkeypatch.setattr(server, "_coordinated_legacy_start", start)
    with pytest.raises(supervisor.SupervisorError, match="READY failure"):
        server.do_install(package(layout, "2.0.0"))
    assert document() == old
    assert json.loads(Path(paths.app_dir(APP), "manifest.json").read_text())["version"] == "1.0.0"


def test_tombstone_write_failure_precedes_fresh_install_side_effects(layout, monkeypatch):
    called = []

    def fail(_document):
        raise browser_overlay.StorageError("injected tombstone failure")

    monkeypatch.setattr(browser_overlay, "_write_atomic", fail)
    monkeypatch.setattr(supervisor, "stop", lambda *_args: called.append("stop"))
    monkeypatch.setattr(installer, "uninstall", lambda *_args: called.append("uninstall"))
    monkeypatch.setattr(installer, "commit_prepared", lambda *_args: called.append("commit"))
    with pytest.raises(browser_overlay.StorageError, match="tombstone failure"):
        server.do_install(package(layout, "1.0.0"))
    assert called == []
    assert not Path(paths.app_dir(APP)).exists()


@pytest.mark.parametrize("phase", ["stop", "delete"])
def test_failed_uninstall_preserves_enabled_choice_without_revision_change(layout, monkeypatch, phase):
    install_dir()
    before = save()

    def fail(*_args, **_kwargs):
        raise RuntimeError("injected " + phase + " failure")

    if phase == "stop":
        monkeypatch.setattr(supervisor, "is_running", lambda _app: 4242)
        monkeypatch.setattr(server, "_stop_external", fail)
    else:
        monkeypatch.setattr(supervisor, "is_running", lambda _app: None)
        monkeypatch.setattr(installer, "uninstall", fail)
    with pytest.raises(RuntimeError, match="injected"):
        server.do_uninstall(APP)
    assert Path(paths.app_dir(APP)).exists()
    assert document() == before


@pytest.mark.parametrize("failure", ["corrupt", "write"])
def test_uninstall_succeeds_when_overlay_store_is_unavailable(layout, monkeypatch, failure):
    install_dir()
    save()
    monkeypatch.setattr(supervisor, "is_running", lambda _app: None)
    if failure == "corrupt":
        Path(browser_overlay.config_path()).write_text("{")
    else:
        def fail(_document):
            raise browser_overlay.StorageError("injected write failure")
        monkeypatch.setattr(browser_overlay, "_write_atomic", fail)
    assert server.do_uninstall(APP)["uninstalled"]
    assert not Path(paths.app_dir(APP)).exists()
    with pytest.raises(FileNotFoundError):
        save(revision=1)


def test_failed_uninstall_cleanup_still_fences_subsequent_reinstall(layout, monkeypatch):
    install_dir()
    old = save()["preferences"][APP]["revision"]
    monkeypatch.setattr(supervisor, "is_running", lambda _app: None)
    real_write = browser_overlay._write_atomic

    def fail(_document):
        raise browser_overlay.StorageError("injected cleanup failure")

    monkeypatch.setattr(browser_overlay, "_write_atomic", fail)
    server.do_uninstall(APP)
    assert document()["preferences"][APP]["enabled"] is True
    monkeypatch.setattr(browser_overlay, "_write_atomic", real_write)
    server.do_install(package(layout, "2.0.0"))
    assert document()["preferences"][APP] == {"enabled": False, "revision": old + 1}
    with pytest.raises(browser_overlay.RevisionConflict):
        save(revision=old)


def test_failed_fresh_install_restores_stale_enabled_with_new_revision(layout, monkeypatch):
    browser_overlay.replace(APP, True, 0)

    def fail(_candidate):
        raise installer.InstallError("injected fresh publish failure")

    monkeypatch.setattr(installer, "commit_prepared", fail)
    with pytest.raises(installer.InstallError, match="fresh publish failure"):
        server.do_install(package(layout, "1.0.0"))
    assert not Path(paths.app_dir(APP)).exists()
    assert document()["preferences"][APP] == {"enabled": True, "revision": 3}
    with pytest.raises(browser_overlay.RevisionConflict):
        browser_overlay.replace(APP, False, 1)


def test_fresh_install_restore_failure_preserves_original_error_and_false_fence(layout, monkeypatch):
    browser_overlay.replace(APP, True, 0)

    def fail_restore(*_args):
        raise browser_overlay.StorageError("injected restore failure")

    def fail_commit(_candidate):
        raise installer.InstallError("injected package failure")

    monkeypatch.setattr(browser_overlay, "replace", fail_restore)
    monkeypatch.setattr(installer, "commit_prepared", fail_commit)
    with pytest.raises(installer.InstallError, match="package failure"):
        server.do_install(package(layout, "1.0.0"))
    assert document()["preferences"][APP] == {"enabled": False, "revision": 2}


def test_incomplete_fresh_install_rollback_never_reenables_stale_choice(layout, monkeypatch):
    browser_overlay.replace(APP, True, 0)
    real_commit = installer.commit_prepared

    def fail_with_package_remaining(candidate):
        real_commit(candidate)
        raise installer.InstallError("injected incomplete rollback")

    def fail_rollback(_transaction):
        raise installer.InstallError("injected rollback failure")

    monkeypatch.setattr(installer, "commit_prepared", fail_with_package_remaining)
    monkeypatch.setattr(installer, "rollback_install_transaction_files", fail_rollback)
    with pytest.raises(installer.InstallError, match="incomplete rollback"):
        server.do_install(package(layout, "1.0.0"))
    assert Path(paths.app_dir(APP)).is_dir()
    assert document()["preferences"][APP] == {"enabled": False, "revision": 2}

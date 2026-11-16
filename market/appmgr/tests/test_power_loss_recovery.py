"""Payload durability and transaction/operation recovery, without device access."""
from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
import threading

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from appmgr import durability, installer, paths, pythonenv, server
from appmgr.tests.test_pythonenv_v2 import app_manifest, make_wheel, prepare_app
from appmgr.tests.test_same_id_upgrade_transactions import layout as base_layout


APP = "power-app"
UPLOAD = "a" * 32
TASK = "b" * 32


@pytest.fixture
def layout(base_layout, monkeypatch):
    monkeypatch.setattr(paths, "APPSTAGE_DIR", str(base_layout / "stage"))
    monkeypatch.setenv("APPMGR_PLATFORM_ARCH", "aarch64")
    monkeypatch.setenv("APPMGR_PLATFORM_PYTHON", sys.executable)
    monkeypatch.setenv("APPMGR_WHEELHOUSE_DIR", str(base_layout / "wheelhouse"))
    monkeypatch.setattr(server, "_service_stopping", False)
    for name in ("_operation_manager_instance", "_operation_manager_layout",
                 "_store_manager_instance", "_store_manager_layout"):
        monkeypatch.setattr(server, name, None)
    yield base_layout
    if server._store_manager_instance:
        server._store_manager_instance.close()
    if server._operation_manager_instance:
        server._operation_manager_instance.close()


def package(root, version="1.0.0"):
    wheel_path = root / ("demo_dep-%s-py3-none-any.whl" % version)
    wheel = make_wheel(wheel_path, version=version, value=version)
    manifest = app_manifest(APP, version, wheel)
    source = root / ("source-" + version)
    lock = prepare_app(source, manifest, wheel_path)
    from appmgr import manifest as contract
    records = {str(path.relative_to(source)): {
        "size": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        for path in source.rglob("*") if path.is_file()}
    _, bom = contract.make_release_metadata(manifest, records)
    (source / contract.RELEASE_LOCK_PATH).write_bytes(contract.canonical_json(lock))
    (source / contract.BOM_PATH).write_bytes(bom)
    archive = root / (version + ".tar.gz")
    with tarfile.open(archive, "w:gz") as tar:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                tar.add(path, arcname=str(path.relative_to(source)))
    return str(archive), manifest, lock


def interrupted_install(root, monkeypatch, *, upgrade=False):
    if upgrade:
        server.do_install(package(root)[0])
    archive, manifest, lock = package(root, "2.0.0")
    manager = server._operation_manager()
    monkeypatch.setattr(manager, "_ensure_worker", lambda: None)
    operation = manager.submit(
        "install", APP, lambda: None, upload_id=UPLOAD,
        request_fingerprint="c" * 64,
        install_target={"version": manifest["version"], "release_id": lock["release_id"],
                        "package_sha256": hashlib.sha256(Path(archive).read_bytes()).hexdigest()})
    manager._update(operation["id"], status="running")
    store_path = Path(paths.APPMGR_DIR) / "store-tasks.json"
    store_path.write_text(json.dumps({"schema_version": 1, "tasks": [{
        "id": TASK, "app_id": APP, "version": manifest["version"],
        "package_sha256": operation["install_target"]["package_sha256"],
        "upload_id": UPLOAD, "operation_id": operation["id"],
        "status": "installing", "error": None}]}))
    result = server.do_install(archive, _operation_binding=manager.install_binding(operation))
    transaction = installer.load_install_transaction()
    assert transaction["phase"] == "committed"
    assert manager.for_upload(UPLOAD)["status"] == "running"
    # Reconstruct the service objects, just as a daemon restart does.
    manager.close()
    server._operation_manager_instance = None
    server._operation_manager_layout = None
    return result, transaction


def test_payloads_and_parent_entries_are_flushed_before_publication(layout, monkeypatch):
    archive, _, _ = package(layout)
    flushed = []
    real_fsync = os.fsync

    def fsync(fd):
        flushed.append((os.readlink("/proc/self/fd/%d" % fd), stat.S_ISREG(os.fstat(fd).st_mode)))
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    candidate = installer.prepare(archive)
    assert not Path(candidate.dest).exists()
    code_root = candidate.staging
    env_root = candidate.env_candidate.staging_dir
    for root, inventory in ((code_root, candidate.integrity),
                            (env_root, candidate.env_candidate.integrity)):
        for relative in inventory["files"]:
            assert (os.path.join(root, relative), True) in flushed
        assert (root, False) in flushed
    assert any(name.endswith("/demo_dep/__init__.py") and regular for name, regular in flushed)
    assert (paths.VENVS_DIR, False) in flushed
    assert (paths.APPS_DIR, False) in flushed
    installer.discard_prepared(candidate)


@pytest.mark.parametrize("error", [errno.EIO, errno.ENOSPC])
@pytest.mark.parametrize("target", ["app.py", "demo_dep/__init__.py", "directory"])
def test_fsync_failure_prevents_publication(layout, monkeypatch, error, target):
    server.do_install(package(layout)[0])
    archive = package(layout, "2.0.0")[0]
    real_fsync = os.fsync

    def fsync(fd):
        path = os.readlink("/proc/self/fd/%d" % fd)
        hit = path.endswith(target) if target != "directory" else stat.S_ISDIR(os.fstat(fd).st_mode)
        if hit and ".stage." in path:
            raise OSError(error, "injected storage failure")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    with pytest.raises(OSError, match="injected storage failure"):
        server.do_install(archive)
    assert json.loads(Path(paths.app_dir(APP), "manifest.json").read_text())["version"] == "1.0.0"
    assert installer.load_install_transaction() is None
    assert not list(Path(paths.APPS_DIR).glob(".*.stage.*"))


def test_reboot_repairs_successful_operation_and_store_task(layout, monkeypatch):
    result, _ = interrupted_install(layout, monkeypatch)
    assert server._operation_manager().for_upload(UPLOAD)["status"] == "failed"
    # Even a store manager loaded before recovery must correct its stale error.
    assert server._store_manager().get(TASK)["status"] == "failed"
    assert server._reconcile_install_transaction()["action"] == "committed"
    operation = server._operation_manager().for_upload(UPLOAD)
    assert operation["status"] == "succeeded" and operation["result"] == result
    task = server._store_manager().get(TASK)
    assert task["status"] == "succeeded" and task["error"] is None
    assert json.loads(Path(paths.APPMGR_DIR, "store-tasks.json").read_text())["tasks"][0]["status"] == "succeeded"
    assert installer.load_install_transaction() is None


@pytest.mark.parametrize("upgrade", [False, True])
@pytest.mark.parametrize("damage", ["missing_code", "corrupt_code", "missing_dependency", "corrupt_dependency", "symlink"])
def test_damaged_committed_payload_is_rolled_back(layout, monkeypatch, upgrade, damage):
    _, transaction = interrupted_install(layout, monkeypatch, upgrade=upgrade)
    path = Path(paths.app_dir(APP), "app.py")
    if "dependency" in damage:
        env = Path(paths.venv_dir(APP), "releases", transaction["target_release_id"])
        path = next(env.glob("lib/python*/site-packages/demo_dep/__init__.py"))
    if damage.startswith("missing"):
        path.unlink()
    elif damage == "symlink":
        original = path.read_bytes()
        path.unlink()
        outside = layout / "outside.py"
        outside.write_bytes(original)
        path.symlink_to(outside)
    else:
        path.chmod(0o644)
        data = path.read_bytes()
        path.write_bytes(b"X" + data[1:])  # Same size, so only hashing detects it.
        path.chmod(0o444)
    assert server._reconcile_install_transaction()["action"] == "rolled_back"
    if upgrade:
        assert json.loads(Path(paths.app_dir(APP), "manifest.json").read_text())["version"] == "1.0.0"
        assert pythonenv.current_release_id(APP) == transaction["previous_environment_release_id"]
    else:
        assert not Path(paths.app_dir(APP)).exists()
        assert pythonenv.current_release_id(APP) is None
    assert server._store_manager().get(TASK)["status"] == "failed"
    assert "verification failed" in server._operation_manager().for_upload(UPLOAD)["error"]
    assert installer.load_install_transaction() is None
    # Retrying the exact package must not reuse a damaged inactive environment.
    assert server.do_install(str(layout / "2.0.0.tar.gz"))["installed"] is True


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("failure", ["operation_save", "store_save", "journal_clear"])
@pytest.mark.parametrize("restart", [False, True])
def test_recovery_is_repeatable_after_another_persistence_failure(layout, monkeypatch, committed, failure, restart):
    interrupted_install(layout, monkeypatch, upgrade=True)
    if not committed:
        Path(paths.app_dir(APP), "app.py").unlink()
    manager = server._operation_manager()
    store = server._store_manager()
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise OSError(errno.EIO, "second power loss")
        if failure == "operation_save":
            patch.setattr(manager, "_save", fail)
        elif failure == "store_save":
            patch.setattr(store, "_save", fail)
        else:
            patch.setattr(installer, "clear_install_transaction", fail)
        with pytest.raises(OSError, match="second power loss"):
            server._reconcile_install_transaction()
    assert installer.load_install_transaction() is not None
    # Recreate managers to discard any updates held only in memory.
    if restart:
        store.close()
        manager.close()
        server._store_manager_instance = None
        server._operation_manager_instance = None
    outcome = server._reconcile_install_transaction()
    assert outcome["action"] == ("committed" if committed else "rolled_back")
    assert server._store_manager().get(TASK)["status"] == ("succeeded" if committed else "failed")
    assert installer.load_install_transaction() is None


@pytest.mark.parametrize("field", ["id", "upload_id", "request_fingerprint", "version", "release_id", "package_sha256"])
def test_recovery_never_repairs_a_different_operation(layout, monkeypatch, field):
    interrupted_install(layout, monkeypatch)
    manager = server._operation_manager()
    record = manager._records[0]
    if field in record["install_target"]:
        record["install_target"][field] = "d" * 64 if field == "package_sha256" else "different"
    else:
        record[field] = "d" * (64 if field == "request_fingerprint" else 32)
    manager._save()
    with pytest.raises(ValueError, match="identity mismatch"):
        server._reconcile_install_transaction()
    assert manager._records[0]["status"] == "failed"
    assert installer.load_install_transaction() is not None


@pytest.mark.parametrize("phase", ["prepared", "publishing_code", "code_published", "publishing_environment",
                                   "environment_published", "ready", "committed"])
def test_real_process_exit_at_transaction_boundaries(layout, phase):
    server.do_install(package(layout)[0])
    archive = package(layout, "2.0.0")[0]
    settings = {key: getattr(paths, key) for key in (
        "APPS_DIR", "APPMGR_DIR", "APPDATA_DIR", "VENVS_DIR", "STATE_FILE", "ALLOWED_PKG_ROOTS")}
    script = '''
import json, os, sys
from appmgr import installer, paths, config, state
for key, value in json.loads(sys.argv[1]).items():
    setattr(paths, key, value)
paths.REQUIRE_SIGNATURE = False
cut = sys.argv[3]
mark = installer.mark_install_transaction
def crash(candidate, phase, **kwargs):
    result = mark(candidate, phase, **kwargs)
    if phase == cut:
        os._exit(73)
    return result
installer.mark_install_transaction = crash
candidate = installer.prepare(sys.argv[2])
installer.begin_install_transaction(candidate,
    config_snapshot=config.snapshot_upgrade_config(candidate.app_id),
    lifecycle_snapshot=state.snapshot_app(candidate.app_id))
if cut == "prepared":
    os._exit(73)
installer.commit_prepared(candidate)
crash(candidate, "ready")
crash(candidate, "committed")
'''
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
    child = subprocess.run([sys.executable, "-c", script, json.dumps(settings), archive, phase],
                           env=env, capture_output=True, text=True, timeout=30)
    assert child.returncode == 73, child.stderr
    outcome = server._reconcile_install_transaction()
    committed = phase in ("ready", "committed")
    assert outcome["action"] == ("committed" if committed else "rolled_back")
    assert json.loads(Path(paths.app_dir(APP), "manifest.json").read_text())["version"] == (
        "2.0.0" if committed else "1.0.0")
    installer.reconcile_interrupted_installs()
    assert installer.load_install_transaction() is None


@pytest.mark.parametrize("post_commit_error", [False, True])
def test_worker_persists_the_committed_result_before_acknowledging(layout, monkeypatch, post_commit_error):
    archive, manifest, lock = package(layout)
    manager = server._operation_manager()
    finished = threading.Event()
    errors = []

    def terminal(record):
        try:
            assert installer.load_install_transaction() is not None
            server._finish_install_operation(record)
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()

    manager.on_terminal = terminal

    def job():
        result = server.do_install(archive, _operation_binding=manager.install_binding(manager.for_upload(UPLOAD)))
        if post_commit_error:
            raise RuntimeError("notification failed after commit")
        return result

    manager.submit("install", APP, job, upload_id=UPLOAD, request_fingerprint="c" * 64,
                   install_target={"version": manifest["version"], "release_id": lock["release_id"],
                                   "package_sha256": hashlib.sha256(Path(archive).read_bytes()).hexdigest()})
    assert finished.wait(5)
    assert errors == []
    assert installer.load_install_transaction() is None
    operation = manager.for_upload(UPLOAD)
    assert operation["status"] == "succeeded" and operation["result"]["installed"] is True
    assert json.loads(Path(paths.operation_state_file()).read_text())["operations"][0]["status"] == "succeeded"


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("damaged", [False, True])
def test_reusing_environment_verifies_payload_including_legacy_environments(layout, legacy, damaged):
    archive, manifest, lock = package(layout)
    server.do_install(archive)
    env = Path(paths.venv_dir(APP), "releases", lock["release_id"])
    if legacy:
        (env / pythonenv.ENV_INTEGRITY).unlink()
    if damaged:
        next(env.glob("lib/python*/site-packages/demo_dep/__init__.py")).unlink()
        with pytest.raises(pythonenv.PythonEnvError, match="integrity verification"):
            pythonenv.stage_environment(paths.app_dir(APP), manifest, lock)
    else:
        candidate = pythonenv.stage_environment(paths.app_dir(APP), manifest, lock)
        assert candidate.reused and candidate.integrity
        durability.verify_tree(str(env), candidate.integrity)
        assert (env / pythonenv.ENV_INTEGRITY).is_file()


def test_legacy_terminal_transaction_without_inventory_rolls_back(layout, monkeypatch):
    _, transaction = interrupted_install(layout, monkeypatch, upgrade=True)
    transaction.pop("code_integrity")
    transaction.pop("environment_integrity")
    installer._atomic_install_transaction(transaction)
    assert server._reconcile_install_transaction()["action"] == "rolled_back"
    assert json.loads(Path(paths.app_dir(APP), "manifest.json").read_text())["version"] == "1.0.0"


def test_inventory_rejects_parent_symlink_traversal(tmp_path):
    root = tmp_path / "payload"
    (root / "nested").mkdir(parents=True)
    (root / "nested/data").write_bytes(b"original")
    inventory = durability.seal_tree(str(root))
    (root / "nested").rename(tmp_path / "outside")
    (root / "nested").symlink_to(tmp_path / "outside", target_is_directory=True)
    with pytest.raises(ValueError, match="directory"):
        durability.verify_tree(str(root), inventory)


def test_failed_callback_cannot_acknowledge_damaged_committed_files(layout, monkeypatch):
    interrupted_install(layout, monkeypatch)
    Path(paths.app_dir(APP), "app.py").unlink()
    record = server._operation_manager().for_upload(UPLOAD)
    assert record["status"] == "failed"
    with pytest.raises(installer.InstallError, match="verification failed"):
        server._finish_install_operation(record)
    assert installer.load_install_transaction() is not None
    assert server._reconcile_install_transaction()["action"] == "rolled_back"


def test_terminal_phase_write_failure_records_rollback_intent(layout, monkeypatch):
    server.do_install(package(layout)[0])
    archive = package(layout, "2.0.0")[0]
    mark = installer.mark_install_transaction

    def fail_after_replace(candidate, phase, **kwargs):
        value = mark(candidate, phase, **kwargs)
        if phase == "committed":
            raise OSError(errno.EIO, "terminal journal flush failed")
        return value

    monkeypatch.setattr(installer, "mark_install_transaction", fail_after_replace)
    monkeypatch.setattr(server, "_rollback_upgrade", lambda *args, **kwargs: None)
    with pytest.raises(OSError, match="terminal journal flush"):
        server.do_install(archive)
    transaction = installer.load_install_transaction()
    assert transaction["phase"] == "committed"
    assert transaction["recovery_action"] == "rollback"
    # The files alone still look committed; the persisted intent must win.
    installer.validate_committed_transaction_files(transaction)


def test_shared_damaged_environment_keeps_recovery_journal(layout, monkeypatch):
    archive, _, lock = package(layout)
    server.do_install(archive)
    candidate = installer.prepare(archive)
    from appmgr import config, state
    installer.begin_install_transaction(
        candidate, config_snapshot=config.snapshot_upgrade_config(APP),
        lifecycle_snapshot=state.snapshot_app(APP))
    installer.commit_prepared(candidate)
    installer.mark_install_transaction(candidate, "ready")
    env = Path(paths.venv_dir(APP), "releases", lock["release_id"])
    next(env.glob("lib/python*/site-packages/demo_dep/__init__.py")).unlink()
    with pytest.raises(installer.InstallError, match="retained environment is also damaged"):
        server._reconcile_install_transaction()
    assert installer.load_install_transaction()["recovery_action"] == "rollback"

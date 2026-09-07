"""Fault-injection coverage for same-id install/upgrade transactions."""
from __future__ import annotations

import json
import os
import sys
import tarfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
from appmgr import (config as appconfig, installer, paths, pythonenv, resources,
                    server, state, supervisor, visualization)  # noqa: E402


APP_ID = "upgrade-fence"


@pytest.fixture
def layout(tmp_path, monkeypatch):
    apps = tmp_path / "apps"
    appmgr = tmp_path / "appmgr"
    appdata = tmp_path / "appdata"
    venvs = tmp_path / "venvs"
    for directory in (apps, appmgr, appdata, venvs):
        directory.mkdir()
    monkeypatch.setattr(paths, "APPS_DIR", str(apps))
    monkeypatch.setattr(paths, "APPMGR_DIR", str(appmgr))
    monkeypatch.setattr(paths, "APPDATA_DIR", str(appdata))
    monkeypatch.setattr(paths, "VENVS_DIR", str(venvs))
    monkeypatch.setattr(paths, "STATE_FILE", str(apps / "state.json"))
    monkeypatch.setattr(paths, "ALLOWED_PKG_ROOTS", (str(tmp_path),))
    monkeypatch.setattr(paths, "REQUIRE_SIGNATURE", False)
    monkeypatch.setattr(server, "_result_hub_instance", None)
    monkeypatch.setattr(server, "_coordinator_instance", None)
    monkeypatch.setattr(server, "_coordinator_layout", None)
    server.cache_clear()
    return tmp_path


def _package(root, version, *, schema=None):
    package = root / ("%s-%s.tar.gz" % (APP_ID, version))
    manifest = {
        "id": APP_ID,
        "name": "Upgrade Fence",
        "version": version,
        "entry": "app.py",
        "config_schema": schema or {},
    }
    source = root / ("source-" + version)
    source.mkdir()
    (source / "manifest.json").write_text(json.dumps(manifest))
    (source / "app.py").write_text("# %s\n" % version)
    with tarfile.open(package, "w:gz") as archive:
        archive.add(source / "manifest.json", arcname="manifest.json")
        archive.add(source / "app.py", arcname="app.py")
    return str(package)


def _installed_version():
    with open(os.path.join(paths.app_dir(APP_ID), "manifest.json")) as source:
        return json.load(source)["version"]


def test_stopped_upgrade_commits_and_clears_journal(layout):
    server.do_install(_package(layout, "1.0.0"))
    result = server.do_install(_package(layout, "2.0.0"))

    assert result["restarted"] is False
    assert _installed_version() == "2.0.0"
    assert installer.load_install_transaction() is None
    record = state.get_app(APP_ID)
    assert record["desired_state"] == state.DESIRED_STOPPED
    assert record["restart_history"] == []


def test_fresh_install_clears_stale_stream_burn_in_before_publish(
        layout, monkeypatch):
    visualization.save({
        "osd": {"enabled": True, "sources": [APP_ID]},
    })
    published_with = []
    real_commit = installer.commit_prepared

    def observe_commit(candidate):
        published_with.append(visualization.load())
        return real_commit(candidate)

    monkeypatch.setattr(installer, "commit_prepared", observe_commit)

    server.do_install(_package(layout, "1.0.0"))

    assert published_with == [visualization.defaults()]
    assert visualization.load() == visualization.defaults()
    assert _installed_version() == "1.0.0"


def test_fresh_install_visualization_cleanup_failure_prevents_publish(
        layout, monkeypatch):
    stale = {"osd": {"enabled": True, "sources": [APP_ID]}}
    visualization.save(stale)
    published = []
    real_commit = installer.commit_prepared

    def observe_commit(candidate):
        published.append(candidate)
        return real_commit(candidate)

    monkeypatch.setattr(installer, "commit_prepared", observe_commit)
    monkeypatch.setattr(
        visualization, "save",
        lambda _value: (_ for _ in ()).throw(
            OSError("injected visualization persistence failure")),
    )

    with pytest.raises(OSError, match="visualization persistence"):
        server.do_install(_package(layout, "1.0.0"))

    assert published == []
    assert not os.path.exists(paths.app_dir(APP_ID))
    assert visualization.load() == stale
    assert installer.load_install_transaction() is None


def test_upgrade_removes_stream_burn_in_when_capability_disappears(
        layout, monkeypatch):
    server.do_install(_package(layout, "1.0.0"))
    visualization.save({
        "osd": {"enabled": True, "sources": [APP_ID]},
    })
    monkeypatch.setattr(
        server, "_supports_detection_stream_osd", lambda _manifest: False)

    server.do_install(_package(layout, "2.0.0"))

    assert visualization.load() == visualization.defaults()


def test_upgrade_preserves_stream_burn_in_while_capability_remains(
        layout, monkeypatch):
    server.do_install(_package(layout, "1.0.0"))
    selected = {"osd": {"enabled": True, "sources": [APP_ID]}}
    visualization.save(selected)
    monkeypatch.setattr(
        server, "_supports_detection_stream_osd", lambda _manifest: True)

    server.do_install(_package(layout, "2.0.0"))

    assert visualization.load() == selected


def test_prepare_failure_happens_before_old_process_stop(layout, monkeypatch):
    server.do_install(_package(layout, "1.0.0"))
    replacement = _package(layout, "2.0.0")
    stops = []
    monkeypatch.setattr(server.supervisor, "is_running", lambda _app: 4242)
    monkeypatch.setattr(server.supervisor, "stop", lambda app: stops.append(app))
    monkeypatch.setattr(
        server.installer, "prepare",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            installer.InstallError("injected candidate environment failure")))

    with pytest.raises(installer.InstallError, match="candidate environment"):
        server.do_install(replacement)

    assert stops == []
    assert _installed_version() == "1.0.0"
    assert installer.load_install_transaction() is None


@pytest.mark.parametrize("teardown_pending", [False, True])
def test_stopping_or_teardown_pending_rejects_before_prepare(
        layout, monkeypatch, teardown_pending):
    server.do_install(_package(layout, "1.0.0"))
    state.transition(
        APP_ID, "stopping" if not teardown_pending else "failed",
        teardown_pending=teardown_pending)
    prepared = []
    monkeypatch.setattr(
        server.installer, "prepare", lambda *_args, **_kwargs: prepared.append(True))

    with pytest.raises(server.BusyError, match="teardown"):
        server.do_install(_package(layout, "2.0.0"))
    assert prepared == []
    assert _installed_version() == "1.0.0"


def test_failed_state_with_instance_is_quiesced_without_pid_record(
        layout, monkeypatch):
    server.do_install(_package(layout, "1.0.0"))
    state.begin_start(APP_ID, "old-instance", version="1.0.0")
    state.transition(APP_ID, "failed", pid=None, pgid=None, allocations=[])
    calls = []

    class Coordinator:
        @staticmethod
        def stop(app_id, *, desired):
            calls.append((app_id, desired))
            state.set_desired(app_id, desired)
            state.transition(
                app_id, "stopped", pid=None, pgid=None, allocations=[],
                teardown_pending=False)
            return {"stopped": app_id}

    monkeypatch.setattr(server, "_coordinator", lambda: Coordinator())
    monkeypatch.setattr(server.supervisor, "is_running", lambda _app: None)
    monkeypatch.setattr(server.supervisor, "has_run_record", lambda _app: False)
    monkeypatch.setattr(
        server.supervisor, "owned_pid_is_running", lambda *_args: False)

    server.do_install(_package(layout, "2.0.0"))

    assert calls == [(APP_ID, state.DESIRED_RUNNING)]
    assert _installed_version() == "2.0.0"
    record = state.get_app(APP_ID)
    assert record["desired_state"] == state.DESIRED_RUNNING
    assert record["observed_state"] == "stopped"
    assert record["instance_id"] is None


def test_waiting_resource_intent_survives_new_release_without_stale_backoff(
        layout, monkeypatch):
    server.do_install(_package(layout, "1.0.0"))
    state.begin_start(APP_ID, "waiting-instance", version="1.0.0")
    state.transition(
        APP_ID, "waiting_resource", reason="old owner", restart_history=[1, 2],
        next_retry_at=999, blocked_resource="memory.managed-mb",
        resource_owners=["old-app"])

    class Coordinator:
        @staticmethod
        def stop(app_id, *, desired):
            state.set_desired(app_id, desired)
            state.transition(
                app_id, "stopped", pid=None, pgid=None, allocations=[],
                teardown_pending=False)
            return {"stopped": app_id}

    monkeypatch.setattr(server, "_coordinator", lambda: Coordinator())
    monkeypatch.setattr(server.supervisor, "is_running", lambda _app: None)
    monkeypatch.setattr(server.supervisor, "has_run_record", lambda _app: False)
    monkeypatch.setattr(
        server.supervisor, "owned_pid_is_running", lambda *_args: False)

    server.do_install(_package(layout, "2.0.0"))
    record = state.get_app(APP_ID)
    assert record["desired_state"] == state.DESIRED_RUNNING
    assert record["observed_state"] == "waiting_resource"
    assert record["restart_history"] == []
    assert record["next_retry_at"] is None
    assert record["blocked_resource"] is None
    assert record["resource_owners"] == []


def test_claimed_stop_with_live_captured_pid_keeps_teardown_fence(
        layout, monkeypatch):
    server.do_install(_package(layout, "1.0.0"))
    state.set_active(APP_ID, "1.0.0")
    monkeypatch.setattr(server.supervisor, "is_running", lambda _app: 4242)
    monkeypatch.setattr(server.supervisor, "has_run_record", lambda _app: False)
    monkeypatch.setattr(server.supervisor, "stop", lambda _app: {"stopped": True})
    monkeypatch.setattr(
        server.supervisor, "owned_pid_is_running", lambda *_args: True)
    committed = []
    monkeypatch.setattr(
        server.installer, "commit_prepared",
        lambda candidate: committed.append(candidate))

    with pytest.raises(server.BusyError, match="process fence"):
        server.do_install(_package(layout, "2.0.0"))

    assert committed == []
    assert _installed_version() == "1.0.0"
    record = state.get_app(APP_ID)
    assert record["observed_state"] == "stopping"
    assert record["teardown_pending"] is True
    assert installer.load_install_transaction() is None


def test_commit_rollback_failure_never_restarts_unverified_code(
        layout, monkeypatch):
    server.do_install(_package(layout, "1.0.0"))
    state.set_active(APP_ID, "1.0.0")
    running = {APP_ID: 4242}
    monkeypatch.setattr(
        server.supervisor, "is_running", lambda app: running.get(app))
    monkeypatch.setattr(server.supervisor, "has_run_record", lambda _app: False)
    monkeypatch.setattr(
        server.supervisor, "owned_pid_is_running", lambda *_args: False)
    monkeypatch.setattr(
        server.supervisor, "stop",
        lambda app: (running.pop(app, None), {"stopped": app})[1])
    monkeypatch.setattr(
        server.installer, "commit_prepared",
        lambda _candidate: (_ for _ in ()).throw(installer.InstallError(
            "install publish failed and rollback also failed")))
    monkeypatch.setattr(
        server.installer, "rollback_install_transaction_files",
        lambda _transaction: (_ for _ in ()).throw(installer.InstallError(
            "injected journal rollback failure")))
    starts = []
    monkeypatch.setattr(
        server, "_coordinated_legacy_start",
        lambda *_args, **_kwargs: starts.append(True))

    with pytest.raises(installer.InstallError, match="recovery also failed"):
        server.do_install(_package(layout, "2.0.0"))

    assert starts == []
    assert _installed_version() == "1.0.0"
    record = state.get_app(APP_ID)
    assert record["observed_state"] == "stopping"
    assert record["teardown_pending"] is True
    assert installer.load_install_transaction() is not None


def test_ready_failure_restores_every_config_file_byte_exact(
        layout, monkeypatch):
    old_schema = {"old_key": {"type": "number", "default": 1}}
    new_schema = {"new_key": {"type": "number", "default": 2}}
    server.do_install(_package(layout, "1.0.0", schema=old_schema))
    state.set_active(APP_ID, "1.0.0")
    appdata = paths.appdata_dir(APP_ID)
    os.makedirs(appdata, exist_ok=True)
    originals = {
        "config.json": b'{  "old_key" : 7 }\n',
        "config.quarantine.json": b"old quarantine bytes\n",
        "config.json.corrupt": b"old corrupt bytes\x00\n",
    }
    for name, content in originals.items():
        with open(os.path.join(appdata, name), "wb") as output:
            output.write(content)
    running = {APP_ID: 4242}
    monkeypatch.setattr(
        server.supervisor, "is_running", lambda app: running.get(app))
    monkeypatch.setattr(server.supervisor, "has_run_record", lambda _app: False)
    monkeypatch.setattr(
        server.supervisor, "owned_pid_is_running", lambda *_args: False)

    def stop(app_id):
        running.pop(app_id, None)
        return {"stopped": app_id}

    starts = []

    def start(app_id, operation, proof=None, **_kwargs):
        starts.append(operation)
        if operation == "upgrade_restart":
            raise supervisor.SupervisorError("injected READY failure")
        running[app_id] = 5252
        return 5252

    monkeypatch.setattr(server.supervisor, "stop", stop)
    monkeypatch.setattr(server, "_prepare_external_start", lambda *_args: {})
    monkeypatch.setattr(server, "_coordinated_legacy_start", start)

    with pytest.raises(supervisor.SupervisorError, match="READY failure"):
        server.do_install(_package(layout, "2.0.0", schema=new_schema))

    assert _installed_version() == "1.0.0"
    assert starts == ["upgrade_restart", "upgrade_rollback"]
    for name, content in originals.items():
        with open(os.path.join(appdata, name), "rb") as source:
            assert source.read() == content
    assert installer.load_install_transaction() is None


def test_ready_failure_with_failed_rollback_keeps_reconciler_fence(
        layout, monkeypatch):
    server.do_install(_package(layout, "1.0.0"))
    state.set_active(APP_ID, "1.0.0")
    running = {APP_ID: 4242}
    monkeypatch.setattr(
        server.supervisor, "is_running", lambda app: running.get(app))
    monkeypatch.setattr(server.supervisor, "has_run_record", lambda _app: False)
    monkeypatch.setattr(
        server.supervisor, "owned_pid_is_running", lambda *_args: False)
    monkeypatch.setattr(
        server.supervisor, "stop", lambda app: running.pop(app, None) or {})
    starts = []

    def fail_start(*_args, **_kwargs):
        starts.append("new")
        raise supervisor.SupervisorError("new READY failed")

    monkeypatch.setattr(server, "_prepare_external_start", lambda *_args: {})
    monkeypatch.setattr(server, "_coordinated_legacy_start", fail_start)
    monkeypatch.setattr(server, "_rollback_upgrade", lambda *_args, **_kwargs: None)

    with pytest.raises(supervisor.SupervisorError, match="READY failed"):
        server.do_install(_package(layout, "2.0.0"))

    assert starts == ["new"]
    record = state.get_app(APP_ID)
    assert record["observed_state"] == "stopping"
    assert record["teardown_pending"] is True
    assert installer.load_install_transaction() is not None


def test_upgrade_rollback_rechecks_captured_leader_after_record_clear(
        layout, monkeypatch):
    server.do_install(_package(layout, "1.0.0"))
    state.set_active(APP_ID, "1.0.0")
    running = {APP_ID: 4242}
    stops = []
    starts = []
    monkeypatch.setattr(
        server.supervisor, "is_running", lambda app: running.get(app))
    monkeypatch.setattr(server.supervisor, "has_run_record", lambda _app: False)
    monkeypatch.setattr(
        server.supervisor, "owned_pid_is_running",
        lambda _app, pid: pid == 5252)

    def stop(app_id):
        stops.append(running.pop(app_id, None))
        # Model stop() deleting the run record even though the captured new
        # leader remains alive according to its independent /proc identity.
        return {"stopped": app_id}

    def start(app_id, operation, proof=None, **_kwargs):
        starts.append(operation)
        if operation == "upgrade_restart":
            running[app_id] = 5252
            raise supervisor.SupervisorError("injected new READY failure")
        raise AssertionError("the retained release must not be restarted")

    monkeypatch.setattr(server.supervisor, "stop", stop)
    monkeypatch.setattr(server, "_prepare_external_start", lambda *_args: {})
    monkeypatch.setattr(server, "_coordinated_legacy_start", start)

    with pytest.raises(supervisor.SupervisorError, match="READY failure"):
        server.do_install(_package(layout, "2.0.0"))

    assert stops == [4242, 5252]
    assert starts == ["upgrade_restart"]
    # The failed generation is still potentially executing, so neither its
    # code nor the recovery authority may be removed underneath it.
    assert _installed_version() == "2.0.0"
    assert installer.load_install_transaction() is not None
    record = state.get_app(APP_ID)
    assert record["observed_state"] == "stopping"
    assert record["teardown_pending"] is True


def test_managed_ready_process_fence_keeps_target_journal_and_generation(
        layout, monkeypatch):
    server.do_install(_package(layout, "1.0.0"))
    state.begin_start(
        APP_ID, "old-managed", version="1.0.0", launch_mode="managed")
    state.transition(
        APP_ID, "running", pid=4242, pgid=4242,
        allocations=["old-allocation"])
    runtime = {"pid": 4242, "record": True}
    calls = {"stop": 0, "start": []}

    class Coordinator:
        def stop(self, app_id, *, desired):
            calls["stop"] += 1
            if calls["stop"] > 1:
                raise supervisor.ProcessFenceError(
                    app_id, 5252, 5252,
                    leader_alive=False, group_alive=True)
            runtime["pid"] = None
            runtime["record"] = False
            state.set_desired(app_id, desired)
            state.transition(
                app_id, "stopped", pid=None, pgid=None, allocations=[],
                teardown_pending=False)
            return {"stopped": app_id}

        def start(self, app_id, **kwargs):
            calls["start"].append(kwargs.get("operation"))
            runtime["pid"] = None
            runtime["record"] = True
            state.begin_start(
                app_id, "failed-new", version="2.0.0",
                launch_mode="managed")
            state.transition(
                app_id, "stopping", pid=5252, pgid=5252,
                allocations=["new-allocation"], teardown_pending=True)
            raise supervisor.ProcessFenceError(
                app_id, 5252, 5252,
                leader_alive=False, group_alive=True)

    coordinator = Coordinator()
    monkeypatch.setattr(server, "_coordinator", lambda: coordinator)
    monkeypatch.setattr(
        server.supervisor, "is_running", lambda _app: runtime["pid"])
    monkeypatch.setattr(
        server.supervisor, "has_run_record", lambda _app: runtime["record"])
    monkeypatch.setattr(
        server.supervisor, "owned_pid_is_running", lambda *_args: False)

    with pytest.raises(supervisor.ProcessFenceError):
        server.do_install(_package(layout, "2.0.0"))

    # The failed target generation still owns an authenticated residual group.
    # Restoring or launching the retained release would overlap it.
    assert calls == {"stop": 2, "start": ["upgrade_restart"]}
    assert _installed_version() == "2.0.0"
    assert runtime["record"] is True
    assert installer.load_install_transaction() is not None
    record = state.get_app(APP_ID)
    assert record["observed_state"] == "stopping"
    assert record["teardown_pending"] is True
    assert record["instance_id"] == "failed-new"
    assert record["allocations"] == ["new-allocation"]


def test_fresh_install_rollback_rechecks_captured_leader_before_delete(
        layout, monkeypatch):
    running = {}
    rollback_calls = []
    monkeypatch.setattr(
        server.supervisor, "is_running", lambda app: running.get(app))
    monkeypatch.setattr(server.supervisor, "has_run_record", lambda _app: False)
    monkeypatch.setattr(
        server.supervisor, "owned_pid_is_running",
        lambda _app, pid: pid == 6262)

    def stop(app_id):
        running.pop(app_id, None)
        return {"stopped": app_id}

    def fail_config(_manifest, app_id):
        # Fault injection: the just-published generation acquired a process
        # between commit and config validation, then its stop erased the only
        # run record without actually terminating the owned leader.
        running[app_id] = 6262
        raise ValueError("injected fresh config failure")

    monkeypatch.setattr(server.supervisor, "stop", stop)
    monkeypatch.setattr(
        server.appconfig, "revalidate_user_config", fail_config)
    monkeypatch.setattr(
        server.installer, "rollback_install_transaction_files",
        lambda transaction: rollback_calls.append(transaction))

    with pytest.raises(ValueError, match="fresh config failure"):
        server.do_install(_package(layout, "1.0.0"))

    assert rollback_calls == []
    assert _installed_version() == "1.0.0"
    assert installer.load_install_transaction() is not None
    record = state.get_app(APP_ID)
    assert record["observed_state"] == "stopping"
    assert record["teardown_pending"] is True


def _identity(pathname):
    info = os.stat(pathname)
    return [int(info.st_dev), int(info.st_ino)]


def _environment(app_id, releases, current, rollback=None):
    root = paths.venv_dir(app_id)
    for release in releases:
        os.makedirs(os.path.join(root, "releases", release), exist_ok=True)
    if current is not None:
        os.symlink(os.path.join("releases", current), os.path.join(root, "current"))
    if rollback is not None:
        with open(os.path.join(root, pythonenv.ROLLBACK_RECORD), "w") as output:
            json.dump({
                "env_format": pythonenv.ENV_FORMAT_VERSION,
                "new_release_id": rollback[0],
                "previous_release_id": rollback[1],
                "release_lock_sha256": "0" * 64,
            }, output)
    return root


@pytest.mark.parametrize("phase", ["code_published", "publishing_environment"])
def test_crash_before_env_switch_does_not_consume_older_rollback_record(
        layout, phase):
    app_id = "journal-app"
    dest = paths.app_dir(app_id)
    prev = dest + ".prev"
    os.makedirs(dest)
    os.makedirs(prev)
    (layout / "apps" / app_id / "marker").write_text("target")
    (layout / "apps" / (app_id + ".prev") / "marker").write_text("installed")
    transaction = {
        "app_id": app_id,
        "phase": phase,
        "pre_installed": True,
        "previous_dir_identity": _identity(prev),
        "target_dir_identity": _identity(dest),
        "installed_release_id": "A",
        "target_release_id": "B",
        "previous_environment_release_id": "A",
    }
    root = _environment(app_id, ["P", "A", "B"], "A", rollback=("A", "P"))
    rollback_bytes = open(
        os.path.join(root, pythonenv.ROLLBACK_RECORD), "rb").read()

    assert installer.rollback_install_transaction_files(transaction) is True
    assert open(os.path.join(dest, "marker")).read() == "installed"
    assert pythonenv.current_release_id(app_id) == "A"
    assert open(os.path.join(
        root, pythonenv.ROLLBACK_RECORD), "rb").read() == rollback_bytes


def test_crash_rollback_retry_finishes_environment_after_code_was_restored(layout):
    app_id = "journal-retry"
    dest = paths.app_dir(app_id)
    os.makedirs(dest)
    (layout / "apps" / app_id / "marker").write_text("installed")
    transaction = {
        "app_id": app_id,
        "phase": "publishing_environment",
        "pre_installed": True,
        "previous_dir_identity": _identity(dest),
        "target_dir_identity": [999, 999],
        "installed_release_id": "A",
        "target_release_id": "B",
        "previous_environment_release_id": "A",
    }
    _environment(app_id, ["A", "B"], "B", rollback=("B", "A"))

    assert installer.rollback_install_transaction_files(transaction) is False
    assert open(os.path.join(dest, "marker")).read() == "installed"
    assert pythonenv.current_release_id(app_id) == "A"


def test_crash_after_target_moved_to_failed_resumes_code_and_env_rollback(layout):
    app_id = "journal-mid-rollback"
    dest = paths.app_dir(app_id)
    prev = dest + ".prev"
    failed = dest + ".failed"
    os.makedirs(prev)
    os.makedirs(failed)
    open(os.path.join(prev, "marker"), "w").write("installed")
    open(os.path.join(failed, "marker"), "w").write("target")
    transaction = {
        "app_id": app_id,
        "phase": "publishing_environment",
        "pre_installed": True,
        "previous_dir_identity": _identity(prev),
        "target_dir_identity": _identity(failed),
        "installed_release_id": "A",
        "target_release_id": "B",
        "previous_environment_release_id": "A",
    }
    _environment(app_id, ["A", "B"], "B", rollback=("B", "A"))

    assert installer.rollback_install_transaction_files(transaction) is True
    assert open(os.path.join(dest, "marker")).read() == "installed"
    assert pythonenv.current_release_id(app_id) == "A"
    assert not os.path.exists(prev)
    assert not os.path.exists(failed)


@pytest.mark.parametrize("phase", ["code_published", "publishing_environment"])
def test_startup_reconcile_rolls_back_published_code_before_boot_restore(
        layout, phase):
    server.do_install(_package(layout, "1.0.0"))
    appdata = paths.appdata_dir(APP_ID)
    os.makedirs(appdata, exist_ok=True)
    config_path = os.path.join(appdata, "config.json")
    with open(config_path, "wb") as output:
        output.write(b'{"before":1}\n')
    config_snapshot = appconfig.snapshot_upgrade_config(APP_ID)
    lifecycle_snapshot = state.snapshot_app(APP_ID)
    candidate = installer.prepare(_package(layout, "2.0.0"))
    installer.begin_install_transaction(
        candidate, config_snapshot=config_snapshot,
        lifecycle_snapshot=lifecycle_snapshot)
    installer.mark_install_transaction(candidate, "stopped")
    installer.mark_install_transaction(candidate, "publishing_code")
    os.rename(paths.app_dir(APP_ID), paths.app_dir(APP_ID) + ".prev")
    os.rename(candidate.staging, paths.app_dir(APP_ID))
    candidate.staging = None
    installer.mark_install_transaction(candidate, "code_published")
    if phase == "publishing_environment":
        installer.mark_install_transaction(candidate, phase)
    with open(config_path, "wb") as output:
        output.write(b'{"after":2}\n')

    result = server._reconcile_install_transaction()

    assert result == {
        "app_id": APP_ID, "phase": phase, "action": "rolled_back"}
    assert _installed_version() == "1.0.0"
    assert open(config_path, "rb").read() == b'{"before":1}\n'
    assert state.get_app(APP_ID) == lifecycle_snapshot["record"]
    assert installer.load_install_transaction() is None


@pytest.mark.parametrize("record_kind", ["cross_boot", "missing"])
def test_boot_restore_retries_stopping_generation_before_resource_reconcile(
        layout, monkeypatch, record_kind):
    server.do_install(_package(layout, "1.0.0"))
    state.begin_start(
        APP_ID, "residual-instance", version="1.0.0",
        launch_mode="managed")
    state.transition(
        APP_ID, "stopping", pid=8181, pgid=8181,
        allocations=["residual-allocation"], teardown_pending=True)
    if record_kind == "cross_boot":
        for pathname, value in (
                (paths.pidfile(APP_ID), "8181"),
                (paths.pgidfile(APP_ID), "8181"),
                (paths.bootfile(APP_ID), "previous-boot")):
            with open(pathname, "w") as output:
                output.write(value)
    events = []

    class Coordinator:
        @staticmethod
        def stop(app_id, *, desired):
            events.append(("stop", desired, record_kind))
            assert desired == state.DESIRED_RUNNING
            for pathname in (paths.pidfile(app_id), paths.pgidfile(app_id),
                             paths.bootfile(app_id)):
                try:
                    os.unlink(pathname)
                except FileNotFoundError:
                    pass
            state.set_desired(app_id, desired)
            state.transition(
                app_id, "stopped", pid=None, pgid=None, allocations=[],
                teardown_pending=False)
            return {"stopped": app_id}

        @staticmethod
        def reconcile_allocations():
            events.append(("reconcile",))
            record = state.get_app(APP_ID)
            assert record["observed_state"] == "stopped"
            assert record["teardown_pending"] is False
            assert record["allocations"] == []
            return ["residual-allocation"]

        @staticmethod
        def start(app_id, **_kwargs):
            events.append(("start",))
            state.begin_start(
                app_id, "restored-instance", version="1.0.0",
                launch_mode="managed")
            state.transition(app_id, "running", pid=9191, pgid=9191)
            return {"pid": 9191, "observed_state": "running"}

    monkeypatch.setattr(server, "_coordinator", lambda: Coordinator())
    monkeypatch.setattr(server.supervisor, "is_running", lambda _app: None)
    monkeypatch.setattr(server, "_audit", lambda *_args, **_kwargs: None)

    server._boot_restore_locked()

    assert [event[0] for event in events] == ["stop", "reconcile", "start"]
    restored = state.get_app(APP_ID)
    assert restored["desired_state"] == state.DESIRED_RUNNING
    assert restored["observed_state"] == "running"
    assert restored["instance_id"] == "restored-instance"


def test_boot_restore_reclaims_cross_boot_running_lease_before_new_start(
        layout, monkeypatch):
    server.do_install(_package(layout, "1.0.0"))
    manifest = server._read_manifest(APP_ID)
    old = state.begin_start(
        APP_ID, "pre-reboot-instance", version="1.0.0",
        launch_mode="managed")
    manager = server._coordinator().resources
    old_allocations = manager.reserve(
        APP_ID, old["instance_id"], old["generation"],
        resources.plan_manifest(manifest, {}))
    manager.bind(old["instance_id"], old["generation"])
    old_ids = [item["allocation_id"] for item in old_allocations]
    state.transition(
        APP_ID, "running", pid=8181, pgid=8181,
        allocations=old_ids, started_at=1.0)
    for pathname, value in (
            (paths.pidfile(APP_ID), "8181"),
            (paths.pgidfile(APP_ID), "8181"),
            (paths.bootfile(APP_ID), "previous-kernel-boot")):
        with open(pathname, "w") as output:
            output.write(value)

    events = []
    real_sweep = supervisor.sweep_stale
    real_reconcile = manager.reconcile

    def tracked_sweep():
        events.append("sweep")
        return real_sweep()

    def tracked_reconcile(live_instances):
        events.append("resource-reconcile")
        return real_reconcile(live_instances)

    def launch(**kwargs):
        events.append("launch")
        kwargs["on_spawn"](9191)
        return 9191

    monkeypatch.setattr(server.supervisor, "sweep_stale", tracked_sweep)
    monkeypatch.setattr(manager, "reconcile", tracked_reconcile)
    monkeypatch.setattr(server, "_managed_launch", lambda *_args: launch)
    monkeypatch.setattr(server, "_audit", lambda *_args, **_kwargs: None)

    server._boot_restore_locked()

    assert events == ["sweep", "resource-reconcile", "launch"]
    restored = state.get_app(APP_ID)
    assert restored["observed_state"] == "running"
    assert restored["pid"] == 9191
    assert restored["instance_id"] != old["instance_id"]
    assert restored["generation"] > old["generation"]
    current = manager.snapshot()["allocations"]
    assert current
    assert not {item["allocation_id"] for item in current}.intersection(old_ids)
    assert {item["instance_id"] for item in current} == {
        restored["instance_id"]}


def test_boot_restore_reuses_reservation_committed_before_state_advance(
        layout, monkeypatch):
    server.do_install(_package(layout, "1.0.0"))
    manifest = server._read_manifest(APP_ID)
    plan = resources.plan_manifest(manifest, {})
    old = state.begin_start(
        APP_ID, "reserved-before-daemon-exit", version="1.0.0",
        launch_mode="managed")
    state.transition(
        APP_ID, "waiting_resource", resource_plan=plan.as_dict(),
        frame_stream_contract=supervisor.managed_frame_stream_contract(
            plan.as_dict()))
    manager = server._coordinator().resources
    reserved = manager.reserve(
        APP_ID, old["instance_id"], old["generation"], plan)
    reserved_ids = [item["allocation_id"] for item in reserved]
    assert reserved_ids
    assert state.get_app(APP_ID)["allocations"] == []

    events = []
    real_reconcile = manager.reconcile

    def tracked_reconcile(live_instances):
        events.append("resource-reconcile")
        return real_reconcile(live_instances)

    def launch(**kwargs):
        events.append("launch")
        kwargs["on_spawn"](9292)
        return 9292

    monkeypatch.setattr(
        server.supervisor, "sweep_stale",
        lambda: events.append("sweep") or [])
    monkeypatch.setattr(manager, "reconcile", tracked_reconcile)
    monkeypatch.setattr(server, "_managed_launch", lambda *_args: launch)
    monkeypatch.setattr(server, "_audit", lambda *_args, **_kwargs: None)

    server._boot_restore_locked()

    assert events == ["sweep", "resource-reconcile", "launch"]
    restored = state.get_app(APP_ID)
    assert restored["observed_state"] == "running"
    assert restored["pid"] == 9292
    assert restored["instance_id"] == old["instance_id"]
    assert restored["generation"] == old["generation"]
    assert restored["allocations"] == reserved_ids
    current = manager.allocations_for(old["instance_id"])
    assert [item["allocation_id"] for item in current] == reserved_ids
    assert {item["state"] for item in current} == {"bound"}


def test_boot_finishes_interrupted_stop_intent_for_live_generation(
        layout, monkeypatch):
    server.do_install(_package(layout, "1.0.0"))
    manifest = server._read_manifest(APP_ID)
    old = state.begin_start(
        APP_ID, "live-stop-intent", version="1.0.0",
        launch_mode="managed")
    manager = server._coordinator().resources
    allocations = manager.reserve(
        APP_ID, old["instance_id"], old["generation"],
        resources.plan_manifest(manifest, {}))
    manager.bind(old["instance_id"], old["generation"])
    allocation_ids = [item["allocation_id"] for item in allocations]
    state.transition(
        APP_ID, "running", pid=8383, pgid=8383,
        allocations=allocation_ids, started_at=1.0)
    # Reproduce a daemon exit after stop() persisted desired=stopped but before
    # it could publish the stopping phase or signal the old generation.
    state.set_desired(APP_ID, state.DESIRED_STOPPED)
    live = {"pid": 8383}
    events = []

    monkeypatch.setattr(
        server.supervisor, "sweep_stale",
        lambda: events.append("sweep") or [])
    monkeypatch.setattr(
        server.supervisor, "is_running", lambda _app: live["pid"])
    monkeypatch.setattr(
        server.supervisor, "has_run_record",
        lambda _app: live["pid"] is not None)

    def stop_process(app_id, **_kwargs):
        events.append("stop-process")
        pid = live["pid"]
        live["pid"] = None
        return {"app": app_id, "pid": pid, "signalled": True,
                "killed": False}

    real_reconcile = manager.reconcile

    def tracked_reconcile(live_instances):
        events.append("resource-reconcile")
        return real_reconcile(live_instances)

    monkeypatch.setattr(server.supervisor, "stop", stop_process)
    monkeypatch.setattr(manager, "reconcile", tracked_reconcile)
    monkeypatch.setattr(server, "_audit", lambda *_args, **_kwargs: None)

    server._boot_restore_locked()

    assert events == ["sweep", "stop-process", "resource-reconcile"]
    stopped = state.get_app(APP_ID)
    assert stopped["desired_state"] == state.DESIRED_STOPPED
    assert stopped["observed_state"] == "stopped"
    assert stopped["allocations"] == []
    assert manager.allocations_for(old["instance_id"]) == []


def test_boot_releases_interrupted_stop_intent_waiting_reservation(
        layout, monkeypatch):
    server.do_install(_package(layout, "1.0.0"))
    manifest = server._read_manifest(APP_ID)
    plan = resources.plan_manifest(manifest, {})
    old = state.begin_start(
        APP_ID, "waiting-stop-intent", version="1.0.0",
        launch_mode="managed")
    state.transition(APP_ID, "waiting_resource", resource_plan=plan.as_dict())
    manager = server._coordinator().resources
    manager.reserve(APP_ID, old["instance_id"], old["generation"], plan)
    state.set_desired(APP_ID, state.DESIRED_STOPPED)
    stops = []

    monkeypatch.setattr(server.supervisor, "sweep_stale", lambda: [])
    monkeypatch.setattr(server.supervisor, "is_running", lambda _app: None)
    monkeypatch.setattr(server.supervisor, "has_run_record", lambda _app: False)
    monkeypatch.setattr(
        server.supervisor, "stop",
        lambda app_id, **_kwargs: stops.append(app_id) or {
            "app": app_id, "pid": None, "signalled": False, "killed": False,
        })
    monkeypatch.setattr(server, "_audit", lambda *_args, **_kwargs: None)

    server._boot_restore_locked()

    stopped = state.get_app(APP_ID)
    assert stops == [APP_ID]
    assert stopped["desired_state"] == state.DESIRED_STOPPED
    assert stopped["observed_state"] == "stopped"
    assert stopped["allocations"] == []
    assert manager.allocations_for(old["instance_id"]) == []


def test_boot_restore_never_mints_over_same_boot_residual_fence(
        layout, monkeypatch):
    server.do_install(_package(layout, "1.0.0"))
    old = state.begin_start(
        APP_ID, "same-boot-residual", version="1.0.0",
        launch_mode="managed")
    state.transition(
        APP_ID, "running", pid=8181, pgid=8181,
        allocations=["old-exclusive-lease"], started_at=1.0)
    before = state.get_app(APP_ID)
    events = []

    class Coordinator:
        @staticmethod
        def stop(app_id, *, desired):
            events.append("stop-fence")
            state.set_desired(app_id, desired)
            state.transition(
                app_id, "stopping", teardown_pending=True,
                reason="same-boot process group remains")
            raise RuntimeError("same-boot process group remains")

        @staticmethod
        def reconcile_allocations():
            events.append("resource-reconcile")
            return []

        @staticmethod
        def start(*_args, **_kwargs):
            events.append("launch")
            raise AssertionError("must not mint over a retained process fence")

    monkeypatch.setattr(server, "_coordinator", lambda: Coordinator())
    monkeypatch.setattr(
        server.supervisor, "sweep_stale",
        lambda: events.append("sweep-retained") or [])
    monkeypatch.setattr(server.supervisor, "is_running", lambda _app: None)
    monkeypatch.setattr(server.supervisor, "has_run_record", lambda _app: True)
    monkeypatch.setattr(server, "_audit", lambda *_args, **_kwargs: None)

    server._boot_restore_locked()

    assert events == ["sweep-retained", "stop-fence", "resource-reconcile"]
    retained = state.get_app(APP_ID)
    assert retained["observed_state"] == "stopping"
    assert retained["teardown_pending"] is True
    assert retained["instance_id"] == before["instance_id"] == old["instance_id"]
    assert retained["generation"] == before["generation"] == old["generation"]
    assert retained["allocations"] == ["old-exclusive-lease"]


def test_new_install_with_stale_running_state_remains_stopped(
        layout, monkeypatch):
    state.begin_start(APP_ID, "stale-instance", version="missing")
    state.transition(APP_ID, "failed", pid=None, pgid=None, allocations=[])
    starts = []

    class Coordinator:
        @staticmethod
        def stop(app_id, *, desired):
            state.set_desired(app_id, desired)
            state.transition(
                app_id, "stopped", pid=None, pgid=None, allocations=[],
                teardown_pending=False)
            return {"stopped": app_id}

        @staticmethod
        def start(*args, **kwargs):
            starts.append((args, kwargs))
            raise AssertionError("new install must not auto-start")

    monkeypatch.setattr(server, "_coordinator", lambda: Coordinator())
    monkeypatch.setattr(server.supervisor, "is_running", lambda _app: None)
    monkeypatch.setattr(server.supervisor, "has_run_record", lambda _app: False)
    monkeypatch.setattr(
        server.supervisor, "owned_pid_is_running", lambda *_args: False)

    result = server.do_install(_package(layout, "1.0.0"))
    assert result["auto_started"] is False
    assert starts == []
    record = state.get_app(APP_ID)
    assert record["desired_state"] == state.DESIRED_STOPPED
    assert record["observed_state"] == "stopped"
    assert record["launch_mode"] == "managed"


def test_fresh_running_confirmation_rechecked_before_any_mutation(
        layout, monkeypatch):
    server.do_install(_package(layout, "1.0.0"))
    replacement = _package(layout, "2.0.0")
    preflight_info = installer.inspect(replacement)
    assert "running_upgrade_confirmed" not in server._install_context(
        preflight_info)["confirmation_required"]

    stops = []
    monkeypatch.setattr(server.supervisor, "is_running", lambda _app: 4242)
    monkeypatch.setattr(server.supervisor, "has_run_record", lambda _app: False)
    monkeypatch.setattr(server.supervisor, "stop", lambda app: stops.append(app))

    with pytest.raises(server.BusyError, match="running application"):
        server.do_install(
            replacement, _enforce_v1_confirmations=True,
            running_upgrade_confirmed=False,
            force_reinstall_confirmed=True)

    assert stops == []
    assert _installed_version() == "1.0.0"
    assert installer.load_install_transaction() is None


def test_running_exact_reinstall_requires_both_fresh_confirmations(
        layout, monkeypatch):
    server.do_install(_package(layout, "1.0.0"))
    replacement = _package(layout, "2.0.0")
    state.set_active(APP_ID, "1.0.0")
    running = {APP_ID: 4242}
    stops = []
    starts = []
    monkeypatch.setattr(
        server, "_install_context", lambda *_args, **_kwargs: {
            "mode": "reinstall"})
    monkeypatch.setattr(
        server.supervisor, "is_running", lambda app: running.get(app))
    monkeypatch.setattr(server.supervisor, "has_run_record", lambda _app: False)
    monkeypatch.setattr(
        server.supervisor, "owned_pid_is_running", lambda *_args: False)

    def stop(app_id):
        stops.append(app_id)
        running.pop(app_id, None)
        return {"stopped": app_id}

    def start(app_id, *_args, **_kwargs):
        starts.append(app_id)
        running[app_id] = 5252
        return 5252

    monkeypatch.setattr(server.supervisor, "stop", stop)
    monkeypatch.setattr(server, "_prepare_external_start", lambda *_args: {})
    monkeypatch.setattr(server, "_coordinated_legacy_start", start)

    with pytest.raises(server.BusyError, match="running application"):
        server.do_install(
            replacement, _enforce_v1_confirmations=True,
            running_upgrade_confirmed=False,
            force_reinstall_confirmed=True)
    with pytest.raises(server.BusyError, match="exact release"):
        server.do_install(
            replacement, _enforce_v1_confirmations=True,
            running_upgrade_confirmed=True,
            force_reinstall_confirmed=False)
    assert stops == []
    assert starts == []

    result = server.do_install(
        replacement, _enforce_v1_confirmations=True,
        running_upgrade_confirmed=True,
        force_reinstall_confirmed=True)
    assert result["restarted"] is True
    assert stops == [APP_ID]
    assert starts == [APP_ID]
    assert _installed_version() == "2.0.0"

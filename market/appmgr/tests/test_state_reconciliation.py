"""Startup-only persistence of state.json canonicalisation."""

import json
import os
import sys
import threading
import time
from contextlib import contextmanager

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
from appmgr import paths, server, state


@pytest.fixture
def state_layout(tmp_path, monkeypatch):
    apps = tmp_path / "apps"
    appmgr = tmp_path / "appmgr"
    apps.mkdir()
    appmgr.mkdir()
    monkeypatch.setattr(paths, "APPS_DIR", str(apps))
    monkeypatch.setattr(paths, "APPMGR_DIR", str(appmgr))
    monkeypatch.setattr(paths, "STATE_FILE", str(apps / "state.json"))
    return apps / "state.json"


def _write_raw(path, value):
    path.write_text(json.dumps(value, sort_keys=True))


def test_startup_reconcile_persists_stale_stopped_diagnostic_cleanup(
        state_layout):
    raw = {
        "schema_version": 2,
        "revision": 17,
        "active_app": None,
        "active_version": None,
        "apps": {
            "qrcode-reader": {
                "id": "qrcode-reader",
                "desired_state": "stopped",
                "observed_state": "stopped",
                "generation": 9,
                "blocked_resource": "camera.frames:camera-0",
                "resource_owners": ["ppocr"],
                "dependency": {"available": False},
                "runtime_guard": {"resource": "thermal.runtime"},
            },
        },
    }
    _write_raw(state_layout, raw)

    result = server._reconcile_startup_state()

    assert result == {
        "status": "reconciled",
        "changed": True,
        "previous_revision": 17,
        "revision": 18,
    }
    persisted = json.loads(state_layout.read_text())
    rec = persisted["apps"]["qrcode-reader"]
    assert persisted["revision"] == 18
    assert rec["desired_state"] == "stopped"
    assert rec["observed_state"] == "stopped"
    assert rec["generation"] == 9
    assert rec["blocked_resource"] is None
    assert rec["resource_owners"] == []
    assert rec["dependency"] is None
    assert rec["runtime_guard"] is None


def test_reconcile_preserves_v1_active_desired_state(state_layout):
    _write_raw(state_layout, {
        "revision": 3,
        "active_app": "legacy-app",
        "active_version": "1.2.3",
    })

    result = state.reconcile_persisted()

    persisted = json.loads(state_layout.read_text())
    assert result["revision"] == 4
    assert persisted["active_app"] == "legacy-app"
    assert persisted["active_version"] == "1.2.3"
    assert persisted["apps"]["legacy-app"]["desired_state"] == "running"
    assert persisted["apps"]["legacy-app"]["launch_mode"] == "legacy"


def test_safe_app_extension_fields_are_preserved_verbatim(state_layout):
    extension = {
        "claims": [{"name": "camera.frames", "mode": "shared"}],
        "limits": {"memory_mb": 128},
    }
    _write_raw(state_layout, {
        "revision": 6,
        "apps": {
            "extended-app": {
                "desired_state": "running",
                "observed_state": "stopped",
                "resource_plan": extension,
            },
        },
    })

    result = state.reconcile_persisted()

    assert result["status"] == "reconciled"
    persisted = json.loads(state_layout.read_text())
    assert persisted["apps"]["extended-app"]["desired_state"] == "running"
    assert persisted["apps"]["extended-app"]["resource_plan"] == extension


def test_canonical_state_is_not_rewritten(state_layout):
    state.save({
        "revision": 11,
        "active_app": None,
        "active_version": None,
        "apps": {},
    })
    before = os.stat(state_layout)
    before_bytes = state_layout.read_bytes()

    result = state.reconcile_persisted()

    after = os.stat(state_layout)
    assert result == {
        "status": "unchanged", "changed": False, "revision": 11,
    }
    assert state_layout.read_bytes() == before_bytes
    assert after.st_ino == before.st_ino
    assert after.st_mtime_ns == before.st_mtime_ns


def test_state_written_by_normal_lifecycle_is_accepted_without_rewrite(
        state_layout):
    state.save({})
    started = state.begin_start("writer-app", "instance-1", version="1.0.0")
    state.transition(
        "writer-app", "running", pid=4123, pgid=4123,
        allocations=["allocation-1"], started_at=time.time(),
        frame_stream_contract={"id": "main", "kind": "frame.sock",
                               "path": "/live/0"},
    )
    before = os.stat(state_layout)

    result = state.reconcile_persisted()

    after = os.stat(state_layout)
    assert result == {
        "status": "unchanged", "changed": False,
        "revision": started["generation"] + 1,
    }
    assert after.st_ino == before.st_ino
    assert after.st_mtime_ns == before.st_mtime_ns


@pytest.mark.parametrize("payload", [
    b"{broken",
    b"[]",
    b"null",
    b'{"revision": 1, "revision": 2}',
])
def test_invalid_state_is_left_byte_for_byte_untouched(state_layout, payload):
    state_layout.write_bytes(payload)
    before = os.stat(state_layout)

    result = state.reconcile_persisted()

    after = os.stat(state_layout)
    assert result == {
        "status": "invalid", "changed": False, "revision": None,
    }
    assert state_layout.read_bytes() == payload
    assert after.st_ino == before.st_ino
    assert after.st_mtime_ns == before.st_mtime_ns


@pytest.mark.parametrize(
    "raw",
    [
        {"schema_version": 3},
        {"schema_version": True},
        {"schema_version": None},
        {"revision": True},
        {"revision": 1.0},
        {"revision": 1 << 80},
        {"revision": float("nan")},
        {"revision": float("inf")},
        {"revision": float("-inf")},
        {"unknown_top_level": {"desired_state": "running"}},
        {"apps": None},
        {"apps": []},
        {"apps": {"Bad App": {}}},
        {"apps": {"bad-value": []}},
        {"apps": {"bad-id": {"id": "another-app"}}},
        {"apps": {"bad-desired": {"desired_state": "maybe"}}},
        {"apps": {"bad-observed": {"observed_state": "unknown"}}},
        {"apps": {"bad-generation": {"generation": True}}},
        {"apps": {"bad-generation": {"generation": float("inf")}}},
        {"apps": {"bad-generation": {"generation": float("nan")}}},
        {"apps": {"bad-generation": {"generation": 1 << 80}}},
        {"apps": {"bad-allocations": {"allocations": {}}}},
        {"apps": {"bad-allocations": {"allocations": [1]}}},
        {"apps": {"bad-endpoints": {"endpoints": []}}},
        {"apps": {"bad-history": {"restart_history": {}}}},
        {"apps": {"bad-history": {"restart_history": [True]}}},
        {"apps": {"bad-history": {"restart_history": [float("inf")]}}},
        {"apps": {"bad-extension": {
            "vendor_extension": {"score": float("nan")},
        }}},
        {"active_app": None, "active_version": "orphaned"},
        {"schema_version": 2, "active_app": "missing-app"},
        {"schema_version": 2, "active_app": "missing-app", "apps": {}},
    ],
    ids=lambda value: repr(value),
)
def test_ambiguous_valid_json_objects_are_not_repaired_on_disk(
        state_layout, raw):
    payload = json.dumps(raw, sort_keys=True).encode()
    state_layout.write_bytes(payload)
    before = os.stat(state_layout)

    result = state.reconcile_persisted()

    after = os.stat(state_layout)
    assert result == {
        "status": "invalid", "changed": False, "revision": None,
    }
    assert state_layout.read_bytes() == payload
    assert after.st_ino == before.st_ino
    assert after.st_mtime_ns == before.st_mtime_ns


def test_one_bad_record_cannot_overwrite_another_apps_running_intent(
        state_layout):
    raw = {
        "schema_version": 2,
        "revision": 8,
        "active_app": None,
        "active_version": None,
        "apps": {
            "important-app": {
                "id": "important-app",
                "desired_state": "running",
                "observed_state": "stopped",
                "generation": 5,
            },
            "broken-app": ["not", "a", "record"],
        },
    }
    payload = json.dumps(raw, sort_keys=True).encode()
    state_layout.write_bytes(payload)

    result = server._reconcile_startup_state()

    assert result["status"] == "invalid"
    assert state_layout.read_bytes() == payload
    # The existing read fail-safe may shape bad records in memory, but it must
    # neither erase nor demote the unrelated valid desired-running record.
    assert state.load()["apps"]["important-app"]["desired_state"] == "running"


def test_overflow_counters_do_not_escape_load_or_startup_reconcile(state_layout):
    payload = (
        b'{"revision": 1e309, "apps": {"overflow-app": '
        b'{"generation": 1e309, "desired_state": "running"}}}'
    )
    state_layout.write_bytes(payload)

    loaded = state.load()
    result = server._reconcile_startup_state()

    assert loaded["revision"] == 0
    assert loaded["apps"]["overflow-app"]["generation"] == 0
    assert loaded["apps"]["overflow-app"]["desired_state"] == "running"
    assert result["status"] == "invalid"
    assert state_layout.read_bytes() == payload


def test_huge_restart_history_integer_cannot_crash_or_rewrite_startup(
        state_layout):
    raw = {
        "revision": 4,
        "apps": {
            "history-app": {
                "desired_state": "running",
                "observed_state": "backoff",
                "restart_history": [1 << 10000],
            },
        },
    }
    payload = json.dumps(raw, sort_keys=True).encode()
    state_layout.write_bytes(payload)

    loaded = state.load()
    result = server._reconcile_startup_state()

    assert loaded["apps"]["history-app"]["restart_history"] == []
    assert result["status"] == "invalid"
    assert state_layout.read_bytes() == payload


def test_normal_save_and_mutate_sanitise_overflow_counters_without_raising(
        state_layout):
    state.save({
        "revision": float("inf"),
        "apps": {
            "ordinary-app": {
                "desired_state": "stopped",
                "observed_state": "stopped",
                "generation": float("inf"),
            },
        },
    })
    saved = json.loads(state_layout.read_text())
    assert saved["revision"] == 0
    assert saved["apps"]["ordinary-app"]["generation"] == 0

    state.set_desired("ordinary-app", state.DESIRED_RUNNING)
    mutated = json.loads(state_layout.read_text())
    assert mutated["revision"] == 1
    assert mutated["apps"]["ordinary-app"]["desired_state"] == "running"


def test_json_equality_is_type_aware_for_booleans():
    assert not state._strict_json_equal(True, 1)
    assert not state._strict_json_equal(
        {"revision": True}, {"revision": 1})


def test_reconcile_serialises_with_process_local_mutation(
        state_layout, monkeypatch):
    _write_raw(state_layout, {
        "schema_version": 2,
        "revision": 4,
        "active_app": None,
        "active_version": None,
        "apps": {
            "race-app": {
                "id": "race-app",
                "desired_state": "stopped",
                "observed_state": "stopped",
                "blocked_resource": "old-owner",
            },
        },
    })
    entered_write = threading.Event()
    release_write = threading.Event()
    mutation_done = threading.Event()
    original_write = state._write_normalised

    def blocking_write(data):
        entered_write.set()
        assert release_write.wait(2.0)
        original_write(data)

    monkeypatch.setattr(state, "_write_normalised", blocking_write)
    reconcile_thread = threading.Thread(target=state.reconcile_persisted)
    reconcile_thread.start()
    assert entered_write.wait(1.0)

    def mutate_desired():
        state.set_desired("race-app", state.DESIRED_RUNNING)
        mutation_done.set()

    mutation_thread = threading.Thread(target=mutate_desired)
    mutation_thread.start()
    time.sleep(0.05)
    assert not mutation_done.is_set(), "mutation bypassed reconciliation lock"
    release_write.set()
    reconcile_thread.join(2.0)
    mutation_thread.join(2.0)
    assert not reconcile_thread.is_alive()
    assert not mutation_thread.is_alive()

    persisted = json.loads(state_layout.read_text())
    assert persisted["revision"] == 6
    assert persisted["apps"]["race-app"]["desired_state"] == "running"
    assert persisted["apps"]["race-app"]["blocked_resource"] is None


def test_startup_reconcile_obeys_cross_process_busy_gate(
        state_layout, monkeypatch):
    _write_raw(state_layout, {"revision": 2, "active_app": None})
    before = state_layout.read_bytes()
    monkeypatch.setattr(paths, "V1_OPERATION_BUSY_TIMEOUT_SEC", 0.0)

    with server.busy_gate():
        with pytest.raises(server.BusyError):
            server._reconcile_startup_state()

    assert state_layout.read_bytes() == before


def test_boot_restore_retries_from_fresh_state_after_cli_gate_contention(
        state_layout, monkeypatch):
    restores = []
    audits = []
    attempts = []

    @contextmanager
    def contended_gate(**_kwargs):
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            raise server.BusyError("held by CLI")
        yield

    monkeypatch.setattr(server, "busy_gate", contended_gate)
    monkeypatch.setattr(server.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        server, "_boot_restore_locked", lambda: restores.append("restore"))
    monkeypatch.setattr(
        server, "_audit",
        lambda action, **fields: audits.append((action, fields)))

    server._boot_restore()

    assert attempts == [1, 2]
    assert restores == ["restore"]
    assert audits == [("boot_restore_waiting", {
        "reason": "mutation gate busy",
    })]


def test_serve_reconciles_after_singleton_and_before_coordinator(monkeypatch):
    # serve owns one daemon lifetime and deliberately leaves mutation admission
    # closed on exit. Isolate that lifecycle from later unit tests in this VM.
    monkeypatch.setattr(server, "_service_stopping", False)
    events = []

    def acquire():
        events.append("single-instance")
        return True

    def reconcile():
        events.append("state-reconcile")
        raise RuntimeError("stop after ordering assertion")

    def coordinator():
        events.append("coordinator")
        raise AssertionError("coordinator constructed before state reconcile")

    monkeypatch.setattr(server, "_acquire_single_instance", acquire)
    monkeypatch.setattr(server, "_reconcile_startup_state", reconcile)
    monkeypatch.setattr(server, "_coordinator", coordinator)

    with pytest.raises(RuntimeError, match="ordering assertion"):
        server.serve(host="127.0.0.1", port=1)

    assert events == ["single-instance", "state-reconcile"]

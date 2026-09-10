"""Durable desired/observed state for the multi-application manager.

``state.json`` used to contain only ``active_app`` and ``active_version``.
Those keys remain authoritative for the legacy activate/switch API, while the
v2 ``apps`` map records independent desired and observed lifecycle state for
every managed application.  Loading a v1 file is an in-memory migration: its
active application becomes one desired-running legacy application; all other
applications stay opt-in.

All mutations rewrite the complete document through fsync + rename + directory
fsync.  appmgr's busy gate serialises HTTP mutations, and the process-local
lock also protects the gateway/reaper threads that update observations.  Plain
loads remain side-effect free; the daemon performs an explicit, one-time
persisted-state reconciliation only after it owns the single-instance and
cross-process mutation locks.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import time
from typing import Callable, Dict, List, Optional

from . import paths


SCHEMA_VERSION = 2

DESIRED_STOPPED = "stopped"
DESIRED_RUNNING = "running"
DESIRED_STATES = {DESIRED_STOPPED, DESIRED_RUNNING}

OBSERVED_STATES = {
    "installed",
    "stopped",
    "preparing_env",
    "waiting_resource",
    "starting",
    "ready",
    "running",
    "stopping",
    "waiting_dependency",
    "degraded",
    "failed",
    "backoff",
    "crash_loop",
}

_LOCK = threading.RLock()
_MAX_DURABLE_COUNTER = (1 << 63) - 2
_TOP_LEVEL_FIELDS = frozenset((
    "schema_version", "revision", "active_app", "active_version", "apps",
))


def _now() -> float:
    return time.time()


def _empty() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "revision": 0,
        # Kept for the old Web/CLI contract.  Managed concurrent starts do not
        # change this pointer; only activate/switch do.
        "active_app": None,
        "active_version": None,
        "apps": {},
    }


def _app_defaults(app_id: str, *, desired: str = DESIRED_STOPPED) -> dict:
    return {
        "id": app_id,
        "desired_state": desired,
        "observed_state": "stopped",
        "generation": 0,
        "instance_id": None,
        "pid": None,
        "pgid": None,
        "version": None,
        "launch_mode": "managed",
        "reason": None,
        # True means the previous generation has stopped (or become
        # unobservable) but one of its security/resource teardown barriers did
        # not complete.  Admission must retain the old identity and allocations
        # until an explicit retry finishes that exact generation.
        "teardown_pending": False,
        "allocations": [],
        "endpoints": {},
        # Exact frame route minted by the coordinator for this generation.
        # Older/adopted processes have no contract and therefore fail closed.
        "frame_stream_contract": {"id": "", "kind": "none"},
        # Admission diagnostics are meaningful only while the app is waiting
        # on that exact condition.  Keeping them in the stable record shape
        # lets old state files be normalised without leaking stale owners into
        # a later stopped/running generation.
        "blocked_resource": None,
        "resource_owners": [],
        "dependency": None,
        "runtime_guard": None,
        "restart_history": [],
        "next_retry_at": None,
        "started_at": None,
        "updated_at": _now(),
    }


def _normalise(raw) -> dict:
    """Return a well-shaped v2 document without trusting persisted types."""
    if not isinstance(raw, dict):
        return _empty()
    out = _empty()
    try:
        out["revision"] = max(0, int(raw.get("revision", 0)))
    except (TypeError, ValueError, OverflowError):
        pass

    active = raw.get("active_app")
    if isinstance(active, str) and paths.valid_app_id(active):
        out["active_app"] = active
        version = raw.get("active_version")
        out["active_version"] = version if isinstance(version, str) else None
    else:
        active = None

    apps = raw.get("apps")
    if isinstance(apps, dict):
        for app_id, value in apps.items():
            if not isinstance(app_id, str) or not paths.valid_app_id(app_id):
                continue
            rec = _app_defaults(app_id)
            if isinstance(value, dict):
                rec.update(value)
            rec["id"] = app_id
            if rec.get("desired_state") not in DESIRED_STATES:
                rec["desired_state"] = DESIRED_STOPPED
            if rec.get("observed_state") not in OBSERVED_STATES:
                rec["observed_state"] = "failed"
                rec["reason"] = "invalid persisted observed state"
            try:
                rec["generation"] = max(0, int(rec.get("generation", 0)))
            except (TypeError, ValueError, OverflowError):
                rec["generation"] = 0
            if not isinstance(rec.get("allocations"), list):
                rec["allocations"] = []
            rec["teardown_pending"] = bool(rec.get("teardown_pending", False))
            if not isinstance(rec.get("endpoints"), dict):
                rec["endpoints"] = {}
            if rec.get("observed_state") == "waiting_resource":
                if not isinstance(rec.get("blocked_resource"), str):
                    rec["blocked_resource"] = None
                if not isinstance(rec.get("resource_owners"), list):
                    rec["resource_owners"] = []
                if not isinstance(rec.get("runtime_guard"), dict):
                    rec["runtime_guard"] = None
            else:
                rec["blocked_resource"] = None
                rec["resource_owners"] = []
                rec["runtime_guard"] = None
            # Preserve a terminal dependency diagnostic so the reconciler does
            # not turn authentication/protocol rejection into endless crash
            # retries. An explicit start probes again after an operator repair.
            terminal_dependency = (
                rec.get("observed_state") == "failed"
                and isinstance(rec.get("dependency"), dict)
                and rec["dependency"].get("retryable") is False)
            if (rec.get("observed_state") != "waiting_dependency"
                    and not terminal_dependency):
                rec["dependency"] = None
            history = rec.get("restart_history")
            if not isinstance(history, list):
                history = []
            clean_history = []
            for item in history:
                if not isinstance(item, (int, float)):
                    continue
                try:
                    converted = float(item)
                except (TypeError, ValueError, OverflowError):
                    continue
                if math.isfinite(converted):
                    clean_history.append(converted)
            rec["restart_history"] = clean_history
            out["apps"][app_id] = rec

    # v1 migration.  Merely reading old state must not enable every installed
    # directory; only the previously active application is restored.
    if active and active not in out["apps"]:
        rec = _app_defaults(active, desired=DESIRED_RUNNING)
        rec["version"] = out.get("active_version")
        rec["launch_mode"] = "legacy"
        out["apps"][active] = rec
    return out


def _strict_json_equal(left, right) -> bool:
    """JSON equality that never aliases booleans with numeric 0/1."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return (left.keys() == right.keys()
                and all(_strict_json_equal(left[key], right[key])
                        for key in left))
    if isinstance(left, list):
        return (len(left) == len(right)
                and all(_strict_json_equal(a, b)
                        for a, b in zip(left, right)))
    return left == right


def _finite_json_tree(value) -> bool:
    """Reject Python's non-standard JSON NaN/Infinity at any depth."""
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_finite_json_tree(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _finite_json_tree(item)
                   for key, item in value.items())
    return value is None or type(value) in (bool, int, str)


def _optional_string(value) -> bool:
    return value is None or isinstance(value, str)


def _optional_finite_number(value) -> bool:
    return (value is None or
            (type(value) in (int, float)
             and (not isinstance(value, float) or math.isfinite(value))))


def _durable_counter(value) -> bool:
    return (type(value) is int
            and 0 <= value <= _MAX_DURABLE_COUNTER)


def _finite_history_number(value) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _unique_json_object(pairs) -> dict:
    """object_pairs_hook that makes duplicate keys an invalid migration input."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _persisted_shape_is_safe(raw: dict) -> bool:
    """Whether startup canonicalisation can preserve every durable intent.

    This is deliberately stricter than ``_normalise``, which is also used by
    fail-safe read paths.  Reconciliation may add known defaults and retire
    stale wait diagnostics, but it must never repair an ambiguous structure by
    dropping an app, replacing an unknown desired state, or downgrading a
    future schema.  App-record extension fields are allowed because
    ``_normalise`` retains them verbatim; unknown top-level fields are not.
    """
    if not isinstance(raw, dict) or not _finite_json_tree(raw):
        return False
    if any(key not in _TOP_LEVEL_FIELDS for key in raw):
        return False

    schema_version = raw.get("schema_version")
    if "schema_version" in raw:
        if type(schema_version) is not int or schema_version not in (1, 2):
            return False
    if "revision" in raw and not _durable_counter(raw["revision"]):
        return False

    active_app = raw.get("active_app")
    if active_app is not None and (
            not isinstance(active_app, str)
            or not paths.valid_app_id(active_app)):
        return False
    active_version = raw.get("active_version")
    if not _optional_string(active_version):
        return False
    if active_app is None and active_version is not None:
        return False

    if "apps" not in raw:
        # Only the documented v1 active pointer may synthesize an app record.
        if active_app is not None and schema_version not in (None, 1):
            return False
        return True
    apps = raw["apps"]
    if not isinstance(apps, dict):
        return False
    if active_app is not None and active_app not in apps:
        return False

    for app_id, rec in apps.items():
        if (not isinstance(app_id, str) or not paths.valid_app_id(app_id)
                or not isinstance(rec, dict)):
            return False
        if "id" in rec and rec["id"] != app_id:
            return False
        if ("desired_state" in rec
                and rec["desired_state"] not in DESIRED_STATES):
            return False
        if ("observed_state" in rec
                and rec["observed_state"] not in OBSERVED_STATES):
            return False
        if "generation" in rec and not _durable_counter(rec["generation"]):
            return False
        if ("instance_id" in rec and not _optional_string(rec["instance_id"])
                or "version" in rec and not _optional_string(rec["version"])
                or "reason" in rec and not _optional_string(rec["reason"])):
            return False
        if "launch_mode" in rec and not isinstance(rec["launch_mode"], str):
            return False
        for process_field in ("pid", "pgid"):
            value = rec.get(process_field)
            if process_field in rec and not (
                    value is None or _durable_counter(value)):
                return False
        if ("teardown_pending" in rec
                and type(rec["teardown_pending"]) is not bool):
            return False
        if "allocations" in rec and (
                not isinstance(rec["allocations"], list)
                or not all(isinstance(item, str)
                           for item in rec["allocations"])):
            return False
        if "endpoints" in rec and not isinstance(rec["endpoints"], dict):
            return False
        if ("frame_stream_contract" in rec
                and not isinstance(rec["frame_stream_contract"], dict)):
            return False
        if ("blocked_resource" in rec
                and not _optional_string(rec["blocked_resource"])):
            return False
        if ("resource_owners" in rec
                and (not isinstance(rec["resource_owners"], list)
                     or not all(isinstance(item, str)
                                for item in rec["resource_owners"]))):
            return False
        if ("dependency" in rec
                and rec["dependency"] is not None
                and not isinstance(rec["dependency"], dict)):
            return False
        if ("runtime_guard" in rec
                and rec["runtime_guard"] is not None
                and not isinstance(rec["runtime_guard"], dict)):
            return False
        history = rec.get("restart_history")
        if "restart_history" in rec and (
                not isinstance(history, list)
                or not all(_finite_history_number(item) for item in history)):
            return False
        for timestamp_field in (
                "next_retry_at", "started_at", "updated_at", "exited_at"):
            if (timestamp_field in rec
                    and not _optional_finite_number(rec[timestamp_field])):
                return False
        if ("last_exit" in rec and rec["last_exit"] is not None
                and not isinstance(rec["last_exit"], dict)):
            return False
    return True


def load() -> dict:
    with _LOCK:
        try:
            with open(paths.STATE_FILE) as f:
                return _normalise(json.load(f))
        except (FileNotFoundError, ValueError, OSError,
                OverflowError, RecursionError):
            return _empty()


def _write_normalised(data: dict) -> None:
    """Atomically publish an already-normalised document and its directory.

    The temporary file is fsynced before rename.  Fsyncing the containing
    directory afterwards makes the rename durable across an abrupt power loss,
    rather than merely atomic to concurrent readers.
    """
    paths.ensure_dirs()
    directory = os.path.dirname(paths.STATE_FILE)
    fd, tmp = tempfile.mkstemp(prefix=".state.", dir=directory)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, paths.STATE_FILE)
        tmp = None
        directory_fd = os.open(
            directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


def save(data: dict) -> None:
    """Atomically replace state with a normalised v2 document."""
    with _LOCK:
        _write_normalised(_normalise(data))


def reconcile_persisted() -> dict:
    """Persist a valid legacy/non-canonical document exactly once at startup.

    ``load()`` intentionally never writes.  The daemon calls this function only
    while holding its single-instance lock and the cross-process busy gate, so
    the read/compare/replace transaction cannot overwrite a CLI mutation.  The
    process-local lock keeps lifecycle/reaper threads out of the same window.

    A real canonicalisation advances ``revision`` once because it is a durable
    state mutation.  A canonical document is not rewritten and retains both its
    revision and inode/mtime.  Missing, unreadable, malformed, or non-object
    JSON is left byte-for-byte untouched: replacing corrupt state with an empty
    document could silently discard a desired-running application.
    """
    with _LOCK:
        try:
            with open(paths.STATE_FILE) as f:
                raw = json.load(f, object_pairs_hook=_unique_json_object)
        except FileNotFoundError:
            return {"status": "missing", "changed": False,
                    "revision": None}
        except (ValueError, OSError, OverflowError, RecursionError):
            return {"status": "invalid", "changed": False,
                    "revision": None}
        try:
            safe_shape = _persisted_shape_is_safe(raw)
        except (TypeError, ValueError, OverflowError, RecursionError):
            safe_shape = False
        if not safe_shape:
            return {"status": "invalid", "changed": False,
                    "revision": None}

        normalised = _normalise(raw)
        old_revision = int(normalised.get("revision", 0))
        if _strict_json_equal(raw, normalised):
            return {"status": "unchanged", "changed": False,
                    "revision": old_revision}

        normalised["revision"] = old_revision + 1
        _write_normalised(normalised)
        return {"status": "reconciled", "changed": True,
                "previous_revision": old_revision,
                "revision": normalised["revision"]}


def mutate(fn: Callable[[dict], None]) -> dict:
    """Apply one process-local atomic mutation and return the saved snapshot."""
    with _LOCK:
        data = load()
        fn(data)
        data["schema_version"] = SCHEMA_VERSION
        data["revision"] = int(data.get("revision", 0)) + 1
        save(data)
        return data


def get_active() -> Optional[str]:
    return load().get("active_app")


def set_active(app_id: Optional[str], version: Optional[str] = None) -> None:
    """Update the legacy exclusive pointer without discarding v2 app state."""
    if app_id is not None and not paths.valid_app_id(app_id):
        raise ValueError("invalid app id %r" % app_id)

    def apply(data: dict) -> None:
        previous = data.get("active_app")
        data["active_app"] = app_id
        data["active_version"] = version
        apps = data.setdefault("apps", {})
        if previous and previous != app_id and previous in apps:
            old = apps[previous]
            if old.get("launch_mode") == "legacy":
                old["desired_state"] = DESIRED_STOPPED
                old["updated_at"] = _now()
        if app_id:
            rec = apps.setdefault(app_id, _app_defaults(app_id))
            rec["desired_state"] = DESIRED_RUNNING
            rec["version"] = version
            rec["launch_mode"] = "legacy"
            rec["updated_at"] = _now()

    mutate(apply)


def clear_active_if(app_id: str) -> None:
    if get_active() == app_id:
        set_active(None, None)


def get_app(app_id: str) -> Optional[dict]:
    rec = load().get("apps", {}).get(app_id)
    return dict(rec) if isinstance(rec, dict) else None


def snapshot_app(app_id: str) -> dict:
    """Return a JSON-serialisable lifecycle snapshot for one install txn."""
    if not paths.valid_app_id(app_id):
        raise ValueError("invalid app id %r" % app_id)
    data = load()
    # A JSON round trip prevents nested allocation/diagnostic values in the
    # returned object from aliasing a caller-owned mutation.
    record = data.get("apps", {}).get(app_id)
    record = (json.loads(json.dumps(record))
              if isinstance(record, dict) else None)
    return {
        "schema_version": 1,
        "app_id": app_id,
        "record": record,
        "active_app": data.get("active_app"),
        "active_version": data.get("active_version"),
    }


def restore_app_snapshot(snapshot: dict) -> dict:
    """Restore one app record and the legacy active pointer atomically.

    The state document revision still advances: rollback is itself a durable
    mutation.  All lifecycle fields, including desired wait intent and restart
    history, otherwise return to the captured semantic value.
    """
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != 1:
        raise ValueError("invalid app lifecycle snapshot")
    app_id = snapshot.get("app_id")
    if not isinstance(app_id, str) or not paths.valid_app_id(app_id):
        raise ValueError("invalid app lifecycle snapshot id")
    record = snapshot.get("record")
    if record is not None:
        candidate = {"schema_version": SCHEMA_VERSION, "revision": 0,
                     "active_app": None, "active_version": None,
                     "apps": {app_id: record}}
        if not _persisted_shape_is_safe(candidate):
            raise ValueError("invalid app lifecycle snapshot record")
        record = _normalise(candidate)["apps"][app_id]
    active_app = snapshot.get("active_app")
    active_version = snapshot.get("active_version")
    if active_app is not None and (
            not isinstance(active_app, str) or not paths.valid_app_id(active_app)):
        raise ValueError("invalid app lifecycle snapshot active app")
    if active_version is not None and not isinstance(active_version, str):
        raise ValueError("invalid app lifecycle snapshot active version")
    if active_app is None and active_version is not None:
        raise ValueError("invalid app lifecycle snapshot active pointer")

    def apply(data: dict) -> None:
        apps = data.setdefault("apps", {})
        if record is None:
            apps.pop(app_id, None)
        else:
            apps[app_id] = json.loads(json.dumps(record))
        data["active_app"] = active_app
        data["active_version"] = active_version

    return mutate(apply)


def reset_for_installed_release(app_id: str, version: Optional[str], *,
                                desired: str, launch_mode: str,
                                previous_observed: Optional[str] = None) -> dict:
    """Fence stale runtime/backoff data after publishing a new release.

    Resource/dependency waits retain their *intent* so the reconciler retries
    the new manifest.  Their old owners, plans, error text and retry clocks are
    cleared because those diagnostics described the replaced release.
    """
    if not paths.valid_app_id(app_id):
        raise ValueError("invalid app id %r" % app_id)
    if desired not in DESIRED_STATES:
        raise ValueError("invalid desired state %r" % desired)
    wait_state = (previous_observed if desired == DESIRED_RUNNING
                  and previous_observed in ("waiting_resource",
                                            "waiting_dependency")
                  else None)

    def apply(data: dict) -> None:
        rec = data.setdefault("apps", {}).setdefault(
            app_id, _app_defaults(app_id))
        rec.update({
            "desired_state": desired,
            "observed_state": wait_state or "stopped",
            "instance_id": None,
            "pid": None,
            "pgid": None,
            "version": version,
            "launch_mode": str(launch_mode),
            "reason": None,
            "teardown_pending": False,
            "allocations": [],
            "endpoints": {},
            "frame_stream_contract": {"id": "", "kind": "none"},
            "blocked_resource": None,
            "resource_owners": [],
            "dependency": None,
            "runtime_guard": None,
            "restart_history": [],
            "next_retry_at": None,
            "started_at": None,
            "last_exit": None,
            "exited_at": None,
            "updated_at": _now(),
        })
        if data.get("active_app") == app_id:
            data["active_version"] = version

    return mutate(apply)["apps"][app_id]


def set_desired(app_id: str, desired: str, *, version: Optional[str] = None,
                launch_mode: Optional[str] = None) -> dict:
    if not paths.valid_app_id(app_id):
        raise ValueError("invalid app id %r" % app_id)
    if desired not in DESIRED_STATES:
        raise ValueError("invalid desired state %r" % desired)

    def apply(data: dict) -> None:
        rec = data.setdefault("apps", {}).setdefault(
            app_id, _app_defaults(app_id))
        rec["desired_state"] = desired
        if version is not None:
            rec["version"] = version
        if launch_mode is not None:
            rec["launch_mode"] = str(launch_mode)
        rec["updated_at"] = _now()

    return mutate(apply)["apps"][app_id]


def begin_start(app_id: str, instance_id: str, *, version: Optional[str] = None,
                launch_mode: str = "managed",
                reset_restart_history: bool = False) -> dict:
    """Create a new generation before any resource is reserved or child spawned."""
    if not instance_id or not isinstance(instance_id, str):
        raise ValueError("instance_id is required")

    def apply(data: dict) -> None:
        rec = data.setdefault("apps", {}).setdefault(
            app_id, _app_defaults(app_id))
        rec["generation"] = int(rec.get("generation", 0)) + 1
        rec.update({
            "desired_state": DESIRED_RUNNING,
            "observed_state": "preparing_env",
            "instance_id": instance_id,
            "pid": None,
            "pgid": None,
            "version": version,
            "launch_mode": launch_mode,
            "reason": None,
            "teardown_pending": False,
            "allocations": [],
            "endpoints": {},
            "frame_stream_contract": {"id": "", "kind": "none"},
            "blocked_resource": None,
            "resource_owners": [],
            "dependency": None,
            "runtime_guard": None,
            "next_retry_at": None,
            "started_at": None,
            "updated_at": _now(),
        })
        if reset_restart_history:
            rec["restart_history"] = []

    return mutate(apply)["apps"][app_id]


def transition(app_id: str, observed: str, **fields) -> dict:
    if observed not in OBSERVED_STATES:
        raise ValueError("invalid observed state %r" % observed)

    def apply(data: dict) -> None:
        rec = data.setdefault("apps", {}).setdefault(
            app_id, _app_defaults(app_id))
        rec["observed_state"] = observed
        for key, value in fields.items():
            rec[key] = value
        rec["updated_at"] = _now()

    return mutate(apply)["apps"][app_id]


def remove_app(app_id: str) -> None:
    def apply(data: dict) -> None:
        data.setdefault("apps", {}).pop(app_id, None)
        if data.get("active_app") == app_id:
            data["active_app"] = None
            data["active_version"] = None

    mutate(apply)


def desired_apps() -> List[str]:
    data = load()
    return sorted(app_id for app_id, rec in data.get("apps", {}).items()
                  if rec.get("desired_state") == DESIRED_RUNNING)


def app_states() -> Dict[str, dict]:
    return {app_id: dict(rec) for app_id, rec in load().get("apps", {}).items()}

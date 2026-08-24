"""Durable desired/observed state for the multi-application manager.

``state.json`` used to contain only ``active_app`` and ``active_version``.
Those keys remain authoritative for the legacy activate/switch API, while the
v2 ``apps`` map records independent desired and observed lifecycle state for
every managed application.  Loading a v1 file is an in-memory migration: its
active application becomes one desired-running legacy application; all other
applications stay opt-in.

All mutations rewrite the complete document through fsync + rename.  appmgr's
busy gate serialises HTTP mutations, and the process-local lock also protects
the gateway/reaper threads that update observations.
"""
from __future__ import annotations

import json
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
    except (TypeError, ValueError):
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
            except (TypeError, ValueError):
                rec["generation"] = 0
            if not isinstance(rec.get("allocations"), list):
                rec["allocations"] = []
            rec["teardown_pending"] = bool(rec.get("teardown_pending", False))
            if not isinstance(rec.get("endpoints"), dict):
                rec["endpoints"] = {}
            history = rec.get("restart_history")
            if not isinstance(history, list):
                history = []
            rec["restart_history"] = [float(item) for item in history
                                      if isinstance(item, (int, float))]
            out["apps"][app_id] = rec

    # v1 migration.  Merely reading old state must not enable every installed
    # directory; only the previously active application is restored.
    if active and active not in out["apps"]:
        rec = _app_defaults(active, desired=DESIRED_RUNNING)
        rec["version"] = out.get("active_version")
        rec["launch_mode"] = "legacy"
        out["apps"][active] = rec
    return out


def load() -> dict:
    with _LOCK:
        try:
            with open(paths.STATE_FILE) as f:
                return _normalise(json.load(f))
        except (FileNotFoundError, ValueError, OSError):
            return _empty()


def save(data: dict) -> None:
    """Atomically replace state with a normalised v2 document."""
    with _LOCK:
        data = _normalise(data)
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
        finally:
            if tmp:
                try:
                    os.remove(tmp)
                except OSError:
                    pass


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
            "blocked_resource": None,
            "resource_owners": [],
            "dependency": None,
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

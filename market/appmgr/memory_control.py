"""Memory protection actions, called ONLY with appmgr's mutation gate held."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import time

from . import memory_guard, paths, state, supervisor


ENABLED_FILE = "/run/recamera-inferenced.enabled"
INIT_SCRIPT = "/oem/usr/etc/init.d/S93inferenced"


def _atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".memory-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(value, output, allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class Controller:
    def __init__(self, monitor, *, clock=time.monotonic, wall_clock=time.time,
                 enabled_file=ENABLED_FILE, init_script=INIT_SCRIPT):
        self.monitor = monitor
        self.clock, self.wall_clock = clock, wall_clock
        self.enabled_file, self.init_script = enabled_file, init_script
        self.last_action = -float("inf")
        self.shared_pressure_identity = None
        # Only the administrator (not package manifests) sets eviction priority.
        # Larger numbers are protected longer; all apps default to the same tier.
        priorities = json.loads(os.environ.get("APPMGR_MEMORY_APP_PRIORITIES", "{}"))
        if not isinstance(priorities, dict) or any(type(v) is not int for v in priorities.values()):
            raise ValueError("APPMGR_MEMORY_APP_PRIORITIES must map app ids to integers")
        self.priorities = priorities

    def _event(self, kind, snap, **details):
        # Only incidents reach flash, never the periodic samples. Keep 20 records
        # and a bounded snapshot (no unbounded logs, tensors or application data).
        path = Path(paths.APPMGR_DIR) / "memory-incidents.json"
        try:
            previous = json.loads(path.read_text())
            if not isinstance(previous, list):
                previous = []
        except (OSError, ValueError):
            previous = []
        event = {"time": self.wall_clock(), "kind": kind, **details, "snapshot": snap}
        _atomic_json(path, (previous[-19:] + [event]))
        print("[appmgr] memory protection: %s %s" % (kind, details), flush=True)

    @staticmethod
    def _scheduled(rec):
        return (rec.get("resource_plan") or {}).get("npu_mode") == "scheduled"

    def _hold(self, coord, app_id, rec, snap, reason):
        protection = {"reason": reason, "time": self.wall_clock(),
                      "available_mb": snap["system"]["available_mb"],
                      "instance_id": rec.get("instance_id"), "generation": rec.get("generation")}
        # Persist intent before stopping. A failure/reboot must not undo a
        # protective stop or create a crash/restart/OOM loop.
        state.transition(app_id, rec["observed_state"],
                         desired_state=state.DESIRED_STOPPED, memory_protection=protection)
        self._event("stop", snap, app_id=app_id, reason=reason)
        coord.stop(app_id)

    def _recover(self, coord, snap, reason):
        if not os.path.isfile(self.enabled_file) or not os.path.isfile(self.init_script):
            return False
        current_pids = memory_guard.inference_pids()
        if reason == "inference_service_lost" and current_pids:
            return False  # A newer healthy launch may have won since sampling.
        if reason != "inference_service_lost":
            identity = snap.get("inference", {}).get("identity") or []
            if len(identity) != 2 or current_pids != [identity[0]]:
                return False
            current = memory_guard.process_identity(identity[0])
            if current is None or current[0] != identity[1]:
                return False
        # Keep the budget across appmgr restarts. A reboot gives a new runtime
        # intent marker but deliberately does not bypass recent failures.
        budget_file = Path(paths.APPMGR_DIR) / "inference-recovery.json"
        now = self.wall_clock()
        try:
            attempts = json.loads(budget_file.read_text())
            if not isinstance(attempts, list) or any(
                    type(t) not in (int, float) or not math.isfinite(t) for t in attempts):
                return False
        except FileNotFoundError:
            attempts = []
        except (OSError, ValueError):
            return False  # Unknown budget must not enable an unlimited loop.
        attempts = [t for t in attempts if t > now - 600]
        delay = (10, 30, 120)[min(len(attempts), 2)]
        if len(attempts) >= 3 or (attempts and now - attempts[-1] < delay):
            return False
        # Record before changing anything; a failed stop or killed worker also
        # consumes an attempt. Stop only users of this shared daemon, CPU apps
        # and the firmware's built-in pipeline are outside this scope.
        _atomic_json(budget_file, attempts + [now])
        self.last_action = self.clock()
        self._event("inference_recovery", snap, reason=reason, attempt=len(attempts) + 1)
        for app_id, rec in state.app_states().items():
            if self._scheduled(rec) and (rec.get("desired_state") == state.DESIRED_RUNNING
                                          or supervisor.is_running(app_id) is not None
                                          or rec.get("teardown_pending")):
                self._hold(coord, app_id, rec, snap, reason)
        # The launcher rechecks operator intent under its own flock. Explicit
        # `stop` cannot be undone by a concurrent watchdog `recover`.
        result = subprocess.run([self.init_script, "recover"], timeout=55,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, check=False)
        self._event("inference_recovery_result", self.monitor.snapshot(),
                    success=result.returncode == 0, output=result.stdout[-2000:])
        self.last_action = self.clock()
        return True

    def tick(self, coord):
        snap = self.monitor.snapshot()
        if snap.get("stale"):
            return False
        now = self.clock()
        cooldown = 5 if snap.get("level") == "critical" else self.monitor.policy.cooldown_seconds
        if now - self.last_action < cooldown:
            return False
        service = snap.get("inference", {})
        if service.get("ambiguous"):
            return False
        if snap.get("service_missing_seconds", 0) >= 10:
            return self._recover(coord, snap, "inference_service_lost")
        if snap.get("level") not in ("low", "critical"):
            return False

        candidates = []
        records = state.app_states()
        for app_id, sample in snap.get("apps", {}).items():
            rec = records.get(app_id) or {}
            ident = sample.get("identity") or []
            if (len(ident) != 4 or ident[2:] != [rec.get("instance_id"), rec.get("generation")]
                    or supervisor.is_running(app_id) != ident[0]):
                continue
            current = memory_guard.process_identity(ident[0])
            if current is None or current[0] != ident[1]:
                continue
            local_growth = sample.get("growing", False)
            shared_growth = service.get("growing", False) and self._scheduled(rec)
            if not (local_growth or shared_growth or snap["level"] == "critical"):
                continue
            reason = ("app_memory_growth" if local_growth else "shared_inference_pressure"
                      if shared_growth else "system_memory_pressure")
            # Confirmed process growth first; within a tier choose the largest
            # process footprint. This is an eviction policy, not leak attribution.
            candidates.append((not local_growth, self.priorities.get(app_id, 100),
                               -(sample.get("pss_mb") or sample.get("rss_mb") or 0),
                               app_id, reason))
        if candidates:
            _, _, _, app_id, reason = min(candidates)
            self.last_action = now
            if reason == "shared_inference_pressure":
                self.shared_pressure_identity = service.get("identity")
            self._hold(coord, app_id, records[app_id], snap, reason)
            self.last_action = self.clock()
            return True
        # Once clients have been stopped, a leaking native allocator may still
        # retain memory. Restart only this service, and only while under pressure.
        service_pressure = (service.get("growing") or (
            service.get("identity") is not None
            and service.get("identity") == self.shared_pressure_identity) or (
                snap.get("level") == "critical" and service.get("rss_mb", 0) >= 128))
        if (service_pressure and not any(
                self._scheduled(rec) and supervisor.is_running(app_id) is not None
                for app_id, rec in records.items())):
            return self._recover(coord, snap, "shared_inference_pressure")
        return False

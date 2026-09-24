"""Bounded /proc memory telemetry and pressure policy (no signals here).

Manifest memory_mb is an admission estimate, NOT a hard resident-memory limit.
Shared inference memory belongs to the service, never to an arbitrary client.
The lifecycle worker executes at most one policy action at a time under its
normal mutation gate; this sampler keeps running during slow install/stop work.
"""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, fields
import copy
import math
import os
from pathlib import Path
import threading
import time

from . import paths, state, supervisor


class MemoryPressureError(RuntimeError):
    code = "memory_pressure"


@dataclass(frozen=True)
class Policy:
    warning_mb: float = 320
    low_mb: float = 256
    critical_mb: float = 128
    recovery_mb: float = 384
    warning_seconds: float = 30
    low_seconds: float = 10
    recovery_seconds: float = 60
    warmup_seconds: float = 120
    growth_window_seconds: float = 300
    growth_mb: float = 64
    cooldown_seconds: float = 20
    rapid_exhaustion_seconds: float = 10
    rapid_drop_mb: float = 64

    @classmethod
    def from_env(cls):
        values = {}
        for field in fields(cls):
            raw = os.environ.get("APPMGR_MEMORY_" + field.name.upper())
            if raw is not None:
                value = float(raw)
                if not math.isfinite(value) or value <= 0:
                    raise ValueError("invalid memory guard setting: " + field.name)
                values[field.name] = value
        policy = cls(**values)
        if not (policy.critical_mb < policy.low_mb < policy.warning_mb < policy.recovery_mb):
            raise ValueError("memory guard thresholds must be critical < low < warning < recovery")
        return policy


def _kb_fields(path):
    result = {}
    for line in Path(path).read_text().splitlines():
        name, _, value = line.partition(":")
        parts = value.split()
        if parts and parts[0].isdigit():
            result[name] = int(parts[0]) / 1024
    return result


def process_identity(pid, proc="/proc"):
    """PID reuse safe start ticks and process group, including zombie detection."""
    try:
        tail = (Path(proc) / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
        if tail[0] in ("Z", "X"):
            return None
        return (int(tail[19]), int(tail[2]), int(tail[1]))
    except (OSError, ValueError, IndexError):
        return None


def inference_pids(proc="/proc"):
    """Match argv tokens, not pidfiles or a command substring."""
    found = []
    for entry in Path(proc).iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
            if (len(argv) > 2 and b"python" in os.path.basename(argv[0])
                    and any(argv[i:i + 2] == [b"-m", b"inferenced"]
                            for i in range(1, len(argv) - 1))
                    and process_identity(int(entry.name), proc)):
                found.append(int(entry.name))
        except OSError:
            pass
    return sorted(found)


class ProcSampler:
    def __init__(self, proc="/proc"):
        self.proc = Path(proc)
        self._pss = {}
        self._service_status = {}
        self._status_at = -math.inf

    def _process(self, pid, ident, now):
        try:
            data = _kb_fields(self.proc / str(pid) / "status")
            # Never substitute zero for an incomplete /proc read.
            rss = data["VmRSS"]
            anon = data["RssAnon"]
            key = (pid, ident[0])
            cached = self._pss.get(key)
            if cached is None or now - cached[0] >= 15:
                try:
                    pss = _kb_fields(self.proc / str(pid) / "smaps_rollup")["Pss"]
                except (OSError, KeyError, ValueError):
                    pss = None
                cached = self._pss[key] = (now, pss)
            if process_identity(pid, self.proc) != ident:
                return None
            return {"rss_mb": rss, "anon_mb": anon, "pss_mb": cached[1]}
        except (OSError, KeyError, ValueError):
            return None

    def __call__(self):
        now = time.monotonic()
        mem = _kb_fields(self.proc / "meminfo")
        # Missing MemAvailable is unknown, not a false emergency.
        system = {"total_mb": mem["MemTotal"], "available_mb": mem["MemAvailable"],
                  "cma_free_mb": mem.get("CmaFree")}
        try:
            vmstat = dict(line.split() for line in (self.proc / "vmstat").read_text().splitlines())
            system["oom_kills"] = int(vmstat["oom_kill"])
        except (OSError, KeyError, ValueError):
            system["oom_kills"] = None
        processes = {}
        for entry in self.proc.iterdir():
            if entry.name.isdigit():
                pid = int(entry.name)
                ident = process_identity(pid, self.proc)
                if ident:
                    processes[pid] = ident
        live_keys = {(pid, ident[0]) for pid, ident in processes.items()}
        self._pss = {k: v for k, v in self._pss.items() if k in live_keys}
        apps = {}
        for app_id, rec in state.app_states().items():
            pid = supervisor.is_running(app_id)
            ident = processes.get(pid)
            if not ident or ident[1] != pid:
                continue
            # AppMgr starts a new session/group for each authenticated app.
            # Include group helpers and descendants which create their own group.
            members = {p for p, value in processes.items() if value[1] == pid}
            while True:
                expanded = members | {p for p, value in processes.items() if value[2] in members}
                if expanded == members:
                    break
                members = expanded
            samples = [self._process(p, processes[p], now) for p in members]
            if any(sample is None for sample in samples) or supervisor.is_running(app_id) != pid:
                continue
            if process_identity(pid, self.proc) != ident:
                continue
            apps[app_id] = {
                "identity": [pid, ident[0], rec.get("instance_id"), rec.get("generation")],
                "pid": pid, "processes": len(members),
                **{key: (sum(sample[key] for sample in samples)
                         if all(sample[key] is not None for sample in samples) else None)
                   for key in ("rss_mb", "anon_mb", "pss_mb")},
            }
        pids = inference_pids(self.proc)
        service = {"pids": pids, "present": bool(pids), "ambiguous": len(pids) > 1}
        if len(pids) == 1:
            pid = pids[0]
            ident = process_identity(pid, self.proc)
            sample = self._process(pid, ident, now) if ident else None
            if sample:
                service.update(sample, pid=pid, identity=[pid, ident[0]])
                if now - self._status_at >= 15:
                    from .resources import probe_inference_service
                    self._service_status = probe_inference_service(paths.INFERENCE_SERVICE_SOCK, timeout=0.5)
                    self._status_at = now
                status = self._service_status.get("status") or {}
                service["models"] = status.get("models", [])
                service["healthy"] = self._service_status.get("available", False)
                service["reserved_mb"] = status.get("memory_reserved_mb")
        else:
            self._service_status = {}
            self._status_at = -math.inf
        return {"system": system, "apps": apps, "inference": service}


class Monitor:
    def __init__(self, sampler=None, policy=None, clock=time.monotonic):
        self.sampler = sampler or ProcSampler()
        self.policy = policy or Policy.from_env()
        self.clock = clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._tracks = {}
        self._available_history = deque(maxlen=20)
        self._since = {}
        self._blocked = False
        self._snapshot = {}

    def _duration(self, key, condition, now):
        if not condition:
            self._since.pop(key, None)
            return 0
        return now - self._since.setdefault(key, now)

    def _track(self, key, sample, now):
        # A model load/unload is a new service baseline, not evidence of a leak.
        identity = (sample.get("identity"), sorted(
            str(model.get("key")) for model in sample.get("models", [])))
        track = self._tracks.get(key)
        if track is None or track["identity"] != identity:
            track = self._tracks[key] = {"identity": identity, "start": now,
                                         "history": deque(maxlen=145)}
        history = track["history"]
        value = sample.get("pss_mb")
        metric = "pss_mb"
        if value is None:
            value, metric = sample.get("anon_mb"), "anon_mb"
        if value is None:
            return
        if history and history[-1]["metric"] != metric:
            history.clear()
            track["start"] = now
        if not history or now - history[-1]["mono"] >= 5:
            history.append({"mono": now, "time": time.time(), "mb": round(value, 2), "metric": metric})
        window = self.policy.growth_window_seconds
        mature = [p for p in history if p["mono"] >= track["start"] + self.policy.warmup_seconds
                  and p["mono"] >= now - window - 10]
        growing, growth = False, 0
        if len(mature) >= 4 and now - mature[0]["mono"] >= window:
            growth = value - mature[0]["mb"]
            # Require increases in all three windows, not just a one-off load.
            anchors = [min(mature, key=lambda p: abs(p["mono"] - (now - window + i * window / 3)))
                       for i in range(4)]
            growing = growth >= self.policy.growth_mb and all(
                anchors[i + 1]["mb"] - anchors[i]["mb"] > self.policy.growth_mb / 10
                for i in range(3))
        sample.update(growing=growing, growth_mb=round(growth, 2), metric=metric,
                      warming_up=now - track["start"] < self.policy.warmup_seconds,
                      history=[{"time": p["time"], "mb": p["mb"]} for p in list(history)[::3]])

    def sample_once(self):
        try:
            raw = self.sampler()
            available = raw["system"]["available_mb"]
            if not math.isfinite(available) or available < 0:
                raise ValueError("invalid MemAvailable")
        except (OSError, KeyError, ValueError):
            with self._lock:
                # Broken continuity must not turn sparse samples into a
                # sustained low-memory decision when /proc becomes readable.
                self._since.clear()
                self._available_history.clear()
            return
        now, p = self.clock(), self.policy
        with self._lock:
            if self._snapshot and now - self._snapshot["sampled_at"] > 15:
                self._since.clear()
                self._tracks.clear()
                self._available_history.clear()
            self._available_history.append((now, available))
            recent = [(ts, mb) for ts, mb in self._available_history if now - 15 <= ts <= now - 3]
            rapid = False
            if recent and available < p.recovery_mb:
                elapsed, drop = now - recent[0][0], recent[0][1] - available
                rapid = drop >= p.rapid_drop_mb and (
                    (available - p.critical_mb) / (drop / elapsed) <= p.rapid_exhaustion_seconds)
            warning = self._duration("warning", available < p.warning_mb, now) >= p.warning_seconds
            low = self._duration("low", available < p.low_mb, now) >= p.low_seconds
            critical = available < p.critical_mb or rapid
            recovered = self._duration("recovery", available > p.recovery_mb, now) >= p.recovery_seconds
            if critical or low or warning:
                self._blocked = True
            elif recovered:
                self._blocked = False
            level = "critical" if critical else "low" if low else "warning" if warning else (
                "recovering" if self._blocked else "normal")
            keys = set()
            for app_id, sample in raw["apps"].items():
                keys.add(app_id)
                self._track(app_id, sample, now)
            if raw["inference"].get("identity"):
                keys.add("@inference")
                self._track("@inference", raw["inference"], now)
            self._tracks = {k: v for k, v in self._tracks.items() if k in keys}
            raw.update(sampled_at=now, timestamp=time.time(), level=level,
                       policy=asdict(p), rapid_decline=rapid,
                       admission_blocked=self._blocked,
                       service_missing_seconds=self._duration(
                           "service_missing", not raw["inference"]["present"], now))
            self._snapshot = raw

    def snapshot(self):
        with self._lock:
            result = copy.deepcopy(self._snapshot)
        result["stale"] = not result or self.clock() - result.get("sampled_at", -math.inf) > 15
        return result

    def _run(self):
        while not self._stop.is_set():
            try:
                self.sample_once()
            except Exception as exc:
                print("[appmgr] memory sampling failed: %s" % exc, flush=True)
            snap = self.snapshot()
            available = snap.get("system", {}).get("available_mb", math.inf)
            self._stop.wait(1 if available < self.policy.recovery_mb else 5)

    def start(self):
        self.sample_once()
        self._thread = threading.Thread(target=self._run, name="appmgr-memory-monitor", daemon=True)
        self._thread.start()
        return self

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)


active_monitor = None


def snapshot():
    return active_monitor.snapshot() if active_monitor else {"stale": True, "level": "unknown"}


def check_admission():
    snap = snapshot()
    if snap.get("admission_blocked"):
        raise MemoryPressureError("system memory pressure; wait for memory recovery before starting or installing apps")


def app_view(app_id, rec):
    snap = snapshot()
    memory = snap.get("apps", {}).get(app_id)
    # Never attach the old generation's measurement to a newly started app.
    if memory and memory["identity"][2:] != [rec.get("instance_id"), rec.get("generation")]:
        memory = None
    service = snap.get("inference", {})
    return {"process": memory, "inference": service,
            "system": snap.get("system"), "level": snap.get("level"),
            "stale": snap.get("stale", True), "timestamp": snap.get("timestamp"),
            "protection": rec.get("memory_protection")}

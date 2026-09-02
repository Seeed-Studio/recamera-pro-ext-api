"""Small durable admission controller for appmgr-managed applications.

The native endpoints remain the final authority.  This module prevents known
conflicts before spawning a child and records which *application instance*
owns each soft reservation.  It intentionally models scheduled NPU inference
as a shared service resource; the legacy direct broker lease is a separate,
exclusive compatibility resource and is never presented as multi-owner NPU.
"""
from __future__ import annotations

import glob
import json
import math
import os
import socket
import struct
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Dict, Iterable, List, Optional

from . import paths


DEFAULT_START_MAX_TEMP_C = 100.0
DEFAULT_RUNTIME_HARD_TEMP_C = 110.0
DEFAULT_THERMAL_RESTART_GAP_C = 10.0


class ResourceError(RuntimeError):
    pass


class ResourceBusy(ResourceError):
    def __init__(self, resource: str, owners: Iterable[str], *, detail: str = ""):
        self.resource = resource
        self.owners = sorted(set(str(x) for x in owners if x))
        owner_text = ", ".join(self.owners) or "unknown"
        message = "resource %s is busy (owners: %s)" % (resource, owner_text)
        if detail:
            message += ": " + str(detail)
        super().__init__(message)


class DependencyUnavailable(ResourceError):
    pass


def _send_control(sock: socket.socket, header: dict) -> None:
    message = dict(header)
    message["protocol"] = 1
    message["tensors"] = []
    raw = json.dumps(message, sort_keys=True, separators=(",", ":")).encode()
    sock.sendall(struct.pack("!I", len(raw)) + raw)


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = []
    while size:
        chunk = sock.recv(size)
        if not chunk:
            raise ConnectionError("inferenced closed during response")
        chunks.append(chunk)
        size -= len(chunk)
    return b"".join(chunks)


def _recv_control(sock: socket.socket) -> dict:
    size = struct.unpack("!I", _recv_exact(sock, 4))[0]
    if size < 1 or size > 64 * 1024:
        raise ValueError("invalid inferenced response header size")
    response = json.loads(_recv_exact(sock, size).decode("utf-8"))
    if not isinstance(response, dict):
        raise ValueError("inferenced response is not an object")
    tensors = response.get("tensors") or []
    if tensors:
        raise ValueError("unexpected tensors in inferenced status response")
    return response


def probe_inference_service(path: str, timeout: float = 0.5) -> dict:
    """Perform inferenced hello then status; an inode alone is not health."""
    started = time.monotonic()
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        conn.settimeout(max(0.05, float(timeout)))
        conn.connect(path)
        request_id = uuid.uuid4().hex
        _send_control(conn, {
            "op": "hello", "request_id": request_id,
            "app_id": "appmgr", "instance_id": "health-%d" % os.getpid(),
            "generation": 0, "control_only": True,
        })
        hello = _recv_control(conn)
        if hello.get("ok") is not True or hello.get("op") != "hello":
            raise ConnectionError("inferenced hello rejected")
        _send_control(conn, {"op": "status", "request_id": request_id})
        status = _recv_control(conn)
        if status.get("ok") is not True or status.get("op") != "status":
            raise ConnectionError("inferenced status rejected")
        return {
            "available": True,
            "socket": path,
            "latency_ms": round((time.monotonic() - started) * 1000.0, 3),
            "capabilities": hello.get("capabilities") or {},
            "status": {k: v for k, v in status.items()
                       if k not in ("protocol", "tensors", "request_id", "ok", "op")},
        }
    except Exception as exc:
        return {"available": False, "socket": path, "error": str(exc)}
    finally:
        conn.close()


@dataclass(frozen=True)
class Request:
    resource: str
    mode: str = "shared"       # shared | exclusive
    amount: int = 1
    capacity: int = 0          # 0 = no numeric cap (exclusive still conflicts)


@dataclass(frozen=True)
class Plan:
    requests: tuple
    npu_mode: str = "none"     # none | scheduled | legacy-direct
    result_gateway: bool = True
    inference_service: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "npu_mode": self.npu_mode,
            "result_gateway": self.result_gateway,
            "inference_service": self.inference_service,
            "requests": [asdict(x) for x in self.requests],
        }


def _int(value, default: int = 1, minimum: int = 1) -> int:
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return default


def _mode(value, default: str = "shared") -> str:
    if isinstance(value, dict):
        value = value.get("mode", default)
    return str(value or default).strip().lower().replace("_", "-")


def _env_capacity(name: str, default: int, *, allow_zero: bool = False) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(0 if allow_zero else 1, value)


def _finite_env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def _thermal_policy() -> tuple[float, float]:
    """Return one coherent start/runtime policy from operator overrides.

    Invalid or non-finite values fall back individually. If an override places
    the start gate at/above the runtime stop fence, retain the (safer) hard
    fence and lower the start gate instead of raising the stop temperature.
    """
    start_max_temp = _finite_env_float(
        "APPMGR_START_MAX_TEMP_C", DEFAULT_START_MAX_TEMP_C)
    runtime_hard_temp = _finite_env_float(
        "APPMGR_RUNTIME_HARD_TEMP_C", DEFAULT_RUNTIME_HARD_TEMP_C)
    if start_max_temp >= runtime_hard_temp:
        start_max_temp = min(
            DEFAULT_START_MAX_TEMP_C,
            runtime_hard_temp - DEFAULT_THERMAL_RESTART_GAP_C,
        )
    return start_max_temp, runtime_hard_temp


def _append_limit_requests(requests: List[Request], resource_spec: dict) -> None:
    """Turn declared app budgets into start-time soft reservations.

    These reservations deliberately live beside camera/NPU claims: installing a
    package never owns them, while ``ResourceManager.reserve`` atomically checks
    and records them for one exact running generation.  Memory is uncapped by
    declared-budget sum unless an operator explicitly configures an aggregate
    cap: the live start probe already observes usage by running apps, so summing
    their manifest maxima as well would reject workloads the device can still
    carry.  ``memory_mb`` remains the prospective new generation's envelope
    when the live MemAvailable/headroom check runs. Storage keeps a reservation
    cap because current free space cannot account for data a running app may
    still write up to its declared envelope.

    CPU is exposed in the journal for diagnostics but is not numerically capped
    by default.  A manifest maximum is not a reliable prediction of steady CPU
    use, so deployments that have calibrated their application set may opt into
    a platform cap with ``APPMGR_MANAGED_CPU_CAP_PERCENT``.
    """
    limits = (resource_spec.get("limits")
              if isinstance(resource_spec, dict) else None)
    if not isinstance(limits, dict):
        return
    memory_mb = limits.get("memory_mb")
    if (isinstance(memory_mb, int) and not isinstance(memory_mb, bool)
            and memory_mb > 0):
        requests.append(Request(
            "memory.managed-mb", "shared", memory_mb,
            _env_capacity("APPMGR_MANAGED_MEMORY_CAP_MB", 0,
                          allow_zero=True),
        ))
    storage_mb = limits.get("storage_mb")
    if (isinstance(storage_mb, int) and not isinstance(storage_mb, bool)
            and storage_mb > 0):
        requests.append(Request(
            "storage.appdata-mb", "shared", storage_mb,
            _env_capacity("APPMGR_MANAGED_STORAGE_CAP_MB", 8192,
                          allow_zero=True),
        ))
    cpu_percent = limits.get("cpu_percent")
    if isinstance(cpu_percent, int) and not isinstance(cpu_percent, bool) \
            and cpu_percent > 0:
        requests.append(Request(
            "cpu.managed-percent", "shared", cpu_percent,
            _env_capacity("APPMGR_MANAGED_CPU_CAP_PERCENT", 0, allow_zero=True),
        ))


def _existing_path(path: str) -> str:
    current = os.path.abspath(path)
    while not os.path.exists(current):
        parent = os.path.dirname(current)
        if parent == current:
            return "/"
        current = parent
    return current


def runtime_capacity_probe() -> dict:
    """Best-effort live inputs for start admission; absence is not fabricated.

    The resource journal supplies deterministic aggregate reservations.  These
    readings add the other half of safe admission: memory/storage already used
    by the platform and current thermal state.  Tests can inject a deterministic
    probe through ``ResourceManager(runtime_probe=...)``.
    """
    mem_available_mb = None
    try:
        with open("/proc/meminfo") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    mem_available_mb = int(line.split()[1]) // 1024
                    break
    except (OSError, ValueError, IndexError):
        pass

    storage_free_mb = None
    try:
        stat = os.statvfs(_existing_path(paths.APPDATA_DIR))
        storage_free_mb = int(stat.f_bavail * stat.f_frsize // (1024 * 1024))
    except (OSError, ValueError, AttributeError):
        pass

    temperatures = []
    for filename in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
        try:
            with open(filename) as handle:
                value = float(handle.readline().strip()) / 1000.0
            if -50.0 <= value <= 250.0:
                temperatures.append(value)
        except (OSError, TypeError, ValueError):
            continue
    return {
        "mem_available_mb": mem_available_mb,
        "storage_free_mb": storage_free_mb,
        "temperature_c": max(temperatures) if temperatures else None,
    }


def _manifest_defaults(manifest: dict) -> dict:
    values = {}
    schema = manifest.get("config_schema") or {}
    if isinstance(schema, dict):
        groups = schema.get("groups") or []
        if isinstance(groups, list):
            for group in groups:
                for item in ((group or {}).get("items") or []):
                    if isinstance(item, dict) and "key" in item and "default" in item:
                        values[item["key"]] = item["default"]
    return values


def _effective_resources(manifest: dict, config: Optional[dict] = None) -> dict:
    base = manifest.get("resources")
    result = dict(base) if isinstance(base, dict) else {}

    # Canonical manifest-v2 shape: resources.claims, optionally selected from
    # resources.profiles[].when using the effective app configuration.
    if "claims" in result or "profiles" in result:
        claims = result.get("claims")
        profiles = result.get("profiles") or []
        values = _manifest_defaults(manifest)
        if isinstance(config, dict):
            values.update(config)
        matched = []
        if isinstance(profiles, list):
            for profile in profiles:
                if not isinstance(profile, dict):
                    continue
                when = profile.get("when")
                if (isinstance(when, dict) and len(when) == 1
                        and all(values.get(key) == value
                                for key, value in when.items())):
                    matched.append(profile.get("claims") or [])
        if len(matched) > 1:
            raise ResourceError("multiple resource profiles match effective config")
        if matched:
            claims = matched[0]
        if claims is None:
            raise ResourceError("no resource profile matches effective config")
        return {"_claims": claims, "limits": result.get("limits") or {}}

    # Transitional draft shape retained for already-built internal packages.
    selected = manifest.get("resource_profile")
    profiles = manifest.get("resource_profiles")
    if selected and isinstance(profiles, dict):
        profile = profiles.get(selected)
        if isinstance(profile, dict):
            # A profile is an overlay: common camera/output limits can stay in
            # resources while config selects rk vs cpu NPU behaviour.
            result.update(profile)
    return result


def plan_manifest(manifest: dict, config: Optional[dict] = None) -> Plan:
    """Translate validated manifest resources into scheduler requests.

    The forthcoming manifest validator owns schema errors.  appmgr remains
    conservative when handed an old manifest: model-backed v1 apps use the
    exclusive ``npu.direct`` compatibility lane; model-less apps are allowed to
    run as CPU applications.  Conditional NPU use such as voice-transcribe must
    move to a v2 resource profile -- it cannot be inferred safely from models[].
    """
    manifest = manifest if isinstance(manifest, dict) else {}
    resources = _effective_resources(manifest, config)
    try:
        max_instances = int((manifest.get("instances") or {}).get("max", 1))
    except (TypeError, ValueError):
        max_instances = 1
    if max_instances != 1:
        raise ResourceError(
            "appmgr currently supports instances.max=1, got %s" % max_instances)
    requests: List[Request] = [
        Request("app.instance:%s" % manifest.get("id", "unknown"),
                mode="exclusive", capacity=1),
    ]

    claims = resources.get("_claims")
    if claims is not None:
        if not isinstance(claims, list):
            raise ResourceError("resources.claims must be an array")
        use_gateway = False
        npu_mode = "none"
        inference_service = None
        for claim in claims:
            if not isinstance(claim, dict):
                continue
            name = str(claim.get("name", ""))
            mode = _mode(claim.get("mode"), "shared")
            amount = _int(claim.get("quantity", 1))
            if name == "npu.rknn":
                if mode in ("scheduled", "brokered"):
                    npu_mode = "scheduled"
                    inference_service = paths.INFERENCE_SERVICE_SOCK
                    requests.append(Request("npu.scheduler", "shared", amount, 0))
                elif mode == "exclusive":
                    npu_mode = "legacy-direct"
                    requests.append(Request("npu.direct", "exclusive", 1, 1))
                else:
                    raise ResourceError(
                        "npu.rknn must use scheduled service or exclusive legacy mode")
            elif name == "camera.frames":
                # frame_export advertises and enforces FE_MAX_CONNS=4. Keep the
                # default scheduler ceiling aligned with that real endpoint
                # capacity instead of reserving an undocumented spare slot.
                capacity = _int(os.environ.get("APPMGR_FRAME_MANAGED_CAP", "4"), 4)
                requests.append(Request("camera.frame:camera-0",
                                        "exclusive" if mode == "exclusive" else "shared",
                                        amount, capacity))
            elif name == "audio.capture":
                capacity = _int(os.environ.get("APPMGR_AUDIO_SHARED_CAP", "4"), 4)
                requests.append(Request("audio.capture:default",
                                        "exclusive" if mode == "exclusive" else "shared",
                                        amount, capacity))
            elif name == "result.publish":
                if mode == "brokered":
                    use_gateway = True
                    requests.append(Request("result.gateway", "shared", amount, 0))
                else:
                    capacity = _int(os.environ.get(
                        "APPMGR_RESULT_INGRESS_CAP", "4"), 4)
                    requests.append(Request("result.ingress",
                                            "exclusive" if mode == "exclusive" else "shared",
                                            amount, capacity))
            elif name == "rga":
                requests.append(Request("rga.bandwidth", "shared", amount, 0))
            elif name == "codec.decode":
                capacity = _int(os.environ.get("APPMGR_CODEC_DECODE_CAP", "4"), 4)
                requests.append(Request("codec.decode", "shared", amount, capacity))
            elif name == "probe.read":
                capacity = _int(os.environ.get("APPMGR_PROBE_CAP", "4"), 4)
                requests.append(Request("probe.read", "shared", amount, capacity))
        _append_limit_requests(requests, resources)
        # result.publish is explicit in v2.  Do not invent an output channel the
        # permission/validator did not grant.
        return Plan(tuple(requests), npu_mode=npu_mode,
                    result_gateway=use_gateway,
                    inference_service=inference_service)

    explicit_npu = "npu" in resources
    npu = resources.get("npu")
    npu_mode = _mode(npu, "none") if explicit_npu else "none"
    if not explicit_npu and manifest.get("models"):
        npu_mode = "legacy-direct"

    inference_service = None
    if npu_mode in ("scheduled", "service", "shared-service", "scheduler"):
        npu_mode = "scheduled"
        requests.append(Request("npu.scheduler", mode="shared", capacity=0))
        inference_service = paths.INFERENCE_SERVICE_SOCK
    elif npu_mode in ("direct", "exclusive", "legacy", "legacy-direct"):
        npu_mode = "legacy-direct"
        requests.append(Request("npu.direct", mode="exclusive", capacity=1))
    elif npu_mode in ("none", "off", "false", "cpu"):
        npu_mode = "none"
    else:
        raise ResourceError("unsupported npu resource mode %r" % npu_mode)

    camera = resources.get("camera")
    if camera and _mode(camera) not in ("none", "off", "false"):
        spec = camera if isinstance(camera, dict) else {}
        stream = str(spec.get("stream", "camera-0"))
        amount = _int(spec.get("subscribers", 1))
        capacity = _int(os.environ.get("APPMGR_FRAME_MANAGED_CAP", "4"), 4)
        requests.append(Request("camera.frame:%s" % stream, "shared",
                                amount, capacity))

    audio = resources.get("audio")
    if audio and _mode(audio) not in ("none", "off", "false"):
        spec = audio if isinstance(audio, dict) else {}
        mode = _mode(audio)
        if mode in ("exclusive", "exclusive-takeover", "takeover", "direct"):
            requests.append(Request("audio.capture:default", "exclusive", 1, 1))
        else:
            capacity = _int(os.environ.get("APPMGR_AUDIO_SHARED_CAP", "4"), 4)
            requests.append(Request("audio.capture:default", "shared", 1, capacity))

    result = resources.get("result")
    result_mode = _mode(result, "hub") if result is not None else "hub"
    use_gateway = result_mode not in ("none", "off", "stdout", "osd")
    if use_gateway:
        requests.append(Request("result.gateway", "shared", 1, 0))
    if result_mode in ("official", "ingress", "osd"):
        cap = _int(os.environ.get("APPMGR_RESULT_INGRESS_CAP", "4"), 4)
        requests.append(Request("result.ingress", "shared", 1, cap))

    osd = resources.get("osd")
    osd_mode = _mode(osd, "none") if osd is not None else "none"
    if osd_mode in ("canvas-exclusive", "exclusive", "canvas"):
        requests.append(Request("osd.canvas", "exclusive", 1, 1))
    elif osd_mode in ("detection-union", "detection", "shared"):
        requests.append(Request("osd.detection-sources", "shared", 1, 8))

    ports = resources.get("ports") or []
    if isinstance(ports, dict):
        ports = [ports]
    if isinstance(ports, list):
        for spec in ports:
            if not isinstance(spec, dict):
                continue
            port = spec.get("port")
            if port in (None, "auto", 0, "0"):
                continue
            try:
                port = int(port)
            except (TypeError, ValueError):
                continue
            proto = str(spec.get("protocol", "tcp")).lower()
            bind = str(spec.get("bind", "loopback")).lower()
            requests.append(Request("port:%s:%s:%d" % (proto, bind, port),
                                    "exclusive", 1, 1))

    _append_limit_requests(requests, resources)
    return Plan(tuple(requests), npu_mode=npu_mode,
                result_gateway=use_gateway,
                inference_service=inference_service)


class ResourceManager:
    """Persist reservations and enforce share/exclusive/capacity conflicts."""

    def __init__(self, state_path: Optional[str] = None, *, runtime_probe=None):
        self._state_path = state_path
        self._lock = threading.RLock()
        self._runtime_probe = runtime_probe or runtime_capacity_probe

    @property
    def state_path(self) -> str:
        return self._state_path or paths.resource_state_file()

    @staticmethod
    def _empty() -> dict:
        return {"schema_version": 1, "revision": 0, "allocations": []}

    def _load(self) -> dict:
        try:
            with open(self.state_path) as f:
                data = json.load(f)
            if not isinstance(data, dict) or not isinstance(
                    data.get("allocations"), list):
                raise ValueError("invalid resource journal")
            data.setdefault("schema_version", 1)
            data.setdefault("revision", 0)
            return data
        except (FileNotFoundError, OSError, ValueError):
            return self._empty()

    def _save(self, data: dict) -> None:
        directory = os.path.dirname(self.state_path)
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".resources.", dir=directory)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.state_path)
            tmp = None
        finally:
            if tmp:
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    @staticmethod
    def _check(request: Request, allocations: List[dict]) -> None:
        # inferenced multiplexes scheduled clients behind one service owner;
        # that whole lane is incompatible with a legacy application taking the
        # broker directly, even though their diagnostic resource names differ.
        npu_group = request.resource in ("npu.scheduler", "npu.direct")
        current = [a for a in allocations
                   if (a.get("resource") == request.resource
                       or (npu_group and a.get("resource") in
                           ("npu.scheduler", "npu.direct")))
                   and a.get("state") in ("reserved", "bound")]
        # A capacity is a ceiling for the aggregate *including the first
        # claimant*.  Returning early for an empty journal used to let one app
        # reserve (for example) 2048 MiB against a 1024 MiB platform cap.
        if request.capacity and request.amount > request.capacity:
            raise ResourceBusy(
                request.resource, [],
                detail="request %d exceeds capacity %d" %
                       (request.amount, request.capacity),
            )
        if not current:
            return
        owners = [a.get("app_id") for a in current]
        if (request.mode == "exclusive" or any(
                a.get("mode") == "exclusive" for a in current)):
            raise ResourceBusy(request.resource, owners)
        used = sum(_int(a.get("amount", 1)) for a in current)
        if request.capacity and used + request.amount > request.capacity:
            raise ResourceBusy(
                request.resource, owners,
                detail=("shared capacity %d would be exceeded "
                        "(%d reserved + %d requested)") %
                       (request.capacity, used, request.amount),
            )

    def _check_runtime(self, plan: Plan, allocations: List[dict]) -> None:
        """Fail closed at *start*, never while inspecting/installing a package."""
        try:
            sample = self._runtime_probe() or {}
        except Exception as exc:
            raise ResourceBusy(
                "runtime.telemetry", [],
                detail="capacity probe failed: %s" % exc,
            ) from exc
        if not isinstance(sample, dict):
            raise ResourceBusy("runtime.telemetry", [],
                               detail="capacity probe returned invalid data")

        temperature = sample.get("temperature_c")
        try:
            temperature = float(temperature) if temperature is not None else None
        except (TypeError, ValueError):
            temperature = None
        max_start_temp, _ = _thermal_policy()
        if temperature is not None and temperature >= max_start_temp:
            thermal_owners = sorted({
                str(item.get("app_id")) for item in allocations
                if item.get("app_id") and item.get("state") in ("reserved", "bound")
            })
            raise ResourceBusy(
                "thermal.start", thermal_owners,
                detail="%.1fC is at/above %.1fC start threshold" %
                       (temperature, max_start_temp),
            )

        requested_memory = sum(
            item.amount for item in plan.requests
            if item.resource == "memory.managed-mb"
        )
        available_memory = sample.get("mem_available_mb")
        try:
            available_memory = (int(available_memory)
                                if available_memory is not None else None)
        except (TypeError, ValueError, OverflowError):
            available_memory = None
        memory_owners = sorted({
            str(item.get("app_id")) for item in allocations
            if item.get("resource") == "memory.managed-mb"
            and item.get("app_id")
            and item.get("state") in ("reserved", "bound")
        })
        if requested_memory and (available_memory is None
                                 or available_memory < 0):
            raise ResourceBusy(
                "memory.telemetry", memory_owners,
                detail="MemAvailable is unavailable; start admission cannot "
                       "prove sufficient live memory",
            )
        memory_headroom = _env_capacity(
            "APPMGR_SYSTEM_MEMORY_HEADROOM_MB", 256, allow_zero=True)
        if (requested_memory
                and available_memory < requested_memory + memory_headroom):
            raise ResourceBusy(
                "memory.available-mb", memory_owners,
                detail=("needs %d MiB plus %d MiB system headroom; "
                        "only %d MiB available") %
                       (requested_memory, memory_headroom, available_memory),
            )

        requested_storage = sum(
            item.amount for item in plan.requests
            if item.resource == "storage.appdata-mb"
        )
        available_storage = sample.get("storage_free_mb")
        try:
            available_storage = (int(available_storage)
                                 if available_storage is not None else None)
        except (TypeError, ValueError, OverflowError):
            available_storage = None
        storage_owners = sorted({
            str(item.get("app_id")) for item in allocations
            if item.get("resource") == "storage.appdata-mb"
            and item.get("app_id")
            and item.get("state") in ("reserved", "bound")
        })
        if requested_storage and (available_storage is None
                                  or available_storage < 0):
            raise ResourceBusy(
                "storage.telemetry", storage_owners,
                detail="app-data free space is unavailable; start admission "
                       "cannot prove sufficient live storage",
            )
        storage_headroom = _env_capacity(
            "APPMGR_STORAGE_HEADROOM_MB", 128, allow_zero=True)
        if (requested_storage
                and available_storage < requested_storage + storage_headroom):
            raise ResourceBusy(
                "storage.available-mb", storage_owners,
                detail=("needs %d MiB plus %d MiB system headroom; "
                        "only %d MiB available") %
                       (requested_storage, storage_headroom, available_storage),
            )

    def reserve(self, app_id: str, instance_id: str, generation: int,
                plan: Plan) -> List[dict]:
        with self._lock:
            data = self._load()
            allocations = data["allocations"]
            existing = [a for a in allocations
                        if a.get("instance_id") == instance_id
                        and int(a.get("generation", -1)) == int(generation)
                        and a.get("state") in ("reserved", "bound")]
            if existing:
                return [dict(a) for a in existing]
            for request in plan.requests:
                self._check(request, allocations)
            self._check_runtime(plan, allocations)
            now = time.time()
            made = []
            for request in plan.requests:
                rec = {
                    "allocation_id": uuid.uuid4().hex,
                    "resource": request.resource,
                    "mode": request.mode,
                    "amount": request.amount,
                    "capacity": request.capacity,
                    "app_id": app_id,
                    "instance_id": instance_id,
                    "generation": int(generation),
                    "state": "reserved",
                    "created_at": now,
                }
                allocations.append(rec)
                made.append(rec)
            data["revision"] = int(data.get("revision", 0)) + 1
            self._save(data)
            return [dict(a) for a in made]

    def bind(self, instance_id: str, generation: int) -> List[dict]:
        with self._lock:
            data = self._load()
            changed = []
            for rec in data["allocations"]:
                if (rec.get("instance_id") == instance_id
                        and int(rec.get("generation", -1)) == int(generation)
                        and rec.get("state") == "reserved"):
                    rec["state"] = "bound"
                    rec["bound_at"] = time.time()
                    changed.append(dict(rec))
            if changed:
                data["revision"] = int(data.get("revision", 0)) + 1
                self._save(data)
            return changed

    def release(self, instance_id: str, generation: int) -> List[dict]:
        """Release only the exact generation; stale exits cannot free a new run."""
        with self._lock:
            data = self._load()
            kept, released = [], []
            for rec in data["allocations"]:
                if (rec.get("instance_id") == instance_id
                        and int(rec.get("generation", -1)) == int(generation)):
                    out = dict(rec)
                    out["state"] = "released"
                    out["released_at"] = time.time()
                    released.append(out)
                else:
                    kept.append(rec)
            if released:
                data["allocations"] = kept
                data["revision"] = int(data.get("revision", 0)) + 1
                self._save(data)
            return released

    def allocations_for(self, instance_id: str) -> List[dict]:
        with self._lock:
            return [dict(a) for a in self._load()["allocations"]
                    if a.get("instance_id") == instance_id]

    def runtime_status(self) -> dict:
        """Expose the same admission inputs/policy used by ``reserve``.

        This is intentionally diagnostic only: callers must never treat a GET
        response as an admission lease.  ``reserve`` samples again while holding
        the resource journal lock immediately before it creates allocations.
        """
        try:
            sample = dict(self._runtime_probe() or {})
            error = None
        except Exception as exc:
            sample = {}
            error = str(exc)
        max_start_temp, runtime_hard_temp = _thermal_policy()
        return {
            "sample": sample,
            "error": error,
            "policy": {
                "start_max_temp_c": max_start_temp,
                "runtime_hard_temp_c": runtime_hard_temp,
                "system_memory_headroom_mb": _env_capacity(
                    "APPMGR_SYSTEM_MEMORY_HEADROOM_MB", 256, allow_zero=True),
                "storage_headroom_mb": _env_capacity(
                    "APPMGR_STORAGE_HEADROOM_MB", 128, allow_zero=True),
                "managed_memory_cap_mb": _env_capacity(
                    "APPMGR_MANAGED_MEMORY_CAP_MB", 0, allow_zero=True),
                "managed_storage_cap_mb": _env_capacity(
                    "APPMGR_MANAGED_STORAGE_CAP_MB", 8192, allow_zero=True),
                "managed_cpu_cap_percent": _env_capacity(
                    "APPMGR_MANAGED_CPU_CAP_PERCENT", 0, allow_zero=True),
            },
        }

    def runtime_guard(self) -> Optional[dict]:
        """Return a hard runtime safety violation, if one is observable.

        Start admission uses a lower threshold and naturally retries through
        ``waiting_resource``.  This second, higher threshold is a containment
        fence for a workload that heats up after a successful launch.  It is
        intentionally limited to thermal safety: choosing which process to
        evict for ordinary memory pressure requires an explicit priority/QoS
        policy and must not be guessed from manifest maxima.
        """
        try:
            sample = self._runtime_probe() or {}
        except Exception:
            return None
        if not isinstance(sample, dict):
            return None
        try:
            temperature = float(sample.get("temperature_c"))
        except (TypeError, ValueError):
            return None
        _, hard_limit = _thermal_policy()
        if temperature < hard_limit:
            return None
        return {
            "resource": "thermal.runtime",
            "temperature_c": temperature,
            "limit_c": hard_limit,
            "message": "%.1fC is at/above %.1fC runtime safety threshold" %
                       (temperature, hard_limit),
        }

    def conflicts(self, plan: Plan, *, ignore_app_id: Optional[str] = None) -> List[dict]:
        """Return current admission conflicts without creating reservations.

        This is advisory preflight only; :meth:`reserve` repeats the check under
        the manager lock immediately before mutation.  ``ignore_app_id`` makes
        an upgrade preflight describe conflicts with *other* applications rather
        than reporting the release it is about to replace as its own blocker.
        """
        with self._lock:
            allocations = [a for a in self._load()["allocations"]
                           if not ignore_app_id or a.get("app_id") != ignore_app_id]
            found = []
            for request in plan.requests:
                try:
                    self._check(request, allocations)
                except ResourceBusy as exc:
                    found.append({
                        "resource": exc.resource,
                        "name": exc.resource,
                        "owners": list(exc.owners),
                        "message": str(exc),
                    })
            # One logical conflict group can be reached by multiple claims.
            unique = {}
            for item in found:
                key = (item["resource"], tuple(item["owners"]))
                unique[key] = item
            return list(unique.values())

    def reconcile(self, live_instances: Iterable[str]) -> List[str]:
        """Drop allocations not belonging to an observed live/starting run."""
        live = set(x for x in live_instances if x)
        with self._lock:
            data = self._load()
            stale = [a for a in data["allocations"]
                     if a.get("instance_id") not in live]
            if stale:
                data["allocations"] = [a for a in data["allocations"]
                                       if a.get("instance_id") in live]
                data["revision"] = int(data.get("revision", 0)) + 1
                self._save(data)
            return [str(a.get("allocation_id")) for a in stale]

    def snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._load()))

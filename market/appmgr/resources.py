"""Small durable admission controller for appmgr-managed applications.

The native endpoints remain the final authority.  This module prevents known
conflicts before spawning a child and records which *application instance*
owns each soft reservation.  It intentionally models scheduled NPU inference
as a shared service resource; the legacy direct broker lease is a separate,
exclusive compatibility resource and is never presented as multi-owner NPU.
"""
from __future__ import annotations

import json
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


class ResourceError(RuntimeError):
    pass


class ResourceBusy(ResourceError):
    def __init__(self, resource: str, owners: Iterable[str]):
        self.resource = resource
        self.owners = sorted(set(str(x) for x in owners if x))
        owner_text = ", ".join(self.owners) or "unknown"
        super().__init__("resource %s is busy (owners: %s)" %
                         (resource, owner_text))


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
                capacity = _int(os.environ.get("APPMGR_FRAME_MANAGED_CAP", "3"), 3)
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
        capacity = _int(os.environ.get("APPMGR_FRAME_MANAGED_CAP", "3"), 3)
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

    return Plan(tuple(requests), npu_mode=npu_mode,
                result_gateway=use_gateway,
                inference_service=inference_service)


class ResourceManager:
    """Persist reservations and enforce share/exclusive/capacity conflicts."""

    def __init__(self, state_path: Optional[str] = None):
        self._state_path = state_path
        self._lock = threading.RLock()

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
        if not current:
            return
        owners = [a.get("app_id") for a in current]
        if (request.mode == "exclusive" or any(
                a.get("mode") == "exclusive" for a in current)):
            raise ResourceBusy(request.resource, owners)
        used = sum(_int(a.get("amount", 1)) for a in current)
        if request.capacity and used + request.amount > request.capacity:
            raise ResourceBusy(request.resource, owners)

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

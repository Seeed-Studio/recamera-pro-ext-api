"""Local multi-model RKNN service for reCamera application processes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import socket
import stat
import struct
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Protocol, Sequence

import numpy as np

from kit.runtime._inference_protocol import ProtocolError, recv_message, send_message
from kit.runtime.engine import ModelSpec, TensorSpec

from .driver_lock import NpuDriverCoordinator
from .authorization import (
    AuthorizationError,
    AuthorizationPending,
    ClientAuthorization,
    ModelAuthorization,
    RegistryAuthorizer,
)
from .scheduler import (
    DeadlineExceededError,
    FairScheduler,
    QueueFullError,
    ScheduledJob,
)


DEFAULT_SOCKET = "/run/recamera/inferenced.sock"
DEFAULT_ALLOWED_ROOTS = (
    "/userdata/local/apps",
    "/userdata/local/artifacts",
    "/userdata/local/models",
    "/oem/usr/share/model",
)
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


class Backend(Protocol):
    def load(self, path: str, spec: Mapping[str, Any]) -> Any: ...
    def infer(self, handle: Any, inputs: Sequence[np.ndarray]) -> list[np.ndarray]: ...
    def release(self, handle: Any) -> None: ...


class RknnBackend:
    """Real target backend.  Every context lives in this daemon process.

    rkipc and this backend use the same crash-safe file lock around vendor
    load/run/release calls.  Contexts therefore remain resident concurrently,
    while the RV1126B driver is never entered from both processes at once.
    """

    def __init__(self, *, runtime_factory=None, coordinator=None) -> None:
        self._runtime_factory = runtime_factory
        self.coordinator = coordinator or NpuDriverCoordinator()

    def _new_runtime(self):
        if self._runtime_factory is not None:
            return self._runtime_factory()
        from rknnlite.api import RKNNLite

        return RKNNLite(verbose=False)

    @staticmethod
    def _tensor(raw: Mapping[str, Any]) -> TensorSpec:
        return TensorSpec(
            name=raw.get("name", "tensor"),
            shape=tuple(raw.get("shape") or ()),
            dtype=raw.get("dtype", "uint8"),
            layout=raw.get("layout", "NHWC"),
        )

    def load(self, path: str, spec: Mapping[str, Any]) -> Any:
        model_spec = ModelSpec(
            path=path,
            name=spec.get("name"),
            core_mask=spec.get("core_mask"),
            inputs=tuple(self._tensor(item) for item in spec.get("inputs") or ()),
            outputs=tuple(self._tensor(item) for item in spec.get("outputs") or ()),
        )
        runtime = self._new_runtime()
        with self.coordinator.hold() as token:
            try:
                result = runtime.load_rknn(path)
                if result != 0:
                    raise RuntimeError(f"load_rknn failed with status {result!r}")
                if model_spec.core_mask is None:
                    result = runtime.init_runtime()
                else:
                    result = runtime.init_runtime(core_mask=model_spec.core_mask)
                if result != 0:
                    raise RuntimeError(f"init_runtime failed with status {result!r}")
            except BaseException:
                try:
                    released = runtime.release()
                    if released not in (None, 0):
                        raise RuntimeError(
                            f"RKNNLite.release returned {released!r} during rollback"
                        )
                except BaseException as release_error:
                    token.retain_fail_closed(
                        f"model-load rollback failed for {path}: {release_error}"
                    )
                raise
        return runtime

    def infer(self, handle: Any, inputs: Sequence[np.ndarray]) -> list[np.ndarray]:
        with self.coordinator.hold():
            outputs = handle.inference(inputs=list(inputs))
            if outputs is None:
                raise RuntimeError("RKNNLite.inference returned no outputs")
            return list(outputs)

    def release(self, handle: Any) -> None:
        with self.coordinator.hold() as token:
            try:
                result = handle.release()
                if result not in (None, 0):
                    raise RuntimeError(f"RKNNLite.release returned {result!r}")
            except BaseException as exc:
                token.retain_fail_closed(f"RKNN context teardown failed: {exc}")
                raise


class FakeBackend:
    """Deterministic host backend used by integration tests."""

    def __init__(self, infer_delay: float = 0.0) -> None:
        self.infer_delay = float(infer_delay)
        self.loaded: list[str] = []
        self.released: list[str] = []
        self.calls: list[str] = []

    def load(self, path: str, spec: Mapping[str, Any]) -> dict[str, Any]:
        # The real service loads from a verified, held descriptor exposed via
        # /proc/self/fd/N.  Resolve it here so host assertions retain the
        # canonical installed path rather than a transient descriptor number.
        canonical = os.path.realpath(path)
        self.loaded.append(canonical)
        return {"path": canonical, "spec": dict(spec)}

    def infer(self, handle: Mapping[str, Any], inputs: Sequence[np.ndarray]) -> list[np.ndarray]:
        if self.infer_delay:
            time.sleep(self.infer_delay)
        self.calls.append(str(handle["path"]))
        return [np.asarray(value) + 1 for value in inputs]

    def release(self, handle: Mapping[str, Any]) -> None:
        self.released.append(str(handle["path"]))


@dataclass
class _Model:
    key: str
    path: str
    sha256: str
    memory_mb: int
    priority: int
    max_fps: float
    spec_key: str
    handle: Any
    inputs: tuple[Mapping[str, Any], ...] = ()
    aliases: set[tuple[str, str]] = field(default_factory=set)
    next_allowed: float = 0.0
    calls: int = 0
    failures: int = 0
    total_ms: float = 0.0
    last_ms: float = 0.0


@dataclass
class _Client:
    id: str
    app_id: str
    instance_id: str
    generation: int
    pid: int
    uid: int
    gid: int
    authorization: Optional[ClientAuthorization] = None
    aliases: dict[str, str] = field(default_factory=dict)


def _error(code: str, message: str, *, retryable: bool = False, **details) -> dict:
    return {
        "ok": False,
        "error": {
            "code": code,
            "message": message,
            "retryable": bool(retryable),
            "details": details,
        },
    }


def _inside(path: str, roots: Sequence[str]) -> bool:
    return any(path == root or path.startswith(root.rstrip("/") + os.sep) for root in roots)


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_fd(fd: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
        size += len(chunk)
    os.lseek(fd, 0, os.SEEK_SET)
    return digest.hexdigest(), size


def _open_verified_model(policy: ModelAuthorization) -> tuple[int, str]:
    """Bind load_rknn to the exact appmgr-authorized model inode and bytes."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(policy.path, flags)
    except OSError as exc:
        raise AuthorizationError(
            f"cannot open authorized model {policy.artifact_id}: {exc}"
        ) from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise AuthorizationError("authorized model is no longer a regular file")
        if before.st_size != policy.size:
            raise AuthorizationError("authorized model size changed after app admission")
        digest, size = _hash_fd(fd)
        after = os.fstat(fd)
        if (
            size != policy.size
            or digest != policy.sha256
            or before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
        ):
            raise AuthorizationError(
                "authorized model bytes changed after app admission"
            )
        return fd, digest
    except BaseException:
        os.close(fd)
        raise


class InferenceService:
    """Own all target RKNN contexts and serve multiple application clients."""

    def __init__(
        self,
        socket_path: str = DEFAULT_SOCKET,
        *,
        backend: Optional[Backend] = None,
        allowed_roots: Sequence[str] = DEFAULT_ALLOWED_ROOTS,
        memory_budget_mb: int = 768,
        max_clients: int = 32,
        max_pending_per_client: int = 8,
        socket_mode: int = 0o660,
        authorizer=None,
        client_idle_timeout: float = 60.0,
    ) -> None:
        self.socket_path = os.path.abspath(socket_path)
        self.backend: Backend = backend or RknnBackend()
        self.allowed_roots = tuple(os.path.realpath(root) for root in allowed_roots)
        self.memory_budget_mb = int(memory_budget_mb)
        self.max_clients = int(max_clients)
        self.socket_mode = int(socket_mode)
        self.client_idle_timeout = float(client_idle_timeout)
        self.authorizer = authorizer if authorizer is not None else RegistryAuthorizer()
        if (
            self.memory_budget_mb <= 0
            or self.max_clients <= 0
            or not np.isfinite(self.client_idle_timeout)
            or self.client_idle_timeout <= 0
        ):
            raise ValueError("memory budget and max_clients must be positive")
        self._lock = threading.RLock()
        self._driver_lock = threading.RLock()
        self._models: dict[str, _Model] = {}
        self._clients: dict[str, _Client] = {}
        self._threads: set[threading.Thread] = set()
        self._connections: set[socket.socket] = set()
        self._listener: Optional[socket.socket] = None
        self._stopping = threading.Event()
        self._closed = False
        self._fault: Optional[str] = None
        self.scheduler = FairScheduler(max_pending_per_client=max_pending_per_client)

    def _record_native_fault(self, exc: BaseException, *, operation: str) -> str:
        """Publish a fail-closed backend quarantine through service status.

        ``RknnBackend`` deliberately retains the process-local mutex and flock
        when native teardown becomes uncertain.  Once that happens, attempting
        another model release would merely wait for the retained mutex and can
        turn daemon shutdown into N models times the lock timeout.  Recording a
        single authoritative fault lets every remaining path stop before it
        re-enters the driver; process exit is then the final fence.
        """
        coordinator = getattr(self.backend, "coordinator", None)
        quarantine = getattr(coordinator, "fault", None)
        reason = str(quarantine or f"{type(exc).__name__}: {exc}")
        message = f"{operation}: {reason}"
        with self._lock:
            if self._fault is None:
                self._fault = message
            return self._fault

    def start(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("inference service has already been closed")
            if self._listener is not None:
                return
            os.makedirs(os.path.dirname(self.socket_path), mode=0o755, exist_ok=True)
            try:
                st = os.lstat(self.socket_path)
            except FileNotFoundError:
                pass
            else:
                if not stat_is_socket(st.st_mode):
                    raise RuntimeError(
                        f"refusing to replace non-socket path {self.socket_path}"
                    )
                os.unlink(self.socket_path)
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                listener.bind(self.socket_path)
                os.chmod(self.socket_path, self.socket_mode)
                listener.listen(self.max_clients)
                listener.settimeout(0.5)
            except BaseException:
                listener.close()
                raise
            self._listener = listener

    def serve_forever(self) -> None:
        self.start()
        while not self._stopping.is_set():
            try:
                conn, _ = self._listener.accept()  # type: ignore[union-attr]
            except socket.timeout:
                continue
            except OSError:
                if self._stopping.is_set():
                    break
                raise
            with self._lock:
                if len(self._connections) >= self.max_clients:
                    try:
                        send_message(conn, _error("resource_busy", "too many clients"))
                    finally:
                        conn.close()
                    continue
                self._connections.add(conn)
            thread = threading.Thread(
                target=self._serve_connection,
                args=(conn,),
                name="inferenced-client",
                daemon=True,
            )
            with self._lock:
                self._threads.add(thread)
            thread.start()

    def request_stop(self) -> None:
        """Ask the accept loop to exit; safe for a Python signal handler."""

        self._stopping.set()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stopping.set()
            listener, self._listener = self._listener, None
        if listener is not None:
            listener.close()
        self.scheduler.close()
        with self._lock:
            clients = list(self._clients.values())
            connections = list(self._connections)
        for conn in connections:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
        for client in clients:
            self._drop_client(client)
        with self._lock:
            threads = list(self._threads)
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=2.0)
        with self._driver_lock:
            with self._lock:
                models = list(self._models.values())
                self._models.clear()
            for model in models:
                try:
                    self.backend.release(model.handle)
                except BaseException as exc:
                    # Shutdown remains best-effort; the process lifetime is the
                    # final driver fence if a vendor release fails.  Do not try
                    # another release: a real backend retains its local mutex
                    # and flock on this exact failure by design.
                    self._record_native_fault(exc, operation="shutdown release failed")
                    break
        try:
            if stat_is_socket(os.lstat(self.socket_path).st_mode):
                os.unlink(self.socket_path)
        except FileNotFoundError:
            pass

    def _peercred(self, conn: socket.socket) -> tuple[int, int, int]:
        if not hasattr(socket, "SO_PEERCRED"):
            return os.getpid(), os.getuid(), os.getgid()
        raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        return struct.unpack("3i", raw)

    def _serve_connection(self, conn: socket.socket) -> None:
        client: Optional[_Client] = None
        try:
            # A connected peer must complete hello promptly; otherwise a handful
            # of silent sockets could consume every daemon client/thread slot.
            conn.settimeout(min(5.0, self.client_idle_timeout))
            header, tensors = recv_message(conn)
            if tensors or header.get("op") != "hello":
                raise ProtocolError("first message must be a tensor-free hello")
            app_id = header.get("app_id")
            instance_id = header.get("instance_id")
            if not isinstance(app_id, str) or not _IDENTIFIER.fullmatch(app_id):
                raise ProtocolError("invalid app_id")
            if not isinstance(instance_id, str) or not _IDENTIFIER.fullmatch(instance_id):
                raise ProtocolError("invalid instance_id")
            generation = header.get("generation", 0)
            if (
                not isinstance(generation, int)
                or isinstance(generation, bool)
                or not 0 <= generation <= (1 << 63) - 1
            ):
                raise ProtocolError("invalid generation")
            control_only = header.get("control_only", False)
            if not isinstance(control_only, bool):
                raise ProtocolError("control_only must be a boolean")
            pid, uid, gid = self._peercred(conn)
            authorization = None
            if control_only:
                # appmgr and init readiness probes need only status/ping.  They do
                # not gain a model-loading identity from caller-provided strings.
                canonical_app = "control"
                canonical_instance = f"peer-{pid}"
                canonical_generation = 0
            else:
                authorization = self.authorizer.authorize(
                    peer_pid=pid,
                    peer_uid=uid,
                    peer_gid=gid,
                    claimed_app=app_id,
                    claimed_instance=instance_id,
                    claimed_generation=generation,
                )
                canonical_app = authorization.app_id
                canonical_instance = authorization.instance_id
                canonical_generation = authorization.generation
            client = _Client(
                uuid.uuid4().hex,
                canonical_app,
                canonical_instance,
                canonical_generation,
                pid,
                uid,
                gid,
                authorization,
            )
            with self._lock:
                self._clients[client.id] = client
            send_message(
                conn,
                {
                    "ok": True,
                    "op": "hello",
                    "request_id": header.get("request_id"),
                    "capabilities": {
                        "multi_model": True,
                        "scheduler": "aging-priority-fair-v1",
                        "memory_budget_mb": self.memory_budget_mb,
                        "max_clients": self.max_clients,
                        "authorized": authorization is not None,
                    },
                },
            )
            conn.settimeout(self.client_idle_timeout)
            while not self._stopping.is_set():
                try:
                    request, tensors = recv_message(conn)
                except EOFError:
                    break
                response, outputs = self._dispatch(client, request, tensors)
                send_message(conn, response, outputs)
        except (EOFError, BrokenPipeError, ConnectionResetError):
            pass
        except ProtocolError as exc:
            try:
                send_message(conn, _error("protocol_error", str(exc)))
            except OSError:
                pass
        except AuthorizationPending as exc:
            try:
                send_message(
                    conn,
                    _error("authorization_pending", str(exc), retryable=True),
                )
            except OSError:
                pass
        except AuthorizationError as exc:
            try:
                send_message(conn, _error("unauthorized", str(exc)))
            except OSError:
                pass
        except OSError:
            pass
        finally:
            if client is not None:
                self._drop_client(client)
            conn.close()
            with self._lock:
                self._connections.discard(conn)
                self._threads.discard(threading.current_thread())

    def _dispatch(
        self, client: _Client, request: Mapping[str, Any], tensors: list[np.ndarray]
    ) -> tuple[dict, list[np.ndarray]]:
        op = request.get("op")
        request_id = request.get("request_id")
        try:
            if client.authorization is None and op not in {"status", "ping"}:
                raise AuthorizationError(
                    "control-only inference connection cannot access models"
                )
            if client.authorization is not None and op != "unload":
                # An accepted Unix socket is not a perpetual bearer credential.
                # Detect appmgr's atomic unlink/replace and a dead/reused PID
                # before every operation, including already-queued workloads.
                #
                # ``unload`` is deliberately the sole exception: it can only
                # discard an alias already owned by this established client and
                # therefore cannot load/infer or regain authority.  Accepting a
                # late/repeated unload after revocation lets cooperative cleanup
                # remain idempotent across stop races while the missing record
                # continues to fence every capability-bearing operation.
                self.authorizer.validate(client.authorization)
            if self._fault and op not in {"status", "ping"}:
                return _error(
                    "service_faulted",
                    self._fault,
                    retryable=False,
                ), []
            if op == "load":
                if tensors:
                    raise ValueError("load does not accept tensors")
                payload = self._load(client, request)
            elif op == "infer":
                payload = self._infer(client, request, tensors)
                outputs = payload.pop("outputs")
                return {"ok": True, "op": op, "request_id": request_id, **payload}, outputs
            elif op == "unload":
                if tensors:
                    raise ValueError("unload does not accept tensors")
                payload = self._unload(client, str(request.get("alias") or ""))
            elif op == "status":
                if tensors:
                    raise ValueError("status does not accept tensors")
                payload = self.status()
            elif op == "ping":
                payload = {"time_monotonic": time.monotonic()}
            else:
                raise ValueError(f"unknown operation {op!r}")
            return {"ok": True, "op": op, "request_id": request_id, **payload}, []
        except QueueFullError as exc:
            return _error("queue_full", str(exc), retryable=True), []
        except DeadlineExceededError as exc:
            return _error("deadline_exceeded", str(exc), retryable=True), []
        except AuthorizationError as exc:
            return _error("unauthorized", str(exc), retryable=False), []
        except FileNotFoundError as exc:
            return _error("model_not_found", str(exc)), []
        except PermissionError as exc:
            return _error("model_path_denied", str(exc)), []
        except MemoryError as exc:
            return _error("memory_budget_exceeded", str(exc), retryable=True), []
        except (KeyError, TypeError, ValueError, ProtocolError) as exc:
            return _error("invalid_request", str(exc)), []
        except BaseException as exc:
            coordinator = getattr(self.backend, "coordinator", None)
            if getattr(coordinator, "fault", None):
                self._record_native_fault(exc, operation=f"{op or 'request'} failed")
            return _error(
                "inference_failed",
                f"{type(exc).__name__}: {exc}",
                retryable=True,
            ), []

    def _load(self, client: _Client, request: Mapping[str, Any]) -> dict:
        alias = request.get("alias")
        if not isinstance(alias, str) or not _IDENTIFIER.fullmatch(alias):
            raise ValueError("invalid model alias")
        if alias in client.aliases:
            raise ValueError("model alias is already loaded")
        if client.authorization is None:
            raise AuthorizationError("model loading requires an app authorization")
        requested_path = request.get("path")
        if not isinstance(requested_path, (str, bytes, os.PathLike)):
            raise ValueError("model path must be path-like")
        policy = self.authorizer.authorize_model(
            client.authorization, os.fspath(requested_path)
        )

        # Client-provided digest, reservation, priority, frame-rate and
        # ModelSpec are deliberately not policy inputs.  They are retained in
        # the wire shape for Kit compatibility, but the installed manifest and
        # platform calculations are the only authority here.
        path = policy.path
        memory_mb = policy.memory_mb
        priority = policy.priority
        max_fps = policy.max_fps
        spec = {
            "name": policy.artifact_id,
            "core_mask": None,
            "inputs": [dict(item) for item in policy.inputs],
            "outputs": [],
        }
        spec_key = json.dumps(spec, sort_keys=True, separators=(",", ":"))
        key = f"{policy.sha256}:{hashlib.sha256(spec_key.encode()).hexdigest()}"
        owner = (client.id, alias)
        with self._driver_lock:
            with self._lock:
                model = self._models.get(key)
                used_mb = sum(item.memory_mb for item in self._models.values())
                if model is None and used_mb + memory_mb > self.memory_budget_mb:
                    raise MemoryError(
                        f"model needs {memory_mb} MiB; {used_mb}/{self.memory_budget_mb} MiB reserved"
                    )
                identity = (
                    client.app_id,
                    client.instance_id,
                    client.generation,
                )
                instance_keys = {
                    model_key
                    for other in self._clients.values()
                    if (other.app_id, other.instance_id, other.generation) == identity
                    for model_key in other.aliases.values()
                }
                app_reserved_mb = sum(
                    self._models[model_key].memory_mb
                    for model_key in instance_keys
                    if model_key in self._models
                )
                if (
                    key not in instance_keys
                    and app_reserved_mb + memory_mb
                    > client.authorization.memory_limit_mb
                ):
                    raise MemoryError(
                        f"app model needs {memory_mb} MiB; "
                        f"{app_reserved_mb}/{client.authorization.memory_limit_mb} "
                        "MiB reserved for this generation"
                    )
            if model is None:
                fd, actual = _open_verified_model(policy)
                try:
                    handle = self.backend.load(f"/proc/self/fd/{fd}", spec)
                finally:
                    os.close(fd)
                model = _Model(
                    key=key,
                    path=path,
                    sha256=actual,
                    memory_mb=memory_mb,
                    priority=priority,
                    max_fps=max_fps,
                    spec_key=spec_key,
                    handle=handle,
                    inputs=policy.inputs,
                )
                with self._lock:
                    existing = self._models.get(key)
                    if existing is None:
                        self._models[key] = model
                    else:
                        self.backend.release(handle)
                        model = existing
            with self._lock:
                model.aliases.add(owner)
                client.aliases[alias] = key
        return {
            "alias": alias,
            "model_key": key,
            "sha256": policy.sha256,
            "memory_mb": memory_mb,
            "priority": priority,
            "max_fps": max_fps,
            "shared_references": len(model.aliases),
        }

    def _infer(
        self, client: _Client, request: Mapping[str, Any], tensors: list[np.ndarray]
    ) -> dict:
        alias = str(request.get("alias") or "")
        timeout_ms = request.get("timeout_ms", 30000)
        if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or not 1 <= timeout_ms <= 300000:
            raise ValueError("timeout_ms must be an integer in 1..300000")
        with self._lock:
            key = client.aliases.get(alias)
            model = self._models.get(key) if key else None
        if model is None:
            raise KeyError("model alias is not loaded")
        if model.inputs:
            if len(tensors) != len(model.inputs):
                raise ValueError(
                    f"authorized model expects {len(model.inputs)} tensors, "
                    f"got {len(tensors)}"
                )
            for index, (tensor, contract) in enumerate(zip(tensors, model.inputs)):
                expected_shape = tuple(int(value) for value in contract["shape"])
                expected_dtype = np.dtype(str(contract["dtype"]))
                if tensor.shape != expected_shape or tensor.dtype != expected_dtype:
                    raise ValueError(
                        f"tensor {index} has shape/dtype {tensor.shape}/{tensor.dtype}; "
                        f"authorized contract is {expected_shape}/{expected_dtype}"
                    )
        deadline = time.monotonic() + timeout_ms / 1000.0

        def execute():
            started = time.monotonic()
            try:
                if client.authorization is None:
                    raise AuthorizationError("inference authorization is missing")
                with self._driver_lock:
                    # A job may have waited behind other applications after its
                    # request was accepted.  Recheck the launch-generation only
                    # after it has acquired the driver admission lock: checking
                    # before this lock would let an already-revoked queued job
                    # enter RKNN after another model finishes.
                    self.authorizer.validate(client.authorization)
                    with self._lock:
                        if self._models.get(model.key) is not model:
                            raise RuntimeError("model was unloaded before inference")
                    outputs = self.backend.infer(model.handle, tensors)
                    model.next_allowed = (
                        time.monotonic() + 1.0 / model.max_fps if model.max_fps > 0 else 0.0
                    )
                return [np.asarray(value) for value in outputs]
            except BaseException:
                model.failures += 1
                raise
            finally:
                elapsed = (time.monotonic() - started) * 1000.0
                model.calls += 1
                model.last_ms = elapsed
                model.total_ms += elapsed

        job = ScheduledJob(
            client_id=client.id,
            fairness_id=(
                f"{client.app_id}:{client.instance_id}:{client.generation}"
            ),
            priority=model.priority,
            deadline=deadline,
            execute=execute,
            ready_at=lambda: model.next_allowed,
        )
        self.scheduler.submit(job)
        outputs = job.result(timeout=timeout_ms / 1000.0 + 1.0)
        return {"outputs": outputs, "model_key": model.key, "latency_ms": model.last_ms}

    def _unload(self, client: _Client, alias: str) -> dict:
        if not alias:
            raise ValueError("missing model alias")
        owner = (client.id, alias)
        with self._driver_lock:
            with self._lock:
                key = client.aliases.get(alias)
                model = self._models.get(key) if key else None
                if model is None or owner not in model.aliases:
                    client.aliases.pop(alias, None)
                    return {"alias": alias, "released": False}
                release = len(model.aliases) == 1
                if not release:
                    model.aliases.discard(owner)
                    client.aliases.pop(alias, None)
                    return {"alias": alias, "released": False}
            try:
                self.backend.release(model.handle)
            except BaseException as exc:
                with self._lock:
                    self._fault = (
                        f"native model teardown failed for {model.path}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                # Keep model+alias references intact.  A backend using the
                # driver coordinator also retains the cross-process flock, so
                # no other context can enter uncertain native state.
                raise
            with self._lock:
                if self._models.get(model.key) is model:
                    self._models.pop(model.key, None)
                model.aliases.discard(owner)
                client.aliases.pop(alias, None)
        return {"alias": alias, "released": True}

    def _drop_client(self, client: _Client) -> None:
        self.scheduler.cancel_client(client.id)
        for alias in list(client.aliases):
            try:
                self._unload(client, alias)
            except BaseException as exc:
                self._record_native_fault(exc, operation="client cleanup failed")
                break
        with self._lock:
            self._clients.pop(client.id, None)

    def status(self) -> dict:
        with self._lock:
            models = [
                {
                    "key": item.key,
                    "sha256": item.sha256,
                    "memory_mb": item.memory_mb,
                    "priority": item.priority,
                    "max_fps": item.max_fps,
                    "references": len(item.aliases),
                    "calls": item.calls,
                    "failures": item.failures,
                    "last_ms": item.last_ms,
                    "average_ms": item.total_ms / item.calls if item.calls else 0.0,
                }
                for item in self._models.values()
            ]
            return {
                "state": (
                    "stopping"
                    if self._stopping.is_set()
                    else ("faulted" if self._fault else "running")
                ),
                "fault": self._fault,
                "clients": len(self._clients),
                "models": models,
                "memory_budget_mb": self.memory_budget_mb,
                "memory_reserved_mb": sum(item.memory_mb for item in self._models.values()),
            }

    def __enter__(self) -> "InferenceService":
        self.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self.close()
        return False


def stat_is_socket(mode: int) -> bool:
    import stat

    return stat.S_ISSOCK(mode)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="reCamera multi-model inference service")
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    parser.add_argument("--memory-mb", type=int, default=768)
    parser.add_argument(
        "--authorization-dir",
        default=None,
        help="root-owned appmgr launch registry (default: /run/recamera/inference-authorizations)",
    )
    parser.add_argument(
        "--allowed-root",
        action="append",
        dest="allowed_roots",
        help="model root allowed for clients (repeatable)",
    )
    args = parser.parse_args(argv)
    service = InferenceService(
        args.socket,
        allowed_roots=args.allowed_roots or DEFAULT_ALLOWED_ROOTS,
        memory_budget_mb=args.memory_mb,
        authorizer=RegistryAuthorizer(root=args.authorization_dir),
    )
    previous_handlers = {}

    def stop_handler(_signum, _frame):
        service.request_stop()

    for signum in (signal.SIGTERM, signal.SIGINT):
        previous_handlers[signum] = signal.signal(signum, stop_handler)
    try:
        service.serve_forever()
    finally:
        service.close()
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    return 0


__all__ = [
    "DEFAULT_ALLOWED_ROOTS",
    "DEFAULT_SOCKET",
    "FakeBackend",
    "InferenceService",
    "RknnBackend",
    "main",
]

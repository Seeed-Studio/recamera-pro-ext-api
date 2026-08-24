"""Read appmgr's runtime-only inference authorization registry safely."""
from __future__ import annotations

import json
import math
import os
import re
import stat
import time
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Optional


DEFAULT_AUTHORIZATION_DIR = "/run/recamera/inference-authorizations"
REGISTRY_SCHEMA_VERSION = 1
MAX_AUTHORIZATION_BYTES = 256 * 1024
MAX_AUTHORIZED_MODELS = 64
MAX_APP_NPU_MEMORY_MB = 768
PLATFORM_MODEL_PRIORITY = 50
PLATFORM_MODEL_MAX_FPS = 0.0
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class AuthorizationError(PermissionError):
    """A peer is not authorized for the requested inference operation."""


class AuthorizationPending(AuthorizationError):
    """The child connected before appmgr atomically published its record."""


@dataclass(frozen=True)
class ModelAuthorization:
    artifact_id: str
    path: str
    sha256: str
    size: int
    memory_mb: int
    priority: int
    max_fps: float
    inputs: tuple[Mapping[str, object], ...] = ()


@dataclass(frozen=True)
class ClientAuthorization:
    app_id: str
    instance_id: str
    generation: int
    pid: int
    uid: int
    gid: int
    pid_starttime_ticks: int
    memory_limit_mb: int
    models: Mapping[str, ModelAuthorization]
    record_path: str
    record_device: int
    record_inode: int


def estimate_model_memory_mb(size_bytes: int) -> int:
    if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or size_bytes <= 0:
        raise AuthorizationError("authorized model size is invalid")
    return max(32, int(math.ceil(size_bytes / (1024 * 1024) * 2.0 + 32.0)))


def _open_regular(
    path: str,
    *,
    maximum: int,
    trusted_uid: Optional[int] = None,
    private: bool = False,
    allow_zero_size: bool = False,
    pending_missing: bool = False,
) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError as exc:
        if pending_missing:
            raise AuthorizationPending(
                f"authorization record is not published: {path}"
            ) from exc
        raise AuthorizationError(f"authorization input is missing: {path}") from exc
    except OSError as exc:
        raise AuthorizationError(f"cannot open authorization input {path}: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise AuthorizationError(f"authorization input is not regular: {path}")
        if (info.st_size < 1 and not allow_zero_size) or info.st_size > maximum:
            raise AuthorizationError(f"authorization input has invalid size: {path}")
        if trusted_uid is not None and info.st_uid != trusted_uid:
            raise AuthorizationError(f"authorization input has unexpected owner: {path}")
        if private and stat.S_IMODE(info.st_mode) & 0o077:
            raise AuthorizationError(f"authorization record must not be group/world accessible: {path}")
        return fd, info
    except BaseException:
        os.close(fd)
        raise


def _read_text(path: str, *, maximum: int = 8192) -> str:
    fd, info = _open_regular(path, maximum=maximum, allow_zero_size=True)
    try:
        raw = bytearray()
        while len(raw) <= maximum:
            chunk = os.read(fd, min(4096, maximum + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
    finally:
        os.close(fd)
    if (info.st_size > 0 and len(raw) != info.st_size) or len(raw) > maximum:
        raise AuthorizationError(f"identity file changed while reading: {path}")
    try:
        value = bytes(raw).decode("ascii", "strict").strip()
    except UnicodeDecodeError as exc:
        raise AuthorizationError(f"identity file is not ASCII: {path}") from exc
    if not value:
        raise AuthorizationError(f"identity file is empty: {path}")
    return value


def _process_starttime(proc_root: str, pid: int) -> int:
    raw = _read_text(os.path.join(proc_root, str(pid), "stat"), maximum=8192)
    end = raw.rfind(")")
    if end < 0:
        raise AuthorizationError(f"malformed /proc stat for peer pid {pid}")
    fields = raw[end + 1 :].strip().split()
    if len(fields) <= 19:
        raise AuthorizationError(f"short /proc stat for peer pid {pid}")
    try:
        value = int(fields[19])
    except ValueError as exc:
        raise AuthorizationError(f"invalid /proc starttime for peer pid {pid}") from exc
    if value <= 0:
        raise AuthorizationError(f"invalid /proc starttime for peer pid {pid}")
    return value


class RegistryAuthorizer:
    """Authenticate a UDS peer against one immutable appmgr launch record."""

    def __init__(
        self,
        root: Optional[str] = None,
        *,
        proc_root: str = "/proc",
        boot_id_path: str = "/proc/sys/kernel/random/boot_id",
        trusted_uid: Optional[int] = None,
        publish_wait: float = 0.0,
    ) -> None:
        self.root = os.path.abspath(
            root
            or os.environ.get("RECAMERA_INFERENCE_AUTH_DIR")
            or DEFAULT_AUTHORIZATION_DIR
        )
        self.proc_root = os.path.abspath(proc_root)
        self.boot_id_path = os.path.abspath(boot_id_path)
        self.trusted_uid = os.geteuid() if trusted_uid is None else int(trusted_uid)
        self.publish_wait = max(0.0, min(float(publish_wait), 5.0))

    def _validate_root(self) -> None:
        try:
            info = os.lstat(self.root)
        except FileNotFoundError as exc:
            raise AuthorizationPending(
                f"authorization registry is not available: {self.root}"
            ) from exc
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise AuthorizationError(
                f"authorization registry is not a directory: {self.root}"
            )
        if info.st_uid != self.trusted_uid:
            raise AuthorizationError(
                f"authorization registry has unexpected owner: {self.root}"
            )
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise AuthorizationError(
                f"authorization registry must be mode 0700: {self.root}"
            )

    def _read_record(self, pid: int) -> tuple[dict, os.stat_result, str]:
        self._validate_root()
        path = os.path.join(self.root, f"{pid}.json")
        fd, info = _open_regular(
            path,
            maximum=MAX_AUTHORIZATION_BYTES,
            trusted_uid=self.trusted_uid,
            private=True,
            pending_missing=True,
        )
        try:
            raw = bytearray()
            while len(raw) <= MAX_AUTHORIZATION_BYTES:
                chunk = os.read(
                    fd,
                    min(64 * 1024, MAX_AUTHORIZATION_BYTES + 1 - len(raw)),
                )
                if not chunk:
                    break
                raw.extend(chunk)
        finally:
            os.close(fd)
        if len(raw) != info.st_size or len(raw) > MAX_AUTHORIZATION_BYTES:
            raise AuthorizationError("authorization record changed while reading")
        try:
            record = json.loads(bytes(raw).decode("ascii", "strict"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise AuthorizationError("authorization record is invalid JSON") from exc
        if not isinstance(record, dict):
            raise AuthorizationError("authorization record root must be an object")
        return record, info, path

    @staticmethod
    def _model(raw: object) -> ModelAuthorization:
        if not isinstance(raw, dict):
            raise AuthorizationError("authorized model entry must be an object")
        artifact_id = raw.get("artifact_id")
        path = raw.get("path")
        digest = raw.get("sha256")
        size = raw.get("size")
        memory_mb = raw.get("memory_mb")
        if not isinstance(artifact_id, str) or not _IDENTIFIER_RE.fullmatch(artifact_id):
            raise AuthorizationError("authorized artifact id is invalid")
        if not isinstance(path, str) or not os.path.isabs(path) or os.path.realpath(path) != path:
            raise AuthorizationError("authorized model path is not canonical")
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise AuthorizationError("authorized model digest is invalid")
        if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= 1024 * 1024 * 1024:
            raise AuthorizationError("authorized model size is invalid")
        expected_memory = estimate_model_memory_mb(size)
        if memory_mb != expected_memory:
            raise AuthorizationError("authorized model memory does not match platform policy")
        if raw.get("priority") != PLATFORM_MODEL_PRIORITY:
            raise AuthorizationError("authorized model priority does not match platform policy")
        try:
            max_fps = float(raw.get("max_fps"))
        except (TypeError, ValueError) as exc:
            raise AuthorizationError("authorized model max_fps is invalid") from exc
        if max_fps != PLATFORM_MODEL_MAX_FPS:
            raise AuthorizationError("authorized model max_fps does not match platform policy")
        inputs = raw.get("inputs") or []
        if not isinstance(inputs, list) or len(inputs) > 16:
            raise AuthorizationError("authorized model input contract is invalid")
        validated_inputs = []
        for item in inputs:
            if not isinstance(item, dict):
                raise AuthorizationError("authorized tensor contract is invalid")
            shape = item.get("shape")
            if (
                not isinstance(shape, list)
                or not 1 <= len(shape) <= 8
                or any(not isinstance(value, int) or isinstance(value, bool) or value <= 0
                       for value in shape)
                or item.get("dtype") not in ("uint8", "float32")
                or item.get("layout") not in ("NHWC", "NCHW", "NC", "N")
            ):
                raise AuthorizationError("authorized tensor contract is invalid")
            validated_inputs.append(MappingProxyType({
                "name": str(item.get("name") or "input"),
                "shape": tuple(shape),
                "dtype": item["dtype"],
                "layout": item["layout"],
            }))
        return ModelAuthorization(
            artifact_id=artifact_id,
            path=path,
            sha256=digest,
            size=size,
            memory_mb=expected_memory,
            priority=PLATFORM_MODEL_PRIORITY,
            max_fps=PLATFORM_MODEL_MAX_FPS,
            inputs=tuple(validated_inputs),
        )

    def _authorize_once(
        self,
        *,
        peer_pid: int,
        peer_uid: int,
        peer_gid: int,
        claimed_app: str,
        claimed_instance: str,
        claimed_generation: int,
    ) -> ClientAuthorization:
        record, record_info, record_path = self._read_record(peer_pid)
        scalar_expectations = {
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "app_id": claimed_app,
            "instance_id": claimed_instance,
            "generation": claimed_generation,
            "pid": peer_pid,
            "uid": peer_uid,
            "gid": peer_gid,
        }
        for field, expected in scalar_expectations.items():
            if record.get(field) != expected:
                raise AuthorizationError(
                    f"inference peer does not match authorized {field}"
                )
        current_boot = _read_text(self.boot_id_path, maximum=256)
        if record.get("boot_id") != current_boot:
            raise AuthorizationError("inference authorization belongs to another boot")
        current_starttime = _process_starttime(self.proc_root, peer_pid)
        if record.get("pid_starttime_ticks") != current_starttime:
            raise AuthorizationError("inference authorization belongs to a reused PID")

        raw_models = record.get("models")
        if not isinstance(raw_models, list) or not 1 <= len(raw_models) <= MAX_AUTHORIZED_MODELS:
            raise AuthorizationError("authorization has no bounded model list")
        models: dict[str, ModelAuthorization] = {}
        for raw in raw_models:
            model = self._model(raw)
            if model.path in models:
                raise AuthorizationError("authorization contains duplicate model paths")
            models[model.path] = model
        memory_limit = record.get("memory_limit_mb")
        if (
            not isinstance(memory_limit, int)
            or isinstance(memory_limit, bool)
            or not 0 < memory_limit <= MAX_APP_NPU_MEMORY_MB
        ):
            raise AuthorizationError("authorization memory limit is invalid")
        if sum(model.memory_mb for model in models.values()) > memory_limit:
            raise AuthorizationError("authorized models exceed the app memory limit")
        return ClientAuthorization(
            app_id=claimed_app,
            instance_id=claimed_instance,
            generation=claimed_generation,
            pid=peer_pid,
            uid=peer_uid,
            gid=peer_gid,
            pid_starttime_ticks=current_starttime,
            memory_limit_mb=memory_limit,
            models=MappingProxyType(models),
            record_path=record_path,
            record_device=record_info.st_dev,
            record_inode=record_info.st_ino,
        )

    def authorize(self, **claims) -> ClientAuthorization:
        deadline = time.monotonic() + self.publish_wait
        while True:
            try:
                return self._authorize_once(**claims)
            except AuthorizationPending:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(min(0.02, max(0.001, deadline - time.monotonic())))

    def validate(self, authorization: ClientAuthorization) -> None:
        """Fail closed after appmgr revokes or replaces a launch record.

        The open connection is not itself an authorization lease: appmgr must
        be able to withdraw a stopped/crashed generation even before the socket
        is observed closed.  Atomic publication gives each generation a unique
        registry inode, so an unlink or replacement is detected without
        trusting mutable client fields or reparsing policy on every frame.
        ``/proc`` starttime is checked as well to keep the original PID binding
        live for inherited/passed Unix-socket descriptors.
        """

        try:
            info = os.lstat(authorization.record_path)
        except FileNotFoundError as exc:
            raise AuthorizationError("inference authorization has been revoked") from exc
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != self.trusted_uid
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_dev != authorization.record_device
            or info.st_ino != authorization.record_inode
        ):
            raise AuthorizationError("inference authorization generation was replaced")
        current_starttime = _process_starttime(self.proc_root, authorization.pid)
        if current_starttime != authorization.pid_starttime_ticks:
            raise AuthorizationError("inference authorization peer is no longer live")

    @staticmethod
    def authorize_model(
        authorization: ClientAuthorization, path: str
    ) -> ModelAuthorization:
        canonical = os.path.realpath(os.path.abspath(os.fspath(path)))
        try:
            return authorization.models[canonical]
        except KeyError as exc:
            raise AuthorizationError(
                f"model is not an authorized bundled artifact for {authorization.app_id}: "
                f"{canonical}"
            ) from exc


__all__ = [
    "AuthorizationError",
    "AuthorizationPending",
    "ClientAuthorization",
    "DEFAULT_AUTHORIZATION_DIR",
    "ModelAuthorization",
    "RegistryAuthorizer",
    "estimate_model_memory_mb",
]

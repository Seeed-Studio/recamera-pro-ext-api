"""Publish fail-closed inference authorizations for supervised app processes.

The inference daemon is a separate process and cannot call
``AppCoordinator.resolve_identity`` directly.  Appmgr therefore materialises
the same PID/app/instance/generation decision as a root-owned record under
``/run``.  Records are not bearer tokens: the daemon still obtains the caller
PID/UID/GID from ``SO_PEERCRED`` and binds it to the current boot plus
``/proc/<pid>/stat`` start time before accepting a model.

Only exact, installed manifest-v2 ``bundled`` RKNN artifacts are published.
Their bytes are checked again from a no-follow file descriptor at launch, and
all scheduling values are platform policy rather than application input.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
import time
from typing import Any, Mapping, Optional

from . import manifest as manifest_contract, paths


REGISTRY_SCHEMA_VERSION = 1
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_AUTHORIZATION_BYTES = 256 * 1024
MAX_AUTHORIZED_MODELS = 64
MAX_APP_NPU_MEMORY_MB = 768
PLATFORM_MODEL_PRIORITY = 50
PLATFORM_MODEL_MAX_FPS = 0.0
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


class InferenceAuthorizationError(RuntimeError):
    """The installed app cannot be represented by a safe daemon policy."""


def estimate_model_memory_mb(size_bytes: int) -> int:
    """Conservative v1 reservation derived only from authenticated file size."""

    if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or size_bytes <= 0:
        raise InferenceAuthorizationError("model size must be a positive integer")
    # RKNN contexts need weights plus driver/runtime workspaces.  File size is
    # not a perfect peak-RSS oracle, so reserve two copies plus a fixed 32 MiB
    # workspace and let the board-wide daemon budget remain the final limit.
    return max(32, int(math.ceil(size_bytes / (1024 * 1024) * 2.0 + 32.0)))


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


def _open_regular(path: str, *, maximum: Optional[int] = None) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise InferenceAuthorizationError(f"cannot open {path}: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise InferenceAuthorizationError(f"not a regular file: {path}")
        if maximum is not None and info.st_size > maximum:
            raise InferenceAuthorizationError(
                f"file exceeds {maximum} byte authorization limit: {path}"
            )
        return fd, info
    except BaseException:
        os.close(fd)
        raise


def _read_json_file(path: str, *, maximum: int) -> dict:
    fd, info = _open_regular(path, maximum=maximum)
    try:
        raw = bytearray()
        while len(raw) <= maximum:
            chunk = os.read(fd, min(64 * 1024, maximum + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) > maximum or len(raw) != info.st_size:
            raise InferenceAuthorizationError(f"invalid JSON file size: {path}")
        value = json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise InferenceAuthorizationError(f"invalid JSON in {path}: {exc}") from exc
    finally:
        os.close(fd)
    if not isinstance(value, dict):
        raise InferenceAuthorizationError(f"JSON root must be an object: {path}")
    return value


def _read_small_text(path: str, maximum: int = 256) -> str:
    fd, info = _open_regular(path, maximum=maximum)
    try:
        raw = os.read(fd, maximum + 1)
    finally:
        os.close(fd)
    # procfs identity files conventionally report st_size=0 even though read()
    # returns content.  Ordinary files still receive the changed-size check.
    if (info.st_size > 0 and len(raw) != info.st_size) or len(raw) > maximum:
        raise InferenceAuthorizationError(f"invalid identity file size: {path}")
    value = raw.decode("ascii", "strict").strip()
    if not value:
        raise InferenceAuthorizationError(f"empty identity file: {path}")
    return value


def _process_starttime(proc_root: str, pid: int) -> int:
    raw = _read_small_text(os.path.join(proc_root, str(pid), "stat"), 8192)
    end = raw.rfind(")")
    if end < 0:
        raise InferenceAuthorizationError(f"malformed /proc stat for pid {pid}")
    fields = raw[end + 1 :].strip().split()
    # fields[0] is process-state (the kernel's field 3); starttime is field 22.
    if len(fields) <= 19:
        raise InferenceAuthorizationError(f"short /proc stat for pid {pid}")
    try:
        value = int(fields[19])
    except ValueError as exc:
        raise InferenceAuthorizationError(f"invalid starttime for pid {pid}") from exc
    if value <= 0:
        raise InferenceAuthorizationError(f"invalid starttime for pid {pid}")
    return value


def _process_credentials(proc_root: str, pid: int) -> tuple[int, int]:
    path = os.path.join(proc_root, str(pid), "status")
    text = _read_small_text(path, 64 * 1024)
    uid = gid = None
    for line in text.splitlines():
        if line.startswith("Uid:"):
            values = line.split()[1:]
            if len(values) >= 2:
                uid = int(values[1])
        elif line.startswith("Gid:"):
            values = line.split()[1:]
            if len(values) >= 2:
                gid = int(values[1])
    if uid is None or gid is None:
        raise InferenceAuthorizationError(f"cannot read credentials for pid {pid}")
    return uid, gid


class InferenceAuthorizationRegistry:
    """Build and atomically publish one authorization per process leader."""

    def __init__(
        self,
        root: Optional[str] = None,
        *,
        proc_root: str = "/proc",
        boot_id_path: str = "/proc/sys/kernel/random/boot_id",
        trusted_uid: Optional[int] = None,
    ) -> None:
        self.root = os.path.abspath(root or paths.inference_authorization_dir())
        self.proc_root = os.path.abspath(proc_root)
        self.boot_id_path = os.path.abspath(boot_id_path)
        self.trusted_uid = os.geteuid() if trusted_uid is None else int(trusted_uid)

    def _ensure_root(self) -> None:
        os.makedirs(self.root, mode=0o700, exist_ok=True)
        info = os.lstat(self.root)
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise InferenceAuthorizationError(
                f"inference authorization root is not a directory: {self.root}"
            )
        if info.st_uid != self.trusted_uid:
            raise InferenceAuthorizationError(
                f"inference authorization root has unexpected owner: {self.root}"
            )
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise InferenceAuthorizationError(
                f"inference authorization root must be mode 0700: {self.root}"
            )

    def _installed_manifest(self, app_id: str, expected: Mapping[str, Any]) -> dict:
        if not paths.valid_app_id(app_id):
            raise InferenceAuthorizationError(f"invalid app id {app_id!r}")
        root = os.path.realpath(paths.app_dir(app_id))
        manifest_path = os.path.join(root, "manifest.json")
        installed = _read_json_file(manifest_path, maximum=MAX_MANIFEST_BYTES)
        try:
            version = manifest_contract.validate_manifest(installed, allow_v1=False)
        except manifest_contract.ManifestValidationError as exc:
            raise InferenceAuthorizationError(
                f"installed manifest is invalid for {app_id}: {exc}"
            ) from exc
        if version != manifest_contract.MANIFEST_VERSION or installed.get("id") != app_id:
            raise InferenceAuthorizationError(
                f"installed manifest identity does not match {app_id!r}"
            )
        # The coordinator's admission/resource plan and the policy must describe
        # the exact same installed generation, never two caller-supplied views.
        if dict(expected) != installed:
            raise InferenceAuthorizationError(
                f"coordinator manifest differs from installed manifest for {app_id}"
            )
        return installed

    def prepare(self, app_id: str, manifest: Mapping[str, Any]) -> dict:
        installed = self._installed_manifest(app_id, manifest)
        permissions = (installed.get("permissions") or {}).get("sdk") or []
        if "npu.infer" not in permissions:
            raise InferenceAuthorizationError(
                f"{app_id} does not have permissions.sdk npu.infer"
            )

        app_root = os.path.realpath(paths.app_dir(app_id))
        model_declarations = {
            item.get("file"): item
            for item in installed.get("models") or []
            if isinstance(item, dict) and isinstance(item.get("file"), str)
        }
        models = []
        for artifact in installed.get("artifacts") or []:
            if artifact.get("kind") != "rknn":
                continue
            if artifact.get("source") != "bundled":
                raise InferenceAuthorizationError(
                    f"scheduled RKNN artifact {artifact.get('id')!r} must be bundled"
                )
            relative = artifact.get("file")
            candidate = os.path.join(app_root, relative)
            resolved = os.path.realpath(candidate)
            if not paths.is_within(resolved, app_root) or os.path.islink(candidate):
                raise InferenceAuthorizationError(
                    f"RKNN artifact escapes installed app root: {relative!r}"
                )
            fd, info = _open_regular(resolved, maximum=1024 * 1024 * 1024)
            try:
                digest, size = _hash_fd(fd)
            finally:
                os.close(fd)
            if size != artifact.get("size") or digest != artifact.get("sha256"):
                raise InferenceAuthorizationError(
                    f"installed RKNN artifact failed digest/size verification: {relative}"
                )
            if info.st_size != size:
                raise InferenceAuthorizationError(
                    f"installed RKNN artifact changed while hashing: {relative}"
                )
            declared = model_declarations.get(relative) or {}
            input_shape = declared.get("input")
            trusted_inputs = []
            if (
                isinstance(input_shape, list)
                and input_shape
                and all(isinstance(value, int) and not isinstance(value, bool) and value > 0
                        for value in input_shape)
            ):
                trusted_inputs.append({
                    "name": "input",
                    "shape": list(input_shape),
                    "dtype": "uint8",
                    "layout": "NHWC" if len(input_shape) == 4 else "NCHW",
                })
            models.append({
                "artifact_id": artifact["id"],
                "path": resolved,
                "sha256": digest,
                "size": size,
                "memory_mb": estimate_model_memory_mb(size),
                "priority": PLATFORM_MODEL_PRIORITY,
                "max_fps": PLATFORM_MODEL_MAX_FPS,
                "inputs": trusted_inputs,
            })

        if not models:
            raise InferenceAuthorizationError(
                f"scheduled application {app_id} has no bundled RKNN artifact"
            )
        if len(models) > MAX_AUTHORIZED_MODELS:
            raise InferenceAuthorizationError(
                f"{app_id} declares too many RKNN artifacts: {len(models)}"
            )
        reserved = sum(model["memory_mb"] for model in models)
        declared_limit = ((installed.get("resources") or {}).get("limits") or {}).get(
            "memory_mb"
        )
        if declared_limit is None:
            memory_limit = reserved
        elif not isinstance(declared_limit, int) or isinstance(declared_limit, bool):
            raise InferenceAuthorizationError("resources.limits.memory_mb is invalid")
        else:
            memory_limit = min(declared_limit, MAX_APP_NPU_MEMORY_MB)
        if reserved > memory_limit:
            raise InferenceAuthorizationError(
                f"platform NPU reservation {reserved} MiB exceeds {app_id} limit "
                f"{memory_limit} MiB"
            )
        return {
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "app_id": app_id,
            "memory_limit_mb": memory_limit,
            "models": models,
        }

    def publish(
        self,
        policy: Mapping[str, Any],
        *,
        pid: int,
        instance_id: str,
        generation: int,
    ) -> str:
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise InferenceAuthorizationError("pid must be a positive integer")
        if not isinstance(instance_id, str) or not _IDENTIFIER_RE.fullmatch(instance_id):
            raise InferenceAuthorizationError("invalid inference instance id")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation <= 0:
            raise InferenceAuthorizationError("generation must be a positive integer")
        uid, gid = _process_credentials(self.proc_root, pid)
        record = dict(policy)
        record.update({
            "pid": pid,
            "uid": uid,
            "gid": gid,
            "instance_id": instance_id,
            "generation": generation,
            "boot_id": _read_small_text(self.boot_id_path),
            "pid_starttime_ticks": _process_starttime(self.proc_root, pid),
        })
        stable_record = dict(record)
        record["issued_at"] = time.time()
        encoded = json.dumps(
            record, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        if len(encoded) > MAX_AUTHORIZATION_BYTES:
            raise InferenceAuthorizationError("inference authorization record is too large")

        self._ensure_root()
        destination = os.path.join(self.root, f"{pid}.json")
        # ``start`` is deliberately idempotent for an already-running app.  Do
        # not replace an identical live record: inferenced binds accepted
        # connections to its inode so replacement is an intentional revocation
        # boundary, not a harmless timestamp refresh.
        try:
            existing_info = os.lstat(destination)
            existing = _read_json_file(
                destination, maximum=MAX_AUTHORIZATION_BYTES
            )
        except (FileNotFoundError, InferenceAuthorizationError, OSError):
            existing = None
            existing_info = None
        if (
            existing is not None
            and existing_info is not None
            and stat.S_ISREG(existing_info.st_mode)
            and not stat.S_ISLNK(existing_info.st_mode)
            and existing_info.st_uid == self.trusted_uid
            and not stat.S_IMODE(existing_info.st_mode) & 0o077
            and {
                key: value for key, value in existing.items() if key != "issued_at"
            } == stable_record
            and isinstance(existing.get("issued_at"), (int, float))
            and not isinstance(existing.get("issued_at"), bool)
        ):
            return destination
        fd, temporary = tempfile.mkstemp(prefix=f".{pid}.", dir=self.root)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            fd = -1
            os.replace(temporary, destination)
            temporary = None
            directory_fd = os.open(
                self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if fd >= 0:
                os.close(fd)
            if temporary:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
        return destination

    def revoke(
        self,
        pid: Optional[int],
        *,
        app_id: Optional[str] = None,
        instance_id: Optional[str] = None,
        generation: Optional[int] = None,
    ) -> bool:
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return False
        path = os.path.join(self.root, f"{pid}.json")
        try:
            record = _read_json_file(path, maximum=MAX_AUTHORIZATION_BYTES)
        except (FileNotFoundError, InferenceAuthorizationError):
            return False
        expected = {
            "app_id": app_id,
            "instance_id": instance_id,
            "generation": generation,
        }
        for key, value in expected.items():
            if value is not None and record.get(key) != value:
                return False
        try:
            os.unlink(path)
        except FileNotFoundError:
            return False
        return True


__all__ = [
    "InferenceAuthorizationError",
    "InferenceAuthorizationRegistry",
    "MAX_APP_NPU_MEMORY_MB",
    "PLATFORM_MODEL_MAX_FPS",
    "PLATFORM_MODEL_PRIORITY",
    "REGISTRY_SCHEMA_VERSION",
    "estimate_model_memory_mb",
]

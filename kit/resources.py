"""Process-level hardware resource guards used by Python AI sessions.

The normal device backend is rkipc's versioned, connection-lifetime NPU broker.
It drains built-in inference before granting external ownership and reclaims the
lease on socket HUP.  The older advisory ``flock`` backend remains available
only when a caller explicitly supplies a custom/test lock path (or the legacy
``RECAMERA_NPU_LOCK`` override); it does not coordinate built-in inference.
"""
from __future__ import annotations

import errno
import fcntl
import math
import os
import threading
import time
import weakref
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional, Protocol, runtime_checkable

from .errors import (
    CapabilityError,
    InputValidationError,
    ResourceBusyError,
    ResourceTimeoutError,
    TransportError,
)
from .diagnostics import get_logger


log = get_logger("resources.npu")
DEFAULT_NPU_BROKER = "/run/recamera/inference-control.sock"
DEFAULT_NPU_LOCK = "/run/recamera/npu-external.lock"
NPU_MANAGED_MARKER = "appmgr-v1"
NPU_BROKER_REQUIRED_ENV = "RECAMERA_NPU_BROKER_REQUIRED"
_BROKER_MAX_TIMEOUT_SECONDS = 30.0


class ResourceKind(str, Enum):
    """Hardware resources an AI workflow may declare."""

    CAMERA = "camera"
    NPU = "npu"
    RGA = "rga"
    RESULT_INGRESS = "result_ingress"
    VEPU = "vepu"
    AUDIO = "audio"


@runtime_checkable
class ResourceLease(Protocol):
    """Minimal backwards-compatible lease contract consumed by sessions.

    Broker-aware implementations may additionally expose ``ready()`` and
    ``alive()``.  :class:`RknnSession` feature-detects those hooks so existing
    third-party acquire/release-only leases remain structurally compatible.
    """

    def acquire(self, timeout: Optional[float] = None) -> "ResourceLease":
        """Acquire ownership or raise a typed resource error."""

    def release(self) -> None:
        """Release ownership; the operation must be idempotent."""


@dataclass
class _HeldLock:
    pid: int
    fd: int
    references: int


@dataclass
class _HeldBroker:
    pid: int
    lease: Any
    references: int
    config: tuple[Any, ...]
    ready: bool = False


_state_lock = threading.RLock()
# Serializes the small "inspect local refs -> take OS flock -> publish local
# ref" transaction. Without this, two threads in one process could open the
# same path concurrently and the second could briefly report EBUSY against the
# first thread before its shared reference was published.
# This lock must be re-entrant.  Typed-error construction can trigger cyclic GC
# while an acquire transaction is in progress; an unreachable lease finalizer
# then calls release() on this same thread.  A plain Lock deadlocks before the
# finalizer can discover that its lease was never acquired.
_acquire_lock = threading.RLock()
_held_by_path: dict[str, _HeldLock] = {}
_held_broker: Optional[_HeldBroker] = None
# Weak references let the child-side fork hook invalidate lease objects without
# keeping otherwise-dead leases alive.  Merely checking ``lease.acquired`` is
# not enough after fork: the inherited flock fd must be closed before the parent
# dies, or a long-lived fork child extends the parent's kernel lock indefinitely.
_lease_instances: "weakref.WeakSet[ExternalNpuLease]" = weakref.WeakSet()


def _before_fork() -> None:
    """Freeze lease publication while fork snapshots the descriptor table."""

    _acquire_lock.acquire()
    _state_lock.acquire()


def _after_fork_parent() -> None:
    _state_lock.release()
    _acquire_lock.release()


def _after_fork_child() -> None:
    """Drop every inherited flock reference in a fork-only child.

    ``flock`` ownership follows the open file description inherited by fork.
    Calling LOCK_UN here would also unlock the parent's shared description, so
    the child must only close its copies.  Fresh locks replace potentially
    poisoned thread locks inherited from a multithreaded parent.
    """

    global _state_lock, _acquire_lock, _held_broker
    closed = set()
    for held in list(_held_by_path.values()):
        if held.fd in closed:
            continue
        closed.add(held.fd)
        try:
            os.close(held.fd)
        except OSError:
            pass
    _held_by_path.clear()
    # Never call RELEASE here: that would revoke the parent's live generation.
    # New SDKs expose a child-only, mutex-free close-without-RELEASE operation.
    # If an older SDK lacks it, retain the inherited fd until child exit; this is
    # safer than falsely telling rkipc that the parent surrendered ownership.
    if _held_broker is not None:
        abandon = getattr(
            _held_broker.lease, "_abandon_after_fork_child", None)
        if callable(abandon):
            try:
                abandon()
            except BaseException:
                pass
    _held_broker = None
    for lease in list(_lease_instances):
        lease._acquired = False
        lease._pid = None
        lease._broker_lease = None
    _state_lock = threading.RLock()
    _acquire_lock = threading.RLock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(
        before=_before_fork,
        after_in_parent=_after_fork_parent,
        after_in_child=_after_fork_child,
    )


def _default_broker_factory(**kwargs):
    """Import the low-level SDK only when the device broker is requested."""

    from recamera_ext import InferenceLease

    return InferenceLease(**kwargs)


def _broker_failure(
    exc: Exception,
    *,
    operation: str,
    timeout: Optional[float] = None,
):
    """Translate low-level SDK errors into the kit's stable error hierarchy."""

    if isinstance(exc, (CapabilityError, InputValidationError,
                        ResourceBusyError, TransportError)):
        return exc
    code_value = getattr(exc, "code_value", None)
    if callable(code_value):
        code_value = code_value()
    if code_value is None:
        native_code = getattr(exc, "code", None)
        try:
            code_value = abs(int(native_code))
        except (TypeError, ValueError):
            code_value = None
    details = {
        "backend": "rkipc",
        "endpoint": DEFAULT_NPU_BROKER,
        "native_error": type(exc).__name__,
        "native_code": code_value,
    }
    if code_value == 3:  # RC_EXT_EBUSY
        error_type = (
            ResourceTimeoutError
            if operation == "npu.lease.acquire" and timeout != 0.0
            else ResourceBusyError
        )
        return error_type(
            "rkipc did not grant external NPU ownership",
            operation=operation,
            retryable=True,
            details={**details, "timeout": timeout},
        )
    if code_value == 4 or type(exc).__name__ == "FormatError":
        return InputValidationError(
            "rkipc rejected the NPU lease request format",
            operation=operation,
            details=details,
        )
    if isinstance(exc, (ImportError, ModuleNotFoundError, OSError)) or \
            type(exc).__name__ in {
                "CapabilityUnavailableError", "LibraryLoadError"
            }:
        return CapabilityError(
            "the installed SDK/firmware lacks the inference lease broker",
            operation=operation,
            retryable=False,
            details=details,
        )
    return TransportError(
        f"rkipc NPU lease operation failed: {exc}",
        operation=operation,
        retryable=True,
        details=details,
    )


class ExternalNpuLease:
    """Exclusive RKNN ownership, brokered by rkipc on normal device paths.

    With no ``path`` argument (and no ``RECAMERA_NPU_LOCK`` override), acquire
    uses :class:`recamera_ext.InferenceLease`.  The broker atomically drains the
    built-in model and its connection fences one ownership generation.  Lease
    objects in the same process/configuration share that one native connection
    through reference counting.

    Passing a path explicitly selects the compatibility ``flock`` backend for
    host tests or legacy deployments.  That backend coordinates cooperating
    Python processes only.  If the explicit path is the historical device lock,
    the appmgr marker/session-leader guard is retained to prevent accidental
    uncoordinated startup.
    """

    def __init__(
        self,
        path: Optional[str] = None,
        *,
        app_id: Optional[str] = None,
        instance_id: Optional[str] = None,
        fallback_builtin: bool = True,
        lib_path: Optional[str] = None,
        broker_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        env_path = os.environ.get("RECAMERA_NPU_LOCK")
        broker_required = os.environ.get(NPU_BROKER_REQUIRED_ENV) == "1"
        if broker_required and (path is not None or env_path):
            raise InputValidationError(
                "appmgr requires the rkipc NPU broker; legacy lock overrides "
                "are disabled for this process",
                operation="npu.lease.configure",
                code="npu_broker_required",
                details={
                    "path_argument": path is not None,
                    "lock_environment": bool(env_path),
                },
            )
        explicit_path = path if path is not None else (env_path or None)
        # Old-firmware appmgr launches have already crossed the strict CGI
        # stopped-state barrier and mint this marker.  Keep their historical
        # lock route when no broker-required marker is present.  An unmarked
        # manual/default process remains broker-first and fail-closed.
        if (explicit_path is None and not broker_required
                and os.environ.get("RECAMERA_NPU_MANAGED") == NPU_MANAGED_MARKER):
            explicit_path = DEFAULT_NPU_LOCK
        self.backend = "broker" if explicit_path is None else "flock"
        self.path = (DEFAULT_NPU_BROKER if self.backend == "broker"
                     else os.path.abspath(os.fspath(explicit_path)))
        self._requires_managed_launch = (
            self.backend == "flock"
            and self.path == os.path.abspath(DEFAULT_NPU_LOCK)
        )
        self.app_id = app_id or os.environ.get("RECAMERA_APP_ID") or "python"
        self.instance_id = (
            instance_id or os.environ.get("RECAMERA_APP_INSTANCE")
            or f"pid-{os.getpid()}"
        )
        self.fallback_builtin = bool(fallback_builtin)
        self.lib_path = (None if lib_path is None else os.fspath(lib_path))
        self._broker_factory = broker_factory or _default_broker_factory
        self._broker_config = (
            self.app_id,
            self.instance_id,
            self.fallback_builtin,
            self.lib_path,
            self._broker_factory,
        )
        self._broker_lease = None
        self._acquired = False
        self._pid: Optional[int] = None
        with _state_lock:
            _lease_instances.add(self)

    @property
    def acquired(self) -> bool:
        """Whether this lease instance currently holds a local reference."""

        return self._acquired and self._pid == os.getpid()

    @staticmethod
    def _normalize_timeout(timeout: Optional[float]) -> Optional[float]:
        if timeout is None:
            return None
        try:
            timeout_value = float(timeout)
        except (TypeError, ValueError) as exc:
            raise InputValidationError(
                "NPU lease timeout must be a finite non-negative number",
                operation="npu.lease.acquire",
                details={"timeout_type": type(timeout).__name__},
            ) from exc
        if not math.isfinite(timeout_value) or timeout_value < 0.0:
            raise InputValidationError(
                "NPU lease timeout must be finite and non-negative",
                operation="npu.lease.acquire",
                details={"timeout": repr(timeout)},
            )
        return timeout_value

    @staticmethod
    def _broker_timeout_ms(timeout: Optional[float]) -> int:
        # Native zero selects the server default.  Preserve timeout=0's public
        # non-blocking intent with the smallest representable positive deadline.
        if timeout is None:
            return 0
        if timeout > _BROKER_MAX_TIMEOUT_SECONDS:
            raise InputValidationError(
                "rkipc NPU lease timeout cannot exceed 30 seconds",
                operation="npu.lease.acquire",
                details={"timeout": timeout, "maximum": 30.0},
            )
        return max(1, int(math.ceil(timeout * 1000.0)))

    def acquire(self, timeout: Optional[float] = None) -> "ExternalNpuLease":
        """Acquire NPU ownership or raise a typed resource/capability error."""

        timeout_value = self._normalize_timeout(timeout)
        if self.backend == "broker":
            return self._acquire_broker(timeout_value)
        return self._acquire_flock(timeout_value)

    def _acquire_broker(
        self, timeout: Optional[float]
    ) -> "ExternalNpuLease":
        global _held_broker

        timeout_ms = self._broker_timeout_ms(timeout)
        pid = os.getpid()
        with _acquire_lock:
            with _state_lock:
                if self.acquired:
                    return self
                held = _held_broker
                if held is not None and held.pid == pid:
                    if held.config != self._broker_config:
                        raise ResourceBusyError(
                            "this process already owns the NPU through a "
                            "different broker configuration",
                            operation="npu.lease.acquire",
                            code="npu_broker_config_conflict",
                            retryable=False,
                            details={"backend": "rkipc"},
                        )
                    held.references += 1
                    self._broker_lease = held.lease
                    self._acquired = True
                    self._pid = pid
                    return self
                if held is not None and held.pid != pid:
                    # Inherited bookkeeping must never let a fork child operate
                    # or RELEASE its parent's broker generation.
                    _held_broker = None

            kwargs = {
                "app_id": self.app_id,
                "instance_id": self.instance_id,
                "timeout_ms": timeout_ms,
                "fallback_builtin": self.fallback_builtin,
            }
            if self.lib_path is not None:
                kwargs["lib_path"] = self.lib_path
            try:
                broker = self._broker_factory(**kwargs)
            except Exception as exc:
                raise _broker_failure(
                    exc,
                    operation="npu.lease.acquire",
                    timeout=timeout,
                ) from exc

            try:
                with _state_lock:
                    _held_broker = _HeldBroker(
                        pid=pid,
                        lease=broker,
                        references=1,
                        config=self._broker_config,
                    )
                    self._broker_lease = broker
                    self._acquired = True
                    self._pid = pid
            except BaseException:
                try:
                    broker.release()
                except BaseException:
                    log.critical(
                        "failed to close an unpublished rkipc NPU lease",
                        exc_info=True,
                    )
                raise
        log.info("acquired rkipc NPU lease app_id=%s pid=%d",
                 self.app_id, pid)
        return self

    def _check_legacy_launch(self) -> None:
        if not self._requires_managed_launch:
            return
        marker = os.environ.get("RECAMERA_NPU_MANAGED", "")
        pid = os.getpid()
        try:
            process_is_session_leader = (
                pid == os.getpgrp() and pid == os.getsid(0))
        except OSError:
            process_is_session_leader = False
        if marker != NPU_MANAGED_MARKER or not process_is_session_leader:
            raise ResourceBusyError(
                "legacy device locks require appmgr-confirmed built-in teardown",
                operation="npu.lease.acquire",
                code="unmanaged_npu_start",
                retryable=False,
                details={
                    "path": self.path,
                    "managed_marker": marker == NPU_MANAGED_MARKER,
                    "session_leader": process_is_session_leader,
                },
            )

    def _acquire_flock(
        self, timeout: Optional[float]
    ) -> "ExternalNpuLease":
        self._check_legacy_launch()
        pid = os.getpid()
        with _acquire_lock:
            with _state_lock:
                if self.acquired:
                    return self
                held = _held_by_path.get(self.path)
                if held is not None and held.pid == pid:
                    held.references += 1
                    self._acquired = True
                    self._pid = pid
                    return self
                if held is not None and held.pid != pid:
                    try:
                        os.close(held.fd)
                    except OSError:
                        pass
                    _held_by_path.pop(self.path, None)

            try:
                os.makedirs(os.path.dirname(self.path), mode=0o750, exist_ok=True)
                fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC,
                             0o660)
            except OSError as exc:
                raise TransportError(
                    f"cannot open NPU coordination lock {self.path}: {exc}",
                    operation="npu.lease.open",
                    retryable=exc.errno in (errno.ENOENT, errno.EACCES, errno.EROFS),
                    details={"path": self.path, "errno": exc.errno},
                ) from exc

            published = False
            try:
                deadline = None if timeout is None else time.monotonic() + timeout
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError as exc:
                        if timeout == 0.0:
                            raise ResourceBusyError(
                                "NPU is owned by another legacy Python process",
                                operation="npu.lease.acquire",
                                retryable=True,
                                details={"path": self.path},
                            ) from exc
                        if deadline is not None and time.monotonic() >= deadline:
                            raise ResourceTimeoutError(
                                "timed out waiting for the legacy NPU lock",
                                operation="npu.lease.acquire",
                                retryable=True,
                                details={"path": self.path, "timeout": timeout},
                            ) from exc
                        time.sleep(0.05)
                    except OSError as exc:
                        raise TransportError(
                            f"failed to acquire NPU coordination lock: {exc}",
                            operation="npu.lease.acquire",
                            retryable=True,
                            details={"path": self.path, "errno": exc.errno},
                        ) from exc

                with _state_lock:
                    _held_by_path[self.path] = _HeldLock(pid, fd, 1)
                    self._acquired = True
                    self._pid = pid
                published = True
            finally:
                if not published:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
        log.debug("acquired legacy NPU process lock path=%s pid=%d",
                  self.path, pid)
        return self

    def _broker_operation(self, method: str, operation: str):
        with _acquire_lock:
            if not self.acquired or self._broker_lease is None:
                raise ResourceBusyError(
                    "NPU lease is not acquired",
                    operation=operation,
                    code="npu_lease_not_acquired",
                )
            try:
                return getattr(self._broker_lease, method)()
            except Exception as exc:
                raise _broker_failure(exc, operation=operation) from exc

    def ready(self) -> None:
        """Mark runtime initialization complete; a no-op for legacy locks."""

        if self.backend == "flock":
            if not self.acquired:
                raise ResourceBusyError(
                    "NPU lease is not acquired",
                    operation="npu.lease.ready",
                    code="npu_lease_not_acquired",
                )
            return
        with _acquire_lock:
            if not self.acquired or self._broker_lease is None:
                raise ResourceBusyError(
                    "NPU lease is not acquired",
                    operation="npu.lease.ready",
                    code="npu_lease_not_acquired",
                )
            held = _held_broker
            if held is None or held.lease is not self._broker_lease:
                raise ResourceBusyError(
                    "NPU broker generation is no longer owned by this process",
                    operation="npu.lease.ready",
                    code="npu_lease_revoked",
                )
            if held.ready:
                return
            try:
                held.lease.ready()
            except Exception as exc:
                raise _broker_failure(
                    exc, operation="npu.lease.ready") from exc
            held.ready = True

    def alive(self) -> bool:
        """Check that the ownership fence is still live before inference."""

        if self.backend == "flock":
            return self.acquired
        result = self._broker_operation("alive", "npu.lease.alive")
        if result not in (True, False):
            raise TransportError(
                "rkipc returned an invalid NPU lease liveness value",
                operation="npu.lease.alive",
                details={"backend": "rkipc", "type": type(result).__name__},
            )
        return bool(result)

    def release(self) -> None:
        """Release this instance's reference; safe to call repeatedly."""

        if self.backend == "broker":
            self._release_broker()
        else:
            self._release_flock()

    def _release_broker(self) -> None:
        global _held_broker

        pid = os.getpid()
        with _acquire_lock:
            with _state_lock:
                if not self._acquired:
                    return
                owner_pid = self._pid
                broker = self._broker_lease
                held = _held_broker
                if (held is None or held.pid != pid or owner_pid != pid
                        or held.lease is not broker):
                    raise ResourceBusyError(
                        "NPU broker generation bookkeeping is inconsistent; "
                        "ownership is retained fail-closed",
                        operation="npu.lease.release",
                        code="npu_lease_state_mismatch",
                        retryable=False,
                        details={"backend": "rkipc"},
                    )
                if held.references > 1:
                    held.references -= 1
                    self._acquired = False
                    self._pid = None
                    self._broker_lease = None
                    return

            # For the final process-local reference, closing the native broker
            # connection is the ownership transition.  Do not discard either
            # the object or global reference before that call succeeds: a
            # custom/native close failure must leave the generation fenced and
            # retryable (RknnSession will quarantine this lease).
            try:
                broker.release()
            except Exception as exc:
                raise _broker_failure(
                    exc, operation="npu.lease.release") from exc
            with _state_lock:
                _held_broker = None
                self._acquired = False
                self._pid = None
                self._broker_lease = None
        log.info("released rkipc NPU lease app_id=%s pid=%d",
                 self.app_id, pid)

    def _release_flock(self) -> None:
        pid = os.getpid()
        with _acquire_lock:
            with _state_lock:
                if not self._acquired:
                    return
                self._acquired = False
                owner_pid, self._pid = self._pid, None
                held = _held_by_path.get(self.path)
                if held is None or held.pid != pid or owner_pid != pid:
                    return
                held.references -= 1
                if held.references > 0:
                    return
                _held_by_path.pop(self.path, None)
                fd = held.fd
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        log.debug("released legacy NPU process lock path=%s pid=%d",
                  self.path, pid)

    def __enter__(self) -> "ExternalNpuLease":
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            self.release()
        except BaseException as cleanup_error:
            if exc is None:
                raise
            log.error(
                "NPU lease cleanup failed while preserving body exception",
                exc_info=(
                    type(cleanup_error),
                    cleanup_error,
                    cleanup_error.__traceback__,
                ),
            )
        return None

    def __del__(self) -> None:
        try:
            self.release()
        except BaseException:
            # Explicit release remains the observable cleanup API.  Kernel HUP
            # or flock cleanup still protects process death.
            pass


__all__ = [
    "DEFAULT_NPU_BROKER",
    "DEFAULT_NPU_LOCK",
    "ExternalNpuLease",
    "NPU_BROKER_REQUIRED_ENV",
    "NPU_MANAGED_MARKER",
    "ResourceKind",
    "ResourceLease",
]

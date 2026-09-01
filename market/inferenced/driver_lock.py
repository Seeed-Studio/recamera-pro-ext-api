"""Crash-safe cross-process serialization for RV1126B RKNN driver calls."""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import stat
import threading
import time
from dataclasses import dataclass
from typing import Iterator, Optional


DEFAULT_DRIVER_LOCK = "/run/recamera/npu-scheduler.lock"


class DriverLockError(RuntimeError):
    pass


@dataclass
class DriverLockToken:
    _owner: "NpuDriverCoordinator"
    _fd: int
    _retained: bool = False

    def retain_fail_closed(self, reason: str) -> None:
        """Keep the kernel lock until process exit after unsafe teardown."""

        self._owner._retain(self, reason)
        self._retained = True


class NpuDriverCoordinator:
    """Serialize native RKNN calls with rkipc and within this process.

    Every acquisition uses a distinct open file description, so ``flock`` has
    well-defined cross-process semantics.  A process-local lock supplies the
    thread serialization that flock alone does not guarantee.  Process death
    closes the descriptor and therefore always releases the kernel fence.
    """

    def __init__(self, path: str = DEFAULT_DRIVER_LOCK) -> None:
        self.path = os.path.abspath(path)
        self._local = threading.Lock()
        self._state = threading.Lock()
        self._quarantine_fd: Optional[int] = None
        self._quarantine_reason: Optional[str] = None

    @property
    def fault(self) -> Optional[str]:
        with self._state:
            return self._quarantine_reason

    def _open(self) -> int:
        os.makedirs(os.path.dirname(self.path), mode=0o750, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.path, flags, 0o660)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise DriverLockError(f"NPU scheduler lock is not regular: {self.path}")
            os.fchmod(fd, 0o660)
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _retain(self, token: DriverLockToken, reason: str) -> None:
        with self._state:
            if self._quarantine_fd is not None and self._quarantine_fd != token._fd:
                raise DriverLockError("NPU driver lock is already quarantined")
            self._quarantine_fd = token._fd
            self._quarantine_reason = str(reason or "native teardown failed")

    @contextlib.contextmanager
    def hold(self, timeout: float = 30.0) -> Iterator[DriverLockToken]:
        if isinstance(timeout, bool):
            raise ValueError("timeout must be numeric, not bool")
        timeout = float(timeout)
        if not 0 < timeout <= 300 or not timeout < float("inf"):
            raise ValueError("timeout must be finite and in (0, 300]")
        with self._state:
            if self._quarantine_reason is not None:
                raise DriverLockError(
                    f"NPU driver is quarantined: {self._quarantine_reason}"
                )
        deadline = time.monotonic() + timeout
        if not self._local.acquire(timeout=timeout):
            raise TimeoutError("timed out waiting for local NPU driver lock")
        fd = -1
        token: Optional[DriverLockToken] = None
        try:
            fd = self._open()
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EINTR):
                        raise
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"timed out waiting for NPU driver lock {self.path}"
                        ) from exc
                    time.sleep(min(0.02, max(0.001, deadline - time.monotonic())))
            token = DriverLockToken(self, fd)
            yield token
        finally:
            if token is not None and token._retained:
                # Deliberately retain both the flock descriptor and local mutex.
                # No further driver call can start in this process, while SIGKILL
                # or supervisor restart still releases the kernel lock.
                pass
            else:
                if fd >= 0:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    finally:
                        os.close(fd)
                self._local.release()


__all__ = [
    "DEFAULT_DRIVER_LOCK",
    "DriverLockError",
    "DriverLockToken",
    "NpuDriverCoordinator",
]

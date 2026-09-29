"""Process-lifetime exclusive claim over the shared microphone (V4-7).

Why this exists
---------------
`audio.capture` is a **shared** resource: the platform admits several consumers
on purpose, because ALSA `dsnoop` really does let more than one reader share the
`ai_asr` PCM.  That is the right contract for "rkipc plus one app", and the wrong
one for "two apps that each load a full ASR model": the failure mode is not
EBUSY, it is two SenseVoice models, two loudnorm chains and two NPU sessions
competing for the same four cores and the same memory budget.

The platform therefore cannot express this exclusion through shared capacity,
and appmgr's admission list is not the place to encode a product decision about
which two specific apps may not co-exist.  What is available, and what this
module uses, is one advisory `flock` on a well-known path:

  * **atomic** -- `flock(LOCK_EX | LOCK_NB)` either takes the lock or fails; the
    check and the claim are the same syscall, so two simultaneous starts cannot
    both win;
  * **symmetric** -- both applications take the *same* lock, so the outcome does
    not depend on which one starts first: the later starter is always the one
    that is refused, and it is refused with a reason naming the holder;
  * **self-healing** -- `flock` is released by the kernel when the owning
    process exits, including SIGKILL, so a crash cannot leave a stale claim.

Because it is symmetric, both sides have to participate.  `voice-transcribe`
takes the same claim in its own `prepare_runtime`, which is what makes the two
launch orders behave identically.
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
import tempfile
import time
from typing import Any, Dict, Optional

from kit.errors import ResourceBusyError


ENV_LOCK_PATH = "RECAMERA_AUDIO_EXCLUSIVE_LOCK"
DEFAULT_LOCK_DIR = "/userdata/tmp"
DEFAULT_LOCK_NAME = "recamera-audio-exclusive.lock"
_RECORD_LIMIT = 512


def default_lock_path() -> str:
    """Where the claim lives: explicit env > device tmpdir > host tempdir."""
    override = str(os.environ.get(ENV_LOCK_PATH) or "").strip()
    if override:
        return override
    if os.path.isdir(DEFAULT_LOCK_DIR) and os.access(DEFAULT_LOCK_DIR, os.W_OK):
        return os.path.join(DEFAULT_LOCK_DIR, DEFAULT_LOCK_NAME)
    return os.path.join(tempfile.gettempdir(), DEFAULT_LOCK_NAME)


def _close_fd_in_child(_claim) -> None:        # pragma: no cover - fork hook
    """Drop the inherited claim fd in a forked child.

    `flock` ownership follows the open file description, so a forked child that
    keeps the descriptor alive keeps the claim alive past the parent's death --
    exactly the "recycling means process death" invariant this claim relies on.
    Closing the child's copy does not release the parent's lock.
    """
    fd = getattr(_claim, "_fd", None)
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass
    _claim._fd = None


class ExclusiveAudioClaim:
    """One process-wide claim on the microphone, held until `release()`."""

    def __init__(self, owner: str, path: Optional[str] = None, *,
                 clock=time.monotonic) -> None:
        self.owner = str(owner)
        self.path = path or default_lock_path()
        self._clock = clock
        self._fd: Optional[int] = None
        self.acquired_at: Optional[float] = None

    # -- state ------------------------------------------------------------- #
    @property
    def held(self) -> bool:
        return self._fd is not None

    def __enter__(self) -> "ExclusiveAudioClaim":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()

    # -- claim ------------------------------------------------------------- #
    def acquire(self) -> "ExclusiveAudioClaim":
        """Take the claim or raise :class:`ResourceBusyError` naming the holder."""
        if self._fd is not None:
            return self
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o666)
        except OSError as exc:
            raise ResourceBusyError(
                f"the exclusive microphone claim at {self.path!r} cannot be "
                f"opened ({exc.strerror or exc}); audio capture cannot be "
                f"arbitrated between applications",
                operation="audio.claim",
                code="audio_claim_unavailable",
                details={"path": self.path, "owner": self.owner}) from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            holder = _read_record(fd)
            os.close(fd)
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                raise ResourceBusyError(
                    f"the exclusive microphone claim at {self.path!r} could not "
                    f"be taken: {exc.strerror or exc}",
                    operation="audio.claim",
                    code="audio_claim_failed",
                    details={"path": self.path, "owner": self.owner}) from exc
            who = holder.get("owner") or "another application"
            pid = holder.get("pid")
            started = holder.get("started")
            detail = f"{who} is already using the microphone"
            if pid:
                detail += f" (pid {pid})"
            raise ResourceBusyError(
                f"{detail}; {self.owner} and {who} both own a full offline ASR "
                f"model and must not run at the same time. Stop {who} first, "
                f"then start {self.owner}.",
                operation="audio.claim",
                code="audio_capture_claimed",
                retryable=False,
                details={"path": self.path, "owner": self.owner,
                         "held_by": who, "held_by_pid": pid,
                         "held_since": started}) from exc
        self._fd = fd
        self.acquired_at = self._clock()
        self._write_record()
        _register_fork_hook(self)
        return self

    def release(self) -> None:
        """Drop the claim; repeated calls are harmless."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    # -- record ------------------------------------------------------------ #
    def _write_record(self) -> None:
        if self._fd is None:
            return
        record = json.dumps({
            "owner": self.owner,
            "pid": os.getpid(),
            "started": time.time(),
        }, separators=(",", ":")).encode("utf-8")[:_RECORD_LIMIT]
        try:
            os.ftruncate(self._fd, 0)
            os.lseek(self._fd, 0, os.SEEK_SET)
            os.write(self._fd, record)
            os.fsync(self._fd)
        except OSError:
            pass

    @property
    def holder(self) -> Dict[str, Any]:
        """Best-effort record of who currently holds the claim ({} if free)."""
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_CLOEXEC)
        except OSError:
            return {}
        try:
            return _read_record(fd)
        finally:
            os.close(fd)


def _read_record(fd: int) -> Dict[str, Any]:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, _RECORD_LIMIT)
    except OSError:
        return {}
    if not raw:
        return {}
    try:
        value = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _register_fork_hook(claim: "ExclusiveAudioClaim") -> None:
    try:
        from multiprocessing.util import register_after_fork
        register_after_fork(claim, _close_fd_in_child)
    except Exception:
        pass

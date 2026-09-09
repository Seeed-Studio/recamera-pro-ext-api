"""
supervisor.py -- start / stop an app process. appmgr IS the process supervisor
(no /etc/init.d, no dentry drop -- APP_CENTER_PORT_DESIGN §4.4).

start(id):
  * builds the launch command from manifest (entry + first model + config default)
    and launches it through the kit entry point: `<python> -m kit.run <app_dir>/<entry>`,
  * launches under a NEW session/process group via os.setsid (start_new_session),
  * PYTHONPATH + KIT_PARENT point at the ONE shared kit copy,
  * atomically records leader identity in <app>/run.pid + <app>/run.pgid and
    binds it to the current kernel boot in <app>/run.boot_id,
  * redirects stdout/stderr to <app>/logs/app.log.

stop(id):
  * reads the persisted PID/PGID and verifies a live leader still belongs to
    this app (via /proc); a dead leader's saved PGID addresses descendants only
    when run.boot_id proves the record belongs to the current kernel boot,
  * signals the whole PROCESS GROUP: TERM -> grace -> KILL (so ffmpeg children
    die too),
  * NEVER uses `pkill -f app.py`/`pkill -f python` (would kill the ssh session);
    it also never uses a system-wide `pkill -x ffmpeg`.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable, Dict, List, Optional

from . import kitversion, mqtt as mqttcfg, paths, pythonenv, voiceruntime


class SupervisorError(Exception):
    pass


class ProcessFenceError(SupervisorError):
    """A trusted app leader/process group could not be proven terminated."""

    process_fence_active = True

    def __init__(self, app_id: str, pid: Optional[int], pgid: Optional[int], *,
                 leader_alive: bool, group_alive: bool):
        self.app_id = app_id
        self.pid = pid
        self.pgid = pgid
        self.leader_alive = bool(leader_alive)
        self.group_alive = bool(group_alive)
        super().__init__(
            "application process fence remains active after stop: %s "
            "(pid=%s, pgid=%s, leader_alive=%s, group_alive=%s)" % (
                app_id, pid, pgid, self.leader_alive, self.group_alive))


# procfs root. Overridable so the unit tests can point the pid inspectors at a
# fixture tree -- macOS (the dev box) has no /proc at all, and even on Linux you
# cannot conjure a process in an arbitrary state on demand.
PROC_ROOT = os.environ.get("APPMGR_PROC_ROOT", "/proc")
# Numeric PIDs/PGIDs are reused after a reboot while /userdata survives.  Tests
# override this path with a deterministic file; production reads Linux's stable
# per-boot UUID.  Failure to read it blocks a new launch because that launch
# could not be cleaned up safely after its leader disappeared.
BOOT_ID_PATH = os.environ.get(
    "APPMGR_BOOT_ID_PATH", "/proc/sys/kernel/random/boot_id")

# ---- app-readiness handshake (lifecycle §core1) ----------------------------- #
# Popen returning does NOT mean the app is up: an interpreter/import failure, a
# missing model, or a socket it cannot bind all surface only after the process
# has already forked, while the UI would already read "running/active". start()
# therefore waits for the app to CREATE its readyfile (kit.run_app writes it once
# App.start() has loaded models, opened the sink and bound the frame source) and
# only then reports success. Timeout is generous -- real vision apps load an RKNN
# model + open the camera before signalling -- but env-overridable so a test can
# drive it low.
READY_TIMEOUT = float(os.environ.get("APPMGR_READY_TIMEOUT", "30"))
_READY_POLL = float(os.environ.get("APPMGR_READY_POLL", "0.05"))
# SIGKILL delivery can precede disappearance of the final helper/zombie from
# the process-group table.  Both startup cleanup and explicit stop allow this
# short bounded settle period before declaring an uncontained process fence.
PROCESS_FENCE_SETTLE_SEC = float(os.environ.get(
    "APPMGR_PROCESS_FENCE_SETTLE_SEC", "0.8"))
_PROCESS_FENCE_POLL_SEC = 0.05

# ---- app log retention (runtime ring buffer via pipe drain) ---------------- #
# app.log is drained from the app's stdout/stderr by a per-run pump thread
# (see _app_log_pump) -- NOT handed to the child as a direct fd -- so it can be
# rotated WHILE the app runs, not only between runs.  When the live app.log
# reaches APP_LOG_MAX_BYTES the generations shift app.log -> .1 -> ... -> .N
# (oldest dropped), bounding the on-disk footprint of a long-running app to
# (APP_LOG_BACKUPS + 1) * APP_LOG_MAX_BYTES.  Set APP_LOG_MAX_BYTES<=0 to
# disable rotation (the pump then just appends without bounding).
APP_LOG_MAX_BYTES = int(os.environ.get(
    "APPMGR_APP_LOG_MAX_BYTES", str(2 * 1024 * 1024)))
APP_LOG_BACKUPS = int(os.environ.get("APPMGR_APP_LOG_BACKUPS", "3"))

# ---- app child registry (健壮#17) ------------------------------------------- #
# pid -> Popen for the app children THIS appmgr launched. SIGCHLD reaping consults
# ONLY this registry (via Popen.poll(), the single reaper for each app pid) and
# NEVER waitpid(-1): a process-wide reap steals the exit status of the short-lived
# helpers appmgr shells out to with subprocess.run() -- openssl (signing), pip
# (voiceruntime), gst-inspect, the pkill sweep -- and subprocess.run reports a
# stolen status as ChildProcessError -> returncode 0, so a FAILED runtime probe
# would read as success (voiceruntime.py judges "present" off that return code).
_apps: Dict[int, "subprocess.Popen"] = {}

# Per-app log-drain threads (one per RUNNING app).  start() spawns the pump to
# drain the child's stdout/stderr pipe into app.log with live rotation; stop()
# / _terminate_proc join it after the group is dead so the final bytes flush.
_log_pumps: Dict[str, threading.Thread] = {}

# The three files in one run record have an explicit commit order
# (PGID -> boot ID -> PID commit marker), but rename(2) can only make each file
# atomic individually.  Normal-context readers and cleanup must therefore not
# inspect the directory between those renames: seeing a same-boot PGID without
# run.pid would look exactly like a dead leader and sweep_stale() could SIGKILL
# the process that start() had just launched.  One process-local lock covers the
# complete record transaction.  It is intentionally NEVER acquired by the
# SIGCHLD path (reap_children); the handler only uses metadata already attached
# to the Popen object and must remain async-safe with respect to Python locks.
_RUN_RECORD_LOCK = threading.RLock()


def _register_child(proc: "subprocess.Popen", app_id: str = None,
                    pgid: int = None, boot_id: str = None) -> None:
    """Record an app child so reap_children() can reap and contain its exit.

    Production launches attach the app id, already-known PGID (equal to the
    leader PID because Popen uses ``start_new_session``), and verified current
    boot identity directly to the Popen object.  Keeping the metadata beside the
    child avoids file I/O in the SIGCHLD path while still letting that path kill
    descendants after the leader disappears.  Tests/legacy callers that only
    pass ``proc`` retain the old reap-only behaviour.
    """
    if app_id is not None:
        proc._appmgr_app_id = app_id
    if pgid is not None:
        proc._appmgr_pgid = int(pgid)
    if boot_id is not None:
        proc._appmgr_boot_id = str(boot_id)
        # Resolve this once in normal launch context.  reap_children may run as
        # the SIGCHLD handler and must neither read procfs nor perform file I/O.
        proc._appmgr_boot_verified = (str(boot_id) == _current_boot_id())
    _apps[proc.pid] = proc


# ---- pid helpers ------------------------------------------------------------ #
def _join_pathlist(parts) -> str:
    """Join a colon path list, dropping empty entries and later duplicates.

    Empty entries are the reason this exists rather than a plain `":".join()`:
    in LD_LIBRARY_PATH glibc reads "" as the current directory. Order is kept --
    the first occurrence of a dir wins -- because search order is semantic.
    """
    seen, out = set(), []
    for p in parts:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return os.pathsep.join(out)


def _read_positive_int(path: str) -> Optional[int]:
    try:
        with open(path) as f:
            value = int(f.read().strip())
    except (FileNotFoundError, ValueError, OSError):
        return None
    # Never allow a corrupt run file to address init/system process groups.
    return value if value > 1 else None


def _write_text(path: str, value: str) -> None:
    """Atomically persist one non-empty run-record field."""
    value = str(value).strip()
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError("refusing invalid run-record value %r" % value)
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".run-id.", dir=directory)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(value)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


def _write_positive_int(path: str, value: int) -> None:
    """Atomically persist one PID/PGID value in its app directory."""
    if int(value) <= 1:
        raise ValueError("refusing to persist unsafe pid/pgid %r" % value)
    _write_text(path, str(int(value)))


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path) as f:
            value = f.read(256).strip()
    except OSError:
        return None
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        return None
    return value


def _current_boot_id() -> Optional[str]:
    """Kernel boot identity, or None when it cannot be established safely."""
    return _read_text(BOOT_ID_PATH)


def _read_pid(app_id: str) -> Optional[int]:
    with _RUN_RECORD_LOCK:
        return _read_positive_int(paths.pidfile(app_id))


def _read_pgid(app_id: str) -> Optional[int]:
    with _RUN_RECORD_LOCK:
        return _read_positive_int(paths.pgidfile(app_id))


def _read_run_boot_id(app_id: str) -> Optional[str]:
    with _RUN_RECORD_LOCK:
        return _read_text(paths.bootfile(app_id))


def _same_boot_record(app_id: str) -> bool:
    """Whether this persisted run record is explicitly from this boot.

    Missing identity (all legacy installs), malformed identity, and inability to
    read the kernel identity are all untrusted.  Callers may clear such stale
    records but must never address a dead leader's numeric PGID with them.
    """
    with _RUN_RECORD_LOCK:
        saved = _read_run_boot_id(app_id)
        current = _current_boot_id()
        return bool(saved and current and saved == current)


def _run_pgid(app_id: str, pid: int = None) -> Optional[int]:
    """Return the saved PGID after validating the leader==group invariant.

    Legacy installs have only run.pid, so its numeric value remains a candidate
    PGID for a *live, ownership-verified* leader.  It is never sufficient for
    dead-leader cleanup: that additionally requires _same_boot_record().  If
    both numeric files exist but disagree, refuse either value because a partial
    or replaced record must fail closed rather than target an unrelated group.
    """
    with _RUN_RECORD_LOCK:
        pgid = _read_pgid(app_id)
        if pgid is None:
            return pid
        if pid is not None and pgid != pid:
            print("[appmgr] refusing mismatched run record for %s: pid=%s pgid=%s"
                  % (app_id, pid, pgid), flush=True)
            return None
        return pgid


def _write_run_ids(app_id: str, pid: int, pgid: int, boot_id: str) -> None:
    """Persist PGID+boot first and PID last; run.pid is the commit marker."""
    if pid != pgid:
        raise SupervisorError("app leader pid %d != pgid %d" % (pid, pgid))
    with _RUN_RECORD_LOCK:
        _write_positive_int(paths.pgidfile(app_id), pgid)
        _write_text(paths.bootfile(app_id), boot_id)
        _write_positive_int(paths.pidfile(app_id), pid)


def _proc_cmdline(pid: int) -> str:
    try:
        with open(f"{PROC_ROOT}/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\x00", b" ").decode("utf-8", "replace")
    except OSError:
        return ""


def _proc_cwd(pid: int) -> str:
    try:
        return os.readlink(f"{PROC_ROOT}/{pid}/cwd")
    except OSError:
        return ""


def _proc_state(pid: int) -> str:
    """The single-letter process state from /proc/<pid>/stat ("R"/"S"/"D"/"Z"/...).

    Returns "" when procfs is unreadable (pid gone, or no procfs at all, e.g. the
    macOS dev box) -- callers must treat "" as "unknown", never as "dead".

    Parsing note: field 2 (comm) is parenthesised and MAY contain spaces and
    ')' , so the state char is taken as the first token AFTER the LAST ')'.
    """
    try:
        with open(f"{PROC_ROOT}/{pid}/stat", "rb") as f:
            raw = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    tail = raw[raw.rfind(")") + 1:].split()
    return tail[0] if tail else ""


def _is_zombie(pid: int) -> bool:
    """True only when procfs positively reports state Z (exited, not yet reaped)."""
    return _proc_state(pid) == "Z"


def _is_ours(pid: int, app_id: str) -> bool:
    """PID-reuse guard: the process must belong to THIS app.

    Strong check: its working dir is the app's install dir (we launch with
    cwd=app_dir). Fallback to a cmdline heuristic only if cwd is unreadable --
    since _build_cmd() switched to `python3 -m kit.run <abs app_dir>/app.py`,
    the cmdline DOES contain the app id (it used to be a bare `python3 app.py
    --model models/..`, which named nothing, so the fallback was near-useless).

    An UNREADABLE cwd must not fall through to the realpath comparison: for a
    zombie (and for any pid we cannot introspect) _proc_cwd() returns "", and
    os.path.realpath("") resolves to the APPMGR's OWN cwd -- which would match
    `want` whenever appmgr happened to be started from the app's install dir,
    declaring a corpse "ours and running". Hence the explicit empty guard.
    """
    want = os.path.realpath(paths.app_dir(app_id))
    cwd = _proc_cwd(pid)
    if cwd and os.path.realpath(cwd) == want:
        return True
    cmd = _proc_cmdline(pid)
    return bool(cmd) and app_id in cmd


def _pid_alive(pid: int) -> bool:
    """The pid still occupies a slot in the process table.

    NOTE: a ZOMBIE satisfies this -- kill(pid, 0) succeeds against an unreaped
    corpse. Use _pid_running() for "is it actually executing".
    """
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _pid_running(pid: int) -> bool:
    """Alive AND not a zombie. This is the liveness predicate callers want."""
    return _pid_alive(pid) and not _is_zombie(pid)


def is_running(app_id: str) -> Optional[int]:
    """Return the live pid if this app is running and the pid is really ours.

    A crashed-but-unreaped child (state Z) counts as NOT running: it no longer
    executes, and reporting `running: true` for a corpse would make the UI lie
    and make switch/activate take the "already running" branch.
    """
    with _RUN_RECORD_LOCK:
        pid = _read_pid(app_id)
        if pid is None or not _pid_running(pid):
            return None
        return pid if _is_ours(pid, app_id) else None


def has_run_record(app_id: str) -> bool:
    """Whether any persistent process identity file still exists.

    This intentionally checks directory entries rather than parsed values.  A
    malformed or partially committed record is still a teardown fence: an
    installer must ask :func:`stop` to clear it before renaming the app
    directory, otherwise the only persisted handle for a surviving process
    group would move to ``<id>.prev`` and become invisible.
    """
    if not paths.valid_app_id(app_id):
        raise SupervisorError(f"invalid app id {app_id!r}")
    with _RUN_RECORD_LOCK:
        return any(os.path.lexists(pathname) for pathname in (
            paths.pidfile(app_id), paths.pgidfile(app_id), paths.bootfile(app_id)))


def owned_pid_is_running(app_id: str, pid: Optional[int]) -> bool:
    """Recheck a captured leader after its run record has been removed."""
    if not paths.valid_app_id(app_id):
        raise SupervisorError(f"invalid app id {app_id!r}")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return False
    return _pid_running(pid) and _is_ours(pid, app_id)


# ---- child reaping + last-exit bookkeeping ---------------------------------- #
# appmgr IS the supervisor, so every app it launches is its direct child. Nobody
# ever called waitpid() on them, so a crashed app stayed in the process table as
# `[python] <defunct>` forever (observed on device: pid 4009, ppid 3741=appmgr),
# and the crash itself was completely silent -- only `ps` revealed it.
#
# Split of work, deliberately:
#   * the SIGCHLD handler polls only registered Popen objects, directly KILLs a
#     saved PGID after its leader exits, then queues the result -- no file I/O,
#     locks or waits. A lock would deadlock (the handler runs in the main thread
#     and could block on a lock that same thread holds); file I/O could re-enter
#     a half-written buffered stream.
#   * drain_exits() does the real work (persist last_exit.json, drop the stale
#     pidfile, log) from normal context -- read-only endpoints call these two
#     phases directly; destructive stale-record sweeping is confined to the
#     reconciler's cross-process busy gate.
# list.append / list.pop are single C-level ops, so the queue needs no lock.
#
# SIGCHLD (not polling) because the daemon otherwise sits in select() with no
# tick of its own: a poll loop would need a whole extra thread just to notice a
# crash that the kernel is already telling us about. The handler cost is one
# waitpid syscall per child death.
_reaped: List[tuple] = []


def reap_children() -> int:
    """Reap exited app leaders and immediately contain their process groups.

    ★Reaps ONLY registered app children★ (via Popen.poll(), the single reaper for
    each app pid), never waitpid(-1). A process-wide reap would steal the wait
    status of the helpers appmgr runs with subprocess.run() -- openssl, pip,
    gst-inspect, the pkill sweep -- turning their clean exit into a
    ChildProcessError that subprocess.run reports as returncode 0, so a failed
    runtime probe would silently read as success (健壮#17). Each reaped pid's
    returncode is queued (as a NEGATIVE signal number when killed) for
    drain_exits(); list.append is a single C op so the queue needs no lock.
    A production Popen carries the PGID and boot identity captured and persisted
    when this appmgr launched it.  Once its leader exits we can no longer ask the
    kernel ``getpgid(pid)``, but descendants may still retain camera/socket
    resources.  Kill that already-verified same-boot PGID before publishing the
    exit.  This uses no file I/O, printing or waits, so the Python SIGCHLD
    handler remains a small bounded operation.

    Returns the number of leaders reaped.
    """
    n = 0
    for pid, proc in list(_apps.items()):
        try:
            rc = proc.poll()           # reaps + caches returncode; None = alive
        except Exception:
            rc = None
        if rc is None:
            continue
        app_id = getattr(proc, "_appmgr_app_id", None)
        pgid = getattr(proc, "_appmgr_pgid", None)
        boot_id = getattr(proc, "_appmgr_boot_id", None)
        boot_verified = bool(getattr(proc, "_appmgr_boot_verified", False))
        contained = False
        if pgid is not None and boot_verified:
            # The app leader is gone; grace belongs to explicit stop(), not to an
            # orphaned runtime.  SIGKILL makes cleanup deterministic even when a
            # helper installed/ignored SIGTERM.
            contained = _killpg_id(pgid, signal.SIGKILL)
        _reaped.append((pid, rc, time.time(), app_id, pgid, boot_id,
                        contained, boot_verified))
        _apps.pop(pid, None)
        n += 1
    return n


def _sigchld(*_):           # pragma: no cover - trivial, exercised via reap_children
    reap_children()


def install_sigchld() -> bool:
    """Arm the SIGCHLD handler. Only possible from the main thread; returns False
    (rather than raising) elsewhere so importing/serving never breaks."""
    try:
        signal.signal(signal.SIGCHLD, _sigchld)
        return True
    except (ValueError, OSError, AttributeError):
        return False


def describe_status(status: int, at: float = None) -> dict:
    """POSIX wait status -> {"code", "signal", "at"}.

    Killed by a signal N is reported the shell/psutil way: code = -N, signal =
    its name (e.g. {"code": -11, "signal": "SIGSEGV"}).
    """
    ts = time.time() if at is None else at
    if os.WIFSIGNALED(status):
        sig = os.WTERMSIG(status)
        try:
            name = signal.Signals(sig).name
        except ValueError:
            name = f"SIG{sig}"
        return {"code": -sig, "signal": name, "at": ts}
    return {"code": os.WEXITSTATUS(status), "signal": None, "at": ts}


def describe_returncode(rc: Optional[int], at: float = None) -> dict:
    """Popen.returncode -> {"code", "signal", "at"}.

    Popen encodes a signal death as a NEGATIVE number (-N), which is exactly the
    shape describe_status() produced from a raw wait status, so the API field is
    unchanged: {"code": -11, "signal": "SIGSEGV"}. A normal exit is {"code": N,
    "signal": None}.
    """
    ts = time.time() if at is None else at
    if rc is not None and rc < 0:
        sig = -rc
        try:
            name = signal.Signals(sig).name
        except ValueError:
            name = f"SIG{sig}"
        return {"code": rc, "signal": name, "at": ts}
    return {"code": rc, "signal": None, "at": ts}


def _app_ids() -> List[str]:
    try:
        names = os.listdir(paths.APPS_DIR)
    except OSError:
        return []
    return [n for n in names
            if paths.valid_app_id(n) and os.path.isdir(paths.app_dir(n))]


def _app_for_pid(pid: int) -> Optional[str]:
    for app_id in _app_ids():
        if _read_pid(app_id) == pid:
            return app_id
    return None


def _write_exit(app_id: str, info: dict) -> None:
    d = paths.app_dir(app_id)
    if not os.path.isdir(d):           # uninstalled meanwhile -- nothing to record
        return
    try:
        with open(paths.exitfile(app_id), "w") as f:
            json.dump(info, f)
    except OSError:
        pass


def _clear_pidfile(app_id: str, pid: int = None) -> None:
    """Remove run.pid, but only if it still names `pid` (never clobber a restart)."""
    with _RUN_RECORD_LOCK:
        if pid is not None and _read_pid(app_id) != pid:
            return
        try:
            os.remove(paths.pidfile(app_id))
        except OSError:
            pass


def _clear_pgidfile(app_id: str, pgid: int = None) -> None:
    """Remove run.pgid, but only if it still names ``pgid``."""
    with _RUN_RECORD_LOCK:
        if pgid is not None and _read_pgid(app_id) != pgid:
            return
        try:
            os.remove(paths.pgidfile(app_id))
        except OSError:
            pass


def _clear_bootfile(app_id: str, boot_id: str = None) -> None:
    """Remove run.boot_id, guarded against clobbering a newer launch."""
    with _RUN_RECORD_LOCK:
        if boot_id is not None and _read_run_boot_id(app_id) != boot_id:
            return
        try:
            os.remove(paths.bootfile(app_id))
        except OSError:
            pass


def last_exit(app_id: str) -> Optional[dict]:
    """The app's most recent recorded process exit, or None if never recorded."""
    try:
        with open(paths.exitfile(app_id)) as f:
            info = json.load(f)
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) else None


def drain_exits() -> List[dict]:
    """Turn queued waitpid results into visible state. Normal context only."""
    out = []
    # Bound this pass to the events present on entry.  A trusted process group
    # that survived containment is requeued for a later retry; a while-loop
    # would immediately pop/requeue it forever.  Concurrently appended events
    # likewise remain for the next cheap drain pass.
    pending = len(_reaped)
    for _ in range(pending):
        try:
            event = _reaped.pop(0)
        except IndexError:             # concurrent drain
            break
        # Eight fields are emitted by current reap_children; accept historic
        # shapes because tests and an in-process upgrade may still have queued
        # one before this code was loaded.
        pid, rc, ts = event[:3]
        app_id = event[3] if len(event) >= 4 else None
        pgid = event[4] if len(event) >= 5 else None
        boot_id = event[5] if len(event) >= 7 else None
        contained = bool(event[6]) if len(event) >= 7 else False
        boot_verified = bool(event[7]) if len(event) >= 8 else False
        app_id = app_id or _app_for_pid(pid)
        if app_id is None:
            continue                   # not one of ours (stale queue entry)
        # A successfully-issued SIGKILL is not proof of process-group death
        # (for example, an uninterruptible D-state helper may remain).  Keep
        # both the exit event and the committed run identity until a future
        # retry observes the exact same-boot group as empty.
        if (boot_verified and pgid is not None
                and _pgid_alive(pgid)):
            _reaped.append(event)
            continue
        info = describe_returncode(rc, ts)
        info["pid"] = pid
        _write_exit(app_id, info)
        # Clear the three-file record only as a unit belonging to this leader.
        # A delayed exit event must not remove run.boot_id from a newer launch
        # in the same boot (the boot IDs intentionally match across launches).
        with _RUN_RECORD_LOCK:
            owns_run_record = (_read_pid(app_id) == pid)
            if owns_run_record:
                _clear_pidfile(app_id, pid)
                if pgid is not None:
                    _clear_pgidfile(app_id, pgid)
                if boot_id is not None:
                    _clear_bootfile(app_id, boot_id)
        out.append(dict(info, app=app_id))
        if contained:
            print("[appmgr] app leader %d exited; killed residual pgid %d"
                  % (pid, pgid), flush=True)
        # No auto-restart: a crash loop must not be hidden behind silent
        # respawns. The app stays stopped and the crash is now visible via
        # /list -> last_exit and this line in the appmgr log.
        print(f"[appmgr] app {app_id} (pid {pid}) exited: "
              f"code={info['code']} signal={info['signal']}", flush=True)
    return out


def sweep_stale() -> List[str]:
    """Drop stale run records, containing only verified same-boot groups.

    Covers the exits appmgr could NOT waitpid: an app started by a previous
    appmgr instance (or by the CLI) is re-parented to init when its starter goes
    away, so its death never reaches our SIGCHLD. Without this, a stale run.pid
    lingers -- harmless for is_running() (which re-validates the pid) but
    confusing in the logs and on disk.
    """
    with _RUN_RECORD_LOCK:
        cleared = []
        for app_id in _app_ids():
            pid = _read_pid(app_id)
            pgid = _run_pgid(app_id, pid)
            saved_boot = _read_run_boot_id(app_id)
            if pid is None and pgid is None and saved_boot is None:
                continue
            if pid is None:
                # run.pid is the transaction's commit marker.  PGID and boot
                # without it can be a writer paused between atomic renames --
                # including a writer in another CLI/appmgr process, beyond the
                # reach of _RUN_RECORD_LOCK.  A read/sweep path must neither
                # signal nor clear that uncommitted record.  Explicit stop (or
                # the next start's stale-record recovery) is busy-gated and can
                # safely contain a genuinely abandoned partial generation.
                continue
            leader_running = bool(pid and _pid_running(pid))
            if leader_running and _is_ours(pid, app_id):
                continue
            # A dead/zombie leader cannot be queried for its old process group.  The
            # separately persisted PGID reaches helpers only if run.boot_id proves
            # the number came from this kernel boot.  /userdata survives reboot and
            # PGIDs do not, so a legacy/missing/different boot ID is cleanup-only.
            # If the PID is alive but no longer ours, treat it as PID reuse and do
            # not signal regardless of the saved boot ID.
            same_boot = _same_boot_record(app_id)
            if pgid is not None and not leader_running and same_boot:
                _killpg_id(pgid, signal.SIGKILL)
                # Signal delivery is not containment proof.  Preserve the
                # complete same-boot retry identity while any helper (including
                # an uninterruptible D-state process) still occupies the group.
                if _pgid_alive(pgid):
                    continue
            elif pgid is not None and not leader_running:
                print("[appmgr] stale run for %s is not from the current boot; "
                      "clearing records without signalling pgid %d"
                      % (app_id, pgid), flush=True)
            _clear_pidfile(app_id, pid)
            # Unconditional here is intentional: this is the serialized stale-run
            # cleanup path and malformed/mismatched legacy values must not survive
            # after their commit record was removed.
            _clear_pgidfile(app_id)
            _clear_bootfile(app_id)
            cleared.append(app_id)
        return cleared


# Minimum spacing between stale-pidfile sweeps on the THROTTLED (read/poll) path.
# Only sweep_stale() is rate-limited -- see reap_and_sweep().
SWEEP_MIN_INTERVAL = float(os.environ.get("APPMGR_SWEEP_MIN_INTERVAL", "1.0"))
_last_sweep = 0.0


def reap_and_sweep(throttle_sweep: bool = False) -> dict:
    """One call for the read paths: reap, publish exits, clean stale pidfiles.

    `throttle_sweep=True` (used by the polled endpoints /list and /metrics) skips
    sweep_stale() when it ran less than SWEEP_MIN_INTERVAL ago. What is throttled
    and what is not, deliberately:

      * reap_children() + drain_exits() ALWAYS run. They are what make a crash
        visible -- drain_exits() writes last_exit.json and drops the dead run.pid
        -- and they are nearly free: one waitpid(WNOHANG) syscall, and a no-op
        when nothing was reaped. Rate-limiting these would make `last_exit` lag.
      * sweep_stale() is throttled. It walks every installed app, and for each
        one with a run.pid does /proc reads (state + cwd + cmdline). Its only
        effect is deleting a run.pid whose process is gone -- pure hygiene:
        is_running() re-validates every pid it reads, so a run.pid that survives
        one extra second never makes the API report a dead app as running.

    Mutating paths (stop(), which calls reap_children()/drain_exits() directly)
    are untouched by the throttle.
    """
    global _last_sweep
    reap_children()
    exits = drain_exits()
    if throttle_sweep:
        now = time.monotonic()
        if (now - _last_sweep) < SWEEP_MIN_INTERVAL:
            return {"exits": exits, "cleared": [], "swept": False}
        _last_sweep = now
    return {"exits": exits, "cleared": sweep_stale(), "swept": True}


# ---- launch command --------------------------------------------------------- #
def _resolve_interpreter(manifest: dict, app_id: str = None) -> str:
    """Pick the Python interpreter to launch this app under.

    A manifest MAY name a per-app interpreter via `interpreter` (or its alias
    `python`) -- e.g. voice-transcribe needs `/userdata/rknnenv/bin/python`
    because sherpa-onnx lives in that venv, not the system python. Absent the
    field, we use the appmgr's own interpreter (`sys.executable`, the system
    python), so every existing vision app keeps launching exactly as before.

    The path must be ABSOLUTE and exist on the device (it is app-author supplied
    but arrives inside a signature-verified manifest). A bad value is a hard
    error rather than a silent fallback, so misconfiguration surfaces at switch.
    """
    # In manifest v2, ``python`` is a closed dependency declaration object, not
    # an executable path.  The installer has already built and atomically
    # selected the matching immutable environment; launching with any other
    # interpreter would break the code/environment transaction.
    if manifest.get("manifest_version") == 2:
        effective_id = app_id or manifest.get("id")
        try:
            interp = pythonenv.current_python(effective_id)
        except (ValueError, pythonenv.PythonEnvError) as exc:
            raise SupervisorError(
                "cannot resolve manifest-v2 Python environment for %r: %s" %
                (effective_id, exc)) from exc
        if not interp:
            raise SupervisorError(
                "manifest-v2 app %r has no active Python environment" %
                effective_id)
        return interp

    interp = manifest.get("interpreter") or manifest.get("python")
    if not interp:
        return sys.executable
    if not isinstance(interp, str) or not os.path.isabs(interp):
        raise SupervisorError(f"manifest interpreter must be an absolute path: {interp!r}")
    if not os.path.exists(interp):
        raise SupervisorError(f"manifest interpreter not found on device: {interp!r}")
    return interp


def _build_cmd(app_id: str, manifest: dict) -> List[str]:
    """`<interp> <KIT_PARENT>/kit/run.py <app_dir>/<entry> [--model ...] [--sink ws --port N]`

    We launch through the kit's own entry point (kit/run.py) rather than exec'ing
    the entry file directly. kit.run derives KIT_PARENT from its OWN location and
    puts the app dir on sys.path, which is what let every app.py drop its ~40-line
    sys.path bootstrap (internal/KIT_APP_SHAPE_SPEC.md §5.1).

    run.py is invoked by ABSOLUTE PATH, not `-m kit.run`. `-m` has to resolve the
    `kit` package through PYTHONPATH first, so a wrong KIT_PARENT takes down every
    app at once with `ModuleNotFoundError: No module named 'kit'` -- which is
    exactly what happened on device when the bootstrap was removed while
    KIT_PARENT still pointed one level too deep. Running the file directly makes
    the launch self-locating: run.py recovers KIT_PARENT from `__file__`, so the
    apps come up even if PYTHONPATH is misconfigured (device-verified: works with
    PYTHONPATH unset entirely). PYTHONPATH is still exported below for the SDK.

    The entry is passed as an ABSOLUTE path on purpose: it embeds the app id, so
    `/proc/<pid>/cmdline` now names the app outright and _is_ours()'s cmdline
    fallback works even when /proc/<pid>/cwd is unreadable (it used to see only
    `python3 app.py --model models/x.rknn`, which identifies nothing).
    """
    entry = manifest.get("entry", "app.py")
    if ".." in entry.split("/") or entry.startswith("/"):
        raise SupervisorError(f"unsafe entry path {entry!r}")
    models = manifest.get("models") or []
    cmd = [_resolve_interpreter(manifest, app_id),
           os.path.join(paths.KIT_DIR, "run.py"),
           os.path.join(paths.app_dir(app_id), entry)]
    # CPU-only apps (e.g. qrcode-reader) declare no models[]; launch without
    # --model. Model-backed apps must still name a real file.
    if models:
        model_file = models[0].get("file")
        if not model_file:
            raise SupervisorError("manifest models[0] has no file")
        cmd += ["--model", model_file]
    # NOTE: conf/iou are intentionally NOT injected here. The app loads its
    # effective config (manifest config_schema defaults overlaid by
    # <app_dir>/config.json) itself via kit.config, so the user's saved config
    # wins. Passing --conf here would clobber config.json on every restart.
    out = manifest.get("output") or {}
    if out.get("sink") == "ws" and out.get("port"):
        cmd += ["--sink", "ws", "--port", str(out["port"])]
    return cmd


def _load_manifest(app_id: str) -> dict:
    mp = os.path.join(paths.app_dir(app_id), "manifest.json")
    with open(mp) as f:
        return json.load(f)


_NO_FRAME_STREAM = {"id": "", "kind": "none"}
_NATIVE_MAIN_STREAM = {"id": "main", "kind": "frame.sock", "path": "/live/0"}
_NATIVE_FRAME_SOCK = "/run/recamera/frame.sock"


def normalise_managed_frame_stream_contract(value: dict) -> dict:
    """Return a fresh, closed stream contract or the fail-closed ``none``.

    The contract is persisted with an exact app instance/generation.  Treat it
    as control-plane input even when it came from appmgr's state file: only the
    one platform route currently implemented by the supervisor is accepted.
    """
    if isinstance(value, dict) and value == _NATIVE_MAIN_STREAM:
        return dict(_NATIVE_MAIN_STREAM)
    return dict(_NO_FRAME_STREAM)


def managed_frame_stream_contract(resource_plan: dict) -> dict:
    """Compile the actual admitted launch plan into its native frame route.

    A manifest claim is authorization, not proof of the selected runtime
    backend.  ``resources.plan_manifest`` has already resolved conditional
    profiles against effective configuration before this helper is called.  A
    canonical camera reservation in that plan opts this generation into
    ``frame.sock``; missing or malformed plans fail closed.
    """
    requests = (resource_plan.get("requests")
                if isinstance(resource_plan, dict) else None)
    if isinstance(requests, list) and any(
            isinstance(request, dict)
            and request.get("resource") == "camera.frame:camera-0"
            for request in requests):
        return dict(_NATIVE_MAIN_STREAM)
    return dict(_NO_FRAME_STREAM)


def _build_env(app_id: str, manifest: dict, *, npu_managed: bool = False,
               npu_broker_required: bool = False,
               instance_id: Optional[str] = None,
               instance_generation: Optional[int] = None,
               result_gateway_sock: Optional[str] = None,
               frame_stream_contract: Optional[dict] = None,
               npu_mode: Optional[str] = None,
               inference_service_sock: Optional[str] = None) -> dict:
    """Environment handed to an app process.

    Split out of start() so it can be asserted without launching anything:
    the LD_LIBRARY_PATH hygiene below is security-relevant and a test that
    has to spawn a real process would not have been written.
    """
    env = dict(os.environ)
    # NPU routing markers are minted only by appmgr's transition paths. Never
    # inherit them from the appmgr service environment, and never add them for
    # direct supervisor.start callers.
    if npu_managed and npu_broker_required:
        raise SupervisorError("conflicting NPU launch route requested")
    env.pop("RECAMERA_NPU_MANAGED", None)
    env.pop("RECAMERA_NPU_BROKER_REQUIRED", None)
    env.pop("RECAMERA_NPU_LOCK", None)
    env.pop("RECAMERA_NPU_MODE", None)
    env.pop("RECAMERA_INFERENCE_SERVICE_SOCK", None)
    env.pop("RECAMERA_RESULT_GATEWAY_SOCK", None)
    env.pop("RECAMERA_RESULT_GATEWAY_REQUIRED", None)
    env.pop("RECAMERA_FRAME_SOURCE", None)
    env.pop("RECAMERA_FRAME_SOCK", None)
    # These broad/manual adapter switches can also select ResultSink and route
    # results directly to result-in.sock.  A managed child must instead keep
    # the authenticated Gateway route minted below; inherit neither value from
    # appmgr's service environment.  Manual processes retain the documented
    # opt-in behaviour because this scrub is local to the child environment.
    env.pop("RECAMERA_ADAPTER_PREFER", None)
    env.pop("RECAMERA_RESULT_OSD", None)
    env.pop("RECAMERA_APP_INSTANCE", None)
    env.pop("RECAMERA_APP_GENERATION", None)
    # Always canonicalise the app id for extension clients.  Previously every
    # Python process without a hand-written override announced itself as
    # ``python`` to the NPU broker.
    env["RECAMERA_APP_ID"] = app_id
    if normalise_managed_frame_stream_contract(
            frame_stream_contract)["kind"] == "frame.sock":
        # Dedicated frame-only opt-in: using the global adapter preference here
        # would also switch the result sink to result-in.sock and bypass the
        # managed Result Gateway / unified Result Hub.
        env["RECAMERA_FRAME_SOURCE"] = "official"
        env["RECAMERA_FRAME_SOCK"] = _NATIVE_FRAME_SOCK
    if npu_managed:
        env["RECAMERA_NPU_MANAGED"] = "appmgr-v1"
    if npu_broker_required:
        env["RECAMERA_NPU_BROKER_REQUIRED"] = "1"
    if instance_id:
        env["RECAMERA_APP_INSTANCE"] = str(instance_id)
    if instance_generation is not None:
        env["RECAMERA_APP_GENERATION"] = str(int(instance_generation))
    if result_gateway_sock:
        env["RECAMERA_RESULT_GATEWAY_SOCK"] = str(result_gateway_sock)
        # A managed app must not silently fall back to binding its own :8124;
        # a missing/incompatible gateway is a startup failure visible to
        # appmgr's READY transaction.
        env["RECAMERA_RESULT_GATEWAY_REQUIRED"] = "1"
    if npu_mode:
        env["RECAMERA_NPU_MODE"] = str(npu_mode)
    if inference_service_sock:
        env["RECAMERA_INFERENCE_SERVICE_SOCK"] = str(inference_service_sock)
    env["KIT_PARENT"] = paths.KIT_PARENT
    # kit.config resolves the user config under this root; export appmgr's own
    # value so the writer (appmgr) and the reader (the app) can never disagree.
    env["APPMGR_APPDATA_DIR"] = paths.APPDATA_DIR
    # Kit first, then the extension SDK python dir. recamera_ext lives in
    # /userdata/sdk/python (dropped there by the firmware install.sh) and is NOT
    # inside the rknnenv venv, so apps launched with the venv interpreter would
    # otherwise die on `ModuleNotFoundError: No module named 'recamera_ext'` --
    # which is exactly what a rollback.sh -> install.sh cycle exposes, since the
    # rollback wipes /userdata/rknnenv and the rebuilt venv only gets wheels.
    _pypath = [paths.KIT_PARENT, paths.SDK_PYTHON]
    if env.get("PYTHONPATH"):
        _pypath.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(_pypath)
    env["PYTHONUNBUFFERED"] = "1"
    # The extension API and the system OpenCV stack ship in /usr/lib.  RGA,
    # RKNN and some MPP libraries are OEM-only, so apps also need the OEM dirs.
    # Search order is critical: /oem/usr/lib contains older compatibility
    # copies of libfreetype/libpng.  Putting it first makes importing the system
    # cv2 load system libfontconfig against the old OEM FreeType and fail with
    # an undefined FT_Set_Var_Design_Coordinates symbol.  Keep /usr/lib first,
    # then the OEM-only locations, then any inherited paths.
    # Normalized, not just prepended. An inherited value picks up junk across
    # appmgr restarts (each deploy re-execs it from a shell that already had the
    # variable), and on device it was observed as
    #   /oem/usr/lib:/oem/lib:/oem/usr/lib:/oem/lib:...:/oem/lib:
    # -- four duplicate pairs and a TRAILING EMPTY SEGMENT. The duplicates are
    # only noise, but the empty segment is not: glibc (2.38 here) reads an empty
    # element of LD_LIBRARY_PATH as THE CURRENT DIRECTORY, so every app would
    # search its cwd for shared objects. Apps run with cwd=/userdata/local, which
    # is root-owned -- but /userdata itself is 0777, so any app that chdir'd into
    # a writable subdir would turn that empty element into a local library
    # injection point for uid 1000. Dropping empties costs nothing and closes it.
    _runtime_libs = ["/usr/lib", "/oem/usr/lib", "/oem/lib"]
    env["LD_LIBRARY_PATH"] = _join_pathlist(
        _runtime_libs + env.get("LD_LIBRARY_PATH", "").split(os.pathsep))
    # On-demand runtime environment (RUNTIME_BUNDLE_SPEC §3). A file-shaped
    # runtime (the RK hardware codec plugins) is useless once unpacked unless the
    # loader and GStreamer are told where to look, and that cannot be done for
    # every app: GST_PLUGIN_PATH on all nine vision apps would make an unrelated
    # plugin failure everyone's problem. So the variables go ONLY to apps whose
    # manifest declares the capability, and only when the runtime actually probes
    # present -- an app declaring `hwcodec` on a device without the bundle still
    # starts (and falls back to software decode) instead of being blocked here.
    # merge_env appends rather than assigns for the path variables: assigning
    # LD_LIBRARY_PATH would erase the ordered system/OEM base set above and
    # librockchip_mpp.so.1 would stop resolving.
    voiceruntime.apply_runtime_env(env, manifest.get("capabilities"))
    # Inject global MQTT/HA broker settings when enabled (app publishes WS+MQTT).
    # Empty dict when disabled -> app stays WS-only (unchanged behaviour).
    try:
        env.update(mqttcfg.env_for_launch())
    except Exception:
        pass

    return env


# ---- readiness handshake ---------------------------------------------------- #
def _clear_ready(app_id: str) -> None:
    try:
        os.remove(paths.readyfile(app_id))
    except OSError:
        pass


def _shift_log_generations(logpath: str, *, backups: int) -> None:
    """Rotate ``logpath`` -> ``.1`` -> ``.2`` -> ... -> ``.N`` (oldest dropped).

    Pure path-based generation shift with NO size check; the caller decides
    WHEN to rotate (the pump checks the cap, the pre-spawn path stats first).
    ``backups < 1`` deletes ``logpath`` outright.  Every step is best-effort: a
    missing file, a read-only tree, or a race with an uninstall must never raise
    -- a failed rotation only means the next chunk keeps appending to app.log.
    """
    if backups < 1:
        try:
            os.unlink(logpath)
        except OSError:
            pass
        return
    logdir = os.path.dirname(logpath)
    # Drop the oldest generation, then shift the survivors up by one.
    try:
        os.unlink(os.path.join(logdir, "app.log.%d" % backups))
    except OSError:
        pass
    for gen in range(backups - 1, 0, -1):
        src = os.path.join(logdir, "app.log.%d" % gen)
        dst = os.path.join(logdir, "app.log.%d" % (gen + 1))
        try:
            os.replace(src, dst)
        except OSError:
            pass
    try:
        os.replace(logpath, os.path.join(logdir, "app.log.1"))
    except OSError:
        pass


def _rotate_app_log(app_id: str, *, max_bytes: int, backups: int) -> None:
    """Pre-spawn rotation: shift the previous run's ``app.log`` out of the way
    before the pump reopens a fresh file.

    Size-gated so a small leftover log is left in place (the pump will rotate it
    live once it actually crosses the cap), preserving recent history across the
    restart instead of bumping every generation on every launch.  ``max_bytes<=0``
    disables rotation entirely.  Kept on the app_id-based signature the tests
    exercise; the live-rotation path uses ``_shift_log_generations`` directly.
    """
    if max_bytes <= 0:
        return
    logpath = os.path.join(paths.logdir(app_id), "app.log")
    try:
        if os.path.getsize(logpath) <= max_bytes:
            return
    except OSError:
        return
    _shift_log_generations(logpath, backups=backups)


def _write_log_chunk(logf, logpath: str, chunk: bytes, *, max_bytes: int,
                     backups: int):
    """Append ``chunk`` to ``logf``, rotating mid-chunk so app.log never exceeds
    ``max_bytes`` by more than the last chunk's worth of slack.

    Returns the (possibly re-opened) file object the caller must keep using.
    ``max_bytes<=0`` skips the cap entirely (unbounded append).
    """
    if max_bytes <= 0:
        logf.write(chunk)
        return logf
    offset = 0
    n = len(chunk)
    while offset < n:
        try:
            cur = os.path.getsize(logpath)
        except OSError:
            cur = 0
        room = max_bytes - cur
        if room <= 0:
            # Current file is full: rotate and reopen before continuing.
            try:
                logf.close()
            except OSError:
                pass
            _shift_log_generations(logpath, backups=backups)
            logf = open(logpath, "ab", buffering=0)
            continue
        take = min(room, n - offset)
        logf.write(chunk[offset:offset + take])
        offset += take
    return logf


def _app_log_pump(pipe_fd: int, logpath: str, *, max_bytes: int,
                  backups: int) -> None:
    """Drain the app's stdout/stderr pipe into ``app.log`` with live rotation.

    Runs in a daemon thread for the lifetime of ONE app run.  The child's
    stdout/stderr is wired to a pipe (NOT a direct file fd) precisely so this
    thread can rotate the log WHILE the app runs: the child no longer owns the
    app.log inode, so renaming the path reclaims it.  The pipe yields EOF once
    every writer -- the leader and any helper that inherited its stdout --
    has exited, so the pump drains the final buffered bytes and exits.

    A failure here (disk full, permission, vanished path) must never kill the
    supervised app or appmgr: it is swallowed and draining simply stops, leaving
    the app running with its output discarded.
    """
    logf = None
    try:
        pipe = os.fdopen(pipe_fd, "rb", buffering=0)
        try:
            logf = open(logpath, "ab", buffering=0)
            while True:
                chunk = pipe.read(16384)
                if not chunk:
                    break
                logf = _write_log_chunk(
                    logf, logpath, chunk, max_bytes=max_bytes, backups=backups)
        finally:
            try:
                pipe.close()
            except OSError:
                pass
    except Exception:
        # Best-effort drain: a broken log sink cannot cascade into appmgr.
        pass
    finally:
        if logf is not None:
            try:
                logf.close()
            except OSError:
                pass


def _join_log_pump(app_id: str, *, timeout: float = 2.0) -> None:
    """Best-effort flush of a run's final log bytes.

    Pops and joins the app's drain thread.  Must be called ONLY after the
    process group is dead (so the pipe has reached EOF); otherwise the join
    blocks up to ``timeout`` and then yields with the daemon pump still running.
    Never raises -- a lingering pump is harmless (it exits on its own once the
    pipe EOFs) and must not interfere with the lifecycle caller.
    """
    pump = _log_pumps.pop(app_id, None)
    if pump is None:
        return
    try:
        pump.join(timeout)
    except Exception:
        pass


def _read_log_file_tail(path: str, budget: int) -> bytes:
    """Last up to ``budget`` bytes of ``path``, or ``b""`` if absent/unreadable."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - budget), os.SEEK_SET)
            return f.read(budget)
    except OSError:
        return b""


def _log_tail(app_id: str, limit: int = 1500) -> str:
    """Last `limit` bytes of the app's CURRENT-RUN log (app.log only) -- the
    root cause a failed startup left behind (ImportError, `model not found`,
    bind refused).  Deliberately single-file so a startup-failure message never
    mixes in the previous run's rotated tail (app.log.1)."""
    return _read_log_file_tail(
        os.path.join(paths.logdir(app_id), "app.log"), limit
    ).decode("utf-8", "replace").strip()


def read_log_span(app_id: str, budget: int = 512 * 1024) -> bytes:
    """Last up to ``budget`` bytes of the app's log stream, spanning app.log
    AND, when the current app.log is smaller than ``budget``, the tail of
    app.log.1.

    For the web log view: a request right after a rotation would otherwise see
    only the few bytes written since the rotation; spanning the boundary keeps
    the window full.  If app.log is momentarily absent (the sub-ms rotation gap
    between rename and reopen) app.log.1 alone fills the budget.  Returns b""
    when no log exists yet.  ``_log_tail`` (startup-failure path) intentionally
    does NOT span, to avoid mixing runs.
    """
    data = _read_log_file_tail(
        os.path.join(paths.logdir(app_id), "app.log"), budget)
    if len(data) < budget:
        data = _read_log_file_tail(
            os.path.join(paths.logdir(app_id), "app.log.1"),
            budget - len(data)) + data
    return data


def _await_ready(proc: "subprocess.Popen", ready_path: str,
                 timeout: float) -> bool:
    """Block until the app signals READY, dies, or `timeout` elapses.

    Returns True the moment the readyfile appears (app reached its loop). Returns
    False if the process exits first (early death -- the failure modes core1
    targets) or the deadline passes (hung startup). The readyfile is checked
    BEFORE proc.poll() each turn so an app that signals and then exits still
    counts as started.
    """
    deadline = time.monotonic() + timeout
    while True:
        if os.path.exists(ready_path):
            return True
        if proc.poll() is not None:            # exited before signalling ready
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(_READY_POLL)


def _terminate_proc(app_id: str, proc: "subprocess.Popen", grace: float = 3.0,
                    before_force_kill: Optional[Callable[[], None]] = None) -> None:
    """Tear down a process group whose startup failed (TERM -> grace -> KILL).

    Kills the whole PGID (the app is a session leader; its ffmpeg children share
    the group), so a half-started app leaves no orphan frame source holding the
    camera. It drops the complete run record only after the same final trusted
    leader/PGID fence used by :func:`stop` proves the generation is gone."""
    pid = proc.pid
    pgid = (getattr(proc, "_appmgr_pgid", None)
            or _run_pgid(app_id, pid) or pid)
    force_fenced = False

    def fence_before_force_kill() -> None:
        nonlocal force_fenced
        if force_fenced:
            return
        force_fenced = True
        if before_force_kill is None:
            return
        try:
            before_force_kill()
        except BaseException as exc:
            # Startup containment is the final safety boundary.  A failed
            # authorization cleanup must never leave a TERM-ignoring group
            # alive, but keep the failure visible in the daemon log.
            print("[appmgr] startup force-fence failed for %s: %s: %s" %
                  (app_id, type(exc).__name__, exc), flush=True)
    if _pid_running(pid):
        _killpg_id(pgid, signal.SIGTERM)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            reap_children()
            if not _pgid_alive(pgid):
                break
            time.sleep(0.1)
        if _pgid_alive(pgid):
            fence_before_force_kill()
            _killpg_id(pgid, signal.SIGKILL)
            time.sleep(0.2)
    else:
        # The startup leader can exit after spawning a helper but before READY.
        # getpgid(pid) is already impossible here; use the value captured at
        # launch and persisted beside run.pid.
        fence_before_force_kill()
        _killpg_id(pgid, signal.SIGKILL)
    reap_children()
    _assert_process_fence_cleared(
        app_id, pid, pgid, leader_authenticated=True)
    drain_exits()
    # The group is now dead -> the pipe has EOF'd -> join the drain thread so
    # its final buffered bytes reach app.log before this run is forgotten.
    _join_log_pump(app_id)
    _clear_ready(app_id)
    _clear_pidfile(app_id, pid)
    _clear_pgidfile(app_id, pgid)
    _clear_bootfile(app_id, getattr(proc, "_appmgr_boot_id", None))


def _startup_failure(app_id: str, proc: "subprocess.Popen", timeout: float) -> str:
    """Human-readable root cause for a failed start(), with the app's log tail."""
    rc = proc.poll()
    if rc is not None:
        # Queue/contain the leader exit, but do not drain it here.  The caller
        # still has to run _terminate_proc's final trusted group fence; draining
        # first would erase the only retry identity when a helper survived.
        reap_children()
        info = describe_returncode(rc)
        base = (f"app {app_id!r} exited during startup "
                f"(code={info.get('code')}, signal={info.get('signal')})")
    else:
        base = f"app {app_id!r} did not signal ready within {timeout:g}s"
    # The pump drains the child's final stderr (the crash traceback) only after
    # the child exits and the pipe EOFs.  Best-effort: when the leader has
    # already exited (rc is not None) join briefly so _log_tail sees the cause
    # instead of stopping one read short; a still-live timeout case skips this
    # (a running app's pipe cannot be drained).  _terminate_proc re-joins later.
    if rc is not None:
        _join_log_pump(app_id, timeout=0.5)
    tail = _log_tail(app_id)
    return base + (f"; last log:\n{tail}" if tail else "")


# ---- public API ------------------------------------------------------------- #
def start(app_id: str, *, wait_ready: bool = True,
          ready_timeout: Optional[float] = None,
          npu_managed: bool = False,
          npu_broker_required: bool = False,
          instance_id: Optional[str] = None,
          instance_generation: Optional[int] = None,
          result_gateway_sock: Optional[str] = None,
          frame_stream_contract: Optional[dict] = None,
          npu_mode: Optional[str] = None,
          inference_service_sock: Optional[str] = None,
          on_spawn: Optional[Callable[[int], None]] = None,
          before_force_kill: Optional[Callable[[], None]] = None) -> int:
    """Launch an app and (by default) block until it signals READY.

    `wait_ready=True` gates success on the app reaching its main loop, so a
    caller (do_switch / do_activate / do_install) only commits `active` for a
    process that is actually up; a failure to come up raises SupervisorError with
    the root cause and leaves NO orphan process. Pass wait_ready=False for a
    fire-and-forget launch (kept for callers that manage readiness themselves).
    """
    if not paths.valid_app_id(app_id):
        raise SupervisorError(f"invalid app id {app_id!r}")
    d = paths.app_dir(app_id)
    if not os.path.isdir(d):
        raise SupervisorError(f"app not installed: {app_id}")

    existing = is_running(app_id)
    if existing:
        return existing

    manifest = _load_manifest(app_id)
    try:
        kitversion.check(manifest)
    except kitversion.KitIncompatible as exc:
        raise SupervisorError("kit compatibility check failed: %s" % exc) from exc

    # A previous leader may have died while a helper stayed in its process
    # group.  start_new_session makes a new leader safe from that old group, but
    # it would leave both generations consuming resources.  Reclaim a persisted
    # stale group before allocating anything for the new run.
    if (_read_pid(app_id) is not None or _read_pgid(app_id) is not None
            or _read_run_boot_id(app_id) is not None):
        stop(app_id, grace=0.0)

    boot_id = _current_boot_id()
    if boot_id is None:
        raise SupervisorError(
            "cannot establish current boot identity from %s; refusing to "
            "launch an app whose orphan PGID could not be validated" %
            BOOT_ID_PATH)

    cmd = _build_cmd(app_id, manifest)

    os.makedirs(paths.logdir(app_id), exist_ok=True)
    # Bound app.log across restarts: rotate the previous run's leftover out of
    # the way before the pump reopens a fresh file (see _rotate_app_log).
    _rotate_app_log(app_id, max_bytes=APP_LOG_MAX_BYTES, backups=APP_LOG_BACKUPS)
    logpath = os.path.join(paths.logdir(app_id), "app.log")
    # The child's stdout/stderr goes to a PIPE, not a direct file fd, so the
    # drain thread can rotate app.log WHILE the app runs without the child still
    # holding the old inode.  The parent must close write_fd right after Popen
    # or the pipe would never reach EOF when the child exits.
    read_fd, write_fd = os.pipe()

    env = _build_env(
        app_id, manifest, npu_managed=npu_managed,
        npu_broker_required=npu_broker_required, instance_id=instance_id,
        instance_generation=instance_generation,
        result_gateway_sock=result_gateway_sock,
        frame_stream_contract=frame_stream_contract, npu_mode=npu_mode,
        inference_service_sock=inference_service_sock)
    # READY handshake: clear any stale marker, then tell the app where to signal.
    ready_path = paths.readyfile(app_id)
    _clear_ready(app_id)
    env["APPMGR_READY_FILE"] = ready_path

    proc = subprocess.Popen(
        cmd,
        cwd=d,
        env=env,
        stdout=write_fd,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,     # setsid: child is session+group leader, pgid == pid
    )
    os.close(write_fd)             # parent must not hold the write end
    pump = threading.Thread(
        target=_app_log_pump,
        args=(read_fd, logpath),
        kwargs={"max_bytes": APP_LOG_MAX_BYTES, "backups": APP_LOG_BACKUPS},
        name="app-log-%s" % app_id,
        daemon=True,
    )
    _log_pumps[app_id] = pump
    pump.start()
    pgid = proc.pid                  # start_new_session => leader PID == PGID
    _register_child(proc, app_id=app_id, pgid=pgid, boot_id=boot_id)
    try:
        # Commit the kernel-backed run identity before publishing the matching
        # app/instance/generation record.  Read paths call is_running() while a
        # start operation is in flight; if state named the PID first, a reader
        # could observe the still-absent run.pid as a crash, revoke the fresh
        # generation and make every gateway hello fail.  With this order there
        # is no point at which state names a PID that the supervisor cannot yet
        # authenticate.  The child may reach its result sink between the two
        # commits, but GatewayResultSink retries and the gateway remains closed
        # until on_spawn publishes the exact generation.
        _write_run_ids(app_id, proc.pid, pgid, boot_id)
        if on_spawn is not None:
            on_spawn(proc.pid)
    except Exception as e:
        # A run we cannot identify persistently is not supervisable.  Tear the
        # whole group down before surfacing the storage error.
        _terminate_proc(app_id, proc, grace=0.0,
                        before_force_kill=before_force_kill)
        raise SupervisorError("failed to commit spawned run identity for %r: %s" %
                              (app_id, e)) from e

    if not wait_ready:
        return proc.pid

    timeout = READY_TIMEOUT if ready_timeout is None else ready_timeout
    if _await_ready(proc, ready_path, timeout):
        return proc.pid
    # Startup failed: capture the cause, then guarantee teardown (no orphan) so
    # the caller's transactional rollback starts from a clean slate.
    reason = _startup_failure(app_id, proc, timeout)
    _terminate_proc(app_id, proc,
                    before_force_kill=before_force_kill)
    raise SupervisorError(reason)


def reload(app_id: str) -> bool:
    """Hot-reload a running app's config via SIGHUP (DESIGN §3.2/§4).

    Sends SIGHUP to the app's MAIN pid ONLY -- never the process group -- so the
    kit App base loop re-reads config.json in place. ffmpeg children must NOT
    receive SIGHUP (default disposition would terminate them), which is exactly
    why we target the single pid rather than killpg.

    Returns True if the signal was delivered, False if the app is not running
    (in which case the caller has already persisted config.json and there is
    nothing to signal -- the new values apply on the next start).
    """
    if not paths.valid_app_id(app_id):
        raise SupervisorError(f"invalid app id {app_id!r}")
    pid = is_running(app_id)
    if pid is None:
        return False
    try:
        os.kill(pid, signal.SIGHUP)
        return True
    except ProcessLookupError:
        return False


def _killpg_id(pgid: int, sig: int) -> bool:
    """Signal an already-resolved numeric PGID; never re-query its leader PID."""
    if pgid is None or int(pgid) <= 1:
        return False
    try:
        os.killpg(int(pgid), sig)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return False


def _pgid_alive(pgid: int) -> bool:
    """Whether any process still occupies ``pgid`` (leader need not exist)."""
    if pgid is None or int(pgid) <= 1:
        return False
    try:
        os.killpg(int(pgid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _assert_process_fence_cleared(app_id: str, pid: Optional[int],
                                  pgid: Optional[int], *,
                                  leader_authenticated: bool) -> None:
    """Fail closed unless an authenticated launch fence is actually empty.

    Both explicit stop and failed-start containment use this exact boundary.
    Run records and queued exit events must remain intact until it succeeds so
    a later retry still has the trusted same-boot process-group identity.
    """
    deadline = time.monotonic() + max(0.0, PROCESS_FENCE_SETTLE_SEC)
    while True:
        leader_alive = bool(
            leader_authenticated and owned_pid_is_running(app_id, pid))
        group_alive = bool(pgid is not None and _pgid_alive(pgid))
        if not leader_alive and not group_alive:
            return
        if time.monotonic() >= deadline:
            raise ProcessFenceError(
                app_id, pid, pgid, leader_alive=leader_alive,
                group_alive=group_alive)
        # Reap direct leaders while waiting, but never drain their events here:
        # the caller may still need the same-boot PGID for a retry.
        reap_children()
        time.sleep(_PROCESS_FENCE_POLL_SEC)


def _owned_live_pgid(pid: int, app_id: str) -> Optional[int]:
    """Resolve a live app leader's group without trusting persisted numbers."""
    if not _pid_running(pid) or not _is_ours(pid, app_id):
        return None
    try:
        pgid = os.getpgid(pid)
    except OSError:
        return None
    # Every appmgr launch is a session/group leader.  Refuse a surprising group
    # rather than risk signalling the appmgr/ssh process group after tampering.
    if pgid != pid or pgid <= 1:
        print("[appmgr] refusing unexpected live process group for %s: "
              "pid=%s actual_pgid=%s" % (app_id, pid, pgid), flush=True)
        return None
    return pgid


def stop(app_id: str, grace: float = 5.0,
         before_force_kill: Optional[Callable[[], None]] = None) -> dict:
    """Stop one owned process group, preferring cooperative teardown.

    ``before_force_kill`` is an internal fail-closed hook used by the managed
    coordinator.  A normal TERM exit never calls it, leaving the process's
    inference authorization live long enough for ``App.finish()`` to unload its
    remote models.  Every path that is about to send SIGKILL calls it exactly
    once first, so a stuck/dead generation is fenced before forced containment.
    Hook failure is reported but can never prevent the kill.
    """
    if not paths.valid_app_id(app_id):
        raise SupervisorError(f"invalid app id {app_id!r}")
    with _RUN_RECORD_LOCK:
        pid = _read_pid(app_id)
        saved_pgid = _run_pgid(app_id, pid)
        saved_boot = _read_run_boot_id(app_id)
        current_boot = _current_boot_id()
        same_boot = bool(saved_boot and current_boot
                         and saved_boot == current_boot)
    result = {"app": app_id, "pid": pid, "pgid": saved_pgid,
              "boot_verified": same_boot,
              "signalled": False, "killed": False}
    force_fenced = False

    def fence_before_force_kill() -> None:
        nonlocal force_fenced
        if force_fenced:
            return
        force_fenced = True
        if before_force_kill is None:
            return
        try:
            before_force_kill()
        except BaseException as exc:
            # Containment is the final safety boundary.  Surface the hook
            # failure to the coordinator without leaving a TERM-ignoring group
            # alive merely because authorization cleanup itself failed.
            result["force_fence_error"] = "%s: %s" % (
                type(exc).__name__, exc)

    leader_running = bool(pid and _pid_running(pid))
    leader_ours = bool(leader_running and _is_ours(pid, app_id))

    # A live owned leader is introspectable, so use its actual group instead of
    # trusting /userdata.  A dead/zombie leader is not introspectable: its saved
    # numeric group may be addressed only when run.boot_id proves it belongs to
    # this boot.  A live PID that is not ours is reuse and is never signalled.
    live_pgid = _owned_live_pgid(pid, app_id) if leader_ours else None
    # Re-evaluate after /proc ownership/group inspection: the leader can exit
    # between those reads, and a prior appmgr instance would not be in _apps for
    # reap_children() to contain on our behalf.
    dead_now = not bool(pid and _pid_running(pid))
    # This is the only process-group identity safe to use for the final fence:
    # prefer the kernel-resolved group of a live owned leader.  Once the leader
    # is gone, a persisted numeric PGID is trustworthy only when its boot ID
    # matches the running kernel.
    fence_pgid = (live_pgid if live_pgid is not None else
                  (saved_pgid if dead_now and same_boot else None))
    if live_pgid is not None:
        result["pgid"] = live_pgid
        result["signalled"] = _killpg_id(live_pgid, signal.SIGTERM)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            reap_children()
            if not _pgid_alive(live_pgid):
                break
            time.sleep(0.2)
        if _pgid_alive(live_pgid):
            fence_before_force_kill()
            result["killed"] = _killpg_id(live_pgid, signal.SIGKILL)
            time.sleep(0.3)
    elif (leader_ours and dead_now
          and saved_pgid is not None and same_boot):
        # The leader raced out between /proc validation and getpgid().  Its
        # same-boot persisted group is still safe to contain immediately.
        fence_before_force_kill()
        result["killed"] = _killpg_id(saved_pgid, signal.SIGKILL)
    elif saved_pgid is not None and dead_now and same_boot:
        fence_before_force_kill()
        result["killed"] = _killpg_id(saved_pgid, signal.SIGKILL)
        if result["killed"]:
            print("[appmgr] %s leader is gone; killed persisted pgid %d"
                  % (app_id, saved_pgid), flush=True)
            time.sleep(0.05)
    elif saved_pgid is not None and dead_now:
        print("[appmgr] %s dead leader has an untrusted cross-boot/legacy "
              "pgid %d; clearing records without signalling"
              % (app_id, saved_pgid), flush=True)
    # Collect the leader corpse, but do not publish/drain its event yet:
    # drain_exits() clears the committed run identity.  First establish that
    # the exact leader/group authenticated above is truly gone.  In particular,
    # SIGKILL can report success while a D-state helper remains in the group.
    reap_children()
    try:
        _assert_process_fence_cleared(
            app_id, pid, fence_pgid, leader_authenticated=leader_ours)
    except ProcessFenceError as exc:
        result["fence_alive"] = {
            "leader": exc.leader_alive,
            "group": exc.group_alive,
        }
        raise

    # Only a proven-empty authenticated fence may retire its exit event and
    # durable identity records.
    drain_exits()
    # The process group is dead -> every pipe writer has gone -> the drain
    # thread sees EOF.  Join it so this app's final log bytes are flushed to
    # app.log before a subsequent start() might rotate the file away.
    _join_log_pump(app_id)

    # ★No global `pkill -x ffmpeg`★ (健壮#19 / P4). The app was launched with
    # start_new_session, so its pgid == its pid, and the ffmpeg the kit frame
    # source spawns runs WITHOUT setsid -> it inherits that same process group.
    # The killpg(pgid) above therefore already delivered TERM/KILL to ffmpeg;
    # a system-wide `pkill -x ffmpeg` added nothing for THIS app while killing
    # every unrelated ffmpeg on the box (another user's transcode, a debug pull).
    _clear_ready(app_id)
    with _RUN_RECORD_LOCK:
        _clear_pidfile(app_id, pid)
        _clear_pgidfile(app_id, saved_pgid)
        # Guard with the snapshot value: a delayed stop from this generation
        # must not remove a newer start's same-directory identity record.
        if saved_boot is not None:
            _clear_bootfile(app_id, saved_boot)
        elif _read_pid(app_id) is None and _read_pgid(app_id) is None:
            # Malformed/legacy boot records are cleanup-only, but only after
            # confirming that no replacement numeric record won the race.
            _clear_bootfile(app_id)
    return result

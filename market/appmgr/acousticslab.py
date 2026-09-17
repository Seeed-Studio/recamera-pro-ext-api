"""acousticslab.py -- system adapter for the firmware's AcousticsLab daemon.

The AcousticsLab sound-event-detection workbench (acousticslabd) is NOT an
appmgr-supervised process and NOT an installable package: the firmware ships
the daemon, its console and its init script.  This module exposes it to the
App Center as a second system application, mirroring ``builtin.py``'s
adapter pattern, without modifying the daemon itself:

  * lifecycle  -> /oem/usr/etc/init.d/S40acousticslabd start|stop|restart
                  (the script daemonizes and waits for the API/result sockets,
                  and gates its boot-time start on the persisted desired state
                  this adapter writes before invoking it);
  * status     -> read-only HTTP/JSON over the daemon's API unix socket
                  (``/api/v1/health``, ``/api/v1/status``, ``/api/v1/active``);
  * results    -> (wired separately) the daemon's protobuf result broadcast is
                  forwarded into the Result Hub as source ``acousticslab`` with
                  FRAME delivery -- never EVENT: vigil routes events straight to
                  recording, bypassing rule filters, while FRAME feeds
                  InferenceRuleSet's source/class/confidence filters + debounce.

Process-level lifecycle semantics: stopping stops the engine AND the console
(the daemon serves both); that is the only control the daemon exposes today.

stdlib only (http.client over AF_UNIX + subprocess) -- appmgr must not grow
third-party dependencies, and the daemon's wire contracts stay its own.
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import struct
import subprocess
import tempfile
import threading
import time
from typing import Optional

from . import paths


AL_ID = "acousticslab"
CONSOLE_PATH = "/extension/acousticslab/"

_API_SOCK = os.environ.get(
    "APPMGR_ACOUSTICSLAB_API_SOCK", "/dev/shm/acousticslabd-api.sock")
_INIT_SCRIPT = os.environ.get(
    "APPMGR_ACOUSTICSLAB_INIT", "/oem/usr/etc/init.d/S40acousticslabd")
_PIDFILE = os.environ.get(
    "APPMGR_ACOUSTICSLAB_PIDFILE", "/var/run/acousticslabd.pid")
_PROC_NAME = "acousticslabd"
_DESIRED_PATH = os.environ.get(
    "APPMGR_ACOUSTICSLAB_DESIRED",
    os.path.join(paths.APPMGR_DIR, "acousticslab.json"))
_LOG_DIR = os.environ.get(
    "APPMGR_ACOUSTICSLAB_LOG_DIR", "/userdata/.acousticslab/logs")
_LOG_PREFIX = "acousticslabd."
_LOG_SUFFIX = ".log"

_PROBE_TIMEOUT = 1.0      # status reads must never stall the app list
_LIFETIME_TIMEOUT = 25.0  # init script waits for sockets on start
_CONFIRM_TIMEOUT = 5.0
_CONFIRM_POLL = 0.1


class AcousticsLabError(Exception):
    """Transport, lifecycle or readback failure at the daemon boundary."""


# --------------------------------------------------------------------------- #
# low-level HTTP over the daemon's API unix socket (localhost equivalent)
# --------------------------------------------------------------------------- #
class _UdsConnection(http.client.HTTPConnection):
    def __init__(self, uds_path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._uds_path = uds_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._uds_path)
        self.sock = sock


def _request(method: str, path: str, *, timeout: float = _PROBE_TIMEOUT) -> dict:
    """One HTTP request against the API socket -> parsed JSON dict.

    Raises AcousticsLabError on transport failure, non-2xx, or non-JSON body.
    """
    conn = _UdsConnection(_API_SOCK, timeout)
    try:
        conn.request(method, path)
        resp = conn.getresponse()
        raw = resp.read()
    except (OSError, http.client.HTTPException) as e:
        raise AcousticsLabError("acousticslabd api %s %s -> transport error: %s"
                                % (method, path, e))
    finally:
        conn.close()
    if not 200 <= resp.status < 300:
        raise AcousticsLabError("acousticslabd api %s %s -> HTTP %d: %s"
                                % (method, path, resp.status, raw[:200]))
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise AcousticsLabError("acousticslabd api %s %s -> non-JSON body: %s"
                                % (method, path, e))
    if not isinstance(data, dict):
        raise AcousticsLabError("acousticslabd api %s %s -> unexpected body type"
                                % (method, path))
    return data


def health() -> bool:
    """Cheap liveness probe of the control plane."""
    try:
        _request("GET", "/api/v1/health")
        return True
    except AcousticsLabError:
        return False


def status() -> dict:
    """Raw /api/v1/status snapshot (subsystem heartbeats, host metrics)."""
    return _request("GET", "/api/v1/status")


def active_head() -> dict:
    """Raw /api/v1/active: the currently installed head and its labels.

    Keys of interest: ``labels`` (list[str]), ``runtime_head_id``, ``origin``
    (``"default"`` | ``"head"``), ``source_workspace_id`` /
    ``source_workspace_alive`` (head origin only; ``alive == False`` means the
    source workspace was deleted after activation -- inference keeps running
    from the published generation), ``n_classes``, ``activated_at``.
    """
    return _request("GET", "/api/v1/active")


def workspaces() -> dict:
    """Raw /api/v1/workspaces: ``{workspaces: [{id, name, ...}], ...}``."""
    return _request("GET", "/api/v1/workspaces")


def active_labels() -> list:
    """Labels of the currently active head; [] when unavailable."""
    try:
        labels = active_head().get("labels")
    except AcousticsLabError:
        return []
    if not isinstance(labels, list):
        return []
    return [str(label) for label in labels]


# --------------------------------------------------------------------------- #
# process observation (pidfile + /proc, mirrors the init script's own check)
# --------------------------------------------------------------------------- #
def process_pid() -> Optional[int]:
    """Live daemon pid from the pidfile, verified against /proc; else None."""
    try:
        with open(_PIDFILE, "r", encoding="ascii", errors="replace") as stream:
            pid = int(stream.read().strip())
    except (OSError, ValueError):
        return None
    if pid <= 1:
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    try:
        with open("/proc/%d/comm" % pid, "r", encoding="ascii",
                  errors="replace") as stream:
            if stream.read().strip() != _PROC_NAME:
                return None
    except OSError:
        return None
    return pid


def is_running() -> bool:
    return process_pid() is not None


def read_log_tail(max_bytes: int = 512 * 1024, tail: int = 300) -> list:
    """Tail of the daemon's own daily logs, oldest-to-newest lines.

    The daemon rotates one file per day under its workspace; span the two
    newest files so a request right after rotation still sees a full window
    (mirrors the app.log + app.log.1 span of supervised app logs). Read-only,
    symlink-free, bounded by ``max_bytes``.
    """
    try:
        names = sorted(
            entry.name for entry in os.scandir(_LOG_DIR)
            if entry.name.startswith(_LOG_PREFIX)
            and entry.name.endswith(_LOG_SUFFIX)
            and entry.is_file(follow_symlinks=False)
        )
    except OSError:
        return []
    chunks = []
    budget = max(1024, int(max_bytes))
    for name in reversed(names):
        if budget <= 0:
            break
        path = os.path.join(_LOG_DIR, name)
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as stream:
                if size > budget:
                    stream.seek(size - budget)
                data = stream.read(budget)
        except OSError:
            continue
        if size > budget and b"\n" in data:
            data = data.split(b"\n", 1)[1]     # drop the partial first line
        if not data:
            continue
        chunks.append(data)
        budget -= len(data)
        if data.count(b"\n") >= tail:
            break
    if not chunks:
        return []
    lines = b"\n".join(reversed(chunks)).decode("utf-8", "replace").splitlines()
    return lines[-max(1, int(tail)):]


# --------------------------------------------------------------------------- #
# persisted desired state (the card's "enabled" intent)
# --------------------------------------------------------------------------- #
_desired_lock = threading.Lock()


def desired_state() -> str:
    """Persisted intent: "running" (default; the daemon autostarts at boot) or
    "stopped". Unknown/corrupt content fails to the boot default."""
    with _desired_lock:
        try:
            with open(_DESIRED_PATH, "rb") as stream:
                data = json.loads(stream.read(64 * 1024).decode("utf-8"))
        except FileNotFoundError:
            return "running"
        except (OSError, ValueError):
            return "running"
    if isinstance(data, dict) and data.get("desired") in ("running", "stopped"):
        return data["desired"]
    return "running"


def _write_desired(desired: str) -> None:
    if desired not in ("running", "stopped"):
        raise ValueError("invalid desired state %r" % desired)
    parent = os.path.dirname(_DESIRED_PATH)
    os.makedirs(parent, mode=0o755, exist_ok=True)
    payload = (json.dumps({"version": 1, "desired": desired}) + "\n").encode()
    with _desired_lock:
        fd, temporary = tempfile.mkstemp(prefix=".acousticslab-", dir=parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, _DESIRED_PATH)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


# --------------------------------------------------------------------------- #
# lifecycle via the shipped init script (never a second supervisor)
# --------------------------------------------------------------------------- #
def _initctl(action: str) -> str:
    """Run the init script; return its combined output. Raises on failure."""
    try:
        proc = subprocess.run(
            [_INIT_SCRIPT, action], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=_LIFETIME_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise AcousticsLabError("init script %s %s failed to run: %s"
                                % (_INIT_SCRIPT, action, e))
    output = proc.stdout.decode("utf-8", "replace") if proc.stdout else ""
    if proc.returncode != 0:
        raise AcousticsLabError("init script %s %s exited %d: %s"
                                % (_INIT_SCRIPT, action, proc.returncode,
                                   output.strip()[:300]))
    return output


def _wait_for(predicate, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(_CONFIRM_POLL)
    raise AcousticsLabError("%s was not confirmed within %.1fs" % (what, timeout))


def start() -> dict:
    """Start the daemon and confirm the control plane answers.

    Desired intent is persisted BEFORE the init script runs: the script gates
    its boot-time start on that file, and the gate must not reject an explicit
    user-initiated start.
    """
    print("[appmgr] acousticslab start requested", flush=True)
    _write_desired("running")
    output = _initctl("start")
    try:
        _wait_for(lambda: is_running() and health(), _CONFIRM_TIMEOUT,
                  "acousticslabd start")
    except AcousticsLabError:
        if not is_running():
            raise
        # Process is up but the API probe is still failing: report honestly
        # instead of failing an operation whose effect already happened.
        print("[appmgr] acousticslabd running but api probe failed", flush=True)
    return {"started": True, "output": output.strip()[-300:]}


def stop() -> dict:
    """Stop the daemon (engine AND console) and confirm teardown."""
    print("[appmgr] acousticslab stop requested", flush=True)
    _write_desired("stopped")
    output = _initctl("stop")
    _wait_for(lambda: not is_running(), _CONFIRM_TIMEOUT, "acousticslabd stop")
    return {"stopped": True, "stop_confirmed": True,
            "output": output.strip()[-300:]}


def restart() -> dict:
    stopped = stop()
    started = start()
    return {"restarted": True, "stopped": stopped, "started": started}


def reconcile() -> None:
    """Boot-time desired-state restore, invoked once from appmgr startup.

    The OEM init script starts the daemon before appmgr; only an explicit
    "stopped" intent needs enforcement.  A "running" intent with a dead daemon
    is restored as well (e.g. the daemon died before appmgr restarted).
    Never raises: reconcile failure must not block appmgr service startup.
    """
    if not os.path.exists(_INIT_SCRIPT):
        return  # not an AcousticsLab-equipped platform (e.g. host tests)
    desired = desired_state()
    try:
        if desired == "stopped" and is_running():
            print("[appmgr] acousticslab reconcile: enforcing stopped", flush=True)
            stop()
        elif desired == "running" and not is_running():
            print("[appmgr] acousticslab reconcile: restoring running", flush=True)
            start()
    except AcousticsLabError as e:
        print("[appmgr] acousticslab reconcile failed (desired=%s): %s"
              % (desired, e), flush=True)


# Session cache of workspace id -> name. A head whose source workspace was
# deleted after activation (the daemon then reports source_workspace_alive
# false and the id is gone from /workspaces) can still display "<name>
# (detached)" -- mirroring the AcousticsLab console's own behavior.
_WORKSPACE_NAME_CACHE: dict = {}


def _resolve_workspace_name(origin, workspace_id, alive):
    """Best-effort source-workspace name for a head-origin active model.

    Live heads resolve against /workspaces (seeding the session cache);
    detached heads fall back to that cache. Lookup failures yield None --
    the name is cosmetic and must never block or fail the status snapshot.
    """
    if origin != "head" or not workspace_id:
        return None
    if alive is not False:
        try:
            listing = workspaces()
        except AcousticsLabError:
            listing = None
        if isinstance(listing, dict):
            entries = listing.get("workspaces")
            if isinstance(entries, list):
                for entry in entries:
                    if (isinstance(entry, dict)
                            and entry.get("id") and entry.get("name")):
                        _WORKSPACE_NAME_CACHE[str(entry["id"])] = \
                            str(entry["name"])
    return _WORKSPACE_NAME_CACHE.get(workspace_id)


# --------------------------------------------------------------------------- #
# observed status for the system card (never fabricates; fail to "unknown")
# --------------------------------------------------------------------------- #
def snapshot() -> dict:
    """One honest status snapshot for the App Center system card.

    ``running`` is process truth (pidfile + /proc); ``available`` is control
    plane truth.  Neither is inferred from the persisted desired intent.
    """
    result = {"available": False, "running": False, "enabled": None,
              "state": "unknown", "head": None, "uptime_s": None,
              "inference": None, "reason": None, "pid": None}
    desired = desired_state()
    result["enabled"] = desired == "running"

    pid = process_pid()
    result["running"] = pid is not None
    result["pid"] = pid
    if pid is None:
        result["state"] = "stopped"
        if desired == "running":
            result["reason"] = "enabled_but_stopped"
        return result

    try:
        data = status()
    except AcousticsLabError as e:
        result["state"] = "unknown"
        result["reason"] = str(e)
        return result

    result["available"] = True
    result["state"] = "running"
    uptime = data.get("uptime_s")
    if isinstance(uptime, (int, float)) and not isinstance(uptime, bool):
        result["uptime_s"] = max(0, int(uptime))
    subsystems = data.get("subsystems")
    if isinstance(subsystems, dict):
        inference = subsystems.get("inference")
        if isinstance(inference, dict):
            result["inference"] = {
                "healthy": bool(inference.get("healthy")),
                "detail": str(inference.get("detail") or ""),
                "stale": bool(inference.get("stale")),
            }

    try:
        head = active_head()
    except AcousticsLabError:
        head = None
    if isinstance(head, dict):
        labels = head.get("labels")
        origin = (str(head.get("origin"))
                  if head.get("origin") is not None else None)
        source_ws = (str(head.get("source_workspace_id"))
                     if head.get("source_workspace_id") is not None else None)
        alive_raw = head.get("source_workspace_alive")
        alive = alive_raw if isinstance(alive_raw, bool) else None
        result["head"] = {
            "id": (str(head.get("runtime_head_id"))
                   if head.get("runtime_head_id") is not None else None),
            "origin": origin,
            "n_classes": (head.get("n_classes")
                          if isinstance(head.get("n_classes"), int) else None),
            "labels": ([str(label) for label in labels]
                       if isinstance(labels, list) else []),
            "activated_at": (str(head.get("activated_at"))
                             if head.get("activated_at") is not None else None),
            "source_workspace_id": source_ws,
            "source_workspace_alive": alive,
            "workspace_name": _resolve_workspace_name(origin, source_ws, alive),
        }
    return result


# --------------------------------------------------------------------------- #
# result forwarding: daemon protobuf broadcast -> Result Hub system frames
# --------------------------------------------------------------------------- #
_RESULT_SOCK = os.environ.get(
    "APPMGR_ACOUSTICSLAB_RESULT_SOCK", "/dev/shm/acousticslabd-result.sock")

# Wire contract (pinned copy: vigil 3rdparty/acousticslab_proto, upstream
# acousticslab proto/). LE32-framed Envelope; InferenceFrame keeps field
# numbers 1-15 reserved for hot fields and only appends at 16+, so this
# minimal proto3 reader (varint + length-delimited + fixed32, unknown fields
# skipped) tracks additive changes. A wire-breaking renumber upstream must be
# mirrored here and in vigil's copy together.
_ENVELOPE_INFERENCE = 11
_FRAME_SEQ = 1
_FRAME_T_CAPTURE_MONOTONIC = 2
_FRAME_TOP_K = 4
_FRAME_HEAD_ID = 5
_FRAME_T_PUBLISH_UNIX = 7
_FRAME_HEAD_VERSION = 9
_TOPK_CLASS_IDX = 1
_TOPK_LABEL = 2
_TOPK_PROB = 3
_MAX_FRAME_BYTES = 64 * 1024


def _read_varint(buf: bytes, pos: int):
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ValueError("truncated varint")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 70:
            raise ValueError("varint overflow")


def _iter_fields(buf: bytes):
    """Yield (field_number, wire_type, value) for wire types 0/1/2/5."""
    pos = 0
    while pos < len(buf):
        key, pos = _read_varint(buf, pos)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = _read_varint(buf, pos)
            yield field, wire, value
        elif wire == 2:
            size, pos = _read_varint(buf, pos)
            if size > len(buf) - pos:
                raise ValueError("truncated length-delimited field")
            yield field, wire, buf[pos:pos + size]
            pos += size
        elif wire == 5:
            if len(buf) - pos < 4:
                raise ValueError("truncated fixed32")
            yield field, wire, struct.unpack_from("<f", buf, pos)[0]
            pos += 4
        elif wire == 1:
            if len(buf) - pos < 8:
                raise ValueError("truncated fixed64")
            yield field, wire, buf[pos:pos + 8]
            pos += 8
        else:
            raise ValueError("unsupported wire type %d" % wire)


def _decode_top_k(buf: bytes) -> dict:
    entry = {"class_idx": 0, "label": "", "prob": 0.0}
    for field, wire, value in _iter_fields(buf):
        if field == _TOPK_CLASS_IDX and wire == 0:
            entry["class_idx"] = value
        elif field == _TOPK_LABEL and wire == 2:
            entry["label"] = value.decode("utf-8", "replace")
        elif field == _TOPK_PROB and wire == 5:
            entry["prob"] = value
    return entry


def decode_envelope(buf: bytes) -> Optional[dict]:
    """Decode one Envelope; return the InferenceFrame dict or None when the
    payload variant is not inference (e.g. audio frames share the broadcast)."""
    for field, wire, value in _iter_fields(buf):
        if field != _ENVELOPE_INFERENCE or wire != 2:
            continue
        frame = {"seq": 0, "top_k": [], "head_id": None,
                 "t_us_capture_monotonic": None, "t_us_publish_unix": None,
                 "head_version": None}
        for f_field, f_wire, f_value in _iter_fields(value):
            if f_field == _FRAME_SEQ and f_wire == 0:
                frame["seq"] = f_value
            elif f_field == _FRAME_T_CAPTURE_MONOTONIC and f_wire == 0:
                frame["t_us_capture_monotonic"] = f_value
            elif f_field == _FRAME_TOP_K and f_wire == 2:
                frame["top_k"].append(_decode_top_k(f_value))
            elif f_field == _FRAME_HEAD_ID and f_wire == 2:
                frame["head_id"] = f_value.decode("utf-8", "replace")
            elif f_field == _FRAME_T_PUBLISH_UNIX and f_wire == 0:
                frame["t_us_publish_unix"] = f_value
            elif f_field == _FRAME_HEAD_VERSION and f_wire == 0:
                frame["head_version"] = f_value
        return frame
    return None


def frame_to_payload(frame: dict) -> dict:
    """Project one InferenceFrame onto the hub's system-result contract.

    Every hop is forwarded with its full top_k -- the adapter never prefilters:
    vigil's rules own class/confidence filtering, and only seeing non-matching
    frames lets their debounce decay.  FRAME delivery, never EVENT (an event
    would bypass the recording rules entirely).
    """
    publish_us = frame.get("t_us_publish_unix")
    capture_us = frame.get("t_us_capture_monotonic")
    entries = [{"score": max(0.0, min(1.0, float(item.get("prob") or 0.0))),
                "class_id": int(item.get("class_idx") or 0),
                "class_name": str(item.get("label") or "")}
               for item in frame.get("top_k") or []]
    payload = {
        "seq": int(frame.get("seq") or 0),
        "timestamp_ms": (int(publish_us) // 1000 if publish_us
                         else int(time.time() * 1000)),
        "pts_us": int(capture_us) if capture_us else 0,
        "task_type": 0,
        "task_type_name": "classification",
        "stream_id": AL_ID,
        "classification": {"entries": entries},
    }
    summary = {}
    if frame.get("head_id"):
        summary["head_id"] = frame["head_id"]
    if frame.get("head_version") is not None:
        summary["head_version"] = frame["head_version"]
    if summary:
        payload["summary"] = summary
    return payload


# --------------------------------------------------------------------------- #
# legacy notify injection: reuse the system notify-server distribution path
# --------------------------------------------------------------------------- #
_NOTIFY_SOCK = os.environ.get(
    "APPMGR_ACOUSTICSLAB_NOTIFY_SOCK", "/var/tmp/notify")
_NOTIFY_ENABLED = os.environ.get(
    "APPMGR_ACOUSTICSLAB_NOTIFY_LEGACY", "1").strip().lower() not in (
        "0", "false", "no")


def _enc_varint(value: int) -> bytes:
    out = bytearray()
    value = int(value) & 0xFFFFFFFFFFFFFFFF
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _enc_tag(field: int, wire: int) -> bytes:
    return _enc_varint((field << 3) | wire)


def _enc_bytes(field: int, payload: bytes) -> bytes:
    return _enc_tag(field, 2) + _enc_varint(len(payload)) + payload


def _encode_classification_entry(entry: dict) -> bytes:
    body = bytearray()
    score = entry.get("score")
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        body += _enc_tag(1, 5) + struct.pack("<f", float(score))
    class_id = entry.get("class_id")
    if isinstance(class_id, int) and not isinstance(class_id, bool):
        body += _enc_tag(2, 0) + _enc_varint(class_id)
    name = entry.get("class_name")
    if name:
        body += _enc_bytes(3, str(name).encode("utf-8"))
    return bytes(body)


def encode_legacy_classification(*, timestamp_ms: int, pts_us: int,
                                 source_id: str, entries: list) -> bytes:
    """Legacy ``InferenceResult`` (classification) for ``/var/tmp/notify``.

    Wire contract: recamera_ipc ``protocol/inference.proto`` (also pinned in
    docs/guide/result-push.md).  ``task_type`` is CLASSIFICATION(0), the
    proto3 default, so it never appears on the wire; ``model_id`` is left 0.
    """
    result = bytearray()
    if timestamp_ms:
        result += _enc_tag(2, 0) + _enc_varint(timestamp_ms)
    if source_id:
        result += _enc_bytes(4, source_id.encode("utf-8"))
    if pts_us:
        result += _enc_tag(5, 0) + _enc_varint(pts_us)
    classification = bytearray()
    for entry in entries:
        payload = _encode_classification_entry(entry)
        if payload:
            classification += _enc_bytes(1, payload)
    if classification:
        result += _enc_bytes(11, bytes(classification))
    return bytes(result)


class _LegacyNotifySink:
    """Best-effort injector for the notify-server legacy distribution path.

    ``/var/tmp/notify`` fans results out to the legacy WS and -- per the
    shared ``/userdata/config/notify.json`` -- MQTT/HTTP/UART.  Injection
    never blocks or fails the hub path: connect/send happen under a 1s
    timeout, errors only close the socket and back off.  The source stays
    ``acousticslab``: notify-server's strict hub publisher filters
    non-builtin sources, so legacy injection can never duplicate the
    canonical hub frames this forwarder already publishes.
    """

    _RETRY_S = 2.0

    def __init__(self, path: Optional[str] = None, connector=None):
        self._path = path or _NOTIFY_SOCK
        # Injectable for tests: (path) -> connected socket-like.
        self._connector = connector or self._connect
        self._sock = None
        self._next_try = 0.0
        self._lock = threading.Lock()
        self.injected = 0
        self.errors = 0

    @staticmethod
    def _connect(path: str):
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            conn.settimeout(1.0)
            conn.connect(path)
        except OSError:
            conn.close()
            raise
        # Keep the timeout for sends: a stalled notify-server must never
        # block the forwarder thread that also feeds the Result Hub.
        return conn

    def publish(self, frame: bytes) -> bool:
        if not _NOTIFY_ENABLED:
            return False
        with self._lock:
            if self._sock is None:
                now = time.monotonic()
                if now < self._next_try:
                    return False
                try:
                    self._sock = self._connector(self._path)
                except OSError:
                    self._next_try = now + self._RETRY_S
                    self.errors += 1
                    return False
            try:
                self._sock.sendall(struct.pack("<I", len(frame)) + frame)
            except OSError:
                self.errors += 1
                self._drop()
                self._next_try = time.monotonic() + self._RETRY_S
                return False
            self.injected += 1
            return True

    def _drop(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def close(self) -> None:
        with self._lock:
            self._drop()

    def status(self) -> dict:
        return {"enabled": _NOTIFY_ENABLED, "injected": self.injected,
                "errors": self.errors}


class ResultForwarder:
    """Subscribe the daemon's result broadcast and publish FRAME
    classification envelopes into the Result Hub as source ``acousticslab``.

    The forwarder self-gates on daemon availability: it reconnects with
    backoff while the daemon is down and registers/deregisters the bridge
    capability as the active head's labels appear/change/disappear.  A head
    swap (head_id+head_version pair changes mid-stream) re-registers the
    capability, which resets vigil's queued state for this source.
    """

    _INITIAL_BACKOFF = 0.1
    _MAX_BACKOFF = 5.0

    def __init__(self, *, publish, capability_changed,
                 result_sock: Optional[str] = None,
                 labels_provider=None, connector=None, notify_sink=None):
        self._publish = publish
        self._capability_changed = capability_changed
        self._result_sock = result_sock or _RESULT_SOCK
        self._labels_provider = labels_provider or active_labels
        # Injectable for tests: (path) -> connected socket-like with recv/close.
        self._connector = connector or self._connect_uds
        self._notify = notify_sink if notify_sink is not None else _LegacyNotifySink()
        self._stop = threading.Event()
        self._thread = None
        self._head_marker = None
        self._registered = False
        self._lock = threading.Lock()
        self._stats = {"published": 0, "decode_errors": 0, "reconnects": 0,
                       "capability_registrations": 0, "last_error": ""}

    def start(self) -> "ResultForwarder":
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, daemon=True, name="acousticslab-forwarder")
            self._thread.start()
        return self

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        self._deregister()
        self._notify.close()

    def status(self) -> dict:
        with self._lock:
            stats = dict(self._stats)
        stats["registered"] = self._registered
        stats["running"] = bool(self._thread and self._thread.is_alive())
        stats["notify"] = self._notify.status()
        return stats

    # -- capability lifecycle ------------------------------------------- #
    def _declaration(self, labels) -> Optional[dict]:
        if not labels:
            return None
        return {"version": 1, "signals": [{
            "id": "classification", "type": "classification",
            "classes": list(labels), "supports_roi": False}]}

    def _register(self) -> None:
        try:
            declaration = self._declaration(self._labels_provider())
        except Exception as e:
            # Fail closed: an unreadable active head means unknown classes,
            # and unknown classes must not keep recording authorization.
            with self._lock:
                self._stats["last_error"] = "labels: %s" % e
            declaration = None
        self._capability_changed(declaration)
        self._registered = declaration is not None
        with self._lock:
            self._stats["capability_registrations"] += 1

    def _deregister(self) -> None:
        if not self._registered:
            return
        try:
            self._capability_changed(None)
        except Exception:
            pass
        self._registered = False

    # -- connection loop ------------------------------------------------- #
    @staticmethod
    def _connect_uds(path: str):
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            conn.settimeout(1.0)
            conn.connect(path)
            conn.settimeout(None)
        except OSError:
            conn.close()
            raise
        return conn

    def _read_exact(self, conn, size: int) -> Optional[bytes]:
        data = bytearray()
        while len(data) < size:
            if self._stop.is_set():
                return None
            try:
                chunk = conn.recv(size - len(data))
            except (InterruptedError):
                continue
            if not chunk:
                return None
            data.extend(chunk)
        return bytes(data)

    def _consume(self, conn) -> None:
        while not self._stop.is_set():
            prefix = self._read_exact(conn, 4)
            if prefix is None:
                return
            frame_len = struct.unpack("<I", prefix)[0]
            if frame_len == 0 or frame_len > _MAX_FRAME_BYTES:
                raise ValueError("invalid frame length %d" % frame_len)
            body = self._read_exact(conn, frame_len)
            if body is None:
                return
            try:
                frame = decode_envelope(body)
            except ValueError:
                with self._lock:
                    self._stats["decode_errors"] += 1
                continue
            if frame is None:
                continue
            marker = (frame.get("head_id"), frame.get("head_version"))
            if marker != self._head_marker:
                self._head_marker = marker
                self._register()
            try:
                self._publish(frame_to_payload(frame),
                              {"id": AL_ID, "trust": "in-process"})
                with self._lock:
                    self._stats["published"] += 1
            except Exception as e:
                with self._lock:
                    self._stats["last_error"] = "publish: %s" % e
            self._notify_legacy(frame_to_payload(frame))

    def _notify_legacy(self, payload: dict) -> None:
        """Mirror one hub payload onto the legacy notify distribution path.

        Fully independent of the hub publish result: encoding or socket
        failures only surface in stats, never in ``last_error`` semantics
        that operators read as hub health.
        """
        try:
            body = encode_legacy_classification(
                timestamp_ms=payload.get("timestamp_ms") or 0,
                pts_us=payload.get("pts_us") or 0,
                source_id=AL_ID,
                entries=(payload.get("classification") or {}).get("entries")
                or [])
        except (TypeError, ValueError, struct.error) as e:
            with self._lock:
                self._stats["last_error"] = "notify encode: %s" % e
            return
        self._notify.publish(body)

    def _run(self) -> None:
        backoff = self._INITIAL_BACKOFF
        while not self._stop.is_set():
            try:
                conn = self._connector(self._result_sock)
            except OSError:
                conn = None
            if conn is not None:
                if not self._registered:
                    self._register()
                try:
                    self._consume(conn)
                    backoff = self._INITIAL_BACKOFF
                except (OSError, ValueError) as e:
                    with self._lock:
                        self._stats["last_error"] = "stream: %s" % e
                finally:
                    self._head_marker = None
                    self._deregister()
                    try:
                        conn.close()
                    except OSError:
                        pass
            with self._lock:
                self._stats["reconnects"] += 1
            self._stop.wait(backoff)
            backoff = min(backoff * 2, self._MAX_BACKOFF)

# --------------------------------------------------------------------------- #
# internal system-app descriptor (bundled with appmgr, never downloaded)
# --------------------------------------------------------------------------- #
def manifest() -> dict:
    """Describe the AcousticsLab system application.

    This is not an installable application manifest and never enters the
    package/signature pipeline.  There is deliberately no ``config_schema``:
    datasets, training, model activation and inference tuning all live in the
    daemon's own console.
    """
    return {
        "id": AL_ID,
        "name": "AcousticsLab",
        "name_zh": "AcousticsLab 声学实验室",
        "type": "system",
        "scene": "system",
        "scene_zh": "系统",
        "version": "firmware",
        "author": "reCamera (firmware)",
        "description": "A local audio classification workbench "
                       "(acousticslabd): manage sound datasets, fine-tune "
                       "models and preview real-time audio inference from its "
                       "own console. Results are published as the "
                       "\"acousticslab\" recording source and can drive event "
                       "recording.",
        "description_zh": "本地声音分类工作台（acousticslabd）：管理声音数据集、"
                          "微调模型并预览实时音频推理。检测结果以 "
                          "\"acousticslab\" 录制源发布，可驱动事件录像。",
        "console": {"path": CONSOLE_PATH},
    }

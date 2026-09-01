"""
ResultSink adapter for reCamera Pro (Rockchip RV1126B).

L0 adapter layer (see docs/guide/kit-design.md §0 / §0.5). The application never
touches a sink directly -- the `App` base class owns one and calls `emit()` for
every processed frame. The concrete backend is swappable behind the `ResultSink`
ABC:

* `StdoutSink`   -- prints one JSON line per frame. Zero deps, debug/CI use.
* `WsResultSink` -- broadcasts the structured per-frame JSON to every connected
                    WebSocket client on a local port, for the `/appcenter`
                    overlay panel to subscribe to and draw boxes on top of the
                    go2rtc preview. Pure stdlib (socket + threading + hashlib);
                    a hand-rolled RFC6455 server frames text messages itself, so
                    NO `websockets`/`aiohttp`/etc. dependency is pulled in.
* `GatewayResultSink` -- appmgr-managed publisher.  It sends NDJSON to one Unix
                         socket; appmgr owns the sole WebSocket listener, so
                         multiple applications never contend for :8124.

Why our own lightweight WS (not the official :8123)
---------------------------------------------------
Per §0.5 the official :8123 result stream belongs to rkipc's own inference and
we must not squat on it. Our self-hosted apps emit their results on a separate
port we own (default 8124). When the official OSD/RGN injection interface lands,
a new `OsdInjectSink` implementation drops in behind this same ABC and the
capability registry selects it -- application code does not change.

Wire format (one JSON object per frame, newline-free, one WS text message):
    {
      "type": "results",
      "app": "<app-id>",
      "pts": 12345.678,          # frame capture timestamp (monotonic seconds)
      "seq": 42,                 # monotonic frame counter
      "results": [ {box,cls,cls_name,score}, ... ],   # raw detections
      "events":  [ ... ]         # app-level events from on_results()
    }
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import socket
import struct
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, List, Optional

from kit.errors import AdapterError
from kit.diagnostics import get_logger


log = get_logger("result.sink")

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# --------------------------------------------------------------------------- #
# WS server hardening knobs (C9 auth-surface / C10 slow-client DoS)
#
# Defaults are conservative and env-overridable. They bound three things a
# hostile or merely slow WS client could otherwise do: (1) reach the result
# stream at all (bind host), (2) starve fds by opening many connections
# (client/per-ip caps), (3) freeze the INFERENCE thread by not reading (per-
# client queue + background writer + drop-oldest + laggard disconnect).
# --------------------------------------------------------------------------- #
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


WS_MAX_CLIENTS = _env_int("RECAMERA_WS_MAX_CLIENTS", 32)
WS_MAX_PER_IP = _env_int("RECAMERA_WS_MAX_PER_IP", 8)
WS_CLIENT_QUEUE = _env_int("RECAMERA_WS_CLIENT_QUEUE", 64)
WS_SEND_TIMEOUT = _env_float("RECAMERA_WS_SEND_TIMEOUT", 2.0)
# Consecutive frames dropped for one client before we give up on it. At 30 fps a
# 64-deep queue plus this many drops is ~6 s of not reading -> that client is
# broken, not just briefly busy; disconnect it so it stops wasting a slot.
WS_LAG_LIMIT = _env_int("RECAMERA_WS_LAG_LIMIT", 128)


def effective_bind_host(host: Optional[str]) -> str:
    """Resolve the requested bind host, DEFAULTING TO LOOPBACK (C9).

    The result stream is published behind the nginx JWT edge, which reverse-
    proxies /appcenter/ws/results to 127.0.0.1:<port>. Binding loopback means
    only nginx (already authenticated) and root can reach the raw port -- a LAN
    peer cannot open an unauthenticated subscription. None/""/localhost/
    loopback/local all resolve to 127.0.0.1. An explicit routable address
    (e.g. "0.0.0.0") is honoured for the documented LAN-direct case, but that
    exposes UNAUTHENTICATED results and must be opted into on purpose.
    """
    h = (host or "").strip().lower()
    if h in ("", "127.0.0.1", "::1", "localhost", "loopback", "local"):
        return "127.0.0.1"
    return (host or "").strip()


def offer_latest_wins(q: "queue.Queue", item) -> bool:
    """Put `item` on a bounded queue, dropping the OLDEST if full (latest-wins).

    Returns True if an old item had to be dropped to make room. A live-video
    overlay wants the freshest frame, never a backlog, so a slow reader loses
    stale frames instead of the producer blocking (C10)."""
    try:
        q.put_nowait(item)
        return False
    except queue.Full:
        dropped = False
        try:
            q.get_nowait()
            dropped = True
        except queue.Empty:
            pass
        try:
            q.put_nowait(item)
        except queue.Full:
            pass
        return dropped


def admit_reason(n_total: int, n_from_ip: int,
                 max_clients: int, max_per_ip: int) -> Optional[str]:
    """Return None if a new client may be admitted, else a short refusal reason.

    Two independent caps (C10): a global fd budget, and a per-IP cap so one peer
    cannot consume the whole budget by itself."""
    if max_clients and n_total >= max_clients:
        return f"server client limit reached ({max_clients})"
    if max_per_ip and n_from_ip >= max_per_ip:
        return f"per-ip client limit reached ({max_per_ip})"
    return None


class _WsClient:
    """One connected WS subscriber: a bounded latest-wins queue drained by a
    dedicated background writer thread, so the inference thread that calls
    `offer()` never blocks on a slow/dead socket (C10).

    A client that stays full for WS_LAG_LIMIT consecutive frames is too far
    behind to be useful and is disconnected, freeing its slot.
    """

    def __init__(self, conn: socket.socket, ip: str, *, maxq: int,
                 send_timeout: float, lag_limit: int):
        self.conn = conn
        self.ip = ip
        self._q: "queue.Queue" = queue.Queue(maxsize=max(1, maxq))
        self._send_timeout = send_timeout
        self._lag_limit = lag_limit
        self._drops = 0
        self._alive = threading.Event()
        self._alive.set()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def alive(self) -> bool:
        return self._alive.is_set()

    def offer(self, frame: bytes) -> None:
        """Enqueue one frame (latest-wins); disconnect a persistently-behind
        client. Never blocks -- this runs on the inference thread."""
        if not self._alive.is_set():
            return
        if offer_latest_wins(self._q, frame):
            self._drops += 1
            if self._drops > self._lag_limit:
                self.close()
        else:
            self._drops = 0

    def _run(self) -> None:
        while self._alive.is_set():
            try:
                frame = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            if frame is None:            # close sentinel
                break
            try:
                self.conn.settimeout(self._send_timeout)
                self.conn.sendall(frame)
            except Exception:
                break                    # dead/too-slow socket: drop the client
        self._alive.clear()
        try:
            self.conn.close()
        except Exception:
            pass

    def close(self) -> None:
        if self._alive.is_set():
            self._alive.clear()
            try:
                self._q.put_nowait(None)  # wake the writer so it exits promptly
            except queue.Full:
                pass


class ResultSink(ABC):
    """Abstract result publisher. The App base class owns one; apps never call it."""

    @abstractmethod
    def emit(self, payload: dict, pts: float) -> None:
        """Publish one frame's structured result payload."""
        raise NotImplementedError

    def emit_meta(self, payload: dict) -> None:
        """Publish an out-of-band meta message NOT tied to a frame's results
        (e.g. the periodic pipeline `metrics` event: FPS + per-stage latency).

        Default: no-op. Only sinks whose audience wants live telemetry override
        it -- WsResultSink broadcasts it to the /appcenter debug panel, while
        MqttSink deliberately ignores it (an empty HA state doc every second is
        noise). This keeps metrics strictly additive: existing results/events/
        MQTT behaviour is untouched.
        """
        pass

    def emit_checked(self, payload: dict, pts: float) -> None:
        """Publish and surface synchronous acceptance failures.

        Historical :meth:`emit` implementations are intentionally best-effort
        so one telemetry/output backend cannot stop an inference loop.  Typed
        callers that must not mistake a local rejection for success use this
        additive method.  The default delegates to ``emit``; sinks that hide
        native/fan-out failures override it.
        """

        self.emit(payload, pts)

    def set_frame_size(self, w: int, h: int) -> None:
        """Tell the sink the current frame's pixel dimensions.

        The App base loop calls this once per frame BEFORE emit(). Only sinks
        that must convert pixel coordinates need it: OfficialResultSink divides
        box/keypoint pixel coords by (w, h) to get the normalized [0,1] fractions
        the extension-API OSD renderer requires. Default: no-op, so the
        WS/stdout/MQTT workaround sinks (which keep their own pixel/JSON
        convention and wire format) are completely unaffected.
        """
        pass

    def set_frame_size_checked(self, w: int, h: int) -> None:
        """Set geometry while surfacing synchronous backend failures."""

        self.set_frame_size(w, h)

    def on_config_reload(self, config: dict) -> None:
        """Live-apply a config change (SIGHUP) to this sink.

        `App._maybe_reload` calls this on the app's sink with the freshly
        re-read effective config, so a sink whose behaviour is config-driven
        (ConfigurableSink: output filters / formatter template) picks up
        apply:"live" changes without a restart -- for EVERY app, whatever loop
        shape it uses. Default: no-op; sinks with no config react to nothing.
        """
        pass

    def stats(self) -> dict:
        """Best-effort send diagnostics for this sink (default: empty).

        Sinks that can count what they published override this so an app (or a
        health probe) can read cumulative counters -- e.g. OfficialResultSink
        surfaces the SDK's local `sent`/`oversize_rejected`/`send_error` tallies.
        Local counters only: a frame accepted locally that the server later
        drops is not reflected here until the server-ACK protocol lands
        (docs/guide/result-push.md)."""
        return {}

    def close(self) -> None:  # optional override
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ``ResultSink`` is retained for all existing imports.  ``ResultPublisher`` is
# the less ambiguous public name: the low-level native package also has a
# ``recamera_ext.ResultSink`` with a different, typed send_* API.
ResultPublisher = ResultSink


class StdoutSink(ResultSink):
    """Debug sink: print one compact JSON line per frame to stdout."""

    def __init__(self, only_nonempty: bool = False):
        self.only_nonempty = only_nonempty

    def emit(self, payload: dict, pts: float) -> None:
        if self.only_nonempty and not payload.get("results") and not payload.get("events"):
            return
        print(json.dumps(payload, separators=(",", ":")), flush=True)

    def emit_meta(self, payload: dict) -> None:
        print(json.dumps(payload, separators=(",", ":")), flush=True)


def _ws_encode_text(data: bytes) -> bytes:
    """Encode a single unmasked server->client WebSocket text frame (FIN=1)."""
    header = bytearray([0x81])  # FIN + opcode 0x1 (text)
    n = len(data)
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126)
        header += struct.pack(">H", n)
    else:
        header.append(127)
        header += struct.pack(">Q", n)
    return bytes(header) + data


class WsResultSink(ResultSink):
    """
    Minimal broadcast WebSocket server, stdlib only.

    Runs a background accept loop; each accepted client is upgraded (RFC6455
    handshake) and added to a broadcast set. `emit()` serialises the payload
    once and best-effort sends it to every client, dropping any that error.
    Slow/dead clients never block the inference loop.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 8124,
                 app_id: str = "app", preserve_envelope: bool = False,
                 *, max_clients: int = WS_MAX_CLIENTS,
                 max_per_ip: int = WS_MAX_PER_IP,
                 client_queue: int = WS_CLIENT_QUEUE,
                 send_timeout: float = WS_SEND_TIMEOUT,
                 lag_limit: int = WS_LAG_LIMIT):
        # Default binds LOOPBACK (C9): the front end reaches this stream through
        # nginx (proxy_pass -> 127.0.0.1:<port> at /appcenter/ws/results), so a
        # loopback bind is fully reachable by the JWT-authenticated edge while a
        # LAN peer cannot open an unauthenticated subscription. Passing an
        # explicit "0.0.0.0" opts into LAN-direct exposure on purpose.
        self.host = effective_bind_host(host)
        self.port = port
        self.app_id = app_id
        self._max_clients = max_clients
        self._max_per_ip = max_per_ip
        self._client_queue = client_queue
        self._send_timeout = send_timeout
        self._lag_limit = lag_limit
        # When True, emit()/publish_envelope() broadcast the payload verbatim
        # (no self-generated type/app/pts/seq/frame). ConfigurableSink's
        # WsChannel sets this so the canonical envelope built ONCE upstream is
        # not stamped a second time here. Default False keeps the standalone
        # WsResultSink behaviour (legacy overlay path) byte-for-byte unchanged.
        self.preserve_envelope = preserve_envelope
        self._clients: List[_WsClient] = []
        self._lock = threading.Lock()
        self._srv: Optional[socket.socket] = None
        self._accept_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._seq = 0
        # Current inference-frame pixel size (base loop calls set_frame_size once
        # per frame BEFORE emit). Emitted as `frame:{width,height}` so the
        # /appcenter overlay knows the coordinate reference space the box/keypoint
        # PIXELS live in -- without it the panel guesses (default 640x480) and
        # every coordinate is scaled wrong. None until the first frame is seen.
        self._frame_w: Optional[int] = None
        self._frame_h: Optional[int] = None
        self._start()

    def set_frame_size(self, w: int, h: int) -> None:
        """Record the current inference-frame pixel size (base loop calls this
        per frame). Unlike OfficialResultSink -- which DIVIDES coords by this to
        normalize -- WsResultSink keeps pixel coords verbatim and just ANNOUNCES
        the reference size in the wire message so the overlay maps 1:1."""
        if w and h and w > 0 and h > 0:
            self._frame_w = int(w)
            self._frame_h = int(h)

    # -- lifecycle -------------------------------------------------------- #
    def _start(self) -> None:
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind((self.host, self.port))
        # Reflect the actually-bound port (matters when port=0 is passed for an
        # OS-assigned ephemeral port, e.g. in tests).
        try:
            self.port = self._srv.getsockname()[1]
        except OSError:
            pass
        self._srv.listen(8)
        self._srv.settimeout(0.5)
        self._accept_thread = threading.Thread(target=self._accept_loop,
                                               daemon=True)
        self._accept_thread.start()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, addr = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            ip = addr[0] if addr else "?"
            # Admission cap BEFORE the handshake: reject cheaply so a flood of
            # connections cannot exhaust fds or the client budget (C10). Reap any
            # dead clients first so their slots are reclaimed.
            with self._lock:
                self._clients = [c for c in self._clients if c.alive()]
                n_total = len(self._clients)
                n_ip = sum(1 for c in self._clients if c.ip == ip)
            reason = admit_reason(n_total, n_ip, self._max_clients,
                                  self._max_per_ip)
            if reason is not None:
                try:
                    conn.close()
                except Exception:
                    pass
                continue
            try:
                self._handshake(conn)
                client = _WsClient(conn, ip, maxq=self._client_queue,
                                   send_timeout=self._send_timeout,
                                   lag_limit=self._lag_limit)
                with self._lock:
                    self._clients.append(client)
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass

    @staticmethod
    def _handshake(conn: socket.socket) -> None:
        conn.settimeout(5)
        req = b""
        while b"\r\n\r\n" not in req:
            chunk = conn.recv(1024)
            if not chunk:
                raise ConnectionError("client closed during handshake")
            req += chunk
            if len(req) > 65536:
                raise ValueError("handshake too large")
        key = None
        for line in req.split(b"\r\n"):
            if line.lower().startswith(b"sec-websocket-key:"):
                key = line.split(b":", 1)[1].strip()
                break
        if not key:
            raise ValueError("no Sec-WebSocket-Key")
        accept = base64.b64encode(
            hashlib.sha1(key + _WS_GUID.encode()).digest()
        )
        resp = (
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n"
        )
        conn.sendall(resp)

    # -- ResultSink ------------------------------------------------------- #
    def _broadcast(self, obj: dict) -> None:
        """Serialise one JSON object once and OFFER it to every client's bounded
        queue. Never blocks the inference loop (C10): each client has its own
        background writer thread and drops stale frames when it falls behind; a
        client that is persistently behind disconnects itself. Dead clients are
        reaped here so their slots free up."""
        frame = _ws_encode_text(
            json.dumps(obj, separators=(",", ":")).encode("utf-8")
        )
        with self._lock:
            clients = list(self._clients)
        for c in clients:
            c.offer(frame)
        # Reap any client whose writer has exited (send error / self-disconnect).
        if any(not c.alive() for c in clients):
            with self._lock:
                self._clients = [c for c in self._clients if c.alive()]

    def publish_envelope(self, envelope: dict) -> None:
        """Broadcast a pre-built canonical envelope verbatim (used by
        ConfigurableSink's WsChannel). No second seq/timestamp/frame stamp."""
        obj = dict(envelope)
        obj.setdefault("type", "results")
        self._broadcast(obj)

    def emit(self, payload: dict, pts: float) -> None:
        if self.preserve_envelope:
            # Upstream already built the canonical envelope (app/seq/timestamp/
            # frame). Broadcast it as-is; do not add a second sequence.
            self.publish_envelope(payload)
            return
        self._seq += 1
        payload = dict(payload)
        payload.setdefault("type", "results")
        payload.setdefault("app", self.app_id)
        payload["pts"] = pts
        payload["seq"] = self._seq
        # Announce the inference-frame reference size the result coords map to.
        # The overlay reads this to scale box/keypoint PIXELS into the video
        # display area; without it the panel falls back to a hard-coded guess.
        if self._frame_w and self._frame_h:
            payload["frame"] = {"width": self._frame_w, "height": self._frame_h}
        self._broadcast(payload)

    def emit_meta(self, payload: dict) -> None:
        """Broadcast a metrics/meta message on the SAME WS channel as results.
        Tagged with its own `type` (e.g. "metrics") so the panel can demux; it
        does NOT advance the results `seq` counter."""
        payload = dict(payload)
        payload.setdefault("type", "meta")
        payload.setdefault("app", self.app_id)
        self._broadcast(payload)

    def client_count(self) -> int:
        with self._lock:
            self._clients = [c for c in self._clients if c.alive()]
            return len(self._clients)

    def close(self) -> None:
        self._stop.set()
        if self._srv:
            try:
                self._srv.close()
            except Exception:
                pass
        with self._lock:
            clients = list(self._clients)
            self._clients.clear()
        for c in clients:
            try:
                c.close()
            except Exception:
                pass


class GatewayResultSink(ResultSink):
    """Publish to appmgr's authenticated Unix-domain result gateway.

    appmgr injects the socket, app id, random instance id and generation.  The
    first NDJSON record is a hello; the gateway validates it against the
    pre-READY PID registry and acknowledges it.  Subsequent records are result
    envelopes.  A bounded latest-wins writer queue keeps a slow/restarting
    gateway off the inference thread.

    ``host`` and ``port`` are accepted for signature compatibility with
    :class:`WsResultSink`; they are intentionally ignored.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 8124,
                 app_id: str = "app", preserve_envelope: bool = False, *,
                 sock: Optional[str] = None, queue_size: int = 64,
                 connect_timeout: float = 5.0, send_timeout: float = 0.25,
                 **_ignored):
        del host, port
        self.sock = sock or os.environ.get("RECAMERA_RESULT_GATEWAY_SOCK", "")
        self.app_id = os.environ.get("RECAMERA_APP_ID", app_id)
        self.instance_id = os.environ.get("RECAMERA_APP_INSTANCE", "")
        generation = os.environ.get("RECAMERA_APP_GENERATION", "")
        try:
            self.generation = int(generation)
        except (TypeError, ValueError):
            self.generation = -1
        if not self.sock:
            raise AdapterError(
                "result gateway socket is not configured",
                operation="result.gateway.connect", retryable=True)
        if not self.instance_id or self.generation < 0:
            raise AdapterError(
                "managed result gateway identity is incomplete",
                operation="result.gateway.hello", retryable=False,
                details={"app": self.app_id})
        self.preserve_envelope = bool(preserve_envelope)
        self._connect_timeout = max(0.1, float(connect_timeout))
        self._send_timeout = max(0.01, float(send_timeout))
        self._q: "queue.Queue" = queue.Queue(maxsize=max(1, int(queue_size)))
        self._conn: Optional[socket.socket] = None
        self._conn_lock = threading.Lock()
        self._stop = threading.Event()
        self._seq = 0
        self._frame_w: Optional[int] = None
        self._frame_h: Optional[int] = None
        self._sent = 0
        self._dropped = 0
        self._errors = 0
        # Fail startup before appmgr READY when the gateway or identity registry
        # is unavailable.  A short retry covers the Popen/on_spawn scheduling
        # race without ever falling back to binding :8124 in the child.
        self._connect(retry_until=time.monotonic() + self._connect_timeout)
        self._writer = threading.Thread(target=self._run, daemon=True)
        self._writer.start()

    @staticmethod
    def _recv_line(conn: socket.socket, limit: int = 4096) -> bytes:
        data = bytearray()
        while len(data) <= limit:
            chunk = conn.recv(1)
            if not chunk:
                break
            if chunk == b"\n":
                return bytes(data)
            data.extend(chunk)
        raise ConnectionError("invalid result gateway hello acknowledgement")

    def _hello(self) -> bytes:
        return (json.dumps({
            "type": "hello",
            "protocol": "recamera-result-gateway@1",
            "app": self.app_id,
            "instance": self.instance_id,
            "generation": self.generation,
            "pid": os.getpid(),
        }, separators=(",", ":")) + "\n").encode("utf-8")

    def _connect(self, retry_until: Optional[float] = None) -> socket.socket:
        last = None
        while not self._stop.is_set():
            conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                conn.settimeout(self._send_timeout)
                conn.connect(self.sock)
                conn.sendall(self._hello())
                ack = json.loads(self._recv_line(conn).decode("utf-8"))
                if not isinstance(ack, dict) or ack.get("ok") is not True:
                    raise PermissionError((ack or {}).get("error", "gateway rejected hello"))
                with self._conn_lock:
                    old, self._conn = self._conn, conn
                if old is not None:
                    try:
                        old.close()
                    except OSError:
                        pass
                return conn
            except Exception as exc:
                last = exc
                try:
                    conn.close()
                except OSError:
                    pass
                if retry_until is None or time.monotonic() >= retry_until:
                    break
                time.sleep(0.05)
        raise AdapterError(
            "cannot connect to result gateway %s: %s" % (self.sock, last),
            operation="result.gateway.connect", retryable=True,
            details={"socket": self.sock, "app": self.app_id})

    def _disconnect(self) -> None:
        with self._conn_lock:
            conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass

    def _offer(self, obj: dict) -> None:
        try:
            line = (json.dumps(obj, separators=(",", ":"), default=str)
                    + "\n").encode("utf-8")
        except Exception:
            self._errors += 1
            return
        if len(line) > 512 * 1024:
            self._errors += 1
            return
        if offer_latest_wins(self._q, line):
            self._dropped += 1

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                line = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            if line is None:
                break
            sent = False
            for attempt in range(2):
                try:
                    with self._conn_lock:
                        conn = self._conn
                    if conn is None:
                        conn = self._connect(
                            retry_until=time.monotonic() + self._connect_timeout)
                    conn.settimeout(self._send_timeout)
                    conn.sendall(line)
                    self._sent += 1
                    sent = True
                    break
                except Exception:
                    self._errors += 1
                    self._disconnect()
                    if attempt == 0 and not self._stop.is_set():
                        continue
            if not sent:
                self._dropped += 1

    def set_frame_size(self, w: int, h: int) -> None:
        if w and h and int(w) > 0 and int(h) > 0:
            self._frame_w, self._frame_h = int(w), int(h)

    def publish_envelope(self, envelope: dict) -> None:
        obj = dict(envelope)
        obj.setdefault("type", "results")
        self._offer(obj)

    def emit(self, payload: dict, pts: float) -> None:
        if self.preserve_envelope:
            self.publish_envelope(payload)
            return
        self._seq += 1
        obj = dict(payload)
        obj.setdefault("type", "results")
        obj.setdefault("app", self.app_id)
        obj["pts"] = pts
        obj["seq"] = self._seq
        if self._frame_w and self._frame_h:
            obj["frame"] = {"width": self._frame_w, "height": self._frame_h}
        self._offer(obj)

    def emit_meta(self, payload: dict) -> None:
        obj = dict(payload)
        obj.setdefault("type", "meta")
        obj.setdefault("app", self.app_id)
        self._offer(obj)

    def stats(self) -> dict:
        return {"sent": self._sent, "dropped": self._dropped,
                "send_error": self._errors, "queued": self._q.qsize(),
                "backend": "appmgr-gateway"}

    def close(self) -> None:
        self._stop.set()
        try:
            self._q.put_nowait(None)
        except queue.Full:
            try:
                self._q.get_nowait()
                self._q.put_nowait(None)
            except (queue.Empty, queue.Full):
                pass
        self._disconnect()
        writer = getattr(self, "_writer", None)
        if writer is not None and writer is not threading.current_thread():
            writer.join(timeout=1.0)


class MultiSink(ResultSink):
    """Fan-out sink: forward every emit() to a list of child sinks.

    Lets one app run publish results to several backends at once -- e.g. the
    /appcenter overlay WS *and* an MQTT/Home-Assistant broker -- without the
    app or the base loop knowing there is more than one. Each child is isolated:
    a raising/slow child never blocks or breaks the others (WsResultSink and
    MqttSink are both already best-effort internally, so this is belt-and-braces).
    """

    def __init__(self, sinks: List[ResultSink]):
        self.sinks = [s for s in sinks if s is not None]

    def emit(self, payload: dict, pts: float) -> None:
        for s in self.sinks:
            try:
                s.emit(payload, pts)
            except Exception:
                pass

    @staticmethod
    def _raise_checked(operation: str, failures) -> None:
        """Raise one aggregate error after every fan-out child was attempted."""

        if not failures:
            return
        details = {
            "operation": operation,
            "failed_children": [
                {"sink": type(sink).__name__, "error": str(exc)}
                for sink, exc in failures
            ],
        }
        first = failures[0][1]
        log.error(
            "result fan-out failed operation=%s children=%s",
            operation,
            [item["sink"] for item in details["failed_children"]],
        )
        raise AdapterError(
            f"{len(failures)} result sink child operation(s) failed",
            operation=f"result.multi.{operation}",
            details=details,
        ) from first

    def emit_checked(self, payload: dict, pts: float) -> None:
        """Attempt every child and report any synchronous failure."""

        failures = []
        for sink in self.sinks:
            emitter = getattr(sink, "emit_checked", None)
            if not callable(emitter):
                emitter = sink.emit
            try:
                emitter(payload, pts)
            except Exception as exc:
                failures.append((sink, exc))
        self._raise_checked("emit", failures)

    def emit_meta(self, payload: dict) -> None:
        for s in self.sinks:
            try:
                s.emit_meta(payload)
            except Exception:
                pass

    def set_frame_size(self, w: int, h: int) -> None:
        # Fan the frame size out so a wrapped OfficialResultSink can normalize.
        for s in self.sinks:
            try:
                s.set_frame_size(w, h)
            except Exception:
                pass

    def set_frame_size_checked(self, w: int, h: int) -> None:
        """Attempt every child geometry update and aggregate failures."""

        failures = []
        for sink in self.sinks:
            setter = getattr(sink, "set_frame_size_checked", None)
            if not callable(setter):
                setter = sink.set_frame_size
            try:
                setter(w, h)
            except Exception as exc:
                failures.append((sink, exc))
        self._raise_checked("set_frame_size", failures)

    def on_config_reload(self, config: dict) -> None:
        # Fan the hot-reload out so a wrapped ConfigurableSink re-applies its
        # apply:"live" output filters / template. Isolated like every other
        # fan-out here: one raising child never stops the rest.
        for s in self.sinks:
            try:
                s.on_config_reload(config)
            except Exception:
                pass

    def client_count(self) -> int:
        total = 0
        for s in self.sinks:
            fn = getattr(s, "client_count", None)
            if callable(fn):
                try:
                    total += fn()
                except Exception:
                    pass
        return total

    def stats(self) -> dict:
        """Per-child stats keyed by child class name (only children that report
        anything). Lets an app read delivery diagnostics through the fan-out
        without knowing which concrete sinks are behind it."""
        out = {}
        for s in self.sinks:
            fn = getattr(s, "stats", None)
            if not callable(fn):
                continue
            try:
                st = fn()
            except Exception:
                continue
            if st:
                out[type(s).__name__] = st
        return out

    def close(self) -> None:
        failures = []
        control_error = None
        for s in self.sinks:
            try:
                s.close()
            except Exception as exc:
                failures.append((s, exc))
            except BaseException as exc:
                if control_error is None:
                    control_error = exc
        if control_error is not None:
            if failures:
                log.error(
                    "result fan-out close also failed for children=%s",
                    [type(sink).__name__ for sink, _exc in failures],
                )
            raise control_error
        self._raise_checked("close", failures)


# --------------------------------------------------------------------------- #
# Configurable-output interfaces (docs internal/OUTPUT_SINK_SPEC.md §2)
#
# These are the ABCs for the unified `ConfigurableSink` output pipeline. The
# concrete channels/formatters and ConfigurableSink itself live in
# kit/adapters/output_sink.py to keep this module lean and avoid disturbing the
# legacy sink path; they import these three names from here.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class OutputMessage:
    """One formatted message ready for a channel to publish.

    `topic` is None for channels that carry their own destination (WS, or an
    MQTT channel falling back to its default state topic); formatters that
    target specific MQTT topics (mapping rows, HA discovery) set it explicitly.
    """
    body: bytes
    topic: Optional[str] = None
    content_type: str = "application/json"
    retain: bool = False
    metadata: dict = field(default_factory=dict)


class OutputChannel(ABC):
    """A transport (ws/mqtt/http/uart). Best-effort; never raises into emit()."""
    name: str = "channel"

    @abstractmethod
    def publish(self, message: "OutputMessage") -> None:
        raise NotImplementedError

    def client_count(self) -> int:
        return 0

    def close(self) -> None:
        pass


class OutputFormatter(ABC):
    """Turns one canonical envelope into zero+ OutputMessages for a channel."""

    @abstractmethod
    def format(self, envelope: dict, *, channel: str) -> List["OutputMessage"]:
        raise NotImplementedError

    def on_channel_ready(self, channel: "OutputChannel") -> List["OutputMessage"]:
        """Messages to publish when a channel (re)connects -- e.g. HA discovery
        + retained `online`. Default: nothing."""
        return []


def open_result_sink(kind: str = "ws", **kw) -> ResultSink:
    """Factory. `kind` = "ws" (broadcast) | "stdout" (debug).

    Delegates to the capability registry, which probes for the official R2
    result ingress and returns an `OsdInjectResultSink` when present. On today's
    firmware there is no official ingress, so the registry falls back to the
    workaround sink selected by `kind` and behaviour is unchanged. The "stdout"
    debug sink is always honoured verbatim.
    """
    from .registry import select_result_sink
    return select_result_sink(kind=kind, **kw)

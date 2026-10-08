"""
MqttSink -- Home Assistant / MQTT result publisher for reCamera Pro (2nd gen).

L0 adapter (same ABC as WsResultSink). Where WsResultSink feeds the /appcenter
overlay a rich per-frame stream (pixel boxes, keypoints, ...), MqttSink feeds a
*home-automation* audience: a compact per-frame state document plus retained
Home Assistant MQTT-Discovery configs so HA auto-creates one entity per app
signal (detection count, fall state, entry/exit counts, QR text, ...).

Why a hand-rolled client (no paho-mqtt)
---------------------------------------
MqttSink only ever PUBLISHes (QoS 0) -- it never subscribes. MQTT 3.1.1 CONNECT
+ PUBLISH + PINGREQ is a few dozen bytes of framing, so we implement it directly
on a stdlib socket. `MqttClient` (below) adds the small remainder an app with a
downlink needs -- QoS 0/1 PUBLISH, QoS 0/1 SUBSCRIBE, inbound PUBLISH dispatch
with PUBACK, and a reader thread that owns keepalive and reconnect -- so apps
such as intrusion-detection need no paho either. Zero new dependencies enter
the shared device venv or any app package. A background thread owns the socket: it connects,
publishes the retained discovery configs + an "online" availability message,
then keeps the link alive with PINGREQ and transparently reconnects (re-arming
discovery) after any drop. `emit()` is best-effort and never blocks or raises
into the inference loop -- a dead broker degrades to "WS only", exactly the
behaviour when MQTT is left unconfigured.

MQTT state document (published to <base_topic>/<app>/state each processed frame)
    {
      "app": "yolo-detector",
      "pts": 123.456, "seq": 42,
      "results_count": 3,                 # len(results)
      "person_count": 2,                  # visible person pose results
      "fallen_count": 1,                  # visible person results in fall state
      "counts_by_kind": {"detection": 3}, # tally of events[].kind
      "class_counts": {"person": 1, ...}, # tally of results[].cls_name
      "summary": { ... },                 # scalar event fields (fall aggregate-safe)
      "events": [ ... ]                   # app events (no pixel boxes dropped;
                                          #   kept small -- raw results omitted)
    }
HA entity `value_template`s (declared in each app manifest's `ha_entities`)
reference this document, e.g. `{{ value_json.results_count }}` or
`{{ value_json.summary.fall_detected }}`.
"""
from __future__ import annotations

import json
import os
import select
import socket
import struct
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from kit.adapters.result_sink import ResultSink


# --------------------------------------------------------------------------- #
# minimal MQTT 3.1.1 wire codec
# --------------------------------------------------------------------------- #
def _remaining_length(n: int) -> bytes:
    """Encode an MQTT variable-length integer (remaining length field)."""
    out = bytearray()
    while True:
        b = n % 128
        n //= 128
        if n > 0:
            b |= 0x80
        out.append(b)
        if n == 0:
            break
    return bytes(out)


def _mqtt_str(s: str) -> bytes:
    """UTF-8 string prefixed with a 2-byte big-endian length (MQTT wire string)."""
    data = s.encode("utf-8")
    return struct.pack(">H", len(data)) + data


# MQTT control packet types (high nibble of the fixed header byte)
_CONNACK, _PUBLISH, _PUBACK, _SUBSCRIBE, _SUBACK, _PINGRESP = 2, 3, 4, 8, 9, 13


class _MqttConnection:
    """A single live MQTT 3.1.1 connection (QoS 0/1). Writes are serialised by
    an internal lock so a reader thread and publishers can share it; reads
    (`read_packet`) must come from one thread only."""

    def __init__(self, host: str, port: int, client_id: str,
                 keepalive: int = 30, username: str = "", password: str = "",
                 will_topic: str = "", will_payload: bytes = b"",
                 will_retain: bool = True, will_qos: int = 0):
        self.host = host
        self.port = port
        self.client_id = client_id
        self.keepalive = keepalive
        self.username = username
        self.password = password
        self.will_topic = will_topic
        self.will_payload = will_payload
        self.will_retain = will_retain
        self.will_qos = will_qos
        self._sock: Optional[socket.socket] = None
        self._send_lock = threading.Lock()
        self._next_pid = 0
        self.last_tx = 0.0   # monotonic time of the last packet sent
        self.last_rx = 0.0   # monotonic time of the last packet received
        self.ping_sent: Optional[float] = None  # outstanding PINGREQ time

    def _packet_id(self) -> int:
        self._next_pid = self._next_pid % 0xFFFF + 1   # 1..65535, never 0
        return self._next_pid

    def _send(self, data: bytes) -> None:
        with self._send_lock:
            # Re-read under the lock: close() clears _sock while holding it,
            # so nothing can be written after the DISCONNECT.
            sock = self._sock
            if sock is None:
                raise ConnectionError("not connected")
            sock.sendall(data)
            self.last_tx = time.monotonic()

    def connect(self, timeout: float = 5.0) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=timeout)
        sock.settimeout(timeout)
        # -- CONNECT variable header -------------------------------------- #
        flags = 0x02  # clean session
        payload = _mqtt_str(self.client_id)
        if self.will_topic:
            flags |= 0x04                       # will flag
            flags |= (self.will_qos & 0x03) << 3  # will QoS
            if self.will_retain:
                flags |= 0x20                   # will retain
            payload += _mqtt_str(self.will_topic)
            payload += struct.pack(">H", len(self.will_payload)) + self.will_payload
        if self.username:
            flags |= 0x80
            payload += _mqtt_str(self.username)
            if self.password:
                flags |= 0x40
                payload += _mqtt_str(self.password)
        var_header = (
            _mqtt_str("MQTT")                   # protocol name
            + bytes([0x04])                     # protocol level 4 (3.1.1)
            + bytes([flags])
            + struct.pack(">H", self.keepalive)
        )
        pkt = var_header + payload
        sock.sendall(bytes([0x10]) + _remaining_length(len(pkt)) + pkt)
        # -- read CONNACK (0x20, len 2, [ack_flags, return_code]) --------- #
        hdr = self._recv_exact(sock, 2)
        if not hdr or hdr[0] != 0x20:
            sock.close()
            raise ConnectionError(f"unexpected CONNACK header {hdr!r}")
        body = self._recv_exact(sock, hdr[1])
        if len(body) < 2 or body[1] != 0x00:
            rc = body[1] if len(body) >= 2 else -1
            sock.close()
            raise ConnectionError(f"MQTT CONNECT refused rc={rc}")
        self._sock = sock
        self.last_tx = time.monotonic()

    @staticmethod
    def _recv_exact(sock: socket.socket, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                break
            buf += chunk
        return buf

    def publish(self, topic: str, payload: bytes, retain: bool = False,
                qos: int = 0) -> None:
        """QoS 0 or 1. A QoS 1 PUBACK is consumed by `read_packet`'s caller;
        there is no retransmission (fire-once at-least-once framing)."""
        if self._sock is None:
            raise ConnectionError("not connected")
        if qos not in (0, 1):
            raise ValueError(f"unsupported QoS {qos}")
        header0 = 0x30 | (qos << 1) | (0x01 if retain else 0x00)  # no dup
        body = _mqtt_str(topic)
        if qos:
            body += struct.pack(">H", self._packet_id())
        body += payload
        self._send(bytes([header0]) + _remaining_length(len(body)) + body)

    def subscribe(self, topic: str, qos: int = 0) -> int:
        """Send SUBSCRIBE for one topic filter; returns its packet id."""
        if qos not in (0, 1):
            raise ValueError(f"unsupported QoS {qos}")
        pid = self._packet_id()
        body = struct.pack(">H", pid) + _mqtt_str(topic) + bytes([qos])
        self._send(b"\x82" + _remaining_length(len(body)) + body)
        return pid

    def puback(self, pid: int) -> None:
        self._send(b"\x40\x02" + struct.pack(">H", pid))

    def ping(self) -> None:
        if self._sock is None:
            raise ConnectionError("not connected")
        self._send(b"\xc0\x00")   # PINGREQ (server replies PINGRESP)

    def wait_readable(self, timeout: float) -> bool:
        sock = self._sock
        if sock is None:
            raise ConnectionError("not connected")
        readable, _, _ = select.select([sock], [], [], timeout)
        return bool(readable)

    def read_packet(self):
        """Read one whole control packet -> (fixed_header_byte, body)."""
        sock = self._sock
        if sock is None:
            raise ConnectionError("not connected")
        hdr = self._recv_exact(sock, 1)
        if not hdr:
            raise ConnectionError("broker closed the connection")
        length, mult = 0, 1
        for _ in range(4):
            b = self._recv_exact(sock, 1)
            if not b:
                raise ConnectionError("truncated remaining length")
            length += (b[0] & 0x7F) * mult
            mult *= 128
            if not b[0] & 0x80:
                break
        else:
            raise ConnectionError("malformed remaining length")
        body = self._recv_exact(sock, length)
        if len(body) != length:
            raise ConnectionError("truncated packet")
        return hdr[0], body

    def close(self) -> None:
        with self._send_lock:
            sock, self._sock = self._sock, None
            if sock is not None:
                try:
                    sock.sendall(b"\xe0\x00")   # DISCONNECT
                except OSError:
                    pass
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)  # wakes a reader in select()
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass


def _parse_publish(header0: int, body: bytes) -> Tuple[str, bytes, int, int]:
    """PUBLISH body -> (topic, payload, qos, packet_id or 0)."""
    qos = (header0 >> 1) & 0x03
    (tlen,) = struct.unpack(">H", body[:2])
    topic = body[2:2 + tlen].decode("utf-8")
    pos = 2 + tlen
    pid = 0
    if qos:
        (pid,) = struct.unpack(">H", body[pos:pos + 2])
        pos += 2
    return topic, body[pos:], qos, pid


class MqttClient:
    """Small MQTT 3.1.1 client: QoS 0/1 publish, QoS 0/1 subscribe, inbound
    PUBLISH dispatch, keepalive and reconnect, on stdlib sockets only.

    One background thread owns the socket's read side: it reads inbound
    packets and answers QoS 1 deliveries with PUBACK. Liveness is judged on
    the broker's answers, never on our own sends: a PINGREQ goes out after
    keepalive/2 without any inbound packet (outbound PUBLISHes do not defer
    it -- QoS 0 is never answered) or keepalive/2 without any outbound
    packet, and the link is declared dead only when a PINGREQ stays
    unanswered for a whole keepalive. Reconnects use the same
    1 s -> 30 s backoff as MqttSink. Subscriptions registered with `subscribe`
    are re-sent on every (re)connect, after which `on_connect(client)` runs.
    `on_message(topic, payload)` runs on the reader thread. `publish` is
    best-effort: it returns False (never raises) while the link is down;
    nothing is queued or retransmitted.

    Lifecycle: `connect`/`disconnect` are serialised. Each `connect` starts a
    new generation; `disconnect` ends it, so a handshake still in flight from
    an ended generation is closed on completion without subscribing or
    running `on_connect`. The reader thread handle is kept until that thread
    has really exited, and `connect` never starts a second reader beside it.
    """

    def __init__(
        self,
        host: str,
        port: int = 1883,
        *,
        client_id: str,
        keepalive: int = 30,
        username: str = "",
        password: str = "",
        will_topic: str = "",
        will_payload: bytes = b"",
        will_qos: int = 0,
        will_retain: bool = False,
        on_connect: Optional[Callable[["MqttClient"], None]] = None,
        on_message: Optional[Callable[[str, bytes], None]] = None,
        connect_timeout: float = 5.0,
        logger: Optional[Callable[[str], None]] = None,
    ):
        self.host = host
        self.port = int(port)
        self.client_id = client_id
        self.keepalive = max(1, int(keepalive))
        self.username = username or ""
        self.password = password or ""
        self.will_topic = will_topic
        self.will_payload = will_payload
        self.will_qos = will_qos
        self.will_retain = will_retain
        self.on_connect = on_connect
        self.on_message = on_message
        self.connect_timeout = connect_timeout
        self._log = logger or (lambda _msg: None)
        self._subs: List[Tuple[str, int]] = []
        self._conn: Optional[_MqttConnection] = None
        self._lock = threading.Lock()          # guards _conn / _gen / _subs
        self._lifecycle = threading.Lock()     # serialises connect/disconnect
        self._connected = threading.Event()
        self._gen = 0                          # current generation
        self._stop = threading.Event()         # stop event of current generation
        self._stop.set()                       # not started
        self._thread: Optional[threading.Thread] = None

    # -- public API ------------------------------------------------------- #
    def subscribe(self, topic: str, qos: int = 0) -> None:
        """Register a subscription; sent now if connected and on every
        reconnect."""
        if qos not in (0, 1):
            raise ValueError(f"unsupported QoS {qos}")
        with self._lock:
            self._subs.append((topic, qos))
            conn = self._conn
        if conn is not None:
            try:
                conn.subscribe(topic, qos)
            except Exception:
                self._drop(conn)

    def connect(self, blocking: bool = True) -> None:
        """Start a new generation. With `blocking`, its first CONNECT happens
        on the caller's thread and a failure raises (no thread is left
        running); later drops reconnect in the background either way.

        A no-op while already running. If the reader of a previous generation
        is still winding down (e.g. stuck in a CONNACK wait), it is joined for
        up to connect_timeout + 1 s, and RuntimeError is raised if it is still
        alive, rather than running two readers.

        `on_connect` runs after the lifecycle lock is released, so the
        callback may itself call `disconnect()`."""
        conn = None
        with self._lifecycle:
            old = self._thread
            if old is not None and old.is_alive():
                if not self._stop.is_set():
                    return                       # already running
                old.join(self.connect_timeout + 1.0)
                if old.is_alive():
                    raise RuntimeError("previous MQTT reader thread still running")
            self._thread = None
            stop = threading.Event()
            with self._lock:
                self._gen += 1
                gen = self._gen
                self._stop = stop
            if blocking:
                try:
                    conn = self._open(gen, stop)
                except Exception:
                    stop.set()
                    raise
            thread = threading.Thread(target=self._run, args=(gen, stop),
                                      daemon=True, name=f"mqtt-{self.client_id}")
            self._thread = thread
            thread.start()
        if conn is not None:
            self._announce(conn, gen)

    def is_connected(self) -> bool:
        return self._connected.is_set()

    def publish(self, topic: str, payload, qos: int = 0,
                retain: bool = False) -> bool:
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        with self._lock:
            conn = self._conn
        if conn is None:
            return False
        try:
            conn.publish(topic, bytes(payload), retain=retain, qos=qos)
            return True
        except ValueError:
            raise
        except Exception:
            self._drop(conn)
            return False

    def disconnect(self, timeout: float = 2.0) -> None:
        """Send DISCONNECT (the broker then discards the will) and stop.

        Ends the current generation first, so a reconnect handshake still in
        flight can no longer install itself or run on_connect. The thread
        handle is kept if the reader outlives `timeout`; `connect` joins it."""
        with self._lifecycle:
            with self._lock:
                self._stop.set()
                self._gen += 1
                conn = self._conn
            if conn is not None:
                self._drop(conn)
            thread = self._thread
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout)
                if not thread.is_alive():
                    self._thread = None

    # -- internals -------------------------------------------------------- #
    def _open(self, gen: int, stop: threading.Event) -> _MqttConnection:
        """Handshake, install and subscribe; the caller then runs
        `_announce` outside any lock (on_connect is user code)."""
        conn = _MqttConnection(
            self.host, self.port, client_id=self.client_id,
            keepalive=self.keepalive, username=self.username,
            password=self.password, will_topic=self.will_topic,
            will_payload=self.will_payload, will_retain=self.will_retain,
            will_qos=self.will_qos,
        )
        conn.connect(timeout=self.connect_timeout)
        with self._lock:
            current = (gen == self._gen and not stop.is_set())
            if current:
                self._conn = conn
                self._connected.set()
                subs = list(self._subs)
        if not current:
            # Handshake of an ended generation: never subscribe/announce.
            try:
                conn.close()
            except Exception:
                pass
            raise ConnectionAbortedError("connect cancelled by disconnect()")
        conn.last_rx = time.monotonic()
        conn.ping_sent = None
        try:
            for topic, qos in subs:
                conn.subscribe(topic, qos)
        except Exception:
            self._drop(conn)
            raise
        return conn

    def _announce(self, conn: _MqttConnection, gen: int) -> None:
        """Run on_connect unless that connection's generation has ended."""
        if self.on_connect is None:
            return
        with self._lock:
            current = self._conn is conn and gen == self._gen
        if not current:
            return
        try:
            self.on_connect(self)
        except Exception as e:
            self._log(f"on_connect callback failed: {e}")

    def _drop(self, conn: _MqttConnection) -> None:
        with self._lock:
            if self._conn is conn:
                self._conn = None
                self._connected.clear()
        try:
            conn.close()
        except Exception:
            pass

    def _handle(self, conn: _MqttConnection, header0: int, body: bytes) -> None:
        ptype = header0 >> 4
        if ptype == _PUBLISH:
            topic, payload, qos, pid = _parse_publish(header0, body)
            if qos == 1:
                conn.puback(pid)
            elif qos > 1:   # never granted: every subscription is QoS <= 1
                self._log(f"dropping QoS {qos} delivery on {topic}")
                return
            if self.on_message is not None:
                try:
                    self.on_message(topic, payload)
                except Exception as e:
                    self._log(f"on_message callback failed: {e}")
        elif ptype == _SUBACK:
            if any(rc == 0x80 for rc in body[2:]):
                self._log("broker refused a subscription (SUBACK 0x80)")
        # PUBACK / PINGRESP / anything else: liveness only

    def _run(self, gen: int, stop: threading.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            with self._lock:
                conn = self._conn if gen == self._gen else None
            if conn is None:
                try:
                    opened = self._open(gen, stop)
                    backoff = 1.0
                    self._log(f"connected {self.host}:{self.port}")
                    self._announce(opened, gen)
                except Exception as e:
                    if stop.is_set():
                        break
                    self._log(f"connect failed: {e} (retry {backoff:.0f}s)")
                    stop.wait(backoff)
                    backoff = min(backoff * 2, 30.0)
                continue
            try:
                tick = min(1.0, self.keepalive / 4.0)
                if conn.wait_readable(tick):
                    header0, body = conn.read_packet()
                    conn.last_rx = time.monotonic()
                    conn.ping_sent = None        # any inbound packet answers
                    self._handle(conn, header0, body)
                now = time.monotonic()
                if conn.ping_sent is not None:
                    if now - conn.ping_sent > self.keepalive:
                        raise ConnectionError("PINGREQ unanswered for a keepalive")
                elif (now - conn.last_rx >= self.keepalive / 2.0
                      or now - conn.last_tx >= self.keepalive / 2.0):
                    # rx-idle: probe the broker; tx-idle: keep the broker's
                    # own keepalive timer satisfied (MQTT 3.1.1 s3.1.2.10).
                    conn.ping()
                    # Deadline counts from send completion, not from the
                    # decision: send-lock waits must not eat the allowance.
                    conn.ping_sent = time.monotonic()
            except Exception as e:
                if not stop.is_set():
                    self._log(f"link dropped: {e}")
                self._drop(conn)
        # A reconnect that raced disconnect() must not leave a live socket.
        with self._lock:
            conn = self._conn if gen == self._gen else None
        if conn is not None:
            self._drop(conn)


# --------------------------------------------------------------------------- #
# device identity
# --------------------------------------------------------------------------- #
def device_identifier() -> str:
    """Stable per-device id for the HA `device.identifiers` grouping.

    RECAMERA_SN env (set by the platform) wins; else the U-Boot `sn`; else the
    hostname. Sanitised to [a-z0-9_] so it is topic/entity-id safe."""
    cand = os.environ.get("RECAMERA_SN", "").strip()
    if not cand:
        try:
            import subprocess
            out = subprocess.run(["fw_printenv", "-n", "sn"], capture_output=True,
                                 text=True, timeout=2)
            if out.returncode == 0:
                cand = out.stdout.strip()
        except Exception:
            cand = ""
    if not cand:
        cand = socket.gethostname() or "recamera"
    safe = "".join(c if (c.isalnum() or c == "_") else "_" for c in cand.lower())
    return safe or "recamera"


# --------------------------------------------------------------------------- #
# reusable HA MQTT-discovery primitives (module-level so ConfigurableSink's
# HaDiscoveryFormatter can WRAP -- not duplicate -- them; MqttSink's own methods
# below delegate here, so its behaviour is byte-for-byte unchanged)
# --------------------------------------------------------------------------- #
def ha_discovery_topic(discovery_prefix: str, node: str, app_id: str,
                       ent: dict) -> str:
    """`<prefix>/<component>/recamera_<node>_<app>/<object_id>/config`."""
    component = ent.get("component", "sensor")
    object_id = ent["object_id"]
    return (f"{discovery_prefix}/{component}/"
            f"recamera_{node}_{app_id}/{object_id}/config")


def ha_discovery_payload(node: str, app_id: str, state_topic: str,
                         status_topic: str, device_name: str,
                         ent: dict) -> dict:
    """The retained HA MQTT-Discovery config document for one entity.

    Availability (`availability_topic`/`payload_available`/`payload_not_available`)
    and a stable `unique_id` are always emitted so HA marks the entity
    online/offline off the LWT and never creates duplicates on reconnect."""
    object_id = ent["object_id"]
    cfg: Dict[str, Any] = {
        "name": ent.get("name", object_id),
        "unique_id": f"recamera_{node}_{app_id}_{object_id}",
        "state_topic": state_topic,
        "value_template": ent.get("value_template", ""),
        "availability_topic": status_topic,
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": {
            "identifiers": [f"recamera_{node}"],
            "name": device_name,
            "manufacturer": "Seeed Studio",
            "model": "reCamera Pro",
        },
    }
    for k in ("device_class", "unit_of_measurement", "state_class", "icon",
              "entity_category"):
        if ent.get(k):
            cfg[k] = ent[k]
    return cfg


# --------------------------------------------------------------------------- #
# MqttSink
# --------------------------------------------------------------------------- #
class MqttSink(ResultSink):
    """Best-effort HA/MQTT publisher. Construct once per app run alongside the
    WS sink (see kit.app.run_app). Never raises into emit()."""

    def __init__(
        self,
        *,
        host: str,
        port: int = 1883,
        app_id: str = "app",
        base_topic: str = "recamera",
        discovery_prefix: str = "homeassistant",
        username: str = "",
        password: str = "",
        entities: Optional[List[dict]] = None,
        device_name: str = "reCamera Pro",
        keepalive: int = 30,
        verbose: bool = False,
    ):
        self.host = host
        self.port = int(port)
        self.app_id = app_id
        self.base_topic = base_topic.rstrip("/") or "recamera"
        self.discovery_prefix = discovery_prefix.rstrip("/") or "homeassistant"
        self.username = username or ""
        self.password = password or ""
        self.entities = entities or []
        self.device_name = device_name
        self.keepalive = max(10, int(keepalive))
        self.verbose = verbose

        self.node = device_identifier()
        self.state_topic = f"{self.base_topic}/{self.app_id}/state"
        self.status_topic = f"{self.base_topic}/{self.app_id}/status"

        self._seq = 0
        self._fall_global_event_id = 0
        self._frame_w = 1
        self._frame_h = 1
        self._conn: Optional[_MqttConnection] = None
        self._lock = threading.Lock()
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    # -- background connect / keepalive / reconnect ----------------------- #
    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            if not self._connected.is_set():
                try:
                    self._open()
                    backoff = 1.0
                    if self.verbose:
                        print(f"[mqtt:{self.app_id}] connected "
                              f"{self.host}:{self.port} node={self.node}", flush=True)
                except Exception as e:
                    if self.verbose:
                        print(f"[mqtt:{self.app_id}] connect failed: {e} "
                              f"(retry {backoff:.0f}s)", flush=True)
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2, 30.0)
                    continue
            # keepalive tick
            self._stop.wait(self.keepalive / 2.0)
            if self._stop.is_set():
                break
            with self._lock:
                conn = self._conn
            if conn is not None:
                try:
                    conn.ping()
                except Exception:
                    self._drop()

    def _open(self) -> None:
        conn = _MqttConnection(
            self.host, self.port,
            client_id=f"recamera-{self.app_id}-{self.node}"[:23] + str(os.getpid() % 1000),
            keepalive=self.keepalive,
            username=self.username, password=self.password,
            will_topic=self.status_topic, will_payload=b"offline", will_retain=True,
        )
        conn.connect()
        with self._lock:
            self._conn = conn
            self._connected.set()
        # availability + retained discovery on every (re)connect
        self._safe_publish(self.status_topic, b"online", retain=True)
        self._publish_discovery()

    def _drop(self) -> None:
        with self._lock:
            conn, self._conn = self._conn, None
            self._connected.clear()
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def _safe_publish(self, topic: str, payload: bytes, retain: bool = False) -> bool:
        with self._lock:
            conn = self._conn
        if conn is None:
            return False
        try:
            conn.publish(topic, payload, retain=retain)
            return True
        except Exception:
            self._drop()
            return False

    # -- HA MQTT discovery ------------------------------------------------ #
    def _discovery_topic(self, ent: dict) -> str:
        return ha_discovery_topic(self.discovery_prefix, self.node,
                                  self.app_id, ent)

    def _discovery_payload(self, ent: dict) -> dict:
        return ha_discovery_payload(self.node, self.app_id, self.state_topic,
                                    self.status_topic, self.device_name, ent)

    def _publish_discovery(self) -> None:
        for ent in self.entities:
            if "object_id" not in ent:
                continue
            topic = self._discovery_topic(ent)
            payload = json.dumps(self._discovery_payload(ent),
                                 separators=(",", ":")).encode("utf-8")
            self._safe_publish(topic, payload, retain=True)

    # -- ResultSink ------------------------------------------------------- #
    @staticmethod
    def _is_person_result(result: dict) -> bool:
        """Return whether a result represents a person.

        Fall detection marks every pose with ``kind=person``.  The class-name
        checks preserve useful counts for generic YOLO results, while the
        track/state shape is a compatibility fallback for older fall app
        payloads that did not set ``kind``.
        """
        if not isinstance(result, dict):
            return False
        for key in ("kind", "cls_name", "label"):
            value = result.get(key)
            if isinstance(value, str) and value.strip().lower() == "person":
                return True
        return (result.get("track_id") is not None and
                ("fall_detected" in result or "state" in result))

    @staticmethod
    def _build_state(app_id: str, seq: int, pts: float,
                     results: List[dict], events: List[dict]) -> dict:
        counts_by_kind: Dict[str, int] = {}
        summary: Dict[str, Any] = {}
        for ev in events:
            k = ev.get("kind")
            if k is not None:
                counts_by_kind[k] = counts_by_kind.get(k, 0) + 1
            for key, val in ev.items():
                if key == "kind":
                    continue
                # last-wins union of scalar fields across all events
                if val is None or isinstance(val, (str, int, float, bool)):
                    summary[key] = val
        class_counts: Dict[str, int] = {}
        for r in results:
            name = r.get("cls_name")
            if isinstance(name, str):
                class_counts[name] = class_counts.get(name, 0) + 1
        person_results = [r for r in results
                          if MqttSink._is_person_result(r)]
        person_count = len(person_results)
        fallen_count = sum(1 for r in person_results
                           if bool(r.get("fall_detected")))
        # Keep the counts in summary too: existing HA/custom consumers often
        # read all app-level values from that object, while the top-level keys
        # make the new values unambiguous and easy to template.
        summary["person_count"] = person_count
        summary["fallen_count"] = fallen_count
        if person_results:
            # The old single-person template reads summary.fall_detected.  Keep
            # that compatibility field aggregate-safe for multi-person frames
            # instead of letting the last pose_state event win.
            summary["fall_detected"] = fallen_count > 0
        fall_event_ids = [
            int(ev["event_id"])
            for ev in events
            if isinstance(ev, dict) and ev.get("kind") == "fall"
            and isinstance(ev.get("event_id"), (int, float))
        ]
        if fall_event_ids:
            # A later normal person's pose_state must not erase the edge event
            # id emitted for another person in this same frame.
            summary["event_id"] = max(fall_event_ids)
        contract = {
            "app": app_id,
            "pts": pts,
            "seq": seq,
            "results_count": len(results),
            "person_count": person_count,
            "fallen_count": fallen_count,
            "counts_by_kind": counts_by_kind,
            "class_counts": class_counts,
            "summary": summary,
            "events": events,
        }
        return contract

    def set_frame_size(self, w: int, h: int) -> None:
        self._frame_w = max(1, int(w))
        self._frame_h = max(1, int(h))

    def _fall_contract(self, payload: dict, pts: float) -> dict:
        results = [r for r in (payload.get("results") or [])
                   if self._is_person_result(r)]
        states = {int(e["track_id"]): e for e in (payload.get("events") or [])
                  if isinstance(e, dict) and e.get("kind") == "pose_state"
                  and isinstance(e.get("track_id"), (int, float))}
        fall_edges = {int(e["track_id"]) for e in (payload.get("events") or [])
                      if isinstance(e, dict) and e.get("kind") == "fall"
                      and isinstance(e.get("track_id"), (int, float))}
        self._fall_global_event_id += len(fall_edges)
        visible = {int(r["track_id"]): r for r in results
                   if isinstance(r.get("track_id"), (int, float))}
        people = []
        for tid in sorted(set(states) | set(visible)):
            r = visible.get(tid, {})
            s = states.get(tid, {})
            box = r.get("box") or s.get("box") or [0, 0, 0, 0]
            if len(box) >= 4:
                x1, y1, x2, y2 = (float(x) for x in box[:4])
                bbox = [round((x1 + x2) / (2 * self._frame_w), 5),
                        round((y1 + y2) / (2 * self._frame_h), 5),
                        round((x2 - x1) / self._frame_w, 5),
                        round((y2 - y1) / self._frame_h, 5)]
            else:
                bbox = [0.0, 0.0, 0.0, 0.0]
            raw_pose = r.get("keypoints") or []
            pose17 = [[round(float(p[0]) / self._frame_w, 5),
                       round(float(p[1]) / self._frame_h, 5), float(p[2])]
                      for p in raw_pose[:17] if isinstance(p, (list, tuple)) and len(p) >= 3]
            is_visible = tid in visible
            features = r.get("features") or s.get("features") or {
                "valid": False, "hip_drop_speed": 0.0, "hip_drop_distance": 0.0,
                "torso_angle_deg": 0.0, "bbox_aspect_ratio": 0.0,
            }
            people.append({
                "track_id": tid, "state": s.get("state", r.get("state", "normal")),
                "fall_detected": bool(s.get("fall_detected", r.get("fall_detected", False))),
                "fall_event": tid in fall_edges,
                "event_id": int(s.get("event_id", r.get("event_id", 0)) or 0),
                "person_detected": is_visible,
                "person_score": float(r.get("person_score", r.get("score", s.get("person_score", 0.0))) or 0.0),
                "tracking": is_visible, "missed_frames": int(s.get("missed_frames", 0) or 0),
                "bbox": bbox, "features": features, "keypoints": [],
                "pose17": pose17 if is_visible else [],
            })
        severity = {"normal": 0, "suspected": 1, "recovering": 2, "fallen": 3}
        primary = max(people, key=lambda p: (p["person_detected"], p["person_score"]), default=None)
        fallen = [p for p in people if p["state"] in ("fallen", "recovering")]
        state = max((p["state"] for p in people), key=lambda x: severity.get(x, 0), default="normal")
        contract = {
            "timestamp": int(time.time() * 1000), "frame_id": self._seq,
            "inference_time_ms": float(payload.get("inference_time_ms", 0.0) or 0.0),
            "pipeline_ms": float(payload.get("pipeline_ms", 0.0) or 0.0),
            "stream_id": str(payload.get("stream_id") or "camera-0"),
            "fall_detected": bool(fallen), "fall_event": bool(fall_edges),
            "event_id": self._fall_global_event_id,
            "global_event_id": self._fall_global_event_id,
            "event_id_scope": "stream_global_event_id", "state": state,
            "person_detected": bool(visible), "person_count": len(visible),
            "fallen_count": len(fallen), "tracking": bool(people),
            "features": primary["features"] if primary else {
                "valid": False, "hip_drop_speed": 0.0, "hip_drop_distance": 0.0,
                "torso_angle_deg": 0.0, "bbox_aspect_ratio": 0.0,
            },
            "keypoints": [], "pose17": primary["pose17"] if primary and primary["person_detected"] else [],
            "persons": people,
        }
        # Preserve the Pro/HA compatibility envelope as additive fields while
        # the required top-level shape follows the cross-platform contract.
        contract["app"] = self.app_id
        contract["pts"] = pts
        contract["seq"] = self._seq
        contract["results_count"] = len(payload.get("results") or [])
        contract["summary"] = {
            "state": state, "event_id": self._fall_global_event_id,
            "fall_detected": bool(fallen), "person_count": len(visible),
            "fallen_count": len(fallen),
        }
        return contract

    def emit(self, payload: dict, pts: float) -> None:
        self._seq += 1
        if self.app_id == "fall-detection":
            state = self._fall_contract(payload, pts)
            body = json.dumps(state, separators=(",", ":")).encode("utf-8")
            self._safe_publish(self.state_topic, body, retain=False)
            return
        state = self._build_state(
            self.app_id, self._seq, pts,
            payload.get("results") or [], payload.get("events") or [],
        )
        body = json.dumps(state, separators=(",", ":")).encode("utf-8")
        self._safe_publish(self.state_topic, body, retain=False)

    def client_count(self) -> int:
        # For the base-loop debug line: 1 if broker link is up, else 0.
        return 1 if self._connected.is_set() else 0

    def close(self) -> None:
        self._stop.set()
        # best-effort graceful offline before tearing down
        self._safe_publish(self.status_topic, b"offline", retain=True)
        self._drop()

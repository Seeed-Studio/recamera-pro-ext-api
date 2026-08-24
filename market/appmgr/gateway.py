"""appmgr-owned result fan-in: authenticated NDJSON UDS -> one WebSocket."""
from __future__ import annotations

import json
import os
import socket
import stat
import struct
import threading
import time
from typing import Callable, Optional

from kit.adapters.result_sink import WsResultSink

from . import paths


PROTOCOL = "recamera-result-gateway@1"


class GatewayError(RuntimeError):
    pass


def _peer_pid(conn: socket.socket) -> Optional[int]:
    """Linux SO_PEERCRED pid; None on hosts that do not expose the option."""
    option = getattr(socket, "SO_PEERCRED", None)
    if option is None:
        return None
    try:
        raw = conn.getsockopt(socket.SOL_SOCKET, option, struct.calcsize("3i"))
        pid, _uid, _gid = struct.unpack("3i", raw)
        return int(pid) if pid > 1 else None
    except (OSError, struct.error):
        return None


class ResultGateway:
    """Receive per-app envelopes and broadcast one canonical WebSocket stream.

    ``identity_resolver`` is called as ``(peer_pid, claimed_app, instance,
    generation)`` and must return canonical identity or ``None``.  Production
    uses :meth:`AppCoordinator.resolve_identity`; tests can inject a resolver
    without weakening the device path.
    """

    def __init__(self, *, uds_path: Optional[str] = None,
                 ws_host: Optional[str] = None, ws_port: Optional[int] = None,
                 identity_resolver: Optional[Callable[..., Optional[dict]]] = None,
                 max_publishers: int = 32, max_line: int = 512 * 1024):
        self.uds_path = uds_path or paths.RESULT_GATEWAY_SOCK
        self.ws_host = ws_host or paths.RESULT_GATEWAY_HOST
        self.ws_port = paths.RESULT_GATEWAY_PORT if ws_port is None else int(ws_port)
        self.identity_resolver = identity_resolver
        self.max_publishers = max(1, int(max_publishers))
        self.max_line = max(4096, int(max_line))
        self._unix: Optional[socket.socket] = None
        self._ws: Optional[WsResultSink] = None
        self._stop = threading.Event()
        self._accept_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._publishers = set()
        self._socket_identity = None
        self._received = 0
        self._rejected = 0
        self._oversize = 0

    def _prepare_path(self) -> None:
        parent = os.path.dirname(self.uds_path)
        os.makedirs(parent, mode=0o755, exist_ok=True)
        try:
            st = os.lstat(self.uds_path)
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(st.st_mode):
            raise GatewayError("refusing to replace non-socket path %s" %
                               self.uds_path)
        # appmgr's single-instance lock rules out a live sibling gateway.  A
        # socket left by a crash is safe to remove; never remove broad paths.
        os.unlink(self.uds_path)

    def start(self) -> "ResultGateway":
        if self._unix is not None:
            return self
        self._stop.clear()
        self._prepare_path()
        # Bind the public endpoint first.  If :8124 is still owned by a legacy
        # app, fail appmgr startup visibly instead of running without its result
        # identity boundary.
        self._ws = WsResultSink(host=self.ws_host, port=self.ws_port,
                                app_id="appmgr", preserve_envelope=True)
        self.ws_port = self._ws.port
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            srv.bind(self.uds_path)
            os.chmod(self.uds_path, 0o660)
            srv.listen(self.max_publishers)
            srv.settimeout(0.5)
            st = os.lstat(self.uds_path)
            self._socket_identity = (st.st_dev, st.st_ino)
            self._unix = srv
            self._accept_thread = threading.Thread(target=self._accept_loop,
                                                   daemon=True,
                                                   name="appmgr-result-ingress")
            self._accept_thread.start()
            return self
        except Exception:
            srv.close()
            if self._ws is not None:
                self._ws.close()
                self._ws = None
            raise

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._unix.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._lock:
                if len(self._publishers) >= self.max_publishers:
                    self._rejected += 1
                    conn.close()
                    continue
                self._publishers.add(conn)
            threading.Thread(target=self._serve_publisher, args=(conn,),
                             daemon=True, name="appmgr-result-publisher").start()

    @staticmethod
    def _ack(conn: socket.socket, ok: bool, error: str = "") -> None:
        obj = {"type": "hello_ack", "protocol": PROTOCOL, "ok": bool(ok)}
        if error:
            obj["error"] = error
        conn.sendall((json.dumps(obj, separators=(",", ":")) + "\n").encode())

    def _readline(self, stream) -> bytes:
        line = stream.readline(self.max_line + 2)
        if len(line) > self.max_line or (line and not line.endswith(b"\n")):
            self._oversize += 1
            raise GatewayError("publisher message exceeds %d bytes" % self.max_line)
        return line

    def _serve_publisher(self, conn: socket.socket) -> None:
        peer_pid = _peer_pid(conn)
        stream = None
        try:
            conn.settimeout(5.0)
            stream = conn.makefile("rb")
            line = self._readline(stream)
            if not line:
                raise GatewayError("publisher closed before hello")
            hello = json.loads(line.decode("utf-8"))
            if (not isinstance(hello, dict) or hello.get("type") != "hello"
                    or hello.get("protocol") != PROTOCOL):
                raise GatewayError("invalid publisher hello")
            claimed_app = str(hello.get("app", ""))
            instance = str(hello.get("instance", ""))
            try:
                generation = int(hello.get("generation"))
            except (TypeError, ValueError):
                raise GatewayError("invalid publisher generation")
            if self.identity_resolver is None:
                raise GatewayError("gateway identity resolver is not configured")
            identity = self.identity_resolver(peer_pid, claimed_app,
                                              instance, generation)
            if not isinstance(identity, dict):
                raise GatewayError("publisher identity rejected")
            self._ack(conn, True)
            conn.settimeout(None)
            while not self._stop.is_set():
                line = self._readline(stream)
                if not line:
                    break
                try:
                    obj = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    self._rejected += 1
                    continue
                if not isinstance(obj, dict) or obj.get("type") == "hello":
                    self._rejected += 1
                    continue
                # Never trust identity fields in an application payload.
                obj["app"] = identity["app_id"]
                obj["instance"] = identity["instance_id"]
                obj["generation"] = int(identity["generation"])
                obj.setdefault("type", "results")
                obj["gateway_ts"] = time.time()
                self._ws.publish_envelope(obj)
                self._received += 1
        except Exception as exc:
            self._rejected += 1
            try:
                self._ack(conn, False, str(exc))
            except Exception:
                pass
        finally:
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
            try:
                conn.close()
            except OSError:
                pass
            with self._lock:
                self._publishers.discard(conn)

    def status(self) -> dict:
        with self._lock:
            publishers = len(self._publishers)
        return {
            "running": self._unix is not None and not self._stop.is_set(),
            "uds": self.uds_path,
            "ws_host": self.ws_host,
            "ws_port": self.ws_port,
            "publishers": publishers,
            "subscribers": self._ws.client_count() if self._ws else 0,
            "received": self._received,
            "rejected": self._rejected,
            "oversize": self._oversize,
        }

    def stop(self) -> None:
        self._stop.set()
        srv, self._unix = self._unix, None
        if srv is not None:
            try:
                srv.close()
            except OSError:
                pass
        with self._lock:
            publishers = list(self._publishers)
            self._publishers.clear()
        for conn in publishers:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
        if self._ws is not None:
            self._ws.close()
            self._ws = None
        if (self._accept_thread is not None
                and self._accept_thread is not threading.current_thread()):
            self._accept_thread.join(timeout=1.0)
        self._accept_thread = None
        # Do not unlink a replacement socket created by a restarted manager.
        try:
            st = os.lstat(self.uds_path)
            if self._socket_identity == (st.st_dev, st.st_ino):
                os.unlink(self.uds_path)
        except FileNotFoundError:
            pass
        self._socket_identity = None

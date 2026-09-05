"""Enrollment/admin HTTP endpoint for face-recognition.

stdlib only (`http.server`), a daemon `ThreadingHTTPServer` bound to
127.0.0.1:<cmd_port> by default — enrollment writes the gallery, so it is a
loopback control channel, not a second public port. Bind 0.0.0.0 only behind
something that authenticates.

    POST /cmd  {"op":"enroll","name":"alice","source":"camera","frames":5}
               {"op":"enroll","name":"alice","source":"image","image_b64":"..."}
               {"op":"remove","name":"alice"}
               {"op":"list"}
               {"op":"reload"}
    GET  /gallery                       same as {"op":"list"}

Every response: ``{"op", "ok", "model_tag", "users", ...}`` plus ``"err"`` when
``ok`` is false.

★Why enrollment does not run here★ the NPU is single-core and the frame loop
owns it. Both enroll sources are therefore posted to a queue and executed by
`run()` between frames; this handler only blocks on the future (default 15 s).
That is also what makes "camera" work at all: the frames it needs arrive in the
loop, not in this thread.
"""
from __future__ import annotations

import json
import threading
from concurrent.futures import Future, TimeoutError as FutureTimeout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

DEFAULT_TIMEOUT = 15.0
MAX_BODY = 8 * 1024 * 1024      # a base64 JPEG, generously
VALID_OPS = ("enroll", "remove", "list", "reload")


class CmdServer:
    """Owns the HTTP thread. `ops` supplies the four operations.

    `ops` must provide::

        model_tag        -> str
        user_count()     -> int
        list_users()     -> list[dict]
        remove_user(str) -> bool
        reload()         -> int
        submit_enroll(req: dict) -> concurrent.futures.Future
    """

    def __init__(self, port: int, ops: Any, host: str = "127.0.0.1",
                 timeout: float = DEFAULT_TIMEOUT) -> None:
        self.port = int(port)
        self.host = str(host)
        self.ops = ops
        self.timeout = float(timeout)
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle ------------------------------------------------------ #
    def start(self) -> "CmdServer":
        if self.port < 0:
            # Disabled. Port 0 keeps its socket meaning ("any free port", which
            # the tests use); the app maps a configured 0 to -1.
            return self
        server = self                       # closed over by the handler class

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):      # keep the app's stdout clean
                pass

            def _send(self, status: int, body: Dict[str, Any]) -> None:
                raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path.rstrip("/") in ("/gallery", "/cmd"):
                    status, body = server.handle({"op": "list"})
                    self._send(status, body)
                else:
                    self._send(404, server.envelope("unknown", False,
                                                    err="not found"))

            def do_POST(self):
                if self.path.rstrip("/") != "/cmd":
                    self._send(404, server.envelope("unknown", False,
                                                    err="not found"))
                    return
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    n = 0
                if n > MAX_BODY:
                    self._send(413, server.envelope("unknown", False,
                                                    err="body too large"))
                    return
                raw = self.rfile.read(n) if n > 0 else b""
                try:
                    req = json.loads(raw.decode("utf-8")) if raw else {}
                    if not isinstance(req, dict):
                        raise ValueError("body must be a JSON object")
                except Exception as e:          # noqa: BLE001
                    self._send(400, server.envelope("unknown", False,
                                                    err=f"bad JSON: {e}"))
                    return
                status, body = server.handle(req)
                self._send(status, body)

        self._httpd = ThreadingHTTPServer((self.host, self.port), _Handler)
        self._httpd.daemon_threads = True
        # Port 0 means "any free port" (tests); publish what we actually got.
        self.port = int(self._httpd.server_address[1])
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="face-cmd", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    # -- request handling (unit-testable without a socket) --------------- #
    def envelope(self, op: str, ok: bool, **extra) -> Dict[str, Any]:
        body: Dict[str, Any] = {"op": op, "ok": bool(ok)}
        try:
            body["model_tag"] = self.ops.model_tag
            body["users"] = int(self.ops.user_count())
        except Exception:                       # noqa: BLE001
            body["model_tag"] = None
            body["users"] = 0
        body.update(extra)
        return body

    def handle(self, req: Dict[str, Any]):
        """Dispatch one request. Returns ``(http_status, body_dict)``."""
        op = str(req.get("op") or "").strip().lower()
        if op not in VALID_OPS:
            return 400, self.envelope(op or "unknown", False,
                                      err=f"unknown op; expected one of "
                                          f"{list(VALID_OPS)}")
        try:
            if op == "list":
                return 200, self.envelope(op, True, names=self.ops.list_users())
            if op == "reload":
                n = self.ops.reload()
                return 200, self.envelope(op, True, reloaded=int(n))
            if op == "remove":
                name = str(req.get("name") or "").strip()
                if not name:
                    return 400, self.envelope(op, False, err="missing 'name'")
                if not self.ops.remove_user(name):
                    return 404, self.envelope(op, False,
                                              err=f"no such user: {name}")
                return 200, self.envelope(op, True, name=name)
            # -- enroll ------------------------------------------------- #
            name = str(req.get("name") or "").strip()
            if not name:
                return 400, self.envelope(op, False, err="missing 'name'")
            source = str(req.get("source") or "camera").strip().lower()
            if source not in ("camera", "image"):
                return 400, self.envelope(op, False,
                                          err="source must be 'camera' or 'image'")
            if source == "image" and not req.get("image_b64"):
                return 400, self.envelope(op, False,
                                          err="source 'image' needs 'image_b64'")
            job = {"name": name, "source": source,
                   "frames": int(req.get("frames") or 0),
                   "image_b64": req.get("image_b64")}
            fut: Future = self.ops.submit_enroll(job)
            try:
                res = fut.result(timeout=self.timeout)
            except FutureTimeout:
                fut.cancel()
                return 504, self.envelope(op, False, name=name,
                                          err=f"enroll timed out after "
                                              f"{self.timeout:g}s (no usable "
                                              f"face in view?)")
            except Exception as e:              # noqa: BLE001
                return 400, self.envelope(op, False, name=name, err=str(e))
            return 200, self.envelope(op, True, **{"name": name, **(res or {})})
        except Exception as e:                  # noqa: BLE001
            return 500, self.envelope(op, False, err=str(e))

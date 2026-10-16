"""最小 Web 面板：健康状态、MJPEG 预览、最近事件。三样，没有别的。

用标准库 http.server，不引 Flask/FastAPI：容器里少一层依赖，
且这个面板只做只读展示，没有表单和状态变更。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

LOGGER = logging.getLogger(__name__)

BOUNDARY = "inspectionframe"
INDEX_FALLBACK = """<!doctype html><meta charset="utf-8">
<title>edge-inspection-assembly</title>
<body style="font-family:system-ui;margin:1rem">
<h1>edge-inspection-assembly</h1>
<img src="/preview.mjpg" style="max-width:100%">
<pre id="s"></pre>
<script>
setInterval(async()=>{const r=await fetch('/healthz');
document.getElementById('s').textContent=JSON.stringify(await r.json(),null,2);},1000);
</script>
</body>"""


class PreviewState:
    """最新一帧 JPEG + 最近事件环形缓冲。推理线程写，HTTP 线程读。"""

    def __init__(self, max_events: int = 50) -> None:
        self._lock = threading.Lock()
        self._jpeg: bytes | None = None
        self._jpeg_seq = 0
        self._events: deque[dict] = deque(maxlen=max_events)
        self._new_frame = threading.Condition(self._lock)

    def set_frame(self, jpeg: bytes) -> None:
        with self._new_frame:
            self._jpeg = jpeg
            self._jpeg_seq += 1
            self._new_frame.notify_all()

    def wait_frame(self, last_seq: int, timeout: float = 2.0
                   ) -> tuple[bytes | None, int]:
        with self._new_frame:
            if self._jpeg_seq == last_seq:
                self._new_frame.wait(timeout)
            return self._jpeg, self._jpeg_seq

    def add_event(self, event: dict) -> None:
        with self._lock:
            self._events.append(event)

    def recent_events(self, limit: int = 20) -> list[dict]:
        with self._lock:
            return list(self._events)[-limit:][::-1]


def _handler_factory(state: PreviewState, health: Callable[[], dict[str, Any]],
                     preview_fps: float, index_html: str):
    min_interval = 1.0 / preview_fps if preview_fps > 0 else 0.0

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "edge-inspection/1.0"

        def log_message(self, fmt, *args):  # noqa: A003 - 覆盖标准库噪音日志
            LOGGER.debug("web %s", fmt % args)

        def _send_json(self, payload: Any, status: int = 200) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_bytes(self, body: bytes, content_type: str,
                        status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - 标准库接口
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self._send_bytes(index_html.encode("utf-8"),
                                 "text/html; charset=utf-8")
            elif path in ("/healthz", "/health", "/api/health"):
                self._send_json(health())
            elif path in ("/events", "/api/events"):
                self._send_json({"events": state.recent_events()})
            elif path in ("/snapshot.jpg", "/preview.jpg"):
                jpeg, _ = state.wait_frame(-1, timeout=2.0)
                if jpeg is None:
                    self._send_json({"error": "no frame yet"}, status=503)
                else:
                    self._send_bytes(jpeg, "image/jpeg")
            elif path == "/preview.mjpg":
                self._stream_mjpeg()
            else:
                self._send_json({"error": "not found"}, status=404)

        def _stream_mjpeg(self) -> None:
            self.send_response(200)
            self.send_header(
                "Content-Type",
                f"multipart/x-mixed-replace; boundary={BOUNDARY}")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            last_seq = -1
            try:
                while True:
                    jpeg, seq = state.wait_frame(last_seq, timeout=5.0)
                    if jpeg is None:
                        time.sleep(0.2)
                        continue
                    last_seq = seq
                    self.wfile.write(
                        f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
                        f"Content-Length: {len(jpeg)}\r\n\r\n".encode("ascii"))
                    self.wfile.write(jpeg)
                    self.wfile.write(b"\r\n")
                    if min_interval:
                        time.sleep(min_interval)
            except (BrokenPipeError, ConnectionResetError):
                return

    return Handler


class WebPanel:
    """在后台线程跑的 HTTP 面板。`enabled=false` 时整体是个空壳。"""

    def __init__(self, config: dict[str, Any], state: PreviewState,
                 health: Callable[[], dict[str, Any]]) -> None:
        self.enabled = bool(config.get("enabled", True))
        self.bind = str(config.get("bind", "0.0.0.0"))
        self.port = int(config.get("port", 8080))
        self.preview_fps = float(config.get("preview_fps", 5))
        self.state = state
        self._health = health
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @staticmethod
    def _index_html() -> str:
        page = Path(__file__).resolve().parents[2] / "web" / "index.html"
        if page.exists():
            return page.read_text(encoding="utf-8")
        return INDEX_FALLBACK

    def start(self) -> None:
        if not self.enabled:
            return
        handler = _handler_factory(self.state, self._health, self.preview_fps,
                                   self._index_html())
        self._server = ThreadingHTTPServer((self.bind, self.port), handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True, name="web-panel")
        self._thread.start()
        LOGGER.info("web panel on http://%s:%s", self.bind, self.port)

    @property
    def actual_port(self) -> int:
        return self._server.server_address[1] if self._server else self.port

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

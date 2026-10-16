"""最小 HTTP 面板：健康检查 + 触发即拍。

只用标准库 `http.server`，不引 Flask/FastAPI——运行镜像里少一层依赖，
`/healthz` 也就不会因为 web 框架自己的初始化顺序而在启动早期 500。

三个端点：

  GET  /healthz   compose 的 healthcheck 打这个，200 = 进程活着且源在出帧
  GET  /metrics   同一份 JSON，语义上给采集侧用，两者内容一致
  POST /trigger   spec §5「触发即拍」的 http 来源；也接受 GET 便于 curl 冒烟

`/trigger` 只把请求塞进一个容量 1 的槽位，真正的拍照与推理在主循环里做：
HTTP 线程不碰加速器 context（不是线程安全的），也不会因为一次推理阻塞住
healthcheck。`merge_pending=true` 时槽位已占就直接算「合并」，不排队。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

LOGGER = logging.getLogger(__name__)


class TriggerBox:
    """容量 1 的触发槽。debounce_ms 内的重复触发直接丢弃并计数。"""

    def __init__(self, debounce_ms: int = 800, merge_pending: bool = True) -> None:
        self.debounce_ms = int(debounce_ms)
        self.merge_pending = bool(merge_pending)
        self._lock = threading.Lock()
        self._pending: str | None = None
        self._last_accept_ms = 0
        self.triggered = 0
        self.debounced = 0
        self.merged = 0

    def offer(self, source: str = "http") -> dict[str, Any]:
        now_ms = int(time.monotonic() * 1000)
        with self._lock:
            if now_ms - self._last_accept_ms < self.debounce_ms:
                self.debounced += 1
                return {"accepted": False, "reason": "debounced"}
            if self._pending is not None:
                # 已有一次在飞。merge_pending 时算合并（不排队），
                # 否则拒绝——两种都不入队，队列会把延迟越堆越长。
                if self.merge_pending:
                    self.merged += 1
                    return {"accepted": False, "reason": "merged"}
                return {"accepted": False, "reason": "busy"}
            self._pending = source
            self._last_accept_ms = now_ms
            self.triggered += 1
            return {"accepted": True, "reason": "queued"}

    def take(self) -> str | None:
        """主循环取走待处理的触发。没有就返回 None。"""
        with self._lock:
            source, self._pending = self._pending, None
            return source

    def stats(self) -> dict[str, Any]:
        return {"triggered": self.triggered, "debounced": self.debounced,
                "merged": self.merged, "debounce_ms": self.debounce_ms}


def _handler_factory(health: Callable[[], dict], trigger: TriggerBox):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # noqa: A003 - 静音默认 stderr 日志
            LOGGER.debug("web %s", fmt % args)

        def _send(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _route(self) -> None:
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path in ("/healthz", "/metrics"):
                snapshot = health()
                ok = snapshot.get("status") in ("ok", "starting")
                self._send(200 if ok else 503, snapshot)
            elif path == "/trigger":
                self._send(200, trigger.offer("http"))
            elif path == "/":
                self._send(200, {"service": "edge-waste-sorting",
                                 "endpoints": ["/healthz", "/metrics",
                                               "/trigger"]})
            else:
                self._send(404, {"error": "not found", "path": path})

        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 的命名约定
            self._route()

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            self._route()

    return Handler


class WebPanel:
    """在后台线程里跑的 HTTP 面板。`enabled=false` 时 start/stop 都是空操作。"""

    def __init__(self, config: dict[str, Any], health: Callable[[], dict],
                 trigger: TriggerBox) -> None:
        self.enabled = bool(config.get("enabled", True))
        self.bind = str(config.get("bind", "0.0.0.0"))
        self.port = int(config.get("port", 8080))
        self._health = health
        self._trigger = trigger
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.enabled:
            return
        handler = _handler_factory(self._health, self._trigger)
        self._server = ThreadingHTTPServer((self.bind, self.port), handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="web", daemon=True)
        self._thread.start()
        LOGGER.info("web panel on http://%s:%d", self.bind, self.port)

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

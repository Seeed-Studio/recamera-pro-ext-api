"""http_control -- daemon 自带的参数 HTTP 接口（stdlib http.server，零依赖）。

路由（默认端口 8099，可用 http_port 配置）：
  GET  /config        -> 当前生效参数（JSON）
  POST /config        -> 一次性改参数：body 为部分 JSON，例如
                         {"sq_diff_threshold": 600, "min_changed_ratio": 0.01}
                         合法项立即热生效并写回 config.json 持久化；
                         返回 {"applied": {...}, "ignored": {...},
                               "restart_required": [...]}
  GET  /state         -> 当前运动状态快照（JSON）

鉴权：config 里 http_token 非空时，要求请求头 Authorization: Bearer <token>。
注意：默认监听 0.0.0.0（局域网内任何人可改参数），生产建议配置 http_token。

服务跑在独立守护线程；任何 handler 异常只回 500，绝不影响检测主循环。
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class ControlServer:
    def __init__(self, host: str, port: int, token: str, *,
                 get_config, update_config, get_state, log=print):
        self.host = host
        self.port = int(port)
        self.token = token or ""
        self._get_config = get_config
        self._get_state = get_state
        self._update = update_config
        self.log = log
        self._httpd = None
        self._thread = None

    def start(self) -> bool:
        if self.port <= 0:
            self.log("[http] http_port<=0，参数接口关闭")
            return False
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):      # 静音默认访问日志
                pass

            def _authorized(self):
                if not server.token:
                    return True
                return self.headers.get("Authorization") == \
                    f"Bearer {server.token}"

            def _json(self, code, obj):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                try:
                    if not self._authorized():
                        return self._json(401, {"error": "unauthorized"})
                    if self.path == "/config":
                        return self._json(200, server._get_config())
                    if self.path == "/state":
                        return self._json(200, server._get_state())
                    self._json(404, {"error": "not found"})
                except Exception as exc:                # noqa: BLE001
                    self._json(500, {"error": str(exc)})

            def do_POST(self):
                try:
                    if not self._authorized():
                        return self._json(401, {"error": "unauthorized"})
                    if self.path != "/config":
                        return self._json(404, {"error": "not found"})
                    length = int(self.headers.get("Content-Length") or 0)
                    if length <= 0 or length > 65536:
                        return self._json(400, {"error": "bad content length"})
                    try:
                        body = json.loads(self.rfile.read(length))
                    except json.JSONDecodeError as exc:
                        return self._json(400, {"error": f"bad json: {exc}"})
                    if not isinstance(body, dict):
                        return self._json(400, {"error": "expect json object"})
                    self._json(200, server._update(body))
                except Exception as exc:                # noqa: BLE001
                    self._json(500, {"error": str(exc)})

        try:
            self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        except OSError as exc:
            self.log(f"[http] 监听 {self.host}:{self.port} 失败：{exc}"
                     "（参数接口关闭，检测不受影响）")
            return False
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        daemon=True, name="http-control")
        self._thread.start()
        self.log(f"[http] 参数接口：http://{self.host}:{self.port} "
                 f"(GET/POST /config, GET /state)")
        return True

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()

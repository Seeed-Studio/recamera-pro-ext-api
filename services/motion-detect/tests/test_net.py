"""http_control / ha_mqtt 的离线测试（纯标准库，无设备）。

运行：python3 -m pytest tests/test_net.py -q   （在 services/motion-detect 目录下）
"""
import json
import time
import urllib.request

import pytest

from http_control import ControlServer


# --------------------------------------------------------------------------- #
# HTTP 参数接口
# --------------------------------------------------------------------------- #
class TestHttpControl:
    @pytest.fixture
    def server(self):
        import socket as _s
        probe = _s.socket(); probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]; probe.close()
        self.cfg = {"sq_diff_threshold": 400.0, "min_changed_ratio": 0.005}
        self.state = {"motion": False, "score": 0.0}
        srv = ControlServer(
            "127.0.0.1", free_port, "tok",
            get_config=lambda: dict(self.cfg),
            update_config=self._update,
            get_state=lambda: dict(self.state),
            log=lambda m: None)
        assert srv.start() is True
        # ThreadingHTTPServer 绑 0 端口后取真实端口
        srv.real_port = srv._httpd.server_address[1]
        yield srv
        srv.stop()

    def _update(self, partial):
        applied = {k: v for k, v in partial.items() if k in self.cfg}
        self.cfg.update(applied)
        return {"applied": applied,
                "ignored": {k: v for k, v in partial.items() if k not in self.cfg},
                "restart_required": []}

    def _req(self, srv, method, path, body=None, token="tok"):
        url = f"http://127.0.0.1:{srv.real_port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_get_config_and_state(self, server):
        code, cfg = self._req(server, "GET", "/config")
        assert code == 200 and cfg["sq_diff_threshold"] == 400.0
        code, st = self._req(server, "GET", "/state")
        assert code == 200 and st["motion"] is False

    def test_post_config_applies(self, server):
        code, rep = self._req(server, "POST", "/config",
                              {"sq_diff_threshold": 600, "bogus": 1})
        assert code == 200
        assert rep["applied"] == {"sq_diff_threshold": 600}
        assert rep["ignored"] == {"bogus": 0} or "bogus" in rep["ignored"]
        code, cfg = self._req(server, "GET", "/config")
        assert cfg["sq_diff_threshold"] == 600

    def test_auth_required(self, server):
        code, _ = self._req(server, "GET", "/config", token="wrong")
        assert code == 401

    def test_bad_json(self, server):
        url = f"http://127.0.0.1:{server.real_port}/config"
        req = urllib.request.Request(url, data=b"{oops", method="POST")
        req.add_header("Authorization", "Bearer tok")
        try:
            urllib.request.urlopen(req, timeout=5)
            assert False, "应返回 400"
        except urllib.error.HTTPError as e:
            assert e.code == 400


class TestHaMqttBridge:
    def test_disabled_without_host(self):
        from ha_mqtt import HaMqttBridge
        b = HaMqttBridge({"mqtt_host": ""}, log=lambda m: None)
        assert not b.enabled
        b.emit([{"kind": "motion_state", "motion": True}], 1.0)   # 不抛
        b.close()

    def test_disabled_gracefully_without_kit(self, monkeypatch):
        # 开发机无 kit：有 host 也应安全降级为空操作
        import builtins, ha_mqtt
        real_import = builtins.__import__
        def fake_import(name, *a, **k):
            if name.startswith("kit"):
                raise ImportError("no kit on host")
            return real_import(name, *a, **k)
        monkeypatch.setattr(builtins, "__import__", fake_import)
        b = ha_mqtt.HaMqttBridge({"mqtt_host": "broker.local"}, log=lambda m: None)
        assert not b.enabled
        b.emit([], 1.0)
        b.close()

    def test_emit_maps_to_kit_sink(self, monkeypatch):
        import sys, types
        sent = []
        class FakeSink:
            def __init__(self, **kw): self.kw = kw
            def emit(self, payload, pts): sent.append((payload, pts))
            def close(self): pass
        sent = []
        mod = types.ModuleType("kit.adapters.mqtt_sink")
        mod.MqttSink = FakeSink
        monkeypatch.setitem(sys.modules, "kit", types.ModuleType("kit"))
        monkeypatch.setitem(sys.modules, "kit.adapters",
                            types.ModuleType("kit.adapters"))
        monkeypatch.setitem(sys.modules, "kit.adapters.mqtt_sink", mod)
        import ha_mqtt
        b = ha_mqtt.HaMqttBridge({"mqtt_host": "h", "mqtt_port": 1883},
                                 log=lambda m: None)
        assert b.enabled
        b.emit([{"kind": "motion_state", "motion": True, "score": 0.5}], 2.0)
        assert sent[0][0]["events"][0]["motion"] is True
        assert sent[0][1] == 2.0
        assert b._sink.kw["app_id"] == "motion-detect"

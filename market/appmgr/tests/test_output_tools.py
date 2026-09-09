import copy
import http.client
import json
import os
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from appmgr import config, output_tools, paths, server

MANIFEST = {"id": "counter", "name": "Counter", "capabilities": ["output"]}


def preview(values, app_id="counter", **extra):
    return output_tools.preview(app_id, {"values": values, **extra}, manifest=MANIFEST,
                                base=config.schema_defaults(MANIFEST))


@contextmanager
def serving(handler):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        worker.join(timeout=2)


def test_preview_uses_draft_formatter_without_writing_or_sending(monkeypatch):
    values = {"iMode": "custom", "template_mode": "mapping", "output_mapping": [
        {"source": "detection.count", "target": "count", "topic": "camera/{{ app }}/result"}],
        "dMqtt": {"sURL": "unused", "sPassword": "never-echo-this"}}
    original = copy.deepcopy(values)
    monkeypatch.setattr(output_tools, "_mqtt_test", lambda *_: pytest.fail("preview opened network"))
    monkeypatch.setattr(config, "write_user_config", lambda *_: pytest.fail("preview saved config"))
    result = preview(values)
    assert json.loads(result["messages"][0]["body"]) == {"count": 1}
    assert result["messages"][0]["topic"] == "camera/counter/result"
    assert values == original
    assert "never-echo-this" not in json.dumps(result)


def test_builtin_default_and_custom_template_preview():
    result = preview({"dTemplate": {}}, "builtin")
    assert json.loads(result["messages"][0]["body"])["detection"]["count"] == 1
    result = preview({"dTemplate": {"sDetection": "count={{ detection.count }}"}}, "builtin")
    assert result["messages"][0]["body"] == "count=1"
    result = preview({"dTemplate": {"sDefault": "time={{ flat.timestamp_ms }}"}}, "builtin")
    assert result["messages"][0]["body"] == "time=1700000000000"


@pytest.mark.parametrize("task,template,expected", [
    ("detection", "{{ task_type }}:{{ model_id }}:{{ detection.entries[0].class_name }}", "1:0:person"),
    ("classification", "{{ classification.entries[0].class_id }}", "0"),
    ("keypoint", "{{ keypoints.instances[0].point_count }}:{{ keypoints.instances[0].points[0].keypoint_id }}", "1:0"),
    ("segmentation", "{{ segmentation.entries[0].mask_width }}:{{ segmentation.entries[0].mask_size }}", "16:256"),
    ("tracking", "{{ tracking.entries[0].track_id }}", "1"),
])
def test_builtin_preview_exposes_the_notify_parser_task_fields(task, template, expected):
    result = preview({"dTemplate": {output_tools._TASKS[task]: template}}, "builtin", task=task)
    assert result["messages"][0]["body"] == expected


def test_builtin_keypoint_preview_does_not_advertise_kit_or_legacy_point_structures():
    result = preview({"dTemplate": {}}, "builtin", task="keypoint")
    data = json.loads(result["messages"][0]["body"])
    assert data["task_type_name"] == "keypoints"
    assert "entries" not in data["keypoints"]
    assert "keypoints" not in data["keypoints"]["instances"][0]
    assert data["timestamp_ms"] == 1700000000000
    assert isinstance(data["timestamp"], str)


@pytest.mark.parametrize("template", ["{{ broken", "{{ 'x' * 20000000 }}", "{{ ''.__class__.__mro__ }}"])
def test_invalid_or_unbounded_templates_never_report_success(template):
    for app_id, values in [
        ("builtin", {"dTemplate": {"sDetection": template}}),
        ("counter", {"iMode": "custom", "template_mode": "template", "dTemplate": {"sDetection": template}}),
    ]:
        result = preview(values, app_id)
        assert result["messages"] == []
        # Runtime undefined-variable errors are a no-message result; compile
        # and resource-limit errors have the same explicit error as the UI.
        assert result.get("error") in (None, "templateInvalid")


def test_http_test_posts_one_marked_message_and_preserves_credentials():
    received = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.path, dict(self.headers), json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *_):
            pass

    with serving(Receiver) as httpd:
        values = {"output_channels": ["http"], "dHttp": {
            "sUrl": "http://127.0.0.1:%d/results?camera=1" % httpd.server_port, "sToken": "test-token"}}
        result = output_tools.test_delivery("counter", {"values": values, "channel": "http"}, manifest=MANIFEST, base={})
    assert result["ok"] and result["status"] == 204
    assert len(received) == 1
    path, headers, payload = received[0]
    assert path == "/results?camera=1"
    assert headers["Authorization"] == "Bearer test-token"
    assert headers["X-Recamera-Test"] == "1"
    assert payload["type"] == "test" and payload["source"] == "counter"
    assert "test-token" not in json.dumps(result)


def test_http_redirect_is_not_followed():
    hits = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            hits.append(self.path)
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(302)
            self.send_header("Location", "/redirected")
            self.end_headers()

        def log_message(self, *_):
            pass

    with serving(Receiver) as httpd:
        result = output_tools.test_delivery("builtin", {"channel": "http", "values": {
            "iMode": 2, "dHttp": {"sUrl": "http://127.0.0.1:%d/start" % httpd.server_port}}}, manifest={}, base={})
    assert hits == ["/start"]
    assert result["ok"] is False and result["reason"] == "receiverRejected"


def test_mqtt_uses_unique_client_and_non_retained_test_message(monkeypatch):
    calls = []

    class Connection:
        def __init__(self, host, port, client_id, **kwargs):
            assert host == "broker.local" and port == 1883
            assert client_id.startswith("rc-test-") and client_id != "running-client"
            assert kwargs == {"username": "owner", "password": "secret"}

        def connect(self, timeout):
            assert timeout == 5

        def publish(self, topic, payload, retain):
            calls.append((topic, json.loads(payload), retain))

        def close(self):
            calls.append("closed")

    monkeypatch.setattr(output_tools, "_MqttConnection", Connection)
    result = output_tools.test_delivery("counter", {"channel": "mqtt", "values": {
        "output_channels": ["mqtt"], "dMqtt": {"sURL": "broker.local", "iPort": 1883,
        "sTopic": "camera/results", "sClientId": "running-client", "sUsername": "owner", "sPassword": "secret"}}}, manifest=MANIFEST, base={})
    assert result["ok"] is True
    assert calls[0][0] == "camera/results" and calls[0][1]["type"] == "test" and calls[0][2] is False
    assert calls[1] == "closed"


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://user:password@localhost/", "http://localhost/#fragment"])
def test_invalid_test_destination_rejected_before_connection(url):
    result = output_tools.test_delivery("counter", {"channel": "http", "values": {
        "output_channels": ["http"], "dHttp": {"sUrl": url}}}, manifest=MANIFEST, base={})
    assert result["ok"] is False and result["reason"] == "invalidSettings"


def test_dormant_channels_and_overlapping_tests_are_rejected():
    with pytest.raises(ValueError):
        output_tools.test_delivery("counter", {"channel": "http", "values": {
            "output_channels": ["ws"], "dHttp": {"sUrl": "http://unused"}}}, manifest=MANIFEST, base={})
    assert output_tools._SLOTS.acquire(blocking=False)
    assert output_tools._SLOTS.acquire(blocking=False)
    try:
        result = output_tools.test_delivery("builtin", {"channel": "http", "values": {"iMode": 2}}, manifest={}, base={})
        assert result["reason"] == "busy"
    finally:
        output_tools._SLOTS.release()
        output_tools._SLOTS.release()


def test_endpoint_preserves_origin_guard_and_installed_app_requirement(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "APPS_DIR", str(tmp_path / "apps"))
    with serving(server._Handler) as httpd:
        def request(app_id, origin=None):
            connection = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=2)
            try:
                headers = {"Content-Type": "application/json"}
                if origin:
                    headers["Origin"] = origin
                connection.request("POST", "/api/app-center/v1/apps/%s/output/preview" % app_id,
                                   body=json.dumps({"values": {"dTemplate": {}}}), headers=headers)
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()

        assert request("builtin", "http://untrusted.example")[0] == 403
        status, result = request("builtin")
        assert status == 200 and result["sample"] is True
        assert request("missing-app")[0] == 404

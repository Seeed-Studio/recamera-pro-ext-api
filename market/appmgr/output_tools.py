"""Draft message previews and explicit, one-shot delivery diagnostics.

Never saves configuration, starts an app, injects inference results, or publishes
HA discovery/retained state. Network tests send only a fixed, marked test body.
The HTTP handler supplies the installed manifest and persisted defaults.
"""
from __future__ import annotations

import http.client
import json
import threading
import time
import uuid
from urllib.parse import urlsplit

from kit.adapters.mqtt_sink import _MqttConnection, device_identifier
from kit.adapters.output_sink import (
    MAX_TEMPLATE_LEN, _bounded_render, build_formatter, make_restricted_env,
    resolve_output_config,
)
from . import config as appconfig

_SLOTS = threading.BoundedSemaphore(2)
_TIMEOUT = 5.0
_TASKS = {"detection": "sDetection", "classification": "sClassification",
          "keypoint": "sKeypoint", "segmentation": "sSegmentation",
          "tracking": "sTracking", "obb": "sOBB"}
_OUTPUT_KEYS = frozenset(("output_channels", "iMode", "template_mode", "dTemplate",
                          "output_mapping", "output_filters", "dMqtt", "dHttp", "dUart"))


def _values(body: dict, base: dict) -> dict:
    incoming = body.get("values", {})
    if not isinstance(incoming, dict) or set(incoming) - _OUTPUT_KEYS:
        raise ValueError("invalid output settings")
    return {**base, **incoming}


def _sample(app_id: str, task: str) -> dict:
    entry = {"box": {"left": 0.1, "top": 0.2, "right": 0.4, "bottom": 0.8},
             "score": 0.95, "class_id": 0, "cls": 0, "cls_name": "person"}
    if task == "classification":
        entry.pop("box")
        entry.update(kind="classification", label="person")
    elif task == "keypoint":
        entry["keypoints"] = [{"x": 0.25, "y": 0.3, "score": 0.95}]
    elif task == "tracking":
        entry["track_id"] = 1
    elif task == "segmentation":
        entry["polygon"] = [[0.1, 0.2], [0.4, 0.2], [0.4, 0.8]]
    return {"app": app_id, "timestamp": 1700000000000, "seq": 1,
            "frame": {"width": 640, "height": 480, "pts": 0},
            "results": [entry], "events": []}


def _builtin_preview(values: dict, task: str, sample: dict) -> list:
    templates = values.get("dTemplate") or {}
    if (not isinstance(templates, dict) or len(templates) > 32
            or any(not isinstance(text, str) or len(text) > MAX_TEMPLATE_LEN
                   for text in templates.values())):
        raise ValueError("invalid templates")
    section = "keypoints" if task == "keypoint" else task
    # Mirror the notify parser's task structures. Kit result entries are a
    # different contract (notably keypoints vs instances[].points), so reusing
    # them would make valid template fields appear unavailable in previews.
    entry = {"score": 0.95, "class_id": 0, "class_name": "person"}
    if task != "classification":
        entry["box"] = {"left": 0.1, "top": 0.2, "right": 0.4, "bottom": 0.8}
    if task == "tracking":
        entry["track_id"] = 1
    if task == "segmentation":
        entry.update(mask_width=16, mask_height=16, mask_size=256)
    if task == "keypoint":
        entry.update(point_count=1, points=[{
            "x": 0.25, "y": 0.3, "score": 0.95, "keypoint_id": 0}])
    data = {"task_type_name": section,
            "task_type": {"classification": 0, "detection": 1,
                          "segmentation": 2, "tracking": 3, "keypoint": 4}.get(task),
            "model_id": 0, "timestamp": "2023-11-14T22:13:20",
            "timestamp_ms": sample["timestamp"],
            section: {"count": 1, "instances" if task == "keypoint" else "entries": [entry]}}
    flat = {}

    def flatten(value, prefix=""):
        for key, item in value.items():
            path = "%s_%s" % (prefix, key) if prefix else key
            if isinstance(item, dict):
                flatten(item, path)
            else:
                flat[path] = item

    flatten(data)
    template = templates.get(_TASKS[task], "").strip() or templates.get("sDefault", "").strip()
    body = (_bounded_render(make_restricted_env().from_string(template),
                            **data, data=data, inference=data, flat=flat)
            if template else json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    return [{"body": body, "topic": None}] if body.strip() else []


def preview(app_id: str, body: dict, *, manifest: dict, base: dict) -> dict:
    """Render a labelled sample with the same bounded app formatter as the Hub."""
    values = _values(body, base)
    task = body.get("task", "detection")
    if not isinstance(task, str) or task not in _TASKS:
        raise ValueError("invalid sample task")
    if not _SLOTS.acquire(blocking=False):
        return {"messages": [], "error": "busy"}
    try:
        sample = _sample(app_id, task)
        if app_id == "builtin":
            messages = _builtin_preview(values, task, sample)
        else:
            # Only validate format inputs here: dormant network fields do not
            # prevent a local message preview, and no connections are opened.
            cfg = resolve_output_config(manifest, values)
            keys = ["iMode"]
            if cfg["mode"] == "custom":
                keys += ["template_mode", "output_mapping" if cfg["template_mode"] == "mapping" else "dTemplate"]
            _, errors = appconfig.validate_config(manifest, {key: values[key] for key in keys if key in values})
            if errors:
                raise ValueError("invalid format settings")
            formatter = build_formatter(
                cfg["mode"], cfg, app_id=app_id, node=device_identifier(),
                base_topic=str((values.get("dMqtt") or {}).get("sTopic") or "recamera"),
                entities=manifest.get("ha_entities") or [],
                device_name=manifest.get("name") or app_id, fallback_raw=False,
            )
            messages = [{"body": message.body.decode("utf-8"), "topic": message.topic}
                        for message in formatter.format(sample, channel="ws")]
        # Cap the whole HTTP response, not just each rendered message.
        if len(messages) > 16 or len(json.dumps(messages).encode()) > 128 * 1024:
            raise ValueError("preview exceeds limit")
        return {"sample": True, "messages": messages}
    except Exception:
        # Template exceptions can embed source text. Do not echo draft contents
        # or any connection credentials in API errors/logs.
        return {"sample": True, "messages": [], "error": "templateInvalid"}
    finally:
        _SLOTS.release()


def _http_test(config: dict, payload: bytes) -> dict:
    url = urlsplit(str(config.get("sUrl") or ""))
    if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.fragment:
        raise ValueError("invalid HTTP URL")
    token = config.get("sToken") or ""
    if not isinstance(token, str) or "\r" in token or "\n" in token:
        raise ValueError("invalid HTTP token")
    cls = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
    connection = cls(url.hostname, url.port, timeout=_TIMEOUT)
    try:
        headers = {"Content-Type": "application/json", "X-Recamera-Test": "1"}
        if token:
            headers["Authorization"] = "Bearer " + token
        path = url.path or "/"
        if url.query:
            path += "?" + url.query
        connection.request("POST", path, body=payload, headers=headers)
        response = connection.getresponse()
        # No redirects, response-body logging, or retries for test messages.
        return {"ok": 200 <= response.status < 300, "status": response.status,
                "reason": "receiverRejected" if not 200 <= response.status < 300 else ""}
    finally:
        connection.close()


def _mqtt_test(config: dict, payload: bytes) -> dict:
    host = str(config.get("sURL") or config.get("sUrl") or "").strip()
    port = config.get("iPort", 1883)
    topic = str(config.get("sTopic") or "").strip()
    if (not host or any(char.isspace() or char in "/\x00" for char in host)
            or not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535
            or not topic or len(topic.encode()) > 4096 or any(char in topic for char in "+#\x00")
            or "{{" in topic or "{%" in topic):
        raise ValueError("invalid MQTT settings")
    connection = _MqttConnection(host, port, "rc-test-" + uuid.uuid4().hex[:12],
                                 username=str(config.get("sUsername") or ""),
                                 password=str(config.get("sPassword") or ""))
    try:
        connection.connect(timeout=_TIMEOUT)
        # Unique test client ID cannot disconnect the running app. Never retain
        # this message or emit availability/discovery messages.
        connection.publish(topic, payload, retain=False)
        return {"ok": True, "topic": topic}
    finally:
        connection.close()


def test_delivery(app_id: str, body: dict, *, manifest: dict, base: dict) -> dict:
    values = _values(body, base)
    channel = body.get("channel")
    if app_id == "builtin":
        mode = values.get("iMode", 0)
        if not isinstance(mode, int) or isinstance(mode, bool) or mode not in (0, 1, 2, 3):
            raise ValueError("invalid builtin output mode")
        channels = [{1: "mqtt", 2: "http", 3: "uart"}.get(mode)]
    else:
        channels = values.get("output_channels") or ["ws"]
        if not isinstance(channels, list) or any(not isinstance(value, str) for value in channels):
            raise ValueError("invalid output channels")
    if channel not in ("mqtt", "http") or channel not in channels:
        raise ValueError("select an enabled HTTP or MQTT destination")
    if not _SLOTS.acquire(blocking=False):
        return {"ok": False, "channel": channel, "reason": "busy"}
    try:
        payload = json.dumps({"type": "test", "source": app_id,
                              "timestamp": int(time.time() * 1000),
                              "message": "reCamera delivery test"}).encode()
        key = "dHttp" if channel == "http" else "dMqtt"
        config = values.get(key)
        if not isinstance(config, dict):
            raise ValueError("invalid connection settings")
        send = _http_test if channel == "http" else _mqtt_test
        return {"channel": channel, **send(config, payload)}
    except (ValueError, TypeError, OverflowError):
        return {"ok": False, "channel": channel, "reason": "invalidSettings"}
    except ConnectionError:
        return {"ok": False, "channel": channel, "reason": "receiverRejected"}
    except Exception:
        return {"ok": False, "channel": channel, "reason": "networkFailed"}
    finally:
        _SLOTS.release()

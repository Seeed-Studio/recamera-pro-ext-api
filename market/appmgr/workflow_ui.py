"""App Center integration for installed x-workflow-ui version 1 applications.

The native Web JWT boundary remains in nginx. This adapter reads only the
selected installed app's definitions and authenticates its fixed loopback HTTP
operations using that app's configured credential; it never accepts a target URL
or credential from the caller. No model/runtime work executes inside appmgr.
"""
from __future__ import annotations

import http.client
import hashlib
import json
import os
import re
import stat

from . import config, paths, state

MAX_DOCUMENT = 256 * 1024
MAX_RESPONSE = 16 * 1024 * 1024


def supported(manifest):
    value = manifest.get("x-workflow-ui") if isinstance(manifest, dict) else None
    return isinstance(value, dict) and value == {"version": 1} and type(value["version"]) is int


def require(manifest):
    if not supported(manifest):
        raise ValueError("This application does not declare Workflow UI version 1")


def workflows(manifest, app_id):
    """Available even while the app is stopped; never follow appdata symlinks."""
    require(manifest)
    if not paths.valid_app_id(app_id):
        raise ValueError("Invalid application ID")
    items, invalid = [], 0
    try:
        app_fd = os.open(os.path.join(paths.APPDATA_DIR, app_id), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {"workflows": [], "invalid_documents": 0}
    try:
        try:
            directory = os.open("workflows", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=app_fd)
        except FileNotFoundError:
            return {"workflows": [], "invalid_documents": 0}
        try:
            for filename in sorted(os.listdir(directory))[:64]:
                if not re.fullmatch(r"[\w-]{1,128}\.json", filename):
                    continue
                try:
                    descriptor = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                    with os.fdopen(descriptor, "rb") as stream:
                        info = os.fstat(stream.fileno())
                        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_DOCUMENT:
                            raise ValueError("Invalid Workflow file")
                        doc = json.loads(stream.read(MAX_DOCUMENT + 1))
                    if not isinstance(doc, dict):
                        raise ValueError("Invalid Workflow document")
                    identifier = doc.get("id")
                    if (not isinstance(identifier, str)
                            or not re.fullmatch(r"[\w-]{1,128}", identifier)
                            or identifier == "models"
                            or hashlib.sha256(identifier.encode()).hexdigest() + ".json" != filename):
                        raise ValueError("Workflow identifier does not match its storage entry")
                    items.append({"id": identifier, "name": str(doc.get("name") or identifier)[:256]})
                except (OSError, ValueError, TypeError, RecursionError):
                    invalid += 1
        finally:
            os.close(directory)
    finally:
        os.close(app_fd)
    return {"workflows": items, "invalid_documents": invalid}


def decorate_config(manifest, app_id, payload):
    if not supported(manifest):
        return payload
    # get_config builds a fresh schema. Deep-copy before replacing field metadata.
    payload = json.loads(json.dumps(payload))
    listed = workflows(manifest, app_id)
    selected = payload["values"].get("workflow_id", "")
    options = [{"value": "", "label": "Editor service only", "label_zh": "仅启动编辑服务"}]
    options += [{"value": item["id"], "label": item["name"], "label_zh": item["name"]}
                for item in listed["workflows"]]
    if selected and not any(item["value"] == selected for item in options):
        options.append({"value": selected, "label": selected + " (unavailable)", "label_zh": selected + "（不存在）"})
    for group in payload["config_schema"].get("groups", []):
        for field in group.get("items", []):
            if field.get("key") == "workflow_id":
                field.update(type="select", options=options)
    payload["workflow_ui"] = {"version": 1, "invalid_documents": listed["invalid_documents"]}
    return payload


def _request(values, path, *, method="GET", body=None, cookie=None, csrf=None):
    port = values.get("port", 9001)
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("Invalid Workflow service port")
    token = values.get("api_token", "")
    if not isinstance(token, str) or "\n" in token or "\r" in token:
        raise ValueError("Invalid Workflow service credential")
    headers = {"Accept": "application/json", "X-Inference-Token": token}
    if cookie:
        headers["Cookie"] = cookie
    if csrf:
        headers["X-CSRF"] = csrf
    raw = None
    if body is not None:
        raw = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request(method, path, body=raw, headers=headers)
        response = connection.getresponse()
        data = response.read(MAX_RESPONSE + 1)
        if response.status != 200 or len(data) > MAX_RESPONSE:
            raise RuntimeError("Workflow service request failed (HTTP %s)" % response.status)
        result = json.loads(data)
        if not isinstance(result, dict):
            raise ValueError("Invalid Workflow service response")
        return result, response.getheader("Set-Cookie", "").split(";", 1)[0]
    except (OSError, http.client.HTTPException, ValueError) as exc:
        # Do not echo upstream URLs, cookie or credentials into appmgr logs/UI.
        raise RuntimeError("Workflow service is unavailable; check the application status") from exc
    finally:
        connection.close()


def runtime(manifest, app_id, include_result=False):
    require(manifest)
    record = state.get_app(app_id) or {}
    if not record.get("pid"):
        return {"service_only": False, "deployment": {"status": "stopped"}, "video": None, "latest": None}
    identity = (record.get("instance_id"), record.get("generation"), record.get("pid"))
    values = config.effective_values(manifest, app_id)
    response, _ = _request(values, "/app-center/workflow-runtime?include_result=" + ("true" if include_result else "false"))
    current = state.get_app(app_id) or {}
    if identity != (current.get("instance_id"), current.get("generation"), current.get("pid")):
        raise RuntimeError("Application restarted; refresh the Workflow results")
    if not isinstance(response.get("deployment"), dict):
        raise RuntimeError("Installed Workflow application needs an update")
    return response


def editor_session(manifest, app_id):
    require(manifest)
    values = config.effective_values(manifest, app_id)
    if values.get("host") != "0.0.0.0":
        raise ValueError("Enable local network access in application configuration to open the online canvas")
    # Exchange the real configured device credential through the normal session
    # API. No trusted headers, fabricated cloud identity or platform grants.
    session, cookie = _request(values, "/ui/session")
    if not cookie or not isinstance(session.get("csrf"), str):
        raise RuntimeError("Workflow session could not be created")
    editor, _ = _request(values, "/ui/builder-session", method="POST", body={}, cookie=cookie, csrf=session["csrf"])
    if (editor.get("origin") != "https://app.roboflow.com"
            or not re.fullmatch(r"/ui/runtime/[\w-]+", str(editor.get("runtime_path", "")))
            or editor.get("csrf") != editor["runtime_path"].rsplit("/", 1)[-1]):
        raise RuntimeError("Invalid Workflow editor connection")
    return {**editor, "port": values.get("port", 9001), "workflow_id": values.get("workflow_id", "")}

"""SenseCraft transport; credentials stay inside the device control plane."""
import http.client
import json
import os
import re
import ssl
import time
import uuid
from urllib.parse import urlencode

from . import builtin
from .workflow_model_contract import MAX_MODEL, read_json

HOST = "sensecraft-train-api.seeed.cc"
SESSION = "/userdata/secure/sensecraft/oauth_session.json"


class LoginRequired(Exception):
    pass


class CloudError(Exception):
    pass


class SubmissionUncertain(CloudError):
    pass


def credentials():
    """Reuse CGI's locked refresh operation; never implement a second token store."""
    try:
        session = read_json(SESSION)
    except (FileNotFoundError, ValueError):
        raise LoginRequired()
    if session.get("access_expires_at", 0) <= time.time() + 60:
        try:
            builtin._request("POST", "/sensecraft/session/refresh", timeout=45)
            session = read_json(SESSION)
        except builtin.BuiltinError:
            # A network failure is not evidence that the user logged out.
            if os.path.isfile(SESSION):
                raise CloudError("cloud_unreachable") from None
            raise LoginRequired() from None
    if not session.get("user_id") or not session.get("access_token") or session.get("access_expires_at", 0) <= time.time():
        raise LoginRequired()
    return session


def _connection():
    context = ssl.create_default_context()
    # Cross-compiled OpenSSL may retain the build host's default CA path.
    # Load the firmware's trust bundle, retaining certificate/hostname checks.
    if os.path.isfile("/etc/ssl/certs/ca-certificates.crt"):
        context.load_verify_locations(cafile="/etc/ssl/certs/ca-certificates.crt")
    return http.client.HTTPSConnection(HOST, timeout=45, context=context)


def _json(response):
    if response.status == 401:
        raise LoginRequired()
    if response.status == 403:
        raise CloudError("cloud_permission_denied")
    if response.status == 429:
        raise CloudError("cloud_quota_exceeded")
    if response.status != 200:
        raise CloudError("cloud_request_failed")
    raw = response.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise CloudError("cloud_response_invalid")
    try:
        result = json.loads(raw)
    except ValueError:
        raise CloudError("cloud_response_invalid") from None
    if not isinstance(result, dict) or str(result.get("code", 0)) not in {"0", "200"}:
        raise CloudError("cloud_request_rejected")
    return result.get("data", result)


def query(endpoint, session, **params):
    params["user_id"] = session["user_id"]
    connection = _connection()
    try:
        connection.request("GET", "/v1/api/" + endpoint + "?" + urlencode(params),
                           headers={"Authorization": session["access_token"]})
        return _json(connection.getresponse())
    finally:
        connection.close()


def submit(session, source, dataset=None, model_name="workflow-model"):
    """Bounded 64 KiB multipart streaming, no requests-toolbelt/ML dependency."""
    boundary = "workflow" + uuid.uuid4().hex
    parts = []
    prompt = re.sub(r"[^A-Za-z0-9_.-]", "-", model_name)[:160] + "-" + source.parent.name[:8] + ".rknn"
    for key, value in {"user_id": session["user_id"], "framework_type": "9", "device_type": "40",
                       "prompt": prompt}.items():
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode())
    files = [("file", "model.onnx", source)]
    if dataset:
        files.append(("dataset_file", "calibration.zip", dataset))
    for key, filename, path in files:
        parts.extend([(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; filename="{filename}"\r\n'
                       'Content-Type: application/octet-stream\r\n\r\n').encode(), path, b"\r\n"])
    parts.append(f"--{boundary}--\r\n".encode())
    length = sum(len(part) if isinstance(part, bytes) else os.stat(part).st_size for part in parts)
    connection = _connection()
    sent = False
    try:
        connection.putrequest("POST", "/v1/api/create_task")
        connection.putheader("Authorization", session["access_token"])
        connection.putheader("Content-Type", "multipart/form-data; boundary=" + boundary)
        connection.putheader("Content-Length", str(length))
        connection.endheaders()
        sent = True
        for part in parts:
            if isinstance(part, bytes):
                connection.send(part)
            else:
                with open(part, "rb") as stream:
                    for chunk in iter(lambda: stream.read(65536), b""):
                        connection.send(chunk)
        data = _json(connection.getresponse())
        identifier = data.get("model_id") if isinstance(data, dict) else None
        if not isinstance(identifier, (str, int)) or not str(identifier) or len(str(identifier)) > 160:
            raise SubmissionUncertain("submission_uncertain")
        return str(identifier)
    except CloudError as exc:
        if str(exc) in {"cloud_permission_denied", "cloud_quota_exceeded", "cloud_request_rejected"}:
            raise
        raise SubmissionUncertain("submission_uncertain") from None
    except (OSError, http.client.HTTPException, ValueError):
        if sent:
            raise SubmissionUncertain("submission_uncertain") from None
        raise CloudError("cloud_unreachable") from None
    finally:
        connection.close()


def download(session, identifier, destination):
    connection = _connection()
    try:
        connection.request("GET", "/v1/api/get_model?" + urlencode({"user_id": session["user_id"], "model_id": identifier}),
                           headers={"Authorization": session["access_token"]})
        response = connection.getresponse()
        if response.status != 200:
            _json(response)
        size = 0
        with open(destination, "wb") as stream:
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_MODEL:
                    raise CloudError("model_too_large")
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        length = response.getheader("Content-Length")
        if length and size != int(length):
            raise CloudError("download_incomplete")
    finally:
        connection.close()

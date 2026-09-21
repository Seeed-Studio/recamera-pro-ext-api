"""Stdlib-only SSH worker. Uses the firmware's authenticated nginx edge.

This file is sent as code over SSH, never installed in the device Python env.
The password stays on the host. Only the selected app may be mutated.
"""
import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import socket
import struct
import sys
import tempfile
import time
import uuid

PREFIX = "/api/app-center/v1"
MAX_PACKAGE = 200 * 1024 * 1024
MAX_RESPONSE = 4 * 1024 * 1024


class DeviceError(RuntimeError):
    pass


class PendingOperation(DeviceError):
    pass


def receive(stream, config, root="/userdata/appstage"):
    size, digest = config["size"], config["sha256"]
    if type(size) is not int or not 0 < size <= MAX_PACKAGE or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise DeviceError("Invalid package size/digest")
    Path(root).mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(root).free < size + 16 * 1024 * 1024:
        raise DeviceError("Insufficient staging space")
    directory = Path(tempfile.mkdtemp(prefix="recamera-skill-", dir=root))
    target = directory / "app.tar.gz"
    try:
        checksum, remaining = hashlib.sha256(), size
        with target.open("xb") as output:
            os.chmod(target, 0o600)
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise DeviceError("Incomplete upload")
                output.write(chunk)
                checksum.update(chunk)
                remaining -= len(chunk)
        if checksum.hexdigest() != digest:
            raise DeviceError("Uploaded package SHA-256 mismatch")
        return target
    except BaseException:
        shutil.rmtree(directory)
        raise


class API:
    def request(self, method, path, body=None, package=None):
        # SSH establishes device identity. The local request still goes through
        # the real firmware auth/origin/signature-policy boundary, not :8130.
        connection = http.client.HTTPConnection("127.0.0.1", 80, timeout=30)
        headers = {"Origin": "http://127.0.0.1", "Accept": "application/json"}
        try:
            if package is not None:
                boundary = "recamera" + uuid.uuid4().hex
                head = (f'--{boundary}\r\nContent-Disposition: form-data; name="package"; '
                        'filename="app.tar.gz"\r\nContent-Type: application/gzip\r\n\r\n').encode()
                tail = f"\r\n--{boundary}--\r\n".encode()
                headers.update({"Content-Type": f"multipart/form-data; boundary={boundary}",
                                "Content-Length": str(len(head) + package.stat().st_size + len(tail))})
                connection.putrequest(method, PREFIX + path)
                for key, value in headers.items():
                    connection.putheader(key, value)
                connection.endheaders()
                connection.send(head)
                with package.open("rb") as source:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        connection.send(chunk)
                connection.send(tail)
            else:
                headers["Content-Type"] = "application/json"
                connection.request(method, PREFIX + path,
                                   json.dumps(body).encode() if body is not None else None, headers)
            response = connection.getresponse()
            data = response.read(MAX_RESPONSE + 1)
            if len(data) > MAX_RESPONSE:
                raise DeviceError("API response exceeds limit")
            if response.status in (401, 403):
                raise DeviceError("Firmware nginx rejected local authentication/origin; use the authenticated Web App Center. No auth bypass attempted.")
            if not 200 <= response.status < 300:
                raise DeviceError(f"{method} {path}: HTTP {response.status}: {data[:1500].decode(errors='replace')}")
            return json.loads(data) if data else {}
        finally:
            connection.close()


def app_state(api, app_id):
    return next((app for app in api.request("GET", "/apps")["apps"] if app.get("id") == app_id), None)


def wait_operation(api, submitted, kind, app_id, timeout, report):
    op = submitted.get("operation", {})
    if not re.fullmatch(r"[a-f0-9]{32}", str(op.get("id", ""))) or op.get("type") != kind or op.get("app_id") != app_id:
        raise PendingOperation("Unexpected operation identity; inspect device state before retrying")
    report.setdefault("operations", []).append(op.copy())
    deadline = time.monotonic() + timeout
    while True:
        report["operations"][-1] = op.copy()
        if op.get("status") == "succeeded":
            return op
        if op.get("status") == "failed":
            raise DeviceError(f"{kind} failed: {op.get('error')}")
        if time.monotonic() >= deadline:
            raise PendingOperation(f"{kind} operation {op['id']} is still pending; inspect it before retrying")
        time.sleep(0.5)
        found = next((item for item in api.request("GET", "/operations")["operations"] if item.get("id") == op["id"]), None)
        if found is None or found.get("type") != kind or found.get("app_id") != app_id:
            raise PendingOperation("Operation disappeared or identity changed; inspect state before retrying")
        op = found


def installed_identity(app_id, config):
    # The /apps API adds presentation defaults to manifest.render; compare the
    # actual installed files instead of that UI projection. Integrated firmware
    # uses this fixed apps root. Custom layouts need an explicit reviewed path.
    root = Path("/userdata/local/apps") / app_id
    manifest = json.loads((root / "manifest.json").read_text())
    lock = json.loads((root / "release.lock.json").read_text())
    if manifest != config["manifest"] or lock.get("release_id") != config["release_id"]:
        raise DeviceError("Installed release identity differs from verified package")


def install(api, package, config, report):
    manifest, app_id = config["manifest"], config["manifest"]["id"]
    policy = api.request("GET", "/policy")
    limit = policy.get("upload", {}).get("max_package_bytes", MAX_PACKAGE)
    if package.stat().st_size > limit:
        raise DeviceError("Package exceeds device upload policy")
    if app_state(api, app_id) and not config["replace"]:
        raise DeviceError("App already installed; replacing it requires --replace within the user's authorized scope")
    uploaded = api.request("POST", "/uploads", package=package)
    upload_id = uploaded.get("upload_id")
    if not re.fullmatch(r"[a-f0-9]{32}", str(upload_id)):
        raise DeviceError("Invalid upload receipt")
    report["upload_id"] = upload_id
    preflight = uploaded.get("preflight", {})
    report["preflight"] = preflight
    # No deletion/retry after install submission: a lost response can still
    # mean the operation was accepted by the daemon.
    submitted = False
    try:
        if preflight.get("manifest") != manifest or preflight.get("release_id") != config["release_id"]:
            raise DeviceError("Device preflight identity differs from verified local archive")
        checks = preflight.get("checks")
        if not isinstance(checks, list) or not checks or any(c.get("passed") is not True for c in checks):
            raise DeviceError("Device preflight did not pass all installation checks")
        context = preflight.get("install_context", {})
        if context.get("mode") not in {"new", "upgrade", "reinstall"}:
            raise DeviceError("Unsupported install context")
        if context["mode"] != "new" and not config["replace"]:
            raise DeviceError("App was installed concurrently; no replacement authorized")
        confirmations = context.get("confirmation_required", [])
        if any(key not in {"running_upgrade_confirmed", "force_reinstall_confirmed"} for key in confirmations):
            raise DeviceError("Unknown install confirmation requirement")
        if confirmations and not config["replace"]:
            raise DeviceError("Replacement confirmation required")
        body = {"upload_id": upload_id, "permissions_confirmed": True,
                "permissions": manifest.get("permissions", {})}
        body.update({key: True for key in confirmations})
        submitted = True
        report["install"] = "pending"
        wait_operation(api, api.request("POST", "/apps", body), "install", app_id, config["timeout"], report)
        installed = app_state(api, app_id)
        if not installed or installed.get("version") != manifest["version"]:
            raise DeviceError("Installed application/version was not observed")
        installed_identity(app_id, config)
        report["install"] = "passed"
    finally:
        if not submitted:
            try:
                api.request("DELETE", "/uploads/" + upload_id)
            except Exception as exc:
                report["upload_cleanup_error"] = str(exc)


class Results:
    """Bounded read-only client for canonical ResultHub via the nginx edge."""
    def __init__(self, app_id):
        self.sock = socket.create_connection(("127.0.0.1", 80), timeout=5)
        self.buffer = bytearray()
        try:
            key = base64.b64encode(os.urandom(16)).decode()
            self.sock.sendall(("GET /ws/ai/results/v2 HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                               "Origin: http://127.0.0.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                               f"Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: {key}\r\n\r\n").encode())
            while b"\r\n\r\n" not in self.buffer:
                chunk = self.sock.recv(4096)
                if not chunk or len(self.buffer) + len(chunk) > 65536:
                    raise DeviceError("Invalid result WebSocket handshake")
                self.buffer.extend(chunk)
            header, rest = bytes(self.buffer).split(b"\r\n\r\n", 1)
            self.buffer = bytearray(rest)
            lines = header.decode("ascii").split("\r\n")
            fields = dict((k.lower().strip(), v.strip()) for k, v in (line.split(":", 1) for line in lines[1:] if ":" in line))
            expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            if lines[0].split()[1] != "101" or fields.get("sec-websocket-accept") != expected:
                raise DeviceError("Canonical result WebSocket is unavailable or requires Web authentication")
            self.send(json.dumps({"type": "subscribe", "view": "raw", "sources": [app_id], "types": ["frame", "metrics", "event"]}).encode())
        except BaseException:
            self.close()
            raise

    def close(self):
        self.sock.close()

    def send(self, payload, opcode=1):
        mask = os.urandom(4)
        size = len(payload)
        header = bytes([128 | opcode, 128 | size]) if size < 126 else bytes([128 | opcode, 254]) + struct.pack("!H", size)
        self.sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def fill(self, size, deadline):
        while len(self.buffer) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self.sock.settimeout(min(0.25, remaining))
            try:
                chunk = self.sock.recv(min(65536, size - len(self.buffer)))
            except socket.timeout:
                return False
            if not chunk:
                raise DeviceError("Result WebSocket closed")
            self.buffer.extend(chunk)
        return True

    def message(self, deadline):
        # Commit bytes only after a complete frame. A temporary timeout leaves
        # partial headers/payloads intact and lets the caller poll app health.
        if not self.fill(2, deadline):
            return {}
        first, second = self.buffer[:2]
        if not first & 128 or second & 128 or first & 112:
            raise DeviceError("Unsupported result WebSocket frame")
        length = second & 127
        offset = 2
        if length == 126:
            if not self.fill(4, deadline):
                return {}
            length, offset = struct.unpack("!H", self.buffer[2:4])[0], 4
        elif length == 127:
            if not self.fill(10, deadline):
                return {}
            length, offset = struct.unpack("!Q", self.buffer[2:10])[0], 10
        if length > MAX_RESPONSE:
            raise DeviceError("Result message exceeds limit")
        if not self.fill(offset + length, deadline):
            return {}
        payload = bytes(self.buffer[offset:offset + length])
        del self.buffer[:offset + length]
        opcode = first & 15
        if opcode == 8:
            raise DeviceError("Result WebSocket closed")
        if opcode == 9:
            self.send(payload, 10)
        result = json.loads(payload) if opcode == 1 else {}
        if not isinstance(result, dict):
            raise DeviceError("Invalid canonical result envelope")
        return result


def identity(app):
    instance = (app or {}).get("instance") or {}
    if not instance.get("id") or type(instance.get("generation")) is not int:
        raise DeviceError("AppMgr did not provide a runtime instance/generation")
    return instance["id"], instance["generation"]


def wait_ready(api, app_id, timeout):
    deadline = time.monotonic() + timeout
    while True:
        app = app_state(api, app_id)
        state = ((app or {}).get("runtime") or {}).get("observed_state")
        if state in {"ready", "running"} and (app or {}).get("running"):
            return app
        if state in {"failed", "crash_loop", "degraded", "stopped"}:
            raise DeviceError(f"App did not become ready: {state}: {(app or {}).get('reason')}")
        if time.monotonic() >= deadline:
            raise DeviceError(f"App readiness deadline exceeded: {state}; resource/dependency wait is not success")
        time.sleep(0.5)


def matching_result(message, app_id, instance):
    source = message.get("source") or {}
    return (message.get("type") in {"frame", "metrics", "event"}
            and source.get("kind") == "app" and source.get("id") == app_id
            and (source.get("instance"), source.get("generation")) == instance
            and type(message.get("seq")) is int)


def observe(api, app_id, seconds, report, label):
    current = app_state(api, app_id)
    expected = identity(current)
    sequences, kinds = set(), set()
    subscriber = None
    evidence = {"state": "unverified", "instance": expected[0], "generation": expected[1]}
    report.setdefault("observations", {})[label] = evidence
    deadline = time.monotonic() + seconds
    try:
        try:
            subscriber = Results(app_id)
        except Exception as exc:
            evidence["reason"] = str(exc)
        next_poll = 0
        while time.monotonic() < deadline:
            if time.monotonic() >= next_poll:
                current = app_state(api, app_id)
                if identity(current) != expected or (current.get("runtime") or {}).get("observed_state") not in {"ready", "running"}:
                    raise DeviceError("App exited, degraded or changed instance during observation")
                next_poll = time.monotonic() + 0.5
            if subscriber:
                try:
                    message = subscriber.message(deadline)
                    if matching_result(message, app_id, expected):
                        sequences.add(message["seq"])
                        kinds.add(message["type"])
                except Exception as exc:
                    evidence["reason"] = str(exc)
                    subscriber.close()
                    subscriber = None
            else:
                time.sleep(min(0.25, max(0, deadline - time.monotonic())))
        final = app_state(api, app_id)
        if identity(final) != expected or (final.get("runtime") or {}).get("observed_state") not in {"ready", "running"}:
            raise DeviceError("App instance/state changed at end of observation")
        # One replayed last value is not progress. Require two sequences from
        # exactly this running generation, without copying user result payloads.
        evidence.update(state="passed" if len(sequences) >= 2 else "unverified",
                        messages=len(sequences), types=sorted(kinds))
        return expected
    finally:
        if subscriber:
            subscriber.close()


def verify(api, config, report):
    app_id = config["manifest"]["id"]
    def lifecycle(action):
        report["lifecycle"] = "pending"
        wait_operation(api, api.request("POST", f"/apps/{app_id}/{action}", {}),
                       action, app_id, config["timeout"], report)
    # If a request times out, do not issue another conflicting mutation. The
    # report retains operation IDs; inspection must resolve uncertainty first.
    lifecycle("start")
    wait_ready(api, app_id, config["timeout"])
    first = observe(api, app_id, config["observe_seconds"], report, "start")
    lifecycle("stop")
    stopped = app_state(api, app_id)
    if not stopped or stopped.get("running") or (stopped.get("runtime") or {}).get("observed_state") != "stopped":
        raise DeviceError("Stop operation completed but app is not stopped")
    lifecycle("start")
    wait_ready(api, app_id, config["timeout"])
    second = observe(api, app_id, config["observe_seconds"], report, "start_after_stop")
    if second == first:
        raise DeviceError("Start after stop reused a stale instance")
    lifecycle("restart")
    wait_ready(api, app_id, config["timeout"])
    third = observe(api, app_id, config["observe_seconds"], report, "restart")
    if third in {first, second}:
        raise DeviceError("Restart reused a stale instance")
    if not config["leave_running"]:
        lifecycle("stop")
        final = app_state(api, app_id)
        if not final or final.get("running") or (final.get("runtime") or {}).get("observed_state") != "stopped":
            raise DeviceError("Final stop was not observed")
    report["lifecycle"] = "passed"
    report["results"] = "passed" if all(o["state"] == "passed" for o in report["observations"].values()) else "unverified"


def main():
    report = {"upload": "not_run", "install": "not_run", "lifecycle": "not_run",
              "results": "not_run", "task_quality": "not_run"}
    config, package, api = {}, None, API()
    stage = "upload"
    try:
        line = sys.stdin.buffer.readline(1024 * 1024 + 1)
        if len(line) > 1024 * 1024 or not line.endswith(b"\n"):
            raise DeviceError("Invalid transfer header")
        config = json.loads(line)
        if config.get("action") not in {"upload", "install", "verify"}:
            raise DeviceError("Invalid action")
        app_id = config["manifest"]["id"]
        if not re.fullmatch(r"[a-z0-9-]{1,64}", app_id) or app_id in {"builtin", "acousticslab"}:
            raise DeviceError("Invalid app identity")
        package = receive(sys.stdin.buffer, config)
        report.update(upload="passed", remote_archive=str(package), sha256=config["sha256"], app_id=app_id)
        if config["action"] != "upload":
            stage = "install"
            install(api, package, config, report)
            # AppMgr owns its own upload/extraction after successful install.
            package.unlink()
            package.parent.rmdir()
            report["remote_archive_removed"] = True
        if config["action"] == "verify":
            stage = "lifecycle"
            verify(api, config, report)
    except Exception as exc:
        if isinstance(exc, PendingOperation):
            report[stage] = "pending"
        elif not isinstance(exc, DeviceError) and report[stage] == "pending":
            report[stage] = "unknown"
        else:
            report[stage] = "failed"
        report["error"] = str(exc)
    finally:
        if report.get("app_id") and config.get("action") != "upload":
            try:
                report["final_app_state"] = app_state(api, report["app_id"])
                if report.get("error"):
                    report["logs"] = api.request("GET", f"/apps/{report['app_id']}/logs?tail=50")
            except Exception as exc:
                report["diagnostic_error"] = str(exc)
        print("RECAMERA_SKILL_RESULT=" + json.dumps(report))


if __name__ == "__main__":
    main()

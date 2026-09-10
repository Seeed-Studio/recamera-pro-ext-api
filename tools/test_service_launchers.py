"""Host regressions for real protocol probes and the OEM init launchers."""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


REPO = Path(__file__).resolve().parents[1]


@contextlib.contextmanager
def http_peer(body, status=200, *, delay=0):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == "/health"
            time.sleep(delay)
            try:
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def run_probe(*args, timeout=0.2):
    return subprocess.run(
        [sys.executable, "-m", "appmgr.service_health", *args,
         "--timeout", str(timeout)],
        env={**os.environ, "PYTHONPATH": str(REPO / "market")},
        capture_output=True, text=True, timeout=3,
    )


@pytest.mark.parametrize(("body", "status", "expected"), [
    (b'{"service":"appmgr","ready":true}', 200, 0),
    (b'{"service":"appmgr","ready":false}', 200, 1),
    (b'{"service":"other","ready":true}', 200, 1),
    (b'{"service":"appmgr","ready":true}', 503, 1),
    (b'not json', 200, 1),
    (b'[]', 200, 1),
    (b'x' * 4097, 200, 1),
])
def test_http_probe_requires_appmgr_identity_and_readiness(body, status, expected):
    with http_peer(body, status) as port:
        result = run_probe("appmgr", "--port", str(port))
    assert result.returncode == expected, result.stderr


def test_http_probe_has_a_complete_request_deadline():
    with http_peer(b'{"service":"appmgr","ready":true}', delay=1) as port:
        started = time.monotonic()
        result = run_probe("appmgr", "--port", str(port), timeout=0.1)
        elapsed = time.monotonic() - started
    assert result.returncode == 1
    assert elapsed < 0.8


def test_inferenced_socket_inode_does_not_prove_readiness(tmp_path):
    path = tmp_path / "inference.sock"
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(path))
        listener.listen()
        started = time.monotonic()
        result = run_probe("inferenced", "--socket", str(path), timeout=0.1)
        elapsed = time.monotonic() - started
    assert result.returncode == 1
    assert elapsed < 0.8


# A real process with the same module argv as each platform service, but with
# no hardware dependencies. Healthy/rejected protocol responses exercise the
# real service_health module instead of mocking its exit code.
FAKE_SERVICE = '''
import json, os, signal, socket, struct, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
Path(os.environ["TEST_SERVICE_PID"]).write_text(str(os.getpid()))
behavior = os.environ["TEST_SERVICE_BEHAVIOR"]
if behavior.endswith("ignore-term"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
ready = behavior in ("ready", "ready-ignore-term")
if behavior == "crash":
    sys.exit(7)
time.sleep(0.08)
if __package__ == "appmgr":
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"service": "appmgr", "ready": ready}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args): pass
    HTTPServer(("127.0.0.1", int(os.environ["TEST_SERVICE_PORT"])), Handler).serve_forever()
else:
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(sys.argv[sys.argv.index("--socket") + 1])
    listener.listen()
    def exact(conn, size):
        body = b""
        while len(body) < size:
            part = conn.recv(size - len(body))
            if not part: raise ConnectionError()
            body += part
        return body
    while True:
        conn, _ = listener.accept()
        with conn:
            try:
                for _ in range(2):
                    size = struct.unpack("!I", exact(conn, 4))[0]
                    request = json.loads(exact(conn, size))
                    response = json.dumps({"op": request["op"], "ok": behavior == "ready",
                                           "request_id": request["request_id"],
                                           "tensors": [], "protocol": 1,
                                           "state": "running"}).encode()
                    conn.sendall(struct.pack("!I", len(response)) + response)
            except (ConnectionError, BrokenPipeError): pass
'''


def launcher_fixture(tmp_path, name):
    runtime = tmp_path / "runtime"
    for package in ("appmgr", "inferenced"):
        root = runtime / package
        root.mkdir(parents=True)
        (root / "__init__.py").write_text("")
        (root / "__main__.py").write_text(FAKE_SERVICE)
    for name_part in ("service_health.py", "resources.py", "paths.py"):
        shutil.copyfile(REPO / "market/appmgr" / name_part,
                        runtime / "appmgr" / name_part)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    source = (REPO / "market/deploy" / name).read_text()
    source = source.rsplit('\ncase "$1" in', 1)[0]
    source = source.replace("appmgr --timeout 1", f"appmgr --port {port} --timeout 0.1")
    source += "\n" + "\n".join(
        f"{key}={shlex.quote(str(value))}" for key, value in {
            "PIDFILE": tmp_path / "service.pid",
            "LOGFILE": tmp_path / "service.log",
            "SOCKET": tmp_path / "i.sock",
            "AUTH_DIR": tmp_path / "auth",
            "APPMGR_PARENT": runtime,
            "PYTHON": sys.executable,
            "PYTHONPATH_VALUE": runtime,
            "LD_LIBRARY_PATH_VALUE": "",
            "START_ATTEMPTS": 4,
            "STOP_ATTEMPTS": 2,
        }.items())
    # Keep the production integer sleep calls, shortening just the test clock.
    source += '\nsleep() { /bin/sleep 0.05; }\n'
    # Limit candidate enumeration to this test's pidfile. The host can contain
    # thousands of unrelated processes; all /proc identity checks stay real.
    source = source.replace('for pid in /proc/[0-9]*; do',
                            'for pid in $(cat "$PIDFILE" 2>/dev/null); do')
    source = source.replace("for pid in $(ls /proc 2>/dev/null | grep -E '^[0-9]+$'); do",
                            'for pid in $(cat "$PIDFILE" 2>/dev/null); do')
    # Inferenced's parent directory is firmware-owned; use the temp socket's
    # existing parent so even root-running tests never touch /run/recamera.
    source = source.replace("mkdir -p /run/recamera", "mkdir -p " + shlex.quote(str(tmp_path)))
    return source, port


@pytest.mark.parametrize("name", ["S93inferenced", "S94appmgr"])
@pytest.mark.parametrize("behavior", ["ready", "unready", "crash"])
def test_launcher_waits_for_protocol_and_cleans_failed_start(tmp_path, name, behavior):
    source, port = launcher_fixture(tmp_path, name)
    source += '''
start
echo "START_RC=$?"
if [ -f "$PIDFILE" ]; then echo PID_PRESENT; else echo PID_ABSENT; fi
stop
echo "STOP_RC=$?"
'''
    result = subprocess.run(
        ["sh", "-c", source], timeout=10, capture_output=True, text=True,
        env={**os.environ, "TEST_SERVICE_PID": str(tmp_path / "actual.pid"),
             "TEST_SERVICE_BEHAVIOR": behavior, "TEST_SERVICE_PORT": str(port)},
    )
    assert result.returncode == 0, result.stderr
    expected = 0 if behavior == "ready" else 1
    assert f"START_RC={expected}" in result.stdout, result.stdout + result.stderr
    assert ("PID_PRESENT" if behavior == "ready" else "PID_ABSENT") in result.stdout
    assert "STOP_RC=0" in result.stdout, result.stdout
    assert not (tmp_path / "service.pid").exists()
    pid = (tmp_path / "actual.pid").read_text()
    assert not Path("/proc", pid).exists(), "failed start or stop leaked a service"


@pytest.mark.parametrize("behavior,start_rc,stop_rc", [
    ("ready-ignore-term", 0, 1),
    ("unready-ignore-term", 1, 0),
])
def test_appmgr_forced_shutdown_reports_failure_and_cleans_dead_pidfile(
        tmp_path, behavior, start_rc, stop_rc):
    source, port = launcher_fixture(tmp_path, "S94appmgr")
    source += '''
start
echo "START_RC=$?"
stop
echo "STOP_RC=$?"
'''
    result = subprocess.run(
        ["sh", "-c", source], timeout=10, capture_output=True, text=True,
        env={**os.environ, "TEST_SERVICE_PID": str(tmp_path / "actual.pid"),
             "TEST_SERVICE_BEHAVIOR": behavior, "TEST_SERVICE_PORT": str(port)},
    )
    assert result.returncode == 0, result.stderr
    assert f"START_RC={start_rc}" in result.stdout, result.stdout
    assert f"STOP_RC={stop_rc}" in result.stdout, result.stdout
    assert "forced SIGKILL" in result.stdout
    assert "shutdown may be incomplete" in result.stdout
    if stop_rc:
        assert "did not stop cleanly" in result.stdout
    assert not (tmp_path / "service.pid").exists()
    assert not Path("/proc", (tmp_path / "actual.pid").read_text()).exists()


def test_restore_uses_only_oem_launchers_in_dependency_order(tmp_path):
    calls = tmp_path / "calls"
    for name in ("S93inferenced", "S94appmgr"):
        launcher = tmp_path / name
        launcher.write_text(f"#!/bin/sh\necho {name} >> {shlex.quote(str(calls))}\n")
        launcher.chmod(0o755)
    source = (REPO / "market/deploy/appmgr-restore.sh").read_text().replace(
        "INIT_DIR=/oem/usr/etc/init.d", "INIT_DIR=" + shlex.quote(str(tmp_path)))
    result = subprocess.run(["sh", "-c", source], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert calls.read_text().splitlines() == ["S93inferenced", "S94appmgr"]
    calls.unlink()
    (tmp_path / "S94appmgr").unlink()
    result = subprocess.run(["sh", "-c", source], capture_output=True, text=True)
    assert result.returncode == 1
    assert not calls.exists(), "a missing firmware launcher must fail before starting"

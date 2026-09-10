"""Real Unix handshake peers and desired-state recovery for delayed firmware."""
import contextlib
import errno
import json
import socket
import struct
import threading
import time

import pytest

from market.appmgr.tests.test_multi_app_coordinator import (
    _manifest, managed,  # noqa: F401 -- shared isolated state/supervisor fixture
)
from appmgr import dependencies, resources, server, state


# Captured wire shape from sdk/proto/ext_api.proto: API 1, peercred, frame@1.
FRAME_ACK = b"\x08\x01\x1a\x08peercred\x22\x09\x0a\x05frame\x10\x01"
HELLO = b"\x08\x01\x10\x01\x1a\x10appmgr-readiness"


@contextlib.contextmanager
def native_peer(path, response=FRAME_ACK, delay=0):
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    listener.bind(str(path))
    listener.listen(1)
    listener.settimeout(1)
    received, errors = [], []

    def run():
        try:
            conn, _ = listener.accept()
            with conn:
                conn.settimeout(1)
                received.append(conn.recv(4096))
                if delay:
                    time.sleep(delay)
                try:
                    conn.send(response)
                    received.append(conn.recv(4096))
                except (BrokenPipeError, ConnectionResetError):
                    pass
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield received
    finally:
        thread.join(2)
        listener.close()
        path.unlink(missing_ok=True)
        assert not thread.is_alive()
        assert not errors


def test_native_hello_only_never_subscribes_or_acquires(tmp_path):
    path = tmp_path / "frame.sock"
    with native_peer(path) as received:
        result = dependencies.probe_extension(str(path), "frame")
    assert result["available"] is True
    assert received == [HELLO, b""]


@pytest.mark.parametrize("response,error", [
    (FRAME_ACK.replace(b"\x08\x01", b"\x08\x02", 1), "version"),
    (FRAME_ACK.replace(b"peercred", b"bad-auth"), "authentication"),
    (b"\x08\x01\x1a\x08peercred", "capability"),
    (b"\x22\x09short", "truncated"),
    (b"x" * 4097, "oversized"),
])
def test_wrong_protocol_is_terminal(tmp_path, response, error):
    path = tmp_path / "fake.sock"
    with native_peer(path, response):
        result = dependencies.probe_extension(str(path), "frame")
    assert result["available"] is False
    assert result["retryable"] is False
    assert error in result["error"]


@pytest.mark.parametrize("code,retryable", [(1, False), (2, False), (3, True)])
def test_handshake_rejection_distinguishes_auth_from_busy(tmp_path, code, retryable):
    path = tmp_path / "frame.sock"
    with native_peer(path, b"\x28" + bytes([code])):
        result = dependencies.probe_extension(str(path), "frame")
    assert result["available"] is False
    assert result["retryable"] is retryable


def test_inode_alone_and_silent_peer_are_not_readiness(tmp_path):
    path = tmp_path / "frame.sock"
    path.touch()
    assert dependencies.probe_extension(str(path), "frame")["available"] is False
    path.unlink()
    with native_peer(path, delay=0.2):
        started = time.monotonic()
        result = dependencies.probe_extension(str(path), "frame", timeout=0.04)
        elapsed = time.monotonic() - started
    assert result["available"] is False
    assert result["retryable"] is True
    assert elapsed < 0.15


def test_effective_profiles_select_only_actual_ipc_dependencies(monkeypatch):
    calls = []
    monkeypatch.setattr(dependencies, "probe_extension", lambda path, cap, timeout: (
        calls.append((path, cap)) or {"available": True}))
    manifest = _manifest("profiled")
    manifest["resources"] = {"profiles": [
        {"when": {"mode": "camera"}, "claims": [
            {"name": "camera.frames", "mode": "shared"},
            {"name": "probe.read", "mode": "shared"},
            {"name": "result.publish", "mode": "shared"},
        ]},
        {"when": {"mode": "audio"}, "claims": [
            {"name": "audio.capture", "mode": "shared"},
            {"name": "result.publish", "mode": "brokered"},
        ]},
    ]}
    assert dependencies.probe_plan(
        resources.plan_manifest(manifest, {"mode": "audio"}))["available"]
    assert calls == []  # ALSA and the appmgr-owned gateway do not require IPC.
    dependencies.probe_plan(resources.plan_manifest(manifest, {"mode": "camera"}))
    assert [cap for _path, cap in calls] == ["frame", "probe", "result"]


def test_delayed_frame_service_recovers_without_restart_budget(managed, tmp_path):
    coord, fake, manager = managed
    path = tmp_path / "frame.sock"
    coord.ipc_dependency_probe = lambda _plan: dependencies.probe_extension(str(path), "frame")
    manifest = _manifest("late-camera", [{"name": "camera.frames", "mode": "shared"}])
    manifest.pop("health")  # policy defaults to never; dependency waits still retry.
    first = coord.start("late-camera", manifest=manifest)
    assert first["observed_state"] == "waiting_dependency"
    assert fake.starts == []
    assert manager.snapshot()["allocations"] == []
    revision = state.load()["revision"]
    for _ in range(3):
        result = coord.reconcile_one("late-camera", manifest=manifest,
                                     launch=lambda **kw: fake.start("late-camera", **kw),
                                     now=time.time() + 10)
        assert result["observed_state"] == "waiting_dependency"
    assert state.load()["revision"] == revision
    with native_peer(path):
        result = coord.reconcile_one("late-camera", manifest=manifest,
                                     launch=lambda **kw: fake.start("late-camera", **kw),
                                     now=time.time() + 10)
    assert result["observed_state"] == "running"
    assert result["instance_id"] == first["instance_id"]
    assert len(fake.starts) == 1
    assert state.get_app("late-camera")["restart_history"] == []


def test_no_ipc_application_starts_while_firmware_is_down(managed, monkeypatch):
    coord, fake, _ = managed
    coord.ipc_dependency_probe = dependencies.probe_plan
    monkeypatch.setattr(dependencies, "probe_extension", lambda *args: pytest.fail(
        "frameless CPU app must not probe IPC"))
    result = coord.start("independent", manifest=_manifest("independent"))
    assert result["observed_state"] == "running"
    assert len(fake.starts) == 1


def test_protocol_failure_is_durable_and_does_not_auto_restart(managed):
    coord, fake, _ = managed
    calls = []
    coord.ipc_dependency_probe = lambda _plan: (
        calls.append(1) or {"available": False, "retryable": False,
                            "socket": "frame.sock", "error": "unsupported version"})
    manifest = _manifest("incompatible")
    result = coord.start("incompatible", manifest=manifest)
    assert result["observed_state"] == "failed"
    for _ in range(3):
        result = coord.reconcile_one("incompatible", manifest=manifest,
                                     launch=lambda **kw: fake.start("incompatible", **kw),
                                     now=time.time() + 1000)
        assert result["action"] == "dependency-incompatible"
    assert len(calls) == 1
    assert fake.starts == []
    coord.ipc_dependency_probe = lambda _plan: {"available": True}
    assert coord.start("incompatible", manifest=manifest)["observed_state"] == "running"


def test_legacy_dependency_wait_retries_but_legacy_crash_does_not(managed):
    coord, fake, _ = managed
    status = {"available": False, "retryable": True, "error": "CGI starting"}
    coord.ipc_dependency_probe = lambda _plan: dict(status)
    manifest = _manifest("legacy")
    coord.start("legacy", manifest=manifest, launch_mode="legacy")
    status["available"] = True
    result = coord.reconcile_one("legacy", manifest=manifest,
                                 launch=lambda **kw: fake.start("legacy", **kw),
                                 now=time.time() + 10)
    assert result["observed_state"] == "running"
    assert state.get_app("legacy")["launch_mode"] == "legacy"
    fake.crash("legacy")
    result = coord.reconcile_one("legacy", manifest=manifest,
                                 launch=lambda **kw: fake.start("legacy", **kw),
                                 now=time.time() + 20)
    assert result["action"] == "legacy-unmanaged"
    assert len(fake.starts) == 1


def test_legacy_wait_can_move_from_ipc_to_busy_resource_and_recover(managed):
    coord, fake, manager = managed
    status = {"available": False, "retryable": True, "error": "starting"}
    coord.ipc_dependency_probe = lambda _plan: dict(status)
    manifest = _manifest("legacy-wait", [{"name": "npu.rknn", "mode": "exclusive"}])
    holder = resources.plan_manifest(_manifest(
        "holder", [{"name": "npu.rknn", "mode": "exclusive"}]))
    manager.reserve("holder", "holder-instance", 1, holder)
    coord.start("legacy-wait", manifest=manifest, launch_mode="legacy")
    status["available"] = True
    launch = lambda **kw: fake.start("legacy-wait", **kw)
    result = coord.reconcile_one("legacy-wait", manifest=manifest, launch=launch,
                                 now=time.time() + 10)
    assert result["observed_state"] == "waiting_resource"
    manager.release("holder-instance", 1)
    result = coord.reconcile_one("legacy-wait", manifest=manifest, launch=launch,
                                 now=time.time() + 20)
    assert result["observed_state"] == "running"
    assert state.get_app("legacy-wait")["launch_mode"] == "legacy"
    assert len(fake.starts) == 1


def test_real_legacy_callback_waits_for_cgi_even_without_model_claims(managed, monkeypatch):
    coord, fake, _manager = managed
    status = {"available": False, "retryable": True, "error": "CGI starting"}
    handoffs = []
    coord.ipc_dependency_probe = dependencies.probe_plan
    monkeypatch.setattr(dependencies, "probe_extension", lambda *args: {
        "available": False, "retryable": True, "errno": errno.ENOENT})
    monkeypatch.setattr(dependencies, "probe_legacy_cgi", lambda timeout: dict(status))
    monkeypatch.setattr(server, "_npu_broker_present", lambda: False)
    monkeypatch.setattr(server.builtin, "stop", lambda: (
        handoffs.append("stop") or {"stop_confirmed": True}))
    monkeypatch.setattr(server, "_builtin_invalidate", lambda: None)
    monkeypatch.setattr(server.supervisor, "start", fake.start)
    manifest = _manifest("legacy-empty")
    launch = server._legacy_launch("legacy-empty", "boot_restore")
    result = coord.start("legacy-empty", manifest=manifest,
                         launch=launch, launch_mode="legacy", operation="boot_restore")
    assert result["observed_state"] == "waiting_dependency"
    assert handoffs == []
    assert fake.starts == []
    status["available"] = True
    result = coord.reconcile_one("legacy-empty", manifest=manifest,
                                 launch=launch, now=time.time() + 10)
    assert result["observed_state"] == "running"
    assert handoffs == ["stop"]
    assert fake.starts[0][1]["npu_managed"] is True


def test_ipc_disappearing_after_preflight_waits_only_after_cleanup(managed):
    coord, fake, manager = managed
    status = {"available": True}
    coord.ipc_dependency_probe = lambda _plan: dict(status)
    manifest = _manifest("racing")
    manifest.pop("health")

    def lost_ipc(**kwargs):
        status.update(available=False, retryable=True, error="connection refused")
        raise RuntimeError("child failed during endpoint open")

    result = coord.start("racing", manifest=manifest, launch=lost_ipc)
    assert result["observed_state"] == "waiting_dependency"
    assert result["instance_id"] is None  # Never reuse a potentially spawned generation.
    assert manager.snapshot()["allocations"] == []
    status["available"] = True
    result = coord.reconcile_one("racing", manifest=manifest,
                                 launch=lambda **kw: fake.start("racing", **kw),
                                 now=time.time() + 10)
    assert result["observed_state"] == "running"
    assert state.get_app("racing")["restart_history"] == []


def test_real_application_error_remains_failed_when_dependencies_are_healthy(managed):
    coord, _fake, _manager = managed
    with pytest.raises(ValueError, match="application bug"):
        coord.start("broken", manifest=_manifest("broken"),
                    launch=lambda **kwargs: (_ for _ in ()).throw(ValueError("application bug")))
    assert state.get_app("broken")["observed_state"] == "failed"


def test_absent_broker_uses_read_only_legacy_check_but_stale_broker_does_not(monkeypatch):
    plan = resources.plan_manifest({"id": "legacy", "models": [{"file": "x.rknn"}]})
    calls = []
    result = {"available": False, "retryable": True, "errno": errno.ENOENT}
    monkeypatch.setattr(dependencies, "probe_extension", lambda *args: dict(result))
    monkeypatch.setattr(dependencies, "probe_legacy_cgi", lambda timeout: (
        calls.append("GET") or {"available": True, "legacy_compatibility": True}))
    assert dependencies.probe_plan(plan)["available"] is True
    assert calls == ["GET"]
    result["errno"] = errno.ECONNREFUSED
    assert dependencies.probe_plan(plan)["available"] is False
    assert calls == ["GET"]


def test_cgi_tls_fallback_and_redirect_stay_read_only_on_loopback(monkeypatch):
    calls = []
    payload = json.dumps({"iEnable": 1, "sStatus": "running", "iActualFPS": 10}).encode()

    def request(tls, port, target, timeout):
        calls.append((tls, port, target, timeout))
        if len(calls) == 1:
            raise ConnectionRefusedError()
        if len(calls) == 2:
            return 307, b"", "https://arbitrary-host:444/cgi-bin/entry.cgi/model/inference?id=0"
        return 200, payload, None

    monkeypatch.setattr(dependencies, "_cgi_get", request)
    assert dependencies.probe_legacy_cgi()["available"] is True
    assert [(tls, port) for tls, port, _, _ in calls] == [(True, 443), (False, 80), (True, 444)]
    assert all(timeout < 0.5 for _, _, _, timeout in calls)


@pytest.mark.parametrize("status,raw,retryable", [
    (503, b"", True), (401, b"", False), (403, b"", False),
    (200, b"<html>login</html>", False), (200, b'{"code":0}', False),
])
def test_cgi_requires_actual_inference_status(monkeypatch, status, raw, retryable):
    monkeypatch.setattr(dependencies, "_cgi_get", lambda *args: (status, raw, None))
    result = dependencies.probe_legacy_cgi()
    assert result["available"] is False
    assert result["retryable"] is retryable


@pytest.mark.parametrize("headers_first", [False, True])
def test_cgi_total_deadline_bounds_slow_headers_and_body(headers_first):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    stop = threading.Event()
    requests = []

    def trickle():
        conn, _ = server.accept()
        with conn:
            conn.settimeout(1)
            requests.append(conn.recv(4096))
            try:
                if headers_first:
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 10000\r\n\r\n")
                while not stop.wait(0.005):
                    conn.sendall(b"x")
            except (OSError, ConnectionError):
                pass

    thread = threading.Thread(target=trickle, daemon=True)
    thread.start()
    try:
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            dependencies._cgi_get(False, port, dependencies._CGI_PATH, 0.08)
        assert time.monotonic() - started < 0.25
    finally:
        stop.set()
        thread.join(1)
        server.close()
    assert requests[0].startswith(b"GET /cgi-bin/entry.cgi/model/inference?id=0 HTTP/1.1")


def test_scheduled_probe_validates_protocol_and_status_without_loading(tmp_path):
    path = tmp_path / "inferenced.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)
    operations = []

    def respond():
        conn, _ = server.accept()
        with conn:
            conn.settimeout(1)
            for expected in ("hello", "status"):
                size = struct.unpack("!I", resources._recv_exact(conn, 4))[0]
                request = json.loads(resources._recv_exact(conn, size))
                operations.append(request["op"])
                if expected == "hello":
                    assert request["control_only"] is True
                response = {"protocol": 1, "op": expected, "ok": True,
                            "request_id": request["request_id"], "tensors": [],
                            "state": "running"}
                raw = json.dumps(response).encode()
                conn.sendall(struct.pack("!I", len(raw)) + raw)

    thread = threading.Thread(target=respond, daemon=True)
    thread.start()
    try:
        assert resources.probe_inference_service(str(path))["available"] is True
    finally:
        thread.join(2)
        server.close()
    assert operations == ["hello", "status"]

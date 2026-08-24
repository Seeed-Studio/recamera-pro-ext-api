import base64
import json
import os
import socket
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
from appmgr import coordinator, paths, resources, state, supervisor
from appmgr.gateway import ResultGateway
from kit.adapters import registry
from kit.adapters.result_sink import GatewayResultSink, WsResultSink


def _ws_connect(port):
    sock = socket.create_connection(("127.0.0.1", port), timeout=2)
    key = base64.b64encode(os.urandom(16)).decode()
    request = (
        "GET / HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    ).encode()
    sock.sendall(request)
    response = b""
    while b"\r\n\r\n" not in response:
        response += sock.recv(1024)
    assert response.startswith(b"HTTP/1.1 101")
    return sock


def _recv_exact(sock, size):
    out = b""
    while len(out) < size:
        out += sock.recv(size - len(out))
    return out


def _ws_json(sock):
    first = _recv_exact(sock, 2)
    assert first[0] == 0x81
    length = first[1] & 0x7F
    if length == 126:
        length = struct.unpack(">H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", _recv_exact(sock, 8))[0]
    return json.loads(_recv_exact(sock, length).decode())


def test_two_publishers_share_one_ws_and_gateway_overwrites_identity(tmp_path,
                                                                    monkeypatch):
    identities = {("app-a", "inst-a", 1), ("app-b", "inst-b", 7)}

    def resolve(peer_pid, app, instance, generation):
        if peer_pid == os.getpid() and (app, instance, generation) in identities:
            return {"app_id": app, "instance_id": instance,
                    "generation": generation, "pid": peer_pid}
        return None

    gateway = ResultGateway(uds_path=str(tmp_path / "results.sock"),
                            ws_port=0, identity_resolver=resolve)
    gateway.start()
    ws = _ws_connect(gateway.ws_port)
    ws.settimeout(3)
    sinks = []
    try:
        monkeypatch.setenv("RECAMERA_APP_ID", "app-a")
        monkeypatch.setenv("RECAMERA_APP_INSTANCE", "inst-a")
        monkeypatch.setenv("RECAMERA_APP_GENERATION", "1")
        a = GatewayResultSink(sock=gateway.uds_path)
        sinks.append(a)
        monkeypatch.setenv("RECAMERA_APP_ID", "app-b")
        monkeypatch.setenv("RECAMERA_APP_INSTANCE", "inst-b")
        monkeypatch.setenv("RECAMERA_APP_GENERATION", "7")
        b = GatewayResultSink(sock=gateway.uds_path)
        sinks.append(b)

        a.emit({"app": "spoofed", "results": [{"cls": 1}]}, 1.25)
        b.emit_meta({"type": "metrics", "app": "spoofed", "fps": 9.5})
        messages = [_ws_json(ws), _ws_json(ws)]
        by_app = {message["app"]: message for message in messages}
        assert set(by_app) == {"app-a", "app-b"}
        assert by_app["app-a"]["instance"] == "inst-a"
        assert by_app["app-a"]["generation"] == 1
        assert by_app["app-b"]["instance"] == "inst-b"
        assert gateway.status()["publishers"] == 2
    finally:
        for sink in sinks:
            sink.close()
        ws.close()
        gateway.stop()
    assert not os.path.exists(gateway.uds_path)


def test_registry_uses_gateway_only_when_managed_env_is_present(tmp_path,
                                                               monkeypatch):
    identity = ("managed", "instance", 3)

    def resolve(peer_pid, app, instance, generation):
        if peer_pid == os.getpid() and (app, instance, generation) == identity:
            return {"app_id": app, "instance_id": instance,
                    "generation": generation, "pid": peer_pid}
        return None

    gateway = ResultGateway(uds_path=str(tmp_path / "results.sock"),
                            ws_port=0, identity_resolver=resolve).start()
    sink = None
    standalone = None
    try:
        monkeypatch.delenv("RECAMERA_RESULT_OSD", raising=False)
        monkeypatch.delenv("RECAMERA_ADAPTER_PREFER", raising=False)
        monkeypatch.setenv("RECAMERA_RESULT_GATEWAY_SOCK", gateway.uds_path)
        monkeypatch.setenv("RECAMERA_APP_ID", identity[0])
        monkeypatch.setenv("RECAMERA_APP_INSTANCE", identity[1])
        monkeypatch.setenv("RECAMERA_APP_GENERATION", str(identity[2]))
        sink = registry.select_result_sink("ws", port=8124, app_id="ignored")
        assert isinstance(sink, GatewayResultSink)

        sink.close()
        sink = None
        monkeypatch.delenv("RECAMERA_RESULT_GATEWAY_SOCK")
        monkeypatch.delenv("RECAMERA_APP_INSTANCE")
        monkeypatch.delenv("RECAMERA_APP_GENERATION")
        standalone = registry.select_result_sink("ws", port=0, app_id="manual")
        assert isinstance(standalone, WsResultSink)
    finally:
        if sink:
            sink.close()
        if standalone:
            standalone.close()
        gateway.stop()


def test_gateway_rejects_unregistered_publisher_before_ready(tmp_path, monkeypatch):
    gateway = ResultGateway(uds_path=str(tmp_path / "results.sock"),
                            ws_port=0,
                            identity_resolver=lambda *_args: None).start()
    try:
        monkeypatch.setenv("RECAMERA_APP_ID", "bad-app")
        monkeypatch.setenv("RECAMERA_APP_INSTANCE", "bad-instance")
        monkeypatch.setenv("RECAMERA_APP_GENERATION", "1")
        started = time.monotonic()
        try:
            GatewayResultSink(sock=gateway.uds_path, connect_timeout=0.2)
        except Exception as exc:
            assert "identity rejected" in str(exc)
        else:
            raise AssertionError("unregistered publisher was accepted")
        assert time.monotonic() - started < 1.0
        assert gateway.status()["rejected"] >= 1
    finally:
        gateway.stop()


def test_concurrent_liveness_read_cannot_revoke_pre_ready_gateway_identity(
        tmp_path, monkeypatch):
    """Reproduce the device race at the run.pid commit boundary.

    Production's GET /apps read path calls ``is_running`` + ``observe`` while
    an operation worker starts the app.  Force that read from another thread
    at the exact `_write_run_ids` boundary.  With the unsafe state-first order
    this deterministically clears the freshly published PID and the real
    GatewayResultSink times out with ``publisher identity rejected``.
    """
    app_id = "qrcode-gateway-race"
    apps = tmp_path / "apps"
    appmgr_dir = tmp_path / "appmgr"
    kit_shim = tmp_path / "kit-shim"
    for directory in (apps, appmgr_dir, kit_shim):
        directory.mkdir()
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))
    monkeypatch.setattr(paths, "APPS_DIR", str(apps))
    monkeypatch.setattr(paths, "APPMGR_DIR", str(appmgr_dir))
    monkeypatch.setattr(paths, "APPDATA_DIR", str(tmp_path / "appdata"))
    monkeypatch.setattr(paths, "STATE_FILE", str(apps / "state.json"))
    monkeypatch.setattr(paths, "KIT_PARENT", repo_root)
    monkeypatch.setattr(paths, "KIT_DIR", str(kit_shim))
    monkeypatch.setattr(paths, "SDK_PYTHON", os.path.join(repo_root, "sdk", "python"))
    monkeypatch.setattr(paths, "RESULT_GATEWAY_SOCK", str(tmp_path / "results.sock"))
    (kit_shim / "run.py").write_text(
        "import runpy, sys\nrunpy.run_path(sys.argv[1], run_name='__main__')\n"
    )
    app_dir = apps / app_id
    app_dir.mkdir()
    manifest = {
        "id": app_id,
        "version": "1.0.0",
        "entry": "app.py",
        "output": {"sink": "ws"},
        "resources": {"claims": [
            {"name": "result.publish", "mode": "brokered", "required": True},
        ]},
    }
    (app_dir / "manifest.json").write_text(json.dumps(manifest))
    (app_dir / "app.py").write_text(
        "import os, time\n"
        "from kit.adapters.result_sink import GatewayResultSink\n"
        "sink = GatewayResultSink(connect_timeout=2.0)\n"
        "open(os.environ['APPMGR_READY_FILE'], 'w').write('ready')\n"
        "time.sleep(120)\n"
    )
    state.save({"active_app": None, "active_version": None})
    supervisor._apps.clear()
    del supervisor._reaped[:]

    class NoopRegistry:
        @staticmethod
        def revoke(*_args, **_kwargs):
            return True

    manager = resources.ResourceManager(str(appmgr_dir / "resources.json"))
    coord = coordinator.AppCoordinator(
        resource_manager=manager,
        supervisor_module=supervisor,
        inference_registry=NoopRegistry(),
        result_gateway_sock=paths.RESULT_GATEWAY_SOCK,
    )
    gateway = ResultGateway(
        uds_path=paths.RESULT_GATEWAY_SOCK,
        ws_port=0,
        identity_resolver=coord.resolve_identity,
    ).start()
    real_write = supervisor._write_run_ids
    raced = []
    read_errors = []

    def write_with_concurrent_read(name, pid, pgid, boot_id):
        def read_liveness():
            try:
                visible = supervisor.is_running(name)
                raced.append((visible, coord.observe(name, visible)))
            except BaseException as exc:
                read_errors.append(exc)

        reader = threading.Thread(target=read_liveness)
        reader.start()
        reader.join(timeout=3)
        assert not reader.is_alive(), "concurrent lifecycle read deadlocked"
        if read_errors:
            raise read_errors[0]
        real_write(name, pid, pgid, boot_id)

    monkeypatch.setattr(supervisor, "_write_run_ids", write_with_concurrent_read)
    started = None
    try:
        result = coord.start(app_id, manifest=manifest)
        started = result["pid"]
        assert started and result["observed_state"] == "running"
        assert raced and raced[0][0] is None
        assert raced[0][1]["observed_state"] == "starting"
        assert raced[0][1]["pid"] is None
        assert gateway.status()["publishers"] == 1
        assert coord.resolve_identity(
            started, app_id, result["instance_id"], result["generation"]
        )["pid"] == started
    finally:
        if started is not None:
            coord.stop(app_id)
        else:
            supervisor.stop(app_id, grace=0.0)
        gateway.stop()
        supervisor._apps.clear()
        del supervisor._reaped[:]

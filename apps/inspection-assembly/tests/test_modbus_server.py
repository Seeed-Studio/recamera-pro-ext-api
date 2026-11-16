"""Exercise the shipped PyModbus API over TCP, including address zero."""
import importlib.util
import json
from pathlib import Path
import socket
import sys
import time

import numpy as np
import pymodbus
from pymodbus.client import ModbusTcpClient
import pytest


APP_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(APP_DIR))
try:
    from core_inspection.modbus_map import HR_COUNT, ModbusFrame
    from core_inspection.modbus_server import ModbusService
finally:
    sys.path.remove(str(APP_DIR))


@pytest.fixture
def service():
    instance = ModbusService({"enabled": True, "bind": "127.0.0.1", "port": 0,
                              "unit_id": 7, "heartbeat_interval_s": 3600})
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()


def connect(service):
    port = service._server.transport.sockets[0].getsockname()[1]
    client = ModbusTcpClient("127.0.0.1", port=port, timeout=1, retries=0)
    assert client.connect()
    return client


def test_regressions_use_the_packaged_pymodbus_version():
    manifest = json.loads((APP_DIR / "manifest.json").read_text())
    wheel = next(w for w in manifest["python"]["wheels"] if w["name"] == "pymodbus")
    assert pymodbus.__version__ == wheel["version"]


def test_disabled_service_does_not_open_a_server():
    service = ModbusService({"enabled": False})
    service.start()
    assert service.apply_verdict(None) is None
    service.stop()
    assert service.status() == {"enabled": False}
    assert service._server_thread is None


def test_verdict_registers_and_coils_reach_tcp_client(service):
    registers = (5, 2, 1234, 2345, 3456, 4567, 0x1234, 0x5678, 3, 4, 1234, 2)
    with connect(service) as client:
        service.store.apply(ModbusFrame(registers, (True, False)))
        result = client.read_holding_registers(0, count=HR_COUNT, device_id=7)
        assert not result.isError()
        assert result.registers == list(registers)
        coils = client.read_coils(0, count=2, device_id=7)
        assert not coils.isError()
        assert coils.bits[:2] == [True, False]

        # Subsequent OK verdict replaces the live store, not only SimData's
        # initialization values. Holding and coil address spaces stay separate.
        service.store.apply(ModbusFrame((0,) * HR_COUNT, (False, True)))
        assert client.read_holding_registers(0, count=HR_COUNT, device_id=7).registers == [0] * HR_COUNT
        assert client.read_coils(0, count=2, device_id=7).bits[:2] == [False, True]
        assert client.read_coils(1, count=1, device_id=7).bits[0] is True
        assert client.read_holding_registers(0, count=1, device_id=8).isError()

    status = service.status()
    assert status["running"] is True
    assert status["writes"] == 2
    assert status["holding_registers"] == [0] * HR_COUNT
    assert status["coils"] == [0, 1]
    assert status["error"] is None


def test_heartbeat_updates_only_its_two_registers(service):
    registers = tuple(range(100, 100 + HR_COUNT))
    service.store.apply(ModbusFrame(registers, (True, False)))
    assert service.store.beat(0x1234ABCD) == (0x1234, 0xABCD)
    expected = list(registers)
    expected[6:8] = [0x1234, 0xABCD]
    with connect(service) as client:
        assert client.read_holding_registers(0, count=HR_COUNT, device_id=7).registers == expected
        assert client.read_coils(0, count=2, device_id=7).bits[:2] == [True, False]
    assert service.store.writes == 1


def test_background_heartbeat_and_restart_release_threads_and_port():
    service = ModbusService({"enabled": True, "bind": "127.0.0.1", "port": 0,
                             "heartbeat_interval_s": 0.01})
    for _ in range(2):
        service.start()
        try:
            port = service._server.transport.sockets[0].getsockname()[1]
            deadline = time.monotonic() + 2
            while service.store.read_holding(6, 2) == [0, 0]:
                assert time.monotonic() < deadline, service.status()
                time.sleep(0.01)
            hi, lo = service.store.read_holding(6, 2)
            assert abs(((hi << 16) | lo) - int(time.time())) <= 2
        finally:
            service.stop()
        assert not service._server_thread.is_alive()
        assert not service._beat_thread.is_alive()
        assert service.status()["running"] is False
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))
        service.port = port
    service.stop()  # Repeated stop is harmless.


def test_stopping_another_service_does_not_stop_this_server(service):
    other = ModbusService({"enabled": True, "bind": "127.0.0.1", "port": 0})
    other.start()
    other.stop()
    with connect(service) as client:
        assert not client.read_holding_registers(0, count=HR_COUNT, device_id=7).isError()


def test_port_conflict_is_reported_and_does_not_leave_threads():
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        service = ModbusService({"enabled": True, "bind": "127.0.0.1",
                                 "port": occupied.getsockname()[1]})
        try:
            with pytest.raises(RuntimeError, match="Modbus server failed to start"):
                service.start()
            assert service.error
            assert service._beat_thread is None
            assert not service._server_thread.is_alive()
        finally:
            service.stop()


@pytest.mark.parametrize("fail_start", [False, True])
def test_inspection_app_starts_with_modbus_enabled(monkeypatch, fail_start):
    monkeypatch.syspath_prepend(str(APP_DIR))
    # Match kit.run's entry loading while keeping the real InspectionApp,
    # publisher, Modbus service and health path. Only hardware is substituted.
    spec = importlib.util.spec_from_file_location("inspection_modbus_entry", APP_DIR / "app.py")
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    from core_inspection.app import InspectionApp
    from core_inspection.config import apply_defaults
    from core_inspection.types import Detection

    class Capture:
        consumed = False
        released = False

        def read(self):
            if self.consumed:
                return False, None
            self.consumed = True
            return True, np.zeros((32, 32, 3), dtype=np.uint8)

        def release(self):
            self.released = True

    class Detector:
        accelerator = "rknn"
        closed = False

        def warmup(self):
            if fail_start:
                raise RuntimeError("controlled warmup failure")

        def detect(self, frame):
            return [Detection(3, "spur", 0.9, 0.5, 0.5, 0.2, 0.1)]

        def close(self):
            self.closed = True

    shell = entry.InspectionAssemblyApp()
    raw = shell.build_config()
    raw["sources"][0].update(kind="file", uri="controlled-frame", fps=0, loop=False)
    raw["web"]["enabled"] = False
    raw["modbus"].update(bind="127.0.0.1", port=0)
    detector = Detector()
    capture = Capture()
    app = InspectionApp(apply_defaults(raw), detector=detector,
                        capture_factory=lambda spec: capture)
    if fail_start:
        with pytest.raises(RuntimeError, match="controlled warmup failure"):
            app.run(duration_s=1)
    else:
        summary = app.run(max_frames=1, duration_s=1)
        assert summary["frames_processed"] == summary["ng_frames"] == 1
        assert summary["mqtt"]["published"] == 1
        assert summary["modbus"]["enabled"] is True
        assert summary["modbus"]["running"] is True
        assert summary["modbus"]["error"] is None
        assert summary["modbus"]["coils"] == [1, 0]
        assert summary["modbus"]["holding_registers"][:6] == [3, 1, 5000, 5000, 2000, 1000]
        assert capture.released
        assert not app.modbus._server_thread.is_alive()
    assert detector.closed

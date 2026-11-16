"""Modbus TCP server（pymodbus）。寄存器表见 contracts/MODBUS.md。

写入顺序是契约的一部分：**先原子更新 HR 0–11，再写 Coil 0/1**。
PLC 侧只要看到线圈翻转，寄存器里就已经是同一次判定的数据。
编码本身在 `modbus_map.py`（纯函数、可单测），这里只负责落盘到 datastore
和跑 TCP server。
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

from .modbus_map import (HR_COUNT, HR_HEARTBEAT_HI, ModbusFrame,
                         encode_heartbeat, encode_verdict)
from .dimension import DimensionResult
from .rules import Verdict

LOGGER = logging.getLogger(__name__)

FX_COILS = 1
FX_HOLDING = 3


def build_context(unit_id: int = 1):
    """PyModbus 3.15 的四个独立地址空间，协议地址从 0 开始。"""
    from pymodbus.simulator import DataType, SimData, SimDevice

    return SimDevice(
        id=unit_id,
        simdata=(
            [SimData(0, values=[False] * 16, datatype=DataType.BITS)],
            [SimData(0, values=[False] * 16, datatype=DataType.BITS)],
            [SimData(0, count=max(HR_COUNT, 16), datatype=DataType.REGISTERS)],
            [SimData(0, count=16, datatype=DataType.REGISTERS)],
        ),
    )


class ModbusStore:
    """把推理/心跳线程的同步调用交给 server 所属的 asyncio loop。"""

    def __init__(self, server, loop, unit_id: int = 1) -> None:
        self.server = server
        self.loop = loop
        self.unit_id = unit_id
        self._lock = threading.Lock()
        self.writes = 0
        self.last_frame: ModbusFrame | None = None

    def _call(self, coroutine):
        if not self.loop.is_running():
            coroutine.close()
            raise RuntimeError("Modbus server is not running")
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        try:
            return future.result(timeout=3.0)
        except Exception:
            future.cancel()
            raise

    async def _apply(self, frame: ModbusFrame) -> None:
        # SimDevice 没有异步 action：两次内存更新之间不会让出事件循环，
        # TCP 请求不能插入半次判定。不能直接修改 SimData 初始化值；server
        # 启动时会复制它们，运行期必须使用 server.async_*Values。
        await self.server.async_setValues(
            self.unit_id, FX_HOLDING, 0, list(frame.registers))
        await self.server.async_setValues(
            self.unit_id, FX_COILS, 0, list(frame.coils))

    def apply(self, frame: ModbusFrame) -> None:
        """先 HR 后 Coil，在 server loop 中完成一次判定更新。"""
        with self._lock:
            self._call(self._apply(frame))
            self.writes += 1
            self.last_frame = frame

    def apply_verdict(self, verdict: Verdict, unix_seconds: int | None = None,
                      dimension: DimensionResult | None = None) -> ModbusFrame:
        frame = encode_verdict(verdict, int(unix_seconds if unix_seconds is not None
                                            else time.time()),
                               dimension=dimension)
        self.apply(frame)
        return frame

    def beat(self, unix_seconds: int | None = None) -> tuple[int, int]:
        """只刷心跳两字，不动判定字段（契约：心跳独立于判定）。"""
        hi, lo = encode_heartbeat(int(unix_seconds if unix_seconds is not None
                                      else time.time()))
        with self._lock:
            self._call(self.server.async_setValues(
                self.unit_id, FX_HOLDING, HR_HEARTBEAT_HI, [hi, lo]))
        return hi, lo

    # -- 读侧（自检与单测用）---------------------------------------------
    def read_holding(self, address: int = 0, count: int = HR_COUNT) -> list[int]:
        with self._lock:
            return list(self._call(self.server.async_getValues(
                self.unit_id, FX_HOLDING, address, count)))

    def read_coils(self, address: int = 0, count: int = 2) -> list[int]:
        with self._lock:
            return [int(bool(v))
                    for v in self._call(self.server.async_getValues(
                        self.unit_id, FX_COILS, address, count))]


class ModbusService:
    """store + 后台 TCP server + 心跳线程。"""

    def __init__(self, config: dict[str, Any]) -> None:
        self.enabled = bool(config.get("enabled", False))
        self.bind = str(config.get("bind", "0.0.0.0"))
        self.port = int(config.get("port", 502))
        self.unit_id = int(config.get("unit_id", 1))
        self.heartbeat_interval_s = float(config.get("heartbeat_interval_s", 1.0))
        self.context = build_context(self.unit_id) if self.enabled else None
        self.store: ModbusStore | None = None
        self._server = None
        self._loop = None
        self._server_thread: threading.Thread | None = None
        self._beat_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self.error: str | None = None

    def start(self) -> None:
        if not self.enabled:
            return
        if self._server_thread is not None and self._server_thread.is_alive():
            return
        self._stop.clear()
        self._ready.clear()
        self.error = None
        self._server_thread = threading.Thread(target=self._serve, daemon=True,
                                               name="modbus-server")
        self._server_thread.start()
        if not self._ready.wait(timeout=5.0):
            self.stop()
            raise RuntimeError("Modbus server startup timed out")
        if self.error is not None:
            self.stop()
            raise RuntimeError(f"Modbus server failed to start: {self.error}")
        self._beat_thread = threading.Thread(target=self._beat_loop, daemon=True,
                                             name="modbus-heartbeat")
        self._beat_thread.start()

    def _serve(self) -> None:
        try:
            asyncio.run(self._serve_async())
        except Exception as exc:  # noqa: BLE001 - 端口占用/权限不足要报出来
            self.error = str(exc)
            LOGGER.error("Modbus server stopped: %s", exc)
        finally:
            self._ready.set()

    async def _serve_async(self) -> None:
        from pymodbus.server import ModbusTcpServer

        self._loop = asyncio.get_running_loop()
        self._server = ModbusTcpServer(
            context=self.context, address=(self.bind, self.port))
        try:
            await self._server.serve_forever(background=True)
            self.store = ModbusStore(self._server, self._loop, self.unit_id)
            LOGGER.info("Modbus TCP server on %s:%s unit=%s",
                        self.bind, self.port, self.unit_id)
            self._ready.set()
            if not self._stop.is_set():
                await self._server.serving
        finally:
            await self._server.shutdown()

    def _beat_loop(self) -> None:
        while not self._stop.wait(self.heartbeat_interval_s):
            try:
                self.store.beat()
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("heartbeat failed: %s", exc)

    def apply_verdict(self, verdict: Verdict,
                      dimension: DimensionResult | None = None
                      ) -> ModbusFrame | None:
        if not self.enabled:
            return None
        if self.store is None:
            raise RuntimeError("Modbus server has not started")
        return self.store.apply_verdict(verdict, dimension=dimension)

    def stop(self) -> None:
        self._stop.set()
        if not self.enabled:
            return
        if self._beat_thread is not None:
            self._beat_thread.join(timeout=4.0)
        if self._loop is not None and self._loop.is_running():
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._server.shutdown(), self._loop)
                future.result(timeout=3.0)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Modbus shutdown failed: %s", exc)
        if self._server_thread is not None:
            self._server_thread.join(timeout=4.0)

    def status(self) -> dict[str, Any]:
        if not self.enabled:
            return {"enabled": False}
        running = (self.store is not None and self._loop is not None
                   and self._loop.is_running())
        return {
            "enabled": True,
            "bind": self.bind,
            "port": self.port,
            "unit_id": self.unit_id,
            "running": running,
            "writes": self.store.writes if self.store is not None else 0,
            "holding_registers": self.store.read_holding() if running else [],
            "coils": self.store.read_coils() if running else [],
            "error": self.error,
        }

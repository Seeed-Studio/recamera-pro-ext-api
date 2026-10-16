"""Modbus TCP server（pymodbus）。寄存器表见 contracts/MODBUS.md。

写入顺序是契约的一部分：**先原子更新 HR 0–11，再写 Coil 0/1**。
PLC 侧只要看到线圈翻转，寄存器里就已经是同一次判定的数据。
编码本身在 `modbus_map.py`（纯函数、可单测），这里只负责落盘到 datastore
和跑 TCP server。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from .modbus_map import (HR_COUNT, HR_HEARTBEAT_HI, HR_HEARTBEAT_LO, ModbusFrame,
                         encode_heartbeat, encode_verdict)
from .dimension import DimensionResult
from .rules import Verdict

LOGGER = logging.getLogger(__name__)

FX_COILS = 1
FX_HOLDING = 3


def build_context(unit_id: int = 1):
    """建一个只含 coil + holding register 的 slave context。"""
    from pymodbus.datastore import (ModbusSequentialDataBlock, ModbusServerContext,
                                    ModbusSlaveContext)

    slave = ModbusSlaveContext(
        co=ModbusSequentialDataBlock(0, [0] * 16),
        hr=ModbusSequentialDataBlock(0, [0] * max(HR_COUNT, 16)),
        di=ModbusSequentialDataBlock(0, [0] * 16),
        ir=ModbusSequentialDataBlock(0, [0] * 16),
        zero_mode=True,
    )
    return ModbusServerContext(slaves={unit_id: slave}, single=False)


class ModbusStore:
    """datastore 的写入面。与 server 分开，单测不必起 TCP 端口。"""

    def __init__(self, context, unit_id: int = 1) -> None:
        self.context = context
        self.unit_id = unit_id
        self._lock = threading.Lock()
        self.writes = 0
        self.last_frame: ModbusFrame | None = None

    @property
    def _slave(self):
        return self.context[self.unit_id]

    def apply(self, frame: ModbusFrame) -> None:
        """先 HR 后 Coil，整段在同一把锁里，读侧看不到半截状态。"""
        with self._lock:
            self._slave.setValues(FX_HOLDING, 0, list(frame.registers))
            self._slave.setValues(FX_COILS, 0,
                                  [1 if bit else 0 for bit in frame.coils])
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
            self._slave.setValues(FX_HOLDING, HR_HEARTBEAT_HI, [hi])
            self._slave.setValues(FX_HOLDING, HR_HEARTBEAT_LO, [lo])
        return hi, lo

    # -- 读侧（自检与单测用）---------------------------------------------
    def read_holding(self, address: int = 0, count: int = HR_COUNT) -> list[int]:
        with self._lock:
            return list(self._slave.getValues(FX_HOLDING, address, count))

    def read_coils(self, address: int = 0, count: int = 2) -> list[int]:
        with self._lock:
            return [int(bool(v))
                    for v in self._slave.getValues(FX_COILS, address, count)]


class ModbusService:
    """store + 后台 TCP server + 心跳线程。"""

    def __init__(self, config: dict[str, Any]) -> None:
        self.enabled = bool(config.get("enabled", False))
        self.bind = str(config.get("bind", "0.0.0.0"))
        self.port = int(config.get("port", 502))
        self.unit_id = int(config.get("unit_id", 1))
        self.heartbeat_interval_s = float(config.get("heartbeat_interval_s", 1.0))
        self.context = build_context(self.unit_id) if self.enabled else None
        self.store = ModbusStore(self.context, self.unit_id) if self.enabled else None
        self._server_thread: threading.Thread | None = None
        self._beat_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.error: str | None = None

    def start(self) -> None:
        if not self.enabled:
            return
        self._server_thread = threading.Thread(target=self._serve, daemon=True,
                                               name="modbus-server")
        self._server_thread.start()
        self._beat_thread = threading.Thread(target=self._beat_loop, daemon=True,
                                             name="modbus-heartbeat")
        self._beat_thread.start()

    def _serve(self) -> None:
        from pymodbus.server import StartTcpServer

        try:
            LOGGER.info("Modbus TCP server on %s:%s unit=%s",
                        self.bind, self.port, self.unit_id)
            StartTcpServer(context=self.context, address=(self.bind, self.port))
        except Exception as exc:  # noqa: BLE001 - 端口占用/权限不足要报出来
            self.error = str(exc)
            LOGGER.error("Modbus server stopped: %s", exc)

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
        return self.store.apply_verdict(verdict, dimension=dimension)

    def stop(self) -> None:
        self._stop.set()
        if not self.enabled:
            return
        try:
            from pymodbus.server import ServerStop

            ServerStop()
        except Exception:  # noqa: BLE001 - server 未起来时忽略
            pass

    def status(self) -> dict[str, Any]:
        if not self.enabled:
            return {"enabled": False}
        return {
            "enabled": True,
            "bind": self.bind,
            "port": self.port,
            "unit_id": self.unit_id,
            "writes": self.store.writes,
            "holding_registers": self.store.read_holding(),
            "coils": self.store.read_coils(),
            "error": self.error,
        }

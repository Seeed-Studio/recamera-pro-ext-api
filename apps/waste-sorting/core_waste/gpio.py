"""GPIO / 继电器输出的抽象。不绑引脚、不 import 任何厂商库。

spec §1 删掉 Modbus，只留这一层；spec §5 要求它是「异步回调接口」——
分类结果出来之后调 `on_result`，具体动作（推杆、翻板、指示灯）由现场
注册的回调决定，core 不知道有几个引脚、是高有效还是低有效。
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Protocol, runtime_checkable

LOGGER = logging.getLogger(__name__)

#: 回调签名：(中国四分类, 物料类名, 置信度) -> None
ResultCallback = Callable[[str, str, float], None]


@runtime_checkable
class ActuatorSink(Protocol):
    """任何「按分类结果动一下」的下游。"""

    def on_result(self, china_category: str, material: str,
                  confidence: float) -> None:
        ...

    def close(self) -> None:
        ...


class CallbackActuator:
    """把注册进来的回调在后台线程里逐个调用，异常不外泄到推理循环。"""

    def __init__(self, min_confidence: float = 0.0) -> None:
        self.min_confidence = float(min_confidence)
        self._callbacks: list[ResultCallback] = []
        self._lock = threading.Lock()
        self.dispatched = 0
        self.skipped = 0
        self.errors = 0

    def register(self, callback: ResultCallback) -> None:
        with self._lock:
            self._callbacks.append(callback)

    def on_result(self, china_category: str, material: str,
                  confidence: float) -> None:
        if confidence < self.min_confidence:
            self.skipped += 1
            return
        with self._lock:
            callbacks = list(self._callbacks)
        for callback in callbacks:
            try:
                callback(china_category, material, float(confidence))
                self.dispatched += 1
            except Exception:  # noqa: BLE001 - 现场回调不得拖垮推理循环
                self.errors += 1
                LOGGER.exception("actuator callback failed")

    def close(self) -> None:
        with self._lock:
            self._callbacks.clear()


class NullActuator:
    """没接执行机构时的占位。计数留着，健康接口能看出「本该动几次」。"""

    def __init__(self) -> None:
        self.dispatched = 0

    def on_result(self, china_category: str, material: str,
                  confidence: float) -> None:
        self.dispatched += 1

    def close(self) -> None:
        pass

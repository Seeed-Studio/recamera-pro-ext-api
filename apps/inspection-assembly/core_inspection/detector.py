"""检测后端接口。M1 冻结，M2 起 backends/{tensorrt,hailo,rknn} 各自实现。

核心层只接收「解码好的帧」并拿回「归一化框」。任何厂商 SDK、engine/HEF/rknn
句柄、预处理张量布局都留在 backend 内部，core 层不感知。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

import numpy as np

from .types import Detection


@runtime_checkable
class Detector(Protocol):
    """所有加速器后端实现的唯一接口。"""

    #: 后端标识，取值同 contracts 的 model.accelerator
    accelerator: str
    #: 类别表，顺序即 class_id
    classes: tuple[str, ...]
    #: 模型输入尺寸 (width, height)
    input_size: tuple[int, int]
    #: 产出该后端制品的 ONNX 的 SHA256，写进 MQTT model.onnx_sha256
    onnx_sha256: str

    def warmup(self, iterations: int = 3) -> None:
        """跑若干次空推理，把 lazy 初始化和显存分配挪到启动阶段。"""

    def detect(self, frame_bgr: np.ndarray) -> list[Detection]:
        """输入 HWC uint8 BGR 帧，返回归一化框，按 score 降序。

        实现负责：letterbox 预处理、推理、解码、NMS、坐标反算回原图比例。
        实现不负责：OK/NG 判定、MQTT、Modbus、绘图。
        """

    def close(self) -> None:
        """释放句柄。可重复调用。"""


class DetectorError(RuntimeError):
    """后端初始化或推理失败。"""

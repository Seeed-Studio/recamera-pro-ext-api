"""与硬件无关的核心数据类型。厂商 SDK 只允许出现在 backends/ 里。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Detection:
    """一个检测框。坐标是归一化的 [0,1] 中心点 + 宽高。"""

    class_id: int
    class_name: str
    score: float
    cx: float
    cy: float
    w: float
    h: float

    @property
    def bbox(self) -> list[float]:
        return [self.cx, self.cy, self.w, self.h]

    @property
    def area(self) -> float:
        return self.w * self.h

    def to_json(self, slot: int) -> dict:
        return {
            "slot": slot,
            "class_id": self.class_id,
            "class_name": self.class_name,
            "score": round(float(self.score), 4),
            "bbox": [round(float(v), 4) for v in self.bbox],
        }


@dataclass(frozen=True)
class FrameMeta:
    """一帧的采集元信息。capture_ns 是采集线程入队时的 monotonic ns。"""

    stream_id: str
    frame_id: int
    width: int
    height: int
    capture_ns: int


@dataclass
class InferenceResult:
    """一次推理的输出。detections 已按 score 降序。"""

    meta: FrameMeta
    detections: list[Detection] = field(default_factory=list)
    inference_time_ms: float = 0.0


def parse_roi(value) -> tuple[float, float, float, float]:
    """归一化 ROI `[x1, y1, x2, y2]` 的解析与校验。

    缺件（`assembly`）与尺寸（`dimension`）两段配置用的是同一种 ROI，
    校验规则也必须是同一份，否则两边对"合法 ROI"的定义会各自漂移。
    """
    if len(value) != 4:
        raise ValueError(f"roi must be [x1, y1, x2, y2], got {value!r}")
    x1, y1, x2, y2 = (float(v) for v in value)
    if x2 <= x1 or y2 <= y1:
        raise ValueError(f"roi must satisfy x2>x1 and y2>y1, got {value!r}")
    return (x1, y1, x2, y2)

"""与硬件无关的核心数据类型。厂商 SDK 只允许出现在 backends/ 里。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class FrameMeta:
    """一帧的采集元信息。capture_ns 是采集线程入队时的 monotonic ns。"""

    stream_id: str
    frame_id: int
    width: int
    height: int
    capture_ns: int


#: 触发来源。spec §5「触发即拍」：按钮 / HTTP / 移动侦测，外加连续模式。
TRIGGERS = ("button", "http", "motion", "continuous", "file")


@dataclass(frozen=True)
class Prediction:
    """一次分类的结果。`ranked` 是 taxonomy.top_k 的输出，已按置信度降序。"""

    meta: FrameMeta
    ranked: list[dict]
    inference_time_ms: float = 0.0
    trigger: str = "continuous"

    @property
    def top1(self) -> dict:
        return self.ranked[0]

    @property
    def class_id(self) -> int:
        return int(self.ranked[0]["class_id"])

    @property
    def class_name(self) -> str:
        return str(self.ranked[0]["class_name"])

    @property
    def confidence(self) -> float:
        return float(self.ranked[0]["confidence"])

    @property
    def china_category(self) -> str:
        return str(self.ranked[0]["china_category"])


@dataclass
class ImageRef:
    """图片只发引用，不发字节。spec §5：本地路径或对象存储 URI。"""

    kind: str = "none"  # none | local | object_store
    uri: str = ""

    def to_json(self) -> dict:
        return {"kind": self.kind, "uri": self.uri}


@dataclass
class RuntimeCounters:
    """去抖 / 合并请求的计数，健康接口读它。"""

    triggered: int = 0
    debounced: int = 0
    merged: int = 0
    published: int = 0
    extras: dict = field(default_factory=dict)

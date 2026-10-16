"""装配缺件模块：模板期望件清单 vs 检测结果。

比较三件事，缺一不可：
1. **类别** —— 期望件声明 `class_name`，只有同类别的检测框才可能匹配上；
2. **ROI** —— 期望件声明归一化 `roi = [x1, y1, x2, y2]`，检测框中心必须落在 ROI 内；
3. **匹配距离** —— 检测框中心到 ROI 中心的归一化欧氏距离必须 <= `match_distance`。

匹配是全局贪心：把所有 (期望件, 检测框) 的合法配对按距离升序排，
依次占用，一个检测框只能匹配一个期望件、一个期望件最多吃 `min_count` 个框。
没吃够的期望件进 `missing`，没被任何期望件吃掉的检测框进 `extra`。

与硬件、协议无关，纯 Python。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .types import Detection, parse_roi

DEFAULT_MATCH_DISTANCE = 0.15


@dataclass(frozen=True)
class ExpectedItem:
    """模板期望件清单里的一条。对应 config schema 的 `assembly.expected[]`。"""

    class_name: str
    roi: tuple[float, float, float, float]
    min_count: int = 1
    label: str = ""
    match_distance: float | None = None   # None = 用 AssemblyConfig 的默认值

    @property
    def name(self) -> str:
        return self.label or f"{self.class_name}@{self.roi}"

    @property
    def roi_center(self) -> tuple[float, float]:
        x1, y1, x2, y2 = self.roi
        return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)

    def contains(self, cx: float, cy: float) -> bool:
        x1, y1, x2, y2 = self.roi
        return x1 <= cx <= x2 and y1 <= cy <= y2

    @classmethod
    def from_config(cls, config: dict) -> "ExpectedItem":
        return cls(
            class_name=str(config["class"]),
            roi=parse_roi(config["roi"]),
            min_count=int(config.get("min_count", 1)),
            label=str(config.get("label", "")),
            match_distance=(None if config.get("match_distance") is None
                            else float(config["match_distance"])),
        )

    def to_json(self) -> dict:
        return {"label": self.name, "class_name": self.class_name,
                "roi": [round(float(v), 4) for v in self.roi],
                "min_count": self.min_count}


@dataclass(frozen=True)
class AssemblyConfig:
    """对应 config schema 的 `assembly` 段。expected 为空 = 不做缺件判定。"""

    expected: tuple[ExpectedItem, ...] = ()
    match_distance: float = DEFAULT_MATCH_DISTANCE
    min_score: float = 0.25
    report_extra: bool = True

    @property
    def enabled(self) -> bool:
        return bool(self.expected)

    @classmethod
    def from_config(cls, config: dict | None) -> "AssemblyConfig":
        config = config or {}
        return cls(
            expected=tuple(ExpectedItem.from_config(item)
                           for item in config.get("expected", ())),
            match_distance=float(config.get("match_distance",
                                            DEFAULT_MATCH_DISTANCE)),
            min_score=float(config.get("min_score", 0.25)),
            report_extra=bool(config.get("report_extra", True)),
        )


@dataclass(frozen=True)
class MissingItem:
    item: ExpectedItem
    found: int

    def to_json(self) -> dict:
        payload = self.item.to_json()
        payload["found"] = self.found
        payload["expected"] = self.item.min_count
        return payload


@dataclass
class AssemblyResult:
    """一帧的缺件比对结果。"""

    missing: tuple[MissingItem, ...] = ()
    extra: tuple[Detection, ...] = ()
    matched: dict[str, list[Detection]] = field(default_factory=dict)
    expected_count: int = 0
    enabled: bool = False

    @property
    def missing_count(self) -> int:
        return len(self.missing)

    @property
    def extra_count(self) -> int:
        return len(self.extra)

    @property
    def matched_count(self) -> int:
        return sum(len(v) for v in self.matched.values())

    def to_json(self) -> dict:
        return {
            "enabled": self.enabled,
            "expected_count": self.expected_count,
            "matched_count": self.matched_count,
            "missing_count": self.missing_count,
            "extra_count": self.extra_count,
            "missing": [m.to_json() for m in self.missing],
            "extra": [{"class_id": d.class_id, "class_name": d.class_name,
                       "score": round(float(d.score), 4),
                       "bbox": [round(float(v), 4) for v in d.bbox]}
                      for d in self.extra],
        }


def compare(detections: list[Detection], config: AssemblyConfig) -> AssemblyResult:
    """把检测结果和期望件清单比一遍，输出 missing / extra。"""
    if not config.enabled:
        return AssemblyResult(enabled=False, expected_count=0)

    candidates = [d for d in detections if d.score >= config.min_score]

    pairs: list[tuple[float, int, int]] = []
    for i, item in enumerate(config.expected):
        limit = (config.match_distance if item.match_distance is None
                 else item.match_distance)
        rcx, rcy = item.roi_center
        for j, det in enumerate(candidates):
            if det.class_name != item.class_name:
                continue
            if not item.contains(det.cx, det.cy):
                continue
            distance = math.hypot(det.cx - rcx, det.cy - rcy)
            if distance > limit:
                continue
            pairs.append((distance, i, j))
    pairs.sort(key=lambda p: (p[0], p[1], p[2]))

    used_det: set[int] = set()
    taken: dict[int, list[Detection]] = {i: [] for i in range(len(config.expected))}
    for distance, i, j in pairs:
        if j in used_det:
            continue
        if len(taken[i]) >= config.expected[i].min_count:
            continue
        taken[i].append(candidates[j])
        used_det.add(j)

    missing = tuple(MissingItem(item=config.expected[i], found=len(taken[i]))
                    for i in range(len(config.expected))
                    if len(taken[i]) < config.expected[i].min_count)
    extra = (tuple(det for j, det in enumerate(candidates) if j not in used_det)
             if config.report_extra else ())
    matched = {config.expected[i].name: taken[i] for i in range(len(config.expected))}
    return AssemblyResult(missing=missing, extra=extra, matched=matched,
                          expected_count=len(config.expected), enabled=True)

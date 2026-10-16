"""OK/NG 规则引擎。与硬件、协议无关。

Demo B 合并四条 NG 依据，**任一成立即 NG**：

| 依据 | 来源 | reason code |
|---|---|---|
| 缺陷框达到数量下限 | `core_inspection.postprocess` 的检测结果（沿用 Demo A） | `defect` |
| 期望件缺失 | `core_inspection.assembly.compare` 的 `missing` | `missing` |
| 出现多余件 | 同上的 `extra` | `extra` |
| 尺寸超差 | `core_inspection.dimension.inspect` 的不合格项 | `dimension_out_of_tolerance` |

四条都可以在配置里单独关掉（`rules.ng_on_*`）。`consecutive_frames` 消抖作用在
**合并后的结果**上：连续 N 帧都命中至少一条依据才判 NG。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .assembly import AssemblyResult, MissingItem
from .dimension import DimensionResult, MeasurementResult
from .types import Detection

REASON_DEFECT = "defect"
REASON_MISSING = "missing"
REASON_EXTRA = "extra"
REASON_DIMENSION = "dimension_out_of_tolerance"
REASON_CODES = (REASON_DEFECT, REASON_MISSING, REASON_EXTRA, REASON_DIMENSION)


@dataclass(frozen=True)
class Rules:
    """对应 config schema 的 `rules` 段。"""

    ng_classes: tuple[str, ...] = ()      # 空 = 任何类别都算缺陷
    min_score: float = 0.35
    min_defects: int = 1
    min_area: float = 0.0
    consecutive_frames: int = 1
    ng_on_defect: bool = True
    ng_on_missing: bool = True
    ng_on_extra: bool = True
    ng_on_dimension: bool = True

    @classmethod
    def from_config(cls, config: dict) -> "Rules":
        return cls(
            ng_classes=tuple(config.get("ng_classes", ())),
            min_score=float(config.get("min_score", 0.35)),
            min_defects=int(config.get("min_defects", 1)),
            min_area=float(config.get("min_area", 0.0)),
            consecutive_frames=int(config.get("consecutive_frames", 1)),
            ng_on_defect=bool(config.get("ng_on_defect", True)),
            ng_on_missing=bool(config.get("ng_on_missing", True)),
            ng_on_extra=bool(config.get("ng_on_extra", True)),
            ng_on_dimension=bool(config.get("ng_on_dimension", True)),
        )


@dataclass(frozen=True)
class Verdict:
    verdict: str          # "OK" / "NG"
    reason: str
    defects: tuple[Detection, ...]
    reasons: tuple[str, ...] = ()                     # 命中的 reason code
    missing: tuple[MissingItem, ...] = ()
    extra: tuple[Detection, ...] = ()
    dimension_failures: tuple[MeasurementResult, ...] = ()

    @property
    def is_ng(self) -> bool:
        return self.verdict == "NG"

    @property
    def primary(self) -> Detection | None:
        return self.defects[0] if self.defects else None

    @property
    def missing_count(self) -> int:
        return len(self.missing)

    @property
    def extra_count(self) -> int:
        return len(self.extra)


def filter_defects(detections: list[Detection], rules: Rules) -> list[Detection]:
    """按阈值、类别白名单、面积下限筛出参与判定的框，score 降序。"""
    kept = [
        det for det in detections
        if det.score >= rules.min_score
        and det.area >= rules.min_area
        and (not rules.ng_classes or det.class_name in rules.ng_classes)
    ]
    kept.sort(key=lambda det: det.score, reverse=True)
    return kept


class VerdictEngine:
    """带 `consecutive_frames` 消抖的判定器。每个 stream 一个实例。"""

    def __init__(self, rules: Rules) -> None:
        self.rules = rules
        self._streak = 0

    def evaluate(self, detections: list[Detection],
                 assembly: AssemblyResult | None = None,
                 dimension: DimensionResult | None = None) -> Verdict:
        rules = self.rules
        defects = filter_defects(detections, rules)
        missing = tuple(assembly.missing) if assembly is not None else ()
        extra = tuple(assembly.extra) if assembly is not None else ()
        dim_failures = tuple(dimension.failures) if dimension is not None else ()

        reasons: list[str] = []
        parts: list[str] = []
        if rules.ng_on_defect and len(defects) >= rules.min_defects:
            reasons.append(REASON_DEFECT)
            parts.append(f"{len(defects)} defect(s) >= min_defects="
                         f"{rules.min_defects}")
        if rules.ng_on_missing and missing:
            reasons.append(REASON_MISSING)
            parts.append(f"{len(missing)} missing item(s): "
                         + ", ".join(m.item.name for m in missing))
        if rules.ng_on_extra and extra:
            reasons.append(REASON_EXTRA)
            parts.append(f"{len(extra)} extra item(s)")
        if rules.ng_on_dimension and dim_failures:
            reasons.append(REASON_DIMENSION)
            parts.append(f"{len(dim_failures)} measurement(s) out of tolerance: "
                         + ", ".join(f"{m.name}={m.status}" for m in dim_failures))

        hit = bool(reasons)
        self._streak = self._streak + 1 if hit else 0
        common = dict(defects=tuple(defects), reasons=tuple(reasons),
                      missing=missing, extra=extra,
                      dimension_failures=dim_failures)
        if hit and self._streak >= rules.consecutive_frames:
            return Verdict("NG", "; ".join(parts), **common)
        if hit:
            reason = (f"pending: {self._streak}/{rules.consecutive_frames} "
                      f"consecutive frames ({'; '.join(parts)})")
            return Verdict("OK", reason, **common)
        return Verdict("OK", f"no NG condition met "
                             f"({len(defects)} defect(s) < min_defects="
                             f"{rules.min_defects})", **common)

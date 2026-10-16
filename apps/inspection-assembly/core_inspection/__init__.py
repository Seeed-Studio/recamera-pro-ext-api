"""与硬件无关的装配缺件 + 尺寸检测核心层。"""

from .assembly import (
    AssemblyConfig, AssemblyResult, ExpectedItem, MissingItem, compare,
)
from .classes import DEEPPCB6_CLASSES, DEFAULT_CLASSES
from .detector import Detector, DetectorError
from .dimension import (
    STATUS_CODES, Calibration, DimensionConfig, DimensionError, DimensionResult,
    MeasurementResult, MeasurementSpec, estimate_mm_per_pixel, inspect, measure,
)
from .event import build_event
from .modbus_map import (
    ModbusFrame, encode_heartbeat, encode_millimetres, encode_normalized,
    encode_verdict,
)
from .rules import (
    REASON_CODES, REASON_DEFECT, REASON_DIMENSION, REASON_EXTRA, REASON_MISSING,
    Rules, Verdict, VerdictEngine, filter_defects,
)
from .types import Detection, FrameMeta, InferenceResult

__all__ = [
    "DEFAULT_CLASSES", "DEEPPCB6_CLASSES",
    "Detector", "DetectorError",
    "Detection", "FrameMeta", "InferenceResult",
    "Rules", "Verdict", "VerdictEngine", "filter_defects",
    "REASON_CODES", "REASON_DEFECT", "REASON_MISSING", "REASON_EXTRA",
    "REASON_DIMENSION",
    "AssemblyConfig", "AssemblyResult", "ExpectedItem", "MissingItem", "compare",
    "Calibration", "DimensionConfig", "DimensionError", "DimensionResult",
    "MeasurementSpec", "MeasurementResult", "STATUS_CODES",
    "estimate_mm_per_pixel", "measure", "inspect",
    "ModbusFrame", "encode_verdict", "encode_normalized", "encode_heartbeat",
    "encode_millimetres",
    "build_event",
]

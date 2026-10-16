"""把一次判定组装成 MQTT 事件 payload。形状见 contracts/mqtt-event.schema.json。

Demo B 在 v1 的基础上加 `assembly` 与 `dimension` 两段，版本号 bump 到 **2.0.0**：
`verdict` 的语义变了（不再只由缺陷框决定），消费方不能把它当 v1 兼容处理。
"""
from __future__ import annotations

import time

from .assembly import AssemblyResult
from .dimension import DimensionResult
from .rules import Verdict
from .types import InferenceResult

EVENT_TYPE = "assembly_inspection_result"
EVENT_VERSION = "2.0.0"
COORDINATE_SPACE = "normalized_center_wh"


def build_event(result: InferenceResult, verdict: Verdict, model: dict,
                device: str = "", pipeline_ms: float | None = None,
                timestamp_ms: int | None = None,
                assembly: AssemblyResult | None = None,
                dimension: DimensionResult | None = None) -> dict:
    """一帧一条消息，帧内框批量放进 detections[]，slot 是帧内有界下标。"""
    detections = list(result.detections)
    payload = {
        "type": EVENT_TYPE,
        "version": EVENT_VERSION,
        "stream_id": result.meta.stream_id,
        "frame_id": result.meta.frame_id,
        "timestamp": int(timestamp_ms if timestamp_ms is not None
                         else time.time() * 1000),
        "inference_time_ms": round(float(result.inference_time_ms), 3),
        "verdict": verdict.verdict,
        "verdict_reason": verdict.reason,
        "verdict_reasons": list(verdict.reasons),
        "defect_count": len(verdict.defects),
        "primary_class_id": (verdict.primary.class_id
                             if verdict.primary is not None else -1),
        "coordinate_space": COORDINATE_SPACE,
        "detections": [det.to_json(slot)
                       for slot, det in enumerate(detections)],
        "model": dict(model),
    }
    payload["assembly"] = (assembly or AssemblyResult()).to_json()
    payload["dimension"] = (dimension or DimensionResult()).to_json()
    if device:
        payload["device"] = device
    if pipeline_ms is not None:
        payload["pipeline_ms"] = round(float(pipeline_ms), 3)
    return payload

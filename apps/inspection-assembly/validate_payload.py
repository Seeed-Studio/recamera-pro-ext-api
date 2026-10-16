#!/usr/bin/env python3
"""无第三方依赖的 MQTT 事件 payload 校验器（v2：含 assembly / dimension 段）。

在设备上（没有 jsonschema）也能跑：
    python3 contracts/validate_payload.py payload.json
"""
from __future__ import annotations

import json
import re
import sys

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ACCELERATORS = {"cpu", "tensorrt", "hailo", "rknn"}
TOP_REQUIRED = (
    "timestamp", "frame_id", "stream_id", "inference_time_ms", "verdict",
    "defect_count", "detections", "coordinate_space", "model", "version",
    "assembly", "dimension",
)
DET_REQUIRED = ("slot", "class_id", "class_name", "score", "bbox")
REASON_CODES = {"defect", "missing", "extra", "dimension_out_of_tolerance"}
ASSEMBLY_REQUIRED = ("enabled", "expected_count", "matched_count",
                     "missing_count", "extra_count", "missing", "extra")
MISSING_REQUIRED = ("label", "class_name", "roi", "expected", "found")
EXTRA_REQUIRED = ("class_id", "class_name", "score", "bbox")
DIMENSION_REQUIRED = ("enabled", "calibrated", "mm_per_pixel",
                      "out_of_tolerance_count", "measurements")
MEASUREMENT_REQUIRED = ("name", "status", "status_code", "in_tolerance",
                        "measured_long_mm", "measured_short_mm")
EXPLANATION_TYPE = "assembly_explanation"
EXPLANATION_REQUIRED = ("type", "version", "stream_id", "frame_id", "timestamp",
                        "explanation", "vlm_model", "vlm_latency_ms",
                        "vlm_fallback_used", "candidates")
CANDIDATE_REQUIRED = ("label", "confidence")
STATUSES = {"ok": 0, "undersize": 1, "oversize": 2, "not_found": 3,
            "uncalibrated": 4}


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _unit_array(value, length: int) -> bool:
    return (isinstance(value, list) and len(value) == length
            and all(_is_num(v) and 0.0 <= v <= 1.0 for v in value))


def _validate_assembly(payload: dict, errors: list[str]) -> None:
    assembly = payload["assembly"]
    if not _err(errors, isinstance(assembly, dict), "assembly must be an object"):
        return
    for key in ASSEMBLY_REQUIRED:
        _err(errors, key in assembly, f"assembly missing required field: {key}")
    if not all(key in assembly for key in ASSEMBLY_REQUIRED):
        return
    _err(errors, isinstance(assembly["enabled"], bool),
         "assembly.enabled must be a boolean")
    for key in ("expected_count", "matched_count", "missing_count", "extra_count"):
        _err(errors, _is_int(assembly[key]) and assembly[key] >= 0,
             f"assembly.{key} must be a non-negative integer")
    for name, required in (("missing", MISSING_REQUIRED), ("extra", EXTRA_REQUIRED)):
        items = assembly[name]
        if not _err(errors, isinstance(items, list),
                    f"assembly.{name} must be an array"):
            continue
        _err(errors, _is_int(assembly[f"{name}_count"])
             and assembly[f"{name}_count"] == len(items),
             f"assembly.{name}_count must equal len(assembly.{name})")
        for index, item in enumerate(items):
            prefix = f"assembly.{name}[{index}]"
            if not isinstance(item, dict):
                errors.append(f"{prefix} must be an object")
                continue
            for key in required:
                _err(errors, key in item, f"{prefix} missing required field: {key}")
            if not all(key in item for key in required):
                continue
            if name == "missing":
                _err(errors, isinstance(item["label"], str) and item["label"],
                     f"{prefix}.label must be a non-empty string")
                _err(errors, _unit_array(item["roi"], 4),
                     f"{prefix}.roi must be 4 numbers in [0,1]")
                _err(errors, _is_int(item["expected"]) and item["expected"] >= 1,
                     f"{prefix}.expected must be an integer >= 1")
                _err(errors, _is_int(item["found"]) and item["found"] >= 0,
                     f"{prefix}.found must be a non-negative integer")
                _err(errors, not (_is_int(item["found"]) and _is_int(item["expected"]))
                     or item["found"] < item["expected"],
                     f"{prefix} is listed as missing but found >= expected")
            else:
                _err(errors, _is_int(item["class_id"]) and item["class_id"] >= 0,
                     f"{prefix}.class_id must be a non-negative integer")
                _err(errors, _is_num(item["score"]) and 0.0 <= item["score"] <= 1.0,
                     f"{prefix}.score must be in [0,1]")
                _err(errors, _unit_array(item["bbox"], 4),
                     f"{prefix}.bbox must be 4 numbers in [0,1]")
            _err(errors, isinstance(item["class_name"], str) and item["class_name"],
                 f"{prefix}.class_name must be a non-empty string")
    # additive（M-B1）：ROI 配置的版本指纹。手写 ROI 时缺省或写 null，
    # 由 tools/annotation/roi_profile.py 生成时是 64 位小写 hex。
    if "roi_profile_sha256" in assembly and assembly["roi_profile_sha256"] is not None:
        _err(errors, isinstance(assembly["roi_profile_sha256"], str)
             and bool(SHA256_RE.match(assembly["roi_profile_sha256"])),
             "assembly.roi_profile_sha256 must be 64 lowercase hex characters or null")


def _validate_dimension(payload: dict, errors: list[str]) -> None:
    dimension = payload["dimension"]
    if not _err(errors, isinstance(dimension, dict), "dimension must be an object"):
        return
    for key in DIMENSION_REQUIRED:
        _err(errors, key in dimension, f"dimension missing required field: {key}")
    if not all(key in dimension for key in DIMENSION_REQUIRED):
        return
    for key in ("enabled", "calibrated"):
        _err(errors, isinstance(dimension[key], bool),
             f"dimension.{key} must be a boolean")
    mpp = dimension["mm_per_pixel"]
    _err(errors, mpp is None or (_is_num(mpp) and mpp > 0),
         "dimension.mm_per_pixel must be a positive number or null")
    _err(errors, (mpp is not None) == bool(dimension.get("calibrated")),
         "dimension.calibrated must agree with mm_per_pixel being non-null")
    _err(errors, _is_int(dimension["out_of_tolerance_count"])
         and dimension["out_of_tolerance_count"] >= 0,
         "dimension.out_of_tolerance_count must be a non-negative integer")
    measurements = dimension["measurements"]
    if not _err(errors, isinstance(measurements, list),
                "dimension.measurements must be an array"):
        return
    failures = 0
    for index, item in enumerate(measurements):
        prefix = f"dimension.measurements[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix} must be an object")
            continue
        for key in MEASUREMENT_REQUIRED:
            _err(errors, key in item, f"{prefix} missing required field: {key}")
        if not all(key in item for key in MEASUREMENT_REQUIRED):
            continue
        _err(errors, isinstance(item["name"], str) and item["name"],
             f"{prefix}.name must be a non-empty string")
        status = item["status"]
        if _err(errors, status in STATUSES,
                f"{prefix}.status must be one of {sorted(STATUSES)}"):
            _err(errors, item["status_code"] == STATUSES[status],
                 f"{prefix}.status_code must be {STATUSES[status]} for "
                 f"status={status}")
            _err(errors, isinstance(item["in_tolerance"], bool)
                 and item["in_tolerance"] == (status == "ok"),
                 f"{prefix}.in_tolerance must agree with status")
            if status != "ok":
                failures += 1
        for key in ("measured_long_mm", "measured_short_mm"):
            value = item[key]
            _err(errors, value is None or (_is_num(value) and value >= 0),
                 f"{prefix}.{key} must be a non-negative number or null")
    _err(errors, dimension["out_of_tolerance_count"] == failures,
         "dimension.out_of_tolerance_count must equal the number of "
         "measurements whose status is not ok")


def _validate_vlm_fields(payload: dict, errors: list[str],
                         prefix: str = "") -> None:
    """五个 VLM 字段的共用校验：主事件里是可选的，解释事件里是必填的。

    契约见 edge-vision-vlm/docs/INTEGRATION.md §1。字段一律可缺席——VLM 不可用
    时消费方看到的就是「没有这几个键」，而不是空串或 -1。
    """
    if "explanation" in payload:
        _err(errors, isinstance(payload["explanation"], str),
             f"{prefix}explanation must be a string")
    if "vlm_model" in payload:
        _err(errors, isinstance(payload["vlm_model"], str)
             and bool(payload["vlm_model"]),
             f"{prefix}vlm_model must be a non-empty string")
    if "vlm_latency_ms" in payload:
        _err(errors, _is_num(payload["vlm_latency_ms"])
             and payload["vlm_latency_ms"] >= 0,
             f"{prefix}vlm_latency_ms must be a non-negative number")
    if "vlm_fallback_used" in payload:
        _err(errors, isinstance(payload["vlm_fallback_used"], bool),
             f"{prefix}vlm_fallback_used must be a boolean")
    if "prompt_sha256" in payload:
        _err(errors, isinstance(payload["prompt_sha256"], str)
             and bool(SHA256_RE.match(payload["prompt_sha256"])),
             f"{prefix}prompt_sha256 must be 64 lowercase hex characters")
    if "candidates" not in payload:
        return
    candidates = payload["candidates"]
    if not _err(errors, isinstance(candidates, list),
                f"{prefix}candidates must be an array"):
        return
    for index, entry in enumerate(candidates):
        where = f"{prefix}candidates[{index}]"
        if not _err(errors, isinstance(entry, dict), f"{where} must be an object"):
            continue
        for key in CANDIDATE_REQUIRED:
            _err(errors, key in entry, f"{where} missing required field: {key}")
        if "label" in entry:
            _err(errors, isinstance(entry["label"], str) and entry["label"],
                 f"{where}.label must be a non-empty string")
        if "confidence" in entry:
            _err(errors, _is_num(entry["confidence"])
                 and 0.0 <= entry["confidence"] <= 1.0,
                 f"{where}.confidence must be in [0,1]")


def validate_explanation(payload: object) -> list[str]:
    """校验 assembly_explanation 事件（inspection/<stream>/explanations）。

    它是主事件之外的一条旁路消息，用 frame_id 与主事件关联；主事件的形状与
    时序不因为 VLM 而改变，见 core_inspection/vlm_client.py 的模块注释。
    """
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["payload must be a JSON object"]
    for key in EXPLANATION_REQUIRED:
        _err(errors, key in payload, f"missing required field: {key}")
    if errors:
        return errors
    _err(errors, payload["type"] == EXPLANATION_TYPE,
         f"type must be {EXPLANATION_TYPE}")
    _err(errors, isinstance(payload["version"], str) and payload["version"],
         "version must be a non-empty string")
    _err(errors, isinstance(payload["stream_id"], str) and payload["stream_id"],
         "stream_id must be a non-empty string")
    _err(errors, _is_int(payload["frame_id"]) and payload["frame_id"] >= 0,
         "frame_id must be a non-negative integer")
    _err(errors, _is_int(payload["timestamp"]) and payload["timestamp"] >= 0,
         "timestamp must be a non-negative integer (Unix epoch milliseconds)")
    _err(errors, isinstance(payload["explanation"], str)
         and bool(payload["explanation"].strip()),
         "explanation must be a non-empty string")
    if "verdict" in payload:
        _err(errors, payload["verdict"] in ("OK", "NG"),
             "verdict must be OK or NG")
    if "verdict_reasons" in payload:
        reasons = payload["verdict_reasons"]
        if _err(errors, isinstance(reasons, list),
                "verdict_reasons must be an array"):
            for reason in reasons:
                _err(errors, reason in REASON_CODES,
                     f"verdict_reasons entry {reason!r} is not a known reason code")
    _validate_vlm_fields(payload, errors)
    return errors


def _err(errors: list[str], cond: bool, msg: str) -> bool:
    if not cond:
        errors.append(msg)
    return cond


def validate_event(payload: object) -> list[str]:
    """返回错误列表，空列表表示通过。"""
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["payload must be a JSON object"]

    for key in TOP_REQUIRED:
        _err(errors, key in payload, f"missing required field: {key}")
    if errors:
        return errors

    _err(errors, isinstance(payload["timestamp"], int)
         and not isinstance(payload["timestamp"], bool)
         and payload["timestamp"] >= 0,
         "timestamp must be a non-negative integer (Unix epoch milliseconds)")
    _err(errors, isinstance(payload["frame_id"], int)
         and not isinstance(payload["frame_id"], bool)
         and payload["frame_id"] >= 0, "frame_id must be a non-negative integer")
    _err(errors, isinstance(payload["stream_id"], str) and payload["stream_id"],
         "stream_id must be a non-empty string")
    _err(errors, isinstance(payload["inference_time_ms"], (int, float))
         and not isinstance(payload["inference_time_ms"], bool)
         and payload["inference_time_ms"] >= 0,
         "inference_time_ms must be a non-negative number")
    _err(errors, payload["verdict"] in ("OK", "NG"), "verdict must be OK or NG")
    _err(errors, payload["coordinate_space"] == "normalized_center_wh",
         "coordinate_space must be normalized_center_wh")
    _err(errors, isinstance(payload["version"], str) and payload["version"],
         "version must be a non-empty string")

    detections = payload["detections"]
    if not _err(errors, isinstance(detections, list), "detections must be an array"):
        return errors
    _err(errors, isinstance(payload["defect_count"], int)
         and not isinstance(payload["defect_count"], bool)
         and payload["defect_count"] >= 0,
         "defect_count must be a non-negative integer")

    if "primary_class_id" in payload:
        _err(errors, isinstance(payload["primary_class_id"], int)
             and not isinstance(payload["primary_class_id"], bool)
             and payload["primary_class_id"] >= -1,
             "primary_class_id must be an integer >= -1")

    seen_slots = set()
    for index, det in enumerate(detections):
        prefix = f"detections[{index}]"
        if not isinstance(det, dict):
            errors.append(f"{prefix} must be an object")
            continue
        for key in DET_REQUIRED:
            _err(errors, key in det, f"{prefix} missing required field: {key}")
        if not all(key in det for key in DET_REQUIRED):
            continue
        slot = det["slot"]
        if _err(errors, isinstance(slot, int) and not isinstance(slot, bool) and slot >= 0,
                f"{prefix}.slot must be a non-negative integer"):
            _err(errors, slot not in seen_slots, f"{prefix}.slot duplicated: {slot}")
            seen_slots.add(slot)
        _err(errors, isinstance(det["class_id"], int)
             and not isinstance(det["class_id"], bool) and det["class_id"] >= 0,
             f"{prefix}.class_id must be a non-negative integer")
        _err(errors, isinstance(det["class_name"], str) and det["class_name"],
             f"{prefix}.class_name must be a non-empty string")
        _err(errors, isinstance(det["score"], (int, float))
             and not isinstance(det["score"], bool) and 0.0 <= det["score"] <= 1.0,
             f"{prefix}.score must be in [0,1]")
        bbox = det["bbox"]
        if _err(errors, isinstance(bbox, list) and len(bbox) == 4,
                f"{prefix}.bbox must be an array of 4 numbers"):
            for axis, value in zip("xywh", bbox):
                _err(errors, isinstance(value, (int, float))
                     and not isinstance(value, bool) and 0.0 <= value <= 1.0,
                     f"{prefix}.bbox {axis} must be normalized into [0,1], got {value!r}")

    if "verdict_reasons" in payload:
        reasons = payload["verdict_reasons"]
        if _err(errors, isinstance(reasons, list),
                "verdict_reasons must be an array"):
            for reason in reasons:
                _err(errors, reason in REASON_CODES,
                     f"verdict_reasons entry {reason!r} is not a known reason code")
            # 只约束一个方向：NG 必须给出原因。反过来不成立——
            # consecutive_frames 消抖期间 verdict 仍是 OK，但依据已经命中。
            _err(errors, payload["verdict"] != "NG" or bool(reasons),
                 "verdict_reasons must be non-empty when verdict is NG")

    # VLM 字段在主事件里是可选的（additive，schema v2 不 bump）。运行时默认
    # 走旁路事件，这里的校验是为了兼容把它们直接内联进主事件的消费方。
    _validate_vlm_fields(payload, errors)

    _validate_assembly(payload, errors)
    _validate_dimension(payload, errors)

    model = payload["model"]
    if _err(errors, isinstance(model, dict), "model must be an object"):
        for key in ("name", "input", "onnx_sha256", "accelerator"):
            _err(errors, key in model, f"model missing required field: {key}")
        if "onnx_sha256" in model:
            _err(errors, isinstance(model["onnx_sha256"], str)
                 and bool(SHA256_RE.match(model["onnx_sha256"])),
                 "model.onnx_sha256 must be 64 lowercase hex characters")
        if "artifact_sha256" in model:
            _err(errors, isinstance(model["artifact_sha256"], str)
                 and bool(SHA256_RE.match(model["artifact_sha256"])),
                 "model.artifact_sha256 must be 64 lowercase hex characters")
        if "accelerator" in model:
            _err(errors, model["accelerator"] in ACCELERATORS,
                 f"model.accelerator must be one of {sorted(ACCELERATORS)}")
    return errors


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Usage: validate_payload.py PAYLOAD.json", file=sys.stderr)
        return 2
    with open(argv[1], encoding="utf-8") as handle:
        payload = json.load(handle)
    explanation = (isinstance(payload, dict)
                   and payload.get("type") == EXPLANATION_TYPE)
    errors = (validate_explanation(payload) if explanation
              else validate_event(payload))
    if errors:
        for error in errors:
            print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print(f"OK: {argv[1]} conforms to "
          + ("explanation-event v1" if explanation else "mqtt-event v2"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

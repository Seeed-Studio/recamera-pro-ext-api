#!/usr/bin/env python3
"""无第三方依赖的 MQTT 事件 payload 校验器（waste-sorting v1）。

两条流：`validate_event` 校验主事件（`mqtt-event.schema.json`），
`validate_fallback` 校验 VLM 兜底的旁路事件（`fallback-event.schema.json`）。

在设备上（没有 jsonschema）也能跑：
    python3 contracts/validate_payload.py payload.json

schema 表达不了、但这里要查的跨字段约束：
- `top3` 必须按 confidence 降序，rank 从 0 连续递增，class_id 不重复；
- `category` 必须等于 `top3[0]`；
- `confidence` 必须等于 `top3[0].confidence`；
- 多件同框（1.1.0）：`bbox` 归一化且 x1<x2 / y1<y2；`item_index` 与
  `item_count` 成对出现（只有一个的话消费端既做不了整帧汇总也定不了位）；
  `fallback` 出现时 `bbox` 必须显式为 null、`item_count` 必须**显式**为 0、
  `item_index` 为 0、且不得带 `detection_confidence` / `track_id`；
  非回退的逐件事件 `item_index < item_count` 且必须有框；`event_id` 出现时
  必须以 `:<frame_id>:<item_index>` 结尾，否则消费端按它去重会串帧；
- 开放词汇 track 的 additive 字段（`prompt_set_sha256` / `open_vocab_top3` /
  `distilled` / `vlm_fallback_used` / `model.track`）出现时类型与取值要对，
  且 `prompt_set_sha256` 与 `open_vocab_top3` 只有 `model.track == "open_vocab"`
  才允许出现——基线事件里冒出 prompt 指纹说明发布方把两条 track 的字段串了。
"""
from __future__ import annotations

import json
import re
import sys

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ACCELERATORS = {"cpu", "tensorrt", "hailo", "rknn", "cvi"}
TRIGGERS = {"button", "http", "motion", "continuous", "file"}
CHINA_CATEGORIES = {"recyclable", "kitchen", "hazardous", "residual"}
IMAGE_REF_KINDS = {"none", "local", "object_store"}
TRACKS = {"baseline", "open_vocab"}
#: 只有开放词汇 track 才允许携带的 additive 字段
OPEN_VOCAB_ONLY = ("prompt_set_sha256", "open_vocab_top3")
TOP_REQUIRED = (
    "timestamp", "stream_id", "frame_id", "trigger", "inference_time_ms",
    "category", "confidence", "top3", "image_ref", "model", "version",
)
RANKED_REQUIRED = ("rank", "class_id", "class_name", "confidence",
                   "china_category")


def _err(errors: list[str], cond: bool, msg: str) -> bool:
    if not cond:
        errors.append(msg)
    return cond


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_num(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_ranked(entry: object, prefix: str, errors: list[str]) -> bool:
    if not isinstance(entry, dict):
        errors.append(f"{prefix} must be an object")
        return False
    complete = True
    for key in RANKED_REQUIRED:
        complete &= _err(errors, key in entry,
                         f"{prefix} missing required field: {key}")
    if not complete:
        return False
    _err(errors, _is_int(entry["rank"]) and 0 <= entry["rank"] <= 2,
         f"{prefix}.rank must be an integer in [0,2]")
    _err(errors, _is_int(entry["class_id"]) and entry["class_id"] >= 0,
         f"{prefix}.class_id must be a non-negative integer")
    _err(errors, isinstance(entry["class_name"], str) and entry["class_name"],
         f"{prefix}.class_name must be a non-empty string")
    _err(errors, _is_num(entry["confidence"]) and 0.0 <= entry["confidence"] <= 1.0,
         f"{prefix}.confidence must be in [0,1]")
    _err(errors, entry["china_category"] in CHINA_CATEGORIES,
         f"{prefix}.china_category must be one of {sorted(CHINA_CATEGORIES)}")
    return True


def _validate_ranked_list(entries: object, name: str, errors: list[str],
                          unique_ids: bool = True) -> bool:
    """三处排名数组（`top3` / `open_vocab_top3` / `primary_top3`）同一套规则：
    1..3 项、逐项形状正确、confidence 降序、rank 从 0 连续、class_id 不重复。

    只有 `open_vocab_top3` 不查 class_id 唯一性——它的 class_id 来自另一套
    词表，重复与否由那条 track 自己定义。
    """
    if not _err(errors, isinstance(entries, list) and 1 <= len(entries) <= 3,
                f"{name} must be an array of 1..3 entries"):
        return False
    if not all(_validate_ranked(entry, f"{name}[{index}]", errors)
               for index, entry in enumerate(entries)):
        return False
    confidences = [e["confidence"] for e in entries]
    _err(errors, all(a >= b for a, b in zip(confidences, confidences[1:])),
         f"{name} must be sorted by descending confidence")
    _err(errors, [e["rank"] for e in entries] == list(range(len(entries))),
         f"{name} ranks must be 0,1,2 in order")
    if unique_ids:
        ids = [e["class_id"] for e in entries]
        _err(errors, len(set(ids)) == len(ids),
             f"{name} class_id duplicated: {ids}")
    return True


def validate_event(payload: object) -> list[str]:
    """返回错误列表，空列表表示通过。"""
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["payload must be a JSON object"]

    for key in TOP_REQUIRED:
        _err(errors, key in payload, f"missing required field: {key}")
    if errors:
        return errors

    _err(errors, _is_int(payload["timestamp"]) and payload["timestamp"] >= 0,
         "timestamp must be a non-negative integer (Unix epoch milliseconds)")
    _err(errors, _is_int(payload["frame_id"]) and payload["frame_id"] >= 0,
         "frame_id must be a non-negative integer")
    _err(errors, isinstance(payload["stream_id"], str) and payload["stream_id"],
         "stream_id must be a non-empty string")
    _err(errors, payload["trigger"] in TRIGGERS,
         f"trigger must be one of {sorted(TRIGGERS)}")
    _err(errors, _is_num(payload["inference_time_ms"])
         and payload["inference_time_ms"] >= 0,
         "inference_time_ms must be a non-negative number")
    _err(errors, isinstance(payload["version"], str) and payload["version"],
         "version must be a non-empty string")
    _err(errors, _is_num(payload["confidence"])
         and 0.0 <= payload["confidence"] <= 1.0,
         "confidence must be in [0,1]")

    top3 = payload["top3"]
    ranked_ok = _validate_ranked_list(top3, "top3", errors)

    category = payload["category"]
    if _err(errors, isinstance(category, dict), "category must be an object"):
        for key in ("class_id", "class_name", "china_category"):
            _err(errors, key in category, f"category missing required field: {key}")
        if "china_category" in category:
            _err(errors, category["china_category"] in CHINA_CATEGORIES,
                 f"category.china_category must be one of "
                 f"{sorted(CHINA_CATEGORIES)}")
        if ranked_ok and all(k in category for k in
                             ("class_id", "class_name", "china_category")):
            best = top3[0]
            _err(errors,
                 (category["class_id"], category["class_name"],
                  category["china_category"])
                 == (best["class_id"], best["class_name"], best["china_category"]),
                 "category must equal top3[0]")
            # 两边都先确认是数字：类型错误已经在上面记过一条，这里再 float()
            # 会把「返回错误列表」变成「抛 TypeError」，发布路径上就成了
            # 未捕获异常而不是 PayloadRejected。
            if _is_num(payload["confidence"]) and _is_num(best["confidence"]):
                _err(errors, abs(float(payload["confidence"])
                                 - float(best["confidence"])) <= 1e-6,
                     "confidence must equal top3[0].confidence")

    image_ref = payload["image_ref"]
    if _err(errors, isinstance(image_ref, dict), "image_ref must be an object"):
        if _err(errors, "kind" in image_ref, "image_ref missing required field: kind"):
            _err(errors, image_ref["kind"] in IMAGE_REF_KINDS,
                 f"image_ref.kind must be one of {sorted(IMAGE_REF_KINDS)}")
            if image_ref["kind"] != "none":
                _err(errors, bool(image_ref.get("uri")),
                     "image_ref.uri is required unless kind is 'none'")
        if "sha256" in image_ref:
            _err(errors, isinstance(image_ref["sha256"], str)
                 and bool(SHA256_RE.match(image_ref["sha256"])),
                 "image_ref.sha256 must be 64 lowercase hex characters")

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
        if "track" in model:
            _err(errors, model["track"] in TRACKS,
                 f"model.track must be one of {sorted(TRACKS)}")

    errors.extend(_validate_open_vocab(payload))
    errors.extend(_validate_multi_item(payload))
    return errors


FALLBACKS = {"no_detection"}


def _validate_multi_item(payload: dict) -> list[str]:
    """多件同框的 additive 字段（事件契约 1.1.0）。全部可选。"""
    errors: list[str] = []
    has_fallback = "fallback" in payload
    has_index = "item_index" in payload
    has_count = "item_count" in payload

    if has_index:
        _err(errors, _is_int(payload["item_index"])
             and payload["item_index"] >= 0,
             "item_index must be a non-negative integer")
    if has_count:
        _err(errors, _is_int(payload["item_count"])
             and payload["item_count"] >= 0,
             "item_count must be a non-negative integer")

    # item_index 与 item_count 是一对，不允许只出现一个：只有 index 的话
    # 消费端不知道这一帧还有没有别的件（等不到齐就没法做整帧汇总），
    # 只有 count 的话不知道这是第几件。1.0.0 的事件两个都没有，仍然合法。
    _err(errors, has_index == has_count,
         "item_index and item_count must appear together (or neither)")

    if has_fallback:
        _err(errors, payload["fallback"] in FALLBACKS,
             f"fallback must be one of {sorted(FALLBACKS)}")
        _err(errors, "bbox" in payload and payload["bbox"] is None,
             "a fallback event must carry bbox: null (explicitly, not omitted)")
        # 回退事件必须**显式**报 item_count 0：缺字段时按 0 兜底会让
        # "这一帧没检出" 和 "发布方还没升级到 1.1.0" 混成一种。
        _err(errors, has_count and payload.get("item_count") == 0,
             "a fallback event must report item_count 0 explicitly - "
             "it is not an item")
        _err(errors, not has_index or payload.get("item_index") == 0,
             "a fallback event must report item_index 0")
        for key in ("detection_confidence", "track_id"):
            _err(errors, key not in payload,
                 f"a fallback event must not carry {key}: there is no box")
    elif has_index and has_count and _is_int(payload["item_index"]) \
            and _is_int(payload["item_count"]):
        _err(errors, payload["item_index"] < payload["item_count"],
             f"item_index {payload['item_index']} must be < item_count "
             f"{payload['item_count']}")
        # 非回退的逐件事件必须有框。没框又不是回退，说明发布方漏了 bbox，
        # 下游画不出来也算不了 IoU。
        _err(errors, payload.get("bbox") is not None,
             "a per-item event must carry a bbox (only a fallback may be null)")

    bbox = payload.get("bbox", "__absent__")
    if bbox != "__absent__" and bbox is not None:
        if _err(errors, isinstance(bbox, list) and len(bbox) == 4,
                "bbox must be an array of 4 numbers or null"):
            if all(_is_num(v) for v in bbox):
                x1, y1, x2, y2 = (float(v) for v in bbox)
                _err(errors, all(0.0 <= v <= 1.0 for v in bbox),
                     f"bbox must be normalized into [0,1]: {bbox}")
                _err(errors, x1 < x2 and y1 < y2,
                     f"bbox must satisfy x1<x2 and y1<y2: {bbox}")
            else:
                errors.append("bbox entries must be numbers")

    if "detection_confidence" in payload:
        _err(errors, _is_num(payload["detection_confidence"])
             and 0.0 <= payload["detection_confidence"] <= 1.0,
             "detection_confidence must be in [0,1]")
    if "track_id" in payload:
        _err(errors, isinstance(payload["track_id"], str)
             and bool(payload["track_id"]),
             "track_id must be a non-empty string")
    if "event_id" in payload:
        event_id = payload["event_id"]
        if _err(errors, isinstance(event_id, str) and bool(event_id),
                "event_id must be a non-empty string"):
            suffix = f":{payload.get('frame_id')}:{payload.get('item_index', 0)}"
            _err(errors, event_id.endswith(suffix),
                 f"event_id must end with {suffix!r} so consumers can "
                 f"deduplicate per frame and item; got {event_id!r}")
    return errors


def _validate_open_vocab(payload: dict) -> list[str]:
    """开放词汇 track 的 additive 字段。全部可选；出现了就必须站得住。"""
    errors: list[str] = []
    model = payload.get("model")
    track = model.get("track") if isinstance(model, dict) else None

    if "prompt_set_sha256" in payload:
        _err(errors, isinstance(payload["prompt_set_sha256"], str)
             and bool(SHA256_RE.match(payload["prompt_set_sha256"])),
             "prompt_set_sha256 must be 64 lowercase hex characters")
    if "taxonomy_version" in payload:
        _err(errors, isinstance(payload["taxonomy_version"], str)
             and bool(payload["taxonomy_version"]),
             "taxonomy_version must be a non-empty string")
    for key in ("distilled", "vlm_fallback_used"):
        if key in payload:
            _err(errors, isinstance(payload[key], bool),
                 f"{key} must be a boolean")

    if "open_vocab_top3" in payload:
        _validate_ranked_list(payload["open_vocab_top3"], "open_vocab_top3",
                              errors, unique_ids=False)

    for key in OPEN_VOCAB_ONLY:
        if key in payload:
            _err(errors, track == "open_vocab",
                 f"{key} is only allowed when model.track == 'open_vocab' "
                 f"(got {track!r})")
    if "distilled" in payload and payload.get("distilled") is True:
        _err(errors, track == "open_vocab",
             "distilled=true requires model.track == 'open_vocab' — the student "
             f"belongs to the open-vocabulary track (got {track!r})")
    return errors


FALLBACK_TRIGGERS = {"low_confidence", "ambiguous"}
FALLBACK_REQUIRED = (
    "type", "version", "taxonomy_version", "stream_id", "frame_id", "timestamp",
    "trigger", "category", "confidence", "vlm_fallback_used", "vlm_model",
    "vlm_latency_ms", "primary_confidence", "primary_top3",
)


def validate_fallback(payload: object) -> list[str]:
    """校验 `waste_fallback` 旁路事件（contracts/fallback-event.schema.json）。

    与主事件分开一个函数而不是塞进 `validate_event`：两条流的 required 集合不同，
    合在一起就得靠 `type` 分支，发布路径上更容易把一条发到另一条的 topic 上。

    schema 表达不了、这里要查的跨字段约束：
    - `primary_top3` 与主事件的 `top3` 同规则（降序、rank 连续、class_id 不重复）；
    - `primary_confidence` 必须等于 `primary_top3[0].confidence`——它是触发门限
      读到的那个值，对不上就说明发布方拼错了帧。
    """
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["payload must be a JSON object"]

    for key in FALLBACK_REQUIRED:
        _err(errors, key in payload, f"missing required field: {key}")
    if errors:
        return errors

    _err(errors, payload["type"] == "waste_fallback",
         "type must be 'waste_fallback'")
    _err(errors, isinstance(payload["version"], str) and payload["version"],
         "version must be a non-empty string")
    _err(errors, isinstance(payload["taxonomy_version"], str)
         and payload["taxonomy_version"],
         "taxonomy_version must be a non-empty string")
    _err(errors, isinstance(payload["stream_id"], str) and payload["stream_id"],
         "stream_id must be a non-empty string")
    _err(errors, _is_int(payload["frame_id"]) and payload["frame_id"] >= 0,
         "frame_id must be a non-negative integer")
    _err(errors, _is_int(payload["timestamp"]) and payload["timestamp"] >= 0,
         "timestamp must be a non-negative integer (Unix epoch milliseconds)")
    _err(errors, payload["trigger"] in FALLBACK_TRIGGERS,
         f"trigger must be one of {sorted(FALLBACK_TRIGGERS)}")
    _err(errors, payload["vlm_fallback_used"] is True,
         "vlm_fallback_used must be true on a waste_fallback event")
    _err(errors, isinstance(payload["vlm_model"], str) and payload["vlm_model"],
         "vlm_model must be a non-empty string")
    _err(errors, _is_num(payload["vlm_latency_ms"])
         and payload["vlm_latency_ms"] >= 0,
         "vlm_latency_ms must be a non-negative number")
    for key in ("confidence", "primary_confidence"):
        _err(errors, _is_num(payload[key]) and 0.0 <= payload[key] <= 1.0,
             f"{key} must be in [0,1]")
    for key in ("rationale", "explanation", "device"):
        if key in payload:
            _err(errors, isinstance(payload[key], str),
                 f"{key} must be a string")
    if "prompt_sha256" in payload:
        _err(errors, isinstance(payload["prompt_sha256"], str)
             and bool(SHA256_RE.match(payload["prompt_sha256"])),
             "prompt_sha256 must be 64 lowercase hex characters")

    category = payload["category"]
    if _err(errors, isinstance(category, dict), "category must be an object"):
        for key in ("class_id", "class_name", "china_category"):
            _err(errors, key in category,
                 f"category missing required field: {key}")
        if "china_category" in category:
            _err(errors, category["china_category"] in CHINA_CATEGORIES,
                 f"category.china_category must be one of "
                 f"{sorted(CHINA_CATEGORIES)}")
        if "class_id" in category:
            _err(errors, _is_int(category["class_id"])
                 and category["class_id"] >= 0,
                 "category.class_id must be a non-negative integer")
        if "class_name" in category:
            _err(errors, isinstance(category["class_name"], str)
                 and category["class_name"],
                 "category.class_name must be a non-empty string")

    ranked = payload["primary_top3"]
    if (_validate_ranked_list(ranked, "primary_top3", errors)
            and _is_num(payload["primary_confidence"])):
        _err(errors, abs(float(payload["primary_confidence"])
                         - float(ranked[0]["confidence"])) <= 1e-6,
             "primary_confidence must equal primary_top3[0].confidence")
    return errors


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Usage: validate_payload.py PAYLOAD.json", file=sys.stderr)
        return 2
    with open(argv[1], encoding="utf-8") as handle:
        payload = json.load(handle)
    # 一个文件里可能是主事件，也可能是旁路事件；按 type 分流。
    is_fallback = isinstance(payload, dict) and payload.get("type") == "waste_fallback"
    errors = validate_fallback(payload) if is_fallback else validate_event(payload)
    if errors:
        for error in errors:
            print(f"FAIL: {error}", file=sys.stderr)
        return 1
    kind = "fallback-event v1" if is_fallback else "mqtt-event v1"
    print(f"OK: {argv[1]} conforms to waste-sorting {kind}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

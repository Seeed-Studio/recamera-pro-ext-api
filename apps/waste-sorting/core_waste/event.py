"""把一次分类组装成 MQTT 事件 payload。形状见 contracts/mqtt-event.schema.json。

字段清单来自 spec §5：timestamp、device / trigger、category、confidence、
top3、image_ref、model / hash。图片只给引用，payload 里不放字节。
"""
from __future__ import annotations

import time

from .taxonomy import china_category_zh
from .types import ImageRef, Prediction

EVENT_TYPE = "waste_sorting_result"
#: 1.1.0：多件同框。新增 item_index / item_count / bbox /
#: detection_confidence / track_id / event_id / fallback，全部 additive——
#: 1.0.0 的消费方读到的字段一个都没变，单件帧的字节序列也和以前一致
#: （单件时 item_index=0、item_count=1，其余字段只有检测流水线才会填）。
EVENT_VERSION = "1.1.0"
TAXONOMY_VERSION = "material8/china4-v1"

#: 无框整帧回退时 `fallback` 的取值。约定只有这一个值，消费方按它排除计件。
FALLBACK_NO_DETECTION = "no_detection"


def ranked_json(entries, top_n: int = 3) -> list[dict]:
    """把 taxonomy.top_k 的排名转成事件里的数组形状。

    主事件的 `top3` / `open_vocab_top3` 与旁路事件的 `primary_top3` 是同一个
    形状（contracts 里也是同一套校验），所以只留这一份实现。
    """
    return [
        {
            "rank": int(entry["rank"]),
            "class_id": int(entry["class_id"]),
            "class_name": str(entry["class_name"]),
            "confidence": round(float(entry["confidence"]), 6),
            "china_category": str(entry["china_category"]),
        }
        for entry in list(entries)[:top_n]
    ]


def build_event(prediction: Prediction, model: dict, device: str = "",
                image_ref: ImageRef | None = None,
                pipeline_ms: float | None = None,
                timestamp_ms: int | None = None,
                top_n: int = 3,
                prompt_set_sha256: str | None = None,
                open_vocab_ranked=None,
                distilled: bool | None = None,
                vlm_fallback_used: bool | None = None,
                item_index: int | None = None,
                item_count: int | None = None,
                bbox=None,
                detection_confidence: float | None = None,
                track_id: str | None = None,
                event_id: str | None = None,
                fallback: str | None = None) -> dict:
    """一次分类一条消息。top3 是 taxonomy.top_k 的前 N 项，默认 3。

    后四个参数是先进 track 的 additive 字段（`docs/SPEC-advanced.md` §M-W1）。
    全部默认 None，**不给就不进 payload**——基线事件的字节序列一个字符都不变，
    已经上线的消费方不受影响。`open_vocab_ranked` 与 `top3` 分开报：
    蒸馏出来的学生、或者 VLM 兜底改判之后，教师原本的排名还留在事件里，
    对比表才有的可比。
    """
    ranked = list(prediction.ranked)[:top_n]
    if not ranked:
        raise ValueError("prediction.ranked is empty")
    best = ranked[0]
    payload = {
        "type": EVENT_TYPE,
        "version": EVENT_VERSION,
        "taxonomy_version": TAXONOMY_VERSION,
        "stream_id": prediction.meta.stream_id,
        "frame_id": prediction.meta.frame_id,
        "timestamp": int(timestamp_ms if timestamp_ms is not None
                         else time.time() * 1000),
        "trigger": prediction.trigger,
        "inference_time_ms": round(float(prediction.inference_time_ms), 3),
        "category": {
            "class_id": int(best["class_id"]),
            "class_name": str(best["class_name"]),
            "china_category": str(best["china_category"]),
            "china_category_zh": china_category_zh(str(best["class_name"])),
        },
        "confidence": round(float(best["confidence"]), 6),
        "top3": ranked_json(ranked, top_n),
        "image_ref": (image_ref or ImageRef()).to_json(),
        "model": dict(model),
    }
    if device:
        payload["device"] = device
    if pipeline_ms is not None:
        payload["pipeline_ms"] = round(float(pipeline_ms), 3)
    if prompt_set_sha256 is not None:
        payload["prompt_set_sha256"] = str(prompt_set_sha256)
    if open_vocab_ranked is not None:
        entries = ranked_json(open_vocab_ranked, top_n)
        if not entries:
            raise ValueError("open_vocab_ranked is empty")
        payload["open_vocab_top3"] = entries
    if distilled is not None:
        payload["distilled"] = bool(distilled)
    if vlm_fallback_used is not None:
        payload["vlm_fallback_used"] = bool(vlm_fallback_used)
    # ---- 多件同框（事件契约 1.1.0）------------------------------------
    # `item_index` 一给，这条就是"某一帧里的第几件"。同帧的多条共享
    # frame_id，靠 item_index 区分；消费端按 event_id 幂等。
    if item_index is not None:
        payload["item_index"] = int(item_index)
    if item_count is not None:
        payload["item_count"] = int(item_count)
    if bbox is not None:
        values = [round(float(v), 6) for v in bbox]
        if len(values) != 4:
            raise ValueError(f"bbox must have 4 values, got {len(values)}")
        x1, y1, x2, y2 = values
        if not (0.0 <= x1 < x2 <= 1.0 and 0.0 <= y1 < y2 <= 1.0):
            raise ValueError(f"bbox must be normalized xyxy with x1<x2, "
                             f"y1<y2 inside [0,1]: {values}")
        payload["bbox"] = values
    if detection_confidence is not None:
        payload["detection_confidence"] = round(float(detection_confidence), 6)
    if track_id is not None:
        payload["track_id"] = str(track_id)
    if event_id is not None:
        payload["event_id"] = str(event_id)
    if fallback is not None:
        # 回退事件没有框：既然整帧都进了分类器，报一个假框会让下游的计件
        # 和可视化都错。约定 bbox 明确为 null，而不是省略。
        payload["fallback"] = str(fallback)
        payload["bbox"] = None
    return payload


def make_event_id(session_id: str, stream_id: str, frame_id: int,
                  item_index: int) -> str:
    """幂等键。含启动会话 id，设备重启后 frame_id 归零也不会撞上旧事件。"""
    return f"{session_id}:{stream_id}:{int(frame_id)}:{int(item_index)}"

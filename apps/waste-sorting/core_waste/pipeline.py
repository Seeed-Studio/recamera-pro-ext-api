"""多件同框流水线：检测 → NMS → 外扩方形裁剪 → 现有分类器 → 每件一条事件。

spec §3。分类那一段一个字都没改——`Classifier` 协议、预处理、taxonomy 的
top-k 全部沿用，检测器只是把「整帧」换成了「若干个裁剪块」。所以已验收的
单件准确率是这条流水线的上界，回归时只要盯住「裁剪之后掉了多少」。

回退（`fallback=no_detection`）：一件都没检出时，把整帧喂给分类器，发一条
`bbox=null` 的事件。理由是投放口的场景里"检测器漏了"和"确实没东西"分不开，
静默丢掉会让下游以为设备挂了；但这条**不计件数**，`item_count=0`。

跨帧去重在 `core_waste.tracking`：连续模式下同一件只在首次确认时计一次。
触发模式（按钮/HTTP）每次都是独立一帧，构造时传 `tracker=None` 即可。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

import numpy as np

from .detect import Detection, square_crop
from .event import FALLBACK_NO_DETECTION, build_event, make_event_id
from .taxonomy import top_k
from .tracking import ItemTracker, appearance_signature
from .types import FrameMeta, ImageRef, Prediction


@dataclass
class PipelineResult:
    """一帧的产物。`events` 已经是可直接发布的 payload 列表。"""

    events: list[dict]
    detections: list[Detection]
    fallback: bool
    detect_ms: float
    classify_ms: float
    total_ms: float

    @property
    def item_count(self) -> int:
        return 0 if self.fallback else len(self.events)

    @property
    def new_item_count(self) -> int:
        return sum(1 for e in self.events if e.get("_new_item"))


def new_session_id() -> str:
    """一次进程启动一个 id。事件的幂等键含它，重启后 frame_id 归零也不撞。"""
    return uuid.uuid4().hex[:12]


class MultiItemPipeline:
    def __init__(self, detector, classifier, model_info: dict,
                 device: str = "", session_id: str | None = None,
                 tracker: ItemTracker | None = None,
                 expand: float = 0.15, max_items: int = 8,
                 top_n: int = 3) -> None:
        self.detector = detector
        self.classifier = classifier
        self.model_info = dict(model_info)
        self.device = device
        self.session_id = session_id or new_session_id()
        self.tracker = tracker
        self.expand = float(expand)
        self.max_items = int(max_items)
        self.top_n = int(top_n)

    # ---- 内部 ----------------------------------------------------------
    def _classify(self, patch: np.ndarray, meta: FrameMeta,
                  trigger: str) -> tuple[Prediction, float]:
        start = time.monotonic()
        logits = self.classifier.predict(patch)
        elapsed = (time.monotonic() - start) * 1000.0
        ranked = top_k(np.asarray(logits).ravel().tolist(), k=self.top_n,
                       classes=self.classifier.classes)
        return Prediction(meta=meta, ranked=ranked,
                          inference_time_ms=round(elapsed, 3),
                          trigger=trigger), elapsed

    def _fallback_event(self, frame, meta, trigger, image_ref,
                        detect_ms) -> PipelineResult:
        prediction, classify_ms = self._classify(frame, meta, trigger)
        payload = build_event(
            prediction, self.model_info, device=self.device,
            image_ref=image_ref, top_n=self.top_n,
            item_index=0, item_count=0,
            event_id=make_event_id(self.session_id, meta.stream_id,
                                   meta.frame_id, 0),
            fallback=FALLBACK_NO_DETECTION,
            pipeline_ms=round(detect_ms + classify_ms, 3))
        return PipelineResult([payload], [], True, round(detect_ms, 3),
                              round(classify_ms, 3),
                              round(detect_ms + classify_ms, 3))

    # ---- 入口 ----------------------------------------------------------
    def process(self, frame: np.ndarray, meta: FrameMeta,
                trigger: str = "continuous",
                image_ref: ImageRef | None = None) -> PipelineResult:
        started = time.monotonic()
        detect_started = time.monotonic()
        detections = list(self.detector.detect(frame))[:self.max_items]
        detect_ms = (time.monotonic() - detect_started) * 1000.0

        if not detections:
            # 空帧也要推进轨迹老化。不推的话"出现 2 帧 → 空 10 帧 → 原位
            # 再出现"会续上同一条轨迹，第二件永远不计数。
            if self.tracker is not None:
                self.tracker.mark_empty_frame(meta.frame_id)
            return self._fallback_event(frame, meta, trigger, image_ref,
                                        detect_ms)

        # 从左上到右下排序：同一场景重复跑，item_index 稳定，
        # 便于人工比对与回归 diff（检测器输出顺序是按分数的，会抖）。
        detections.sort(key=lambda d: (round(d.bbox[1], 1), round(d.bbox[0], 1)))

        patches = [square_crop(frame, d.bbox, self.expand) for d in detections]
        assignments = None
        if self.tracker is not None:
            signatures = [appearance_signature(p) for p in patches]
            assignments = self.tracker.update(detections, signatures,
                                              meta.frame_id)

        events: list[dict] = []
        classify_ms = 0.0
        for index, (detection, patch) in enumerate(zip(detections, patches)):
            prediction, elapsed = self._classify(patch, meta, trigger)
            classify_ms += elapsed
            track_id = assignments[index]["track_id"] if assignments else None
            payload = build_event(
                prediction, self.model_info, device=self.device,
                image_ref=image_ref, top_n=self.top_n,
                item_index=index, item_count=len(detections),
                bbox=detection.normalized(meta.width, meta.height),
                detection_confidence=detection.score,
                track_id=track_id,
                event_id=make_event_id(self.session_id, meta.stream_id,
                                       meta.frame_id, index))
            if assignments is not None:
                # 下划线开头的键只在进程内部用来算"新件数"，
                # 发布前由 publisher 侧剥掉，不进 MQTT payload。
                payload["_new_item"] = assignments[index]["is_new_item"]
            events.append(payload)

        total_ms = (time.monotonic() - started) * 1000.0
        for payload in events:
            payload["pipeline_ms"] = round(total_ms, 3)
        return PipelineResult(events, detections, False, round(detect_ms, 3),
                              round(classify_ms, 3), round(total_ms, 3))


def strip_internal(payload: dict) -> dict:
    """去掉下划线开头的进程内部字段。发布前调用一次。"""
    return {k: v for k, v in payload.items() if not k.startswith("_")}

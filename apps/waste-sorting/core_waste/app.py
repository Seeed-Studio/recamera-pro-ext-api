"""常驻分类服务：采集 → 分类 → 门控 → MQTT / 执行机构 / Web。

    python3 -m core_waste.app --config /app/config/config.json --device pi5-hailo

这是 Pi(Hailo) 与 Orin(TensorRT) 两个运行镜像的入口。reCamera 上跑的是
`platforms/cvi/waste-sorting` 的 C++ 版；两边共用同一份事件契约
（`contracts/mqtt-event.schema.json`）与同一个发布门控状态机
（`core_waste.publish_gate` 是 `waste_publish_gate.h` 的逐条移植），所以同一段
输入在三个平台上发出的事件条数与字段是可比的。

线程模型与 A 项目一致：每路源一个独占采集线程（`sources.py`），推理在主线程
串行做——加速器 context 不是线程安全的，把推理留在单线程比加锁简单，也让
`inference_time_ms` 只包含推理调用本身（契约 MQTT.md 的字段语义）。
HTTP 面板在自己的线程里，只往容量 1 的触发槽塞请求，不碰加速器。

两种触发模式：

  continuous   每帧都过一遍门控，稳定 N 帧才发（rules.consecutive_frames）
  on_demand    只在收到触发（HTTP /trigger）时抓一帧发一条，绕过门控——
               「按一次拍一次」的语义下，去抖由 TriggerBox 的 debounce_ms 做
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("core_waste.app")


def _ensure_repo_on_path() -> Path:
    """让 `backends.*` 可导入。core 层不 import backend，只在工厂里按名加载。"""
    root = Path(__file__).resolve().parents[2]
    for entry in (str(root), str(root / "core-py")):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    return root


REPO_ROOT = _ensure_repo_on_path()

from core_waste.config import load_config  # noqa: E402
from core_waste.event import build_event, make_event_id  # noqa: E402
from core_waste.gpio import CallbackActuator, NullActuator  # noqa: E402
from core_waste.pipeline import new_session_id  # noqa: E402
from core_waste.publish_gate import PublishGate  # noqa: E402
from core_waste.publisher import PayloadRejected, build_publisher  # noqa: E402
from core_waste.sources import build_sources  # noqa: E402
from core_waste.taxonomy import top_k  # noqa: E402
from core_waste.types import ImageRef, Prediction  # noqa: E402
from core_waste.webui import TriggerBox, WebPanel  # noqa: E402


def build_classifier(config: dict[str, Any]):
    """按 model.accelerator 选后端。厂商 SDK 只在被选中的分支里 import。"""
    model = config["model"]
    accelerator = model["accelerator"]
    classes = tuple(config["classes"])
    input_size = (int(model["input_size"][0]), int(model["input_size"][1]))
    kwargs = dict(classes=classes, input_size=input_size,
                  backbone=str(model.get("backbone", "")))
    if accelerator == "cpu":
        from backends.onnxruntime.classifier import OnnxRuntimeClassifier

        return OnnxRuntimeClassifier(model["path"], **kwargs)
    if accelerator == "hailo":
        from backends.hailo.classifier import HailoClassifier

        return HailoClassifier(model["path"],
                               onnx_sha256=str(model.get("onnx_sha256", "")),
                               input_range=str(model.get("input_range", "float32")),
                               **kwargs)
    if accelerator == "tensorrt":
        from backends.tensorrt.classifier import TensorRTClassifier

        return TensorRTClassifier(model["path"],
                                  onnx_sha256=str(model.get("onnx_sha256", "")),
                                  **kwargs)
    if accelerator == "rknn":
        from backends.rknn.classifier import RknnClassifier

        return RknnClassifier(model["path"],
                              onnx_sha256=str(model.get("onnx_sha256", "")),
                              core_mask=str(model.get("core_mask", "")),
                              **kwargs)
    raise NotImplementedError(f"accelerator not implemented yet: {accelerator}")


class WasteApp:
    """一个配置 = 一个进程。"""

    def __init__(self, config: dict[str, Any], device_name: str = "",
                 classifier=None, capture_factory=None) -> None:
        self.config = config
        self.device_name = device_name
        self.classifier = (classifier if classifier is not None
                           else build_classifier(config))
        self.classes = tuple(self.classifier.classes)
        self.top_n = int(config["model"].get("top_k", 3))
        self.session_id = new_session_id()
        if capture_factory is None:
            self.sources = build_sources(config)
        else:
            self.sources = build_sources(config, capture_factory)
        self.gates = {source.spec.stream_id: PublishGate.from_config(config)
                      for source in self.sources}
        self.publisher = build_publisher(config, client_id=f"waste-{device_name}")

        trigger_cfg = config["trigger"]
        self.mode = str(trigger_cfg.get("mode", "on_demand"))
        self.max_fps = float(trigger_cfg.get("max_fps", 5))
        self._frame_interval = 1.0 / self.max_fps if self.max_fps > 0 else 0.0
        self._last_frame_at = 0.0
        self.trigger_box = TriggerBox(
            debounce_ms=int(trigger_cfg.get("debounce_ms", 800)),
            merge_pending=bool(trigger_cfg.get("merge_pending", True)))

        actuator_cfg = config["actuator"]
        self.actuator = (CallbackActuator(float(actuator_cfg.get(
            "min_confidence", 0.5))) if actuator_cfg.get("enabled")
            else NullActuator())

        store = config["capture_store"]
        self.capture_kind = str(store.get("kind", "none"))
        self.capture_dir = Path(store["dir"]) if store.get("dir") else None
        self.capture_base_uri = str(store.get("base_uri", ""))
        if self.capture_kind == "local" and self.capture_dir is not None:
            self.capture_dir.mkdir(parents=True, exist_ok=True)

        self.rules = config["rules"]
        self.web = WebPanel(config["web"], self.health, self.trigger_box)
        self.model_info = {
            "name": Path(config["model"]["path"]).stem,
            "input": (f"images:1x3x{config['model']['input_size'][1]}"
                      f"x{config['model']['input_size'][0]}"),
            "onnx_sha256": (getattr(self.classifier, "onnx_sha256", "")
                            or config["model"].get("onnx_sha256", "")),
            "accelerator": self.classifier.accelerator,
            "backbone": getattr(self.classifier, "backbone", ""),
        }
        artifact = getattr(self.classifier, "artifact_sha256", "")
        if artifact:
            self.model_info["artifact_sha256"] = artifact

        self.started_at = time.monotonic()
        self.frames_processed = 0
        self.events_published = 0
        self.publish_failures = 0
        self.inference_ms_total = 0.0
        self._stop = False

    # -- 健康状态 ---------------------------------------------------------
    def health(self) -> dict[str, Any]:
        uptime = time.monotonic() - self.started_at
        return {
            "status": "ok" if self.frames_processed else "starting",
            "device": self.device_name,
            "session_id": self.session_id,
            "uptime_s": round(uptime, 2),
            "mode": self.mode,
            "frames_processed": self.frames_processed,
            "events_published": self.events_published,
            "fps": round(self.frames_processed / uptime, 3) if uptime > 0 else 0.0,
            "inference_ms_avg": (round(self.inference_ms_total /
                                       self.frames_processed, 3)
                                 if self.frames_processed else 0.0),
            "model": self.model_info,
            "sources": [source.stats() for source in self.sources],
            "trigger": self.trigger_box.stats(),
            "gates": {sid: gate.stats() for sid, gate in self.gates.items()},
            "actuator": {"dispatched": getattr(self.actuator, "dispatched", 0)},
            "mqtt": {
                "published": self.publisher.published,
                "rejected": self.publisher.rejected,
                "connected": getattr(self.publisher, "connected", False),
                "last_error": self.publisher.last_error,
            },
        }

    # -- 生命周期 ---------------------------------------------------------
    def start(self) -> None:
        self.classifier.warmup()
        self.web.start()
        for source in self.sources:
            source.start()

    def stop(self) -> None:
        self._stop = True
        for source in self.sources:
            source.stop()
        self.web.stop()
        self.actuator.close()
        self.publisher.close()
        self.classifier.close()

    def request_stop(self, *_args) -> None:
        LOGGER.info("stop requested")
        self._stop = True

    # -- 采集图片落盘 -----------------------------------------------------
    def _store_capture(self, frame, meta) -> ImageRef:
        """图片落本地或对象存储，payload 里只带引用（spec §5）。"""
        if self.capture_kind == "none":
            return ImageRef()
        name = f"{meta.stream_id}_{int(time.time() * 1000)}_{meta.frame_id}.jpg"
        if self.capture_kind == "object_store":
            return ImageRef(kind="object_store",
                            uri=f"{self.capture_base_uri.rstrip('/')}/{name}")
        if self.capture_dir is None:
            return ImageRef()
        import cv2

        path = self.capture_dir / name
        try:
            cv2.imwrite(str(path), frame)
        except Exception as exc:  # noqa: BLE001 - 落盘失败不该停产线
            LOGGER.warning("capture write failed: %s", exc)
            return ImageRef()
        return ImageRef(kind="local", uri=str(path))

    # -- 单帧处理 ---------------------------------------------------------
    def _classify(self, frame, meta, trigger: str) -> Prediction:
        start = time.perf_counter()
        logits = self.classifier.predict(frame)
        elapsed = (time.perf_counter() - start) * 1000.0
        self.inference_ms_total += elapsed
        import numpy as np

        ranked = top_k(np.asarray(logits).ravel().tolist(), k=self.top_n,
                       classes=self.classes)
        return Prediction(meta=meta, ranked=ranked,
                          inference_time_ms=round(elapsed, 3), trigger=trigger)

    def _apply_confidence_rule(self, ranked: list[dict]) -> list[dict]:
        """top-1 低于 min_confidence 时改判为 fallback_category（schema 的语义）。

        只改 top-1 的类别标签，`top3` 里原本的排名一个字不动——现场要能看出
        「模型本来想说什么」，否则低置信度的批量误判在事件流里查不出来源。
        """
        gate = float(self.rules.get("min_confidence", 0.5))
        if not ranked or float(ranked[0]["confidence"]) >= gate:
            return ranked
        fallback = str(self.rules.get("fallback_category", "residual"))
        for entry in ranked:
            if entry["class_name"] == fallback:
                return [dict(entry, rank=0)] + [dict(e) for e in ranked]
        return ranked

    def _publish(self, prediction: Prediction, image_ref: ImageRef,
                 pipeline_ms: float) -> bool:
        ranked = self._apply_confidence_rule(list(prediction.ranked))
        payload = build_event(
            Prediction(meta=prediction.meta, ranked=ranked,
                       inference_time_ms=prediction.inference_time_ms,
                       trigger=prediction.trigger),
            self.model_info, device=self.device_name, image_ref=image_ref,
            top_n=self.top_n, pipeline_ms=pipeline_ms,
            # 不报 item_index / item_count：这条链路是整帧分类，没有检测框。
            # 契约里那对字段一出现就意味着「某一帧里的第几件」，消费端会要求
            # 配套的 bbox（validate_payload.py:227-241）。整帧事件保持 1.0.0
            # 的形状，多件同框走 core_waste.pipeline 那条路。
            event_id=make_event_id(self.session_id, prediction.meta.stream_id,
                                   prediction.meta.frame_id, 0))
        try:
            self.publisher.publish(payload)
        except PayloadRejected as exc:
            self.publish_failures += 1
            LOGGER.error("payload rejected by contract: %s", exc)
            return False
        except Exception as exc:  # noqa: BLE001 - broker 抖动不该停产线
            self.publish_failures += 1
            LOGGER.warning("publish failed: %s", exc)
            return False
        self.events_published += 1
        best = payload["category"]
        self.actuator.on_result(best["china_category"], best["class_name"],
                                float(payload["confidence"]))
        return True

    def process_once(self, source) -> bool:
        """处理该源的一帧。返回是否真的处理了帧。"""
        item = source.read(timeout=0.2)
        if item is None:
            return False
        frame, meta = item

        forced = self.trigger_box.take()
        if self.mode == "on_demand" and forced is None:
            # 触发模式下没人按，帧读出来就丢——保持采集线程不积压。
            return True
        if self.mode == "continuous" and self._frame_interval:
            now = time.monotonic()
            if now - self._last_frame_at < self._frame_interval:
                return True
            self._last_frame_at = now

        trigger = forced or ("continuous" if self.mode == "continuous" else "http")
        prediction = self._classify(frame, meta, trigger)
        self.frames_processed += 1
        now_ms = int(time.monotonic() * 1000)
        gate = self.gates[meta.stream_id]

        if self.mode == "on_demand":
            # 「按一次拍一次」：门控不参与，去抖已经由 TriggerBox 做过。
            should_publish = True
        else:
            should_publish = gate.on_frame(prediction.class_id,
                                           prediction.confidence, now_ms)
        if not should_publish:
            return True

        image_ref = self._store_capture(frame, meta)
        pipeline_ms = (time.monotonic_ns() - meta.capture_ns) / 1e6
        ok = self._publish(prediction, image_ref, pipeline_ms)
        if self.mode != "on_demand":
            gate.on_publish_result(ok, prediction.class_id, now_ms)
        return True

    def run(self, max_frames: int | None = None,
            duration_s: float | None = None) -> dict[str, Any]:
        self.start()
        deadline = (time.monotonic() + duration_s) if duration_s else None
        try:
            while not self._stop:
                progressed = False
                for source in self.sources:
                    if self._stop:
                        break
                    if self.process_once(source):
                        progressed = True
                    if max_frames and self.frames_processed >= max_frames:
                        self._stop = True
                        break
                if deadline and time.monotonic() >= deadline:
                    break
                if all(source.exhausted for source in self.sources):
                    LOGGER.info("all sources exhausted")
                    break
                if not progressed:
                    time.sleep(0.005)
        finally:
            summary = self.health()
            self.stop()
        return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="edge-waste-sorting runtime")
    parser.add_argument("--config", required=True)
    parser.add_argument("--device", default="", help="device name for the payload")
    parser.add_argument("--max-frames", type=int, default=None)
    parser.add_argument("--duration", type=float, default=None,
                        help="stop after N seconds")
    parser.add_argument("--summary-out", default=None,
                        help="write the run summary JSON here")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = load_config(args.config)
    app = WasteApp(config, device_name=args.device)
    signal.signal(signal.SIGINT, app.request_stop)
    signal.signal(signal.SIGTERM, app.request_stop)
    summary = app.run(max_frames=args.max_frames, duration_s=args.duration)
    text = json.dumps(summary, indent=2, ensure_ascii=False)
    if args.summary_out:
        Path(args.summary_out).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

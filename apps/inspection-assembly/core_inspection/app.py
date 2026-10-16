"""运行时入口：采集 → 推理 → 缺件 + 尺寸 → 判定 → MQTT / Modbus / Web。

    python3 -m core_inspection.app --config config/config.json

线程模型：每路源一个独占采集线程（`sources.py`），推理在主线程串行做。
加速器 context 不是线程安全的，把推理留在单线程比加锁更简单，也让
`inference_time_ms` 只包含推理调用本身（契约 MQTT.md 的字段语义）。

Demo B 在推理与判定之间插了两个纯 CPU 模块：`assembly.compare`（检测框 vs
期望件清单）与 `dimension.inspect`（标定 + 测量）。两者都是 per-source 配置：
ROI 是画面坐标，一路源一套。`VerdictEngine` 把四条依据合并成一次 OK/NG。
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

LOGGER = logging.getLogger("core_inspection.app")


def _ensure_repo_on_path() -> Path:
    """让 `backends.*` 可导入。core 层不 import backend，只在工厂里按名加载。"""
    root = Path(__file__).resolve().parents[2]
    for entry in (str(root), str(root / "core-py")):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    return root


REPO_ROOT = _ensure_repo_on_path()

from core_inspection.assembly import AssemblyConfig, compare  # noqa: E402
from core_inspection.config import load_config, section_for  # noqa: E402
from core_inspection.dimension import DimensionConfig, inspect  # noqa: E402
from core_inspection.event import build_event  # noqa: E402
from core_inspection.modbus_server import ModbusService  # noqa: E402
from core_inspection.overlay import draw, encode_jpeg  # noqa: E402
from core_inspection.publisher import PayloadRejected, build_publisher  # noqa: E402
from core_inspection.rules import Rules, VerdictEngine  # noqa: E402
from core_inspection.sources import build_sources  # noqa: E402
from core_inspection.types import InferenceResult  # noqa: E402
from core_inspection.vlm_client import (ExplainJob, VlmConfig,  # noqa: E402
                                        VlmExplainer, boxes_from_detections,
                                        should_explain)
from core_inspection.webui import PreviewState, WebPanel  # noqa: E402


def build_detector(config: dict[str, Any]):
    """按 model.accelerator 选后端。厂商 SDK 只在被选中的分支里被 import。"""
    model = config["model"]
    accelerator = model["accelerator"]
    classes = tuple(config["classes"])
    input_size = (int(model["input_size"][0]), int(model["input_size"][1]))
    kwargs = dict(classes=classes, input_size=input_size,
                  score_threshold=float(model["score_threshold"]),
                  nms_threshold=float(model["nms_threshold"]))
    if accelerator == "cpu":
        from backends.onnxruntime.detector import OnnxRuntimeDetector

        return OnnxRuntimeDetector(model["path"], **kwargs)
    if accelerator == "tensorrt":
        from backends.tensorrt.detector import TensorRTDetector

        return TensorRTDetector(model["path"],
                                onnx_sha256=model.get("onnx_sha256", ""), **kwargs)
    if accelerator == "hailo":
        from backends.hailo.detector import HailoDetector

        return HailoDetector(model["path"],
                             onnx_sha256=model.get("onnx_sha256", ""), **kwargs)
    if accelerator == "rknn":
        from backends.rknn.detector import RknnDetector

        return RknnDetector(model["path"],
                            onnx_sha256=model.get("onnx_sha256", ""), **kwargs)
    raise NotImplementedError(f"accelerator not implemented yet: {accelerator}")


class InspectionApp:
    """一个配置 = 一个进程。多路源共享一份 Modbus 寄存器，最后一次判定生效。"""

    def __init__(self, config: dict[str, Any], device_name: str = "",
                 detector=None, capture_factory=None, vlm=None) -> None:
        self.config = config
        self.device_name = device_name
        self.detector = detector if detector is not None else build_detector(config)
        self.rules = Rules.from_config(config["rules"])
        if capture_factory is None:
            self.sources = build_sources(config)
        else:
            self.sources = build_sources(config, capture_factory)
        self.engines = {source.spec.stream_id: VerdictEngine(self.rules)
                        for source in self.sources}
        # 缺件 / 尺寸都是 per-source：源上写了用源上的，否则退回全局那份。
        self.assembly_configs = {}
        self.dimension_configs = {}
        for entry, source in zip(config["sources"], self.sources):
            stream_id = source.spec.stream_id
            self.assembly_configs[stream_id] = AssemblyConfig.from_config(
                section_for(config, entry, "assembly"))
            self.dimension_configs[stream_id] = DimensionConfig.from_config(
                section_for(config, entry, "dimension"))
        self.publisher = build_publisher(config,
                                         client_id=f"inspection-{device_name}")
        # VLM 只解释不判定：链路完全旁挂，关掉时下面两处判断直接短路。
        self.vlm_config = VlmConfig.from_config(config.get("vlm"))
        self.vlm = vlm if vlm is not None else (
            VlmExplainer(self.vlm_config) if self.vlm_config.enabled else None)
        self.modbus = ModbusService(config["modbus"])
        self.preview = PreviewState()
        self.web = WebPanel(config["web"], self.preview, self.health)
        self.model_info = {
            "name": Path(config["model"]["path"]).stem,
            "input": (f"images:1x3x{config['model']['input_size'][1]}"
                      f"x{config['model']['input_size'][0]}"),
            "onnx_sha256": getattr(self.detector, "onnx_sha256", "") or
            config["model"].get("onnx_sha256", ""),
            "accelerator": self.detector.accelerator,
        }
        artifact = getattr(self.detector, "artifact_sha256", "")
        if artifact:
            self.model_info["artifact_sha256"] = artifact
        self.started_at = time.monotonic()
        self.frames_processed = 0
        self.ng_frames = 0
        self.missing_frames = 0
        self.dimension_failure_frames = 0
        self.inference_ms_total = 0.0
        self.publish_failures = 0
        self.latency_samples_ms: list[float] = []
        self._preview_interval = (1.0 / float(config["web"].get("preview_fps", 5))
                                 if config["web"].get("enabled", True) else 0.0)
        self._last_preview = 0.0
        self._stop = False

    # -- 健康状态 ---------------------------------------------------------
    def health(self) -> dict[str, Any]:
        uptime = time.monotonic() - self.started_at
        latency = sorted(self.latency_samples_ms)
        health = {
            "status": "ok" if self.frames_processed else "starting",
            "device": self.device_name,
            "uptime_s": round(uptime, 2),
            "frames_processed": self.frames_processed,
            "ng_frames": self.ng_frames,
            "missing_frames": self.missing_frames,
            "dimension_failure_frames": self.dimension_failure_frames,
            "fps": round(self.frames_processed / uptime, 3) if uptime > 0 else 0.0,
            "inference_ms_avg": round(
                self.inference_ms_total / self.frames_processed, 3)
            if self.frames_processed else 0.0,
            "capture_to_coil_ms_p50": (round(latency[len(latency) // 2], 3)
                                       if latency else None),
            "model": self.model_info,
            "sources": [source.stats() for source in self.sources],
            "mqtt": {
                "published": self.publisher.published,
                "rejected": self.publisher.rejected,
                "connected": getattr(self.publisher, "connected", False),
                "last_error": self.publisher.last_error,
            },
            "modbus": self.modbus.status(),
        }
        if self.vlm is not None:
            health["vlm"] = dict(self.vlm.metrics.to_json(),
                                 published=self.publisher.explanations_published)
        return health

    # -- 生命周期 ---------------------------------------------------------
    def start(self) -> None:
        self.detector.warmup()
        if self.vlm is not None:
            self.vlm.start()
        self.modbus.start()
        self.web.start()
        for source in self.sources:
            source.start()

    def stop(self) -> None:
        self._stop = True
        for source in self.sources:
            source.stop()
        self.web.stop()
        if self.vlm is not None:
            self.vlm.stop()
        self.modbus.stop()
        self.publisher.close()
        self.detector.close()

    def request_stop(self, *_args) -> None:
        LOGGER.info("stop requested")
        self._stop = True

    # -- 主循环 -----------------------------------------------------------
    def process_once(self, source) -> bool:
        """处理该源的一帧。返回是否真的处理了帧。"""
        item = source.read(timeout=0.2)
        if item is None:
            return False
        frame, meta = item
        detections = self.detector.detect(frame)
        inference_ms = float(getattr(self.detector, "last_inference_ms", 0.0))
        # 两个模块都跑在 CPU 上，不进推理路径；关掉时各自返回 enabled=False。
        assembly = compare(detections, self.assembly_configs[meta.stream_id])
        dimension = inspect(frame, self.dimension_configs[meta.stream_id])
        verdict = self.engines[meta.stream_id].evaluate(detections, assembly,
                                                        dimension)

        # 契约要求先落寄存器再翻线圈；ModbusStore.apply 在同一把锁里做完。
        self.modbus.apply_verdict(verdict, dimension=dimension)
        coil_ns = time.monotonic_ns()
        pipeline_ms = (coil_ns - meta.capture_ns) / 1e6
        self.latency_samples_ms.append(pipeline_ms)
        if len(self.latency_samples_ms) > 10000:
            del self.latency_samples_ms[:-10000]

        result = InferenceResult(meta=meta, detections=detections,
                                 inference_time_ms=inference_ms)
        event = build_event(result, verdict, self.model_info,
                            device=self.device_name, pipeline_ms=pipeline_ms,
                            assembly=assembly, dimension=dimension)
        try:
            self.publisher.publish(event)
        except PayloadRejected as exc:
            self.publish_failures += 1
            LOGGER.error("payload rejected by contract: %s", exc)
        self.preview.add_event({
            "stream_id": event["stream_id"], "frame_id": event["frame_id"],
            "timestamp": event["timestamp"], "verdict": event["verdict"],
            "defect_count": event["defect_count"],
            "verdict_reasons": event["verdict_reasons"],
            "missing_count": event["assembly"]["missing_count"],
            "extra_count": event["assembly"]["extra_count"],
            "out_of_tolerance_count": event["dimension"]["out_of_tolerance_count"],
            "inference_time_ms": event["inference_time_ms"],
        })
        self.frames_processed += 1
        self.inference_ms_total += inference_ms
        if verdict.is_ng:
            self.ng_frames += 1
        if verdict.missing_count:
            self.missing_frames += 1
        if verdict.dimension_failures:
            self.dimension_failure_frames += 1
        self._maybe_vlm(frame, detections, verdict, assembly, event)
        self._maybe_preview(frame, detections, verdict, inference_ms)
        return True

    def _maybe_vlm(self, frame, detections, verdict, assembly, event) -> None:
        """投递一次解释请求 + 收回已完成的解释。两步都不阻塞。

        主事件此刻已经发出去了：VLM 的 P50 以秒计（INTEGRATION §3），等它就是
        把产线时序绑在 VLM 上。解释另发一条 assembly_explanation，用 frame_id
        对上号。这里只做 put_nowait / get_nowait，没有任何网络调用。
        """
        if self.vlm is None:
            return
        if should_explain(verdict, assembly, self.vlm_config):
            self.vlm.submit(ExplainJob(
                stream_id=event["stream_id"], frame_id=event["frame_id"],
                frame=frame, boxes=boxes_from_detections(detections),
                verdict=event["verdict"],
                verdict_reasons=list(event["verdict_reasons"]),
                device=self.device_name))
        for explanation in self.vlm.drain():
            try:
                self.publisher.publish_explanation(explanation)
            except PayloadRejected as exc:
                self.publish_failures += 1
                LOGGER.error("explanation rejected by contract: %s", exc)

    def _maybe_preview(self, frame, detections, verdict, inference_ms) -> None:
        if not self._preview_interval:
            return
        now = time.monotonic()
        if now - self._last_preview < self._preview_interval:
            return
        self._last_preview = now
        try:
            header = f"{verdict.reason} | {inference_ms:.1f} ms"
            self.preview.set_frame(encode_jpeg(draw(frame, detections, verdict,
                                                    header)))
        except Exception as exc:  # noqa: BLE001 - 预览失败不该停产线
            LOGGER.warning("preview failed: %s", exc)

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
    parser = argparse.ArgumentParser(description="edge-inspection-assembly runtime")
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
    app = InspectionApp(config, device_name=args.device)
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

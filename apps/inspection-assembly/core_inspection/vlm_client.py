"""共享视觉 VLM 服务（edge-vision-vlm）的客户端。**永不进帧循环**。

契约：`edge-vision-vlm/docs/INTEGRATION.md`。核心约束照抄那份文件：
VLM 只解释，不参与判定；调用慢、失败或服务不在，判定与事件节奏都不许变。

线程模型：
    主线程 process_once() --submit()--> 有界队列(drop-oldest) --> worker 线程
                                                                    |
    主线程 drain() <-- 结果队列 <-------------------- httpx.AsyncClient(单事件循环)

`submit()` 只做一次 `put_nowait`（队列满就丢最旧的），不等待、不阻塞；worker
线程里跑一个自己的 asyncio 事件循环，用 `httpx.AsyncClient` 发 `POST /v1/explain`。
JPEG 编码也放在 worker 里，主线程只交一份帧的引用副本。

结果不回填到主事件里：主事件在同一次 `process_once` 里就发出去了，等 VLM 回来
再补字段等于把主事件推迟到 VLM 的时延上（P50 ≈ 3.2 s，见 INTEGRATION §3）。
所以另发一条 `assembly_explanation` 事件到 `inspection/<stream>/explanations`，
用 `frame_id` 与主事件关联。主事件的时序与 payload 一个字节都不变。

降级：超时 / 503 / 502 / 连不上，一律只记数，不重试当前帧（帧已经过期），
连续 N 次失败后熔断冷却，冷却期用 `GET /healthz` 探活。字段直接缺席。
"""
from __future__ import annotations

import asyncio
import base64
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

LOGGER = logging.getLogger(__name__)

EXPLANATION_TYPE = "assembly_explanation"
EXPLANATION_VERSION = "1.0.0"
DEFAULT_TOPIC = "inspection/{stream_id}/explanations"


@dataclass(frozen=True)
class VlmTrigger:
    """什么时候值得叫一次 VLM。对应 config schema 的 `vlm.trigger` 段。"""

    min_confidence: float = 0.5
    on_missing_parts: bool = True
    min_interval_s: float = 5.0     # 每路源两次调用之间的最小间隔

    @classmethod
    def from_config(cls, config: dict | None) -> "VlmTrigger":
        config = config or {}
        return cls(
            min_confidence=float(config.get("min_confidence", 0.5)),
            on_missing_parts=bool(config.get("on_missing_parts", True)),
            min_interval_s=float(config.get("min_interval_s", 5.0)),
        )


@dataclass(frozen=True)
class VlmConfig:
    """对应 config schema 的 `vlm` 段。enabled=false 时整条链路不存在。"""

    enabled: bool = False
    base_url: str = "http://127.0.0.1:8100"
    timeout_ms: int = 4000
    language: str = "zh"
    max_sentences: int = 4
    max_tokens: int = 320
    queue_size: int = 2                    # INTEGRATION §3 规则 2：深度 1–2
    breaker_failures: int = 5              # 连续 N 次 503 后熔断
    breaker_cooloff_s: float = 30.0
    jpeg_quality: int = 75
    topic: str = DEFAULT_TOPIC
    trigger: VlmTrigger = field(default_factory=VlmTrigger)

    @classmethod
    def from_config(cls, config: dict | None) -> "VlmConfig":
        config = config or {}
        return cls(
            enabled=bool(config.get("enabled", False)),
            base_url=str(config.get("base_url", "http://127.0.0.1:8100")).rstrip("/"),
            timeout_ms=int(config.get("timeout_ms", 4000)),
            language=str(config.get("language", "zh")),
            max_sentences=int(config.get("max_sentences", 4)),
            max_tokens=int(config.get("max_tokens", 320)),
            queue_size=int(config.get("queue_size", 2)),
            breaker_failures=int(config.get("breaker_failures", 5)),
            breaker_cooloff_s=float(config.get("breaker_cooloff_s", 30.0)),
            jpeg_quality=int(config.get("jpeg_quality", 75)),
            topic=str(config.get("topic", DEFAULT_TOPIC)),
            trigger=VlmTrigger.from_config(config.get("trigger")),
        )


def should_explain(verdict, assembly, config: VlmConfig) -> bool:
    """触发规则：缺件，或主缺陷置信度低于阈值。

    低置信度是「机器判不准、人要看一眼」的信号；高置信度的 NG 不需要解释，
    `verdict_reasons` 已经是机器可读的原因。OK 帧一律不叫。
    """
    if not config.enabled:
        return False
    trigger = config.trigger
    if trigger.on_missing_parts and assembly is not None and assembly.missing_count:
        return True
    primary = getattr(verdict, "primary", None)
    if primary is not None and float(primary.score) < trigger.min_confidence:
        return True
    return False


@dataclass
class ExplainJob:
    """投递给 worker 的一次调用。frame 是 BGR ndarray，worker 里才编码。"""

    stream_id: str
    frame_id: int
    frame: Any
    boxes: list[dict]
    verdict: str
    verdict_reasons: list[str]
    device: str = ""


class VlmMetrics:
    """只增不减的计数器。降级不是异常路径，是常态，必须能在 /healthz 里看到。"""

    FIELDS = ("submitted", "dropped", "succeeded", "timeout", "unavailable",
              "backend_error", "transport_error", "breaker_skipped",
              "encode_failed")

    def __init__(self) -> None:
        for name in self.FIELDS:
            setattr(self, name, 0)
        self.last_error: str | None = None
        self.breaker_open = False

    def bump(self, name: str) -> None:
        setattr(self, name, getattr(self, name) + 1)

    def to_json(self) -> dict[str, Any]:
        payload = {name: getattr(self, name) for name in self.FIELDS}
        payload["breaker_open"] = self.breaker_open
        payload["last_error"] = self.last_error
        return payload


def build_explanation_event(job: ExplainJob, body: dict,
                            timestamp_ms: int | None = None) -> dict:
    """把 /v1/explain 的响应组装成 assembly_explanation 事件。

    `vlm_fallback_used` 恒 false 但保留：本 demo v1 不调 `/v1/fallback`
    （没有闭集分类头要兜底），字段留着是为了三个 demo 的消费方共用一套解析。
    `candidates` 同理——它来自 `/v1/label`，本 demo 不在线上调。
    """
    payload = {
        "type": EXPLANATION_TYPE,
        "version": EXPLANATION_VERSION,
        "stream_id": job.stream_id,
        "frame_id": job.frame_id,
        "timestamp": int(timestamp_ms if timestamp_ms is not None
                         else time.time() * 1000),
        "verdict": job.verdict,
        "verdict_reasons": list(job.verdict_reasons),
        "explanation": str(body.get("explanation", "")),
        "vlm_model": str(body.get("vlm_model", "")),
        "vlm_latency_ms": round(float(body.get("vlm_latency_ms", 0.0)), 3),
        "vlm_fallback_used": bool(body.get("vlm_fallback_used", False)),
        "candidates": list(body.get("candidates", ())),
    }
    if body.get("prompt_sha256"):
        payload["prompt_sha256"] = str(body["prompt_sha256"])
    if job.device:
        payload["device"] = job.device
    return payload


class VlmExplainer:
    """有界 worker：主线程只 submit / drain，网络与编码都在 worker 线程里。"""

    def __init__(self, config: VlmConfig, transport=None,
                 encode_jpeg: Callable[[Any, int], bytes] | None = None) -> None:
        self.config = config
        self.metrics = VlmMetrics()
        self._transport = transport            # 测试注入 httpx.MockTransport
        self._encode = encode_jpeg
        self._jobs: "queue.Queue[ExplainJob | None]" = queue.Queue(
            maxsize=max(1, config.queue_size))
        self._results: "queue.Queue[dict]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_call: dict[str, float] = {}
        self._consecutive_failures = 0
        self._breaker_until = 0.0

    # -- 主线程侧 ---------------------------------------------------------
    def start(self) -> None:
        if not self.config.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="vlm-explain",
                                        daemon=True)
        self._thread.start()

    def submit(self, job: ExplainJob) -> bool:
        """非阻塞投递。队列满就丢最旧的一条——旧帧比新帧更没有解释价值。"""
        if not self.config.enabled or self._thread is None:
            return False
        now = time.monotonic()
        last = self._last_call.get(job.stream_id, 0.0)
        if now - last < self.config.trigger.min_interval_s:
            return False
        self._last_call[job.stream_id] = now
        while True:
            try:
                self._jobs.put_nowait(job)
                self.metrics.bump("submitted")
                return True
            except queue.Full:
                try:
                    self._jobs.get_nowait()
                    self.metrics.bump("dropped")
                except queue.Empty:      # 竞态：worker 刚取走，重试一次即可
                    pass

    def drain(self, limit: int = 8) -> list[dict]:
        """取回已完成的解释事件。主线程调用，永不阻塞。"""
        out: list[dict] = []
        for _ in range(limit):
            try:
                out.append(self._results.get_nowait())
            except queue.Empty:
                break
        return out

    def stop(self, timeout: float = 2.0) -> None:
        if self._thread is None:
            return
        self._stop.set()
        try:
            self._jobs.put_nowait(None)
        except queue.Full:
            pass
        self._thread.join(timeout)
        self._thread = None

    # -- worker 线程侧 ----------------------------------------------------
    def _run(self) -> None:
        try:
            asyncio.run(self._worker())
        except Exception as exc:            # noqa: BLE001 - worker 死了不该拖垮产线
            LOGGER.error("vlm worker crashed: %s", exc)
            self.metrics.last_error = f"worker crashed: {exc}"

    async def _worker(self) -> None:
        import httpx

        timeout = httpx.Timeout(self.config.timeout_ms / 1000.0)
        kwargs: dict[str, Any] = {"base_url": self.config.base_url,
                                  "timeout": timeout}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        async with httpx.AsyncClient(**kwargs) as client:
            while not self._stop.is_set():
                job = await asyncio.get_running_loop().run_in_executor(
                    None, self._next_job)
                if job is None:
                    continue
                if self._stop.is_set():
                    break
                await self._handle(client, job)

    def _next_job(self) -> ExplainJob | None:
        try:
            return self._jobs.get(timeout=0.2)
        except queue.Empty:
            return None

    async def _handle(self, client, job: ExplainJob) -> None:
        import httpx

        now = time.monotonic()
        if now < self._breaker_until:
            self.metrics.bump("breaker_skipped")
            return
        if self.metrics.breaker_open:
            # 冷却结束：先探活，探不通就再冷却一轮，不拿业务帧去试。
            if not await self._probe(client):
                self._breaker_until = now + self.config.breaker_cooloff_s
                self.metrics.bump("breaker_skipped")
                return
            self.metrics.breaker_open = False
            self._consecutive_failures = 0

        try:
            image_b64 = self._encode_frame(job.frame)
        except Exception as exc:            # noqa: BLE001 - 编码失败只丢这一帧
            self.metrics.bump("encode_failed")
            self.metrics.last_error = f"jpeg encode failed: {exc}"
            LOGGER.warning("vlm skipped frame %s: jpeg encode failed: %s",
                           job.frame_id, exc)
            return

        request = {
            "image": {"image_b64": image_b64},
            "boxes": job.boxes,
            "verdict": job.verdict,
            "language": self.config.language,
            "max_sentences": self.config.max_sentences,
            "max_tokens": self.config.max_tokens,
        }
        try:
            response = await client.post("/v1/explain", json=request)
        except httpx.TimeoutException as exc:
            self._fail("timeout", f"/v1/explain timed out after "
                                  f"{self.config.timeout_ms} ms: {exc}", job)
            return
        except httpx.HTTPError as exc:
            self._fail("transport_error", f"/v1/explain transport error: {exc}", job)
            return

        if response.status_code == 503:
            self._fail("unavailable", "/v1/explain 503 vlm_unavailable", job)
            return
        if response.status_code >= 400:
            self._fail("backend_error",
                       f"/v1/explain HTTP {response.status_code}", job)
            return
        try:
            body = response.json()
        except ValueError as exc:
            self._fail("backend_error", f"/v1/explain body not JSON: {exc}", job)
            return

        self._consecutive_failures = 0
        self.metrics.bump("succeeded")
        self._results.put(build_explanation_event(job, body))

    async def _probe(self, client) -> bool:
        import httpx

        try:
            response = await client.get("/healthz")
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    def _fail(self, counter: str, message: str, job: ExplainJob) -> None:
        """降级路径：记数 + 一行日志，不重试、不发事件、不影响主链路。"""
        self.metrics.bump(counter)
        self.metrics.last_error = message
        self._consecutive_failures += 1
        LOGGER.warning("vlm degraded (%s) stream=%s frame=%s: %s",
                       counter, job.stream_id, job.frame_id, message)
        if (not self.metrics.breaker_open
                and self._consecutive_failures >= self.config.breaker_failures):
            self.metrics.breaker_open = True
            self._breaker_until = time.monotonic() + self.config.breaker_cooloff_s
            LOGGER.warning("vlm circuit breaker open for %.0f s after %d "
                           "consecutive failures", self.config.breaker_cooloff_s,
                           self._consecutive_failures)

    def _encode_frame(self, frame) -> str:
        if isinstance(frame, (bytes, bytearray)):
            return base64.b64encode(bytes(frame)).decode("ascii")
        encode = self._encode
        if encode is None:
            from .overlay import encode_jpeg

            encode = encode_jpeg
        return base64.b64encode(
            encode(frame, self.config.jpeg_quality)).decode("ascii")


def boxes_from_detections(detections) -> list[dict]:
    """把检测框按 INTEGRATION §4 的形状原样送过去，不改坐标空间。"""
    return [det.to_json(slot) for slot, det in enumerate(detections)]

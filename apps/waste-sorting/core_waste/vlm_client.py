"""共享视觉 VLM 服务（edge-vision-vlm）的客户端。**永不进帧循环**。

契约：`edge-vision-vlm/docs/INTEGRATION.md`。核心约束照抄那份文件：
调用慢、失败或服务不在，主分类与事件节奏都不许变。

线程模型（与 A/B 两个 demo 逐字相同）：
    主线程 classify() --submit()--> 有界队列(drop-oldest) --> worker 线程
                                                                |
    主线程 drain() <-- 结果队列 <------------- httpx.AsyncClient(单事件循环)

`submit()` 只做一次 `put_nowait`（队列满就丢最旧的），不等待、不阻塞；worker
线程里跑一个自己的 asyncio 事件循环发请求。JPEG 编码也在 worker 里。

**本项目用的是 `/v1/fallback`，不是 `/v1/explain`。** 两个检查 demo 有机器可读
的框和判定理由，缺的是给人看的话；本项目的模糊物品缺的是**类别本身**——八类闭集
分类头在 0.3/0.28 这种分布上给出的 top-1 没有意义，开放词汇的 VLM 能看出那是
一个带塑料窗的纸质信封。所以这里调的是「给我一个类」的那个端点，
`/v1/explain` 只在 `vlm.explain_on_fallback` 打开时追加一段说明。

**兜底结果不改主事件的 `category`。** 主事件在同一次分类里就发出去了（触发即拍
模式下 VLM 的 P50 ≈ 3.2 s，见 INTEGRATION §3），等它回来再改判等于把投放动作
推迟到 VLM 的时延上。改判走旁路事件 `waste_fallback` → `waste/<stream>/fallback`，
用 `frame_id` 与主事件对上号，消费方自己决定要不要采信。
`vlm.apply_fallback_to_gpio` 是给「愿意等 3 s 再落闸」的现场留的开关，默认 false，
本轮不接执行机构（`gpio.py` 的回调仍只由主分类驱动）。

降级：超时 / 503 / 502 / 连不上，一律只记数，不重试当前帧（帧已经过期），
连续 N 次失败后熔断冷却，冷却期用 `GET /healthz` 探活。旁路事件直接缺席。
"""
from __future__ import annotations

import asyncio
import base64
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from .event import ranked_json
from .taxonomy import (CLASS_ID, MATERIAL_CLASSES, MATERIAL_TO_CHINA,
                       china_category_zh)

LOGGER = logging.getLogger(__name__)

FALLBACK_EVENT_TYPE = "waste_fallback"
FALLBACK_EVENT_VERSION = "1.0.0"
DEFAULT_TOPIC = "waste/{stream_id}/fallback"

#: 触发依据。两条都在说「主分类这一帧靠不住」，但靠不住的形状不同。
TRIGGER_LOW_CONFIDENCE = "low_confidence"
TRIGGER_AMBIGUOUS = "ambiguous"

#: 传给服务的类别描述。服务方不持有任何类别表（INTEGRATION §5），
#: 描述写在这里，跟着 `taxonomy_version` 一起版本化。
MATERIAL_DESCRIPTIONS: dict[str, str] = {
    "paper": "纸张：报纸、书页、办公用纸、纸质包装内衬",
    "cardboard": "纸板：瓦楞纸箱、蛋托、厚纸卡",
    "glass": "玻璃：瓶罐、碎玻璃、玻璃容器",
    "metal": "金属：易拉罐、罐头、金属瓶盖、金属器具",
    "plastic": "塑料：饮料瓶、塑料袋、塑料餐盒、泡沫",
    "textile": "纺织物：衣物、布料、鞋、毛绒制品",
    "organic": "厨余：果皮、剩饭剩菜、可降解的食物残渣",
    "residual": "其他垃圾：以上都不是，或多材质混合无法拆分的物品",
}


@dataclass(frozen=True)
class VlmTrigger:
    """什么时候值得叫一次 VLM。对应 config schema 的 `vlm.trigger` 段。"""

    #: top-1 置信度低于此值 → `low_confidence`
    min_confidence: float = 0.6
    #: top-1 与 top-2 的差小于此值 → `ambiguous`（模糊物品）。
    #: **可达性有个硬约束**：softmax 下 top-2 ≤ 1 − top-1，所以在 top-1 ≥ g 的
    #: 那一侧，两者的差至少是 2g − 1。g = 0.6 时 margin ≤ 0.2 这条依据永远不会
    #: 触发（是死代码，不是保守配置）。默认 0.25 留出一条窄但非空的带。
    #: `config.validate` 会在启动时拒掉不可达的组合。
    margin: float = 0.25
    #: 每路源两次调用之间的最小间隔。`/v1/fallback` 永远不是每帧都叫。
    min_interval_s: float = 5.0

    @classmethod
    def from_config(cls, config: dict | None) -> "VlmTrigger":
        config = config or {}
        return cls(
            min_confidence=float(config.get("min_confidence", 0.6)),
            margin=float(config.get("margin", 0.25)),
            min_interval_s=float(config.get("min_interval_s", 5.0)),
        )


@dataclass(frozen=True)
class VlmConfig:
    """对应 config schema 的 `vlm` 段。enabled=false 时整条链路不存在。"""

    enabled: bool = False
    base_url: str = "http://127.0.0.1:8100"
    timeout_ms: int = 4000
    language: str = "zh"
    max_tokens: int = 320
    queue_size: int = 2                    # INTEGRATION §3 规则 2：深度 1–2
    breaker_failures: int = 5              # 连续 N 次失败后熔断
    breaker_cooloff_s: float = 30.0
    jpeg_quality: int = 75
    topic: str = DEFAULT_TOPIC
    #: 追加一段 `/v1/explain` 的说明到同一条旁路事件里。多一次调用，默认关。
    explain_on_fallback: bool = False
    explain_max_sentences: int = 3
    #: 预留：兜底结果是否驱动 GPIO 回调。默认 false——投放动作不等 VLM。
    apply_fallback_to_gpio: bool = False
    trigger: VlmTrigger = field(default_factory=VlmTrigger)

    @classmethod
    def from_config(cls, config: dict | None) -> "VlmConfig":
        config = config or {}
        return cls(
            enabled=bool(config.get("enabled", False)),
            base_url=str(config.get("base_url", "http://127.0.0.1:8100")).rstrip("/"),
            timeout_ms=int(config.get("timeout_ms", 4000)),
            language=str(config.get("language", "zh")),
            max_tokens=int(config.get("max_tokens", 320)),
            queue_size=int(config.get("queue_size", 2)),
            breaker_failures=int(config.get("breaker_failures", 5)),
            breaker_cooloff_s=float(config.get("breaker_cooloff_s", 30.0)),
            jpeg_quality=int(config.get("jpeg_quality", 75)),
            topic=str(config.get("topic", DEFAULT_TOPIC)),
            explain_on_fallback=bool(config.get("explain_on_fallback", False)),
            explain_max_sentences=int(config.get("explain_max_sentences", 3)),
            apply_fallback_to_gpio=bool(config.get("apply_fallback_to_gpio", False)),
            trigger=VlmTrigger.from_config(config.get("trigger")),
        )


def fallback_trigger(ranked: Sequence[dict], config: VlmConfig) -> str | None:
    """返回触发原因，不该叫就返回 None。`ranked` 是 `taxonomy.top_k` 的输出。

    两条依据，`low_confidence` 优先（它更强）：

    * `low_confidence` —— top-1 置信度低于 `trigger.min_confidence`。分类头对整幅
      图都没把握，兜底最有价值。
    * `ambiguous` —— top-1 过了门限，但 top-2 咬得很紧（差 < `trigger.margin`）。
      典型是纸/纸板、塑料/玻璃这种物料边界；argmax 在这种分布上是掷硬币，
      置信度门限却拦不住它。**这条是本项目相对 A/B 两个 demo 多出来的一条。**
      它能覆盖的区间很窄（见 `VlmTrigger.margin` 上的可达性约束），
      这是 softmax 的性质决定的，不是参数没调好。

    只有一个类别时没有 top-2，`ambiguous` 不成立。
    """
    if not config.enabled or not ranked:
        return None
    trigger = config.trigger
    top1 = float(ranked[0]["confidence"])
    if top1 < trigger.min_confidence:
        return TRIGGER_LOW_CONFIDENCE
    if len(ranked) >= 2:
        if top1 - float(ranked[1]["confidence"]) < trigger.margin:
            return TRIGGER_AMBIGUOUS
    return None


def should_fallback(ranked: Sequence[dict], config: VlmConfig) -> bool:
    """`fallback_trigger` 的布尔外壳。"""
    return fallback_trigger(ranked, config) is not None


def taxonomy_payload(classes: Sequence[str] = MATERIAL_CLASSES,
                     version: str = "material8/china4-v1") -> dict:
    """按 INTEGRATION §4/§5 的形状组装 taxonomy：类别表随请求走，服务方不留副本。

    每个物料类的 description 里带上它的中国四分类归属：模型看到的是「玻璃 →
    可回收物」这一层信息，而不是只有一个英文单词。四分类映射是本项目的 spec §2
    规定的（`taxonomy.MATERIAL_TO_CHINA`），跟着 `version` 一起版本化——
    换了映射就换 `taxonomy_version`，否则事后无法把一条兜底结果对回当时的表。
    """
    labels = []
    for name in classes:
        parts = []
        if name in MATERIAL_DESCRIPTIONS:
            parts.append(MATERIAL_DESCRIPTIONS[name])
        if name in MATERIAL_TO_CHINA:
            parts.append(f"投放类别：{china_category_zh(name)}"
                         f"（{MATERIAL_TO_CHINA[name]}）")
        entry: dict[str, Any] = {"name": name}
        if parts:
            entry["description"] = "；".join(parts)
        labels.append(entry)
    return {"version": version, "labels": labels}


@dataclass
class FallbackJob:
    """投递给 worker 的一次调用。frame 是 BGR ndarray，worker 里才编码。"""

    stream_id: str
    frame_id: int
    frame: Any
    #: 主分类的 top-k，原样带进旁路事件（消费方能看到被兜底的是什么）
    primary_ranked: list[dict] = field(default_factory=list)
    trigger: str = TRIGGER_LOW_CONFIDENCE
    device: str = ""
    taxonomy_version: str = "material8/china4-v1"
    classes: tuple[str, ...] = MATERIAL_CLASSES
    extra_fields: dict = field(default_factory=dict)

    @property
    def primary_confidence(self) -> float:
        return (float(self.primary_ranked[0]["confidence"])
                if self.primary_ranked else 0.0)


class VlmMetrics:
    """只增不减的计数器。降级不是异常路径，是常态，必须能在 /healthz 里看到。"""

    FIELDS = ("submitted", "dropped", "succeeded", "timeout", "unavailable",
              "backend_error", "transport_error", "breaker_skipped",
              "encode_failed", "off_taxonomy", "explain_failed")

    def __init__(self) -> None:
        for name in self.FIELDS:
            setattr(self, name, 0)
        self.last_error: str | None = None
        self.breaker_open = False
        self.triggered = {TRIGGER_LOW_CONFIDENCE: 0, TRIGGER_AMBIGUOUS: 0}

    def bump(self, name: str) -> None:
        setattr(self, name, getattr(self, name) + 1)

    def to_json(self) -> dict[str, Any]:
        payload = {name: getattr(self, name) for name in self.FIELDS}
        payload["breaker_open"] = self.breaker_open
        payload["last_error"] = self.last_error
        payload["triggered"] = dict(self.triggered)
        return payload


class OffTaxonomyCategory(ValueError):
    """服务给回了不在提交的类别表里的类。按 INTEGRATION §5 一律丢弃，不猜。"""


def _category_object(name: str) -> dict:
    """把 `/v1/fallback` 的 category 字符串补成主事件同形状的对象。

    形状与 `event.build_event` 的 `category` 逐字一致，消费方一套解析走两条流。
    """
    if name not in CLASS_ID:
        raise OffTaxonomyCategory(name)
    return {
        "class_id": CLASS_ID[name],
        "class_name": name,
        "china_category": MATERIAL_TO_CHINA[name],
        "china_category_zh": china_category_zh(name),
    }


def build_fallback_event(job: FallbackJob, body: dict,
                         explanation: str | None = None,
                         timestamp_ms: int | None = None) -> dict:
    """把 `/v1/fallback` 的响应组装成 `waste_fallback` 事件。

    `vlm_fallback_used` 在这条事件里恒 true：这条事件存在，就说明 VLM 答了。
    它与主事件上那个同名的 additive 字段（`contracts/mqtt-event.schema.json`）
    含义相同，但主事件那个由 W1 决定填不填，本链路一个字节都不改主事件。

    `primary_top3` 是被兜底的那份排名。留着它，事后才能回答「VLM 改了什么」——
    只看兜底结果无法区分「纠正了一次误判」和「把对的改错了」。
    """
    payload = {
        "type": FALLBACK_EVENT_TYPE,
        "version": FALLBACK_EVENT_VERSION,
        "taxonomy_version": str(body.get("taxonomy_version")
                                or job.taxonomy_version),
        "stream_id": job.stream_id,
        "frame_id": job.frame_id,
        "timestamp": int(timestamp_ms if timestamp_ms is not None
                         else time.time() * 1000),
        "trigger": job.trigger,
        "category": _category_object(str(body.get("category", ""))),
        "confidence": round(float(body.get("confidence", 0.0)), 6),
        "rationale": str(body.get("rationale", "")),
        "vlm_fallback_used": True,
        "vlm_model": str(body.get("vlm_model", "")),
        "vlm_latency_ms": round(float(body.get("vlm_latency_ms", 0.0)), 3),
        "primary_confidence": round(job.primary_confidence, 6),
        "primary_top3": ranked_json(job.primary_ranked, 3),
    }
    if body.get("prompt_sha256"):
        payload["prompt_sha256"] = str(body["prompt_sha256"])
    if job.device:
        payload["device"] = job.device
    if explanation:
        payload["explanation"] = explanation
    payload.update(job.extra_fields)
    return payload


class VlmFallbackClient:
    """有界 worker：主线程只 submit / drain，网络与编码都在 worker 线程里。"""

    def __init__(self, config: VlmConfig, transport=None,
                 encode_jpeg: Callable[[Any, int], bytes] | None = None) -> None:
        self.config = config
        self.metrics = VlmMetrics()
        self._transport = transport            # 测试注入 httpx.MockTransport
        self._encode = encode_jpeg
        self._jobs: "queue.Queue[FallbackJob | None]" = queue.Queue(
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
        self._thread = threading.Thread(target=self._run, name="vlm-fallback",
                                        daemon=True)
        self._thread.start()

    def submit(self, job: FallbackJob) -> bool:
        """非阻塞投递。队列满就丢最旧的一条——旧帧的物品早就走过传送带了。"""
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
                if job.trigger in self.metrics.triggered:
                    self.metrics.triggered[job.trigger] += 1
                return True
            except queue.Full:
                try:
                    self._jobs.get_nowait()
                    self.metrics.bump("dropped")
                except queue.Empty:      # 竞态：worker 刚取走，重试一次即可
                    pass

    def drain(self, limit: int = 8) -> list[dict]:
        """取回已完成的旁路事件。主线程调用，永不阻塞。"""
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
        except Exception as exc:            # noqa: BLE001 - worker 死了不该拖垮投放
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

    def _next_job(self) -> FallbackJob | None:
        try:
            return self._jobs.get(timeout=0.2)
        except queue.Empty:
            return None

    async def _handle(self, client, job: FallbackJob) -> None:
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
            "taxonomy": taxonomy_payload(job.classes, job.taxonomy_version),
            "primary_confidence": round(min(1.0, max(0.0,
                                                     job.primary_confidence)), 6),
            "language": self.config.language,
            "max_tokens": self.config.max_tokens,
        }
        body = await self._post(client, "/v1/fallback", request, job)
        if body is None:
            return

        explanation = None
        if self.config.explain_on_fallback:
            explanation = await self._explain(client, job, image_b64,
                                              str(body.get("category", "")))

        try:
            event = build_fallback_event(job, body, explanation=explanation)
        except OffTaxonomyCategory as exc:
            # INTEGRATION §5：编出一个不存在的类比不兜底更糟。丢掉并单独记数。
            self.metrics.bump("off_taxonomy")
            self.metrics.last_error = f"off-taxonomy category: {exc}"
            LOGGER.warning("vlm returned off-taxonomy category %s for frame %s",
                           exc, job.frame_id)
            return
        self._consecutive_failures = 0
        self.metrics.bump("succeeded")
        self._results.put(event)

    async def _explain(self, client, job: FallbackJob, image_b64: str,
                       category: str) -> str | None:
        """兜底之后再要一段人话。失败只记数，旁路事件照发，只是少一个字段。"""
        request = {
            "image": {"image_b64": image_b64},
            "boxes": [],                      # 整图分类，没有框可传
            "verdict": category,
            "language": self.config.language,
            "max_sentences": self.config.explain_max_sentences,
            "max_tokens": self.config.max_tokens,
        }
        body = await self._post(client, "/v1/explain", request, job,
                                counter_override="explain_failed")
        if body is None:
            return None
        return str(body.get("explanation", "")) or None

    async def _post(self, client, path: str, request: dict, job: FallbackJob,
                    counter_override: str | None = None) -> dict | None:
        import httpx

        def fail(counter: str, message: str) -> None:
            if counter_override is not None:
                self.metrics.bump(counter_override)
                self.metrics.last_error = message
                LOGGER.warning("vlm explain skipped (%s) stream=%s frame=%s: %s",
                               counter, job.stream_id, job.frame_id, message)
            else:
                self._fail(counter, message, job)

        try:
            response = await client.post(path, json=request)
        except httpx.TimeoutException as exc:
            fail("timeout", f"{path} timed out after {self.config.timeout_ms} ms: {exc}")
            return None
        except httpx.HTTPError as exc:
            fail("transport_error", f"{path} transport error: {exc}")
            return None

        if response.status_code == 503:
            fail("unavailable", f"{path} 503 vlm_unavailable")
            return None
        if response.status_code >= 400:
            fail("backend_error", f"{path} HTTP {response.status_code}")
            return None
        try:
            return response.json()
        except ValueError as exc:
            fail("backend_error", f"{path} body not JSON: {exc}")
            return None

    async def _probe(self, client) -> bool:
        import httpx

        try:
            response = await client.get("/healthz")
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    def _fail(self, counter: str, message: str, job: FallbackJob) -> None:
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
            encode = _default_encode_jpeg
        return base64.b64encode(
            encode(frame, self.config.jpeg_quality)).decode("ascii")


def _default_encode_jpeg(frame, quality: int) -> bytes:
    """默认编码器。cv2 只在 worker 线程里 import，core 层不硬依赖它。"""
    import cv2

    ok, buffer = cv2.imencode(".jpg", frame,
                              [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not ok:
        raise RuntimeError("cv2.imencode failed")
    return buffer.tobytes()


def maybe_fallback(client: "VlmFallbackClient | None", config: VlmConfig,
                   prediction, frame, event: dict, publish: Callable[[dict], Any],
                   device: str = "", classes: Sequence[str] = MATERIAL_CLASSES,
                   ) -> str | None:
    """接线点：投递一次兜底请求 + 把已完成的旁路事件发出去。两步都不阻塞。

    调用方（未来的 app / 任何一条运行时循环）在**主事件已经发出去之后**调它一次，
    参数里的 `event` 就是那条主事件。返回本帧的触发原因，没触发返回 None。

    这里只做 put_nowait / get_nowait，没有任何网络调用——把它放进帧循环是安全的，
    把 `VlmFallbackClient` 的 HTTP 调用放进帧循环则不是。
    """
    if client is None:
        return None
    trigger = fallback_trigger(prediction.ranked, config)
    if trigger is not None:
        client.submit(FallbackJob(
            stream_id=event["stream_id"], frame_id=event["frame_id"],
            frame=frame, primary_ranked=list(prediction.ranked),
            trigger=trigger, device=device or str(event.get("device", "")),
            taxonomy_version=str(event.get("taxonomy_version",
                                           "material8/china4-v1")),
            classes=tuple(classes)))
    for payload in client.drain():
        publish(payload)
    return trigger

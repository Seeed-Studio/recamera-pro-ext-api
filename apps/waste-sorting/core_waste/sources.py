"""视频源采集。RTSP / USB / 视频文件，每路一个独占采集线程。

spec §5「单线程独占采集」：`cv2.VideoCapture` 只在它自己的线程里被调用，
其它线程一律通过 `read()` 拿已解码好的帧，不共享 capture 句柄。

实时源（rtsp/usb）只保留最新帧，堆积就丢，保证判定跟得上产线；
文件源不丢帧（评测要可复现），采集线程在下游没取走时阻塞。
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Any

import numpy as np

from .config import infer_kind
from .types import FrameMeta

LIVE_KINDS = ("rtsp", "usb")


@dataclass(frozen=True)
class SourceSpec:
    """config 里的一条 `sources[]`。"""

    stream_id: str
    uri: str
    kind: str
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    loop: bool = False

    @classmethod
    def from_config(cls, entry: dict[str, Any]) -> "SourceSpec":
        uri = str(entry["uri"])
        return cls(
            stream_id=str(entry["stream_id"]),
            uri=uri,
            kind=str(entry.get("kind") or infer_kind(uri)),
            width=entry.get("width"),
            height=entry.get("height"),
            fps=entry.get("fps"),
            loop=bool(entry.get("loop", False)),
        )

    @property
    def is_live(self) -> bool:
        return self.kind in LIVE_KINDS


def open_capture(spec: SourceSpec):
    """按 kind 打开 cv2.VideoCapture。厂商/后端差异都收敛在这里。"""
    import cv2

    if spec.kind == "usb":
        index = int(spec.uri.rsplit("video", 1)[-1]) if "video" in spec.uri \
            else int(spec.uri)
        capture = cv2.VideoCapture(index, cv2.CAP_V4L2)
    else:
        capture = cv2.VideoCapture(spec.uri)
    if not capture.isOpened():
        raise SourceError(f"cannot open source {spec.stream_id}: {spec.uri}")
    if spec.width:
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, int(spec.width))
    if spec.height:
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, int(spec.height))
    if spec.fps and spec.is_live:
        capture.set(cv2.CAP_PROP_FPS, float(spec.fps))
    if spec.is_live:
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return capture


class SourceError(RuntimeError):
    """采集源打开失败或读取连续失败。"""


class FrameSource:
    """一路视频源。`start()` 起独占采集线程，`read()` 从线程取帧。

    `capture_factory` 让单测可以注入假 capture，不依赖真实摄像头/文件。
    """

    def __init__(self, spec: SourceSpec, capture_factory=open_capture,
                 max_read_failures: int = 30) -> None:
        self.spec = spec
        self._capture_factory = capture_factory
        self._max_read_failures = max_read_failures
        self._queue: queue.Queue = queue.Queue(maxsize=1 if spec.is_live else 2)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._frame_id = 0
        self.frames_captured = 0
        self.frames_dropped = 0
        self.loops = 0
        self.finished = False
        self.error: str | None = None

    # -- 生命周期 ---------------------------------------------------------
    def start(self) -> "FrameSource":
        if self._thread is not None:
            raise RuntimeError("source already started")
        self._thread = threading.Thread(
            target=self._run, name=f"capture-{self.spec.stream_id}", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        self._thread = None

    # -- 采集线程 ---------------------------------------------------------
    def _run(self) -> None:
        try:
            capture = self._capture_factory(self.spec)
        except Exception as exc:  # noqa: BLE001 - 线程里必须把异常带出来
            self.error = str(exc)
            self.finished = True
            return
        interval = 1.0 / float(self.spec.fps) if self.spec.fps else 0.0
        failures = 0
        next_deadline = time.monotonic()
        try:
            while not self._stop.is_set():
                ok, frame = capture.read()
                if not ok or frame is None:
                    if self.spec.loop and not self.spec.is_live:
                        # 文件放完就重开：72 h soak 需要一段连续输入，
                        # 否则源结束进程退出，容器 restart 会把"进程重启"这个
                        # soak 指标污染掉。
                        capture.release()
                        capture = self._capture_factory(self.spec)
                        self.loops += 1
                        continue
                    if self.spec.is_live:
                        failures += 1
                        if failures >= self._max_read_failures:
                            self.error = (f"{self.spec.stream_id}: "
                                          f"{failures} consecutive read failures")
                            break
                        time.sleep(0.05)
                        continue
                    break  # 文件读完
                failures = 0
                meta = FrameMeta(
                    stream_id=self.spec.stream_id,
                    frame_id=self._frame_id,
                    width=int(frame.shape[1]),
                    height=int(frame.shape[0]),
                    capture_ns=time.monotonic_ns(),
                )
                self._frame_id += 1
                self.frames_captured += 1
                self._offer(frame, meta)
                if interval > 0.0 and not self.spec.is_live:
                    next_deadline += interval
                    delay = next_deadline - time.monotonic()
                    if delay > 0:
                        time.sleep(delay)
                    else:
                        next_deadline = time.monotonic()
        except Exception as exc:  # noqa: BLE001 - 采集中途失败也必须带出来
            # 不接这一层的话，`capture.read()` 中途抛异常只会打到线程
            # excepthook 上：`error` 仍是 None、`exhausted` 变 True，
            # 与「文件正常放完」完全一样，健康接口看不出这路源已经死了。
            self.error = f"{self.spec.stream_id}: {exc}"
        finally:
            self.finished = True
            try:
                capture.release()
            except Exception:  # noqa: BLE001 - 释放失败不该盖住真正的错误
                pass

    def _offer(self, frame: np.ndarray, meta: FrameMeta) -> None:
        """实时源丢旧帧，文件源等下游。"""
        if self.spec.is_live:
            while True:
                try:
                    self._queue.put_nowait((frame, meta))
                    return
                except queue.Full:
                    try:
                        self._queue.get_nowait()
                        self.frames_dropped += 1
                    except queue.Empty:
                        pass
        while not self._stop.is_set():
            try:
                self._queue.put((frame, meta), timeout=0.2)
                return
            except queue.Full:
                continue

    # -- 消费端 -----------------------------------------------------------
    def read(self, timeout: float = 1.0) -> tuple[np.ndarray, FrameMeta] | None:
        """取一帧；超时或源已结束返回 None。"""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    @property
    def exhausted(self) -> bool:
        """采集线程已结束且缓冲取空。"""
        return self.finished and self._queue.empty()

    def stats(self) -> dict[str, Any]:
        return {
            "stream_id": self.spec.stream_id,
            "kind": self.spec.kind,
            "uri": self.spec.uri,
            "frames_captured": self.frames_captured,
            "frames_dropped": self.frames_dropped,
            "loops": self.loops,
            "finished": self.finished,
            "error": self.error,
        }


def build_sources(config: dict[str, Any], capture_factory=open_capture
                  ) -> list[FrameSource]:
    """按 config 的 `sources[]` 建全部源，不 start。"""
    return [FrameSource(SourceSpec.from_config(entry), capture_factory)
            for entry in config["sources"]]

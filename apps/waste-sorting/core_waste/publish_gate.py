"""「这一帧该不该发事件」的状态机。

`platforms/cvi/waste-sorting/main/waste_publish_gate.h` 的逐条 Python 移植：
reCamera 上是 C++，Pi/Orin 上是 Python，两边必须是同一个状态机，否则同一段
输入在两个平台上会发出不同条数的事件，跨平台对比表就失去意义。

三个触发源与 C++ 版一致：

  边沿   稳定类别变了 -> 发
  心跳   稳定类别没变，但离上次成功发布超过 republish_ms -> 发
  补发   上一次发布失败 -> 每 retry_ms 重试，直到成功

关键约束同样保留：`announced` 与 `last_publish_ms` 只在**发布成功**之后前进。
发布失败还把 announced 推到当前类别的话，republish_ms=0（只在切换时发）的
部署里，broker 断线期间那次切换就永远补不回来了。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PublishGate:
    debounce_frames: int = 3
    min_confidence: float = 0.50
    republish_ms: int = 2000       # 0 = 只在稳定类别切换时发
    retry_ms: int = 500            # 发布失败后的重试间隔

    candidate: int = -1            # 当前候选类别
    stable_run: int = 0            # 候选已连续出现的帧数
    announced: int = -1            # 最近**成功**发出去的稳定类别
    last_publish_ms: int = 0       # 最近一次成功发布的时刻
    pending: bool = False          # 有一条稳定判定还没发成功
    last_attempt_ms: int = 0       # 最近一次尝试发布的时刻（成功与否都算）
    failures: int = 0

    @classmethod
    def from_config(cls, config: dict) -> "PublishGate":
        """从 config 的 `rules` / `trigger` 两段推导门控参数。

        `trigger.debounce_ms` 是「两次触发之间的最小间隔」，语义与去抖帧数
        不同，不在这里读；连续模式下的去抖帧数来自 `rules.consecutive_frames`。
        """
        rules = config.get("rules", {})
        return cls(
            debounce_frames=int(rules.get("consecutive_frames", 1)),
            min_confidence=float(rules.get("min_confidence", 0.5)),
        )

    def stable(self) -> bool:
        return self.stable_run >= self.debounce_frames

    def on_frame(self, cls_id: int, confidence: float, now_ms: int) -> bool:
        """每帧调一次，返回 True 表示这一帧应该发布。"""
        confident = confidence >= self.min_confidence
        if confident and cls_id == self.candidate:
            self.stable_run += 1
        else:
            self.candidate = cls_id if confident else -1
            self.stable_run = 1 if confident else 0
        if not self.stable():
            return False
        # 补发优先于心跳：还没发成功的那条判定先补上。
        if self.pending:
            return now_ms - self.last_attempt_ms >= self.retry_ms
        if cls_id != self.announced:
            return True
        return self.republish_ms > 0 and \
            now_ms - self.last_publish_ms >= self.republish_ms

    def on_publish_result(self, ok: bool, cls_id: int, now_ms: int) -> None:
        """发布结果回填。ok=False 时 announced 不动，pending 置位等下一轮补发。"""
        self.last_attempt_ms = now_ms
        if ok:
            self.announced = cls_id
            self.last_publish_ms = now_ms
            self.pending = False
        else:
            self.pending = True
            self.failures += 1

    def stats(self) -> dict:
        return {
            "debounce_frames": self.debounce_frames,
            "min_confidence": self.min_confidence,
            "candidate": self.candidate,
            "stable_run": self.stable_run,
            "announced": self.announced,
            "pending": self.pending,
            "failures": self.failures,
        }

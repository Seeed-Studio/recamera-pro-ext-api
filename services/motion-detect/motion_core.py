"""motion_core -- 窗口化平方差移动检测核心（守护进程版）。

只依赖 numpy。与 App Center 版的差别：
  - 输入直接是 NV12 的 Y 平面（frame.array，零拷贝单通道灰度）；
  - 不做连通域/画框，只输出 是否运动 + 强度 + 变化占比。

720p 实机基准（RV1126B A53 单核，降采样到 160x90）：numpy 两段式块均值
≈5.8ms，cv2.INTER_AREA ≈4.6ms——差距不足 0.6% CPU（5fps），而 import
cv2 要多占 ≈18MB RSS，故采用纯 numpy 两段式（朴素 4D 均值 ≈17.6ms 已弃用）。

算法（参数全部可调）：
  基准画面 = 早于 (now - baseline_gap_ms) 的最近 baseline_frames 个样本的均值
  当前画面 = 最近 current_frames 个样本的均值
  平方差图 score_map = (current - baseline) ** 2        (0..65025)
  变化像素 = score_map > sq_diff_threshold
  运动判定 = 变化像素占比 >= min_changed_ratio
"""
from collections import deque

import numpy as np


class MotionCore:
    """单线程驱动的检测核心。所有 evaluate 参数每次传入，天然支持热更新。"""

    def __init__(self, analysis_width: int = 160):
        self.analysis_width = max(16, int(analysis_width))
        self.samples = deque()          # [(pts_seconds, small_gray_uint8)]

    # -- 采样缓冲 ------------------------------------------------------- #
    def downscale(self, gray: np.ndarray) -> np.ndarray:
        """Y 平面灰度图 -> 拥有独立所有权的 (ah, aw) 小图。

        numpy 块均值：先裁到分析尺寸的整数倍，再 reshape 求均值。
        只在采样帧上调用；返回的小图可跨帧安全持有。
        """
        h, w = gray.shape[:2]
        aw = min(self.analysis_width, w)
        ah = max(1, round(h * aw / w))
        fy, fx = h // ah, w // aw            # 每个输出像素对应的源块 (fy x fx)
        if fy < 1 or fx < 1:
            return gray[:ah, :aw].copy()
        block = gray[: ah * fy, : aw * fx]
        # 两段式均值（行→列）比一次性 4D 均值快约 3 倍（缓存友好）
        rows = block.reshape(ah, fy, aw * fx).mean(axis=1, dtype=np.float32)
        mean = rows.reshape(ah, aw, fx).mean(axis=2, dtype=np.float32)
        return np.clip(mean, 0, 255).astype(np.uint8)

    def push(self, pts: float, gray_small: np.ndarray) -> None:
        self.samples.append((float(pts), gray_small))

    def trim(self, now: float, retention_s: float) -> None:
        cutoff = float(now) - max(0.0, float(retention_s))
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()

    def __len__(self) -> int:
        return len(self.samples)

    # -- 判定 ----------------------------------------------------------- #
    def evaluate(self, now: float, *, baseline_gap_ms, baseline_frames,
                 current_frames, sq_diff_threshold, min_changed_ratio):
        """返回 None = 基准样本不足（预热期）；否则返回判定结果 dict。"""
        now = float(now)
        gap_s = max(0.0, float(baseline_gap_ms)) / 1000.0
        cutoff = now - gap_s
        baseline_pool = [(p, g) for (p, g) in self.samples if p <= cutoff]
        n_base = max(1, int(baseline_frames))
        n_cur = max(1, int(current_frames))
        if len(baseline_pool) < n_base or len(self.samples) < n_cur:
            return None

        base = np.mean(np.stack([g for _, g in baseline_pool[-n_base:]]),
                       axis=0, dtype=np.float32)
        cur = np.mean(np.stack([g for _, g in list(self.samples)[-n_cur:]]),
                      axis=0, dtype=np.float32)

        diff = cur - base                       # float32, -255..255
        score_map = diff * diff                 # float32, 0..65025
        changed = int((score_map > float(sq_diff_threshold)).sum())
        total = score_map.size
        changed_ratio = changed / float(total)
        active = changed_ratio >= float(min_changed_ratio)

        strength = 0.0
        if changed:
            strength = float(np.sqrt(
                score_map[score_map > float(sq_diff_threshold)].mean()) / 255.0)

        return {
            "active": active,
            "score": round(min(1.0, strength), 4),
            "changed_ratio": round(changed_ratio, 5),
            "changed_pixels": changed,
        }


def retention_seconds(sample_interval_ms, baseline_gap_ms,
                      baseline_frames, current_frames) -> float:
    """样本缓冲需要覆盖：时间间隔 + 基准窗口 + 当前窗口 + 余量。"""
    interval_s = max(0.02, float(sample_interval_ms) / 1000.0)
    window_s = interval_s * (int(baseline_frames) + int(current_frames) + 4)
    return max(0.0, float(baseline_gap_ms)) / 1000.0 + window_s

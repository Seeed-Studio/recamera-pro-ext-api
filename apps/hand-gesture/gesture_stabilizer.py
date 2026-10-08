"""手势标签时间稳定器：轻量 track + 滑动窗口多数投票 + 滞回。

解决临界姿态（手偏远/偏侧/轻微移动）时 None ↔ 手势逐帧闪烁的问题：
同一只手的标签进入滑动窗口，窗口内多数标签成为稳定输出；不足最小票数
或平票时保持上一个稳定标签（滞回），不随单帧波动切换。

track 匹配按 box 中心归一化距离最近邻，不引入外观/运动模型；手消失
超过 timeout 帧后删除 track，重新出现时从零积累，防止旧标签污染。

window=1 时逐帧直通（等价关闭）。None 是合法类别，同样参与投票。
"""
from __future__ import annotations

import math
from collections import Counter, deque


class _Track:
    __slots__ = ("tid", "cx", "cy", "votes", "stable", "last")

    def __init__(self, tid: int, cx: float, cy: float, frame_idx: int):
        self.tid = tid
        self.cx, self.cy = cx, cy
        self.votes = deque()
        self.stable = "None"
        self.last = frame_idx


class GestureStabilizer:
    """对 app 每帧 result rows 做时间多数投票，原地改写稳定标签。"""

    def __init__(self, window: int = 5, min_votes: int = 0,
                 match_dist: float = 0.15, timeout: int = 15):
        self.window = max(1, int(window))
        # min_votes=0 -> 自动多数票（> 半数）
        self.min_votes = int(min_votes) if min_votes else self.window // 2 + 1
        self.match_dist = float(match_dist)
        self.timeout = int(timeout)
        self._tracks: list[_Track] = []
        self._next_tid = 0

    def update(self, rows: list, frame_idx: int,
               img_w: int, img_h: int) -> list:
        diag = math.hypot(img_w, img_h) or 1.0
        self._tracks = [t for t in self._tracks
                        if frame_idx - t.last <= self.timeout]
        used = set()
        for r in rows:
            x1, y1, x2, y2 = r["box"]
            cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
            best, best_d = None, self.match_dist
            for t in self._tracks:
                if t.tid in used:
                    continue
                d = math.hypot(cx - t.cx, cy - t.cy) / diag
                if d < best_d:
                    best, best_d = t, d
            if best is None:
                best = _Track(self._next_tid, cx, cy, frame_idx)
                self._next_tid += 1
                self._tracks.append(best)
            used.add(best.tid)
            best.cx, best.cy, best.last = cx, cy, frame_idx

            cur = r.get("gesture", "None")
            best.votes.append(cur)
            while len(best.votes) > self.window:
                best.votes.popleft()
            counts = Counter(best.votes)
            top, n = counts.most_common(1)[0]
            if n >= self.min_votes:
                best.stable = top

            r["gesture_raw"] = cur
            r["vote_count"] = int(counts.get(best.stable, 0))
            r["gesture"] = best.stable
            r["label"] = best.stable
            r["class_name"] = best.stable
        return rows

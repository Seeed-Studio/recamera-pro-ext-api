"""Greedy IoU tracker.

Shared verbatim with ``platforms/generic`` so the two platforms cannot drift in
ID semantics. Originally ported from ``fall-detection/platforms/rknn``. The
hub only maintains zone/loiter/line state for ``track_id >= 1``
(HUB_SPEC 2.1), so a detector feeding hub-side rules must track. IDs start at
1 and increase monotonically within a session; 0 is never emitted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Track:
    track_id: int
    box: list[float]
    last_seen: float
    score: float
    missed: int = 0


class IoUTracker:
    def __init__(self, threshold: float = 0.2, max_lost_sec: float = 0.75) -> None:
        self.threshold = threshold
        self.max_lost_sec = max_lost_sec
        self.tracks: dict[int, Track] = {}
        self.next_id = 1
        self.expired_ids: list[int] = []

    @staticmethod
    def iou(a: list[float], b: list[float]) -> float:
        x1, y1 = max(a[0], b[0]), max(a[1], b[1])
        x2, y2 = min(a[2], b[2]), min(a[3], b[3])
        inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
        aa = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
        ab = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
        return inter / max(aa + ab - inter, 1e-9)

    def update(self, detections: list, now: float) -> list[tuple[Track, Optional[object]]]:
        """Associate ``detections`` (objects with ``.box`` xyxy and ``.score``).

        Returns ``(track, detection)`` pairs for tracks matched this frame, in
        ascending track_id order. Coasting tracks are kept alive internally but
        are not published -- the contract's detections array describes what was
        seen in this frame.
        """
        self.expired_ids = [
            tid for tid, tr in self.tracks.items() if now - tr.last_seen > self.max_lost_sec
        ]
        for tid in self.expired_ids:
            del self.tracks[tid]
        for tr in self.tracks.values():
            tr.missed += 1

        pairs = sorted(
            (self.iou(tr.box, det.box), tid, di)
            for tid, tr in self.tracks.items()
            for di, det in enumerate(detections)
        )
        used_t: set[int] = set()
        used_d: set[int] = set()
        matched: dict[int, object] = {}
        for score, tid, di in reversed(pairs):
            if score < self.threshold or tid in used_t or di in used_d:
                continue
            det = detections[di]
            tr = self.tracks[tid]
            tr.box, tr.last_seen, tr.score, tr.missed = det.box, now, det.score, 0
            used_t.add(tid)
            used_d.add(di)
            matched[tid] = det

        for di, det in enumerate(detections):
            if di in used_d:
                continue
            tid = self.next_id
            self.next_id += 1
            self.tracks[tid] = Track(tid, det.box, now, det.score, 0)
            matched[tid] = det

        return [(self.tracks[tid], matched[tid]) for tid in sorted(matched)]

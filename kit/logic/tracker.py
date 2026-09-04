"""
Multi-object tracker (IoU association + velocity prediction + track lifecycle).

Ported from the first-gen retail-vision C++ `person_tracker.cpp`
(PersonTracker::update / matchDetections / updateVelocity). Model-free and
app-agnostic: it consumes plain detection boxes (pixel xyxy) and emits stable
per-object `Track`s with an id, a velocity estimate and a foot point, frame over
frame. Zone / line / dwell counting lives in `kit.logic.zones`; this module only
owns identity and motion so any "detect + track + count" app reuses it.

Boxes are normalised to [0,1] internally (centre form cx,cy,w,h) so the same
IoU / distance thresholds behave the same at any capture resolution. `speed_px_s`
is expressed in a nominal 640x640 frame so the first-gen dwell thresholds
(px/s) carry over unchanged.

Association strategy (two passes, faithful to the C++):
  1. IoU match, tracks tried oldest-first, each detection used once. Lost tracks
     have their box predicted forward by their velocity before matching.
  2. Centre-distance fallback for still-unmatched, recently-lost tracks.
Unmatched detections spawn new tracks; unmatched tracks age out (edge tracks
faster than centre tracks, since edge losses are usually real exits).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from kit.logic.geometry import iou_xyxy

# Nominal frame the px/s speed is expressed in (matches first-gen 640x640 head).
_NOMINAL = 640.0


@dataclass
class TrackerConfig:
    iou_threshold: float = 0.2          # min IoU to associate (pass 1)
    dist_threshold: float = 0.15        # max normalised centre dist (pass 2)
    max_lost_frames_center: int = 90    # ~3 s @30fps: keep occluded centre tracks
    max_lost_frames_edge: int = 15      # ~0.5 s: drop edge tracks fast (real exit)
    dist_fallback_max_lost: int = 5     # pass-2 only for tracks lost <= this
    vel_alpha: float = 0.08             # velocity EMA (steady)
    vel_alpha_sudden: float = 0.6       # velocity EMA (sudden start/stop)
    velocity_zero_threshold: float = 3.0  # px/s below which velocity snaps to 0
    edge_margin: float = 0.15           # fraction of frame treated as "edge"
    min_frames_for_count: int = 10      # min age before appearance-count exits
    assumed_fps: float = 15.0           # for velocity-based lost-track prediction

    def clamp(self) -> "TrackerConfig":
        self.iou_threshold = min(0.95, max(0.0, self.iou_threshold))
        self.dist_threshold = max(0.0, self.dist_threshold)
        self.max_lost_frames_center = max(0, int(self.max_lost_frames_center))
        self.max_lost_frames_edge = max(0, int(self.max_lost_frames_edge))
        self.edge_margin = min(0.49, max(0.0, self.edge_margin))
        self.assumed_fps = max(1.0, self.assumed_fps)
        return self


@dataclass
class Track:
    """One tracked object. Coordinates normalised to [0,1]; foot = bbox
    bottom-centre (where the person stands), used for zone / line tests."""
    track_id: int
    cx: float
    cy: float
    w: float
    h: float
    score: float = 0.0
    velocity_x: float = 0.0             # normalised units / second
    velocity_y: float = 0.0
    speed_px_s: float = 0.0             # px/s in the nominal 640 frame
    first_seen: float = 0.0
    last_seen: float = 0.0
    frames_tracked: int = 0
    lost_frames: int = 0
    foot: Tuple[float, float] = (0.0, 0.0)       # current foot point (norm)
    prev_foot: Tuple[float, float] = (0.0, 0.0)  # last frame's foot point
    near_edge: bool = False
    # Index into the `dets` list handed to the most recent `update()`, or -1 if
    # this track was not matched to a detection this frame. Cascade apps (a
    # second-stage classifier running per detection) need to attach a stable
    # identity to the detection they are about to crop; without this they would
    # have to re-associate by IoU right after the tracker already did it.
    det_index: int = -1

    @property
    def xyxy_norm(self) -> List[float]:
        return [self.cx - self.w / 2, self.cy - self.h / 2,
                self.cx + self.w / 2, self.cy + self.h / 2]


def _foot(cx: float, cy: float, h: float) -> Tuple[float, float]:
    return (cx, cy + h * 0.5)


class Tracker:
    """Greedy IoU tracker with velocity prediction and a two-pass matcher."""

    def __init__(self, config: Optional[TrackerConfig] = None) -> None:
        self.cfg = (config or TrackerConfig()).clamp()
        self._tracks: Dict[int, Track] = {}
        self._next_id: int = 1
        self._last_t: float = -1.0
        # Ids created / removed during the most recent update() call. Lets an app
        # do appearance-based entry/exit counting (new id = entry, removed id =
        # exit) faithfully -- removal fires only after the lost budget, so brief
        # occlusions do NOT produce a spurious exit + re-entry.
        self.new_ids: List[int] = []
        self.removed_ids: List[int] = []

    # -- geometry helpers ------------------------------------------------- #
    def _is_near_edge(self, cx: float, cy: float) -> bool:
        m = self.cfg.edge_margin
        return cx < m or cx > (1.0 - m) or cy < m or cy > (1.0 - m)

    def _predict(self, tr: Track) -> Tuple[float, float, float, float]:
        """Box centre predicted forward by velocity while a track is lost."""
        cx, cy = tr.cx, tr.cy
        if tr.lost_frames > 0 and tr.speed_px_s > 1.0:
            dt = tr.lost_frames / self.cfg.assumed_fps
            cx += tr.velocity_x * dt
            cy += tr.velocity_y * dt
        return cx, cy, tr.w, tr.h

    # -- association ------------------------------------------------------ #
    def _match(self, dets: Sequence[Tuple[float, float, float, float, float]]
               ) -> List[Tuple[int, int]]:
        """dets: list of (cx,cy,w,h,score) normalised. Returns [(track_id, det_idx)]."""
        matches: List[Tuple[int, int]] = []
        if not self._tracks or not dets:
            return matches

        # Prefer older tracks (more frames tracked) when contending for a det.
        order = sorted(self._tracks.keys(),
                       key=lambda tid: -self._tracks[tid].frames_tracked)
        used = [False] * len(dets)

        # Pass 1: IoU on velocity-predicted boxes.
        unmatched: List[int] = []
        for tid in order:
            tr = self._tracks[tid]
            pcx, pcy, pw, ph = self._predict(tr)
            pbox = [pcx - pw / 2, pcy - ph / 2, pcx + pw / 2, pcy + ph / 2]
            best_iou = self.cfg.iou_threshold
            best = -1
            for d, det in enumerate(dets):
                if used[d]:
                    continue
                dcx, dcy, dw, dh, _ = det
                dbox = [dcx - dw / 2, dcy - dh / 2, dcx + dw / 2, dcy + dh / 2]
                iou = iou_xyxy(pbox, dbox)
                if iou > best_iou:
                    best_iou = iou
                    best = d
            if best >= 0:
                matches.append((tid, best))
                used[best] = True
            else:
                unmatched.append(tid)

        # Pass 2: centre-distance fallback for recently-lost tracks.
        for tid in unmatched:
            tr = self._tracks[tid]
            if tr.lost_frames > self.cfg.dist_fallback_max_lost:
                continue
            pcx, pcy, _, _ = self._predict(tr)
            best_dist = self.cfg.dist_threshold
            best = -1
            for d, det in enumerate(dets):
                if used[d]:
                    continue
                dist = math.hypot(det[0] - pcx, det[1] - pcy)
                if dist < best_dist:
                    best_dist = dist
                    best = d
            if best >= 0:
                matches.append((tid, best))
                used[best] = True

        return matches

    def _update_velocity(self, tr: Track, ncx: float, ncy: float, dt: float) -> None:
        if dt <= 0.001:
            return
        # Instantaneous velocity in normalised units/s; speed in nominal px/s.
        ivx = (ncx - tr.cx) / dt
        ivy = (ncy - tr.cy) / dt
        instant_px_s = math.hypot(ivx * _NOMINAL, ivy * _NOMINAL)

        prev = tr.speed_px_s
        sudden_stop = prev > 10.0 and (prev - instant_px_s) / prev > 0.5
        sudden_start = prev < 5.0 and instant_px_s > 50.0

        if instant_px_s < self.cfg.velocity_zero_threshold:
            tr.velocity_x = 0.0
            tr.velocity_y = 0.0
            tr.speed_px_s = 0.0
        else:
            a = self.cfg.vel_alpha_sudden if (sudden_stop or sudden_start) \
                else self.cfg.vel_alpha
            tr.velocity_x = (1.0 - a) * tr.velocity_x + a * ivx
            tr.velocity_y = (1.0 - a) * tr.velocity_y + a * ivy
            tr.speed_px_s = math.hypot(tr.velocity_x * _NOMINAL,
                                       tr.velocity_y * _NOMINAL)

    # -- public API ------------------------------------------------------- #
    def update(self, dets: Sequence[dict], t: float,
               frame_w: int, frame_h: int) -> List[Track]:
        """Advance the tracker one frame.

        dets      : detections to track this frame, each {"box":[x1,y1,x2,y2] in
                    ORIGINAL pixels, "score": float}. Pre-filter to the class you
                    want (e.g. person) before calling.
        t         : monotonic timestamp (seconds).
        frame_w/h : original frame size, to normalise boxes.

        Returns the list of currently-VISIBLE tracks (lost_frames == 0), each
        carrying a stable `track_id`, velocity and current/previous foot point.
        """
        dt = (t - self._last_t) if self._last_t >= 0 else 0.0
        self._last_t = t
        fw = float(frame_w) if frame_w else 1.0
        fh = float(frame_h) if frame_h else 1.0

        norm: List[Tuple[float, float, float, float, float]] = []
        for d in dets:
            x1, y1, x2, y2 = d["box"]
            cx = ((x1 + x2) * 0.5) / fw
            cy = ((y1 + y2) * 0.5) / fh
            w = abs(x2 - x1) / fw
            h = abs(y2 - y1) / fh
            norm.append((cx, cy, w, h, float(d.get("score", 0.0))))

        self.new_ids = []
        self.removed_ids = []
        matches = self._match(norm)
        matched_det = [False] * len(norm)
        matched_tid = set()

        for tid, di in matches:
            tr = self._tracks[tid]
            ncx, ncy, nw, nh, sc = norm[di]
            self._update_velocity(tr, ncx, ncy, dt)
            tr.prev_foot = tr.foot
            tr.cx, tr.cy, tr.w, tr.h = ncx, ncy, nw, nh
            tr.foot = _foot(ncx, ncy, nh)
            tr.score = sc
            tr.last_seen = t
            tr.frames_tracked += 1
            tr.lost_frames = 0
            tr.near_edge = self._is_near_edge(ncx, ncy)
            tr.det_index = di
            matched_det[di] = True
            matched_tid.add(tid)

        # Age unmatched tracks; drop when past their edge-aware lost budget.
        to_remove: List[int] = []
        for tid, tr in self._tracks.items():
            if tid in matched_tid:
                continue
            tr.lost_frames += 1
            tr.det_index = -1          # no detection backs this track this frame
            max_lost = (self.cfg.max_lost_frames_edge if tr.near_edge
                        else self.cfg.max_lost_frames_center)
            if tr.lost_frames > max_lost:
                to_remove.append(tid)
        for tid in to_remove:
            del self._tracks[tid]
        self.removed_ids = to_remove

        # Spawn new tracks for unmatched detections.
        for di, seen in enumerate(matched_det):
            if seen:
                continue
            ncx, ncy, nw, nh, sc = norm[di]
            foot = _foot(ncx, ncy, nh)
            tr = Track(track_id=self._next_id, cx=ncx, cy=ncy, w=nw, h=nh,
                       score=sc, first_seen=t, last_seen=t, frames_tracked=1,
                       lost_frames=0, foot=foot, prev_foot=foot,
                       near_edge=self._is_near_edge(ncx, ncy),
                       det_index=di)
            self._tracks[self._next_id] = tr
            self.new_ids.append(self._next_id)
            self._next_id += 1

        return [tr for tr in self._tracks.values() if tr.lost_frames == 0]

    def active_tracks(self) -> List[Track]:
        return [tr for tr in self._tracks.values() if tr.lost_frames == 0]

    @property
    def track_count(self) -> int:
        return sum(1 for tr in self._tracks.values() if tr.lost_frames == 0)

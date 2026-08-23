"""
crayfish-fight -- the app's cross-frame business logic, as pure Python.

Everything here is state that spans frames and decides *when* something is
true; it holds no numpy, no rknn, no kit imports, so `tests/test_logic.py`
exercises it on the Mac without a device. `app.py` owns the per-frame plumbing
(pre / infer / post / crop / emit) and calls into these four objects:

    SexVoter            -- vote a track's sex over its first N gate-passing
                           frames, then stop paying for the classifier.
    ProximityWindow     -- "these two tracks have been near each other for
                           >= min_hits of the last `window` frames" -> the pair
                           is a behaviour-classifier candidate.
    BehaviorStateMachine-- turn a stream of per-frame ROI verdicts into
                           start/stop events with hysteresis, so one bad frame
                           neither raises nor clears an event.
    CaptureQuota        -- rate-limit the data-flywheel dump.

Coordinates in this module are whatever the caller passes in CONSISTENTLY:
`ProximityWindow`/`pair_is_close`/`union_box` are scale-free (they only compare
distances against the boxes' own diagonals), so pixel or normalised input both
work. `app.py` feeds them ORIGINAL-frame pixels, the same space
`kit.runtime.postprocess.detect` returns and the same space the result sink
normalises to [0,1] on the way out.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Class vocabularies. Order is the ULTRALYTICS model's, read off
# `YOLO(best.pt).names` at export time (see tools/export_onnx.py, whose
# export_summary.json records it). Do not reorder: index i here must be the
# i-th logit of the corresponding rknn.
SEX_LABELS = ["female", "male"]              # runs/sex_cls/best.pt      names
BEHAVIOR_LABELS = ["fight", "harass", "none"]  # runs/behavior_cls/best.pt names
SEX_UNKNOWN = "unknown"
# Behaviour labels that constitute a reportable event ("none" is the negative
# class and includes "merely close" and "perspective overlap" -- PLAN §3.2).
BEHAVIOR_EVENT_LABELS = ("fight", "harass")

PairKey = Tuple[int, int]


def pair_key(a: int, b: int) -> PairKey:
    """Order-free key for a track pair."""
    return (a, b) if a <= b else (b, a)


# ---------------------------------------------------------------------------
# geometry (scale-free, no numpy)
# ---------------------------------------------------------------------------
def _wh(box: Sequence[float]) -> Tuple[float, float]:
    return max(0.0, box[2] - box[0]), max(0.0, box[3] - box[1])


def box_iou(a: Sequence[float], b: Sequence[float]) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0.0:
        return 0.0
    aw, ah = _wh(a)
    bw, bh = _wh(b)
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0.0 else 0.0


def box_center(box: Sequence[float]) -> Tuple[float, float]:
    return ((box[0] + box[2]) * 0.5, (box[1] + box[3]) * 0.5)


def box_diag(box: Sequence[float]) -> float:
    w, h = _wh(box)
    return (w * w + h * h) ** 0.5


def center_distance(a: Sequence[float], b: Sequence[float]) -> float:
    ax, ay = box_center(a)
    bx, by = box_center(b)
    return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5


def pair_is_close(a: Sequence[float], b: Sequence[float], k: float) -> bool:
    """★Stage-2 trigger predicate★ (PLAN §1.1): the two boxes overlap at all,
    OR their centres are within `k` x the MEAN of the two box diagonals.

    Scaling the distance by the boxes' own size is what makes one `k` work at
    any capture resolution and at any distance from the camera: a crayfish near
    the glass has a big box and is allowed a proportionally bigger gap.
    """
    if box_iou(a, b) > 0.0:
        return True
    mean_diag = 0.5 * (box_diag(a) + box_diag(b))
    if mean_diag <= 0.0:
        return False
    return center_distance(a, b) < k * mean_diag


def union_box(a: Sequence[float], b: Sequence[float], pad: float = 0.0,
              clip: Optional[Tuple[float, float]] = None) -> List[float]:
    """Union of two boxes, grown by `pad` (a FRACTION of the union's own size).

    `clip=(w, h)` clamps into the frame. `pad=0.15` is the interaction-ROI
    margin from PLAN §1.1 -- context around the two animals (raised claws reach
    outside the body boxes) without diluting them in background water.
    """
    x1 = min(a[0], b[0])
    y1 = min(a[1], b[1])
    x2 = max(a[2], b[2])
    y2 = max(a[3], b[3])
    if pad:
        dw = (x2 - x1) * pad * 0.5
        dh = (y2 - y1) * pad * 0.5
        x1, y1, x2, y2 = x1 - dw, y1 - dh, x2 + dw, y2 + dh
    if clip is not None:
        w, h = clip
        x1 = max(0.0, min(x1, w))
        y1 = max(0.0, min(y1, h))
        x2 = max(0.0, min(x2, w))
        y2 = max(0.0, min(y2, h))
    return [x1, y1, x2, y2]


def to_norm(box: Sequence[float], w: float, h: float) -> List[float]:
    """ORIGINAL-frame pixel xyxy -> normalised [0,1] xyxy.

    The result sink normalises what `emit()` publishes, so app.py hands it
    pixels; this helper exists for anything that must be normalised BEFORE that
    (the capture-sidecar json, so a dumped record is resolution-independent).
    """
    w = float(w) or 1.0
    h = float(h) or 1.0
    return [box[0] / w, box[1] / h, box[2] / w, box[3] / h]


# ---------------------------------------------------------------------------
# 1. sex voting
# ---------------------------------------------------------------------------
@dataclass
class SexVote:
    counts: Dict[str, int] = field(default_factory=dict)
    frames: int = 0
    settled: bool = False
    label: str = SEX_UNKNOWN
    confidence: float = 0.0


class SexVoter:
    """Vote each track's sex over its first `vote_frames` classified frames.

    Why a vote and not one frame: a single 128px crop of a moving animal behind
    glass flips label frame to frame. Why STOP after N: sex does not change, so
    after the vote the classifier is pure cost -- this is what keeps the second
    model off the per-frame budget (PLAN §3.2 "只在新 track 出现后前 N 帧投票
    一次，之后沿用").

    A frame whose softmax max is below `min_conf` votes `unknown` rather than
    being discarded: an animal that is genuinely unidentifiable from this angle
    should end up labelled unknown, which is an allowed customer label, instead
    of letting a handful of lucky frames speak for it.
    """

    def __init__(self, vote_frames: int = 10, min_conf: float = 0.65) -> None:
        self.vote_frames = max(1, int(vote_frames))
        self.min_conf = float(min_conf)
        self._votes: Dict[int, SexVote] = {}

    def needs_vote(self, track_id: int) -> bool:
        v = self._votes.get(track_id)
        return v is None or not v.settled

    def add(self, track_id: int, label: str, confidence: float) -> SexVote:
        v = self._votes.setdefault(track_id, SexVote())
        if v.settled:
            return v
        eff = label if confidence >= self.min_conf else SEX_UNKNOWN
        v.counts[eff] = v.counts.get(eff, 0) + 1
        v.frames += 1
        self._recompute(v)
        if v.frames >= self.vote_frames:
            v.settled = True
        return v

    @staticmethod
    def _recompute(v: SexVote) -> None:
        if not v.counts:
            v.label, v.confidence = SEX_UNKNOWN, 0.0
            return
        # Majority vote, with `unknown` losing every tie: it only wins when it
        # is STRICTLY the largest bucket, so a couple of low-confidence frames
        # cannot erase an otherwise consistent verdict. A genuine female/male
        # tie is what unknown is for, so that one does resolve to unknown.
        best = max(v.counts.values())
        top = [k for k, c in v.counts.items() if c == best]
        real = [k for k in top if k != SEX_UNKNOWN]
        v.label = real[0] if len(real) == 1 else SEX_UNKNOWN
        v.confidence = round(v.counts.get(v.label, 0) / max(1, v.frames), 4)

    def verdict(self, track_id: int) -> SexVote:
        return self._votes.get(track_id, SexVote())

    def sex_of(self, track_id: int) -> str:
        return self.verdict(track_id).label

    def drop(self, track_ids: Iterable[int]) -> None:
        for tid in track_ids:
            self._votes.pop(tid, None)

    def __len__(self) -> int:
        return len(self._votes)


# ---------------------------------------------------------------------------
# 2. proximity trigger
# ---------------------------------------------------------------------------
class ProximityWindow:
    """Sliding-window "these two have been together" detector.

    One boolean deque per track pair. A pair is TRIGGERED on a frame when at
    least `min_hits` of the last `window` observations were close (default 5/8,
    ~0.5 s at 10-15 fps -- PLAN §1.1). The window is what keeps a single frame
    of two animals crossing paths from spending a classifier inference, and the
    "of the last N" form (rather than N consecutive) survives a dropped
    detection mid-approach.

    Pairs not observed for `forget_after` calls are dropped so the dict cannot
    grow with track ids.
    """

    def __init__(self, window: int = 8, min_hits: int = 5,
                 forget_after: int = 30) -> None:
        self.window = max(1, int(window))
        self.min_hits = max(1, min(int(min_hits), self.window))
        self.forget_after = max(1, int(forget_after))
        self._hist: Dict[PairKey, Deque[bool]] = {}
        self._idle: Dict[PairKey, int] = {}
        self._step = 0

    def update(self, observations: Dict[PairKey, bool]) -> List[PairKey]:
        """Feed this frame's per-pair closeness. Returns the triggered pairs.

        `observations` must contain EVERY pair both of whose tracks were visible
        this frame -- including the False ones, so a pair that drifted apart
        actually decays out of its window.
        """
        self._step += 1
        for key, close in observations.items():
            dq = self._hist.get(key)
            if dq is None:
                dq = deque(maxlen=self.window)
                self._hist[key] = dq
            dq.append(bool(close))
            self._idle[key] = 0

        triggered: List[PairKey] = []
        for key in list(self._hist):
            if key not in observations:
                self._idle[key] = self._idle.get(key, 0) + 1
                if self._idle[key] >= self.forget_after:
                    self._hist.pop(key, None)
                    self._idle.pop(key, None)
                continue
            if sum(self._hist[key]) >= self.min_hits:
                triggered.append(key)
        triggered.sort()
        return triggered

    def hits(self, key: PairKey) -> int:
        return sum(self._hist.get(key, ()))

    def drop_track(self, track_id: int) -> None:
        for key in [k for k in self._hist if track_id in k]:
            self._hist.pop(key, None)
            self._idle.pop(key, None)

    def __len__(self) -> int:
        return len(self._hist)


# ---------------------------------------------------------------------------
# 3. behaviour event state machine
# ---------------------------------------------------------------------------
@dataclass
class BehaviorState:
    label: Optional[str] = None      # currently REPORTED label (None = idle)
    candidate: Optional[str] = None  # label accumulating towards a report
    streak: int = 0                  # consecutive candidate frames
    misses: int = 0                  # consecutive non-confirming frames while active
    started_at: float = 0.0
    last_at: float = 0.0
    confidence: float = 0.0
    frames: int = 0                  # confirming frames in the current event


class BehaviorStateMachine:
    """Per-pair debounce: raw ROI verdicts -> start / update / end events.

    Two asymmetric thresholds, on purpose:

      * RAISE needs `min_streak` consecutive frames of the same event label at
        >= `min_conf` (default 3). The classifier's top-1 on a 224 ROI is 0.788
        on a small-sample val set, so one frame is not evidence.
      * RELEASE needs `release_frames` consecutive non-confirming frames
        (default 3, i.e. hysteresis). A fight where the animals separate for a
        tenth of a second is one fight, not three.

    `none` is the negative class: it never raises, and while an event is active
    it counts as a miss.

    Returns None when nothing changed and a dict when an event begins, changes
    label, or ends. The caller decides what to publish; this decides *when*.
    """

    def __init__(self, min_streak: int = 3, release_frames: int = 3,
                 min_conf: float = 0.5) -> None:
        self.min_streak = max(1, int(min_streak))
        self.release_frames = max(1, int(release_frames))
        self.min_conf = float(min_conf)
        self._states: Dict[PairKey, BehaviorState] = {}

    def state(self, key: PairKey) -> BehaviorState:
        return self._states.setdefault(key, BehaviorState())

    def active_label(self, key: PairKey) -> Optional[str]:
        return self._states.get(key, BehaviorState()).label

    def update(self, key: PairKey, label: str, confidence: float,
               t: float) -> Optional[dict]:
        st = self.state(key)
        confirming = (label in BEHAVIOR_EVENT_LABELS
                      and confidence >= self.min_conf)

        if confirming:
            st.streak = st.streak + 1 if st.candidate == label else 1
            st.candidate = label
            st.confidence = float(confidence)
            st.last_at = t
        else:
            st.candidate = None
            st.streak = 0

        # -- already reporting an event ----------------------------------- #
        if st.label is not None:
            if confirming and label == st.label:
                st.misses = 0
                st.frames += 1
                return None                      # ongoing, nothing to report
            if confirming and st.streak >= self.min_streak:
                prev, st.label = st.label, label  # fight <-> harass switch
                st.misses = 0
                st.frames = st.streak
                st.started_at = t
                return self._event("change", key, st, previous=prev)
            st.misses += 1
            if st.misses >= self.release_frames:
                ended = self._event("end", key, st)
                self._states[key] = BehaviorState()
                return ended
            return None

        # -- idle: can we raise? ------------------------------------------ #
        if confirming and st.streak >= self.min_streak:
            st.label = label
            st.misses = 0
            st.frames = st.streak
            st.started_at = t
            return self._event("start", key, st)
        return None

    def timeout(self, key: PairKey, t: float) -> Optional[dict]:
        """End an active event whose pair stopped being observed at all.

        A pair that leaves the trigger window (one animal swam off, a track
        died) stops calling `update`, so without this its event would stay open
        forever. The app calls it for pairs it dropped this frame.
        """
        st = self._states.get(key)
        if st is None or st.label is None:
            self._states.pop(key, None)
            return None
        st.misses += 1
        if st.misses < self.release_frames:
            return None
        ended = self._event("end", key, st, at=t)
        self._states.pop(key, None)
        return ended

    @staticmethod
    def _event(phase: str, key: PairKey, st: BehaviorState,
               previous: Optional[str] = None, at: Optional[float] = None) -> dict:
        ev = {
            "phase": phase,
            "pair": list(key),
            "label": st.label,
            "confidence": round(st.confidence, 4),
            "frames": st.frames,
            "started_at": round(st.started_at, 3),
            "t": round(st.last_at if at is None else at, 3),
            "duration_sec": round(max(0.0, (st.last_at if at is None else at)
                                      - st.started_at), 3),
        }
        if previous is not None:
            ev["previous"] = previous
        return ev

    def drop(self, keys: Iterable[PairKey]) -> None:
        for k in keys:
            self._states.pop(k, None)

    def __len__(self) -> int:
        return len(self._states)


# ---------------------------------------------------------------------------
# 4. capture quota (data flywheel)
# ---------------------------------------------------------------------------
class CaptureQuota:
    """Two-level rate limit for the trigger-time image dump.

    Per-minute caps burst (a long fight would otherwise write one frame per
    inference and fill /userdata), per-day caps the total so an unattended
    multi-day run cannot exhaust the eMMC. The day counter is keyed by the
    caller's date string, so it rolls over without a timer.

    `allow()` is called ONCE per trigger and consumes the budget only when it
    returns True.
    """

    def __init__(self, per_minute: int = 6, per_day: int = 2000) -> None:
        self.per_minute = max(0, int(per_minute))
        self.per_day = max(0, int(per_day))
        self._recent: Deque[float] = deque()
        self._day: Optional[str] = None
        self._day_count = 0

    def allow(self, now: float, day: str) -> bool:
        if self.per_minute <= 0 or self.per_day <= 0:
            return False
        if day != self._day:
            self._day, self._day_count = day, 0
        while self._recent and now - self._recent[0] >= 60.0:
            self._recent.popleft()
        if len(self._recent) >= self.per_minute:
            return False
        if self._day_count >= self.per_day:
            return False
        self._recent.append(now)
        self._day_count += 1
        return True

    @property
    def today_count(self) -> int:
        return self._day_count

    def stats(self) -> dict:
        return {"minute": len(self._recent), "day": self._day,
                "day_count": self._day_count,
                "per_minute": self.per_minute, "per_day": self.per_day}

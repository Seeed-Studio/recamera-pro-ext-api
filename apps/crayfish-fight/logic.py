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
    PairFrameBuffer     -- keep the last 8 ROI crops of each live pair, so the
                           TEMPORAL classifier has a motion sequence to look at.
    TemporalScheduler   -- decide WHICH pairs get a temporal inference this
                           frame, under a hard per-frame budget.

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
# The temporal (8-frame collage) classifier shares the single-frame vocabulary
# AND its order -- read off runs/behavior_temporal_v3/weights/best.pt:
#   YOLO(best.pt).names == {0: 'fight', 1: 'harass', 2: 'none'}
# Same list object semantics on purpose: if one is ever reordered the other
# must move with it, and `classify_head` is called with the same `size`.
TEMPORAL_LABELS = list(BEHAVIOR_LABELS)
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
               t: float, weight: int = 1) -> Optional[dict]:
        """Feed one verdict. `weight` is how many frames' worth of evidence it
        is: the SINGLE-FRAME classifier votes 1, the TEMPORAL one votes 2.

        Why the temporal verdict counts double (PLAN §3.3): it looks at 8 frames
        of motion and scores sequence-level recall 0.95 / precision 0.905, where
        the single-frame model's fight recall is at most 1/14. Weighting it
        rather than replacing the single-frame vote keeps the fast path's
        latency (an event can still raise on 3 consecutive single frames) while
        letting two temporal confirmations alone clear the default streak of 3.

        RELEASE is deliberately NOT weighted: `misses` still counts one per
        non-confirming verdict, so hysteresis stays measured in verdicts and a
        temporal `none` cannot slam an event shut on its own.
        """
        st = self.state(key)
        w = max(1, int(weight))
        confirming = (label in BEHAVIOR_EVENT_LABELS
                      and confidence >= self.min_conf)

        if confirming:
            st.streak = st.streak + w if st.candidate == label else w
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

    def minute_fraction(self, now: float) -> float:
        """Recent per-minute usage as a fraction in [0, 1] (>= 1 at/over cap).

        Prunes the same rolling 60 s window `allow()` uses, so a caller can
        check pressure WITHOUT consuming budget. `per_minute<=0` (capture
        disabled) reads as 1.0 -- maximally tight, so any consumer that only
        acts below some fraction (e.g. `CaptureDecider`'s random-keep gate)
        correctly never fires.
        """
        if self.per_minute <= 0:
            return 1.0
        while self._recent and now - self._recent[0] >= 60.0:
            self._recent.popleft()
        return len(self._recent) / float(self.per_minute)

    def stats(self) -> dict:
        return {"minute": len(self._recent), "day": self._day,
                "day_count": self._day_count,
                "per_minute": self.per_minute, "per_day": self.per_day}


# ---------------------------------------------------------------------------
# 5. capture decision (alarm / suspect / plain, + optional gating)
# ---------------------------------------------------------------------------
CAPTURE_REASONS = ("alarm", "suspect", "plain")
CAPTURE_MODES = ("trigger", "alarm_gated")


class CaptureDecider:
    """Tag WHY a triggered pair is captured this frame, and (in one mode)
    decide WHETHER it is captured at all.

    The behaviour classifier's recall is still low, so the default policy
    (`capture_mode="trigger"`) keeps the ORIGINAL "capture every proximity
    trigger, quota permitting" behaviour unchanged -- gating on the
    classifier's own verdict would filter out exactly the true-fight frames
    it is currently missing. What changes is that every capture now carries a
    `capture_reason` so the retraining pipeline can tell positives from
    background:

      * `alarm`   -- the pair has a CONFIRMED event this frame (state machine
                     is active: just started, changed, or mid-event).
      * `suspect` -- not yet confirmed by the state machine's streak, but this
                     frame's RAW classifier verdict already reads fight/harass
                     at >= `suspect_conf`.
      * `plain`   -- neither of the above: an ordinary proximity trigger with
                     no behaviour signal yet (includes `none` verdicts, which
                     are still useful negatives/hard-negatives).

    `capture_mode="alarm_gated"` is held in reserve for once the behaviour
    classifier's recall is trusted: it captures ONLY `alarm`/`suspect` and
    skips `plain` triggers outright, cutting the flywheel down to behaviour
    positives.

    In BOTH modes, when the per-minute quota is under pressure, `plain`
    triggers yield first (`alarm` > `suspect` > `plain`): a `plain` capture is
    additionally skipped once the caller's recent per-minute usage
    (`quota_fraction`) reaches 50%, reserving the remaining half of the
    minute's budget for positives. `alarm`/`suspect` are gated only by the
    caller's hard `CaptureQuota.allow()` cap, never by this soft threshold.

    `label`/`confidence` passed to `decide()` are the classifier's PER-FRAME
    verdict, before `BehaviorStateMachine` hysteresis -- exactly what the
    caller already computed for `BehaviorStateMachine.update()`, so this adds
    no extra inference.
    """

    def __init__(self, suspect_conf: float = 0.30,
                mode: str = "trigger") -> None:
        self.suspect_conf = float(suspect_conf)
        self.mode = mode if mode in CAPTURE_MODES else "trigger"

    def decide(self, *, is_alarm: bool, label: str, confidence: float,
              quota_fraction: float) -> Optional[str]:
        """Return one of `CAPTURE_REASONS`, or None to skip this trigger.

        `is_alarm`: the pair's `BehaviorStateMachine` state is active this
        frame (`fsm.active_label(key) is not None` AFTER calling `update()`).
        `quota_fraction`: caller's recent per-minute quota usage in [0, 1],
        e.g. `quota.minute_fraction(now)` -- gates `plain` only.
        """
        if is_alarm:
            reason = "alarm"
        elif label in BEHAVIOR_EVENT_LABELS and confidence >= self.suspect_conf:
            reason = "suspect"
        else:
            reason = "plain"

        if reason == "plain":
            if self.mode == "alarm_gated":
                return None
            if quota_fraction >= 0.5:
                return None
        return reason


# ---------------------------------------------------------------------------
# 6. temporal collage geometry (PLAN §3.3, scripts/temporal_proto.py)
# ---------------------------------------------------------------------------
# The temporal classifier is the SAME yolo11n-cls architecture as the
# single-frame one; the only thing that makes it temporal is what it is shown:
# 8 consecutive ROI crops of the same pair, tiled into one image. No new
# operator, no 3D conv, no rknn feature the RV1126B lacks -- which is exactly
# why this route was taken (PLAN §3.3 "拼帧法，零架构改动").
#
#   8 x 224x224 crops  ->  2 rows x 4 cols  ->  896 x 448  ->  resize 448x448
#
# Frame i goes to row i//4, col i%4, i.e. reading order: the top row is the
# first 4 (oldest) frames, the bottom row the last 4 (newest). This MUST match
# `scripts/temporal_proto.py:collage()` exactly -- the model learned that
# layout, and a transposed grid is a different image to it.
TEMPORAL_FRAMES = 8
TEMPORAL_TILE = 224          # per-frame crop side, letterboxed square
TEMPORAL_COLS = 4
TEMPORAL_ROWS = 2
TEMPORAL_INPUT = 448         # rknn input side after the final resize
# Letterbox fill. RGA pads out-of-frame ROI with gray 114 and so does
# `temporal_proto.crop()`'s PIL letterbox -- same value on both sides of the
# train/deploy line, so the model never sees an unfamiliar border.
TEMPORAL_PAD_VALUE = 114


def collage_canvas_size(tile: int = TEMPORAL_TILE) -> Tuple[int, int]:
    """(width, height) of the pre-resize collage: 4 tiles wide, 2 tall."""
    return (TEMPORAL_COLS * int(tile), TEMPORAL_ROWS * int(tile))


def collage_slots(tile: int = TEMPORAL_TILE) -> List[Tuple[int, int, int, int]]:
    """Destination rects (x0, y0, x1, y1) of the 8 frames, in feed order.

    Pure arithmetic so the geometry -- the one thing that can silently be wrong
    and still produce a plausible-looking image -- is unit-tested without
    numpy. `app.py` does the pixel copy with these slots.
    """
    t = int(tile)
    slots = []
    for i in range(TEMPORAL_FRAMES):
        x0 = (i % TEMPORAL_COLS) * t
        y0 = (i // TEMPORAL_COLS) * t
        slots.append((x0, y0, x0 + t, y0 + t))
    return slots


# ---------------------------------------------------------------------------
# 7. temporal frame buffer
# ---------------------------------------------------------------------------
@dataclass
class PairSequence:
    crops: Deque = field(default_factory=lambda: deque(maxlen=TEMPORAL_FRAMES))
    first_at: float = 0.0        # pts of the pair's first buffered frame
    last_at: float = 0.0         # pts of the most recent one
    pushes: int = 0              # total crops ever appended

    @property
    def duration(self) -> float:
        """How long this pair has been continuously close, in seconds."""
        return max(0.0, self.last_at - self.first_at)


class PairFrameBuffer:
    """Per-pair ring buffer of the last `TEMPORAL_FRAMES` interaction crops.

    Fed ONE crop per pair per TRIGGERED frame -- the very crop stage C already
    cut for the single-frame classifier, so buffering costs no extra RGA work.
    Once a pair has 8, it is `ready()` and the temporal model can be run on it.

    ★Why a cap on pairs★ the buffer is the only place in this app that holds
    pixels across frames: 8 x 224 x 224 x 3 = 1.15 MB per pair. `max_pairs`
    (default 6 -> ~7 MB) bounds that regardless of how many animals crowd the
    tank -- n animals make n(n-1)/2 pairs, so 8 animals is already 28 pairs.
    When over the cap the pairs that have been close LONGEST are kept: a long
    sustained approach is what an event looks like, while a pair that has been
    adjacent for three frames is most likely two animals passing each other.

    The buffer holds crops as opaque objects (numpy arrays in production), so
    this module stays importable without numpy.
    """

    def __init__(self, max_pairs: int = 6, frames: int = TEMPORAL_FRAMES) -> None:
        self.max_pairs = max(1, int(max_pairs))
        self.frames = max(1, int(frames))
        self._seqs: Dict[PairKey, PairSequence] = {}

    def push(self, key: PairKey, crop, t: float) -> None:
        """Append this frame's crop for `key`, evicting if over `max_pairs`."""
        seq = self._seqs.get(key)
        if seq is None:
            seq = PairSequence(crops=deque(maxlen=self.frames), first_at=t)
            self._seqs[key] = seq
        elif seq.crops.maxlen != self.frames:
            # `frames` changed under a hot-reload: re-seat the deque.
            seq.crops = deque(seq.crops, maxlen=self.frames)
        seq.crops.append(crop)
        seq.last_at = t
        seq.pushes += 1
        self._evict()

    def _evict(self) -> None:
        if len(self._seqs) <= self.max_pairs:
            return
        # Longest-close first; ties broken by the key so eviction is
        # deterministic (a test, and a device, must agree on who is dropped).
        ranked = sorted(self._seqs.items(),
                        key=lambda kv: (-kv[1].duration, kv[0]))
        for key, _ in ranked[self.max_pairs:]:
            self._seqs.pop(key, None)

    def ready(self, key: PairKey) -> bool:
        seq = self._seqs.get(key)
        return seq is not None and len(seq.crops) >= self.frames

    def ready_keys(self) -> List[PairKey]:
        return sorted(k for k in self._seqs if self.ready(k))

    def sequence(self, key: PairKey) -> Optional[List]:
        """The 8 crops oldest-first, or None if the pair is not ready yet."""
        if not self.ready(key):
            return None
        return list(self._seqs[key].crops)

    def duration(self, key: PairKey) -> float:
        seq = self._seqs.get(key)
        return seq.duration if seq is not None else 0.0

    def retain(self, keys: Iterable[PairKey]) -> List[PairKey]:
        """Drop every pair not in `keys`; returns what was dropped.

        Called once a frame with the currently-TRIGGERED pairs: a pair that
        stopped being close is not going to be classified again, and its 1.15 MB
        must not linger. `ProximityWindow`'s 5-of-8 window already smooths the
        flicker, so leaving `triggered` is a real separation, not a dropped
        detection.
        """
        keep = set(keys)
        gone = [k for k in self._seqs if k not in keep]
        for k in gone:
            self._seqs.pop(k, None)
        return sorted(gone)

    def drop(self, keys: Iterable[PairKey]) -> None:
        for k in keys:
            self._seqs.pop(k, None)

    def drop_track(self, track_id: int) -> None:
        for k in [k for k in self._seqs if track_id in k]:
            self._seqs.pop(k, None)

    def __len__(self) -> int:
        return len(self._seqs)


# ---------------------------------------------------------------------------
# 8. temporal inference scheduler
# ---------------------------------------------------------------------------
class TemporalScheduler:
    """Round-robin the temporal model over ready pairs, under a frame budget.

    The 448² collage is ~13 GFLOPs -- roughly 4x one 224² single-frame pass --
    so it cannot run per pair per frame. Two independent limits make the cost
    flat instead of quadratic in the number of animals:

      * `budget` (default 1): at most this many temporal inferences PER FRAME,
        no matter how many pairs are ready. This alone bounds the added load to
        a constant, which is why 30 pairs cost the same as 2.
      * `stride` (default 4): a given pair is not re-run until `stride` frames
        after its last run. At ~12 fps that is a fresh verdict every ~0.33 s per
        pair, while the buffer's 8 frames span ~0.66 s -- consecutive verdicts
        overlap by half, so an event cannot slip between two windows.

    Selection is least-recently-run first (never-run pairs lead), so with more
    ready pairs than budget every pair still gets served in turn rather than the
    lowest track id monopolising the model.
    """

    def __init__(self, stride: int = 4, budget: int = 1) -> None:
        self.stride = max(1, int(stride))
        self.budget = max(0, int(budget))
        self._last_run: Dict[PairKey, int] = {}
        self._frame = 0

    @property
    def frame(self) -> int:
        return self._frame

    def select(self, ready: Iterable[PairKey]) -> List[PairKey]:
        """Advance one frame and return the pairs to infer NOW.

        ★Call exactly once per frame★, even when nothing is ready -- the frame
        counter is what `stride` is measured in, so skipping a call would make
        the stride elastic. The returned pairs are recorded as run immediately:
        the caller is expected to infer all of them.
        """
        self._frame += 1
        if self.budget <= 0:
            return []
        due = [k for k in ready
               if self._frame - self._last_run.get(k, -self.stride) >= self.stride]
        # least-recently-run first; -1 for never-run so they lead. Key breaks
        # ties, keeping the order deterministic.
        due.sort(key=lambda k: (self._last_run.get(k, -1), k))
        picks = due[: self.budget]
        for k in picks:
            self._last_run[k] = self._frame
        return picks

    def last_run(self, key: PairKey) -> Optional[int]:
        return self._last_run.get(key)

    def drop(self, keys: Iterable[PairKey]) -> None:
        for k in keys:
            self._last_run.pop(k, None)

    def drop_track(self, track_id: int) -> None:
        for k in [k for k in self._last_run if track_id in k]:
            self._last_run.pop(k, None)

    def __len__(self) -> int:
        return len(self._last_run)

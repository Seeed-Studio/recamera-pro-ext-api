"""
Per-identity face-attribute evidence: quality gating, temporal voting and
once-per-person demographic counting.

A second-stage attribute classifier (FairFace age/gender/race, an emotion head)
produces one softmax per FRAME. Reading that softmax straight out is wrong in
three separate ways, and this module owns the fix for all three:

  1. **Quality.** A 30x30 face upsampled to 224, or a detection that barely
     cleared the score threshold, still yields a confident-looking softmax. The
     gate (`AttributeConfig.min_face_px` / `min_det_score`) rejects those ROIs
     BEFORE the classifier runs, so they cost no inference and contribute no
     evidence -- rather than emitting a coin-toss dressed up as a prediction.

  2. **Single-frame argmax.** The same person flips label frame to frame. A
     `TrackAttributes` accumulator sums the per-frame probability vectors for
     one `track_id` and argmaxes the SUM, so the verdict is a vote over every
     frame that passed the gate. `stable` says whether enough frames have
     accumulated (`min_track_frames`) for the verdict to mean anything.

  3. **Counting.** A demographic histogram bumped once per face per frame does
     not measure people; it measures dwell-weighted face-frames -- someone
     standing still for a minute outvotes sixty people walking past. `Aggregator`
     folds each `track_id` into the histogram EXACTLY ONCE, when its evidence
     first becomes stable, so the window reports unique faces. The raw
     face-frame count is kept alongside it for anyone who wants the old number.

Softmax **temperature** is a deployment-calibration knob, not a property of the
model: a ResNet classification head is systematically overconfident, and the
correction is fitted per head on a held-out set. `AttributeConfig` owns the
policy (which head gets which temperature); the arithmetic lives one layer down
in `kit.runtime.postprocess.classify.softmax`, which the caller reaches through
`fairface_decode(outputs, temperature=cfg.temperature)`. Default 1.0 everywhere
= exact no-op, so turning it on is a deliberate, measured act. `min_conf`
likewise defaults to 0 (off): suppressing a label is a product decision that
needs a measured threshold behind it, and this module only supplies the
mechanism.

The module is model-free and app-agnostic: it consumes `(head, probability
vector)` pairs keyed by a track id and knows nothing about FairFace, RKNN or
which labels exist. `kit.logic.tracker` supplies the ids.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional, Sequence

import numpy as np


@dataclass
class AttributeConfig:
    """Gating / voting / calibration policy for one cascade app."""

    # -- quality gate (applied to the DETECTION, before stage 2 runs) ------ #
    min_face_px: float = 64.0
    """Shorter side of the face box in ORIGINAL frame pixels. Below this the ROI
    is mostly upsampling artefact: the crop is blown up to the classifier's 224
    input and the head reports whatever texture the interpolator invented."""

    min_det_score: float = 0.0
    """Extra detector-score floor on top of the detector's own threshold, for
    when attributes should be stricter than detection. 0 disables it."""

    # -- temporal voting -------------------------------------------------- #
    min_track_frames: int = 3
    """Gate-passing frames a track needs before its verdict counts as `stable`
    and is folded into the demographic histogram."""

    max_frames: int = 0
    """Cap on accumulated frames per track (0 = whole track life). A nonzero cap
    makes the accumulator a sliding weight: older evidence is decayed by
    `decay` instead of kept forever, which matters if one track id can outlive
    the person it started on (an identity swap through an occlusion)."""

    decay: float = 1.0
    """Per-frame multiplier applied to accumulated evidence before adding the
    new frame. 1.0 = plain sum (every frame equal). <1.0 = exponential
    forgetting, so a mid-track identity swap recovers instead of being
    outvoted by history."""

    # -- calibration (per head; missing head => default) ------------------- #
    temperature: Dict[str, float] = field(default_factory=dict)
    """Softmax temperature per head name. >1 softens, <1 sharpens, 1.0 = no-op."""

    min_conf: Dict[str, float] = field(default_factory=dict)
    """Per-head confidence floor. A verdict below its floor reports label None
    rather than a guess. 0 / missing = no suppression."""

    def clamp(self) -> "AttributeConfig":
        self.min_face_px = max(0.0, float(self.min_face_px))
        self.min_det_score = min(1.0, max(0.0, float(self.min_det_score)))
        self.min_track_frames = max(1, int(self.min_track_frames))
        self.max_frames = max(0, int(self.max_frames))
        self.decay = min(1.0, max(0.0, float(self.decay)))
        self.temperature = {k: max(1e-3, float(v))
                            for k, v in (self.temperature or {}).items()}
        self.min_conf = {k: min(1.0, max(0.0, float(v)))
                         for k, v in (self.min_conf or {}).items()}
        return self

    def temp(self, head: str) -> float:
        return float(self.temperature.get(head, 1.0))

    def floor(self, head: str) -> float:
        return float(self.min_conf.get(head, 0.0))


def passes_gate(box: Sequence[float], score: float,
                cfg: AttributeConfig) -> bool:
    """Is this detection worth running an attribute classifier on?

    `box` is [x1,y1,x2,y2] in ORIGINAL frame pixels -- the gate is a physical
    resolution test, so it must NOT be fed letterboxed or normalised
    coordinates. Called before the crop, so a rejected face costs nothing.
    """
    x1, y1, x2, y2 = (float(v) for v in box[:4])
    side = min(abs(x2 - x1), abs(y2 - y1))
    if side < cfg.min_face_px:
        return False
    if cfg.min_det_score > 0.0 and float(score) < cfg.min_det_score:
        return False
    return True


class TrackAttributes:
    """Accumulated per-head probability evidence for ONE track id."""

    __slots__ = ("cfg", "sums", "frames", "counted", "last_seen")

    def __init__(self, cfg: AttributeConfig) -> None:
        self.cfg = cfg
        self.sums: Dict[str, np.ndarray] = {}
        self.frames: int = 0          # gate-passing frames accumulated
        self.counted: bool = False    # already folded into the histogram?
        self.last_seen: float = 0.0

    def add(self, head: str, probs: Sequence[float]) -> None:
        """Fold one frame's probability vector for `head` into the evidence."""
        p = np.asarray(probs, dtype=np.float32).reshape(-1)
        if p.size == 0:
            return
        prev = self.sums.get(head)
        if prev is None or prev.shape != p.shape:
            self.sums[head] = p.copy()
        else:
            self.sums[head] = prev * self.cfg.decay + p

    def bump_frame(self, t: float) -> None:
        """Count one gate-passing frame. Call once per frame, after `add()`s."""
        self.last_seen = float(t)
        if self.cfg.max_frames and self.frames >= self.cfg.max_frames:
            return
        self.frames += 1

    @property
    def stable(self) -> bool:
        return self.frames >= self.cfg.min_track_frames

    def verdict(self, head: str,
                labels: Optional[Sequence[str]] = None) -> dict:
        """Argmax of the ACCUMULATED evidence for one head.

        `confidence` is the vote share (accumulated probability mass of the
        winner / total), not a single frame's softmax -- so it reads as "how
        much of the evidence points here", which is what a caller thresholding
        it actually wants. Falls back to a label-less empty verdict when the
        head has no evidence yet.
        """
        acc = self.sums.get(head)
        if acc is None or acc.size == 0:
            return {"index": -1, "label": None, "confidence": 0.0,
                    "frames": self.frames, "stable": False}
        total = float(np.sum(acc))
        probs = acc / total if total > 0 else np.full_like(acc, 1.0 / acc.size)
        idx = int(np.argmax(probs))
        conf = round(float(probs[idx]), 4)
        label = labels[idx] if labels is not None and idx < len(labels) else idx
        if conf < self.cfg.floor(head):
            label = None            # measured floor not cleared -> no guess
        return {"index": idx, "label": label, "confidence": conf,
                "frames": self.frames, "stable": self.stable}


class Aggregator:
    """Track-scoped attribute store + a once-per-identity demographic window.

    Owns one `TrackAttributes` per live track id and the running histogram.
    `sweep()` drops the state of tracks the tracker has retired, so memory is
    bounded by the number of CONCURRENT faces, not by the number of people
    seen since boot.
    """

    def __init__(self, cfg: Optional[AttributeConfig] = None,
                 heads: Sequence[str] = ()) -> None:
        self.cfg = (cfg or AttributeConfig()).clamp()
        self.heads = list(heads)
        self._tracks: Dict[int, TrackAttributes] = {}
        self.reset_window(0.0)

    # -- per-track evidence ------------------------------------------------ #
    def track(self, track_id: int) -> TrackAttributes:
        ta = self._tracks.get(track_id)
        if ta is None:
            ta = TrackAttributes(self.cfg)
            self._tracks[track_id] = ta
        return ta

    def sweep(self, removed_ids: Sequence[int]) -> None:
        """Forget retired tracks (ids the tracker dropped this frame)."""
        for tid in removed_ids:
            self._tracks.pop(int(tid), None)

    # -- demographic window ------------------------------------------------ #
    def reset_window(self, t: float) -> None:
        self.window_start = float(t)
        self.unique_faces = 0        # tracks folded in this window
        self.face_frames = 0         # raw face-frame count (the OLD semantics)
        self.hist: Dict[str, Dict[str, float]] = {h: {} for h in self.heads}

    def note_face_frame(self) -> None:
        self.face_frames += 1

    def maybe_count(self, track_id: int,
                    labels_by_head: Dict[str, Optional[str]]) -> bool:
        """Fold a track into the histogram if it is stable and not yet counted.

        Returns True when this call actually counted the track. Counting happens
        at the moment the evidence FIRST becomes stable, not at track exit, so a
        person who lingers is reported in the window they arrived in rather than
        being withheld until they leave.
        """
        ta = self._tracks.get(int(track_id))
        if ta is None or ta.counted or not ta.stable:
            return False
        ta.counted = True
        self.unique_faces += 1
        for head, label in labels_by_head.items():
            if label is None:
                continue
            d = self.hist.setdefault(head, {})
            d[label] = d.get(label, 0) + 1
        return True

    def elapsed(self, t: float) -> float:
        return float(t) - self.window_start

    def snapshot(self, t: float) -> dict:
        """The demographics event body for the window ending at `t`."""
        return {
            "kind": "demographics",
            "window_sec": round(self.elapsed(t), 1),
            "faces": int(self.unique_faces),
            "face_frames": int(self.face_frames),
            **{h: dict(self.hist.get(h, {})) for h in self.heads},
        }

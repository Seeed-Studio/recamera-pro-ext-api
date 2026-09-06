"""Per-track temporal liveness: motion, blink, texture EMA, fusion.

Pure CPU/numpy, no model handles and no frame pixels — everything here takes
already-extracted features so it is unit-testable without an NPU.

Three independent pieces of evidence, combined by `fuse_liveness`:

  * **texture** — the exponential moving average of the two-model MiniFAS
    ensemble P(real). Primary discriminator; a printed photo or a phone screen
    is what it was trained on.
  * **motion** — the residual left over after the best similarity transform
    (scale + rotation + translation) between two consecutive five-point sets is
    removed. A photo waved in front of the lens is a *rigid* object: its five
    points move together, the similarity fit explains them, and the residual
    collapses to detector jitter. A real face deforms — that is what survives
    the fit. Residuals are normalised by the face's short side so the same
    threshold holds at 80 px and at 300 px.
  * **blink** — a 1..3-sample dip of the FaceMesh EAR below threshold followed
    by an open sample. A blink is positive proof and latches for the lifetime
    of the track; the *absence* of a blink is never evidence of a spoof (people
    stare, and FaceMesh is only sampled every K frames).

★Why a residual and not a displacement★ the naive "did the landmarks move?"
test is satisfied by holding a photo unsteadily. Removing the similarity
component first is what makes the test about the *face* rather than about the
hand holding it.

★Why the correlation term★ detector jitter is white: its residual vector is
uncorrelated from one frame pair to the next. A genuine deformation (a smile
starting, an eye closing) persists across several pairs, so the mean
off-diagonal correlation of the residual vectors separates "the detector is
noisy" from "the face is moving" at the same residual magnitude.

★Why pending is a first-class verdict★ with three texture samples required and
motion needing a populated window, the honest answer for the first second is
"not yet". The app withholds identity while pending rather than guessing, and
falls back to texture (plus optional depth) once `timeout_sec` elapses so a
perfectly still real face is not stuck pending forever.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Sequence, Tuple

import numpy as np

# Observations needed inside the window before motion is reportable: two
# adjacent pairs, which is the minimum for one off-diagonal correlation.
MIN_MOTION_OBS = 3

PENDING = "pending"
LIVE = "live"
SPOOF = "spoof"


@dataclass
class LivenessConfig:
    """Mirror of the manifest's ``liveness_*`` keys, without the prefix."""
    texture_ema_alpha: float = 0.4
    min_samples: int = 3
    motion_window_sec: float = 1.75
    timeout_sec: float = 2.0
    motion_noise_floor: float = 0.003
    motion_high: float = 0.020
    correlation_low: float = 0.15
    correlation_high: float = 0.65
    facemesh_interval: int = 2
    # Cost gates (capture mode ignores them): FaceMesh EAR is meaningless on
    # tiny faces and depth needs a face patch of tens of pixels; a track that
    # is already live is only re-checked every live_recheck_interval frames.
    min_face_px: int = 100
    depth_min_face_px: int = 150
    live_recheck_interval: int = 15
    ear_threshold: float = 0.21
    blink_min_samples: int = 1
    blink_max_samples: int = 3
    w_texture: float = 0.70
    w_motion: float = 0.30
    w_depth: float = 0.20
    blink_bonus: float = 1.0
    t_live: float = 0.65
    t_spoof: float = 0.45
    depth_enabled: bool = False

    @classmethod
    def from_config(cls, c: Dict, prefix: str = "liveness_") -> "LivenessConfig":
        """Build from the app config dict, ignoring unknown keys."""
        out = cls()
        for f in cls.__dataclass_fields__:                  # noqa: SLF001
            key = prefix + f
            if key in (c or {}):
                cur = getattr(out, f)
                val = c[key]
                if isinstance(cur, bool):
                    val = bool(val)
                elif isinstance(cur, int):
                    val = int(val)
                else:
                    val = float(val)
                setattr(out, f, val)
        return out


@dataclass
class LivenessState:
    """One track's accumulated liveness evidence."""
    texture_ema: Optional[float] = None
    texture_samples: int = 0
    keypoints: Deque = field(default_factory=deque)
    ear: Optional[float] = None
    closed_samples: int = 0
    blink_seen: bool = False
    last_heavy_frame: int = -(10 ** 9)   # frame index of the last texture/mesh/depth pass
    decision: str = PENDING           # pending|live|spoof
    score: Optional[float] = None
    reason: str = "insufficient_samples"
    # Last motion features, kept for the calibration capture rows.
    motion_residual: Optional[float] = None
    correlation: Optional[float] = None
    motion_score: Optional[float] = None
    depth: Optional[Dict[str, float]] = None

    # -- texture --------------------------------------------------------- #
    def update_texture(self, p_mean: Optional[float], alpha: float) -> Optional[float]:
        """Fold one ensemble P(real) into the EMA. Returns the new EMA."""
        if p_mean is None or not math.isfinite(float(p_mean)):
            return self.texture_ema
        p = float(p_mean)
        a = min(1.0, max(0.0, float(alpha)))
        self.texture_ema = p if self.texture_ema is None else \
            a * p + (1.0 - a) * float(self.texture_ema)
        self.texture_samples += 1
        return self.texture_ema


# --------------------------------------------------------------------------- #
# Motion
# --------------------------------------------------------------------------- #
def _similarity_residual(a: np.ndarray, b: np.ndarray) -> Optional[np.ndarray]:
    """Residual of `b` after the best similarity transform of `a` is removed.

    Complex least squares: with the point sets written as complex numbers and
    both centred, ``b ~= z*a`` where the single complex ``z`` carries both the
    scale and the rotation, so ``z = <a, b> / <a, a>``. Returns an (n, 2)
    residual in the same pixel units as the inputs, or None when `a` is
    degenerate (all five points coincident).
    """
    ac = a.astype(np.float64)
    bc = b.astype(np.float64)
    ac = ac - ac.mean(axis=0, keepdims=True)
    bc = bc - bc.mean(axis=0, keepdims=True)
    za = ac[:, 0] + 1j * ac[:, 1]
    zb = bc[:, 0] + 1j * bc[:, 1]
    denom = float(np.vdot(za, za).real)
    if denom < 1e-9:
        return None
    z = complex(np.vdot(za, zb)) / denom        # vdot conjugates the first arg
    res = zb - z * za
    return np.stack([res.real, res.imag], axis=1)


def _mean_offdiag_correlation(rows: np.ndarray) -> Optional[float]:
    """Mean off-diagonal Pearson correlation between residual vectors."""
    if rows.shape[0] < 2:
        return None
    x = rows - rows.mean(axis=1, keepdims=True)
    norm = np.sqrt((x * x).sum(axis=1))
    keep = norm > 1e-12
    if int(keep.sum()) < 2:
        return None
    x = x[keep] / norm[keep][:, None]
    c = x @ x.T
    n = c.shape[0]
    off = (float(c.sum()) - float(np.trace(c))) / (n * (n - 1))
    return float(off)


def update_motion(
    state: LivenessState,
    ts: float,
    kps5: Sequence[Sequence[float]],
    face_px: float,
    cfg: LivenessConfig,
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Append one five-point observation and recompute the motion features.

    Returns ``(residual_rms, correlation, motion_score)``.

    * `residual_rms` is in face-short-side units (dimensionless), so it is
      comparable across face sizes and camera resolutions.
    * `motion_score` is None whenever the window is too short OR the residual
      sits at or below `motion_noise_floor` — at that point the observation is
      indistinguishable from detector jitter and must not be scored as evidence
      in either direction. `fuse_liveness` therefore drops the motion term
      instead of penalising the face for standing still.
    """
    pts = np.asarray(kps5, dtype=np.float64).reshape(-1, 2)
    t = float(ts)
    state.keypoints.append((t, pts))
    window = max(0.0, float(cfg.motion_window_sec))
    while state.keypoints and (t - state.keypoints[0][0]) > window:
        state.keypoints.popleft()

    state.motion_residual = state.correlation = state.motion_score = None
    if len(state.keypoints) < MIN_MOTION_OBS:
        return None, None, None

    scale = max(1.0, float(face_px))
    residuals: List[np.ndarray] = []
    obs = list(state.keypoints)
    for (_, a), (_, b) in zip(obs, obs[1:]):
        if a.shape != b.shape or a.shape[0] < 3:
            continue
        r = _similarity_residual(a, b)
        if r is None:
            continue
        residuals.append(r / scale)
    if len(residuals) < 2:
        return None, None, None

    stack = np.stack(residuals)                       # (pairs, n, 2)
    rms = float(np.sqrt(np.mean(stack * stack)))
    corr = _mean_offdiag_correlation(stack.reshape(stack.shape[0], -1))

    state.motion_residual = rms
    state.correlation = corr
    state.motion_score = normalized_motion(rms, corr, cfg)
    return rms, corr, state.motion_score


def normalized_motion(residual: Optional[float], correlation: Optional[float],
                      cfg: LivenessConfig) -> Optional[float]:
    """Clipped piecewise-linear calibration of (residual, correlation).

    None below the noise floor — see `update_motion`.
    """
    if residual is None or not math.isfinite(float(residual)):
        return None
    if float(residual) <= float(cfg.motion_noise_floor):
        return None
    span = max(1e-9, float(cfg.motion_high) - float(cfg.motion_noise_floor))
    m_res = (float(residual) - float(cfg.motion_noise_floor)) / span
    m_res = min(1.0, max(0.0, m_res))
    c = 0.0 if (correlation is None or not math.isfinite(float(correlation))) \
        else float(correlation)
    cspan = max(1e-9, float(cfg.correlation_high) - float(cfg.correlation_low))
    m_corr = (c - float(cfg.correlation_low)) / cspan
    m_corr = min(1.0, max(0.0, m_corr))
    return 0.5 * (m_res + m_corr)


# --------------------------------------------------------------------------- #
# Blink
# --------------------------------------------------------------------------- #
def update_blink(state: LivenessState, ear: Optional[float], threshold: float,
                 min_closed: int, max_closed: int) -> bool:
    """Advance the blink state machine by ONE FaceMesh sample.

    `ear` is None on frames where FaceMesh was not sampled — those frames are
    not observations and must not reset a dip in progress.

    A blink is an open sample that follows a closed run of
    ``min_closed..max_closed`` samples. An overlong run (an eye held shut, or a
    face that left) resets the counter without latching a blink; `blink_seen`
    itself never un-latches, because one confirmed blink is proof for the
    lifetime of the track.
    """
    if ear is None or not math.isfinite(float(ear)):
        return False
    state.ear = float(ear)
    lo = max(1, int(min_closed))
    hi = max(lo, int(max_closed))
    if float(ear) < float(threshold):
        state.closed_samples += 1
        return False
    n = state.closed_samples
    state.closed_samples = 0
    if lo <= n <= hi:
        state.blink_seen = True
        return True
    return False


# --------------------------------------------------------------------------- #
# Fusion
# --------------------------------------------------------------------------- #
def _hysteresis(prev: str, score: float, cfg: LivenessConfig) -> str:
    """T_live/T_spoof band: a verdict holds until the OTHER threshold is met."""
    if prev == LIVE:
        return SPOOF if score < float(cfg.t_spoof) else LIVE
    if prev == SPOOF:
        return LIVE if score >= float(cfg.t_live) else SPOOF
    if score >= float(cfg.t_live):
        return LIVE
    if score < float(cfg.t_spoof):
        return SPOOF
    return PENDING


def fuse_liveness(
    state: LivenessState,
    now: float,
    first_seen: float,
    motion_score: Optional[float],
    depth_score: Optional[float],
    cfg: LivenessConfig,
) -> Dict:
    """Combine the available evidence into one verdict for this track.

    Order of precedence:

      1. Fewer than `min_samples` texture samples -> pending. Nothing is
         published on a single MiniFAS frame.
      2. A latched blink -> live, immediately, with `blink_bonus` folded into
         the reported score. Passive liveness has no stronger positive.
      3. Otherwise a weighted mean of the available evidence, weights
         renormalised over exactly the terms that are present, then the
         T_live/T_spoof hysteresis. Motion that is absent (still face, short
         window) is DROPPED, never scored as zero.
      4. Before `timeout_sec` a face with no usable motion stays pending;
         after it, texture (plus optional depth) decides on its own.
    """
    tex = state.texture_ema
    if state.texture_samples < max(1, int(cfg.min_samples)) or tex is None:
        state.decision = PENDING
        state.reason = "insufficient_samples"
        state.score = tex
        return result_dict(state, depth_score)

    if state.blink_seen:
        state.decision = LIVE
        state.reason = "blink"
        base = float(tex)
        state.score = min(1.0, base + float(cfg.blink_bonus))
        return result_dict(state, depth_score)

    timed_out = (float(now) - float(first_seen)) >= float(cfg.timeout_sec)
    if motion_score is None and not timed_out:
        state.decision = PENDING
        state.reason = "awaiting_motion"
        state.score = float(tex)
        return result_dict(state, depth_score)

    terms: List[Tuple[float, float]] = [(float(cfg.w_texture), float(tex))]
    used = ["texture"]
    if motion_score is not None:
        terms.append((float(cfg.w_motion), float(motion_score)))
        used.append("motion")
    if cfg.depth_enabled and depth_score is not None:
        terms.append((float(cfg.w_depth), float(depth_score)))
        used.append("depth")

    wsum = sum(w for w, _ in terms)
    score = sum(w * s for w, s in terms) / wsum if wsum > 0 else float(tex)
    score = min(1.0, max(0.0, float(score)))

    prev = state.decision
    state.decision = _hysteresis(prev, score, cfg)
    state.score = score
    if state.decision == PENDING:
        state.reason = "uncertain"
    else:
        state.reason = "+".join(used)
        if timed_out and motion_score is None:
            state.reason = "timeout_" + state.reason
    return result_dict(state, depth_score)


def result_dict(state: LivenessState, depth_score: Optional[float] = None) -> Dict:
    """The `extra.faces[].liveness` object for one track."""
    out: Dict = {
        "score": (None if state.score is None else float(state.score)),
        "texture": (None if state.texture_ema is None
                    else float(state.texture_ema)),
        "motion": (None if state.motion_score is None
                   else float(state.motion_score)),
        "blink": bool(state.blink_seen),
        "decision": state.decision,
        "reason": state.reason,
    }
    if state.depth is not None:
        # depth_flatness also returns diagnostics (box tuple, sample count);
        # only scalar numbers belong in the result payload.
        out["depth"] = {k: float(v) for k, v in state.depth.items()
                        if isinstance(v, (int, float, np.floating, np.integer))
                        and not isinstance(v, bool)}
    elif depth_score is not None:
        out["depth"] = {"score": float(depth_score)}
    return out


def skip_heavy_for_live(state: LivenessState, frame_idx: int, cfg: LivenessConfig,
                        capturing: bool) -> bool:
    """True when a track already judged live can keep its verdict this frame
    without spending FaceMesh/texture/depth inference."""
    if capturing or state.decision != LIVE:
        return False
    return (frame_idx - state.last_heavy_frame) < max(1, int(cfg.live_recheck_interval))


def facemesh_allowed(face_px: float, cfg: LivenessConfig, capturing: bool) -> bool:
    return capturing or face_px >= float(cfg.min_face_px)


def depth_allowed(face_px: float, cfg: LivenessConfig, capturing: bool) -> bool:
    return capturing or face_px >= float(cfg.depth_min_face_px)


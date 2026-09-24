"""
Temporal fall state machine, ported from the first-gen C++
(solutions/fall-detection/main/fall_detector.cpp).

Design faithful to the production first-gen/Jetson detector. Only geometry
arms ``suspected``, by one of two paths, each followed by a horizontal cue
(torso angle or box aspect):

* speed: a fast hip drop within ``motion_window_sec`` of the cue;
* displacement: when the pose was invalid (occluded) between the last upright
  baseline and the cue, a hip drop of at least ``hip_drop_distance_threshold``
  below that baseline, with the baseline at most ``motion_window_sec +
  occlusion_grace_sec`` old.  Speed measured across such a gap is diluted.

A temporal-positive result alone never leaves ``normal``.  By default
``fallen`` is confirmed only from ``suspected`` when the learned temporal gate
is positive AND the current valid pose is lying with enough evidence features.
For confirmation the motion feature counts as met because the candidate was
motion-armed (latched); the current hip speed of a victim lying still is ~0.

``suspected`` normally ends after ``suspected_timeout_sec``.  If the person is
still lying at that point, the candidate stays ``suspected`` (a late learned
positive may still confirm it) until ``suspected_timeout_sec +
late_confirmation_sec`` after arming, or until the pose is no longer lying.
``temporal_confirmation_required`` may be set false explicitly for legacy
geometry-only bring-up.

State: Normal -> Suspected -> Fallen -> Recovering -> Normal.
No fall is declared from a single feature or a single frame: evidence is scored
(hip speed, torso angle, box aspect) and must persist for `confirmation_sec`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict

from kit.logic.geometry import Observation

NORMAL = "normal"
SUSPECTED = "suspected"
FALLEN = "fallen"
RECOVERING = "recovering"


@dataclass
class FallConfig:
    temporal_confirmation_required: bool = True
    hip_drop_speed_threshold: float = 0.25      # normalised y units / second
    hip_drop_distance_threshold: float = 0.02   # from last non-horizontal pose
    motion_window_sec: float = 0.75             # drop may precede horizontal pose
    torso_angle_threshold_deg: float = 55.0
    bbox_aspect_ratio_threshold: float = 1.25
    min_suspected_features: int = 2             # of speed, torso, aspect
    confirmation_sec: float = 0.80
    suspected_timeout_sec: float = 1.50
    occlusion_grace_sec: float = 0.75
    recovery_torso_angle_deg: float = 35.0
    recovery_aspect_ratio: float = 1.10
    recovery_window_sec: float = 2.00
    cooldown_sec: float = 3.00
    # Extra time after suspected_timeout_sec during which a still-lying
    # candidate may be confirmed by a late temporal positive.  3.2 s is the
    # learned gate's window (48 frames at 15 fps): after that the arming drop
    # has left the classifier's input, so a positive is no longer about it.
    late_confirmation_sec: float = 3.20

    def clamp(self) -> "FallConfig":
        self.hip_drop_speed_threshold = max(0.0, self.hip_drop_speed_threshold)
        self.hip_drop_distance_threshold = max(0.0, self.hip_drop_distance_threshold)
        self.motion_window_sec = max(0.0, self.motion_window_sec)
        self.torso_angle_threshold_deg = min(89.0, max(1.0, self.torso_angle_threshold_deg))
        self.bbox_aspect_ratio_threshold = max(1.0, self.bbox_aspect_ratio_threshold)
        self.min_suspected_features = min(3, max(1, int(self.min_suspected_features)))
        self.confirmation_sec = max(0.0, self.confirmation_sec)
        self.suspected_timeout_sec = max(self.confirmation_sec, self.suspected_timeout_sec)
        self.occlusion_grace_sec = max(0.0, self.occlusion_grace_sec)
        self.recovery_torso_angle_deg = min(89.0, max(1.0, self.recovery_torso_angle_deg))
        self.recovery_aspect_ratio = max(1.0, self.recovery_aspect_ratio)
        self.recovery_window_sec = max(0.0, self.recovery_window_sec)
        self.cooldown_sec = max(0.0, self.cooldown_sec)
        self.late_confirmation_sec = max(0.0, self.late_confirmation_sec)
        return self


@dataclass
class FallOutput:
    state: str = NORMAL
    fall_detected: bool = False   # true while Fallen or Recovering
    fall_event: bool = False      # edge: Suspected -> Fallen
    event_id: int = 0
    diagnostics: Dict[str, float] = field(default_factory=dict)


class FallDetector:
    def __init__(self, config: FallConfig | None = None):
        self.config = (config or FallConfig()).clamp()
        self.reset()

    def set_config(self, config: FallConfig) -> None:
        self.config = config.clamp()

    def reset(self) -> None:
        self._state = NORMAL
        self._event_id = 0
        self._initialized = False
        self._have_prev = False
        self._prev_hip_y = 0.0
        self._prev_ts = 0.0
        self._last_fast_drop = -1.0
        self._baseline_hip_y = 0.0
        self._baseline_ts = -1.0
        self._have_baseline = False
        self._gap_since_baseline = False
        self._max_drop = 0.0
        self._suspected_since = -1.0
        self._last_strong_evidence = -1.0
        self._motion_triggered = False
        self._recovery_since = -1.0
        self._cooldown_until = -1.0
        self._diag: Dict[str, float] = {}

    # -- helpers (mirror C++ isLying / isUpright / featureCount) ---------- #
    def _is_lying(self, o: Observation) -> bool:
        c = self.config
        return (o.torso_angle_deg >= c.torso_angle_threshold_deg and
                o.bbox_aspect_ratio >= c.bbox_aspect_ratio_threshold)

    def _is_upright(self, o: Observation) -> bool:
        c = self.config
        return (o.torso_angle_deg <= c.recovery_torso_angle_deg and
                o.bbox_aspect_ratio <= c.recovery_aspect_ratio)

    def _feature_count(self, o: Observation, hip_speed: float) -> int:
        c = self.config
        n = 0
        if hip_speed >= c.hip_drop_speed_threshold:
            n += 1
        if o.torso_angle_deg >= c.torso_angle_threshold_deg:
            n += 1
        if o.bbox_aspect_ratio >= c.bbox_aspect_ratio_threshold:
            n += 1
        return n

    def _finish(self, o: Observation) -> FallOutput:
        self._diag["in_cooldown"] = o.timestamp_sec < self._cooldown_until
        self._diag["suspected_for_sec"] = (
            max(0.0, o.timestamp_sec - self._suspected_since)
            if self._suspected_since >= 0.0 else 0.0)
        self._diag["recovery_for_sec"] = (
            max(0.0, o.timestamp_sec - self._recovery_since)
            if self._recovery_since >= 0.0 else 0.0)
        self._diag["hip_drop_distance"] = self._max_drop
        self._diag["state"] = self._state
        out = FallOutput()
        out.state = self._state
        out.fall_detected = self._state in (FALLEN, RECOVERING)
        out.event_id = self._event_id
        out.fall_event = self._fall_event
        out.diagnostics = dict(self._diag)
        return out

    def update(self, o: Observation, *, temporal_available: bool = False,
               temporal_positive: bool = False,
               temporal_probability: float = 0.0) -> FallOutput:
        c = self.config
        self._fall_event = False

        hip_speed = 0.0
        if o.valid and self._have_prev:
            dt = o.timestamp_sec - self._prev_ts
            if 1e-4 < dt < 10.0:
                hip_speed = (o.hip_y - self._prev_hip_y) / dt

        if not o.valid:
            # No usable pose (occlusion right after impact is common). Only a
            # motion-triggered candidate with a recent strong lying pose may
            # confirm through the gap; a generic disappearance never alarms.
            self._diag = {
                "hip_drop_speed": 0.0,
                "hip_drop_distance": self._max_drop,
                "torso_angle_deg": 0.0,
                "bbox_aspect_ratio": 0.0,
                "evidence_features": 0,
                "evidence_score": 0.0,
                "lying_posture": False,
                "upright_posture": False,
                "temporal_positive": bool(temporal_positive),
                "temporal_probability": float(temporal_probability),
            }
            if self._have_baseline:
                self._gap_since_baseline = True
            if self._state == SUSPECTED and self._suspected_since >= 0.0:
                suspected_for = o.timestamp_sec - self._suspected_since
                recent_strong = (self._last_strong_evidence >= 0.0 and
                                 o.timestamp_sec - self._last_strong_evidence
                                 <= c.occlusion_grace_sec)
                if (not c.temporal_confirmation_required and
                        self._motion_triggered and recent_strong and
                        self._max_drop >= c.hip_drop_distance_threshold and
                        suspected_for >= c.confirmation_sec):
                    self._to_fallen(o)
                elif (suspected_for > c.suspected_timeout_sec and
                        not (recent_strong and self._in_late_latch(o))):
                    # A late-confirmation candidate survives only a short
                    # occlusion right after a lying frame.
                    self._to_normal()
            return self._finish(o)

        # ---- valid observation ----
        self._update_diag(o, hip_speed)
        self._diag["temporal_positive"] = bool(temporal_positive)
        self._diag["temporal_probability"] = float(temporal_probability)
        horizontal_cue = (o.torso_angle_deg >= c.torso_angle_threshold_deg or
                          o.bbox_aspect_ratio >= c.bbox_aspect_ratio_threshold)
        if self._state == NORMAL and not horizontal_cue:
            self._baseline_hip_y = o.hip_y
            self._baseline_ts = o.timestamp_sec
            self._have_baseline = True
            self._gap_since_baseline = False
        if self._state == SUSPECTED and self._have_baseline:
            self._max_drop = max(self._max_drop, o.hip_y - self._baseline_hip_y)
        if hip_speed >= c.hip_drop_speed_threshold:
            self._last_fast_drop = o.timestamp_sec

        if not self._initialized:
            self._initialized = True
            # Starting while somebody is already lying is not evidence of a
            # fall. A fresh detector always needs motion/history first.
        else:
            lying = bool(self._diag["lying_posture"])
            cooldown = o.timestamp_sec < self._cooldown_until

            if self._state == NORMAL:
                # Geometry is the only way out of NORMAL.  A temporal-positive
                # window on its own (e.g. somebody sitting down quickly) must
                # not raise an alarm without the hip-drop + horizontal arming.
                fast_drop = (self._last_fast_drop >= 0.0 and
                             o.timestamp_sec - self._last_fast_drop
                             <= c.motion_window_sec)
                displaced = (self._have_baseline and self._gap_since_baseline and
                             o.timestamp_sec - self._baseline_ts
                             <= c.motion_window_sec + c.occlusion_grace_sec and
                             o.hip_y - self._baseline_hip_y
                             >= c.hip_drop_distance_threshold)
                if not cooldown and horizontal_cue and (fast_drop or displaced):
                    self._state = SUSPECTED
                    self._suspected_since = (self._last_fast_drop if fast_drop
                                             else o.timestamp_sec)
                    self._last_strong_evidence = o.timestamp_sec if lying else -1.0
                    self._motion_triggered = True
                    self._max_drop = (max(0.0, o.hip_y - self._baseline_hip_y)
                                      if self._have_baseline else 0.0)
            elif self._state == SUSPECTED:
                # The motion feature is latched by the arming drop; the
                # current speed of somebody lying still is ~0.
                evidence = int(self._diag["confirmation_features"])
                enough = evidence >= c.min_suspected_features
                if lying and enough:
                    self._last_strong_evidence = o.timestamp_sec
                if (not cooldown and temporal_available and temporal_positive and
                        lying and enough):
                    # Learned confirmation also needs the current pose lying.
                    self._to_fallen(o)
                else:
                    if (not c.temporal_confirmation_required and
                            self._motion_triggered and lying and enough and
                            self._max_drop >= c.hip_drop_distance_threshold and
                            o.timestamp_sec - self._suspected_since >= c.confirmation_sec):
                        self._to_fallen(o)
                    elif (self._diag["upright_posture"] or
                            (o.timestamp_sec - self._suspected_since >
                             c.suspected_timeout_sec and
                             not (lying and self._in_late_latch(o)))):
                        # Past the timeout only a still-lying candidate inside
                        # the bounded late-confirmation latch is kept.
                        self._to_normal()
            elif self._state == FALLEN:
                if self._diag["upright_posture"]:
                    if c.recovery_window_sec <= 0.0:
                        self._to_normal()
                        self._state = NORMAL
                    else:
                        self._state = RECOVERING
                        self._recovery_since = o.timestamp_sec
            elif self._state == RECOVERING:
                if not self._diag["upright_posture"]:
                    self._state = FALLEN
                    self._recovery_since = -1.0
                elif o.timestamp_sec - self._recovery_since >= c.recovery_window_sec:
                    self._to_normal()

        self._prev_hip_y = o.hip_y
        self._prev_ts = o.timestamp_sec
        self._have_prev = True
        return self._finish(o)

    def _in_late_latch(self, o: Observation) -> bool:
        c = self.config
        return (self._motion_triggered and self._suspected_since >= 0.0 and
                o.timestamp_sec - self._suspected_since
                <= c.suspected_timeout_sec + c.late_confirmation_sec)

    # -- transitions ----------------------------------------------------- #
    def _to_fallen(self, o: Observation) -> None:
        self._state = FALLEN
        self._recovery_since = -1.0
        self._cooldown_until = o.timestamp_sec + self.config.cooldown_sec
        self._event_id += 1
        self._fall_event = True

    def _to_normal(self) -> None:
        self._state = NORMAL
        self._suspected_since = -1.0
        self._last_strong_evidence = -1.0
        self._motion_triggered = False
        self._last_fast_drop = -1.0
        self._max_drop = 0.0
        self._recovery_since = -1.0

    def _update_diag(self, o: Observation, hip_speed: float) -> None:
        feat = self._feature_count(o, hip_speed)
        c = self.config
        # Confirmation evidence: current posture features plus the motion
        # feature, which a motion-armed SUSPECTED candidate has latched.
        motion = (hip_speed >= c.hip_drop_speed_threshold or
                  (self._state == SUSPECTED and self._motion_triggered))
        posture = (int(o.torso_angle_deg >= c.torso_angle_threshold_deg) +
                   int(o.bbox_aspect_ratio >= c.bbox_aspect_ratio_threshold))
        self._diag = {
            "hip_drop_speed": hip_speed,
            "hip_drop_distance": self._max_drop,
            "torso_angle_deg": o.torso_angle_deg,
            "bbox_aspect_ratio": o.bbox_aspect_ratio,
            "evidence_features": feat,
            "evidence_score": feat / 3.0,
            "confirmation_features": posture + int(motion),
            "lying_posture": self._is_lying(o),
            "upright_posture": self._is_upright(o),
        }

"""Offline tests for the fall app's identity + per-track state plumbing."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from types import SimpleNamespace


_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

def _person(box):
    return {"box": list(box), "score": 0.9}


def _load_app_module():
    path = os.path.join(_HERE, "app.py")
    spec = importlib.util.spec_from_file_location("fall_detection_test_app", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _make_app(mod, config):
    """A started-enough FallDetectionApp, without a camera or an NPU.

    `App.start()` binds the manifest `config_schema` keys onto the instance and
    THEN calls `setup()`; these offline tests do the same two steps by hand so
    the auto-bound knobs (`occlusion_grace_sec`, ...) really take effect.
    """
    app = mod.FallDetectionApp()
    with open(os.path.join(_HERE, "manifest.json")) as f:
        app._manifest = json.load(f)
    app._bind_params(config)
    app.setup(config)
    return app


def _pose(box):
    """Make a valid COCO-17 pose with an upright torso."""
    x1, y1, x2, y2 = box
    cx = (x1 + x2) * 0.5
    shoulder_y = y1 + (y2 - y1) * 0.28
    hip_y = y1 + (y2 - y1) * 0.58
    kpts = [[cx, shoulder_y, 0.9] for _ in range(17)]
    kpts[5] = [cx - 8, shoulder_y, 0.9]
    kpts[6] = [cx + 8, shoulder_y, 0.9]
    kpts[11] = [cx - 7, hip_y, 0.9]
    kpts[12] = [cx + 7, hip_y, 0.9]
    return {"box": list(box), "score": 0.9, "keypoints": kpts}


def test_iou_tracker_keeps_ids_through_reordering_and_short_occlusion():
    mod = _load_app_module()
    IoUTracker = mod.IoUTracker
    tracker = IoUTracker(iou_threshold=0.2, max_lost_sec=0.75)
    first = [_person([0, 0, 40, 80]), _person([100, 0, 140, 80])]
    tracks = tracker.update(first, 0.0)
    assert [t.track_id for t in tracks] == [1, 2]

    # The model may return detections in a different score/order sequence.
    swapped = [_person([100, 0, 140, 80]), _person([0, 0, 40, 80])]
    tracks = tracker.update(swapped, 0.1)
    # Use tracker assignments (rather than output order) to annotate the
    # detection and verify each box retained its original identity.
    for tr in tracks:
        assert tr.detection_index is not None
        swapped[tr.detection_index]["track_id"] = tr.track_id
    assert {round(r["box"][0]): r["track_id"] for r in swapped} == {
        100: 2, 0: 1}

    # One empty frame is retained as a lost track, not a new identity.
    lost = tracker.update([], 0.2)
    assert {t.track_id for t in lost} == {1, 2}
    assert all(not t.visible for t in lost)
    assert tracker.expired_ids == []

    returned = [_person([0, 0, 40, 80])]
    tracks = tracker.update(returned, 0.3)
    visible = [t for t in tracks if t.visible]
    assert len(visible) == 1 and visible[0].track_id == 1
    assert visible[0].detection_index == 0
    assert tracker.track_count == 1

    # A track that is missing beyond the configured grace is removed and can
    # never be silently revived by a coincident later box.
    tracker.update([], 1.2)
    assert set(tracker.expired_ids) == {1, 2}
    assert tracker.active_tracks() == []


def test_fall_app_runs_one_detector_per_track_and_feeds_invalid_on_loss():
    mod = _load_app_module()
    app = _make_app(mod, {"keypoint_confidence": 0.5,
                          "occlusion_grace_sec": 0.5})
    frame = SimpleNamespace(pts=0.0, w=200, h=100)

    results = [_pose([0, 0, 50, 90]), _pose([100, 0, 150, 90])]
    events = app._advance_tracks(results, frame)
    assert {r["track_id"] for r in results} == {1, 2}
    assert len(app.detectors) == 2
    assert len(app.temporal_classifiers) == 2
    assert all(r["state"] == "normal" for r in results)
    assert {e["track_id"] for e in events if e["kind"] == "pose_state"} == {1, 2}

    # Brief occlusion advances both independent state machines with invalid
    # observations and retains their identities.
    frame.pts = 0.2
    events = app._advance_tracks([], frame)
    states = [e for e in events if e["kind"] == "pose_state"]
    assert {e["track_id"] for e in states} == {1, 2}
    assert all(e["visible"] is False for e in states)
    assert len(app.detectors) == 2

    # Returning within the grace revives the same id; later timeout removes
    # both detector instances.
    frame.pts = 0.3
    returned = [_pose([100, 0, 150, 90])]
    app._advance_tracks(returned, frame)
    assert returned[0]["track_id"] == 2
    assert returned[0]["state"] == "normal"
    assert returned[0]["fall_detected"] is False
    frame.pts = 1.0
    app._advance_tracks([], frame)
    assert app.detectors == {}
    assert app.temporal_classifiers == {}


def test_temporal_profile_and_strict_confirmation_policy():
    mod = _load_app_module()
    from kit.logic.geometry import Observation
    from kit.logic.temporal import FallConfig, FallDetector

    profile = mod._TemporalProfile(os.path.join(
        _HERE, "models", "temporal_yolo11s_pose_v1.json.gz"))
    assert profile.window == 48
    assert profile.w1.shape == (504, 32)
    assert profile.threshold == 0.8
    classifier = mod._TemporalClassifier(profile)
    evaluated, positive, probability = classifier.update(
        mod.np.zeros(56, dtype=mod.np.float32), 0.0)
    assert evaluated and not positive and 0.0 <= probability <= 1.0

    def observation(ts, hip, torso, aspect, valid=True):
        obs = Observation(ts)
        obs.valid = valid
        obs.hip_y = hip
        obs.torso_angle_deg = torso
        obs.bbox_aspect_ratio = aspect
        obs.person_score = 0.9
        return obs

    cfg = FallConfig(confirmation_sec=0.2, suspected_timeout_sec=2.0,
                     temporal_confirmation_required=True)
    detector = FallDetector(cfg)
    # Even a temporal-positive first lying frame cannot originate an event.
    first = detector.update(observation(0.0, 0.7, 75.0, 1.6),
                            temporal_available=True, temporal_positive=True,
                            temporal_probability=0.99)
    assert first.state == "normal" and not first.fall_event

    detector = FallDetector(cfg)
    detector.update(observation(0.0, 0.4, 10.0, 0.7))
    armed = detector.update(observation(0.2, 0.65, 70.0, 1.5))
    assert armed.state == "suspected"
    geometry_only = detector.update(observation(0.7, 0.68, 75.0, 1.6))
    assert geometry_only.state == "suspected" and not geometry_only.fall_event
    invalid = detector.update(observation(0.8, 0.0, 0.0, 0.0, False),
                              temporal_available=True, temporal_positive=True,
                              temporal_probability=0.99)
    assert invalid.state == "suspected" and not invalid.fall_event
    confirmed = detector.update(observation(0.9, 0.68, 75.0, 1.6),
                                temporal_available=True, temporal_positive=True,
                                temporal_probability=0.99)
    assert confirmed.state == "fallen" and confirmed.fall_event

    legacy = FallDetector(FallConfig(
        confirmation_sec=0.2, suspected_timeout_sec=2.0,
        temporal_confirmation_required=False))
    legacy.update(observation(0.0, 0.4, 10.0, 0.7))
    legacy.update(observation(0.2, 0.65, 70.0, 1.5))
    legacy_out = legacy.update(observation(0.7, 0.68, 75.0, 1.6))
    assert legacy_out.state == "fallen" and legacy_out.fall_event


def _obs(ts, hip, torso, aspect, valid=True):
    from kit.logic.geometry import Observation

    obs = Observation(ts)
    obs.valid = valid
    obs.hip_y = hip
    obs.torso_angle_deg = torso
    obs.bbox_aspect_ratio = aspect
    obs.person_score = 0.9
    return obs


def _replay(detector, frames, temporal_from=0.0):
    """Feed (ts, hip, torso, aspect) frames; temporal is positive from
    ``temporal_from`` onward (p=0.99), mimicking a saturated learned gate."""
    outs = []
    for ts, hip, torso, aspect in frames:
        positive = ts >= temporal_from
        outs.append(detector.update(
            _obs(ts, hip, torso, aspect), temporal_available=True,
            temporal_positive=positive,
            temporal_probability=0.99 if positive else 0.1))
    return outs


def test_upright_person_with_temporal_positive_never_alarms():
    from kit.logic.temporal import FallDetector

    detector = FallDetector()
    frames = [(i / 15.0, 0.45, 8.0, 0.45) for i in range(150)]
    outs = _replay(detector, frames)
    assert not any(o.fall_event for o in outs)
    assert {o.state for o in outs} == {"normal"}


def test_sit_down_with_temporal_positive_never_alarms():
    """Fast hip drop (and a wide seated box) but no lying torso."""
    from kit.logic.temporal import FallDetector

    detector = FallDetector()
    frames = []
    for i in range(150):
        ts = i / 15.0
        if ts < 1.0:
            hip, torso, aspect = 0.45, 8.0, 0.45       # standing
        elif ts < 1.8:
            k = (ts - 1.0) / 0.8                        # sitting down in 0.8 s
            hip, torso, aspect = 0.45 + 0.2 * k, 8.0 + 22.0 * k, 0.45 + 0.9 * k
        else:
            hip, torso, aspect = 0.65, 30.0, 1.35       # seated, wide box
        frames.append((ts, hip, torso, aspect))
    outs = _replay(detector, frames, temporal_from=1.2)
    assert not any(o.fall_event for o in outs)
    assert "fallen" not in {o.state for o in outs}


def test_real_fall_with_temporal_positive_alarms_once():
    from kit.logic.temporal import FallDetector

    detector = FallDetector()
    frames = []
    for i in range(150):
        ts = i / 15.0
        if ts < 1.0:
            hip, torso, aspect = 0.45, 8.0, 0.45        # standing
        elif ts < 1.4:
            k = (ts - 1.0) / 0.4                        # falling in 0.4 s
            hip, torso, aspect = 0.45 + 0.3 * k, 8.0 + 72.0 * k, 0.45 + 2.0 * k
        else:
            hip, torso, aspect = 0.75, 80.0, 2.45       # lying on the floor
        frames.append((ts, hip, torso, aspect))
    outs = _replay(detector, frames, temporal_from=1.6)
    edges = [i for i, o in enumerate(outs) if o.fall_event]
    assert len(edges) == 1 and outs[edges[0]].event_id == 1
    # FALLEN was reached through SUSPECTED, never straight from NORMAL.
    assert outs[edges[0] - 1].state == "suspected"


def test_temporal_positive_while_suspected_needs_current_lying_pose():
    from kit.logic.temporal import FallConfig, FallDetector

    detector = FallDetector(FallConfig(suspected_timeout_sec=3.0))
    detector.update(_obs(0.0, 0.4, 10.0, 0.7))
    armed = detector.update(_obs(0.2, 0.65, 40.0, 1.5))   # wide box, not lying
    assert armed.state == "suspected"
    not_lying = detector.update(_obs(0.3, 0.66, 40.0, 1.5),
                                temporal_available=True,
                                temporal_positive=True,
                                temporal_probability=0.99)
    assert not_lying.state == "suspected" and not not_lying.fall_event
    lying = detector.update(_obs(0.4, 0.68, 75.0, 1.6),
                            temporal_available=True, temporal_positive=True,
                            temporal_probability=0.99)
    assert lying.state == "fallen" and lying.fall_event


class _EdgeOnFirstUpdate:
    """Stub detector: reports a fall edge on its first visible update."""

    def __init__(self):
        from kit.logic.temporal import FallOutput

        self._out = FallOutput
        self.fired = False

    def update(self, obs, **_kwargs):
        edge = bool(obs.valid) and not self.fired
        self.fired = self.fired or edge
        return self._out(state="fallen" if self.fired else "normal",
                         fall_detected=self.fired, fall_event=edge,
                         event_id=1 if self.fired else 0)


def test_location_cooldown_survives_track_churn():
    mod = _load_app_module()
    app = _make_app(mod, {"occlusion_grace_sec": 0.2,
                          "location_cooldown_sec": 10.0})
    assert app.location_cooldown_sec == 10.0
    detectors = {}

    def detector_for(track_id):
        return detectors.setdefault(track_id, _EdgeOnFirstUpdate())

    app._detector_for = detector_for
    frame = SimpleNamespace(w=640, h=480, pts=0.0)
    here = (100, 300, 300, 380)        # lying person
    here_jitter = (110, 296, 305, 384)
    far = (420, 40, 480, 200)

    def step(pts, boxes):
        frame.pts = pts
        return [e for e in app._advance_tracks([_pose(b) for b in boxes], frame)
                if e["kind"] == "fall"]

    falls = step(0.0, [here])
    assert len(falls) == 1 and falls[0]["event_id"] == 1
    first_track = falls[0]["track_id"]
    # Same person re-detected under fresh track ids within the cooldown.
    for pts in (1.0, 2.0, 3.0):
        assert step(pts, []) == []           # track lost and expired
        assert step(pts + 0.5, [here_jitter]) == []
    assert len(detectors) == 4 and first_track in detectors
    # A second person far away still alarms, with its own track's event id.
    falls = step(4.0, [here_jitter, far])
    assert len(falls) == 1 and falls[0]["event_id"] == 1
    assert falls[0]["track_id"] not in (first_track,)
    # Once the location has been quiet for the whole window it may alarm again.
    step(5.0, [])
    falls = step(20.0, [here])
    assert len(falls) == 1


def test_location_cooldown_zero_disables_suppression():
    mod = _load_app_module()
    app = _make_app(mod, {"occlusion_grace_sec": 0.2,
                          "location_cooldown_sec": 0.0})
    app._detector_for = lambda tid, d={}: d.setdefault(tid, _EdgeOnFirstUpdate())
    frame = SimpleNamespace(w=640, h=480, pts=0.0)
    count = 0
    for pts in (0.0, 1.0, 2.0):
        for p, boxes in ((pts, [(100, 300, 300, 380)]), (pts + 0.5, [])):
            frame.pts = p
            count += sum(e["kind"] == "fall" for e in app._advance_tracks(
                [_pose(b) for b in boxes], frame))
    assert count == 3


# --------------------------------------------------------------------------- #
# Arming / confirmation timing (kit.logic.temporal) on 15 fps sequences.
# --------------------------------------------------------------------------- #
_FPS = 15.0
_STAND = (8.0, 0.45)                     # torso deg, box aspect
_LIE = (80.0, 2.45)


def _fall_frames(t_fall, n, fall_sec=0.4, hip0=0.45, hip1=0.75,
                 stand_up_at=None, gap=None):
    """Standing, a ``fall_sec`` fall starting at ``t_fall``, then lying.

    ``gap=(a, b)`` marks frames in [a, b) as invalid pose (occlusion);
    ``stand_up_at`` switches back to an upright pose from that time.
    Yields (ts, hip, torso, aspect, valid).
    """
    for i in range(n):
        ts = i / _FPS
        if stand_up_at is not None and ts >= stand_up_at:
            hip, (torso, aspect) = hip0, _STAND
        elif ts < t_fall:
            hip, (torso, aspect) = hip0, _STAND
        elif fall_sec > 0 and ts < t_fall + fall_sec:
            k = (ts - t_fall) / fall_sec
            hip = hip0 + (hip1 - hip0) * k
            torso = _STAND[0] + (_LIE[0] - _STAND[0]) * k
            aspect = _STAND[1] + (_LIE[1] - _STAND[1]) * k
        else:
            hip, (torso, aspect) = hip1, _LIE
        valid = not (gap and gap[0] <= ts < gap[1])
        yield ts, hip, torso, aspect, valid


def _run(detector, frames, positive_from):
    outs = []
    for ts, hip, torso, aspect, valid in frames:
        positive = ts >= positive_from
        outs.append(detector.update(
            _obs(ts, hip, torso, aspect, valid), temporal_available=True,
            temporal_positive=positive,
            temporal_probability=0.99 if positive else 0.1))
    return outs


def _edges(outs):
    return [o for o in outs if o.fall_event]


def test_occluded_fall_arms_by_displacement_and_alarms_once():
    """Pose invalid through most of the fall: the hip speed measured across
    the gap (0.2 / 1.0 s) stays under the 0.25/s threshold."""
    from kit.logic.temporal import FallDetector

    detector = FallDetector()
    frames = list(_fall_frames(1.0, 150, fall_sec=0.0, hip0=0.40, hip1=0.60,
                               gap=(1.0, 2.0)))
    outs = _run(detector, frames, positive_from=2.4)
    speeds = [o.diagnostics["hip_drop_speed"] for o in outs]
    assert max(speeds) < 0.25
    assert len(_edges(outs)) == 1


def test_occluded_sit_down_never_alarms():
    from kit.logic.temporal import FallDetector

    detector = FallDetector()
    frames = []
    for ts, hip, _t, _a, valid in _fall_frames(
            1.0, 150, fall_sec=0.0, hip0=0.45, hip1=0.65,
            gap=(1.0, 2.0)):
        seated = ts >= 1.0
        frames.append((ts, hip, 30.0 if seated else 8.0,
                       1.35 if seated else 0.45, valid))
    outs = _run(detector, frames, positive_from=1.2)
    assert not _edges(outs)


def test_late_temporal_positive_while_still_lying_confirms_once():
    from kit.logic.temporal import FallDetector

    detector = FallDetector()          # suspected_timeout 1.5 s
    # Armed at ~1.1 s; the learned gate only turns positive 2.9 s later.
    outs = _run(detector, _fall_frames(1.0, 150), positive_from=4.0)
    assert len(_edges(outs)) == 1
    assert _edges(outs)[0].event_id == 1


def test_late_temporal_positive_after_standing_up_does_not_alarm():
    from kit.logic.temporal import FallDetector

    detector = FallDetector()
    outs = _run(detector, _fall_frames(1.0, 150, stand_up_at=3.0),
                positive_from=4.0)
    assert not _edges(outs)
    assert outs[-1].state == "normal"


def test_late_confirmation_latch_is_bounded():
    from kit.logic.temporal import FallDetector

    detector = FallDetector()
    # Positive arrives long after the arming drop: the latch has expired.
    outs = _run(detector, _fall_frames(1.0, 200), positive_from=9.0)
    assert not _edges(outs)
    assert outs[-1].state == "normal"


def test_min_features_three_confirms_stationary_lying_victim():
    """Confirmation counts the latched arming motion, not the current hip
    speed, which is ~0 once the victim lies still."""
    from kit.logic.temporal import FallConfig, FallDetector

    detector = FallDetector(FallConfig(min_suspected_features=3))
    outs = _run(detector, _fall_frames(1.0, 150), positive_from=1.9)
    edges = _edges(outs)
    assert len(edges) == 1
    assert edges[0].diagnostics["hip_drop_speed"] == 0.0


# --------------------------------------------------------------------------- #
# Location cooldown vs. identity (real FallDetector, stubbed temporal gate).
# --------------------------------------------------------------------------- #
def _lying_pose(box):
    x1, y1, x2, y2 = box
    y = (y1 + y2) * 0.5
    kpts = [[x1 + (x2 - x1) * 0.2, y, 0.9] for _ in range(17)]
    kpts[5] = [x1 + (x2 - x1) * 0.25, y - 4, 0.9]
    kpts[6] = [x1 + (x2 - x1) * 0.25, y + 4, 0.9]
    kpts[11] = [x1 + (x2 - x1) * 0.6, y - 4, 0.9]
    kpts[12] = [x1 + (x2 - x1) * 0.6, y + 4, 0.9]
    return {"box": list(box), "score": 0.9, "keypoints": kpts}


def _falling_pose(stand_box, lie_box, k):
    """Linear blend of an upright and a lying pose, k in [0, 1]."""
    a, b = _pose(stand_box), _lying_pose(lie_box)
    mix = lambda u, v: [p + (q - p) * k for p, q in zip(u, v)]
    return {"box": mix(a["box"], b["box"]), "score": 0.9,
            "keypoints": [mix(p, q) for p, q in zip(a["keypoints"],
                                                    b["keypoints"])]}


def _person_at(ts, stand_box, lie_box, t_fall, fall_sec=0.4):
    if ts < t_fall:
        return _pose(stand_box)
    if ts < t_fall + fall_sec:
        return _falling_pose(stand_box, lie_box, (ts - t_fall) / fall_sec)
    return _lying_pose(lie_box)


class _PositiveFrom:
    """Temporal gate stub: positive from ``t`` on (a saturated learned gate)."""

    def __init__(self, t):
        self.t = t

    def update(self, _frame, ts):
        positive = ts >= self.t
        return True, positive, 0.99 if positive else 0.1


def _identity_app(mod, positive_from, **config):
    app = _make_app(mod, dict({"occlusion_grace_sec": 0.75,
                               "location_cooldown_sec": 10.0}, **config))
    app._temporal_for = lambda _tid: _PositiveFrom(positive_from)
    app.recordings = []
    mod.request_configured_recording = (
        lambda _app, kind, pts: app.recordings.append((kind, pts)) or True)
    return app


_A_STAND, _A_LIE = (170, 100, 230, 380), (100, 300, 300, 380)
_B_STAND, _B_LIE = (270, 200, 330, 480), (100, 400, 300, 480)


def _replay_people(app, people, n):
    """people: [(stand_box, lie_box, t_fall)]; returns the fall events."""
    frame = SimpleNamespace(w=640, h=480, pts=0.0)
    falls = []
    for i in range(n):
        frame.pts = i / 15.0
        results = [_person_at(frame.pts, s, l, t) for s, l, t in people]
        falls += [e for e in app._advance_tracks(results, frame)
                  if e["kind"] == "fall"]
    return falls


def test_boxes_used_by_identity_tests_are_disjoint_but_near():
    mod = _load_app_module()
    assert mod.IoUTracker._iou(list(_A_LIE), list(_B_LIE)) == 0.0
    assert mod._boxes_near(_A_LIE, _B_LIE)


def test_two_people_falling_near_each_other_at_once_both_alarm():
    mod = _load_app_module()
    app = _identity_app(mod, positive_from=1.8)
    falls = _replay_people(app, [(_A_STAND, _A_LIE, 1.0),
                                 (_B_STAND, _B_LIE, 1.0)], 60)
    assert len({e["track_id"] for e in falls}) == 2
    assert len(falls) == 2
    assert [k for k, _ in app.recordings] == ["fall", "fall"]


def test_second_nearby_fall_while_first_person_still_visible_alarms():
    mod = _load_app_module()
    app = _identity_app(mod, positive_from=1.8)
    falls = _replay_people(app, [(_A_STAND, _A_LIE, 1.0),
                                 (_B_STAND, _B_LIE, 3.0)], 75)
    assert len(falls) == 2
    assert falls[0]["track_id"] != falls[1]["track_id"]
    assert len(app.recordings) == 2


def test_location_cooldown_window_is_not_extended_by_suppressed_edges():
    mod = _load_app_module()
    app = _make_app(mod, {"occlusion_grace_sec": 0.2,
                          "location_cooldown_sec": 10.0})
    app._detector_for = lambda tid, d={}: d.setdefault(tid, _EdgeOnFirstUpdate())
    frame = SimpleNamespace(w=640, h=480, pts=0.0)

    def step(pts, boxes):
        frame.pts = pts
        return [e for e in app._advance_tracks([_pose(b) for b in boxes], frame)
                if e["kind"] == "fall"]

    here = (100, 300, 300, 380)
    assert len(step(0.0, [here])) == 1
    # Re-detections under new ids (old id gone) every second are suppressed...
    for pts in range(1, 10):
        assert step(pts, []) == []
        assert step(pts + 0.5, [here]) == []
    # ...but the window runs from the emitted event only (0.0 + 10.0).
    step(10.2, [])
    assert len(step(10.5, [here])) == 1

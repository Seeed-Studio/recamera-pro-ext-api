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

"""
Unit tests for crayfish-fight's cross-frame logic (apps/crayfish-fight/logic.py).

No device, no numpy, no rknn: `logic.py` is deliberately free of them, so the
trigger geometry, the sex vote, the behaviour state machine and the capture
quota are all testable on the Mac.

Run:  uv run pytest apps/crayfish-fight/tests/ -q
"""
import os
import sys

import pytest

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

from logic import (BehaviorStateMachine, CaptureQuota, ProximityWindow,  # noqa: E402
                   SexVoter, SEX_UNKNOWN, box_iou, pair_is_close, pair_key,
                   to_norm, union_box)


# --------------------------------------------------------------------------- #
# geometry / trigger predicate
# --------------------------------------------------------------------------- #
def test_pair_is_close_on_any_overlap():
    a = [0, 0, 100, 100]
    b = [90, 90, 190, 190]
    assert box_iou(a, b) > 0
    # overlapping boxes trigger regardless of how small k is
    assert pair_is_close(a, b, 0.01)


def test_pair_is_close_scales_with_box_size():
    """One k works at any object scale: the gap is measured in box diagonals."""
    small_a, small_b = [0, 0, 10, 10], [15, 0, 25, 10]      # gap 5, diag ~14
    big_a, big_b = [0, 0, 100, 100], [150, 0, 250, 100]     # gap 50, diag ~141
    assert pair_is_close(small_a, small_b, 1.2)
    assert pair_is_close(big_a, big_b, 1.2)
    # ...and both fall out together when the gap grows past the same factor
    far_a, far_b = [0, 0, 10, 10], [60, 0, 70, 10]
    assert not pair_is_close(far_a, far_b, 1.2)


def test_pair_is_close_boundary_is_strict():
    # zero-height boxes: each diagonal == its width == 100, centres 50 and 250,
    # so the centre distance is exactly 2.0 x the mean diagonal.
    a, b = [0, 0, 100, 0], [200, 0, 300, 0]
    assert not pair_is_close(a, b, 2.0)       # strict `<`: the boundary is out
    assert pair_is_close(a, b, 2.01)


def test_union_box_pads_and_clips():
    u = union_box([10, 10, 20, 20], [30, 30, 40, 40], pad=0.0)
    assert u == [10, 10, 40, 40]
    p = union_box([10, 10, 20, 20], [30, 30, 40, 40], pad=0.2)
    assert p == pytest.approx([7.0, 7.0, 43.0, 43.0])
    c = union_box([-50, -50, 20, 20], [30, 30, 400, 400], pad=0.5,
                  clip=(200, 200))
    assert c == [0.0, 0.0, 200.0, 200.0]


def test_to_norm():
    assert to_norm([0, 0, 960, 540], 1920, 1080) == [0.0, 0.0, 0.5, 0.5]


def test_pair_key_is_order_free():
    assert pair_key(7, 3) == pair_key(3, 7) == (3, 7)


# --------------------------------------------------------------------------- #
# sex voting
# --------------------------------------------------------------------------- #
def test_sex_vote_majority_and_settling():
    v = SexVoter(vote_frames=5, min_conf=0.6)
    assert v.needs_vote(1)
    for label, conf in [("male", 0.9), ("female", 0.8), ("male", 0.7),
                        ("male", 0.95), ("female", 0.9)]:
        v.add(1, label, conf)
    assert not v.needs_vote(1)          # settled after vote_frames
    assert v.sex_of(1) == "male"        # 3 male vs 2 female
    assert v.verdict(1).confidence == pytest.approx(0.6)


def test_sex_vote_ignores_low_confidence_frames_as_unknown():
    v = SexVoter(vote_frames=4, min_conf=0.65)
    v.add(1, "female", 0.9)
    v.add(1, "female", 0.2)             # -> counted as unknown
    v.add(1, "male", 0.3)               # -> counted as unknown
    v.add(1, "female", 0.8)
    assert v.sex_of(1) == "female"      # 2 female vs 2 unknown -> label wins tie


def test_sex_vote_all_low_confidence_yields_unknown():
    v = SexVoter(vote_frames=3, min_conf=0.65)
    for _ in range(3):
        v.add(1, "male", 0.4)
    assert v.sex_of(1) == SEX_UNKNOWN


def test_sex_vote_stops_after_settling():
    v = SexVoter(vote_frames=2, min_conf=0.5)
    v.add(1, "male", 0.9)
    v.add(1, "male", 0.9)
    v.add(1, "female", 0.99)            # ignored: track already settled
    assert v.sex_of(1) == "male"
    assert v.verdict(1).frames == 2


def test_sex_vote_drop_frees_dead_tracks():
    v = SexVoter()
    v.add(1, "male", 0.9)
    v.add(2, "female", 0.9)
    v.drop([1])
    assert len(v) == 1 and v.sex_of(1) == SEX_UNKNOWN


# --------------------------------------------------------------------------- #
# proximity window
# --------------------------------------------------------------------------- #
def test_proximity_needs_min_hits_in_window():
    w = ProximityWindow(window=8, min_hits=5)
    key = (1, 2)
    for i in range(4):
        assert w.update({key: True}) == []
    assert w.update({key: True}) == [key]      # 5th close frame trips it


def test_proximity_tolerates_a_dropped_frame():
    """5-of-8, not 5-consecutive: one missed detection must not reset."""
    w = ProximityWindow(window=8, min_hits=5)
    key = (1, 2)
    seq = [True, True, False, True, True, True]
    fired = [w.update({key: s}) for s in seq]
    assert fired[-1] == [key]
    assert all(f == [] for f in fired[:-1])


def test_proximity_decays_when_pair_separates():
    w = ProximityWindow(window=4, min_hits=3)
    key = (1, 2)
    for _ in range(4):
        w.update({key: True})
    assert w.update({key: True}) == [key]
    for _ in range(2):
        w.update({key: False})
    assert w.update({key: False}) == []


def test_proximity_forgets_unobserved_pairs():
    w = ProximityWindow(window=4, min_hits=2, forget_after=3)
    w.update({(1, 2): True})
    assert len(w) == 1
    for _ in range(3):
        w.update({})
    assert len(w) == 0


def test_proximity_drop_track_removes_all_its_pairs():
    w = ProximityWindow()
    w.update({(1, 2): True, (1, 3): True, (2, 3): True})
    w.drop_track(1)
    assert len(w) == 1


# --------------------------------------------------------------------------- #
# behaviour state machine
# --------------------------------------------------------------------------- #
KEY = (1, 2)


def _feed(fsm, seq, t0=0.0):
    """Feed (label, conf) pairs one per second; return the events raised."""
    out = []
    for i, (label, conf) in enumerate(seq):
        ev = fsm.update(KEY, label, conf, t0 + i)
        if ev is not None:
            out.append(ev)
    return out


def test_behavior_needs_consecutive_confirmations():
    fsm = BehaviorStateMachine(min_streak=3, release_frames=3, min_conf=0.5)
    assert _feed(fsm, [("fight", 0.9), ("fight", 0.9)]) == []
    ev = fsm.update(KEY, "fight", 0.9, 2.0)
    assert ev["phase"] == "start" and ev["label"] == "fight"


def test_behavior_streak_resets_on_a_none_frame():
    fsm = BehaviorStateMachine(min_streak=3)
    evs = _feed(fsm, [("fight", 0.9), ("fight", 0.9), ("none", 0.9),
                      ("fight", 0.9), ("fight", 0.9)])
    assert evs == []                     # never reached 3 in a row
    assert fsm.active_label(KEY) is None


def test_behavior_low_confidence_does_not_confirm():
    fsm = BehaviorStateMachine(min_streak=2, min_conf=0.6)
    assert _feed(fsm, [("fight", 0.5), ("fight", 0.55)]) == []


def test_behavior_hysteresis_survives_a_single_bad_frame():
    fsm = BehaviorStateMachine(min_streak=2, release_frames=3)
    _feed(fsm, [("fight", 0.9), ("fight", 0.9)])
    assert fsm.active_label(KEY) == "fight"
    assert fsm.update(KEY, "none", 0.9, 5.0) is None       # miss 1
    assert fsm.update(KEY, "fight", 0.9, 6.0) is None      # recovered
    assert fsm.active_label(KEY) == "fight"


def test_behavior_ends_after_release_frames():
    fsm = BehaviorStateMachine(min_streak=2, release_frames=3)
    _feed(fsm, [("fight", 0.9), ("fight", 0.9)])
    assert fsm.update(KEY, "none", 0.9, 5.0) is None
    assert fsm.update(KEY, "none", 0.9, 6.0) is None
    end = fsm.update(KEY, "none", 0.9, 7.0)
    assert end["phase"] == "end" and end["label"] == "fight"
    assert fsm.active_label(KEY) is None


def test_behavior_switches_label_without_dropping_to_idle():
    fsm = BehaviorStateMachine(min_streak=2, release_frames=5)
    _feed(fsm, [("fight", 0.9), ("fight", 0.9)])
    assert fsm.update(KEY, "harass", 0.9, 3.0) is None
    ev = fsm.update(KEY, "harass", 0.9, 4.0)
    assert ev["phase"] == "change"
    assert ev["label"] == "harass" and ev["previous"] == "fight"


def test_behavior_event_carries_duration():
    fsm = BehaviorStateMachine(min_streak=1, release_frames=1)
    start = fsm.update(KEY, "fight", 0.9, 10.0)
    assert start["duration_sec"] == 0.0
    fsm.update(KEY, "fight", 0.9, 11.0)
    fsm.update(KEY, "fight", 0.9, 12.0)
    end = fsm.update(KEY, "none", 0.9, 13.0)
    assert end["phase"] == "end" and end["duration_sec"] == pytest.approx(2.0)


def test_behavior_timeout_closes_an_abandoned_event():
    """A pair that leaves the trigger window stops calling update(); without
    timeout() its event would stay open forever."""
    fsm = BehaviorStateMachine(min_streak=1, release_frames=2)
    fsm.update(KEY, "fight", 0.9, 1.0)
    assert fsm.timeout(KEY, 2.0) is None
    ev = fsm.timeout(KEY, 3.0)
    assert ev["phase"] == "end" and ev["label"] == "fight"
    assert len(fsm) == 0


def test_behavior_timeout_on_idle_pair_is_silent():
    fsm = BehaviorStateMachine()
    assert fsm.timeout((9, 9), 1.0) is None


def test_behavior_none_never_raises():
    fsm = BehaviorStateMachine(min_streak=1)
    assert _feed(fsm, [("none", 0.99)] * 10) == []


# --------------------------------------------------------------------------- #
# capture quota
# --------------------------------------------------------------------------- #
def test_quota_per_minute():
    q = CaptureQuota(per_minute=3, per_day=100)
    assert [q.allow(1000.0 + i, "2026-08-24") for i in range(5)] == \
        [True, True, True, False, False]
    assert q.allow(1061.0, "2026-08-24")      # first slot aged out of the 60 s


def test_quota_per_day_and_rollover():
    q = CaptureQuota(per_minute=60, per_day=2)
    t = 0.0
    assert q.allow(t, "2026-08-24")
    assert q.allow(t + 1, "2026-08-24")
    assert not q.allow(t + 2, "2026-08-24")
    assert q.allow(t + 3, "2026-08-25")       # new day resets the counter
    assert q.today_count == 1


def test_quota_zero_disables():
    assert not CaptureQuota(per_minute=0).allow(0.0, "d")
    assert not CaptureQuota(per_day=0).allow(0.0, "d")

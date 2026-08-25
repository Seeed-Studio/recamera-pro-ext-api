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

from logic import (BehaviorStateMachine, CaptureDecider, CaptureQuota,  # noqa: E402
                   PairFrameBuffer, ProximityWindow, SexVoter, SEX_UNKNOWN,
                   TEMPORAL_COLS, TEMPORAL_FRAMES, TEMPORAL_INPUT,
                   TEMPORAL_LABELS, TEMPORAL_ROWS, TEMPORAL_TILE,
                   TemporalScheduler, box_iou, collage_canvas_size,
                   collage_slots, pair_is_close, pair_key, to_norm, union_box)


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


def test_quota_minute_fraction_tracks_usage_and_ages_out():
    q = CaptureQuota(per_minute=4, per_day=100)
    assert q.minute_fraction(0.0) == 0.0
    q.allow(0.0, "d")
    q.allow(0.0, "d")
    assert q.minute_fraction(0.0) == pytest.approx(0.5)
    # the same two writes age out of the 60 s window
    assert q.minute_fraction(61.0) == 0.0


def test_quota_minute_fraction_disabled_reads_as_full():
    assert CaptureQuota(per_minute=0).minute_fraction(0.0) == 1.0


# --------------------------------------------------------------------------- #
# capture decision (alarm / suspect / plain, + capture_mode)
# --------------------------------------------------------------------------- #
def test_decider_alarm_always_wins_even_under_quota_pressure():
    d = CaptureDecider(suspect_conf=0.3)
    assert d.decide(is_alarm=True, label="none", confidence=0.1,
                    quota_fraction=0.99) == "alarm"


def test_decider_suspect_on_raw_verdict_before_confirmation():
    d = CaptureDecider(suspect_conf=0.3)
    assert d.decide(is_alarm=False, label="fight", confidence=0.5,
                    quota_fraction=0.0) == "suspect"
    # below suspect_conf and not yet confirmed -> falls through to plain
    assert d.decide(is_alarm=False, label="fight", confidence=0.2,
                    quota_fraction=0.0) == "plain"


def test_decider_default_mode_captures_plain_triggers():
    """capture_mode='trigger' (the default): every proximity trigger is still
    captured, tagged 'plain' when it carries no behaviour signal."""
    d = CaptureDecider()
    assert d.decide(is_alarm=False, label="none", confidence=0.9,
                    quota_fraction=0.0) == "plain"


def test_decider_plain_yields_once_quota_pressure_hits_half():
    d = CaptureDecider()
    assert d.decide(is_alarm=False, label="none", confidence=0.9,
                    quota_fraction=0.49) == "plain"
    assert d.decide(is_alarm=False, label="none", confidence=0.9,
                    quota_fraction=0.5) is None
    assert d.decide(is_alarm=False, label="none", confidence=0.9,
                    quota_fraction=0.9) is None
    # alarm/suspect are unaffected by the same pressure
    assert d.decide(is_alarm=True, label="none", confidence=0.1,
                    quota_fraction=0.9) == "alarm"
    assert d.decide(is_alarm=False, label="fight", confidence=0.9,
                    quota_fraction=0.9) == "suspect"


def test_decider_alarm_gated_mode_skips_plain_entirely():
    d = CaptureDecider(suspect_conf=0.3, mode="alarm_gated")
    assert d.decide(is_alarm=False, label="none", confidence=0.9,
                    quota_fraction=0.0) is None
    assert d.decide(is_alarm=False, label="fight", confidence=0.5,
                    quota_fraction=0.0) == "suspect"
    assert d.decide(is_alarm=True, label="none", confidence=0.1,
                    quota_fraction=0.0) == "alarm"


def test_decider_unknown_mode_falls_back_to_trigger():
    d = CaptureDecider(mode="not-a-real-mode")
    assert d.mode == "trigger"
    assert d.decide(is_alarm=False, label="none", confidence=0.9,
                    quota_fraction=0.0) == "plain"


# --------------------------------------------------------------------------- #
# temporal collage geometry
#
# The collage layout is the one thing that can be wrong while still producing a
# perfectly plausible-looking image, so it is asserted directly: 8 solid-colour
# tiles are painted into a pure-Python canvas through `collage_slots()` and
# every cell is checked for the colour that belongs there. Must stay identical
# to scripts/temporal_proto.py:collage() -- the model learned THAT grid.
# --------------------------------------------------------------------------- #
def _paint(tile=TEMPORAL_TILE):
    """Paint 8 solid colours (1..8) through the slots into a w*h canvas."""
    w, h = collage_canvas_size(tile)
    canvas = [[0] * w for _ in range(h)]
    for i, (x0, y0, x1, y1) in enumerate(collage_slots(tile)):
        for y in range(y0, y1):
            row = canvas[y]
            for x in range(x0, x1):
                row[x] = i + 1
    return canvas, w, h


def test_collage_canvas_is_two_by_four_tiles():
    assert collage_canvas_size(TEMPORAL_TILE) == (896, 448)
    assert (TEMPORAL_ROWS, TEMPORAL_COLS) == (2, 4)
    assert TEMPORAL_ROWS * TEMPORAL_COLS == TEMPORAL_FRAMES == 8
    # the final resize is an exact 2:1 horizontal decimation, nothing else
    assert collage_canvas_size(TEMPORAL_TILE)[0] == 2 * TEMPORAL_INPUT
    assert collage_canvas_size(TEMPORAL_TILE)[1] == TEMPORAL_INPUT


def test_collage_slots_are_reading_order():
    """Frame i -> row i//4, col i%4: oldest top-left, newest bottom-right."""
    slots = collage_slots(TEMPORAL_TILE)
    assert len(slots) == 8
    t = TEMPORAL_TILE
    assert slots[0] == (0, 0, t, t)                  # first frame: top-left
    assert slots[3] == (3 * t, 0, 4 * t, t)          # 4th: top-right
    assert slots[4] == (0, t, t, 2 * t)              # 5th: wraps to row 2
    assert slots[7] == (3 * t, t, 4 * t, 2 * t)      # last: bottom-right


def test_collage_slots_tile_the_canvas_exactly():
    """No gap, no overlap: every pixel of 896x448 belongs to exactly one frame."""
    canvas, w, h = _paint(tile=8)          # small tile, same arithmetic
    flat = [v for row in canvas for v in row]
    assert 0 not in flat                                    # full coverage
    for i in range(1, 9):
        assert flat.count(i) == 8 * 8                       # equal, non-overlapping


def test_collage_grid_positions_of_eight_solid_frames():
    """Sample the centre of each of the 8 cells: it must hold that frame."""
    t = TEMPORAL_TILE
    canvas, _, _ = _paint(t)
    for i in range(8):
        cx = (i % TEMPORAL_COLS) * t + t // 2
        cy = (i // TEMPORAL_COLS) * t + t // 2
        assert canvas[cy][cx] == i + 1
    # and the corners of the whole canvas are frames 0 and 7
    assert canvas[0][0] == 1
    assert canvas[2 * t - 1][4 * t - 1] == 8


def test_temporal_labels_match_the_single_frame_vocabulary():
    """runs/behavior_temporal_v3/weights/best.pt names == fight/harass/none."""
    assert TEMPORAL_LABELS == ["fight", "harass", "none"]


# --------------------------------------------------------------------------- #
# PairFrameBuffer
# --------------------------------------------------------------------------- #
def test_buffer_is_ready_only_at_eight_frames():
    buf = PairFrameBuffer()
    for i in range(TEMPORAL_FRAMES - 1):
        buf.push((1, 2), f"crop{i}", float(i))
        assert not buf.ready((1, 2))
        assert buf.sequence((1, 2)) is None
    buf.push((1, 2), "crop7", 7.0)
    assert buf.ready((1, 2))
    assert buf.sequence((1, 2)) == [f"crop{i}" for i in range(7)] + ["crop7"]
    assert buf.ready_keys() == [(1, 2)]


def test_buffer_ring_keeps_the_newest_eight():
    buf = PairFrameBuffer()
    for i in range(12):
        buf.push((1, 2), i, float(i))
    assert buf.sequence((1, 2)) == [4, 5, 6, 7, 8, 9, 10, 11]


def test_buffer_evicts_the_shortest_close_pairs_first():
    """Over `max_pairs`, the pairs close LONGEST survive (PLAN: a sustained
    approach is what an event looks like; three adjacent frames is a fly-by)."""
    buf = PairFrameBuffer(max_pairs=2)
    buf.push((1, 2), "a", 0.0)
    buf.push((1, 2), "a", 10.0)            # duration 10
    buf.push((3, 4), "b", 0.0)
    buf.push((3, 4), "b", 5.0)             # duration 5
    assert len(buf) == 2
    buf.push((5, 6), "c", 100.0)           # duration 0 -> evicted immediately
    assert len(buf) == 2
    assert buf.duration((1, 2)) == 10.0
    assert buf.duration((3, 4)) == 5.0
    assert buf.duration((5, 6)) == 0.0     # gone: unknown pairs read as 0


def test_buffer_eviction_is_deterministic_on_ties():
    buf = PairFrameBuffer(max_pairs=2)
    for key in ((3, 4), (1, 2), (5, 6)):   # all duration 0
        buf.push(key, "x", 0.0)
    assert sorted(buf.ready_keys() or []) == []
    assert len(buf) == 2
    # ties break on the key, so the two lowest survive -- not "whoever pushed"
    assert buf.duration((1, 2)) == 0.0 and buf.duration((3, 4)) == 0.0
    remaining = {k for k in ((1, 2), (3, 4), (5, 6)) if k in buf._seqs}
    assert remaining == {(1, 2), (3, 4)}


def test_buffer_retain_drops_pairs_that_stopped_being_triggered():
    buf = PairFrameBuffer()
    for key in ((1, 2), (3, 4), (5, 6)):
        buf.push(key, "x", 0.0)
    assert buf.retain([(1, 2), (5, 6)]) == [(3, 4)]
    assert len(buf) == 2
    assert buf.retain([]) == [(1, 2), (5, 6)]
    assert len(buf) == 0


def test_buffer_drop_track_removes_every_pair_of_that_track():
    buf = PairFrameBuffer()
    for key in ((1, 2), (1, 3), (2, 3)):
        buf.push(key, "x", 0.0)
    buf.drop_track(1)
    assert len(buf) == 1
    assert buf.duration((2, 3)) == 0.0 and (2, 3) in buf._seqs


# --------------------------------------------------------------------------- #
# TemporalScheduler
# --------------------------------------------------------------------------- #
def _ready(sched, keys, frames):
    """Run `frames` frames with a fixed ready set; return the picks per frame."""
    return [sched.select(keys) for _ in range(frames)]


def test_scheduler_respects_the_per_frame_budget():
    sched = TemporalScheduler(stride=1, budget=1)
    picks = _ready(sched, [(1, 2), (3, 4), (5, 6)], 3)
    assert all(len(p) <= 1 for p in picks)


def test_scheduler_rotates_over_ready_pairs():
    """Budget 1, stride 1, 3 ready pairs -> each is served in turn, so no pair
    starves behind the lowest track id."""
    sched = TemporalScheduler(stride=1, budget=1)
    keys = [(1, 2), (3, 4), (5, 6)]
    picks = [p[0] for p in _ready(sched, keys, 6)]
    assert picks[:3] == keys              # never-run first, in key order
    assert picks[3:] == keys              # then round-robin, least-recent first


def test_scheduler_stride_holds_a_pair_back():
    """One ready pair, stride 4: it runs on frames 1, 5, 9 -- not every frame."""
    sched = TemporalScheduler(stride=4, budget=1)
    picks = _ready(sched, [(1, 2)], 10)
    ran = [i + 1 for i, p in enumerate(picks) if p]
    assert ran == [1, 5, 9]


def test_scheduler_stride_is_counted_in_frames_not_calls():
    """`select` must be called once per frame even with nothing ready, or the
    stride would stretch. An empty frame still advances the counter."""
    sched = TemporalScheduler(stride=3, budget=1)
    assert sched.select([(1, 2)]) == [(1, 2)]     # frame 1
    assert sched.select([]) == []                 # frame 2: nothing ready
    assert sched.select([]) == []                 # frame 3
    assert sched.frame == 3
    assert sched.select([(1, 2)]) == [(1, 2)]     # frame 4: 3 frames elapsed


def test_scheduler_zero_budget_disables_temporal_inference():
    sched = TemporalScheduler(stride=1, budget=0)
    assert _ready(sched, [(1, 2), (3, 4)], 5) == [[], [], [], [], []]
    assert sched.frame == 5                       # still counting frames


def test_scheduler_budget_above_one_picks_several():
    sched = TemporalScheduler(stride=4, budget=2)
    assert sched.select([(1, 2), (3, 4), (5, 6)]) == [(1, 2), (3, 4)]
    assert sched.select([(1, 2), (3, 4), (5, 6)]) == [(5, 6)]   # only one due


def test_scheduler_drop_track_forgets_its_pairs():
    sched = TemporalScheduler(stride=10, budget=3)
    sched.select([(1, 2), (1, 3), (2, 3)])
    assert len(sched) == 3
    sched.drop_track(1)
    assert len(sched) == 1
    assert sched.last_run((2, 3)) == 1
    assert sched.last_run((1, 2)) is None


# --------------------------------------------------------------------------- #
# temporal verdicts in the state machine (weighted votes)
# --------------------------------------------------------------------------- #
def test_temporal_verdict_counts_as_two_votes():
    """Default streak is 3: two temporal confirmations (2+2=4) raise on their
    own, where two single-frame ones (1+1=2) do not."""
    fsm = BehaviorStateMachine(min_streak=3)
    assert fsm.update((1, 2), "fight", 0.9, 0.0, weight=2) is None   # streak 2
    ev = fsm.update((1, 2), "fight", 0.9, 1.0, weight=2)             # streak 4
    assert ev is not None and ev["phase"] == "start" and ev["label"] == "fight"


def test_two_single_frame_votes_do_not_raise():
    fsm = BehaviorStateMachine(min_streak=3)
    assert fsm.update((1, 2), "fight", 0.9, 0.0) is None
    assert fsm.update((1, 2), "fight", 0.9, 1.0) is None             # streak 2


def test_temporal_vote_completes_a_single_frame_streak():
    """The mixed path: one coarse single-frame hit (1) + one temporal (2) = 3."""
    fsm = BehaviorStateMachine(min_streak=3)
    assert fsm.update((1, 2), "harass", 0.8, 0.0) is None            # streak 1
    ev = fsm.update((1, 2), "harass", 0.95, 0.1, weight=2)           # streak 3
    assert ev is not None and ev["phase"] == "start"
    assert ev["label"] == "harass"


def test_weighted_streak_resets_on_a_label_change():
    """A temporal `harass` after a single-frame `fight` restarts at ITS weight,
    it does not inherit the other label's streak."""
    fsm = BehaviorStateMachine(min_streak=3)
    fsm.update((1, 2), "fight", 0.9, 0.0)                            # streak 1
    assert fsm.update((1, 2), "harass", 0.9, 1.0, weight=2) is None   # streak 2
    ev = fsm.update((1, 2), "harass", 0.9, 2.0)                      # streak 3
    assert ev is not None and ev["label"] == "harass"


def test_release_is_not_weighted():
    """A temporal `none` spends ONE miss, not two: hysteresis stays measured in
    verdicts so a single sequence read cannot slam an event shut."""
    fsm = BehaviorStateMachine(min_streak=1, release_frames=2)
    assert fsm.update((1, 2), "fight", 0.9, 0.0)["phase"] == "start"
    assert fsm.update((1, 2), "none", 0.9, 1.0, weight=2) is None    # 1 miss
    ev = fsm.update((1, 2), "none", 0.9, 2.0, weight=2)              # 2 misses
    assert ev is not None and ev["phase"] == "end"


def test_weight_below_one_is_clamped():
    fsm = BehaviorStateMachine(min_streak=1)
    ev = fsm.update((1, 2), "fight", 0.9, 0.0, weight=0)
    assert ev is not None and ev["frames"] == 1

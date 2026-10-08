"""Voice lifecycle: admission gate, bounded quiescence, abandoned decode.

overlay phase-2 V4-6 / V5-4 / V6-2 / V7-1 / V8-1, as three separately testable
claims:

  * **closing is one flag under one lock** -- once it is set, no callback is
    admitted and no publication is admitted (V8-1: "zero new publication" means
    zero new publication ADMISSION); a callback admitted before the boundary
    runs to completion and is never killed;
  * **quiescence is bounded** -- all already-admitted callbacks finish inside
    the 2 s budget or the stop is recorded as a failure and teardown proceeds
    anyway; the budget is measured from the stop REQUEST;
  * **an uncancellable decode is abandoned, not waited for** -- `transcribe()`
    is synchronous (on the default RK backend it drives the platform inference
    service), so closing stops NEW decodes from starting, the gate drops
    whatever one in flight returns, and the owner's join is bounded. The
    backend itself is released by the owner afterwards, which is what lets the
    inference service reclaim the model.

Hardware-free: fake audio source, fake wake/VAD, fake in-process ASR.
"""
from __future__ import annotations

import os
import sys
import threading
import time

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from kit.asr import AsrResult                                        # noqa: E402
from kit.lifecycle import DEFAULT_QUIESCENCE_SEC, PublicationGate    # noqa: E402
from kit.logic.voice_sm import VoiceStateMachine                     # noqa: E402


# --------------------------------------------------------------------------- #
# publication gate
# --------------------------------------------------------------------------- #
def test_closing_refuses_both_new_callbacks_and_new_publications():
    gate = PublicationGate(name="t")
    assert gate.admit_publication() is True
    assert gate.admit_callback() is True
    gate.release_callback()

    boundary = gate.request_stop()
    assert isinstance(boundary, float)
    assert gate.closing is True
    assert gate.admit_callback() is False
    assert gate.admit_publication() is False
    assert gate.wait_quiescent(0.01) is True


def test_an_admitted_callback_finishes_and_the_stop_waits_for_it():
    """The stop request must not block on the gate lock; the drain must.

    `request_stop()` marks closing and returns; a callback admitted before that
    instant keeps running and releases its slot later, which is what ends the
    drain. Holding the lock across the wait would deadlock the two.
    """
    gate = PublicationGate(name="t")
    running = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def callback():
        assert gate.admit_callback() is True
        running.set()
        release.wait(5.0)
        gate.release_callback()
        finished.set()

    thread = threading.Thread(target=callback)
    thread.start()
    assert running.wait(2.0), "the callback was never admitted"

    started = time.monotonic()
    gate.request_stop()
    request_returned = time.monotonic() - started
    assert request_returned < 0.5, "request_stop() must not wait for the drain"
    assert gate.inflight == 1

    releaser = threading.Thread(target=lambda: (time.sleep(0.1), release.set()))
    releaser.start()
    # The drain is released by the callback, not by the timeout.
    assert gate.wait_quiescent(2.0) is True, gate.quiescence_failures

    thread.join(5.0)
    releaser.join(5.0)
    assert finished.is_set()
    assert gate.quiescent is True
    assert gate.quiescence_failures == 0
    assert gate.quiescence_latency() is not None
    assert gate.stop_requested_at >= started


def test_an_overrunning_callback_is_recorded_as_a_quiescence_failure():
    gate = PublicationGate(name="t")
    assert gate.admit_callback() is True
    gate.release_callback()          # the callback never releases in time
    assert gate.admit_callback() is True
    gate.request_stop()
    assert gate.wait_quiescent(0.05) is False
    assert gate.quiescence_failures == 1
    gate.release_callback()
    assert gate.wait_quiescent(0.05) is True


def test_default_quiescence_budget_is_two_seconds():
    assert DEFAULT_QUIESCENCE_SEC == 2.0


# --------------------------------------------------------------------------- #
# the state machine honours the gate and the bounded stop
# --------------------------------------------------------------------------- #
class _Source:
    """Endless audio source; `read()` returns None only once closed."""

    def __init__(self):
        self.open_calls = 0
        self.close_calls = 0
        self.closed = threading.Event()

    def open(self):
        self.open_calls += 1
        return self

    def read(self):
        if self.closed.is_set():
            return None
        time.sleep(0.002)
        return object()

    def close(self):
        self.close_calls += 1
        self.closed.set()


class _Wake:
    keyword = "test"
    backend = "fake"
    score = 1.0
    transcript = ""

    def reset(self):
        pass

    def accept(self, frame):
        return self


class _Segment:
    duration_sec = 0.5
    pcm = b"\x00\x01" * 160


class _Vad:
    def reset(self):
        pass

    def accept(self, frame):
        pass

    def segments(self):
        return [_Segment()]

    def is_speech(self):
        return True

    def flush(self):
        pass


class _Asr:
    def __init__(self, delay=0.0):
        self.calls = 0
        self.delay = delay

    def transcribe(self, pcm, sample_rate=None):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        return AsrResult(text=f"line {self.calls}", elapsed=0.01,
                         audio_sec=0.5, rtf=0.02, language="zh")


def _drive(on_event, *, stop_after=0.2, quiescence=2.0, asr=None):
    """Run the machine on a thread; stop it after `stop_after` seconds."""
    src = _Source()
    sm = VoiceStateMachine(src, _Wake(), _Vad(), asr or _Asr(), on_event=on_event,
                           verbose=False)
    result = {}

    def loop():
        result["returned"] = time.monotonic()
        try:
            sm.run()
        except BaseException as exc:            # noqa: BLE001
            result["error"] = exc
        result["exited"] = time.monotonic()

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    time.sleep(stop_after)
    boundary = time.monotonic()
    verdict = sm.request_stop(quiescence_timeout=quiescence)
    request_returned = time.monotonic()
    # The owner's teardown: close the source, then join the loop.
    sm.close()
    thread.join(5.0)
    return {"sm": sm, "src": src, "thread": thread, "verdict": verdict,
            "boundary": boundary, "request_returned": request_returned,
            "result": result}


def test_no_publication_is_admitted_after_the_stop_request():
    published = []
    lock = threading.Lock()

    def on_event(ev):
        with lock:
            published.append((time.monotonic(), ev.get("type")))
        time.sleep(0.002)                       # callback work, not instant

    state = _drive(on_event)
    boundary = state["boundary"]
    late = [item for item in published if item[0] > boundary]
    assert late == [], f"{len(late)} publication(s) after the boundary"
    assert state["verdict"] is True
    assert state["sm"].gate.quiescence_failures == 0
    assert state["result"]["exited"] - boundary <= 2.0
    assert state["src"].close_calls == 1, "the owner closes the source exactly once"
    assert state["sm"].close.__self__ is state["sm"]
    print(f"\nstop->quiescence {state['request_returned'] - boundary:.4f}s, "
          f"stop->loop exit {state['result']['exited'] - boundary:.4f}s, "
          f"publications={len(published)}")


def test_an_admitted_callback_is_allowed_to_finish_inside_the_budget():
    """The stop waits for a callback that was already admitted."""
    started = threading.Event()
    release = threading.Event()
    published = []

    def on_event(ev):
        if ev.get("type") != "transcript":
            return
        started.set()
        release.wait(5.0)                       # blocks past the boundary
        published.append(ev)

    state = {}

    def run_then_stop():
        state.update(_drive(on_event, stop_after=0.05, quiescence=2.0))

    driver = threading.Thread(target=run_then_stop, daemon=True)
    driver.start()
    assert started.wait(3.0), "no transcript callback was admitted"
    time.sleep(0.05)
    release.set()
    driver.join(10.0)
    assert state["verdict"] is True, state["sm"].gate.quiescence_failures
    assert published, "the admitted callback ran to completion"


def test_overrunning_callback_is_reported_not_forced():
    hold = threading.Event()

    def on_event(ev):
        if ev.get("type") == "transcript":
            hold.wait(5.0)

    state = _drive(on_event, stop_after=0.05, quiescence=0.1)
    hold.set()
    assert state["verdict"] is False
    assert state["sm"].gate.quiescence_failures == 1
    # Recycling is not the gate's job: the loop is still alive and the owner
    # closes the source regardless.
    assert state["src"].close_calls == 1


# --------------------------------------------------------------------------- #
# the ASR backend: app-owned, in-process, abandoned rather than waited for
# --------------------------------------------------------------------------- #
class _SlowAsr:
    """ASR whose decode blocks until released; records call/close bookkeeping."""

    def __init__(self, hold, *, calls=None):
        self.hold = hold
        self.calls = calls if calls is not None else []
        self.closed = 0
        self.started = threading.Event()

    def transcribe(self, pcm, sample_rate=None):
        self.calls.append(pcm)
        self.started.set()
        self.hold.wait(30.0)                 # a decode that will not end on its own
        return AsrResult(text="late", elapsed=0.01, audio_sec=1.0, rtf=0.01,
                         language="zh")

    def close(self):
        self.closed += 1


def test_no_new_decode_starts_once_closing_is_set():
    """`_transcribe` refuses to start a decode after the boundary (V5-4)."""
    calls = []
    asr = _SlowAsr(threading.Event(), calls=calls)
    published = []
    sm = VoiceStateMachine(_Source(), _Wake(), _Vad(), asr,
                           on_event=lambda ev: published.append(ev),
                           verbose=False)
    sm.gate.request_stop()                   # boundary first: nothing may start
    assert sm._transcribe(_Segment()) == 0
    assert calls == [], "a decode started after closing"
    assert published == []


def test_a_decode_result_arriving_after_the_boundary_is_dropped():
    """The in-flight decode is abandoned, and its text never reaches the sink."""
    hold = threading.Event()
    calls = []
    asr = _SlowAsr(hold, calls=calls)
    published = []
    lock = threading.Lock()

    def on_event(ev):
        with lock:
            published.append((time.monotonic(), ev.get("type")))

    sm = VoiceStateMachine(_Source(), _Wake(), _Vad(), asr, on_event=on_event,
                           verbose=False)
    decode = threading.Thread(target=sm._transcribe, args=(_Segment(),),
                              daemon=True)
    decode.start()
    assert asr.started.wait(3.0), "the decode never started"
    assert calls, "the decode was not started before the boundary"

    boundary = time.monotonic()
    assert sm.request_stop(quiescence_timeout=0.2) is True
    hold.set()                               # the decode returns AFTER closing
    decode.join(5.0)
    with lock:
        events = list(published)
    print(f"\npre-boundary events={[k for _t, k in events]}")
    late = [(t, k) for t, k in events if t > boundary]
    assert late == [], f"a post-boundary decode published {late}"
    assert [k for _t, k in events if k == "transcript"] == [], (
        "the abandoned decode's text reached the callback")
    assert sm.gate.quiescence_failures == 0
    assert time.monotonic() - boundary <= 2.0


def test_stop_does_not_wait_for_an_unfinishable_decode():
    """The 2 s budget is met by abandoning the WAIT, not the decode.

    `transcribe()` is synchronous and uncancellable, so a stop that waited for
    it would blow the budget. The gate is closed, the loop's join is bounded,
    and the caller moves on to tearing the backend down.
    """
    hold = threading.Event()
    asr = _SlowAsr(hold)
    src = _Source()
    sm = VoiceStateMachine(src, _Wake(), _Vad(), asr, on_event=lambda ev: None,
                           verbose=False)
    loop = threading.Thread(target=sm._transcribe, args=(_Segment(),),
                            daemon=True)
    loop.start()
    assert asr.started.wait(3.0)

    started = time.monotonic()
    assert sm.request_stop(quiescence_timeout=2.0) is True
    stop_elapsed = time.monotonic() - started
    assert stop_elapsed <= 2.0, "the stop request waited for the decode"

    # The owner's teardown: read cancel, then a BOUNDED join. The join is
    # allowed to hit its own ceiling -- that is the point -- but it must return.
    src.close()
    join_started = time.monotonic()
    loop.join(3.0)
    join_elapsed = time.monotonic() - join_started
    print(f"\nstop={stop_elapsed:.3f}s bounded join={join_elapsed:.3f}s")
    assert join_elapsed <= 3.1, "the join was not bounded"
    assert loop.is_alive(), "the decode is abandoned, not finished"
    assert asr.closed == 0, "the backend is closed by the owner, not by stop()"
    hold.set()
    loop.join(5.0)

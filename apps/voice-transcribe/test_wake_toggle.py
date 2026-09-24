"""
Wake-word on/off switch for voice-transcribe (apps/voice-transcribe/app.py).

Drives the REAL kit VoiceStateMachine with the app's `_SwitchableWake`
wrapper and the app's own `_on_voice_event` filter, using fakes for audio,
wake detector, VAD and ASR. No sherpa, no ALSA, no NPU.

Run:  uv run pytest apps/voice-transcribe/test_wake_toggle.py -q
"""
import importlib.util
import os
import types

import pytest

from kit.logic.voice_sm import VoiceStateMachine

_APP_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")


def _load_app_module():
    spec = importlib.util.spec_from_file_location("voice_transcribe_app_wt",
                                                  _APP_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load_app_module()
VTA = mod.VoiceTranscribeApp

SEGMENT_AFTER = 3      # fake VAD endpoints after this many frames post-reset


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #
class FakeSource:
    """Yields `n` frames (their index), calling hooks[i]() before frame i."""

    def __init__(self, n, hooks=None):
        self.n = n
        self.i = -1
        self.hooks = hooks or {}

    def open(self):
        pass

    def close(self):
        pass

    def read(self):
        self.i += 1
        if self.i >= self.n:
            return None
        hook = self.hooks.get(self.i)
        if hook:
            hook()
        return self.i


class FakeWake:
    """Fires only on frame indices listed in `fire_on`."""

    def __init__(self, fire_on=()):
        self.fire_on = set(fire_on)
        self.accepted = []
        self.resets = 0

    def accept(self, frame):
        self.accepted.append(frame)
        if frame in self.fire_on:
            return types.SimpleNamespace(keyword="hello camera", backend="fake",
                                         score=2.0, transcript="")
        return None

    def reset(self):
        self.resets += 1


class FakeVad:
    def __init__(self):
        self.count = 0
        self.pending = None

    def reset(self):
        self.count = 0
        self.pending = None

    def accept(self, frame):
        self.count += 1
        if self.count == SEGMENT_AFTER:
            self.pending = types.SimpleNamespace(pcm=b"", duration_sec=0.3)

    def segments(self):
        if self.pending is not None:
            seg, self.pending = self.pending, None
            yield seg

    def is_speech(self):
        return True

    def flush(self):
        pass


class FakeAsr:
    def transcribe(self, pcm):
        return types.SimpleNamespace(text="hello world", audio_sec=0.3,
                                     rtf=0.1, language="en")


class StandInApp:
    """Minimal app: borrows the production event methods from the real class.

    `tick()` mimics kit's SIGHUP re-bind: pending live values are set onto
    `self`, then `on_params_changed(changed)` is called.
    """

    _on_voice_event = VTA._on_voice_event
    _suppress_in_continuous_mode = VTA._suppress_in_continuous_mode
    on_params_changed = VTA.on_params_changed

    def __init__(self, enabled, src):
        self.wake_word_enabled = enabled
        self._state = mod.IDLE
        self._last_text = ""
        self.src = src
        self.pending = {}
        self.ticks = 0
        self.published = []    # (event, frame index, top-level state)

    def tick(self):
        self.ticks += 1
        if self.pending:
            changed = set(self.pending)
            for k, v in self.pending.items():
                setattr(self, k, v)
            self.pending = {}
            self.on_params_changed(changed)

    def emit(self, events, t, extra=None):
        for ev in events:
            self.published.append((ev, self.src.i, extra["state"]))


class StepClock:
    def __init__(self, step=0.1):
        self.t = 0.0
        self.step = step

    def __call__(self):
        self.t += self.step
        return self.t


def _run(enabled, n, fire_on=(), hooks_factory=None):
    src = FakeSource(n)
    app = StandInApp(enabled, src)
    if hooks_factory:
        src.hooks = hooks_factory(app)
    inner = FakeWake(fire_on)
    wake = mod._SwitchableWake(inner, app, clock=StepClock())
    sm = VoiceStateMachine(src, wake, FakeVad(), FakeAsr(),
                           on_event=app._on_voice_event,
                           listen_timeout_sec=1000.0, verbose=False)
    # what VoiceTranscribeApp.run() publishes first
    app._on_voice_event({"type": "state", "state": mod.IDLE, "_initial": True})
    count = sm.run()
    return app, inner, count


def _kinds(app):
    return [ev["kind"] for ev, _, _ in app.published]


def _transcript_frames(app):
    return [i for ev, i, _ in app.published if ev["kind"] == "transcript"]


# --------------------------------------------------------------------------- #
# tests
# --------------------------------------------------------------------------- #
def test_disabled_transcribes_continuously_without_wake():
    app, inner, count = _run(enabled=False, n=20)
    kinds = _kinds(app)
    assert count >= 4
    assert kinds.count("transcript") == count
    assert "wake" not in kinds
    assert "listen_timeout" not in kinds
    idle = [ev for ev, _, _ in app.published
            if ev["kind"] == "state" and ev["state"] == mod.IDLE]
    assert len(idle) == 1, "only run()'s initial idle may be published"
    assert "_initial" not in idle[0]
    states = [ev["state"] for ev, _, _ in app.published
              if ev["kind"] == "state"][1:]
    assert set(states) == {"listening", "transcribing"}
    # real detector never consulted while disabled
    assert inner.accepted == []
    # summary state never parked on "transcribing" after a transcript
    assert app._state == "listening"
    assert app.ticks > 0


def test_enabled_transcribes_only_after_wake():
    app, inner, count = _run(enabled=True, n=20, fire_on={10})
    kinds = _kinds(app)
    assert count == 1
    frames = _transcript_frames(app)
    assert frames and all(f > 10 for f in frames)
    wakes = [ev for ev, _, _ in app.published if ev["kind"] == "wake"]
    assert len(wakes) == 1 and wakes[0]["backend"] == "fake"
    assert kinds.index("wake") < kinds.index("transcript")
    # idle states are published normally in wake-word mode
    idle = [ev for ev, _, _ in app.published
            if ev["kind"] == "state" and ev["state"] == mod.IDLE]
    assert len(idle) >= 2
    assert 10 in inner.accepted


def test_live_flip_on_requires_wake_afterwards(capsys):
    flip_at, wake_at = 12, 30

    def hooks(app):
        return {flip_at: lambda: app.pending.update(wake_word_enabled=True)}

    app, inner, count = _run(enabled=False, n=45, fire_on={wake_at},
                             hooks_factory=hooks)
    assert app.wake_word_enabled is True
    assert "[voice-transcribe] wake word enabled=True" in capsys.readouterr().out
    frames = _transcript_frames(app)
    before = [f for f in frames if f < flip_at]
    after = [f for f in frames if f >= flip_at]
    assert len(before) >= 2, "continuous mode transcribed before the flip"
    # at most the in-flight utterance completes, then nothing until the wake
    in_flight = [f for f in after if f < flip_at + SEGMENT_AFTER + 1]
    gated = [f for f in after if f >= flip_at + SEGMENT_AFTER + 1]
    assert len(in_flight) <= 1
    assert gated and all(f > wake_at for f in gated)
    assert len(gated) == 1
    wakes = [ev for ev, _, _ in app.published if ev["kind"] == "wake"]
    assert [w["backend"] for w in wakes] == ["fake"]


def test_live_flip_off_resumes_continuous():
    def hooks(app):
        return {5: lambda: app.pending.update(wake_word_enabled=False)}

    app, inner, count = _run(enabled=True, n=25, hooks_factory=hooks)
    assert app.wake_word_enabled is False
    frames = _transcript_frames(app)
    assert len(frames) >= 3 and all(f >= 5 for f in frames)
    assert "wake" not in _kinds(app)


@pytest.mark.parametrize("value,expected", [
    (True, True), (False, False), (None, False), ("", False),
    ("true", True), ("On", True), ("1", True), ("yes", True),
    ("false", False), ("off", False), ("0", False), (1, True), (0, False),
])
def test_as_bool(value, expected):
    assert mod._as_bool(value, False) is expected


def test_wrapper_ticks_about_once_per_second_and_forwards():
    app = types.SimpleNamespace(wake_word_enabled=False, ticks=0)
    app.tick = lambda: setattr(app, "ticks", app.ticks + 1)

    class Inner(FakeWake):
        closed = False

        def close(self):
            Inner.closed = True

    inner = Inner()
    w = mod._SwitchableWake(inner, app, clock=StepClock(0.1))
    for i in range(25):                     # 2.5 s of 100 ms frames
        ev = w.accept(i)
        assert ev.backend == "always-on" and ev.keyword == ""
        assert ev.score == 1.0 and ev.transcript == ""
    assert app.ticks == 3                   # t=0.1, 1.1, 2.1
    w.reset()
    assert inner.resets == 1
    w.close()
    assert Inner.closed

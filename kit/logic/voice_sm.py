"""
Voice interaction state machine for reCamera Pro (docs/guide/voice-app.md §0/§3).

This is the one piece of business logic the voice app owns -- everything else
(audio capture, VAD, KWS, ASR) is a swappable kit building block. It wires them
into the core interaction:

    idle ──wake word──► listening ──(silence N s | max len)──► transcribing
     ▲                    (VAD collects the utterance)              │
     └──────────────────────── transcript emitted ─────────────────┘

States (voice-app §0):
    idle          feed every PcmFrame to the WakeWord detector; wait for a hit.
    listening     wake fired -> feed frames to the VAD until it endpoints one
                  utterance (trailing silence >= vad.min_silence_duration) or a
                  hard `listen_timeout_sec` elapses with no speech.
    transcribing  run Asr.transcribe on the captured utterance, emit the text,
                  return to idle.

Events are pushed to an optional `on_event(dict)` callback (and, if given, a
kit ResultSink) so the app / debug panel / test harness can observe every
transition and the final transcript. The class is transport-agnostic: it just
pulls `PcmFrame`s from any `AudioSource` (live `RtspAudioSource` on device, or
`WavFileAudioSource` for injection tests).

Resource ownership (V4-5/V5-4/V6-2 -- read this before wrapping the machine in
another thread):

  * `open()` opens the audio source, `close()` closes it, and `run()` does
    NEITHER on the way out beyond what an open failure needs. The owner of the
    state machine (the application's teardown sequence) decides when the
    microphone is released.
  * `run()` never releases the ASR backend either: it is not this loop's
    property. The application builds it, keeps it, and closes it in its own
    teardown, AFTER the stop drain and the bounded join below.
  * `request_stop()` is the shutdown entry. It marks the shared
    `kit.lifecycle.PublicationGate` closing UNDER THE LOCK, releases the lock,
    and waits a bounded time for already-admitted callbacks to finish. A caller
    that publishes from another thread assigns the same gate object to
    `App.publication_gate`, which makes "no new publication after closing" hold
    for the application as a whole, not just for this loop.

Shutdown of an in-flight decode: `transcribe()` is synchronous and cannot be
cancelled, so the contract is "abandon the WAIT, not the model" -- closing
blocks new decodes from starting and drops whatever one in flight returns
(:meth:`_transcribe`), the owner bounds both the drain and the join, and only
then is the backend torn down (which, on the RK backend, is what lets the
platform inference service reclaim the model).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from kit.lifecycle import DEFAULT_QUIESCENCE_SEC, PublicationGate


logger = logging.getLogger(__name__)


# -- states -- #
IDLE = "idle"
LISTENING = "listening"
TRANSCRIBING = "transcribing"


class VoiceStateMachine:
    def __init__(
        self,
        audio_source,
        wakeword,
        vad,
        asr,
        *,
        on_event: Optional[Callable[[dict], None]] = None,
        listen_timeout_sec: float = 8.0,
        verbose: bool = True,
        gate: Optional[PublicationGate] = None,
    ):
        self.src = audio_source
        self.wake = wakeword
        self.vad = vad
        self.asr = asr
        self.on_event = on_event
        self.listen_timeout_sec = float(listen_timeout_sec)
        self.verbose = verbose
        self.state = IDLE
        self._opened = False
        # ★Admission gate★ (V5-4/V6-2): guards callback admission and is shared
        # with the application's publication path, so "closing" is one flag read
        # under one lock by both. A caller that publishes from another thread
        # MUST pass its own gate in (the merged eldercare-monitor app assigns
        # this very object to `App.publication_gate`); the default keeps the
        # single-threaded voice app working unchanged.
        self.gate = gate or PublicationGate(name="voice")
        self._stop = threading.Event()

    def open(self) -> "VoiceStateMachine":
        """Open and probe the audio source exactly once.

        Voice applications call this during their pre-READY transaction.  The
        main loop calls it again defensively, but the second call is a no-op so
        an ALSA/RTSP subprocess is never replaced or opened twice.
        """

        if self._opened:
            return self
        # Mark ownership before open(): source implementations may create one
        # subprocess and then fail while probing the first PCM chunk.  close()
        # must still get a chance to tear that partial state down.
        self._opened = True
        try:
            self.src.open()
        except BaseException as primary:
            try:
                self.close()
            except BaseException as cleanup_error:
                if hasattr(primary, "add_note"):
                    primary.add_note(
                        f"audio source cleanup also failed: {cleanup_error}")
            raise
        return self

    def close(self) -> None:
        """Close the pre-opened source; repeated calls are harmless.

        Called by the OWNER of the state machine (the app's teardown sequence),
        never by `run()`: the audio source is a resource of the process, not of
        the loop, and a loop that closed it would decide for the app when the
        microphone is released (V5-4).
        """

        if not self._opened:
            return
        # Only commit the closed state after the source accepts cleanup.  A
        # caller can retry if a custom source reports an uncertain teardown.
        self.src.close()
        self._opened = False

    # -- shutdown -------------------------------------------------------------- #
    def request_stop(self,
                     quiescence_timeout: float = DEFAULT_QUIESCENCE_SEC) -> bool:
        """Request the stop, then bound the wait for silence (V5-4/V7-1/V8-1).

        Order, all of it load-bearing:

          1. mark closing UNDER THE GATE LOCK -- the quiescence boundary. After
             this instant no new callback is admitted and no new publication is;
          2. RELEASE the lock, so a callback already in flight can drain instead
             of blocking the stop on the lock the admission path needs;
          3. wait, bounded by `quiescence_timeout`, for those callbacks;
          4. return the verdict. False means the silence budget was exceeded:
             that is an acceptance FAILURE to be reported, not a reason to
             force-recycle. Recycling is the caller's next step: it closes the
             ASR backend on its own bounded wait, which abandons the decode
             rather than waiting for it.

        Returns True when silence was reached inside the budget.
        """

        self.gate.request_stop()
        self._stop.set()
        return self.gate.wait_quiescent(quiescence_timeout)

    # -- event plumbing ------------------------------------------------------- #
    def _emit(self, ev: dict) -> None:
        ev.setdefault("t", round(time.monotonic(), 3))
        if self.verbose:
            print(f"[voice-sm] {ev}", flush=True)
        if self.on_event is None:
            return
        # ★Admission★ (V5-4/V6-2/V8-1): refused once closing. An admitted
        # callback always runs to completion -- it is never killed mid-flight --
        # but any publication it attempts after the boundary is refused by the
        # application's own entry-point check, which reads the same flag.
        if not self.gate.admit_callback():
            return
        try:
            self.on_event(ev)
        except Exception:
            pass  # observation must never break the loop
        finally:
            self.gate.release_callback()

    def _set_state(self, state: str, **extra) -> None:
        self.state = state
        self._emit({"type": "state", "state": state, **extra})

    # -- main loop ------------------------------------------------------------ #
    def run(self, *, max_wakes: int = 0) -> int:
        """Drive the machine over the audio source until it ends.

        `max_wakes>0` stops after that many completed wake->transcript cycles
        (used by the injection test); 0 runs until the stream ends. Returns the
        number of transcripts emitted.

        The loop does NOT close the audio source on the way out (V5-4): source
        cleanup belongs to the owner's teardown (`VoiceStateMachine.close()`),
        so a stop involving a blocked read is still the app's to sequence
        against its other shutdown steps.
        """
        wakes = 0
        listen_deadline = 0.0
        self._set_state(IDLE)
        self.wake.reset()
        self.open()
        src = self.src
        while not self._stop.is_set():
            frame = src.read()
            if frame is None:
                break

            if self.state == IDLE:
                ev = self.wake.accept(frame)
                if ev is not None:
                    self._emit({"type": "wake", "keyword": ev.keyword,
                                "backend": ev.backend, "score": ev.score,
                                "transcript": ev.transcript})
                    self.vad.reset()
                    listen_deadline = time.monotonic() + self.listen_timeout_sec
                    self._set_state(LISTENING)

            elif self.state == LISTENING:
                self.vad.accept(frame)
                seg = next(iter(self.vad.segments()), None)
                if seg is not None:
                    wakes += self._transcribe(seg)
                elif time.monotonic() > listen_deadline and not self.vad.is_speech():
                    # user woke but said nothing -> quietly re-arm
                    self._emit({"type": "listen_timeout"})
                    self._set_state(IDLE)
                    self.wake.reset()

            if max_wakes and wakes >= max_wakes:
                break

        # stream ended mid-listen: flush the trailing utterance. Skipped when a
        # stop was requested -- that is shutdown, not the end of the audio, and
        # the gate would refuse the callback anyway.
        if self.state == LISTENING and not self._stop.is_set():
            self.vad.flush()
            seg = next(iter(self.vad.segments()), None)
            if seg is not None:
                wakes += self._transcribe(seg)
        return wakes

    def _transcribe(self, seg) -> int:
        """Transcribe one endpointed utterance and return to idle.

        Returns 1 when a transcript was published, 0 when the utterance was
        abandoned at shutdown. Both checks below are deliberate (V5-4/V6-2):

          * **no new decode starts after closing.** The decode is synchronous
            and, on the RK backend, driven through the platform inference
            service, so "cancel it" is not an option -- not starting it is;
          * **a result that arrives after the boundary is dropped.** The decode
            may already have been in flight when closing was set; its text must
            not reach the sink. The callback gate enforces this for every event,
            and this check makes the intent local and testable instead of
            relying on a reader reconstructing it from the gate.
        """
        if self.gate.closing:
            return 0
        self._set_state(TRANSCRIBING,
                        utterance_sec=round(seg.duration_sec, 2))
        res = self.asr.transcribe(seg.pcm)
        if self.gate.closing:
            return 0
        self._emit({"type": "transcript", "text": res.text,
                    "audio_sec": round(res.audio_sec, 2),
                    "rtf": round(res.rtf, 2), "language": res.language})
        self._set_state(IDLE)
        self.wake.reset()
        return 1


# --- CLI: on-device verification / live run ---------------------------------- #
def _build(args):
    """Assemble the pipeline from CLI args (device paths default correctly)."""
    from kit.asr import Asr
    from kit.logic.vad import VadSegmenter
    from kit.logic.wakeword import SherpaKwsWakeWord, AsrKeywordWakeWord

    if args.wav:
        from kit.adapters.audio_source import WavFileAudioSource
        src = WavFileAudioSource(args.wav, chunk_ms=args.chunk_ms,
                                 realtime=args.realtime)
    else:
        from kit.adapters.audio_source import RtspAudioSource
        src = RtspAudioSource(args.url, chunk_ms=args.chunk_ms)

    print("[voice-sm] loading ASR (SenseVoice int8)...", flush=True)
    asr = Asr()
    vad = VadSegmenter(model=args.vad_model,
                       min_silence_duration=args.min_silence,
                       max_speech_duration=args.max_utterance,
                       preroll_ms=args.preroll_ms)

    if args.backend == "kws":
        print("[voice-sm] wake backend = sherpa KeywordSpotter", flush=True)
        wake = SherpaKwsWakeWord(keywords_file=args.keywords_file,
                                 keywords_threshold=args.kws_threshold,
                                 keywords_score=args.kws_score)
    else:
        print(f"[voice-sm] wake backend = ASR keyword match {args.wakeword!r}",
              flush=True)
        wake = AsrKeywordWakeWord(asr, args.wakeword.split("|"))

    return VoiceStateMachine(src, wake, vad, asr,
                             listen_timeout_sec=args.listen_timeout)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="reCamera Pro voice state machine")
    ap.add_argument("--backend", choices=["kws", "asr"], default="kws",
                    help="wake-word backend: sherpa KWS (default) or ASR match")
    ap.add_argument("--wav", default=None,
                    help="inject a WAV file instead of live RTSP audio")
    ap.add_argument("--url", default="rtsp://admin:admin@127.0.0.1:5554/live/1")
    ap.add_argument("--wakeword", default="hello camera",
                    help="asr backend: '|'-separated wake phrases")
    ap.add_argument("--keywords-file", default="/userdata/tmp/asr/kws/keywords.txt")
    ap.add_argument("--vad-model", default="/userdata/tmp/asr/silero_vad.onnx")
    ap.add_argument("--kws-threshold", type=float, default=0.25)
    ap.add_argument("--kws-score", type=float, default=1.5)
    ap.add_argument("--preroll-ms", type=float, default=300.0,
                    help="prepend this much pre-speech audio to each utterance")
    ap.add_argument("--min-silence", type=float, default=0.6)
    ap.add_argument("--max-utterance", type=float, default=15.0)
    ap.add_argument("--listen-timeout", type=float, default=8.0)
    ap.add_argument("--chunk-ms", type=int, default=100)
    ap.add_argument("--realtime", action="store_true")
    ap.add_argument("--max-wakes", type=int, default=0)
    args = ap.parse_args(argv)

    sm = _build(args)
    try:
        n = sm.run(max_wakes=args.max_wakes)
    finally:
        # run() no longer owns the source (V5-4); this CLI is the owner here.
        sm.close()
    print(f"[voice-sm] done: {n} transcript(s)", flush=True)
    return 0 if n or not args.wav else 1


if __name__ == "__main__":
    raise SystemExit(main())

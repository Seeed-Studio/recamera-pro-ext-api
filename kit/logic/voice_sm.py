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
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional


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
        """Close the pre-opened source; repeated calls are harmless."""

        if not self._opened:
            return
        # Only commit the closed state after the source accepts cleanup.  A
        # caller can retry if a custom source reports an uncertain teardown.
        self.src.close()
        self._opened = False

    # -- event plumbing ------------------------------------------------------- #
    def _emit(self, ev: dict) -> None:
        ev.setdefault("t", round(time.monotonic(), 3))
        if self.verbose:
            print(f"[voice-sm] {ev}", flush=True)
        if self.on_event is not None:
            try:
                self.on_event(ev)
            except Exception:
                pass  # observation must never break the loop

    def _set_state(self, state: str, **extra) -> None:
        self.state = state
        self._emit({"type": "state", "state": state, **extra})

    # -- main loop ------------------------------------------------------------ #
    def run(self, *, max_wakes: int = 0) -> int:
        """Drive the machine over the audio source until it ends.

        `max_wakes>0` stops after that many completed wake->transcript cycles
        (used by the injection test); 0 runs until the stream ends. Returns the
        number of transcripts emitted.
        """
        wakes = 0
        listen_deadline = 0.0
        primary_error = None
        try:
            # Include initialization in the cleanup transaction.  A caller may
            # have pre-opened the source before READY; wake.reset() must not
            # strand that source if it fails before the read loop begins.
            self._set_state(IDLE)
            self.wake.reset()
            self.open()
            src = self.src
            while True:
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

            # stream ended mid-listen: flush the trailing utterance
            if self.state == LISTENING:
                self.vad.flush()
                seg = next(iter(self.vad.segments()), None)
                if seg is not None:
                    wakes += self._transcribe(seg)
        except BaseException as exc:
            primary_error = (exc, exc.__traceback__)

        try:
            self.close()
        except BaseException as cleanup_error:
            if primary_error is None:
                raise
            primary, _traceback = primary_error
            if hasattr(primary, "add_note"):
                primary.add_note(
                    f"audio source cleanup also failed: {cleanup_error}")
            logger.critical(
                "voice audio cleanup failed while preserving earlier loop "
                "error", exc_info=True)

        if primary_error is not None:
            primary, traceback = primary_error
            raise primary.with_traceback(traceback)
        return wakes

    def _transcribe(self, seg) -> int:
        """Transcribe one endpointed utterance and return to idle. Returns 1."""
        self._set_state(TRANSCRIBING,
                        utterance_sec=round(seg.duration_sec, 2))
        res = self.asr.transcribe(seg.pcm)
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
    n = sm.run(max_wakes=args.max_wakes)
    print(f"[voice-sm] done: {n} transcript(s)", flush=True)
    return 0 if n or not args.wav else 1


if __name__ == "__main__":
    raise SystemExit(main())

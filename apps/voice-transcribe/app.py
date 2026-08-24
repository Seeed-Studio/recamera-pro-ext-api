#!/usr/bin/env python3
"""
voice-transcribe -- reCamera Pro voice app (wake word -> VAD -> ASR).

This is the P3 packaging of the verified voice pipeline. It is the SELF-PACED
variant of the new app shape (internal/KIT_APP_SHAPE_SPEC.md §3, "接管"): the
input is audio chunks, not camera frames, so `run()` drives the pipeline at its
own rhythm instead of iterating `self.frames()`:

    AiAsrAudioSource / RtspAudioSource (mic; no /dev/snd takeover)
        -> VoiceStateMachine (idle --wake--> listening --endpoint--> transcribing)
             WakeWord (sherpa KWS | ASR keyword)  +  VAD (silero)  +  Asr (SenseVoice)
        -> self.emit(events, t) -> the manifest `output` block (WS panel, MQTT/HA)

Two class flags declare that shape to kit:

    owns_loop    = True   -- `def run(self):` takes over the rhythm
    needs_frames = False  -- kit opens NO camera frame source; this app never
                             touches /dev/video, the RTSP video stream or VPSS

Owning the rhythm costs nothing else (spec §2 hard constraint: "an escape hatch
must not strip the rest of the infrastructure"). kit still auto-binds every
`config_schema` key onto `self`, still re-binds the apply:"live" ones on SIGHUP
(`self.tick()` applies them at an event boundary and kit routes the same change
into the output sink), and `self.emit()` still publishes through the sink
assembled from the manifest `output` block. Consequently this file contains no
sink lookup, no output plumbing and no MQTT code at all.

Because `needs_model = False` kit never constructs its frame-oriented
`RknnModel`.  The default `asr_backend = "rk"` still loads SenseVoice through
`kit.asr_rknn_backend`; an appmgr-managed v2 instance opens an authorized model
session on the platform `inferenced` scheduler instead of constructing RKNNLite
in the app process.  ASR is built during `setup()`; VAD/KWS and a probed audio
source are then built by `prepare_runtime()`.  Both hooks complete before
appmgr receives READY, and all resources remain owned until `finish()`.

Manifest-v2 packages carry authenticated model assets under the installed app
root (normally ``models/asr``).  Appmgr's scheduled inference service authorizes
that exact installed RKNN path, so managed v2 launches resolve it against the
app module location -- never against the process cwd -- and reject a path
override that the daemon could not authorize.  Standalone/legacy launches keep
the older shared and staging directories as compatibility fallbacks.

Test injection: set env `RECAMERA_VOICE_WAV=/path/to.wav` (or config `wav_file`)
to feed a "<wake word> + <sentence>" WAV through `WavFileAudioSource` instead of
the live RTSP mic -- the whole state machine runs identically. `RECAMERA_VOICE_MAX_WAKES`
stops after N completed transcripts (used by the on-device e2e test).
"""
import os
import sys

from kit.app import App, run_app


# States mirrored from kit.logic.voice_sm (kept local to avoid importing sherpa
# at module import time -- voice_sm only pulls sherpa when a pipeline is built).
IDLE = "idle"

SHARED_MODEL_DIR = "/userdata/local/models/asr"
STAGING_MODEL_DIR = "/userdata/tmp/asr"
BUNDLED_MODEL_SUBDIR = os.path.join("models", "asr")
_INFERENCE_SERVICE_ENVS = (
    "RECAMERA_INFERENCE_SERVICE_SOCK",
    # Read-only compatibility alias also honoured by kit.runtime.remote.
    "RECAMERA_INFERENCE_SERVICE",
)


class VoiceTranscribeApp(App):
    id = "voice-transcribe"
    name = "Voice Transcribe"
    postproc = "voice"
    owns_loop = True             # explicit new shape: run() owns the rhythm
    needs_frames = False         # audio app: kit opens no camera frame source
    # The voice backend owns an ASR session outside kit's frame-model registry;
    # setup()/prepare_runtime()/finish() fence the complete voice pipeline and
    # its appmgr-authorized inference-service session.
    needs_model = False

    # -- config --------------------------------------------------------------- #
    def setup(self, config):
        """Normalise the auto-bound `config_schema` params.

        kit has already bound every schema key onto `self` before this runs
        (App.start -> _bind_params), with the declared type applied. What is
        left here is the app-specific normalisation the schema cannot express:
        case-folding the enums and resolving `model_dir` against authenticated
        package assets or legacy standalone locations. `getattr(self, k, ...)`
        keeps a hand-built config that omits a key working (unit tests,
        `--sink stdout` by hand).
        """
        # Refuse to overwrite a resource retained after a failed cleanup.  A
        # normal start-after-finish sees None and can build a fresh backend.
        if (getattr(self, "_asr", None) is not None
                or getattr(self, "_voice_sm", None) is not None):
            raise RuntimeError("voice runtime is already initialized")
        self._asr = None
        self._voice_sm = None

        super().setup(config or {})
        c = self.config

        def _s(key, default):
            return str(getattr(self, key, None) or c.get(key, default))

        def _f(key, default):
            try:
                return float(getattr(self, key, c.get(key, default)))
            except (TypeError, ValueError):
                return float(default)

        self.wake_backend = _s("wake_backend", "kws").lower()
        # ASR backend selector (voxedge consumer): "rk" (NPU w4a16 via
        # kit.asr_rknn_backend, default -- the bundled asset dir ships the w4a16
        # .rknn, NOT a sherpa model.int8.onnx) or "sherpa" (CPU). Downstream is
        # identical.
        self.asr_backend = _s("asr_backend", "rk").lower()
        self.wakeword = _s("wakeword", "hello camera")
        self.language = _s("language", "auto")
        self.min_silence_sec = _f("min_silence_sec", 0.6)
        self.max_utterance_sec = _f("max_utterance_sec", 15.0)
        # Pre-roll look-back: prepend this much audio from *before* the VAD's
        # confirmed speech-start so clipped utterance heads ("今天"->"天天") are
        # recovered. Too large and the wake-word tail ("...CAMERA") leaks in.
        self.preroll_ms = _f("preroll_ms", 300.0)
        self.listen_timeout_sec = _f("listen_timeout_sec", 8.0)
        self.kws_threshold = _f("kws_threshold", 0.25)
        self.kws_score = _f("kws_score", 1.5)
        self.model_dir = self._resolve_model_dir(
            getattr(self, "model_dir", None) or c.get("model_dir"))
        # runtime state broadcast to the panel / HA summary
        self._state = IDLE
        self._last_text = ""

        # App.start() owns setup() as part of its rollback transaction.  For a
        # managed v2 RK backend this resolves the exact authorized bundled path
        # and opens the inferenced model session. Standalone/legacy direct RKNN
        # keeps its broker-lease compatibility path inside the backend. run_app
        # only emits APPMGR_READY after this method returns.
        self._asr = self._build_asr()

    def _build_asr(self):
        """Build the app-owned ASR backend (small seam for host tests)."""
        from kit.asr import Asr

        md = self.model_dir
        return Asr(model=os.path.join(md, "model.int8.onnx"),
                   tokens=os.path.join(md, "tokens.txt"),
                   language=self.language, backend=self.asr_backend)

    def _close_asr(self):
        """Close the app-owned ASR, retaining it only if cleanup failed."""
        asr = getattr(self, "_asr", None)
        if asr is None:
            return
        close = getattr(asr, "close", None)
        if not callable(close):
            close = getattr(asr, "unload", None)
        if callable(close):
            close()
        self._asr = None

    def _close_voice_pipeline(self):
        """Close the pre-opened audio pipeline, retaining it on failure."""

        sm = getattr(self, "_voice_sm", None)
        if sm is None:
            return
        sm.close()
        self._voice_sm = None

    def finish(self):
        """Release ASR and kit resources while preserving the first failure."""
        primary_error = None

        def clean(label, callback):
            nonlocal primary_error
            try:
                callback()
            except BaseException as exc:
                if primary_error is None:
                    primary_error = (exc, exc.__traceback__)
                else:
                    print(f"[app:{self.id}] secondary {label} cleanup "
                          f"failure: {exc}", file=sys.stderr, flush=True)

        # Stop input before destroying ASR/NPU state, then let the kit release
        # signal handlers, frame/model resources and its owned sink.
        clean("voice pipeline", self._close_voice_pipeline)
        clean("ASR", self._close_asr)
        clean("kit", super().finish)

        if primary_error is not None:
            error, traceback = primary_error
            raise error.with_traceback(traceback)

    def on_params_changed(self, changed):
        """Apply only parameters that are genuinely safe for this live graph.

        The audio source, VAD, wake-word detector and state machine capture all
        Voice runtime parameters during ``prepare_runtime``.  Their manifest
        entries are therefore ``apply:restart`` and do not reach this hook.
        Output formatter/filter settings are reloaded independently by the sink
        tree.  Keeping this hook explicit makes an accidental future live field
        visible in logs without pretending that a captured object changed.
        """
        if changed:
            print(f"[voice-transcribe] live app parameters changed="
                  f"{sorted(changed)}", flush=True)

    def _app_root(self):
        """Return this app's root without trusting the process cwd."""

        from kit import config as kit_config

        return os.path.realpath(kit_config.app_dir_of(self))

    def _bundled_model_dir(self, app_root, *, required):
        """Derive Voice's authorized asset directory from its v2 manifest."""

        artifacts = [
            item for item in (self._manifest or {}).get("artifacts", [])
            if isinstance(item, dict)
            and item.get("kind") == "rknn"
            and item.get("source") == "bundled"
        ]
        if len(artifacts) != 1 or not isinstance(artifacts[0].get("file"), str):
            if not required:
                return os.path.realpath(os.path.join(app_root,
                                                     BUNDLED_MODEL_SUBDIR))
            from kit.errors import ConfigurationError

            raise ConfigurationError(
                "managed voice inference requires exactly one bundled RKNN "
                "artifact in manifest v2",
                operation="voice.configure",
                code="invalid_bundled_model",
            )

        relative = artifacts[0]["file"]
        model_dir = os.path.realpath(os.path.join(app_root,
                                                  os.path.dirname(relative)))
        try:
            contained = os.path.commonpath((app_root, model_dir)) == app_root
        except ValueError:
            contained = False
        if not contained:
            from kit.errors import ConfigurationError

            raise ConfigurationError(
                f"bundled RKNN artifact escapes the app root: {relative!r}",
                operation="voice.configure",
                code="invalid_bundled_model",
            )
        return model_dir

    def _uses_managed_v2_inference(self):
        """Whether RK ASR will cross the appmgr-authorized daemon boundary."""

        return (
            (self._manifest or {}).get("manifest_version") == 2
            and self.asr_backend == "rk"
            and any(str(os.environ.get(name) or "").strip()
                    for name in _INFERENCE_SERVICE_ENVS)
        )

    def _resolve_model_dir(self, preferred):
        """Resolve package assets securely, retaining standalone fallbacks.

        A relative config value is app-relative, regardless of cwd.  In a
        managed manifest-v2 RK launch the inference daemon authorizes only the
        bundled RKNN artifact, so an override to a shared/staging/custom
        directory cannot work and is rejected here with a configuration error.
        Standalone and legacy launches may still use their historical external
        model directories.
        """

        app_root = self._app_root()
        managed_v2 = self._uses_managed_v2_inference()
        bundled = self._bundled_model_dir(app_root, required=managed_v2)
        explicit = str(preferred or "").strip()
        raw = explicit or BUNDLED_MODEL_SUBDIR
        configured = os.path.realpath(
            raw if os.path.isabs(raw) else os.path.join(app_root, raw))

        if managed_v2:
            if configured != bundled:
                from kit.errors import ConfigurationError

                raise ConfigurationError(
                    f"model_dir {raw!r} is not authorized for managed "
                    f"manifest-v2 inference; use the bundled artifact directory "
                    f"{bundled!r}",
                    operation="voice.configure",
                    code="unauthorized_model_path",
                    details={"configured": configured, "authorized": bundled},
                )
            if not os.path.isdir(bundled):
                from kit.errors import ConfigurationError

                raise ConfigurationError(
                    f"bundled voice model directory is missing: {bundled}",
                    operation="voice.configure",
                    code="model_assets_missing",
                    details={"authorized": bundled},
                )
            return bundled

        # Standalone/v1 compatibility: prefer an explicit or package-local
        # directory when it exists, then the historical shared/staging layouts.
        for candidate in (configured, bundled, SHARED_MODEL_DIR,
                          STAGING_MODEL_DIR):
            if candidate and os.path.isdir(candidate):
                return os.path.realpath(candidate)

        # Keep legacy hand-built tests/configurations diagnosable: if nothing is
        # installed yet, preserve the caller's spelling instead of inventing a
        # different external path. Managed v2 never reaches this branch.
        return raw if explicit else SHARED_MODEL_DIR

    # -- event plumbing -------------------------------------------------------- #
    def _on_voice_event(self, ev):
        """VoiceStateMachine callback -> shape + publish one event.

        The state machine emits {"type": state|wake|transcript|listen_timeout, ...}.
        We mirror `type` into `kind` (the field the /appcenter event log + MQTT
        summary key off), and attach a top-level `state` + `summary{state,text}`
        so the panel and Home Assistant always see the current state and the last
        transcript regardless of which event arrived.

        This is this app's "loop body": the frame-driven apps tick + emit once
        per frame, this one does it once per voice event.
        """
        # Apply any pending SIGHUP config hot-reload at an event boundary. The
        # frame-driven shape gets this from self.frames(); a self-paced run()
        # calls it itself (spec §2, `self.tick()`).
        self.tick()
        kind = ev.get("type", "event")
        out = dict(ev)
        out["kind"] = kind
        if kind == "state":
            self._state = ev.get("state", self._state)
        elif kind == "transcript":
            self._last_text = ev.get("text", "") or self._last_text
        # Same emit path as every other app: kit publishes through the sinks
        # assembled from the manifest `output` block.
        self.emit([out], float(ev.get("t", 0.0)),
                  extra={"state": self._state,
                         "summary": {"state": self._state,
                                     "text": self._last_text}})

    # -- pre-READY runtime (audio chunks, not frames) ------------------------- #
    def prepare_runtime(self):
        """Build and probe Voice's complete pipeline before start() returns."""

        if self._voice_sm is not None:
            raise RuntimeError("voice pipeline is already initialized")
        from kit.logic.vad import VadSegmenter
        from kit.logic.wakeword import SherpaKwsWakeWord, AsrKeywordWakeWord
        from kit.logic.voice_sm import VoiceStateMachine

        verbose = self.verbose
        md = self.model_dir
        vad_model = os.path.join(md, "silero_vad.onnx")
        kws_dir = os.path.join(md, "kws")

        if verbose:
            print(f"[app:{self.id}] model_dir={md} backend={self.wake_backend} "
                  f"lang={self.language} min_silence={self.min_silence_sec} "
                  f"max_utt={self.max_utterance_sec} preroll_ms={self.preroll_ms}",
                  flush=True)

        # audio source: WAV injection (tests) or live RTSP mic (default) ------- #
        wav = os.environ.get("RECAMERA_VOICE_WAV") or self.config.get("wav_file")
        if wav:
            from kit.adapters.audio_source import WavFileAudioSource
            rt = str(os.environ.get("RECAMERA_VOICE_WAV_REALTIME", "")).strip().lower() \
                in ("1", "true", "yes", "on")
            if verbose:
                print(f"[app:{self.id}] audio source = WavFileAudioSource({wav}) "
                      f"realtime={rt}", flush=True)
            src = WavFileAudioSource(wav, realtime=rt, pad_silence_sec=1.0)
        else:
            from kit.adapters.audio_source import DEFAULT_AUDIO_FILTER
            # The live mic is very quiet (~-49 dBFS); apply an adaptive gain
            # filter so KWS/ASR get a normal level (wake-word detection depends
            # on it -- see DEFAULT_AUDIO_FILTER). Same knob on every backend:
            # "" / "none" disables -> unity gain.
            audio_filter = self.config.get("audio_filter", DEFAULT_AUDIO_FILTER)
            # audio_source: "ai_asr" (official, default) or "rtsp" (fallback).
            #   ai_asr -> ALSA shared-capture PCM: clean, no rkipc transcode hop,
            #             shares the mic with rkipc via dsnoop (no takeover). Needs
            #             root/audio group -> OK under appmgr (runs as root).
            #   rtsp   -> demux rkipc's combined RTSP audio track (kept as a
            #             fallback / A-B comparison; works even as non-root).
            audio_backend = str(self.config.get("audio_source", "ai_asr")).lower()
            if audio_backend == "rtsp":
                from kit.adapters.audio_source import RtspAudioSource
                # `self.source_url` is the CLI `--url` kit was started with (the
                # same value the pre-migration run(url=...) parameter carried).
                rtsp = self.source_url or self.config.get("rtsp_url") \
                    or "rtsp://admin:admin@127.0.0.1:5554/live/1"
                if verbose:
                    print(f"[app:{self.id}] audio source = RtspAudioSource({rtsp}) "
                          f"audio_filter={audio_filter!r}", flush=True)
                src = RtspAudioSource(rtsp, audio_filter=audio_filter)
            else:
                from kit.adapters.audio_source import AiAsrAudioSource
                device = self.config.get("ai_asr_device", "ai_asr")
                if verbose:
                    print(f"[app:{self.id}] audio source = "
                          f"AiAsrAudioSource({device}) audio_filter="
                          f"{audio_filter!r}", flush=True)
                src = AiAsrAudioSource(device, audio_filter=audio_filter)

        asr = self._asr
        if asr is None:
            raise RuntimeError("voice ASR is not initialized; call start() first")
        vad = VadSegmenter(model=vad_model,
                           min_silence_duration=self.min_silence_sec,
                           max_speech_duration=self.max_utterance_sec,
                           preroll_ms=self.preroll_ms)

        if self.wake_backend == "asr":
            if verbose:
                print(f"[app:{self.id}] wake backend = ASR keyword {self.wakeword!r}",
                      flush=True)
            # Give the wake-word's internal VAD the SAME model_dir silero as the
            # listening VAD above -- otherwise AsrKeywordWakeWord builds a bare
            # VadSegmenter() that falls back to kit.logic.vad.DEFAULT_VAD_MODEL.
            wake = AsrKeywordWakeWord(asr, self.wakeword.split("|"),
                                     vad_kwargs={"model": vad_model})
        else:
            if verbose:
                print(f"[app:{self.id}] wake backend = sherpa KeywordSpotter "
                      f"({kws_dir})", flush=True)
            wake = SherpaKwsWakeWord(
                keywords_file=os.path.join(kws_dir, "keywords.txt"),
                tokens=os.path.join(kws_dir, "tokens.txt"),
                encoder=os.path.join(kws_dir, "encoder.int8.onnx"),
                decoder=os.path.join(kws_dir, "decoder.int8.onnx"),
                joiner=os.path.join(kws_dir, "joiner.int8.onnx"),
                keywords_threshold=self.kws_threshold,
                keywords_score=self.kws_score,
            )

        sm = VoiceStateMachine(src, wake, vad, asr,
                               on_event=self._on_voice_event,
                               listen_timeout_sec=self.listen_timeout_sec,
                               verbose=verbose)
        # Publish ownership before open so App.start's rollback can close a
        # source that partially initialized and then failed its first PCM probe.
        self._voice_sm = sm
        sm.open()

    # -- main loop (self-paced: audio chunks, not frames) ---------------------- #
    def run(self):
        sm = self._voice_sm
        if sm is None:
            raise RuntimeError(
                "voice pipeline is not initialized; call start() first")
        verbose = self.verbose
        # publish the initial idle state so the panel shows something immediately
        self._on_voice_event({"type": "state", "state": IDLE})

        max_wakes = int(os.environ.get("RECAMERA_VOICE_MAX_WAKES", "0") or 0)
        if verbose:
            print(f"[app:{self.id}] ready -- listening (max_wakes={max_wakes})",
                  flush=True)
        count = sm.run(max_wakes=max_wakes)
        if verbose:
            print(f"[app:{self.id}] stopped after {count} transcript(s)", flush=True)


if __name__ == "__main__":
    run_app(VoiceTranscribeApp())

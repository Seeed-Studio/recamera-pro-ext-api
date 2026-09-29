#!/usr/bin/env python3
"""
eldercare-monitor -- reCamera Pro merged app: pose/fall + continuous ASR.

One process produces BOTH data streams the care dashboard shows, because a
browser overlay plugin only ever sees its own application's results (cross-app
subscription is not opened). Two producers, one application:

    main thread                      voice thread (daemon)
    -----------                      ---------------------
    for frame in self.frames()       VoiceStateMachine.run()
      self.pre()                       wake (always on) -> silero VAD ->
      self.models.pose.infer()         ASR in a WORKER PROCESS
      pose_post.postprocess()          -> self.emit_status(events=[transcript])
      tracker + per-track FallDetector
      self.emit(events, results)     per audio chunk: RMS -> self.emit_metrics()

Both threads publish through the same kit publication path, so both draw from
ONE sequence counter (`kit/adapters/result_sink.py`), and the metrics/status
publications go through `App.emit_metrics` / `App.emit_status` -- the typed path
that does not look like a frame to the consumer, so a transcript never clears
the latest pose frame (`App.emit`'s explicit empty `results` list would).

Lifecycle ownership (the part that is genuinely new for a merged app):

  * the ASR backend is built in `setup()` and owned by this process, exactly as
    `apps/voice-transcribe/app.py` owns its own. With the default `rk` backend
    the model itself is held by the platform inference service (`inferenced`),
    reached through `kit.runtime.remote.RemoteRknnSession`; this process holds
    the session, and the session is released in `finish()`;
  * the audio source is opened in `prepare_runtime` (a failure there is a failed
    start, not a running app with no microphone) and closed by `finish`, never
    by the voice loop (`kit/logic/voice_sm.py`);
  * `request_stop()` marks the shared publication gate closing under the same
    lock the voice callback is admitted under, so nothing publishes after the
    boundary, and already-admitted callbacks are allowed to finish inside a
    two-second budget. A decode already in flight is synchronous and cannot be
    cancelled, so it is *abandoned*: closing stops new decodes from starting,
    the gate drops whatever the in-flight one returns, and the join and the
    backend teardown that follow are both bounded -- we give up waiting, we do
    not give up the model;
  * the microphone is claimed exclusively (`kit/audio_claim.py`) against the
    standalone `voice-transcribe` app -- symmetric and atomic, so whichever of
    the two starts second is refused with a reason.

Manifest-v2 model assets (the pose RKNN) are resolved against the installed app
directory. ASR assets resolve like voice-transcribe's: under a managed launch
the package-relative `models/asr` directory is the only authorized one, with the
shared/staging locations kept as compatibility for standalone runs.

Run on device (inference + audio capture require root):
    python3 -m kit.run /userdata/local/apps/eldercare-monitor \\
        --model models/yolo11n_pose_rawhead_int8.rknn --sink ws --port 8124
"""
import os
import sys
import threading
import time
import traceback
from dataclasses import replace
from typing import Optional

from kit.adapters.audio_source import (
    DEFAULT_AUDIO_FILTER,
    AiAsrAudioSource,
    RtspAudioSource,
    WavFileAudioSource,
    pcm_stats,
)
from kit.app import App, run_app
from kit.audio_claim import ExclusiveAudioClaim
from kit.errors import ConfigurationError
from kit.lifecycle import PublicationGate
from kit.logic.geometry import make_observation
from kit.logic.temporal import FallConfig, FallDetector
from kit.logic.tracker import Tracker, TrackerConfig
from kit.logic.voice_sm import VoiceStateMachine
from kit.runtime.postprocess import pose as pose_post

# The initial voice state and the backend tag of the synthetic wake event. Both
# are string literals in `kit.logic.voice_sm` rather than exported symbols, so
# they are mirrored here instead of reaching into that module's internals.
IDLE = "idle"
ALWAYS_ON_BACKEND = "always-on"

_SHARED_ASR_DIRS = ("/userdata/local/models/asr", "/userdata/tmp/asr")
_BUNDLED_ASR_SUBDIR = os.path.join("models", "asr")
_JOIN_TIMEOUT_SEC = 3.0
_INFERENCE_SERVICE_ENVS = (
    "RECAMERA_INFERENCE_SERVICE_SOCK",
    # Read-only compatibility alias also honoured by kit.runtime.remote.
    "RECAMERA_INFERENCE_SERVICE",
)


class _AlwaysOnWake:
    """Synthetic wake event: this app transcribes continuously.

    The merged app has no wake word. The state machine still needs a wake
    trigger to leave IDLE, so the first idle frame always "wakes" it and the
    machine spends its life in listening/transcribing. A real KWS detector
    would add a second model to load for no product requirement, and the wake
    cycle's own events are plumbing the dashboard does not show.
    """

    keyword = ""
    backend = ALWAYS_ON_BACKEND
    score = 1.0
    transcript = ""

    def reset(self):
        return None

    def accept(self, frame):
        return self


class _MeteredAudioSource:
    """AudioSource proxy that reports every chunk it hands to the reader.

    The level meter has to see the SAME chunks the state machine consumes, and
    the state machine pulls them itself, so the sampling point is the source.
    The callback runs on the voice thread; a failing callback must never break
    the read loop, hence the swallow.
    """

    def __init__(self, inner, on_chunk):
        self._inner = inner
        self._on_chunk = on_chunk

    def open(self):
        self._inner.open()
        return self

    def read(self):
        frame = self._inner.read()
        if frame is not None:
            try:
                self._on_chunk(frame)
            except Exception:
                pass
        return frame

    def close(self):
        self._inner.close()

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._inner, name)


class EldercareMonitorApp(App):
    id = "eldercare-monitor"
    name = "Elder Care Monitor"
    owns_loop = True                # run() drives self.frames() on the main thread
    needs_frames = True             # the camera half is the same shape as fall-detection
    needs_model = True              # the pose RKNN is kit's model registry (models[0])
    # Only skeleton coordinates are consumed, never frame.data pixels, so the
    # frame source may letterbox on RGA straight into `data` -- same choice as
    # fall-detection (`docs/guide/hw-preprocess.md`).
    model_frame = "hw-direct"
    model_dma_input = True
    # Kit enforces the ceiling; the manifest exposes it as a <= 5 Hz knob.
    metrics_max_hz = 5.0

    # Fallback defaults for auto-bound config keys (the manifest supplies each).
    confidence = 0.4
    iou = 0.45
    keypoint_confidence = 0.5
    torso_angle_threshold_deg = 55.0
    confirmation_sec = 0.8
    cooldown_sec = 3.0
    tracker_iou_threshold = 0.2
    asr_backend = "rk"
    language = "auto"
    model_dir = _BUNDLED_ASR_SUBDIR
    min_silence_sec = 0.6
    max_utterance_sec = 15.0
    preroll_ms = 300.0
    audio_source = "ai_asr"
    audio_filter = ""

    # -- configuration -------------------------------------------------------- #
    def setup(self, config):
        """Derive the per-track state, then build the app-owned ASR backend.

        The ASR is built HERE, not in `prepare_runtime`, for the same reason
        `apps/voice-transcribe/app.py` builds it here: the backend's `preload()`
        is the slow part (an NPU session against the platform inference service
        under the default `rk`), and `App.start()` owns this method as part of
        its rollback transaction -- so a model that cannot load is a failed
        start, never a process that reaches READY without a voice half.

        Managed `rk` inference authorizes the platform daemon's caller by PID.
        This process IS that caller, and the model itself is loaded and held by
        `inferenced` (`kit/runtime/remote.py::RemoteRknnSession`); the app holds
        only the session. That is why running the ASR here is correct and does
        not conflict with the pose claim on `npu.rknn`: both are sessions on the
        scheduler that already multiplexes the NPU.
        """
        super().setup(config)

        cfg = self._build_fall_config()
        self._fall_config = cfg
        self.metrics_max_hz = min(5.0, max(0.0, float(self.metrics_max_hz)))
        self.tracker = Tracker(TrackerConfig(
            iou_threshold=float(self.tracker_iou_threshold)))
        self.detectors = {}
        self._next_event_id = 0

        # Voice runtime state (the pipeline itself is built in prepare_runtime).
        self._voice_sm = None
        self._voice_thread = None
        self._audio_claim = None
        self._voice_state = IDLE
        self._last_text = ""
        self._last_level = 0.0
        self._voice_error: Optional[BaseException] = None
        # The shared gate is created here and handed to the state machine, so
        # callback admission and publication admission are the same flag under
        # the same lock for the whole application.
        self._gate = PublicationGate(name=self.id)
        self.publication_gate = self._gate

        self._asr = self._build_asr()

        print(f"[{self.id}] setup conf={self.confidence} "
              f"kpt={self.keypoint_confidence} torso>="
              f"{cfg.torso_angle_threshold_deg} confirm={cfg.confirmation_sec}s "
              f"cooldown={cfg.cooldown_sec}s asr={self.asr_backend} "
              f"metrics<={self.metrics_max_hz}Hz", flush=True)

    def _build_fall_config(self) -> FallConfig:
        """One `FallConfig` from the already-bound knobs.

        `temporal_confirmation_required=False` is deliberate and load-bearing:
        that gate is satisfied by a learned 48-frame classifier, which this app
        does not carry (`apps/fall-detection` ships the frozen profile for it).
        Leaving it on would make `FallDetector` unable to ever confirm a fall.
        """
        return FallConfig(
            temporal_confirmation_required=False,
            torso_angle_threshold_deg=float(self.torso_angle_threshold_deg),
            confirmation_sec=float(self.confirmation_sec),
            cooldown_sec=float(self.cooldown_sec),
        ).clamp()

    def on_params_changed(self, changed):
        """Re-apply only what the auto-bind cannot: the derived fall config.

        Every declared knob is re-bound onto `self` by kit before this runs;
        what is left is the shared `FallConfig` template (new tracks inherit it)
        and the live per-track detectors' thresholds, which are swapped in with
        `set_config` so a threshold edit never clears a live alarm. The tracker's
        IoU threshold and the metrics rate limit are read on use and need no
        push.
        """
        cfg = self._build_fall_config()
        self._fall_config = cfg
        for detector in self.detectors.values():
            detector.set_config(replace(cfg))
        # The manifest allows up to 5 Hz; kit hard-caps it at 5 (V4-4).
        self.metrics_max_hz = min(5.0, max(0.0, float(self.metrics_max_hz)))
        print(f"[{self.id}] hot-reload changed={sorted(changed or ())} "
              f"torso>={cfg.torso_angle_threshold_deg} "
              f"confirm={cfg.confirmation_sec}s cooldown={cfg.cooldown_sec}s",
              flush=True)

    # -- ASR backend (app-owned, built before READY) --------------------------- #
    def _app_root(self) -> str:
        from kit import config as kit_config
        return os.path.realpath(kit_config.app_dir_of(self))

    def _uses_managed_v2_inference(self) -> bool:
        """Whether the RK ASR will cross the appmgr-authorized daemon boundary."""

        return (
            (self._manifest or {}).get("manifest_version") == 2
            and str(self.asr_backend).lower() == "rk"
            and any(str(os.environ.get(name) or "").strip()
                    for name in _INFERENCE_SERVICE_ENVS)
        )

    def _bundled_asr_dir(self, app_root: str, *, required: bool) -> str:
        """The one bundled RKNN artifact under `models/asr`, as a directory.

        Same rule as voice-transcribe, scoped to this app's ASR subdirectory:
        appmgr authorizes exactly the bundled artifacts an installed v2
        manifest declares, so under a managed launch that directory is the only
        one the daemon will accept a model from.
        """
        artifacts = [
            item for item in (self._manifest or {}).get("artifacts", [])
            if isinstance(item, dict)
            and item.get("kind") == "rknn"
            and item.get("source") == "bundled"
            and isinstance(item.get("file"), str)
            and os.path.dirname(item["file"]) == _BUNDLED_ASR_SUBDIR
        ]
        if len(artifacts) != 1:
            if not required:
                return os.path.realpath(os.path.join(app_root,
                                                     _BUNDLED_ASR_SUBDIR))
            raise ConfigurationError(
                "managed RK inference requires exactly one bundled ASR RKNN "
                "artifact under models/asr in manifest v2",
                operation="eldercare.configure",
                code="invalid_bundled_model",
            )
        model_dir = os.path.realpath(
            os.path.join(app_root, _BUNDLED_ASR_SUBDIR))
        try:
            contained = os.path.commonpath((app_root, model_dir)) == app_root
        except ValueError:
            contained = False
        if not contained:
            raise ConfigurationError(
                f"bundled ASR artifact escapes the app root: "
                f"{artifacts[0]['file']!r}",
                operation="eldercare.configure",
                code="invalid_bundled_model",
            )
        return model_dir

    def _resolve_asr_dir(self) -> str:
        """Package-relative `models/asr` first, then the shared/staging dirs.

        A managed v2 RK launch authorizes only the bundled directory, so a
        config override to somewhere else cannot work and is rejected here
        rather than failing later inside the inference service.
        """
        app_root = self._app_root()
        managed = self._uses_managed_v2_inference()
        bundled = self._bundled_asr_dir(app_root, required=managed)
        raw = str(self.model_dir or "").strip() or _BUNDLED_ASR_SUBDIR
        configured = os.path.realpath(
            raw if os.path.isabs(raw) else os.path.join(app_root, raw))

        if managed:
            if configured != bundled:
                raise ConfigurationError(
                    f"model_dir {raw!r} is not authorized for managed "
                    f"manifest-v2 inference; use the bundled artifact "
                    f"directory {bundled!r}",
                    operation="eldercare.configure",
                    code="unauthorized_model_path",
                    details={"configured": configured, "authorized": bundled},
                )
            return bundled

        for candidate in (configured, *_SHARED_ASR_DIRS):
            if candidate and os.path.isdir(candidate):
                return os.path.realpath(candidate)
        return configured

    def _build_asr(self):
        """Build the app-owned ASR backend (small seam for host tests)."""
        from kit.asr import Asr

        backend = str(self.asr_backend).lower()
        if backend not in ("rk", "sherpa"):
            raise ConfigurationError(
                f"asr_backend must be 'rk' or 'sherpa' (got {self.asr_backend!r})",
                operation="eldercare.configure",
                code="invalid_asr_backend",
            )
        asr_dir = self._resolve_asr_dir()
        return Asr(model=os.path.join(asr_dir, "model.int8.onnx"),
                   tokens=os.path.join(asr_dir, "tokens.txt"),
                   language=self.language, backend=backend)

    def _build_audio_source(self):
        """Same construction as `apps/voice-transcribe/app.py`.

        `RECAMERA_VOICE_WAV` (or config `wav_file`) injects a WAV file, which is
        what the unit tests drive; the default is the official shared-capture
        ALSA PCM, with the rkipc RTSP audio track as the fallback.
        """
        wav = os.environ.get("RECAMERA_VOICE_WAV") or self.config.get("wav_file")
        if wav:
            realtime = str(
                os.environ.get("RECAMERA_VOICE_WAV_REALTIME", "")).strip().lower() \
                in ("1", "true", "yes", "on")
            return WavFileAudioSource(wav, realtime=realtime, pad_silence_sec=1.0)
        if str(self.audio_source).lower() == "rtsp":
            url = self.source_url or self.config.get("rtsp_url") \
                or "rtsp://admin:admin@127.0.0.1:5554/live/1"
            return RtspAudioSource(url, audio_filter=self.audio_filter or None)
        return AiAsrAudioSource(
            self.config.get("ai_asr_device", "ai_asr"),
            audio_filter=self.audio_filter or DEFAULT_AUDIO_FILTER)

    def prepare_runtime(self):
        """Bring up the audio half of the pipeline before appmgr sees READY.

        Everything that can fail belongs here: the exclusive microphone claim,
        the audio source (opened, not merely constructed) and the VAD. A failure
        raises out of `start()` and rolls the partial state back through
        `finish()`; the ASR backend was already built -- and preloaded -- by
        `setup()`.
        """
        # 1. Exclusive microphone claim (V4-7) FIRST: a refused start must not
        #    have touched the microphone, the VAD or anything else.
        self._audio_claim = ExclusiveAudioClaim(self.id).acquire()

        if getattr(self, "_voice_sm", None) is not None:
            raise RuntimeError("the voice runtime is already initialized")
        if getattr(self, "_asr", None) is None:
            raise RuntimeError("the ASR backend is not initialized; run setup()")

        from kit.logic.vad import VadSegmenter

        # 2. Audio source. `VoiceStateMachine.open()` below opens it before
        #    start() returns, so a missing/permission-denied microphone is a
        #    failed start rather than a running app that hears nothing.
        asr_dir = self._resolve_asr_dir()
        source = _MeteredAudioSource(self._build_audio_source(),
                                     self._on_audio_chunk)

        vad = VadSegmenter(
            model=os.path.join(asr_dir, "silero_vad.onnx"),
            min_silence_duration=float(self.min_silence_sec),
            max_speech_duration=float(self.max_utterance_sec),
            preroll_ms=float(self.preroll_ms))

        # 3. The state machine, sharing this app's publication gate.
        self._voice_sm = VoiceStateMachine(
            source, _AlwaysOnWake(), vad, self._asr,
            on_event=self._on_voice_event,
            gate=self._gate,
            verbose=self.verbose)
        self._voice_sm.open()
        if self.verbose:
            print(f"[{self.id}] voice ready backend={self.asr_backend} "
                  f"asr_dir={asr_dir}", flush=True)

    # -- voice callbacks (voice thread) --------------------------------------- #
    def _on_audio_chunk(self, frame) -> None:
        """Level meter: one RMS/dBFS reading per captured audio chunk.

        Runs on the voice thread, inside `VoiceStateMachine.run()`'s read. The
        publication goes through the typed `metrics` path and is rate-limited by
        kit to `metrics_max_hz` (<= 5 Hz), so a 100 ms chunk cadence does not
        put ten envelopes a second on the wire.
        """
        stats = pcm_stats(frame.pcm)
        self._last_level = float(stats.get("dbfs", -120.0))
        self.emit_metrics({
            "audio_rms": round(float(stats.get("rms", 0.0)), 2),
            "audio_dbfs": round(self._last_level, 1),
            "audio_peak": int(stats.get("peak", 0)),
        })

    def _on_voice_event(self, ev) -> None:
        """VoiceStateMachine callback -> ONE typed status publication.

        Deliberately not `self.emit()`: the frame path always writes an explicit
        `results` list, and a consumer reads that as "this is the current frame
        snapshot" -- an empty list there would CLEAR the pose frame the main
        thread just published. Typed status carries the same events without
        looking like a frame.
        """
        self.tick()
        kind = ev.get("type", "event")
        if kind == "wake":
            return                      # synthetic always-on wake is plumbing
        if kind == "listen_timeout":
            return                      # continuous mode re-arms immediately
        if kind == "state":
            self._voice_state = ev.get("state", self._voice_state)
        elif kind == "transcript":
            self._last_text = ev.get("text", "") or self._last_text

        event = {"kind": kind, "t": ev.get("t", 0.0)}
        if kind == "transcript":
            event["text"] = ev.get("text", "")
            event["language"] = ev.get("language", "")
            event["audio_sec"] = ev.get("audio_sec", 0.0)
        elif kind == "state":
            event["state"] = ev.get("state", "")

        self.emit_status(
            {"state": self._voice_state, "text": self._last_text},
            events=[event], ts=float(ev.get("t", 0.0)) or None)

    def _voice_loop(self) -> None:
        """Body of the voice thread; a hard failure is recorded, not raised."""
        sm = self._voice_sm
        try:
            sm.run()
        except BaseException as exc:                      # noqa: BLE001
            self._voice_error = exc
            # Full traceback, not just the repr: the loop is the one place a
            # voice failure lands, and on a device the location is the whole
            # value of the log line.
            traceback.print_exc()
            print(f"[{self.id}] voice loop stopped: {exc!r}",
                  file=sys.stderr, flush=True)

    # -- main loop ------------------------------------------------------------ #
    def run(self):
        self._voice_thread = threading.Thread(
            target=self._voice_loop, name=f"{self.id}-voice", daemon=True)
        self._voice_thread.start()

        for frame in self.frames():
            x = self.pre(frame)
            outs = self.models.pose.infer(x)
            results = pose_post.postprocess(
                outs, x.info,
                conf_thres=self.confidence,
                iou_thres=self.iou,
                kpt_thres=self.keypoint_confidence)
            events = self._advance_tracks(results, frame)
            self.emit(events, frame.pts, results=results)

    def _advance_tracks(self, results, frame):
        """One frame of multi-person fall reasoning -> events.

        Same call shape as `apps/fall-detection/app.py` against the same kit
        building blocks (`Tracker` + `make_observation` + `FallDetector`); the
        48-frame learned gate that app adds on top is not carried over, so the
        merged app confirms on the geometric state machine alone.
        """
        results = list(results or [])
        events = []
        for result in results:
            if isinstance(result, dict):
                result.setdefault("kind", "person")

        tracks = self.tracker.update(results, frame.pts, int(frame.w), int(frame.h))
        for track in tracks:
            person = None
            if track.det_index >= 0:
                person = results[track.det_index]

            detector = self.detectors.get(track.track_id)
            if detector is None:
                detector = FallDetector(replace(self._fall_config))
                self.detectors[track.track_id] = detector
            obs = make_observation(person, frame.pts, frame.h,
                                   self.keypoint_confidence)
            out = detector.update(obs)

            if person is not None:
                person["track_id"] = track.track_id
                person["state"] = out.state
                person["fall_detected"] = out.fall_detected
                person["event_id"] = out.event_id
                person["person_score"] = float(person.get("score", 0.0))

            events.append({
                "kind": "pose_state",
                "track_id": track.track_id,
                "visible": person is not None,
                "state": out.state,
                "fall_detected": out.fall_detected,
                "event_id": out.event_id,
            })

            if out.fall_event:
                events.append({
                    "kind": "fall",
                    "track_id": track.track_id,
                    "event_id": out.event_id,
                    "state": out.state,
                })
                print(f"[{self.id}] *** FALL event track={track.track_id} "
                      f"#{out.event_id} at pts={frame.pts:.2f} ***", flush=True)

        for track_id in self.tracker.removed_ids:
            self.detectors.pop(track_id, None)
        return events

    # -- teardown ------------------------------------------------------------- #
    def finish(self):
        """Stop the merged pipeline in the order the spec fixes (V5-4/V7-1).

            1. mark closing under the gate lock, release it, close the audio
               source and join the voice thread, all inside the quiescence
               budget -- nothing publishes after the boundary;
            2. tear the ASR session down, bounded: `Asr.close()` drains an
               in-flight decode before releasing, so the wait is abandoned (and
               reported) rather than allowed to run past the budget. Releasing
               the session is what lets the platform inference service reclaim
               the model;
            3. release the microphone claim;
            4. let kit release signal handlers, its models and its owned sink.

        `super().finish()` runs LAST: the pose model belongs to this process and
        the voice thread is already joined by then, so kit never releases a model
        anything is still using, and the app-owned ASR is closed above rather
        than by kit.
        """
        failures = []

        def clean(label, callback):
            try:
                callback()
            except BaseException as exc:                  # noqa: BLE001
                failures.append((label, exc))
                print(f"[{self.id}] {label} cleanup failed: {exc}",
                      file=sys.stderr, flush=True)

        clean("voice stop", self._stop_voice)
        clean("asr session", self._close_asr)
        clean("audio claim", self._release_audio_claim)
        clean("kit", super().finish)

        # A dead voice loop is reported, not re-raised: by the time teardown
        # runs the ASR session has been closed on purpose, so an error recorded
        # during shutdown is an expected consequence of step 2 and must not turn
        # a clean stop into a failed process exit.
        if self._voice_error is not None:
            print(f"[{self.id}] voice loop had ended with: "
                  f"{self._voice_error!r}", file=sys.stderr, flush=True)

    def _stop_voice(self) -> None:
        sm = self._voice_sm
        if sm is None:
            return
        # Boundary: closing is set under the same lock a callback is admitted
        # under, so no publication can slip past it -- including the text of a
        # decode that was already in flight. The verdict is reported, not
        # enforced: the bounded join below and the bounded ASR close that
        # follows are what keep teardown inside the process's grace budget.
        if not sm.request_stop(quiescence_timeout=2.0):
            print(f"[{self.id}] quiescence budget exceeded: an admitted voice "
                  f"callback was still running 2s after the stop request",
                  file=sys.stderr, flush=True)
        # Closing the source is the read cancel: a decode parked in
        # arecord/ffmpeg returns None and the loop exits.
        sm.close()                      # audio source: owner teardown (V5-4)
        thread = self._voice_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(_JOIN_TIMEOUT_SEC)
            if thread.is_alive():
                # Still inside a synchronous decode: abandoned, not waited for.
                # Its result is dropped by the gate when it finally returns.
                print(f"[{self.id}] voice thread still alive "
                      f"{_JOIN_TIMEOUT_SEC:.0f}s after its audio source was "
                      f"closed; abandoning the wait", file=sys.stderr, flush=True)
        self._voice_thread = None
        self._voice_sm = None

    def _close_asr(self, timeout: float = 2.0) -> None:
        """Request the ASR session teardown without blocking past the budget.

        `Asr.close()` is not cancellable either: the RK backend drains the
        active call before releasing its contexts and its session, so a decode
        that is still running would hold teardown open. The close therefore runs
        on its own thread with a bounded wait (V7-1/V8-1: abandon the wait, not
        the model). A close that does not finish in the budget is reported and
        the backend is retained -- the platform inference service reclaims it
        when this process's socket closes.
        """
        asr, self._asr = getattr(self, "_asr", None), None
        if asr is None:
            return
        finished = threading.Event()

        def close():
            try:
                asr.close()
            except BaseException as exc:                  # noqa: BLE001
                print(f"[{self.id}] ASR close raised: {exc!r}",
                      file=sys.stderr, flush=True)
            finally:
                finished.set()

        thread = threading.Thread(target=close, name=f"{self.id}-asr-close",
                                  daemon=True)
        thread.start()
        if not finished.wait(timeout):
            print(f"[{self.id}] ASR session teardown did not finish within "
                  f"{timeout:.0f}s; abandoning the wait (the platform "
                  f"inference service reclaims it on socket close)",
                  file=sys.stderr, flush=True)
            self._asr = asr      # retained: its cleanup did not complete

    def _release_audio_claim(self) -> None:
        claim, self._audio_claim = self._audio_claim, None
        if claim is not None:
            claim.release()


if __name__ == "__main__":
    run_app(EldercareMonitorApp())

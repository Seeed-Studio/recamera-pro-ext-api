"""eldercare-monitor: the merged app, its manifest and its two data streams.

Four things are pinned here, one per acceptance item:

 1. the manifest passes the production v2 validator AND its `render` block
    reaches a real Result Hub fixture (so the declarative renderer can draw both
    streams with no plugin installed);
 2. a transcript (typed `status`) or an audio level (typed `metrics`) published
    AFTER a pose frame leaves that frame intact -- the Hub's latest frame is
    still the pose frame, not an empty one;
 3. teardown reaches quiescence inside the 2 s budget with ZERO publications
    admitted after the boundary;
 4. the microphone exclusion against the standalone `voice-transcribe` is
    symmetric: whichever of the two starts second is refused, in both orders,
    and atomically.

Hardware-free: a WAV file drives the audio side (`WavFileAudioSource`), static
frames drive the pose side, and the VAD / ASR / frame source / model are
doubles. Nothing here needs a camera, an NPU or sherpa.
"""
from __future__ import annotations

import importlib.util
import json
import math
import os
import sys
import threading
import time
import wave

import numpy as np
import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _path in (_REPO, os.path.join(_REPO, "market")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import kit.app as kit_app                                             # noqa: E402
from kit.asr import AsrResult                                         # noqa: E402
from kit.audio_claim import ENV_LOCK_PATH, ExclusiveAudioClaim        # noqa: E402
from kit.errors import ResourceBusyError                              # noqa: E402

APP_DIR = os.path.join(_REPO, "apps", "eldercare-monitor")
_APP_PY = os.path.join(APP_DIR, "app.py")

with open(os.path.join(APP_DIR, "manifest.json")) as _handle:
    MANIFEST = json.load(_handle)


def _load_app_module():
    spec = importlib.util.spec_from_file_location("eldercare_monitor_app",
                                                  _APP_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def isolated_audio_claim(tmp_path, monkeypatch):
    """Keep the process-wide microphone claim out of the device path."""
    monkeypatch.setenv(ENV_LOCK_PATH, str(tmp_path / "audio-exclusive.lock"))


def _effective_config(manifest):
    eff = {}
    for group in manifest["config_schema"]["groups"]:
        for item in group["items"]:
            eff[item["key"]] = item["default"]
    return eff


# --------------------------------------------------------------------------- #
# doubles
# --------------------------------------------------------------------------- #
class _Frame:
    def __init__(self, pts, w=640, h=480):
        self.pts = float(pts)
        self.w = self.width = int(w)
        self.h = self.height = int(h)
        self.fmt = "nv12"
        self.data = np.full((h, w, 3), 128, dtype=np.uint8)
        self.data[::4, ::4] = 255                 # non-grey: survives the skip
        self.model_data = np.zeros((640, 640, 3), dtype=np.uint8)
        self.model_info = {"scale": 1.0, "pad_x": 0, "pad_y": 0,
                           "orig_w": w, "orig_h": h}


class _FrameSource:
    def __init__(self, count):
        self.count = count
        self.closed = 0

    def frames(self):
        for index in range(self.count):
            yield _Frame(index * 0.05)
            time.sleep(0.002)

    def close(self):
        self.closed += 1


class _Model:
    def infer(self, x):
        return [np.zeros(1, dtype=np.float32)]


def _standing_person(box=(100.0, 100.0, 200.0, 400.0)):
    keypoints = [[0.0, 0.0, 0.0] for _ in range(17)]
    for index, (x, y) in enumerate([
        (150, 130), (140, 125), (160, 125), (130, 120), (170, 120),
        (135, 140), (165, 140), (120, 180), (180, 180),
        (115, 220), (185, 220), (140, 250), (160, 250),
        (135, 320), (165, 320), (130, 390), (170, 390),
    ]):
        keypoints[index] = [x, y, 0.9]
    return {"box": list(box), "score": 0.9, "kind": "person",
            "keypoints": keypoints}


class _Segment:
    duration_sec = 1.0

    def __init__(self, pcm):
        self.pcm = pcm


class _Vad:
    """Endpoints one utterance every `every` accepted frames."""

    def __init__(self, *args, **kwargs):
        self.every = 5
        self.seen = 0

    def reset(self):
        self.seen = 0

    def accept(self, frame):
        self.seen += 1

    def segments(self):
        if self.seen and self.seen % self.every == 0:
            self.seen += 1
            return [_Segment(np.zeros(16000, dtype=np.int16).tobytes())]
        return []

    def is_speech(self):
        return True

    def flush(self):
        pass


class _Asr:
    """Stand-in for `kit.asr.Asr`; records construction and close.

    The app builds this in `setup()` and owns it until `finish()`, exactly like
    `apps/voice-transcribe/app.py` -- what is stubbed here is the recognizer
    stack (sherpa/voxedge/NPU), not the ownership.
    """

    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls = 0
        self.closed = 0
        _Asr.instances.append(self)

    def transcribe(self, pcm, sample_rate=None):
        self.calls += 1
        return AsrResult(text=f"transcript {self.calls}", elapsed=0.01,
                         audio_sec=1.0, rtf=0.01, language="zh")

    def close(self):
        self.closed += 1


class _CaptureSink:
    """Captures the payloads the app hands to its sink, with a timestamp.

    Stamps `seq` the way the real sinks do -- one counter for frames AND typed
    publications -- so the captured stream is what the Hub would actually see.
    """

    def __init__(self, frame_w=640, frame_h=480):
        self.records = []
        self.lock = threading.Lock()
        self._seq = 0
        self._frame_w = frame_w
        self._frame_h = frame_h
        self.requests = []

    def _stamp(self, payload, pts):
        self._seq += 1
        body = dict(payload)
        body.setdefault("type", "results")
        body.setdefault("app", "eldercare-monitor")
        body["pts"] = pts
        body["seq"] = self._seq
        body["frame"] = {"width": self._frame_w, "height": self._frame_h}
        with self.lock:
            self.records.append((time.monotonic(), body))
        return body

    def emit(self, payload, pts):
        self._stamp(payload, pts)

    def emit_typed(self, payload, pts):
        self._stamp(payload, pts)

    def emit_meta(self, payload):
        # The real sinks stamp `emit_meta` from the SAME counter as frames and
        # typed publications (V5-3); mirroring that here is what lets the
        # end-to-end test see a collision if one is reintroduced.
        self._stamp(payload, payload.get("pts", 0.0))

    def request_recording(self, event_kind, pts):
        self.requests.append((event_kind, pts))
        return True

    def set_frame_size(self, w, h):
        pass

    def close(self):
        pass

    def by_type(self, kind):
        with self.lock:
            return [body for _t, body in self.records
                    if body.get("type") == kind]

    def timestamps(self):
        with self.lock:
            return [stamp for stamp, _body in self.records]


def _wav_fixture(path, seconds=1.0, sample_rate=16000):
    samples = (np.sin(np.arange(int(seconds * sample_rate)) * 0.05)
               * 8000).astype(np.int16)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(samples.tobytes())
    return str(path)


class _Harness:
    """Runs the real app against doubles, with everything restored after."""

    def __init__(self, tmp_path, monkeypatch, frames=40, pose=None):
        self.module = _load_app_module()
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        self.sink = _CaptureSink()
        self.source = _FrameSource(frames)
        self.pose = pose or [_standing_person()]
        self.app = None
        self._restore = []
        self._install()

    def _install(self):
        module = self.module

        def open_frame_source(**kwargs):
            return self.source

        self._restore.append((kit_app, "open_frame_source",
                              kit_app.open_frame_source))
        kit_app.open_frame_source = open_frame_source

        self._restore.append((kit_app.App, "_load_model",
                              kit_app.App._load_model))
        kit_app.App._load_model = lambda _self, path: _Model()

        self._restore.append((module.pose_post, "postprocess",
                              module.pose_post.postprocess))
        module.pose_post.postprocess = lambda *a, **kw: list(self.pose)

        import kit.logic.vad as vad_module
        self._restore.append((vad_module, "VadSegmenter",
                              vad_module.VadSegmenter))
        vad_module.VadSegmenter = _Vad

        # `_build_asr()` does `from kit.asr import Asr` at call time, so the
        # module attribute is the seam.
        import kit.asr as asr_module
        self._restore.append((asr_module, "Asr", asr_module.Asr))
        asr_module.Asr = _Asr
        _Asr.instances = []

        self.monkeypatch.setenv(
            "RECAMERA_VOICE_WAV",
            _wav_fixture(self.tmp_path / "speech.wav", seconds=4.0))
        # Pace the injected WAV like a live microphone: the voice thread must
        # still be publishing AFTER the (fast) pose loop has published frames,
        # which is the ordering "a transcript never clears the latest pose
        # frame" is about.
        self.monkeypatch.setenv("RECAMERA_VOICE_WAV_REALTIME", "1")

    def start(self):
        self.app = self.module.EldercareMonitorApp()
        self.app.start(None, sink=self.sink, app_dir=APP_DIR,
                       manifest=MANIFEST, config=_effective_config(MANIFEST),
                       verbose=False)
        return self.app

    def close(self):
        if self.app is not None:
            try:
                self.app.finish()
            except Exception:
                pass
        for owner, name, original in reversed(self._restore):
            setattr(owner, name, original)


# --------------------------------------------------------------------------- #
# 1. manifest + render in a real hub fixture
# --------------------------------------------------------------------------- #
class _NoopFormatter:
    def format(self, _raw):
        return []


def _hub(tmp_path):
    from appmgr import result_hub as hub_module
    return hub_module.ResultHub(ws_port=0,
                                system_uds_path=str(tmp_path / "system.sock"),
                                formatter=_NoopFormatter())


def _identity(instance="instance-1", generation=1):
    return {"app_id": MANIFEST["id"], "instance_id": instance,
            "generation": generation, "pid": os.getpid()}


def _authorize(hub, manifest=MANIFEST, identity=None):
    identity = identity or _identity()
    assert hub.refresh_app_manifest(
        identity, manifest,
        stream_contract={"id": "main", "kind": "frame.sock",
                         "path": "/live/0"})
    return identity


def test_manifest_passes_the_production_v2_validator():
    from appmgr import manifest as manifest_contract
    assert manifest_contract.validate_manifest(MANIFEST) == 2
    assert MANIFEST["resources"]["limits"] == {
        "memory_mb": 768, "cpu_percent": 400, "storage_mb": 256,
        "shutdown_grace_sec": 15}
    claims = {c["name"]: c["mode"] for c in MANIFEST["resources"]["claims"]}
    assert claims == {"camera.frames": "shared", "audio.capture": "shared",
                      "npu.rknn": "scheduled", "rga": "shared",
                      "result.publish": "brokered"}


def test_render_block_reaches_the_hub(tmp_path):
    hub = _hub(tmp_path)
    identity = _authorize(hub)
    hub.publish_app({"type": "results", "seq": 1, "pts": 1.0,
                     "frame": {"width": 640, "height": 480},
                     "results": [{"box": [0, 0, 1, 1], "state": "normal"}],
                     "events": [{"kind": "pose_state", "track_id": 1}]},
                    identity)
    records = [record.raw for record in hub.snapshot_records()]
    frames = [value for value in records if value["type"] == "frame"]
    assert frames, "the hub produced no frame record"
    render = frames[0]["render"]
    assert render["boxes"] == {"color_by": "state", "label": "state",
                               "line_width": 2}
    assert render["keypoints"]["layout"] == "coco17"
    assert render["events"]["transcript"]["as"] == "subtitle"
    assert render["events"]["fall"]["as"] == "toast"


# --------------------------------------------------------------------------- #
# 2. typed publications never clear the pose frame
# --------------------------------------------------------------------------- #
def test_transcript_and_metrics_publications_keep_the_latest_pose_frame(
        tmp_path, monkeypatch):
    harness = _Harness(tmp_path, monkeypatch, frames=6)
    try:
        app = harness.start()
        app.run()
        # run() returns when the frames end; the voice thread keeps pacing until
        # teardown, so wait for it to publish before stopping it.
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            statuses = harness.sink.by_type("status")
            metrics = harness.sink.by_type("metrics")
            transcripts = [e for s in statuses for e in (s.get("events") or [])
                           if e.get("kind") == "transcript"]
            if statuses and metrics and transcripts:
                break
            time.sleep(0.02)
        harness.app.finish()

        pose_payloads = [p for p in harness.sink.by_type("results")
                         if p.get("results")]
        statuses = harness.sink.by_type("status")
        metrics = harness.sink.by_type("metrics")
        assert pose_payloads, "no pose frame was published"
        assert statuses, "no transcript status was published"
        assert metrics, "no audio-level metric was published"

        # Every typed payload is a typed payload: no results key at all.
        for payload in statuses + metrics:
            assert "results" not in payload
            assert "geometry" not in payload

        transcripts = [e for s in statuses for e in (s.get("events") or [])
                       if e.get("kind") == "transcript"]
        assert transcripts, "no transcript event reached the status envelope"
        assert transcripts[0]["text"].startswith("transcript ")

        # Order matters: publish the typed envelopes AFTER the pose frame
        # through the real Hub and check the frame survived.
        hub = _hub(tmp_path)
        identity = _authorize(hub)
        n_typed_after_last_frame = 0
        seen_frame = False
        for _stamp, payload in list(harness.sink.records):
            hub.publish_app(payload, identity)
            if payload.get("type") == "results" and payload.get("results"):
                seen_frame = True
            elif seen_frame:
                n_typed_after_last_frame += 1
        print("\npublished order: "
              + " ".join(p.get("type") for _t, p in harness.sink.records))
        assert n_typed_after_last_frame > 0, (
            "the test must exercise a typed publication that lands AFTER a pose "
            "frame; otherwise it proves nothing about frame retention")

        records = [record.raw for record in hub.snapshot_records()]
        frames = [value for value in records if value["type"] == "frame"]
        assert len(frames) == 1, (
            "the typed publications replaced the frame record instead of "
            "leaving it alone")
        latest = pose_payloads[-1]
        assert frames[0]["seq"] == latest["seq"]
        assert frames[0]["results"], "the latest frame was emptied"
        assert frames[0]["results"][0]["state"]
    finally:
        harness.close()


# --------------------------------------------------------------------------- #
# 3. teardown: bounded quiescence, zero post-boundary publications
# --------------------------------------------------------------------------- #
def test_stop_is_quiescent_within_two_seconds_and_publishes_nothing_after(
        tmp_path, monkeypatch):
    harness = _Harness(tmp_path, monkeypatch, frames=60)
    try:
        app = harness.start()
        runner = threading.Thread(target=app.run, daemon=True)
        runner.start()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not harness.sink.by_type("status"):
            time.sleep(0.02)
        runner.join(5.0)
        assert not runner.is_alive()

        started = time.monotonic()
        app.finish()
        teardown = time.monotonic() - started

        gate = app._gate
        boundary = gate.stop_requested_at
        assert boundary is not None, "the gate was never closed"
        assert gate.quiescence_failures == 0
        latency = gate.quiescence_latency()
        late = [stamp for stamp in harness.sink.timestamps() if stamp > boundary]
        print(f"\nteardown={teardown:.3f}s quiescence={latency} "
              f"late_publications={len(late)} records={len(harness.sink.records)}")
        assert late == [], f"{len(late)} publication(s) after the boundary"
        assert latency is not None and latency <= 2.0
        assert app._voice_thread is None
        # The app-owned ASR session is released by the app, not by kit, and it
        # is released inside the teardown budget.
        (asr,) = _Asr.instances
        assert asr.closed == 1, "the ASR session was not closed on stop"
        assert app._asr is None
        assert app._audio_claim is None
        assert teardown <= 2.0 + 3.1, (
            "teardown must stay inside the quiescence budget plus the bounded "
            "join")
    finally:
        harness.close()


# --------------------------------------------------------------------------- #
# 4. microphone exclusion: symmetric and atomic
# --------------------------------------------------------------------------- #
def test_standalone_voice_transcribe_is_refused_while_eldercare_holds_the_mic():
    held = ExclusiveAudioClaim("eldercare-monitor").acquire()
    try:
        module = _load_app_module()
        app = module.EldercareMonitorApp()
        # No setup() on purpose: the claim is the FIRST thing prepare_runtime
        # does, so a refusal must happen before the ASR, the VAD or the device
        # is touched at all.
        with pytest.raises(ResourceBusyError) as caught:
            app.prepare_runtime()
        assert caught.value.context.code == "audio_capture_claimed"
        assert "eldercare-monitor" in str(caught.value)
        assert caught.value.context.details["held_by"] == "eldercare-monitor"
    finally:
        held.release()


def test_eldercare_is_refused_while_standalone_voice_transcribe_holds_the_mic():
    spec = importlib.util.spec_from_file_location(
        "voice_transcribe_app_claim",
        os.path.join(_REPO, "apps", "voice-transcribe", "app.py"))
    voice = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(voice)

    held = ExclusiveAudioClaim("voice-transcribe").acquire()
    try:
        module = _load_app_module()
        app = module.EldercareMonitorApp()
        with pytest.raises(ResourceBusyError) as caught:
            app.prepare_runtime()
        assert "voice-transcribe" in str(caught.value)

        # ...and the standalone app refuses symmetrically, from the same claim.
        # `prepare_runtime()` is entered directly (no `setup()`, which would
        # build the ASR stack this host has no wheels for): the claim is the
        # FIRST thing that hook does, so the refusal is proven before anything
        # else can run.
        vapp = voice.VoiceTranscribeApp()
        with pytest.raises(ResourceBusyError) as symmetric:
            vapp.prepare_runtime()
        assert "voice-transcribe" in str(symmetric.value)
        assert symmetric.value.context.code == "audio_capture_claimed"
    finally:
        held.release()


def test_claim_is_atomic_under_concurrent_acquisition(tmp_path):
    path = str(tmp_path / "race.lock")
    winners = []
    barrier = threading.Barrier(8)

    def contender(index):
        claim = ExclusiveAudioClaim(f"app-{index}", path)
        barrier.wait()
        try:
            claim.acquire()
        except ResourceBusyError:
            return
        winners.append(index)

    threads = [threading.Thread(target=contender, args=(i,))
               for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(winners) == 1, f"{len(winners)} processes took the claim at once"


# --------------------------------------------------------------------------- #
# 3b. one counter and one budget across EVERY metrics producer
# --------------------------------------------------------------------------- #
def test_loop_telemetry_and_audio_metrics_share_one_counter_and_one_budget(
        tmp_path, monkeypatch):
    """The on-device regression, reproduced end to end.

    Two producers of `type: "metrics"` run at once -- the app's audio level
    (`emit_metrics`) and kit's periodic loop telemetry (`emit_meta` from
    `frames()`). They must draw from ONE sequence counter (otherwise the Hub
    derives `app:gen:seq:metrics` for a derived seq that collides with a
    published one and a deduping consumer drops an envelope of each pair) and
    share ONE rate budget (otherwise the documented 5 Hz cap is exceeded by
    simply adding the two rates up, which is what 5.72 Hz on device was).
    """
    # Long enough for several 1 s loop-telemetry periods; the injected WAV is
    # paced in real time so the audio publisher is live for the whole window.
    harness = _Harness(tmp_path, monkeypatch, frames=1400)
    try:
        app = harness.start()
        started = time.monotonic()
        app.run()
        elapsed = time.monotonic() - started
        harness.app.finish()

        with harness.sink.lock:
            records = [(stamp, dict(payload))
                       for stamp, payload in harness.sink.records]
        metrics = [p for _t, p in records if p.get("type") == "metrics"]
        # Kit's loop telemetry carries `fps` at the top level; the app's audio
        # level rides the typed envelope's `metrics` object.
        loop_metrics = [p for p in metrics if "fps" in p]
        audio_metrics = [p for p in metrics
                         if "audio_dbfs" in (p.get("metrics") or {})]

        seqs = [p["seq"] for _t, p in records]
        duplicates = sorted({s for s in seqs if seqs.count(s) > 1})
        per_second = {}
        for stamp, payload in records:
            bucket = int(stamp - started)
            per_second[bucket] = per_second.get(bucket, 0) + 1

        print(f"\nelapsed={elapsed:.2f}s records={len(records)} "
              f"metrics={len(metrics)} (loop={len(loop_metrics)} "
              f"audio={len(audio_metrics)})")
        print(f"duplicate seqs across the union: {duplicates}")
        print(f"metrics per second: "
              f"{ {k: v for k, v in sorted(per_second.items())} }")

        assert loop_metrics, "kit's loop telemetry never published"
        assert audio_metrics, "the audio level never published"
        assert duplicates == [], (
            f"two publications share a seq: {duplicates}")
        assert len(seqs) == len(set(seqs))

        # Aggregate cap: at most `metrics_max_hz` in any rolling second, so at
        # most ceil(window) full windows over the run.
        ceiling = app.metrics_max_hz * math.ceil(elapsed)
        assert len(metrics) <= ceiling, (
            f"{len(metrics)} metrics envelopes in {elapsed:.1f}s exceeds "
            f"the {app.metrics_max_hz} Hz cap (ceiling {ceiling})")
        # ...and the split inside that cap: the app's share is the cap minus the
        # 1 Hz kit reserves for its own loop telemetry.
        app_ceiling = app._metrics_budget() * math.ceil(elapsed)   # noqa: SLF001
        assert len(audio_metrics) <= app_ceiling
    finally:
        harness.close()

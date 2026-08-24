"""
Equivalence gate for the voice-transcribe app-shape migration (KIT_APP_SHAPE_SPEC §3).

voice-transcribe is the SELF-PACED case: its input is audio chunks, not camera
frames, so it keeps `run()` but declares `owns_loop = True` + `needs_frames =
False`. The migration's point is the spec §2 hard constraint --

    "任何逃生口都不得连带剥夺其他基础设施"

-- i.e. taking over the rhythm must NOT cost the app `emit` / `tick` / models /
param auto-binding. Before the migration it did: the app carried a 17-line
`_find_configurable_sink()` that walked the sink tree by hand, called
`self._sink.emit(...)` directly, polled `self._maybe_reload()` itself and
forwarded config changes to the sink it had found. All of that is deleted; this
file pins that the OBSERVABLE output did not move.

Hardware-free by construction. Everything below the app is stubbed at the
sys.modules level -- `kit.asr`, `kit.logic.vad`, `kit.logic.wakeword` and
`kit.adapters.audio_source` never load sherpa-onnx, ffmpeg or ALSA -- and the
stubs are driven by ONE fixed chunk script, so both runs see byte-identical
audio. `kit.logic.voice_sm`'s clock is replaced by a deterministic counter so
the event timestamps (`t`, and therefore the emit `pts`) are comparable field
for field rather than "close enough".

The script exercises CROSS-CHUNK state on purpose:

    wake -> speech -> endpoint      a complete utterance (transcript #1)
    wake -> silence -> timeout      woke but said nothing (listen_timeout)
    wake -> speech -> endpoint      a second complete utterance (transcript #2)
    wake -> speech -> stream end    the trailing flush path (transcript #3)

Both paths are compared on:
  * the full published payload stream (events / results / state / summary / pts);
  * the VAD + wake-word + ASR call sequences (the cross-chunk state carriers);
  * the constructor kwargs each path handed the pipeline building blocks.

★Two deliberate, benign differences★, asserted explicitly rather than hidden:
  1. the new payload carries the three envelope keys every other app already
     emits (`inference_time_ms`, `pipeline_ms`, `stream_id`) because `emit()` is
     now the shared kit one. Additive only -- the manifest `output` block reads
     `state`, `summary.*` and `events[]`, none of which moved.
  2. `pipeline_ms` is 0.0 for a frameless app (there is no frame boundary to
     measure from).

Run: `python3 -m pytest kit/tests/test_voice_shape_equivalence.py -q`
"""
import importlib.util
import json
import os
import signal
import sys
import types
import unittest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from kit import app as kit_app                                       # noqa: E402
from kit.tests.legacy_loop import LegacyLoopApp              # noqa: E402
from kit.adapters.result_sink import MultiSink, ResultSink           # noqa: E402

APP_DIR = os.path.join(_REPO, "apps", "voice-transcribe")

# --------------------------------------------------------------------------- #
# the fixed audio-chunk script (one label per 100 ms chunk)
# --------------------------------------------------------------------------- #
#   "sil"       silence
#   "wake"      the chunk the wake-word detector fires on (when armed)
#   "speech"    voiced audio the VAD accumulates
#   "end"       the chunk on which the VAD endpoints the accumulated utterance
SCRIPT = [
    "sil", "sil",
    "wake", "speech", "speech", "end",          # utterance 1
    "sil",
    "wake", "sil", "sil", "sil", "sil", "sil", "sil", "sil", "sil",  # timeout
    "wake", "speech", "speech", "speech", "end",  # utterance 2
    "sil",
    "wake", "speech", "speech",                 # stream ends mid-listen -> flush
]

LISTEN_TIMEOUT_SEC = 0.5     # small, so the silence run above trips it


class _Seg:
    def __init__(self, seg_id, pcm, duration_sec):
        self.id = seg_id
        self.pcm = pcm
        self.duration_sec = duration_sec


class _AsrResult:
    def __init__(self, text, audio_sec, rtf, language):
        self.text = text
        self.audio_sec = audio_sec
        self.rtf = rtf
        self.language = language


class _WakeEvent:
    def __init__(self, keyword, backend, score, transcript):
        self.keyword = keyword
        self.backend = backend
        self.score = score
        self.transcript = transcript


class _Chunk:
    """Stand-in for a PcmFrame: an index + label is all the stubs need."""

    def __init__(self, idx, label):
        self.idx = idx
        self.label = label

    def __repr__(self):                      # pragma: no cover - debug aid
        return f"<chunk {self.idx} {self.label}>"


# --------------------------------------------------------------------------- #
# scripted stubs (shared by BOTH paths -- one instance per run)
# --------------------------------------------------------------------------- #
class _StubAudioSource:
    def __init__(self, path, realtime=False, pad_silence_sec=0.0, **kw):
        self.kwargs = dict(path=path, realtime=realtime,
                           pad_silence_sec=pad_silence_sec, **kw)
        self._i = 0
        self.closed = False
        self.open_calls = 0
        self.close_calls = 0

    def open(self):
        self.open_calls += 1
        return self

    def read(self):
        if self._i >= len(SCRIPT):
            return None
        c = _Chunk(self._i, SCRIPT[self._i])
        self._i += 1
        return c

    def close(self):
        self.close_calls += 1
        self.closed = True


class _StubWake:
    """Fires on a `wake` chunk while armed; `reset()` re-arms. Stateful across
    chunks exactly like the real KWS (it must NOT re-fire mid-utterance)."""

    def __init__(self, **kw):
        self.kwargs = kw
        self.armed = False
        self.calls = []
        self.resets = 0

    def reset(self):
        self.armed = True
        self.resets += 1

    def accept(self, frame):
        self.calls.append(frame.idx)
        if self.armed and frame.label == "wake":
            self.armed = False
            return _WakeEvent("hello camera", "kws", 0.87, "")
        return None


class _StubVad:
    """Accumulates `speech` chunks; endpoints on an `end` chunk (or on flush)."""

    def __init__(self, **kw):
        self.kwargs = kw
        self.buf = []
        self.pending = []
        self.calls = []
        self.resets = 0
        self.flushes = 0
        self._n = 0
        self._last_speech = False

    def reset(self):
        self.buf = []
        self.pending = []
        self.resets += 1
        self._last_speech = False

    def accept(self, frame):
        self.calls.append(frame.idx)
        if frame.label == "speech":
            self.buf.append(frame.idx)
            self._last_speech = True
            return
        if frame.label == "end":
            self.buf.append(frame.idx)
            self._close_segment()
            self._last_speech = False
            return
        self._last_speech = False

    def _close_segment(self):
        if not self.buf:
            return
        self._n += 1
        self.pending.append(_Seg(self._n, list(self.buf), len(self.buf) * 0.1))
        self.buf = []

    def flush(self):
        self.flushes += 1
        self._close_segment()

    def is_speech(self):
        return self._last_speech

    def segments(self):
        out, self.pending = self.pending, []
        return out


class _StubAsr:
    """Transcript is a pure function of the segment id -> both runs agree."""

    TEXTS = {1: "打开客厅的灯", 2: "hello world", 3: "把音量调小一点"}

    def __init__(self, **kw):
        self.kwargs = kw
        self.calls = []
        self.close_calls = 0

    def transcribe(self, pcm):
        seg_id = len(self.calls) + 1
        self.calls.append(list(pcm))
        return _AsrResult(self.TEXTS.get(seg_id, f"seg{seg_id}"),
                          audio_sec=len(pcm) * 0.1,
                          rtf=0.25 + 0.01 * seg_id,
                          language="zh")

    def close(self):
        self.close_calls += 1


class _FakeClock:
    """Deterministic monotonic clock: +0.1 s per call, identical in both runs."""

    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        self.t += 0.1
        return round(self.t, 3)


class _RecordingSink(ResultSink):
    def __init__(self):
        self.payloads = []
        self.metas = []
        self.reloads = []

    def emit(self, payload, pts):
        self.payloads.append((json.loads(json.dumps(payload)), pts))

    def emit_meta(self, payload):
        self.metas.append(payload)

    def on_config_reload(self, config):
        self.reloads.append(config)


# --------------------------------------------------------------------------- #
# OLD shape: the pre-migration voice-transcribe, reproduced verbatim
# (git edf95fd, apps/voice-transcribe/app.py lines 63-322)
# --------------------------------------------------------------------------- #
IDLE = "idle"
SHARED_MODEL_DIR = "/userdata/local/models/asr"
STAGING_MODEL_DIR = "/userdata/tmp/asr"


def _find_configurable_sink(sink):
    """VERBATIM copy of the 17 lines the migration deletes."""
    try:
        from kit.adapters.output_sink import ConfigurableSink
    except Exception:
        return None
    stack = [sink]
    while stack:
        s = stack.pop()
        if s is None:
            continue
        if isinstance(s, ConfigurableSink):
            return s
        children = getattr(s, "sinks", None)
        if isinstance(children, (list, tuple)):
            stack.extend(children)
    return None


class _LegacyVoiceApp(LegacyLoopApp):
    id = "voice-transcribe"
    name = "Voice Transcribe"
    postproc = "voice"
    needs_model = False

    def setup(self, config):
        super().setup(config or {})
        c = self.config
        self.wake_backend = str(c.get("wake_backend", "kws")).lower()
        self.asr_backend = str(c.get("asr_backend", "rk")).lower()
        self.wakeword = str(c.get("wakeword", "hello camera"))
        self.language = str(c.get("language", "auto"))
        self.min_silence_sec = float(c.get("min_silence_sec", 0.6))
        self.max_utterance_sec = float(c.get("max_utterance_sec", 15.0))
        self.preroll_ms = float(c.get("preroll_ms", 300.0))
        self.listen_timeout_sec = float(c.get("listen_timeout_sec", 8.0))
        self.kws_threshold = float(c.get("kws_threshold", 0.25))
        self.kws_score = float(c.get("kws_score", 1.5))
        self.model_dir = self._resolve_model_dir(c.get("model_dir") or SHARED_MODEL_DIR)
        self._state = IDLE
        self._last_text = ""
        self._sink = None
        self._out_sink = None

    def on_config_reload(self, config):
        params = self._reload_params(config)
        self.config = config or {}
        if "wakeword" in params:
            self.wakeword = str(params["wakeword"])
        self.min_silence_sec = self._reload_float(
            params, "min_silence_sec", self.min_silence_sec)
        self.max_utterance_sec = self._reload_float(
            params, "max_utterance_sec", self.max_utterance_sec)
        self.preroll_ms = self._reload_float(params, "preroll_ms", self.preroll_ms)
        self.listen_timeout_sec = self._reload_float(
            params, "listen_timeout_sec", self.listen_timeout_sec)
        out = getattr(self, "_out_sink", None)
        if out is not None:
            try:
                out.on_config_reload(config or {})
            except Exception:
                pass

    def _resolve_model_dir(self, preferred):
        for d in (preferred, SHARED_MODEL_DIR, STAGING_MODEL_DIR):
            if d and os.path.isdir(d):
                return d
        return preferred or SHARED_MODEL_DIR

    def _on_voice_event(self, ev):
        self._maybe_reload()
        kind = ev.get("type", "event")
        out = dict(ev)
        out["kind"] = kind
        if kind == "state":
            self._state = ev.get("state", self._state)
        elif kind == "transcript":
            self._last_text = ev.get("text", "") or self._last_text
        payload = {
            "results": [],
            "events": [out],
            "state": self._state,
            "summary": {"state": self._state, "text": self._last_text},
        }
        if self._sink is not None:
            try:
                self._sink.emit(payload, float(ev.get("t", 0.0)))
            except Exception:
                pass

    def run(self, model_path=None, *, source="ffmpeg", url=None,
            sink=None, n=0, every=1, verbose=True, **_kw):
        from kit.asr import Asr
        from kit.logic.vad import VadSegmenter
        from kit.logic.wakeword import SherpaKwsWakeWord, AsrKeywordWakeWord
        from kit.logic.voice_sm import VoiceStateMachine

        self._sink = sink
        self._install_reload_handler()
        self._out_sink = _find_configurable_sink(sink)
        md = self.model_dir
        asr_model = os.path.join(md, "model.int8.onnx")
        asr_tokens = os.path.join(md, "tokens.txt")
        vad_model = os.path.join(md, "silero_vad.onnx")
        kws_dir = os.path.join(md, "kws")

        wav = os.environ.get("RECAMERA_VOICE_WAV") or self.config.get("wav_file")
        from kit.adapters.audio_source import WavFileAudioSource
        rt = str(os.environ.get("RECAMERA_VOICE_WAV_REALTIME", "")).strip().lower() \
            in ("1", "true", "yes", "on")
        src = WavFileAudioSource(wav, realtime=rt, pad_silence_sec=1.0)

        asr = Asr(model=asr_model, tokens=asr_tokens, language=self.language,
                  backend=self.asr_backend)
        vad = VadSegmenter(model=vad_model,
                           min_silence_duration=self.min_silence_sec,
                           max_speech_duration=self.max_utterance_sec,
                           preroll_ms=self.preroll_ms)
        if self.wake_backend == "asr":
            wake = AsrKeywordWakeWord(asr, self.wakeword.split("|"),
                                      vad_kwargs={"model": vad_model})
        else:
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
        self._on_voice_event({"type": "state", "state": IDLE})
        max_wakes = int(os.environ.get("RECAMERA_VOICE_MAX_WAKES", "0") or 0)
        sm.run(max_wakes=max_wakes)


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #
def _load_new_app_module():
    path = os.path.join(APP_DIR, "app.py")
    spec = importlib.util.spec_from_file_location(
        "_voice_transcribe_app_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class _Stubs:
    """One set of scripted building blocks + the sys.modules shims that serve
    them, so neither path can reach sherpa-onnx / ffmpeg / ALSA."""

    def __init__(self):
        self.audio = None
        self.wake = None
        self.vad = None
        self.asr = None
        self.clock = _FakeClock()

    def _mk_audio(self, *a, **kw):
        self.audio = _StubAudioSource(*a, **kw)
        return self.audio

    def _mk_wake(self, *a, **kw):
        self.wake = _StubWake(**kw)
        return self.wake

    def _mk_vad(self, *a, **kw):
        self.vad = _StubVad(**kw)
        return self.vad

    def _mk_asr(self, *a, **kw):
        self.asr = _StubAsr(**kw)
        return self.asr

    def modules(self):
        m_asr = types.ModuleType("kit.asr")
        m_asr.Asr = self._mk_asr
        m_vad = types.ModuleType("kit.logic.vad")
        m_vad.VadSegmenter = self._mk_vad
        m_vad.DEFAULT_VAD_MODEL = "/dev/null"
        m_wake = types.ModuleType("kit.logic.wakeword")
        m_wake.SherpaKwsWakeWord = self._mk_wake
        m_wake.AsrKeywordWakeWord = self._mk_wake
        m_audio = types.ModuleType("kit.adapters.audio_source")
        m_audio.WavFileAudioSource = self._mk_audio
        m_audio.RtspAudioSource = self._mk_audio
        m_audio.AiAsrAudioSource = self._mk_audio
        m_audio.DEFAULT_AUDIO_FILTER = "loudnorm=I=-16:TP=-1.5"
        return {"kit.asr": m_asr, "kit.logic.vad": m_vad,
                "kit.logic.wakeword": m_wake,
                "kit.adapters.audio_source": m_audio}


class _Base(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(APP_DIR, "manifest.json")) as f:
            self.manifest = json.load(f)
        # effective config = manifest config_schema defaults + the test override
        self.eff = {}
        for g in self.manifest["config_schema"]["groups"]:
            for it in g["items"]:
                self.eff[it["key"]] = it["default"]
        self.eff["listen_timeout_sec"] = LISTEN_TIMEOUT_SEC

        self._env = os.environ.get("RECAMERA_VOICE_WAV")
        os.environ["RECAMERA_VOICE_WAV"] = "/tmp/fixture.wav"

        # ★load-bearing★: a voice app must never open a camera frame source.
        self._orig_open = kit_app.open_frame_source
        self.frame_source_opens = []

        def _tripwire(*a, **kw):
            self.frame_source_opens.append(kw)
            raise AssertionError("open_frame_source() called by an audio app")

        kit_app.open_frame_source = _tripwire
        self._orig_load = kit_app.App._load_model
        self.model_loads = []

        def _model_tripwire(app_self, path):
            self.model_loads.append(path)
            raise AssertionError(
                "kit's frame-oriented RKNN loader was used by an audio app")

        kit_app.App._load_model = _model_tripwire
        self._saved_modules = {}

    def tearDown(self):
        kit_app.open_frame_source = self._orig_open
        kit_app.App._load_model = self._orig_load
        if self._env is None:
            os.environ.pop("RECAMERA_VOICE_WAV", None)
        else:
            os.environ["RECAMERA_VOICE_WAV"] = self._env
        try:
            signal.signal(signal.SIGHUP, signal.SIG_DFL)
        except (ValueError, OSError):
            pass

    # -- module shim + deterministic clock, installed around one run -------- #
    def _install(self, stubs):
        saved = {k: sys.modules.get(k) for k in stubs.modules()}
        sys.modules.update(stubs.modules())
        import kit.logic.voice_sm as vsm
        saved_time = vsm.time
        vsm.time = stubs.clock
        return saved, saved_time

    def _restore(self, saved, saved_time):
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        import kit.logic.voice_sm as vsm
        vsm.time = saved_time

    def _run_old(self):
        sink = _RecordingSink()
        stubs = _Stubs()
        saved, saved_time = self._install(stubs)
        try:
            app = _LegacyVoiceApp()
            app.setup(dict(self.eff))
            app.run(None, source="ffmpeg", url=None, sink=sink, n=0,
                    verbose=False)
        finally:
            self._restore(saved, saved_time)
        return sink, app, stubs

    def _run_new(self, sink=None):
        sink = sink if sink is not None else _RecordingSink()
        stubs = _Stubs()
        saved, saved_time = self._install(stubs)
        try:
            mod = _load_new_app_module()
            app = mod.VoiceTranscribeApp()
            app.start(None, sink=sink, verbose=False, app_dir=APP_DIR,
                      manifest=self.manifest, config=dict(self.eff))
            try:
                app.run()
            finally:
                app.finish()
        finally:
            self._restore(saved, saved_time)
        return sink, app, stubs


_ADDED_KEYS = {"inference_time_ms", "pipeline_ms", "stream_id",
               # `render`: the kit-injected display declaration, added after
               # this reference was frozen (RENDER_DECLARATION_SPEC §3). It is
               # metadata about drawing, not app output -- own suite in
               # kit/tests/test_render_declaration.py.
               "render"}


def _core(payload):
    """The fields both shapes publish (drops the shared-envelope additions)."""
    return {k: v for k, v in payload.items() if k not in _ADDED_KEYS}


class VoiceEquivalenceTests(_Base):

    def test_state_machine_preserves_loop_error_when_close_also_fails(self):
        from kit.logic.voice_sm import VoiceStateMachine

        primary = ValueError("PRIMARY_READ")
        cleanup = RuntimeError("SECONDARY_CLOSE")

        class Source:
            def __init__(self):
                self.open_calls = 0
                self.close_calls = 0
                self.close_error = cleanup

            def open(self):
                self.open_calls += 1
                return self

            def read(self):
                raise primary

            def close(self):
                self.close_calls += 1
                if self.close_error is not None:
                    raise self.close_error

        class Wake:
            def reset(self):
                pass

        src = Source()
        sm = VoiceStateMachine(src, Wake(), object(), object(), verbose=False)
        with self.assertRaises(ValueError) as caught:
            sm.run()
        self.assertIs(caught.exception, primary)
        self.assertEqual(src.open_calls, 1)
        self.assertEqual(src.close_calls, 1)
        self.assertTrue(sm._opened)
        self.assertTrue(any("SECONDARY_CLOSE" in note
                            for note in getattr(primary, "__notes__", ())))

        src.close_error = None
        sm.close()
        self.assertEqual(src.close_calls, 2)
        self.assertFalse(sm._opened)

    def test_state_machine_closes_preopened_source_when_wake_reset_fails(self):
        from kit.logic.voice_sm import VoiceStateMachine

        primary = KeyboardInterrupt()

        class Source:
            def __init__(self):
                self.open_calls = 0
                self.close_calls = 0

            def open(self):
                self.open_calls += 1
                return self

            def close(self):
                self.close_calls += 1

        class Wake:
            def reset(self):
                raise primary

        src = Source()
        sm = VoiceStateMachine(src, Wake(), object(), object(), verbose=False)
        sm.open()
        with self.assertRaises(KeyboardInterrupt) as caught:
            sm.run()
        self.assertIs(caught.exception, primary)
        self.assertEqual(src.open_calls, 1)
        self.assertEqual(src.close_calls, 1)
        self.assertFalse(sm._opened)

    def test_deep_equal_across_chunks(self):
        old, _old_app, old_stubs = self._run_old()
        new, _new_app, new_stubs = self._run_new()

        print(f"\nchunks fed: {len(SCRIPT)}  "
              f"payloads: OLD {len(old.payloads)} NEW {len(new.payloads)}")
        self.assertEqual(len(new.payloads), len(old.payloads),
                         "the two paths published a different number of events")

        print("--- per-event (pts, kind, state, summary.text) ---")
        for i, ((po, ts_o), (pn, ts_n)) in enumerate(zip(old.payloads,
                                                         new.payloads)):
            co, cn = _core(po), _core(pn)
            eo = co["events"][0]
            en = cn["events"][0]
            print(f"ev {i:2d}: OLD t={ts_o} kind={eo['kind']:<14} "
                  f"state={co['state']:<12} text={co['summary']['text']!r}")
            print(f"ev {i:2d}: NEW t={ts_n} kind={en['kind']:<14} "
                  f"state={cn['state']:<12} text={cn['summary']['text']!r}")
            print(f"ev {i:2d}: EQUAL={(co, ts_o) == (cn, ts_n)}")
            self.assertEqual(ts_o, ts_n, f"event {i}: timestamp differs")
            self.assertEqual(eo, en, f"event {i}: event dict differs")
            self.assertEqual(co["state"], cn["state"], f"event {i}: state differs")
            self.assertEqual(co["summary"], cn["summary"],
                             f"event {i}: summary differs")
            self.assertEqual(co["results"], cn["results"],
                             f"event {i}: results differ")

        self.assertEqual([(_core(p), t) for p, t in old.payloads],
                         [(_core(p), t) for p, t in new.payloads],
                         "DEEP EQUAL failed on the published payload stream")
        print("DEEP EQUAL: payload streams identical")

        # -- the cross-chunk state carriers agreed too --------------------- #
        print("--- cross-chunk state carriers ---")
        for name, o, n in (
            ("wake.accept chunk idx", old_stubs.wake.calls, new_stubs.wake.calls),
            ("wake.reset count", old_stubs.wake.resets, new_stubs.wake.resets),
            ("vad.accept chunk idx", old_stubs.vad.calls, new_stubs.vad.calls),
            ("vad.reset count", old_stubs.vad.resets, new_stubs.vad.resets),
            ("vad.flush count", old_stubs.vad.flushes, new_stubs.vad.flushes),
            ("asr.transcribe pcm", old_stubs.asr.calls, new_stubs.asr.calls),
        ):
            print(f"  {name:<22} OLD={o}")
            print(f"  {name:<22} NEW={n}")
            self.assertEqual(o, n, f"{name} diverged")

        # -- both paths configured the building blocks identically ---------- #
        for name, o, n in (("asr", old_stubs.asr.kwargs, new_stubs.asr.kwargs),
                           ("vad", old_stubs.vad.kwargs, new_stubs.vad.kwargs),
                           ("wake", old_stubs.wake.kwargs, new_stubs.wake.kwargs),
                           ("audio", old_stubs.audio.kwargs,
                            new_stubs.audio.kwargs)):
            print(f"  {name} ctor kwargs equal: {o == n}")
            self.assertEqual(o, n, f"{name} was constructed differently")

        # -- anti-vacuous-pass assertions ---------------------------------- #
        evs = [p["events"][0] for p, _t in old.payloads]
        kinds = {k: sum(1 for e in evs if e["kind"] == k)
                 for k in sorted({e["kind"] for e in evs})}
        print("--- event kind distribution ---")
        print(kinds)
        self.assertGreater(len(evs), 0, "the fixture produced no events")
        self.assertGreaterEqual(kinds.get("transcript", 0), 1,
                                "no FINAL transcript in the fixture")
        self.assertEqual(kinds.get("transcript"), 3,
                         "expected 3 transcripts (2 endpointed + 1 flushed)")
        self.assertEqual(kinds.get("wake"), 4, "expected 4 wake events")
        self.assertGreaterEqual(kinds.get("listen_timeout", 0), 1,
                                "the wake-but-silent timeout path never ran")
        self.assertGreater(kinds.get("state", 0), 5,
                           "too few state transitions to be meaningful")
        texts = [e["text"] for e in evs if e["kind"] == "transcript"]
        print("transcripts:", texts)
        self.assertEqual(texts, ["打开客厅的灯", "hello world", "把音量调小一点"])
        # the persistent summary must carry the LAST transcript forward across
        # the non-transcript events that follow it
        after = [p["summary"]["text"] for p, _t in old.payloads][-1]
        self.assertEqual(after, texts[-1],
                         "summary.text did not persist past the transcript")

    def test_new_path_opens_no_camera_frame_source(self):
        """(a) the audio app must not touch the camera."""
        _sink, app, _stubs = self._run_new()
        print(f"\nopen_frame_source calls: {self.frame_source_opens} "
              f"(tripwire raises if non-empty)")
        print(f"kit frame-model loads: {self.model_loads}")
        print(f"needs_frames={app.needs_frames} owns_loop={app.owns_loop} "
              f"needs_model={app.needs_model}")
        self.assertEqual(self.frame_source_opens, [],
                         "a camera frame source was opened")
        self.assertEqual(self.model_loads, [],
                         "kit's frame-model loader was invoked")
        self.assertFalse(app.needs_frames)
        self.assertTrue(app.owns_loop)
        # and frames() refuses with the reason rather than silently yielding 0
        stubs = _Stubs()
        saved, saved_time = self._install(stubs)
        try:
            app.start(None, sink=_RecordingSink(), verbose=False,
                      app_dir=APP_DIR, manifest=self.manifest,
                      config=dict(self.eff))
            try:
                with self.assertRaises(RuntimeError) as cm:
                    next(iter(app.frames()))
                print(f"frames() -> {cm.exception}")
                self.assertIn("needs_frames = False", str(cm.exception))
            finally:
                app.finish()
        finally:
            self._restore(saved, saved_time)

    def test_asr_is_ready_before_start_returns_and_closed_exactly_once(self):
        """ASR initialization is inside App.start's appmgr READY transaction."""
        stubs = _Stubs()
        saved, saved_time = self._install(stubs)
        app = None
        try:
            mod = _load_new_app_module()
            app = mod.VoiceTranscribeApp()
            app.start(None, sink=_RecordingSink(), verbose=False,
                      app_dir=APP_DIR, manifest=self.manifest,
                      config=dict(self.eff))
            self.assertIsNotNone(stubs.asr)
            self.assertIs(app._asr, stubs.asr)
            self.assertEqual(stubs.asr.close_calls, 0)
            self.assertIsNotNone(app._voice_sm)
            self.assertEqual(stubs.audio.open_calls, 1)
            self.assertEqual(stubs.audio.close_calls, 0)

            app.finish()
            app.finish()
            self.assertEqual(stubs.asr.close_calls, 1)
            self.assertEqual(stubs.audio.close_calls, 1)
            self.assertIsNone(app._asr)
            self.assertIsNone(app._voice_sm)
        finally:
            if app is not None:
                app.finish()
            self._restore(saved, saved_time)

    def test_prepared_audio_source_is_not_reopened_by_run(self):
        stubs = _Stubs()
        saved, saved_time = self._install(stubs)
        app = None
        try:
            mod = _load_new_app_module()
            app = mod.VoiceTranscribeApp()
            app.start(None, sink=_RecordingSink(), verbose=False,
                      app_dir=APP_DIR, manifest=self.manifest,
                      config=dict(self.eff))
            self.assertEqual(stubs.audio.open_calls, 1)

            app.run()
            self.assertEqual(stubs.audio.open_calls, 1)
            self.assertEqual(stubs.audio.close_calls, 1)

            app.finish()
            app.finish()
            self.assertEqual(stubs.audio.close_calls, 1)
        finally:
            if app is not None:
                app.finish()
            self._restore(saved, saved_time)

    def test_audio_probe_failure_aborts_start_and_rolls_back_asr(self):
        error = RuntimeError("injected audio probe failure")

        class FailingAudio(_StubAudioSource):
            def open(self):
                self.open_calls += 1
                raise error

        stubs = _Stubs()

        def make_failing_audio(*args, **kwargs):
            stubs.audio = FailingAudio(*args, **kwargs)
            return stubs.audio

        stubs._mk_audio = make_failing_audio
        saved, saved_time = self._install(stubs)
        app = None
        try:
            mod = _load_new_app_module()
            app = mod.VoiceTranscribeApp()
            with self.assertRaisesRegex(RuntimeError, "audio probe") as caught:
                app.start(None, sink=_RecordingSink(), verbose=False,
                          app_dir=APP_DIR, manifest=self.manifest,
                          config=dict(self.eff))
            self.assertIs(caught.exception, error)
            self.assertEqual(stubs.audio.open_calls, 1)
            self.assertEqual(stubs.audio.close_calls, 1)
            self.assertEqual(stubs.asr.close_calls, 1)
            self.assertIsNone(app._voice_sm)
            self.assertIsNone(app._asr)
        finally:
            if app is not None:
                app.finish()
            self._restore(saved, saved_time)

    def test_start_failure_after_asr_preload_rolls_voice_resource_back(self):
        stubs = _Stubs()
        saved, saved_time = self._install(stubs)
        app = None
        try:
            mod = _load_new_app_module()
            app = mod.VoiceTranscribeApp()

            def fail_after_setup():
                raise RuntimeError("injected post-setup startup failure")

            app._install_reload_handler = fail_after_setup
            with self.assertRaisesRegex(RuntimeError, "post-setup"):
                app.start(None, sink=_RecordingSink(), verbose=False,
                          app_dir=APP_DIR, manifest=self.manifest,
                          config=dict(self.eff))
            self.assertIsNotNone(stubs.asr)
            self.assertEqual(stubs.asr.close_calls, 1)
            self.assertIsNone(app._asr)
        finally:
            if app is not None:
                app.finish()
            self._restore(saved, saved_time)

    def test_emit_goes_through_the_manifest_output_block(self):
        """(b) `emit` reaches the ConfigurableSink assembled from the manifest,
        with the app carrying ZERO sink-lookup code."""
        from kit.adapters.output_sink import (ConfigurableSink, RawJsonFormatter,
                                              assemble_output_sink)
        from kit.adapters.result_sink import OutputChannel

        class _Rec(OutputChannel):
            name = "mqtt"

            def __init__(self):
                self.msgs = []

            def publish(self, message):
                self.msgs.append(message)

            def close(self):
                pass

        rec = _Rec()
        cs = ConfigurableSink(app_id="voice-transcribe", channels=[rec],
                              formatter=RawJsonFormatter())
        ws = _RecordingSink()
        sink, app, _stubs = self._run_new(sink=MultiSink([ws, cs]))

        # the manifest DOES opt this app into the unified output pipeline
        _s, opted_in = assemble_output_sink(app, APP_DIR, self.manifest,
                                            dict(self.eff), verbose=False)
        print(f"\nmanifest capabilities={self.manifest['capabilities']} "
              f"opted_in={opted_in}")
        self.assertTrue(opted_in, "voice-transcribe must opt into `output`")

        self.assertTrue(rec.msgs, "nothing reached the ConfigurableSink")
        bodies = [json.loads(m.body.decode("utf-8")) for m in rec.msgs]
        print(f"ConfigurableSink messages: {len(bodies)} "
              f"(WS payloads: {len(ws.payloads)})")
        print("last envelope keys:", sorted(bodies[-1]))
        # the manifest `output.fields` read state / summary.* / events[]
        for b in bodies:
            self.assertIn("state", b)
            self.assertIn("summary", b)
            self.assertIn("events", b)
        last_transcript = [b for b in bodies
                           if b["events"] and b["events"][0]["kind"] == "transcript"]
        self.assertTrue(last_transcript, "no transcript reached the output block")
        print("transcript envelope summary:", last_transcript[-1]["summary"])

        # the app itself owns no sink plumbing any more
        src = open(os.path.join(APP_DIR, "app.py")).read()
        for banned in ("_find_configurable_sink", "_out_sink", "self._sink",
                       "_maybe_reload", "on_config_reload"):
            self.assertNotIn(banned, src,
                             f"{banned} is still present in the app")

    def test_shape_flags_and_no_legacy_hooks(self):
        mod = _load_new_app_module()
        cls = mod.VoiceTranscribeApp
        self.assertTrue(cls.owns_loop)
        self.assertFalse(cls.needs_frames)
        self.assertFalse(cls.needs_model)
        for hook in ("process_frame", "on_results", "run_postproc"):
            # the pre-migration hooks were deleted from kit.app.App; defining
            # one on an app is now dead code (and a _check_loop_shape error)
            self.assertFalse(hasattr(kit_app.App, hook))
            self.assertFalse(hasattr(cls, hook),
                             f"removed hook {hook} is still defined")
        # run() takes no positional args (the new-shape signature)
        self.assertFalse(kit_app._run_takes_positional(cls.run))

    def test_captured_voice_params_require_restart_but_sink_still_reloads(self):
        """SIGHUP must not claim to mutate the already-open Voice graph."""
        sink = _RecordingSink()
        stubs = _Stubs()
        saved, saved_time = self._install(stubs)
        try:
            mod = _load_new_app_module()
            app = mod.VoiceTranscribeApp()
            app.start(None, sink=sink, verbose=False, app_dir=APP_DIR,
                      manifest=self.manifest, config=dict(self.eff))
        finally:
            self._restore(saved, saved_time)
        try:
            print(f"\nauto-bound: wakeword={app.wakeword!r} "
                  f"min_silence_sec={app.min_silence_sec} "
                  f"preroll_ms={app.preroll_ms} "
                  f"audio_filter={app.audio_filter!r}")
            self.assertEqual(app.wakeword, "hello camera")
            self.assertEqual(app.min_silence_sec, 0.6)
            self.assertEqual(app.audio_filter, "loudnorm=I=-16:TP=-1.5")

            from kit import config as _cfg
            orig = _cfg.effective_config
            new_cfg = dict(self.eff)
            new_cfg.update({"wakeword": "hey cam", "min_silence_sec": 1.2,
                            "wake_backend": "asr"})     # apply:"restart" -> ignored
            _cfg.effective_config = lambda *a, **kw: new_cfg
            try:
                app._reload_flag = True
                # Driven through the app's real loop body -- it must call
                # self.tick() at the event boundary, exactly where the
                # pre-migration code called self._maybe_reload().
                app._on_voice_event({"type": "state", "state": "listening",
                                     "t": 5.0})
            finally:
                _cfg.effective_config = orig
            print(f"after SIGHUP: wakeword={app.wakeword!r} "
                  f"min_silence_sec={app.min_silence_sec} "
                  f"wake_backend={app.wake_backend!r} "
                  f"sink reloads={len(sink.reloads)}")
            self.assertEqual(app.wakeword, "hello camera",
                             "captured wakeword was changed without restart")
            self.assertEqual(app.min_silence_sec, 0.6,
                             "captured VAD threshold changed without restart")
            self.assertEqual(app.wake_backend, "kws",
                             "an apply:'restart' knob was hot-reloaded")
            self.assertEqual(len(sink.reloads), 1,
                             "kit did not route the reload into the sink")
        finally:
            app.finish()


if __name__ == "__main__":
    unittest.main()

"""
Offline ASR wrapper for reCamera Pro (Rockchip RV1126B) -- voxedge consumer.

Path B (voxedge-consumer) refactor: the `Asr` class is now a THIN WRAPPER around
a voxedge `ASRBackend` (voxedge.backends.base). Instead of calling sherpa-onnx
`from_sense_voice` directly, it delegates transcription to a swappable voxedge
backend selected by the `backend=` argument:

    backend="sherpa"  (default)  CPU SenseVoice-int8 via voxedge SherpaASRBackend
    backend="rk"                 NPU w4a16 SenseVoice via kit.asr_rknn_backend
                                 (a lightweight voxedge ASRBackend subclass that
                                  reuses our verified rknnlite + fbank + CTC decode)

Everything downstream is unchanged: `transcribe(pcm_16k_mono) -> AsrResult`
(still unpackable as `(text, info)`), and we keep computing `elapsed / rtf /
audio_sec` ourselves (voxedge's `TranscriptionResult` carries only text +
language). The heavy model load happens once in `__init__`; `transcribe()` is
the hot path fed by an `AudioSource`.

Why a thin kit-side subclass for the CPU path
---------------------------------------------
voxedge's stock `SherpaASRBackend` exposes only `transcribe(wav_bytes)` (which
needs `soundfile`/libsndfile -- awkward on the musl device) and resolves the
SenseVoice model via a `{model_root}/sensevoice/sherpa-onnx-sense-voice-*` glob.
Our device stages a FLAT layout (`/userdata/tmp/asr/model.int8.onnx`) and the
venv has no libsndfile. So `_SenseVoiceCPUBackend` below subclasses the voxedge
backend to (a) load the offline recognizer from our explicit model/tokens paths
with `num_threads=4` (byte-identical to the previously-verified spike), and
(b) add `transcribe_array(float32_16k)` -- a numpy-in, soundfile-free entry that
plugs into voxedge's `supports_offline_streaming` contract. It reuses voxedge's
ABC, `SherpaASRConfig`, capability reporting and `resolve_reported_language`.

Feasibility (verified on device root@192.168.42.1, firmware 6.1.157, 2026-08-09)
------------------------------------------------------------------------------
sherpa-onnx CPU + SenseVoice int8 ONNX decodes 16k mono correctly.
Known-good baseline: `asr_example_zh.wav` ->
"欢迎大家来体验达摩院推出的语音识别模型。".

Runtime dependency
------------------
`voxedge` (pure python core, +numpy) plus `sherpa_onnx` live in the device venv
`/userdata/rknnenv`. voxedge + sherpa are imported LAZILY inside `Asr.__init__`
so importing this module (e.g. for the RtspAudioSource half) does NOT require
them on hosts that only exercise the audio path.

Model artifacts (staged on device by the feasibility spike, reuse verbatim):
    /userdata/tmp/asr/model.int8.onnx   SenseVoice int8 ONNX
    /userdata/tmp/asr/tokens.txt        token table
"""
from __future__ import annotations

import time
import wave
from dataclasses import dataclass
from typing import Optional, Union

# Default on-device artifact locations (feasibility spike output; reuse as-is).
DEFAULT_MODEL = "/userdata/tmp/asr/model.int8.onnx"
DEFAULT_TOKENS = "/userdata/tmp/asr/tokens.txt"


@dataclass
class AsrResult:
    """Structured transcription result (also unpackable as `(text, info)`)."""
    text: str
    elapsed: float          # wall-clock decode seconds (excludes model load)
    audio_sec: float        # duration of the decoded audio
    rtf: float              # elapsed / audio_sec (CPU real-time factor)
    language: str = ""

    def __iter__(self):
        # allow `text, info = asr.transcribe(...)`
        yield self.text
        yield {"elapsed": self.elapsed, "audio_sec": self.audio_sec,
               "rtf": self.rtf, "language": self.language}


def _build_cpu_backend(model, tokens, *, num_threads, use_itn, language):
    """Construct + return a voxedge SherpaASRBackend subclass for the CPU path.

    Imported lazily so the module imports without voxedge/sherpa present.
    """
    import numpy as np
    from voxedge.backends.sherpa.asr import SherpaASRBackend, SherpaASRConfig
    from voxedge.backends.base import TranscriptionResult

    # "auto" -> "" (voxedge/sherpa convention: empty offline_language == auto).
    offline_language = "" if (language in (None, "auto", "")) else str(language)

    class _SenseVoiceCPUBackend(SherpaASRBackend):
        """Explicit-path SenseVoice offline backend + numpy `transcribe_array`.

        Reuses voxedge's config / capability / language-resolution machinery;
        only overrides model loading (flat explicit paths + num_threads=4) and
        adds the soundfile-free array entry so the device venv needs no
        libsndfile.
        """

        # Opt into voxedge's generic offline->streaming adapter + STREAMING cap.
        supports_offline_streaming = True

        def __init__(self, _model, _tokens):
            super().__init__(SherpaASRConfig(
                offline_language=offline_language,
                offline_use_itn=bool(use_itn),
                offline_provider="cpu",
                streaming_provider="cpu",
                num_threads=int(num_threads),
            ))
            self._model_path = _model
            self._tokens_path = _tokens

        def preload(self) -> None:
            # Load ONLY the offline SenseVoice recognizer (no streaming
            # Paraformer -- we don't ship it and don't need it here).
            from voxedge.backends._deps import check_sherpa_deps
            check_sherpa_deps()
            self._offline_recognizer = self._load_offline_recognizer()

        def _load_offline_recognizer(self):
            # Override voxedge's `{model_root}/sensevoice/...` glob: load from the
            # flat explicit paths the device actually stages, preserving the
            # verified num_threads=4 / use_itn / language settings.
            import sherpa_onnx
            cfg = self._config
            return sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=self._model_path,
                tokens=self._tokens_path,
                num_threads=cfg.num_threads,
                use_itn=cfg.offline_use_itn,
                language=cfg.offline_language,
                provider=cfg.offline_provider,
            )

        def transcribe_array(self, samples, language="auto") -> "TranscriptionResult":
            """Offline transcription of a float32 mono 16k sample array.

            The soundfile-free counterpart to voxedge's `transcribe(wav_bytes)`;
            consumed by our `Asr.transcribe`. Returns voxedge's
            `TranscriptionResult`.
            """
            if self._offline_recognizer is None:
                raise RuntimeError("Offline recognizer not loaded; call preload() first")
            # Reported language: prefer what SenseVoice actually detected; fall
            # back to the reconciled config pin (voxedge semantics).
            eff = self._effective_language(language)
            rec = self._offline_recognizer
            s = rec.create_stream()
            s.accept_waveform(16000, np.ascontiguousarray(samples, dtype=np.float32))
            rec.decode_stream(s)
            res = s.result
            detected = getattr(res, "lang", "") or ""
            # SenseVoice tags language like "<|zh|>"; strip decoration if present.
            detected = detected.strip("<|>") if detected else ""
            return TranscriptionResult(text=res.text.strip(),
                                       language=detected or eff)

    b = _SenseVoiceCPUBackend(model, tokens)
    b.preload()
    return b


class Asr:
    """voxedge-backed SenseVoice offline recognizer, loaded once.

    Example:
        asr = Asr()                       # CPU SenseVoice via voxedge, loads once
        asr = Asr(backend="rk")           # NPU w4a16 via voxedge (kit rk backend)
        text, info = asr.transcribe(pcm)  # pcm = int16 ndarray | bytes, 16k mono
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        tokens: str = DEFAULT_TOKENS,
        *,
        backend: str = "sherpa",
        num_threads: int = 4,
        use_itn: bool = True,
        language: str = "auto",
        sample_rate: int = 16000,
        debug: bool = False,
        rknn_backend=None,
    ):
        self.model = model
        self.tokens = tokens
        self.num_threads = int(num_threads)
        self.use_itn = bool(use_itn)
        self.language = language
        self.sample_rate = int(sample_rate)
        self.backend_name = str(backend).lower()
        self._debug = bool(debug)
        self._closed = False

        t0 = time.time()
        if rknn_backend is not None:
            # Caller supplied an already-constructed voxedge ASRBackend.
            self._backend = rknn_backend
            if not self._backend.is_ready():
                self._backend.preload()
        elif self.backend_name == "rk":
            # NPU w4a16 SenseVoice via our lightweight voxedge ASRBackend subclass
            # (reuses the verified rknnlite + fbank + CTC decode). Lazy import so
            # the CPU path never pulls rknnlite.
            from kit.asr_rknn_backend import build_rknn_backend
            self._backend = build_rknn_backend(
                model=model,
                tokens=tokens,
                language=self.language,
                use_itn=self.use_itn,
                debug=debug,
            )
        else:
            self._backend = _build_cpu_backend(
                model, tokens,
                num_threads=self.num_threads,
                use_itn=self.use_itn,
                language=self.language,
            )
        self.load_sec = time.time() - t0

    def close(self) -> None:
        """Release backend resources exactly once.

        The RK backend destroys every RKNN context before releasing its shared
        broker lease.  CPU/third-party voxedge backends are feature-detected so
        they remain source compatible.  A cleanup error leaves ``_closed``
        false, allowing a retry while the RK backend retains its lease
        fail-closed.
        """

        if self._closed:
            return
        backend = self._backend
        cleanup = getattr(backend, "unload", None)
        if not callable(cleanup):
            cleanup = getattr(backend, "close", None)
        if callable(cleanup):
            cleanup()
        self._closed = True

    unload = close

    def __enter__(self) -> "Asr":
        if self._closed:
            raise RuntimeError("cannot enter a closed ASR backend")
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        try:
            self.close()
        except BaseException:
            if exc is None:
                raise
        return False

    @property
    def backend(self):
        """The underlying voxedge `ASRBackend` (for capability introspection)."""
        return self._backend

    # -- input normalisation --------------------------------------------------- #
    def _to_float32(self, pcm) -> "object":
        """Coerce int16 ndarray / bytes / float32 ndarray -> mono float32 [-1,1]."""
        import numpy as np
        if isinstance(pcm, (bytes, bytearray, memoryview)):
            arr = np.frombuffer(bytes(pcm), dtype=np.int16).astype(np.float32) / 32768.0
        else:
            arr = np.asarray(pcm)
            if arr.dtype == np.int16:
                arr = arr.astype(np.float32) / 32768.0
            else:
                arr = arr.astype(np.float32)
        if arr.ndim > 1:  # interleaved -> mono
            arr = arr.reshape(arr.shape[0], -1).mean(axis=1)
        return arr

    # -- hot path -------------------------------------------------------------- #
    def transcribe(
        self,
        pcm: Union[bytes, "object"],
        sample_rate: Optional[int] = None,
    ) -> AsrResult:
        """Transcribe one utterance of 16k mono PCM through the voxedge backend.

        `pcm` may be a little-endian int16 `bytes` buffer (as carried by
        `PcmFrame.pcm`) or a numpy int16/float32 array. Returns `AsrResult`,
        which also unpacks as `(text, info_dict)`. We keep our own timing
        (elapsed / audio_sec / rtf); voxedge supplies text + language.
        """
        if self._closed:
            raise RuntimeError("cannot transcribe with a closed ASR backend")
        sr = int(sample_rate or self.sample_rate)
        audio = self._to_float32(pcm)
        audio_sec = float(audio.size) / sr if audio.size else 0.0

        t0 = time.time()
        result = self._backend.transcribe_array(audio, self.language)
        elapsed = time.time() - t0

        text = result.text or ""
        lang = result.language or ""
        rtf = elapsed / audio_sec if audio_sec > 0 else 0.0
        return AsrResult(text=text, elapsed=elapsed, audio_sec=audio_sec,
                         rtf=rtf, language=lang)

    def transcribe_wav(self, path: str) -> AsrResult:
        """Convenience: read a 16k(-ish) WAV file and transcribe it."""
        import numpy as np
        with wave.open(path) as wf:
            sr, n, ch = wf.getframerate(), wf.getnframes(), wf.getnchannels()
            raw = wf.readframes(n)
        arr = np.frombuffer(raw, dtype=np.int16)
        if ch > 1:
            arr = arr.reshape(-1, ch).mean(axis=1).astype(np.int16)
        return self.transcribe(arr, sample_rate=sr)

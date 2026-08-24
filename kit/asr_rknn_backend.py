"""
NPU (rv1126b) SenseVoice w4a16 ASR backend -- voxedge ASRBackend subclass.

Phase-2(a) of the voxedge-consumer refactor (see kit/asr.py). This is a
LIGHTWEIGHT voxedge `ASRBackend` implementation that reuses our already-verified
on-device decode:

    kaldi_native_fbank (80-bin, hamming)  ->  LFR (m=7,n=6, 560-dim)  ->  CMVN
    -> 4 SenseVoice prompt frames (lang / event / itn embeddings)
    -> RKNNLite encoder (w4a16, single-core init_runtime -- NO core_mask)
    -> greedy CTC collapse -> sentencepiece detokenize

Why NOT voxedge's stock `RKASRBackend` (voxedge.backends.rk.asr): that adapter
wraps the full `rkvoice_stream` stack (rknn-toolkit-lite2 + spm + kaldi_fbank +
rkvoice_stream, ~50-100 MB, and its `sensevoice_rknn.py` hard-codes
`core_mask=NPU_CORE_0` which is a 3576/3588 multi-core concept that ERRORS on the
single-core rv1126b). On a 2 GB device we instead implement the voxedge
`ASRBackend` interface directly over the exact decode we already proved works.
The backend is fully swappable via `Asr(backend="rk")` and satisfies the same
`transcribe_array(float32_16k) -> TranscriptionResult` contract as the CPU path,
so downstream (VAD / wake / state machine / app) is untouched.

Ported verbatim (numerically) from the device spike scripts
    /userdata/tmp/asr/device_e2e_rv.py
    /userdata/tmp/asr/device_decode_lowmem.py

Runtime deps (device venv /userdata/rknnenv): rknn-toolkit-lite2 (rknnlite),
kaldi_native_fbank, sentencepiece, numpy. All imported LAZILY in `preload()` so
this module imports on a Mac / CPU host without the NPU runtime.

Model + assets (staged on device):
    sensevoice_rv1126b_w4a16.rknn   the w4a16 encoder (127 MB)
    am.mvn                          CMVN stats (two 560-dim vectors)
    embedding.npy                   SenseVoice prompt embeddings
    chn_jpn_yue_eng_ko_spectok.bpe.model   sentencepiece model
"""
from __future__ import annotations

import glob
import io
import logging
import os
import re
import threading
import time
import wave
from typing import Any, Callable, Optional

import numpy as np

from kit.errors import InferenceError
from kit.resources import ExternalNpuLease
from kit.runtime.engine import ModelSpec, TensorSpec
from kit.runtime.remote import RemoteRknnSession, configured_inference_socket
from voxedge.backends.base import (
    ASRBackend,
    ASRCapability,
    TranscriptionResult,
)

logger = logging.getLogger(__name__)

# Decode constants -- identical to the verified device scripts.
# Dual-tier windows: a VAD segment whose LFR frame count (incl. 4 prompt frames)
# fits in T_SHORT is routed to the small T=100 encoder (~350ms); anything longer
# uses the production T=344 encoder (~1000ms). Both are w4a16 and single-input.
# Their fixed graphs treat all physical T frames as valid: zero padding is seen
# by encoder attention, while ``valid`` only limits the CTC rows decoded below.
# A short tier therefore reduces (but does not semantically mask) padding.
T_SHORT = 100
T_LONG = 344
T_FIXED = T_LONG  # back-compat alias (>T_LONG segments are still truncated to T_LONG)
LFR_DIM = 560
BLANK_ID = 0
_LANG_IDS = {"auto": 0, "zh": 3, "en": 4, "yue": 7, "ja": 11, "ko": 12}
_TEXTNORM_IDS = {"withitn": 14, "woitn": 15}

# Default on-device artifact filenames (co-located with the rknn model).
DEFAULT_RKNN_NAME = "sensevoice_rv1126b_w4a16.rknn"          # T=344 (production)
DEFAULT_RKNN_SHORT_NAME = "sensevoice_rv1126b_w4a16_t100.rknn"  # T=100 (short tier)
DEFAULT_CMVN_NAME = "am.mvn"
DEFAULT_EMB_NAME = "embedding.npy"
DEFAULT_BPE_NAME = "chn_jpn_yue_eng_ko_spectok.bpe.model"

# Mirrors RknnSession's fail-closed release quarantine.  This is not another
# ownership mechanism: it only keeps the existing ExternalNpuLease and any
# uncertain native contexts strongly reachable when destruction fails.  That
# prevents their finalizers from releasing the one broker generation while a
# caller catches an initialization error and keeps the process alive.
_RELEASE_QUARANTINE: dict[object, tuple[tuple[Any, ...], Any, str]] = {}


class _RemoteServiceGuard:
    """Lease-shaped adapter for a model already fenced by inferenced.

    The actual connection-lifetime NPU lease belongs to the platform daemon.
    Voice keeps its model RPC connections in ``_owned_runtimes``; this guard
    only lets the existing exact-once lifecycle stay shared between local and
    remote backends without acquiring a second, conflicting broker lease.
    """

    def acquire(self, timeout=None):
        return self

    def ready(self) -> None:
        return None

    def alive(self) -> bool:
        return True

    def release(self) -> None:
        return None


def _new_voice_npu_lease():
    """Build the one process-shared broker lease used by the RK ASR backend.

    ``ExternalNpuLease`` is broker-first and already enforces
    ``RECAMERA_NPU_BROKER_REQUIRED=1``: a legacy ``RECAMERA_NPU_LOCK`` override
    is rejected rather than silently selecting flock.  Its process-global
    reference counting also lets two backend objects share one native broker
    connection without inventing a Voice-specific lock or lease protocol.
    """

    return ExternalNpuLease(
        app_id=os.environ.get("RECAMERA_APP_ID") or "voice-transcribe")


# ── frontend (fbank + LFR + CMVN + prompt) ───────────────────────────────────
def _compute_feats(audio: np.ndarray) -> np.ndarray:
    import kaldi_native_fbank as knf
    o = knf.FbankOptions()
    o.frame_opts.samp_freq = 16000
    o.frame_opts.dither = 0.0
    o.frame_opts.window_type = "hamming"
    o.frame_opts.snip_edges = True
    o.mel_opts.num_bins = 80
    fb = knf.OnlineFbank(o)
    fb.accept_waveform(16000, (audio * 32768).tolist())
    fb.input_finished()
    return np.stack([fb.get_frame(i) for i in range(fb.num_frames_ready)])


def _apply_lfr(feats: np.ndarray, m: int = 7, n: int = 6) -> np.ndarray:
    T = feats.shape[0]
    pad = (m - 1) // 2
    feats = np.vstack([np.tile(feats[0], (pad, 1)), feats])
    T2 = feats.shape[0]
    out = []
    i = 0
    while i * n < T:
        idx0 = i * n
        if idx0 + m <= T2:
            out.append(feats[idx0:idx0 + m].reshape(-1))
        else:
            chunk = feats[idx0:T2]
            need = m - chunk.shape[0]
            chunk = np.vstack([chunk, np.tile(feats[-1], (need, 1))])
            out.append(chunk.reshape(-1))
        i += 1
    return np.stack(out).astype(np.float32)


def _load_cmvn(path: str) -> tuple[np.ndarray, np.ndarray]:
    with open(path, encoding="utf-8") as fh:
        txt = fh.read()
    vals = [np.array(b.split(), dtype=np.float32)
            for b in re.findall(r"\[([^\]]*)\]", txt)]
    big = [v for v in vals if v.size == LFR_DIM]
    return big[0], big[1]


class RknnSenseVoiceBackend(ASRBackend):
    """voxedge `ASRBackend` over the rv1126b w4a16 SenseVoice RKNN encoder.

    Offline backend that opts into ``supports_offline_streaming`` so it gets the
    generic voxedge offline->streaming adapter + STREAMING capability for free,
    exactly like the CPU SenseVoice path.
    """

    supports_offline_streaming = True
    supports_hot_reload = True

    def __init__(
        self,
        rknn_model: str,
        cmvn_path: str,
        embedding_path: str,
        bpe_path: str,
        *,
        rknn_model_short: Optional[str] = None,
        language: str = "auto",
        textnorm: str = "withitn",
        debug: bool = False,
        lease=None,
        lease_factory: Optional[Callable[[], Any]] = None,
        lease_timeout: Optional[float] = 30.0,
        runtime_factory: Optional[Callable[[], Any]] = None,
        sentencepiece_factory: Optional[Callable[[], Any]] = None,
    ):
        if lease is not None and lease_factory is not None:
            raise ValueError("pass lease or lease_factory, not both")
        self._rknn_model = rknn_model              # T=344 (long / production)
        self._rknn_model_short = rknn_model_short  # T=100 (short) -- optional
        self._cmvn_path = cmvn_path
        self._embedding_path = embedding_path
        self._bpe_path = bpe_path
        self._language = (language or "auto")
        self._textnorm = textnorm
        self._debug = bool(debug)
        self._lease_factory = ((lambda: lease) if lease is not None
                               else (lease_factory or _new_voice_npu_lease))
        self._lease_timeout = lease_timeout
        self._runtime_factory = runtime_factory
        self._sentencepiece_factory = sentencepiece_factory
        service = configured_inference_socket()
        # Explicit test/vendor injections retain the local backend.  A managed
        # production process has no such injection and is routed entirely
        # through the appmgr-authorized service endpoint.
        self._inference_service = (
            service
            if service and lease is None and lease_factory is None
            and runtime_factory is None
            else None
        )
        if self._inference_service:
            self._lease_factory = _RemoteServiceGuard
        # RKNNLite contexts are not safe to infer and destroy concurrently.
        # Serialize inference calls and let unload() drain the active call
        # before touching either native context or the broker lease.
        self._lifecycle = threading.Condition(threading.RLock())
        self._infer_active = False
        self._infer_owner = None
        self._closing = False
        self._closing_owner = None
        # populated in preload()
        self._lease = None
        self._lease_ready = False
        self._release_failed = False
        self._quarantine_key = object()
        # A runtime is registered here immediately after construction, before
        # load_rknn/init_runtime.  That makes a BaseException at either native
        # step rollback-able even though _load_one never returned the handle.
        self._owned_runtimes = []
        self._rknn = None        # long (T=344) handle
        self._rknn_short = None   # short (T=100) handle, or None -> single-tier
        self._sp = None
        self._cmvn_add = None
        self._cmvn_scale = None
        self._emb = None

    # -- voxedge ASRBackend interface ---------------------------------------- #
    @property
    def name(self) -> str:
        return "rk:sensevoice_w4a16"

    @property
    def capabilities(self) -> set:
        caps = set()
        if self._rknn is not None:
            caps.add(ASRCapability.OFFLINE)
            caps.add(ASRCapability.MULTI_LANGUAGE)
        return caps

    @property
    def sample_rate(self) -> int:
        return 16000

    def is_ready(self) -> bool:
        with self._lifecycle:
            return (not self._release_failed and not self._closing
                    and self._lease is not None and self._lease_ready
                    and self._rknn is not None and self._sp is not None)

    def preload(self) -> None:
        """Load assets and RKNN contexts as one broker-owned transaction.

        CPU-side assets are validated first so a missing BPE/CMVN file never
        pauses the built-in detector.  The external lease is then acquired
        *before* constructing any RKNNLite object.  READY is emitted only after
        every required initialization step succeeds (the optional short tier
        may explicitly degrade after its partial context has been released).
        """

        thread_id = threading.get_ident()
        with self._lifecycle:
            while self._closing:
                if (self._closing_owner == thread_id
                        or self._infer_owner == thread_id):
                    raise InferenceError(
                        "RK ASR preload cannot wait from an active inference "
                        "or its own unload",
                        operation="asr.preload",
                        code="reentrant_preload",
                    )
                self._lifecycle.wait()
            self._preload_locked()

    def _preload_locked(self) -> None:
        """Preload implementation executed while holding ``_lifecycle``."""

        if self.is_ready():
            return
        if self._lease is not None or self._owned_runtimes or self._release_failed:
            raise RuntimeError(
                "RknnSenseVoiceBackend has an incomplete/quarantined lifecycle; "
                "call unload() successfully before preload()")

        cmvn_add, cmvn_scale = _load_cmvn(self._cmvn_path)
        emb = np.load(self._embedding_path)
        if self._sentencepiece_factory is None:
            import sentencepiece as spm
            sp = spm.SentencePieceProcessor()
        else:
            sp = self._sentencepiece_factory()
        if sp.load(self._bpe_path) is False:
            raise RuntimeError(f"sentencepiece load failed: {self._bpe_path}")

        try:
            # Store the object before acquire: a custom lease that obtains the
            # resource and then raises still receives an idempotent release in
            # _rollback_initialization(), matching RknnSession's contract.
            self._lease = self._lease_factory()
            self._lease.acquire(timeout=self._lease_timeout)

            t0 = time.time()
            self._rknn = self._load_one(
                self._rknn_model, frames=T_LONG)  # T=344 (required)

            # T=100 is optional.  A normal Exception degrades only after every
            # partially-created short-tier handle is destroyed.  A
            # BaseException escapes to the outer transaction and tears down
            # the long context + lease as well.
            if self._rknn_model_short and os.path.exists(self._rknn_model_short):
                mark = len(self._owned_runtimes)
                try:
                    self._rknn_short = self._load_one(
                        self._rknn_model_short, frames=T_SHORT)
                except Exception:
                    self._release_owned_from(mark)
                    logger.exception(
                        "short-tier T=%d load failed; using long tier only",
                        T_SHORT)
                    self._rknn_short = None
            load_sec = time.time() - t0

            self._cmvn_add, self._cmvn_scale = cmvn_add, cmvn_scale
            self._emb = emb
            self._sp = sp

            # ExternalNpuLease itself coalesces READY across process-shared
            # references; this backend also calls it exactly once per successful
            # preload transaction.
            self._lease.ready()
            self._lease_ready = True

            logger.info("RknnSenseVoiceBackend loaded (%.2fs) long=%s short=%s",
                        load_sec, os.path.basename(self._rknn_model),
                        os.path.basename(self._rknn_model_short)
                        if self._rknn_short else "(none)")
        except BaseException:
            self._rollback_initialization()
            raise

    def _new_runtime(self):
        if self._runtime_factory is not None:
            return self._runtime_factory()
        from rknnlite.api import RKNNLite
        return RKNNLite(verbose=False)

    def _load_one(self, path: str, *, frames: int):
        """Create/load/init one context while the already-held lease fences it."""

        if self._lease is None:
            raise RuntimeError("internal error: RKNN construction without NPU lease")
        if self._inference_service:
            # SenseVoice's encoder input is already batched [N,T,F].  The
            # permissive RemoteRknnModel compatibility wrapper treats an
            # untyped rank-3 value as a legacy HWC image, adds another batch
            # dimension and casts float features to uint8.  Declare the exact
            # sequence contract so validation preserves 3-D float32 bytes on
            # the wire for both fixed-shape encoder tiers.
            spec = ModelSpec(
                path=path,
                name="sensevoice-encoder",
                inputs=(TensorSpec(
                    "speech",
                    (1, int(frames), LFR_DIM),
                    "float32",
                    "NTF",
                ),),
            )
            memory_mb = 128 if frames == T_SHORT else 256
            runtime = RemoteRknnSession(
                spec,
                socket_path=self._inference_service,
                memory_mb=memory_mb,
                priority=60,
            )
            self._owned_runtimes.append(runtime)
            return runtime
        r = self._new_runtime()
        self._owned_runtimes.append(r)
        if r.load_rknn(path) != 0:
            raise RuntimeError(f"load_rknn failed: {path}")
        # rv1126b is SINGLE-CORE: init_runtime() takes NO core_mask (the
        # NPU_CORE_0 mask used on rk3576/3588 errors here).
        if r.init_runtime() != 0:
            raise RuntimeError(
                f"init_runtime failed (rv1126b: no core_mask): {path}")
        return r

    @staticmethod
    def _release_runtime(runtime) -> None:
        result = runtime.release()
        if result not in (None, 0):
            raise RuntimeError(
                f"RKNNLite.release returned non-zero status {result!r}")

    def _release_owned_from(self, start: int) -> None:
        """Release owned contexts in reverse order, retaining failed handles.

        If any native destroy fails the caller must retain the lease.  Successful
        handles are removed, failed/uncertain ones stay in ``_owned_runtimes`` so
        a later explicit unload can retry without falsely unlocking the NPU.
        """

        first_error = None
        targets = list(self._owned_runtimes[start:])
        for runtime in reversed(targets):
            try:
                self._release_runtime(runtime)
            except BaseException as exc:
                if first_error is None:
                    first_error = (exc, exc.__traceback__)
                logger.critical(
                    "RKNN runtime release failed; retaining NPU lease",
                    exc_info=True)
                continue
            try:
                self._owned_runtimes.remove(runtime)
            except ValueError:
                pass
            if self._rknn is runtime:
                self._rknn = None
            if self._rknn_short is runtime:
                self._rknn_short = None
        if first_error is not None:
            self._quarantine_release()
            exc, tb = first_error
            raise exc.with_traceback(tb)

    def _quarantine_release(self) -> None:
        _RELEASE_QUARANTINE[self._quarantine_key] = (
            tuple(self._owned_runtimes), self._lease, self._rknn_model)

    def _clear_release_quarantine(self) -> None:
        _RELEASE_QUARANTINE.pop(self._quarantine_key, None)

    def _clear_cpu_assets(self) -> None:
        self._sp = None
        self._cmvn_add = None
        self._cmvn_scale = None
        self._emb = None

    def _rollback_initialization(self) -> None:
        """Best-effort rollback which never masks the initialization error."""

        try:
            self._release_owned_from(0)
        except BaseException:
            self._release_failed = True
            logger.critical(
                "RK ASR initialization rollback could not destroy every RKNN "
                "context; NPU lease remains held fail-closed",
                exc_info=True)
            return

        self._clear_cpu_assets()
        lease = self._lease
        if lease is not None:
            try:
                lease.release()
            except BaseException:
                self._release_failed = True
                self._quarantine_release()
                logger.critical(
                    "RK ASR initialization rollback could not release NPU lease",
                    exc_info=True)
                return
        self._lease = None
        self._lease_ready = False
        self._release_failed = False
        self._clear_release_quarantine()

    def unload(self) -> None:
        """Destroy every RKNN context, then release the shared lease once.

        Idempotent after success.  A native release failure is deliberately
        fail-closed: the live/uncertain context and lease are retained and the
        exception is surfaced so a later call may retry.
        """

        thread_id = threading.get_ident()
        with self._lifecycle:
            if (self._lease is None and not self._owned_runtimes
                    and not self._release_failed):
                return
            if self._infer_owner == thread_id:
                raise InferenceError(
                    "inference cannot unload its own active RK ASR context",
                    operation="asr.unload",
                    code="reentrant_release",
                )
            while self._closing:
                if self._closing_owner == thread_id:
                    raise InferenceError(
                        "RK ASR unload cannot recursively wait for itself",
                        operation="asr.unload",
                        code="reentrant_release",
                    )
                self._lifecycle.wait()
                if (self._lease is None and not self._owned_runtimes
                        and not self._release_failed):
                    return
            self._closing = True
            self._closing_owner = thread_id
            self._lifecycle.notify_all()
            try:
                while self._infer_active:
                    self._lifecycle.wait()
            except BaseException:
                self._closing = False
                self._closing_owner = None
                self._lifecycle.notify_all()
                raise

        try:
            self._release_owned_from(0)
            self._rknn = None
            self._rknn_short = None
            self._clear_cpu_assets()
            lease = self._lease
            if lease is not None:
                try:
                    lease.release()
                except BaseException:
                    self._quarantine_release()
                    logger.critical(
                        "RK ASR NPU lease release failed", exc_info=True)
                    raise
        except BaseException:
            with self._lifecycle:
                self._release_failed = True
                self._closing = False
                self._closing_owner = None
                self._lifecycle.notify_all()
            raise

        with self._lifecycle:
            self._lease = None
            self._lease_ready = False
            self._release_failed = False
            self._closing = False
            self._closing_owner = None
            self._lifecycle.notify_all()
        self._clear_release_quarantine()
        import gc
        gc.collect()

    def __del__(self) -> None:
        try:
            self.unload()
        except BaseException:
            # Explicit unload is the observable error path.  On process death
            # the broker connection itself still supplies crash-safe HUP cleanup.
            pass

    def transcribe(self, audio_bytes: bytes, language: str = "auto") -> TranscriptionResult:
        """One-shot offline transcription of WAV bytes (satisfies the ABC)."""
        audio, sr = self._wav_bytes_to_float(audio_bytes)
        if sr != 16000:
            audio = self._resample_16k(audio, sr)
        return self.transcribe_array(audio, language)

    def transcribe_array(self, samples: np.ndarray, language: str = "auto") -> TranscriptionResult:
        thread_id = threading.get_ident()
        try:
            with self._lifecycle:
                if self._infer_owner == thread_id:
                    raise InferenceError(
                        "recursive inference on one RK ASR context is unsupported",
                        operation="asr.infer",
                        code="reentrant_inference",
                    )
                while self._infer_active and not self._closing:
                    self._lifecycle.wait()
                if self._release_failed:
                    raise InferenceError(
                        "cannot infer with a quarantined RK ASR context",
                        operation="asr.infer",
                        code="session_quarantined",
                    )
                if self._closing:
                    raise InferenceError(
                        "cannot infer while the RK ASR context is closing",
                        operation="asr.infer",
                        code="session_closing",
                    )
                if self._rknn is None or self._sp is None:
                    raise RuntimeError(
                        "RknnSenseVoiceBackend not loaded; call preload() first")
                self._ensure_lease_alive()
                self._infer_active = True
                self._infer_owner = thread_id
        except BaseException as exc:
            if isinstance(exc, Exception):
                raise
            # If a signal/control-flow exception interrupts broker admission,
            # no inference is active, so teardown can proceed immediately.
            try:
                self.unload()
            except BaseException:
                logger.critical(
                    "RK ASR cleanup failed while preserving admission "
                    "control-flow exception",
                    exc_info=True)
            raise

        control_flow = False
        try:
            return self._transcribe_array_impl(samples, language)
        except BaseException as exc:
            control_flow = not isinstance(exc, Exception)
            raise
        finally:
            with self._lifecycle:
                self._infer_active = False
                self._infer_owner = None
                self._lifecycle.notify_all()
            if control_flow:
                # Leave the active region before unload() waits/drains.  This
                # preserves the original control-flow exception and never
                # destroys a native context underneath RKNNLite.inference().
                try:
                    self.unload()
                except BaseException:
                    logger.critical(
                        "RK ASR cleanup failed while preserving inference "
                        "control-flow exception",
                        exc_info=True)

    def _transcribe_array_impl(
        self, samples: np.ndarray, language: str = "auto"
    ) -> TranscriptionResult:
        """Inference body protected by the lifecycle admission barrier."""

        lang = self._resolve_lang(language)
        audio = np.ascontiguousarray(samples, dtype=np.float32)

        # Front-end once (prompt + LFR + CMVN), then route by actual frame count.
        sp_in = self._prep(audio, lang)
        n = int(sp_in.shape[0])
        if self._rknn_short is not None and n <= T_SHORT:
            rk, T, tier = self._rknn_short, T_SHORT, "short"
        else:
            rk, T, tier = self._rknn, T_LONG, "long"

        speech, valid = self._pad(sp_in, T)
        input_tensor = speech.astype(np.float32)
        if self._inference_service:
            out = rk.infer(input_tensor)
        else:
            out = rk.inference(inputs=[input_tensor])
        logits = np.asarray(out[0][0])[:valid]

        text, detected = self._ctc_decode(logits)
        logger.info("ASR route: tier=%s frames=%d T=%d valid=%d text=%r",
                    tier, n, T, valid, text[:48])
        reported_language = detected or (lang if lang != "auto" else "")
        return TranscriptionResult(text=text, language=reported_language)

    def _ensure_lease_alive(self) -> None:
        lease = self._lease
        if lease is None or not self._lease_ready:
            raise InferenceError(
                "RK ASR inference has no ready NPU lease",
                operation="asr.infer.lease",
                code="npu_lease_not_acquired",
                details={"model": self._rknn_model},
            )
        try:
            is_alive = lease.alive()
        except Exception as exc:
            raise InferenceError(
                f"could not verify RK ASR NPU lease liveness: {exc}",
                operation="asr.infer.lease",
                code="npu_lease_check_failed",
                retryable=True,
                details={"model": self._rknn_model},
            ) from exc
        if is_alive is not True:
            raise InferenceError(
                "rkipc revoked the RK ASR NPU ownership generation",
                operation="asr.infer.lease",
                code="npu_lease_revoked",
                retryable=False,
                details={"model": self._rknn_model},
            )

    # -- decode internals ---------------------------------------------------- #
    def _resolve_lang(self, requested: str) -> str:
        requested = (requested or "auto").strip().lower() or "auto"
        if requested in _LANG_IDS:
            return requested
        # Unknown/unsupported per-request language -> fall back to configured pin.
        return self._language if self._language in _LANG_IDS else "auto"

    def _prep(self, audio: np.ndarray, lang: str) -> np.ndarray:
        """Prompt frames + LFR + CMVN -> unpadded [N, LFR_DIM] feature sequence."""
        lfr = _apply_lfr(_compute_feats(audio))
        lfr = (lfr + self._cmvn_add) * self._cmvn_scale
        prefix = np.stack([
            self._emb[_LANG_IDS.get(lang, 0)],
            self._emb[1],
            self._emb[2],
            self._emb[_TEXTNORM_IDS[self._textnorm]],
        ]).astype(np.float32)
        return np.concatenate([prefix, lfr], axis=0).astype(np.float32)

    @staticmethod
    def _pad(sp_in: np.ndarray, T: int) -> tuple[np.ndarray, int]:
        """Pad/truncate the [N, LFR_DIM] sequence to exactly T frames for encoder T."""
        valid = int(sp_in.shape[0])
        if valid > T:
            sp_in = sp_in[:T]
            valid = T
        else:
            sp_in = np.vstack([sp_in, np.zeros((T - valid, LFR_DIM), dtype=np.float32)])
        return sp_in[None], valid

    def _ctc_decode(self, logits: np.ndarray) -> tuple[str, str]:
        ids = logits.argmax(-1).tolist()
        collapsed = []
        prev = -1
        for x in ids:
            if x != prev and x != BLANK_ID:
                collapsed.append(x)
            prev = x
        sp = self._sp
        pieces = [sp.id_to_piece(i) for i in collapsed if 0 <= i < sp.get_piece_size()]
        raw = "".join(pieces).replace("▁", " ")
        # Detected language rides in a <|xx|> tag -- but SenseVoice also emits
        # emotion / event / itn tags in the same <|..|> form, so match ONLY the
        # known language codes (else we'd report e.g. "withitn").
        m = re.search(r"<\|(zh|en|yue|ja|ko|nospeech)\|>", raw)
        detected = m.group(1) if m else ""
        text = re.sub(r"<\|[^|]*\|>", "", raw).strip()
        return text, detected

    # -- audio helpers ------------------------------------------------------- #
    @staticmethod
    def _wav_bytes_to_float(wav_bytes: bytes) -> tuple[np.ndarray, int]:
        with wave.open(io.BytesIO(wav_bytes)) as wf:
            sr, n, ch = wf.getframerate(), wf.getnframes(), wf.getnchannels()
            raw = wf.readframes(n)
        a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if ch > 1:
            a = a.reshape(-1, ch).mean(axis=1)
        return a, sr

    @staticmethod
    def _resample_16k(audio: np.ndarray, sr: int) -> np.ndarray:
        if sr == 16000:
            return audio
        new_len = int(len(audio) * 16000 / sr)
        return np.interp(
            np.linspace(0, len(audio) - 1, new_len),
            np.arange(len(audio)), audio,
        ).astype(np.float32)


def _resolve_assets(model: Optional[str]) -> dict:
    """Resolve the rknn model + co-located assets from a dir / model path.

    ``model`` may be: a directory, a path to the ``.rknn`` file, or any path
    whose *directory* holds the assets (e.g. the CPU ``model.int8.onnx`` the
    generic app config points at). Falls back to the standard staging dirs.
    """
    candidates = []
    if model:
        candidates.append(model if os.path.isdir(model) else os.path.dirname(model))
    candidates += ["/userdata/local/models/asr", "/userdata/tmp/asr"]

    for d in candidates:
        if not d or not os.path.isdir(d):
            continue
        rk = os.path.join(d, DEFAULT_RKNN_NAME)
        if not os.path.exists(rk):
            hits = sorted(glob.glob(os.path.join(d, "*w4a16*.rknn"))) \
                or sorted(glob.glob(os.path.join(d, "sensevoice_rv1126b*.rknn")))
            rk = hits[0] if hits else rk
        cmvn = os.path.join(d, DEFAULT_CMVN_NAME)
        emb = os.path.join(d, DEFAULT_EMB_NAME)
        bpe = os.path.join(d, DEFAULT_BPE_NAME)
        if not os.path.exists(bpe):
            hits = sorted(glob.glob(os.path.join(d, "*.bpe.model")))
            bpe = hits[0] if hits else bpe
        if os.path.exists(rk) and os.path.exists(cmvn) and os.path.exists(emb) \
                and os.path.exists(bpe):
            short = os.path.join(d, DEFAULT_RKNN_SHORT_NAME)
            return {"rknn_model": rk, "cmvn_path": cmvn,
                    "embedding_path": emb, "bpe_path": bpe,
                    "rknn_model_short": short if os.path.exists(short) else None}

    # Nothing complete found: return best-guess paths from the first candidate so
    # the error names the intended location.
    d = next((c for c in candidates if c), "/userdata/local/models/asr")
    short = os.path.join(d, DEFAULT_RKNN_SHORT_NAME)
    return {"rknn_model": os.path.join(d, DEFAULT_RKNN_NAME),
            "cmvn_path": os.path.join(d, DEFAULT_CMVN_NAME),
            "embedding_path": os.path.join(d, DEFAULT_EMB_NAME),
            "bpe_path": os.path.join(d, DEFAULT_BPE_NAME),
            "rknn_model_short": short if os.path.exists(short) else None}


def build_rknn_backend(model: Optional[str] = None, tokens: Optional[str] = None,
                       *, language: str = "auto", use_itn: bool = True,
                       debug: bool = False,
                       **_kw) -> RknnSenseVoiceBackend:
    """Construct + preload the NPU backend. Called by ``kit.asr.Asr(backend='rk')``.

    ``tokens`` (the CPU sherpa tokens path) is ignored -- the NPU decode uses
    the sentencepiece bpe model resolved alongside the rknn model.
    """
    assets = _resolve_assets(model)
    textnorm = "withitn" if bool(use_itn) else "woitn"
    b = RknnSenseVoiceBackend(
        language=language,
        textnorm=textnorm,
        debug=debug,
        **assets,
    )
    b.preload()
    return b

from __future__ import annotations

import importlib.util
import sys
import threading
import time
import types
from enum import Enum
from pathlib import Path

import numpy as np
import pytest

import kit.resources as resources
from kit.asr import Asr
from kit.errors import InferenceError, InputValidationError
from kit.resources import ExternalNpuLease


def _load_backend_module():
    """Load the backend with a tiny voxedge ABC shim for host-only tests.

    voxedge lives in the device voice venv rather than this repository's host
    development environment.  The lifecycle under test does not depend on its
    implementation, so bind only the three interface symbols imported by the
    backend and restore sys.modules immediately after loading it.
    """

    class ASRBackend:
        pass

    class ASRCapability(Enum):
        OFFLINE = "offline"
        MULTI_LANGUAGE = "multi_language"

    class TranscriptionResult:
        def __init__(self, *, text, language=""):
            self.text = text
            self.language = language

    modules = {
        "voxedge": types.ModuleType("voxedge"),
        "voxedge.backends": types.ModuleType("voxedge.backends"),
        "voxedge.backends.base": types.ModuleType("voxedge.backends.base"),
    }
    modules["voxedge"].__path__ = []
    modules["voxedge.backends"].__path__ = []
    base = modules["voxedge.backends.base"]
    base.ASRBackend = ASRBackend
    base.ASRCapability = ASRCapability
    base.TranscriptionResult = TranscriptionResult

    missing = object()
    saved = {name: sys.modules.get(name, missing) for name in modules}
    sys.modules.update(modules)
    try:
        path = Path(__file__).resolve().parents[1] / "asr_rknn_backend.py"
        name = "kit._asr_rknn_backend_lifecycle_test_impl"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module
    finally:
        for name, previous in saved.items():
            if previous is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


backend_module = _load_backend_module()
RknnSenseVoiceBackend = backend_module.RknnSenseVoiceBackend


def test_asr_forwards_use_itn_to_rk_builder(monkeypatch):
    calls = []
    fake_module = types.ModuleType("kit.asr_rknn_backend")
    fake_module.build_rknn_backend = lambda **kwargs: calls.append(kwargs) or object()
    monkeypatch.setitem(sys.modules, "kit.asr_rknn_backend", fake_module)

    Asr(backend="rk", use_itn=False, language="en")

    assert len(calls) == 1
    assert calls[0]["use_itn"] is False
    assert calls[0]["language"] == "en"


@pytest.mark.parametrize(
    ("use_itn", "expected"),
    [(True, "withitn"), (False, "woitn")],
)
def test_rk_builder_maps_use_itn_to_prompt_embedding(
    monkeypatch, use_itn, expected,
):
    calls = []

    class FakeBackend:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def preload(self):
            calls.append("preload")

    monkeypatch.setattr(backend_module, "_resolve_assets", lambda _model: {})
    monkeypatch.setattr(backend_module, "RknnSenseVoiceBackend", FakeBackend)

    backend_module.build_rknn_backend(use_itn=use_itn)

    assert calls == [{
        "language": "auto",
        "textnorm": expected,
        "debug": False,
    }, "preload"]


class FakeSentencePiece:
    def __init__(self):
        self.loaded = []

    def load(self, path):
        self.loaded.append(path)
        return True

    def id_to_piece(self, token_id):
        return f"token-{token_id}"

    def get_piece_size(self):
        return 32


class FakeLease:
    def __init__(self, events=None, *, acquire_error=None, ready_error=None,
                 alive_result=True, alive_error=None, release_error=None):
        self.events = events if events is not None else []
        self.acquire_error = acquire_error
        self.ready_error = ready_error
        self.alive_result = alive_result
        self.alive_error = alive_error
        self.release_error = release_error
        self.acquire_calls = []
        self.ready_calls = 0
        self.alive_calls = 0
        self.release_calls = 0

    def acquire(self, timeout=None):
        self.events.append("lease.acquire")
        self.acquire_calls.append(timeout)
        if self.acquire_error is not None:
            raise self.acquire_error
        return self

    def ready(self):
        self.events.append("lease.ready")
        self.ready_calls += 1
        if self.ready_error is not None:
            raise self.ready_error

    def alive(self):
        self.events.append("lease.alive")
        self.alive_calls += 1
        if self.alive_error is not None:
            raise self.alive_error
        return self.alive_result

    def release(self):
        self.events.append("lease.release")
        self.release_calls += 1
        if self.release_error is not None:
            raise self.release_error


class FakeRuntime:
    def __init__(self, events=None, *, load_error=None, init_error=None,
                 load_rc=0, init_rc=0, infer_error=None,
                 release_error=None, release_rc=None):
        self.events = events if events is not None else []
        self.load_error = load_error
        self.init_error = init_error
        self.load_rc = load_rc
        self.init_rc = init_rc
        self.infer_error = infer_error
        self.release_error = release_error
        self.release_rc = release_rc
        self.loaded = []
        self.init_calls = 0
        self.inference_calls = 0
        self.release_calls = 0

    def load_rknn(self, path):
        self.events.append("runtime.load")
        self.loaded.append(path)
        if self.load_error is not None:
            raise self.load_error
        return self.load_rc

    def init_runtime(self):
        self.events.append("runtime.init")
        self.init_calls += 1
        if self.init_error is not None:
            raise self.init_error
        return self.init_rc

    def inference(self, inputs):
        self.events.append("runtime.infer")
        self.inference_calls += 1
        if self.infer_error is not None:
            raise self.infer_error
        return [np.zeros((1, backend_module.T_LONG, 2), dtype=np.float32)]

    def release(self):
        self.events.append("runtime.release")
        self.release_calls += 1
        if self.release_error is not None:
            raise self.release_error
        return self.release_rc


class FakeNativeBroker:
    def __init__(self):
        self.ready_calls = 0
        self.alive_calls = 0
        self.release_calls = 0

    def ready(self):
        self.ready_calls += 1

    def alive(self):
        self.alive_calls += 1
        return True

    def release(self):
        self.release_calls += 1


class FakeNativeBrokerFactory:
    def __init__(self):
        self.broker = FakeNativeBroker()
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.broker


@pytest.fixture(autouse=True)
def _clean_process_broker_state(monkeypatch):
    monkeypatch.delenv("RECAMERA_NPU_LOCK", raising=False)
    monkeypatch.delenv("RECAMERA_NPU_MANAGED", raising=False)
    monkeypatch.delenv(resources.NPU_BROKER_REQUIRED_ENV, raising=False)
    monkeypatch.delenv("RECAMERA_INFERENCE_SERVICE_SOCK", raising=False)
    monkeypatch.delenv("RECAMERA_INFERENCE_SERVICE", raising=False)
    assert resources._held_broker is None
    yield
    held = resources._held_broker
    resources._held_broker = None
    if held is not None:
        held.lease.release()
    try:
        assert backend_module._RELEASE_QUARANTINE == {}
    finally:
        backend_module._RELEASE_QUARANTINE.clear()


@pytest.fixture
def assets(tmp_path):
    long_model = tmp_path / "sensevoice_rv1126b_w4a16.rknn"
    long_model.touch()
    cmvn = tmp_path / "am.mvn"
    zeros = " ".join(["0"] * backend_module.LFR_DIM)
    ones = " ".join(["1"] * backend_module.LFR_DIM)
    cmvn.write_text(f"[ {zeros} ]\n[ {ones} ]\n", encoding="utf-8")
    embedding = tmp_path / "embedding.npy"
    np.save(embedding, np.zeros((16, backend_module.LFR_DIM), dtype=np.float32))
    bpe = tmp_path / "tokens.bpe.model"
    bpe.touch()
    return {
        "rknn_model": str(long_model),
        "cmvn_path": str(cmvn),
        "embedding_path": str(embedding),
        "bpe_path": str(bpe),
    }


def make_backend(assets, *, lease=None, lease_factory=None,
                 runtime_factory=None, short_model=None):
    return RknnSenseVoiceBackend(
        **assets,
        rknn_model_short=short_model,
        lease=lease,
        lease_factory=lease_factory,
        runtime_factory=runtime_factory,
        sentencepiece_factory=FakeSentencePiece,
    )


def test_success_acquires_before_rknn_ready_once_and_unloads_once(assets):
    events = []
    lease = FakeLease(events)
    runtime = FakeRuntime(events)

    def new_runtime():
        events.append("runtime.new")
        return runtime

    backend = make_backend(
        assets, lease=lease, runtime_factory=new_runtime)
    backend.preload()
    backend.preload()

    assert events[:5] == [
        "lease.acquire", "runtime.new", "runtime.load", "runtime.init",
        "lease.ready",
    ]
    assert lease.acquire_calls == [30.0]
    assert lease.ready_calls == 1
    assert backend.is_ready()

    backend.unload()
    backend.unload()
    assert runtime.release_calls == 1
    assert lease.release_calls == 1
    assert events[-2:] == ["runtime.release", "lease.release"]
    assert not backend.is_ready()


def test_managed_voice_uses_remote_service_without_second_broker_lease(
    assets, monkeypatch, tmp_path
):
    events = []

    class FakeRemote:
        def __init__(self, spec, **kwargs):
            input_spec = spec.inputs[0]
            events.append((
                "remote.load",
                spec.path,
                {
                    "shape": input_spec.shape,
                    "dtype": input_spec.dtype,
                    "layout": input_spec.layout,
                    **kwargs,
                },
            ))
            self.frames = input_spec.shape[1]
            self.released = False

        def infer(self, value):
            events.append(("remote.infer", tuple(value.shape), str(value.dtype)))
            assert value.shape == (1, self.frames, backend_module.LFR_DIM)
            assert value.dtype == np.float32
            return [np.zeros((1, self.frames, 2), dtype=np.float32)]

        def release(self):
            assert not self.released
            self.released = True
            events.append(("remote.release",))

    monkeypatch.setenv(
        "RECAMERA_INFERENCE_SERVICE_SOCK", "/run/recamera/inferenced.sock"
    )
    monkeypatch.setattr(backend_module, "RemoteRknnSession", FakeRemote)
    short_model = tmp_path / "sensevoice_rv1126b_w4a16_t100.rknn"
    short_model.touch()
    backend = make_backend(assets, short_model=str(short_model))
    backend.preload()

    assert backend.is_ready()
    assert resources._held_broker is None
    loads = [event for event in events if event[0] == "remote.load"]
    assert [event[2]["shape"] for event in loads] == [
        (1, backend_module.T_LONG, backend_module.LFR_DIM),
        (1, backend_module.T_SHORT, backend_module.LFR_DIM),
    ]
    assert all(event[2]["dtype"] == "float32" for event in loads)
    assert all(event[2]["layout"] == "NTF" for event in loads)
    assert all(
        event[2]["socket_path"] == "/run/recamera/inferenced.sock"
        for event in loads
    )
    backend._prep = lambda _audio, _lang: np.zeros(
        (5, backend_module.LFR_DIM), dtype=np.float32
    )
    result = backend.transcribe_array(np.zeros(160, dtype=np.float32))
    assert result.text == ""
    assert events[-1] == (
        "remote.infer",
        (1, backend_module.T_SHORT, backend_module.LFR_DIM),
        "float32",
    )

    backend._prep = lambda _audio, _lang: np.zeros(
        (backend_module.T_SHORT + 1, backend_module.LFR_DIM), dtype=np.float32
    )
    result = backend.transcribe_array(np.zeros(160, dtype=np.float32))
    assert result.text == ""
    assert events[-1] == (
        "remote.infer",
        (1, backend_module.T_LONG, backend_module.LFR_DIM),
        "float32",
    )

    backend.unload()
    assert [item[0] for item in events].count("remote.release") == 2
    assert not backend.is_ready()


def test_optional_short_context_is_initialized_before_single_ready(assets, tmp_path):
    events = []
    lease = FakeLease(events)
    long_runtime = FakeRuntime(events)
    short_runtime = FakeRuntime(events)
    runtimes = iter((long_runtime, short_runtime))
    short_model = tmp_path / "sensevoice_rv1126b_w4a16_t100.rknn"
    short_model.touch()

    def new_runtime():
        events.append("runtime.new")
        return next(runtimes)

    backend = make_backend(
        assets,
        lease=lease,
        runtime_factory=new_runtime,
        short_model=str(short_model),
    )
    backend.preload()

    assert events == [
        "lease.acquire",
        "runtime.new", "runtime.load", "runtime.init",
        "runtime.new", "runtime.load", "runtime.init",
        "lease.ready",
    ]
    assert lease.ready_calls == 1

    backend.unload()
    assert long_runtime.release_calls == 1
    assert short_runtime.release_calls == 1
    assert events[-3:] == [
        "runtime.release", "runtime.release", "lease.release"]


@pytest.mark.parametrize("failure_stage", [
    "acquire", "runtime_factory", "load", "init", "ready",
])
def test_every_baseexception_init_stage_rolls_back_without_masking_original(
    assets, failure_stage,
):
    class FatalInit(BaseException):
        pass

    fatal = FatalInit(f"fatal at {failure_stage}")
    events = []
    lease = FakeLease(
        events,
        acquire_error=fatal if failure_stage == "acquire" else None,
        ready_error=fatal if failure_stage == "ready" else None,
    )
    runtime = FakeRuntime(
        events,
        load_error=fatal if failure_stage == "load" else None,
        init_error=fatal if failure_stage == "init" else None,
    )

    def new_runtime():
        events.append("runtime.new")
        if failure_stage == "runtime_factory":
            raise fatal
        return runtime

    backend = make_backend(
        assets, lease=lease, runtime_factory=new_runtime)
    with pytest.raises(FatalInit) as caught:
        backend.preload()

    assert caught.value is fatal
    assert lease.release_calls == 1
    expected_runtime_release = failure_stage in {"load", "init", "ready"}
    assert runtime.release_calls == int(expected_runtime_release)
    if expected_runtime_release:
        assert events.index("runtime.release") < events.index("lease.release")
    assert backend._owned_runtimes == []
    assert backend._lease is None
    assert not backend.is_ready()


def test_release_failure_retains_lease_fail_closed_and_can_retry(assets):
    lease = FakeLease()
    release_error = RuntimeError("injected native destroy failure")
    runtime = FakeRuntime(release_error=release_error)
    backend = make_backend(
        assets, lease=lease, runtime_factory=lambda: runtime)
    backend.preload()

    with pytest.raises(RuntimeError, match="native destroy"):
        backend.unload()
    assert backend._lease is lease
    assert lease.release_calls == 0
    assert backend._release_failed is True

    backend._prep = lambda *_args: pytest.fail(
        "quarantined inference reached feature preparation")
    with pytest.raises(InferenceError) as caught:
        backend.transcribe_array(np.zeros(160, dtype=np.float32))
    assert caught.value.code == "session_quarantined"
    assert runtime.inference_calls == 0

    runtime.release_error = None
    backend.unload()
    assert runtime.release_calls == 2
    assert lease.release_calls == 1
    assert backend._lease is None


def test_unload_drains_active_inference_before_destroying_runtime(assets):
    inference_started = threading.Event()
    inference_continue = threading.Event()
    inference_finished = threading.Event()
    events = []

    class BlockingRuntime(FakeRuntime):
        def inference(self, inputs):
            self.events.append("runtime.infer.start")
            self.inference_calls += 1
            inference_started.set()
            if not inference_continue.wait(2.0):
                raise RuntimeError("test did not release blocked inference")
            self.events.append("runtime.infer.end")
            inference_finished.set()
            return [np.zeros(
                (1, backend_module.T_LONG, 2), dtype=np.float32)]

    lease = FakeLease(events)
    runtime = BlockingRuntime(events)
    backend = make_backend(
        assets, lease=lease, runtime_factory=lambda: runtime)
    backend.preload()
    backend._prep = lambda _audio, _lang: np.zeros(
        (5, backend_module.LFR_DIM), dtype=np.float32)

    errors = []
    infer_thread = threading.Thread(
        target=lambda: _capture_thread_error(
            errors, backend.transcribe_array,
            np.zeros(160, dtype=np.float32)))
    unload_thread = threading.Thread(
        target=lambda: _capture_thread_error(errors, backend.unload))
    infer_thread.start()
    assert inference_started.wait(1.0)
    unload_thread.start()

    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        with backend._lifecycle:
            if backend._closing:
                break
        time.sleep(0.005)
    else:
        pytest.fail("unload did not enter the closing/drain state")

    assert runtime.release_calls == 0
    assert lease.release_calls == 0
    assert not inference_finished.is_set()

    inference_continue.set()
    infer_thread.join(2.0)
    unload_thread.join(2.0)
    assert not infer_thread.is_alive()
    assert not unload_thread.is_alive()
    assert errors == []
    assert events.index("runtime.infer.end") < events.index("runtime.release")
    assert events.index("runtime.release") < events.index("lease.release")


def test_inference_owner_cannot_wait_in_preload_for_concurrent_unload(assets):
    backend = make_backend(
        assets, lease=FakeLease(), runtime_factory=FakeRuntime)
    backend.preload()
    thread_id = threading.get_ident()
    with backend._lifecycle:
        backend._infer_active = True
        backend._infer_owner = thread_id
        backend._closing = True
        backend._closing_owner = thread_id + 1
    try:
        with pytest.raises(InferenceError) as caught:
            backend.preload()
        assert caught.value.code == "reentrant_preload"
    finally:
        with backend._lifecycle:
            backend._infer_active = False
            backend._infer_owner = None
            backend._closing = False
            backend._closing_owner = None
            backend._lifecycle.notify_all()
        backend.unload()


def _capture_thread_error(errors, function, *args):
    try:
        function(*args)
    except BaseException as exc:
        errors.append(exc)


def test_lease_release_failure_retains_fence_and_can_retry(assets):
    release_error = RuntimeError("injected broker close failure")
    lease = FakeLease(release_error=release_error)
    runtime = FakeRuntime()
    backend = make_backend(
        assets, lease=lease, runtime_factory=lambda: runtime)
    backend.preload()

    with pytest.raises(RuntimeError, match="broker close"):
        backend.unload()
    assert runtime.release_calls == 1
    assert lease.release_calls == 1
    assert backend._lease is lease
    assert backend._release_failed is True

    lease.release_error = None
    backend.unload()
    assert runtime.release_calls == 1
    assert lease.release_calls == 2
    assert backend._lease is None


def test_init_rollback_destroy_failure_quarantines_and_can_retry(assets):
    class FatalInit(BaseException):
        pass

    fatal = FatalInit("init interrupted")
    release_error = RuntimeError("destroy failed")
    lease = FakeLease()
    runtime = FakeRuntime(init_error=fatal, release_error=release_error)
    backend = make_backend(
        assets, lease=lease, runtime_factory=lambda: runtime)

    with pytest.raises(FatalInit) as caught:
        backend.preload()
    assert caught.value is fatal
    assert backend._quarantine_key in backend_module._RELEASE_QUARANTINE
    assert backend._lease is lease
    assert lease.release_calls == 0

    runtime.release_error = None
    backend.unload()
    assert runtime.release_calls == 2
    assert lease.release_calls == 1
    assert backend._quarantine_key not in backend_module._RELEASE_QUARANTINE


def test_ready_rollback_lease_failure_quarantines_and_can_retry(assets):
    class FatalReady(BaseException):
        pass

    fatal = FatalReady("ready interrupted")
    release_error = RuntimeError("broker close failed")
    lease = FakeLease(ready_error=fatal, release_error=release_error)
    runtime = FakeRuntime()
    backend = make_backend(
        assets, lease=lease, runtime_factory=lambda: runtime)

    with pytest.raises(FatalReady) as caught:
        backend.preload()
    assert caught.value is fatal
    assert runtime.release_calls == 1
    assert backend._quarantine_key in backend_module._RELEASE_QUARANTINE
    assert backend._lease is lease

    lease.release_error = None
    backend.unload()
    assert runtime.release_calls == 1
    assert lease.release_calls == 2
    assert backend._quarantine_key not in backend_module._RELEASE_QUARANTINE


def test_every_inference_checks_alive_and_revocation_fences_driver(assets):
    lease = FakeLease()
    runtime = FakeRuntime()
    backend = make_backend(
        assets, lease=lease, runtime_factory=lambda: runtime)
    backend.preload()
    backend._prep = lambda _audio, _lang: np.zeros(
        (5, backend_module.LFR_DIM), dtype=np.float32)

    backend.transcribe_array(np.zeros(160, dtype=np.float32))
    backend.transcribe_array(np.zeros(160, dtype=np.float32))
    assert lease.alive_calls == 2
    assert runtime.inference_calls == 2

    lease.alive_result = False
    with pytest.raises(InferenceError) as caught:
        backend.transcribe_array(np.zeros(160, dtype=np.float32))
    assert caught.value.code == "npu_lease_revoked"
    assert runtime.inference_calls == 2
    backend.unload()


def test_liveness_transport_loss_is_typed_and_fences_driver(assets):
    backend_error = RuntimeError("broker HUP")
    lease = FakeLease(alive_error=backend_error)
    runtime = FakeRuntime()
    backend = make_backend(
        assets, lease=lease, runtime_factory=lambda: runtime)
    backend.preload()
    backend._prep = lambda _audio, _lang: np.zeros(
        (5, backend_module.LFR_DIM), dtype=np.float32)

    with pytest.raises(InferenceError) as caught:
        backend.transcribe_array(np.zeros(160, dtype=np.float32))
    assert caught.value.code == "npu_lease_check_failed"
    assert caught.value.__cause__ is backend_error
    assert runtime.inference_calls == 0
    backend.unload()


@pytest.mark.parametrize("failure_stage", ["alive", "driver"])
def test_control_flow_exception_during_inference_releases_context_and_lease(
    assets, failure_stage,
):
    fatal = KeyboardInterrupt()
    lease = FakeLease(alive_error=fatal if failure_stage == "alive" else None)
    runtime = FakeRuntime(
        infer_error=fatal if failure_stage == "driver" else None)
    backend = make_backend(
        assets, lease=lease, runtime_factory=lambda: runtime)
    backend.preload()
    backend._prep = lambda _audio, _lang: np.zeros(
        (5, backend_module.LFR_DIM), dtype=np.float32)

    with pytest.raises(KeyboardInterrupt) as caught:
        backend.transcribe_array(np.zeros(160, dtype=np.float32))
    assert caught.value is fatal
    assert runtime.inference_calls == int(failure_stage == "driver")
    assert runtime.release_calls == 1
    assert lease.release_calls == 1
    assert backend._lease is None


def test_two_backends_share_one_native_broker_generation(assets):
    factory = FakeNativeBrokerFactory()

    def new_lease():
        return ExternalNpuLease(
            app_id="voice-transcribe",
            instance_id="same-process",
            broker_factory=factory,
        )

    first_runtime = FakeRuntime()
    second_runtime = FakeRuntime()
    first = make_backend(
        assets, lease_factory=new_lease,
        runtime_factory=lambda: first_runtime)
    second = make_backend(
        assets, lease_factory=new_lease,
        runtime_factory=lambda: second_runtime)

    first.preload()
    second.preload()
    assert len(factory.calls) == 1
    assert factory.broker.ready_calls == 1
    assert resources._held_broker is not None
    assert resources._held_broker.references == 2

    first.unload()
    assert factory.broker.release_calls == 0
    assert resources._held_broker.references == 1
    second.unload()
    assert factory.broker.release_calls == 1
    assert resources._held_broker is None


def test_broker_required_rejects_lock_override_before_rknn_construction(
    assets, tmp_path, monkeypatch,
):
    lock_path = tmp_path / "must-not-be-created.lock"
    monkeypatch.setenv(resources.NPU_BROKER_REQUIRED_ENV, "1")
    monkeypatch.setenv("RECAMERA_NPU_LOCK", str(lock_path))
    runtime_factory_calls = []

    def new_runtime():
        runtime_factory_calls.append(True)
        return FakeRuntime()

    backend = make_backend(assets, runtime_factory=new_runtime)
    with pytest.raises(InputValidationError) as caught:
        backend.preload()

    assert caught.value.code == "npu_broker_required"
    assert runtime_factory_calls == []
    assert not lock_path.exists()


def test_asr_wrapper_closes_backend_exactly_once():
    class ReadyBackend:
        def __init__(self):
            self.unload_calls = 0

        def is_ready(self):
            return True

        def unload(self):
            self.unload_calls += 1

    backend = ReadyBackend()
    asr = Asr(rknn_backend=backend)
    asr.close()
    asr.close()
    assert backend.unload_calls == 1
    with pytest.raises(RuntimeError, match="closed"):
        asr.transcribe(np.zeros(16, dtype=np.float32))

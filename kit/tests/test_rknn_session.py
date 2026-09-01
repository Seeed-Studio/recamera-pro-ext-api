from __future__ import annotations

import signal
import threading

import numpy as np
import pytest

import kit
from kit.app import _GracefulStop
from kit.errors import (
    ConfigurationError,
    InferenceError,
    InputValidationError,
    ModelLoadError,
)
from kit.runtime.engine import ModelSpec, RknnModel, RknnSession, TensorSpec


class FakeLease:
    def __init__(self, *, ready_error=None, alive_result=True, alive_error=None):
        self.acquire_calls = []
        self.release_calls = 0
        self.ready_calls = 0
        self.alive_calls = 0
        self.ready_error = ready_error
        self.alive_result = alive_result
        self.alive_error = alive_error

    def acquire(self, timeout=None):
        self.acquire_calls.append(timeout)
        return self

    def release(self):
        self.release_calls += 1

    def ready(self):
        self.ready_calls += 1
        if self.ready_error is not None:
            raise self.ready_error

    def alive(self):
        self.alive_calls += 1
        if self.alive_error is not None:
            raise self.alive_error
        return self.alive_result


class FakeRuntime:
    def __init__(self, load_rc=0, init_rc=0, output=None, infer_error=None,
                 release_error=None, release_rc=None):
        self.load_rc = load_rc
        self.init_rc = init_rc
        self.output = output if output is not None else [np.array([1])]
        self.infer_error = infer_error
        self.release_error = release_error
        self.release_rc = release_rc
        self.loaded = []
        self.initialized = []
        self.inputs = []
        self.releases = 0

    def load_rknn(self, path):
        self.loaded.append(path)
        return self.load_rc

    def init_runtime(self, **kwargs):
        self.initialized.append(kwargs)
        return self.init_rc

    def inference(self, inputs):
        self.inputs.append(inputs)
        if self.infer_error:
            raise self.infer_error
        return self.output

    def release(self):
        self.releases += 1
        if self.release_error is not None:
            raise self.release_error
        return self.release_rc


def test_session_acquires_before_runtime_and_releases_exactly_once():
    assert kit.RknnSession is RknnSession
    events = []

    class OrderedLease(FakeLease):
        def acquire(self, timeout=None):
            events.append("lease")
            return super().acquire(timeout)

        def ready(self):
            events.append("ready")
            return super().ready()

    lease = OrderedLease()
    runtime = FakeRuntime()

    def factory():
        events.append("runtime")
        return runtime

    session = RknnSession("model.rknn", lease=lease, runtime_factory=factory)
    assert events == ["lease", "runtime", "ready"]
    assert lease.ready_calls == 1
    session.release()
    session.release()
    assert runtime.releases == 1
    assert lease.release_calls == 1


@pytest.mark.parametrize("session_type", [RknnSession, RknnModel])
def test_managed_scheduled_process_cannot_construct_local_runtime(
    session_type, monkeypatch,
):
    monkeypatch.setenv("RECAMERA_NPU_MODE", "scheduled")
    monkeypatch.setenv(
        "RECAMERA_INFERENCE_SERVICE_SOCK", "/run/recamera/inferenced.sock"
    )
    lease = FakeLease()
    runtime_calls = []

    with pytest.raises(ConfigurationError) as caught:
        session_type(
            "must-be-remote.rknn",
            lease=lease,
            runtime_factory=lambda: runtime_calls.append(True) or FakeRuntime(),
        )

    assert caught.value.code == "scheduled_npu_requires_remote"
    assert lease.acquire_calls == []
    assert runtime_calls == []


def test_explicit_local_test_injection_remains_available_outside_managed_mode(
    monkeypatch,
):
    monkeypatch.delenv("RECAMERA_NPU_MODE", raising=False)
    lease = FakeLease()
    runtime = FakeRuntime()

    session = RknnSession(
        "host-test.rknn", lease=lease, runtime_factory=lambda: runtime
    )

    assert runtime.loaded == ["host-test.rknn"]
    session.release()


def test_ready_is_sent_only_after_load_and_runtime_initialization_succeed():
    events = []

    class OrderedLease(FakeLease):
        def acquire(self, timeout=None):
            events.append("acquire")
            return super().acquire(timeout)

        def ready(self):
            events.append("ready")
            return super().ready()

        def release(self):
            events.append("lease-release")
            return super().release()

    class OrderedRuntime(FakeRuntime):
        def load_rknn(self, path):
            events.append("load")
            return super().load_rknn(path)

        def init_runtime(self, **kwargs):
            events.append("init")
            return super().init_runtime(**kwargs)

        def release(self):
            events.append("runtime-release")
            return super().release()

    lease = OrderedLease()
    runtime = OrderedRuntime()
    session = RknnSession(
        "ordered.rknn", lease=lease, runtime_factory=lambda: runtime)
    assert events == ["acquire", "load", "init", "ready"]
    session.release()
    assert events[-2:] == ["runtime-release", "lease-release"]


def test_ready_failure_rolls_back_runtime_before_releasing_lease():
    backend = RuntimeError("broker generation changed")
    events = []

    class FailingLease(FakeLease):
        def ready(self):
            events.append("ready")
            raise backend

        def release(self):
            events.append("lease-release")
            return super().release()

    class OrderedRuntime(FakeRuntime):
        def release(self):
            events.append("runtime-release")
            return super().release()

    lease = FailingLease()
    runtime = OrderedRuntime()
    with pytest.raises(ModelLoadError) as caught:
        RknnSession("ready-fail.rknn", lease=lease,
                    runtime_factory=lambda: runtime)
    assert caught.value.operation == "model.lease.ready"
    assert caught.value.code == "npu_lease_ready_failed"
    assert caught.value.__cause__ is backend
    assert events == ["ready", "runtime-release", "lease-release"]


def test_every_inference_checks_shared_lease_liveness_before_driver():
    lease = FakeLease()
    runtime = FakeRuntime()
    session = RknnSession(
        "alive.rknn", lease=lease, runtime_factory=lambda: runtime)
    sample = np.zeros((1, 2, 2, 3), dtype=np.uint8)

    session.infer(sample)
    session.infer(sample)

    assert lease.alive_calls == 2
    assert len(runtime.inputs) == 2
    session.release()


def test_revoked_or_unverifiable_lease_fences_inference_before_driver():
    sample = np.zeros((1, 2, 2, 3), dtype=np.uint8)

    revoked_lease = FakeLease(alive_result=False)
    revoked_runtime = FakeRuntime()
    revoked = RknnSession(
        "revoked.rknn",
        lease=revoked_lease,
        runtime_factory=lambda: revoked_runtime,
    )
    with pytest.raises(InferenceError) as caught:
        revoked.infer(sample)
    assert caught.value.operation == "model.infer.lease"
    assert caught.value.code == "npu_lease_revoked"
    assert revoked_runtime.inputs == []
    revoked.release()

    backend = RuntimeError("control socket reset")
    broken_lease = FakeLease(alive_error=backend)
    broken_runtime = FakeRuntime()
    broken = RknnSession(
        "unverifiable.rknn",
        lease=broken_lease,
        runtime_factory=lambda: broken_runtime,
    )
    with pytest.raises(InferenceError) as caught:
        broken.infer(sample)
    assert caught.value.code == "npu_lease_check_failed"
    assert caught.value.__cause__ is backend
    assert broken_runtime.inputs == []
    broken.release()


def test_load_failure_is_typed_and_rolls_back_every_resource():
    lease = FakeLease()
    runtime = FakeRuntime(load_rc=9)
    with pytest.raises(ModelLoadError) as caught:
        RknnSession("bad.rknn", lease=lease,
                    runtime_factory=lambda: runtime)
    assert caught.value.operation == "model.load"
    assert caught.value.details["native_code"] == 9
    assert runtime.releases == 1
    assert lease.release_calls == 1


def test_declared_input_contract_supports_named_multi_input():
    lease = FakeLease()
    runtime = FakeRuntime(output=[np.zeros((1, 2), dtype=np.float32)])
    spec = ModelSpec(
        "multi.rknn",
        inputs=(
            TensorSpec("image", (1, 4, 4, 3), "uint8", "NHWC"),
            TensorSpec("scale", (1,), "float32", "N"),
        ),
    )
    session = RknnSession(spec, lease=lease, runtime_factory=lambda: runtime)
    result = session.infer({
        "scale": np.ones((1,), dtype=np.float32),
        "image": np.zeros((1, 4, 4, 3), dtype=np.uint8),
    })
    assert len(result) == 1
    assert runtime.inputs[0][0].shape == (1, 4, 4, 3)
    assert runtime.inputs[0][1].dtype == np.float32
    assert session.stats.calls == 1
    session.release()


def test_strict_session_rejects_dtype_without_entering_driver():
    runtime = FakeRuntime()
    spec = ModelSpec("m.rknn", inputs=(
        TensorSpec("image", (1, 2, 2, 3), "uint8", "NHWC"),
    ))
    session = RknnSession(spec, lease=FakeLease(),
                          runtime_factory=lambda: runtime)
    with pytest.raises(InputValidationError):
        session.infer(np.zeros((2, 2, 3), dtype=np.float32))
    assert runtime.inputs == []
    session.release()


def test_dynamic_tensor_dimension_rejects_empty_array_before_driver():
    runtime = FakeRuntime()
    spec = ModelSpec("m.rknn", inputs=(
        TensorSpec("items", (1, -1), "uint8", "NC"),
    ))
    session = RknnSession(spec, lease=FakeLease(),
                          runtime_factory=lambda: runtime)
    with pytest.raises(InputValidationError) as caught:
        session.infer(np.zeros((1, 0), dtype=np.uint8))
    assert caught.value.operation == "model.infer.validate"
    assert runtime.inputs == []
    session.release()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"inputs": ("not-a-spec",)},
        {"inputs": (
            TensorSpec("image", (1, 2), "uint8", "NC"),
            TensorSpec("image", (1, 2), "uint8", "NC"),
        )},
        {"outputs": (
            TensorSpec("scores", (1, 2), "float32", "NC"),
            TensorSpec("scores", (1, 2), "float32", "NC"),
        )},
    ],
)
def test_model_spec_rejects_wrong_or_duplicate_tensor_declarations(kwargs):
    with pytest.raises(ConfigurationError) as caught:
        ModelSpec("m.rknn", **kwargs)
    assert caught.value.operation == "model.spec"


def test_legacy_model_keeps_uint8_cast_and_hwc_batch_expansion():
    runtime = FakeRuntime()
    model = RknnModel("m.rknn", lease=FakeLease(),
                      runtime_factory=lambda: runtime)
    model.infer(np.zeros((2, 2, 3), dtype=np.float32))
    sent = runtime.inputs[0][0]
    assert sent.shape == (1, 2, 2, 3)
    assert sent.dtype == np.uint8
    model.release()


def test_driver_exception_is_wrapped_with_cause():
    backend = RuntimeError("driver reset")
    session = RknnSession("m.rknn", lease=FakeLease(),
                          runtime_factory=lambda: FakeRuntime(infer_error=backend))
    with pytest.raises(InferenceError) as caught:
        session.infer(np.zeros((1, 2, 2, 3), dtype=np.uint8))
    assert caught.value.__cause__ is backend
    assert caught.value.retryable is True
    session.release()


def _control_flow_exceptions():
    return [
        pytest.param(_GracefulStop(signal.SIGTERM), id="graceful-stop"),
        pytest.param(KeyboardInterrupt("operator stop"), id="keyboard-interrupt"),
        pytest.param(SystemExit(23), id="system-exit"),
    ]


@pytest.mark.parametrize("control_error", _control_flow_exceptions())
def test_init_control_flow_propagates_exactly_and_rolls_back(control_error):
    lease = FakeLease()
    runtime = FakeRuntime()

    def interrupted_init(**_kwargs):
        raise control_error

    runtime.init_runtime = interrupted_init
    with pytest.raises(type(control_error)) as caught:
        RknnSession("m.rknn", lease=lease,
                    runtime_factory=lambda: runtime)

    assert caught.value is control_error
    assert runtime.releases == 1
    assert lease.release_calls == 1


def test_lease_acquire_control_flow_propagates_and_calls_idempotent_release():
    error = _GracefulStop(signal.SIGTERM)

    class InterruptedLease(FakeLease):
        def acquire(self, timeout=None):
            self.acquire_calls.append(timeout)
            raise error

    lease = InterruptedLease()
    with pytest.raises(_GracefulStop) as caught:
        RknnSession("m.rknn", lease=lease,
                    runtime_factory=lambda: pytest.fail("runtime constructed"))
    assert caught.value is error
    assert lease.release_calls == 1


def test_init_exception_is_wrapped_after_rollback():
    backend = RuntimeError("driver init reset")
    lease = FakeLease()
    runtime = FakeRuntime()

    def failed_init(**_kwargs):
        raise backend

    runtime.init_runtime = failed_init
    with pytest.raises(ModelLoadError) as caught:
        RknnSession("m.rknn", lease=lease,
                    runtime_factory=lambda: runtime)
    assert caught.value.__cause__ is backend
    assert runtime.releases == 1
    assert lease.release_calls == 1


@pytest.mark.parametrize("control_error", _control_flow_exceptions())
def test_infer_control_flow_propagates_exactly_and_releases(control_error):
    lease = FakeLease()
    runtime = FakeRuntime(infer_error=control_error)
    session = RknnSession("m.rknn", lease=lease,
                          runtime_factory=lambda: runtime)

    with pytest.raises(type(control_error)) as caught:
        session.infer(np.zeros((1, 2, 2, 3), dtype=np.uint8))

    assert caught.value is control_error
    assert session.released is True
    assert session.stats.failures == 1
    assert runtime.releases == 1
    assert lease.release_calls == 1
    session.release()
    assert runtime.releases == 1
    assert lease.release_calls == 1


def test_input_conversion_control_flow_also_releases_session():
    error = SystemExit(47)
    lease = FakeLease()
    runtime = FakeRuntime()
    session = RknnSession("m.rknn", lease=lease,
                          runtime_factory=lambda: runtime)
    session._ordered_inputs = lambda _inputs: (_ for _ in ()).throw(error)

    with pytest.raises(SystemExit) as caught:
        session.infer(object())

    assert caught.value is error
    assert session.released is True
    assert runtime.releases == 1
    assert lease.release_calls == 1


def test_runtime_release_failure_retains_lease_until_retry_succeeds():
    backend_error = RuntimeError("driver still owns context")
    lease = FakeLease()
    runtime = FakeRuntime(release_error=backend_error)
    session = RknnSession("m.rknn", lease=lease,
                          runtime_factory=lambda: runtime)

    with pytest.raises(InferenceError) as caught:
        session.release()
    assert caught.value.code == "runtime_release_failed"
    assert caught.value.details["lease_retained"] is True
    assert caught.value.__cause__ is backend_error
    assert session.released is False
    assert lease.release_calls == 0

    runtime.release_error = None
    session.release()
    assert runtime.releases == 2
    assert lease.release_calls == 1
    assert session.released is True


def test_nonzero_runtime_release_status_is_fail_closed():
    lease = FakeLease()
    runtime = FakeRuntime(release_rc=5)
    session = RknnSession("m.rknn", lease=lease,
                          runtime_factory=lambda: runtime)
    with pytest.raises(InferenceError) as caught:
        session.release()
    assert caught.value.code == "runtime_release_failed"
    assert lease.release_calls == 0
    runtime.release_rc = 0
    session.release()
    assert lease.release_calls == 1


def test_lease_release_failure_is_quarantined_and_retryable_after_runtime_close():
    backend_error = RuntimeError("broker close failed")

    class FailsOnceLease(FakeLease):
        def release(self):
            self.release_calls += 1
            if self.release_calls == 1:
                raise backend_error

    lease = FailsOnceLease()
    runtime = FakeRuntime()
    session = RknnSession(
        "m.rknn", lease=lease, runtime_factory=lambda: runtime)

    with pytest.raises(InferenceError) as caught:
        session.release()

    assert caught.value.code == "lease_release_failed"
    assert caught.value.details["lease_retained"] is True
    assert caught.value.__cause__ is backend_error
    assert runtime.releases == 1
    assert lease.release_calls == 1
    assert session.released is False

    session.release()
    assert runtime.releases == 1
    assert lease.release_calls == 2
    assert session.released is True


def test_context_manager_preserves_body_error_when_release_is_quarantined():
    body_error = ValueError("workflow failed")
    runtime = FakeRuntime(release_error=RuntimeError("release failed"))
    lease = FakeLease()
    session = RknnSession("m.rknn", lease=lease,
                          runtime_factory=lambda: runtime)
    with pytest.raises(ValueError) as caught:
        with session:
            raise body_error
    assert caught.value is body_error
    assert lease.release_calls == 0

    # Let the explicit recovery retry succeed so this test does not retain a
    # fake quarantined context for the remainder of the process.
    runtime.release_error = None
    session.release()


def test_release_waits_for_active_inference_before_destroying_runtime():
    events: list[str] = []
    inference_started = threading.Event()
    allow_inference = threading.Event()

    class BlockingRuntime(FakeRuntime):
        def inference(self, inputs):
            events.append("infer-start")
            inference_started.set()
            assert allow_inference.wait(2.0)
            events.append("infer-end")
            return super().inference(inputs)

        def release(self):
            events.append("release")
            return super().release()

    runtime = BlockingRuntime()
    session = RknnSession("m.rknn", lease=FakeLease(),
                          runtime_factory=lambda: runtime)
    runner = threading.Thread(
        target=lambda: session.infer(np.zeros((1, 2, 2, 3), dtype=np.uint8)))
    closer = threading.Thread(target=session.release)
    runner.start()
    assert inference_started.wait(1.0)
    closer.start()
    assert "release" not in events
    allow_inference.set()
    runner.join(2.0)
    closer.join(2.0)
    assert events == ["infer-start", "infer-end", "release"]


def test_concurrent_inference_calls_are_serialized_per_context():
    state_lock = threading.Lock()
    active = 0
    max_active = 0

    # A shared active counter proves the second call never enters the driver
    # until the first one leaves it.
    entered = threading.Event()
    finish_first = threading.Event()

    class SerialRuntime(FakeRuntime):
        def inference(self, inputs):
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
                call_number = len(self.inputs)
            if call_number == 0:
                entered.set()
                assert finish_first.wait(2.0)
            result = super().inference(inputs)
            with state_lock:
                active -= 1
            return result

    runtime = SerialRuntime()
    session = RknnSession("m.rknn", lease=FakeLease(),
                          runtime_factory=lambda: runtime)
    threads = [threading.Thread(
        target=lambda: session.infer(np.zeros((1, 2, 2, 3), dtype=np.uint8)))
        for _ in range(2)]
    threads[0].start()
    assert entered.wait(1.0)
    threads[1].start()
    finish_first.set()
    for thread in threads:
        thread.join(2.0)
    assert max_active == 1
    assert len(runtime.inputs) == 2
    session.release()


def test_inference_callback_cannot_reentrantly_destroy_its_context():
    holder = {}

    class ReentrantRuntime(FakeRuntime):
        def inference(self, inputs):
            with pytest.raises(InferenceError) as caught:
                holder["session"].release()
            assert caught.value.code == "reentrant_release"
            return super().inference(inputs)

    runtime = ReentrantRuntime()
    session = RknnSession("m.rknn", lease=FakeLease(),
                          runtime_factory=lambda: runtime)
    holder["session"] = session
    session.infer(np.zeros((1, 2, 2, 3), dtype=np.uint8))
    session.release()

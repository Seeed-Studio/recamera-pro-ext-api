"""DMA transport integration with real Unix sockets and explicit fake DMA fds."""
import mmap
import os
import threading
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from kit.errors import CapabilityError, InferenceError, TransportError
from kit.runtime import _inference_shared as shared
from kit.runtime.remote import RemoteRknnSession
from market.inferenced.authorization import AuthorizationError
from market.inferenced.driver_lock import DriverLockError, NpuDriverCoordinator
from market.inferenced.server import FakeBackend, RknnBackend
from market.inferenced.tests.test_service import (
    RunningService, _TestAuthorizer, _wait_for, model,
)
from market.inferenced.tests.test_shared_protocol import memfd, open_fds  # noqa: F401


SHAPE = (1, 2, 3, 3)


class Buffer:
    def __init__(self, factory, dtype, *, allocation=0):
        self.dtype = np.dtype(dtype)
        self.shape = SHAPE
        self.size = max(int(np.prod(SHAPE)) * self.dtype.itemsize, allocation)
        self.offset = 0
        self.fd = factory(self.size)
        self.mapping = mmap.mmap(self.fd, self.size)
        self.array = np.ndarray(SHAPE, dtype=self.dtype, buffer=self.mapping)
        self.closed = False

    def describe(self):
        return dict(fd=self.fd, size=self.size, offset=self.offset,
                    shape=list(self.shape), strides=list(self.array.strides),
                    dtype=str(self.dtype), w_stride=3, h_stride=2)

    def close(self):
        self.closed = True
        self.array = None
        self.mapping.close()
        os.close(self.fd)
        self.fd = -1


class SharedBackend(FakeBackend):
    def __init__(self, factory, *, supported=True, estimate=90, allocation=0):
        super().__init__()
        self.factory = factory
        self.supported = supported
        self.estimate = estimate
        self.allocation = allocation
        self.channels = []
        self.closed_channels = []
        self.entered = threading.Event()
        self.unblock = threading.Event()
        self.finished = threading.Event()
        self.block = False
        self.active = False
        self.closed_while_active = False
        self.seen_inputs = []

    def shared_io_size(self, _handle):
        return self.estimate if self.supported else 0

    def open_shared_io(self, _handle):
        pair = (Buffer(self.factory, "uint8", allocation=self.allocation),
                [Buffer(self.factory, "float32", allocation=self.allocation)])
        self.channels.append(pair)
        return pair

    def close_shared_io(self, _handle, channel):
        self.closed_while_active |= self.active
        for item in [channel.input, *channel.outputs]:
            item.close()
        self.closed_channels.append(channel)

    def infer(self, handle, inputs):
        self.calls.append(handle["path"])
        return [np.asarray(item, dtype=np.float32) + 1 for item in inputs]

    def infer_shared_io(self, handle, channel):
        self.active = True
        self.entered.set()
        try:
            if self.block:
                assert self.unblock.wait(5), "test did not release blocked fake NPU"
            self.seen_inputs.append(channel.input.array.copy())
            np.add(channel.input.array, 1, out=channel.outputs[0].array)
            self.calls.append(handle["path"])
            return {"runtime": 0.5, "driver_wait": 0.1}
        finally:
            self.active = False
            self.finished.set()


@pytest.fixture(autouse=True)
def fake_dma_sync(monkeypatch):
    # These buffers are host memfds; only this fixture replaces DMA cache ioctls.
    monkeypatch.setattr(shared.fcntl, "ioctl", lambda *_a, **_kw: 0)


@pytest.fixture
def launch(tmp_path, memfd):
    services, sessions = [], []
    path, digest = model(tmp_path)

    def start(backend=None, *, memory_mb=128, authorizer=None):
        backend = SharedBackend(memfd) if backend is None else backend
        running = RunningService(tmp_path, backend=backend, memory_mb=memory_mb,
                                 authorizer=authorizer)
        services.append(running)

        def connect(instance="a", **kwargs):
            session = RemoteRknnSession(
                str(path), socket_path=running.socket, model_sha256=digest,
                app_id="test-rgb", instance_id=instance, generation=1, **kwargs)
            sessions.append(session)
            return session

        return running, backend, connect

    yield start
    for running in services:
        if hasattr(running.backend, "unblock"):
            running.backend.unblock.set()
    for session in sessions:
        if not session.released:
            session.release()
    for running in services:
        running.close()


def run_async(function):
    result, errors = [], []
    done = threading.Event()

    def call():
        try:
            result.append(function())
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    thread = threading.Thread(target=call, daemon=True)
    thread.start()
    return thread, done, result, errors


def test_same_cached_model_uses_isolated_dma_channels_during_concurrent_calls(launch):
    running, backend, connect = launch()
    a, b = connect("a"), connect("b")
    assert a.io_transport == b.io_transport == shared.SHARED_IO_VERSION
    assert len(backend.loaded) == 1
    assert running.service.status()["models"][0]["references"] == 2
    assert a._shared_io.token != b._shared_io.token
    assert os.fstat(a._shared_io.input.fd).st_ino != os.fstat(b._shared_io.input.fd).st_ino

    backend.block = True
    ta, da, ra, ea = run_async(lambda: a.infer(np.full(SHAPE, 21, np.uint8)))
    assert backend.entered.wait(1)
    tb, db, rb, eb = run_async(lambda: b.infer(np.full(SHAPE, 97, np.uint8)))
    try:
        assert _wait_for(lambda: np.all(backend.channels[1][0].array == 97))
        assert np.all(backend.channels[0][0].array == 21)
        assert not da.is_set() and not db.is_set()
    finally:
        backend.unblock.set()
        ta.join(2)
        tb.join(2)
    assert da.is_set() and db.is_set() and not ea and not eb
    np.testing.assert_array_equal(ra[0][0], np.full(SHAPE, 22, np.float32))
    np.testing.assert_array_equal(rb[0][0], np.full(SHAPE, 98, np.float32))
    held = ra[0][0]
    a.infer(np.full(SHAPE, 201, np.uint8))
    a.release()
    assert len(backend.closed_channels) == 1
    assert backend.released == []
    b.release()
    assert len(backend.closed_channels) == 2 and len(backend.released) == 1
    running.close()
    np.testing.assert_array_equal(held, np.full(SHAPE, 22, np.float32))
    assert running.service.status()["shared_io_reserved_bytes"] == 0


def test_timed_out_dma_call_cannot_reuse_channel_and_server_waits_for_active_work(launch):
    running, backend, connect = launch()
    session = connect()
    backend.block = True
    thread, done, _results, errors = run_async(
        lambda: session.infer(np.full(SHAPE, 42, np.uint8), timeout=0.02))
    assert backend.entered.wait(1)
    try:
        assert done.wait(2), "deadline must reach the caller while native work is in flight"
        assert len(errors) == 1 and isinstance(errors[0], InferenceError)
        assert backend.active and not backend.finished.is_set()
        with pytest.raises(TransportError):
            session.infer(np.full(SHAPE, 99, np.uint8))
        with pytest.raises(TransportError):
            session.infer_prepared(
                lambda _meta: pytest.fail("timed-out buffer must never be prepared again"),
                lambda: pytest.fail("timed-out session must never be reused"))
        assert np.all(backend.channels[0][0].array == 42)
        session.release()
        assert backend.closed_channels == []
        assert backend.channels[0][0].closed is False
    finally:
        backend.unblock.set()
        thread.join(2)
    assert backend.finished.wait(1)
    assert _wait_for(lambda: len(backend.closed_channels) == 1)
    assert backend.closed_while_active is False
    assert _wait_for(lambda: running.service.status()["shared_io_reserved_bytes"] == 0)


@pytest.mark.parametrize("mode", ["old-backend", "unsupported-model", "explicit-opt-out"])
def test_legacy_or_unsupported_shared_io_keeps_normal_inference(launch, memfd, mode):
    backend = FakeBackend() if mode == "old-backend" else SharedBackend(
        memfd, supported=mode != "unsupported-model")
    _running, _backend, connect = launch(backend)
    session = connect(shared_io=mode != "explicit-opt-out")
    assert session.io_transport == "tensor-v1"
    output = session.infer(np.full(SHAPE, 31, np.uint8))[0]
    assert np.all(output == 32)
    if isinstance(backend, SharedBackend):
        assert backend.channels == []


def test_shared_io_estimate_budget_rejection_only_disables_optimization(launch, memfd):
    backend = SharedBackend(memfd, estimate=40 * 1024 * 1024)
    running, _backend, connect = launch(backend, memory_mb=64)
    session = connect()
    assert session.io_transport == "tensor-v1"
    assert backend.channels == []
    assert np.all(session.infer(np.full(SHAPE, 31, np.uint8))[0] == 32)
    assert running.service.status()["shared_io_reserved_bytes"] == 0


def test_actual_shared_allocation_budget_rejection_rolls_back_and_falls_back(launch, memfd):
    # Sparse memfds consume virtual address space here, not 40 MiB of RAM.
    backend = SharedBackend(memfd, estimate=90, allocation=20 * 1024 * 1024)
    running, _backend, connect = launch(backend, memory_mb=64)
    session = connect()
    assert session.io_transport == "tensor-v1"
    assert len(backend.closed_channels) == 1
    assert all(item.closed for pair in backend.channels for item in [pair[0], *pair[1]])
    assert np.all(session.infer(np.full(SHAPE, 31, np.uint8))[0] == 32)
    assert running.service.status()["shared_io_reserved_bytes"] == 0


def test_application_shared_budget_is_enforced_before_allocation(launch, memfd, tmp_path):
    class SmallApplication(_TestAuthorizer):
        def authorize(self, **claims):
            return replace(super().authorize(**claims), memory_limit_mb=34)

    backend = SharedBackend(memfd, estimate=2 * 1024 * 1024)
    _running, _backend, connect = launch(
        backend, memory_mb=128, authorizer=SmallApplication(tmp_path))
    session = connect()
    assert session.io_transport == "tensor-v1"
    assert backend.channels == []
    assert np.all(session.infer(np.full(SHAPE, 4, np.uint8))[0] == 5)


def test_prepared_rgb_matches_array_input_and_decline_uses_lazy_fallback(launch):
    _running, _backend, connect = launch()
    session = connect()
    pixels = np.arange(np.prod(SHAPE), dtype=np.uint8).reshape(SHAPE)
    reference = session.infer(pixels)[0]
    prepared = []

    def prepare(meta):
        prepared.append(meta)
        assert meta["offset"] == 0
        assert meta["shape"] == list(SHAPE)
        os.pwrite(meta["fd"], pixels.tobytes(), 0)  # fake completed hardware write
        return True

    output = session.infer_prepared(prepare, lambda: pytest.fail("unexpected CPU fallback"))[0]
    np.testing.assert_array_equal(output, reference)
    assert len(prepared) == 1
    fallbacks = []

    def fallback():
        fallbacks.append(True)
        return pixels

    declined = session.infer_prepared(lambda _meta: False, fallback)[0]
    np.testing.assert_array_equal(declined, reference)
    assert fallbacks == [True]


def test_prepared_callback_is_skipped_on_legacy_transport(launch):
    _running, _backend, connect = launch(FakeBackend())
    session = connect()
    output = session.infer_prepared(
        lambda _meta: pytest.fail("legacy service has no DMA descriptor"),
        lambda: np.full(SHAPE, 12, np.uint8))[0]
    assert np.all(output == 13)


def test_revocation_cleans_only_revoked_client_dma_channel(launch, tmp_path):
    class Revocable(_TestAuthorizer):
        revoked = set()

        def validate(self, authorization):
            if authorization.instance_id in self.revoked:
                raise AuthorizationError("test authorization revoked")

    authorizer = Revocable(tmp_path)
    running, backend, connect = launch(authorizer=authorizer)
    old, live = connect("old"), connect("live")
    old.infer(np.full(SHAPE, 6, np.uint8))
    authorizer.revoked.add("old")
    with pytest.raises(CapabilityError):
        old.infer(np.full(SHAPE, 9, np.uint8))
    old.release()
    assert _wait_for(lambda: len(backend.closed_channels) == 1)
    assert backend.released == []
    assert np.all(live.infer(np.full(SHAPE, 11, np.uint8))[0] == 12)
    assert running.service.status()["models"][0]["references"] == 1
    live.release()
    assert len(backend.closed_channels) == 2 and len(backend.released) == 1


def test_queued_shared_call_rechecks_revocation_before_touching_native_context(launch, tmp_path):
    class Revocable(_TestAuthorizer):
        revoked = False
        admitted = threading.Event()

        def validate(self, authorization):
            if authorization.instance_id == "queued":
                if self.revoked:
                    raise AuthorizationError("queued launch was revoked")
                self.admitted.set()

    authorizer = Revocable(tmp_path)
    _running, backend, connect = launch(authorizer=authorizer)
    active, queued = connect("active"), connect("queued")
    authorizer.admitted.clear()
    backend.block = True
    ta, da, _ra, ea = run_async(lambda: active.infer(np.full(SHAPE, 7, np.uint8)))
    assert backend.entered.wait(1)
    tb, db, _rb, eb = run_async(lambda: queued.infer(np.full(SHAPE, 31, np.uint8)))
    try:
        assert authorizer.admitted.wait(1)
        authorizer.revoked = True
    finally:
        backend.unblock.set()
        ta.join(2)
        tb.join(2)
    assert da.is_set() and db.is_set() and not ea
    assert len(eb) == 1 and isinstance(eb[0], CapabilityError)
    assert len(backend.seen_inputs) == 1
    assert np.all(backend.seen_inputs[0] == 7)
    queued.release()
    assert _wait_for(lambda: len(backend.closed_channels) == 1)
    assert np.all(active.infer(np.full(SHAPE, 9, np.uint8))[0] == 10)


@pytest.mark.parametrize("stage", ["input", "outputs"])
@pytest.mark.parametrize("cleanup_failed", [False, True])
def test_native_allocation_rollback_failure_quarantines_driver_until_process_exit(
    tmp_path, stage, cleanup_failed,
):
    class Runtime:
        native_cleanup_failed = cleanup_failed
        attempts = 0
        input_released = False

        def allocate_input_buffer(self):
            self.attempts += 1
            if stage == "input":
                raise RuntimeError("native allocation failed")
            return object()

        def allocate_output_buffers(self):
            self.attempts += 1
            raise RuntimeError("native allocation failed")

        def release_input_buffer(self, _buffer):
            self.input_released = True

    coordinator = NpuDriverCoordinator(str(tmp_path / "npu.lock"))
    backend = RknnBackend(coordinator=coordinator)
    runtime = Runtime()
    try:
        with pytest.raises(RuntimeError, match="native allocation failed"):
            backend.open_shared_io(runtime)
        if cleanup_failed:
            assert coordinator.fault and "rollback failed" in coordinator.fault
            attempts = runtime.attempts
            with pytest.raises(DriverLockError, match="quarantined"):
                backend.open_shared_io(runtime)
            assert runtime.attempts == attempts
        else:
            assert coordinator.fault is None
            assert runtime.input_released is (stage == "outputs")
            with coordinator.hold(timeout=0.01):
                pass  # a confirmed clean rollback does not quarantine the NPU
    finally:
        # Production deliberately retains this process-lifetime fence. This
        # test owns the coordinator and drops it only after proving isolation.
        if coordinator._quarantine_fd is not None:
            os.close(coordinator._quarantine_fd)
            coordinator._quarantine_fd = None
            coordinator._local.release()


def test_model_initialization_cannot_hide_failed_native_cleanup_with_a_second_release(tmp_path):
    class Runtime:
        native_cleanup_failed = False
        release_calls = 0

        def load_rknn(self, _path):
            return 0

        def init_runtime(self):
            self.native_cleanup_failed = True
            raise RuntimeError("initial native cleanup failed")

        def release(self):
            self.release_calls += 1
            return 0  # must not be used as proof that an earlier failure was safe

    runtime = Runtime()
    coordinator = NpuDriverCoordinator(str(tmp_path / "npu.lock"))
    backend = RknnBackend(runtime_factory=lambda: runtime, coordinator=coordinator)
    try:
        with pytest.raises(RuntimeError, match="initial native cleanup failed"):
            backend.load(str(tmp_path / "fake.rknn"), {})
        assert runtime.release_calls == 0
        assert coordinator.fault
        with pytest.raises(DriverLockError, match="quarantined"):
            with coordinator.hold(timeout=0.01):
                pass
    finally:
        if coordinator._quarantine_fd is not None:
            os.close(coordinator._quarantine_fd)
            coordinator._quarantine_fd = None
            coordinator._local.release()


def test_repeated_multiclient_shared_lifecycle_has_bounded_allocations_and_no_fd_leak(
    launch, memfd,
):
    class AuditedBackend(SharedBackend):
        def __init__(self, factory):
            super().__init__(factory)
            self.live_handles = {}

        def load(self, path, spec):
            handle = super().load(path, spec)
            self.live_handles[id(handle)] = handle
            return handle

        def release(self, handle):
            assert self.live_handles.pop(id(handle)) is handle
            super().release(handle)

    backend = AuditedBackend(memfd)
    running, _backend, connect = launch(backend)
    baseline = open_fds()
    retained_mappings = []
    clients_per_round, rounds, calls_per_client = 4, 25, 10

    def cleanup_finished():
        with running.service._lock:
            return (not running.service._clients
                    and not running.service._connections
                    and not running.service._threads
                    and not running.service._models)

    with ThreadPoolExecutor(max_workers=clients_per_round) as pool:
        for round_index in range(rounds):
            sessions = [
                connect(f"stress-{round_index}-{index}")
                for index in range(clients_per_round)
            ]
            assert all(s.io_transport == shared.SHARED_IO_VERSION for s in sessions)
            assert len(backend.live_handles) == 1
            assert len(backend.loaded) == round_index + 1
            allocated_channels = len(backend.channels)
            assert allocated_channels == (round_index + 1) * clients_per_round
            for session in sessions:
                retained_mappings.extend(
                    item.mapping for item in
                    [session._shared_io.input, *session._shared_io.outputs])
            active_fds = open_fds()

            for iteration in range(calls_per_client):
                values = [round_index + iteration + index for index in range(clients_per_round)]
                futures = [
                    pool.submit(session.infer, np.full(SHAPE, value, np.uint8))
                    for session, value in zip(sessions, values)
                ]
                for future, value in zip(futures, values):
                    assert np.all(future.result(timeout=2)[0] == value + 1)
                # An active channel reuses its existing buffers every call;
                # neither RKNN buffer creation nor FD/mmap allocation grows.
                assert len(backend.channels) == allocated_channels
                assert open_fds() == active_fds

            stops = [pool.submit(session.release) for session in sessions]
            for stop in stops:
                stop.result(timeout=2)
            assert _wait_for(cleanup_finished, timeout=2)
            assert len(backend.closed_channels) == allocated_channels
            assert backend.live_handles == {}
            assert len(backend.released) == round_index + 1
            assert running.service.status()["shared_io_reserved_bytes"] == 0
            assert open_fds() == baseline

    assert len(backend.calls) == rounds * clients_per_round * calls_per_client
    assert len(backend.channels) == rounds * clients_per_round
    # Retain Python wrappers deliberately: passing cannot rely on gc/destructors
    # to close leaked mappings, client fds, native buffers, or model handles.
    assert len(retained_mappings) == rounds * clients_per_round * 2
    assert all(mapping.closed for mapping in retained_mappings)
    assert all(item.closed and item.mapping.closed and item.fd == -1
               for input_buffer, outputs in backend.channels
               for item in [input_buffer, *outputs])

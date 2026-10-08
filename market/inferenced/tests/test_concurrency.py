"""Concurrency limits and resource lifetime with real queues, locks and UDS."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
import threading
import time
import types

import numpy as np
import pytest

from kit.errors import CapabilityError, InferenceError, TransportError
from kit.runtime.remote import RemoteRknnSession
from market.inferenced._concurrency import NativeOperationGate
from market.inferenced._memory import DeferredCycleCollector
from market.inferenced.driver_lock import NpuDriverCoordinator
from market.inferenced.scheduler import FairScheduler, ScheduledJob
from market.inferenced.server import FakeBackend, RknnBackend
from market.inferenced.tests.test_service import RunningService, _TestAuthorizer, _wait_for, model


class BlockingBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.lock = threading.Lock()
        self.proceed = threading.Event()
        self.active = Counter()
        self.peak = 0
        self.context_peak = Counter()

    def infer(self, handle, inputs):
        key = handle['path']
        with self.lock:
            self.active[key] += 1
            self.context_peak[key] = max(self.context_peak[key], self.active[key])
            self.peak = max(self.peak, sum(self.active.values()))
        try:
            assert self.proceed.wait(5), 'test did not release inference'
            return super().infer(handle, inputs)
        finally:
            with self.lock:
                self.active[key] -= 1

    def release(self, handle):
        assert self.active[handle['path']] == 0, 'destroyed an executing context'
        super().release(handle)


def connect(running, path, name):
    return RemoteRknnSession(str(path), socket_path=running.socket,
                             app_id=name, instance_id='test', generation=1,
                             shared_io=False)


@pytest.mark.parametrize('limit', [1, 4])
def test_service_limits_independent_contexts_and_preserves_results(tmp_path, limit):
    backend = BlockingBackend()
    running = RunningService(tmp_path, backend=backend, memory_mb=512,
                             max_concurrent_models=limit)
    sessions = [connect(running, model(tmp_path, f'm{i}.rknn', bytes([i]))[0], f'app{i}')
                for i in range(6)]
    pool = ThreadPoolExecutor(max_workers=6)
    try:
        futures = [pool.submit(s.infer, np.full((1, 8), i, np.float32))
                   for i, s in enumerate(sessions)]
        assert _wait_for(lambda: sum(backend.active.values()) == limit)
        assert _wait_for(lambda: running.service.status()['pending_requests'] == 6-limit)
        assert backend.peak == limit
        assert running.service.status()['max_concurrent_models'] == limit
        backend.proceed.set()
        for i, future in enumerate(futures):
            np.testing.assert_array_equal(future.result(3)[0], np.full((1, 8), i+1, np.float32))
        assert backend.peak == limit
        assert all(v == 1 for v in backend.context_peak.values())
    finally:
        backend.proceed.set()
        pool.shutdown()
        for s in sessions: s.release()
        running.close()


def test_busy_cached_context_does_not_consume_other_workers(tmp_path):
    backend = BlockingBackend()
    running = RunningService(tmp_path, backend=backend, memory_mb=512)
    path, _ = model(tmp_path, 'same.rknn')
    sessions = [connect(running, path, f'app{i}') for i in range(5)]
    sessions += [connect(running, model(tmp_path, f'other{i}.rknn', bytes([i]))[0], f'other{i}')
                 for i in range(3)]
    pool = ThreadPoolExecutor(max_workers=8)
    try:
        futures = [pool.submit(s.infer, np.full((1, 8), i, np.float32))
                   for i, s in enumerate(sessions)]
        assert _wait_for(lambda: sum(backend.active.values()) == 4)
        assert _wait_for(lambda: running.service.status()['pending_requests'] == 4)
        assert len(running.service.status()['models']) == 4
        assert max(backend.context_peak.values()) == 1
        backend.proceed.set()
        for i, f in enumerate(futures):
            np.testing.assert_array_equal(f.result(3)[0], np.full((1, 8), i+1, np.float32))
        assert max(backend.context_peak.values()) == 1
    finally:
        backend.proceed.set()
        pool.shutdown()
        for s in sessions: s.release()
        running.close()


def test_shutdown_drains_all_active_contexts_and_cancels_pending(tmp_path):
    backend = BlockingBackend()
    running = RunningService(tmp_path, backend=backend, memory_mb=512)
    sessions = [connect(running, model(tmp_path, f'm{i}.rknn', bytes([i]))[0], f'app{i}')
                for i in range(5)]
    pool = ThreadPoolExecutor(max_workers=5)
    closer = None
    try:
        futures = [pool.submit(s.infer, np.zeros((1, 8), np.float32)) for s in sessions]
        assert _wait_for(lambda: sum(backend.active.values()) == 4)
        assert _wait_for(lambda: running.service.status()['pending_requests'] == 1)
        closer = threading.Thread(target=running.close)
        closer.start()
        assert _wait_for(lambda: running.service._stopping.is_set())
        assert not backend.released
        backend.proceed.set()
        closer.join(5)
        assert not closer.is_alive()
        assert len(backend.calls) == 4
        assert len(backend.released) == 5
        assert not any(t.is_alive() for t in running.service.scheduler._threads)
        for f in futures:
            try: f.result(1)
            except Exception: pass  # A stopping server may close before sending.
    finally:
        backend.proceed.set()
        if closer: closer.join(5)
        pool.shutdown()
        for s in sessions:
            try: s.release()
            except TransportError: pass  # Server shutdown intentionally closed UDS.
        running.close()


@pytest.mark.parametrize('bad', [0, 5, True, 1.5, '4'])
def test_invalid_concurrency_bound_is_rejected(bad):
    with pytest.raises(ValueError, match='max_concurrent_models'):
        FairScheduler(max_concurrent_models=bad)


def test_same_app_queue_can_skip_busy_model_and_run_other_model():
    scheduler = FairScheduler()
    first_entered, other_entered, unblock = (threading.Event() for _ in range(3))
    def first():
        first_entered.set()
        assert unblock.wait(3)
    def job(key, execute):
        return ScheduledJob('client', 50, time.monotonic()+3, execute,
                            fairness_id='app', concurrency_key=key)
    try:
        a = scheduler.submit(job('a', first))
        assert first_entered.wait(1)
        a2 = scheduler.submit(job('a', lambda: 'a2'))
        b = scheduler.submit(job('b', other_entered.set))
        assert other_entered.wait(1)
        assert not a2._done.is_set()
        unblock.set()
        a.result(1); b.result(1)
        assert a2.result(1) == 'a2'
    finally:
        unblock.set()
        scheduler.close()


def test_gc_barrier_drains_native_calls_and_prevents_writer_starvation():
    gate = NativeOperationGate()
    release_readers, writer_entered, release_writer, late_entered = (threading.Event() for _ in range(4))
    count = [0]
    lock = threading.Lock()
    def reader():
        with gate.shared():
            with lock: count[0] += 1
            assert release_readers.wait(3)
    def writer():
        with gate.exclusive():
            writer_entered.set()
            assert release_writer.wait(3)
    def late():
        with gate.shared(): late_entered.set()
    pool = ThreadPoolExecutor(max_workers=6)
    try:
        readers = [pool.submit(reader) for _ in range(4)]
        assert _wait_for(lambda: count[0] == 4)
        with gate.exclusive(blocking=False) as acquired: assert not acquired
        w = pool.submit(writer)
        assert _wait_for(lambda: gate._waiting_writers == 1)
        last = pool.submit(late)
        assert not writer_entered.wait(.05) and not late_entered.is_set()
        release_readers.set()
        assert writer_entered.wait(1)
        assert not late_entered.is_set()
        release_writer.set()
        for f in [*readers, w, last]: f.result(1)
    finally:
        release_readers.set(); release_writer.set()
        pool.shutdown()


@pytest.mark.parametrize('shared_io', [False, True])
def test_due_rknnlite_cleanup_not_starved_by_ctypes_traffic(tmp_path, monkeypatch, shared_io):
    backend = RknnBackend(coordinator=NpuDriverCoordinator(str(tmp_path/'npu.lock')))
    backend._cycles = DeferredCycleCollector(delay=0)
    backend._cycles.request()  # An earlier RKNNLite response has been dropped.
    collected = threading.Event()

    def collect():
        assert backend._native_gate._readers == 0
        collected.set()
        return 0

    def native(*args, **kwargs):
        assert collected.is_set(), 'a new ctypes call bypassed due GC'
        return [np.ones(1)]

    monkeypatch.setattr('market.inferenced._memory.gc.collect', collect)
    handle = types.SimpleNamespace(backend='ctypes', inference=native, infer_dma_buffers=native)
    channel = types.SimpleNamespace(input=None, outputs=[])
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        with backend._native_gate.shared():  # Another context is still running.
            call = (lambda: backend.infer_shared_io(handle, channel)) if shared_io else (
                lambda: backend.infer(handle, []))
            pending = pool.submit(call)
            assert _wait_for(lambda: backend._native_gate._waiting_writers == 1)
            assert not pending.done() and not collected.is_set()
            assert not backend.collect_pending()  # The accept loop never waits.
        pending.result(2)
        assert collected.is_set()
        assert not backend._cycles.pending(force=True)
    finally:
        pool.shutdown()


@pytest.mark.parametrize('reason', ['revoked', 'deadline'])
def test_admission_rechecked_after_waiting_for_kernel_driver_fence(tmp_path, reason):
    class Runtime:
        backend = 'ctypes'
        called = False
        def load_rknn(self, _): return 0
        def init_runtime(self): return 0
        def inference(self, *, inputs):
            self.called = True
            return inputs
        def release(self): return 0
    class Authorizer(_TestAuthorizer):
        revoked = False
        def validate(self, auth):
            if self.revoked:
                from market.inferenced.authorization import AuthorizationError
                raise AuthorizationError('revoked')
    class Coordinator(NpuDriverCoordinator):
        entered = threading.Event()
        @contextmanager
        def inference(self, **kw):
            self.entered.set()
            with super().inference(**kw): yield
    coordinator = Coordinator(str(tmp_path/'npu.lock'))
    runtime = Runtime()
    authorizer = Authorizer(tmp_path)
    backend = RknnBackend(runtime_factory=lambda: runtime, coordinator=coordinator)
    running = RunningService(tmp_path, backend=backend, authorizer=authorizer)
    s = connect(running, model(tmp_path)[0], 'app')
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        with coordinator.hold():
            f = pool.submit(s.infer, np.zeros((1, 8), np.float32),
                            timeout=.2 if reason == 'deadline' else 30)
            assert coordinator.entered.wait(1)
            if reason == 'revoked':
                authorizer.revoked = True
            else:
                # The worker has passed scheduler admission and is waiting
                # for LOCK_SH. Expire the request before allowing that lock.
                time.sleep(.25)
        error = CapabilityError if reason == 'revoked' else InferenceError
        message = 'revoked' if reason == 'revoked' else 'deadline'
        with pytest.raises(error, match=message): f.result(2)
        assert not runtime.called
    finally:
        pool.shutdown()
        s.release()
        running.close()


def test_real_backend_gate_and_driver_fence_admit_four_contexts(tmp_path):
    entered = [0]
    lock = threading.Lock()
    proceed = threading.Event()
    class Runtime:
        backend = 'ctypes'
        def load_rknn(self, _): return 0
        def init_runtime(self): return 0
        def inference(self, *, inputs):
            with lock: entered[0] += 1
            assert proceed.wait(3), 'backend still serialized unrelated contexts'
            return [inputs[0]+1]
        def release(self): return 0
    backend = RknnBackend(runtime_factory=Runtime,
                         coordinator=NpuDriverCoordinator(str(tmp_path/'npu.lock')))
    running = RunningService(tmp_path, backend=backend, memory_mb=256)
    sessions = [connect(running, model(tmp_path, f'm{i}.rknn', bytes([i]))[0], f'a{i}')
                for i in range(4)]
    pool = ThreadPoolExecutor(max_workers=4)
    try:
        futures = [pool.submit(s.infer, np.full((1, 8), i, np.float32))
                   for i, s in enumerate(sessions)]
        assert _wait_for(lambda: entered[0] == 4), 'not all native calls overlapped'
        proceed.set()
        for i, f in enumerate(futures):
            np.testing.assert_array_equal(f.result(2)[0], np.full((1, 8), i+1, np.float32))
    finally:
        proceed.set()
        pool.shutdown()
        for s in sessions: s.release()
        running.close()

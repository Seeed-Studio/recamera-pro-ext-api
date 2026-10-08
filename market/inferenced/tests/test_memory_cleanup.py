"""Real cyclic buffers and threaded RPC, without the target RKNN binary."""

import ctypes
import gc
import sys
import threading
import time
import types
import weakref
from contextlib import contextmanager

import numpy as np
import pytest

from kit.errors import InferenceError, TransportError
from kit.runtime._inference_protocol import recv_message, send_message
from kit.runtime.remote import RemoteRknnSession
from kit.runtime.rknnlite import RknnLiteRuntime
from market.inferenced import server
from market.inferenced._memory import DeferredCycleCollector
from market.inferenced.server import FakeBackend, InferenceService, RknnBackend
from market.inferenced.tests.test_service import RunningService, model


class Coordinator:
    def __init__(self):
        self.active = False
        self.retained = []

    @contextmanager
    def hold(self):
        assert not self.active
        self.active = True
        try:
            yield types.SimpleNamespace(retain_fail_closed=self.retained.append)
        finally:
            self.active = False


@pytest.fixture
def vendor(monkeypatch):
    class Vendor:
        instances = []
        init_error = False

        def __init__(self):
            self.instances.append(self)
            self.buffers = []
            self.last_output = lambda: None
            self.rknn_data = b"model-bytes"
            self.value = 11
            self.mode = "ok"
            self.block = None
            self.release_result = 0

        def buffer(self):
            buf = (ctypes.c_float * 8)(*[self.value] * 8)
            buf.cycle = buf
            self.buffers.append(weakref.ref(buf))
            return buf

        def load_rknn(self, path):
            return 0

        def init_runtime(self):
            buf = self.buffer()
            if self.init_error:
                raise RuntimeError("initialization failed")
            return 0

        def inference(self, *, inputs):
            if self.block is not None:
                entered, proceed = self.block
                entered.set()
                assert proceed.wait(5), "test did not unblock native call"
            buf = self.buffer()
            if self.mode == "raise":
                raise RuntimeError("native inference failed")
            if self.mode == "none":
                return None
            out = np.ctypeslib.as_array(buf).reshape(1, 8)
            self.last_output = weakref.ref(out)
            return [out]

        def release(self):
            self.buffer()
            return self.release_result

    monkeypatch.setitem(sys.modules, "rknnlite.api", types.SimpleNamespace(RKNNLite=Vendor))
    monkeypatch.setenv("ESK_RKNN_BACKEND", "rknnlite")
    enabled = gc.isenabled()
    gc.disable()  # Only explicit cleanup can make these weak references expire.
    try:
        yield Vendor
    finally:
        gc.collect()
        if enabled:
            gc.enable()


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(.01)
    assert predicate()


def session(running, tmp_path, name="voice", content=b"model"):
    path, digest = model(tmp_path, name + ".rknn", content=content)
    return RemoteRknnSession(str(path), socket_path=running.socket,
                             model_sha256=digest, shared_io=False)


def dead(vendor):
    return all(ref() is None for handle in vendor.instances for ref in handle.buffers)


def test_collection_requests_coalesce_without_postponing_deadline(monkeypatch):
    now, calls = [0.0], []
    collector = DeferredCycleCollector(clock=lambda: now[0])
    monkeypatch.setattr(gc, "collect", lambda: calls.append(now[0]))
    collector.request()
    now[0] = .4
    collector.request()
    assert not collector.collect()
    now[0] = .5
    assert collector.collect()
    assert not collector.collect(force=True)
    assert calls == [.5]


def test_notification_during_collection_is_not_lost(monkeypatch):
    collector = DeferredCycleCollector()
    collector.request()
    monkeypatch.setattr(gc, "collect", collector.request)
    assert collector.collect(force=True)
    monkeypatch.setattr(gc, "collect", lambda: None)
    assert collector.collect(force=True)
    assert not collector.collect(force=True)


def test_service_returns_vendor_array_and_keeps_other_models_live(vendor, monkeypatch):
    coordinator = Coordinator()
    backend = RknnBackend(coordinator=coordinator)
    collect, phases = gc.collect, []

    def checked_collect():
        assert not coordinator.active, "GC must not hold the NPU driver lock"
        phases.append("gc")
        return collect()

    monkeypatch.setattr(gc, "collect", checked_collect)
    a, b = backend.load("a.rknn", {}), backend.load("b.rknn", {})
    vendor.instances[1].value = 22
    try:
        out_a = backend.infer(a, [])
        assert out_a[0] is vendor.instances[0].last_output()
        assert not out_a[0].flags.owndata
        ref_a = vendor.instances[0].buffers[-1]
        for _ in range(10):
            out_b = backend.infer(b, [])
            assert out_b[0] is vendor.instances[1].last_output()
            np.testing.assert_array_equal(out_a[0], np.full((1, 8), 11))
            np.testing.assert_array_equal(out_b[0], np.full((1, 8), 22))
            del out_b
        assert ref_a() is not None
        del out_a
        backend.infer(b, [])
        assert ref_a() is None
    finally:
        backend.release(a)
        backend.release(b)
    assert dead(vendor)
    assert phases


def test_local_wrapper_still_returns_owned_outputs(vendor):
    runtime = RknnLiteRuntime()
    out = runtime.inference(inputs=[])
    assert out[0].flags.owndata
    assert dead(vendor)
    runtime.release()
    np.testing.assert_array_equal(out[0], np.full((1, 8), 11))


def test_one_collection_per_completed_call_not_before_and_after(vendor, monkeypatch):
    backend = RknnBackend(coordinator=Coordinator())
    handle = backend.load("a.rknn", {})
    count = []
    collect = gc.collect
    monkeypatch.setattr(gc, "collect", lambda: (count.append(1), collect())[1])
    for _ in range(6):
        out = backend.infer(handle, [])
        del out
        backend.response_finished()
    assert len(count) == 5
    assert backend.collect_pending(force=True)
    assert len(count) == 6
    assert not backend.collect_pending(force=True)
    assert dead(vendor)
    backend.release(handle)


def test_idle_service_reclaims_last_output_without_next_request(vendor, tmp_path):
    running = RunningService(tmp_path, backend=RknnBackend(coordinator=Coordinator()))
    remote = session(running, tmp_path)
    try:
        out = remote.infer(np.zeros((1, 8), np.float32))
        wait_for(lambda: dead(vendor))
        assert len(running.service.status()["models"]) == 1
        np.testing.assert_array_equal(out[0], np.full((1, 8), 11))
    finally:
        remote.release()
        running.close()
    assert dead(vendor)


@pytest.mark.parametrize("mode", ["raise", "none"])
def test_failed_call_reclaims_buffers_and_allows_retry(vendor, tmp_path, mode):
    running = RunningService(tmp_path, backend=RknnBackend(coordinator=Coordinator()))
    remote = session(running, tmp_path)
    try:
        vendor.instances[0].mode = mode
        with pytest.raises(InferenceError):
            remote.infer(np.zeros((1, 8), np.float32))
        wait_for(lambda: dead(vendor))
        vendor.instances[0].mode = "ok"
        np.testing.assert_array_equal(remote.infer(np.zeros((1, 8), np.float32))[0],
                                      np.full((1, 8), 11))
    finally:
        remote.release()
        running.close()


def test_failed_load_reclaims_traceback_cycles(vendor, tmp_path):
    vendor.init_error = True
    coordinator = Coordinator()
    running = RunningService(tmp_path, backend=RknnBackend(coordinator=coordinator))
    try:
        with pytest.raises(InferenceError, match="initialization failed"):
            session(running, tmp_path)
        wait_for(lambda: dead(vendor))
        assert vendor.instances[0].rknn_data is None
        assert not coordinator.retained
    finally:
        running.close()


def test_failed_release_preserves_model_for_quarantine(vendor):
    coordinator = Coordinator()
    backend = RknnBackend(coordinator=coordinator)
    handle = backend.load("a.rknn", {})
    raw = vendor.instances[0]
    raw.release_result = 9
    with pytest.raises(RuntimeError, match="release returned 9"):
        backend.release(handle)
    assert raw.rknn_data == b"model-bytes"
    assert coordinator.retained
    assert dead(vendor)


def test_slow_response_survives_other_model_gc_then_reclaims(vendor, tmp_path, monkeypatch):
    running = RunningService(tmp_path, backend=RknnBackend(coordinator=Coordinator()))
    a = session(running, tmp_path, "a", b"a")
    b = session(running, tmp_path, "b", b"b")
    vendor.instances[1].value = 22
    sending, proceed = threading.Event(), threading.Event()
    original = server.send_message
    results = []

    def slow_send(conn, header, tensors=()):
        if header.get("op") == "infer" and tensors and tensors[0][0, 0] == 11:
            sending.set()
            assert proceed.wait(5)
            np.testing.assert_array_equal(tensors[0], np.full((1, 8), 11))
        return original(conn, header, tensors)

    monkeypatch.setattr(server, "send_message", slow_send)
    thread = threading.Thread(target=lambda: results.extend(a.infer(np.zeros((1, 8), np.float32))))
    thread.start()
    try:
        assert sending.wait(2)
        ref_a = vendor.instances[0].buffers[-1]
        for _ in range(5):
            np.testing.assert_array_equal(b.infer(np.zeros((1, 8), np.float32))[0],
                                          np.full((1, 8), 22))
        running.backend.collect_pending(force=True)
        assert ref_a() is not None
        proceed.set()
        thread.join(2)
        assert not thread.is_alive()
        np.testing.assert_array_equal(results[0], np.full((1, 8), 11))
        wait_for(lambda: dead(vendor))
    finally:
        proceed.set()
        thread.join(2)
        a.release()
        b.release()
        running.close()


def test_send_failure_reclaims_buffers(vendor, tmp_path, monkeypatch):
    running = RunningService(tmp_path, backend=RknnBackend(coordinator=Coordinator()))
    remote = session(running, tmp_path)
    original = server.send_message

    def failed_send(conn, header, tensors=()):
        if header.get("op") == "infer" and header.get("ok"):
            raise BrokenPipeError("client left during send")
        return original(conn, header, tensors)

    monkeypatch.setattr(server, "send_message", failed_send)
    try:
        with pytest.raises((InferenceError, TransportError)):
            remote.infer(np.zeros((1, 8), np.float32))
        wait_for(lambda: dead(vendor))
        wait_for(lambda: not running.service.status()["models"])
    finally:
        remote.release()
        running.close()


@pytest.mark.parametrize("mode", ["ok", "raise"])
def test_worker_finishing_after_timeout_reclaims_without_another_request(vendor, tmp_path, mode, monkeypatch):
    running = RunningService(tmp_path, backend=RknnBackend(coordinator=Coordinator()))
    remote = session(running, tmp_path)
    entered, proceed = threading.Event(), threading.Event()
    vendor.instances[0].block = (entered, proceed)
    vendor.instances[0].mode = mode

    def response_wait_expires(job, timeout=None):
        # Exercise a response timeout while native work is already running.
        # A short wire deadline can instead expire during pre-inference GC,
        # which correctly rejects the call before it reaches the vendor.
        assert entered.wait(3), "native call never started"
        raise server.DeadlineExceededError("scheduler result wait timed out")

    monkeypatch.setattr(server.ScheduledJob, "result", response_wait_expires)
    try:
        # Keep the connection open after a server-side deadline. The real
        # vendor call outlives the response, so response cleanup alone is wrong.
        remote._sock.settimeout(5)
        send_message(remote._sock, {"op": "infer", "alias": remote._alias,
                                    "request_id": 900, "timeout_ms": 30000},
                     [np.zeros((1, 8), np.float32)])
        reply, _ = recv_message(remote._sock)
        assert reply["error"]["code"] == "deadline_exceeded"
        assert not running.backend.collect_pending()  # Native work still active.
        proceed.set()
        wait_for(lambda: len(vendor.instances[0].buffers) >= 2)
        wait_for(lambda: dead(vendor))
        assert len(running.service.status()["models"]) == 1
    finally:
        proceed.set()
        remote.release()
        running.close()


def test_ctypes_only_workload_does_not_schedule_gc(monkeypatch):
    backend = RknnBackend(coordinator=Coordinator())
    handle = types.SimpleNamespace(backend="ctypes", inference=lambda **kw: [np.ones(2)],
                                   release=lambda: 0)
    calls = []
    monkeypatch.setattr(gc, "collect", lambda: calls.append(1))
    for _ in range(5):
        assert backend.output_cleanup(handle) is None
        backend.infer(handle, [])
        backend.response_finished()
        backend.collect_pending(force=True)
    backend.release(handle)
    assert calls == []


def test_stop_during_maintenance_does_not_crash_accept_loop(tmp_path):
    entered, proceed = threading.Event(), threading.Event()
    failures = []

    class Backend(FakeBackend):
        def collect_pending(self, *, force=False):
            if not force:
                entered.set()
                assert proceed.wait(5)

    service = InferenceService(str(tmp_path / "test.sock"), backend=Backend())

    def serve():
        try:
            service.serve_forever()
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        assert entered.wait(2)
        service.close()
    finally:
        proceed.set()
        thread.join(2)
        service.close()
    assert not thread.is_alive()
    assert failures == []

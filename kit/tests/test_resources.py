from __future__ import annotations

import gc
import os
import signal
import subprocess
import sys
import threading
import weakref
from pathlib import Path

import pytest

import kit.resources as resources
from kit.errors import (
    CapabilityError,
    InputValidationError,
    ResourceBusyError,
    ResourceTimeoutError,
    TransportError,
)
from kit.resources import ExternalNpuLease


class FakeBroker:
    def __init__(self):
        self.ready_calls = 0
        self.alive_calls = 0
        self.alive_result = True
        self.release_calls = 0
        self.abandon_calls = 0

    def ready(self):
        self.ready_calls += 1

    def alive(self):
        self.alive_calls += 1
        return self.alive_result

    def release(self):
        self.release_calls += 1

    def _abandon_after_fork_child(self):
        self.abandon_calls += 1


class FakeBrokerFactory:
    def __init__(self, broker=None, error=None):
        self.broker = broker or FakeBroker()
        self.error = error
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.broker


@pytest.fixture(autouse=True)
def _clean_broker_state(monkeypatch):
    """A failed broker assertion must not poison later process-global tests."""

    monkeypatch.delenv(resources.NPU_BROKER_REQUIRED_ENV, raising=False)
    assert resources._held_broker is None
    yield
    held = resources._held_broker
    resources._held_broker = None
    if held is not None:
        held.lease.release()


def test_default_device_backend_uses_rkipc_broker_without_legacy_marker(
    monkeypatch,
):
    factory = FakeBrokerFactory()
    monkeypatch.delenv("RECAMERA_NPU_LOCK", raising=False)
    monkeypatch.delenv("RECAMERA_NPU_MANAGED", raising=False)

    lease = ExternalNpuLease(
        app_id="vision",
        instance_id="worker-1",
        fallback_builtin=False,
        lib_path="/test/librecamera_ext.so.1",
        broker_factory=factory,
    ).acquire(timeout=0.1251)

    assert lease.backend == "broker"
    assert lease.path == resources.DEFAULT_NPU_BROKER
    assert factory.calls == [{
        "app_id": "vision",
        "instance_id": "worker-1",
        "timeout_ms": 126,
        "fallback_builtin": False,
        "lib_path": "/test/librecamera_ext.so.1",
    }]
    lease.release()
    assert factory.broker.release_calls == 1


@pytest.mark.parametrize("override", ["argument", "environment"])
def test_appmgr_broker_required_marker_rejects_legacy_overrides(
    tmp_path, monkeypatch, override
):
    path = str(tmp_path / "legacy.lock")
    monkeypatch.setenv(resources.NPU_BROKER_REQUIRED_ENV, "1")
    kwargs = {}
    if override == "argument":
        kwargs["path"] = path
    else:
        monkeypatch.setenv("RECAMERA_NPU_LOCK", path)

    with pytest.raises(InputValidationError) as caught:
        ExternalNpuLease(**kwargs)

    assert caught.value.code == "npu_broker_required"
    assert not os.path.exists(path)


def test_appmgr_broker_required_marker_selects_broker(monkeypatch):
    factory = FakeBrokerFactory()
    monkeypatch.setenv(resources.NPU_BROKER_REQUIRED_ENV, "1")
    monkeypatch.setenv("RECAMERA_NPU_MANAGED", resources.NPU_MANAGED_MARKER)
    monkeypatch.delenv("RECAMERA_NPU_LOCK", raising=False)

    lease = ExternalNpuLease(broker_factory=factory).acquire(timeout=0)
    assert lease.backend == "broker"
    lease.release()


def test_broker_connection_is_process_shared_ready_once_and_closed_at_last_ref(
    monkeypatch,
):
    factory = FakeBrokerFactory()
    monkeypatch.delenv("RECAMERA_NPU_LOCK", raising=False)
    first = ExternalNpuLease(
        app_id="multi", instance_id="one", broker_factory=factory)
    second = ExternalNpuLease(
        app_id="multi", instance_id="one", broker_factory=factory)

    first.acquire(timeout=1)
    second.acquire(timeout=1)
    assert len(factory.calls) == 1
    assert first.acquired and second.acquired

    first.ready()
    second.ready()
    assert factory.broker.ready_calls == 1
    assert first.alive() is True
    assert second.alive() is True
    assert factory.broker.alive_calls == 2

    first.release()
    assert factory.broker.release_calls == 0
    assert second.acquired
    second.release()
    second.release()
    assert factory.broker.release_calls == 1


def test_final_broker_release_failure_retains_fence_and_can_be_retried(
    monkeypatch,
):
    class FailsOnceBroker(FakeBroker):
        def release(self):
            self.release_calls += 1
            if self.release_calls == 1:
                raise RuntimeError("injected close failure")

    broker = FailsOnceBroker()
    factory = FakeBrokerFactory(broker=broker)
    monkeypatch.delenv("RECAMERA_NPU_LOCK", raising=False)
    lease = ExternalNpuLease(broker_factory=factory).acquire(timeout=0)

    with pytest.raises(TransportError) as caught:
        lease.release()

    assert caught.value.operation == "npu.lease.release"
    assert lease.acquired is True
    assert lease.alive() is True
    assert resources._held_broker is not None
    assert resources._held_broker.lease is broker
    assert resources._held_broker.references == 1

    lease.release()
    assert broker.release_calls == 2
    assert lease.acquired is False
    assert resources._held_broker is None


def test_lease_finalizer_cannot_self_deadlock_inside_acquire_transaction(
    tmp_path,
):
    """GC may run while typed acquire errors are allocated under this lock."""

    code = """
from kit import resources
from kit.resources import ExternalNpuLease

lease = ExternalNpuLease(__import__('sys').argv[1])
with resources._acquire_lock:
    lease.__del__()
print('finalizer-returned', flush=True)
"""
    completed = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path / "unused.lock")],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "finalizer-returned"


def test_default_broker_fails_closed_without_falling_back_to_flock(
    tmp_path, monkeypatch
):
    class MissingBroker(OSError):
        pass

    factory = FakeBrokerFactory(error=MissingBroker("missing native ABI"))
    legacy_path = str(tmp_path / "must-not-exist.lock")
    monkeypatch.setattr(resources, "DEFAULT_NPU_LOCK", legacy_path)
    monkeypatch.delenv("RECAMERA_NPU_LOCK", raising=False)

    with pytest.raises(CapabilityError) as caught:
        ExternalNpuLease(broker_factory=factory).acquire(timeout=0)

    assert caught.value.operation == "npu.lease.acquire"
    assert not os.path.exists(legacy_path)


@pytest.mark.parametrize(
    ("timeout", "error_type", "expected_ms"),
    [
        (0, ResourceBusyError, 1),
        (1.25, ResourceTimeoutError, 1250),
        (None, ResourceTimeoutError, 0),
    ],
)
def test_broker_busy_errors_are_typed_without_silent_legacy_fallback(
    monkeypatch, timeout, error_type, expected_ms
):
    class NativeBusy(RuntimeError):
        code_value = 3

    factory = FakeBrokerFactory(error=NativeBusy("busy"))
    monkeypatch.delenv("RECAMERA_NPU_LOCK", raising=False)
    with pytest.raises(error_type) as caught:
        ExternalNpuLease(broker_factory=factory).acquire(timeout=timeout)
    assert caught.value.details["backend"] == "rkipc"
    assert factory.calls[0]["timeout_ms"] == expected_ms


def test_broker_timeout_over_native_limit_is_rejected_before_factory(
    monkeypatch,
):
    factory = FakeBrokerFactory()
    monkeypatch.delenv("RECAMERA_NPU_LOCK", raising=False)
    with pytest.raises(InputValidationError):
        ExternalNpuLease(broker_factory=factory).acquire(timeout=30.001)
    assert factory.calls == []


def test_child_fork_hook_abandons_shared_broker_without_release(monkeypatch):
    factory = FakeBrokerFactory()
    monkeypatch.delenv("RECAMERA_NPU_LOCK", raising=False)
    lease = ExternalNpuLease(broker_factory=factory).acquire(timeout=0)

    resources._after_fork_child()

    assert lease.acquired is False
    assert resources._held_broker is None
    assert factory.broker.abandon_calls == 1
    assert factory.broker.release_calls == 0


def test_same_process_sessions_share_one_lock(tmp_path):
    path = str(tmp_path / "npu.lock")
    first = ExternalNpuLease(path).acquire(timeout=0)
    second = ExternalNpuLease(path).acquire(timeout=0)
    assert first.acquired and second.acquired
    first.release()
    assert second.acquired
    second.release()


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), -1.0, "bad"])
def test_invalid_timeout_is_rejected_before_opening_lock(tmp_path, timeout):
    path = str(tmp_path / "never-opened.lock")
    with pytest.raises(InputValidationError) as caught:
        ExternalNpuLease(path).acquire(timeout=timeout)
    assert caught.value.operation == "npu.lease.acquire"
    assert not os.path.exists(path)


def test_discarded_lease_releases_its_process_reference(tmp_path):
    path = str(tmp_path / "gc.lock")
    lease = ExternalNpuLease(path).acquire(timeout=0)
    reference = weakref.ref(lease)
    assert path in resources._held_by_path
    del lease
    gc.collect()
    assert reference() is None
    assert path not in resources._held_by_path

    # The finalizer released the kernel lock too, not only local bookkeeping.
    replacement = ExternalNpuLease(path).acquire(timeout=0)
    replacement.release()


def test_release_to_acquire_handoff_is_serialized(tmp_path, monkeypatch):
    path = str(tmp_path / "handoff.lock")
    first = ExternalNpuLease(path).acquire(timeout=0)
    real_flock = resources.fcntl.flock
    unlock_started = threading.Event()
    allow_unlock = threading.Event()

    def delayed_flock(fd, operation):
        if operation == resources.fcntl.LOCK_UN:
            unlock_started.set()
            assert allow_unlock.wait(2.0)
        return real_flock(fd, operation)

    monkeypatch.setattr(resources.fcntl, "flock", delayed_flock)
    release_thread = threading.Thread(target=first.release)
    acquired: list[ExternalNpuLease] = []
    failures: list[BaseException] = []

    def take_next():
        try:
            acquired.append(ExternalNpuLease(path).acquire(timeout=0))
        except BaseException as exc:
            failures.append(exc)

    acquire_thread = threading.Thread(target=take_next)
    release_thread.start()
    assert unlock_started.wait(1.0)
    acquire_thread.start()
    assert acquire_thread.is_alive()  # waits for the atomic release transaction
    allow_unlock.set()
    release_thread.join(2.0)
    acquire_thread.join(2.0)
    assert not failures
    assert len(acquired) == 1 and acquired[0].acquired
    acquired[0].release()


def test_default_device_lock_rejects_unmanaged_manual_process(
    tmp_path, monkeypatch
):
    device_lock = str(tmp_path / "device-npu.lock")
    monkeypatch.setattr(resources, "DEFAULT_NPU_LOCK", device_lock)
    monkeypatch.delenv("RECAMERA_NPU_LOCK", raising=False)
    monkeypatch.delenv("RECAMERA_NPU_MANAGED", raising=False)

    with pytest.raises(ResourceBusyError) as caught:
        ExternalNpuLease(device_lock).acquire(timeout=0)

    assert caught.value.code == "unmanaged_npu_start"
    assert caught.value.details["managed_marker"] is False
    assert not os.path.exists(device_lock)


def test_appmgr_marker_requires_the_managed_process_to_be_session_leader(
    tmp_path, monkeypatch
):
    device_lock = str(tmp_path / "device-npu.lock")
    monkeypatch.setattr(resources, "DEFAULT_NPU_LOCK", device_lock)
    monkeypatch.delenv("RECAMERA_NPU_LOCK", raising=False)
    monkeypatch.setenv("RECAMERA_NPU_MANAGED", resources.NPU_MANAGED_MARKER)
    monkeypatch.setattr(resources.os, "getpid", lambda: 4200)
    monkeypatch.setattr(resources.os, "getpgrp", lambda: 4199)
    monkeypatch.setattr(resources.os, "getsid", lambda _pid: 4199)

    with pytest.raises(ResourceBusyError) as caught:
        ExternalNpuLease(device_lock).acquire(timeout=0)
    assert caught.value.details["managed_marker"] is True
    assert caught.value.details["session_leader"] is False


def test_appmgr_managed_session_leader_may_acquire_default_device_lock(
    tmp_path, monkeypatch
):
    device_lock = str(tmp_path / "device-npu.lock")
    monkeypatch.setattr(resources, "DEFAULT_NPU_LOCK", device_lock)
    monkeypatch.delenv("RECAMERA_NPU_LOCK", raising=False)
    monkeypatch.setenv("RECAMERA_NPU_MANAGED", resources.NPU_MANAGED_MARKER)
    real_pid = os.getpid()
    monkeypatch.setattr(resources.os, "getpgrp", lambda: real_pid)
    monkeypatch.setattr(resources.os, "getsid", lambda _pid: real_pid)

    lease = ExternalNpuLease(device_lock).acquire(timeout=0)
    lease.release()


def test_legacy_appmgr_marker_selects_historical_lock_when_broker_not_required(
    tmp_path, monkeypatch
):
    device_lock = str(tmp_path / "device-npu.lock")
    monkeypatch.setattr(resources, "DEFAULT_NPU_LOCK", device_lock)
    monkeypatch.delenv("RECAMERA_NPU_LOCK", raising=False)
    monkeypatch.setenv("RECAMERA_NPU_MANAGED", resources.NPU_MANAGED_MARKER)
    real_pid = os.getpid()
    monkeypatch.setattr(resources.os, "getpgrp", lambda: real_pid)
    monkeypatch.setattr(resources.os, "getsid", lambda _pid: real_pid)

    lease = ExternalNpuLease()
    assert lease.backend == "flock"
    assert lease.path == device_lock
    lease.acquire(timeout=0)
    lease.release()


def test_concurrent_threads_share_the_process_lock_without_false_busy(tmp_path):
    path = str(tmp_path / "npu.lock")
    start = threading.Barrier(9)
    acquired = threading.Barrier(9)
    failures = []

    def worker():
        lease = ExternalNpuLease(path)
        try:
            start.wait(timeout=3)
            lease.acquire(timeout=0)
            acquired.wait(timeout=3)
        except BaseException as exc:
            failures.append(exc)
        finally:
            lease.release()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    start.wait(timeout=3)
    acquired.wait(timeout=3)
    for thread in threads:
        thread.join(timeout=3)
    assert not failures
    assert all(not thread.is_alive() for thread in threads)


def test_control_flow_while_waiting_closes_unpublished_fd(tmp_path, monkeypatch):
    path = str(tmp_path / "interrupted-npu.lock")
    error = KeyboardInterrupt("stop waiting")
    real_close = os.close
    closed = []

    def close(fd):
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(resources.fcntl, "flock",
                        lambda *_args: (_ for _ in ()).throw(BlockingIOError()))
    monkeypatch.setattr(resources.time, "sleep",
                        lambda _seconds: (_ for _ in ()).throw(error))
    monkeypatch.setattr(resources.os, "close", close)

    with pytest.raises(KeyboardInterrupt) as caught:
        ExternalNpuLease(path).acquire(timeout=1)

    assert caught.value is error
    assert len(closed) == 1


def test_other_process_is_rejected_and_kernel_recovers_lock(tmp_path):
    path = str(tmp_path / "npu.lock")
    code = (
        "import fcntl, os, sys; "
        "fd=os.open(sys.argv[1], os.O_RDWR|os.O_CREAT, 0o600); "
        "fcntl.flock(fd, fcntl.LOCK_EX); print('ready', flush=True); "
        "sys.stdin.read()"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, path],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "ready"
        with pytest.raises(ResourceBusyError):
            ExternalNpuLease(path).acquire(timeout=0)
    finally:
        child.kill()
        child.wait(timeout=5)

    # SIGKILL closes the child's fd in the kernel; no Python finally is needed.
    lease = ExternalNpuLease(path).acquire(timeout=1)
    lease.release()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_fork_child_cannot_extend_dead_parent_npu_lock(tmp_path):
    """A fork-only child must close (not unlock) the parent's flock fd.

    The helper leader exits via os._exit without calling lease.release while its
    fork child stays alive.  Acquiring here proves that the child did not keep
    the leader's shared open-file-description lock alive.
    """

    path = str(tmp_path / "forked-npu.lock")
    repo_root = str(Path(__file__).resolve().parents[2])
    code = """
import os
import sys
import time
from kit.resources import ExternalNpuLease

lease = ExternalNpuLease(sys.argv[1]).acquire(timeout=0)
child = os.fork()
if child == 0:
    print(f"{os.getpid()}:{int(lease.acquired)}", flush=True)
    time.sleep(30)
    os._exit(0)
os._exit(0)
"""
    leader = subprocess.Popen(
        [sys.executable, "-c", code, path],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    child_pid = None
    try:
        line = leader.stdout.readline().strip()
        assert line, leader.stderr.read()
        child_text, acquired_text = line.split(":")
        child_pid = int(child_text)
        assert acquired_text == "0", "fork child retained local lease state"
        leader.wait(timeout=5)
        os.kill(child_pid, 0)  # the fork child is deliberately still alive

        lease = ExternalNpuLease(path).acquire(timeout=1)
        lease.release()
    finally:
        if leader.poll() is None:
            leader.kill()
            leader.wait(timeout=5)
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if leader.stdout is not None:
            leader.stdout.close()
        if leader.stderr is not None:
            leader.stderr.close()

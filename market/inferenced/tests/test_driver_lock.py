from __future__ import annotations

import os
import signal
import threading
import time

import pytest

from market.inferenced.driver_lock import (
    DriverLockError,
    NpuDriverCoordinator,
)


def test_threads_are_serialized(tmp_path):
    coordinator = NpuDriverCoordinator(str(tmp_path / "npu.lock"))
    entered = threading.Event()

    def contender():
        with coordinator.hold(timeout=1):
            entered.set()

    with coordinator.hold():
        thread = threading.Thread(target=contender)
        thread.start()
        assert not entered.wait(0.05)
    thread.join(1)
    assert entered.is_set()


def test_other_process_times_out_and_crash_releases_kernel_fence(tmp_path):
    path = str(tmp_path / "npu.lock")
    ready_read, ready_write = os.pipe()
    child = os.fork()
    if child == 0:  # pragma: no cover - assertions execute in the parent
        os.close(ready_read)
        try:
            coordinator = NpuDriverCoordinator(path)
            with coordinator.hold(timeout=1):
                os.write(ready_write, b"1")
                while True:
                    time.sleep(10)
        finally:
            os._exit(0)

    os.close(ready_write)
    try:
        assert os.read(ready_read, 1) == b"1"
        coordinator = NpuDriverCoordinator(path)
        with pytest.raises(TimeoutError):
            with coordinator.hold(timeout=0.05):
                pass
        os.kill(child, signal.SIGKILL)
        os.waitpid(child, 0)
        child = 0
        with coordinator.hold(timeout=1):
            pass
    finally:
        os.close(ready_read)
        if child:
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(child, 0)


def test_fail_closed_retention_preserves_primary_exception_and_faults_process(
    tmp_path,
):
    path = str(tmp_path / "npu.lock")
    coordinator = NpuDriverCoordinator(path)
    with pytest.raises(RuntimeError, match="PRIMARY"):
        with coordinator.hold() as token:
            token.retain_fail_closed("release failed")
            raise RuntimeError("PRIMARY")
    assert coordinator.fault == "release failed"
    with pytest.raises(DriverLockError, match="quarantined"):
        with coordinator.hold(timeout=0.01):
            pass
    # The descriptor is intentionally process-lifetime in production.  This
    # unit test owns the object and may close it only after all assertions.
    os.close(coordinator._quarantine_fd)
    coordinator._quarantine_fd = None
    coordinator._local.release()

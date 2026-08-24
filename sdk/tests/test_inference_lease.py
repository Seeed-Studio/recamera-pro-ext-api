"""Host tests for the connection-lifetime NPU lease ctypes wrapper."""

from __future__ import annotations

import ctypes
import os

import pytest


class _Function:
    """ctypes function-shaped callable used to exercise ``_bind`` itself."""

    def __init__(self):
        self.restype = None
        self.argtypes = None

    def __call__(self, *_args):
        return 0


class _AllSymbolCDLL:
    def __init__(self):
        self._symbols = {}

    def __getattr__(self, name):
        function = self._symbols.get(name)
        if function is None:
            function = self._symbols[name] = _Function()
        return function


class _PartialInferenceCDLL(_AllSymbolCDLL):
    def __getattr__(self, name):
        if name == "rc_ext_inference_lease_abandon_after_fork":
            raise AttributeError(name)
        return super().__getattr__(name)


class FakeInferenceLib:
    def __init__(self, module, *, open_error=0):
        self.module = module
        self.open_error = open_error
        self.open_calls = []
        self.ready_calls = 0
        self.ready_rc = 0
        self.fallback_calls = []
        self.fallback_rc = 0
        self.status_calls = 0
        self.status_rc = 0
        self.alive_result = 1
        self.close_calls = 0
        self.abandon_calls = 0

    def rc_ext_inference_lease_open(
        self, app_id, instance_id, timeout_ms, fallback_builtin, err_ptr
    ):
        self.open_calls.append((
            app_id,
            instance_id,
            int(timeout_ms.value),
            int(fallback_builtin.value),
        ))
        err_ptr._obj.value = self.open_error
        return None if self.open_error else 0xCAFE

    def rc_ext_inference_lease_ready(self, handle):
        assert handle == 0xCAFE
        self.ready_calls += 1
        return self.ready_rc

    def rc_ext_inference_lease_set_fallback(self, handle, enabled):
        assert handle == 0xCAFE
        self.fallback_calls.append(int(enabled.value))
        return self.fallback_rc

    def rc_ext_inference_lease_status(self, handle, status_ptr):
        assert handle == 0xCAFE
        self.status_calls += 1
        status = status_ptr._obj
        assert status.struct_size == ctypes.sizeof(self.module._InferenceStatus)
        status.state = int(self.module.InferenceState.EXTERNAL_READY)
        status.lease_id = 101
        status.epoch = 202
        status.generation = 303
        status.actual_fps = 0
        status.peer_pid = 404
        status.builtin_enabled = 0
        status.handle_present = 0
        status.fallback_builtin = 1
        status.builtin_state = b"stopped"
        status.source_id = b"vision-app"
        return self.status_rc

    def rc_ext_inference_lease_alive(self, handle):
        assert handle == 0xCAFE
        return self.alive_result

    def rc_ext_inference_lease_close(self, handle):
        assert handle == 0xCAFE
        self.close_calls += 1

    def rc_ext_inference_lease_abandon_after_fork(self, handle):
        assert handle == 0xCAFE
        self.abandon_calls += 1


def test_bind_declares_complete_inference_lease_ctypes_surface(sdk_module):
    lib = sdk_module._bind(_AllSymbolCDLL())

    assert lib.rc_ext_inference_lease_open.restype is ctypes.c_void_p
    assert lib.rc_ext_inference_lease_open.argtypes == [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_int),
    ]
    assert lib.rc_ext_inference_lease_status.argtypes == [
        ctypes.c_void_p,
        ctypes.POINTER(sdk_module._InferenceStatus),
    ]
    assert lib.rc_ext_inference_lease_abandon_after_fork.argtypes == [
        ctypes.c_void_p,
    ]
    assert ctypes.sizeof(sdk_module._InferenceStatus) == 128
    assert sdk_module._InferenceStatus.source_id.offset == 60


def test_bind_rejects_partial_broker_abi_without_fork_safe_abandon(sdk_module):
    with pytest.raises(AttributeError):
        sdk_module._bind(_PartialInferenceCDLL())


def test_full_lease_lifecycle_status_and_ready_are_typed_and_idempotent(
    sdk_module, monkeypatch
):
    fake = FakeInferenceLib(sdk_module)
    monkeypatch.setattr(sdk_module, "_load", lambda _path=None: fake)

    lease = sdk_module.InferenceLease(
        "vision-app",
        "worker-1",
        timeout_ms=12_345,
        fallback_builtin=True,
    )
    assert fake.open_calls == [(b"vision-app", b"worker-1", 12_345, 1)]
    assert lease.acquired
    assert lease.ready() is True
    assert lease.ready() is False
    assert fake.ready_calls == 1

    status = lease.status()
    assert isinstance(status, sdk_module.InferenceStatus)
    assert status.state is sdk_module.InferenceState.EXTERNAL_READY
    assert status.lease_id == 101
    assert status.epoch == 202
    assert status.generation == 303
    assert status.peer_pid == 404
    assert status.builtin_state == "stopped"
    assert status.source_id == "vision-app"
    assert status.fallback_builtin is True
    assert lease.alive() is True

    lease.set_fallback(False)
    assert fake.fallback_calls == [0]
    assert lease.fallback_builtin is False
    assert lease.release() is True
    assert lease.release() is False
    assert fake.close_calls == 1
    assert lease.acquired is False
    with pytest.raises(sdk_module.HandleClosedError):
        lease.alive()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"app_id": ""},
        {"app_id": "bad\x00id"},
        {"app_id": "界" * 22},  # 66 UTF-8 bytes
        {"instance_id": object()},
        {"timeout_ms": -1},
        {"timeout_ms": 30_001},
        {"timeout_ms": True},
    ],
)
def test_constructor_rejects_invalid_abi_inputs_before_loading_native(
    sdk_module, monkeypatch, kwargs
):
    monkeypatch.setattr(
        sdk_module,
        "_load",
        lambda _path=None: pytest.fail("native library must not be loaded"),
    )
    with pytest.raises(sdk_module.FormatError):
        sdk_module.InferenceLease(**kwargs)


def test_missing_optional_symbols_and_native_returns_are_typed(
    sdk_module, monkeypatch
):
    monkeypatch.setattr(sdk_module, "_load", lambda _path=None: object())
    with pytest.raises(sdk_module.CapabilityUnavailableError):
        sdk_module.InferenceLease()

    open_failure = FakeInferenceLib(
        sdk_module, open_error=int(sdk_module.ErrorCode.EBUSY))
    monkeypatch.setattr(sdk_module, "_load", lambda _path=None: open_failure)
    with pytest.raises(sdk_module.BusyError) as caught:
        sdk_module.InferenceLease()
    assert caught.value.operation == "rc_ext_inference_lease_open"

    operation_failure = FakeInferenceLib(sdk_module)
    operation_failure.ready_rc = -int(sdk_module.ErrorCode.EAUTH)
    monkeypatch.setattr(sdk_module, "_load", lambda _path=None: operation_failure)
    lease = sdk_module.InferenceLease()
    with pytest.raises(sdk_module.AuthenticationError):
        lease.ready()
    lease.close()


def test_liveness_distinguishes_revocation_errors_and_invalid_native_values(
    sdk_module, monkeypatch
):
    fake = FakeInferenceLib(sdk_module)
    monkeypatch.setattr(sdk_module, "_load", lambda _path=None: fake)
    lease = sdk_module.InferenceLease()

    fake.alive_result = 0
    assert lease.alive() is False
    fake.alive_result = -int(sdk_module.ErrorCode.EINTERNAL)
    with pytest.raises(sdk_module.InternalError):
        lease.alive()
    fake.alive_result = 2
    with pytest.raises(sdk_module.UnknownNativeError):
        lease.alive()
    lease.close()


def test_fork_child_abandons_its_fd_without_sending_parent_release(
    sdk_module, monkeypatch
):
    fake = FakeInferenceLib(sdk_module)
    monkeypatch.setattr(sdk_module, "_load", lambda _path=None: fake)
    monkeypatch.setattr(sdk_module.os, "getpid", lambda: 100)
    lease = sdk_module.InferenceLease()

    monkeypatch.setattr(sdk_module.os, "getpid", lambda: 200)
    assert lease._abandon_after_fork_child() is True
    assert fake.abandon_calls == 1
    assert fake.close_calls == 0
    assert lease.closed
    assert lease._abandon_after_fork_child() is False


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_registered_fork_hook_abandons_child_copy_and_parent_stays_open(
    sdk_module, monkeypatch
):
    read_fd, write_fd = os.pipe()

    class ReportingLib(FakeInferenceLib):
        def rc_ext_inference_lease_abandon_after_fork(self, handle):
            super().rc_ext_inference_lease_abandon_after_fork(handle)
            os.write(write_fd, b"abandoned")

    fake = ReportingLib(sdk_module)
    monkeypatch.setattr(sdk_module, "_load", lambda _path=None: fake)
    lease = sdk_module.InferenceLease()
    child = os.fork()
    if child == 0:
        try:
            os.close(read_fd)
            os._exit(0 if lease.closed else 41)
        except BaseException:
            os._exit(42)

    os.close(write_fd)
    try:
        assert os.read(read_fd, 64) == b"abandoned"
        waited, status = os.waitpid(child, 0)
        assert waited == child
        assert os.waitstatus_to_exitcode(status) == 0
        assert lease.acquired is True
        assert fake.close_calls == 0
    finally:
        os.close(read_fd)
        lease.close()
    assert fake.close_calls == 1

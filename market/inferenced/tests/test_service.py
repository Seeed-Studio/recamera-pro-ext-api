import hashlib
import os
import socket
import threading
import time
from contextlib import contextmanager

import numpy as np
import pytest

from kit.errors import CapabilityError, InferenceError, ResourceBusyError
from kit.app import App
from kit.capabilities import Capabilities
from kit.device import Device
from kit.runtime._inference_protocol import recv_message, send_message
from kit.runtime.engine import ModelSpec, TensorSpec
from kit.runtime.remote import RemoteRknnModel, RemoteRknnSession
from market.inferenced.authorization import (
    AuthorizationError,
    ClientAuthorization,
    ModelAuthorization,
    estimate_model_memory_mb,
)
from market.inferenced.server import FakeBackend, InferenceService, RknnBackend, _Model


class _TestAuthorizer:
    """Explicit host-only policy injection; production always uses appmgr."""

    def __init__(self, root):
        self.root = os.path.realpath(str(root))

    def authorize(self, **claims):
        return ClientAuthorization(
            app_id=claims["claimed_app"],
            instance_id=claims["claimed_instance"],
            generation=claims["claimed_generation"],
            pid=claims["peer_pid"],
            uid=claims["peer_uid"],
            gid=claims["peer_gid"],
            pid_starttime_ticks=1,
            memory_limit_mb=768,
            models={},
            record_path="host-test-injection",
            record_device=0,
            record_inode=0,
        )

    def validate(self, _authorization):
        return None

    def authorize_model(self, _authorization, path):
        canonical = os.path.realpath(os.path.abspath(os.fspath(path)))
        if not (canonical == self.root or canonical.startswith(self.root + os.sep)):
            raise AuthorizationError("host test model is outside its injected root")
        with open(canonical, "rb") as stream:
            raw = stream.read()
        return ModelAuthorization(
            artifact_id=os.path.basename(canonical).removesuffix(".rknn"),
            path=canonical,
            sha256=hashlib.sha256(raw).hexdigest(),
            size=len(raw),
            memory_mb=estimate_model_memory_mb(len(raw)),
            priority=50,
            max_fps=0.0,
        )


class RunningService:
    def __init__(self, tmp_path, *, backend=None, memory_mb=128,
                 authorizer=None, client_idle_timeout=60.0):
        self.socket = str(tmp_path / "inferenced.sock")
        self.backend = backend or FakeBackend()
        self.service = InferenceService(
            self.socket,
            backend=self.backend,
            allowed_roots=[str(tmp_path)],
            memory_budget_mb=memory_mb,
            authorizer=authorizer or _TestAuthorizer(tmp_path),
            client_idle_timeout=client_idle_timeout,
        )
        self.thread = threading.Thread(target=self.service.serve_forever, daemon=True)
        self.thread.start()
        for _ in range(100):
            if os.path.exists(self.socket):
                break
            time.sleep(0.01)
        else:
            raise AssertionError("service socket did not appear")

    def close(self):
        self.service.close()
        self.thread.join(timeout=2)


def model(tmp_path, name="model.rknn", content=b"fake-rknn"):
    path = tmp_path / name
    path.write_bytes(content)
    return path, hashlib.sha256(content).hexdigest()


def _wait_for(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


def test_authorized_model_connection_survives_idle_timeout(tmp_path):
    idle_timeout = 0.05
    running = RunningService(tmp_path, client_idle_timeout=idle_timeout)
    path, digest = model(tmp_path)
    session = None
    try:
        session = RemoteRknnSession(
            str(path), socket_path=running.socket, model_sha256=digest,
            app_id="ppocr-reader", instance_id="idle-recognizer", generation=7,
        )

        # PPOCR's recognizer is legitimately quiet while the detector sees no
        # text.  Remaining idle for well beyond the control-client deadline
        # must not make the next infer/unload hit BrokenPipe.
        time.sleep(idle_timeout * 3.5)
        output = session.infer(np.zeros((1, 1, 1, 1), dtype=np.uint8))
        assert output[0].item() == 1
        session.release()
        assert running.backend.released == [str(path)]
    finally:
        if session is not None and not session.released:
            session.release()
        running.close()


def test_control_only_connection_still_expires_when_idle(tmp_path):
    idle_timeout = 0.05
    running = RunningService(tmp_path, client_idle_timeout=idle_timeout)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(1.0)
    try:
        client.connect(running.socket)
        send_message(client, {
            "op": "hello",
            "request_id": 1,
            "app_id": "probe",
            "instance_id": "idle-control",
            "generation": 0,
            "control_only": True,
        })
        hello, tensors = recv_message(client)
        assert hello["ok"] is True
        assert hello["capabilities"]["authorized"] is False
        assert tensors == []

        assert _wait_for(
            lambda: running.service.status()["clients"] == 0,
            timeout=idle_timeout * 6,
        )
        with pytest.raises(EOFError):
            recv_message(client)
    finally:
        client.close()
        running.close()


def test_shutdown_wakes_authorized_client_with_no_idle_deadline(tmp_path):
    running = RunningService(tmp_path, client_idle_timeout=0.05)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(1.0)
    try:
        client.connect(running.socket)
        send_message(client, {
            "op": "hello",
            "request_id": 1,
            "app_id": "ppocr-reader",
            "instance_id": "blocked-recognizer",
            "generation": 8,
        })
        hello, _ = recv_message(client)
        assert hello["ok"] is True
        assert hello["capabilities"]["authorized"] is True
        assert _wait_for(lambda: running.service.status()["clients"] == 1)
        with running.service._lock:
            connection_threads = list(running.service._threads)
        assert connection_threads

        started = time.monotonic()
        running.close()
        assert time.monotonic() - started < 1.0
        assert all(not thread.is_alive() for thread in connection_threads)
    finally:
        client.close()
        running.close()


def test_two_models_are_resident_and_inferred_through_one_backend(tmp_path):
    running = RunningService(tmp_path)
    first, first_sha = model(tmp_path, "first.rknn", b"first")
    second, second_sha = model(tmp_path, "second.rknn", b"second")
    try:
        a = RemoteRknnModel(
            str(first), socket_path=running.socket, model_sha256=first_sha, memory_mb=32
        )
        b = RemoteRknnModel(
            str(second), socket_path=running.socket, model_sha256=second_sha, memory_mb=32
        )
        np.testing.assert_array_equal(
            a.infer(np.zeros((2, 2, 3), dtype=np.uint8))[0],
            np.ones((1, 2, 2, 3), dtype=np.uint8),
        )
        np.testing.assert_array_equal(
            b.infer(np.ones((1, 1, 1, 1), dtype=np.uint8))[0],
            np.full((1, 1, 1, 1), 2, dtype=np.uint8),
        )
        status = running.service.status()
        assert len(status["models"]) == 2
        assert status["memory_reserved_mb"] == 66
        assert len(running.backend.loaded) == 2
        a.release()
        b.release()
        assert len(running.backend.released) == 2
    finally:
        running.close()


def test_remote_sequence_contract_preserves_rank3_float32_on_wire(tmp_path):
    class CapturingBackend(FakeBackend):
        def __init__(self):
            super().__init__()
            self.inputs = []

        def infer(self, handle, inputs):
            self.inputs.append([
                (tuple(value.shape), value.dtype.name) for value in inputs
            ])
            return super().infer(handle, inputs)

    backend = CapturingBackend()
    running = RunningService(tmp_path, backend=backend)
    path, digest = model(tmp_path, "sensevoice.rknn")
    spec = ModelSpec(
        str(path),
        name="sensevoice-encoder",
        inputs=(TensorSpec(
            "speech", (1, 344, 560), "float32", "NTF"),),
    )
    session = None
    try:
        session = RemoteRknnSession(
            spec,
            socket_path=running.socket,
            model_sha256=digest,
            memory_mb=32,
        )
        value = np.zeros((1, 344, 560), dtype=np.float32)
        output = session.infer(value)[0]
        assert backend.inputs == [[((1, 344, 560), "float32")]]
        assert output.shape == (1, 344, 560)
        assert output.dtype == np.float32
    finally:
        if session is not None:
            session.release()
        running.close()


def test_rknn_backend_reports_none_outputs_explicitly():
    class Coordinator:
        @contextmanager
        def hold(self):
            yield

    class Runtime:
        def inference(self, *, inputs):
            assert len(inputs) == 1
            return None

    backend = RknnBackend(coordinator=Coordinator())
    with pytest.raises(RuntimeError, match="RKNNLite.inference returned no outputs"):
        backend.infer(Runtime(), [np.zeros((1, 3, 4), dtype=np.float32)])


def test_identical_digest_and_spec_share_one_loaded_context(tmp_path):
    running = RunningService(tmp_path)
    path, digest = model(tmp_path)
    try:
        a = RemoteRknnSession(
            str(path), socket_path=running.socket, model_sha256=digest, memory_mb=40
        )
        b = RemoteRknnSession(
            str(path), socket_path=running.socket, model_sha256=digest, memory_mb=40
        )
        assert len(running.backend.loaded) == 1
        assert running.service.status()["models"][0]["references"] == 2
        a.release()
        assert running.backend.released == []
        b.release()
        assert running.backend.released == [str(path)]
    finally:
        running.close()


def test_revoked_client_can_release_own_shared_alias_without_restoring_access(
    tmp_path,
):
    class RevocableAuthorizer(_TestAuthorizer):
        def __init__(self, root):
            super().__init__(root)
            self.revoked = set()

        def validate(self, authorization):
            identity = (authorization.instance_id, authorization.generation)
            if identity in self.revoked:
                raise AuthorizationError("inference authorization has been revoked")

    authorizer = RevocableAuthorizer(tmp_path)
    running = RunningService(tmp_path, authorizer=authorizer)
    path, digest = model(tmp_path)
    try:
        revoked = RemoteRknnSession(
            str(path), socket_path=running.socket, model_sha256=digest,
            app_id="demo", instance_id="old-run", generation=1,
        )
        live = RemoteRknnSession(
            str(path), socket_path=running.socket, model_sha256=digest,
            app_id="demo", instance_id="new-run", generation=2,
        )
        revoked.infer(np.zeros((1, 1, 1, 1), dtype=np.uint8))
        live.infer(np.zeros((1, 1, 1, 1), dtype=np.uint8))
        before = running.service.status()["models"][0]
        assert before["references"] == 2
        assert before["calls"] == 2 and before["failures"] == 0

        authorizer.revoked.add(("old-run", 1))
        with pytest.raises(CapabilityError) as caught:
            revoked.infer(np.zeros((1, 1, 1, 1), dtype=np.uint8))
        assert caught.value.code == "unauthorized"

        revoked.release()
        after = running.service.status()["models"][0]
        assert after["references"] == 1
        assert after["calls"] == 2 and after["failures"] == 0
        assert running.backend.released == []
        # The unaffected generation remains fully usable; late cleanup did not
        # replace/re-publish any authorization or tear down its shared model.
        live.infer(np.zeros((1, 1, 1, 1), dtype=np.uint8))
        live.release()
        assert running.backend.released == [str(path)]
    finally:
        running.close()


def test_queued_inference_revalidates_after_driver_admission(tmp_path):
    class OrderedAuthorizer(_TestAuthorizer):
        def __init__(self, root):
            super().__init__(root)
            self.calls = 0
            self.revoked = False
            self.request_validated = threading.Event()
            self.execution_validated = threading.Event()

        def arm(self):
            self.calls = 0
            self.revoked = False
            self.request_validated.clear()
            self.execution_validated.clear()

        def validate(self, authorization):
            self.calls += 1
            if self.calls == 1:
                self.request_validated.set()
            elif self.calls == 2:
                self.execution_validated.set()
            if self.revoked:
                raise AuthorizationError("inference authorization has been revoked")

    authorizer = OrderedAuthorizer(tmp_path)
    running = RunningService(tmp_path, authorizer=authorizer)
    path, digest = model(tmp_path)
    session = RemoteRknnSession(
        str(path), socket_path=running.socket, model_sha256=digest,
        app_id="demo", instance_id="queued-run", generation=1,
    )
    result = {}

    def infer():
        try:
            session.infer(np.zeros((1, 1, 1, 1), dtype=np.uint8))
        except BaseException as exc:
            result["error"] = exc

    try:
        authorizer.arm()
        with running.service._driver_lock:
            thread = threading.Thread(target=infer, daemon=True)
            thread.start()
            assert authorizer.request_validated.wait(1.0)
            # The scheduler worker must not validate execution admission until
            # it owns the same lock that fences all vendor driver calls.
            assert not authorizer.execution_validated.wait(0.1)
            authorizer.revoked = True
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert isinstance(result.get("error"), CapabilityError)
        assert result["error"].code == "unauthorized"
        assert running.backend.calls == []
    finally:
        session.release()
        running.close()


def test_remote_release_converges_when_legacy_daemon_rejects_late_unload():
    class Socket:
        def __init__(self):
            self.closed = 0

        def close(self):
            self.closed += 1

    session = object.__new__(RemoteRknnSession)
    session._lock = threading.RLock()
    session._released = False
    session._transport_broken = False
    session._request_ids = iter([1])
    session._alias = "owned-alias"
    session._sock = Socket()
    exchange_calls = []

    def rejected(*_args, **_kwargs):
        exchange_calls.append(True)
        raise CapabilityError(
            "authorization revoked", operation="remote_model.release",
            code="unauthorized")

    session._exchange = rejected
    session.release()
    session.release()

    assert session.released is True
    assert exchange_calls == [True]
    assert session._sock.closed == 1


def test_relative_model_path_is_canonicalized_before_crossing_process(tmp_path,
                                                                      monkeypatch):
    """A managed app and inferenced never share a cwd contract.

    The client must put an absolute canonical path on the wire; resolving a
    relative app-bundled path in the daemon would otherwise turn
    ``models/x.rknn`` into ``/models/x.rknn`` when the service cwd is ``/``.
    """

    running = RunningService(tmp_path)
    path, digest = model(tmp_path, "relative.rknn", b"relative")
    monkeypatch.chdir(tmp_path)
    try:
        session = RemoteRknnSession(
            path.name,
            socket_path=running.socket,
            model_sha256=digest,
            memory_mb=8,
        )
        assert session.path == os.path.realpath(path)
        session.release()
    finally:
        running.close()


def test_memory_admission_fails_before_loading_second_model(tmp_path):
    running = RunningService(tmp_path, memory_mb=50)
    first, first_sha = model(tmp_path, "first.rknn", b"first")
    second, second_sha = model(tmp_path, "second.rknn", b"second")
    try:
        session = RemoteRknnSession(
            str(first), socket_path=running.socket, model_sha256=first_sha, memory_mb=40
        )
        with pytest.raises(ResourceBusyError) as caught:
            RemoteRknnSession(
                str(second),
                socket_path=running.socket,
                model_sha256=second_sha,
                memory_mb=40,
            )
        assert caught.value.code == "memory_budget_exceeded"
        assert running.backend.loaded == [str(first)]
        session.release()
    finally:
        running.close()


def test_queued_request_honours_deadline(tmp_path):
    backend = FakeBackend(infer_delay=0.25)
    running = RunningService(tmp_path, backend=backend)
    first, digest = model(tmp_path)
    try:
        a = RemoteRknnSession(
            str(first), socket_path=running.socket, model_sha256=digest, memory_mb=20
        )
        b = RemoteRknnSession(
            str(first), socket_path=running.socket, model_sha256=digest, memory_mb=20
        )
        active = threading.Thread(
            target=lambda: a.infer(np.zeros((1, 1, 1, 1), dtype=np.uint8)),
            daemon=True,
        )
        active.start()
        time.sleep(0.04)
        with pytest.raises(InferenceError) as caught:
            b.infer(np.zeros((1, 1, 1, 1), dtype=np.uint8), timeout=0.05)
        assert caught.value.code == "deadline_exceeded"
        active.join(timeout=2)
        a.release()
        b.release()
    finally:
        running.close()


def test_client_digest_and_resource_claims_cannot_change_platform_policy(tmp_path):
    running = RunningService(tmp_path)
    path, actual = model(tmp_path)
    try:
        session = RemoteRknnSession(
            str(path), socket_path=running.socket, model_sha256="0" * 64,
            memory_mb=1, priority=100, max_fps=999,
        )
        status = running.service.status()["models"]
        assert len(status) == 1
        assert status[0]["sha256"] == actual
        assert status[0]["memory_mb"] == 33
        assert status[0]["priority"] == 50
        assert status[0]["max_fps"] == 0.0
        session.release()
    finally:
        running.close()


def test_release_failure_keeps_model_reference_and_faults_service(tmp_path):
    class ReleaseFailBackend(FakeBackend):
        def release(self, handle):
            raise RuntimeError("destroy failed")

    running = RunningService(tmp_path, backend=ReleaseFailBackend())
    path, digest = model(tmp_path)
    try:
        session = RemoteRknnSession(
            str(path), socket_path=running.socket, model_sha256=digest, memory_mb=8
        )
        with pytest.raises(InferenceError, match="destroy failed"):
            session.release()
        for _ in range(100):
            if running.service.status()["state"] == "faulted":
                break
            time.sleep(0.01)
        status = running.service.status()
        assert status["state"] == "faulted"
        assert "destroy failed" in status["fault"]
        assert len(status["models"]) == 1
        assert status["models"][0]["references"] == 1
    finally:
        running.close()


def test_backend_quarantine_during_load_is_visible_in_status(tmp_path):
    class Coordinator:
        fault = None

    class QuarantinedLoadBackend(FakeBackend):
        coordinator = Coordinator()

        def load(self, path, spec):
            self.coordinator.fault = "rollback destroy failed"
            raise RuntimeError("load failed")

    running = RunningService(tmp_path, backend=QuarantinedLoadBackend())
    path, digest = model(tmp_path)
    try:
        with pytest.raises(InferenceError, match="load failed"):
            RemoteRknnSession(
                str(path), socket_path=running.socket,
                model_sha256=digest, memory_mb=8,
            )
        status = running.service.status()
        assert status["state"] == "faulted"
        assert "rollback destroy failed" in status["fault"]
    finally:
        running.close()


def test_shutdown_stops_releasing_after_first_native_failure(tmp_path):
    class ReleaseFailBackend(FakeBackend):
        def __init__(self):
            super().__init__()
            self.release_calls = 0

        def release(self, handle):
            self.release_calls += 1
            raise RuntimeError("native teardown uncertain")

    backend = ReleaseFailBackend()
    service = InferenceService(
        str(tmp_path / "unused.sock"), backend=backend,
        allowed_roots=[str(tmp_path)], memory_budget_mb=64,
    )
    for index in range(2):
        key = f"model-{index}"
        service._models[key] = _Model(
            key=key, path=key, sha256=str(index) * 64,
            memory_mb=8, priority=50, max_fps=0, spec_key="{}",
            handle={"path": key, "spec": {}},
        )

    service.close()

    assert backend.release_calls == 1
    assert service.status()["state"] == "stopping"
    assert "native teardown uncertain" in service.status()["fault"]


def test_managed_app_and_device_select_remote_service(tmp_path, monkeypatch):
    running = RunningService(tmp_path)
    path, digest = model(tmp_path)
    monkeypatch.setenv("RECAMERA_INFERENCE_SERVICE_SOCK", running.socket)
    try:
        app_model = App()._load_model(str(path))
        assert isinstance(app_model, RemoteRknnModel)
        np.testing.assert_array_equal(
            app_model.infer(np.zeros((1, 1, 3), dtype=np.uint8))[0],
            np.ones((1, 1, 1, 3), dtype=np.uint8),
        )

        device = Device(Capabilities())
        session = device.rknn_session(
            str(path), model_sha256=digest, memory_mb=8
        )
        assert isinstance(session, RemoteRknnSession)
        assert device.close().closed == 1
        app_model.release()
    finally:
        running.close()


@pytest.mark.parametrize("timeout", [True, 0, -1, float("nan"), 301, "bad"])
def test_remote_timeout_validation_does_not_desynchronise_connection(
    tmp_path, timeout
):
    running = RunningService(tmp_path)
    path, digest = model(tmp_path)
    try:
        session = RemoteRknnSession(
            str(path), socket_path=running.socket, model_sha256=digest, memory_mb=8
        )
        with pytest.raises(InferenceError):
            session.infer(np.zeros((1, 1, 1, 1), dtype=np.uint8), timeout=timeout)
        # Validation happens before writing a request.  A valid call on the same
        # framed connection must still work.
        assert session.infer(np.zeros((1, 1, 1, 1), dtype=np.uint8))[0].item() == 1
        session.release()
    finally:
        running.close()

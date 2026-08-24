"""End-to-end tests for appmgr -> SO_PEERCRED inference admission."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time

import numpy as np
import pytest

from kit.errors import CapabilityError
from kit.runtime.remote import RemoteRknnSession
from market.appmgr import paths
from market.appmgr.inference_auth import InferenceAuthorizationRegistry
from market.inferenced.authorization import (
    AuthorizationError,
    RegistryAuthorizer,
    estimate_model_memory_mb,
)
from market.inferenced.server import FakeBackend, InferenceService


def _manifest(model_bytes: bytes) -> dict:
    digest = hashlib.sha256(model_bytes).hexdigest()
    return {
        "manifest_version": 2,
        "id": "auth-demo",
        "name": "Authorization demo",
        "version": "1.0.0",
        "type": "self-hosted",
        "entry": "app.py",
        "release": {"sequence": 1, "channel": "stable"},
        "compatibility": {
            "platform_profile": "recamera-rv1126b-v1",
            "arch": "aarch64",
            "python": "==3.11.*",
            "kit_api": ">=0.2,<0.3",
        },
        "python": {
            "runtime_profile": "system-cp311-rknn232",
            "isolation": "per-release",
            "wheels": [],
            "imports": [],
        },
        "artifacts": [{
            "id": "detector",
            "kind": "rknn",
            "source": "bundled",
            "file": "models/model.rknn",
            "sha256": digest,
            "size": len(model_bytes),
            "mount": "models/model.rknn",
            "required": True,
            "share_scope": "content",
        }],
        "config_schema": {"revision": 1, "groups": []},
        "resources": {
            "claims": [{
                "name": "npu.rknn", "mode": "scheduled", "required": True,
            }],
            "limits": {"memory_mb": 128},
        },
        "permissions": {
            "sdk": ["npu.infer"],
            "filesystem": {"read": ["app", "artifacts"], "write": ["appdata", "tmp"]},
            "network": {"listen": [], "outbound": []},
        },
        "health": {
            "protocol": "kit-health-v1",
            "startup_timeout_sec": 30,
            "stabilization_sec": 0,
            "liveness_interval_sec": 10,
            "liveness_failures": 3,
            "restart": {
                "policy": "on-failure",
                "max_attempts": 3,
                "window_sec": 60,
                "backoff_sec": [1, 2, 5],
            },
        },
        "instances": {
            "max": 1,
            "config_scope": "app",
            "data_scope": "app",
            "endpoint_mode": "allocated",
        },
        "capabilities": [],
        "models": [{
            "id": "detector",
            "file": "models/model.rknn",
            "task": "detect",
            "input": [1, 1, 1, 1],
            "quant": "int8",
        }],
    }


@pytest.fixture
def installed(tmp_path, monkeypatch):
    apps = tmp_path / "apps"
    app = apps / "auth-demo"
    models = app / "models"
    models.mkdir(parents=True)
    raw = b"authenticated-rknn-model"
    model = models / "model.rknn"
    model.write_bytes(raw)
    manifest = _manifest(raw)
    (app / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (app / "app.py").write_text("# app\n", encoding="utf-8")
    monkeypatch.setattr(paths, "APPS_DIR", str(apps))
    registry_root = tmp_path / "authorizations"
    writer = InferenceAuthorizationRegistry(str(registry_root))
    policy = writer.prepare("auth-demo", manifest)
    return model, manifest, registry_root, writer, policy


def test_registry_binds_peer_generation_model_and_revocation(installed):
    model, _manifest_value, root, writer, policy = installed
    pid = os.getpid()
    writer.publish(policy, pid=pid, instance_id="run-one", generation=7)
    reader = RegistryAuthorizer(str(root), publish_wait=0)

    authorization = reader.authorize(
        peer_pid=pid,
        peer_uid=os.geteuid(),
        peer_gid=os.getegid(),
        claimed_app="auth-demo",
        claimed_instance="run-one",
        claimed_generation=7,
    )
    trusted_model = reader.authorize_model(authorization, str(model))

    assert trusted_model.sha256 == hashlib.sha256(model.read_bytes()).hexdigest()
    assert trusted_model.memory_mb == estimate_model_memory_mb(model.stat().st_size)
    assert trusted_model.priority == 50
    assert trusted_model.max_fps == 0.0
    record = root / f"{pid}.json"
    original_inode = record.stat().st_ino
    writer.publish(policy, pid=pid, instance_id="run-one", generation=7)
    assert record.stat().st_ino == original_inode
    reader.validate(authorization)
    with pytest.raises(AuthorizationError, match="authorized generation"):
        reader.authorize(
            peer_pid=pid,
            peer_uid=os.geteuid(),
            peer_gid=os.getegid(),
            claimed_app="auth-demo",
            claimed_instance="run-one",
            claimed_generation=8,
        )

    assert writer.revoke(
        pid, app_id="auth-demo", instance_id="run-one", generation=7
    ) is True
    with pytest.raises(AuthorizationError, match="revoked"):
        reader.validate(authorization)


def test_remote_retries_only_pending_publication_and_server_ignores_forged_policy(
    installed, tmp_path
):
    model, _manifest_value, root, writer, policy = installed
    first_authorize = threading.Event()

    class SignallingAuthorizer(RegistryAuthorizer):
        validations = 0

        def authorize(self, **claims):
            first_authorize.set()
            return super().authorize(**claims)

        def validate(self, authorization):
            self.validations += 1
            return super().validate(authorization)

    backend = FakeBackend()
    socket_path = str(tmp_path / "inferenced.sock")
    service = InferenceService(
        socket_path,
        backend=backend,
        memory_budget_mb=128,
        authorizer=SignallingAuthorizer(str(root), publish_wait=0),
    )
    server_thread = threading.Thread(target=service.serve_forever, daemon=True)
    server_thread.start()
    for _ in range(100):
        if os.path.exists(socket_path):
            break
        time.sleep(0.01)

    result = {}

    def connect_before_publish():
        try:
            result["session"] = RemoteRknnSession(
                str(model),
                socket_path=socket_path,
                app_id="auth-demo",
                instance_id="run-race",
                generation=9,
                connect_timeout=2.0,
                model_sha256="0" * 64,
                memory_mb=1,
                priority=100,
                max_fps=999,
            )
        except BaseException as exc:  # surfaced in the parent test thread
            result["error"] = exc

    client_thread = threading.Thread(target=connect_before_publish)
    client_thread.start()
    assert first_authorize.wait(1.0), "client did not reach hello before publish"
    writer.publish(policy, pid=os.getpid(), instance_id="run-race", generation=9)
    client_thread.join(timeout=3.0)

    try:
        assert not client_thread.is_alive()
        assert "error" not in result
        session = result["session"]
        status = service.status()["models"]
        assert len(status) == 1
        assert status[0]["sha256"] == policy["models"][0]["sha256"]
        assert status[0]["memory_mb"] == policy["models"][0]["memory_mb"]
        assert status[0]["priority"] == policy["models"][0]["priority"] == 50
        assert status[0]["max_fps"] == policy["models"][0]["max_fps"] == 0.0
        assert backend.loaded == [str(model)]
        output = session.infer(np.zeros((1, 1, 1, 1), dtype=np.uint8))[0]
        assert output.item() == 1

        writer.revoke(
            os.getpid(), app_id="auth-demo", instance_id="run-race", generation=9
        )
        with pytest.raises(CapabilityError) as caught:
            session.infer(np.zeros((1, 1, 1, 1), dtype=np.uint8))
        assert caught.value.code == "unauthorized"
        validations_after_rejected_infer = service.authorizer.validations
        # Revocation fences capability-bearing work but must not strand model
        # references during cooperative teardown.  A late unload only removes
        # this already-established client's alias and never republishes auth.
        session.release()
        session.release()  # local + protocol release are both idempotent
        assert service.authorizer.validations == validations_after_rejected_infer
        assert backend.released == [str(model)]
        assert service.status()["models"] == []
        assert not (root / f"{os.getpid()}.json").exists()

        with pytest.raises(CapabilityError) as caught:
            RemoteRknnSession(
                str(model), socket_path=socket_path,
                app_id="auth-demo", instance_id="run-race", generation=9,
                connect_timeout=0.1,
            )
        assert caught.value.code in {"authorization_pending", "unauthorized"}
    finally:
        service.close()
        server_thread.join(timeout=2.0)


def test_generation_spoof_fails_immediately_and_model_tamper_never_loads(
    installed, tmp_path
):
    model, _manifest_value, root, writer, policy = installed
    writer.publish(policy, pid=os.getpid(), instance_id="real-run", generation=3)
    backend = FakeBackend()
    socket_path = str(tmp_path / "inferenced.sock")
    service = InferenceService(
        socket_path,
        backend=backend,
        authorizer=RegistryAuthorizer(str(root), publish_wait=0),
    )
    server_thread = threading.Thread(target=service.serve_forever, daemon=True)
    server_thread.start()
    for _ in range(100):
        if os.path.exists(socket_path):
            break
        time.sleep(0.01)
    try:
        with pytest.raises(CapabilityError) as caught:
            RemoteRknnSession(
                str(model), socket_path=socket_path,
                app_id="auth-demo", instance_id="real-run", generation=4,
                connect_timeout=1.0,
            )
        assert caught.value.code == "unauthorized"

        model.write_bytes(b"tampered-after-appmgr-admission")
        with pytest.raises(CapabilityError) as caught:
            RemoteRknnSession(
                str(model), socket_path=socket_path,
                app_id="auth-demo", instance_id="real-run", generation=3,
                connect_timeout=1.0,
            )
        assert caught.value.code == "unauthorized"
        assert backend.loaded == []
    finally:
        service.close()
        server_thread.join(timeout=2.0)

import http.client
import io
import json
import os
import sys
import threading
import time
from contextlib import contextmanager

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
from appmgr import (operations, paths, resources, server, state, trust, uploads,
                    visualization)


def _multipart(package: bytes, *, boundary="bounded-test", signature=None,
               signature_filename="demo.tar.gz.sig"):
    parts = [
        b"--" + boundary.encode() + b"\r\n",
        b'Content-Disposition: form-data; name="package"; filename="demo.tar.gz"\r\n',
        b"Content-Type: application/gzip\r\n\r\n",
        package,
        b"\r\n",
    ]
    if signature is not None:
        parts += [
            b"--" + boundary.encode() + b"\r\n",
            ('Content-Disposition: form-data; name="signature"; filename="%s"\r\n\r\n'
             % signature_filename).encode("ascii"),
            signature,
            b"\r\n",
        ]
    parts += [b"--" + boundary.encode() + b"--\r\n"]
    return b"".join(parts), "multipart/form-data; boundary=" + boundary


def _wait_operation(operation_id: str, *, timeout: float = 3.0) -> dict:
    deadline = time.monotonic() + timeout
    current = None
    while time.monotonic() < deadline:
        current = next(
            item for item in server._operation_manager().list()
            if item["id"] == operation_id)
        if current["status"] in operations.TERMINAL:
            return current
        time.sleep(0.005)
    pytest.fail("operation did not finish: %r" % current)


def _observe_operation_worker_lock_contention(monkeypatch) -> threading.Event:
    """Signal only after the async worker really loses a non-blocking flock."""
    contended = threading.Event()
    real_flock = server.fcntl.flock

    def observed_flock(fileobj, operation):
        try:
            return real_flock(fileobj, operation)
        except OSError:
            if (threading.current_thread().name == "appmgr-operations"
                    and operation & server.fcntl.LOCK_NB):
                contended.set()
            raise

    monkeypatch.setattr(server.fcntl, "flock", observed_flock)
    return contended


def _observe_busy_gate_contention(monkeypatch) -> threading.Event:
    """Signal when any request thread reaches an already-held busy gate."""
    contended = threading.Event()
    real_flock = server.fcntl.flock

    def observed_flock(fileobj, operation):
        try:
            return real_flock(fileobj, operation)
        except OSError:
            if operation & server.fcntl.LOCK_NB:
                contended.set()
            raise

    monkeypatch.setattr(server.fcntl, "flock", observed_flock)
    return contended


def _json_request(httpd, method: str, path: str, payload: dict):
    connection = http.client.HTTPConnection(
        "127.0.0.1", httpd.server_port, timeout=5)
    try:
        connection.request(
            method, path, body=json.dumps(payload),
            headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


@pytest.fixture
def layout(tmp_path, monkeypatch):
    apps = tmp_path / "apps"
    appmgr = tmp_path / "appmgr"
    stage = tmp_path / "stage"
    apps.mkdir(); appmgr.mkdir(); stage.mkdir()
    monkeypatch.setattr(paths, "APPS_DIR", str(apps))
    monkeypatch.setattr(paths, "APPMGR_DIR", str(appmgr))
    monkeypatch.setattr(paths, "APPSTAGE_DIR", str(stage))
    monkeypatch.setattr(paths, "STATE_FILE", str(apps / "state.json"))
    monkeypatch.setattr(paths, "MAX_PKG_BYTES", 1024 * 1024)
    monkeypatch.setattr(paths, "MAX_UPLOAD_STAGING_BYTES", 4 * 1024 * 1024)
    monkeypatch.setattr(paths, "MAX_STAGED_UPLOADS", 8)
    monkeypatch.setattr(paths, "UPLOAD_TTL_SEC", 3600)
    monkeypatch.setattr(paths, "MIN_UPLOAD_FREE_BYTES", 0)
    if server._operation_manager_instance is not None:
        server._operation_manager_instance.close()
    monkeypatch.setattr(server, "_operation_manager_instance", None)
    monkeypatch.setattr(server, "_operation_manager_layout", None)
    monkeypatch.setattr(server, "_coordinator_instance", None)
    monkeypatch.setattr(server, "_coordinator_layout", None)
    yield tmp_path
    if server._operation_manager_instance is not None:
        server._operation_manager_instance.close()
        server._operation_manager_instance = None


def test_multipart_package_is_streamed_in_bounded_chunks(layout):
    payload = os.urandom(300_000)
    body, content_type = _multipart(payload, signature=b"c2lnbmF0dXJl")

    class Guarded(io.BytesIO):
        largest = 0

        def read(self, size=-1):
            assert 0 < size <= uploads.CHUNK
            self.largest = max(self.largest, size)
            return super().read(size)

    source = Guarded(body)
    record = uploads.receive(source, len(body), content_type)

    assert source.largest <= uploads.CHUNK
    assert record["size"] == len(payload)
    assert record["signature"] == "c2lnbmF0dXJl"
    with open(record["package_path"], "rb") as package:
        assert package.read() == payload


def test_multipart_rejects_signature_for_a_different_package(layout):
    body, content_type = _multipart(
        b"package", signature=b"c2lnbmF0dXJl",
        signature_filename="other.tar.gz.sig")

    with pytest.raises(
            uploads.MultipartError,
            match="signature filename must match package filename"):
        uploads.receive(io.BytesIO(body), len(body), content_type)


def test_multipart_type_and_declared_size_fail_before_staging(layout):
    body, content_type = _multipart(b"package")
    with pytest.raises(uploads.MultipartError, match="multipart/form-data"):
        uploads.receive(io.BytesIO(body), len(body), "application/json")
    with pytest.raises(uploads.MultipartError, match="too large"):
        uploads.receive(
            io.BytesIO(b""),
            paths.MAX_PKG_BYTES + uploads.MAX_MULTIPART_OVERHEAD + 1,
            content_type,
        )
    assert not os.path.exists(paths.uploads_dir())


def test_v1_policy_reports_live_package_and_signature_limits(layout, monkeypatch):
    monkeypatch.setattr(paths, "MAX_PKG_BYTES", 123_456)
    monkeypatch.setattr(paths, "MAX_UNPACKED_BYTES", 654_321)
    monkeypatch.setattr(paths, "MAX_MEMBERS", 321)
    monkeypatch.setattr(paths, "MAX_UPLOAD_STAGING_BYTES", 777_777)
    monkeypatch.setattr(paths, "MAX_STAGED_UPLOADS", 5)
    monkeypatch.setattr(paths, "MIN_UPLOAD_FREE_BYTES", 44_444)
    monkeypatch.setattr(paths, "UPLOAD_TTL_SEC", 678)
    monkeypatch.setattr(paths, "REQUIRE_SIGNATURE", True)
    monkeypatch.setattr(paths, "DEVELOPER_MODE_ALLOWED", False)
    monkeypatch.setattr(paths, "MAX_OWNER_KEYS", 7)
    monkeypatch.setattr(paths, "MAX_TRUST_KEY_BYTES", 8192)

    policy = server.do_v1_policy()

    assert policy["manifest"] == {"required_version": 2}
    assert policy["upload"] == {
        "package_field": "package",
        "signature_field": "signature",
        "filename_pattern": uploads.PACKAGE_FILENAME_PATTERN,
        "max_package_bytes": 123_456,
        "max_request_bytes": 123_456 + uploads.MAX_MULTIPART_OVERHEAD,
        "max_signature_bytes": uploads.MAX_SIGNATURE_BYTES,
        "max_unpacked_bytes": 654_321,
        "max_members": 321,
        "max_staging_bytes": 777_777,
        "max_staged_uploads": 5,
        "min_free_bytes": 44_444,
        "ttl_sec": 678,
    }
    assert policy["signature"] == {
        "algorithm": "ecdsa-sha256",
        "encoding": "base64-der",
        "required": True,
        "invalid_signatures_rejected": True,
        "local_web_unsigned": {
            "allowed": True,
            "source": "local-web",
            "channel": "app-center-v1-same-origin",
            "requires_explicit_confirmation": True,
            "confirmation_field": "unsigned_risk_confirmed",
            "auto_start": False,
            "warning_code": "unsigned-root-code",
        },
        "owner_keys": {
            "management_enabled": True,
            "format": "pem",
            "curve": "P-256",
            "max_keys": 7,
            "max_key_bytes": 8192,
            "explicit_confirmation_required": True,
        },
    }

    # The historic global switch remains available only to legacy tooling. The
    # v1 direct/cloud baseline stays signed-only and the local exception is
    # reported separately without exposing a developer-mode policy field.
    monkeypatch.setattr(paths, "REQUIRE_SIGNATURE", False)
    relaxed = server.do_v1_policy()
    assert relaxed["signature"]["required"] is True
    assert "developer_mode_allowed" not in relaxed["signature"]

    monkeypatch.setattr(paths, "REQUIRE_SIGNATURE", True)
    monkeypatch.setattr(paths, "DEVELOPER_MODE_ALLOWED", True)
    explicit_developer = server.do_v1_policy()
    assert explicit_developer["signature"]["required"] is True
    assert "developer_mode_allowed" not in explicit_developer["signature"]


def test_resources_api_exposes_start_admission_telemetry(layout, monkeypatch):
    coordinator = server._coordinator()
    runtime = {
        "sample": {
            "mem_available_mb": 768,
            "storage_free_mb": 4096,
            "temperature_c": 61.5,
        },
        "error": None,
        "policy": {
            "start_max_temp_c": 100.0,
            "runtime_hard_temp_c": 110.0,
        },
    }
    monkeypatch.setattr(
        coordinator.resources, "runtime_status", lambda: runtime)
    monkeypatch.setattr(
        coordinator, "inference_status",
        lambda: {"available": True, "socket": "/tmp/inferenced.sock"})

    result = server.do_resources()

    assert result["runtime_admission"] == runtime
    assert result["inference_service"]["available"] is True


def test_v1_trust_mutations_require_confirmation_gate_audit_and_event(
        layout, monkeypatch):
    fingerprint = "sha256:" + "1" * 64
    key = {
        "kind": "owner", "name": "lab.pem", "label": "lab",
        "fingerprint": fingerprint, "algorithm": "ecdsa-sha256",
        "removable": True,
    }
    installs = []
    removals = []
    gates = []
    audits = []

    @contextmanager
    def observed_gate(*, wait_timeout=0.0, retry_interval=None):
        gates.append(wait_timeout)
        yield

    monkeypatch.setattr(server, "busy_gate", observed_gate)
    monkeypatch.setattr(
        server.apptrust, "install_owner_key",
        lambda label, public_key: installs.append((label, public_key)) or {
            "key": key, "created": True})
    monkeypatch.setattr(
        server.apptrust, "remove_owner_key",
        lambda supplied: removals.append(supplied) or {
            "fingerprint": supplied, "deleted": 1})
    monkeypatch.setattr(
        server, "_audit",
        lambda action, **fields: audits.append((action, fields)))
    subscription = server._operation_manager().events.subscribe()
    try:
        with pytest.raises(ValueError, match="explicitly confirmed"):
            server.do_v1_install_owner_key({
                "label": "lab", "public_key": "PEM", "confirm_trust": False,
            })
        assert installs == []

        installed = server.do_v1_install_owner_key({
            "label": "lab", "public_key": "PEM", "confirm_trust": True,
        })
        install_event = subscription.get(timeout=1)
        removed = server.do_v1_remove_owner_key("2" * 64)
        remove_event = subscription.get(timeout=1)
    finally:
        server._operation_manager().events.unsubscribe(subscription)

    assert installed == {"key": key, "created": True}
    assert removed == {"fingerprint": "sha256:" + "2" * 64, "deleted": 1}
    assert installs == [("lab", "PEM")]
    assert removals == ["sha256:" + "2" * 64]
    assert gates == [paths.V1_OPERATION_BUSY_TIMEOUT_SEC] * 2
    assert install_event["type"] == "trust"
    assert install_event["action"] == "owner-installed"
    assert install_event["key"] == key
    assert remove_event["type"] == "trust"
    assert remove_event["action"] == "owner-deleted"
    assert [item[0] for item in audits] == [
        "v1_trust_owner_install", "v1_trust_owner_delete"]
    assert all("PEM" not in repr(item) for item in audits)


def test_http_v1_trust_contract_origin_and_error_mapping(layout, monkeypatch):
    vendor = {
        "kind": "vendor", "name": "release_pub.pem",
        "fingerprint": "sha256:" + "a" * 64,
        "algorithm": "ecdsa-sha256", "removable": False,
    }
    owner_fingerprint = "sha256:" + "1" * 64
    owner = {
        "kind": "owner", "name": "lab.pem", "label": "lab",
        "fingerprint": owner_fingerprint,
        "algorithm": "ecdsa-sha256", "removable": True,
    }
    install_calls = []
    installed = set()
    remove_calls = []

    monkeypatch.setattr(server.apptrust, "list_trust", lambda: [vendor])
    monkeypatch.setattr(paths, "MAX_OWNER_KEYS", 7)
    monkeypatch.setattr(paths, "MAX_TRUST_KEY_BYTES", 8192)

    def install(label, public_key):
        install_calls.append((label, public_key))
        if public_key == "invalid":
            raise trust.TrustValidationError("invalid owner public key")
        if label == "vendor-shadow":
            raise trust.ImmutableTrustAnchorError("immutable vendor anchor")
        created = public_key not in installed
        installed.add(public_key)
        return {"key": owner, "created": created}

    def remove(fingerprint):
        remove_calls.append(fingerprint)
        if fingerprint.endswith("0" * 64):
            raise trust.TrustNotFoundError("owner key not found")
        if fingerprint.endswith("f" * 64):
            raise trust.ImmutableTrustAnchorError("immutable vendor anchor")
        return {"fingerprint": fingerprint, "deleted": 1}

    monkeypatch.setattr(server.apptrust, "install_owner_key", install)
    monkeypatch.setattr(server.apptrust, "remove_owner_key", remove)
    monkeypatch.setattr(server, "_audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        server._operation_manager().events, "publish",
        lambda kind, **payload: {"type": kind, **payload})

    httpd = server._AppHTTPServer(("127.0.0.1", 0), server._Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    def request(method, path, body=None, *, origin=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", httpd.server_port, timeout=5)
        headers = {"Host": "camera.local", "X-Forwarded-Proto": "https"}
        if body is not None:
            body = json.dumps(body)
            headers["Content-Type"] = "application/json"
        if origin is not None:
            headers["Origin"] = origin
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    try:
        status, payload = request("GET", "/api/app-center/v1/trust")
        assert status == 200
        assert payload == {
            "keys": [vendor],
            "limits": {
                "max_owner_keys": 7,
                "max_trust_key_bytes": 8192,
            },
        }

        body = {"label": "lab", "public_key": "PEM", "confirm_trust": True}
        status, payload = request(
            "POST", "/api/app-center/v1/trust/owners", body,
            origin="https://evil.local")
        assert status == 403
        assert "cross-origin" in payload["error"]
        assert install_calls == []

        status, payload = request(
            "POST", "/api/app-center/v1/trust/owners",
            {**body, "confirm_trust": False}, origin="https://camera.local")
        assert status == 400
        assert "explicitly confirmed" in payload["error"]

        status, payload = request(
            "POST", "/api/app-center/v1/trust/owners", body,
            origin="https://camera.local")
        assert status == 201
        assert payload == {"key": owner, "created": True}

        status, payload = request(
            "POST", "/api/app-center/v1/trust/owners", body,
            origin="https://camera.local")
        assert status == 200
        assert payload == {"key": owner, "created": False}

        status, payload = request(
            "POST", "/api/app-center/v1/trust/owners",
            {**body, "public_key": "invalid"}, origin="https://camera.local")
        assert status == 400
        assert payload["error"] == "invalid owner public key"

        status, payload = request(
            "POST", "/api/app-center/v1/trust/owners",
            {**body, "label": "vendor-shadow"},
            origin="https://camera.local")
        assert status == 409
        assert "immutable vendor" in payload["error"]

        digest = "1" * 64
        status, payload = request(
            "DELETE", "/api/app-center/v1/trust/owners/" + digest.upper(),
            origin="https://camera.local")
        assert status == 200
        assert payload == {"fingerprint": owner_fingerprint, "deleted": 1}
        assert remove_calls[-1] == owner_fingerprint

        status, payload = request(
            "DELETE", "/api/app-center/v1/trust/owners/" + "0" * 64,
            origin="https://camera.local")
        assert status == 404
        assert "not found" in payload["error"]

        status, payload = request(
            "DELETE", "/api/app-center/v1/trust/owners/" + "f" * 64,
            origin="https://camera.local")
        assert status == 409
        assert "immutable vendor" in payload["error"]

        status, payload = request(
            "DELETE", "/api/app-center/v1/trust/owners/not-a-fingerprint",
            origin="https://camera.local")
        assert status == 404
        assert payload == {"error": "not found"}

        before = list(remove_calls)
        status, payload = request(
            "DELETE", "/api/app-center/v1/trust/owners/" + digest,
            origin="https://evil.local")
        assert status == 403
        assert "cross-origin" in payload["error"]
        assert remove_calls == before
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("status", [
    "uploaded", "preflighted", "rejected", "failed", "installed",
])
def test_cancel_upload_is_idempotent_for_every_inactive_state(layout, status):
    body, content_type = _multipart(b"inactive-package")
    record = uploads.receive(io.BytesIO(body), len(body), content_type)
    if status != "uploaded":
        uploads.update(record["upload_id"], status=status)

    deleted = server.do_v1_cancel_upload(record["upload_id"])
    assert deleted == {
        "upload_id": record["upload_id"],
        "deleted": True,
        "state": "deleted",
        "previous_status": status,
    }
    assert not os.path.lexists(os.path.dirname(record["package_path"]))

    repeated = server.do_v1_cancel_upload(record["upload_id"])
    assert repeated == {
        "upload_id": record["upload_id"],
        "deleted": False,
        "state": "absent",
    }
    unknown = server.do_v1_cancel_upload("f" * 32)
    assert unknown == {
        "upload_id": "f" * 32,
        "deleted": False,
        "state": "absent",
    }


@pytest.mark.parametrize("status", ["install_queued", "installing"])
def test_cancel_upload_rejects_active_states_without_removing_bytes(layout, status):
    body, content_type = _multipart(b"active-package")
    record = uploads.receive(io.BytesIO(body), len(body), content_type)
    uploads.update(record["upload_id"], status=status)

    with pytest.raises(uploads.UploadConflictError, match="upload is active"):
        server.do_v1_cancel_upload(record["upload_id"])

    assert uploads.load(record["upload_id"])["status"] == status
    with open(record["package_path"], "rb") as package:
        assert package.read() == b"active-package"


def test_cancel_upload_fails_closed_for_invalid_or_unknown_state(layout):
    with pytest.raises(ValueError, match="invalid upload_id"):
        server.do_v1_cancel_upload("../not-an-upload")

    body, content_type = _multipart(b"unknown-state-package")
    record = uploads.receive(io.BytesIO(body), len(body), content_type)
    uploads.update(record["upload_id"], status="future-active-state")
    with pytest.raises(uploads.UploadConflictError, match="unknown state"):
        server.do_v1_cancel_upload(record["upload_id"])
    assert os.path.isfile(record["package_path"])


def test_cancel_cannot_race_preflight_to_install_queued_transition(
        layout, monkeypatch):
    permissions = {
        "sdk": [],
        "filesystem": {"read": [], "write": []},
        "network": {"listen": [], "outbound": []},
        "devices": [],
    }
    body, content_type = _multipart(b"finalize-race-package")
    record = uploads.receive(io.BytesIO(body), len(body), content_type)
    uploads.update(record["upload_id"], status="preflighted", preflight={
        "manifest": {
            "manifest_version": 2,
            "id": "demo",
            "permissions": permissions,
        },
        "release_id": "demo-r1",
        "signature": {"status": "verified"},
        "install_context": {
            "mode": "new",
            "target_version": None,
            "target_release_id": "demo-r1",
            "confirmation_required": [],
        },
    })

    submit_entered = threading.Event()
    release_submit = threading.Event()

    class HeldSubmitManager:
        def for_upload(self, _upload_id):
            return None

        def submit(self, operation_type, app_id, callback, **correlation):
            assert (operation_type, app_id) == ("install", "demo")
            assert correlation["upload_id"] == record["upload_id"]
            assert len(correlation["request_fingerprint"]) == 64
            submit_entered.set()
            assert release_submit.wait(timeout=2)
            return {"id": "held-op", "type": operation_type, "app_id": app_id}

    monkeypatch.setattr(server, "_operation_manager", lambda: HeldSubmitManager())
    finalize_result = []
    finalize_error = []
    cancel_result = []
    cancel_error = []

    def finalize():
        try:
            finalize_result.append(server.do_v1_install({
                "upload_id": record["upload_id"],
                "permissions_confirmed": True,
                "permissions": permissions,
                "developer_mode": False,
            }))
        except Exception as exc:
            finalize_error.append(exc)

    def cancel():
        try:
            cancel_result.append(server.do_v1_cancel_upload(record["upload_id"]))
        except Exception as exc:
            cancel_error.append(exc)

    finalize_thread = threading.Thread(target=finalize)
    cancel_thread = threading.Thread(target=cancel)
    finalize_thread.start()
    assert submit_entered.wait(timeout=2)
    cancel_thread.start()
    try:
        # Finalize owns _upload_finalize_lock while submit is held, so delete
        # cannot observe the earlier preflighted state and remove its bytes.
        cancel_thread.join(timeout=0.05)
        assert cancel_thread.is_alive()
    finally:
        release_submit.set()
        finalize_thread.join(timeout=2)
        cancel_thread.join(timeout=2)

    assert not finalize_error
    assert finalize_result == [{
        "operation": {"id": "held-op", "type": "install", "app_id": "demo"},
    }]
    assert not cancel_result
    assert len(cancel_error) == 1
    assert isinstance(cancel_error[0], uploads.UploadConflictError)
    assert uploads.load(record["upload_id"])["status"] == "install_queued"
    assert os.path.isfile(record["package_path"])


def test_upload_staging_has_aggregate_quota_count_and_ttl_gc(layout, monkeypatch):
    body, content_type = _multipart(b"first-package")
    first = uploads.receive(io.BytesIO(body), len(body), content_type)

    monkeypatch.setattr(paths, "MAX_STAGED_UPLOADS", 1)
    with pytest.raises(uploads.StagingQuotaError, match="count limit"):
        uploads.receive(io.BytesIO(body), len(body), content_type)

    monkeypatch.setattr(paths, "MAX_STAGED_UPLOADS", 8)
    used = uploads._tree_size(paths.uploads_dir())
    monkeypatch.setattr(paths, "MAX_UPLOAD_STAGING_BYTES", used + len(body) - 1)
    with pytest.raises(uploads.StagingQuotaError, match="byte limit"):
        uploads.receive(io.BytesIO(body), len(body), content_type)

    uploads.update(first["upload_id"], created_at=1.0)
    monkeypatch.setattr(paths, "UPLOAD_TTL_SEC", 10)
    removed = uploads.gc_expired(now=time.time() + 20.0)
    assert first["upload_id"] in removed
    assert not os.path.exists(os.path.dirname(first["package_path"]))


def test_runtime_gc_preserves_active_install_but_startup_gc_reclaims_it(
        layout, monkeypatch):
    body, content_type = _multipart(b"active-package")
    record = uploads.receive(io.BytesIO(body), len(body), content_type)
    uploads.update(record["upload_id"], status="installing", created_at=1.0)
    monkeypatch.setattr(paths, "UPLOAD_TTL_SEC", 10)

    future = time.time() + 20.0
    assert uploads.gc_expired(now=future) == []
    assert os.path.exists(record["package_path"])
    assert record["upload_id"] in uploads.gc_expired(
        now=future, include_active=True)
    assert not os.path.exists(os.path.dirname(record["package_path"]))


def test_rejected_preflight_removes_uploaded_bytes(layout, monkeypatch):
    body, content_type = _multipart(b"invalid-package")
    monkeypatch.setattr(
        server.installer, "inspect",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            server.installer.InstallError("bad package")))

    with pytest.raises(server.installer.InstallError, match="bad package"):
        server.do_v1_upload(io.BytesIO(body), len(body), content_type)

    assert os.listdir(paths.uploads_dir()) == []


def test_unsigned_preflight_is_local_only_deferred_and_risk_confirmed(
        layout, monkeypatch):
    permissions = {
        "sdk": [],
        "filesystem": {"read": [], "write": []},
        "network": {"listen": [], "outbound": []},
        "devices": [],
    }
    manifest = {
        "manifest_version": 2,
        "id": "unsigned-demo",
        "name": "Unsigned Demo",
        "version": "1.0.0",
        "entry": "app.py",
        "permissions": permissions,
        "resources": {"claims": []},
        "instances": {"max": 1},
        "config_schema": {"groups": []},
        "python": {"wheels": []},
    }
    inspected = {
        "id": "unsigned-demo",
        "manifest": manifest,
        "signature": {
            "signed": False,
            "verified": False,
            "alg": "ecdsa-sha256",
            "detail": "package is unsigned",
        },
        "preflight": {"release_id": "unsigned-demo-r1"},
    }
    allow_unsigned_calls = []

    def inspect(*_args, allow_unsigned=False, **_kwargs):
        allow_unsigned_calls.append(allow_unsigned)
        if not allow_unsigned and paths.REQUIRE_SIGNATURE:
            raise server.installer.InstallError("package signature is required")
        return inspected

    monkeypatch.setattr(paths, "REQUIRE_SIGNATURE", True)
    monkeypatch.setattr(paths, "DEVELOPER_MODE_ALLOWED", False)
    monkeypatch.setattr(server.installer, "inspect", inspect)
    body, content_type = _multipart(b"unsigned-package")

    # Direct/API callers remain signed-only even if they try the same v1 API.
    with pytest.raises(
            server.installer.InstallError, match="signature is required"):
        server.do_v1_upload(io.BytesIO(body), len(body), content_type)
    assert allow_unsigned_calls == [False]
    assert os.listdir(paths.uploads_dir()) == []

    # The v1 provenance rule remains strict even if a legacy migration image
    # disables the installer's global signature switch.
    monkeypatch.setattr(paths, "REQUIRE_SIGNATURE", False)
    with pytest.raises(server.installer.InstallError, match="direct/cloud"):
        server.do_v1_upload(io.BytesIO(body), len(body), content_type)
    assert allow_unsigned_calls == [False, False]
    assert os.listdir(paths.uploads_dir()) == []

    # Live resource state must not be consulted by package preflight.
    monkeypatch.setattr(
        server, "_coordinator",
        lambda: pytest.fail("preflight must not query live resource occupancy"))
    monkeypatch.setattr(
        server.appconfig, "effective_values",
        lambda *_args, **_kwargs: pytest.fail(
            "preflight must not read/migrate a stale installed config overlay"))
    uploaded = server.do_v1_upload(
        io.BytesIO(body), len(body), content_type,
        source=server.V1_LOCAL_UPLOAD_SOURCE,
        channel=server.V1_LOCAL_UPLOAD_CHANNEL)
    preflight = uploaded["preflight"]
    assert allow_unsigned_calls == [False, False, True]
    assert preflight["source"] == server.V1_LOCAL_UPLOAD_SOURCE
    assert preflight["channel"] == server.V1_LOCAL_UPLOAD_CHANNEL
    assert preflight["signature"]["status"] == "unsigned"
    assert preflight["signature"]["unsigned_install_allowed"] is True
    assert "developer_mode_allowed" not in preflight
    assert "developer_mode_allowed" not in preflight["signature"]
    assert preflight["conflicts"] == []
    assert preflight["start_admission"]["enforced_on"] == "start"
    assert preflight["start_admission"]["live_resources"] == "deferred"
    assert preflight["start_admission"]["dependencies"] == "deferred"
    assert preflight["warnings"][0]["code"] == "unsigned-root-code"
    assert preflight["warnings"][0]["severity"] == "critical"
    assert preflight["unsigned_confirmation"] == {
        "required": True,
        "fields": ["unsigned_risk_confirmed"],
        "confirmation_field": "unsigned_risk_confirmed",
        "expected": True,
        "auto_start": False,
    }
    stored = uploads.load(uploaded["upload_id"])
    assert stored["source"] == server.V1_LOCAL_UPLOAD_SOURCE
    assert stored["channel"] == server.V1_LOCAL_UPLOAD_CHANNEL

    base_finalize = {
        "upload_id": uploaded["upload_id"],
        "permissions_confirmed": True,
        "permissions": permissions,
        "developer_mode": True,
    }
    with pytest.raises(ValueError, match="risk must be explicitly confirmed"):
        server.do_v1_install(base_finalize)
    with pytest.raises(ValueError, match="server-assigned"):
        server.do_v1_install({
            **base_finalize,
            "unsigned_risk_confirmed": True,
            "source": server.V1_LOCAL_UPLOAD_SOURCE,
            "channel": server.V1_LOCAL_UPLOAD_CHANNEL,
        })


def test_bad_signature_is_rejected_even_on_local_unsigned_route(
        layout, monkeypatch):
    def reject_bad_signature(*_args, allow_unsigned=False, **_kwargs):
        assert allow_unsigned is True
        raise server.installer.InstallError("package signature verification failed")

    monkeypatch.setattr(server.installer, "inspect", reject_bad_signature)
    body, content_type = _multipart(
        b"signed-content", signature=b"Zm9yZ2VkLXNpZ25hdHVyZQ==")
    with pytest.raises(
            server.installer.InstallError, match="signature verification failed"):
        server.do_v1_upload(
            io.BytesIO(body), len(body), content_type,
            source=server.V1_LOCAL_UPLOAD_SOURCE,
            channel=server.V1_LOCAL_UPLOAD_CHANNEL)
    assert os.listdir(paths.uploads_dir()) == []


def test_failed_install_operation_removes_single_use_upload(layout, monkeypatch):
    permissions = {
        "sdk": [],
        "filesystem": {"read": [], "write": []},
        "network": {"listen": [], "outbound": []},
        "devices": [],
    }
    manifest = {
        "manifest_version": 2, "id": "demo", "permissions": permissions,
        "resources": {"claims": []},
    }
    monkeypatch.setattr(paths, "DEVELOPER_MODE_ALLOWED", True)
    monkeypatch.setattr(server.installer, "inspect", lambda *args, **kwargs: {
        "id": "demo", "manifest": manifest,
        "signature": {"signed": False, "verified": False,
                      "alg": "ecdsa-sha256", "detail": "unsigned"},
        "preflight": {"release_id": "demo-r1"},
    })
    body, content_type = _multipart(b"install-will-fail")
    uploaded = server.do_v1_upload(
        io.BytesIO(body), len(body), content_type,
        source=server.V1_LOCAL_UPLOAD_SOURCE,
        channel=server.V1_LOCAL_UPLOAD_CHANNEL)

    def fail_install(*args, **kwargs):
        raise RuntimeError("staging failed")

    monkeypatch.setattr(server, "do_install", fail_install)
    queued = server.do_v1_install({
        "upload_id": uploaded["upload_id"],
        "permissions_confirmed": True,
        "permissions": permissions,
        "unsigned_risk_confirmed": True,
    })["operation"]

    deadline = time.monotonic() + 3
    current = None
    while time.monotonic() < deadline:
        current = next(item for item in server._operation_manager().list()
                       if item["id"] == queued["id"])
        if current["status"] in operations.TERMINAL:
            break
        time.sleep(0.01)
    assert current["status"] == "failed"
    assert not os.path.exists(os.path.join(
        paths.uploads_dir(), uploaded["upload_id"]))


def test_operation_journal_and_event_bus_survive_callback_failure(layout):
    manager = operations.OperationManager(paths.operation_state_file())
    subscription = manager.events.subscribe()
    try:
        succeeded = manager.submit("start", "ok", lambda: {"pid": 42})

        def fail():
            raise RuntimeError("boom")

        failed = manager.submit("restart", "bad", fail)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            values = {item["id"]: item for item in manager.list()}
            if (values[succeeded["id"]]["status"] == "succeeded"
                    and values[failed["id"]]["status"] == "failed"):
                break
            time.sleep(0.01)
        assert values[succeeded["id"]]["result"] == {"pid": 42}
        assert "RuntimeError: boom" in values[failed["id"]]["error"]
        events = []
        while not subscription.empty():
            events.append(subscription.get_nowait())
        assert any(event["type"] == "operation" for event in events)
    finally:
        manager.events.unsubscribe(subscription)
        manager.close()


def test_operation_queue_is_bounded_and_rejects_same_app_overlap(layout):
    manager = operations.OperationManager(
        paths.operation_state_file(), queue_depth=2)
    release = threading.Event()
    running = threading.Event()

    def blocked():
        running.set()
        assert release.wait(3)

    try:
        manager.submit("start", "one", blocked)
        assert running.wait(1)
        with pytest.raises(operations.OperationBusyError, match="already active"):
            manager.submit("restart", "one", lambda: None)

        manager.submit("start", "two", lambda: None)
        manager.submit("start", "three", lambda: None)
        with pytest.raises(operations.OperationBusyError, match="capacity"):
            manager.submit("start", "four", lambda: None)
    finally:
        release.set()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and any(
                item["status"] in operations.ACTIVE for item in manager.list()):
            time.sleep(0.01)
        manager.close()


@pytest.mark.parametrize("action", ["start", "stop", "restart"])
def test_v1_lifecycle_waits_for_transient_busy_gate(
        layout, monkeypatch, action):
    """An accepted async action waits at the gate; its body runs exactly once."""
    app_dir = os.path.join(paths.APPS_DIR, "demo")
    os.mkdir(app_dir)
    calls = []

    class Coordinator:
        def start(self, app_id, **kwargs):
            calls.append(("start", app_id))
            return {"id": app_id, "pid": 101, "observed_state": "running"}

        def stop(self, app_id, **kwargs):
            calls.append(("stop", app_id))
            return {"id": app_id, "stopped": True}

        def restart(self, app_id, **kwargs):
            calls.append(("restart", app_id))
            return {"id": app_id, "pid": 102, "observed_state": "running"}

    monkeypatch.setattr(server, "_coordinator", lambda: Coordinator())
    monkeypatch.setattr(server, "_managed_launch", lambda *args, **kwargs: None)
    monkeypatch.setattr(paths, "V1_OPERATION_BUSY_TIMEOUT_SEC", 0.5)
    monkeypatch.setattr(paths, "V1_OPERATION_BUSY_RETRY_SEC", 0.005)
    contended = _observe_operation_worker_lock_contention(monkeypatch)

    with server.busy_gate():
        queued = server.do_v1_lifecycle("demo", action)["operation"]
        assert contended.wait(1.0), "worker never reached the held mutation gate"
        assert calls == []

    terminal = _wait_operation(queued["id"])
    assert terminal["status"] == "succeeded"
    assert calls == [(action, "demo")]


@pytest.mark.parametrize("action,expected", [
    ("start", ["start", "invalidate"]),
    ("stop", ["stop", "invalidate"]),
    ("restart", ["stop", "start", "invalidate"]),
])
def test_v1_builtin_lifecycle_is_async_and_never_requires_app_directory(
        layout, monkeypatch, action, expected):
    effects = []

    monkeypatch.setattr(
        server.builtin, "start",
        lambda: effects.append("start") or {"iEnable": 1})
    monkeypatch.setattr(
        server.builtin, "stop",
        lambda: effects.append("stop") or {"stop_confirmed": True})
    monkeypatch.setattr(
        server, "_builtin_invalidate",
        lambda: effects.append("invalidate"))

    queued = server.do_v1_lifecycle("builtin", action)["operation"]
    terminal = _wait_operation(queued["id"])

    assert terminal["status"] == "succeeded"
    assert effects == expected


def test_v1_builtin_restart_fails_closed_before_start(layout, monkeypatch):
    effects = []

    def stop_fails():
        effects.append("stop")
        raise server.builtin.BuiltinError("teardown failed")

    monkeypatch.setattr(server.builtin, "stop", stop_fails)
    monkeypatch.setattr(
        server.builtin, "start", lambda: effects.append("start"))
    monkeypatch.setattr(
        server, "_builtin_invalidate",
        lambda: effects.append("invalidate"))

    queued = server.do_v1_lifecycle("builtin", "restart")["operation"]
    terminal = _wait_operation(queued["id"])

    assert terminal["status"] == "failed"
    assert "teardown failed" in terminal["error"]
    assert effects == ["stop", "invalidate"]


@pytest.mark.parametrize("teardown", [{}, {"stop_confirmed": False}])
def test_v1_builtin_restart_rejects_unconfirmed_teardown(
        layout, monkeypatch, teardown):
    effects = []
    monkeypatch.setattr(
        server.builtin, "stop", lambda: effects.append("stop") or teardown)
    monkeypatch.setattr(
        server.builtin, "start", lambda: effects.append("start"))
    monkeypatch.setattr(
        server, "_builtin_invalidate",
        lambda: effects.append("invalidate"))

    queued = server.do_v1_lifecycle("builtin", "restart")["operation"]
    terminal = _wait_operation(queued["id"])

    assert terminal["status"] == "failed"
    assert "confirmed teardown proof" in terminal["error"]
    assert effects == ["stop", "invalidate"]


def test_v1_builtin_restart_waits_for_busy_gate_before_driver_calls(
        layout, monkeypatch):
    effects = []
    monkeypatch.setattr(
        server.builtin, "stop",
        lambda: effects.append("stop") or {"stop_confirmed": True})
    monkeypatch.setattr(
        server.builtin, "start",
        lambda: effects.append("start") or {"iEnable": 1})
    monkeypatch.setattr(
        server, "_builtin_invalidate",
        lambda: effects.append("invalidate"))
    monkeypatch.setattr(paths, "V1_OPERATION_BUSY_TIMEOUT_SEC", 0.5)
    monkeypatch.setattr(paths, "V1_OPERATION_BUSY_RETRY_SEC", 0.005)
    contended = _observe_operation_worker_lock_contention(monkeypatch)

    with server.busy_gate():
        queued = server.do_v1_lifecycle("builtin", "restart")["operation"]
        assert contended.wait(1.0)
        assert effects == []

    terminal = _wait_operation(queued["id"])
    assert terminal["status"] == "succeeded"
    assert effects == ["stop", "start", "invalidate"]


def test_v1_builtin_lifecycle_http_route_returns_operation(
        layout, monkeypatch):
    monkeypatch.setattr(server.builtin, "start", lambda: {"iEnable": 1})
    monkeypatch.setattr(server, "_builtin_invalidate", lambda: None)
    httpd = server._AppHTTPServer(("127.0.0.1", 0), server._Handler)
    serve_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    serve_thread.start()
    try:
        status, payload = _json_request(
            httpd, "POST", "/api/app-center/v1/apps/builtin/start", {})
        assert status == 202
        terminal = _wait_operation(payload["operation"]["id"])
        assert terminal["status"] == "succeeded"
    finally:
        httpd.shutdown()
        httpd.server_close()
        serve_thread.join(timeout=2)


def test_v1_install_waits_for_transient_busy_gate(layout, monkeypatch):
    permissions = {
        "sdk": [],
        "filesystem": {"read": [], "write": []},
        "network": {"listen": [], "outbound": []},
        "devices": [],
    }
    manifest = {
        "manifest_version": 2,
        "id": "demo",
        "name": "Demo",
        "version": "1.0.0",
        "entry": "app.py",
        "permissions": permissions,
        "resources": {"claims": []},
        "instances": {"max": 1},
        "config_schema": {"groups": []},
        "python": {"wheels": []},
    }
    inspected = {
        "id": "demo",
        "manifest": manifest,
        "signature": {
            "signed": False,
            "verified": False,
            "alg": "ecdsa-sha256",
            "detail": "unsigned",
        },
        "preflight": {"release_id": "demo-r1"},
    }
    installs = []

    monkeypatch.setattr(paths, "DEVELOPER_MODE_ALLOWED", True)
    monkeypatch.setattr(paths, "V1_OPERATION_BUSY_TIMEOUT_SEC", 0.5)
    monkeypatch.setattr(paths, "V1_OPERATION_BUSY_RETRY_SEC", 0.005)
    monkeypatch.setattr(
        server.installer, "inspect", lambda *args, **kwargs: inspected)

    candidate = server.installer.PreparedInstall(
        app_id="demo", manifest=manifest, info=inspected,
        dest=paths.app_dir("demo"), staging=None)

    def prepare(*args, **kwargs):
        return candidate

    def commit_prepared(prepared):
        assert prepared is candidate
        installs.append(prepared)
        return "demo", manifest

    monkeypatch.setattr(server.installer, "prepare", prepare)
    monkeypatch.setattr(
        server.installer, "begin_install_transaction", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        server.installer, "mark_install_transaction", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        server.installer, "clear_install_transaction", lambda *args, **kwargs: None)
    monkeypatch.setattr(server.installer, "discard_prepared", lambda *_args: None)
    monkeypatch.setattr(server.installer, "commit_prepared", commit_prepared)

    def install(*args, **kwargs):
        installs.append((args, kwargs))
        return "demo", manifest

    # The transaction-native path must not regress to the legacy convenience
    # wrapper merely because the async worker waited for the busy gate.
    monkeypatch.setattr(server.installer, "install", install)
    body, content_type = _multipart(b"package")
    uploaded = server.do_v1_upload(
        io.BytesIO(body), len(body), content_type,
        source=server.V1_LOCAL_UPLOAD_SOURCE,
        channel=server.V1_LOCAL_UPLOAD_CHANNEL)
    contended = _observe_operation_worker_lock_contention(monkeypatch)

    with server.busy_gate():
        queued = server.do_v1_install({
            "upload_id": uploaded["upload_id"],
            "permissions_confirmed": True,
            "permissions": permissions,
            "unsigned_risk_confirmed": True,
        })["operation"]
        assert contended.wait(1.0), "worker never reached the held mutation gate"
        assert installs == []

    terminal = _wait_operation(queued["id"])
    assert terminal["status"] == "succeeded"
    assert installs == [candidate]


def test_v1_delete_waits_for_transient_busy_gate(layout, monkeypatch):
    app_dir = os.path.join(paths.APPS_DIR, "demo")
    os.mkdir(app_dir)
    uninstalls = []
    monkeypatch.setattr(paths, "V1_OPERATION_BUSY_TIMEOUT_SEC", 0.5)
    monkeypatch.setattr(paths, "V1_OPERATION_BUSY_RETRY_SEC", 0.005)
    monkeypatch.setattr(server.supervisor, "is_running", lambda app_id: None)
    monkeypatch.setattr(
        server.installer, "uninstall", lambda app_id: uninstalls.append(app_id))
    contended = _observe_operation_worker_lock_contention(monkeypatch)

    with server.busy_gate():
        queued = server.do_v1_delete("demo")["operation"]
        assert contended.wait(1.0), "worker never reached the held mutation gate"
        assert uninstalls == []

    terminal = _wait_operation(queued["id"])
    assert terminal["status"] == "succeeded"
    assert uninstalls == ["demo"]


def test_v1_busy_wait_is_bounded_and_does_not_enter_mutation(
        layout, monkeypatch):
    os.mkdir(os.path.join(paths.APPS_DIR, "demo"))
    calls = []

    class Coordinator:
        def start(self, app_id, **kwargs):
            calls.append(app_id)
            return {"id": app_id, "pid": 101, "observed_state": "running"}

    monkeypatch.setattr(server, "_coordinator", lambda: Coordinator())
    monkeypatch.setattr(server, "_managed_launch", lambda *args, **kwargs: None)
    monkeypatch.setattr(paths, "V1_OPERATION_BUSY_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(paths, "V1_OPERATION_BUSY_RETRY_SEC", 0.005)
    contended = _observe_operation_worker_lock_contention(monkeypatch)

    with server.busy_gate():
        queued = server.do_v1_lifecycle("demo", "start")["operation"]
        assert contended.wait(1.0)
        terminal = _wait_operation(queued["id"])
        assert terminal["status"] == "failed"
        assert "BusyError: appmgr busy" in terminal["error"]
        assert calls == []


def test_legacy_synchronous_mutation_remains_fail_fast(layout, monkeypatch):
    os.mkdir(os.path.join(paths.APPS_DIR, "demo"))
    calls = []

    class Coordinator:
        def start(self, app_id, **kwargs):
            calls.append(app_id)
            return {"id": app_id, "pid": 101, "observed_state": "running"}

    monkeypatch.setattr(server, "_coordinator", lambda: Coordinator())
    monkeypatch.setattr(server, "_managed_launch", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        server.time, "sleep",
        lambda _delay: pytest.fail("legacy busy path must not retry or sleep"))

    with server.busy_gate():
        with pytest.raises(server.BusyError, match="appmgr busy"):
            server.do_start("demo")

    assert calls == []


@pytest.mark.parametrize("apply_mode", ["live", "restart"])
def test_v1_config_put_waits_before_normal_app_side_effects(
        layout, monkeypatch, apply_mode):
    """A transient flock collision neither fails nor replays config business."""
    app_dir = os.path.join(paths.APPS_DIR, "demo")
    os.mkdir(app_dir)
    with open(os.path.join(app_dir, "manifest.json"), "w") as manifest_file:
        json.dump({
            "id": "demo",
            "name": "Demo",
            "version": "1.0.0",
            "config_schema": {"groups": [{"items": [{
                "key": "threshold",
                "type": "number",
                "default": 0.5,
                "min": 0.0,
                "max": 1.0,
                "apply": apply_mode,
            }]}]},
        }, manifest_file)

    effects = []
    monkeypatch.setattr(
        server.appconfig, "write_user_config",
        lambda app_id, values: effects.append(("write", app_id, values)))
    monkeypatch.setattr(
        server.supervisor, "is_running", lambda app_id: 101)
    monkeypatch.setattr(
        server.supervisor, "reload",
        lambda app_id: effects.append(("reload", app_id)) or True)
    monkeypatch.setattr(
        server.state, "get_app",
        lambda app_id: {"launch_mode": "managed"})
    monkeypatch.setattr(server, "_managed_launch", lambda *args, **kwargs: None)

    class Coordinator:
        def restart(self, app_id, **kwargs):
            effects.append(("restart", app_id))
            return {"id": app_id, "pid": 102, "observed_state": "running"}

    monkeypatch.setattr(server, "_coordinator", lambda: Coordinator())
    monkeypatch.setattr(paths, "V1_OPERATION_BUSY_TIMEOUT_SEC", 0.5)
    monkeypatch.setattr(paths, "V1_OPERATION_BUSY_RETRY_SEC", 0.005)
    contended = _observe_busy_gate_contention(monkeypatch)
    httpd = server._AppHTTPServer(("127.0.0.1", 0), server._Handler)
    serve_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    serve_thread.start()
    response = {}

    def put_config():
        response["value"] = _json_request(
            httpd, "PUT", "/api/app-center/v1/apps/demo/config",
            {"values": {"threshold": 0.75}})

    request_thread = threading.Thread(target=put_config)
    try:
        with server.busy_gate():
            request_thread.start()
            assert contended.wait(1.0), "PUT never reached the held mutation gate"
            assert effects == [], "config business started before flock ownership"
        request_thread.join(timeout=2)
        assert not request_thread.is_alive()
        status, payload = response["value"]
        assert status == 200
        assert payload["applied"] == apply_mode
        expected_action = "reload" if apply_mode == "live" else "restart"
        assert effects == [
            ("write", "demo", {"threshold": 0.75}),
            (expected_action, "demo"),
        ]
    finally:
        httpd.shutdown()
        httpd.server_close()
        serve_thread.join(timeout=2)


def test_v1_builtin_config_put_waits_before_driver_side_effects(
        layout, monkeypatch):
    effects = []

    def set_builtin(values):
        effects.append(("set", values))
        return {
            "id": "builtin", "saved": True, "applied": "restart",
            "restarted": True, "config": values,
        }

    monkeypatch.setattr(server.builtin, "set_config", set_builtin)
    monkeypatch.setattr(
        server, "_builtin_invalidate", lambda: effects.append(("invalidate",)))
    monkeypatch.setattr(paths, "V1_OPERATION_BUSY_TIMEOUT_SEC", 0.5)
    monkeypatch.setattr(paths, "V1_OPERATION_BUSY_RETRY_SEC", 0.005)
    contended = _observe_busy_gate_contention(monkeypatch)
    httpd = server._AppHTTPServer(("127.0.0.1", 0), server._Handler)
    serve_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    serve_thread.start()
    response = {}

    def put_config():
        response["value"] = _json_request(
            httpd, "PUT", "/api/app-center/v1/apps/builtin/config",
            {"values": {"iFPS": 10}})

    request_thread = threading.Thread(target=put_config)
    try:
        with server.busy_gate():
            request_thread.start()
            assert contended.wait(1.0), "PUT never reached the held mutation gate"
            assert effects == [], "builtin driver ran before flock ownership"
        request_thread.join(timeout=2)
        assert not request_thread.is_alive()
        assert response["value"][0] == 200
        assert effects == [("set", {"iFPS": 10}), ("invalidate",)]
    finally:
        httpd.shutdown()
        httpd.server_close()
        serve_thread.join(timeout=2)


def test_v1_config_busy_timeout_never_enters_business_body(layout, monkeypatch):
    app_dir = os.path.join(paths.APPS_DIR, "demo")
    os.mkdir(app_dir)
    with open(os.path.join(app_dir, "manifest.json"), "w") as manifest_file:
        json.dump({
            "id": "demo", "name": "Demo", "version": "1.0.0",
            "config_schema": {"threshold": {
                "type": "number", "default": 0.5, "min": 0.0, "max": 1.0,
                "apply": "live",
            }},
        }, manifest_file)
    effects = []
    real_read_manifest = server._read_manifest
    monkeypatch.setattr(
        server, "_read_manifest",
        lambda app_id: effects.append(("read_manifest", app_id))
        or real_read_manifest(app_id))
    monkeypatch.setattr(
        server.appconfig, "write_user_config",
        lambda *args: effects.append(("write", args)))
    monkeypatch.setattr(
        server.supervisor, "is_running", lambda app_id: 101)
    monkeypatch.setattr(
        server.supervisor, "reload",
        lambda app_id: effects.append(("reload", app_id)) or True)
    monkeypatch.setattr(paths, "V1_OPERATION_BUSY_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(paths, "V1_OPERATION_BUSY_RETRY_SEC", 0.005)
    contended = _observe_busy_gate_contention(monkeypatch)
    httpd = server._AppHTTPServer(("127.0.0.1", 0), server._Handler)
    serve_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    serve_thread.start()
    response = {}

    def put_config():
        response["value"] = _json_request(
            httpd, "PUT", "/api/app-center/v1/apps/demo/config",
            {"values": {"threshold": 0.75}})

    request_thread = threading.Thread(target=put_config)
    try:
        with server.busy_gate():
            request_thread.start()
            assert contended.wait(1.0)
            request_thread.join(timeout=1)
            assert not request_thread.is_alive(), "bounded wait did not expire"
            assert response["value"][0] == 409
            assert "appmgr busy" in response["value"][1]["error"]
            assert effects == []
    finally:
        httpd.shutdown()
        httpd.server_close()
        serve_thread.join(timeout=2)


def test_legacy_config_post_remains_fail_fast(layout, monkeypatch):
    app_dir = os.path.join(paths.APPS_DIR, "demo")
    os.mkdir(app_dir)
    with open(os.path.join(app_dir, "manifest.json"), "w") as manifest_file:
        json.dump({
            "id": "demo", "name": "Demo", "version": "1.0.0",
            "config_schema": {"threshold": {
                "type": "number", "default": 0.5, "min": 0.0, "max": 1.0,
                "apply": "live",
            }},
        }, manifest_file)
    effects = []
    monkeypatch.setattr(
        server.appconfig, "write_user_config",
        lambda *args: effects.append(("write", args)))
    monkeypatch.setattr(
        server.time, "sleep",
        lambda _delay: pytest.fail("legacy config busy path must not wait"))
    httpd = server._AppHTTPServer(("127.0.0.1", 0), server._Handler)
    serve_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    serve_thread.start()
    try:
        with server.busy_gate():
            status, payload = _json_request(
                httpd, "POST", "/api/appMgr/config",
                {"id": "demo", "config": {"threshold": 0.75}})
        assert status == 409
        assert "appmgr busy" in payload["error"]
        assert effects == []
    finally:
        httpd.shutdown()
        httpd.server_close()
        serve_thread.join(timeout=2)


def test_polled_app_list_does_not_mutate_start_identity_or_allocations(
        layout, monkeypatch):
    """GET /apps is a projection, never a lifecycle/revocation authority."""
    app_id = "poll-race"
    app_dir = os.path.join(paths.APPS_DIR, app_id)
    os.mkdir(app_dir)
    manifest = {
        "manifest_version": 2,
        "id": app_id,
        "name": "Poll Race",
        "version": "1.0.0",
        "entry": "app.py",
        "instances": {"max": 1},
        "resources": {"claims": [
            {"name": "result.publish", "mode": "brokered", "required": True},
        ]},
        "config_schema": {"groups": []},
    }
    with open(os.path.join(app_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f)
    state.save({"active_app": None, "active_version": None})
    instance = "identity-must-survive-polling"
    rec = state.begin_start(app_id, instance, version="1.0.0")
    generation = int(rec["generation"])
    manager = server._coordinator().resources
    allocations = manager.reserve(
        app_id, instance, generation, resources.plan_manifest(manifest, {}))
    state.transition(
        app_id,
        "starting",
        pid=424242,
        pgid=424242,
        allocations=[item["allocation_id"] for item in allocations],
    )
    before_state = state.load()
    before_resources = manager.snapshot()

    monkeypatch.setattr(server.supervisor, "reap_children", lambda: 0)
    monkeypatch.setattr(server.supervisor, "drain_exits", lambda: [])
    monkeypatch.setattr(server.supervisor, "is_running", lambda _app_id: None)
    monkeypatch.setattr(server.supervisor, "last_exit", lambda _app_id: None)
    monkeypatch.setattr(server.builtin, "is_running", lambda: False)
    for _ in range(25):
        entry = next(
            item for item in server.do_list()["apps"]
            if item["id"] == app_id)
        assert entry["running"] is False
        assert entry["generation"] == generation
        assert entry["instance_id"] == instance

    assert state.load() == before_state
    assert manager.snapshot() == before_resources


def test_reconciler_sweeps_only_after_acquiring_mutation_gate(
        layout, monkeypatch):
    events = []

    class Coordinator:
        @staticmethod
        def reconcile_allocations():
            events.append("resource-reconcile")
            return []

    monkeypatch.setattr(server.supervisor, "reap_children", lambda: 0)
    monkeypatch.setattr(server.supervisor, "drain_exits", lambda: [])
    monkeypatch.setattr(
        server.supervisor, "sweep_stale",
        lambda: events.append("sweep") or [])
    monkeypatch.setattr(server, "_coordinator", lambda: Coordinator())
    state.save({"active_app": None, "active_version": None})

    with server.busy_gate():
        assert server._reconcile_once() == []
        assert events == []

    assert server._reconcile_once() == []
    assert events == ["sweep", "resource-reconcile"]


def test_reconciler_closes_teardown_after_sweep_before_restore(
        layout, monkeypatch):
    app_id = "teardown-recovery"
    directory = paths.app_dir(app_id)
    os.makedirs(directory)
    with open(os.path.join(directory, "manifest.json"), "w") as output:
        json.dump({
            "id": app_id, "version": "1.0.0", "entry": "app.py",
        }, output)
    state.save({"active_app": None, "active_version": None})
    state.begin_start(
        app_id, "retained-generation", version="1.0.0",
        launch_mode="managed")
    state.transition(
        app_id, "stopping", pid=8181, pgid=8181,
        allocations=["retained-allocation"], teardown_pending=True)
    record = {"present": True}
    events = []

    class Coordinator:
        @staticmethod
        def stop(stopped_id, *, desired):
            assert stopped_id == app_id
            assert record["present"] is False
            events.append("finish-teardown")
            state.set_desired(app_id, desired)
            state.transition(
                app_id, "stopped", pid=None, pgid=None, allocations=[],
                teardown_pending=False)
            return {"stopped": app_id}

        @staticmethod
        def reconcile_allocations():
            events.append("resource-reconcile")
            return ["retained-allocation"]

        @staticmethod
        def reconcile_one(restored_id, **_kwargs):
            assert restored_id == app_id
            current = state.get_app(app_id)
            assert current["observed_state"] == "stopped"
            assert current["teardown_pending"] is False
            events.append("restore")
            state.transition(app_id, "running", pid=9191, pgid=9191)
            return {
                "id": app_id, "action": "restored", "pid": 9191,
                "observed_state": "running",
            }

    def sweep():
        events.append("sweep")
        record["present"] = False
        return [app_id]

    class EventSink:
        @staticmethod
        def publish(*_args, **_kwargs):
            return None

    class Operations:
        events = EventSink()

    monkeypatch.setattr(server, "_coordinator", lambda: Coordinator())
    monkeypatch.setattr(server, "_managed_launch", lambda *_args: None)
    monkeypatch.setattr(server, "_operation_manager", lambda: Operations())
    monkeypatch.setattr(server, "_audit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server.supervisor, "reap_children", lambda: 0)
    monkeypatch.setattr(server.supervisor, "drain_exits", lambda: [])
    monkeypatch.setattr(server.supervisor, "sweep_stale", sweep)
    monkeypatch.setattr(server.supervisor, "is_running", lambda _app: None)
    monkeypatch.setattr(
        server.supervisor, "has_run_record",
        lambda _app: record["present"])

    assert server._reconcile_once() == [{
        "id": app_id, "action": "restored", "pid": 9191,
        "observed_state": "running",
    }]
    assert events == ["sweep", "finish-teardown",
                      "resource-reconcile", "restore"]


def test_sse_subscriber_count_and_each_subscriber_queue_are_bounded():
    bus = operations.EventBus(subscriber_depth=4, max_subscribers=2)
    first = bus.subscribe()
    second = bus.subscribe()
    try:
        with pytest.raises(operations.EventCapacityError, match="connection limit"):
            bus.subscribe()
        for value in range(20):
            bus.publish("test", value=value)
        assert first.qsize() == 4
        assert second.qsize() == 4
        assert first.get_nowait()["value"] == 16
    finally:
        bus.unsubscribe(first)
        bus.unsubscribe(second)


def test_lifecycle_reconciler_runs_without_any_get_request(layout, monkeypatch):
    called = threading.Event()
    monkeypatch.setenv("APPMGR_RECONCILE_INTERVAL", "0.02")
    monkeypatch.setattr(server, "_reconcile_once", lambda: called.set() or [])
    server._stop_reconciler()
    try:
        server._start_reconciler()
        assert called.wait(1.0)
    finally:
        server._stop_reconciler()


def test_sse_event_endpoint_emits_json_invalidation(layout):
    httpd = server._AppHTTPServer(("127.0.0.1", 0), server._Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
    try:
        connection.request("GET", "/api/app-center/v1/events")
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Type").startswith("text/event-stream")
        assert response.fp.readline() == b": connected\n"
        assert response.fp.readline() == b"\n"

        server._operation_manager().events.publish(
            "app", app_id="demo", action="running")
        event_id = response.fp.readline()
        data = response.fp.readline()
        assert event_id.startswith(b"id: ")
        assert json.loads(data.removeprefix(b"data: "))["app_id"] == "demo"
    finally:
        connection.close()
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def test_web_api_does_not_claim_sensecraft_v1_namespace(layout):
    httpd = server._AppHTTPServer(("127.0.0.1", 0), server._Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection(
        "127.0.0.1", httpd.server_port, timeout=5)
    try:
        connection.request("GET", "/api/v1/apps")
        response = connection.getresponse()
        assert response.status == 404
        assert json.loads(response.read()) == {"error": "not found"}

        connection.request("GET", "/api/app-center/v1/apps")
        response = connection.getresponse()
        assert response.status == 200
        payload = json.loads(response.read())
        assert "apps" in payload
        assert "builtin" not in {item["id"] for item in payload["apps"]}
    finally:
        connection.close()
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def test_recording_sources_http_hides_apps_without_valid_capability(
        layout, monkeypatch):
    detection_output = {
        "contract_version": 2,
        "fields": [
            {"name": "label", "from": "results[].label"},
            {"name": "score", "from": "results[].score"},
            {"name": "box", "from": "results[].box",
             "coord": "normalized_xyxy"},
        ],
    }
    event_output = {
        "contract_version": 2,
        "fields": [
            {"name": "kind", "from": "events[kind=fall].kind"},
        ],
    }
    monkeypatch.setattr(server, "do_v1_apps", lambda: {"apps": [
        {
            "id": "person-detector", "name": "Person detector",
            "version": "1.2.3", "installed": True,
            "running": True, "status": "running",
            "manifest": {
                "manifest_version": 2, "id": "person-detector",
                "name": "Person detector", "version": "1.2.3",
                "output": detection_output,
                "record_trigger": {"version": 1, "signals": [{
                    "id": "people", "type": "detection",
                    "classes": ["person"], "supports_roi": True,
                }]},
            },
        },
        {
            "id": "fall-alarm", "name": "Fall alarm",
            "version": "2.0.0", "installed": True,
            "running": False, "status": "failed",
            "manifest": {
                "manifest_version": 2, "id": "fall-alarm",
                "name": "Fall alarm", "version": "2.0.0",
                "output": event_output,
                "record_trigger": {"version": 1, "signals": [{
                    "id": "fall", "type": "event",
                    "event_kind": "fall", "supports_roi": False,
                }]},
            },
        },
        {
            "id": "undeclared-event", "name": "Undeclared event",
            "version": "1.0.0", "installed": True,
            "running": False, "status": "stopped",
            "manifest": {
                "manifest_version": 2, "id": "undeclared-event",
                "output": event_output,
                "record_trigger": {"version": 1, "signals": [{
                    "id": "smoke", "type": "event",
                    "event_kind": "smoke", "supports_roi": False,
                }]},
            },
        },
        {
            "id": "no-recording", "name": "No recording capability",
            "version": "1.0.0", "installed": True,
            "running": True, "status": "running",
            "manifest": {
                "manifest_version": 2, "id": "no-recording",
                "output": detection_output,
            },
        },
    ]})
    monkeypatch.setattr(server, "_builtin_running", lambda: True)

    expected_bridge_status = {
        "running": True, "active_sources": ["fall-alarm", "person-detector"],
        "queued": 0, "sent": 7, "frames": 5, "events": 2,
        "resets": 2, "dropped": 0, "frame_dropped": 0,
        "event_dropped": 0, "frame_coalesced": 0, "duplicates": 0,
        "send_errors": 0, "last_error": "",
    }

    class Bridge:
        @staticmethod
        def status():
            return dict(expected_bridge_status)

    monkeypatch.setattr(server, "_recording_bridge_instance", Bridge())
    httpd = server._AppHTTPServer(("127.0.0.1", 0), server._Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        status, payload = _json_request(
            httpd, "GET", "/api/app-center/v1/recording/sources", {})
        assert status == 200
        assert payload["version"] == 1
        assert payload["status"] == expected_bridge_status
        assert [source["id"] for source in payload["sources"]] == [
            "builtin", "fall-alarm", "person-detector",
        ]

        builtin_source, fall_source, detector_source = payload["sources"]
        assert builtin_source == {
            "id": "builtin", "kind": "builtin", "name": "Built-in Detection",
            "name_zh": "系统内置检测", "version": "firmware",
            "installed": True, "running": True, "status": "running",
            "supports_roi": True, "signals": [],
        }
        assert fall_source["installed"] is True
        assert fall_source["running"] is False
        assert fall_source["status"] == "failed"
        assert fall_source["signals"] == [{
            "id": "fall", "type": "event", "supports_roi": False,
            "event_kind": "fall",
        }]
        assert detector_source["installed"] is True
        assert detector_source["running"] is True
        assert detector_source["status"] == "running"
        assert detector_source["supports_roi"] is True
        assert detector_source["signals"] == [{
            "id": "people", "type": "detection", "supports_roi": True,
            "classes": ["person"],
        }]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def test_visualization_http_policy_is_persisted_and_manifest_gated(
        layout, monkeypatch):
    compatible = os.path.join(paths.APPS_DIR, "compatible")
    legacy_compatible = os.path.join(paths.APPS_DIR, "legacy-compatible")
    incompatible = os.path.join(paths.APPS_DIR, "incompatible")
    os.mkdir(compatible)
    os.mkdir(legacy_compatible)
    os.mkdir(incompatible)
    with open(os.path.join(compatible, "manifest.json"), "w") as stream:
        json.dump({
            "manifest_version": 2,
            "id": "compatible",
            "render": {
                "schema_version": 1,
                "boxes": {"line_width": 2},
                "stream_osd": {"supported": ["boxes"], "default": False},
            },
            "output": {"contract_version": 2, "fields": [{
                "name": "box", "from": "results[].box",
                "coord": "pixel_xyxy",
            }]},
        }, stream)
    with open(os.path.join(legacy_compatible, "manifest.json"), "w") as stream:
        json.dump({
            "manifest_version": 2,
            "id": "legacy-compatible",
            "render": {"schema_version": 1,
                       "boxes": {"line_width": 2}},
            "output": {"contract_version": 2, "fields": [{
                "from": "results[].box", "coord": "normalized_xyxy",
            }]},
        }, stream)
    with open(os.path.join(incompatible, "manifest.json"), "w") as stream:
        json.dump({
            "manifest_version": 2,
            "id": "incompatible",
            "render": {"schema_version": 1},
        }, stream)
    monkeypatch.setenv(
        "APPMGR_VISUALIZATION_CONFIG",
        os.path.join(paths.APPMGR_DIR, "visualization.json"))
    server.cache_clear()

    class Bridge:
        def __init__(self):
            self.reloaded = []

        def reload(self, config):
            self.reloaded.append(config)

        def status(self):
            return {
                "running": True, "enabled": True,
                "sources": ["compatible"], "active_sources": [],
                "sent": 0, "send_errors": 0, "dropped": 0,
                "last_error": "",
            }

    bridge = Bridge()
    monkeypatch.setattr(server, "_visualization_bridge_instance", bridge)
    monkeypatch.setattr(server, "_audit", lambda *args, **kwargs: None)
    httpd = server._AppHTTPServer(("127.0.0.1", 0), server._Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        status, initial = _json_request(
            httpd, "GET", "/api/app-center/v1/visualization", {})
        assert status == 200
        assert initial["osd"]["enabled"] is False

        status, result = _json_request(
            httpd, "PUT", "/api/app-center/v1/visualization",
            {"osd": {"enabled": True, "sources": ["compatible"]}})
        assert status == 200
        assert result["osd"]["enabled"] is True
        assert result["osd"]["sources"] == ["compatible"]
        assert bridge.reloaded == [{
            "osd": {"enabled": True, "sources": ["compatible"]},
        }]
        assert visualization.load() == bridge.reloaded[-1]

        status, apps = _json_request(
            httpd, "GET", "/api/app-center/v1/apps", {})
        assert status == 200
        legacy = next(item for item in apps["apps"]
                      if item["id"] == "legacy-compatible")
        assert legacy["manifest"]["render"]["stream_osd"] == {
            "supported": ["boxes"], "default": False,
        }

        status, error = _json_request(
            httpd, "PUT", "/api/app-center/v1/visualization",
            {"osd": {"enabled": True, "sources": []}})
        assert status == 400
        assert "at least one" in error["error"]
        assert visualization.load() == bridge.reloaded[-1]

        status, result = _json_request(
            httpd, "PUT", "/api/app-center/v1/visualization",
            {"osd": {"enabled": True, "sources": ["legacy-compatible"]}})
        assert status == 200
        assert result["osd"]["sources"] == ["legacy-compatible"]
        assert visualization.load() == bridge.reloaded[-1]

        status, error = _json_request(
            httpd, "PUT", "/api/app-center/v1/visualization",
            {"osd": {"enabled": True, "sources": ["incompatible"]}})
        assert status == 400
        assert "does not support" in error["error"]
        assert visualization.load() == bridge.reloaded[-1]

        status, error = _json_request(
            httpd, "PUT", "/api/app-center/v1/visualization",
            {"osd": {"enabled": True, "sources": ["not-installed"]}})
        assert status == 404
        assert "not installed" in error["error"]
        assert visualization.load() == bridge.reloaded[-1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def test_app_scoped_visualization_is_atomic_and_capability_gated(
        layout, monkeypatch):
    def write_manifest(app_id, *, supported=True):
        directory = os.path.join(paths.APPS_DIR, app_id)
        os.mkdir(directory)
        manifest = {
            "manifest_version": 2,
            "id": app_id,
            "render": {"schema_version": 1},
            "output": {"contract_version": 2, "fields": []},
        }
        if supported:
            manifest["render"]["stream_osd"] = {
                "supported": ["boxes"], "default": False,
            }
            manifest["output"]["fields"] = [{
                "name": "box", "from": "results[].box",
                "coord": "normalized_xyxy",
            }]
        with open(os.path.join(directory, "manifest.json"), "w") as stream:
            json.dump(manifest, stream)

    write_manifest("alpha")
    write_manifest("beta")
    write_manifest("unsupported", supported=False)
    write_manifest("limit-target")
    for index in range(visualization.MAX_OSD_SOURCES):
        write_manifest("source-%d" % index)
    monkeypatch.setenv(
        "APPMGR_VISUALIZATION_CONFIG",
        os.path.join(paths.APPMGR_DIR, "visualization.json"))
    monkeypatch.setattr(
        server.supervisor, "is_running",
        lambda app_id: 4200 if app_id in {"alpha", "beta", "limit-target"} else None)
    audits = []
    monkeypatch.setattr(
        server, "_audit",
        lambda action, **payload: audits.append({"action": action, **payload}),
    )
    server.cache_clear()

    class Bridge:
        def __init__(self):
            self.config = visualization.defaults()

        def reload(self, config):
            self.config = config

        def status(self):
            osd = self.config["osd"]
            return {
                "running": True,
                "enabled": osd["enabled"],
                "sources": list(osd["sources"]),
                "active_sources": list(osd["sources"]),
                "sent": 0, "send_errors": 0, "dropped": 0,
                "last_error": "",
            }

    bridge = Bridge()
    monkeypatch.setattr(server, "_visualization_bridge_instance", bridge)
    httpd = server._AppHTTPServer(("127.0.0.1", 0), server._Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        status, initial = _json_request(
            httpd, "GET", "/api/app-center/v1/apps/alpha/visualization", {})
        assert status == 200
        assert initial["stream_burn_in"] == {
            "supported": True,
            "enabled": False,
            "effective": False,
            "reason": "disabled",
            "affects": ["preview", "rtsp", "recording", "snapshot"],
            "max_sources": visualization.MAX_OSD_SOURCES,
            "status": "disabled",
        }

        status, alpha = _json_request(
            httpd, "PUT", "/api/app-center/v1/apps/alpha/visualization",
            {"stream_burn_in": {"enabled": True}})
        assert status == 200
        assert alpha["stream_burn_in"]["enabled"] is True
        assert alpha["stream_burn_in"]["effective"] is True

        status, beta = _json_request(
            httpd, "PUT", "/api/app-center/v1/apps/beta/visualization",
            {"stream_burn_in": {"enabled": True}})
        assert status == 200
        assert beta["stream_burn_in"]["enabled"] is True
        assert visualization.load()["osd"] == {
            "enabled": True, "sources": ["alpha", "beta"],
        }

        status, alpha = _json_request(
            httpd, "PUT", "/api/app-center/v1/apps/alpha/visualization",
            {"stream_burn_in": {"enabled": False}})
        assert status == 200
        assert alpha["stream_burn_in"]["enabled"] is False
        assert visualization.load()["osd"] == {
            "enabled": True, "sources": ["beta"],
        }

        status, error = _json_request(
            httpd, "PUT", "/api/app-center/v1/apps/unsupported/visualization",
            {"stream_burn_in": {"enabled": True}})
        assert status == 400
        assert "does not support" in error["error"]
        assert visualization.load()["osd"]["sources"] == ["beta"]

        status, builtin_view = _json_request(
            httpd, "GET", "/api/app-center/v1/apps/builtin/visualization", {})
        assert status == 200
        assert builtin_view["stream_burn_in"]["supported"] is False
        assert builtin_view["stream_burn_in"]["reason"] == \
            "builtin_system_controlled"

        # A disabled legacy master may retain dormant selections. Enabling one
        # app through the scoped endpoint must not revive all of them.
        visualization.save({
            "osd": {"enabled": False, "sources": ["alpha", "beta"]},
        })
        status, _ = _json_request(
            httpd, "PUT", "/api/app-center/v1/apps/alpha/visualization",
            {"stream_burn_in": {"enabled": True}})
        assert status == 200
        assert visualization.load()["osd"] == {
            "enabled": True, "sources": ["alpha"],
        }

        # Old firmware did not clean sources on uninstall or on a release that
        # lost OSD capability. A scoped edit repairs both without discarding
        # another valid application's selection.
        visualization.save({
            "osd": {
                "enabled": True,
                "sources": ["missing-source", "unsupported", "alpha"],
            },
        })
        status, _ = _json_request(
            httpd, "PUT", "/api/app-center/v1/apps/beta/visualization",
            {"stream_burn_in": {"enabled": True}})
        assert status == 200
        assert visualization.load()["osd"] == {
            "enabled": True, "sources": ["alpha", "beta"],
        }
        reconciled = [entry for entry in audits
                      if entry["action"] == "v1_app_visualization_reconciled"]
        assert reconciled[-1]["removed_sources"] == [
            {"id": "missing-source", "reason": "not_installed"},
            {"id": "unsupported", "reason": "unsupported"},
        ]

        # The limit still applies when all occupied slots are real installed
        # applications with trusted stream-OSD capability.
        visualization.save({
            "osd": {"enabled": True,
                    "sources": [
                        "source-%d" % index
                        for index in range(visualization.MAX_OSD_SOURCES)
                    ]},
        })
        status, error = _json_request(
            httpd, "PUT", "/api/app-center/v1/apps/limit-target/visualization",
            {"stream_burn_in": {"enabled": True}})
        assert status == 409
        assert "at most" in error["error"]

        status, error = _json_request(
            httpd, "GET", "/api/app-center/v1/apps/missing/visualization", {})
        assert status == 404
        assert "not installed" in error["error"]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def test_app_scoped_visualization_rechecks_install_after_mutation_gate(
        layout, monkeypatch):
    app_id = "visualization-race"
    directory = os.path.join(paths.APPS_DIR, app_id)
    os.mkdir(directory)
    manifest_path = os.path.join(directory, "manifest.json")
    with open(manifest_path, "w") as stream:
        json.dump({
            "manifest_version": 2,
            "id": app_id,
            "render": {
                "schema_version": 1,
                "stream_osd": {"supported": ["boxes"], "default": False},
            },
            "output": {
                "contract_version": 2,
                "fields": [{
                    "name": "box", "from": "results[].box",
                    "coord": "normalized_xyxy",
                }],
            },
        }, stream)
    monkeypatch.setenv(
        "APPMGR_VISUALIZATION_CONFIG",
        os.path.join(paths.APPMGR_DIR, "visualization.json"))
    stale = {"osd": {"enabled": True, "sources": [app_id]}}
    visualization.save(stale)

    @contextmanager
    def uninstall_before_lock_body(**_kwargs):
        os.unlink(manifest_path)
        os.rmdir(directory)
        yield

    monkeypatch.setattr(server, "busy_gate", uninstall_before_lock_body)

    with pytest.raises(FileNotFoundError, match="not installed"):
        server.do_set_app_visualization(
            app_id, {"stream_burn_in": {"enabled": False}})

    assert visualization.load() == stale


def test_http_policy_and_idempotent_upload_delete_contract(layout):
    inactive_body, content_type = _multipart(b"http-inactive-package")
    inactive = uploads.receive(
        io.BytesIO(inactive_body), len(inactive_body), content_type)
    uploads.update(inactive["upload_id"], status="preflighted")

    active_body, active_content_type = _multipart(b"http-active-package")
    active = uploads.receive(
        io.BytesIO(active_body), len(active_body), active_content_type)
    uploads.update(active["upload_id"], status="installing")

    httpd = server._AppHTTPServer(("127.0.0.1", 0), server._Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    def request(method, path, *, origin=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", httpd.server_port, timeout=5)
        headers = {"Host": "camera.local", "X-Forwarded-Proto": "https"}
        if origin is not None:
            headers["Origin"] = origin
        try:
            connection.request(method, path, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    try:
        # Policy is read-only; the public nginx layer still supplies JWT auth,
        # while appmgr itself does not impose the mutation-only Origin gate.
        status, policy = request("GET", "/api/app-center/v1/policy")
        assert status == 200
        assert policy["manifest"]["required_version"] == 2
        assert policy["upload"]["max_package_bytes"] == paths.MAX_PKG_BYTES
        assert policy["signature"]["invalid_signatures_rejected"] is True

        inactive_path = "/api/app-center/v1/uploads/" + inactive["upload_id"]
        status, payload = request(
            "DELETE", inactive_path, origin="https://evil.local")
        assert status == 403
        assert "cross-origin" in payload["error"]
        assert os.path.isfile(inactive["package_path"])

        status, payload = request(
            "DELETE", inactive_path, origin="https://camera.local")
        assert status == 200
        assert payload == {
            "upload_id": inactive["upload_id"],
            "deleted": True,
            "state": "deleted",
            "previous_status": "preflighted",
        }
        assert not os.path.lexists(os.path.dirname(inactive["package_path"]))

        # A repeat and a never-issued, syntactically valid id are both explicit
        # successful no-ops; clients may safely retry modal cleanup.
        status, payload = request(
            "DELETE", inactive_path, origin="https://camera.local")
        assert status == 200
        assert payload["deleted"] is False
        assert payload["state"] == "absent"
        unknown_id = "0" * 32
        status, payload = request(
            "DELETE", "/api/app-center/v1/uploads/" + unknown_id,
            origin="https://camera.local")
        assert status == 200
        assert payload == {
            "upload_id": unknown_id,
            "deleted": False,
            "state": "absent",
        }

        active_path = "/api/app-center/v1/uploads/" + active["upload_id"]
        status, payload = request(
            "DELETE", active_path, origin="https://camera.local")
        assert status == 409
        assert "upload is active (installing)" in payload["error"]
        assert os.path.isfile(active["package_path"])

        status, payload = request(
            "DELETE", "/api/app-center/v1/uploads/not-an-id",
            origin="https://camera.local")
        assert status == 404
        assert payload == {"error": "not found"}
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def test_mutations_require_same_origin_or_nonambient_client_auth(
        layout, monkeypatch):
    calls = []
    monkeypatch.setattr(
        server, "do_v1_install",
        lambda body: calls.append(body) or {"operation": {"id": "op-1"}})
    legacy_calls = []
    monkeypatch.setattr(
        server, "do_stop",
        lambda app_id=None: legacy_calls.append(app_id) or {"stopped": app_id})
    httpd = server._AppHTTPServer(("127.0.0.1", 0), server._Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    def post(path, *, host, origin=None, authorization=None,
             forwarded_proto=None, body=b"{}"):
        connection = http.client.HTTPConnection(
            "127.0.0.1", httpd.server_port, timeout=5)
        headers = {"Host": host, "Content-Type": "text/plain"}
        if origin is not None:
            headers["Origin"] = origin
        if authorization is not None:
            headers["Authorization"] = authorization
        if forwarded_proto is not None:
            headers["X-Forwarded-Proto"] = forwarded_proto
        try:
            connection.request("POST", path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    try:
        status, _ = post(
            "/api/app-center/v1/apps", host="camera.local",
            origin="https://camera.local", forwarded_proto="https")
        assert status == 202

        status, payload = post(
            "/api/app-center/v1/apps", host="camera.local",
            origin="https://evil.local", forwarded_proto="https")
        assert status == 403
        assert "cross-origin" in payload["error"]

        status, _ = post(
            "/api/app-center/v1/apps", host="camera.local")
        assert status == 403

        status, _ = post(
            "/api/app-center/v1/apps", host="camera.local",
            authorization="Bearer explicit-token")
        assert status == 202

        status, _ = post(
            "/api/app-center/v1/apps",
            host="127.0.0.1:%d" % httpd.server_port)
        assert status == 202

        status, _ = post(
            "/api/appMgr/stop", host="camera.local",
            origin="http://evil.local", body=b"{}")
        assert status == 403
        assert legacy_calls == []
        assert len(calls) == 3
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def test_legacy_json_body_is_bounded_and_strict(layout):
    httpd = server._AppHTTPServer(("127.0.0.1", 0), server._Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    def request(body, declared_length=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", httpd.server_port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if declared_length is not None:
            headers["Content-Length"] = str(declared_length)
        try:
            connection.request(
                "POST", "/api/appMgr/stop", body=body, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    try:
        status, payload = request(b"not-json")
        assert status == 400
        assert payload["error"] == "request body is not valid JSON"

        # Refusal happens before a body-sized allocation/read.  The connection
        # closes because the declared body remains unread.
        status, payload = request(b"{}", declared_length=1024 * 1024 + 1)
        assert status == 400
        assert payload["error"] == "JSON request body is too large"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def test_http_v1_same_origin_without_trusted_edge_stamp_is_signed_only(
        layout, monkeypatch):
    calls = []

    def inspect(*_args, allow_unsigned=False, **_kwargs):
        calls.append(allow_unsigned)
        raise server.installer.InstallError("package signature is required")

    monkeypatch.setattr(server.installer, "inspect", inspect)
    httpd = server._AppHTTPServer(("127.0.0.1", 0), server._Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection(
        "127.0.0.1", httpd.server_port, timeout=5)
    try:
        body, content_type = _multipart(b"unsigned-direct-request")
        connection.request(
            "POST", "/api/app-center/v1/uploads", body=body,
            headers={
                "Content-Type": content_type,
                "Host": "camera.local",
                "Origin": "https://camera.local",
                "X-Forwarded-Proto": "https",
                "Authorization": "Bearer real-react-token",
                # Deliberately no post-auth nginx route stamp.
            })
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == 400
        assert "signature is required" in payload["error"]
        assert calls == [False]
        assert os.listdir(paths.uploads_dir()) == []
    finally:
        connection.close()
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def test_http_v1_upload_finalize_and_operations(layout, monkeypatch):
    audits = []
    monkeypatch.setattr(
        server, "_audit",
        lambda action, **fields: audits.append((action, fields)))
    permissions = {
        "sdk": ["frame.read"],
        "filesystem": {"read": ["app"], "write": ["appdata"]},
        "network": {"listen": [], "outbound": []},
        "devices": [],
    }
    manifest = {
        "manifest_version": 2,
        "id": "demo",
        "name": "Demo",
        "version": "1.0.0",
        "entry": "app.py",
        "permissions": permissions,
        "resources": {"claims": []},
        "instances": {"max": 1},
        "config_schema": {"groups": []},
        "python": {"wheels": []},
    }
    monkeypatch.setattr(paths, "DEVELOPER_MODE_ALLOWED", True)

    def inspect(package, signature=None, *, allow_unsigned=False):
        assert allow_unsigned is True
        return {
            "id": "demo", "manifest": manifest,
            "signature": {"signed": False, "verified": False,
                          "alg": "ecdsa-sha256", "detail": "unsigned"},
            "preflight": {"release_id": "demo-1"},
        }

    installs = []

    def install(package, signature=None, *, allow_unsigned=False,
                expected_preflight=None,
                running_upgrade_confirmed=False,
                force_reinstall_confirmed=False,
                _enforce_v1_confirmations=False,
                _busy_timeout=0.0):
        installs.append((package, signature, allow_unsigned,
                         expected_preflight, _busy_timeout,
                         running_upgrade_confirmed,
                         force_reinstall_confirmed,
                         _enforce_v1_confirmations))
        return {"id": "demo", "installed": True}

    monkeypatch.setattr(server.installer, "inspect", inspect)
    monkeypatch.setattr(server, "do_install", install)
    httpd = server._AppHTTPServer(("127.0.0.1", 0), server._Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
    try:
        body, content_type = _multipart(b"not-buffered-as-json")
        browser_headers = {
            "Content-Type": content_type,
            "Host": "camera.local",
            "Origin": "https://camera.local",
            "X-Forwarded-Proto": "https",
            "Authorization": "Bearer real-react-token",
            server.V1_TRUSTED_EDGE_HEADER: server.V1_TRUSTED_EDGE_VALUE,
        }
        connection.request("POST", "/api/app-center/v1/uploads", body=body,
                           headers=browser_headers)
        response = connection.getresponse()
        uploaded = json.loads(response.read())
        assert response.status == 201
        assert uploaded["preflight"]["manifest"]["id"] == "demo"
        assert uploaded["preflight"]["signature"]["status"] == "unsigned"
        assert uploaded["preflight"]["signature"][
            "unsigned_install_allowed"] is True
        assert "developer_mode_allowed" not in uploaded["preflight"]
        assert "developer_mode_allowed" not in uploaded["preflight"]["signature"]
        assert uploaded["preflight"]["source"] == "local-web"
        assert uploaded["preflight"]["channel"] == \
            "app-center-v1-same-origin"
        assert uploaded["preflight"]["warnings"] == [{
            "code": "unsigned-root-code",
            "severity": "critical",
            "message": server.UNSIGNED_WARNING_MESSAGE,
        }]
        assert uploaded["preflight"]["unsigned_confirmation"] == {
            "required": True,
            "fields": ["unsigned_risk_confirmed"],
            "confirmation_field": "unsigned_risk_confirmed",
            "expected": True,
            "auto_start": False,
        }
        assert uploaded["preflight"]["conflicts"] == []
        assert uploaded["preflight"]["start_admission"] == {
            "enforced_on": "start",
            "live_resources": "deferred",
            "dependencies": "deferred",
            "message": (
                "Live resource occupancy and dependency availability are "
                "checked when the application starts"),
        }

        request = json.dumps({
            "upload_id": uploaded["upload_id"],
            "permissions_confirmed": True,
            "permissions": permissions,
            "unsigned_risk_confirmed": True,
        })
        finalize_headers = dict(browser_headers)
        finalize_headers["Content-Type"] = "application/json"
        connection.request("POST", "/api/app-center/v1/apps", body=request,
                           headers=finalize_headers)
        response = connection.getresponse()
        queued = json.loads(response.read())
        assert response.status == 202
        assert queued["operation"]["type"] == "install"

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            connection.request("GET", "/api/app-center/v1/operations")
            response = connection.getresponse()
            operation_list = json.loads(response.read())["operations"]
            current = next(item for item in operation_list
                           if item["id"] == queued["operation"]["id"])
            if current["status"] in operations.TERMINAL:
                break
            time.sleep(0.01)
        assert current["status"] == "succeeded"
        assert installs and installs[0][2] is True
        assert installs[0][3]["release_id"] == "demo-1"
        assert installs[0][4] == paths.V1_OPERATION_BUSY_TIMEOUT_SEC
        assert installs[0][5:] == (False, False, True)
        confirmation = next(
            fields for action, fields in audits
            if action == "v1_unsigned_risk_confirmed")
        assert confirmation["id"] == "demo"
        assert confirmation["source"] == server.V1_LOCAL_UPLOAD_SOURCE
        assert confirmation["channel"] == server.V1_LOCAL_UPLOAD_CHANNEL
        assert not os.path.exists(os.path.join(
            paths.uploads_dir(), uploaded["upload_id"]))
    finally:
        connection.close()
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def test_preflight_binding_rejects_manifest_release_or_signer_changes():
    manifest = {"manifest_version": 2, "id": "demo",
                "permissions": {"sdk": ["frame.read"]}}
    expected = {
        "manifest": manifest,
        "release_id": "demo-r1",
        "signature": {
            "status": "verified", "signer_kind": "vendor",
            "key_fingerprint": "sha256:vendor",
        },
    }
    inspected = {
        "manifest": manifest,
        "preflight": {"release_id": "demo-r1"},
        "signature": {
            "signed": True, "verified": True, "signer_kind": "vendor",
            "key_fingerprint": "sha256:vendor",
        },
    }
    server._assert_v1_preflight_binding(expected, inspected)

    changed = dict(inspected)
    changed["manifest"] = {**manifest, "permissions": {"sdk": ["npu.infer"]}}
    with pytest.raises(server.installer.InstallError, match="manifest changed"):
        server._assert_v1_preflight_binding(expected, changed)

    changed = dict(inspected)
    changed["preflight"] = {"release_id": "demo-r2"}
    with pytest.raises(server.installer.InstallError, match="release identity"):
        server._assert_v1_preflight_binding(expected, changed)

    changed = dict(inspected)
    changed["signature"] = {**inspected["signature"],
                            "signer_kind": "owner",
                            "key_fingerprint": "sha256:owner"}
    with pytest.raises(server.installer.InstallError, match="signer identity"):
        server._assert_v1_preflight_binding(expected, changed)

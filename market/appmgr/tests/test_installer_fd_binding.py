"""Regression tests for signature-to-extraction inode binding."""
from __future__ import annotations

import io
import json
import os
import sys
import tarfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from appmgr import installer, paths, signing  # noqa: E402


def package(path, *, marker, app_id="fd-app", version="1.0.0", padding=b""):
    manifest = json.dumps({
        "id": app_id, "name": "FD App", "version": version, "entry": "app.py",
    }).encode()
    with tarfile.open(path, "w:gz") as archive:
        for name, data in (
            ("manifest.json", manifest),
            ("app.py", b"# entry\n" + padding),
            ("marker.txt", marker.encode()),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(data))
    return path


@pytest.fixture
def layout(tmp_path, monkeypatch):
    apps = tmp_path / "apps"
    state = tmp_path / "state"
    apps.mkdir()
    state.mkdir()
    monkeypatch.setattr(paths, "APPS_DIR", str(apps))
    monkeypatch.setattr(paths, "APPMGR_DIR", str(state))
    monkeypatch.setattr(paths, "VENVS_DIR", str(tmp_path / "venvs"))
    monkeypatch.setattr(paths, "ALLOWED_PKG_ROOTS", (str(tmp_path),))
    monkeypatch.setattr(paths, "REQUIRE_SIGNATURE", False)
    return tmp_path


def unsigned_status():
    return {"signed": False, "verified": False, "alg": "ecdsa-sha256",
            "detail": "test policy"}


def test_path_replacement_during_verify_cannot_change_extracted_bytes(layout, monkeypatch):
    original = package(str(layout / "candidate.tar.gz"), marker="GOOD")
    replacement = package(str(layout / "replacement.tar.gz"), marker="BAD")
    observed = {}

    def verify(verified_path, signature):
        assert verified_path.startswith("/proc/self/fd/")
        with tarfile.open(verified_path, "r:gz") as archive:
            observed["verified"] = archive.extractfile("marker.txt").read()
        os.replace(replacement, original)
        observed["raced"] = True
        return unsigned_status()

    monkeypatch.setattr(signing, "verify_package", verify)
    app_id, _ = installer.install(original)
    assert observed == {"verified": b"GOOD", "raced": True}
    assert app_id == "fd-app"
    assert (layout / "apps" / "fd-app" / "marker.txt").read_text() == "GOOD"
    # The pathname really does now name B; success above therefore proves the
    # extraction used the held descriptor rather than reopening this path.
    with tarfile.open(original, "r:gz") as archive:
        assert archive.extractfile("marker.txt").read() == b"BAD"


def test_procless_snapshot_replacement_fails_closed_and_preserves_old_app(layout, monkeypatch):
    original = package(str(layout / "candidate.tar.gz"), marker="GOOD")
    replacement = package(str(layout / "replacement.tar.gz"), marker="BAD")
    installed = layout / "apps" / "fd-app"
    installed.mkdir()
    (installed / "marker.txt").write_text("OLD")
    monkeypatch.setattr(installer, "_fd_proc_path", lambda _fd: None)

    def replace_snapshot(snapshot_path, signature):
        assert snapshot_path.endswith(".tar.gz")
        os.replace(replacement, snapshot_path)
        return unsigned_status()

    monkeypatch.setattr(signing, "verify_package", replace_snapshot)
    with pytest.raises(installer.InstallError, match="snapshot was replaced"):
        installer.install(original)
    assert (installed / "marker.txt").read_text() == "OLD"
    assert not any(".stage." in name for name in os.listdir(layout / "apps"))


def test_opened_file_is_regated_before_signature_verification(layout, monkeypatch):
    original = package(str(layout / "candidate.tar.gz"), marker="GOOD")
    oversized = package(
        str(layout / "oversized.tar.gz"), marker="BAD", padding=os.urandom(16 * 1024))
    monkeypatch.setattr(paths, "MAX_PKG_BYTES", os.path.getsize(original) + 32)
    real_validate = installer._validate_pkg_path
    verify_calls = []

    def race_after_path_gate(pkg_path):
        real = real_validate(pkg_path)
        os.replace(oversized, real)
        return real

    monkeypatch.setattr(installer, "_validate_pkg_path", race_after_path_gate)
    monkeypatch.setattr(signing, "verify_package",
                        lambda *args: verify_calls.append(args) or unsigned_status())
    with pytest.raises(installer.InstallError, match="package too large"):
        installer.install(original)
    assert verify_calls == []
    assert not (layout / "apps" / "fd-app").exists()


def test_signature_failure_closes_fd_and_creates_no_staging(layout, monkeypatch):
    original = package(str(layout / "candidate.tar.gz"), marker="GOOD")
    before = len(os.listdir("/proc/self/fd"))

    def fail(*_args):
        raise signing.SignatureError("deliberate signature failure")

    monkeypatch.setattr(signing, "verify_package", fail)
    with pytest.raises(installer.InstallError, match="deliberate signature failure"):
        installer.install(original)
    after = len(os.listdir("/proc/self/fd"))
    assert after == before
    assert list((layout / "apps").iterdir()) == []


def test_unsigned_developer_mode_is_explicit_and_bad_signature_still_fails(layout, monkeypatch):
    original = package(str(layout / "candidate.tar.gz"), marker="GOOD")
    monkeypatch.setattr(paths, "REQUIRE_SIGNATURE", True)
    with pytest.raises(installer.InstallError, match="package is unsigned"):
        installer.inspect(original)

    info = installer.inspect(original, allow_unsigned=True)
    assert info["preflight"]["requires_developer_mode"] is True
    assert info["preflight"]["developer_mode_allowed"] is True
    assert info["preflight"]["manifest_version"] == 1

    public_key = layout / "release_pub.pem"
    public_key.write_text("test anchor; base64 parsing fails before openssl reads this")
    monkeypatch.setattr(paths, "RELEASE_PUBKEY", str(public_key))
    with pytest.raises(installer.InstallError, match="valid base64"):
        installer.inspect(original, signature="%%%", allow_unsigned=True)


@pytest.mark.parametrize("key_path", [
    "keys/release_pub.pem",
    "config/device-owner.key",
    ".ssh/authorized_keys",
])
def test_package_cannot_carry_or_install_trust_keys(layout, key_path):
    candidate = layout / "candidate.tar.gz"
    manifest = json.dumps({
        "id": "key-app", "name": "Key App", "version": "1.0.0", "entry": "app.py",
    }).encode()
    with tarfile.open(candidate, "w:gz") as archive:
        for name, data in (
            ("manifest.json", manifest), ("app.py", b"# entry\n"),
            (key_path, b"-----BEGIN PUBLIC KEY-----\nnot trusted\n"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(data))
    with pytest.raises(installer.InstallError, match="must not contain signing keys"):
        installer.install(str(candidate), allow_unsigned=True)
    assert not (layout / "apps" / "key-app").exists()

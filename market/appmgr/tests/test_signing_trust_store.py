"""Vendor + device-owner app-package signing trust tests."""
from __future__ import annotations

import base64
import hashlib
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from appmgr import paths, signing


pytestmark = pytest.mark.skipif(
    shutil.which("openssl") is None, reason="openssl is required for signing tests")


def _keypair(root, name):
    private = root / f"{name}.private"
    public = root / f"{name}.public"
    subprocess.run(
        ["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout",
         "-out", str(private)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(
        ["openssl", "ec", "-in", str(private), "-pubout", "-out", str(public)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.chmod(public, 0o644)
    return private, public


def _sign(package, private):
    signature = subprocess.check_output(
        ["openssl", "dgst", "-sha256", "-sign", str(private), str(package)],
        stderr=subprocess.DEVNULL)
    return base64.b64encode(signature).decode("ascii")


def _fingerprint(public):
    der = subprocess.check_output(
        ["openssl", "pkey", "-pubin", "-in", str(public), "-outform", "DER"],
        stderr=subprocess.DEVNULL)
    return "sha256:" + hashlib.sha256(der).hexdigest()


@pytest.fixture
def trust(tmp_path, monkeypatch):
    vendor_private, vendor_public = _keypair(tmp_path, "vendor")
    owner_private, owner_public = _keypair(tmp_path, "owner")
    attacker_private, _ = _keypair(tmp_path, "attacker")
    owners = tmp_path / "owners"
    owners.mkdir(mode=0o755)
    owner_anchor = owners / "factory-floor.pem"
    owner_anchor.write_bytes(owner_public.read_bytes())
    os.chmod(owner_anchor, 0o644)
    package = tmp_path / "app.tar.gz"
    package.write_bytes(b"authenticated package bytes")
    monkeypatch.setattr(paths, "RELEASE_PUBKEY", str(vendor_public))
    monkeypatch.setattr(paths, "OWNER_KEYS_DIR", str(owners))
    monkeypatch.setattr(paths, "MAX_OWNER_KEYS", 16)
    monkeypatch.setattr(paths, "MAX_TRUST_KEY_BYTES", 64 * 1024)
    monkeypatch.setattr(paths, "REQUIRE_SIGNATURE", True)
    return {
        "package": package,
        "vendor_private": vendor_private,
        "vendor_public": vendor_public,
        "owner_private": owner_private,
        "owner_public": owner_public,
        "owner_anchor": owner_anchor,
        "attacker_private": attacker_private,
        "owners": owners,
    }


def test_vendor_signature_reports_vendor_and_spki_fingerprint(trust):
    status = signing.verify_package(
        str(trust["package"]), _sign(trust["package"], trust["vendor_private"]))
    assert status["verified"] is True
    assert status["signer_kind"] == "vendor"
    assert status["key_fingerprint"] == _fingerprint(trust["vendor_public"])


def test_firmware_vendor_anchor_does_not_depend_on_sdk_build_uid(trust, monkeypatch,
                                                                 tmp_path):
    """mke2fs -d preserves the unprivileged SDK builder's numeric uid.

    The immutable firmware vendor anchor must therefore be authenticated by its
    path/type/write-mode and signature contents, while writable owner anchors
    remain euid-owned.  Simulate the flashed image by making appmgr's effective
    uid differ from the host-owned vendor key.
    """
    monkeypatch.setattr(paths, "OWNER_KEYS_DIR", str(tmp_path / "no-owner-store"))
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 10000)
    status = signing.verify_package(
        str(trust["package"]), _sign(trust["package"], trust["vendor_private"]))
    assert status["verified"] is True
    assert status["signer_kind"] == "vendor"


def test_explicit_tool_pubkey_keeps_strict_owner_check(trust, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 10000)
    with pytest.raises(signing.SignatureError, match="expected"):
        signing.verify_package(
            str(trust["package"]),
            _sign(trust["package"], trust["vendor_private"]),
            pubkey=str(trust["vendor_public"]),
        )


def test_owner_signature_extends_vendor_trust_and_reports_owner(trust):
    status = signing.verify_package(
        str(trust["package"]), _sign(trust["package"], trust["owner_private"]))
    assert status["verified"] is True
    assert status["signer_kind"] == "owner"
    assert status["key_fingerprint"] == _fingerprint(trust["owner_public"])


@pytest.mark.parametrize("mode", [0o666, 0o664, 0o646])
def test_group_or_world_writable_owner_key_fails_closed(trust, mode):
    os.chmod(trust["owner_anchor"], mode)
    with pytest.raises(signing.SignatureError, match="group/world-writable"):
        signing.verify_package(
            str(trust["package"]), _sign(trust["package"], trust["owner_private"]))


def test_owner_key_symlink_is_rejected(trust):
    trust["owner_anchor"].unlink()
    os.symlink(trust["owner_public"], trust["owner_anchor"])
    with pytest.raises(signing.SignatureError, match="cannot open owner public key"):
        signing.verify_package(
            str(trust["package"]), _sign(trust["package"], trust["owner_private"]))


def test_owner_key_count_limit_fails_closed(trust, monkeypatch):
    (trust["owners"] / "second.pem").write_bytes(trust["owner_public"].read_bytes())
    monkeypatch.setattr(paths, "MAX_OWNER_KEYS", 1)
    with pytest.raises(signing.SignatureError, match="too many keys"):
        signing.verify_package(
            str(trust["package"]), _sign(trust["package"], trust["owner_private"]))


def test_owner_key_size_limit_fails_closed(trust, monkeypatch):
    vendor_size = trust["vendor_public"].stat().st_size
    trust["owner_anchor"].write_bytes(
        trust["owner_anchor"].read_bytes() + b"#" * (vendor_size + 128))
    monkeypatch.setattr(paths, "MAX_TRUST_KEY_BYTES", vendor_size + 64)
    with pytest.raises(signing.SignatureError, match="too large"):
        signing.verify_package(
            str(trust["package"]), _sign(trust["package"], trust["owner_private"]))


def test_group_writable_owner_store_fails_closed(trust):
    os.chmod(trust["owners"], 0o775)
    with pytest.raises(signing.SignatureError, match="trust store is group/world-writable"):
        signing.verify_package(
            str(trust["package"]), _sign(trust["package"], trust["owner_private"]))


def test_invalid_owner_pem_fails_closed_even_for_vendor_signature(trust):
    trust["owner_anchor"].write_text("not a public key\n")
    with pytest.raises(signing.SignatureError, match="owner/factory-floor.pem is invalid"):
        signing.verify_package(
            str(trust["package"]), _sign(trust["package"], trust["vendor_private"]))


def test_signature_from_untrusted_key_is_rejected(trust):
    with pytest.raises(signing.SignatureError, match="FAILED against every trusted key"):
        signing.verify_package(
            str(trust["package"]), _sign(trust["package"], trust["attacker_private"]))

"""Security and atomicity tests for explicit owner-key management."""
from __future__ import annotations

import base64
import errno
import hashlib
import os
import shutil
import stat
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from appmgr import paths, signing, trust


pytestmark = pytest.mark.skipif(
    shutil.which("openssl") is None, reason="openssl is required for trust tests")


def _keypair(root, name, *, curve="prime256v1"):
    private = root / f"{name}.private.pem"
    public = root / f"{name}.public.pem"
    subprocess.run(
        ["openssl", "ecparam", "-name", curve, "-genkey", "-noout",
         "-out", str(private)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(
        ["openssl", "ec", "-in", str(private), "-pubout", "-out", str(public)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.chmod(public, 0o644)
    return private, public


def _rsa_public(root, name):
    private = root / f"{name}.rsa.private.pem"
    public = root / f"{name}.rsa.public.pem"
    subprocess.run(
        ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt",
         "rsa_keygen_bits:2048", "-out", str(private)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(
        ["openssl", "pkey", "-in", str(private), "-pubout", "-out", str(public)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return public


def _fingerprint(public):
    der = subprocess.check_output(
        ["openssl", "pkey", "-pubin", "-in", str(public), "-outform", "DER"],
        stderr=subprocess.DEVNULL)
    return "sha256:" + hashlib.sha256(der).hexdigest()


def _sign(package, private):
    signature = subprocess.check_output(
        ["openssl", "dgst", "-sha256", "-sign", str(private), str(package)],
        stderr=subprocess.DEVNULL)
    return base64.b64encode(signature).decode("ascii")


@pytest.fixture
def layout(tmp_path, monkeypatch):
    vendor_private, vendor_public = _keypair(tmp_path, "vendor")
    owner_private, owner_public = _keypair(tmp_path, "owner")
    second_private, second_public = _keypair(tmp_path, "second")
    key_parent = tmp_path / "state-keys"
    key_parent.mkdir(mode=0o700)
    owners = key_parent / "owners"
    monkeypatch.setattr(paths, "RELEASE_PUBKEY", str(vendor_public))
    monkeypatch.setattr(paths, "OWNER_KEYS_DIR", str(owners))
    monkeypatch.setattr(paths, "MAX_OWNER_KEYS", 16)
    monkeypatch.setattr(paths, "MAX_TRUST_KEY_BYTES", 64 * 1024)
    return {
        "root": tmp_path,
        "vendor_private": vendor_private,
        "vendor_public": vendor_public,
        "owner_private": owner_private,
        "owner_public": owner_public,
        "second_private": second_private,
        "second_public": second_public,
        "key_parent": key_parent,
        "owners": owners,
    }


def test_list_trust_always_reports_immutable_vendor(layout):
    result = trust.list_trust()
    assert result == [{
        "kind": "vendor",
        "name": layout["vendor_public"].name,
        "fingerprint": _fingerprint(layout["vendor_public"]),
        "algorithm": "ecdsa-sha256",
        "removable": False,
    }]
    assert not layout["owners"].exists(), "read-only listing must not create state"


def test_install_is_explicit_atomic_canonical_and_idempotent(layout):
    result = trust.install_owner_key(
        "factory-floor", layout["owner_public"].read_bytes() + b"\n# ignored input tail\n")
    assert result["created"] is True
    entry = result["key"]
    assert entry["kind"] == "owner"
    assert entry["label"] == "factory-floor"
    assert entry["fingerprint"] == _fingerprint(layout["owner_public"])
    assert entry["name"] == (
        "factory-floor--" + entry["fingerprint"].removeprefix("sha256:") + ".pem")

    installed = layout["owners"] / entry["name"]
    assert stat.S_IMODE(installed.stat().st_mode) == 0o600
    assert installed.stat().st_uid == os.geteuid()
    assert installed.read_bytes().startswith(b"-----BEGIN PUBLIC KEY-----")
    assert b"ignored input tail" not in installed.read_bytes()
    assert list(layout["owners"].glob(".*.tmp")) == []

    again = trust.install_owner_key("different-label", layout["owner_public"].read_bytes())
    assert again["created"] is False
    assert again["key"]["name"] == entry["name"]
    assert len(list(layout["owners"].glob("*.pem"))) == 1


def test_one_argument_provisioning_api_uses_safe_neutral_label(layout):
    result = trust.install_owner_key(layout["owner_public"].read_bytes())
    assert result["key"]["label"] == "owner"
    assert result["key"]["name"].startswith("owner--")


def test_installed_owner_key_extends_vendor_without_replacing_it(layout):
    package = layout["root"] / "app.tar.gz"
    package.write_bytes(b"package bytes are never interpreted as a key")
    trust.install_owner_key("local-publisher", layout["owner_public"].read_bytes())

    owner_status = signing.verify_package(
        str(package), _sign(package, layout["owner_private"]))
    vendor_status = signing.verify_package(
        str(package), _sign(package, layout["vendor_private"]))
    assert owner_status["signer_kind"] == "owner"
    assert owner_status["key_fingerprint"] == _fingerprint(layout["owner_public"])
    assert vendor_status["signer_kind"] == "vendor"
    assert vendor_status["key_fingerprint"] == _fingerprint(layout["vendor_public"])


def test_application_or_signature_bytes_are_not_implicitly_trusted(layout):
    with pytest.raises(trust.TrustValidationError, match="invalid"):
        trust.install_owner_key("package", b"tar/gzip bytes and a detached signature")
    assert not layout["owners"].exists()


def test_vendor_key_cannot_be_added_to_or_removed_from_owner_store(layout):
    vendor_fingerprint = _fingerprint(layout["vendor_public"])
    with pytest.raises(trust.ImmutableTrustAnchorError, match="immutable vendor"):
        trust.install_owner_key("shadow", layout["vendor_public"].read_bytes())
    assert not layout["owners"].exists()

    with pytest.raises(trust.ImmutableTrustAnchorError, match="cannot be deleted"):
        trust.remove_owner_key(vendor_fingerprint)
    assert layout["vendor_public"].exists()


@pytest.mark.parametrize("kind", ["rsa", "p384"])
def test_only_p256_public_keys_are_accepted(layout, kind):
    if kind == "rsa":
        public = _rsa_public(layout["root"], "wrong")
    else:
        _, public = _keypair(layout["root"], "wrong", curve="secp384r1")
    with pytest.raises(trust.TrustValidationError, match="not an ECDSA P-256"):
        trust.install_owner_key("wrong-curve", public.read_bytes())
    assert not layout["owners"].exists()


def test_input_size_and_label_are_bounded_before_store_creation(layout, monkeypatch):
    monkeypatch.setattr(paths, "MAX_TRUST_KEY_BYTES", 32)
    with pytest.raises(trust.TrustValidationError, match="too large"):
        trust.install_owner_key("owner", layout["owner_public"].read_bytes())
    with pytest.raises(trust.TrustValidationError, match="label"):
        trust.install_owner_key("../escape", layout["owner_public"].read_bytes())
    assert not layout["owners"].exists()


def test_count_limit_is_enforced_without_overwriting(layout, monkeypatch):
    trust.install_owner_key("first", layout["owner_public"].read_bytes())
    monkeypatch.setattr(paths, "MAX_OWNER_KEYS", 1)
    with pytest.raises(trust.TrustConflictError, match="limit reached"):
        trust.install_owner_key("second", layout["second_public"].read_bytes())
    assert len(list(layout["owners"].glob("*.pem"))) == 1


@pytest.mark.parametrize("store_mode", [0o720, 0o702, 0o777])
def test_unsafe_store_mode_is_rejected(layout, store_mode):
    layout["owners"].mkdir(mode=0o700)
    os.chmod(layout["owners"], store_mode)
    with pytest.raises(trust.TrustValidationError, match="group/world-writable"):
        trust.list_trust()


def test_store_and_key_symlinks_are_rejected(layout):
    real_store = layout["root"] / "real-owners"
    real_store.mkdir(mode=0o700)
    os.symlink(real_store, layout["owners"])
    with pytest.raises(trust.TrustValidationError, match="cannot open owner trust store"):
        trust.list_trust()

    layout["owners"].unlink()
    layout["owners"].mkdir(mode=0o700)
    os.symlink(layout["owner_public"], layout["owners"] / "linked.pem")
    with pytest.raises(trust.TrustValidationError, match="cannot open owner public key"):
        trust.list_trust()


def test_owner_file_uid_and_mode_are_enforced(layout, monkeypatch):
    layout["owners"].mkdir(mode=0o700)
    anchor = layout["owners"] / "unsafe.pem"
    anchor.write_bytes(layout["owner_public"].read_bytes())
    os.chmod(anchor, 0o666)
    with pytest.raises(trust.TrustValidationError, match="group/world-writable"):
        trust.list_trust()

    os.chmod(anchor, 0o600)
    real_uid = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: real_uid + 10000)
    with pytest.raises(trust.TrustValidationError, match="expected"):
        trust.list_trust()


def test_first_install_creates_keys_and_owners_from_existing_appmgr(layout,
                                                                    monkeypatch):
    appmgr_state = layout["root"] / "empty-appmgr-state"
    appmgr_state.mkdir(mode=0o700)
    key_parent = appmgr_state / "keys"
    owners = key_parent / "owners"
    monkeypatch.setattr(paths, "OWNER_KEYS_DIR", str(owners))

    result = trust.install_owner_key("first-owner", layout["owner_public"].read_bytes())
    assert result["created"] is True
    assert stat.S_IMODE(key_parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(owners.stat().st_mode) == 0o700
    assert (owners / result["key"]["name"]).is_file()


def test_missing_store_grandparent_fails_closed(layout, monkeypatch):
    owners = layout["root"] / "missing-appmgr" / "keys" / "owners"
    monkeypatch.setattr(paths, "OWNER_KEYS_DIR", str(owners))
    with pytest.raises(trust.TrustValidationError, match="grandparent"):
        trust.install_owner_key("owner", layout["owner_public"].read_bytes())


def test_unsafe_or_symlinked_keys_parent_fails_closed(layout):
    os.chmod(layout["key_parent"], 0o770)
    with pytest.raises(trust.TrustValidationError, match="group/world-writable"):
        trust.install_owner_key("owner", layout["owner_public"].read_bytes())

    os.chmod(layout["key_parent"], 0o700)
    layout["key_parent"].rmdir()
    real_parent = layout["root"] / "real-key-parent"
    real_parent.mkdir(mode=0o700)
    os.symlink(real_parent, layout["key_parent"])
    with pytest.raises(trust.TrustValidationError, match="parent"):
        trust.install_owner_key("owner", layout["owner_public"].read_bytes())


def test_atomic_publish_failure_leaves_no_key_or_staging_file(layout, monkeypatch):
    def fail_link(*args, **kwargs):
        raise OSError(errno.EIO, "injected link failure")

    monkeypatch.setattr(os, "link", fail_link)
    with pytest.raises(trust.TrustValidationError, match="atomically"):
        trust.install_owner_key("owner", layout["owner_public"].read_bytes())
    assert list(layout["owners"].iterdir()) == []


def test_delete_owner_by_case_insensitive_fingerprint(layout):
    installed = trust.install_owner_key(
        "factory", layout["owner_public"].read_bytes())["key"]
    upper = "sha256:" + installed["fingerprint"].split(":", 1)[1].upper()
    result = trust.remove_owner_key(upper)
    assert result == {"fingerprint": installed["fingerprint"], "deleted": 1}
    assert trust.list_trust()[0]["kind"] == "vendor"
    assert len(trust.list_trust()) == 1


def test_delete_removes_duplicate_legacy_owner_entries(layout):
    layout["owners"].mkdir(mode=0o700)
    for name in ("legacy-a.pem", "legacy-b.pem"):
        target = layout["owners"] / name
        target.write_bytes(layout["owner_public"].read_bytes())
        os.chmod(target, 0o600)
    result = trust.remove_owner_key(_fingerprint(layout["owner_public"]))
    assert result["deleted"] == 2
    assert list(layout["owners"].glob("*.pem")) == []


def test_delete_unknown_or_malformed_fingerprint_fails_closed(layout):
    with pytest.raises(trust.TrustValidationError, match="64 hexadecimal"):
        trust.remove_owner_key("sha256:nope")
    with pytest.raises(trust.TrustNotFoundError, match="not found"):
        trust.remove_owner_key("sha256:" + "0" * 64)


def test_existing_destination_is_never_replaced(layout):
    desired_fingerprint = _fingerprint(layout["owner_public"])
    collision = layout["owners"] / (
        "owner--" + desired_fingerprint.removeprefix("sha256:") + ".pem")
    layout["owners"].mkdir(mode=0o700)
    collision.write_bytes(layout["second_public"].read_bytes())
    os.chmod(collision, 0o600)
    original = collision.read_bytes()

    with pytest.raises(trust.TrustConflictError, match="already exists"):
        trust.install_owner_key(layout["owner_public"].read_bytes())
    assert collision.read_bytes() == original
    assert list(layout["owners"].glob(".*.tmp")) == []

"""
signing.py -- release-signature verification for appmgr (device side).

TODO #4 (package authenticity). The existing installer already gives us
integrity (sha256) and safe-unpack (zip-slip); this adds *authenticity*: proof
the package was published by the holder of the release private key, so a hostile
party who can serve a catalog cannot deliver arbitrary root code.

Trust model
-----------
  * The vendor PRIVATE key lives only on the publisher's workstation
    (`~/.recamera_release_key/`, never in repo, never on device).
  * The vendor PUBLIC key is immutable firmware content at
    ``paths.RELEASE_PUBKEY``.
  * Device owners may add public keys as direct ``*.pem`` children of
    ``paths.OWNER_KEYS_DIR``.  This store extends the vendor anchor; it cannot
    replace or mutate it.
  * Each package carries a DETACHED signature over the *raw .tar.gz bytes*:
        ECDSA, curve prime256v1 (P-256), digest SHA-256.
    Distributed base64 in the catalog (`package.signature`) and/or as a
    `<pkg>.tar.gz.sig` sidecar.

Why ECDSA-P256 via `openssl dgst` and not Ed25519
--------------------------------------------------
The device ships OpenSSL 1.1.1, whose `pkeyutl` has no `-rawin` (a 3.0 flag), so
Ed25519 one-shot verify is not reachable from the 1.1.1 CLI. `openssl dgst
-sha256 -verify` with an EC key is solid on both 1.1.1 (device) and 3.x (build
host). Zero new Python deps -- we shell out to the device's own openssl.
"""
from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import os
import shutil
import stat
import subprocess
import tempfile

from . import paths

SIGNATURE_ALG = "ecdsa-sha256"          # curve prime256v1
_PROC_SELF_FD_PREFIX = "/proc/self/fd/"


class SignatureError(Exception):
    """Raised when a package fails authenticity verification (or is unsigned
    while a signature is required)."""


def _openssl() -> str:
    exe = shutil.which("openssl") or "/usr/bin/openssl"
    if not os.path.exists(exe):
        raise SignatureError("openssl not found on device; cannot verify signature")
    return exe


def sidecar_path(pkg_path: str) -> str:
    return pkg_path + ".sig"


def load_signature(pkg_path: str, signature_b64: str | None) -> str | None:
    """Return the base64 signature to check: explicit arg wins, else the
    `<pkg>.sig` sidecar if present, else None (unsigned)."""
    if signature_b64:
        return signature_b64.strip()
    side = sidecar_path(pkg_path)
    if os.path.isfile(side):
        try:
            with open(side, "r") as f:
                s = f.read().strip()
            return s or None
        except OSError:
            return None
    return None


def _package_pass_fds(pkg_path: str) -> tuple[int, ...]:
    """Return the package fd that an openssl child must inherit, if any.

    Runtime installation deliberately verifies ``/proc/self/fd/<n>`` so the
    bytes checked are from the same open file description later extracted.
    Python opens descriptors close-on-exec by default, however, so that proc
    path disappears in the openssl child unless the one referenced descriptor
    is passed explicitly.  Do not resolve the proc symlink back to a pathname:
    doing so would undo the TOCTOU binding.
    """
    if not isinstance(pkg_path, str) or not pkg_path.startswith(_PROC_SELF_FD_PREFIX):
        return ()

    suffix = pkg_path[len(_PROC_SELF_FD_PREFIX):]
    if not suffix or not suffix.isascii() or not suffix.isdigit():
        return ()
    try:
        package_fd = int(suffix, 10)
        os.fstat(package_fd)
    except (OSError, OverflowError, ValueError):
        return ()
    return (package_fd,)


def _read_key_fd(fd: int, label: str, *, require_euid_owner: bool = True) -> bytes:
    """Read one already-open trust anchor after applying immutable-file gates.

    Owner-managed anchors live on writable ``/userdata`` and therefore must be
    owned by the appmgr effective uid (root on the device).  The vendor anchor
    is different: it is part of the firmware image, whose ext4 creator preserves
    the numeric uid of the unprivileged SDK build user.  Requiring uid 0 for that
    one file would make every vendor-signed package fail after flashing even
    though the anchor is a non-writable, no-follow firmware payload.  Its trust
    comes from the verified firmware image/path and write-mode checks, not from
    a host-specific build uid.
    """
    try:
        info = os.fstat(fd)
    except OSError as exc:
        raise SignatureError(f"cannot stat trusted public key {label}: {exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise SignatureError(f"trusted public key {label} is not a regular file")
    if require_euid_owner and info.st_uid != os.geteuid():
        raise SignatureError(
            f"trusted public key {label} has uid {info.st_uid}, expected {os.geteuid()}")
    if info.st_mode & 0o022:
        raise SignatureError(
            f"trusted public key {label} is group/world-writable; refusing unsafe anchor")
    if info.st_size <= 0:
        raise SignatureError(f"trusted public key {label} is empty")
    if info.st_size > paths.MAX_TRUST_KEY_BYTES:
        raise SignatureError(
            f"trusted public key {label} is too large: "
            f"{info.st_size} > {paths.MAX_TRUST_KEY_BYTES}")
    chunks = []
    remaining = info.st_size
    os.lseek(fd, 0, os.SEEK_SET)
    while remaining:
        chunk = os.read(fd, min(remaining, 1 << 16))
        if not chunk:
            raise SignatureError(f"trusted public key {label} was truncated while reading")
        chunks.append(chunk)
        remaining -= len(chunk)
    # Refuse in-place mutation while the bytes were read. An owner may rotate a
    # key by atomic rename; this held descriptor safely remains the old inode.
    after = os.fstat(fd)
    if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != \
            (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns):
        raise SignatureError(f"trusted public key {label} changed while being read")
    return b"".join(chunks)


def _open_key_path(path: str, label: str, *, require_euid_owner: bool = True) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise SignatureError(f"cannot open trusted public key {label}: {exc}") from exc
    try:
        return _read_key_fd(fd, label, require_euid_owner=require_euid_owner)
    finally:
        os.close(fd)


def _owner_key_bytes() -> list[tuple[str, bytes]]:
    """Return validated owner anchors from one held, non-symlink directory."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        directory_fd = os.open(paths.OWNER_KEYS_DIR, flags)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise SignatureError(
            f"cannot open owner trust store {paths.OWNER_KEYS_DIR}: {exc}") from exc
    try:
        info = os.fstat(directory_fd)
        if not stat.S_ISDIR(info.st_mode):
            raise SignatureError("owner trust store is not a directory")
        if info.st_uid != os.geteuid():
            raise SignatureError(
                f"owner trust store has uid {info.st_uid}, expected {os.geteuid()}")
        if info.st_mode & 0o022:
            raise SignatureError(
                "owner trust store is group/world-writable; refusing unsafe trust store")
        try:
            names = sorted(
                name for name in os.listdir(directory_fd)
                if isinstance(name, str) and name.endswith(".pem"))
        except OSError as exc:
            raise SignatureError(f"cannot enumerate owner trust store: {exc}") from exc
        if len(names) > paths.MAX_OWNER_KEYS:
            raise SignatureError(
                f"owner trust store has too many keys: {len(names)} > {paths.MAX_OWNER_KEYS}")
        anchors = []
        key_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            key_flags |= os.O_NOFOLLOW
        for name in names:
            # os.listdir(dirfd) produces a single path component. Keep the
            # explicit check as a fail-closed invariant for alternate runtimes.
            if name in (".", "..") or "/" in name or "\\" in name:
                raise SignatureError(f"unsafe owner public-key name {name!r}")
            try:
                fd = os.open(name, key_flags, dir_fd=directory_fd)
            except OSError as exc:
                raise SignatureError(
                    f"cannot open owner public key {name!r}: {exc}") from exc
            try:
                anchors.append((name, _read_key_fd(fd, f"owner/{name}")))
            finally:
                os.close(fd)
        return anchors
    finally:
        os.close(directory_fd)


def _public_key_fingerprint(key_bytes: bytes, label: str) -> str:
    """Return SHA-256 of canonical SubjectPublicKeyInfo DER."""
    try:
        description = subprocess.run(
            [_openssl(), "pkey", "-pubin", "-text", "-noout"],
            input=key_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=15,
        )
        proc = subprocess.run(
            [_openssl(), "pkey", "-pubin", "-inform", "PEM", "-outform", "DER"],
            input=key_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=15,
        )
    except subprocess.TimeoutExpired as exc:
        raise SignatureError(f"timed out parsing trusted public key {label}") from exc
    key_description = (description.stdout or b"").decode("utf-8", "replace")
    if description.returncode != 0:
        detail = (description.stderr or b"").decode("utf-8", "replace").strip()
        raise SignatureError(f"trusted public key {label} is invalid: {detail or 'openssl failed'}")
    if "ASN1 OID: prime256v1" not in key_description and \
            "NIST CURVE: P-256" not in key_description:
        raise SignatureError(
            f"trusted public key {label} is not an ECDSA P-256 public key: "
            "curve/type mismatch")
    if proc.returncode != 0 or not proc.stdout:
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip()
        raise SignatureError(f"trusted public key {label} is invalid: {detail or 'openssl failed'}")
    return "sha256:" + hashlib.sha256(proc.stdout).hexdigest()


@contextlib.contextmanager
def _trusted_key_snapshots(pubkey: str | None):
    """Yield secure snapshots of all trusted keys in deterministic order.

    Snapshotting the bytes read from a no-follow descriptor binds OpenSSL and
    the reported fingerprint to the same key even if an owner atomically
    rotates a store entry during verification.
    """
    raw_anchors: list[tuple[str, str, bytes]] = []
    vendor_path = pubkey or paths.RELEASE_PUBKEY
    raw_anchors.append(("vendor", os.path.basename(vendor_path) or "vendor",
                        _open_key_path(
                            vendor_path, "vendor",
                            # Production firmware images preserve the SDK build
                            # user's uid; see _read_key_fd().  An explicit
                            # pubkey remains a tool/test input and keeps the
                            # stricter ownership rule.
                            require_euid_owner=pubkey is not None)))
    # An explicit pubkey is a test/tool override and preserves the historical
    # single-anchor API. Production calls omit it and add the owner trust store.
    if pubkey is None:
        raw_anchors.extend(("owner", name, data) for name, data in _owner_key_bytes())

    snapshots = []
    created = []
    fingerprints = set()
    try:
        for kind, name, data in raw_anchors:
            fingerprint = _public_key_fingerprint(data, f"{kind}/{name}")
            # A duplicate owner key does not change the signer identity: vendor
            # wins by deterministic priority and is reported as such.
            if fingerprint in fingerprints:
                continue
            fingerprints.add(fingerprint)
            fd, snapshot = tempfile.mkstemp(prefix=".trustkey.", suffix=".pem")
            created.append(snapshot)
            with os.fdopen(fd, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(snapshot, 0o600)
            snapshots.append({
                "kind": kind,
                "name": name,
                "fingerprint": fingerprint,
                "path": snapshot,
            })
        yield snapshots
    finally:
        for snapshot in created:
            try:
                os.unlink(snapshot)
            except OSError:
                pass


def _verify_raw(pkg_path: str, sig_der: bytes, pubkey: str) -> tuple[bool, str]:
    """Run `openssl dgst -sha256 -verify pub -signature sig pkg`. Returns
    (ok, detail). openssl prints 'Verified OK' + exit 0 on success."""
    fd, sigfile = tempfile.mkstemp(prefix=".sigverify.", suffix=".der")
    try:
        with os.fdopen(fd, "wb") as w:
            w.write(sig_der)
        proc = subprocess.run(
            [_openssl(), "dgst", "-sha256", "-verify", pubkey,
             "-signature", sigfile, pkg_path],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60,
            pass_fds=_package_pass_fds(pkg_path),
        )
        out = (proc.stdout or b"").decode("utf-8", "replace").strip()
        ok = proc.returncode == 0 and "Verified OK" in out
        return ok, out or f"openssl exit {proc.returncode}"
    except subprocess.TimeoutExpired:
        return False, "openssl verify timed out"
    finally:
        try:
            os.unlink(sigfile)
        except OSError:
            pass


def verify_package(pkg_path: str, signature_b64: str | None = None,
                   *, require: bool | None = None,
                   pubkey: str | None = None) -> dict:
    """Verify a package's release signature and apply the require-signature
    policy. Returns a status dict; RAISES SignatureError on any hard failure.

    status = {
      "signed": bool,       # a signature was supplied/found
      "verified": bool,     # it checked out against the trust anchor
      "alg": "ecdsa-sha256",
      "detail": "<openssl line or policy note>",
      "signer_kind": "vendor" | "owner" | None,
      "key_fingerprint": "sha256:<SPKI digest>" | None,
    }

    Rules:
      * present signature -> MUST verify; bad/garbled -> SignatureError (always,
        independent of `require`).
      * no signature      -> require=True  -> SignatureError ("unsigned");
                             require=False -> allowed, status verified=False.
    """
    require = paths.REQUIRE_SIGNATURE if require is None else require
    sig_b64 = load_signature(pkg_path, signature_b64)

    if not sig_b64:
        if require:
            raise SignatureError(
                "package is unsigned and signature verification is required "
                "(set APPMGR_REQUIRE_SIGNATURE=0 to allow unsigned installs)")
        return {"signed": False, "verified": False, "alg": SIGNATURE_ALG,
                "detail": "unsigned package allowed by policy (require_signature=0)",
                "signer_kind": None, "key_fingerprint": None}

    try:
        sig_der = base64.b64decode(sig_b64, validate=True)
    except (binascii.Error, ValueError) as e:
        raise SignatureError(f"signature is not valid base64: {e}")
    if not sig_der:
        raise SignatureError("signature is empty after base64 decode")

    attempts = []
    with _trusted_key_snapshots(pubkey) as anchors:
        if not anchors:
            raise SignatureError("no trusted public keys are configured")
        for anchor in anchors:
            ok, detail = _verify_raw(pkg_path, sig_der, anchor["path"])
            if ok:
                return {
                    "signed": True,
                    "verified": True,
                    "alg": SIGNATURE_ALG,
                    "detail": detail,
                    "signer_kind": anchor["kind"],
                    "key_fingerprint": anchor["fingerprint"],
                }
            attempts.append(f"{anchor['kind']}/{anchor['name']}: {detail}")
    # Details name only the trust-store entry and OpenSSL result; key bytes and
    # host paths never cross the API boundary.
    raise SignatureError(
        "signature verification FAILED against every trusted key: " + "; ".join(attempts))

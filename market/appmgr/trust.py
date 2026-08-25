"""Explicit management of app-package signing trust anchors.

The immutable vendor key is firmware content.  Device-owner keys are an
*additional* trust store under :data:`paths.OWNER_KEYS_DIR`; this module never
modifies, shadows, or removes the vendor anchor.

Only a PEM-encoded P-256 public key can enter the owner store.  In particular,
an application package, its manifest, and its detached signature are never
interpreted as trust material.  Trusting a publisher therefore remains a
separate, explicit administrative action.
"""
from __future__ import annotations

import contextlib
import os
import re
import secrets
import stat
import subprocess
import threading
from typing import Iterator

from . import paths, signing


_FINGERPRINT_RE = re.compile(r"sha256:([0-9a-fA-F]{64})\Z")
_LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,47}\Z")
_MANAGED_NAME_RE = re.compile(
    r"([A-Za-z0-9][A-Za-z0-9_.-]{0,47})--([0-9a-f]{64})\.pem\Z")
_MUTATION_LOCK = threading.RLock()


class TrustError(Exception):
    """Base class for owner trust-store management failures."""


class TrustValidationError(TrustError):
    """The store, key, or fingerprint failed a security validation."""


class TrustConflictError(TrustError):
    """The requested mutation conflicts with existing trust material."""


class TrustNotFoundError(TrustError):
    """No removable owner key has the requested fingerprint."""


class ImmutableTrustAnchorError(TrustConflictError):
    """The operation attempted to mutate the firmware vendor anchor."""


def _security_flags(*, directory: bool = False) -> int:
    """Return fail-closed flags for opening writable trust-store objects."""
    if not hasattr(os, "O_NOFOLLOW"):
        raise TrustValidationError(
            "platform cannot enforce no-follow trust-store access")
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    if directory:
        if not hasattr(os, "O_DIRECTORY"):
            raise TrustValidationError(
                "platform cannot enforce directory-only trust-store access")
        flags |= os.O_DIRECTORY
    return flags


def _validate_directory_fd(fd: int, label: str) -> None:
    try:
        info = os.fstat(fd)
    except OSError as exc:
        raise TrustValidationError(f"cannot stat {label}: {exc}") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise TrustValidationError(f"{label} is not a directory")
    if info.st_uid != os.geteuid():
        raise TrustValidationError(
            f"{label} has uid {info.st_uid}, expected {os.geteuid()}")
    if info.st_mode & 0o022:
        raise TrustValidationError(
            f"{label} is group/world-writable; refusing unsafe trust store")


def _owner_store_path() -> tuple[str, str, str]:
    store = os.path.normpath(paths.OWNER_KEYS_DIR)
    if not os.path.isabs(store):
        raise TrustValidationError("owner trust-store path must be absolute")
    parent, leaf = os.path.split(store)
    if not parent or leaf in ("", ".", "..") or "/" in leaf or "\\" in leaf:
        raise TrustValidationError("owner trust-store path is unsafe")
    return store, parent, leaf


def _open_or_create_parent(path: str) -> int:
    """Open a secure parent directory, creating at most its final component.

    ``paths.ensure_dirs()`` guarantees ``APPMGR_DIR`` but intentionally knows
    nothing about trust provisioning.  On a new device the expected layout is
    therefore ``APPMGR_DIR`` (present) -> ``keys`` (missing) -> ``owners``
    (missing).  This helper creates the ``keys`` layer without recursive path
    traversal; the caller creates ``owners`` in the returned held directory.
    """
    flags = _security_flags(directory=True)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        grandparent, leaf = os.path.split(path)
        if not grandparent or leaf in ("", ".", "..") or \
                "/" in leaf or "\\" in leaf:
            raise TrustValidationError("owner trust-store parent path is unsafe")
        try:
            grandparent_fd = os.open(grandparent, flags)
        except OSError as exc:
            raise TrustValidationError(
                f"cannot open owner trust-store grandparent {grandparent}: {exc}") from exc
        try:
            _validate_directory_fd(
                grandparent_fd, "owner trust-store grandparent")
            try:
                os.mkdir(leaf, 0o700, dir_fd=grandparent_fd)
                os.fsync(grandparent_fd)
            except FileExistsError:
                pass
            except OSError as exc:
                raise TrustValidationError(
                    f"cannot create owner trust-store parent: {exc}") from exc
            try:
                fd = os.open(leaf, flags, dir_fd=grandparent_fd)
            except OSError as exc:
                raise TrustValidationError(
                    f"cannot open newly created owner trust-store parent: {exc}") from exc
        finally:
            os.close(grandparent_fd)
    except OSError as exc:
        raise TrustValidationError(
            f"cannot open owner trust-store parent {path}: {exc}") from exc
    try:
        _validate_directory_fd(fd, "owner trust-store parent")
    except Exception:
        os.close(fd)
        raise
    return fd


@contextlib.contextmanager
def _open_owner_store(*, create: bool) -> Iterator[int | None]:
    """Open and validate the owner directory without following its symlink.

    On first provisioning, the final ``keys`` parent and ``owners`` directory
    may be created one component at a time.  The existing appmgr state root and
    every newly opened directory pass the same uid/write-mode gates, avoiding
    an unsafe recursive ``makedirs`` through attacker-controlled components.
    """
    store, parent, leaf = _owner_store_path()
    flags = _security_flags(directory=True)
    try:
        directory_fd = os.open(store, flags)
    except FileNotFoundError:
        if not create:
            yield None
            return
        parent_fd = _open_or_create_parent(parent)
        try:
            try:
                os.mkdir(leaf, 0o700, dir_fd=parent_fd)
                os.fsync(parent_fd)
            except FileExistsError:
                # Another in-process request is serialized by _MUTATION_LOCK,
                # but tolerate a safe external provisioner winning the race.
                pass
            except OSError as exc:
                raise TrustValidationError(
                    f"cannot create owner trust store: {exc}") from exc
            try:
                directory_fd = os.open(leaf, flags, dir_fd=parent_fd)
            except OSError as exc:
                raise TrustValidationError(
                    f"cannot open newly created owner trust store: {exc}") from exc
        finally:
            os.close(parent_fd)
    except OSError as exc:
        raise TrustValidationError(
            f"cannot open owner trust store {store}: {exc}") from exc

    try:
        _validate_directory_fd(directory_fd, "owner trust store")
        yield directory_fd
    finally:
        os.close(directory_fd)


def _translate_signature_error(exc: signing.SignatureError) -> TrustValidationError:
    return TrustValidationError(str(exc))


def _fingerprint(key_bytes: bytes, label: str) -> str:
    try:
        return signing._public_key_fingerprint(key_bytes, label)
    except signing.SignatureError as exc:
        raise _translate_signature_error(exc) from exc


def _vendor_entry() -> dict:
    try:
        key_bytes = signing._open_key_path(
            paths.RELEASE_PUBKEY, "vendor", require_euid_owner=False)
    except signing.SignatureError as exc:
        raise _translate_signature_error(exc) from exc
    return {
        "kind": "vendor",
        "name": os.path.basename(paths.RELEASE_PUBKEY) or "vendor",
        "fingerprint": _fingerprint(key_bytes, "vendor"),
        "algorithm": signing.SIGNATURE_ALG,
        "removable": False,
    }


def _owner_entries(directory_fd: int) -> list[dict]:
    try:
        names = sorted(
            name for name in os.listdir(directory_fd)
            if isinstance(name, str) and name.endswith(".pem"))
    except OSError as exc:
        raise TrustValidationError(
            f"cannot enumerate owner trust store: {exc}") from exc
    if len(names) > paths.MAX_OWNER_KEYS:
        raise TrustValidationError(
            f"owner trust store has too many keys: "
            f"{len(names)} > {paths.MAX_OWNER_KEYS}")

    entries = []
    flags = _security_flags()
    for name in names:
        # listdir(dirfd) supplies components, but retain this invariant for
        # alternate Python runtimes and future refactors.
        if name in (".", "..") or "/" in name or "\\" in name:
            raise TrustValidationError(f"unsafe owner public-key name {name!r}")
        try:
            key_fd = os.open(name, flags, dir_fd=directory_fd)
        except OSError as exc:
            raise TrustValidationError(
                f"cannot open owner public key {name!r}: {exc}") from exc
        try:
            try:
                key_bytes = signing._read_key_fd(key_fd, f"owner/{name}")
            except signing.SignatureError as exc:
                raise _translate_signature_error(exc) from exc
            info = os.fstat(key_fd)
            entries.append({
                "kind": "owner",
                "name": name,
                "label": _label_from_filename(name),
                "fingerprint": _fingerprint(key_bytes, f"owner/{name}"),
                "algorithm": signing.SIGNATURE_ALG,
                "removable": True,
                # Private identity data is used only for a race-safe unlink and
                # is stripped from public results by _public_entry().
                "_identity": (
                    info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns),
            })
        finally:
            os.close(key_fd)
    return entries


def _public_entry(entry: dict) -> dict:
    return {key: value for key, value in entry.items() if not key.startswith("_")}


def _label_from_filename(name: str) -> str:
    managed = _MANAGED_NAME_RE.fullmatch(name)
    if managed is not None:
        return managed.group(1)
    return name[:-4] if name.endswith(".pem") else name


def list_keys() -> list[dict]:
    """Return validated vendor and owner fingerprints in stable order.

    A malformed/unsafe configured anchor fails the entire operation instead of
    presenting a misleading partial view of the keys used by verification.
    """
    with _MUTATION_LOCK:
        vendor = _vendor_entry()
        with _open_owner_store(create=False) as directory_fd:
            owners = [] if directory_fd is None else _owner_entries(directory_fd)
        return [_public_entry(vendor)] + [_public_entry(item) for item in owners]


def list_trust() -> list[dict]:
    """API-facing alias for :func:`list_keys`."""
    return list_keys()


def _canonical_public_key(pem: bytes | str) -> tuple[bytes, str]:
    if isinstance(pem, str):
        try:
            raw = pem.encode("ascii")
        except UnicodeEncodeError as exc:
            raise TrustValidationError(
                "owner public key must be ASCII PEM") from exc
    elif isinstance(pem, bytes):
        raw = pem
    else:
        raise TrustValidationError("owner public key must be PEM text or bytes")
    if not raw:
        raise TrustValidationError("owner public key is empty")
    if len(raw) > paths.MAX_TRUST_KEY_BYTES:
        raise TrustValidationError(
            f"owner public key is too large: "
            f"{len(raw)} > {paths.MAX_TRUST_KEY_BYTES}")

    try:
        proc = subprocess.run(
            [signing._openssl(), "pkey", "-pubin", "-inform", "PEM",
             "-pubout", "-outform", "PEM"],
            input=raw, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=15,
        )
    except signing.SignatureError as exc:
        raise _translate_signature_error(exc) from exc
    except subprocess.TimeoutExpired as exc:
        raise TrustValidationError(
            "timed out parsing owner public key") from exc
    if proc.returncode != 0 or not proc.stdout:
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip()
        raise TrustValidationError(
            f"owner public key is invalid: {detail or 'openssl failed'}")
    canonical = proc.stdout
    if len(canonical) > paths.MAX_TRUST_KEY_BYTES:
        raise TrustValidationError(
            f"canonical owner public key is too large: "
            f"{len(canonical)} > {paths.MAX_TRUST_KEY_BYTES}")
    fingerprint = _fingerprint(canonical, "prospective owner")
    return canonical, fingerprint


def _write_all(fd: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        try:
            written = os.write(fd, content[offset:])
        except OSError as exc:
            raise TrustValidationError(
                f"cannot write owner public key: {exc}") from exc
        if written <= 0:
            raise TrustValidationError("cannot write complete owner public key")
        offset += written


def _install_file(directory_fd: int, filename: str, content: bytes) -> None:
    """Publish a complete 0600 key without ever replacing an existing name."""
    write_flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC |
                   os.O_NOFOLLOW)
    temp_name = None
    temp_fd = None
    for _ in range(32):
        candidate = f".owner-key-{secrets.token_hex(12)}.tmp"
        try:
            temp_fd = os.open(
                candidate, write_flags, 0o600, dir_fd=directory_fd)
            temp_name = candidate
            break
        except FileExistsError:
            continue
        except OSError as exc:
            raise TrustValidationError(
                f"cannot create owner public-key staging file: {exc}") from exc
    if temp_fd is None or temp_name is None:
        raise TrustValidationError("cannot allocate owner public-key staging file")

    try:
        os.fchmod(temp_fd, 0o600)
        _write_all(temp_fd, content)
        os.fsync(temp_fd)
        try:
            info = os.fstat(temp_fd)
        except OSError as exc:
            raise TrustValidationError(
                f"cannot stat staged owner public key: {exc}") from exc
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or \
                stat.S_IMODE(info.st_mode) != 0o600 or info.st_size != len(content):
            raise TrustValidationError(
                "staged owner public key failed uid/mode/size validation")
        os.close(temp_fd)
        temp_fd = None

        # linkat is the portable no-replace publication primitive available in
        # the device Python: the complete inode becomes visible atomically, and
        # EEXIST cannot overwrite a pre-existing trust anchor.
        try:
            os.link(
                temp_name, filename,
                src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise TrustConflictError(
                f"owner public-key destination already exists: {filename}") from exc
        except OSError as exc:
            raise TrustValidationError(
                f"cannot publish owner public key atomically: {exc}") from exc
        os.unlink(temp_name, dir_fd=directory_fd)
        temp_name = None
        os.fsync(directory_fd)
    finally:
        if temp_fd is not None:
            os.close(temp_fd)
        if temp_name is not None:
            try:
                os.unlink(temp_name, dir_fd=directory_fd)
                os.fsync(directory_fd)
            except OSError:
                pass


def _normalize_label(value: str | None) -> str:
    if value is None:
        return "owner"
    if not isinstance(value, str) or _LABEL_RE.fullmatch(value.strip()) is None:
        raise TrustValidationError(
            "owner key label must be 1-48 ASCII letters, digits, '.', '_' or '-'")
    return value.strip()


def install_owner_key(label: str | bytes, pem: bytes | str | None = None) -> dict:
    """Explicitly add one P-256 PEM owner anchor.

    Returns ``{"key": <public entry>, "created": bool}``.  Re-adding the same
    owner key is idempotent; trying to add the vendor key is always rejected so
    the writable store can neither shadow nor claim ownership of that anchor.

    The preferred API is ``install_owner_key(label, pem)``.  A one-argument
    ``install_owner_key(pem)`` call remains supported for provisioning tools and
    uses the neutral label ``owner``.  Labels are deliberately restricted to a
    safe portable subset before they participate in the managed filename.
    """
    if pem is None:
        public_key = label
        normalized_label = "owner"
    else:
        public_key = pem
        normalized_label = _normalize_label(label if isinstance(label, str) else None)
        if not isinstance(label, str):
            raise TrustValidationError("owner key label must be a string")
    canonical, fingerprint = _canonical_public_key(public_key)
    with _MUTATION_LOCK:
        vendor = _vendor_entry()
        if fingerprint == vendor["fingerprint"]:
            raise ImmutableTrustAnchorError(
                "owner key duplicates the immutable vendor trust anchor")

        with _open_owner_store(create=True) as directory_fd:
            assert directory_fd is not None
            owners = _owner_entries(directory_fd)
            for entry in owners:
                if entry["fingerprint"] == fingerprint:
                    return {"key": _public_entry(entry), "created": False}
            if len(owners) >= paths.MAX_OWNER_KEYS:
                raise TrustConflictError(
                    f"owner trust store key limit reached: {paths.MAX_OWNER_KEYS}")

            digest = fingerprint.removeprefix("sha256:")
            filename = f"{normalized_label}--{digest}.pem"
            _install_file(directory_fd, filename, canonical)
            entry = {
                "kind": "owner",
                "name": filename,
                "label": normalized_label,
                "fingerprint": fingerprint,
                "algorithm": signing.SIGNATURE_ALG,
                "removable": True,
            }
            return {"key": entry, "created": True}


def _normalize_fingerprint(value: str) -> str:
    if not isinstance(value, str):
        raise TrustValidationError("key fingerprint must be a string")
    match = _FINGERPRINT_RE.fullmatch(value.strip())
    if match is None:
        raise TrustValidationError(
            "key fingerprint must be sha256 followed by 64 hexadecimal digits")
    return "sha256:" + match.group(1).lower()


def delete_owner_key(fingerprint: str) -> dict:
    """Delete all owner entries matching ``fingerprint``.

    Fingerprint lookup avoids exposing filesystem names as mutation input.  The
    inode read and fingerprinted must still be the inode unlinked; otherwise a
    concurrent replacement fails closed.
    """
    normalized = _normalize_fingerprint(fingerprint)
    with _MUTATION_LOCK:
        vendor = _vendor_entry()
        if normalized == vendor["fingerprint"]:
            raise ImmutableTrustAnchorError(
                "the immutable vendor trust anchor cannot be deleted")

        with _open_owner_store(create=False) as directory_fd:
            if directory_fd is None:
                raise TrustNotFoundError(
                    f"owner key not found: {normalized}")
            owners = _owner_entries(directory_fd)
            targets = [entry for entry in owners
                       if entry["fingerprint"] == normalized]
            if not targets:
                raise TrustNotFoundError(
                    f"owner key not found: {normalized}")

            # Validate every pathname before the first unlink so ordinary
            # races cannot cause a partial multi-name deletion.
            for entry in targets:
                try:
                    info = os.stat(
                        entry["name"], dir_fd=directory_fd,
                        follow_symlinks=False)
                except OSError as exc:
                    raise TrustConflictError(
                        f"owner key changed before deletion: {entry['name']}") from exc
                identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                if not stat.S_ISREG(info.st_mode) or identity != entry["_identity"]:
                    raise TrustConflictError(
                        f"owner key changed before deletion: {entry['name']}")

            for entry in targets:
                try:
                    os.unlink(entry["name"], dir_fd=directory_fd)
                except OSError as exc:
                    raise TrustConflictError(
                        f"cannot delete owner key {entry['name']}: {exc}") from exc
            os.fsync(directory_fd)
            return {"fingerprint": normalized, "deleted": len(targets)}


def remove_owner_key(fingerprint: str) -> dict:
    """API-facing alias for :func:`delete_owner_key`."""
    return delete_owner_key(fingerprint)


__all__ = [
    "ImmutableTrustAnchorError",
    "TrustConflictError",
    "TrustError",
    "TrustNotFoundError",
    "TrustValidationError",
    "delete_owner_key",
    "install_owner_key",
    "list_keys",
    "list_trust",
    "remove_owner_key",
]

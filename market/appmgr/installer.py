"""
installer.py -- validate + safely unpack an app package into /userdata/local/apps/<id>/.

Security (APP_CENTER_PORT_DESIGN §4.9): installApp is effectively "deliver root
code to the device", so every package is treated as hostile:

  * package path: realpath under an allowed root, `.tar.gz` suffix, regular
    file, size cap.
  * per-member (anti zip-slip / tar-bomb): reject absolute paths, `..`
    traversal, symlinks, hardlinks, device/fifo nodes, setuid/setgid bits;
    enforce the resolved path stays inside the target dir; cap member count and
    total unpacked size.
  * manifest: `id` whitelist [a-z0-9-]{1,64}, must match the requested id.

Extraction uses Python's tarfile (gzip handled natively), because busybox tar on
the device has no `-z`. We never call tar.extractall() blindly -- each member is
vetted then extracted by hand.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import shutil
import stat
import tarfile
import tempfile
import time
from dataclasses import dataclass
from typing import Optional, Tuple

from . import (config as appconfig, kitversion, manifest as manifest_contract,
               paths, pythonenv, signing)


class InstallError(Exception):
    pass


MAX_METADATA_BYTES = 4 * 1024 * 1024
INSTALL_TRANSACTION_VERSION = 1
MAX_INSTALL_TRANSACTION_BYTES = 8 * 1024 * 1024
_INSTALL_PHASES = (
    "prepared", "stopped", "publishing_code", "code_published",
    "publishing_environment", "environment_published", "configured",
    "restarting", "ready", "committed",
)


@dataclass
class PreparedInstall:
    """An authenticated code/environment candidate that has not been published."""
    app_id: str
    manifest: dict
    info: dict
    dest: str
    staging: str | None
    env_candidate: object | None = None
    transaction_id: str | None = None
    published: bool = False
    consumed: bool = False


def _fsync_dir(path: str) -> None:
    """Flush a directory entry to disk so a rename survives a power cut (健壮#16).

    Best-effort: some filesystems refuse O_RDONLY fsync on a directory, and the
    dev box's tmpfs has nothing to flush -- neither is a reason to fail an
    install."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _install_transaction_path() -> str:
    return os.path.join(paths.APPMGR_DIR, "install-transaction.json")


def _directory_identity(pathname: str) -> list[int] | None:
    try:
        info = os.lstat(pathname)
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        raise InstallError("install transaction path is not a directory: %s" % pathname)
    return [int(info.st_dev), int(info.st_ino)]


def _atomic_install_transaction(value: dict) -> None:
    paths.ensure_dirs()
    directory = paths.APPMGR_DIR
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise InstallError("install transaction is not JSON serialisable") from exc
    if len(encoded) > MAX_INSTALL_TRANSACTION_BYTES:
        raise InstallError("install transaction exceeds size limit")
    fd, temporary = tempfile.mkstemp(prefix=".install-transaction.", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, _install_transaction_path())
        temporary = None
        os.chmod(_install_transaction_path(), 0o600)
        _fsync_dir(directory)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def load_install_transaction() -> dict | None:
    """Read and validate the single durable install transaction record."""
    pathname = _install_transaction_path()
    try:
        info = os.lstat(pathname)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise InstallError("install transaction journal is not a regular file")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise InstallError("install transaction journal has unsafe permissions")
    if info.st_size > MAX_INSTALL_TRANSACTION_BYTES:
        raise InstallError("install transaction journal exceeds size limit")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(pathname, flags)
    try:
        opened = os.fstat(fd)
        if (not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != info.st_dev or opened.st_ino != info.st_ino
                or opened.st_size != info.st_size):
            raise InstallError("install transaction journal changed while reading")
        with os.fdopen(fd, "rb", closefd=False) as source:
            raw = source.read(MAX_INSTALL_TRANSACTION_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) != info.st_size:
        raise InstallError("install transaction journal changed while reading")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise InstallError("install transaction journal is invalid") from exc
    if (not isinstance(value, dict)
            or value.get("journal_version") != INSTALL_TRANSACTION_VERSION
            or value.get("phase") not in _INSTALL_PHASES
            or not isinstance(value.get("transaction_id"), str)
            or not isinstance(value.get("app_id"), str)
            or not paths.valid_app_id(value["app_id"])):
        raise InstallError("install transaction journal has invalid fields")
    for field in ("previous_dir_identity", "target_dir_identity"):
        identity = value.get(field)
        if identity is not None and (
                not isinstance(identity, list) or len(identity) != 2
                or any(type(item) is not int or item < 0 for item in identity)):
            raise InstallError("install transaction journal has invalid directory identity")
    return value


def begin_install_transaction(candidate: PreparedInstall, *,
                              config_snapshot: dict,
                              lifecycle_snapshot: dict) -> dict:
    """Durably bind rollback inputs before lifecycle or filesystem mutation."""
    if not isinstance(candidate, PreparedInstall) or not candidate.staging:
        raise InstallError("invalid prepared install transaction")
    pathname = _install_transaction_path()
    if os.path.lexists(pathname):
        raise InstallError("an interrupted install transaction requires reconciliation")
    transaction_id = secrets.token_hex(16)
    value = {
        "journal_version": INSTALL_TRANSACTION_VERSION,
        "transaction_id": transaction_id,
        "app_id": candidate.app_id,
        "phase": "prepared",
        "created_at": time.time(),
        "pre_installed": os.path.isdir(candidate.dest),
        "previous_dir_identity": _directory_identity(candidate.dest),
        "target_dir_identity": _directory_identity(candidate.staging),
        "installed_version": None,
        "target_version": candidate.manifest.get("version"),
        "installed_release_id": (
            _installed_v2_release_id(candidate.dest)
            if os.path.isdir(candidate.dest) else None),
        "previous_environment_release_id": None,
        "target_release_id": ((candidate.info.get("release_lock") or {}).get(
            "release_id")),
        "config_snapshot": config_snapshot,
        "lifecycle_snapshot": lifecycle_snapshot,
    }
    try:
        value["previous_environment_release_id"] = \
            pythonenv.current_release_id(candidate.app_id)
    except pythonenv.PythonEnvError as exc:
        raise InstallError(
            "installed Python environment cannot be journaled: %s" % exc) from exc
    try:
        if os.path.isdir(candidate.dest):
            with open(os.path.join(candidate.dest, "manifest.json"), "rb") as source:
                installed_manifest = json.load(source)
            if isinstance(installed_manifest, dict):
                value["installed_version"] = installed_manifest.get("version")
    except (OSError, ValueError):
        pass
    _atomic_install_transaction(value)
    candidate.transaction_id = transaction_id
    return value


def mark_install_transaction(candidate: PreparedInstall, phase: str) -> dict | None:
    if phase not in _INSTALL_PHASES:
        raise InstallError("invalid install transaction phase %r" % phase)
    if not candidate.transaction_id:
        return None
    value = load_install_transaction()
    if (value is None or value.get("transaction_id") != candidate.transaction_id
            or value.get("app_id") != candidate.app_id):
        raise InstallError("install transaction journal identity changed")
    if _INSTALL_PHASES.index(phase) < _INSTALL_PHASES.index(value["phase"]):
        raise InstallError("install transaction phase cannot move backwards")
    value["phase"] = phase
    value["updated_at"] = time.time()
    _atomic_install_transaction(value)
    return value


def clear_install_transaction(candidate: PreparedInstall | None = None,
                              *, transaction_id: str | None = None) -> None:
    value = load_install_transaction()
    if value is None:
        return
    expected = transaction_id or (
        candidate.transaction_id if isinstance(candidate, PreparedInstall) else None)
    if expected is not None and value.get("transaction_id") != expected:
        raise InstallError("refusing to clear a different install transaction")
    os.unlink(_install_transaction_path())
    _fsync_dir(paths.APPMGR_DIR)


def _validate_pkg_path(pkg_path: str) -> str:
    real = os.path.realpath(pkg_path)
    if not real.endswith(".tar.gz"):
        raise InstallError(f"package must be .tar.gz: {pkg_path}")
    if not any(real == r or real.startswith(r.rstrip("/") + "/")
               for r in paths.ALLOWED_PKG_ROOTS):
        raise InstallError(f"package path {real} not under allowed roots {paths.ALLOWED_PKG_ROOTS}")
    if not os.path.isfile(real):
        raise InstallError(f"package is not a regular file: {real}")
    size = os.path.getsize(real)
    if size > paths.MAX_PKG_BYTES:
        raise InstallError(f"package too large: {size} > {paths.MAX_PKG_BYTES}")
    if size == 0:
        raise InstallError("package is empty")
    return real


def _vet_member(m: tarfile.TarInfo, dest_root: str) -> None:
    name = m.name
    # absolute path / drive / traversal
    if name.startswith("/") or name.startswith("\\") or os.path.isabs(name):
        raise InstallError(f"zip-slip: absolute member path {name!r}")
    if ".." in name.replace("\\", "/").split("/"):
        raise InstallError(f"zip-slip: '..' in member path {name!r}")
    try:
        manifest_contract.validate_package_member_path(
            name.replace("\\", "/").rstrip("/"), "package member")
    except manifest_contract.ManifestValidationError as exc:
        raise InstallError(str(exc)) from exc
    # non-regular members
    if m.issym() or m.islnk():
        raise InstallError(f"unsafe member (sym/hard link): {name!r}")
    if m.isdev() or m.ischr() or m.isblk() or m.isfifo():
        raise InstallError(f"unsafe member (device/fifo): {name!r}")
    if not (m.isfile() or m.isdir()):
        raise InstallError(f"unsupported member type: {name!r}")
    # setuid / setgid / sticky
    if m.mode & 0o7000:
        raise InstallError(f"unsafe member mode {oct(m.mode)}: {name!r}")
    # resolved path must stay inside dest_root
    target = os.path.realpath(os.path.join(dest_root, name))
    if not paths.is_within(target, os.path.realpath(dest_root)):
        raise InstallError(f"zip-slip: member escapes target dir: {name!r}")
    _vet_icon_member(m, name)


def _vet_icon_member(m: tarfile.TarInfo, name: str) -> None:
    """Extra rules for a package's top-level `icon.*` (§5 P0-1).

    appmgr SERVES this file back to the browser (GET /api/appMgr/icon), so it is
    the one package member whose bytes reach a rendering context. Two rules:

      * extension whitelist -- raster only (paths.ICON_EXTS). An `icon.svg`
        would be an active document served same-origin behind the JWT edge, and
        `icon.html`/`icon.js` even more obviously so, hence a hard refusal
        rather than "ignore it": a package that thinks it ships an icon and
        silently doesn't is a worse outcome than a loud install failure.
      * size cap -- paths.MAX_ICON_BYTES. The whole-package caps are 200/400 MB;
        a card thumbnail has no business being anywhere near that.

    Only the TOP-LEVEL member is governed: `assets/icon.svg` is just an ordinary
    package file that appmgr never serves.
    """
    norm = name.replace("\\", "/").lstrip("./")
    if "/" in norm or not m.isfile():
        return
    stem, ext = os.path.splitext(norm)
    if stem.lower() != "icon":
        return
    if ext.lower() not in paths.ICON_EXTS:
        raise InstallError(
            f"unsupported icon type {name!r}: allowed {paths.ICON_EXTS}")
    if m.size > paths.MAX_ICON_BYTES:
        raise InstallError(
            f"icon too large: {m.size} > {paths.MAX_ICON_BYTES} ({name!r})")


def validate_pkg_path(pkg_path: str) -> str:
    """Public form of the package-path check (root whitelist, suffix, size cap).

    Exposed because the on-demand runtime installer (voiceruntime.py) receives a
    device path from the same browser-relayed /upload flow and must apply the
    same gate; re-deriving it there is how the two would drift.
    """
    return _validate_pkg_path(pkg_path)


def extract_vetted(pkg_path: str, dest_dir: str) -> list:
    """Unpack a .tar.gz into `dest_dir` with the app-install member vetting.

    Same anti-zip-slip/tar-bomb rules as install() -- no absolute paths, no `..`,
    no links or device nodes, no setuid bits, member count and unpacked-size caps
    -- but it drops the payload into a caller-chosen directory instead of
    /userdata/local/apps/<id>/ and does not look for a manifest. Used for
    non-app payloads (the voice runtime's wheel bundle). Returns the extracted
    member names.
    """
    real = _validate_pkg_path(pkg_path)
    with tarfile.open(real, "r:gz") as tar:
        return extract_vetted_tar(tar, dest_dir)


def extract_vetted_tar(tar: tarfile.TarFile, dest_dir: str) -> list:
    """Member-vetting extraction from an ALREADY-OPEN TarFile into `dest_dir`.

    Split out of extract_vetted() so a caller that must bind signature
    verification and extraction to a SINGLE open file descriptor (voiceruntime,
    TOCTOU C12) can hand us the tar it already opened on that fd, instead of
    re-opening the package by path after the signature was checked. Path/root/
    size gating is the caller's responsibility here (the fd was already vetted);
    the per-member zip-slip/tar-bomb rules still run on every member.
    """
    names = []
    total = 0
    members = tar.getmembers()
    if len(members) > paths.MAX_MEMBERS:
        raise InstallError(f"too many members: {len(members)} > {paths.MAX_MEMBERS}")
    for m in members:
        _vet_member(m, dest_dir)
        total += max(0, m.size)
        if total > paths.MAX_UNPACKED_BYTES:
            raise InstallError(f"unpacked size exceeds cap {paths.MAX_UNPACKED_BYTES}")
        outp = os.path.join(dest_dir, m.name)
        if m.isdir():
            os.makedirs(outp, exist_ok=True)
            continue
        f = tar.extractfile(m)
        if f is None:
            raise InstallError(f"cannot extract member {m.name!r}")
        os.makedirs(os.path.dirname(outp) or dest_dir, exist_ok=True)
        with f, open(outp, "wb") as w:
            shutil.copyfileobj(f, w)
        os.chmod(outp, 0o644)
        names.append(m.name)
    return names


def _read_member_bytes(tar: tarfile.TarFile, name: str) -> bytes:
    try:
        member = tar.getmember(name)
    except KeyError as exc:
        raise InstallError(f"package has no {name} at top level") from exc
    if not member.isfile():
        raise InstallError(f"package member {name!r} must be a regular file")
    if member.size > MAX_METADATA_BYTES:
        raise InstallError(
            f"package metadata {name!r} is too large: {member.size} > {MAX_METADATA_BYTES}")
    source = tar.extractfile(member)
    if source is None:
        raise InstallError(f"cannot read package member {name!r}")
    with source:
        data = source.read(MAX_METADATA_BYTES + 1)
    if len(data) != member.size:
        raise InstallError(f"package member {name!r} is truncated")
    return data


def _read_manifest_from_tar(tar: tarfile.TarFile) -> dict:
    try:
        value = json.loads(_read_member_bytes(tar, "manifest.json").decode("utf-8"))
    except InstallError:
        raise
    except Exception as exc:
        raise InstallError(f"manifest.json is not valid JSON: {exc}") from exc
    try:
        manifest_contract.validate_manifest(value)
    except manifest_contract.ManifestValidationError as exc:
        raise InstallError(f"invalid manifest.json: {exc}") from exc
    return value


def _fd_proc_path(fd: int) -> str | None:
    """Return a usable proc-fd spelling, isolated for fallback regression tests."""
    candidate = f"/proc/self/fd/{fd}"
    return candidate if os.path.exists(candidate) else None


def _snapshot_fd(source_fd: int) -> tuple[int, str]:
    """Make a private fallback snapshot when procfs is unavailable.

    The random path is still checked against the held descriptor after openssl
    exits.  Extraction reads that descriptor, never the pathname.
    """
    fd, pathname = tempfile.mkstemp(prefix=".app-package.", suffix=".tar.gz")
    try:
        os.lseek(source_fd, 0, os.SEEK_SET)
        while True:
            chunk = os.read(source_fd, 1 << 20)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short write while snapshotting package")
                view = view[written:]
        os.fsync(fd)
        os.lseek(fd, 0, os.SEEK_SET)
        return fd, pathname
    except BaseException:
        os.close(fd)
        try:
            os.unlink(pathname)
        except OSError:
            pass
        raise


@contextlib.contextmanager
def _open_verified_package(pkg_path: str, signature: Optional[str], *,
                           allow_unsigned: bool = False):
    """Yield ``(TarFile, signature_status)`` bound to one open inode.

    Signature lookup happens against the caller's original pathname, then the
    package is opened with ``O_NOFOLLOW`` and re-gated using ``fstat``.  On
    Linux, openssl verifies ``/proc/self/fd/N`` and tarfile reads a duplicate of
    that same open file description.  The proc-less fallback verifies a private
    snapshot and refuses any pathname/inode change before reading its held FD.
    """
    real = _validate_pkg_path(pkg_path)
    signature_b64 = signing.load_signature(real, signature)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        source_fd = os.open(real, flags)
    except OSError as exc:
        raise InstallError(f"cannot open package {real}: {exc}") from exc

    verify_fd = source_fd
    verify_path = _fd_proc_path(source_fd)
    snapshot_path = None
    try:
        info = os.fstat(source_fd)
        if not stat.S_ISREG(info.st_mode):
            raise InstallError(f"package is not a regular file: {real}")
        if info.st_size <= 0:
            raise InstallError("package is empty")
        if info.st_size > paths.MAX_PKG_BYTES:
            raise InstallError(f"package too large: {info.st_size} > {paths.MAX_PKG_BYTES}")

        if verify_path is None:
            verify_fd, snapshot_path = _snapshot_fd(source_fd)
            verify_path = snapshot_path
        before = os.fstat(verify_fd)
        try:
            if allow_unsigned:
                sig_status = signing.verify_package(
                    verify_path, signature_b64, require=False)
            else:
                sig_status = signing.verify_package(verify_path, signature_b64)
        except signing.SignatureError as exc:
            raise InstallError(str(exc)) from exc
        if snapshot_path is not None:
            try:
                after_path = os.stat(snapshot_path, follow_symlinks=False)
            except OSError as exc:
                raise InstallError("package verification snapshot disappeared") from exc
            after_fd = os.fstat(verify_fd)
            if (before.st_dev, before.st_ino, before.st_size) != \
                    (after_fd.st_dev, after_fd.st_ino, after_fd.st_size) or \
                    (after_fd.st_dev, after_fd.st_ino) != \
                    (after_path.st_dev, after_path.st_ino):
                raise InstallError("package verification snapshot was replaced")

        os.lseek(verify_fd, 0, os.SEEK_SET)
        stream = os.fdopen(os.dup(verify_fd), "rb")
        try:
            try:
                tar = tarfile.open(fileobj=stream, mode="r:gz")
            except (OSError, tarfile.TarError) as exc:
                raise InstallError(f"package is not a valid tar.gz: {exc}") from exc
            try:
                yield tar, sig_status
            finally:
                tar.close()
        finally:
            stream.close()
    finally:
        if verify_fd != source_fd:
            os.close(verify_fd)
        os.close(source_fd)
        if snapshot_path is not None:
            try:
                os.unlink(snapshot_path)
            except OSError:
                pass


def _package_records(tar: tarfile.TarFile, members: list[tarfile.TarInfo]) -> dict:
    records = {}
    for member in members:
        if not member.isfile() or member.name in manifest_contract.RESERVED_PACKAGE_PATHS:
            continue
        source = tar.extractfile(member)
        if source is None:
            raise InstallError(f"cannot hash package member {member.name!r}")
        digest = hashlib.sha256()
        size = 0
        with source:
            for chunk in iter(lambda: source.read(1 << 20), b""):
                digest.update(chunk)
                size += len(chunk)
        if size != member.size:
            raise InstallError(f"package member {member.name!r} is truncated")
        records[member.name] = {"sha256": digest.hexdigest(), "size": size}
    return records


def _validate_declared_icon_payload(tar: tarfile.TarFile,
                                    manifest: dict) -> None:
    """Bind a v2 icon declaration to bounded raster bytes in this tar."""
    if (manifest_contract.manifest_version(manifest)
            != manifest_contract.MANIFEST_VERSION
            or "icon" not in manifest):
        return
    icon = manifest_contract.validate_icon_declaration(manifest["icon"])
    try:
        member = tar.getmember(icon["path"])
    except KeyError as exc:
        raise InstallError(
            f"declared icon is missing from package: {icon['path']!r}") from exc
    if not member.isfile():
        raise InstallError(
            f"declared icon must be a regular file: {icon['path']!r}")
    if member.size > paths.MAX_ICON_BYTES:
        raise InstallError(
            f"icon too large: {member.size} > {paths.MAX_ICON_BYTES} "
            f"({icon['path']!r})")
    source = tar.extractfile(member)
    if source is None:
        raise InstallError(f"cannot read declared icon {icon['path']!r}")
    with source:
        prefix = source.read(16)
    if not manifest_contract.icon_bytes_match_media_type(
            prefix, icon["media_type"]):
        raise InstallError(
            "declared icon bytes do not match %s: %r" % (
                icon["media_type"], icon["path"]))


def _inspect_open_tar(tar: tarfile.TarFile, sig_status: dict) -> dict:
    try:
        members = tar.getmembers()
    except (OSError, tarfile.TarError) as exc:
        raise InstallError(f"cannot read package members: {exc}") from exc
    if len(members) > paths.MAX_MEMBERS:
        raise InstallError(f"too many members: {len(members)} > {paths.MAX_MEMBERS}")
    names = [member.name for member in members]
    if len(set(names)) != len(names):
        raise InstallError("package contains duplicate member paths")
    total = 0
    with tempfile.TemporaryDirectory() as probe:
        for member in members:
            _vet_member(member, probe)
            total += max(0, member.size)
            if total > paths.MAX_UNPACKED_BYTES:
                raise InstallError(f"unpacked size exceeds cap {paths.MAX_UNPACKED_BYTES}")

    manifest = _read_manifest_from_tar(tar)
    try:
        manifest_contract.check_platform_compatibility(manifest)
        try:
            kitversion.check(manifest)
        except kitversion.KitIncompatible as exc:
            raise InstallError("kit compatibility check failed: %s" % exc) from exc
        records = _package_records(tar, members)
        manifest_contract.validate_package_files(manifest, records)
        _validate_declared_icon_payload(tar, manifest)
    except manifest_contract.ManifestValidationError as exc:
        raise InstallError(f"invalid package contract: {exc}") from exc

    release_lock = None
    if manifest_contract.manifest_version(manifest) == manifest_contract.MANIFEST_VERSION:
        try:
            lock_bytes = _read_member_bytes(tar, manifest_contract.RELEASE_LOCK_PATH)
            bom_bytes = _read_member_bytes(tar, manifest_contract.BOM_PATH)
            release_lock = json.loads(lock_bytes.decode("utf-8"))
            release_lock = manifest_contract.verify_release_metadata(
                manifest, release_lock, bom_bytes, records)
        except InstallError:
            raise
        except (UnicodeDecodeError, ValueError,
                manifest_contract.ManifestValidationError) as exc:
            raise InstallError(f"invalid manifest-v2 release metadata: {exc}") from exc

    return {
        "id": manifest["id"],
        "version": manifest.get("version"),
        "manifest": manifest,
        "members": names,
        "signature": sig_status,
        "release_lock": release_lock,
    }


def _preflight_summary(info: dict, *, allow_unsigned: bool) -> dict:
    manifest = info["manifest"]
    version = manifest_contract.manifest_version(manifest)
    signed = bool((info.get("signature") or {}).get("signed"))
    return {
        "manifest_version": version,
        "release_id": ((info.get("release_lock") or {}).get("release_id")
                       if version == manifest_contract.MANIFEST_VERSION else None),
        "compatibility": manifest.get("compatibility"),
        "permissions": manifest.get("permissions"),
        "resources": manifest.get("resources"),
        "instances": manifest.get("instances"),
        "health": manifest.get("health"),
        "requires_developer_mode": bool(not signed and paths.REQUIRE_SIGNATURE),
        "developer_mode_allowed": bool(not signed and allow_unsigned),
    }


def _check_release_progression(dest: str, info: dict) -> None:
    """Reject v2 downgrade/equivocation before creating either candidate."""
    if info.get("release_lock") is None or not os.path.isdir(dest):
        return
    try:
        with open(os.path.join(dest, "manifest.json"), "rb") as source:
            old_manifest = json.load(source)
    except FileNotFoundError:
        return
    except (OSError, ValueError) as exc:
        raise InstallError(f"installed manifest cannot be checked for upgrade: {exc}") from exc
    try:
        old_version = manifest_contract.manifest_version(old_manifest)
    except manifest_contract.ManifestValidationError as exc:
        raise InstallError(f"installed manifest has invalid version: {exc}") from exc
    if old_version != manifest_contract.MANIFEST_VERSION:
        return
    old_sequence = (old_manifest.get("release") or {}).get("sequence")
    new_sequence = info["manifest"]["release"]["sequence"]
    if not isinstance(old_sequence, int) or isinstance(old_sequence, bool) or old_sequence <= 0:
        raise InstallError("installed v2 manifest has invalid release.sequence")
    old_release_id = _installed_v2_release_id(dest)
    new_release_id = info["release_lock"]["release_id"]
    if new_sequence < old_sequence:
        raise InstallError(
            f"release downgrade refused: sequence {new_sequence} < installed {old_sequence}")
    if new_sequence == old_sequence and new_release_id != old_release_id:
        raise InstallError(
            f"release sequence equivocation refused: sequence {new_sequence} already identifies "
            f"{old_release_id}")


def inspect(pkg_path: str, signature: Optional[str] = None, *,
            allow_unsigned: bool = False) -> dict:
    """Authenticate and validate a package without installing it.

    Verification, manifest/BOM inspection and tar parsing are all performed
    from the same held inode.  A later call to :func:`install` intentionally
    re-authenticates its own inode; callers must not treat this result as a
    long-lived authorization token.
    """
    with _open_verified_package(
            pkg_path, signature, allow_unsigned=allow_unsigned) as (tar, sig_status):
        info = _inspect_open_tar(tar, sig_status)
    info["preflight"] = _preflight_summary(info, allow_unsigned=allow_unsigned)
    return info


def _set_extracted_modes(root: str) -> None:
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        for dirname in dirnames:
            os.chmod(os.path.join(directory, dirname), 0o755)
        for filename in filenames:
            path = os.path.join(directory, filename)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            os.chmod(path, 0o755 if rel == "run" or rel.endswith(".py") else 0o644)


def _restore_code_after_publish_failure(dest: str, backup: str | None) -> None:
    failed = dest + ".failed"
    shutil.rmtree(failed, ignore_errors=True)
    if os.path.exists(dest):
        os.rename(dest, failed)
    try:
        if backup and os.path.exists(backup):
            os.rename(backup, dest)
        _fsync_dir(paths.APPS_DIR)
    except BaseException:
        if os.path.exists(failed) and not os.path.exists(dest):
            os.rename(failed, dest)
            _fsync_dir(paths.APPS_DIR)
        raise
    shutil.rmtree(failed, ignore_errors=True)


def _installed_v2_release_id(root: str) -> str | None:
    """Read the generation bound to installed v2 code; v1 has no generation."""
    manifest_path = os.path.join(root, "manifest.json")
    try:
        with open(manifest_path, "rb") as source:
            installed_manifest = json.load(source)
    except (OSError, ValueError):
        return None
    try:
        if manifest_contract.manifest_version(installed_manifest) != \
                manifest_contract.MANIFEST_VERSION:
            return None
    except manifest_contract.ManifestValidationError as exc:
        raise InstallError(f"installed manifest has invalid version: {exc}") from exc
    try:
        with open(os.path.join(root, manifest_contract.RELEASE_LOCK_PATH), "rb") as source:
            release_lock = json.load(source)
    except (OSError, ValueError) as exc:
        raise InstallError(f"installed v2 release lock is unreadable: {exc}") from exc
    release_id = release_lock.get("release_id") if isinstance(release_lock, dict) else None
    if not isinstance(release_id, str) or not release_id \
            or "/" in release_id or "\\" in release_id or release_id in (".", ".."):
        raise InstallError("installed v2 release lock has unsafe release_id")
    return release_id


def installed_release_id(app_id: str) -> str | None:
    """Return the installed v2 release identity for preflight comparison."""
    if not paths.valid_app_id(app_id):
        raise InstallError(f"invalid app id {app_id!r}")
    root = paths.app_dir(app_id)
    return _installed_v2_release_id(root) if os.path.isdir(root) else None


def prepare(pkg_path: str, signature: Optional[str] = None, *,
            allow_unsigned: bool = False) -> PreparedInstall:
    """Authenticate and build every fallible candidate without touching live code.

    Release progression, extraction, mode hardening and the complete v2 Python
    environment build all finish here.  Lifecycle orchestration can therefore
    call this while the old process is still running and stop it only after the
    replacement is known to be publishable.
    """
    paths.ensure_dirs()
    staging = None
    env_candidate = None
    try:
        with _open_verified_package(
                pkg_path, signature, allow_unsigned=allow_unsigned) as (tar, sig_status):
            info = _inspect_open_tar(tar, sig_status)
            app_id = info["id"]
            manifest = info["manifest"]
            dest = paths.app_dir(app_id)
            _check_release_progression(dest, info)
            staging = tempfile.mkdtemp(prefix=f".{app_id}.stage.", dir=paths.APPS_DIR)
            extract_vetted_tar(tar, staging)
        _set_extracted_modes(staging)

        if info["release_lock"] is not None:
            try:
                env_candidate = pythonenv.stage_environment(
                    staging, manifest, info["release_lock"])
            except pythonenv.PythonEnvError as exc:
                raise InstallError(f"cannot stage Python environment: {exc}") from exc
        info["preflight"] = _preflight_summary(
            info, allow_unsigned=allow_unsigned)
        result = PreparedInstall(
            app_id=app_id, manifest=manifest, info=info, dest=dest,
            staging=staging, env_candidate=env_candidate)
        staging = None
        env_candidate = None
        return result
    finally:
        if staging and os.path.isdir(staging):
            shutil.rmtree(staging, ignore_errors=True)
        if env_candidate is not None:
            pythonenv.discard_candidate(env_candidate)


def discard_prepared(candidate: PreparedInstall) -> None:
    """Best-effort cleanup for an unpublished/failed prepared candidate."""
    if not isinstance(candidate, PreparedInstall) or candidate.consumed:
        return
    if candidate.staging and os.path.isdir(candidate.staging):
        shutil.rmtree(candidate.staging, ignore_errors=True)
    candidate.staging = None
    if candidate.env_candidate is not None:
        pythonenv.discard_candidate(candidate.env_candidate)


def commit_prepared(candidate: PreparedInstall) -> Tuple[str, dict]:
    """Atomically publish a :func:`prepare` result, rolling back on error."""
    if not isinstance(candidate, PreparedInstall):
        raise InstallError("invalid prepared install")
    if candidate.consumed or not candidate.staging:
        raise InstallError("prepared install was already consumed")
    app_id = candidate.app_id
    dest = candidate.dest
    backup = None
    code_published = False

    # Rescue the user's config before the code-directory swap.  The server's
    # surrounding transaction snapshots both locations before calling us, so
    # a later schema/READY failure can reverse this migration byte-for-byte.
    try:
        appconfig.migrate_legacy_config(app_id)
    except OSError:
        pass

    try:
        mark_install_transaction(candidate, "publishing_code")
        if os.path.exists(dest):
            backup = dest + ".prev"
            if os.path.exists(backup):
                shutil.rmtree(backup, ignore_errors=True)
            os.rename(dest, backup)
        try:
            os.rename(candidate.staging, dest)
            candidate.staging = None
            candidate.published = True
            code_published = True
            _fsync_dir(paths.APPS_DIR)
        except BaseException:
            if backup and os.path.exists(backup) and not os.path.exists(dest):
                os.rename(backup, dest)
                _fsync_dir(paths.APPS_DIR)
            raise
        mark_install_transaction(candidate, "code_published")

        if candidate.env_candidate is not None:
            try:
                mark_install_transaction(candidate, "publishing_environment")
                pythonenv.activate_environment(candidate.env_candidate)
                mark_install_transaction(candidate, "environment_published")
            except BaseException as exc:
                try:
                    pythonenv.rollback_candidate_activation(candidate.env_candidate)
                    _restore_code_after_publish_failure(dest, backup)
                    candidate.published = False
                    code_published = False
                except BaseException as rollback_exc:
                    raise InstallError(
                        f"Python environment activation failed ({exc}); "
                        f"transaction rollback also failed: {rollback_exc}") from rollback_exc
                if isinstance(exc, Exception):
                    raise InstallError(f"cannot activate Python environment: {exc}") from exc
                raise
        else:
            mark_install_transaction(candidate, "environment_published")

        stale = dest + ".old"
        if os.path.isdir(stale):
            shutil.rmtree(stale, ignore_errors=True)
        candidate.consumed = True
        return app_id, candidate.manifest
    except BaseException:
        # The explicit environment-activation branch already restores code.
        # This fallback covers a future post-publish step without allowing an
        # exception to escape while live code remains half-swapped.
        if code_published:
            try:
                if candidate.env_candidate is not None:
                    pythonenv.rollback_candidate_activation(candidate.env_candidate)
                _restore_code_after_publish_failure(dest, backup)
                candidate.published = False
            except BaseException as rollback_exc:
                raise InstallError(
                    "install publish failed and rollback also failed: %s" %
                    rollback_exc) from rollback_exc
        raise


def install(pkg_path: str, signature: Optional[str] = None, *,
            allow_unsigned: bool = False) -> Tuple[str, dict]:
    """Authenticate, prepare and atomically publish one app release."""
    candidate = prepare(
        pkg_path, signature, allow_unsigned=allow_unsigned)
    try:
        return commit_prepared(candidate)
    finally:
        discard_prepared(candidate)


def restore_prev(app_id: str) -> bool:
    """Restore retained code and its per-release Python generation together.

    Used by the upgrade transaction when the NEW version fails to come up: the
    broken dir is moved aside to `<id>.failed` (then removed) and `.prev` is
    renamed back to the live dir.  A v2 environment activation is rolled back
    in the same transaction.  If that refuses, the directory swap is reversed
    so runtime never observes old code with the new interpreter.

    Returns False when there is no `.prev` to restore. Renames are fsync'd like
    install().
    """
    if not paths.valid_app_id(app_id):
        raise InstallError(f"invalid app id {app_id!r}")
    dest = paths.app_dir(app_id)
    prev = dest + ".prev"
    if not os.path.isdir(prev):
        return False
    failed_release = _installed_v2_release_id(dest) if os.path.isdir(dest) else None
    previous_release = _installed_v2_release_id(prev)
    failed = dest + ".failed"
    if os.path.exists(dest):
        shutil.rmtree(failed, ignore_errors=True)
        os.rename(dest, failed)
    try:
        os.rename(prev, dest)
        _fsync_dir(paths.APPS_DIR)
        try:
            if failed_release is not None:
                pythonenv.restore_previous(app_id)
                active_release = pythonenv.current_release_id(app_id)
                if active_release == failed_release and previous_release != failed_release:
                    raise pythonenv.PythonEnvError(
                        "environment rollback did not leave the failed release")
                if previous_release is not None and active_release != previous_release:
                    raise pythonenv.PythonEnvError(
                        f"restored code requires {previous_release!r}, current environment is "
                        f"{active_release!r}")
        except pythonenv.PythonEnvError as exc:
            # Reverse the code swap; the environment helper fails before
            # switching on contract errors, so code/env stay on the new release.
            os.rename(dest, prev)
            if os.path.exists(failed):
                os.rename(failed, dest)
            _fsync_dir(paths.APPS_DIR)
            raise InstallError(f"cannot restore previous Python environment: {exc}") from exc
    except BaseException:
        if not os.path.exists(dest) and os.path.exists(failed):
            os.rename(failed, dest)
            _fsync_dir(paths.APPS_DIR)
        raise
    shutil.rmtree(failed, ignore_errors=True)
    return True


def _reconcile_transaction_environment(transaction: dict,
                                       expected_release: str | None) -> bool:
    """Leave ``current`` on the pre-transaction release, idempotently."""
    app_id = transaction["app_id"]
    target_release = transaction.get("target_release_id")
    try:
        current_release = pythonenv.current_release_id(app_id)
        if (isinstance(target_release, str)
                and target_release != expected_release
                and current_release == target_release):
            pythonenv.restore_previous(app_id)
            current_release = pythonenv.current_release_id(app_id)
        if current_release != expected_release:
            raise pythonenv.PythonEnvError(
                "current=%r, expected pre-transaction release %r" % (
                    current_release, expected_release))
    except pythonenv.PythonEnvError as exc:
        raise InstallError(
            "cannot reconcile interrupted Python environment: %s" % exc) from exc
    return current_release == expected_release


def rollback_install_transaction_files(transaction: dict) -> bool:
    """Restore code/environment described by an unfinished phase journal.

    Directory device/inode identities distinguish the retained pre-upgrade
    release from an older, perfectly legitimate ``.prev`` generation.  This
    prevents a crash before the first rename from accidentally downgrading an
    otherwise untouched app during boot reconciliation.
    """
    if not isinstance(transaction, dict):
        raise InstallError("invalid install transaction")
    app_id = transaction.get("app_id")
    if not isinstance(app_id, str) or not paths.valid_app_id(app_id):
        raise InstallError("invalid install transaction app id")
    dest = paths.app_dir(app_id)
    prev = dest + ".prev"
    current_identity = _directory_identity(dest)
    previous_identity = transaction.get("previous_dir_identity")
    target_identity = transaction.get("target_dir_identity")
    pre_installed = transaction.get("pre_installed") is True

    if pre_installed:
        if current_identity == previous_identity:
            # No code rename became visible.  A pre-existing .prev belongs to
            # an older successful upgrade and must remain untouched.
            installed_release = transaction.get("installed_release_id")
            expected_environment = (
                installed_release if isinstance(installed_release, str)
                else transaction.get("previous_environment_release_id"))
            _reconcile_transaction_environment(
                transaction, expected_environment)
            failed = dest + ".failed"
            if _directory_identity(failed) == target_identity:
                shutil.rmtree(failed)
                _fsync_dir(paths.APPS_DIR)
            return False
        if current_identity is None:
            if _directory_identity(prev) != previous_identity:
                raise InstallError(
                    "interrupted install lost both the live and retained release")
            failed = dest + ".failed"
            failed_identity = _directory_identity(failed)
            if (failed_identity is not None
                    and failed_identity != target_identity):
                raise InstallError(
                    "interrupted install failed-copy identity changed")
            installed_release = transaction.get("installed_release_id")
            expected_environment = (
                installed_release if isinstance(installed_release, str)
                else transaction.get("previous_environment_release_id"))
            _reconcile_transaction_environment(
                transaction, expected_environment)
            os.rename(prev, dest)
            _fsync_dir(paths.APPS_DIR)
            if failed_identity is not None:
                shutil.rmtree(failed)
                _fsync_dir(paths.APPS_DIR)
            return True
        if current_identity != target_identity:
            raise InstallError(
                "installed code identity changed during interrupted transaction")
        if _directory_identity(prev) != previous_identity:
            raise InstallError(
                "retained release identity changed during interrupted transaction")
        target_release = transaction.get("target_release_id")
        installed_release = transaction.get("installed_release_id")
        expected_environment = (
            installed_release if isinstance(installed_release, str)
            else transaction.get("previous_environment_release_id"))
        restore_environment = False
        if isinstance(target_release, str) and target_release != expected_environment:
            try:
                current_release = pythonenv.current_release_id(app_id)
            except pythonenv.PythonEnvError as exc:
                raise InstallError(
                    "cannot inspect interrupted Python environment: %s" % exc) from exc
            if current_release == target_release:
                restore_environment = True
            elif current_release != expected_environment:
                raise InstallError(
                    "interrupted environment identity changed: current=%r, "
                    "installed=%r, target=%r" % (
                        current_release, expected_environment, target_release))

        # Restore code first but consume pythonenv.rollback.json only when the
        # active pointer proves THIS transaction switched to target_release.
        # An older successful activation may legitimately have left its own
        # rollback record; consuming it while current still equals the installed
        # release would incorrectly jump back another generation.
        failed = dest + ".failed"
        shutil.rmtree(failed, ignore_errors=True)
        os.rename(dest, failed)
        environment_changed = False
        try:
            os.rename(prev, dest)
            _fsync_dir(paths.APPS_DIR)
            if restore_environment:
                pythonenv.restore_previous(app_id)
                environment_changed = True
                restored_release = pythonenv.current_release_id(app_id)
                if restored_release != expected_environment:
                    raise pythonenv.PythonEnvError(
                        "environment rollback restored %r, expected %r" % (
                            restored_release, expected_environment))
        except BaseException as exc:
            try:
                if not environment_changed:
                    if os.path.isdir(dest):
                        os.rename(dest, prev)
                    if os.path.isdir(failed):
                        os.rename(failed, dest)
                    _fsync_dir(paths.APPS_DIR)
            except BaseException as reverse_exc:
                raise InstallError(
                    "interrupted install rollback failed (%s) and code reversal "
                    "also failed: %s" % (exc, reverse_exc)) from reverse_exc
            if isinstance(exc, pythonenv.PythonEnvError):
                raise InstallError(
                    "cannot restore interrupted Python environment: %s" % exc) from exc
            raise
        shutil.rmtree(failed, ignore_errors=True)
        return True

    if current_identity is None:
        _reconcile_transaction_environment(
            transaction, transaction.get("previous_environment_release_id"))
        return False
    if current_identity != target_identity:
        raise InstallError(
            "new install directory identity changed during interrupted transaction")
    _reconcile_transaction_environment(
        transaction, transaction.get("previous_environment_release_id"))
    shutil.rmtree(dest)
    _fsync_dir(paths.APPS_DIR)
    return True


def validate_committed_transaction_files(transaction: dict) -> None:
    """Fail closed unless a terminal journal still names the published pair."""
    app_id = transaction.get("app_id") if isinstance(transaction, dict) else None
    if not isinstance(app_id, str) or not paths.valid_app_id(app_id):
        raise InstallError("invalid committed install transaction")
    if _directory_identity(paths.app_dir(app_id)) != transaction.get(
            "target_dir_identity"):
        raise InstallError("committed install code identity does not match journal")
    target_release = transaction.get("target_release_id")
    if isinstance(target_release, str):
        try:
            current = pythonenv.current_release_id(app_id)
        except pythonenv.PythonEnvError as exc:
            raise InstallError(
                "cannot inspect committed Python environment: %s" % exc) from exc
        if current != target_release:
            raise InstallError(
                "committed code/environment mismatch: code=%r env=%r" % (
                    target_release, current))


def reconcile_interrupted_installs() -> list:
    """Recover installs a crash/power-cut interrupted mid-swap (健壮#16).

    install() does `dest -> <id>.prev` then `staging -> dest`. A crash between
    the two leaves `<id>.prev` with NO live dir -- the app has vanished though
    its previous version sits right there. On boot, for every `<id>.prev` whose
    live `<id>` dir is missing, rename it back. Also sweeps orphaned staging dirs
    (`.<id>.stage.*`) and abandoned `<id>.failed` copies from a rollback that
    died. Returns the list of app ids restored. Best-effort per entry."""
    restored = []
    try:
        names = os.listdir(paths.APPS_DIR)
    except OSError:
        return restored
    for name in names:
        full = os.path.join(paths.APPS_DIR, name)
        # orphaned in-flight extraction dir -> just remove it
        if name.startswith(".") and ".stage." in name:
            shutil.rmtree(full, ignore_errors=True)
            continue
        if name.endswith(".failed"):
            shutil.rmtree(full, ignore_errors=True)
            continue
        if not name.endswith(".prev"):
            continue
        base = name[:-len(".prev")]
        if not paths.valid_app_id(base):
            continue
        dest = paths.app_dir(base)
        if os.path.isdir(dest):
            continue                     # live dir present -> normal one-gen .prev
        if not os.path.isdir(full):
            continue
        try:
            os.rename(full, dest)
            _fsync_dir(paths.APPS_DIR)
            restored.append(base)
        except OSError:
            pass
    # A crash during prepare() happens before a code-directory rename but can
    # leave an unpublished v2 environment candidate.  Sweep only valid app-id
    # roots; immutable published generations/current pointers are preserved by
    # pythonenv's own reconciliation contract.
    try:
        env_apps = os.listdir(paths.VENVS_DIR)
    except OSError:
        env_apps = []
    for app_id in env_apps:
        if not paths.valid_app_id(app_id):
            continue
        try:
            pythonenv.reconcile_staging(app_id)
        except pythonenv.PythonEnvError:
            pass
    return restored


def uninstall(app_id: str, purge_config: bool = False) -> None:
    """Remove an app's on-disk artifacts: its install dir, the retained
    `<app_dir>.prev` rollback copy and (if present) its per-app venv.

    ★User config is KEPT by default★ (/userdata/local/appdata/<id>/config.json).
    Uninstall is routinely used as "reinstall/upgrade by hand", and the settings
    it holds -- thresholds, ROI/counting lines, output channel + field mapping --
    are minutes of manual work that no package can regenerate. Reinstalling the
    same id therefore restores the previous behaviour. Callers that really want a
    clean slate pass purge_config=True (nothing in the HTTP API does today).

    We deliberately touch ONLY the app's own dirs. Models under
    /userdata/local/models are SHARED across apps (one-gen models[]+target_path),
    so removing a single app must never delete them -- this function has no path
    into the models tree by construction.

    Idempotent: missing dirs are skipped, so uninstalling something already gone
    (or an app that never grew a venv) is a no-op rather than an error.
    """
    if app_id in manifest_contract.RESERVED_APP_IDS:
        raise InstallError("a firmware system application cannot be uninstalled")
    if not paths.valid_app_id(app_id):
        raise InstallError(f"invalid app id {app_id!r}")
    dest = paths.app_dir(app_id)
    if os.path.isdir(dest):
        shutil.rmtree(dest, ignore_errors=True)
    for leftover in (dest + ".prev", dest + ".old"):
        if os.path.isdir(leftover):
            shutil.rmtree(leftover, ignore_errors=True)
    if purge_config:
        shutil.rmtree(paths.appdata_dir(app_id), ignore_errors=True)
    # Remove every immutable environment generation owned by this app.
    venv = paths.venv_dir(app_id)
    if os.path.isdir(venv):
        shutil.rmtree(venv, ignore_errors=True)

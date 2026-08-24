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
import shutil
import stat
import tarfile
import tempfile
from typing import Optional, Tuple

from . import config as appconfig, manifest as manifest_contract, paths, pythonenv, signing


class InstallError(Exception):
    pass


MAX_METADATA_BYTES = 4 * 1024 * 1024


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
        records = _package_records(tar, members)
        manifest_contract.validate_package_files(manifest, records)
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


def install(pkg_path: str, signature: Optional[str] = None, *,
            allow_unsigned: bool = False) -> Tuple[str, dict]:
    """Authenticate, stage and atomically publish one app release.

    v2 packages additionally build their offline per-release environment while
    both the old code and old ``current`` interpreter are still untouched.  If
    publishing either side fails, both are restored before the error escapes.
    """
    paths.ensure_dirs()
    staging = None
    env_candidate = None
    backup = None
    app_id = None
    manifest = None
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

        # Rescue the user's config before the code-directory swap.
        try:
            appconfig.migrate_legacy_config(app_id)
        except OSError:
            pass

        if os.path.exists(dest):
            backup = dest + ".prev"
            if os.path.exists(backup):
                shutil.rmtree(backup, ignore_errors=True)
            os.rename(dest, backup)
        try:
            os.rename(staging, dest)
            staging = None
            _fsync_dir(paths.APPS_DIR)
        except BaseException:
            if backup and os.path.exists(backup) and not os.path.exists(dest):
                os.rename(backup, dest)
                _fsync_dir(paths.APPS_DIR)
            raise

        if env_candidate is not None:
            try:
                pythonenv.activate_environment(env_candidate)
            except BaseException as exc:
                try:
                    pythonenv.rollback_candidate_activation(env_candidate)
                    _restore_code_after_publish_failure(dest, backup)
                except BaseException as rollback_exc:
                    raise InstallError(
                        f"Python environment activation failed ({exc}); "
                        f"transaction rollback also failed: {rollback_exc}") from rollback_exc
                if isinstance(exc, Exception):
                    raise InstallError(f"cannot activate Python environment: {exc}") from exc
                raise

        stale = dest + ".old"
        if os.path.isdir(stale):
            shutil.rmtree(stale, ignore_errors=True)
        return app_id, manifest
    finally:
        if staging and os.path.isdir(staging):
            shutil.rmtree(staging, ignore_errors=True)
        if env_candidate is not None and staging is not None:
            pythonenv.discard_candidate(env_candidate)


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

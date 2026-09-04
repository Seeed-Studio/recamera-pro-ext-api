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

import json
import os
import shutil
import tarfile
import tempfile
from typing import Optional, Tuple

from . import config as appconfig, kitversion, paths, signing


class InstallError(Exception):
    pass


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
        with open(outp, "wb") as w:
            shutil.copyfileobj(f, w)
        os.chmod(outp, 0o644)
        names.append(m.name)
    return names


def _read_manifest_from_tar(tar: tarfile.TarFile) -> dict:
    try:
        member = tar.getmember("manifest.json")
    except KeyError:
        raise InstallError("package has no manifest.json at top level")
    f = tar.extractfile(member)
    if f is None:
        raise InstallError("cannot read manifest.json")
    try:
        return json.loads(f.read().decode("utf-8"))
    except Exception as e:
        raise InstallError(f"manifest.json is not valid JSON: {e}")


def inspect(pkg_path: str, signature: Optional[str] = None) -> dict:
    """Validate a package WITHOUT installing; return {id, version, manifest,
    members, signature}.

    Authenticity (TODO #4) is checked FIRST, before we spend any effort parsing
    the tar: verify the detached release signature (base64 arg, or `<pkg>.sig`
    sidecar) over the raw .tar.gz bytes using the device's openssl + the baked-in
    public key. A bad signature -> InstallError; an unsigned package -> InstallError
    unless policy (paths.REQUIRE_SIGNATURE) allows it. This runs in addition to the
    existing sha256/zip-slip defences, not instead of them.
    """
    real = _validate_pkg_path(pkg_path)
    try:
        sig_status = signing.verify_package(real, signature)
    except signing.SignatureError as e:
        raise InstallError(str(e))
    with tarfile.open(real, "r:gz") as tar:
        # Vet every member against a throwaway root (traversal/type checks only).
        members = tar.getmembers()
        if len(members) > paths.MAX_MEMBERS:
            raise InstallError(f"too many members: {len(members)} > {paths.MAX_MEMBERS}")
        total = 0
        with tempfile.TemporaryDirectory() as probe:
            for m in members:
                _vet_member(m, probe)
                total += max(0, m.size)
                if total > paths.MAX_UNPACKED_BYTES:
                    raise InstallError(f"unpacked size exceeds cap {paths.MAX_UNPACKED_BYTES}")
        manifest = _read_manifest_from_tar(tar)
    app_id = manifest.get("id")
    if not paths.valid_app_id(app_id):
        raise InstallError(f"manifest id {app_id!r} not in whitelist [a-z0-9-]{{1,64}}")
    # Kit compatibility LAST, after identity/authenticity: an app package does not
    # carry the shared kit runtime, so one built against a newer kit installs fine
    # and then dies at startup with ModuleNotFoundError. Checked here rather than
    # in install() so the "validate without installing" path reports the same
    # verdict, and so the refusal lands before anything is extracted.
    try:
        kit_have = kitversion.check(manifest)
    except kitversion.KitIncompatible as e:
        raise InstallError(str(e))
    return {
        "id": app_id,
        "version": manifest.get("version"),
        "manifest": manifest,
        "members": [m.name for m in members],
        "signature": sig_status,
        "kit_required": manifest.get("kit"),
        "kit_installed": kit_have,
    }


def install(pkg_path: str, signature: Optional[str] = None) -> Tuple[str, dict]:
    """Validate + extract package into /userdata/local/apps/<id>/. Returns (id, manifest).

    Signature verification happens up front via inspect(); a package that is
    unsigned-under-policy or carries a bad signature never reaches extraction.

    Extraction is atomic-ish: unpack to a temp dir next to APPS_DIR, then swap
    the target dir into place (old dir moved aside then removed).
    """
    info = inspect(pkg_path, signature)
    app_id = info["id"]
    manifest = info["manifest"]
    real = os.path.realpath(pkg_path)

    paths.ensure_dirs()
    dest = paths.app_dir(app_id)
    staging = tempfile.mkdtemp(prefix=f".{app_id}.stage.", dir=paths.APPS_DIR)
    try:
        with tarfile.open(real, "r:gz") as tar:
            for m in tar.getmembers():
                _vet_member(m, staging)          # re-vet at extract time
                if m.isdir():
                    os.makedirs(os.path.join(staging, m.name), exist_ok=True)
                else:
                    f = tar.extractfile(m)
                    if f is None:
                        raise InstallError(f"cannot extract member {m.name!r}")
                    outp = os.path.join(staging, m.name)
                    os.makedirs(os.path.dirname(outp) or staging, exist_ok=True)
                    with open(outp, "wb") as w:
                        shutil.copyfileobj(f, w)
                    os.chmod(outp, 0o755 if m.name == "run" or outp.endswith(".py") else 0o644)
        # ★Rescue the user's config BEFORE the dir swap★. On a device upgraded
        # from an older appmgr the settings still sit in <app_dir>/config.json,
        # which the swap below throws away. Move them to the appdata tree first.
        # Best-effort: a corrupt/unreadable legacy file must not block install.
        try:
            appconfig.migrate_legacy_config(app_id)
        except OSError:
            pass
        # atomic-ish swap. The previous install is KEPT as <app_dir>.prev (one
        # generation) so a bad upgrade can be rolled back by hand; the next
        # upgrade replaces it. `.prev`/`.old` contain a dot -> never a valid app
        # id, so do_list() skips them.
        backup = None
        if os.path.exists(dest):
            backup = dest + ".prev"
            if os.path.exists(backup):
                shutil.rmtree(backup, ignore_errors=True)
            os.rename(dest, backup)
        os.rename(staging, dest)
        staging = None
        # ★Durability★: fsync the parent dir so the two renames above are on
        # stable storage. Without it a power cut between `dest->.prev` and
        # `staging->dest` can leave ONLY `.prev` (dest gone) -- reconcile_
        # interrupted_installs() recovers that on the next boot, but fsyncing
        # here makes the window as small as the FS allows (健壮#16).
        _fsync_dir(paths.APPS_DIR)
        # retire the pre-.prev naming if an older appmgr left one behind
        stale = dest + ".old"
        if os.path.isdir(stale):
            shutil.rmtree(stale, ignore_errors=True)
    finally:
        if staging and os.path.isdir(staging):
            shutil.rmtree(staging, ignore_errors=True)
    return app_id, manifest


def restore_prev(app_id: str) -> bool:
    """Swap the retained `<id>.prev` rollback copy back into place (健壮#15).

    Used by the upgrade transaction when the NEW version fails to come up: the
    broken dir is moved aside to `<id>.failed` (then removed) and `.prev` is
    renamed back to the live dir. Returns False when there is no `.prev` to
    restore (nothing to roll back to). Renames are fsync'd like install()."""
    if not paths.valid_app_id(app_id):
        raise InstallError(f"invalid app id {app_id!r}")
    dest = paths.app_dir(app_id)
    prev = dest + ".prev"
    if not os.path.isdir(prev):
        return False
    failed = dest + ".failed"
    if os.path.exists(dest):
        shutil.rmtree(failed, ignore_errors=True)
        os.rename(dest, failed)
    os.rename(prev, dest)
    _fsync_dir(paths.APPS_DIR)
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
    # Future per-app venv hook: remove /userdata/local/venvs/<id> if it exists.
    venv = paths.venv_dir(app_id)
    if os.path.isdir(venv):
        shutil.rmtree(venv, ignore_errors=True)

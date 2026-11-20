"""Install firmware-carried applications once, through normal app transactions.

Firmware authenticates these bytes by carrying both the archive and its digest
in its immutable image. This trust never follows a package copied to userdata;
store/upload packages continue to require their normal publisher signature.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import stat
import tempfile
import time

from . import config, paths, state


# Deliberately not environment-configurable or supplied by an HTTP request.
FIRMWARE_DIR = "/usr/share/recamera/default-apps"
LEGACY_MOTION_DIR = "/userdata/local/motion-detect"
LEGACY_MOTION_PID = "/var/run/motion-detectd.pid"
LEGACY_MOTION_CODE_DIR = "/usr/share/motion-detect"


def _secure_file(path: str) -> bytes:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o022 or info.st_size > 256 * 1024):
            raise ValueError("unsafe firmware defaults metadata: %s" % path)
        with os.fdopen(fd, "rb", closefd=False) as stream:
            return stream.read(256 * 1024 + 1)
    finally:
        os.close(fd)


def catalog() -> list[dict]:
    if not os.path.lexists(FIRMWARE_DIR):
        return []
    if os.path.realpath(FIRMWARE_DIR) != os.path.abspath(FIRMWARE_DIR):
        raise ValueError("firmware defaults may not use symlinks")
    info = os.stat(FIRMWARE_DIR)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o022):
        raise ValueError("unsafe firmware defaults directory")
    data = json.loads(_secure_file(os.path.join(FIRMWARE_DIR, "index.json")))
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("apps"), list):
        raise ValueError("invalid firmware defaults index")
    seen = set()
    for item in data["apps"]:
        if (not isinstance(item, dict) or not paths.valid_app_id(item.get("id"))
                or item["id"] in seen
                or not isinstance(item.get("package"), str)
                or os.path.basename(item["package"]) != item["package"]
                or not item["package"].endswith(".tar.gz")
                or not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256", "")))):
            raise ValueError("invalid firmware defaults entry")
        seen.add(item["id"])
    return data["apps"]


def is_firmware_path(path: str) -> bool:
    return os.path.dirname(os.path.abspath(path)) == FIRMWARE_DIR


def verify_package(path: str, fd: int) -> dict:
    """Authenticate the installer's held archive inode against the image index."""
    if not is_firmware_path(path):
        raise ValueError("package is not firmware content")
    entries = [item for item in catalog() if item["package"] == os.path.basename(path)]
    if len(entries) != 1:
        raise ValueError("package is not declared by the firmware")
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o022):
        raise ValueError("unsafe firmware package")
    digest = hashlib.sha256()
    offset = 0
    while block := os.pread(fd, 1024 * 1024, offset):
        digest.update(block)
        offset += len(block)
    if digest.hexdigest() != entries[0]["sha256"]:
        raise ValueError("firmware package digest mismatch")
    return {"signed": False, "verified": True, "signer_kind": "firmware",
            "firmware_app_id": entries[0]["id"]}


def _load_record(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
    except FileNotFoundError:
        return {"version": 1, "initialized": [], "pending": []}
    if (not isinstance(data, dict) or data.get("version") != 1
            or not isinstance(data.get("initialized"), list)
            or not all(isinstance(x, str) for x in data["initialized"])
            or not isinstance(data.get("pending", []), list)
            or not all(isinstance(x, str) for x in data.get("pending", []))):
        raise ValueError("invalid default-app initialization record")
    data.setdefault("pending", [])
    return data


def _save_record(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".default-apps-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(os.path.dirname(path), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _retire_legacy_motion() -> None:
    """Stop only a verified old daemon; keep its data for recovery."""
    try:
        with open(LEGACY_MOTION_PID, encoding="ascii") as stream:
            pid = int(stream.read().strip())
    except (FileNotFoundError, ValueError):
        return
    if pid <= 1:
        raise ValueError("invalid legacy motion PID")

    def matches():
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as stream:
                argv = stream.read().split(b"\0")
            cwd = os.readlink(f"/proc/{pid}/cwd").removesuffix(" (deleted)")
            scripts = {os.fsencode(os.path.join(LEGACY_MOTION_CODE_DIR, "motion_daemon.py"))}
            if cwd == LEGACY_MOTION_CODE_DIR:
                scripts.add(b"motion_daemon.py")
            return (os.path.basename(argv[0]).startswith(b"python")
                    and any(arg in scripts for arg in argv[1:]))
        except FileNotFoundError:
            return False

    if matches():
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 10.0
        while matches():
            if time.monotonic() >= deadline:
                raise RuntimeError("legacy motion daemon did not stop")
            time.sleep(0.1)


def _migrate_motion_config(manifest: dict) -> str:
    app_id = manifest["id"]
    canonical = os.path.join(paths.appdata_dir(app_id), "config.json")
    legacy = os.path.join(LEGACY_MOTION_DIR, "config.json")
    if not os.path.exists(canonical) and os.path.isfile(legacy):
        with open(legacy, encoding="utf-8") as stream:
            old = json.load(stream)
        # Removed debug HTTP keys are not part of the managed application's UI.
        specs = config.schema_specs(manifest)
        if not isinstance(old, dict):
            raise ValueError("invalid legacy motion config")
        clean, errors = config.validate_config(
            manifest, {key: val for key, val in old.items() if key in specs})
        if errors:
            raise ValueError("invalid legacy motion config: " + "; ".join(errors))
        config.write_user_config(app_id, clean)
    desired_path = os.path.join(paths.APPMGR_DIR, "motion-detect.json")
    try:
        with open(desired_path, encoding="utf-8") as stream:
            desired = json.load(stream).get("desired")
    except FileNotFoundError:
        desired = None
    return "running" if desired == "running" else "stopped"


def reconcile(*, install, audit) -> None:
    """Called before listeners/boot restore. Never start a fresh default app."""
    record_path = os.path.join(paths.APPMGR_DIR, "default-apps.json")
    try:
        entries = catalog()
        if not entries:
            return
        record = _load_record(record_path)
    except Exception as exc:
        audit("default_apps_failed", error=str(exc))
        return
    for entry in entries:
        app_id = entry["id"]
        if app_id in record["initialized"]:
            continue
        try:
            if app_id == "motion-detect":
                _retire_legacy_motion()
            if not os.path.isdir(paths.app_dir(app_id)):
                if app_id not in record["pending"]:
                    record["pending"].append(app_id)
                    _save_record(record_path, record)
                result = install(os.path.join(FIRMWARE_DIR, entry["package"]))
                if result.get("id") != app_id:
                    raise ValueError("firmware package installed an unexpected application")
            if app_id in record["pending"] and app_id == "motion-detect":
                with open(os.path.join(paths.app_dir(app_id), "manifest.json"), encoding="utf-8") as stream:
                    desired = _migrate_motion_config(json.load(stream))
                # An explicit legacy user choice survives; a factory device
                # has no choice and remains stopped after installation.
                state.set_desired(app_id, desired, launch_mode="managed")
            record["initialized"].append(app_id)
            if app_id in record["pending"]:
                record["pending"].remove(app_id)
            _save_record(record_path, record)
            audit("default_app_initialized", id=app_id)
        except Exception as exc:
            audit("default_app_failed", id=app_id, error=str(exc))

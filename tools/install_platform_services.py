#!/usr/bin/env python3
"""Stage appmgr/inferenced and their boot/edge integration into firmware.

The service code is immutable rootfs content.  Applications, environments,
configuration, operation journals and logs remain under ``/userdata`` at
runtime.  This script deliberately excludes every test/cache/build artifact and
replaces package trees atomically so an incremental app build cannot retain a
file removed from source.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import shutil
import stat
import tempfile
from pathlib import Path


EXCLUDED_DIRS = {"__pycache__", ".pytest_cache", "tests", "build"}


def _ignore(_directory: str, names: list[str]) -> list[str]:
    return [
        name
        for name in names
        if name in EXCLUDED_DIRS
        or name.endswith((".pyc", ".pyo", ".egg-info"))
        or fnmatch.fnmatch(name, "test_*.py")
    ]


def _validate_source_tree(source: Path) -> None:
    if not source.is_dir():
        raise FileNotFoundError(source)
    for root, dirs, files in os.walk(source, followlinks=False):
        for name in dirs + files:
            path = Path(root, name)
            if path.is_symlink():
                raise ValueError(f"platform service source must not contain symlinks: {path}")


def _replace_tree(source: Path, destination: Path) -> None:
    _validate_source_tree(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.stage.", dir=destination.parent)
    )
    backup = destination.with_name(f".{destination.name}.old.{os.getpid()}")
    try:
        shutil.rmtree(stage)
        shutil.copytree(source, stage, ignore=_ignore)
        if backup.exists():
            shutil.rmtree(backup)
        if destination.exists():
            os.replace(destination, backup)
        os.replace(stage, destination)
        if backup.exists():
            shutil.rmtree(backup)
    except BaseException:
        if not destination.exists() and backup.exists():
            os.replace(backup, destination)
        raise
    finally:
        if stage.exists():
            shutil.rmtree(stage)
        if backup.exists():
            shutil.rmtree(backup)


def _copy_file(source: Path, destination: Path, mode: int) -> None:
    if not source.is_file() or source.is_symlink():
        raise FileNotFoundError(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with source.open("rb") as reader, os.fdopen(fd, "wb") as writer:
            shutil.copyfileobj(reader, writer, 1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _assert_clean(root: Path) -> None:
    for path in root.rglob("*"):
        if (
            any(part in EXCLUDED_DIRS for part in path.parts)
            or path.name.endswith((".pyc", ".pyo", ".egg-info"))
            or fnmatch.fnmatch(path.name, "test_*.py")
        ):
            raise ValueError(f"forbidden platform artifact was staged: {path}")
        if path.is_symlink():
            raise ValueError(f"unexpected platform service symlink: {path}")


def stage(repo: Path, rootfs: Path, oem: Path) -> None:
    platform = rootfs / "usr/lib/recamera"
    _replace_tree(repo / "market/appmgr", platform / "appmgr")
    _replace_tree(repo / "market/inferenced", platform / "inferenced")
    # Source worktrees are commonly group-writable.  Trust anchors are not:
    # signing.py deliberately rejects any group/world-writable public key so a
    # build user cannot accidentally turn a mutable checkout mode into a
    # mutable device trust root.  Normalize the immutable vendor anchor after
    # copytree (which otherwise preserves the source mode).
    vendor_key = platform / "appmgr/keys/release_pub.pem"
    if not vendor_key.is_file() or vendor_key.is_symlink():
        raise FileNotFoundError(vendor_key)
    os.chmod(vendor_key, 0o644)
    _copy_file(repo / "market/deploy/S93inferenced", rootfs / "etc/init.d/S93inferenced", 0o755)
    _copy_file(repo / "market/deploy/S94appmgr", rootfs / "etc/init.d/S94appmgr", 0o755)
    _copy_file(repo / "market/deploy/ext_appmgr.conf", oem / "etc/nginx/ext_appmgr.conf", 0o644)
    _assert_clean(platform)
    required = (
        platform / "appmgr/__main__.py",
        platform / "appmgr/result_hub.py",
        platform / "appmgr/visualization.py",
        platform / "appmgr/inference_auth.py",
        platform / "appmgr/trust.py",
        platform / "appmgr/schema/manifest-v2.schema.json",
        vendor_key,
        platform / "inferenced/__main__.py",
        platform / "inferenced/authorization.py",
        platform / "inferenced/server.py",
        rootfs / "etc/init.d/S93inferenced",
        rootfs / "etc/init.d/S94appmgr",
        oem / "etc/nginx/ext_appmgr.conf",
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError("platform staging is incomplete: " + ", ".join(missing))
    for script in required[-3:-1]:
        if not stat.S_IMODE(script.stat().st_mode) & stat.S_IXUSR:
            raise PermissionError(f"init script is not executable: {script}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--rootfs", required=True, type=Path)
    parser.add_argument("--oem", required=True, type=Path)
    args = parser.parse_args(argv)
    stage(args.repo_root.resolve(), args.rootfs.resolve(), args.oem.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

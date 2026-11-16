"""Durable payload publication and verification for interrupted installs."""
from __future__ import annotations

import errno
import hashlib
import os
import stat


def fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            if exc.errno not in (errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP):
                raise
    finally:
        os.close(fd)


def _file_record(path: str, *, sync: bool = False) -> list:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("payload is not a regular file: %s" % path)
        digest = hashlib.sha256()
        with os.fdopen(fd, "rb", closefd=False) as source:
            for chunk in iter(lambda: source.read(1 << 20), b""):
                digest.update(chunk)
        if sync:
            os.fsync(fd)
        return [info.st_size, digest.hexdigest(), stat.S_IMODE(info.st_mode)]
    finally:
        os.close(fd)


def seal_tree(root: str, *, exclude=(), ignore_bytecode: bool = False) -> dict:
    """Hash and flush payloads, then directories bottom up, before rename.

    Symlinks are recorded without following them (venvs link platform Python).
    Mutable interpreter caches are optionally omitted from the inventory, but
    still flushed. New ancestor directory entries must also reach the disk.
    """
    if not stat.S_ISDIR(os.lstat(root).st_mode):
        raise ValueError("payload root is not a directory")
    inventory = {"files": {}, "links": {}, "directories": []}
    for directory, dirs, files in os.walk(root, topdown=False, followlinks=False):
        for name in dirs + files:
            path = os.path.join(directory, name)
            relative = os.path.relpath(path, root)
            info = os.lstat(path)
            omitted = (relative in exclude or (ignore_bytecode and
                       ("__pycache__" in relative.split(os.sep) or
                        relative.endswith((".pyc", ".pyo")))))
            if stat.S_ISLNK(info.st_mode):
                if not omitted:
                    inventory["links"][relative] = os.readlink(path)
            elif stat.S_ISREG(info.st_mode):
                record = _file_record(path, sync=True)
                if not omitted:
                    inventory["files"][relative] = record
            elif stat.S_ISDIR(info.st_mode):
                if not omitted:
                    inventory["directories"].append(relative)
            else:
                raise ValueError("unsupported payload file: %s" % relative)
        fsync_dir(directory)
    parent = os.path.abspath(root)
    while parent != os.path.dirname(parent):
        parent = os.path.dirname(parent)
        fsync_dir(parent)
    return inventory


def _safe_path(root: str, relative: str) -> str:
    if (not isinstance(relative, str) or not relative or
            any(part in ("", ".", "..") for part in relative.split("/")) or
            "\\" in relative):
        raise ValueError("invalid payload inventory path")
    parent = root
    for part in relative.split("/")[:-1]:
        parent = os.path.join(parent, part)
        if not stat.S_ISDIR(os.lstat(parent).st_mode):
            raise ValueError("payload parent is not a directory: %s" % relative)
    return os.path.join(root, relative)


def verify_tree(root: str, inventory: dict) -> None:
    """Verify every sealed file/link without trusting directory identity alone."""
    if (not isinstance(inventory, dict) or
            not isinstance(inventory.get("files"), dict) or not inventory["files"] or
            not isinstance(inventory.get("links"), dict) or
            not isinstance(inventory.get("directories"), list)):
        raise ValueError("missing or invalid payload integrity inventory")
    if not stat.S_ISDIR(os.lstat(root).st_mode):
        raise ValueError("payload root is not a directory")
    for relative in inventory["directories"]:
        if not stat.S_ISDIR(os.lstat(_safe_path(root, relative)).st_mode):
            raise ValueError("payload directory changed: %s" % relative)
    for relative, expected in inventory["files"].items():
        if _file_record(_safe_path(root, relative)) != expected:
            raise ValueError("payload integrity mismatch: %s" % relative)
    for relative, target in inventory["links"].items():
        if os.readlink(_safe_path(root, relative)) != target:
            raise ValueError("payload symlink changed: %s" % relative)

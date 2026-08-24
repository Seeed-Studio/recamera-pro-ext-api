#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import os
import posixpath
import re
import shutil
import stat
from pathlib import Path, PurePosixPath
from zipfile import ZipFile


_RUNTIME_EXCLUDED_DIRS = {"tests", "__pycache__", ".pytest_cache"}
_RKNN_RUNTIME_LINK = PurePosixPath("usr/lib/librknnrt.so")
_RKNN_RUNTIME_TARGET = "../../oem/usr/lib/librknnrt.so"
_RKNN_RUNTIME_DESTINATION = PurePosixPath("oem/usr/lib/librknnrt.so")
_EM_AARCH64 = 183


def _ignore(_root: str, names: list[str]) -> list[str]:
    skipped: list[str] = []
    for name in names:
        if name in {
            "__pycache__",
            ".pytest_cache",
            "build",
            "tests",
            "pyproject.toml",
            "setup.py",
        }:
            skipped.append(name)
            continue
        if name.endswith((".pyc", ".pyo")) or name.endswith(".egg-info"):
            skipped.append(name)
            continue
        if fnmatch.fnmatch(name, "test_*.py"):
            skipped.append(name)
    return skipped


def _replace_tree(src: Path, dst: Path) -> None:
    if dst.is_symlink() or dst.is_file():
        dst.unlink()
    elif dst.is_dir():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=_ignore, symlinks=True)


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _aarch64_elf_entity(path: Path, description: str) -> Path:
    """Resolve one non-dangling regular ELF64/AArch64 runtime entity."""

    try:
        entity = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"{description} is missing or dangling: {path}"
        ) from exc
    if not entity.is_file():
        raise ValueError(f"{description} is not a regular file: {entity}")
    header = entity.read_bytes()[:20]
    if len(header) < 20 or header[:4] != b"\x7fELF":
        raise ValueError(f"{description} is not an ELF file: {entity}")
    if header[4] != 2 or header[5] != 1:
        raise ValueError(
            f"{description} must be little-endian ELF64: {entity}"
        )
    machine = int.from_bytes(header[18:20], "little")
    if machine != _EM_AARCH64:
        raise ValueError(
            f"{description} must be AArch64 (machine={machine}): {entity}"
        )
    return entity


def _rootfs_rknn_target(rootfs: Path, raw_target: str) -> Path:
    """Map the staged relative link to its lexical path inside ``rootfs``."""

    logical = PurePosixPath(
        posixpath.normpath(
            str(PurePosixPath("/") / _RKNN_RUNTIME_LINK.parent / raw_target)
        )
    )
    expected = PurePosixPath("/") / _RKNN_RUNTIME_DESTINATION
    if logical != expected:
        raise ValueError(
            "librknnrt rootfs link does not map to the OEM runtime: "
            f"{raw_target!r} -> {str(logical)!r}, expected {str(expected)!r}"
        )
    return rootfs.resolve().joinpath(*logical.parts[1:])


def _verify_rknn_runtime_link(
    rootfs: Path,
    runtime_source: Path,
    *,
    require_resolved_target: bool = False,
) -> Path:
    """Verify the rootfs link and the AArch64 entity installed at its target.

    The app build produces an overlay before the firmware packager merges the
    media payload into ``/oem``.  ``runtime_source`` is that explicit media
    provider.  When an assembled rootfs is available,
    ``require_resolved_target`` additionally rejects a link that still dangles
    and proves that the installed OEM bytes match the provider.
    """

    rootfs = rootfs.resolve()
    link = rootfs.joinpath(*_RKNN_RUNTIME_LINK.parts)
    if not link.is_symlink():
        raise ValueError(f"rootfs RKNN runtime is not a symlink: {link}")
    raw_target = os.readlink(link)
    if raw_target != _RKNN_RUNTIME_TARGET:
        raise ValueError(
            f"unexpected librknnrt link target {raw_target!r}; "
            f"expected {_RKNN_RUNTIME_TARGET!r}"
        )

    provider = _aarch64_elf_entity(
        runtime_source, "OEM RKNN runtime provider"
    )
    installed = _rootfs_rknn_target(rootfs, raw_target)
    if installed.is_symlink() and not installed.exists():
        raise FileNotFoundError(
            f"assembled OEM RKNN runtime is dangling: {installed}"
        )
    if installed.exists():
        installed_entity = _aarch64_elf_entity(
            installed, "assembled OEM RKNN runtime"
        )
        if hashlib.sha256(installed_entity.read_bytes()).digest() != \
                hashlib.sha256(provider.read_bytes()).digest():
            raise ValueError(
                "assembled OEM RKNN runtime differs from its media provider: "
                f"{installed_entity} != {provider}"
            )
    elif require_resolved_target:
        raise FileNotFoundError(
            f"rootfs RKNN runtime link is dangling: {link} -> {raw_target}"
        )
    return link


def _stage_rknn_runtime_link(rootfs: Path, runtime_source: Path) -> Path:
    """Atomically stage the relocatable ``/usr/lib/librknnrt.so`` link."""

    # Validate the media provider before changing the output tree.  In the
    # firmware packager this exact file is copied to /oem/usr/lib.
    _aarch64_elf_entity(runtime_source, "OEM RKNN runtime provider")
    rootfs = rootfs.resolve()
    link = rootfs.joinpath(*_RKNN_RUNTIME_LINK.parts)
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_dir() and not link.is_symlink():
        raise ValueError(f"refusing to replace directory with runtime link: {link}")
    temporary = link.with_name(f".{link.name}.tmp.{os.getpid()}")
    _remove_path(temporary)
    try:
        temporary.symlink_to(_RKNN_RUNTIME_TARGET)
        os.replace(temporary, link)
    finally:
        _remove_path(temporary)
    return _verify_rknn_runtime_link(rootfs, runtime_source)


def _locked_wheels(lock_path: Path, wheel_dir: Path) -> list[Path]:
    wheels: list[Path] = []
    for lineno, raw in enumerate(lock_path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) != 2:
            raise ValueError(f"{lock_path}:{lineno}: expected SHA-256 and filename")
        expected, filename = fields
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError(f"{lock_path}:{lineno}: invalid SHA-256")
        if Path(filename).name != filename or not filename.endswith(".whl"):
            raise ValueError(f"{lock_path}:{lineno}: unsafe wheel filename")
        wheel = wheel_dir / filename
        if not wheel.is_file():
            raise FileNotFoundError(f"locked runtime wheel is missing: {wheel}")
        actual = hashlib.sha256(wheel.read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(
                f"runtime wheel hash mismatch: {wheel} got {actual} want {expected}"
            )
        wheels.append(wheel)
    if not wheels:
        raise ValueError(f"runtime wheel lock is empty: {lock_path}")
    return wheels


def _runtime_wheel_member_filtered(path: PurePosixPath) -> bool:
    leaf = path.name
    return (
        any(part in _RUNTIME_EXCLUDED_DIRS for part in path.parts)
        or any(part.endswith(".egg-info") for part in path.parts)
        or leaf.endswith((".pyc", ".pyo"))
        or fnmatch.fnmatch(leaf, "test_*.py")
    )


def _runtime_wheel_filter_anchor(path: PurePosixPath) -> PurePosixPath:
    """Return the file/cache/test subtree that must be absent after staging."""
    for index, part in enumerate(path.parts):
        if part in _RUNTIME_EXCLUDED_DIRS or part.endswith(".egg-info"):
            return PurePosixPath(*path.parts[: index + 1])
    return path


def _validated_wheel_roots(wheel: Path) -> set[str]:
    roots: set[str] = set()
    with ZipFile(wheel) as archive:
        for info in archive.infolist():
            name = info.filename
            path = PurePosixPath(name)
            if (not name or name.startswith("/") or "\\" in name
                    or any(part in {"", ".", ".."} for part in path.parts)):
                raise ValueError(f"unsafe member in {wheel}: {name!r}")
            mode = (info.external_attr >> 16) & 0o170000
            if stat.S_ISLNK(mode):
                raise ValueError(f"wheel symlink is not allowed: {wheel}:{name}")
            root = path.parts[0]
            if root.endswith(".data"):
                raise ValueError(
                    f"wheel .data layout is unsupported by offline staging: {wheel}"
                )
            if not _runtime_wheel_member_filtered(path):
                roots.add(root)
    return roots


def _install_locked_wheels(lock_path: Path, wheel_dir: Path, site: Path) -> None:
    prepared: list[tuple[Path, set[str]]] = []
    owners: dict[str, Path] = {}
    for wheel in _locked_wheels(lock_path, wheel_dir):
        roots = _validated_wheel_roots(wheel)
        for root in roots:
            previous = owners.get(root)
            if previous is not None:
                raise ValueError(
                    f"runtime wheels share top-level path {root!r}: "
                    f"{previous.name}, {wheel.name}"
                )
            owners[root] = wheel
        prepared.append((wheel, roots))

    # Validation and hash checks complete before the first staging mutation.
    for wheel, roots in prepared:
        for root in sorted(roots):
            _remove_path(site / root)
        with ZipFile(wheel) as archive:
            members = []
            filtered: set[PurePosixPath] = set()
            for info in archive.infolist():
                path = PurePosixPath(info.filename)
                if _runtime_wheel_member_filtered(path):
                    filtered.add(_runtime_wheel_filter_anchor(path))
                else:
                    members.append(info)
            # Remove stale test/cache content from an earlier staging run even
            # when the current wheel member is deliberately not extracted.
            for path in sorted(filtered, key=lambda item: (len(item.parts), item.parts)):
                _remove_path(site.joinpath(*path.parts))
            archive.extractall(site, members=members)


def main() -> int:
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--stage-rknn-runtime", action="store_true")
    mode.add_argument("--verify-rknn-runtime", action="store_true")
    ap.add_argument("--repo-root")
    ap.add_argument("--site-packages")
    ap.add_argument("--wheel-lock")
    ap.add_argument("--wheel-dir")
    ap.add_argument("--rootfs")
    ap.add_argument("--rknn-runtime-source")
    ap.add_argument("--require-resolved-rknn-target", action="store_true")
    args = ap.parse_args()

    if args.stage_rknn_runtime or args.verify_rknn_runtime:
        if not args.rootfs or not args.rknn_runtime_source:
            ap.error(
                "RKNN runtime mode requires --rootfs and "
                "--rknn-runtime-source"
            )
        if args.stage_rknn_runtime and args.require_resolved_rknn_target:
            ap.error(
                "--require-resolved-rknn-target is valid only with "
                "--verify-rknn-runtime"
            )
        rootfs = Path(args.rootfs)
        source = Path(args.rknn_runtime_source)
        if args.stage_rknn_runtime:
            _stage_rknn_runtime_link(rootfs, source)
        else:
            _verify_rknn_runtime_link(
                rootfs,
                source,
                require_resolved_target=args.require_resolved_rknn_target,
            )
        return 0

    if args.require_resolved_rknn_target:
        ap.error(
            "--require-resolved-rknn-target requires --verify-rknn-runtime"
        )
    if not args.repo_root or not args.site_packages:
        ap.error("--repo-root and --site-packages are required")

    repo = Path(args.repo_root).resolve()
    site = Path(args.site_packages).resolve()
    site.mkdir(parents=True, exist_ok=True)

    for src_rel, dst_name in (
        ("sdk/python/recamera_ext", "recamera_ext"),
        ("kit", "kit"),
    ):
        _replace_tree(repo / src_rel, site / dst_name)
    if bool(args.wheel_lock) != bool(args.wheel_dir):
        ap.error("--wheel-lock and --wheel-dir must be provided together")
    if args.wheel_lock:
        _install_locked_wheels(
            Path(args.wheel_lock).resolve(),
            Path(args.wheel_dir).resolve(),
            site,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

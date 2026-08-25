#!/usr/bin/env python3
"""Check the standalone source closure and the frozen ELF/C ABI surface."""

import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


EXPECTED_SONAME = "librecamera_ext.so.1"
REQUIRED_FILES = (
    "CMakeLists.txt",
    "LICENSE",
    "NOTICE",
    "SOURCE_PROVENANCE.md",
    "VERSION",
    "abi/librecamera_ext.so.1.symbols",
    "include/recamera_ext.h",
    "src/recamera_ext.c",
    "src/recamera_ext_mask.c",
    "src/frame_recv.c",
    "src/probe_recv.c",
    "src/inference_lease.c",
    "src/ext_client_common.c",
    "src/ext_client_common.h",
    "src/rc_ext_errno.h",
    "proto/inference.proto",
    "proto/ext_api.proto",
    "generated/inference.pb-c.c",
    "generated/inference.pb-c.h",
    "generated/ext_api.pb-c.c",
    "generated/ext_api.pb-c.h",
    "tests/native_abi_layout.c",
)
FORBIDDEN_CMAKE_DEPENDENCIES = (
    "${RKIPC_COMMON}",
    "../../common",
    "/recamera_ipc/common/",
)


def _symbols(path):
    result = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            result.add(line)
    return result


def _run(command):
    return subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    ).stdout


def _check_source(source_root):
    errors = []
    for relative in REQUIRED_FILES:
        if not (source_root / relative).is_file():
            errors.append("missing source-closure file: " + relative)

    cmake_path = source_root / "CMakeLists.txt"
    if cmake_path.is_file():
        cmake = cmake_path.read_text(encoding="utf-8")
        for token in FORBIDDEN_CMAKE_DEPENDENCIES:
            if token in cmake:
                errors.append("CMake retains external firmware dependency: " + token)
        for relative in REQUIRED_FILES:
            if relative.endswith(".c") and relative != "tests/native_abi_layout.c":
                if relative not in cmake:
                    errors.append("CMake does not compile required source: " + relative)

    abi_path = source_root / "abi" / "librecamera_ext.so.1.symbols"
    header_path = source_root / "include" / "recamera_ext.h"
    if abi_path.is_file() and header_path.is_file():
        expected = _symbols(abi_path)
        header = header_path.read_text(encoding="utf-8")
        declared = set(
            re.findall(
                r"\b(rc_ext_(?:result|osd|frame|probe|inference|mask)_[A-Za-z0-9_]+)\s*\(",
                header,
            )
        )
        if declared != expected:
            errors.append(
                "header/ABI baseline mismatch: missing=%r unexpected=%r"
                % (sorted(expected - declared), sorted(declared - expected))
            )

    version_path = source_root / "VERSION"
    if version_path.is_file():
        version = version_path.read_text(encoding="utf-8")
        if not re.search(r"^version:\s+\d+\.\d+\.\d+", version, re.MULTILINE):
            errors.append("VERSION does not contain a semantic native SDK version")
        for capability in (
            "frame@1",
            "result@1",
            "osd@1",
            "probe@1",
            "inference-control@1",
        ):
            if capability not in version:
                errors.append("VERSION omits native capability: " + capability)

    for path in source_root.rglob("*"):
        if not path.is_symlink():
            continue
        try:
            path.resolve().relative_to(source_root)
        except (OSError, ValueError):
            errors.append("symlink escapes standalone SDK tree: " + str(path))

    return errors


def _check_elf(source_root, library, readelf):
    errors = []
    dynamic = _run([readelf, "--wide", "--dynamic", str(library)])
    sonames = re.findall(r"\(SONAME\).*?\[([^]]+)\]", dynamic)
    if sonames != [EXPECTED_SONAME]:
        errors.append("expected SONAME %s, got %r" % (EXPECTED_SONAME, sonames))

    needed = set(re.findall(r"\(NEEDED\).*?\[([^]]+)\]", dynamic))
    if "libprotobuf-c.so.1" not in needed:
        errors.append("ELF does not require libprotobuf-c.so.1: %r" % sorted(needed))

    dynsyms = _run([readelf, "--wide", "--dyn-syms", str(library)])
    defined = set()
    for line in dynsyms.splitlines():
        fields = line.split()
        if len(fields) < 8 or fields[3] != "FUNC" or fields[6] == "UND":
            continue
        name = fields[7].split("@", 1)[0]
        if name.startswith("rc_ext_"):
            defined.add(name)

    expected = _symbols(source_root / "abi" / "librecamera_ext.so.1.symbols")
    missing = expected - defined
    if missing:
        errors.append("ELF is missing ABI symbols: %r" % sorted(missing))

    for alias_name in ("librecamera_ext.so", EXPECTED_SONAME):
        alias = library.parent / alias_name
        if not alias.exists() or alias.resolve() != library.resolve():
            errors.append("missing or incorrect build-tree alias: " + str(alias))

    header = _run([readelf, "--wide", "--file-header", str(library)])
    elf_class = re.search(r"^\s*Class:\s*(.+)$", header, re.MULTILINE)
    machine = re.search(r"^\s*Machine:\s*(.+)$", header, re.MULTILINE)
    print(
        "ELF ABI OK: soname=%s class=%s machine=%s required_symbols=%d extra_symbols=%d"
        % (
            EXPECTED_SONAME,
            elf_class.group(1) if elf_class else "unknown",
            machine.group(1) if machine else "unknown",
            len(expected),
            len(defined - expected),
        )
    )
    return errors


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--library", type=Path)
    parser.add_argument("--readelf")
    args = parser.parse_args()

    source_root = args.source_root.resolve()
    errors = _check_source(source_root)

    if args.library is not None:
        library = args.library.resolve()
        if not library.is_file():
            errors.append("native library was not built: " + str(library))
        else:
            requested = args.readelf or os.environ.get("READELF") or "readelf"
            readelf = shutil.which(requested)
            if readelf is None:
                errors.append("readelf was not found: " + requested)
            else:
                try:
                    errors.extend(_check_elf(source_root, library, readelf))
                except subprocess.CalledProcessError as exc:
                    errors.append("readelf failed: " + str(exc))

    if errors:
        for error in errors:
            print("native SDK check FAILED: " + error, file=sys.stderr)
        return 1

    print("source closure OK: " + str(source_root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

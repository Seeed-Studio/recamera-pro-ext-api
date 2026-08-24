#!/usr/bin/env python3
"""Regenerate protobuf-c bindings with the release toolchain and reject drift."""

import argparse
import difflib
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


PINNED_VERSION_LINES = ("protobuf-c 1.4.1", "libprotoc 3.21.12")
PROTO_NAMES = ("inference.proto", "ext_api.proto")
GENERATED_NAMES = (
    "inference.pb-c.c",
    "inference.pb-c.h",
    "ext_api.pb-c.c",
    "ext_api.pb-c.h",
)


def _resolve_tool(requested):
    candidate = requested or os.environ.get("PROTOC_C") or "protoc-c"
    resolved = shutil.which(candidate)
    if resolved is None:
        raise RuntimeError(
            "protoc-c was not found; install protobuf-c 1.4.1 or set PROTOC_C"
        )
    return resolved


def _run(command):
    return subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    ).stdout


def _first_diff(expected, actual, name):
    expected_lines = expected.decode("utf-8", "replace").splitlines(True)
    actual_lines = actual.decode("utf-8", "replace").splitlines(True)
    lines = difflib.unified_diff(
        expected_lines,
        actual_lines,
        fromfile="committed/" + name,
        tofile="regenerated/" + name,
    )
    return "".join(list(lines)[:200])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--sdk-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--protoc-c")
    args = parser.parse_args()

    sdk_root = args.sdk_root.resolve()
    proto_dir = sdk_root / "proto"
    committed_dir = sdk_root / "generated"
    tool = _resolve_tool(args.protoc_c)

    version_lines = tuple(
        line.strip()
        for line in _run([tool, "--version"]).splitlines()
        if line.strip()
    )
    if version_lines != PINNED_VERSION_LINES:
        raise RuntimeError(
            "wrong protobuf generator version: expected %r, got %r from %s"
            % (PINNED_VERSION_LINES, version_lines, tool)
        )

    proto_paths = [proto_dir / name for name in PROTO_NAMES]
    missing_proto = [str(path) for path in proto_paths if not path.is_file()]
    if missing_proto:
        raise RuntimeError("missing canonical proto files: " + ", ".join(missing_proto))

    committed_names = sorted(path.name for path in committed_dir.glob("*.pb-c.*"))
    if committed_names != sorted(GENERATED_NAMES):
        raise RuntimeError(
            "unexpected committed generated-file set: expected %r, got %r"
            % (sorted(GENERATED_NAMES), committed_names)
        )

    with tempfile.TemporaryDirectory(prefix="recamera-protoc-c.") as temp_dir:
        command = [
            tool,
            "--c_out=" + temp_dir,
            "--proto_path=" + str(proto_dir),
        ] + [str(path) for path in proto_paths]
        _run(command)

        drift = []
        hashes = []
        for name in GENERATED_NAMES:
            committed = (committed_dir / name).read_bytes()
            regenerated = (Path(temp_dir) / name).read_bytes()
            if committed != regenerated:
                drift.append(_first_diff(committed, regenerated, name))
            hashes.append("%s=%s" % (name, hashlib.sha256(committed).hexdigest()))

    if drift:
        sys.stderr.write("protobuf-c generated output drifted:\n")
        sys.stderr.write("\n".join(drift))
        return 1

    print("generated drift OK: " + ", ".join(PINNED_VERSION_LINES))
    print("  " + "\n  ".join(hashes))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print("generated drift FAILED: %s" % exc, file=sys.stderr)
        raise SystemExit(1)

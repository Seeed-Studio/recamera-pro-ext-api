"""Pinned, offline SDK contract selection shared by validation and packaging."""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess

LOCK = json.loads(Path(__file__).with_name("sdk-contract-lock.json").read_text())
SKILL_SDK_SOURCE_REPO = LOCK["repository"]
SKILL_SDK_SOURCE_COMMIT = LOCK["commit"]
SKILL_SDK_BUILDER_SHA256 = {Path(k): v for k, v in LOCK["bundled_files"].items()}
SDK_BUILDER_REQUIRED_FILES = frozenset(SKILL_SDK_BUILDER_SHA256)


class ContractError(ValueError):
    pass


def bundled_sdk_root() -> Path:
    return Path(__file__).resolve().parent / "sdk-builder"


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_sdk_builder(root: Path, source_label: str = "selected") -> Path:
    root = root.expanduser().resolve()
    for path, expected in SKILL_SDK_BUILDER_SHA256.items():
        if not (root / path).is_file():
            raise ContractError(f"{source_label} SDK builder missing {path}")
        if root == bundled_sdk_root() and file_hash(root / path) != expected:
            raise ContractError(f"bundled SDK builder hash mismatch: {path}")
    return root


def find_sdk_root(explicit: Path | None = None) -> Path:
    if explicit is not None:
        return validate_sdk_builder(explicit, "--sdk-root")
    for name in ("RECAMERA_SDK_ROOT", "RECAMERA_EXT_API_ROOT"):
        if os.environ.get(name):
            return validate_sdk_builder(Path(os.environ[name]), name)
    return validate_sdk_builder(bundled_sdk_root(), "bundled")


def load_sdk_contract(root: Path):
    spec = importlib.util.spec_from_file_location(
        "recamera_skill_manifest_contract", root / "market/appmgr/manifest.py")
    if spec is None or spec.loader is None:
        raise ContractError("cannot load selected SDK manifest contract")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                                text=True, timeout=10, check=False)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def provenance(root: Path) -> dict:
    bundled = root == bundled_sdk_root()
    # Do not attribute an exported directory to an unrelated ancestor checkout.
    top = _git(root, "rev-parse", "--show-toplevel") if not bundled else None
    is_checkout = top is not None and Path(top).resolve() == root
    status = _git(root, "status", "--porcelain") if is_checkout else None
    return {
        "mode": "bundled" if bundled else "local",
        "source": "skill-bundled-pinned" if bundled else "caller-supplied-override",
        "upstream_repository": SKILL_SDK_SOURCE_REPO,
        "sdk_source_commit": SKILL_SDK_SOURCE_COMMIT if bundled else (
            _git(root, "rev-parse", "HEAD") if is_checkout else None),
        "dirty": False if bundled else (bool(status) if status is not None else None),
        "files_sha256": {str(p): file_hash(root / p) for p in sorted(SDK_BUILDER_REQUIRED_FILES)},
        "kit_api": {
            "source": "selected-checkout" if (root / "kit/app.py").is_file() else "bundled-signature-snapshot",
            "sha256": file_hash(root / "kit/app.py") if (root / "kit/app.py").is_file() else LOCK["watched_files"]["kit/app.py"],
            "snapshot_commit": None if (root / "kit/app.py").is_file() else SKILL_SDK_SOURCE_COMMIT,
        },
    }


def api_signatures(source: str) -> dict:
    """Extract argument contracts without importing hardware-dependent Kit."""
    app = next(n for n in ast.parse(source).body if isinstance(n, ast.ClassDef) and n.name == "App")
    result = {}
    for node in app.body:
        if not isinstance(node, ast.FunctionDef) or node.name not in {
            "emit", "request_recording", "pre", "crop_roi_hw", "frames"
        }:
            continue
        args = node.args
        positional = args.posonlyargs + args.args
        result[node.name] = {
            "positional": [p.arg for p in positional[1:]],
            "posonly": max(0, len(args.posonlyargs) - 1),
            "required": max(0, len(positional) - 1 - len(args.defaults)),
            "keyword_only": [p.arg for p in args.kwonlyargs],
            "required_keywords": [p.arg for p, d in zip(args.kwonlyargs, args.kw_defaults) if d is None],
            "varargs": bool(args.vararg), "kwargs": bool(args.kwarg),
        }
    return result


def selected_api_signatures(root: Path) -> dict:
    path = root / "kit/app.py"
    return api_signatures(path.read_text()) if path.is_file() else LOCK["kit_api"]


def check_drift(root: Path) -> dict:
    changes = []
    for relative, expected in LOCK["watched_files"].items():
        path = root / relative
        actual = file_hash(path) if path.is_file() else None
        if actual != expected:
            changes.append({"file": relative, "expected": expected, "actual": actual})
    return {"pinned_commit": SKILL_SDK_SOURCE_COMMIT, "matches": not changes,
            "changes": changes, "action": "Review changes before explicitly refreshing the pin; no files were updated."}


def main() -> int:
    parser = argparse.ArgumentParser(description="Check SDK contract drift without updating the pinned builder")
    parser.add_argument("--sdk-root", required=True, type=Path)
    args = parser.parse_args()
    report = check_drift(args.sdk_root)
    print(json.dumps(report, indent=2))
    return 0 if report["matches"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
fetch_assets.py -- download missing models, artifacts, and wheels for an app.

Usage:
    python3 market/packaging/fetch_assets.py apps/yolo-detector
    python3 market/packaging/fetch_assets.py apps/eldercare-monitor
    python3 market/packaging/fetch_assets.py --all

Resolution order per missing file:
1. `x-url` declared on the model/artifact entry in manifest.json (if present)
2. OSS model pool via ossutil:
   oss://sensecraft-statics/solution-app/recamera_pro/models/{app_id}/{rel_path}
3. Fallback OSS path (flat filename or shared model pool):
   oss://sensecraft-statics/solution-app/recamera_pro/models/{app_id}/{basename}
4. CDN HTTP fallback (when ossutil is not installed/configured):
   https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/models/{app_id}/{rel_path}
   https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/models/{app_id}/{basename}

Integrity:
- Compares SHA-256 against manifest `sha256` if declared.
- Idempotent: skips files that already exist locally with matching SHA-256.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_MARKET = os.path.dirname(_HERE)
if _MARKET not in sys.path:
    sys.path.insert(0, _MARKET)
from appmgr import manifest as manifest_contract

OSS_DEFAULT_BASE = "oss://sensecraft-statics/solution-app/recamera_pro/models"
CDN_DEFAULT_BASE = "https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/models"


def compute_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def _safe_dest_path(app_dir: str, rel_path: str) -> str:
    """Validate relative path does not escape the application directory."""
    if os.path.isabs(rel_path) or ".." in rel_path.split("/"):
        raise ValueError(f"Path traversal detected in path {rel_path!r}")
    dest = os.path.abspath(os.path.join(app_dir, rel_path))
    app_dir_abs = os.path.abspath(app_dir)
    if not (dest == app_dir_abs or dest.startswith(app_dir_abs + os.sep)):
        raise ValueError(f"Destination {dest} escapes application directory {app_dir_abs}")
    current = app_dir_abs
    for component in rel_path.split("/"):
        current = os.path.join(current, component)
        if os.path.islink(current):
            resolved = os.path.realpath(current)
            if not resolved.startswith(app_dir_abs + os.sep):
                raise ValueError(f"Symlink {current} -> {resolved} escapes application directory")
    return dest


def _has_ossutil() -> bool:
    return shutil.which("ossutil") is not None


def _download_url(url: str, dest_path: str) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "recamera-fetch-assets/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp, open(dest_path, "wb") as out:
            shutil.copyfileobj(resp, out)
        return True
    except Exception as e:
        if os.path.exists(dest_path):
            os.remove(dest_path)
        return False


def _download_oss(oss_uri: str, dest_path: str) -> bool:
    if not _has_ossutil():
        return False
    res = subprocess.run(["ossutil", "cp", "-f", oss_uri, dest_path], capture_output=True, text=True)
    if res.returncode == 0 and os.path.isfile(dest_path):
        return True
    if os.path.exists(dest_path):
        os.remove(dest_path)
    return False


def fetch_app_assets(app_dir: str, oss_base: str = OSS_DEFAULT_BASE, cdn_base: str = CDN_DEFAULT_BASE) -> list[str]:
    app_dir = os.path.abspath(app_dir)
    manifest_path = os.path.join(app_dir, "manifest.json")
    if not os.path.isfile(manifest_path):
        raise FileNotFoundError(f"No manifest.json in {app_dir}")

    with open(manifest_path, "r", encoding="utf-8") as f:
        mf = json.load(f)

    manifest_contract.validate_manifest(mf)

    app_id = mf.get("id", os.path.basename(app_dir))

    # Resolve artifact mounts so models backed by artifacts don't create duplicate mount downloads
    artifact_mounts = {a["mount"] for a in mf.get("artifacts", []) if isinstance(a, dict) and a.get("mount")}

    # Collect all declared bundled files
    declared = []
    for a in mf.get("artifacts", []):
        if isinstance(a, dict) and a.get("source") == "bundled" and a.get("file"):
            declared.append({
                "rel_path": a["file"],
                "sha256": a.get("sha256"),
                "x_url": a.get("x-url"),
                "kind": "artifact",
            })
    for m in mf.get("models", []):
        if isinstance(m, dict) and m.get("file"):
            model_file = m["file"]
            if model_file not in artifact_mounts:
                declared.append({
                    "rel_path": model_file,
                    "sha256": m.get("sha256"),
                    "x_url": m.get("x-url"),
                    "kind": "model",
                })
    for w in (mf.get("python") or {}).get("wheels", []):
        if isinstance(w, dict) and w.get("source") == "bundled" and w.get("file"):
            declared.append({
                "rel_path": w["file"],
                "sha256": w.get("sha256"),
                "x_url": w.get("x-url"),
                "kind": "wheel",
            })

    # Deduplicate by relative path and merge metadata (e.g. hash from artifacts + model spec)
    unique = {}
    for item in declared:
        rp = item["rel_path"]
        if rp not in unique:
            unique[rp] = dict(item)
        else:
            if item.get("sha256") and not unique[rp].get("sha256"):
                unique[rp]["sha256"] = item["sha256"]
            if item.get("x_url") and not unique[rp].get("x_url"):
                unique[rp]["x_url"] = item["x_url"]

    downloaded_files = []
    print(f"[{app_id}] Checking {len(unique)} declared bundled assets...")

    for rel_path, item in sorted(unique.items()):
        dest = _safe_dest_path(app_dir, rel_path)
        expected_sha = item.get("sha256")
        x_url = item.get("x_url")
        basename = os.path.basename(rel_path)

        # Check if already cached with valid sha256
        if os.path.isfile(dest):
            if expected_sha:
                if compute_sha256(dest) == expected_sha:
                    print(f"  ✓ Cached: {rel_path}")
                    continue
                else:
                    print(f"  ! SHA256 mismatch for existing {rel_path}, re-downloading...")
            else:
                print(f"  ✓ Exists: {rel_path}")
                continue

        os.makedirs(os.path.dirname(dest), exist_ok=True)
        success = False

        # 1. Custom x-url
        if x_url:
            print(f"  -> Downloading from x-url: {x_url}")
            success = _download_url(x_url, dest)

        # 2. OSS primary path: oss://.../<app_id>/<rel_path>
        if not success and _has_ossutil():
            oss_uri = f"{oss_base}/{app_id}/{rel_path}"
            success = _download_oss(oss_uri, dest)

        # 3. OSS fallback (flat basename): oss://.../<app_id>/<basename>
        if not success and _has_ossutil():
            oss_uri = f"{oss_base}/{app_id}/{basename}"
            success = _download_oss(oss_uri, dest)

        # 4. Eldercare shared model pool fallback
        if not success and _has_ossutil() and app_id == "eldercare-monitor":
            donor = "fall-detection" if "pose" in basename else "voice-transcribe"
            oss_uri = f"{oss_base}/{donor}/{rel_path}"
            success = _download_oss(oss_uri, dest)

        # 5. Public CDN fallback (HTTP GET)
        if not success:
            cdn_url = f"{cdn_base}/{app_id}/{rel_path}"
            print(f"  -> Downloading from CDN: {cdn_url}")
            success = _download_url(cdn_url, dest)

        if not success:
            cdn_url = f"{cdn_base}/{app_id}/{basename}"
            success = _download_url(cdn_url, dest)

        if not success:
            raise RuntimeError(
                f"Failed to download asset {rel_path} for app {app_id}. "
                f"Ensure the asset is uploaded to {oss_base}/{app_id}/{rel_path} "
                f"or specify 'x-url' in manifest.json."
            )

        # Verify sha256
        if expected_sha:
            actual_sha = compute_sha256(dest)
            if actual_sha != expected_sha:
                os.remove(dest)
                raise ValueError(
                    f"SHA256 verification failed for {rel_path} in {app_id}! "
                    f"Expected: {expected_sha}, Got: {actual_sha}"
                )
            print(f"  ✓ Downloaded & verified: {rel_path} (sha256: {expected_sha[:8]}...)")
        else:
            print(f"  ✓ Downloaded: {rel_path} ({os.path.getsize(dest)} bytes)")

        downloaded_files.append(dest)

    return downloaded_files


def main():
    parser = argparse.ArgumentParser(description="Download missing models and artifacts for reCamera apps.")
    parser.add_argument("apps", nargs="*", help="App directory or ID (e.g. apps/yolo-detector)")
    parser.add_argument("--all", action="store_true", help="Fetch assets for all apps in apps/")
    parser.add_argument("--oss-base", default=OSS_DEFAULT_BASE, help="Base OSS URI for models")
    parser.add_argument("--cdn-base", default=CDN_DEFAULT_BASE, help="Base CDN URL for models")

    args = parser.parse_args()

    app_dirs = []
    if args.all:
        app_dirs = sorted(glob.glob("apps/*"))
    elif args.apps:
        for a in args.apps:
            if os.path.isdir(a):
                app_dirs.append(a)
            elif os.path.isdir(os.path.join("apps", a)):
                app_dirs.append(os.path.join("apps", a))
            else:
                sys.exit(f"error: application directory not found: {a}")
    else:
        parser.print_help()
        sys.exit(1)

    has_error = False
    for app_dir in app_dirs:
        if not os.path.isfile(os.path.join(app_dir, "manifest.json")):
            continue
        try:
            fetch_app_assets(app_dir, oss_base=args.oss_base, cdn_base=args.cdn_base)
        except Exception as e:
            print(f"::error::Error fetching assets for {app_dir}: {e}", file=sys.stderr)
            has_error = True

    if has_error:
        sys.exit(1)


if __name__ == "__main__":
    main()

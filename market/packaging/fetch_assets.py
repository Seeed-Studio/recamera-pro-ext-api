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

An optional manifest `x-asset-bundle: {url, sha256, size}` selects one pinned
HTTPS tar.gz instead of per-file lookups. Only declared bundled assets are
copied, after verifying both the archive and every selected file. This keeps
the standard fetch/build/publish sequence usable for multi-wheel applications.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_MARKET = os.path.dirname(_HERE)
if _MARKET not in sys.path:
    sys.path.insert(0, _MARKET)
from appmgr import manifest as manifest_contract
from appmgr import paths

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
    if (not rel_path or os.path.isabs(rel_path) or "\\" in rel_path
            or any(part in ("", ".", "..") for part in rel_path.split("/"))):
        raise ValueError(f"Path traversal detected in path {rel_path!r}")
    dest = os.path.abspath(os.path.join(app_dir, rel_path))
    app_dir_abs = os.path.abspath(app_dir)
    if os.path.islink(app_dir_abs):
        raise ValueError(f"Application directory must not be a symlink: {app_dir_abs}")
    if not (dest == app_dir_abs or dest.startswith(app_dir_abs + os.sep)):
        raise ValueError(f"Destination {dest} escapes application directory {app_dir_abs}")
    current = app_dir_abs
    for component in rel_path.split("/"):
        current = os.path.join(current, component)
        if os.path.islink(current):
            raise ValueError(f"Asset path must not use symlinks: {current}")
    return dest


def _matches_asset(path: str, item: dict) -> bool:
    return (os.path.isfile(path)
            and (item.get("size") is None or os.path.getsize(path) == item["size"])
            and (not item.get("sha256") or compute_sha256(path) == item["sha256"]))


def _fetch_asset_bundle(app_dir: str, bundle: dict, declared: dict) -> list[str]:
    """Fill declared assets from one pinned archive, never overlay application code.

    A prior published package can act as an immutable asset cache. Its manifest,
    entry and release metadata are not copied unless explicitly declared as
    assets by the current manifest. No archive member is extracted by tarfile.
    """
    if not isinstance(bundle, dict) or set(bundle) != {"url", "sha256", "size"}:
        raise ValueError("x-asset-bundle requires url, sha256 and size")
    if not isinstance(bundle["url"], str):
        raise ValueError("x-asset-bundle.url must be an HTTPS asset URL")
    url = urllib.parse.urlsplit(bundle["url"])
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.fragment):
        raise ValueError("x-asset-bundle.url must be an HTTPS asset URL")
    if not isinstance(bundle["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", bundle["sha256"]):
        raise ValueError("x-asset-bundle.sha256 must pin the archive digest")
    if type(bundle["size"]) is not int or not 0 < bundle["size"] <= paths.MAX_PKG_BYTES:
        raise ValueError("x-asset-bundle.size exceeds the package limit")

    missing = {}
    for relative, item in declared.items():
        if not item.get("sha256") or not item.get("size"):
            raise ValueError(f"Bundle asset {relative} requires sha256 and size")
        destination = _safe_dest_path(app_dir, relative)
        if not _matches_asset(destination, item):
            missing[relative] = item
    if not missing:
        print("  All pinned bundle assets are cached and verified")
        return []

    # Keep temporary files on the destination filesystem for atomic replacement.
    with tempfile.TemporaryDirectory(prefix=".asset-bundle-", dir=app_dir) as temporary:
        archive_path = os.path.join(temporary, "bundle.tar.gz")
        print(f"  -> Downloading pinned asset bundle: {bundle['url']}")
        request = urllib.request.Request(bundle["url"], headers={"User-Agent": "recamera-fetch-assets/1.0"})
        digest, received = hashlib.sha256(), 0
        with urllib.request.urlopen(request, timeout=60) as response, open(archive_path, "wb") as output:
            if urllib.parse.urlsplit(response.geturl()).scheme != "https":
                raise ValueError("Asset bundle redirected outside HTTPS")
            for chunk in iter(lambda: response.read(1 << 20), b""):
                received += len(chunk)
                if received > bundle["size"]:
                    raise ValueError("Asset bundle exceeds declared size")
                digest.update(chunk)
                output.write(chunk)
        if received != bundle["size"] or digest.hexdigest() != bundle["sha256"]:
            raise ValueError("Asset bundle digest/size mismatch")

        prepared, seen, expanded = {}, set(), 0
        with tarfile.open(archive_path, "r|gz") as archive:
            for member in archive:
                relative = member.name.rstrip("/")
                manifest_contract.validate_package_member_path(relative)
                if relative in seen or not (member.isfile() or member.isdir()) or member.mode & 0o7000:
                    raise ValueError(f"Invalid or duplicate asset bundle member: {member.name}")
                seen.add(relative)
                expanded += member.size
                if len(seen) > paths.MAX_MEMBERS or expanded > paths.MAX_UNPACKED_BYTES:
                    raise ValueError("Asset bundle exceeds member/unpacked limits")
                if relative not in missing:
                    continue
                item = missing[relative]
                if not member.isfile() or member.size != item["size"]:
                    raise ValueError(f"Asset bundle member size/type mismatch: {relative}")
                candidate = os.path.join(temporary, str(len(prepared)))
                with archive.extractfile(member) as source, open(candidate, "wb") as output:
                    shutil.copyfileobj(source, output, 1 << 20)
                if not _matches_asset(candidate, item):
                    raise ValueError(f"Asset bundle member digest/size mismatch: {relative}")
                prepared[relative] = candidate
        absent = sorted(set(missing) - set(prepared))
        if absent:
            raise ValueError(f"Declared assets missing from bundle: {absent}")
        downloaded = []
        # Verify the whole set before publishing any asset into the app tree.
        for relative, candidate in sorted(prepared.items()):
            destination = _safe_dest_path(app_dir, relative)
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            os.replace(candidate, destination)
            downloaded.append(destination)
        print(f"  Verified and staged {len(downloaded)} declared assets")
        return downloaded


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

    # Identify non-bundled (e.g. catalog) artifact mounts to avoid downloading them as bundled assets
    catalog_mounts = {
        a["mount"] for a in mf.get("artifacts", [])
        if isinstance(a, dict) and a.get("source") != "bundled" and a.get("mount")
    }

    # Collect all declared bundled files
    declared = []
    for a in mf.get("artifacts", []):
        if isinstance(a, dict) and a.get("source") == "bundled" and a.get("file"):
            declared.append({
                "rel_path": a["file"],
                "sha256": a.get("sha256"),
                "size": a.get("size"),
                "x_url": a.get("x-url"),
                "kind": "artifact",
            })
    for m in mf.get("models", []):
        if isinstance(m, dict) and m.get("file"):
            model_file = m["file"]
            if model_file not in catalog_mounts:
                declared.append({
                    "rel_path": model_file,
                    "sha256": m.get("sha256"),
                    "size": m.get("size"),
                    "x_url": m.get("x-url"),
                    "kind": "model",
                })
    for w in (mf.get("python") or {}).get("wheels", []):
        if isinstance(w, dict) and w.get("source") == "bundled" and w.get("file"):
            declared.append({
                "rel_path": w["file"],
                "sha256": w.get("sha256"),
                "size": w.get("size"),
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
            for field in ("sha256", "size"):
                if item.get(field) is not None:
                    if unique[rp].get(field) not in (None, item[field]):
                        raise ValueError(f"Conflicting {field} for asset {rp}")
                    unique[rp][field] = item[field]
            if item.get("sha256") and not unique[rp].get("sha256"):
                unique[rp]["sha256"] = item["sha256"]
            if item.get("x_url") and not unique[rp].get("x_url"):
                unique[rp]["x_url"] = item["x_url"]

    downloaded_files = []
    print(f"[{app_id}] Checking {len(unique)} declared bundled assets...")

    if "x-asset-bundle" in mf:
        return _fetch_asset_bundle(app_dir, mf["x-asset-bundle"], unique)

    for rel_path, item in sorted(unique.items()):
        dest = _safe_dest_path(app_dir, rel_path)
        expected_sha = item.get("sha256")
        x_url = item.get("x_url")
        basename = os.path.basename(rel_path)

        # Check if already cached with valid sha256
        if os.path.isfile(dest):
            if expected_sha:
                if _matches_asset(dest, item):
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
        if item.get("size") is not None and os.path.getsize(dest) != item["size"]:
            os.remove(dest)
            raise ValueError(f"Size verification failed for {rel_path} in {app_id}")
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

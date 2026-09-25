#!/usr/bin/env python3
"""
publish_app.py -- publish ONE prebuilt App Center package to the CDN store.

Device appmgr's store reads a single catalog:

    https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/catalog.json

This script adds (or upgrades) exactly one app in that catalog without touching
the other entries:

  1. fetch the live catalog; save its raw bytes as <staging>/catalog.prev.json
     (rollback copy);
  2. assemble a clean staging dist containing ONLY the packages the live catalog
     lists (local packaging/dist copy when its sha256 matches, else downloaded
     from the live URL and sha256-checked), with .sig files written from the
     catalog's own signatures;
  3. inspect the new package without executing anything (safe member list,
     size/member caps, manifest v2, BOM + release lock, icon declaration);
  4. signature: `<pkg>.sig` next to the input, or `--sign` (signs a COPY in
     staging); always verified against the vendor public key;
  5. regenerate the catalog with gen_catalog.build_catalog and require every
     existing entry to be byte-for-byte equal to the live one;
  6. print the upload plan. Only with --yes: upload package -> icon ->
     catalog.json LAST, each downloaded back and sha256-compared, then poll the
     CDN until the new entry is visible.

Usage:
    python3 publish_app.py <pkg.tar.gz> [--icon PATH] [--sign] [--yes] [--staging DIR]

Stdlib + openssl CLI + ossutil CLI (uses ~/.ossutilconfig; never pass keys).
Exit codes: 0 ok / already live, 1 refused or failed, 3 uploaded but CDN not
refreshed within the retry window.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_MARKET = os.path.dirname(_HERE)
if _MARKET not in sys.path:
    sys.path.insert(0, _MARKET)

from appmgr import manifest as contract  # noqa: E402  (stdlib-only validator)
from appmgr import paths as appmgr_paths  # noqa: E402


def _load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gen_catalog = _load("publish_gen_catalog", os.path.join(_MARKET, "catalog", "gen_catalog.py"))
sign = _load("publish_sign", os.path.join(_HERE, "sign.py"))

OSS_BASE = "oss://sensecraft-statics/solution-app/recamera_pro"
CDN_BASE = "https://sensecraft-statics.seeed.cc/solution-app/recamera_pro"
CATALOG_URL = CDN_BASE + "/catalog.json"
PACKAGES_BASE = CDN_BASE + "/packages/"
ICONS_BASE = CDN_BASE + "/icons/"
DEFAULT_DIST = os.path.join(_HERE, "dist")
VENDOR_PUB = os.path.join(_MARKET, "appmgr", "keys", "release_pub.pem")
ICON_MEDIA = {"png": "image/png", "webp": "image/webp",
              "jpg": "image/jpeg", "jpeg": "image/jpeg"}
CDN_RETRIES = 12
CDN_RETRY_SEC = 10


class PublishError(Exception):
    """A refusal. Message is printed; exit code 1."""


def log(msg: str = "") -> None:
    print(msg, flush=True)


def sha256_file(path: str) -> str:
    return gen_catalog._sha256_and_size(path)[0]


# --------------------------------------------------------------------------- network

def http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def http_download(url: str, dest: str) -> None:
    req = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as out:
        shutil.copyfileobj(resp, out, 1 << 20)


def ossutil(*args: str) -> None:
    p = subprocess.run(["ossutil", *args], stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT)
    if p.returncode != 0:
        tail = p.stdout.decode("utf-8", "replace").strip().splitlines()[-3:]
        raise PublishError(f"ossutil {args[0]} failed: {' | '.join(tail)}")


# --------------------------------------------------------------------------- staging

def assemble_staging(live: dict, dist_dir: str, pkg_dir: str, downloader=None) -> None:
    """Put exactly the live catalog's packages (+ .sig from the catalog) in pkg_dir."""
    downloader = downloader or http_download
    os.makedirs(pkg_dir, exist_ok=True)
    for app in live.get("apps", []):
        p = app["package"]
        fname = p["filename"]
        if os.path.basename(fname) != fname or not fname.endswith(".tar.gz"):
            raise PublishError(f"live catalog has unsafe filename {fname!r}")
        dest = os.path.join(pkg_dir, fname)
        local = os.path.join(dist_dir, fname)
        if os.path.isfile(local) and sha256_file(local) == p["sha256"]:
            try:
                os.link(local, dest)       # read-only use; never written
            except OSError:
                shutil.copy2(local, dest)
            src = "local"
        else:
            downloader(p["url"], dest)
            if sha256_file(dest) != p["sha256"]:
                raise PublishError(f"{fname}: downloaded bytes do not match live sha256")
            src = "downloaded"
        if not p.get("signature"):
            raise PublishError(f"live entry {app['id']} has no signature")
        with open(dest + ".sig", "w") as f:
            f.write(p["signature"] + "\n")
        log(f"  stage {fname:48s} ({src})")


def stage_existing_icons(live: dict, icons_dir: str) -> None:
    """Placeholder files so build_catalog re-emits each live icon_url unchanged.

    Only the new app's icon is ever uploaded; these are never read for bytes."""
    os.makedirs(icons_dir, exist_ok=True)
    for app in live.get("apps", []):
        url = app.get("icon_url")
        if not url:
            continue
        if not url.startswith(ICONS_BASE):
            raise PublishError(f"{app['id']}: icon_url outside {ICONS_BASE}: {url}")
        name = url[len(ICONS_BASE):]
        if "/" in name or not name:
            raise PublishError(f"{app['id']}: unexpected icon_url {url}")
        open(os.path.join(icons_dir, name), "wb").close()


# --------------------------------------------------------------------------- inspect

def inspect_package(path: str) -> dict:
    """Validate a package without extracting or executing it."""
    size = os.path.getsize(path)
    if size > appmgr_paths.MAX_PKG_BYTES:
        raise PublishError(f"package {size} B exceeds MAX_PKG_BYTES {appmgr_paths.MAX_PKG_BYTES}")
    try:
        tar = tarfile.open(path, "r:gz")
    except (tarfile.TarError, OSError) as e:
        raise PublishError(f"not a gzip tar: {e}")
    with tar:
        members = tar.getmembers()
        if len(members) > appmgr_paths.MAX_MEMBERS:
            raise PublishError(f"{len(members)} members > MAX_MEMBERS {appmgr_paths.MAX_MEMBERS}")
        names = [m.name for m in members]
        if len(set(names)) != len(names):
            raise PublishError("duplicate member paths")
        total = 0
        for m in members:
            n = m.name
            if n.startswith(("/", "\\")) or ".." in n.replace("\\", "/").split("/"):
                raise PublishError(f"unsafe member path {n!r}")
            if m.issym() or m.islnk():
                raise PublishError(f"link member {n!r}")
            if not (m.isfile() or m.isdir()):
                raise PublishError(f"special member (device/fifo/other) {n!r}")
            if m.mode & 0o7000:
                raise PublishError(f"setuid/setgid/sticky member {n!r}")
            try:
                contract.validate_package_member_path(n.rstrip("/"), "package member")
            except contract.ManifestValidationError as e:
                raise PublishError(str(e))
            total += max(0, m.size)
        if total > appmgr_paths.MAX_UNPACKED_BYTES:
            raise PublishError(f"unpacked {total} B > MAX_UNPACKED_BYTES")

        def read(name: str) -> bytes:
            try:
                f = tar.extractfile(tar.getmember(name))
            except KeyError:
                raise PublishError(f"missing {name}")
            if f is None:
                raise PublishError(f"{name} is not a regular file")
            return f.read()

        try:
            manifest = json.loads(read("manifest.json").decode("utf-8"))
            contract.validate_manifest(manifest, allow_v1=False)
        except (ValueError, contract.ManifestValidationError) as e:
            raise PublishError(f"manifest invalid (v2 required): {e}")

        records = {}
        for m in members:
            if not m.isfile() or m.name in contract.RESERVED_PACKAGE_PATHS:
                continue
            h = hashlib.sha256()
            got = 0
            with tar.extractfile(m) as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
                    got += len(chunk)
            records[m.name] = {"sha256": h.hexdigest(), "size": got}
        try:
            contract.validate_package_files(manifest, records)
            lock = json.loads(read(contract.RELEASE_LOCK_PATH).decode("utf-8"))
            contract.verify_release_metadata(manifest, lock, read(contract.BOM_PATH), records)
        except (ValueError, contract.ManifestValidationError) as e:
            raise PublishError(f"BOM/release lock check failed: {e}")

        icon = None
        if "icon" in manifest:
            decl = contract.validate_icon_declaration(manifest["icon"])
            data = read(decl["path"])
            check_icon_bytes(data, decl["media_type"], decl["path"])
            icon = {"ext": decl["path"].rsplit(".", 1)[1].lower(), "data": data}
    return {"manifest": manifest, "id": manifest["id"], "version": manifest["version"],
            "icon": icon, "members": len(members), "unpacked": total}


def check_icon_bytes(data: bytes, media_type: str, label: str) -> None:
    if len(data) > contract.MAX_ICON_BYTES:
        raise PublishError(f"icon {label}: {len(data)} B > {contract.MAX_ICON_BYTES}")
    if not contract.icon_bytes_match_media_type(data[:16], media_type):
        raise PublishError(f"icon {label}: bytes do not match {media_type}")


def load_icon_override(path: str) -> dict:
    ext = path.rsplit(".", 1)[-1].lower()
    if ext not in ICON_MEDIA:
        raise PublishError(f"--icon must be one of {sorted(ICON_MEDIA)}: {path}")
    with open(path, "rb") as f:
        data = f.read()
    check_icon_bytes(data, ICON_MEDIA[ext], path)
    return {"ext": ext, "data": data}


def _semver_core(v: str):
    m = re.match(r"(\d+)\.(\d+)\.(\d+)", v or "")
    return tuple(int(x) for x in m.groups()) if m else None


def classify(live: dict, app_id: str, version: str, sha: str):
    """Return ("noop"|"add"|"upgrade", old_entry_or_None). Raise on refusal."""
    old = next((a for a in live.get("apps", []) if a["id"] == app_id), None)
    if old is None:
        return "add", None
    if old["version"] == version:
        if old["package"]["sha256"] == sha:
            return "noop", old
        raise PublishError(
            f"{app_id} {version} is already live with different bytes "
            f"(live sha256 {old['package']['sha256'][:12]}…, new {sha[:12]}…). "
            "Published versions are immutable: bump the version.")
    new_v, old_v = _semver_core(version), _semver_core(old["version"])
    if new_v is None or old_v is None or new_v <= old_v:
        raise PublishError(f"{app_id}: new version {version} is not greater than live "
                           f"{old['version']} (downgrade / ambiguous prerelease refused)")
    return "upgrade", old


# --------------------------------------------------------------------------- signature

def verify_sig(pkg: str, pub: str) -> None:
    with open(pkg + ".sig") as f:
        b64 = f.read().strip()
    fd, der = tempfile.mkstemp(suffix=".der")
    try:
        with os.fdopen(fd, "wb") as w:
            w.write(base64.b64decode(b64))
        p = subprocess.run(["openssl", "dgst", "-sha256", "-verify", pub,
                            "-signature", der, pkg],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    finally:
        os.unlink(der)
    if p.returncode != 0 or b"Verified OK" not in p.stdout:
        raise PublishError(f"signature does NOT verify against vendor key {pub}")


def stage_signature(src_pkg: str, staged_pkg: str, do_sign: bool, key: str, pub: str) -> str:
    side = src_pkg + ".sig"
    if os.path.isfile(side):
        shutil.copyfile(side, staged_pkg + ".sig")
        how = f"existing {os.path.basename(side)}"
    elif do_sign:
        if not os.path.isfile(key):
            raise PublishError(f"--sign: private key not found at {key}")
        sign.sign_one(staged_pkg, key)
        how = "signed staging copy with release key"
    else:
        raise PublishError(
            "package is unsigned (no <pkg>.sig next to it). The device store only "
            "installs packages with a trusted signature. Re-run with --sign "
            "(vendor release key) or provide the .sig.")
    verify_sig(staged_pkg, pub)
    return how


# --------------------------------------------------------------------------- catalog

def regenerate(live: dict, pkg_dir: str, icons_dir: str, empty_dir: str) -> dict:
    # Store packages are self-contained (bundled artifacts), so every app gets
    # models: [] regardless of the repo's models.json, which describes the
    # legacy shared-model layout and would require staged model files.
    # A missing spec file loads as {} in gen_catalog._load_models_spec.
    cat = gen_catalog.build_catalog(
        pkg_dir, PACKAGES_BASE,
        models_dir=gen_catalog.DEFAULT_MODELS_DIR,
        models_spec_path=os.path.join(empty_dir, "models.json"),
        runtimes_dir=empty_dir, icons_dir=icons_dir)
    # Runtime bundles are not apps and are not re-uploaded here: carry the live
    # descriptors over verbatim instead of re-hashing local copies.
    cat.pop("runtimes", None)
    if "runtimes" in live:
        cat["runtimes"] = live["runtimes"]
    for k in ("schema", "source"):
        if k in live:
            cat[k] = live[k]
    return cat


def check_diff(live: dict, new: dict, app_id: str) -> dict:
    """Every live entry except app_id must be unchanged; app_id is the only change."""
    live_by = {a["id"]: a for a in live.get("apps", [])}
    new_by = {a["id"]: a for a in new.get("apps", [])}
    problems = []
    for i, a in live_by.items():
        if i == app_id:
            continue
        b = new_by.get(i)
        if b is None:
            problems.append(f"  {i}: disappeared from regenerated catalog")
            continue
        for k in sorted(set(a) | set(b)):
            if a.get(k) != b.get(k):
                problems.append(f"  {i}.{k}: live={json.dumps(a.get(k), ensure_ascii=False)[:160]}"
                                f"\n  {' ' * len(i)} new ={json.dumps(b.get(k), ensure_ascii=False)[:160]}")
    extra = set(new_by) - set(live_by) - {app_id}
    for i in sorted(extra):
        problems.append(f"  {i}: unexpected new app in regenerated catalog")
    if app_id not in new_by:
        problems.append(f"  {app_id}: missing from regenerated catalog")
    for k in ("schema", "source", "runtimes"):
        if live.get(k) != new.get(k):
            problems.append(f"  top-level {k} differs")
    if problems:
        raise PublishError("regenerated catalog differs from live beyond the new app:\n"
                           + "\n".join(problems))
    return new_by[app_id]


# --------------------------------------------------------------------------- upload

def upload_verified(local: str, oss_url: str) -> None:
    ossutil("cp", "-f", local, oss_url)
    fd, tmp = tempfile.mkstemp()
    os.close(fd)
    try:
        ossutil("cp", "-f", oss_url, tmp)
        want, got = sha256_file(local), sha256_file(tmp)
    finally:
        os.unlink(tmp)
    if want != got:
        raise PublishError(f"verify FAIL {oss_url}: want {want} got {got}")
    log(f"  verify OK  {oss_url}  {got[:16]}…")


def wait_cdn(entry: dict) -> bool:
    for i in range(CDN_RETRIES):
        try:
            cat = json.loads(http_get(f"{CATALOG_URL}?_={int(time.time())}"))
            hit = next((a for a in cat.get("apps", []) if a["id"] == entry["id"]), None)
            if hit and hit["version"] == entry["version"] and \
                    hit["package"]["sha256"] == entry["package"]["sha256"]:
                log(f"  CDN catalog shows {entry['id']} {entry['version']} (try {i + 1})")
                return True
        except Exception as e:  # noqa: BLE001 -- transient CDN errors are retried
            log(f"  CDN fetch error: {e}")
        time.sleep(CDN_RETRY_SEC)
    return False


# --------------------------------------------------------------------------- main

def run(args) -> int:
    src = os.path.abspath(args.package)
    if not os.path.isfile(src):
        raise PublishError(f"no such package: {src}")

    staging = args.staging or tempfile.mkdtemp(
        prefix=time.strftime("recamera-publish-%Y%m%d-%H%M%S-"),
        dir="/tmp" if os.path.isdir("/tmp") else None)
    os.makedirs(staging, exist_ok=True)
    if os.listdir(staging):
        raise PublishError(f"staging dir must be empty: {staging}")
    pkg_dir = os.path.join(staging, "packages")
    icons_dir = os.path.join(staging, "icons")
    empty_dir = os.path.join(staging, "empty")
    os.makedirs(empty_dir)
    log(f"staging: {staging}")

    raw = fetch_live()
    with open(os.path.join(staging, "catalog.prev.json"), "wb") as f:
        f.write(raw)
    live = json.loads(raw)
    log(f"live catalog: {len(live.get('apps', []))} apps, generated {live.get('generated')}")

    log("\n[1] inspect new package")
    info = inspect_package(src)
    sha = sha256_file(src)
    app_id, version = info["id"], info["version"]
    log(f"  {app_id} {version}  sha256 {sha[:16]}…  {os.path.getsize(src)} B, "
        f"{info['members']} members, unpacked {info['unpacked']} B")
    log("  manifest v2 / BOM / release lock / member paths: OK")
    action, old = classify(live, app_id, version, sha)
    if action == "noop":
        log(f"\n{app_id} {version} is already live with identical bytes. Nothing to do.")
        return 0

    log("\n[2] assemble staging from live catalog")
    assemble_staging(live, args.dist, pkg_dir)
    stage_existing_icons(live, icons_dir)
    if old is not None:
        for p in (old["package"]["filename"], old["package"]["filename"] + ".sig"):
            os.unlink(os.path.join(pkg_dir, p))
        log(f"  upgrade: removed {old['package']['filename']} from staging")
    canonical = f"{app_id}-{version}-arm64.tar.gz"
    staged_pkg = os.path.join(pkg_dir, canonical)
    shutil.copyfile(src, staged_pkg)
    if sha256_file(staged_pkg) != sha:
        raise PublishError("staging copy sha256 mismatch")

    log("\n[3] signature")
    how = stage_signature(src, staged_pkg, args.sign, args.key, args.pub)
    log(f"  {how}; verified against {os.path.relpath(args.pub, _MARKET)}: OK")

    log("\n[4] icon")
    icon = load_icon_override(args.icon) if args.icon else info["icon"]
    icon_local = None
    if icon:
        for e in gen_catalog._ICON_EXTS:
            p = os.path.join(icons_dir, f"{app_id}.{e}")
            if os.path.exists(p):
                os.unlink(p)
        icon_local = os.path.join(icons_dir, f"{app_id}.{icon['ext']}")
        with open(icon_local, "wb") as f:
            f.write(icon["data"])
        log(f"  {'--icon' if args.icon else 'package icon'} -> icons/{app_id}.{icon['ext']} "
            f"({len(icon['data'])} B)")
    elif old is not None and old.get("icon_url"):
        log(f"  no icon supplied; keeping live {old['icon_url']}")
    else:
        log("  no icon (store will show the default tile)")

    log("\n[5] regenerate catalog")
    cat = regenerate(live, pkg_dir, icons_dir, empty_dir)
    entry = check_diff(live, cat, app_id)
    if entry.get("models"):
        raise PublishError("new entry declares shared models[]; this script does not "
                           "upload models -- use publish_oss.sh")
    cat_path = os.path.join(staging, "catalog.json")
    with open(cat_path, "w") as f:
        json.dump(cat, f, indent=2, ensure_ascii=False)
        f.write("\n")
    log("  runtimes: carried over from the live catalog verbatim "
        "(gen_catalog 'no voice-runtime/gst-hwcodec' notes above are expected)")
    unchanged = len(live.get("apps", [])) - (1 if old else 0)
    log(f"  unchanged: {unchanged}   {'upgraded' if old else 'added'}: 1   "
        f"total: {len(cat['apps'])}")
    if old:
        log(f"  upgrade {app_id}: {old['version']} -> {version}")
    shown = {k: v for k, v in entry.items() if k != "description"}
    shown["package"] = {k: v for k, v in entry["package"].items() if k != "signature"}
    log("  new entry: " + json.dumps(shown, ensure_ascii=False, indent=2).replace("\n", "\n  "))

    uploads = [(staged_pkg, f"{OSS_BASE}/packages/{canonical}")]
    if icon_local:
        uploads.append((icon_local, f"{OSS_BASE}/icons/{os.path.basename(icon_local)}"))
    uploads.append((cat_path, f"{OSS_BASE}/catalog.json"))
    log("\n[6] upload plan (in order; catalog.json last)")
    for local, url in uploads:
        log(f"  {os.path.relpath(local, staging):48s} -> {url}  "
            f"({os.path.getsize(local)} B, sha256 {sha256_file(local)[:16]}…)")
    rollback = f"ossutil cp -f {staging}/catalog.prev.json {OSS_BASE}/catalog.json"
    log(f"  rollback: {rollback}")

    if not args.yes:
        log("\nDRY RUN -- nothing uploaded. Re-run with --yes after confirming the plan.")
        return 0

    log("\n[7] uploading")
    for local, url in uploads:
        upload_verified(local, url)
    log("\n[8] waiting for CDN catalog")
    if not wait_cdn(entry):
        log(f"OSS objects verified, but {CATALOG_URL} does not show the new entry yet. "
            f"Re-check later; rollback: {rollback}")
        return 3
    log(f"\npublished {app_id} {version}. Rollback: {rollback}")
    return 0


def fetch_live() -> bytes:
    return http_get(f"{CATALOG_URL}?_={int(time.time())}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Publish one App Center package to the CDN store.")
    ap.add_argument("package", help="prebuilt manifest-v2 <id>-<ver>-arm64.tar.gz")
    ap.add_argument("--icon", help="icon override (png/webp/jpg, <=1 MiB)")
    ap.add_argument("--sign", action="store_true",
                    help="sign a staging copy with the release key if no .sig is supplied")
    ap.add_argument("--yes", action="store_true", help="actually upload (default: dry run)")
    ap.add_argument("--staging", help="empty dir for staging (default: new temp dir)")
    ap.add_argument("--dist", default=DEFAULT_DIST, help=argparse.SUPPRESS)
    ap.add_argument("--key", default=sign.DEFAULT_KEY, help=argparse.SUPPRESS)
    ap.add_argument("--pub", default=VENDOR_PUB, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    try:
        return run(args)
    except PublishError as e:
        print(f"\nREFUSED: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

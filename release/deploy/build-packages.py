#!/usr/bin/env python3
"""
build-packages.py -- pack the three application-layer bundles for a reCamera Pro
one-shot deploy. Fully deterministic (same inputs -> same bytes -> same
md5), modelled on market/packaging/build.py:

  1. appmgr-v<ver>.tar.gz    market/appmgr/  ->  extract at /userdata/local/  (dir: appmgr/)
  2. frontend-v<ver>.tar.gz  web build/      ->  extract at /oem/usr/www/
  3. apps-v<ver>.tar.gz      apps/<id>/      ->  extract at /userdata/local/apps/

Model files (*.rknn/*.onnx) are NOT bundled: they are shared device-side deps
delivered via the catalog. Packages carry only code + manifests + small config.

Determinism: tar member uid/gid/uname/gname=0/"", mtime=0, normalized mode;
gzip written with mtime=0 and no embedded filename.

Stdlib only.  Usage:
    python3 build-packages.py [--repo <recamera_pro>] [--frontend <build_dir>] [--out <dir>]
"""
from __future__ import annotations

import argparse
import fnmatch
import gzip
import hashlib
import io
import os
import sys
import tarfile

DEFAULT_VERSION = "1.5.0"

# The shipped apps. This list is the single source of truth for what a release
# contains -- kit/tests/test_kit_run_entry.py reads it, so an app added to
# apps/ without being added here is caught rather than shipped by accident.
APPS = [
    "crayfish-fight",
    "face-analysis", "facemesh-reader", "fall-detection", "fitness-trainer",
    "ppocr-reader", "qrcode-reader", "retail-vision", "voice-transcribe",
    "yolo-detector",
]

# Apps that live in apps/ but are NOT shipped yet. Listed so the inventory test
# can tell "deliberately held back" from "someone forgot to add it to APPS".
UNSHIPPED_APPS = [
    # imports paho.mqtt, which the kit deliberately does NOT depend on
    # (kit/adapters/mqtt_sink.py hand-rolls a client for exactly that reason)
    # and which the manifest does not declare as a runtime.
    "intrusion-detection",
    # work in progress; not reviewed for release.
    "face-recognition",
]

# Bundled per app: thin code + manifest (+ small model-side config in models/).
# `app.py` is not the whole app: several ship sibling helper modules next to it
# (ppocr-reader/striptext.py, crayfish-fight/logic.py) and an app package
# missing one installs fine and dies at `import` on the first frame. The App
# Center builder (market/packaging/build.py) already takes "any sibling helper
# .py"; this list used to name app.py alone, so the two packagers disagreed.
APP_INCLUDE_TOP = ("manifest.json", "app.py", "README.md", "models")
# ...plus every other top-level .py in the app dir, minus tests.
APP_INCLUDE_SIBLING_PY = True
# Big model weights never travel in an app package (shared, via catalog).
MODEL_EXCLUDE_GLOBS = ("*.rknn", "*.onnx")
# fall-detection ships dev/training extras we never deploy.
FALL_EXCLUDE_TOP = ("evaluation", "tools", "models")

# appmgr: everything under market/appmgr/ EXCEPT caches / tests / editor bak.
APPMGR_EXCLUDE_DIRS = ("__pycache__", "tests", ".pytest_cache")
APPMGR_EXCLUDE_GLOBS = ("*.bak", "*.bak.*", "*.bak-*", "*.local.bak", "*.pyc",
                        ".DS_Store", "._*")


def _reset(ti: tarfile.TarInfo) -> tarfile.TarInfo:
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = ""
    ti.mtime = 0
    if ti.isdir():
        ti.mode = 0o755
    else:
        ti.mode = 0o755 if (ti.mode & 0o111) else 0o644
    return ti


def _write(members, out_path):
    """members: list of (abspath, arcname). Deterministic tar.gz -> md5/size."""
    members = sorted(set(members), key=lambda m: m[1])
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for ap, arc in members:
            tar.add(ap, arcname=arc, filter=_reset, recursive=False)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "wb") as fout:
        with gzip.GzipFile(filename="", mode="wb", fileobj=fout, mtime=0) as gz:
            gz.write(tar_buf.getvalue())
    data = open(out_path, "rb").read()
    return len(data), hashlib.md5(data).hexdigest(), len(members)


def _skip(name, globs):
    return any(fnmatch.fnmatch(name, g) for g in globs)


# ---- appmgr ----------------------------------------------------------------
def collect_appmgr(appmgr_dir):
    members = []
    base = os.path.dirname(appmgr_dir.rstrip("/"))  # parent so arc starts with appmgr/
    for root, dirs, files in os.walk(appmgr_dir):
        dirs[:] = [d for d in dirs if d not in APPMGR_EXCLUDE_DIRS
                   and not _skip(d, APPMGR_EXCLUDE_GLOBS)]
        arc_dir = os.path.relpath(root, base)
        members.append((root, arc_dir))
        for f in files:
            if _skip(f, APPMGR_EXCLUDE_GLOBS):
                continue
            members.append((os.path.join(root, f), os.path.relpath(os.path.join(root, f), base)))
    return members


# ---- frontend --------------------------------------------------------------
def collect_frontend(build_dir):
    members = []
    for root, dirs, files in os.walk(build_dir):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        rel = os.path.relpath(root, build_dir)
        if rel != ".":
            members.append((root, rel))
        for f in files:
            if f == ".DS_Store" or f.startswith("._"):
                continue
            ap = os.path.join(root, f)
            members.append((ap, os.path.relpath(ap, build_dir)))
    return members


# ---- apps ------------------------------------------------------------------
def collect_apps(apps_dir):
    members = []
    for app in APPS:
        adir = os.path.join(apps_dir, app)
        if not os.path.isdir(adir):
            sys.exit(f"error: missing app dir {adir}")
        members.append((adir, app))
        if APP_INCLUDE_SIBLING_PY:
            for f in sorted(os.listdir(adir)):
                if (f.endswith(".py") and f not in APP_INCLUDE_TOP
                        and not f.startswith("test_")
                        and os.path.isfile(os.path.join(adir, f))):
                    members.append((os.path.join(adir, f), f"{app}/{f}"))
        fall = (app == "fall-detection")
        for top in APP_INCLUDE_TOP:
            if fall and top in FALL_EXCLUDE_TOP:
                continue
            p = os.path.join(adir, top)
            if not os.path.exists(p):
                continue
            if os.path.isfile(p):
                members.append((p, f"{app}/{top}"))
                continue
            for root, dirs, files in os.walk(p):
                dirs[:] = [d for d in dirs if d != "__pycache__" and not d.startswith(".")]
                rel = os.path.relpath(root, adir)
                members.append((root, f"{app}/{rel}"))
                for f in files:
                    if (f.startswith(".") or f.endswith(".pyc")
                            or f.startswith("test_")
                            or _skip(f, MODEL_EXCLUDE_GLOBS)):
                        continue
                    ap = os.path.join(root, f)
                    members.append((ap, f"{app}/{os.path.relpath(ap, adir)}"))
    return members


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    repo_default = os.path.abspath(os.path.join(here, "..", ".."))
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=repo_default)
    ap.add_argument("--frontend", required=True, help="path to the web build/ dir")
    ap.add_argument("--out", default=here)
    ap.add_argument("--version", default=DEFAULT_VERSION,
                    help=f"release version (default: {DEFAULT_VERSION})")
    args = ap.parse_args()

    jobs = [
        ("appmgr",   f"appmgr-v{args.version}.tar.gz",   collect_appmgr(os.path.join(args.repo, "market", "appmgr"))),
        ("frontend", f"frontend-v{args.version}.tar.gz", collect_frontend(args.frontend)),
        ("apps",     f"apps-v{args.version}.tar.gz",     collect_apps(os.path.join(args.repo, "apps"))),
    ]
    for label, name, members in jobs:
        out = os.path.join(args.out, name)
        size, md5, n = _write(members, out)
        print(f"[{label:8}] {name}")
        print(f"           members {n}  size {size} bytes ({size/1024:.1f} KiB)  md5 {md5}")


if __name__ == "__main__":
    main()

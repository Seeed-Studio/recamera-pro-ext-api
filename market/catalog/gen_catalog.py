#!/usr/bin/env python3
"""
gen_catalog.py -- generate the cloud install catalog from built app packages.

The reCamera has no route to the internet in the common setup (over USB its only
interface is usb0, default route points back at itself), so the device cannot
fetch its own packages. The browser -- the machine the user actually sits at --
does have a route, so it downloads on the device's behalf, verifies the sha256,
and pushes the bytes to appmgr's upload endpoint. Nothing on the device reaches
out.

This script is the single source of truth for the catalog: it scans
`packaging/dist/*.tar.gz`, reads each package's embedded manifest.json for
id/name/version/description, and records the real sha256 + size. Package URLs
and checksums are never hand-written.

Catalog schema (v1):

    {
      "schema": 1,
      "generated": "2026-08-09T12:00:00Z",
      "source": "recamera_pro/market/packaging/dist",
      "apps": [
        {
          "id": "fall-detection",
          "name": "Fall Detection",
          "version": "0.1.0",
          "description": "...",
          "arch": "arm64",
          "package": {
            "url": "/appcenter/apps/fall-detection-0.1.0-arm64.tar.gz",
            "filename": "fall-detection-0.1.0-arm64.tar.gz",
            "sha256": "<hex>",
            "size": 2865713
          }
        }
      ]
    }

Shared models (one-gen `models[]`+`target_path` parity): most reCamera Pro app
packages bundle their model(s) inside the tar (see packaging/build.py), so their
`models` list is empty. But some apps (voice-transcribe) ship a LARGE, SHARED
model that lives in a well-known device dir (/userdata/local/models/asr) reused
by several apps -- bundling it in every package would be wasteful. Those files
are declared in `models.json` (app id -> single-target {target_path, files[]},
or multi-target {groups:[{target_path, files[], subdir?}, ...]} so one app's
files can land in several device dirs -- e.g. voice-transcribe drops ASR files
in …/asr and KWS files in …/asr/kws), staged under `<models-dir>/<app_id>/`
(plus the group's optional `subdir`), hashed here, and emitted as `models[]`
entries {url, filename, sha256, size, target_path} -- one entry per file, each
carrying its own group's target_path. The browser downloads each, verifies the
sha256, and drops it into target_path via appmgr's /putModel before install.

Stdlib only (hashlib, tarfile, json).

Usage:
    python3 gen_catalog.py                       # scan ../packaging/dist, base /appcenter/apps/
    python3 gen_catalog.py --dist DIR --out FILE --base-url https://cdn.example/packages/
    python3 gen_catalog.py --models-dir DIR --models-base-url https://cdn.example/models/
"""
from __future__ import annotations

import argparse
import datetime
import glob
import hashlib
import json
import os
import re
import sys
import tarfile

APP_ID_RE = re.compile(r"[a-z0-9-]{1,64}")
_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DIST = os.path.normpath(os.path.join(_HERE, "..", "packaging", "dist"))
DEFAULT_OUT = os.path.join(_HERE, "catalog.json")
# Device-local (integrated layout) default: packages are served from
# /appcenter/apps/ (nginx alias -> /userdata/local/appcenter/apps/). This is the
# authoritative "post-integration" layout; production CDN dist overrides it via
# --base-url (e.g. https://sensecraft-statics.seeed.cc/.../packages/).
DEFAULT_BASE_URL = "/appcenter/apps/"
DEFAULT_MODELS_SPEC = os.path.join(_HERE, "models.json")
DEFAULT_MODELS_DIR = os.path.normpath(os.path.join(_HERE, "..", "packaging", "models"))
# On-demand runtime bundles (release/build-voice-runtime.sh output). These are
# NOT apps: they are ~18 MB of aarch64 wheels a device only needs if it installs
# an app declaring the matching capability, which is exactly why they are not in
# the kit package (INSTALL_ASSETS_SPEC §3). Keyed by CAPABILITY, because that is
# what the store has in hand -- it reads `capabilities: ["audio"]` off the app.
DEFAULT_RUNTIMES_DIR = os.path.normpath(os.path.join(_HERE, "..", "..", "release", "dist"))
RUNTIME_BUNDLES = {
    "audio": "voice-runtime-*.tar.gz",       # release/build-voice-runtime.sh
    # release/build-gst-hwcodec.sh -- 3 .so (~0.4 MB) + the env vars appmgr
    # injects. Same channel as the audio bundle, different shape: it unpacks to
    # /userdata/lib instead of pip-installing into the venv (RUNTIME_BUNDLE_SPEC).
    "hwcodec": "gst-hwcodec-*.tar.gz",
}


def _sha256_and_size(path: str) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def _read_sig(pkg_path: str) -> str | None:
    """Return the base64 signature from the `<pkg>.sig` sidecar, or None."""
    sig_path = pkg_path + ".sig"
    try:
        with open(sig_path) as f:
            s = f.read().strip()
        return s or None
    except OSError:
        return None


def _read_manifest(pkg_path: str) -> dict:
    """Pull manifest.json out of the package (top-level member)."""
    with tarfile.open(pkg_path, "r:gz") as tar:
        try:
            m = tar.getmember("manifest.json")
        except KeyError:
            raise ValueError(f"{os.path.basename(pkg_path)}: no top-level manifest.json")
        f = tar.extractfile(m)
        if f is None:
            raise ValueError(f"{os.path.basename(pkg_path)}: cannot read manifest.json")
        return json.loads(f.read().decode("utf-8"))


def _derive_models_base(base_url: str, override: str | None) -> str:
    """URL prefix the shared model files are served under.

    Explicit --models-base-url wins. Otherwise mirror the package base with the
    trailing `packages/` or `pkgs/` segment swapped for `models/` (so a CDN base
    like .../recamera_pro/packages/ -> .../recamera_pro/models/)."""
    if override:
        return override if override.endswith("/") else override + "/"
    base = base_url if base_url.endswith("/") else base_url + "/"
    swapped = re.sub(r"(packages|pkgs)/$", "models/", base)
    return swapped if swapped != base else base + "models/"


def _load_models_spec(spec_path: str) -> dict:
    """Read models.json (app id -> single-target {target_path, files[]} OR
    multi-target {groups:[{target_path, files[], subdir?}, ...]}); see
    _spec_groups. Missing file -> {}."""
    try:
        with open(spec_path) as f:
            spec = json.load(f)
    except FileNotFoundError:
        return {}
    return {k: v for k, v in spec.items() if not k.startswith("_")}


def _spec_groups(app_id: str, entry: dict) -> list:
    """Normalize an app's models.json entry into a list of groups.

    Two accepted forms (see models.json `_schema`):
      * single target (backward compatible): {"target_path", "files"}
        -> one group with subdir "".
      * multi target: {"groups": [{"target_path", "files", "subdir"?}, ...]}
        -> each group carries its OWN target_path (so different files land in
        different device subdirs) and an optional staging/URL `subdir`.
    Every returned group is {"target_path", "subdir", "files"}."""
    if "groups" in entry:
        groups = entry["groups"]
        if not isinstance(groups, list) or not groups:
            raise SystemExit(f"models.json: {app_id} 'groups' must be a non-empty list")
        norm = []
        for i, g in enumerate(groups):
            if "target_path" not in g or "files" not in g:
                raise SystemExit(
                    f"models.json: {app_id} group #{i} needs 'target_path' and 'files'")
            norm.append({
                "target_path": g["target_path"],
                "subdir": g.get("subdir", ""),
                "files": g["files"],
            })
        return norm
    # Single-target backward-compatible form.
    if "target_path" not in entry or "files" not in entry:
        raise SystemExit(
            f"models.json: {app_id} needs 'target_path'+'files' or 'groups'")
    return [{"target_path": entry["target_path"], "subdir": "", "files": entry["files"]}]


def _build_models(app_id: str, spec: dict, models_dir: str, models_base: str) -> list:
    """Resolve one app's shared-model files into catalog `models[]` entries.

    Files are staged at <models-dir>/<app_id>/<subdir>/<filename> (subdir empty
    for the common case); each is hashed for its real sha256/size. A missing
    staged file is a hard error -- shipping a catalog that points at a model the
    browser can't fetch is worse than failing loudly. Each entry carries its own
    group's target_path, so one app can drop files into several device dirs (e.g.
    voice-transcribe: .../asr plus .../asr/kws). The emitted entry shape
    {url, filename, sha256, size, target_path} is unchanged -- the browser still
    fetches `url`, verifies `sha256`, and pushes `filename` into `target_path`."""
    entry = spec.get(app_id)
    if not entry:
        return []
    out = []
    app_stage = os.path.join(models_dir, app_id)
    for group in _spec_groups(app_id, entry):
        target_path = group["target_path"]
        subdir = group["subdir"].strip("/")
        stage_dir = os.path.join(app_stage, subdir) if subdir else app_stage
        url_prefix = models_base + app_id + "/" + (subdir + "/" if subdir else "")
        for fname in group["files"]:
            fpath = os.path.join(stage_dir, fname)
            if not os.path.isfile(fpath):
                raise SystemExit(
                    f"models.json: {app_id} needs {fname} but it is not staged at {fpath}")
            sha, size = _sha256_and_size(fpath)
            out.append({
                "url": url_prefix + fname,
                "filename": fname,
                "sha256": sha,
                "size": size,
                "target_path": target_path,
            })
            print(f"  model {fname:40s} {size/1024:10.1f} KiB  {sha[:12]}… -> {target_path}",
                  file=sys.stderr)
    return out


def _build_runtimes(runtimes_dir: str, base: str) -> dict:
    """Describe the on-demand runtime bundles present in `runtimes_dir`.

    Absent bundle -> absent key, not a stub: the store treats "no descriptor" as
    "this device cannot be provisioned from the catalog" and says so, which is
    honest. A stub with a dead URL would instead fail mid-download.
    """
    out = {}
    for capability, pattern in RUNTIME_BUNDLES.items():
        hits = sorted(glob.glob(os.path.join(runtimes_dir, pattern)))
        if not hits:
            print(f"note  no {pattern} in {runtimes_dir} -- catalog omits the "
                  f"{capability!r} runtime (apps needing it cannot self-provision)",
                  file=sys.stderr)
            continue
        # Newest by version-ish sort order; warn rather than guess silently.
        if len(hits) > 1:
            print(f"WARN  {len(hits)} bundles match {pattern}; using "
                  f"{os.path.basename(hits[-1])}", file=sys.stderr)
        path = hits[-1]
        filename = os.path.basename(path)
        sha, size = _sha256_and_size(path)
        desc = {
            "url": base + filename,
            "filename": filename,
            "sha256": sha,
            "size": size,
        }
        # Embed the detached release signature (C1) exactly as app packages do:
        # runtime bundles are unpacked as root into shared trees (/userdata/rknnenv,
        # /userdata/lib), so the device verifies this base64 against the baked-in
        # public key before install. Unsigned -> flagged loudly; with the default
        # policy (require_signature=1) the device REFUSES the bundle.
        sig = _read_sig(path)
        if sig:
            desc["signature"] = sig
            desc["signature_alg"] = "ecdsa-sha256"
        else:
            print(f"WARN  {filename}: no .sig sidecar -- runtime bundle is UNSIGNED "
                  f"and will be REFUSED under the default signature policy "
                  f"(run packaging/sign.py --dist {runtimes_dir})", file=sys.stderr)
        out[capability] = desc
        print(f"add   runtime {capability:12s} {size/1048576:6.1f} MiB  {sha[:12]}…"
              f"  {'signed' if sig else 'UNSIGNED'}", file=sys.stderr)
    return out


def build_catalog(dist_dir: str, base_url: str, models_dir: str = DEFAULT_MODELS_DIR,
                  models_spec_path: str = DEFAULT_MODELS_SPEC,
                  models_base_url: str | None = None,
                  runtimes_dir: str = DEFAULT_RUNTIMES_DIR) -> dict:
    pkgs = sorted(glob.glob(os.path.join(dist_dir, "*.tar.gz")))
    if not pkgs:
        raise SystemExit(f"no *.tar.gz packages found in {dist_dir}")

    base = base_url if base_url.endswith("/") else base_url + "/"
    models_base = _derive_models_base(base_url, models_base_url)
    models_spec = _load_models_spec(models_spec_path)
    apps = []
    seen: dict[str, str] = {}   # id -> filename, to catch dup ids
    for pkg in pkgs:
        filename = os.path.basename(pkg)
        try:
            man = _read_manifest(pkg)
        except (ValueError, tarfile.TarError, OSError) as e:
            print(f"skip  {filename}: {e}", file=sys.stderr)
            continue
        app_id = man.get("id")
        if not (app_id and APP_ID_RE.fullmatch(app_id)):
            print(f"skip  {filename}: manifest id {app_id!r} not in [a-z0-9-]{{1,64}}",
                  file=sys.stderr)
            continue
        if app_id in seen:
            print(f"skip  {filename}: duplicate id {app_id!r} (already from {seen[app_id]})",
                  file=sys.stderr)
            continue
        seen[app_id] = filename
        sha, size = _sha256_and_size(pkg)
        package = {
            "url": base + filename,
            "filename": filename,
            "sha256": sha,
            "size": size,
        }
        # Embed the detached release signature (TODO #4) if sign.py produced a
        # `<pkg>.sig` sidecar. The base64 rides in the catalog so the browser can
        # relay it to the device on /install; the device verifies it against the
        # baked-in public key. Unsigned packages are flagged loudly -- with the
        # default policy (require_signature=1) the device will REFUSE them.
        sig = _read_sig(pkg)
        if sig:
            package["signature"] = sig
            package["signature_alg"] = "ecdsa-sha256"
        else:
            print(f"WARN  {filename}: no .sig sidecar -- package is UNSIGNED "
                  f"(run sign.py; device refuses unsigned installs by default)",
                  file=sys.stderr)
        apps.append({
            "id": app_id,
            "name": man.get("name", app_id),
            "version": man.get("version"),
            "description": man.get("description", ""),
            # Gallery presentation + i18n, passed through RAW from the manifest
            # (RENDER_DECLARATION_SPEC §5 P0-1/P0-2). The catalog is what the
            # browser renders BEFORE anything is installed, so dropping these
            # meant a third-party app could never show its own art or Chinese
            # copy in the store listing. Language selection stays a front-end
            # decision; keys absent from the manifest are omitted entirely.
            **{k: man[k] for k in ("image", "scene", "author",
                                   "name_zh", "description_zh", "scene_zh")
               if man.get(k) is not None},
            # Declared capabilities, forwarded verbatim. The browser needs these
            # BEFORE install to know an app pulls in something the device may not
            # have yet -- `"audio"` means the ~18 MB voice runtime has to be
            # provisioned first (INSTALL_ASSETS_SPEC §3.1). Dropping them here is
            # why that check could never fire: the manifest carries them, the
            # catalog did not, and the catalog is all the store can see.
            **({"capabilities": man["capabilities"]}
               if isinstance(man.get("capabilities"), list) else {}),
            # Kit requirement, forwarded verbatim -- same reason as capabilities
            # above, one step earlier. appmgr enforces this at install time from
            # the manifest INSIDE the package (appmgr/kitversion.py), so the gate
            # holds with or without this field; but the device only sees the
            # manifest after the browser has fetched the whole package and pushed
            # it over. Carrying the requirement in the catalog is what lets the
            # store compare it against the device's kit version and say "upgrade
            # the kit first" BEFORE spending a 50 MB download on a refusal.
            **({"kit": man["kit"]} if man.get("kit") else {}),
            "arch": "arm64",
            "package": package,
            # Shared models the browser drops into target_path before install.
            # Empty for the common case (model bundled inside the package).
            "models": _build_models(app_id, models_spec, models_dir, models_base),
        })
        print(f"add   {app_id:20s} v{man.get('version','?'):8s} "
              f"{size/1024:8.1f} KiB  {sha[:12]}…", file=sys.stderr)

    out = {
        "schema": 1,
        "generated": datetime.datetime.now(datetime.timezone.utc)
                        .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "recamera_pro/market/packaging/dist",
        "apps": apps,
    }
    runtimes = _build_runtimes(runtimes_dir, base)
    if runtimes:
        out["runtimes"] = runtimes
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Generate the App Center cloud catalog.")
    ap.add_argument("--dist", default=DEFAULT_DIST,
                    help=f"dir of built *.tar.gz packages (default: {DEFAULT_DIST})")
    ap.add_argument("--out", default=DEFAULT_OUT,
                    help=f"output catalog.json path (default: {DEFAULT_OUT})")
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL,
                    help="URL prefix packages are served under "
                         f"(default: {DEFAULT_BASE_URL!r}; use a CDN/OSS base for production)")
    ap.add_argument("--models-dir", default=DEFAULT_MODELS_DIR,
                    help=f"dir of staged shared-model files, <models-dir>/<app_id>/<file> "
                         f"(default: {DEFAULT_MODELS_DIR})")
    ap.add_argument("--models-spec", default=DEFAULT_MODELS_SPEC,
                    help=f"models.json mapping app id -> shared model files "
                         f"(default: {DEFAULT_MODELS_SPEC})")
    ap.add_argument("--models-base-url", default=None,
                    help="URL prefix shared models are served under "
                         "(default: package base with packages/|pkgs/ -> models/)")
    ap.add_argument("--runtimes-dir", default=DEFAULT_RUNTIMES_DIR,
                    help="dir holding on-demand runtime bundles, e.g. "
                         f"voice-runtime-<ver>.tar.gz (default: {DEFAULT_RUNTIMES_DIR})")
    args = ap.parse_args(argv)

    catalog = build_catalog(args.dist, args.base_url, args.models_dir,
                            args.models_spec, args.models_base_url,
                            args.runtimes_dir)
    with open(args.out, "w") as f:
        json.dump(catalog, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"\nwrote {args.out}  ({len(catalog['apps'])} apps, base-url {args.base_url})",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
server.py -- appmgr orchestration + minimal loopback HTTP API.

Orchestration (list / install / switch / stop) lives here as plain functions so
both the CLI (`python3 -m appmgr ...`) and the HTTP server share one code path.

Concurrency (APP_CENTER_PORT_DESIGN §4.1):
  * BUSY-GATE: every mutating op (install/switch/stop) takes an flock on
    busy.lock, serialising them; a second concurrent mutation gets "busy".
  * SINGLE-INSTANCE: the HTTP daemon takes an flock on appmgr.lock at startup;
    a second daemon exits rather than double-binding :8130.

API (loopback 127.0.0.1:8130; nginx /_jwt_verify guards the public edge):
  GET  /api/appMgr/list      -> installed apps + manifest + running + active
  GET  /api/appMgr/icon?id=  -> the app's package-bundled icon.<ext> bytes
                                (image/png|webp|jpeg). manifest's `image` field
                                points at /appcenter/apps/<id>.png, which serves
                                only .tar.gz + catalog and therefore 404s on the
                                device; this endpoint serves the icon out of the
                                install dir instead, so a third-party app can
                                have a card image without shipping it inside the
                                front-end bundle. `icon_url` in /list points here
                                (null when the package ships no icon).
  GET  /api/appMgr/assets?paths=a/b.rknn,c.mvn
                             -> per-path {present,size?,sha256?} under the shared
                                model root + free_bytes, so the front end can skip
                                a model that is already on the device instead of
                                re-fetching and re-uploading it (INSTALL_ASSETS_SPEC
                                §1; the 133 MB voice model cannot beat nginx's
                                proxy_read_timeout). Digests are memoized on
                                (size, mtime_ns, inode) -- see assets.py.
  GET  /api/appMgr/runtime?name=voice
                             -> is the on-demand audio runtime importable in
                                /userdata/rknnenv (INSTALL_ASSETS_SPEC §3)
  POST /api/appMgr/runtime   {name?, path?} -> offline pip-install the runtime
                                bundle previously staged via /upload; idempotent
  POST /api/appMgr/install   {path: "/userdata/.../x.tar.gz"}
  POST /api/appMgr/uninstall {id}   (stop if running, clear active, rm app dir;
                                     shared /userdata/local/models untouched)
  POST /api/appMgr/switch    {id}   (single-active: stop old active, start id)
  POST /api/appMgr/stop      {id?}  (stop id, or current active)
  POST /api/appMgr/upload    raw tar.gz bytes + X-Filename header
                             -> stage under /userdata/appstage, return device path
                             (browser-relayed cloud install: the browser fetches a
                              catalog package, sha256-verifies it, uploads here,
                              then calls /install with the returned path)
  POST /api/appMgr/putModel  raw model bytes + X-Filename + X-Target-Path
                             (+ optional X-Sha256) headers
                             -> write a SHARED model file into a whitelisted
                              directory (default /userdata/local/models*), verify
                              sha256. This is the one-gen `models[]`+`target_path`
                              path: packages that don't bundle their model let the
                              browser drop the shared asset here before /install.
                              Hardened in modelstore.py (root whitelist, no
                              traversal/symlink escape, size cap, atomic write).
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from typing import Optional
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

from . import (assets, builtin, config as appconfig, installer, kitversion,
               modelstore, mqtt as mqttcfg, paths, state, supervisor,
               voiceruntime)


# --------------------------------------------------------------------------- #
# audit + busy-gate
# --------------------------------------------------------------------------- #
def _audit(action: str, **kv) -> None:
    paths.ensure_dirs()
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "action": action, **kv}
    try:
        with open(paths.AUDIT_LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


class BusyError(Exception):
    pass


@contextmanager
def busy_gate():
    paths.ensure_dirs()
    f = open(paths.BUSY_FILE, "w")
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise BusyError("appmgr busy: another install/switch/stop in progress")
        yield
    finally:
        try:
            fcntl.flock(f, fcntl.LOCK_UN)
        finally:
            f.close()


# --------------------------------------------------------------------------- #
# operations
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# read-path caches (GET /list is polled by the App Center page; it was doing
# 9 JSON parses + ~36 icon stats + a TLS round-trip to entry.cgi on EVERY call)
#
# Every cache below is keyed on an OS-observed identity of the thing it caches,
# never on a timer alone, so a change is picked up on the next call:
#   manifest -> stat(manifest.json) = (mtime_ns, size, inode, device)
#   icon     -> stat(<app_dir>)     = same tuple (a dir's mtime bumps when a file
#                                     is created/removed inside it)
# An install/upgrade swaps the whole app dir, so BOTH keys change by inode alone
# even if the clock stood still.
#
# ★Settle window★: a stat key is only TRUSTED once the file has been quiet for
# _SETTLE_SEC. Coarse mtime granularity (some filesystems round to ms, HFS+ to
# 1s) means two rewrites inside one tick can share a key, and an in-place rewrite
# keeps the inode -- so a just-touched path is always re-read rather than served
# from a key that cannot yet distinguish versions. Steady-state polling reads
# manifests that are minutes old, so this costs nothing where it matters.
# --------------------------------------------------------------------------- #
_SETTLE_SEC = 1.0

_manifest_cache: dict = {}     # app_id -> (statkey, manifest)
_icon_cache: dict = {}         # app_id -> (statkey, icon_path or None)


def _stat_key(path: str):
    """(mtime_ns, size, inode, device) or None when the path is gone."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino, st.st_dev)


def _settled(statkey) -> bool:
    """True when the path has not been written within the last _SETTLE_SEC."""
    return statkey is not None and (time.time() - statkey[0] / 1e9) > _SETTLE_SEC


def cache_clear() -> None:
    """Drop every read-path cache. Not needed for correctness (the stat keys do
    that); exists so tests and a manual `appmgr` CLI run start from a clean slate."""
    _manifest_cache.clear()
    _icon_cache.clear()
    _builtin_invalidate()


def _read_manifest(app_id: str):
    """The app's manifest, or None. ★The returned dict is SHARED with the cache --
    treat it as read-only.★ Every consumer today only reads (config.py's
    effective_manifest/_normalized_schema copy before they touch anything), so
    nothing needs a defensive deepcopy on the polled path."""
    mp = os.path.join(paths.app_dir(app_id), "manifest.json")
    key = _stat_key(mp)
    if key is None:                       # not installed / unreadable
        _manifest_cache.pop(app_id, None)
        return None
    hit = _manifest_cache.get(app_id)
    if hit is not None and hit[0] == key and _settled(key):
        return hit[1]
    try:
        with open(mp) as f:
            man = json.load(f)
    except (OSError, ValueError):
        _manifest_cache.pop(app_id, None)
        return None
    _manifest_cache[app_id] = (key, man)
    return man


ICON_ENDPOINT = "/api/appMgr/icon"


def _icon_file_cached(app_id: str):
    """paths.icon_file() memoised on the app dir's stat key.

    The uncached call stats up to 4 candidate extensions per app per list; the
    icon itself is written once at install time and never changes in between.
    """
    if not paths.valid_app_id(app_id):
        return None
    key = _stat_key(paths.app_dir(app_id))
    if key is None:
        _icon_cache.pop(app_id, None)
        return None
    hit = _icon_cache.get(app_id)
    if hit is not None and hit[0] == key and _settled(key):
        return hit[1]
    p = paths.icon_file(app_id)
    _icon_cache[app_id] = (key, p)
    return p


def _icon_url(app_id: str, manifest: dict = None):
    """URL for a package-bundled icon, or None when the package ships none.

    The `v=<version>` suffix is a cache-buster: the response carries a long
    max-age, so without it an upgraded app would keep showing the old artwork.
    """
    if _icon_file_cached(app_id) is None:
        return None
    ver = str((manifest or {}).get("version") or "")
    q = "id=" + quote(app_id, safe="")
    if ver:
        q += "&v=" + quote(ver, safe="")
    return f"{ICON_ENDPOINT}?{q}"


def do_icon(app_id: str):
    """Return (bytes, content_type) for an app's bundled icon.

    Raises ValueError for an invalid id and FileNotFoundError when the app ships
    no icon -- the HTTP layer maps those to 400 / 404. Nothing here can read
    outside /userdata/local/apps/<id>/: the id is whitelist-validated and the
    filename is one of a fixed set built by paths.icon_file().
    """
    if not paths.valid_app_id(app_id):
        raise ValueError(f"invalid app id {app_id!r}")
    p = paths.icon_file(app_id)
    if p is None:
        raise FileNotFoundError(f"app {app_id!r} has no bundled icon")
    ext = os.path.splitext(p)[1].lower()
    ctype = paths.ICON_CONTENT_TYPES.get(ext, "application/octet-stream")
    with open(p, "rb") as f:
        data = f.read(paths.MAX_ICON_BYTES + 1)
    if len(data) > paths.MAX_ICON_BYTES:
        # Belt-and-braces: the installer caps this at unpack time, but an icon
        # dropped in by hand must not turn the endpoint into a memory hog.
        raise ValueError(f"icon too large: > {paths.MAX_ICON_BYTES}")
    return data, ctype


def do_assets(paths_param: str) -> dict:
    """GET /api/appMgr/assets?paths=a,b,c -> INSTALL_ASSETS_SPEC §1 payload.

    Comma-separated relative paths under the shared model root. The front end
    calls this BEFORE downloading an app's models from the CDN so an asset that
    is already on the device is never fetched, let alone re-uploaded (a 133 MB
    upload cannot beat nginx's proxy_read_timeout -- see assets.py).

    Raises ValueError (AssetPathError is one) -> HTTP 400 when any path is
    unsafe; an unsafe path is never quietly dropped, because "dropped" reads as
    "absent" and triggers exactly the re-upload this endpoint exists to avoid.
    """
    parts = (paths_param or "").split(",")
    if not any(p.strip() for p in parts):
        raise ValueError("missing 'paths'")
    return assets.query(parts)


def do_runtime_status(name: str = "voice") -> dict:
    """GET /api/appMgr/runtime?name=voice -> is the on-demand runtime importable?

    The front end calls this before installing an app whose manifest declares
    capabilities:["audio"], so it can ask the user about the extra ~18 MB instead
    of silently downloading it (INSTALL_ASSETS_SPEC §3.4).
    """
    return voiceruntime.status(name)


def do_runtime_install(name: str, pkg_path: str = None, signature: str = None) -> dict:
    """POST /api/appMgr/runtime {name, path, signature?} -> install the runtime bundle.

    `path` is what /api/appMgr/upload returned for voice-runtime-<ver>.tar.gz.
    `signature` is the detached base64 release signature the catalog carries for
    the runtime bundle; like an app install it is verified against the baked-in
    public key BEFORE the bundle is unpacked into the shared venv / lib tree
    (C1). Absent it, the `<pkg>.sig` sidecar is consulted, and policy
    (paths.REQUIRE_SIGNATURE, default on) refuses an unsigned bundle.
    Idempotent: an already-importable runtime returns already_present without
    running pip, so a repeat install of an audio app costs nothing.
    """
    with busy_gate():
        res = voiceruntime.install(name, pkg_path, signature)
        _audit("runtime", name=name, installed=res.get("installed"),
               already_present=res.get("already_present"))
        return res


def do_list() -> dict:
    # Reap any child that died since the last call BEFORE reporting liveness:
    # this both clears the `<defunct>` entries appmgr used to leak and makes a
    # crash visible (last_exit below) instead of it only showing up in `ps`.
    #
    # throttle_sweep: reaping + last_exit publication still happen on EVERY call
    # (they are one waitpid syscall and a no-op on an empty queue); only the
    # stale-pidfile sweep -- which walks /proc for every app -- is rate-limited.
    # That sweep is pure hygiene: is_running() re-validates each pid anyway, so a
    # stale run.pid never reaches the response. See supervisor.reap_and_sweep().
    try:
        supervisor.reap_and_sweep(throttle_sweep=True)
    except Exception:
        pass
    active = state.get_active()
    apps = []
    if os.path.isdir(paths.APPS_DIR):
        for name in sorted(os.listdir(paths.APPS_DIR)):
            d = os.path.join(paths.APPS_DIR, name)
            # `<id>.prev` (kept rollback copy) and `.<id>.stage.*` (in-flight
            # extraction) contain a dot -> never a valid app id -> not listed.
            if not os.path.isdir(d) or not paths.valid_app_id(name):
                continue
            if name == "kit":       # shared runtime, not an app
                continue
            man = _read_manifest(name)
            if man is None:
                continue
            pid = supervisor.is_running(name)
            apps.append({
                "id": name,
                "name": man.get("name", name),
                "version": man.get("version"),
                "type": man.get("type"),
                # Gallery presentation fields (image + copy). Kept optional so
                # older manifests without them still list cleanly.
                # ★i18n★: the *_zh variants are passed through RAW -- the backend
                # never picks a language, the front end does that per locale
                # (RENDER_DECLARATION_SPEC §5 P0-2). _builtin_entry() below has
                # always passed them; installed apps used to drop them silently,
                # so a third-party app could ship Chinese copy that never showed.
                "image": man.get("image"),
                "description": man.get("description"),
                "scene": man.get("scene"),
                "author": man.get("author"),
                "name_zh": man.get("name_zh"),
                "description_zh": man.get("description_zh"),
                "scene_zh": man.get("scene_zh"),
                # ★Usable★ icon URL (§5 P0-1). manifest's `image` points at
                # /appcenter/apps/<id>.png, which 404s on the device -- so the
                # front end could only render cards for the ids baked into its
                # own bundle. When the package ships icon.<ext> we hand back the
                # appmgr endpoint that actually serves it; otherwise null, and
                # the front end falls back (its bundled art, then a placeholder).
                "icon_url": _icon_url(name, man),
                # ★Display declaration★ (RENDER_DECLARATION_SPEC §4): the second
                # of the front end's three lookups (envelope.render -> THIS ->
                # shape-driven fallback). Passed through RAW -- appmgr never
                # interprets a layout / `as` primitive, it only carries the block
                # so the overlay can read it without fetching the package.
                "render": man.get("render"),
                "installed": True,
                "running": pid is not None,
                "pid": pid,
                # ★Crash visibility★: last recorded process exit, e.g.
                # {"code": -11, "signal": "SIGSEGV", "at": 1765..., "pid": 4009}.
                # null when the app has never exited under this appmgr. There is
                # deliberately NO auto-restart, so a non-null last_exit with
                # running=false is the UI's only signal that the app died.
                "last_exit": supervisor.last_exit(name),
                "active": (name == active),
            })
    # Bound the caches: an app that is gone (uninstalled, or a dir renamed out
    # from under us) must not keep its slot forever. do_list() is the only place
    # that sees the full id set, so the prune lives here.
    seen = {a["id"] for a in apps}
    for cache in (_manifest_cache, _icon_cache):
        for gone in [k for k in cache if k not in seen]:
            cache.pop(gone, None)
    apps.append(_builtin_entry(active))
    # The device's own kit version, so the store can compare it against each
    # catalog entry's `kit` requirement and refuse (or warn) BEFORE downloading a
    # package the device will reject. appmgr enforces the requirement regardless
    # at install time (kitversion.check in installer.inspect); this field only
    # moves the verdict earlier, to the one call the App Center page already
    # polls. None on a pre-1.6.4 kit, which reports no version -- the store
    # should read that as "older than 1.6.4", the same reading the gate uses.
    return {"active_app": active, "apps": apps,
            "kit_version": kitversion.installed_version()}


# ★The one network call on the list path★. builtin.is_running() is an HTTPS
# request to entry.cgi on 127.0.0.1:443 -- TLS handshake + a CGI process fork per
# call, with a 10 s timeout. Unthrottled that is one such round-trip per /list,
# and the App Center page polls /list; a slow or wedged entry.cgi therefore
# stalls the whole listing.
#
# Cached for _BUILTIN_TTL, and this cache is TIME-based because iEnable lives
# behind an HTTP endpoint -- there is no inode to watch. Correctness comes from
# explicit invalidation instead: appmgr is the only writer of iEnable it needs to
# care about (do_activate / do_stop / do_set_config all go through this process),
# and each of those calls _builtin_invalidate(). A change made behind appmgr's
# back -- somebody POSTing entry.cgi directly -- shows up within the TTL.
_BUILTIN_TTL = float(os.environ.get("APPMGR_BUILTIN_TTL", "2.0"))
# None, or (monotonic_at, running). A TUPLE rebound as a whole, not a dict
# mutated in place: the HTTP server is threaded, and rebinding one global name is
# atomic under the GIL, so a concurrent reader can never observe a half-updated
# entry (it sees either the old tuple or the new one).
_builtin_probe = None


def _builtin_invalidate() -> None:
    """Force the next list to re-read /model/inference (call after any write)."""
    global _builtin_probe
    _builtin_probe = None


def _builtin_running() -> bool:
    global _builtin_probe
    hit = _builtin_probe
    if hit is not None and (time.monotonic() - hit[0]) < _BUILTIN_TTL:
        return hit[1]
    try:
        running = builtin.is_running()
    except Exception:
        # Do NOT cache a transport failure: entry.cgi being momentarily
        # unreachable must not pin running=False for the whole TTL.
        return False
    _builtin_probe = (time.monotonic(), running)
    return running


def _builtin_entry(active_self: str) -> dict:
    """Synthesize the built-in inference list entry (DESIGN §3.1). running/active
    derive from /model/inference's iEnable, NOT a run.pid; a best-effort read
    (endpoint may be momentarily unreachable) degrades to running=False rather
    than dropping the entry."""
    man = builtin.manifest()
    running = _builtin_running()
    return {
        "id": builtin.BUILTIN_ID,
        "name": man.get("name"),
        "name_zh": man.get("name_zh"),
        "version": man.get("version"),
        "type": "builtin",
        "image": man.get("image"),
        "description": man.get("description"),
        "description_zh": man.get("description_zh"),
        "scene": man.get("scene"),
        "scene_zh": man.get("scene_zh"),
        "author": man.get("author"),
        "render": man.get("render"),
        "installed": True,
        "running": running,
        "pid": None,
        # The built-in pipeline is not an appmgr child (it lives behind
        # /model/inference), so there is no wait status to report -- always null,
        # kept so every /list entry has the same shape.
        "last_exit": None,
        # active = iEnable AND no self-hosted app is active (mutual exclusion is
        # maintained by do_activate; this AND is belt-and-braces for the UI).
        "active": running and not active_self,
    }


# Upload filename whitelist: a bare basename, package suffix, no separators.
# The browser sends the catalog's package.filename (e.g. fall-detection-0.1.0-arm64.tar.gz).
_UPLOAD_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.tar\.gz")


def do_upload(filename: str, data: bytes) -> dict:
    """Stage a browser-relayed package under /userdata/appstage/<filename>.

    This is the *only* way bytes enter the device for a cloud install: the
    browser downloads the package from the catalog, sha256-verifies it, then
    POSTs the raw bytes here. We do NOT install -- we validate + persist, and
    return the device path so the caller can then hit /install (which re-runs
    the full installer vetting: tar member zip-slip, size caps, manifest id).

    Validation here is defence-in-depth around that:
      * filename must be a bare `<name>.tar.gz` basename (no path separators,
        no traversal) -- prevents writing outside the staging dir.
      * size 1..MAX_PKG_BYTES -- reject empty and oversized before touching disk.
    """
    base = os.path.basename(filename or "")
    if not _UPLOAD_NAME_RE.fullmatch(base):
        raise ValueError("invalid filename: expected a bare <name>.tar.gz basename")
    if os.sep in (filename or "") or "/" in (filename or "") or ".." in base:
        raise ValueError("invalid filename: path separators/traversal not allowed")
    n = len(data)
    if n == 0:
        raise ValueError("empty upload")
    if n > paths.MAX_PKG_BYTES:
        raise ValueError(f"upload too large: {n} > {paths.MAX_PKG_BYTES}")

    stage = paths.ensure_appstage()
    dest = os.path.join(stage, base)
    # Confirm the resolved path really stays inside the staging dir.
    if os.path.realpath(dest) != os.path.realpath(os.path.join(stage, base)) or \
       not os.path.realpath(dest).startswith(os.path.realpath(stage) + os.sep):
        raise ValueError("invalid filename: escapes staging dir")
    # Write atomically: full write to a temp then rename into place.
    fd, tmp = tempfile.mkstemp(prefix=".upload.", dir=stage)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
        tmp = None
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)
    os.chmod(dest, 0o644)
    _audit("upload", filename=base, size=n, path=dest)
    return {"path": dest, "filename": base, "size": n}


def do_putmodel(target_path: str, filename: str, data: bytes,
                sha256: str = None) -> dict:
    """Write a browser-relayed SHARED model file to a whitelisted device dir.

    One-gen `models[]`+`target_path` parity: for apps whose package does NOT
    bundle the model (voice-transcribe), the browser downloads each catalog
    `models[]` entry, sha256-verifies it, and POSTs the raw bytes here with the
    entry's `target_path`. All the hostile-input handling (destination-root
    whitelist, traversal/symlink escape, size cap, atomic write, sha256 verify)
    lives in modelstore.write_model(); this wrapper only audits the result.
    """
    res = modelstore.write_model(target_path, filename, data, sha256)
    _audit("putmodel", filename=res["filename"], size=res["size"],
           path=res["path"], sha256=res["sha256"])
    return res


def _rollback_upgrade(app_id: str, restore_active: bool) -> Optional[str]:
    """Undo a failed upgrade: stop the broken new version, swap the retained
    `<id>.prev` back into place, and restart the old app (健壮#15). Returns the
    id if the previous version is running again, else None. Caller holds the
    busy-gate."""
    try:
        supervisor.stop(app_id)
    except Exception:
        pass
    if not installer.restore_prev(app_id):
        state.clear_active_if(app_id)
        return None
    try:
        supervisor.start(app_id)
        if restore_active:
            man = _read_manifest(app_id) or {}
            state.set_active(app_id, man.get("version"))
        return app_id
    except Exception:
        state.clear_active_if(app_id)
        return None


def do_install(pkg_path: str, signature: str = None) -> dict:
    """Install (or UPGRADE) an app as a transaction (健壮#15).

    An upgrade of the running app is the dangerous case: installer.install()
    renames the LIVE dir (holding run.pid) to `<id>.prev`, so the old process
    keeps running from .prev while the new dir has no pidfile -- is_running()
    then reads false and a later switch starts a SECOND instance (the observed
    "11 apps"). So: stop the old process first, install atomically, and if the
    app was up, restart the NEW version and gate on READY. Any failure rolls the
    whole thing back to .prev and restarts the previous version.
    """
    with busy_gate():
        info = installer.inspect(pkg_path, signature)   # verifies signature
        app_id = info["id"]
        pre_installed = os.path.isdir(paths.app_dir(app_id))
        was_running = pre_installed and supervisor.is_running(app_id) is not None
        was_active = state.get_active() == app_id

        # ★Stop the old process BEFORE the dir swap★ so it cannot linger on the
        # soon-to-be-.prev copy as an orphan double.
        if was_running:
            supervisor.stop(app_id)

        app_id, manifest = installer.install(pkg_path, signature)
        # Drop caches so the freshly swapped manifest/icon are re-read now.
        cache_clear()
        # Prune any stored config keys the NEW schema no longer accepts, so the
        # restarted app never reads a removed/type-changed/out-of-range value
        # (健壮#20). Best-effort -- a revalidation hiccup must not fail the install.
        try:
            appconfig.revalidate_user_config(manifest, app_id)
        except Exception:
            pass

        restarted = False
        if was_running:
            try:
                supervisor.start(app_id)      # blocks until the NEW version is READY
            except Exception as e:
                restored = _rollback_upgrade(app_id, was_active)
                _audit("install_failed", id=app_id,
                       version=manifest.get("version"), error=str(e),
                       restored=restored)
                cache_clear()
                raise
            restarted = True
            if was_active:
                man = _read_manifest(app_id) or {}
                state.set_active(app_id, man.get("version"))

        sig = info.get("signature") or {}
        _audit("install", id=app_id, version=manifest.get("version"),
               pkg=os.path.realpath(pkg_path), upgrade=pre_installed,
               restarted=restarted,
               signed=sig.get("signed"), sig_verified=sig.get("verified"))
        return {"id": app_id, "version": manifest.get("version"),
                "installed": True, "restarted": restarted, "signature": sig}


def do_uninstall(app_id: str) -> dict:
    """Remove an installed app. Shared by the CLI (`uninstall <id>`) and the HTTP
    POST /api/appMgr/uninstall route.

    Sequence (mirrors do_switch/do_stop discipline under the busy-gate):
      1. if the app is running -> stop it first (clean process-group teardown);
      2. if it is the single-active app -> clear active state so nothing tries to
         boot-restore a now-deleted app;
      3. installer.uninstall() deletes /userdata/local/apps/<id>/ and, if present,
         the future per-app venv /userdata/local/venvs/<id>.
    Shared models under /userdata/local/models are intentionally left untouched
    (they are cross-app assets; installer.uninstall has no path into that tree).

    Uninstalling an unknown app is a hard ValueError (not a crash); the running /
    active handling is idempotent so double-uninstall is safe.
    """
    if not paths.valid_app_id(app_id):
        raise ValueError(f"invalid app id {app_id!r}")
    if not os.path.isdir(paths.app_dir(app_id)):
        raise ValueError(f"app not installed: {app_id}")
    with busy_gate():
        stopped = False
        if supervisor.is_running(app_id) is not None:
            supervisor.stop(app_id)
            stopped = True
        was_active = (state.get_active() == app_id)
        if was_active:
            state.clear_active_if(app_id)
        installer.uninstall(app_id)
        _audit("uninstall", id=app_id, stopped=stopped, was_active=was_active)
        return {"id": app_id, "uninstalled": True,
                "stopped": stopped, "was_active": was_active}


def _restore_active(prev: str, failed_id: str) -> Optional[str]:
    """Transactionally bring the PREVIOUS active app back after a target failed
    to start (健壮#19). Ensures the failed target is fully stopped, then restarts
    `prev` and re-points `active` at it. If prev is absent or itself won't start,
    active is cleared (None) -- never left pointing at an app that isn't up.
    Caller holds the busy-gate."""
    try:
        supervisor.stop(failed_id)          # guarantee the corpse is gone
    except Exception:
        pass
    if not prev or prev == failed_id:
        state.set_active(None, None)
        return None
    try:
        supervisor.start(prev)
        man = _read_manifest(prev) or {}
        state.set_active(prev, man.get("version"))
        return prev
    except Exception:
        state.set_active(None, None)
        return None


def do_switch(app_id: str) -> dict:
    if not paths.valid_app_id(app_id):
        raise ValueError(f"invalid app id {app_id!r}")
    if not os.path.isdir(paths.app_dir(app_id)):
        raise ValueError(f"app not installed: {app_id}")
    with busy_gate():
        prev = state.get_active()
        # single-active: stop whoever is currently active (and the target, to
        # guarantee a clean (re)start) before starting the target.
        if prev and prev != app_id:
            supervisor.stop(prev)
        supervisor.stop(app_id)
        try:
            pid = supervisor.start(app_id)     # blocks until READY (or fails)
        except Exception as e:
            # rollback: restart the previous active app rather than leaving the
            # device with nothing running because the target wouldn't come up.
            restored = _restore_active(prev, app_id)
            _audit("switch_failed", id=app_id, error=str(e), restored=restored)
            raise
        man = _read_manifest(app_id) or {}
        state.set_active(app_id, man.get("version"))
        _audit("switch", id=app_id, pid=pid, prev=prev)
        return {"active_app": app_id, "pid": pid, "prev": prev}


def _stop_active_self() -> str:
    """Stop the current self-hosted active app (if any) and clear active state.
    Returns the id that was stopped, or None. Caller must hold the busy-gate."""
    prev = state.get_active()
    if prev:
        supervisor.stop(prev)
        state.clear_active_if(prev)
    return prev


def do_activate(app_id: str) -> dict:
    """Single-active semantics across self-hosted apps AND the built-in detector
    (DESIGN §2). Exactly one inference app is active afterwards; the built-in and
    self-hosted worlds are kept mutually exclusive here (state.json still stores
    only the self-hosted active; the built-in's active = /model/inference iEnable).

      id == "builtin" -> stop the active self-hosted app + enable built-in.
      id == "none"    -> stop the active self-hosted app + disable built-in.
      id == <app>     -> disable built-in + stop others + start <app>.

    One busy-gate for the whole op (do_switch/do_stop are NOT reused -- the gate
    is a non-reentrant flock, so nesting them would self-deadlock into BusyError)."""
    with busy_gate():
        if app_id in (None, "", "none"):
            prev = _stop_active_self()
            binf = builtin.stop()
            _builtin_invalidate()
            _audit("activate", id="none", prev=prev)
            return {"active": None, "prev_self": prev, "builtin": False,
                    "inference": binf}

        if app_id == builtin.BUILTIN_ID:
            prev = _stop_active_self()
            binf = builtin.start()          # iEnable=1, keeps persisted model/fps
            _builtin_invalidate()
            _audit("activate", id="builtin", prev=prev)
            return {"active": "builtin", "prev_self": prev, "builtin": True,
                    "inference": binf}

        # self-hosted target
        if not paths.valid_app_id(app_id):
            raise ValueError(f"invalid app id {app_id!r}")
        if not os.path.isdir(paths.app_dir(app_id)):
            raise ValueError(f"app not installed: {app_id}")
        prev = state.get_active()
        # Remember whether the built-in detector was the thing running BEFORE we
        # tear it down, so a failed activation can restore it (not just a
        # self-hosted prev).
        prev_builtin = _builtin_running() if not prev else False
        builtin.stop()                       # turn the built-in detector off first
        _builtin_invalidate()
        if prev and prev != app_id:
            supervisor.stop(prev)
        supervisor.stop(app_id)              # clean (re)start
        try:
            pid = supervisor.start(app_id)   # blocks until READY (or fails)
        except Exception as e:
            restored = _restore_active(prev, app_id)
            if restored is None and prev_builtin:
                try:
                    builtin.start()          # re-enable the built-in we stopped
                    _builtin_invalidate()
                    restored = "builtin"
                except Exception:
                    pass
            _audit("activate_failed", id=app_id, error=str(e), restored=restored)
            raise
        man = _read_manifest(app_id) or {}
        state.set_active(app_id, man.get("version"))
        _audit("activate", id=app_id, pid=pid, prev=prev)
        return {"active": app_id, "pid": pid, "prev_self": prev, "builtin": False}


def do_stop(app_id: str = None) -> dict:
    with busy_gate():
        target = app_id or state.get_active()
        if target == builtin.BUILTIN_ID:
            res = builtin.stop()
            _builtin_invalidate()
            _audit("stop", id="builtin", result=res)
            return {"stopped": "builtin", "detail": res}
        if not target:
            return {"stopped": None, "note": "no active app"}
        res = supervisor.stop(target)
        state.clear_active_if(target)
        _audit("stop", id=target, result=res)
        return {"stopped": target, "detail": res}


def do_get_config(app_id: str) -> dict:
    if app_id == builtin.BUILTIN_ID:
        return builtin.get_config()          # driver-backed, app-isomorphic shape
    if not paths.valid_app_id(app_id):
        raise ValueError(f"invalid app id {app_id!r}")
    if not os.path.isdir(paths.app_dir(app_id)):
        raise ValueError(f"app not installed: {app_id}")
    man = _read_manifest(app_id) or {}
    return appconfig.get_config(man, app_id)


def _apply_mode(manifest: dict, keys) -> str:
    """Classify a config change as "live" or "restart" from the schema's per-item
    `apply` field (DESIGN §1.1/§3.2). A change is "live" only if EVERY changed
    key is apply:"live"; a single restart-class key (or one lacking the field --
    default "restart", conservative) forces the whole change to "restart"."""
    specs = appconfig.schema_specs(manifest)
    for k in keys:
        spec = specs.get(k) or {}
        if str(spec.get("apply", "restart")).lower() != "live":
            return "restart"
    return "live"


def do_set_config(app_id: str, incoming: dict) -> dict:
    if app_id == builtin.BUILTIN_ID:
        with busy_gate():
            res = builtin.set_config(incoming)   # driver dispatches per-item bind
        # A builtin config write can flip iEnable (and always restarts the
        # pipeline), so the cached liveness must not survive it.
        _builtin_invalidate()
        _audit("config", id="builtin", keys=sorted((res.get("config") or {}).keys()),
               applied=res.get("applied"), restarted=res.get("restarted"))
        return res
    if not paths.valid_app_id(app_id):
        raise ValueError(f"invalid app id {app_id!r}")
    if not os.path.isdir(paths.app_dir(app_id)):
        raise ValueError(f"app not installed: {app_id}")
    man = _read_manifest(app_id) or {}
    clean, errors = appconfig.validate_config(man, incoming)
    if errors:
        raise ValueError("; ".join(errors))
    mode = _apply_mode(man, clean.keys())
    with busy_gate():
        # Persist first (survives even if a restart hiccups), then apply.
        appconfig.write_user_config(app_id, clean)
        restarted = False
        reloaded = False
        running = supervisor.is_running(app_id) is not None
        if mode == "live":
            # LIVE change: signal the running app to re-read config.json in
            # place (SIGHUP). If it isn't running, config.json is already
            # written and will be picked up on the next start -- nothing to do.
            if running:
                reloaded = supervisor.reload(app_id)
        else:
            # RESTART change: bounce the app so it reloads structural params
            # (model / input_size / backend). Only the active, running app is
            # bounced -- unchanged from prior behaviour.
            if state.get_active() == app_id and running:
                supervisor.stop(app_id)
                supervisor.start(app_id)   # picks up config.json via kit.config
                restarted = True
        _audit("config", id=app_id, keys=sorted(clean.keys()),
               applied=mode, restarted=restarted, reloaded=reloaded)
        return {"id": app_id, "saved": True, "applied": mode,
                "restarted": restarted, "reloaded": reloaded, "config": clean}


def _read_first_line(path: str) -> str:
    try:
        with open(path) as f:
            return f.readline().strip()
    except OSError:
        return ""


def _npu_load() -> dict:
    """Parse /proc/rknpu/load. Format varies across RKNPU driver versions, e.g.
        "NPU load:  Core0: 43%,"                 (single core)
        "NPU load:  Core0: 12%, Core1:  0%, ..." (multi core)
    Return {raw, cores:[..%], avg} -- best-effort, never raises."""
    raw = _read_first_line("/proc/rknpu/load")
    cores = []
    if raw:
        import re
        cores = [int(x) for x in re.findall(r"(\d+)\s*%", raw)]
    avg = round(sum(cores) / len(cores), 1) if cores else None
    return {"raw": raw, "cores": cores, "avg": avg}


def _mem_info() -> dict:
    """Used/total MiB from /proc/meminfo (used = total - available)."""
    total_kb = avail_kb = None
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    total_kb = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    avail_kb = int(line.split()[1])
                if total_kb is not None and avail_kb is not None:
                    break
    except (OSError, ValueError, IndexError):
        pass
    if total_kb is None:
        return {"used_mb": None, "total_mb": None, "used_pct": None}
    used_kb = total_kb - (avail_kb or 0)
    return {
        "used_mb": round(used_kb / 1024, 1),
        "total_mb": round(total_kb / 1024, 1),
        "used_pct": round(used_kb / total_kb * 100, 1) if total_kb else None,
    }


def _temp_c() -> float:
    """Highest thermal_zone*/temp reading in degrees C (values are milli-C)."""
    import glob
    best = None
    for p in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
        v = _read_first_line(p)
        try:
            c = int(v) / 1000.0
        except (ValueError, TypeError):
            continue
        if best is None or c > best:
            best = c
    return round(best, 1) if best is not None else None


def do_metrics() -> dict:
    """Lightweight device telemetry for the /appcenter debug panel. Reads a few
    procfs/sysfs files on demand (no daemon, no polling loop)."""
    try:
        supervisor.reap_and_sweep(throttle_sweep=True)   # polled endpoint
    except Exception:
        pass
    up = _read_first_line("/proc/uptime").split()
    try:
        uptime_s = int(float(up[0])) if up else None
    except (ValueError, IndexError):
        uptime_s = None
    active = state.get_active()
    return {
        "npu_load": _npu_load(),
        "mem": _mem_info(),
        "temp_c": _temp_c(),
        "active_app": active,
        # Whether the ACTIVE app is actually alive, plus how it died last time.
        # A panel showing active_app with active_running=false and a signal in
        # active_last_exit is the crash indicator; appmgr does not auto-restart.
        "active_running": bool(active and supervisor.is_running(active)),
        "active_last_exit": supervisor.last_exit(active) if active else None,
        "uptime_s": uptime_s,
        "ts": time.time(),
    }


def do_get_mqtt() -> dict:
    """Global MQTT/HA broker config (password redacted -> password_set flag)."""
    return mqttcfg.public_view()


def do_set_mqtt(incoming: dict) -> dict:
    """Persist the global MQTT config, then restart the active app so it picks
    up the new broker env (single-active model: only one app runs at a time)."""
    clean, errors = mqttcfg.validate(incoming)
    if errors:
        raise ValueError("; ".join(errors))
    with busy_gate():
        mqttcfg.save(clean)
        restarted = None
        active = state.get_active()
        if active and supervisor.is_running(active):
            supervisor.stop(active)
            supervisor.start(active)   # re-launches with RECAMERA_MQTT_* env
            restarted = active
        _audit("mqtt", enabled=clean.get("enabled"), host=clean.get("host"),
               restarted=restarted)
        view = mqttcfg.public_view()
        view["restarted"] = restarted
        return view


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #
class _Handler(BaseHTTPRequestHandler):
    server_version = "appmgr/0.1"

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, code: int, data: bytes, content_type: str,
                    cache: str = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if cache:
            self.send_header("Cache-Control", cache)
        # Served same-origin to <img>; nothing here is a document, and the
        # extension whitelist already excludes SVG -- pin the type anyway.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _body_json(self) -> dict:
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            return {}

    def log_message(self, *a):     # silence default stderr logging
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        if path == "/api/appMgr/list":
            return self._send(200, do_list())
        if path == "/api/appMgr/config":
            app_id = (parse_qs(parsed.query).get("id") or [None])[0]
            if not app_id:
                return self._send(400, {"error": "missing 'id'"})
            try:
                return self._send(200, do_get_config(app_id))
            except ValueError as e:
                return self._send(400, {"error": str(e)})
        if path == ICON_ENDPOINT:
            app_id = (parse_qs(parsed.query).get("id") or [None])[0]
            if not app_id:
                return self._send(400, {"error": "missing 'id'"})
            try:
                data, ctype = do_icon(app_id)
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            except FileNotFoundError as e:
                return self._send(404, {"error": str(e)})
            except OSError as e:
                return self._send(500, {"error": repr(e)})
            # Immutable per (id, version): the URL carries `v=<version>`, so a
            # long max-age is safe and an upgrade busts it by changing the URL.
            return self._send_bytes(200, data, ctype,
                                    cache="public, max-age=86400")
        if path == "/api/appMgr/assets":
            q = (parse_qs(parsed.query).get("paths") or [""])[0]
            try:
                return self._send(200, do_assets(q))
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            except OSError as e:
                return self._send(500, {"error": repr(e)})
        if path == "/api/appMgr/runtime":
            name = (parse_qs(parsed.query).get("name") or ["voice"])[0]
            try:
                return self._send(200, do_runtime_status(name))
            except ValueError as e:
                return self._send(400, {"error": str(e)})
        if path == "/api/appMgr/mqtt":
            return self._send(200, do_get_mqtt())
        if path == "/api/appMgr/metrics":
            return self._send(200, do_metrics())
        self._send(404, {"error": "not found"})

    def _read_raw_body(self, cap: int = None) -> bytes:
        """Read exactly Content-Length bytes, refusing oversized uploads before
        allocating. Used by /upload (raw package bytes) and /putModel (raw model
        bytes); `cap` defaults to the package cap."""
        cap = paths.MAX_PKG_BYTES if cap is None else cap
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n <= 0:
            raise ValueError("missing/empty body")
        if n > cap:
            raise ValueError(f"upload too large: {n} > {cap}")
        buf = bytearray()
        remaining = n
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 1 << 20))
            if not chunk:
                break
            buf.extend(chunk)
            remaining -= len(chunk)
        return bytes(buf)

    def do_POST(self):
        path = self.path.rstrip("/")
        # /upload carries raw package bytes -> read the body BEFORE _body_json()
        # (which would consume rfile as JSON). Filename rides an X-Filename header.
        if path == "/api/appMgr/upload":
            try:
                filename = self.headers.get("X-Filename", "")
                data = self._read_raw_body()
                return self._send(200, do_upload(filename, data))
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            except Exception as e:
                return self._send(500, {"error": repr(e)})
        # /putModel also carries raw bytes -> read BEFORE _body_json(). Target
        # dir + filename + optional expected sha256 ride in headers.
        if path == "/api/appMgr/putModel":
            try:
                filename = self.headers.get("X-Filename", "")
                target = self.headers.get("X-Target-Path", "")
                sha256 = self.headers.get("X-Sha256", "") or None
                data = self._read_raw_body(modelstore.MAX_MODEL_BYTES)
                return self._send(200, do_putmodel(target, filename, data, sha256))
            except (ValueError, modelstore.ModelStoreError) as e:
                return self._send(400, {"error": str(e)})
            except Exception as e:
                return self._send(500, {"error": repr(e)})
        body = self._body_json()
        try:
            if path == "/api/appMgr/install":
                p = body.get("path")
                if not p:
                    return self._send(400, {"error": "missing 'path'"})
                return self._send(200, do_install(p, body.get("signature")))
            if path == "/api/appMgr/uninstall":
                i = body.get("id")
                if not i:
                    return self._send(400, {"error": "missing 'id'"})
                return self._send(200, do_uninstall(i))
            if path == "/api/appMgr/switch":
                i = body.get("id")
                if not i:
                    return self._send(400, {"error": "missing 'id'"})
                return self._send(200, do_switch(i))
            if path == "/api/appMgr/activate":
                # {id} may be a self-hosted app id, "builtin", or "none".
                i = body.get("id")
                if not i:
                    return self._send(400, {"error": "missing 'id'"})
                return self._send(200, do_activate(i))
            if path == "/api/appMgr/stop":
                return self._send(200, do_stop(body.get("id")))
            if path == "/api/appMgr/config":
                i = body.get("id")
                if not i:
                    return self._send(400, {"error": "missing 'id'"})
                if "config" not in body or not isinstance(body["config"], dict):
                    return self._send(400, {"error": "missing/invalid 'config'"})
                return self._send(200, do_set_config(i, body["config"]))
            if path == "/api/appMgr/runtime":
                # {name?: "voice", path?: "/userdata/appstage/voice-runtime-*.tar.gz",
                #  signature?: "<base64 detached release signature>"}
                return self._send(200, do_runtime_install(
                    body.get("name") or "voice", body.get("path"),
                    body.get("signature")))
            if path == "/api/appMgr/mqtt":
                cfg = body.get("mqtt", body)
                if not isinstance(cfg, dict):
                    return self._send(400, {"error": "missing/invalid 'mqtt'"})
                return self._send(200, do_set_mqtt(cfg))
            self._send(404, {"error": "not found"})
        except BusyError as e:
            self._send(409, {"error": str(e), "code": -2})
        except (installer.InstallError, supervisor.SupervisorError, ValueError) as e:
            self._send(400, {"error": str(e)})
        except Exception as e:
            self._send(500, {"error": repr(e)})


_single_instance_fh = None


def _acquire_single_instance() -> bool:
    global _single_instance_fh
    paths.ensure_dirs()
    _single_instance_fh = open(paths.LOCK_FILE, "w")
    try:
        fcntl.flock(_single_instance_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _single_instance_fh.write(str(os.getpid()))
        _single_instance_fh.flush()
        return True
    except OSError:
        return False


def _boot_restore() -> None:
    """On daemon boot, re-launch the last active app if it isn't already running.

    Fixes the reboot gap: serve() only bound HTTP and never resumed the app the
    user had running, so every reboot left no AI app running until a manual
    start. Idempotent (a running app is left untouched, so restarting serve()
    never double-starts or bounces it) and best-effort (any failure is logged,
    never propagated -- boot-restore must not take the HTTP daemon down)."""
    try:
        active = state.get_active()
        if not active:
            return
        if supervisor.is_running(active) is not None:
            print(f"[appmgr] boot-restore: {active} already running, skip",
                  flush=True)
            return
        print(f"[appmgr] boot-restore: starting active app {active}", flush=True)
        pid = supervisor.start(active)
        _audit("boot_restore", id=active, pid=pid)
        print(f"[appmgr] boot-restore: started {active} pid={pid}", flush=True)
    except Exception as e:
        _audit("boot_restore_failed", error=repr(e))
        print(f"[appmgr] boot-restore: failed: {e!r}", flush=True)


def serve(host: str = None, port: int = None) -> None:
    host = host or paths.HTTP_HOST
    port = port or paths.HTTP_PORT
    if not _acquire_single_instance():
        raise SystemExit("appmgr already running (single-instance lock held)")
    # Reap app processes as they die. Without this the daemon leaks a
    # `[python] <defunct>` entry for every crashed app (it is their parent and
    # never called waitpid). The handler itself only does waitpid(WNOHANG) and
    # queues the status; the visible bookkeeping (last_exit.json, run.pid
    # cleanup, log line) happens in normal context from do_list/do_metrics/stop.
    if not supervisor.install_sigchld():
        print("[appmgr] warning: could not install SIGCHLD handler", flush=True)
    # Recover any install a crash interrupted mid dir-swap BEFORE we look at what
    # is installed / boot-restore the active app (健壮#16): a `<id>.prev` with no
    # live `<id>` dir means the app silently vanished and must be swapped back.
    try:
        restored = installer.reconcile_interrupted_installs()
        if restored:
            print(f"[appmgr] reconciled interrupted installs: {restored}",
                  flush=True)
    except Exception as e:
        print(f"[appmgr] install reconciliation skipped: {e!r}", flush=True)
    httpd = ThreadingHTTPServer((host, port), _Handler)
    print(f"[appmgr] listening on http://{host}:{port}", flush=True)
    # Boot-restore: HTTP is up; resume the last active app if it isn't running.
    _boot_restore()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()

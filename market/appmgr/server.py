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

import errno
import fcntl
import ipaddress
import json
import os
import queue
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from typing import Optional
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

from . import (assets, builtin, config as appconfig,
               coordinator as appcoordinator, gateway as resultgateway,
               inference_auth, installer, manifest as appmanifest, modelstore,
               mqtt as mqttcfg, operations as appoperations, paths,
               result_hub as canonical_results,
               resources as appresources, state, supervisor,
               signing as appsigning, trust as apptrust,
               uploads as appuploads, visualization as appvisualization,
               voiceruntime)


_coordinator_instance = None
_coordinator_layout = None
_result_gateway_instance = None
_result_hub_instance = None
_visualization_bridge_instance = None
_operation_manager_instance = None
_operation_manager_layout = None
_upload_finalize_lock = threading.Lock()
_reconcile_stop = None
_reconcile_thread = None


def add_result_observer(callback):
    """Register an asynchronous observer on authenticated raw v2 envelopes.

    This is the platform seam reserved for a later OSD bridge.  ResultHub owns a
    bounded worker queue per observer, so a slow/failing bridge cannot block app
    or built-in inference ingress.  No native SDK is imported here.
    """
    hub = _result_hub_instance
    if hub is None:
        raise RuntimeError("result hub is not running")
    return hub.add_observer(callback)


def remove_result_observer(callback) -> bool:
    hub = _result_hub_instance
    return bool(hub is not None and hub.remove_observer(callback))


def _supports_detection_stream_osd(manifest: dict) -> bool:
    """Whether one installed v2 manifest explicitly opts into box burn-in."""
    if not isinstance(manifest, dict) or manifest.get("manifest_version") != 2:
        return False
    render = manifest.get("render")
    if not isinstance(render, dict) or render.get("schema_version") != 1:
        return False
    stream_osd = render.get("stream_osd")
    supported = (stream_osd.get("supported")
                 if isinstance(stream_osd, dict) else None)
    return isinstance(supported, list) and "boxes" in supported


def do_get_visualization() -> dict:
    return appvisualization.public_view(_visualization_bridge_instance)


def do_set_visualization(incoming: dict) -> dict:
    """Persist the device-global stream burn-in policy.

    Browser overlays remain a per-client choice.  Stream OSD changes every
    encoded consumer (preview, RTSP, recordings and snapshots), so changes are
    serialized with the same mutation gate as lifecycle/config operations and
    only accept installed manifests that explicitly advertise box support.
    """
    if not isinstance(incoming, dict):
        raise appvisualization.VisualizationError(
            "visualization config must be an object")
    with busy_gate(wait_timeout=paths.V1_OPERATION_BUSY_TIMEOUT_SEC):
        clean = appvisualization.validate(
            incoming, current=appvisualization.load())
        for app_id in clean["osd"]["sources"]:
            _require_installed(app_id)
            manifest = _read_manifest(app_id)
            if not _supports_detection_stream_osd(manifest):
                raise appvisualization.VisualizationError(
                    "application does not support detection stream OSD: %s" %
                    app_id)
        saved = appvisualization.save(clean)
        if _visualization_bridge_instance is not None:
            _visualization_bridge_instance.reload(saved)
        _audit(
            "v1_visualization",
            osd_enabled=saved["osd"]["enabled"],
            osd_sources=saved["osd"]["sources"],
        )
        _operation_manager().events.publish(
            "visualization", action="updated", osd=saved["osd"])
        return appvisualization.public_view(_visualization_bridge_instance)


class RequestOriginError(ValueError):
    """An unsafe browser request did not prove a same-origin boundary."""


def _request_authority(value: str, scheme: str):
    """Return a normalized (host, port), rejecting ambiguous Host syntax."""
    if not isinstance(value, str) or not value or any(
            character in value for character in "/?#@"):
        raise RequestOriginError("missing or invalid Host header")
    try:
        parsed = urlparse("%s://%s" % (scheme, value))
        port = parsed.port
    except ValueError as exc:
        raise RequestOriginError("missing or invalid Host header") from exc
    if (not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.path not in ("", "/")
            or parsed.params or parsed.query or parsed.fragment):
        raise RequestOriginError("missing or invalid Host header")
    host = parsed.hostname.rstrip(".").lower()
    return host, port if port is not None else (443 if scheme == "https" else 80)


def _origin_authority(value: str):
    if not isinstance(value, str) or not value or value == "null":
        raise RequestOriginError("unsafe request Origin is required")
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError as exc:
        raise RequestOriginError("invalid request Origin") from exc
    scheme = parsed.scheme.lower()
    if (scheme not in ("http", "https") or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in ("", "/") or parsed.params
            or parsed.query or parsed.fragment):
        raise RequestOriginError("invalid request Origin")
    host = parsed.hostname.rstrip(".").lower()
    return scheme, host, port if port is not None else (
        443 if scheme == "https" else 80)


def _is_loopback_authority(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _coordinator() -> appcoordinator.AppCoordinator:
    """Return a coordinator bound to the currently selected test/device layout."""
    global _coordinator_instance, _coordinator_layout
    layout = (paths.STATE_FILE, paths.resource_state_file(),
              paths.RESULT_GATEWAY_SOCK, paths.INFERENCE_SERVICE_SOCK,
              paths.inference_authorization_dir())
    if _coordinator_instance is None or _coordinator_layout != layout:
        _coordinator_instance = appcoordinator.AppCoordinator(
            resource_manager=appresources.ResourceManager(layout[1]),
            result_gateway_sock=layout[2], inference_service_sock=layout[3],
            inference_registry=inference_auth.InferenceAuthorizationRegistry(
                layout[4]))
        _coordinator_layout = layout
    return _coordinator_instance


def _operation_manager() -> appoperations.OperationManager:
    """Return the operation journal/event bus for the selected device layout."""
    global _operation_manager_instance, _operation_manager_layout
    layout = paths.operation_state_file()
    if (_operation_manager_instance is None
            or _operation_manager_layout != layout):
        if _operation_manager_instance is not None:
            _operation_manager_instance.close()
        _operation_manager_instance = appoperations.OperationManager(layout)
        _operation_manager_layout = layout
    return _operation_manager_instance


def _managed_launch(app_id: str, operation: str, manifest: dict):
    """Build the launch callback used after coordinator resource reservation."""
    plan = appresources.plan_manifest(
        manifest, appconfig.effective_values(manifest, app_id))

    def launch(**identity):
        if plan.npu_mode == "legacy-direct":
            proof = _prepare_external_start(operation, app_id)
            return _start_external_authorized(app_id, proof, **identity)
        return supervisor.start(app_id, **identity)

    return launch


# --------------------------------------------------------------------------- #
# audit + busy-gate
# --------------------------------------------------------------------------- #
def _audit(action: str, **kv) -> None:
    paths.ensure_dirs()
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "action": action, **kv}
    try:
        with open(paths.audit_log(), "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


class BusyError(Exception):
    pass


@contextmanager
def busy_gate(*, wait_timeout: float = 0.0,
              retry_interval: Optional[float] = None):
    """Acquire the process-wide mutation gate.

    The default remains the legacy non-blocking contract.  App Center v1's
    already-queued worker may opt into a bounded wait so a brief background
    reconciler critical section does not randomly fail the operation.  Waiting
    happens entirely before ``yield``: no mutation has begun and no operation
    with side effects is ever replayed.
    """
    paths.ensure_dirs()
    f = open(paths.busy_file(), "w")
    timeout = max(0.0, float(wait_timeout))
    retry = max(0.001, float(
        paths.V1_OPERATION_BUSY_RETRY_SEC
        if retry_interval is None else retry_interval))
    deadline = time.monotonic() + timeout
    acquired = False
    try:
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as exc:
                # Retry lock contention only.  Preserve the legacy BusyError
                # surface for an unexpected flock failure, but do not mask it
                # behind a five-second retry loop.
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise BusyError(
                        "appmgr busy: another install/switch/stop in progress"
                    ) from exc
                remaining = deadline - time.monotonic()
                if timeout <= 0.0 or remaining <= 0.0:
                    raise BusyError(
                        "appmgr busy: another install/switch/stop in progress")
                time.sleep(min(retry, remaining))
        yield
    finally:
        try:
            if acquired:
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


def _refresh_result_manifest(app_id: str, manifest: dict = None,
                             identity: dict = None) -> bool:
    """Refresh Result Hub's generation-bound render cache off the data path."""
    hub = _result_hub_instance
    if hub is None or not hasattr(hub, "refresh_app_manifest"):
        return False
    resolved = dict(identity or state.get_app(app_id) or {})
    resolved.setdefault("app_id", app_id)
    if not resolved.get("instance_id") or resolved.get("generation") is None:
        hub.invalidate_app_manifest(app_id)
        return False
    trusted_manifest = manifest if isinstance(manifest, dict) else _read_manifest(app_id)
    return hub.refresh_app_manifest(resolved, trusted_manifest)


def _resolve_result_identity(coord, hub, peer_pid, claimed_app,
                             instance_id, generation):
    """Authenticate one gateway publisher, then prime trusted manifest state.

    Manifest I/O occurs once during the publisher hello/control path.  Every
    subsequent result uses Result Hub's exact instance/generation cache only.
    """
    identity = coord.resolve_identity(
        peer_pid, claimed_app, instance_id, generation)
    if identity is not None:
        manifest = _read_manifest(claimed_app)
        if not hub.refresh_app_manifest(identity, manifest):
            hub.invalidate_app_manifest(claimed_app)
    return identity


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
    # Reap children owned by this daemon before reporting liveness so crashes
    # are visible immediately.  GET is otherwise lifecycle read-only: it never
    # sweeps arbitrary persisted records and never calls coordinator.observe()
    # (which revokes authorization, releases allocations and rewrites state).
    # The busy-gated reconciler owns those transitions within one second.  This
    # separation is also cross-process safe: a CLI start can be between the
    # PGID/boot writes and its run.pid commit marker while this daemon serves GET.
    try:
        supervisor.reap_children()
        supervisor.drain_exits()
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
            last_exit = supervisor.last_exit(name)
            observed = state.get_app(name)
            if observed is not None:
                # Project current /proc liveness into this response only.  Do
                # not persist it: a reader racing startup must preserve the
                # exact generation/identity/allocation transaction for the
                # worker and reconciler.  A starting record without a bound PID
                # is intentionally left as starting, not shown as a crash.
                observed = dict(observed)
                phase = observed.get("observed_state")
                bound_pid = observed.get("pid")
                live_phases = ("starting", "ready", "running", "degraded")
                if pid is None and bound_pid is not None and phase in live_phases:
                    observed["observed_state"] = (
                        "failed" if observed.get("desired_state") ==
                        state.DESIRED_RUNNING else "stopped")
                    observed["reason"] = (
                        "process exited: %s" % last_exit
                        if isinstance(last_exit, dict) else "process exited")
                elif pid is not None and phase not in live_phases:
                    observed["observed_state"] = "running"
                    observed["reason"] = None
            desired_state = ((observed or {}).get("desired_state")
                             or ("running" if name == active else "stopped"))
            observed_state = ((observed or {}).get("observed_state")
                              or ("running" if pid is not None else "stopped"))
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
                "last_exit": last_exit,
                "active": (name == active),
                # Multi-app lifecycle is additive; legacy clients continue to
                # use running/active while the Web deployment UI can explain
                # queued/dependency/failed states without guessing from pid.
                "desired_state": desired_state,
                "observed_state": observed_state,
                "state_reason": (observed or {}).get("reason"),
                "instance_id": (observed or {}).get("instance_id"),
                "generation": (observed or {}).get("generation", 0),
                "allocations": list((observed or {}).get("allocations") or []),
                "endpoints": dict((observed or {}).get("endpoints") or {}),
            })
    # Bound the caches: an app that is gone (uninstalled, or a dir renamed out
    # from under us) must not keep its slot forever. do_list() is the only place
    # that sees the full id set, so the prune lives here.
    seen = {a["id"] for a in apps}
    for cache in (_manifest_cache, _icon_cache):
        for gone in [k for k in cache if k not in seen]:
            cache.pop(gone, None)
    apps.append(_builtin_entry(active))
    snapshot = state.load()
    result = {
        "active_app": active,
        "running_apps": [a["id"] for a in apps
                         if a.get("running") and a.get("id") != builtin.BUILTIN_ID],
        "state_revision": snapshot.get("revision", 0),
        "apps": apps,
    }
    try:
        result["resources"] = _coordinator().resources.snapshot()
    except Exception:
        result["resources"] = {"allocations": []}
    if _result_gateway_instance is not None:
        result["result_gateway"] = _result_gateway_instance.status()
    if _result_hub_instance is not None:
        result["result_hub"] = _result_hub_instance.status()
    return result


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


def _npu_broker_present() -> bool:
    return os.path.exists(paths.INFERENCE_CONTROL_SOCK)


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
        "desired_state": "running" if running else "stopped",
        "observed_state": "running" if running else "stopped",
        "state_reason": None,
        "instance_id": None,
        "generation": 0,
        "allocations": [],
        "endpoints": {},
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


def _rollback_upgrade(app_id: str, restore_active: bool,
                      restore_managed: bool = False) -> Optional[str]:
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
        man = _read_manifest(app_id) or {}
        if restore_managed:
            managed = _coordinator().start(
                app_id, manifest=man, operation="upgrade_rollback",
                launch=_managed_launch(app_id, "upgrade_rollback", man))
            if managed.get("pid") is None:
                raise supervisor.SupervisorError(
                    "rollback app is not runnable: %s" % managed.get("reason"))
        else:
            proof = _prepare_external_start("upgrade_rollback", app_id)
            _coordinated_legacy_start(app_id, "upgrade_rollback", proof)
        if restore_active:
            state.set_active(app_id, man.get("version"))
        return app_id
    except Exception:
        state.clear_active_if(app_id)
        return None


def do_install(pkg_path: str, signature: str = None, *,
               allow_unsigned: bool = False,
               expected_preflight: dict = None,
               _busy_timeout: float = 0.0) -> dict:
    """Install (or UPGRADE) an app as a transaction (健壮#15).

    An upgrade of the running app is the dangerous case: installer.install()
    renames the LIVE dir (holding run.pid) to `<id>.prev`, so the old process
    keeps running from .prev while the new dir has no pidfile -- is_running()
    then reads false and a later switch starts a SECOND instance (the observed
    "11 apps"). So: stop the old process first, install atomically, and if the
    app was up, restart the NEW version and gate on READY. Any failure rolls the
    whole thing back to .prev and restarts the previous version.
    """
    with busy_gate(wait_timeout=_busy_timeout):
        info = installer.inspect(
            pkg_path, signature, allow_unsigned=allow_unsigned)
        if expected_preflight is not None:
            _assert_v1_preflight_binding(expected_preflight, info)
        app_id = info["id"]
        pre_installed = os.path.isdir(paths.app_dir(app_id))
        was_running = pre_installed and supervisor.is_running(app_id) is not None
        was_active = state.get_active() == app_id
        prior_state = state.get_app(app_id)
        was_managed = bool(prior_state and
                           prior_state.get("launch_mode") == "managed")

        # ★Stop the old process BEFORE the dir swap★ so it cannot linger on the
        # soon-to-be-.prev copy as an orphan double.
        if was_running:
            if was_managed:
                _coordinator().stop(app_id, desired=state.DESIRED_RUNNING)
            else:
                _stop_external(app_id)

        app_id, manifest = installer.install(
            pkg_path, signature, allow_unsigned=allow_unsigned)
        # Drop caches so the freshly swapped manifest/icon are re-read now.
        cache_clear()
        if (_result_hub_instance is not None
                and hasattr(_result_hub_instance, "invalidate_app_manifest")):
            _result_hub_instance.invalidate_app_manifest(app_id)
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
                if was_managed:
                    managed = _coordinator().start(
                        app_id, manifest=manifest, operation="upgrade_restart",
                        launch=_managed_launch(
                            app_id, "upgrade_restart", manifest))
                    if managed.get("pid") is None:
                        raise supervisor.SupervisorError(
                            "upgraded app is not runnable: %s" %
                            managed.get("reason"))
                else:
                    proof = _prepare_external_start("upgrade_restart", app_id)
                    _coordinated_legacy_start(
                        app_id, "upgrade_restart", proof)
            except Exception as e:
                restored = _rollback_upgrade(app_id, was_active, was_managed)
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
        _refresh_result_manifest(app_id, manifest)
        return {"id": app_id, "version": manifest.get("version"),
                "installed": True, "restarted": restarted, "signature": sig}


def do_uninstall(app_id: str, *, _busy_timeout: float = 0.0) -> dict:
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
    with busy_gate(wait_timeout=_busy_timeout):
        stopped = False
        rec = state.get_app(app_id)
        if supervisor.is_running(app_id) is not None:
            if rec and rec.get("launch_mode") == "managed":
                _coordinator().stop(app_id)
            else:
                supervisor.stop(app_id)
            stopped = True
        was_active = (state.get_active() == app_id)
        if was_active:
            state.clear_active_if(app_id)
        # A WAITING_RESOURCE/failed managed app has no process but may still
        # carry a desired state or reservations from an interrupted launch.
        if rec and rec.get("instance_id"):
            _coordinator().resources.release(
                rec["instance_id"], int(rec.get("generation", 0)))
        installer.uninstall(app_id)
        state.remove_app(app_id)
        if (_result_hub_instance is not None
                and hasattr(_result_hub_instance, "invalidate_app_manifest")):
            _result_hub_instance.invalidate_app_manifest(app_id)
        _audit("uninstall", id=app_id, stopped=stopped, was_active=was_active)
        return {"id": app_id, "uninstalled": True,
                "stopped": stopped, "was_active": was_active}


def _stop_builtin_for_external(operation: str, app_id: str) -> dict:
    """Cross the firmware's asynchronous builtin-teardown barrier.

    ``builtin.stop`` does the strict readback wait.  This wrapper gives every
    external switch a consistent audit/log record and, most importantly, lets
    the exception propagate *before* supervisor.start can allocate an external
    RKNN context.
    """
    try:
        result = builtin.stop()
    except Exception as e:
        _builtin_invalidate()
        message = ("%s blocked for %s: built-in inference teardown was not "
                   "confirmed; external RKNN was not started: %s" %
                   (operation, app_id, e))
        _audit("external_start_blocked", operation=operation, id=app_id,
               phase="builtin_stop_barrier", error=str(e))
        print("[appmgr] " + message, flush=True)
        raise builtin.BuiltinError(message) from e
    if not isinstance(result, dict) or result.get("stop_confirmed") is not True:
        _builtin_invalidate()
        message = ("%s blocked for %s: built-in stop returned no confirmed "
                   "teardown proof; external RKNN was not started" %
                   (operation, app_id))
        _audit("external_start_blocked", operation=operation, id=app_id,
               phase="builtin_stop_proof", error=repr(result))
        print("[appmgr] " + message, flush=True)
        raise builtin.BuiltinError(message)
    _builtin_invalidate()
    print("[appmgr] %s: builtin teardown confirmed before starting %s"
          % (operation, app_id), flush=True)
    return result


def _prepare_external_start(operation: str, app_id: str):
    """Prepare one orchestrated external-app launch.

    Broker present -> skip the legacy CGI barrier and let the child perform the
    authoritative broker ACQUIRE/drain path with fail-closed env.
    Broker absent  -> preserve the strict builtin-stop barrier and legacy
    appmgr-v1 marker.
    """
    if _npu_broker_present():
        print("[appmgr] %s: broker route selected for %s via %s"
              % (operation, app_id, paths.INFERENCE_CONTROL_SOCK), flush=True)
        return None
    return _stop_builtin_for_external(operation, app_id)


def _start_external_authorized(app_id: str, proof=None, **start_kwargs) -> int:
    """Launch one orchestrated external app under the selected NPU route."""
    if _npu_broker_present():
        return supervisor.start(app_id, npu_broker_required=True,
                                **start_kwargs)
    if not isinstance(proof, dict) or proof.get("stop_confirmed") is not True:
        raise builtin.BuiltinError(
            "external start authorization is missing a confirmed built-in "
            "teardown proof")
    return supervisor.start(app_id, npu_managed=True, **start_kwargs)


def _coordinated_legacy_start(app_id: str, operation: str, proof=None) -> int:
    """Run an activate/switch-compatible app through v2 identity + gateway.

    The operation remains exclusive at the API layer, including its legacy NPU
    barrier, but the child now receives the same instance generation and shared
    result gateway as applications started through /start.
    """
    manifest = _read_manifest(app_id) or {}

    def launch(**identity):
        return _start_external_authorized(app_id, proof, **identity)

    result = _coordinator().start(
        app_id, manifest=manifest, operation=operation, launch=launch,
        launch_mode="legacy")
    if result.get("pid") is None:
        raise supervisor.SupervisorError(
            "%s could not start %s: %s" %
            (operation, app_id, result.get("reason") or
             result.get("observed_state")))
    return int(result["pid"])


def _stop_external(app_id: str) -> dict:
    """Stop through the coordinator when an instance record exists."""
    rec = state.get_app(app_id)
    if rec and rec.get("instance_id"):
        return _coordinator().stop(app_id)
    return supervisor.stop(app_id)


def _restore_active(prev: str, failed_id: str) -> Optional[str]:
    """Transactionally bring the PREVIOUS active app back after a target failed
    to start (健壮#19). Ensures the failed target is fully stopped, then restarts
    `prev` and re-points `active` at it. If prev is absent or itself won't start,
    active is cleared (None) -- never left pointing at an app that isn't up.
    Caller holds the busy-gate."""
    try:
        _stop_external(failed_id)           # guarantee the corpse is gone
    except Exception:
        pass
    if not prev or prev == failed_id:
        state.set_active(None, None)
        return None
    try:
        proof = _prepare_external_start("rollback_restore", prev)
        _coordinated_legacy_start(prev, "rollback_restore", proof)
        man = _read_manifest(prev) or {}
        state.set_active(prev, man.get("version"))
        return prev
    except Exception:
        state.set_active(None, None)
        return None


def do_start(app_id: str, *, _busy_timeout: float = 0.0) -> dict:
    """Concurrently start one managed app (max one process per app id).

    CPU apps may run together.  A v2 ``npu.mode=scheduled`` application waits
    for inferenced and never acquires the direct broker owner.  Old model-backed
    manifests retain the fail-closed direct-NPU compatibility route.
    """
    if app_id == builtin.BUILTIN_ID:
        with busy_gate(wait_timeout=_busy_timeout):
            try:
                result = builtin.start()
            finally:
                # A lost/failed response can still change the driver state.
                # Never leave the short-lived list cache claiming the old one.
                _builtin_invalidate()
            _audit("start", id=builtin.BUILTIN_ID, result=result)
            return {
                "id": builtin.BUILTIN_ID,
                "started": True,
                "detail": result,
            }
    if not paths.valid_app_id(app_id):
        raise ValueError(f"invalid app id {app_id!r}")
    if not os.path.isdir(paths.app_dir(app_id)):
        raise ValueError(f"app not installed: {app_id}")
    with busy_gate(wait_timeout=_busy_timeout):
        manifest = _read_manifest(app_id) or {}
        result = _coordinator().start(
            app_id, manifest=manifest, operation="start",
            launch=_managed_launch(app_id, "start", manifest),
            reset_restart_history=True)
        _refresh_result_manifest(app_id, manifest, result)
        _audit("start", id=app_id, pid=result.get("pid"),
               instance=result.get("instance_id"),
               generation=result.get("generation"),
               desired=result.get("desired_state"),
               observed=result.get("observed_state"),
               reason=result.get("reason"))
        return result


def do_restart(app_id: str, *, _busy_timeout: float = 0.0) -> dict:
    if app_id == builtin.BUILTIN_ID:
        with busy_gate(wait_timeout=_busy_timeout):
            try:
                stopped = builtin.stop()
                if (not isinstance(stopped, dict)
                        or stopped.get("stop_confirmed") is not True):
                    raise builtin.BuiltinError(
                        "builtin restart did not receive confirmed teardown proof")
                started = builtin.start()
            finally:
                _builtin_invalidate()
            _audit("restart", id=builtin.BUILTIN_ID,
                   stopped=stopped, started=started)
            return {
                "id": builtin.BUILTIN_ID,
                "restarted": True,
                "stopped": stopped,
                "started": started,
            }
    if not paths.valid_app_id(app_id):
        raise ValueError(f"invalid app id {app_id!r}")
    if not os.path.isdir(paths.app_dir(app_id)):
        raise ValueError(f"app not installed: {app_id}")
    with busy_gate(wait_timeout=_busy_timeout):
        manifest = _read_manifest(app_id) or {}
        result = _coordinator().restart(
            app_id, manifest=manifest,
            launch=_managed_launch(app_id, "restart", manifest))
        _refresh_result_manifest(app_id, manifest, result)
        _audit("restart", id=app_id, pid=result.get("pid"),
               instance=result.get("instance_id"),
               generation=result.get("generation"),
               observed=result.get("observed_state"),
               reason=result.get("reason"))
        return result


def do_switch(app_id: str) -> dict:
    if not paths.valid_app_id(app_id):
        raise ValueError(f"invalid app id {app_id!r}")
    if not os.path.isdir(paths.app_dir(app_id)):
        raise ValueError(f"app not installed: {app_id}")
    with busy_gate():
        prev = state.get_active()
        # The legacy /switch endpoint used to bypass the built-in detector
        # entirely.  It is still a public CLI/HTTP path, so enforce the same
        # fail-closed barrier as /activate before stopping or starting any
        # external process.  Remember builtin liveness only for rollback.
        prev_builtin = (_builtin_running()
                        if (not prev and not _npu_broker_present()) else False)
        proof = _prepare_external_start("switch", app_id)
        # single-active: stop whoever is currently active (and the target, to
        # guarantee a clean (re)start) before starting the target.
        if prev and prev != app_id:
            _stop_external(prev)
        _stop_external(app_id)
        try:
            pid = _coordinated_legacy_start(app_id, "switch", proof)
        except Exception as e:
            # rollback: restart the previous active app rather than leaving the
            # device with nothing running because the target wouldn't come up.
            restored = _restore_active(prev, app_id)
            if restored is None and prev_builtin:
                try:
                    builtin.start()
                    _builtin_invalidate()
                    restored = "builtin"
                except Exception:
                    pass
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
        _stop_external(prev)
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
        prev_builtin = (_builtin_running()
                        if (not prev and not _npu_broker_present()) else False)
        proof = _prepare_external_start("activate", app_id)
        if prev and prev != app_id:
            _stop_external(prev)
        _stop_external(app_id)                # clean (re)start
        try:
            pid = _coordinated_legacy_start(app_id, "activate", proof)
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


def do_stop(app_id: str = None, *, _busy_timeout: float = 0.0) -> dict:
    with busy_gate(wait_timeout=_busy_timeout):
        target = app_id or state.get_active()
        if target == builtin.BUILTIN_ID:
            try:
                res = builtin.stop()
            finally:
                _builtin_invalidate()
            _audit("stop", id="builtin", result=res)
            return {"stopped": "builtin", "detail": res}
        if not target:
            return {"stopped": None, "note": "no active app"}
        rec = state.get_app(target)
        if rec is None or rec.get("instance_id"):
            res = _coordinator().stop(target)
            state.clear_active_if(target)
            _audit("stop", id=target, result=res)
            return res
        res = supervisor.stop(target)
        state.clear_active_if(target)
        if rec:
            state.set_desired(target, state.DESIRED_STOPPED)
            state.transition(target, "stopped", pid=None, pgid=None,
                             allocations=[], endpoints={}, reason=None)
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


def do_set_config(app_id: str, incoming: dict, *,
                  _busy_timeout: float = 0.0) -> dict:
    if app_id == builtin.BUILTIN_ID:
        with busy_gate(wait_timeout=_busy_timeout):
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
    with busy_gate(wait_timeout=_busy_timeout):
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
            if running:
                rec = state.get_app(app_id)
                if rec and rec.get("launch_mode") == "managed":
                    _coordinator().restart(
                        app_id, manifest=man,
                        launch=_managed_launch(app_id, "config_restart", man))
                    restarted = True
                elif state.get_active() == app_id:
                    _stop_external(app_id)
                    proof = _prepare_external_start("config_restart", app_id)
                    _coordinated_legacy_start(
                        app_id, "config_restart", proof)
                    restarted = True
        _audit("config", id=app_id, keys=sorted(clean.keys()),
               applied=mode, restarted=restarted, reloaded=reloaded)
        # Config writes can restart into a new generation; refresh from the
        # installed manifest/state once here rather than consulting it for each
        # result.  Live-only output/template changes retain the same identity.
        _refresh_result_manifest(app_id, man)
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
        # Same read-only rule as do_list(): publish exits for children this
        # daemon owns, but leave stale-record cleanup to the busy-gated
        # reconciler so a CLI writer's partial run record cannot be destroyed.
        supervisor.reap_children()
        supervisor.drain_exits()
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


def do_resources() -> dict:
    """Resource journal plus a protocol-level inferenced health snapshot."""
    coord = _coordinator()
    snapshot = coord.resources.snapshot()
    grouped = {}
    for allocation in snapshot.get("allocations") or []:
        if allocation.get("state") not in ("reserved", "bound"):
            continue
        name = str(allocation.get("resource") or "unknown")
        item = grouped.setdefault(name, {
            "name": name, "used": 0, "total": None,
            "status": "available", "owners": [],
        })
        try:
            item["used"] += int(allocation.get("amount", 1))
        except (TypeError, ValueError):
            item["used"] += 1
        capacity = allocation.get("capacity")
        try:
            capacity = int(capacity)
        except (TypeError, ValueError):
            capacity = 0
        if capacity > 0:
            item["total"] = max(item.get("total") or 0, capacity)
        owner = allocation.get("app_id")
        if owner and owner not in item["owners"]:
            item["owners"].append(owner)
        if allocation.get("mode") == "exclusive":
            item["status"] = "busy"
    for item in grouped.values():
        if item["total"] is not None and item["used"] >= item["total"]:
            item["status"] = "busy"
        item["owners"].sort()
    # Keep service resources visible at zero usage so the UI need not infer
    # whether an absent row means idle or unsupported.
    for name in ("npu.scheduler", "npu.direct", "result.gateway"):
        grouped.setdefault(name, {
            "name": name, "used": 0, "total": (1 if name == "npu.direct" else None),
            "status": "available", "owners": [],
        })
    conflicts = []
    for app_id, rec in state.app_states().items():
        if rec.get("observed_state") != "waiting_resource":
            continue
        resource = rec.get("blocked_resource")
        conflicts.append({
            "id": app_id,
            "resource": resource,
            "name": resource,
            "owners": list(rec.get("resource_owners") or []),
            "message": rec.get("reason") or "waiting for resource",
        })
    snapshot["resources"] = sorted(grouped.values(), key=lambda item: item["name"])
    snapshot["conflicts"] = conflicts
    snapshot["inference_service"] = coord.inference_status()
    if _result_gateway_instance is not None:
        snapshot["result_gateway"] = _result_gateway_instance.status()
    else:
        snapshot["result_gateway"] = {
            "running": False, "uds": paths.RESULT_GATEWAY_SOCK,
            "ws_host": paths.RESULT_GATEWAY_HOST,
            "ws_port": paths.RESULT_GATEWAY_PORT,
        }
    if _result_hub_instance is not None:
        snapshot["result_hub"] = _result_hub_instance.status()
    else:
        snapshot["result_hub"] = {
            "running": False,
            "schema": canonical_results.SCHEMA,
            "schema_version": canonical_results.SCHEMA_VERSION,
            "system_uds": paths.SYSTEM_RESULT_SOCK,
            "ws_host": paths.RESULT_HUB_HOST,
            "ws_port": paths.RESULT_HUB_PORT,
        }
    return snapshot


def do_get_mqtt() -> dict:
    """Global MQTT/HA broker config (password redacted -> password_set flag)."""
    return mqttcfg.public_view()


def do_set_mqtt(incoming: dict) -> dict:
    """Persist global MQTT config and restart every affected running app."""
    clean, errors = mqttcfg.validate(incoming)
    if errors:
        raise ValueError("; ".join(errors))
    with busy_gate():
        mqttcfg.save(clean)
        restarted_apps = []
        active = state.get_active()
        candidates = []
        if os.path.isdir(paths.APPS_DIR):
            candidates = [name for name in sorted(os.listdir(paths.APPS_DIR))
                          if paths.valid_app_id(name)
                          and os.path.isdir(paths.app_dir(name))]
        for app_id in candidates:
            if not supervisor.is_running(app_id):
                continue
            rec = state.get_app(app_id)
            if rec and rec.get("launch_mode") == "managed":
                manifest = _read_manifest(app_id) or {}
                _coordinator().restart(
                    app_id, manifest=manifest,
                    launch=_managed_launch(app_id, "mqtt_restart", manifest))
                restarted_apps.append(app_id)
            elif app_id == active:
                _stop_external(app_id)
                proof = _prepare_external_start("mqtt_restart", app_id)
                _coordinated_legacy_start(app_id, "mqtt_restart", proof)
                restarted_apps.append(app_id)
        # Keep the v1 scalar for old clients; add the complete concurrent set.
        restarted = (active if active in restarted_apps else
                     (restarted_apps[0] if len(restarted_apps) == 1 else None))
        _audit("mqtt", enabled=clean.get("enabled"), host=clean.get("host"),
               restarted=restarted, restarted_apps=restarted_apps)
        view = mqttcfg.public_view()
        view["restarted"] = restarted
        view["restarted_apps"] = restarted_apps
        return view


# --------------------------------------------------------------------------- #
# Web-native /api/app-center/v1 facade.  This deliberately does not occupy the
# firmware's existing /api/v1 namespace, which proxies SenseCraft cloud APIs.
# --------------------------------------------------------------------------- #
def do_v1_policy() -> dict:
    """Return the live, non-secret package admission policy for Web clients."""
    return {
        "manifest": {
            "required_version": appmanifest.MANIFEST_VERSION,
        },
        "upload": {
            "package_field": "package",
            "signature_field": "signature",
            "filename_pattern": appuploads.PACKAGE_FILENAME_PATTERN,
            "max_package_bytes": int(paths.MAX_PKG_BYTES),
            "max_request_bytes": int(
                paths.MAX_PKG_BYTES + appuploads.MAX_MULTIPART_OVERHEAD),
            "max_signature_bytes": int(appuploads.MAX_SIGNATURE_BYTES),
            "max_unpacked_bytes": int(paths.MAX_UNPACKED_BYTES),
            "max_members": int(paths.MAX_MEMBERS),
            "max_staging_bytes": int(paths.MAX_UPLOAD_STAGING_BYTES),
            "max_staged_uploads": int(paths.MAX_STAGED_UPLOADS),
            "min_free_bytes": int(paths.MIN_UPLOAD_FREE_BYTES),
            "ttl_sec": int(paths.UPLOAD_TTL_SEC),
        },
        "signature": {
            "algorithm": appsigning.SIGNATURE_ALG,
            "encoding": "base64-der",
            "required": bool(paths.REQUIRE_SIGNATURE),
            "developer_mode_allowed": _developer_mode_allowed(),
            "invalid_signatures_rejected": True,
            "owner_keys": {
                "management_enabled": True,
                "format": "pem",
                "curve": "P-256",
                "max_keys": int(paths.MAX_OWNER_KEYS),
                "max_key_bytes": int(paths.MAX_TRUST_KEY_BYTES),
                "explicit_confirmation_required": True,
            },
        },
    }


def do_v1_trust() -> dict:
    """Return public metadata for immutable vendor and removable owner keys."""
    return {
        "keys": apptrust.list_trust(),
        "limits": {
            "max_owner_keys": int(paths.MAX_OWNER_KEYS),
            "max_trust_key_bytes": int(paths.MAX_TRUST_KEY_BYTES),
        },
    }


def do_v1_install_owner_key(body: dict) -> dict:
    """Explicitly extend device-owner trust; never infer trust from a package."""
    if body.get("confirm_trust") is not True:
        raise ValueError("owner-key trust must be explicitly confirmed")
    label = body.get("label")
    public_key = body.get("public_key")
    if not isinstance(label, str):
        raise ValueError("missing/invalid 'label'")
    if not isinstance(public_key, str):
        raise ValueError("missing/invalid 'public_key'")

    with busy_gate(wait_timeout=paths.V1_OPERATION_BUSY_TIMEOUT_SEC):
        result = apptrust.install_owner_key(label, public_key)
        key = result["key"]
        _audit(
            "v1_trust_owner_install",
            label=key.get("label"), fingerprint=key.get("fingerprint"),
            created=bool(result.get("created")),
        )
        _operation_manager().events.publish(
            "trust",
            action=("owner-installed" if result.get("created")
                    else "owner-present"),
            key=key,
        )
        return result


def do_v1_remove_owner_key(fingerprint_hex: str) -> dict:
    """Remove one owner trust identity addressed by its canonical digest."""
    fingerprint = "sha256:" + fingerprint_hex.lower()
    with busy_gate(wait_timeout=paths.V1_OPERATION_BUSY_TIMEOUT_SEC):
        result = apptrust.remove_owner_key(fingerprint)
        _audit(
            "v1_trust_owner_delete",
            fingerprint=result.get("fingerprint"),
            deleted=result.get("deleted"),
        )
        _operation_manager().events.publish(
            "trust", action="owner-deleted",
            fingerprint=result.get("fingerprint"),
        )
        return result


def _v1_status(app: dict) -> str:
    observed = str(app.get("observed_state") or "stopped")
    if app.get("running") or observed in ("ready", "running", "degraded"):
        return "running"
    if observed in ("preparing_env", "waiting_dependency", "waiting_resource",
                    "starting"):
        return "starting"
    if observed == "stopping":
        return "stopping"
    if observed in ("failed", "backoff", "crash_loop"):
        return "failed"
    return "stopped"


def do_v1_apps() -> dict:
    """Stable list envelope consumed by the Web App Center."""
    listing = do_list()
    operations = _operation_manager()
    items = []
    for raw in listing.get("apps") or []:
        app_id = raw.get("id")
        item = dict(raw)
        manifest = (builtin.manifest() if app_id == builtin.BUILTIN_ID
                    else (_read_manifest(app_id) or {}))
        status = _v1_status(raw)
        active_operation = operations.active_for(app_id)
        if active_operation:
            operation_type = active_operation.get("type")
            if operation_type == "install":
                status = "installing"
            elif operation_type in ("stop", "delete"):
                status = "stopping"
            elif operation_type in ("start", "restart"):
                status = "starting"
        item.update({
            "status": status,
            "manifest": manifest,
            "runtime": {
                "status": status,
                "desired_state": raw.get("desired_state"),
                "observed_state": raw.get("observed_state"),
                "pid": raw.get("pid"),
                "reason": raw.get("state_reason"),
            },
            "instance": {
                "id": raw.get("instance_id"),
                "generation": raw.get("generation", 0),
                "status": status,
            } if raw.get("instance_id") else None,
            "reason": raw.get("state_reason"),
            "error": (raw.get("state_reason") if status == "failed" else None),
            "resource_conflicts": ([{
                "resource": (state.get_app(app_id) or {}).get("blocked_resource"),
                "owners": list((state.get_app(app_id) or {}).get(
                    "resource_owners") or []),
                "message": raw.get("state_reason"),
            }] if raw.get("observed_state") == "waiting_resource" else []),
        })
        items.append(item)
    return {
        "apps": items,
        "active_app": listing.get("active_app"),
        "running_apps": listing.get("running_apps") or [],
        "revision": listing.get("state_revision", 0),
    }


def _signature_view(status: dict, *, developer_mode_allowed: bool) -> dict:
    signed = bool((status or {}).get("signed"))
    verified = bool((status or {}).get("verified"))
    return {
        "status": ("verified" if verified else "unsigned" if not signed else "invalid"),
        "signed": signed,
        "verified": verified,
        "alg": (status or {}).get("alg"),
        "detail": (status or {}).get("detail"),
        "signer_kind": (status or {}).get("signer_kind"),
        "key_fingerprint": (status or {}).get("key_fingerprint"),
        "developer_mode_allowed": bool(developer_mode_allowed and not signed),
    }


def _developer_mode_allowed() -> bool:
    # REQUIRE_SIGNATURE=0 is the historic device-owner developer switch.  The
    # dedicated v1 flag is clearer for new firmware, but retaining the old one
    # prevents a developer image from becoming stricter merely by moving from
    # /api/appMgr/install to the Web App Center flow.
    return bool(paths.DEVELOPER_MODE_ALLOWED or not paths.REQUIRE_SIGNATURE)


def do_v1_upload(stream, content_length: int, content_type: str) -> dict:
    """Receive a package without buffering it, then perform non-mutating preflight."""
    upload = appuploads.receive(stream, content_length, content_type)
    upload_id = upload["upload_id"]
    try:
        # Inspection never executes package code.  It may parse an unsigned
        # package so the UI can explain why production install is disabled;
        # finalize independently enforces the device developer-mode policy.
        info = installer.inspect(
            upload["package_path"], upload.get("signature"), allow_unsigned=True)
        manifest = info["manifest"]
        version = manifest.get("manifest_version", 1)
        developer_allowed = bool(
            _developer_mode_allowed()
            and not (info.get("signature") or {}).get("signed"))
        signature = _signature_view(
            info.get("signature") or {},
            developer_mode_allowed=developer_allowed)
        conflicts = []
        resource_error = None
        try:
            plan = appresources.plan_manifest(
                manifest,
                appconfig.effective_values(manifest, manifest.get("id", "")))
            conflicts = _coordinator().resources.conflicts(
                plan, ignore_app_id=manifest.get("id"))
        except (ValueError, appresources.ResourceError) as exc:
            resource_error = str(exc)
        checks = [
            {"id": "package", "label": "Package integrity", "passed": True,
             "message": "archive and release metadata validated"},
            {"id": "manifest-v2", "label": "Manifest v2",
             "passed": version == 2,
             "message": ("manifest v2" if version == 2
                         else "the Web App Center installs manifest v2 packages only")},
            {"id": "compatibility", "label": "Platform compatibility",
             "passed": True, "message": "declared platform is compatible"},
            {"id": "resources", "label": "Resource declaration",
             "passed": resource_error is None,
             "message": resource_error or "resource declaration is schedulable"},
            {"id": "signature", "label": "Publisher signature",
             "passed": signature["status"] == "verified" or developer_allowed,
             "message": (signature.get("detail") or signature["status"])},
        ]
        preflight = {
            "manifest": manifest,
            "manifest_version": version,
            "release_id": (info.get("preflight") or {}).get("release_id"),
            "compatibility": manifest.get("compatibility"),
            "permissions": manifest.get("permissions") or {},
            "resources": manifest.get("resources") or {},
            "dependencies": manifest.get("dependencies") or {},
            "python": manifest.get("python") or {},
            "instances": manifest.get("instances") or {"max": 1},
            "health": manifest.get("health") or {},
            "signature": signature,
            "conflicts": conflicts,
            "checks": checks,
            "developer_mode_allowed": developer_allowed,
        }
        appuploads.update(
            upload_id, status="preflighted", app_id=manifest.get("id"),
            manifest_version=version, preflight=preflight)
        _audit("v1_upload", upload_id=upload_id, id=manifest.get("id"),
               filename=upload.get("filename"), size=upload.get("size"),
               signature=signature["status"])
        return {"upload_id": upload_id, "preflight": preflight}
    except Exception as exc:
        try:
            appuploads.update(upload_id, status="rejected", error=str(exc))
        except Exception:
            pass
        finally:
            # A rejected archive can never be finalized.  Retaining its full
            # bytes would let repeated bad preflights consume /userdata.
            if not appuploads.remove(upload_id):
                _audit("v1_upload_cleanup_failed", upload_id=upload_id,
                       terminal="rejected")
        raise


def _same_json(left, right) -> bool:
    try:
        return json.dumps(left, sort_keys=True, separators=(",", ":")) == \
            json.dumps(right, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return False


def _assert_v1_preflight_binding(expected: dict, inspected: dict) -> None:
    """Bind the mutation pass to exactly what the user approved in preflight."""
    actual_manifest = inspected.get("manifest") or {}
    if not _same_json(expected.get("manifest") or {}, actual_manifest):
        raise installer.InstallError(
            "package manifest changed after permission confirmation")
    expected_release = expected.get("release_id")
    actual_release = (inspected.get("preflight") or {}).get("release_id")
    if expected_release != actual_release:
        raise installer.InstallError(
            "package release identity changed after preflight")

    expected_signature = expected.get("signature") or {}
    actual_signature = inspected.get("signature") or {}
    expected_identity = (
        expected_signature.get("status"),
        expected_signature.get("signer_kind"),
        expected_signature.get("key_fingerprint"),
    )
    actual_identity = (
        "verified" if actual_signature.get("verified") else
        "unsigned" if not actual_signature.get("signed") else "invalid",
        actual_signature.get("signer_kind"),
        actual_signature.get("key_fingerprint"),
    )
    if expected_identity != actual_identity:
        raise installer.InstallError(
            "package signer identity changed after preflight")


def do_v1_install(body: dict) -> dict:
    upload_id = body.get("upload_id")
    if not isinstance(upload_id, str) or not upload_id:
        raise ValueError("missing/invalid 'upload_id'")
    developer_mode = body.get("developer_mode", False)
    if not isinstance(developer_mode, bool):
        raise ValueError("'developer_mode' must be a boolean")
    if body.get("permissions_confirmed") is not True:
        raise ValueError("permissions must be explicitly confirmed")

    with _upload_finalize_lock:
        upload = appuploads.verify(upload_id)
        if upload.get("status") in ("install_queued", "installing", "installed"):
            raise ValueError("upload has already been finalized")
        preflight = upload.get("preflight")
        if not isinstance(preflight, dict):
            raise ValueError("upload has no successful preflight")
        manifest = preflight.get("manifest") or {}
        if manifest.get("manifest_version") != 2:
            raise ValueError(
                "/api/app-center/v1/apps requires manifest_version=2")
        expected_permissions = manifest.get("permissions") or {}
        if not _same_json(body.get("permissions"), expected_permissions):
            raise ValueError("confirmed permissions do not match package manifest")

        signature = preflight.get("signature") or {}
        signature_status = signature.get("status")
        if signature_status == "verified":
            allow_unsigned = False
        elif signature_status == "unsigned":
            if not developer_mode:
                raise ValueError("unsigned package requires explicit developer_mode")
            if not _developer_mode_allowed():
                raise ValueError("developer mode is disabled on this device")
            allow_unsigned = True
        else:
            raise ValueError("package signature is invalid")

        app_id = manifest.get("id")
        appuploads.update(upload_id, status="install_queued")

        def install_job():
            try:
                appuploads.update(upload_id, status="installing")
                result = do_install(
                    upload["package_path"], upload.get("signature"),
                    allow_unsigned=allow_unsigned,
                    expected_preflight=preflight,
                    _busy_timeout=paths.V1_OPERATION_BUSY_TIMEOUT_SEC)
            except Exception as exc:
                try:
                    appuploads.update(upload_id, status="failed", error=str(exc))
                except Exception:
                    pass
                raise
            else:
                try:
                    appuploads.update(upload_id, status="installed", error=None)
                except Exception:
                    pass
                _operation_manager().events.publish("app", app_id=app_id,
                                                    action="installed")
                return result
            finally:
                # Operations retain the terminal result/error.  Uploaded package
                # bytes are single-use and must not accumulate after either path.
                if not appuploads.remove(upload_id):
                    _audit("v1_upload_cleanup_failed", upload_id=upload_id,
                           terminal="install")

        try:
            operation = _operation_manager().submit(
                "install", app_id, install_job)
        except Exception:
            appuploads.update(upload_id, status="preflighted")
            raise
        # Do not write the operation id back after submit: the worker may finish
        # (and remove this single-use upload) before submit returns.  The
        # operation object is already the durable client-visible correlation.
        return {"operation": operation}


def do_v1_cancel_upload(upload_id: str) -> dict:
    """Delete an inactive upload without racing installation finalization."""
    with _upload_finalize_lock:
        result = appuploads.cancel(upload_id)
    _audit(
        "v1_upload_delete", upload_id=upload_id,
        deleted=result.get("deleted"),
        previous_status=result.get("previous_status"),
    )
    return result


def _require_installed(app_id: str) -> None:
    if not paths.valid_app_id(app_id):
        raise ValueError("invalid app id %r" % app_id)
    if not os.path.isdir(paths.app_dir(app_id)):
        raise FileNotFoundError("app not installed: %s" % app_id)


def do_v1_lifecycle(app_id: str, action: str) -> dict:
    # ``builtin`` is a synthetic first-class app backed by rkipc/entry.cgi. It
    # deliberately has no /userdata/local/apps/builtin directory, so only
    # self-hosted applications participate in the installed-directory gate.
    if app_id != builtin.BUILTIN_ID:
        _require_installed(app_id)
    callbacks = {"start": do_start, "stop": do_stop, "restart": do_restart}
    callback = callbacks.get(action)
    if callback is None:
        raise ValueError("unsupported lifecycle action")

    def lifecycle_job():
        result = callback(
            app_id, _busy_timeout=paths.V1_OPERATION_BUSY_TIMEOUT_SEC)
        _operation_manager().events.publish("app", app_id=app_id, action=action)
        return result

    operation = _operation_manager().submit(action, app_id, lifecycle_job)
    return {"operation": operation}


def do_v1_delete(app_id: str) -> dict:
    _require_installed(app_id)

    def delete_job():
        result = do_uninstall(
            app_id, _busy_timeout=paths.V1_OPERATION_BUSY_TIMEOUT_SEC)
        _operation_manager().events.publish("app", app_id=app_id, action="deleted")
        return result

    operation = _operation_manager().submit("delete", app_id, delete_job)
    return {"operation": operation}


def do_v1_logs(app_id: str, tail: int = 200) -> dict:
    _require_installed(app_id)
    try:
        tail = int(tail)
    except (TypeError, ValueError) as exc:
        raise ValueError("tail must be an integer") from exc
    tail = min(2000, max(1, tail))
    logfile = os.path.join(paths.logdir(app_id), "app.log")
    try:
        with open(logfile, "rb") as source:
            source.seek(0, os.SEEK_END)
            size = source.tell()
            source.seek(max(0, size - 512 * 1024), os.SEEK_SET)
            data = source.read(512 * 1024)
    except FileNotFoundError:
        data = b""
    lines = data.decode("utf-8", "replace").splitlines()[-tail:]
    return {"id": app_id, "lines": lines, "text": "\n".join(lines)}


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #
class _Handler(BaseHTTPRequestHandler):
    server_version = "appmgr/1.0"
    protocol_version = "HTTP/1.1"

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

    def _body_json(self, cap: int = 1024 * 1024) -> dict:
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid Content-Length") from exc
        if n < 0:
            raise ValueError("invalid Content-Length")
        if n == 0:
            return {}
        if n > cap:
            raise ValueError("JSON request body is too large")
        raw = self.rfile.read(n)
        if len(raw) != n:
            raise ValueError("request body is truncated")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("request body is not valid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("JSON request body must be an object")
        return value

    def _body_json_v1(self, cap: int = 1024 * 1024) -> dict:
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid Content-Length") from exc
        if length <= 0:
            raise ValueError("JSON request body is required")
        if length > cap:
            raise ValueError("JSON request body is too large")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValueError("request body is truncated")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("request body is not valid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("JSON request body must be an object")
        return value

    def _require_safe_mutation_origin(self) -> None:
        """Block ambient-cookie CSRF while preserving explicit API clients.

        Browser mutations carry Origin and must exactly match the public
        scheme/authority forwarded by nginx.  Origin-less requests are accepted
        only for a direct loopback Host or when the caller presents an explicit
        Bearer credential; a cross-site page cannot synthesize that header
        without a successful CORS preflight.
        """
        forwarded = self.headers.get("X-Forwarded-Proto", "http")
        if not isinstance(forwarded, str) or forwarded.lower() not in (
                "http", "https"):
            raise RequestOriginError("invalid forwarded request scheme")
        scheme = forwarded.lower()
        host, port = _request_authority(self.headers.get("Host", ""), scheme)
        origin = self.headers.get("Origin")
        if origin is not None:
            origin_scheme, origin_host, origin_port = _origin_authority(origin)
            if (origin_scheme, origin_host, origin_port) != (scheme, host, port):
                raise RequestOriginError("cross-origin mutation is not allowed")
            return

        authorization = self.headers.get("Authorization", "")
        explicit_bearer = (isinstance(authorization, str)
                           and authorization.startswith("Bearer ")
                           and bool(authorization[7:].strip()))
        if _is_loopback_authority(host) or explicit_bearer:
            return
        raise RequestOriginError(
            "unsafe request requires same-origin Origin or explicit Bearer authentication")

    def _guard_mutation_origin(self) -> bool:
        try:
            self._require_safe_mutation_origin()
            return True
        except RequestOriginError as exc:
            # The request body may still be in flight (notably multipart).
            # Refuse connection reuse so those bytes cannot become a request.
            self.close_connection = True
            self._send(403, {"error": str(exc)})
            return False

    def _v1_error(self, exc: Exception) -> None:
        if isinstance(exc, (FileNotFoundError, apptrust.TrustNotFoundError)):
            code = 404
        elif isinstance(exc, apptrust.TrustConflictError):
            code = 409
        elif isinstance(exc, apptrust.TrustValidationError):
            code = 400
        elif isinstance(exc, appuploads.UploadConflictError):
            code = 409
        elif isinstance(exc, appuploads.StagingQuotaError):
            code = 507
        elif isinstance(exc, appoperations.EventCapacityError):
            code = 503
        elif isinstance(exc, appoperations.OperationBusyError):
            code = 409
        elif isinstance(exc, BusyError):
            code = 409
        elif isinstance(exc, (appuploads.MultipartError, installer.InstallError,
                              supervisor.SupervisorError,
                              appresources.ResourceError, ValueError)):
            code = 400
        else:
            code = 500
        self._send(code, {
            "error": str(exc) if code < 500 else "%s: %s" %
            (type(exc).__name__, exc),
        })

    def _send_events(self) -> None:
        manager = _operation_manager()
        subscription = manager.events.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    event = subscription.get(timeout=15.0)
                except queue.Empty:
                    self.wfile.write(b": heartbeat\n\n")
                else:
                    payload = json.dumps(
                        event, separators=(",", ":"), default=str).encode("utf-8")
                    self.wfile.write(
                        b"id: " + str(event.get("id", "")).encode("ascii") + b"\n")
                    self.wfile.write(b"data: " + payload + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            manager.events.unsubscribe(subscription)
            self.close_connection = True

    def log_message(self, *a):     # silence default stderr logging
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        if path == "/api/app-center/v1/policy":
            return self._send(200, do_v1_policy())
        if path == "/api/app-center/v1/trust":
            try:
                return self._send(200, do_v1_trust())
            except Exception as exc:
                return self._v1_error(exc)
        if path == "/api/app-center/v1/apps":
            try:
                return self._send(200, do_v1_apps())
            except Exception as exc:
                return self._v1_error(exc)
        if path == "/api/app-center/v1/operations":
            return self._send(200, {
                "operations": _operation_manager().list(),
            })
        if path == "/api/app-center/v1/resources":
            try:
                return self._send(200, do_resources())
            except Exception as exc:
                return self._v1_error(exc)
        if path == "/api/app-center/v1/visualization":
            try:
                return self._send(200, do_get_visualization())
            except Exception as exc:
                return self._v1_error(exc)
        if path == "/api/app-center/v1/results/status":
            if _result_hub_instance is None:
                return self._send(503, {
                    "running": False,
                    "schema": canonical_results.SCHEMA,
                    "schema_version": canonical_results.SCHEMA_VERSION,
                })
            return self._send(200, _result_hub_instance.status())
        if path == "/api/app-center/v1/events":
            try:
                return self._send_events()
            except Exception as exc:
                return self._v1_error(exc)
        match = re.fullmatch(
            r"/api/app-center/v1/apps/([a-z0-9-]{1,64})/(config|logs)",
            path)
        if match:
            app_id, endpoint = match.groups()
            try:
                if endpoint == "config":
                    if app_id != builtin.BUILTIN_ID:
                        _require_installed(app_id)
                    return self._send(200, do_get_config(app_id))
                tail = (parse_qs(parsed.query).get("tail") or [200])[0]
                return self._send(200, do_v1_logs(app_id, tail))
            except Exception as exc:
                return self._v1_error(exc)
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
        if path == "/api/appMgr/resources":
            return self._send(200, do_resources())
        if path == "/api/appMgr/resultGateway":
            if _result_gateway_instance is None:
                return self._send(503, {"running": False})
            return self._send(200, _result_gateway_instance.status())
        if path == "/api/appMgr/resultHub":
            if _result_hub_instance is None:
                return self._send(503, {
                    "running": False,
                    "schema": canonical_results.SCHEMA,
                    "schema_version": canonical_results.SCHEMA_VERSION,
                })
            return self._send(200, _result_hub_instance.status())
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
        if not self._guard_mutation_origin():
            return
        path = urlparse(self.path).path.rstrip("/")
        if path == "/api/app-center/v1/uploads":
            try:
                try:
                    length = int(self.headers.get("Content-Length", 0) or 0)
                except (TypeError, ValueError) as exc:
                    raise appuploads.MultipartError("invalid Content-Length") from exc
                result = do_v1_upload(
                    self.rfile, length, self.headers.get("Content-Type", ""))
                return self._send(201, result)
            except Exception as exc:
                # A rejected streaming body may not have reached its terminal
                # boundary.  Do not reuse that HTTP connection for another
                # request whose bytes could be mistaken for the remainder.
                self.close_connection = True
                return self._v1_error(exc)
        if path == "/api/app-center/v1/apps":
            try:
                return self._send(202, do_v1_install(self._body_json_v1()))
            except Exception as exc:
                return self._v1_error(exc)
        if path == "/api/app-center/v1/trust/owners":
            try:
                result = do_v1_install_owner_key(self._body_json_v1())
                return self._send(201 if result.get("created") else 200, result)
            except Exception as exc:
                return self._v1_error(exc)
        match = re.fullmatch(
            r"/api/app-center/v1/apps/([a-z0-9-]{1,64})/"
            r"(start|stop|restart)", path)
        if match:
            try:
                app_id, action = match.groups()
                # Accept an empty body for action endpoints.  If a client sends
                # one, validate that it is JSON rather than leaving unread bytes.
                if int(self.headers.get("Content-Length", 0) or 0):
                    self._body_json_v1()
                return self._send(202, do_v1_lifecycle(app_id, action))
            except Exception as exc:
                return self._v1_error(exc)
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
        try:
            body = self._body_json()
        except ValueError as exc:
            self.close_connection = True
            return self._send(400, {"error": str(exc)})
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
            if path == "/api/appMgr/start":
                i = body.get("id")
                if not i:
                    return self._send(400, {"error": "missing 'id'"})
                return self._send(200, do_start(i))
            if path == "/api/appMgr/restart":
                i = body.get("id")
                if not i:
                    return self._send(400, {"error": "missing 'id'"})
                return self._send(200, do_restart(i))
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
        except (installer.InstallError, supervisor.SupervisorError,
                appresources.ResourceError, ValueError) as e:
            self._send(400, {"error": str(e)})
        except Exception as e:
            self._send(500, {"error": repr(e)})

    def do_PUT(self):
        if not self._guard_mutation_origin():
            return
        path = urlparse(self.path).path.rstrip("/")
        if path == "/api/app-center/v1/visualization":
            try:
                return self._send(
                    200, do_set_visualization(self._body_json_v1()))
            except Exception as exc:
                return self._v1_error(exc)
        match = re.fullmatch(
            r"/api/app-center/v1/apps/([a-z0-9-]{1,64})/config", path)
        if not match:
            return self._send(404, {"error": "not found"})
        try:
            app_id = match.group(1)
            if app_id != builtin.BUILTIN_ID:
                _require_installed(app_id)
            body = self._body_json_v1()
            values = body.get("values")
            if not isinstance(values, dict):
                raise ValueError("missing/invalid 'values'")
            result = do_set_config(
                app_id, values,
                _busy_timeout=paths.V1_OPERATION_BUSY_TIMEOUT_SEC)
            _operation_manager().events.publish(
                "app", app_id=app_id, action="config")
            return self._send(200, result)
        except Exception as exc:
            return self._v1_error(exc)

    def do_DELETE(self):
        if not self._guard_mutation_origin():
            return
        path = urlparse(self.path).path.rstrip("/")
        upload_match = re.fullmatch(
            r"/api/app-center/v1/uploads/([0-9a-f]{32})", path)
        if upload_match:
            try:
                return self._send(
                    200, do_v1_cancel_upload(upload_match.group(1)))
            except Exception as exc:
                return self._v1_error(exc)
        trust_match = re.fullmatch(
            r"/api/app-center/v1/trust/owners/([0-9a-fA-F]{64})", path)
        if trust_match:
            try:
                return self._send(
                    200, do_v1_remove_owner_key(trust_match.group(1)))
            except Exception as exc:
                return self._v1_error(exc)
        match = re.fullmatch(
            r"/api/app-center/v1/apps/([a-z0-9-]{1,64})", path)
        if not match:
            return self._send(404, {"error": "not found"})
        try:
            return self._send(202, do_v1_delete(match.group(1)))
        except Exception as exc:
            return self._v1_error(exc)


class _AppHTTPServer(ThreadingHTTPServer):
    # SSE connections are intentionally long lived.  They must not prevent a
    # service restart from closing the listening socket and exiting promptly.
    daemon_threads = True
    allow_reuse_address = True


_single_instance_fh = None


def _acquire_single_instance() -> bool:
    global _single_instance_fh
    paths.ensure_dirs()
    _single_instance_fh = open(paths.lock_file(), "w")
    try:
        fcntl.flock(_single_instance_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _single_instance_fh.write(str(os.getpid()))
        _single_instance_fh.flush()
        return True
    except OSError:
        return False


def _boot_restore() -> None:
    """Reconcile and restore every desired-running app, independently."""
    coord = _coordinator()
    try:
        stale = coord.reconcile_allocations()
        if stale:
            print(f"[appmgr] released stale allocations: {stale}", flush=True)
    except Exception as exc:
        _audit("resource_reconcile_failed", error=repr(exc))

    active = state.get_active()
    for app_id in state.desired_apps():
        try:
            if not os.path.isdir(paths.app_dir(app_id)):
                state.transition(app_id, "failed",
                                 reason="desired app is not installed")
                continue
            running = supervisor.is_running(app_id)
            manifest = _read_manifest(app_id) or {}
            if running is not None:
                # A previous daemon may have died after supervisor committed
                # run.pid but before the coordinator's on_spawn callback.  The
                # idempotent running branch completes identity/resource binding
                # and republishes scheduled-inference authorization; observe()
                # alone cannot reconstruct all of those pieces.
                result = coord.start(
                    app_id, manifest=manifest, operation="boot_adopt",
                    launch=_managed_launch(app_id, "boot_adopt", manifest))
                _audit("boot_adopt", id=app_id, pid=result.get("pid"),
                       instance=result.get("instance_id"),
                       generation=result.get("generation"),
                       observed=result.get("observed_state"))
                print("[appmgr] boot-adopt: %s state=%s pid=%s" %
                      (app_id, result.get("observed_state"),
                       result.get("pid")), flush=True)
                continue
            rec = state.get_app(app_id) or {}
            if rec.get("launch_mode") == "legacy" and app_id == active:
                proof = _prepare_external_start("boot_restore", app_id)
                pid = _coordinated_legacy_start(
                    app_id, "boot_restore", proof)
                result = {"pid": pid, "observed_state": "running"}
            else:
                result = coord.start(
                    app_id, manifest=manifest, operation="boot_restore",
                    launch=_managed_launch(app_id, "boot_restore", manifest))
            _audit("boot_restore", id=app_id, pid=result.get("pid"),
                   observed=result.get("observed_state"),
                   reason=result.get("reason"))
            print("[appmgr] boot-restore: %s state=%s pid=%s" %
                  (app_id, result.get("observed_state"), result.get("pid")),
                  flush=True)
        except Exception as exc:
            _audit("boot_restore_failed", id=app_id, error=repr(exc))
            print(f"[appmgr] boot-restore: {app_id} failed: {exc!r}",
                  flush=True)


def _reconcile_once() -> list:
    """Advance desired lifecycle state once; safe to call from host tests."""
    try:
        supervisor.reap_children()
        supervisor.drain_exits()
    except Exception:
        pass
    # Stale-record cleanup can kill a verified same-boot orphan group and is
    # therefore a mutation, not a GET side effect.  Serialize it with starts,
    # stops and CLI processes using the same flock.  If another mutation owns
    # the gate, skip this tick; liveness reads remain accurate without cleanup.
    try:
        with busy_gate():
            supervisor.sweep_stale()
    except BusyError:
        pass
    except Exception:
        pass
    results = []
    coord = _coordinator()
    for app_id in state.desired_apps():
        rec = state.get_app(app_id) or {}
        if rec.get("launch_mode") != "managed":
            continue
        if not os.path.isdir(paths.app_dir(app_id)):
            state.transition(app_id, "failed", pid=None, pgid=None,
                             allocations=[],
                             reason="desired app is not installed")
            continue
        try:
            with busy_gate():
                # Re-read after taking the mutation gate: an explicit stop may
                # have won the race while this tick was enumerating desired ids.
                current = state.get_app(app_id) or {}
                if current.get("desired_state") != state.DESIRED_RUNNING:
                    continue
                manifest = _read_manifest(app_id) or {}
                before = current.get("observed_state")
                result = coord.reconcile_one(
                    app_id, manifest=manifest,
                    launch=_managed_launch(app_id, "reconcile", manifest),
                    retry_interval=float(os.environ.get(
                        "APPMGR_RECONCILE_RETRY", "1.0")))
                results.append(result)
                after = result.get("observed_state")
                action = result.get("action")
                if (after != before or action in
                        ("restarted", "restart_failed", "crash_loop")):
                    _audit("reconcile", id=app_id, before=before,
                           observed=after, result=action,
                           reason=result.get("reason"))
                    _operation_manager().events.publish(
                        "app", app_id=app_id, action=action,
                        observed_state=after, reason=result.get("reason"))
        except BusyError:
            continue
        except Exception as exc:
            _audit("reconcile_failed", id=app_id, error=str(exc))
            results.append({"id": app_id, "action": "error",
                            "reason": str(exc)})
    return results


def _reconcile_loop(stop_event: threading.Event, interval: float) -> None:
    while not stop_event.wait(max(0.1, interval)):
        _reconcile_once()


def _start_reconciler() -> None:
    global _reconcile_stop, _reconcile_thread
    if _reconcile_thread is not None and _reconcile_thread.is_alive():
        return
    _reconcile_stop = threading.Event()
    interval = float(os.environ.get("APPMGR_RECONCILE_INTERVAL", "1.0"))
    _reconcile_thread = threading.Thread(
        target=_reconcile_loop, args=(_reconcile_stop, interval), daemon=True,
        name="appmgr-lifecycle-reconciler")
    _reconcile_thread.start()


def _stop_reconciler() -> None:
    global _reconcile_stop, _reconcile_thread
    if _reconcile_stop is not None:
        _reconcile_stop.set()
    thread, _reconcile_thread = _reconcile_thread, None
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=2.0)
    _reconcile_stop = None


def serve(host: str = None, port: int = None) -> None:
    global _result_gateway_instance, _result_hub_instance
    global _visualization_bridge_instance
    global _operation_manager_instance
    host = host or paths.HTTP_HOST
    port = port or paths.HTTP_PORT
    if not _acquire_single_instance():
        raise SystemExit("appmgr already running (single-instance lock held)")
    try:
        expired_uploads = appuploads.gc_expired(include_active=True)
        if expired_uploads:
            _audit("upload_gc", removed=expired_uploads)
    except Exception as exc:
        _audit("upload_gc_failed", error=str(exc))
    # Reap app processes as they die. Without this the daemon leaks a
    # `[python] <defunct>` entry for every crashed app (it is their parent and
    # never called waitpid). The handler polls registered leaders and, when one
    # exits, immediately KILLs its captured PGID before queuing the status; the
    # visible bookkeeping (last_exit.json, run.pid/run.pgid/run.boot_id cleanup,
    # log line) happens in normal context from do_list/do_metrics/stop.
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
    coord = _coordinator()
    _result_hub_instance = canonical_results.ResultHub(
        ws_host=paths.RESULT_HUB_HOST,
        ws_port=paths.RESULT_HUB_PORT,
        system_uds_path=paths.SYSTEM_RESULT_SOCK,
        system_identity_resolver=canonical_results.resolve_builtin_notify_identity)
    try:
        _result_hub_instance.start()
        _visualization_bridge_instance = (
            appvisualization.DetectionOsdBridge().start())
        _result_hub_instance.add_observer(
            _visualization_bridge_instance.observe)
        _result_gateway_instance = resultgateway.ResultGateway(
            uds_path=paths.RESULT_GATEWAY_SOCK,
            ws_host=paths.RESULT_GATEWAY_HOST,
            ws_port=paths.RESULT_GATEWAY_PORT,
            identity_resolver=lambda peer_pid, claimed_app, instance_id, generation: (
                _resolve_result_identity(
                    coord, _result_hub_instance, peer_pid, claimed_app,
                    instance_id, generation)),
            canonical_publisher=_result_hub_instance.submit_app)
        try:
            _result_gateway_instance.start()
        except OSError as exc:
            # One-time migration from v1: a verified legacy active child can still
            # own :8124 while appmgr itself is restarting. Stop only that exact app,
            # then let desired-state boot restore relaunch it through the gateway.
            active = state.get_active()
            if getattr(exc, "errno", None) == errno.EADDRINUSE and active and \
                    supervisor.is_running(active) is not None:
                supervisor.stop(active)
                _result_gateway_instance.start()
            else:
                _result_gateway_instance = None
                raise
    except Exception:
        if _result_gateway_instance is not None:
            _result_gateway_instance.stop()
            _result_gateway_instance = None
        if _visualization_bridge_instance is not None:
            if _result_hub_instance is not None:
                _result_hub_instance.remove_observer(
                    _visualization_bridge_instance.observe)
            _visualization_bridge_instance.close()
            _visualization_bridge_instance = None
        if _result_hub_instance is not None:
            _result_hub_instance.stop()
            _result_hub_instance = None
        raise
    httpd = None
    try:
        httpd = _AppHTTPServer((host, port), _Handler)
        print(f"[appmgr] listening on http://{host}:{port}", flush=True)
        print("[appmgr] result gateway on unix://%s -> ws://%s:%s" %
              (paths.RESULT_GATEWAY_SOCK, paths.RESULT_GATEWAY_HOST,
               _result_gateway_instance.ws_port), flush=True)
        print("[appmgr] result hub on unix://%s -> ws://%s:%s" %
              (paths.SYSTEM_RESULT_SOCK, paths.RESULT_HUB_HOST,
               _result_hub_instance.ws_port), flush=True)
        # Boot-restore after both public endpoints are bound.  Resume every
        # desired app independently; a failed app does not block HTTP or peers.
        _boot_restore()
        _start_reconciler()
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _stop_reconciler()
        if httpd is not None:
            httpd.server_close()
        if _result_gateway_instance is not None:
            _result_gateway_instance.stop()
            _result_gateway_instance = None
        if _visualization_bridge_instance is not None:
            if _result_hub_instance is not None:
                _result_hub_instance.remove_observer(
                    _visualization_bridge_instance.observe)
            _visualization_bridge_instance.close()
            _visualization_bridge_instance = None
        if _result_hub_instance is not None:
            _result_hub_instance.stop()
            _result_hub_instance = None
        if _operation_manager_instance is not None:
            _operation_manager_instance.close()
            _operation_manager_instance = None

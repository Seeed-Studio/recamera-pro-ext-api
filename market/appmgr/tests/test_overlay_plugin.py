"""Sandboxed overlay plugins (overlay phase-2 spec, appmgr side).

Covers the three server-side gates a plugin's bytes pass through:

  * manifest -- closed ``ui = {overlay: {entry, sha256}}`` schema, mirrored in
    manifest-v2.schema.json;
  * route -- GET /api/app-center/v1/apps/<id>/overlay: 404 unless installed and
    declared, mandatory ?h= (400) bound to the served bytes (409), no-follow
    regular-file reads under a 256 KiB cap, text/plain + nosniff + no-store +
    ETag headers so top-level navigation can never execute the document;
  * installer -- the declared entry must exist in the package, be a regular
    file, fit the cap and match its declared digest, or the install is
    rejected with an explicit reason.
"""
from __future__ import annotations

import hashlib
import http.client
import io
import json
import os
import sys
import tarfile
import threading

import pytest
from jsonschema import Draft202012Validator

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
from appmgr import installer, manifest as contract, paths, server  # noqa: E402


APP_ID = "plugin-app"
OVERLAY_DOC = (b"<!doctype html><html><head><meta charset=\"utf-8\"></head>"
               b"<body><script>/* plugin */</script></body></html>")
GOOD_SHA = hashlib.sha256(OVERLAY_DOC).hexdigest()


def overlay_ui(entry="web/overlay.html", sha=GOOD_SHA):
    return {"overlay": {"entry": entry, "sha256": sha}}


def minimal_manifest(**updates):
    value = {
        "manifest_version": 2,
        "id": APP_ID,
        "name": "Plugin App",
        "version": "1.2.3",
        "type": "self-hosted",
        "entry": "src/main.py",
        "release": {"sequence": 7, "channel": "stable"},
        "compatibility": {
            "platform_profile": contract.DEFAULT_PLATFORM_PROFILE,
            "arch": "aarch64",
            "python": "==3.11.*",
        },
        "python": {
            "runtime_profile": "system-cp311-rknn232",
            "isolation": "per-release",
            "wheels": [],
            "imports": [],
        },
        "artifacts": [],
        "config_schema": {"revision": 1, "groups": []},
        "resources": {"claims": [], "limits": {"memory_mb": 128}},
        "permissions": {
            "sdk": [],
            "filesystem": {"read": ["app"], "write": ["appdata", "tmp"]},
            "network": {"listen": [], "outbound": []},
        },
        "health": {
            "protocol": "kit-health-v1",
            "startup_timeout_sec": 30,
            "stabilization_sec": 2,
            "liveness_interval_sec": 10,
            "liveness_failures": 3,
            "restart": {
                "policy": "on-failure",
                "max_attempts": 3,
                "window_sec": 60,
                "backoff_sec": [1, 2, 5],
            },
        },
        "instances": {
            "max": 1,
            "config_scope": "app",
            "data_scope": "app",
            "endpoint_mode": "allocated",
        },
        "capabilities": [],
    }
    value.update(updates)
    return value


def schema_errors(value):
    schema_path = os.path.join(
        os.path.dirname(contract.__file__), "schema", "manifest-v2.schema.json")
    with open(schema_path, encoding="utf-8") as source:
        return list(Draft202012Validator(json.load(source)).iter_errors(value))


# --------------------------------------------------------------------------
# manifest validation


def test_manifest_accepts_valid_ui_overlay():
    value = minimal_manifest(ui=overlay_ui())
    assert contract.validate_manifest(value) == 2
    assert schema_errors(value) == []
    assert contract.validate_overlay_declaration(value["ui"]) == {
        "entry": "web/overlay.html", "sha256": GOOD_SHA}


@pytest.mark.parametrize("ui", [
    {"overlay": {"entry": "web/overlay.html"}},                       # missing sha
    {"overlay": {"entry": "web/overlay.html", "sha256": "a" * 63}},   # short
    {"overlay": {"entry": "web/overlay.html", "sha256": "a" * 65}},   # long
    {"overlay": {"entry": "web/overlay.html", "sha256": "A" * 64}},   # uppercase
    {"overlay": {"entry": "web/../secret.html", "sha256": GOOD_SHA}},
    {"overlay": {"entry": "web/a/../../secret.html", "sha256": GOOD_SHA}},
    {"overlay": {"entry": "web/overlay.htm", "sha256": GOOD_SHA}},    # wrong ext
    {"overlay": {"entry": "web/overlay.html.txt", "sha256": GOOD_SHA}},
    {"overlay": {"entry": "/web/overlay.html", "sha256": GOOD_SHA}},  # absolute
    {"overlay": {"entry": "assets/overlay.html", "sha256": GOOD_SHA}},
    {"overlay": {"entry": "web/" + "x" * 121 + ".html",
                 "sha256": GOOD_SHA}},                                # too long
    {"overlay": {"entry": "web/overlay.html", "sha256": GOOD_SHA,
                 "defer": True}},                                     # unknown key
    {"overlay": {"entry": "web/overlay.html", "sha256": GOOD_SHA},
     "preload": True},                                                # unknown key
    {"overlay": "web/overlay.html"},                                  # not object
    {},                                                               # missing overlay
    "web/overlay.html",                                               # ui not object
])
def test_manifest_rejects_bad_ui_declarations(ui):
    value = minimal_manifest(ui=ui)
    with pytest.raises(contract.ManifestValidationError):
        contract.validate_manifest(value)
    with pytest.raises(contract.ManifestValidationError):
        contract.validate_overlay_declaration(ui)


def test_manifest_without_ui_is_unaffected_and_schema_stays_closed():
    value = minimal_manifest()
    assert contract.validate_manifest(value) == 2
    assert schema_errors(value) == []
    closed = minimal_manifest(ui={"overlay": {"entry": "web/overlay.html",
                                              "sha256": GOOD_SHA},
                                 "theme": "dark"})
    assert schema_errors(closed)


# --------------------------------------------------------------------------
# HTTP route


@pytest.fixture
def layout(tmp_path, monkeypatch):
    apps = tmp_path / "apps"
    appmgr = tmp_path / "appmgr"
    appdata = tmp_path / "appdata"
    venvs = tmp_path / "venvs"
    for directory in (apps, appmgr, appdata, venvs):
        directory.mkdir()
    monkeypatch.setattr(paths, "APPS_DIR", str(apps))
    monkeypatch.setattr(paths, "APPMGR_DIR", str(appmgr))
    monkeypatch.setattr(paths, "APPDATA_DIR", str(appdata))
    monkeypatch.setattr(paths, "VENVS_DIR", str(venvs))
    monkeypatch.setattr(paths, "STATE_FILE", str(apps / "state.json"))
    monkeypatch.setattr(paths, "ALLOWED_PKG_ROOTS", (str(tmp_path),))
    monkeypatch.setattr(paths, "REQUIRE_SIGNATURE", False)
    monkeypatch.setattr(server, "_result_hub_instance", None)
    monkeypatch.setattr(server, "_coordinator_instance", None)
    monkeypatch.setattr(server, "_coordinator_layout", None)
    if server._operation_manager_instance is not None:
        server._operation_manager_instance.close()
    monkeypatch.setattr(server, "_operation_manager_instance", None)
    monkeypatch.setattr(server, "_operation_manager_layout", None)
    server.cache_clear()
    yield tmp_path
    if server._operation_manager_instance is not None:
        server._operation_manager_instance.close()
        server._operation_manager_instance = None


@pytest.fixture
def httpd(layout):
    from http.server import ThreadingHTTPServer
    instance = ThreadingHTTPServer(("127.0.0.1", 0), server._Handler)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield instance
    instance.shutdown()
    instance.server_close()
    thread.join(timeout=5)


def _get(httpd, path):
    connection = http.client.HTTPConnection(
        "127.0.0.1", httpd.server_port, timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read()
        return response.status, dict(response.getheaders()), body
    finally:
        connection.close()


def _install_tree(ui=None, overlay_bytes=OVERLAY_DOC, entry=None):
    """Write an installed app tree directly (route tests skip the installer)."""
    directory = paths.app_dir(APP_ID)
    os.makedirs(os.path.join(directory, "web"), exist_ok=True)
    manifest = minimal_manifest()
    if ui is not None:
        manifest["ui"] = ui
    if entry is not None:
        manifest["ui"] = ui = overlay_ui(entry=entry)
    with open(os.path.join(directory, "manifest.json"), "w") as stream:
        json.dump(manifest, stream)
    if overlay_bytes is not None:
        with open(os.path.join(directory, "web", "overlay.html"), "wb") as sink:
            sink.write(overlay_bytes)
    server.cache_clear()
    return directory


def test_route_serves_declared_entry_with_contract_headers(httpd):
    _install_tree(ui=overlay_ui())
    status, headers, body = _get(
        httpd, f"/api/app-center/v1/apps/{APP_ID}/overlay?h={GOOD_SHA}")
    assert status == 200
    assert body == OVERLAY_DOC
    assert headers["Content-Type"] == "text/plain; charset=utf-8"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Cache-Control"] == "no-store"
    assert headers["ETag"] == GOOD_SHA[:16]


def test_route_requires_h_and_rejects_malformed_hashes(httpd):
    _install_tree(ui=overlay_ui())
    base = f"/api/app-center/v1/apps/{APP_ID}/overlay"
    for suffix in ("", "?h=", "?h=xyz", "?h=" + "A" * 64, "?h=" + "0" * 63,
                   "?h=" + "0" * 64 + "&h=" + "0" * 64):
        status, headers, body = _get(httpd, base + suffix)
        assert status == 400, suffix
        assert headers["Content-Type"] == "application/json"
        assert "error" in json.loads(body)


def test_route_returns_409_on_hash_mismatch(httpd):
    _install_tree(ui=overlay_ui())
    status, headers, body = _get(
        httpd, f"/api/app-center/v1/apps/{APP_ID}/overlay?h={'b' * 64}")
    assert status == 409
    assert headers["Content-Type"] == "application/json"
    assert "hash does not match" in json.loads(body)["error"]


def test_route_serves_upgraded_bytes_and_rejects_stale_hash(httpd):
    _install_tree(ui=overlay_ui())
    replacement = OVERLAY_DOC + b"<b>v2</b>"
    with open(os.path.join(paths.app_dir(APP_ID), "web", "overlay.html"),
              "wb") as sink:
        sink.write(replacement)
    server.cache_clear()
    status, headers, body = _get(
        httpd, f"/api/app-center/v1/apps/{APP_ID}/overlay?h={GOOD_SHA}")
    assert status == 409
    assert body != replacement
    new_sha = hashlib.sha256(replacement).hexdigest()
    status, _headers, body = _get(
        httpd, f"/api/app-center/v1/apps/{APP_ID}/overlay?h={new_sha}")
    assert status == 200
    assert body == replacement


def test_route_404_without_install_or_without_declaration(httpd):
    good = f"/api/app-center/v1/apps/{APP_ID}/overlay?h={GOOD_SHA}"
    assert _get(httpd, good)[0] == 404                    # not installed
    _install_tree(ui=None)
    assert _get(httpd, good)[0] == 404                    # installed, no ui
    _install_tree(ui={"other": {}})
    assert _get(httpd, good)[0] == 404                    # ui without overlay


def test_route_404_on_tampered_declaration_and_missing_entry(httpd):
    good = f"/api/app-center/v1/apps/{APP_ID}/overlay?h={GOOD_SHA}"
    # traversal entry smuggled into the installed manifest
    _install_tree(entry="web/../../secret.html")
    assert _get(httpd, good)[0] == 404
    # entry outside web/ (schema-invalid corruption)
    _install_tree(entry="assets/evil.html")
    assert _get(httpd, good)[0] == 404
    # declared but absent on disk
    _install_tree(ui=overlay_ui())
    os.unlink(os.path.join(paths.app_dir(APP_ID), "web", "overlay.html"))
    server.cache_clear()
    assert _get(httpd, good)[0] == 404


def test_route_404_on_symlinked_or_non_regular_entry(httpd):
    outside = paths.APPMGR_DIR + "-outside.html"
    with open(outside, "wb") as sink:
        sink.write(b"evil")
    _install_tree(ui=overlay_ui(), overlay_bytes=None)
    entry_path = os.path.join(paths.app_dir(APP_ID), "web", "overlay.html")
    os.symlink(outside, entry_path)
    server.cache_clear()
    assert _get(
        httpd,
        f"/api/app-center/v1/apps/{APP_ID}/overlay?h="
        + hashlib.sha256(b"evil").hexdigest())[0] == 404

    os.unlink(entry_path)
    os.mkfifo(entry_path)
    server.cache_clear()
    assert _get(httpd, f"/api/app-center/v1/apps/{APP_ID}/overlay?h="
                + "0" * 64)[0] == 404


def test_route_404_when_entry_exceeds_cap(httpd):
    big = b"x" * 4096
    _install_tree(ui=overlay_ui(sha=hashlib.sha256(big).hexdigest()),
                  overlay_bytes=big)
    monkey_cap = 32
    saved = paths.MAX_OVERLAY_BYTES
    paths.MAX_OVERLAY_BYTES = monkey_cap
    try:
        server.cache_clear()
        status, _headers, _body = _get(
            httpd, f"/api/app-center/v1/apps/{APP_ID}/overlay?h="
            + hashlib.sha256(big).hexdigest())
        assert status == 404
    finally:
        paths.MAX_OVERLAY_BYTES = saved
        server.cache_clear()


def test_route_rejects_encoded_separators_and_bad_ids(httpd):
    for path in ("/api/app-center/v1/apps/x%2Fy%2F..%2Fz/overlay?h=" + GOOD_SHA,
                 "/api/app-center/v1/apps/%2E%2E/overlay?h=" + GOOD_SHA,
                 f"/api/app-center/v1/apps/{APP_ID}/overlay/extra?h={GOOD_SHA}"):
        assert _get(httpd, path)[0] == 404
    assert _get(httpd, "/api/app-center/v1/apps/Overlay-App/overlay?h="
                + GOOD_SHA)[0] == 404     # id case is whitelisted lowercase


def test_do_overlay_function_contract(httpd):
    _install_tree(ui=overlay_ui())
    data, digest = server.do_overlay(APP_ID)
    assert data == OVERLAY_DOC
    assert digest == GOOD_SHA
    with pytest.raises(ValueError):
        server.do_overlay("../evil")
    with pytest.raises(FileNotFoundError):
        server.do_overlay("never-installed")


# --------------------------------------------------------------------------
# installer member validation


@pytest.fixture
def install_layout(layout, monkeypatch):
    monkeypatch.setattr(paths, "REQUIRE_SIGNATURE", True)
    monkeypatch.setenv("APPMGR_PLATFORM_ARCH", "aarch64")
    monkeypatch.setenv("APPMGR_PLATFORM_PYTHON", sys.executable)
    monkeypatch.setenv("APPMGR_WHEELHOUSE_DIR", str(layout / "wheelhouse"))
    return layout


def _records(files):
    return {name: {"sha256": hashlib.sha256(data).hexdigest(),
                   "size": len(data)}
            for name, data in files.items()}


def _make_metadata(man, records):
    """make_release_metadata minus its validate_package_files gate, so tests
    can build self-consistent packages whose ui.overlay binding is broken."""
    bom = contract._bom_bytes(records)
    bom_sha = hashlib.sha256(bom).hexdigest()
    manifest_sha = records["manifest.json"]["sha256"]
    identity = hashlib.sha256(
        (manifest_sha + bom_sha).encode("ascii")).hexdigest()[:16]
    lock = {
        "lock_version": contract.RELEASE_LOCK_VERSION,
        "manifest_version": contract.MANIFEST_VERSION,
        "app": {"id": man["id"], "version": man["version"]},
        "release": dict(man["release"]),
        "release_id": f"{man['version']}-{identity}",
        "manifest_sha256": manifest_sha,
        "bom": {
            "path": contract.BOM_PATH,
            "sha256": bom_sha,
            "entries": len(records),
            "payload_bytes": sum(r["size"] for r in records.values()),
        },
        "compatibility": dict(man["compatibility"]),
        "python": json.loads(json.dumps(man["python"])),
        "artifacts": json.loads(json.dumps(man["artifacts"])),
    }
    return lock, bom


def build_pkg(path, man, *, tar_files=None, omit=(), raw_members=(),
              metadata_files=None):
    """Build a real v2 .tar.gz under `path`.

    `tar_files`: {arcname: bytes} actually written; `omit`: names present in
    the declared payload but skipped when writing (missing-file case);
    `raw_members`: [(TarInfo, bytes|None)] added verbatim (link case);
    `metadata_files`: payload the BOM/lock are computed over when the tar must
    diverge from a valid declaration.
    """
    base = {"manifest.json": contract.canonical_json(man),
            "src/main.py": b"# app\n"}
    base.update(tar_files or {})
    metadata_source = dict(base)
    metadata_source.update(metadata_files or {})
    records = _records(metadata_source)
    lock, bom = _make_metadata(man, records)
    payload = {k: v for k, v in base.items() if k not in omit}
    payload[contract.BOM_PATH] = bom
    payload[contract.RELEASE_LOCK_PATH] = contract.canonical_json(lock)
    with tarfile.open(path, "w:gz") as archive:
        for name, data in sorted(payload.items()):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(data))
        for info, data in raw_members:
            archive.addfile(info, io.BytesIO(data) if data is not None
                            and not info.isdir() and not info.issym()
                            and not info.islnk() else None)
    return path


def overlay_manifest(**ui_updates):
    ui = overlay_ui()
    ui["overlay"].update(ui_updates.pop("overlay_extra", {}))
    ui.update(ui_updates.pop("ui_extra", {}))
    return minimal_manifest(ui=ui)


def test_installer_accepts_valid_overlay_package(install_layout):
    pkg = build_pkg(str(install_layout / "good.tar.gz"),
                    overlay_manifest(),
                    tar_files={"web/overlay.html": OVERLAY_DOC})
    app_id, manifest = installer.install(pkg, allow_unsigned=True)
    assert app_id == APP_ID
    installed = os.path.join(paths.app_dir(APP_ID), "web", "overlay.html")
    assert open(installed, "rb").read() == OVERLAY_DOC
    assert manifest["ui"] == overlay_ui()


def test_installer_rejects_missing_entry_file(install_layout):
    pkg = build_pkg(str(install_layout / "missing.tar.gz"),
                    overlay_manifest(),
                    omit=("web/overlay.html",))
    with pytest.raises(installer.InstallError, match="package is missing"):
        installer.install(pkg, allow_unsigned=True)
    assert not os.path.isdir(paths.app_dir(APP_ID))


def test_installer_rejects_symlinked_entry(install_layout):
    link = tarfile.TarInfo("web/overlay.html")
    link.type = tarfile.SYMTYPE
    link.linkname = "../../etc/hostname"
    link.mode = 0o777
    declared = b"# declared bytes for the bom\n"
    pkg = build_pkg(
        str(install_layout / "symlink.tar.gz"), overlay_manifest(),
        metadata_files={"web/overlay.html": declared},
        raw_members=[(link, None)])
    with pytest.raises(installer.InstallError,
                       match="unsafe member \\(sym/hard link\\)"):
        installer.install(pkg, allow_unsigned=True)
    assert not os.path.isdir(paths.app_dir(APP_ID))


def test_installer_rejects_oversize_entry(install_layout, monkeypatch):
    big = b"y" * 4096
    monkeypatch.setattr(contract, "MAX_OVERLAY_BYTES", 32)
    pkg = build_pkg(
        str(install_layout / "oversize.tar.gz"),
        overlay_manifest(overlay_extra={
            "sha256": hashlib.sha256(big).hexdigest()}),
        tar_files={"web/overlay.html": big})
    with pytest.raises(installer.InstallError, match="exceeds 32 byte limit"):
        installer.install(pkg, allow_unsigned=True)
    assert not os.path.isdir(paths.app_dir(APP_ID))


def test_installer_rejects_digest_mismatch(install_layout):
    pkg = build_pkg(str(install_layout / "digest.tar.gz"),
                    overlay_manifest(overlay_extra={"sha256": "b" * 64}),
                    tar_files={"web/overlay.html": OVERLAY_DOC})
    with pytest.raises(installer.InstallError, match="digest does not match"):
        installer.install(pkg, allow_unsigned=True)
    assert not os.path.isdir(paths.app_dir(APP_ID))


def test_installer_rejects_hardlinked_entry(install_layout):
    link = tarfile.TarInfo("web/overlay.html")
    link.type = tarfile.LNKTYPE
    link.linkname = "src/main.py"
    declared = b"# declared\n"
    pkg = build_pkg(
        str(install_layout / "hardlink.tar.gz"), overlay_manifest(),
        metadata_files={"web/overlay.html": declared},
        raw_members=[(link, None)])
    with pytest.raises(installer.InstallError,
                       match="unsafe member \\(sym/hard link\\)"):
        installer.install(pkg, allow_unsigned=True)


def test_installer_still_rejects_packages_without_ui_unchanged(install_layout):
    pkg = build_pkg(str(install_layout / "plain.tar.gz"), minimal_manifest())
    app_id, manifest = installer.install(pkg, allow_unsigned=True)
    assert app_id == APP_ID
    assert "ui" not in manifest

"""
Gallery presentation pipes: app icons (P0-1) and i18n copy (P0-2).
RENDER_DECLARATION_SPEC §5.

The two bugs these lock down -- both "the data was already in the manifest, the
transport was broken":

  P0-1  manifest says `"image": "/appcenter/apps/<id>.png"`, but that nginx alias
        serves /userdata/local/appcenter/apps/ (install packages + catalog only)
        while the app unpacks to /userdata/local/apps/<id>/ -- so the URL 404s on
        every device. The front end could therefore only draw cards for the ids
        hard-coded in its own bundle; a third-party app never got a card image.
        Fix: packages may ship a top-level `icon.<ext>`; the installer keeps it
        (raster-only whitelist + size cap), appmgr serves it at
        GET /api/appMgr/icon?id=<id>, and /list hands out that URL as `icon_url`.

  P0-2  do_list() dropped name_zh / description_zh / scene_zh, while
        _builtin_entry() in the SAME file passed them through -- so the built-in
        entry was bilingual and every installed app was not.

Everything runs on temp dirs against real .tar.gz packages, so the installer's
member vetting is exercised for real.
"""
import base64
import hashlib
import http.client
import json
import os
import shutil
import sys
import tarfile
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

_BASE = os.path.realpath(tempfile.mkdtemp(prefix="appmgr-gallery."))
_APPS = os.path.join(_BASE, "apps")
_APPMGR = os.path.join(_BASE, "appmgr")
_APPDATA = os.path.join(_BASE, "appdata")
_PKGS = os.path.join(_BASE, "pkgs")
os.environ["APPMGR_APPS_DIR"] = _APPS
os.environ["APPMGR_DIR"] = _APPMGR
os.environ["APPMGR_APPDATA_DIR"] = _APPDATA
os.environ["APPMGR_ALLOWED_ROOTS"] = _BASE
os.environ["APPMGR_REQUIRE_SIGNATURE"] = "0"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from appmgr import installer, paths, server  # noqa: E402

APP = "demo-app"

# A real 1x1 PNG (68 bytes) -- content is never parsed, but using genuine bytes
# keeps the Content-Type assertion honest.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmM"
    "IQAAAABJRU5ErkJggg==")
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 24


def manifest(version="1.0.0", **extra):
    man = {"id": APP, "version": version, "name": "Demo App",
           "description": "an English description",
           "image": "/appcenter/apps/demo-app.png"}
    man.update(extra)
    return man


def make_pkg(version="1.0.0", man=None, files=None, raw_members=None):
    """Build a real .tar.gz under an allowed root.

    `files`: {arcname: bytes} written normally.
    `raw_members`: [(TarInfo, bytes)] added verbatim -- used to forge hostile
    members (traversal, oversized) that a normal filesystem write can't express.
    """
    os.makedirs(_PKGS, exist_ok=True)
    src = tempfile.mkdtemp(prefix="src.", dir=_PKGS)
    with open(os.path.join(src, "manifest.json"), "w") as f:
        json.dump(man if man is not None else manifest(version), f)
    with open(os.path.join(src, "app.py"), "wb") as f:
        f.write(b"# app\n")
    for name, body in (files or {}).items():
        p = os.path.join(src, name)
        os.makedirs(os.path.dirname(p) or src, exist_ok=True)
        with open(p, "wb") as f:
            f.write(body)
    pkg = os.path.join(_PKGS, f"{APP}-{version}-{os.path.basename(src)}.tar.gz")
    with tarfile.open(pkg, "w:gz") as tar:
        for root, _dirs, fs in os.walk(src):
            for fn in sorted(fs):
                ap = os.path.join(root, fn)
                tar.add(ap, arcname=os.path.relpath(ap, src))
        for ti, body in (raw_members or []):
            tar.addfile(ti, __import__("io").BytesIO(body))
    return pkg


class _Pinned(unittest.TestCase):
    """paths.py snapshots the layout from the env AT IMPORT TIME and a sibling
    test module may have imported it first, so pin the constants per-test."""
    _PIN = {"APPS_DIR": _APPS, "APPMGR_DIR": _APPMGR, "APPDATA_DIR": _APPDATA,
            "ALLOWED_PKG_ROOTS": (_BASE,), "REQUIRE_SIGNATURE": False,
            "STATE_FILE": os.path.join(_APPS, "state.json")}

    def setUp(self):
        self._saved = {k: getattr(paths, k) for k in self._PIN}
        for k, v in self._PIN.items():
            setattr(paths, k, v)
        for d in (_APPS, _APPMGR, _APPDATA):
            os.makedirs(d, exist_ok=True)
        for d in (paths.app_dir(APP), paths.app_dir(APP) + ".prev"):
            shutil.rmtree(d, ignore_errors=True)
        paths.ensure_dirs()
        server.cache_clear()

    def tearDown(self):
        server.cache_clear()
        for k, v in self._saved.items():
            setattr(paths, k, v)

    def _entry(self, app_id=APP):
        for e in server.do_list()["apps"]:
            if e["id"] == app_id:
                return e
        self.fail(f"{app_id} not in do_list()")

    def _write_declared_icon(self, relative_path="assets/icon.png",
                             media_type="image/png", payload=PNG):
        """Create an installed-tree fixture for runtime tamper/symlink tests."""
        app_dir = paths.app_dir(APP)
        os.makedirs(app_dir, exist_ok=True)
        declared = {"path": relative_path, "media_type": media_type}
        with open(os.path.join(app_dir, "manifest.json"), "w") as sink:
            json.dump(manifest(
                manifest_version=2, icon=declared), sink)
        icon_path = os.path.join(app_dir, *relative_path.split("/"))
        os.makedirs(os.path.dirname(icon_path), exist_ok=True)
        if payload is not None:
            with open(icon_path, "wb") as sink:
                sink.write(payload)
        server.cache_clear()
        return declared


# --------------------------------------------------------------------------- #
# P0-1  icon: package -> install dir -> endpoint
# --------------------------------------------------------------------------- #
class IconPipeTests(_Pinned):
    def test_bundled_png_survives_install_and_is_served(self):
        installer.install(make_pkg(files={"icon.png": PNG}))
        on_disk = os.path.join(paths.app_dir(APP), "icon.png")
        self.assertTrue(os.path.isfile(on_disk), "icon.png must land in the app dir")
        self.assertEqual(open(on_disk, "rb").read(), PNG)

        entry = self._entry()
        digest = hashlib.sha256(PNG).hexdigest()[:16]
        self.assertEqual(
            entry["icon_url"],
            f"/api/appMgr/icon?id=demo-app&v=1.0.0&h={digest}")
        # and the URL it advertises really resolves
        data, ctype = server.do_icon(APP)
        self.assertEqual(data, PNG)
        self.assertEqual(ctype, "image/png")

    def test_webp_content_type(self):
        installer.install(make_pkg(files={"icon.webp": WEBP}))
        data, ctype = server.do_icon(APP)
        self.assertEqual(ctype, "image/webp")
        self.assertEqual(data, WEBP)

    def test_jpg_content_type(self):
        installer.install(make_pkg(files={"icon.jpg": b"\xff\xd8\xff\xe0jpegish"}))
        self.assertEqual(server.do_icon(APP)[1], "image/jpeg")

    def test_package_without_icon_yields_null_and_404_not_an_error(self):
        installer.install(make_pkg())
        entry = self._entry()
        self.assertIsNone(entry["icon_url"])
        # the list call itself must not blow up, and the rest of the entry is fine
        self.assertEqual(entry["name"], "Demo App")
        with self.assertRaises(FileNotFoundError):
            server.do_icon(APP)

    def test_unknown_or_invalid_id(self):
        with self.assertRaises(FileNotFoundError):
            server.do_icon("never-installed")
        for bad in ("../../etc/passwd", "Demo_App", "", "a/b"):
            with self.assertRaises(ValueError):
                server.do_icon(bad)

    def test_icon_url_busts_cache_on_upgrade(self):
        installer.install(make_pkg("1.0.0", files={"icon.png": PNG}))
        first = self._entry()["icon_url"]
        installer.install(make_pkg("2.0.0", man=manifest("2.0.0"),
                                   files={"icon.png": PNG + b"x"}))
        second = self._entry()["icon_url"]
        self.assertNotEqual(first, second)
        self.assertIn("v=2.0.0", second)

    def test_same_version_reinstall_with_new_icon_gets_new_content_url(self):
        installer.install(make_pkg("1.0.0", files={"icon.png": PNG}))
        first = self._entry()["icon_url"]

        replacement = PNG + b"replacement"
        installer.install(make_pkg("1.0.0", files={"icon.png": replacement}))
        second = self._entry()["icon_url"]

        self.assertNotEqual(first, second)
        self.assertIn("v=1.0.0", second)
        self.assertIn(hashlib.sha256(replacement).hexdigest()[:16], second)
        self.assertEqual(server.do_icon(APP)[0], replacement)

    def test_declared_nested_icon_is_authoritative_and_listed(self):
        declared = self._write_declared_icon()

        entry = self._entry()
        self.assertEqual(entry["icon"], declared)
        self.assertIn(hashlib.sha256(PNG).hexdigest()[:16], entry["icon_url"])
        self.assertEqual(server.do_icon(APP), (PNG, "image/png"))
        v1_entry = next(
            item for item in server.do_v1_apps()["apps"] if item["id"] == APP)
        self.assertEqual(v1_entry["icon"], declared)
        self.assertEqual(v1_entry["icon_url"], entry["icon_url"])
        self.assertEqual(v1_entry["manifest"]["icon"], declared)

    def test_missing_declared_icon_fails_closed_without_legacy_fallback(self):
        declared = self._write_declared_icon(payload=None)
        with open(os.path.join(paths.app_dir(APP), "icon.png"), "wb") as sink:
            sink.write(PNG)
        server.cache_clear()

        self.assertEqual(self._entry()["icon"], declared)
        self.assertIsNone(self._entry()["icon_url"])
        with self.assertRaises(FileNotFoundError):
            server.do_icon(APP)

    def test_declared_icon_magic_mismatch_fails_closed(self):
        self._write_declared_icon(
            relative_path="assets/icon.jpg", media_type="image/jpeg",
            payload=PNG)

        self.assertIsNone(self._entry()["icon_url"])
        with self.assertRaises(FileNotFoundError):
            server.do_icon(APP)

    def test_declared_icon_rejects_final_and_parent_symlinks(self):
        outside = tempfile.mkdtemp(prefix="outside-icon.", dir=_BASE)
        self.addCleanup(shutil.rmtree, outside, True)
        outside_icon = os.path.join(outside, "outside.png")
        with open(outside_icon, "wb") as sink:
            sink.write(PNG)

        for parent_link in (False, True):
            with self.subTest(parent_link=parent_link):
                shutil.rmtree(paths.app_dir(APP), ignore_errors=True)
                self._write_declared_icon(payload=None)
                assets = os.path.join(paths.app_dir(APP), "assets")
                if parent_link:
                    shutil.rmtree(assets)
                    os.symlink(outside, assets)
                else:
                    os.symlink(outside_icon, os.path.join(assets, "icon.png"))
                server.cache_clear()
                self.assertIsNone(self._entry()["icon_url"])
                with self.assertRaises(FileNotFoundError):
                    server.do_icon(APP)

    def test_declared_icon_rejects_fifo_and_runtime_oversize(self):
        self._write_declared_icon(payload=None)
        icon_path = os.path.join(paths.app_dir(APP), "assets", "icon.png")
        os.mkfifo(icon_path)
        server.cache_clear()
        self.assertIsNone(self._entry()["icon_url"])
        with self.assertRaises(FileNotFoundError):
            server.do_icon(APP)

        os.unlink(icon_path)
        saved = paths.MAX_ICON_BYTES
        paths.MAX_ICON_BYTES = 32
        try:
            with open(icon_path, "wb") as sink:
                sink.write(PNG)
            server.cache_clear()
            self.assertIsNone(self._entry()["icon_url"])
            with self.assertRaises(FileNotFoundError):
                server.do_icon(APP)
        finally:
            paths.MAX_ICON_BYTES = saved
            server.cache_clear()

    # -- hostile packages --------------------------------------------------- #
    def test_active_icon_type_is_refused(self):
        for name in ("icon.svg", "icon.html", "icon.js", "icon.php"):
            with self.assertRaises(installer.InstallError) as cm:
                installer.install(make_pkg(files={name: b"<svg onload=alert(1)/>"}))
            self.assertIn("unsupported icon type", str(cm.exception))
            self.assertFalse(os.path.exists(os.path.join(paths.app_dir(APP), name)))

    def test_oversized_icon_is_refused(self):
        saved = paths.MAX_ICON_BYTES
        paths.MAX_ICON_BYTES = 1024
        try:
            with self.assertRaises(installer.InstallError) as cm:
                installer.install(make_pkg(files={"icon.png": b"\x00" * 4096}))
            self.assertIn("icon too large", str(cm.exception))
        finally:
            paths.MAX_ICON_BYTES = saved
        self.assertFalse(os.path.isdir(paths.app_dir(APP)),
                         "a refused package must not be installed at all")

    def test_traversal_icon_member_is_refused(self):
        ti = tarfile.TarInfo("../evil-icon.png")
        ti.size = len(PNG)
        ti.mode = 0o644
        with self.assertRaises(installer.InstallError) as cm:
            installer.install(make_pkg(raw_members=[(ti, PNG)]))
        self.assertIn("zip-slip", str(cm.exception))
        self.assertFalse(os.path.exists(os.path.join(_APPS, "..", "evil-icon.png")))

    def test_nested_icon_is_not_governed_and_is_not_served(self):
        # assets/icon.svg is an ordinary package file: appmgr never serves it,
        # so it must not fail the install either.
        installer.install(make_pkg(files={"assets/icon.svg": b"<svg/>"}))
        self.assertTrue(os.path.isfile(
            os.path.join(paths.app_dir(APP), "assets", "icon.svg")))
        self.assertIsNone(self._entry()["icon_url"])
        with self.assertRaises(FileNotFoundError):
            server.do_icon(APP)


class IconHttpTests(_Pinned):
    """End-to-end through the real handler: status, Content-Type, cache header."""

    def setUp(self):
        super().setUp()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server._Handler)
        self.port = self.httpd.server_address[1]
        self.t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.t.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.t.join(timeout=5)
        super().tearDown()

    def _get(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", path)
        r = c.getresponse()
        body = r.read()
        hdrs = dict(r.getheaders())
        c.close()
        return r.status, hdrs, body

    def test_get_icon_ok(self):
        installer.install(make_pkg(files={"icon.png": PNG}))
        status, hdrs, body = self._get("/api/appMgr/icon?id=demo-app&v=1.0.0")
        self.assertEqual(status, 200)
        self.assertEqual(hdrs["Content-Type"], "image/png")
        self.assertEqual(hdrs["Content-Length"], str(len(PNG)))
        self.assertIn("max-age", hdrs["Cache-Control"])
        self.assertEqual(hdrs["X-Content-Type-Options"], "nosniff")
        self.assertEqual(body, PNG)

    def test_same_version_reinstall_rejects_old_hash_url(self):
        installer.install(make_pkg(files={"icon.png": PNG}))
        old_url = self._entry()["icon_url"]
        self.assertEqual(self._get(old_url)[0], 200)

        replacement = PNG + b"replacement"
        installer.install(make_pkg(files={"icon.png": replacement}))
        new_url = self._entry()["icon_url"]

        self.assertNotEqual(old_url, new_url)
        old_status, old_headers, old_body = self._get(old_url)
        self.assertEqual(old_status, 412)
        self.assertEqual(old_headers["Content-Type"], "application/json")
        self.assertIn("error", json.loads(old_body))
        new_status, _new_headers, new_body = self._get(new_url)
        self.assertEqual(new_status, 200)
        self.assertEqual(new_body, replacement)

    def test_icon_hash_parameter_is_strict_when_present(self):
        installer.install(make_pkg(files={"icon.png": PNG}))
        for suffix in ("", "xyz", "A" * 16, "0" * 15, "0" * 17,
                       "0" * 16 + "&h=" + "0" * 16):
            with self.subTest(suffix=suffix):
                status, _headers, _body = self._get(
                    "/api/appMgr/icon?id=demo-app&h=" + suffix)
                self.assertEqual(status, 400)

    def test_get_icon_missing_is_404_json(self):
        installer.install(make_pkg())
        status, hdrs, body = self._get("/api/appMgr/icon?id=demo-app")
        self.assertEqual(status, 404)
        self.assertEqual(hdrs["Content-Type"], "application/json")
        self.assertIn("error", json.loads(body))

    def test_declared_unsafe_or_mismatched_icons_are_http_404(self):
        outside = tempfile.mkdtemp(prefix="outside-http-icon.", dir=_BASE)
        self.addCleanup(shutil.rmtree, outside, True)
        outside_icon = os.path.join(outside, "outside.png")
        with open(outside_icon, "wb") as sink:
            sink.write(PNG)

        fixtures = ("symlink", "mismatch")
        for fixture in fixtures:
            with self.subTest(fixture=fixture):
                shutil.rmtree(paths.app_dir(APP), ignore_errors=True)
                if fixture == "symlink":
                    self._write_declared_icon(payload=None)
                    os.symlink(
                        outside_icon,
                        os.path.join(paths.app_dir(APP), "assets", "icon.png"))
                else:
                    self._write_declared_icon(
                        relative_path="assets/icon.jpg",
                        media_type="image/jpeg", payload=PNG)
                server.cache_clear()
                status, hdrs, body = self._get(
                    "/api/appMgr/icon?id=demo-app")
                self.assertEqual(status, 404)
                self.assertEqual(hdrs["Content-Type"], "application/json")
                self.assertIn("error", json.loads(body))

    def test_get_icon_bad_id_is_400(self):
        self.assertEqual(self._get("/api/appMgr/icon?id=..%2F..%2Fetc")[0], 400)
        self.assertEqual(self._get("/api/appMgr/icon")[0], 400)


# --------------------------------------------------------------------------- #
# P0-2  i18n passthrough
# --------------------------------------------------------------------------- #
class I18nPassthroughTests(_Pinned):
    ZH = {"name_zh": "演示应用", "description_zh": "中文描述", "scene_zh": "零售",
          "scene": "retail", "author": "Seeed"}

    def test_zh_fields_reach_do_list(self):
        installer.install(make_pkg(man=manifest(**self.ZH)))
        entry = self._entry()
        for k, v in self.ZH.items():
            self.assertEqual(entry.get(k), v, f"{k} dropped by do_list()")
        # the backend must NOT pick a language for the front end
        self.assertEqual(entry["name"], "Demo App")
        self.assertEqual(entry["description"], "an English description")

    def test_manifest_without_zh_lists_cleanly(self):
        installer.install(make_pkg())
        entry = self._entry()
        for k in ("name_zh", "description_zh", "scene_zh"):
            self.assertIsNone(entry.get(k), f"{k} should be null, not invented")
        self.assertEqual(entry["name"], "Demo App")

    def test_installed_entry_and_builtin_entry_expose_the_same_keys(self):
        """The original bug was an inconsistency between the two entry builders
        in the same file, so assert they agree on the presentation keys."""
        installer.install(make_pkg(man=manifest(**self.ZH)))
        listing = server.do_list()["apps"]
        installed = [e for e in listing if e["id"] == APP][0]
        builtin = [e for e in listing if e["type"] == "builtin"][0]
        keys = {"name", "name_zh", "description", "description_zh",
                "scene", "scene_zh", "image", "author", "render"}
        self.assertTrue(keys <= set(installed), keys - set(installed))
        self.assertTrue(keys <= set(builtin), keys - set(builtin))


# --------------------------------------------------------------------------- #
# render declaration passthrough (RENDER_DECLARATION_SPEC §4, lookup #2)
# --------------------------------------------------------------------------- #
class RenderPassthroughTests(_Pinned):
    RENDER = {"keypoints": {"layout": "facemesh468", "point_radius": 1},
              "events": {"blink": {"as": "toast", "duration_sec": 2}}}

    def test_render_block_reaches_do_list_verbatim(self):
        """appmgr carries the block; it never interprets a layout or `as`."""
        installer.install(make_pkg(man=manifest(render=self.RENDER)))
        self.assertEqual(self._entry()["render"], self.RENDER)

    def test_unknown_vocabulary_is_still_passed_through(self):
        """Forward compat: a primitive newer than this appmgr must not be
        filtered out on the way to the front end."""
        weird = {"events": {"x": {"as": "hologram", "spin": True}}}
        installer.install(make_pkg(man=manifest(render=weird)))
        self.assertEqual(self._entry()["render"], weird)

    def test_manifest_without_render_lists_null(self):
        installer.install(make_pkg())
        self.assertIsNone(self._entry()["render"])


# --------------------------------------------------------------------------- #
# gen_catalog.py passthrough (the pre-install listing the browser renders)
# --------------------------------------------------------------------------- #
_CATALOG_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "catalog")


def _load_gen_catalog():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "gen_catalog", os.path.join(_CATALOG_DIR, "gen_catalog.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class CatalogPassthroughTests(unittest.TestCase):
    def setUp(self):
        self.gc = _load_gen_catalog()
        self.dist = tempfile.mkdtemp(prefix="catalog-dist.", dir=_BASE)

    def _build(self, man):
        pkg = make_pkg(man=man)
        shutil.copy(pkg, os.path.join(self.dist, f"{man['id']}-{man['version']}-arm64.tar.gz"))
        return self.gc.build_catalog(self.dist, "/appcenter/apps/",
                                     os.path.join(_BASE, "nonexistent-models"),
                                     os.path.join(_BASE, "nonexistent-models.json"),
                                     None)

    def test_image_scene_author_and_zh_are_passed_through(self):
        man = manifest(name_zh="演示应用", description_zh="中文描述",
                       scene="retail", scene_zh="零售", author="Seeed")
        app = self._build(man)["apps"][0]
        self.assertEqual(app["image"], "/appcenter/apps/demo-app.png")
        self.assertEqual(app["scene"], "retail")
        self.assertEqual(app["scene_zh"], "零售")
        self.assertEqual(app["author"], "Seeed")
        self.assertEqual(app["name_zh"], "演示应用")
        self.assertEqual(app["description_zh"], "中文描述")
        # untouched originals
        self.assertEqual(app["name"], "Demo App")
        self.assertEqual(app["description"], "an English description")

    def test_absent_optional_keys_are_omitted_not_nulled(self):
        man = manifest()
        del man["image"]
        app = self._build(man)["apps"][0]
        for k in ("image", "scene", "scene_zh", "name_zh", "description_zh", "author"):
            self.assertNotIn(k, app)
        self.assertEqual(app["id"], APP)


if __name__ == "__main__":
    unittest.main()

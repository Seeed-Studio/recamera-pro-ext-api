"""
do_list() read-path caches + the reap/sweep throttle.

GET /api/appMgr/list is what the App Center page polls, and it had grown into
9 JSON parses + ~36 icon stats + a /proc walk + an HTTPS round-trip to entry.cgi
per call. The caches added to server.py cut that down -- and a cache that misses
an invalidation is worse than no cache, so almost everything here is an
INVALIDATION test:

  * manifest edited in place        -> next list shows the new content
  * app upgraded (real installer)   -> next list shows the new version
  * icon dropped in after install   -> next list grows an icon_url
  * icon removed                    -> next list drops it
  * app uninstalled                 -> entry (and its cache slot) disappear

Two mechanisms are pinned explicitly because they are the ones that break
silently:
  1. the stat key really is consulted (freeze the key -> stale content is served,
     which proves the cache is live rather than accidentally a no-op);
  2. the SETTLE WINDOW -- a rewrite within _SETTLE_SEC is never served from
     cache, because coarse filesystem mtime granularity plus an in-place rewrite
     (same inode, same size) can produce an identical key for different content.
"""
import json
import hashlib
import os
import shutil
import sys
import tarfile
import tempfile
import time
import unittest

_BASE = os.path.realpath(tempfile.mkdtemp(prefix="appmgr-cache."))
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
from appmgr import builtin, installer, paths, server, supervisor  # noqa: E402

APP = "cache-app"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 60
OLD = 10_000.0          # a timestamp far outside the settle window


def _manifest(version="1.0.0", **extra):
    man = {"id": APP, "version": version, "name": "Cache App",
           "description": "d", "image": f"/appcenter/apps/{APP}.png"}
    man.update(extra)
    return man


def _make_pkg(version="1.0.0", man=None, files=None):
    os.makedirs(_PKGS, exist_ok=True)
    src = tempfile.mkdtemp(prefix="src.", dir=_PKGS)
    with open(os.path.join(src, "manifest.json"), "w") as f:
        json.dump(man if man is not None else _manifest(version), f)
    with open(os.path.join(src, "app.py"), "wb") as f:
        f.write(b"# app\n")
    for name, body in (files or {}).items():
        p = os.path.join(src, name)
        os.makedirs(os.path.dirname(p) or src, exist_ok=True)
        with open(p, "wb") as f:
            f.write(body)
    pkg = os.path.join(_PKGS, f"{APP}-{version}-{os.path.basename(src)}.tar.gz")
    with tarfile.open(pkg, "w:gz") as tar:
        for root, _d, fs in os.walk(src):
            for fn in sorted(fs):
                ap = os.path.join(root, fn)
                tar.add(ap, arcname=os.path.relpath(ap, src))
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
        shutil.rmtree(paths.app_dir(APP), ignore_errors=True)
        shutil.rmtree(paths.app_dir(APP) + ".prev", ignore_errors=True)
        paths.ensure_dirs()
        server.cache_clear()
        # entry.cgi does not exist off-device; keep direct builtin cache tests
        # deterministic while the application-list tests assert it is not used.
        self._real_is_running = builtin.is_running
        builtin.is_running = lambda: False

    def tearDown(self):
        builtin.is_running = self._real_is_running
        server.cache_clear()
        for k, v in self._saved.items():
            setattr(paths, k, v)

    # -- helpers ---------------------------------------------------------- #
    def _entry(self, app_id=APP):
        for e in server.do_list()["apps"]:
            if e["id"] == app_id:
                return e
        return None

    def _write_manifest(self, man, mtime=OLD):
        """Write <app>/manifest.json and age it past the settle window."""
        d = paths.app_dir(APP)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "manifest.json")
        with open(p, "w") as f:
            json.dump(man, f)
        if mtime is not None:
            os.utime(p, (mtime, mtime))
            os.utime(d, (mtime, mtime))
        return p


# --------------------------------------------------------------------------- #
# the cache is really a cache (and really keyed on the stat)
# --------------------------------------------------------------------------- #
class ManifestCacheMechanicsTests(_Pinned):
    def test_frozen_stat_key_serves_the_cached_manifest(self):
        """Proves the cache is live: with the key pinned to an identical value,
        changed bytes are NOT re-read. (Never happens in practice -- an install
        changes the inode -- it is the control for the tests below.)"""
        p = self._write_manifest(_manifest("1.0.0", name="First"))
        self.assertEqual(self._entry()["name"], "First")
        with open(p, "w") as f:
            json.dump(_manifest("1.0.0", name="Zirst"), f)   # SAME byte length
        os.utime(p, (OLD, OLD))                               # SAME mtime, size, inode
        self.assertEqual(self._entry()["name"], "First", "cache was not consulted")

    def test_changed_mtime_invalidates(self):
        p = self._write_manifest(_manifest("1.0.0", name="First"))
        self.assertEqual(self._entry()["name"], "First")
        with open(p, "w") as f:
            json.dump(_manifest("1.0.0", name="Zirst"), f)
        os.utime(p, (OLD + 60, OLD + 60))
        self.assertEqual(self._entry()["name"], "Zirst")

    def test_rewrite_inside_the_settle_window_is_never_cached(self):
        """The dangerous case: an in-place rewrite keeps the inode, and a coarse
        filesystem clock can hand back the same mtime for both versions. Anything
        touched in the last _SETTLE_SEC must therefore be re-read."""
        p = self._write_manifest(_manifest("1.0.0", name="First"), mtime=None)
        self.assertEqual(self._entry()["name"], "First")
        for i in range(5):                       # tight loop, same second
            with open(p, "w") as f:
                json.dump(_manifest("1.0.0", name=f"Rev{i}"), f)
            self.assertEqual(self._entry()["name"], f"Rev{i}",
                             "a just-written manifest was served from cache")

    def test_uninstalled_app_drops_out_and_forgets_its_cache_slot(self):
        self._write_manifest(_manifest("1.0.0"))
        self.assertIsNotNone(self._entry())
        shutil.rmtree(paths.app_dir(APP))
        self.assertIsNone(self._entry(), "removed app still listed")
        self.assertNotIn(APP, server._manifest_cache)
        self.assertNotIn(APP, server._icon_cache)

    def test_corrupt_manifest_is_not_cached_and_recovers(self):
        p = self._write_manifest(_manifest("1.0.0"))
        self.assertIsNotNone(self._entry())
        with open(p, "w") as f:
            f.write("{not json")
        os.utime(p, (OLD + 60, OLD + 60))
        self.assertIsNone(self._entry(), "unparseable manifest must drop the entry")
        self.assertNotIn(APP, server._manifest_cache)
        self._write_manifest(_manifest("2.0.0"), mtime=OLD + 120)
        self.assertEqual(self._entry()["version"], "2.0.0")


# --------------------------------------------------------------------------- #
# invalidation through the REAL install path (dir swap -> new inode)
# --------------------------------------------------------------------------- #
class UpgradeInvalidationTests(_Pinned):
    def test_upgrade_is_visible_on_the_next_list(self):
        installer.install(_make_pkg("1.0.0"))
        self.assertEqual(self._entry()["version"], "1.0.0")
        installer.install(_make_pkg("2.0.0", man=_manifest("2.0.0", name="Renamed")))
        e = self._entry()
        self.assertEqual(e["version"], "2.0.0")
        self.assertEqual(e["name"], "Renamed")

    def test_reinstall_that_adds_an_icon_is_visible(self):
        installer.install(_make_pkg("1.0.0"))
        self.assertIsNone(self._entry()["icon_url"])
        installer.install(_make_pkg("2.0.0", man=_manifest("2.0.0"),
                                    files={"icon.png": PNG}))
        self.assertEqual(self._entry()["icon_url"],
                         "/api/appMgr/icon?id=cache-app&v=2.0.0&h="
                         + hashlib.sha256(PNG).hexdigest()[:16])

    def test_uninstall_then_reinstall_does_not_resurrect_stale_data(self):
        installer.install(_make_pkg("1.0.0", man=_manifest("1.0.0", name="Old"),
                                    files={"icon.png": PNG}))
        self.assertEqual(self._entry()["name"], "Old")
        installer.uninstall(APP)
        self.assertIsNone(self._entry())
        installer.install(_make_pkg("3.0.0", man=_manifest("3.0.0", name="New")))
        e = self._entry()
        self.assertEqual(e["name"], "New")
        self.assertIsNone(e["icon_url"], "icon from the previous install came back")


class IconCacheInvalidationTests(_Pinned):
    def _age_dir(self, delta=0.0):
        d = paths.app_dir(APP)
        os.utime(d, (OLD + delta, OLD + delta))

    def test_icon_dropped_in_by_hand_after_install_is_picked_up(self):
        installer.install(_make_pkg("1.0.0"))
        self._age_dir()
        self.assertIsNone(self._entry()["icon_url"])
        with open(os.path.join(paths.app_dir(APP), "icon.png"), "wb") as f:
            f.write(PNG)
        self._age_dir(60)                     # dir mtime bumps for real; age it out
        self.assertEqual(self._entry()["icon_url"],
                         "/api/appMgr/icon?id=cache-app&v=1.0.0&h="
                         + hashlib.sha256(PNG).hexdigest()[:16])

    def test_icon_removed_by_hand_is_picked_up(self):
        installer.install(_make_pkg("1.0.0", files={"icon.png": PNG}))
        self._age_dir()
        self.assertIsNotNone(self._entry()["icon_url"])
        os.remove(os.path.join(paths.app_dir(APP), "icon.png"))
        self._age_dir(60)
        self.assertIsNone(self._entry()["icon_url"])

    def test_icon_change_inside_the_settle_window_is_not_cached(self):
        installer.install(_make_pkg("1.0.0"))
        self.assertIsNone(self._entry()["icon_url"])
        with open(os.path.join(paths.app_dir(APP), "icon.png"), "wb") as f:
            f.write(PNG)                       # no utime: dir mtime is NOW
        self.assertIsNotNone(self._entry()["icon_url"],
                             "a just-added icon was hidden by the cache")

    def test_extension_precedence_survives_caching(self):
        installer.install(_make_pkg("1.0.0", files={"icon.webp": PNG}))
        self._age_dir()
        self.assertIsNotNone(self._entry()["icon_url"])
        self.assertEqual(server.do_icon(APP)[1], "image/webp")
        # png outranks webp in paths.ICON_EXTS; adding one must flip the answer
        with open(os.path.join(paths.app_dir(APP), "icon.png"), "wb") as f:
            f.write(PNG)
        self._age_dir(60)
        server.do_list()
        self.assertTrue(server._icon_file_cached(APP).endswith("icon.png"))


# --------------------------------------------------------------------------- #
# internal built-in liveness cache (not part of either application list)
# --------------------------------------------------------------------------- #
class BuiltinLivenessCacheTests(_Pinned):
    def setUp(self):
        super().setUp()
        self.calls = []
        self.value = True

        def _probe():
            self.calls.append(1)
            if isinstance(self.value, Exception):
                raise self.value
            return self.value
        builtin.is_running = _probe

    def test_repeated_internal_reads_hit_entry_cgi_once(self):
        for _ in range(5):
            server._builtin_running()
        self.assertEqual(len(self.calls), 1,
                         "entry.cgi probed once per read instead of once per TTL")

    def test_invalidate_forces_immediate_internal_refresh(self):
        self.assertTrue(server._builtin_running())
        self.value = False
        self.assertTrue(server._builtin_running(), "TTL not in effect")
        server._builtin_invalidate()
        self.assertFalse(server._builtin_running(),
                         "invalidate() did not force a re-probe")

    def test_ttl_expiry_re_probes(self):
        saved = server._BUILTIN_TTL
        server._BUILTIN_TTL = 0.0
        try:
            server._builtin_running()
            server._builtin_running()
            self.assertEqual(len(self.calls), 2)
        finally:
            server._BUILTIN_TTL = saved

    def test_transport_failure_is_not_cached(self):
        self.value = builtin.BuiltinError("entry.cgi down")
        self.assertFalse(server._builtin_running())
        self.value = True
        self.assertTrue(server._builtin_running(),
                        "a failed probe pinned running=False for the whole TTL")


class ApplicationListScopeTests(_Pinned):
    def test_legacy_and_web_lists_exclude_builtin_without_driver_probe(self):
        self._write_manifest(_manifest("1.0.0"))
        builtin_dir = paths.app_dir("builtin")
        os.makedirs(builtin_dir, exist_ok=True)
        self.addCleanup(shutil.rmtree, builtin_dir, True)
        with open(os.path.join(builtin_dir, "manifest.json"), "w") as sink:
            json.dump({"id": "builtin", "name": "Must stay hidden"}, sink)

        def unexpected_probe():
            self.fail("application listing must not query builtin inference")

        builtin.is_running = unexpected_probe
        legacy = server.do_list()
        web = server.do_v1_apps()
        self.assertEqual([item["id"] for item in legacy["apps"]], [APP])
        self.assertEqual([item["id"] for item in web["apps"]], [APP])
        self.assertNotIn("builtin", {item["id"] for item in legacy["apps"]})
        self.assertNotIn("builtin", {item["id"] for item in web["apps"]})


# --------------------------------------------------------------------------- #
# reap / sweep throttle
# --------------------------------------------------------------------------- #
class SweepThrottleTests(_Pinned):
    def setUp(self):
        super().setUp()
        supervisor._last_sweep = 0.0
        self.swept = []
        self._real_sweep = supervisor.sweep_stale
        supervisor.sweep_stale = lambda: (self.swept.append(1) or [])

    def tearDown(self):
        supervisor.sweep_stale = self._real_sweep
        supervisor._last_sweep = 0.0
        super().tearDown()

    def test_throttled_path_sweeps_at_most_once_per_interval(self):
        first = supervisor.reap_and_sweep(throttle_sweep=True)
        self.assertTrue(first["swept"])
        for _ in range(10):
            self.assertFalse(supervisor.reap_and_sweep(throttle_sweep=True)["swept"])
        self.assertEqual(len(self.swept), 1)

    def test_untrottled_default_always_sweeps(self):
        """stop()/uninstall and the CLI must never be delayed by the poll throttle."""
        for _ in range(5):
            self.assertTrue(supervisor.reap_and_sweep()["swept"])
        self.assertEqual(len(self.swept), 5)

    def test_interval_elapses(self):
        saved = supervisor.SWEEP_MIN_INTERVAL
        supervisor.SWEEP_MIN_INTERVAL = 0.05
        try:
            supervisor.reap_and_sweep(throttle_sweep=True)
            self.assertFalse(supervisor.reap_and_sweep(throttle_sweep=True)["swept"])
            time.sleep(0.06)
            self.assertTrue(supervisor.reap_and_sweep(throttle_sweep=True)["swept"])
        finally:
            supervisor.SWEEP_MIN_INTERVAL = saved

    def test_reaping_and_exit_publication_are_never_throttled(self):
        """The throttle covers sweep_stale() ONLY -- last_exit must not lag."""
        reaped = []
        drained = []
        real_reap, real_drain = supervisor.reap_children, supervisor.drain_exits
        supervisor.reap_children = lambda: (reaped.append(1) or 0)
        supervisor.drain_exits = lambda: (drained.append(1) or [])
        try:
            for _ in range(4):
                supervisor.reap_and_sweep(throttle_sweep=True)
        finally:
            supervisor.reap_children, supervisor.drain_exits = real_reap, real_drain
        self.assertEqual(len(reaped), 4)
        self.assertEqual(len(drained), 4)


class ReadPathNeverSweepsTests(_Pinned):
    """GET stays non-destructive; the active stop path still reclaims records."""

    def test_list_and_metrics_leave_stale_record_for_stop(self):
        installer.install(_make_pkg("1.0.0"))
        # a pid that cannot be running and is definitely not ours
        with open(paths.pidfile(APP), "w") as f:
            f.write("2147483646")
        server.do_list()
        server.do_metrics()
        self.assertTrue(os.path.exists(paths.pidfile(APP)),
                        "a polled GET destructively swept the run record")
        supervisor.stop(APP)
        self.assertFalse(os.path.exists(paths.pidfile(APP)),
                         "stop() left the stale pidfile behind")


if __name__ == "__main__":
    unittest.main()

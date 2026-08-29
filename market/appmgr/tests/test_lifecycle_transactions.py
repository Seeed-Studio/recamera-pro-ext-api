"""
Unit tests for the appmgr lifecycle / transactional fixes (健壮 #14-20):

  * core1 READY handshake -- start() gates on the app reaching its main loop, so
    a crash-on-startup app is reported as a FAILED start (root cause + log tail),
    never as "running/active";
  * core2 upgrade transaction -- a failed upgrade restarts the previous version
    and never leaves a double instance or lists `<id>.prev`;
  * core3 registry reaping -- reap_children() only waitpids REGISTERED app pids,
    so the exit status of an unrelated subprocess is never stolen;
  * core4 switch rollback + pgid-only teardown -- a target that fails to start
    brings the previous active app back, and stop() kills only the target's
    process group (its ffmpeg child, sharing the pgid, dies with it);
  * minor6 config revalidation -- an upgrade drops stored keys the new schema
    rejects, and legacy config migration never retires the legacy file while the
    canonical copy is unusable.

Each fix carries a REVERSE assertion (the happy path still works / a valid value
survives) so the test cannot pass vacuously.

Layout is redirected onto throwaway temp dirs; paths.* attributes are pinned
per-test (a sibling module may have imported paths first). Runnable with plain
stdlib or via pytest.
"""
import json
import os
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest

_BASE = os.path.realpath(tempfile.mkdtemp(prefix="appmgr-lifecycle."))
os.environ["APPMGR_APPS_DIR"] = os.path.join(_BASE, "apps")
os.environ["APPMGR_DIR"] = os.path.join(_BASE, "appmgr")
os.environ["APPMGR_APPDATA_DIR"] = os.path.join(_BASE, "appdata")
os.environ["APPMGR_ALLOWED_ROOTS"] = _BASE
os.environ["APPMGR_REQUIRE_SIGNATURE"] = "0"
# Keep a hung startup from stalling the suite: real vision apps get 30s, tests 5s.
os.environ["APPMGR_READY_TIMEOUT"] = "5"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from appmgr import (config as appconfig, installer, paths, server,  # noqa: E402
                    state, supervisor)


def _pin_paths(tc):
    """Pin the path constants sibling modules may have overwritten at import."""
    apps = os.path.join(_BASE, "apps")
    pins = {
        "APPS_DIR": apps,
        "APPMGR_DIR": os.path.join(_BASE, "appmgr"),
        "APPDATA_DIR": os.path.join(_BASE, "appdata"),
        "STATE_FILE": os.path.join(apps, "state.json"),
        "ALLOWED_PKG_ROOTS": (_BASE,),
        "REQUIRE_SIGNATURE": False,
    }
    saved = {k: getattr(paths, k) for k in pins}
    for k, v in pins.items():
        setattr(paths, k, v)
    tc.addCleanup(lambda: [setattr(paths, k, v) for k, v in saved.items()])
    for d in (pins["APPS_DIR"], pins["APPMGR_DIR"], pins["APPDATA_DIR"]):
        os.makedirs(d, exist_ok=True)


def _alive_running(pid):
    """True while pid is a real, non-zombie process (portable via ps)."""
    out = subprocess.run(["ps", "-o", "state=", "-p", str(pid)],
                         capture_output=True, text=True).stdout.strip()
    return bool(out) and not out.startswith("Z")


# --------------------------------------------------------------------------- #
# core1 -- READY handshake (real processes)
# --------------------------------------------------------------------------- #
class ReadyHandshakeTests(unittest.TestCase):
    """A launched process that never reaches its loop must fail start()."""

    HAPPY = ("import os, time\n"
             "open(os.environ['APPMGR_READY_FILE'], 'w').write('ok')\n"
             "time.sleep(60)\n")
    # Fails the way a missing dependency does: writes to stderr, exits, and
    # crucially NEVER touches APPMGR_READY_FILE.
    CRASH = ("import sys\n"
             "sys.stderr.write('ModuleNotFoundError: No module named voxedge\\n')\n"
             "sys.exit(1)\n")

    def setUp(self):
        _pin_paths(self)
        # A KIT_DIR/run.py shim that just runs the entry file as __main__, so the
        # entry script below IS the app -- no real kit needed.
        kit = os.path.join(_BASE, "kit-shim")
        os.makedirs(kit, exist_ok=True)
        with open(os.path.join(kit, "run.py"), "w") as f:
            f.write("import runpy, sys\n"
                    "runpy.run_path(sys.argv[1], run_name='__main__')\n")
        for k in ("KIT_DIR",):
            saved = getattr(paths, k)
            self.addCleanup(lambda k=k, v=saved: setattr(paths, k, v))
        paths.KIT_DIR = kit
        supervisor._apps.clear()
        del supervisor._reaped[:]
        # do_switch now crosses the real firmware teardown barrier before it can
        # launch this host-test app.  Stub only that device boundary; startup and
        # process cleanup below remain real.
        real_stop = server.builtin.stop
        real_running = server._builtin_running
        server.builtin.stop = lambda *a, **k: {"stop_confirmed": True}
        server._builtin_running = lambda: False
        self.addCleanup(lambda: setattr(server.builtin, "stop", real_stop))
        self.addCleanup(lambda: setattr(server, "_builtin_running", real_running))
        self._pids = []
        self.addCleanup(self._kill_all)

    def _kill_all(self):
        for pid in self._pids:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except OSError:
                pass
        supervisor._apps.clear()
        del supervisor._reaped[:]

    def _mkapp(self, app_id, body):
        d = paths.app_dir(app_id)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "manifest.json"), "w") as f:
            json.dump({"id": app_id, "entry": "app.py"}, f)
        with open(os.path.join(d, "app.py"), "w") as f:
            f.write(body)
        return d

    def test_app_that_reaches_its_loop_starts(self):
        """REVERSE assertion: a well-behaved app that signals READY starts."""
        self._mkapp("good-app", self.HAPPY)
        pid = supervisor.start("good-app")
        self._pids.append(pid)
        # (is_running() needs /proc for its PID-reuse guard, absent on the macOS
        # dev box -- so assert on the readiness handshake directly instead.)
        self.assertTrue(_alive_running(pid))
        self.assertTrue(os.path.exists(paths.readyfile("good-app")),
                        "start() must return only after the app signalled READY")

    def test_crash_on_startup_fails_start_with_root_cause(self):
        self._mkapp("bad-app", self.CRASH)
        with self.assertRaises(supervisor.SupervisorError) as cm:
            supervisor.start("bad-app")
        msg = str(cm.exception)
        # Root cause surfaced: exit reason + the app's own log tail.
        self.assertIn("exited during startup", msg)
        self.assertIn("voxedge", msg, "log tail (real root cause) must be reported")
        # No orphan, no stale run.pid, and the crash is recorded for the UI.
        self.assertIsNone(supervisor.is_running("bad-app"))
        self.assertFalse(os.path.exists(paths.pidfile("bad-app")))
        self.assertFalse(os.path.exists(paths.readyfile("bad-app")))
        self.assertEqual(supervisor.last_exit("bad-app")["code"], 1)

    def test_do_switch_does_not_commit_active_for_a_crashing_app(self):
        """The whole point: the UI must never show a dead app as active."""
        self._mkapp("bad-app", self.CRASH)
        state.set_active(None, None)
        with self.assertRaises(Exception):
            server.do_switch("bad-app")
        self.assertIsNone(state.get_active(),
                          "active must not point at an app that failed to start")


# --------------------------------------------------------------------------- #
# core3 -- registry reaping (real processes)
# --------------------------------------------------------------------------- #
class RegistryReapTests(unittest.TestCase):
    def setUp(self):
        _pin_paths(self)
        supervisor._apps.clear()
        del supervisor._reaped[:]
        self._procs = []
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for p in self._procs:
            try:
                p.kill(); p.wait(timeout=5)
            except Exception:
                pass
        supervisor._apps.clear()
        del supervisor._reaped[:]

    def test_reap_ignores_a_non_app_child_and_never_steals_its_status(self):
        # A registered app child (long-lived) ...
        app = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self._procs.append(app)
        supervisor._register_child(app)
        # ... and an UNREGISTERED child (stands in for openssl/pip/gst-inspect),
        # left as a zombie: we deliberately do NOT wait it yet.
        nonapp = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(7)"])
        deadline = time.monotonic() + 5
        while _alive_running(nonapp.pid) and time.monotonic() < deadline:
            time.sleep(0.02)

        # reap_children must reap NOTHING (the app is alive; the non-app child is
        # not registered) and must not have swept the non-app zombie.
        self.assertEqual(supervisor.reap_children(), 0)
        self.assertEqual(supervisor._reaped, [])

        # PROOF the status was not stolen: we can still waitpid the non-app child
        # ourselves and read its real exit code. The old waitpid(-1) reaper would
        # have collected it here, making this raise ChildProcessError.
        _pid, status = os.waitpid(nonapp.pid, 0)
        self.assertEqual(os.WEXITSTATUS(status), 7)

        # REVERSE: once the registered app dies, reap_children DOES collect it.
        app.terminate()
        deadline = time.monotonic() + 5
        while app.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertGreaterEqual(supervisor.reap_children(), 1)
        self.assertTrue(any(e[0] == app.pid for e in supervisor._reaped))


# --------------------------------------------------------------------------- #
# core4 -- stop() kills only the target's process group (pgid coverage)
# --------------------------------------------------------------------------- #
class StopProcessGroupTests(unittest.TestCase):
    """stop() must take down the app's ffmpeg child (same pgid) and nothing
    else -- proving the removed global `pkill -x ffmpeg` was unnecessary."""

    APP = "pgid-app"
    # Parent (the app) spawns a child in its OWN process group (no setsid), the
    # way kit's frame source spawns ffmpeg, writes both pids, then idles.
    PARENT = (
        "import os, sys, subprocess, time\n"
        "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        "open(sys.argv[1], 'w').write(str(c.pid))\n"
        "time.sleep(120)\n")

    def setUp(self):
        _pin_paths(self)
        supervisor._apps.clear()
        del supervisor._reaped[:]
        self._proc_root = supervisor.PROC_ROOT
        self._procdir = tempfile.mkdtemp(prefix="fakeproc.", dir=_BASE)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        supervisor.PROC_ROOT = self._proc_root
        supervisor._apps.clear()
        del supervisor._reaped[:]

    def _fake_proc(self, pid, state_c, cwd):
        d = os.path.join(self._procdir, str(pid))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "stat"), "w") as f:
            f.write("%d (python3) %s 1 1 0 -1 0 0 0 0 0\n" % (pid, state_c))
        with open(os.path.join(d, "cmdline"), "wb") as f:
            f.write(b"")
        link = os.path.join(d, "cwd")
        if os.path.islink(link) or os.path.exists(link):
            os.remove(link)
        os.symlink(cwd, link)

    def test_stop_kills_the_ffmpeg_child_via_the_process_group(self):
        d = paths.app_dir(self.APP)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "manifest.json"), "w") as f:
            json.dump({"id": self.APP, "entry": "app.py"}, f)
        childpid_file = os.path.join(d, "child.pid")
        parent = subprocess.Popen([sys.executable, "-c", self.PARENT, childpid_file],
                                  cwd=d, start_new_session=True)
        self.addCleanup(lambda: parent.poll() is None and parent.kill())
        supervisor._register_child(parent)
        with open(paths.pidfile(self.APP), "w") as f:
            f.write(str(parent.pid))
        # wait for the child ("ffmpeg") to exist and record its pid
        deadline = time.monotonic() + 5
        while not os.path.isfile(childpid_file) and time.monotonic() < deadline:
            time.sleep(0.02)
        child_pid = int(open(childpid_file).read().strip())
        self.assertTrue(_alive_running(parent.pid))
        self.assertTrue(_alive_running(child_pid), "ffmpeg child must be up first")
        # Same process group -> killpg reaches both.
        self.assertEqual(os.getpgid(child_pid), os.getpgid(parent.pid))

        # /proc fixture so _is_ours() accepts the parent on macOS (no real /proc).
        supervisor.PROC_ROOT = self._procdir
        self._fake_proc(parent.pid, "S", cwd=d)

        supervisor.stop(self.APP, grace=3.0)

        for pid, who in ((parent.pid, "app"), (child_pid, "ffmpeg child")):
            deadline = time.monotonic() + 5
            while _alive_running(pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(_alive_running(pid),
                             f"{who} still running after stop() -- pgid teardown failed")


# --------------------------------------------------------------------------- #
# core4 -- switch failure rolls back to the previous active app (stubbed)
# --------------------------------------------------------------------------- #
class SwitchRollbackTests(unittest.TestCase):
    def setUp(self):
        _pin_paths(self)
        state.set_active(None, None)
        for app_id in ("prev-app", "target-app"):
            d = paths.app_dir(app_id)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "manifest.json"), "w") as f:
                json.dump({"id": app_id, "version": "1.0.0", "name": app_id}, f)
        self.calls = []
        self.running = {}
        self.fail_ids = set()
        orig = {n: getattr(server.supervisor, n)
                for n in ("is_running", "start", "stop")}
        builtin_stop = server.builtin.stop
        self.addCleanup(lambda: [setattr(server.supervisor, n, v)
                                 for n, v in orig.items()])
        self.addCleanup(lambda: setattr(server.builtin, "stop", builtin_stop))
        server.supervisor.is_running = lambda a: self.running.get(a)
        server.supervisor.start = self._fake_start
        server.supervisor.stop = self._fake_stop
        server.builtin.stop = lambda *a, **k: {"stop_confirmed": True}

    def _fake_start(self, app_id, **k):
        self.calls.append(("start", app_id))
        if app_id in self.fail_ids:
            self.fail_ids.discard(app_id)   # only the FIRST start fails; a
            raise server.supervisor.SupervisorError(f"{app_id}: boom")  # rollback restart succeeds
        self.running[app_id] = 4321
        return 4321

    def _fake_stop(self, app_id, *a, **k):
        self.calls.append(("stop", app_id))
        self.running.pop(app_id, None)
        return {"app": app_id}

    def test_target_failure_restarts_previous_active(self):
        server.state.set_active("prev-app", "1.0.0")
        self.running["prev-app"] = 4321
        self.fail_ids = {"target-app"}
        with self.assertRaises(server.supervisor.SupervisorError):
            server.do_switch("target-app")
        # Previous app is running again and IS the active app after rollback.
        self.assertEqual(state.get_active(), "prev-app")
        self.assertEqual(self.running.get("prev-app"), 4321)
        self.assertIn(("start", "prev-app"), self.calls)

    def test_successful_switch_still_activates_target(self):
        """REVERSE: a target that starts fine becomes active as before."""
        server.state.set_active("prev-app", "1.0.0")
        self.running["prev-app"] = 4321
        res = server.do_switch("target-app")
        self.assertEqual(res["active_app"], "target-app")
        self.assertEqual(state.get_active(), "target-app")


# --------------------------------------------------------------------------- #
# core2 -- upgrade transaction (real installer, stubbed supervisor)
# --------------------------------------------------------------------------- #
class UpgradeTransactionTests(unittest.TestCase):
    APP = "upgrade-app"

    def setUp(self):
        _pin_paths(self)
        self._pkgs = os.path.join(_BASE, "pkgs")
        os.makedirs(self._pkgs, exist_ok=True)
        state.set_active(None, None)
        self.calls = []
        self.running = {}
        self.fail_ids = set()
        orig = {n: getattr(server.supervisor, n)
                for n in ("is_running", "start", "stop")}
        builtin_stop = server.builtin.stop
        self.addCleanup(lambda: [setattr(server.supervisor, n, v)
                                 for n, v in orig.items()])
        self.addCleanup(lambda: setattr(server.builtin, "stop", builtin_stop))
        server.supervisor.is_running = lambda a: self.running.get(a)
        server.supervisor.start = self._fake_start
        server.supervisor.stop = self._fake_stop
        server.builtin.stop = lambda *a, **k: {"stop_confirmed": True}
        server.cache_clear()

    def _fake_start(self, app_id, **k):
        self.calls.append(("start", app_id))
        if app_id in self.fail_ids:
            self.fail_ids.discard(app_id)   # new version fails; rollback of the
            raise server.supervisor.SupervisorError(f"{app_id}: boom")  # OLD version then succeeds
        self.running[app_id] = 999
        return 999

    def _fake_stop(self, app_id, *a, **k):
        self.calls.append(("stop", app_id))
        self.running.pop(app_id, None)
        return {"app": app_id}

    def _pkg(self, version):
        src = tempfile.mkdtemp(prefix=f"src-{version}.", dir=self._pkgs)
        with open(os.path.join(src, "manifest.json"), "w") as f:
            json.dump({"id": self.APP, "version": version, "name": self.APP,
                       "entry": "app.py"}, f)
        with open(os.path.join(src, "app.py"), "w") as f:
            f.write(f"# {version}\n")
        pkg = os.path.join(self._pkgs, f"{self.APP}-{version}.tar.gz")
        with tarfile.open(pkg, "w:gz") as tar:
            for name in sorted(os.listdir(src)):
                tar.add(os.path.join(src, name), arcname=name)
        return pkg

    def _installed_version(self):
        with open(os.path.join(paths.app_dir(self.APP), "manifest.json")) as f:
            return json.load(f)["version"]

    def test_failed_upgrade_rolls_back_to_previous_version(self):
        # v1 installed, active + running.
        server.do_install(self._pkg("1.0.0"))
        state.set_active(self.APP, "1.0.0")
        self.running[self.APP] = 999
        self.calls.clear()

        # v2 install: the NEW version fails to come up.
        self.fail_ids = {self.APP}
        with self.assertRaises(server.supervisor.SupervisorError):
            server.do_install(self._pkg("2.0.0"))

        # Rolled back: the live dir is v1 again, no `.prev` left dangling as the
        # live app, and state still points at the running v1.
        self.assertEqual(self._installed_version(), "1.0.0",
                         "failed upgrade must restore the previous version")
        self.assertEqual(state.get_active(), self.APP)
        self.assertEqual(self.running.get(self.APP), 999,
                         "previous version must be running again")
        # The old process was stopped before the swap (no double instance).
        self.assertEqual(self.calls[0], ("stop", self.APP))

    def test_prev_dir_is_never_listed_as_an_app(self):
        server.do_install(self._pkg("1.0.0"))
        server.cache_clear()
        server.do_install(self._pkg("2.0.0"))     # leaves upgrade-app.prev
        self.assertTrue(os.path.isdir(paths.app_dir(self.APP) + ".prev"))
        ids = [a["id"] for a in server.do_list()["apps"]]
        self.assertIn(self.APP, ids)
        self.assertNotIn(self.APP + ".prev", ids)

    def test_successful_upgrade_restarts_when_running(self):
        """REVERSE: an upgrade of the running app installs v2 and restarts it."""
        server.do_install(self._pkg("1.0.0"))
        state.set_active(self.APP, "1.0.0")
        self.running[self.APP] = 999
        self.calls.clear()
        res = server.do_install(self._pkg("2.0.0"))
        self.assertTrue(res["restarted"])
        self.assertEqual(self._installed_version(), "2.0.0")
        self.assertEqual(("start", self.APP), self.calls[-1])

    def test_local_unsigned_upgrade_is_installed_stopped(self):
        """A Web-approved unsigned upgrade never inherits a running state."""
        server.do_install(self._pkg("1.0.0"))
        state.set_active(self.APP, "1.0.0")
        self.running[self.APP] = 999
        self.calls.clear()

        result = server.do_install(
            self._pkg("2.0.0"), allow_unsigned=True,
            expected_preflight={
                "manifest": {
                    "id": self.APP,
                    "version": "2.0.0",
                    "name": self.APP,
                    "entry": "app.py",
                },
                "release_id": None,
                "signature": {
                    "status": "unsigned",
                    "signer_kind": None,
                    "key_fingerprint": None,
                },
                "source": server.V1_LOCAL_UPLOAD_SOURCE,
                "channel": server.V1_LOCAL_UPLOAD_CHANNEL,
            })

        self.assertEqual(self.calls, [("stop", self.APP)])
        self.assertFalse(result["restarted"])
        self.assertFalse(result["auto_started"])
        self.assertTrue(result["requires_manual_start"])
        self.assertEqual(self._installed_version(), "2.0.0")
        self.assertNotIn(self.APP, self.running)
        self.assertIsNone(state.get_active())
        self.assertEqual(
            state.get_app(self.APP)["desired_state"], state.DESIRED_STOPPED)


# --------------------------------------------------------------------------- #
# minor5 -- reconcile an install interrupted mid dir-swap
# --------------------------------------------------------------------------- #
class ReconcileInterruptedInstallTests(unittest.TestCase):
    APP = "reconcile-app"

    def setUp(self):
        _pin_paths(self)
        import shutil
        for suffix in ("", ".prev", ".failed"):
            shutil.rmtree(paths.app_dir(self.APP) + suffix, ignore_errors=True)

    def test_prev_without_live_dir_is_restored(self):
        # Simulate the crash window: dest -> <id>.prev happened, staging -> dest
        # did NOT. Only <id>.prev survives.
        prev = paths.app_dir(self.APP) + ".prev"
        os.makedirs(prev, exist_ok=True)
        with open(os.path.join(prev, "manifest.json"), "w") as f:
            json.dump({"id": self.APP, "version": "1.0.0"}, f)
        self.assertFalse(os.path.isdir(paths.app_dir(self.APP)))

        restored = installer.reconcile_interrupted_installs()
        self.assertIn(self.APP, restored)
        self.assertTrue(os.path.isdir(paths.app_dir(self.APP)))
        self.assertFalse(os.path.isdir(prev))

    def test_prev_with_live_dir_is_left_as_rollback_copy(self):
        """REVERSE: a normal one-generation `.prev` (live dir present) is kept."""
        dest = paths.app_dir(self.APP)
        prev = dest + ".prev"
        for d in (dest, prev):
            os.makedirs(d, exist_ok=True)
        restored = installer.reconcile_interrupted_installs()
        self.assertNotIn(self.APP, restored)
        self.assertTrue(os.path.isdir(prev), "rollback copy must be preserved")


# --------------------------------------------------------------------------- #
# minor6 -- config revalidation + migration idempotency
# --------------------------------------------------------------------------- #
class ConfigRevalidationTests(unittest.TestCase):
    APP = "cfg-app"

    def setUp(self):
        _pin_paths(self)
        import shutil
        shutil.rmtree(paths.appdata_dir(self.APP), ignore_errors=True)

    def _manifest(self, conf_max, with_mode):
        items = [{"key": "conf", "type": "number", "min": 0, "max": conf_max,
                  "default": 0.2}]
        if with_mode:
            items.append({"key": "mode", "type": "enum",
                          "options": ["a", "b"], "default": "a"})
        return {"id": self.APP, "version": "2.0.0",
                "config_schema": {"groups": [{"key": "g", "items": items}]}}

    def test_upgrade_drops_incompatible_keys_and_keeps_valid_ones(self):
        appconfig.write_user_config(self.APP, {
            "conf": 0.5,          # valid under v1 (max 1.0) ...
            "mode": "a",          # ... exists under v1 ...
            "legacy_only": 42,    # ... a key v2 removes entirely
        })
        # v2: conf max narrowed to 0.3, `mode` removed, `legacy_only` unknown.
        res = appconfig.revalidate_user_config(self._manifest(0.3, with_mode=False),
                                               self.APP)
        self.assertEqual(set(res["dropped"]), {"conf", "mode", "legacy_only"})
        self.assertEqual(appconfig.load_user_config(self.APP), {},
                         "every now-invalid key must be dropped")
        # Quarantined, not silently lost.
        self.assertTrue(os.path.isfile(
            os.path.join(paths.appdata_dir(self.APP), "config.quarantine.json")))

    def test_valid_config_survives_revalidation(self):
        """REVERSE: values the new schema accepts are kept verbatim."""
        appconfig.write_user_config(self.APP, {"conf": 0.25, "mode": "b"})
        res = appconfig.revalidate_user_config(self._manifest(1.0, with_mode=True),
                                               self.APP)
        self.assertEqual(res["dropped"], {})
        self.assertEqual(appconfig.load_user_config(self.APP),
                         {"conf": 0.25, "mode": "b"})

    def test_no_schema_manifest_never_wipes_config(self):
        """A third-party app without a schema must not lose its whole config."""
        appconfig.write_user_config(self.APP, {"anything": 1})
        res = appconfig.revalidate_user_config({"id": self.APP}, self.APP)
        self.assertTrue(res.get("skipped"))
        self.assertEqual(appconfig.load_user_config(self.APP), {"anything": 1})

    def test_migrate_keeps_legacy_when_canonical_is_corrupt(self):
        """健壮#20: a corrupt canonical must NOT trigger retiring the legacy file
        (which would leave the app on defaults with both copies gone)."""
        os.makedirs(paths.appdata_dir(self.APP), exist_ok=True)
        os.makedirs(paths.app_dir(self.APP), exist_ok=True)  # legacy lives here
        # valid legacy, corrupt canonical
        with open(appconfig.legacy_config_path(self.APP), "w") as f:
            json.dump({"conf": 0.7}, f)
        with open(appconfig.config_path(self.APP), "w") as f:
            f.write("{ this is not json")
        self.assertFalse(appconfig.migrate_legacy_config(self.APP))
        self.assertTrue(os.path.isfile(appconfig.legacy_config_path(self.APP)),
                        "legacy must be kept while canonical is unusable")
        # The user's value is still reachable (via the legacy fallback).
        self.assertEqual(appconfig.load_user_config(self.APP), {"conf": 0.7})


if __name__ == "__main__":
    unittest.main(verbosity=2)

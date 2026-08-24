"""Real-process regression tests for saved-PGID crash containment.

The leader is intentionally allowed to disappear while a helper in the same
process group stays alive.  That is the failure mode ``run.pgid`` addresses:
querying getpgid(leader_pid) is too late after a SIGSEGV/SIGKILL.
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

_BASE = os.path.realpath(tempfile.mkdtemp(prefix="appmgr-crash-containment."))
os.environ.setdefault("APPMGR_APPS_DIR", os.path.join(_BASE, "apps"))
os.environ.setdefault("APPMGR_DIR", os.path.join(_BASE, "appmgr"))
os.environ.setdefault("APPMGR_READY_TIMEOUT", "3")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from appmgr import paths, supervisor  # noqa: E402


def _running(pid):
    out = subprocess.run(["ps", "-o", "state=", "-p", str(pid)],
                         capture_output=True, text=True).stdout.strip()
    return bool(out) and not out.startswith("Z")


def _wait_not_running(pid, timeout=5.0):
    deadline = time.monotonic() + timeout
    while _running(pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    return not _running(pid)


class CrashContainmentTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="case.", dir=_BASE)
        apps = os.path.join(self.root, "apps")
        appmgr = os.path.join(self.root, "appmgr")
        kit = os.path.join(self.root, "kit")
        for d in (apps, appmgr, kit):
            os.makedirs(d, exist_ok=True)
        with open(os.path.join(kit, "run.py"), "w") as f:
            f.write("import runpy, sys\n"
                    "runpy.run_path(sys.argv[1], run_name='__main__')\n")

        pins = {
            "APPS_DIR": apps,
            "APPMGR_DIR": appmgr,
            "KIT_DIR": kit,
        }
        self.saved_paths = {name: getattr(paths, name) for name in pins}
        for name, value in pins.items():
            setattr(paths, name, value)

        self.old_sigchld = signal.getsignal(signal.SIGCHLD)
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)
        self.boot_id_path = os.path.join(self.root, "kernel-boot-id")
        with open(self.boot_id_path, "w") as f:
            f.write("boot-A")
        self.saved_boot_id_path = supervisor.BOOT_ID_PATH
        supervisor.BOOT_ID_PATH = self.boot_id_path
        self.groups = []
        self.procs = []
        supervisor._apps.clear()
        del supervisor._reaped[:]
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        signal.signal(signal.SIGCHLD, self.old_sigchld)
        for pgid in self.groups:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass
        for proc in self.procs:
            try:
                proc.wait(timeout=2)
            except Exception:
                pass
        supervisor._apps.clear()
        del supervisor._reaped[:]
        supervisor.BOOT_ID_PATH = self.saved_boot_id_path
        for name, value in self.saved_paths.items():
            setattr(paths, name, value)

    def _mkapp(self, app_id, body):
        directory = paths.app_dir(app_id)
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "manifest.json"), "w") as f:
            json.dump({"id": app_id, "entry": "app.py"}, f)
        with open(os.path.join(directory, "app.py"), "w") as f:
            f.write(body)
        return directory

    def _child_pid(self, path):
        deadline = time.monotonic() + 3
        while not os.path.isfile(path) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(os.path.isfile(path), "helper did not publish its pid")
        return int(open(path).read().strip())

    def test_startup_failure_kills_helper_spawned_before_ready(self):
        app_id = "startup-child-crash"
        child_file = os.path.join(self.root, "startup-child.pid")
        body = (
            "import subprocess, sys\n"
            "c = subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(120)'])\n"
            "open(%r, 'w').write(str(c.pid))\n"
            "sys.stderr.write('startup exploded after helper spawn\\n')\n"
            "sys.exit(7)\n" % child_file)
        self._mkapp(app_id, body)

        with self.assertRaises(supervisor.SupervisorError) as cm:
            supervisor.start(app_id)

        child = self._child_pid(child_file)
        self.assertIn("startup exploded", str(cm.exception))
        self.assertTrue(_wait_not_running(child),
                        "startup failure left its helper process alive")
        self.assertFalse(os.path.exists(paths.pidfile(app_id)))
        self.assertFalse(os.path.exists(paths.pgidfile(app_id)))
        self.assertFalse(os.path.exists(paths.bootfile(app_id)))

    def test_startup_timeout_fences_once_before_forced_kill(self):
        app_id = "startup-force-fence"
        child_file = os.path.join(self.root, "startup-force-child.pid")
        child_ready = os.path.join(self.root, "startup-force-child.ready")
        child_code = (
            "import signal,time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "open(%r, 'w').write('ready'); time.sleep(120)" % child_ready)
        body = (
            "import os, signal, subprocess, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "c = subprocess.Popen([sys.executable, '-c', %r])\n"
            "deadline = time.monotonic() + 3\n"
            "while not os.path.exists(%r) and time.monotonic() < deadline: "
            "time.sleep(0.01)\n"
            "open(%r, 'w').write(str(c.pid))\n"
            "time.sleep(120)\n" % (child_code, child_ready, child_file))
        self._mkapp(app_id, body)
        fenced = []

        with self.assertRaises(supervisor.SupervisorError):
            supervisor.start(
                app_id, ready_timeout=0.2,
                before_force_kill=lambda: fenced.append("fenced"))

        child = self._child_pid(child_file)
        self.assertEqual(fenced, ["fenced"])
        self.assertTrue(_wait_not_running(child))
        self.assertFalse(os.path.exists(paths.pidfile(app_id)))
        self.assertFalse(os.path.exists(paths.pgidfile(app_id)))
        self.assertFalse(os.path.exists(paths.bootfile(app_id)))

    def test_run_identity_is_committed_before_on_spawn_is_published(self):
        """A state/gateway callback must never name an unobservable PID.

        GET /apps may call is_running() concurrently with startup.  This
        callback is the exact boundary where coordinator state becomes
        externally resolvable, so all three root-owned run identity files must
        already authenticate the live leader here.
        """
        app_id = "spawn-identity-order"
        body = (
            "import os, time\n"
            "open(os.environ['APPMGR_READY_FILE'], 'w').write('ready')\n"
            "time.sleep(120)\n"
        )
        self._mkapp(app_id, body)
        observed = {}

        def on_spawn(pid):
            observed.update({
                "pid": int(open(paths.pidfile(app_id)).read().strip()),
                "pgid": int(open(paths.pgidfile(app_id)).read().strip()),
                "boot_id": open(paths.bootfile(app_id)).read().strip(),
                "running": supervisor.is_running(app_id),
            })

        leader = supervisor.start(app_id, on_spawn=on_spawn)
        self.groups.append(leader)
        self.assertEqual(observed, {
            "pid": leader,
            "pgid": leader,
            "boot_id": "boot-A",
            "running": leader,
        })
        supervisor.stop(app_id, grace=0.5)

    def test_stale_sweep_waits_for_complete_run_record_commit(self):
        """A partial PGID+boot record must never make sweep kill a new app."""
        app_id = "partial-record-sweep"
        body = (
            "import os, time\n"
            "open(os.environ['APPMGR_READY_FILE'], 'w').write('ready')\n"
            "time.sleep(120)\n"
        )
        self._mkapp(app_id, body)
        partial = threading.Event()
        allow_pid_commit = threading.Event()
        sweep_entered = threading.Event()
        sweep_done = threading.Event()
        started = {}
        start_errors = []
        swept = {}
        real_write_positive_int = supervisor._write_positive_int

        def pause_before_pid_commit(path, value):
            if path == paths.pidfile(app_id):
                # At this point _write_run_ids has already committed run.pgid
                # and run.boot_id.  Hold that exact old sweep hazard open.
                partial.set()
                if not allow_pid_commit.wait(3):
                    raise AssertionError("test did not release PID commit")
            real_write_positive_int(path, value)

        def launch():
            try:
                started["pid"] = supervisor.start(app_id)
            except BaseException as exc:
                start_errors.append(exc)

        def sweep():
            sweep_entered.set()
            swept["apps"] = supervisor.sweep_stale()
            sweep_done.set()

        with mock.patch.object(
                supervisor, "_write_positive_int",
                side_effect=pause_before_pid_commit):
            starter = threading.Thread(target=launch)
            starter.start()
            self.assertTrue(partial.wait(3), "start never reached partial commit")
            leader = next(iter(supervisor._apps))
            self.groups.append(leader)
            sweeper = threading.Thread(target=sweep)
            sweeper.start()
            self.assertTrue(sweep_entered.wait(1))
            self.assertFalse(
                sweep_done.wait(0.15),
                "sweep inspected a partially committed run identity",
            )
            self.assertTrue(_running(leader),
                            "partial-record sweep killed the new leader")
            allow_pid_commit.set()
            starter.join(timeout=5)
            sweeper.join(timeout=5)

        self.assertFalse(starter.is_alive())
        self.assertFalse(sweeper.is_alive())
        self.assertEqual(start_errors, [])
        self.assertEqual(started.get("pid"), leader)
        self.assertNotIn(app_id, swept.get("apps", []))
        self.assertTrue(_running(leader))
        supervisor.stop(app_id, grace=0.5)

    def test_start_refuses_when_current_boot_identity_is_unavailable(self):
        app_id = "missing-kernel-boot-id"
        self._mkapp(app_id, "raise SystemExit(0)\n")
        os.remove(self.boot_id_path)

        with self.assertRaises(supervisor.SupervisorError) as cm:
            supervisor.start(app_id)

        self.assertIn("cannot establish current boot identity", str(cm.exception))
        self.assertFalse(os.path.exists(paths.pidfile(app_id)))
        self.assertFalse(os.path.exists(paths.pgidfile(app_id)))
        self.assertFalse(os.path.exists(paths.bootfile(app_id)))

    def test_runtime_sigsegv_immediately_kills_same_group_helper(self):
        app_id = "runtime-segv"
        child_file = os.path.join(self.root, "runtime-child.pid")
        body = (
            "import os, signal, subprocess, sys, time\n"
            "c = subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(120)'])\n"
            "open(%r, 'w').write(str(c.pid))\n"
            "open(os.environ['APPMGR_READY_FILE'], 'w').write(str(os.getpid()))\n"
            "time.sleep(0.15)\n"
            "os.kill(os.getpid(), signal.SIGSEGV)\n" % child_file)
        self._mkapp(app_id, body)

        leader = supervisor.start(app_id)
        self.groups.append(leader)
        child = self._child_pid(child_file)
        self.assertEqual(int(open(paths.pgidfile(app_id)).read()), leader)
        self.assertEqual(open(paths.bootfile(app_id)).read(), "boot-A")
        self.assertEqual(os.getpgid(child), leader)

        deadline = time.monotonic() + 5
        while _running(leader) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(_running(leader), "SIGSEGV leader did not exit")
        # The SIGCHLD-safe phase may poll/killpg/append only.  User-visible
        # logging is deferred until drain_exits() runs in normal context.
        with mock.patch("builtins.print") as emitted:
            self.assertEqual(supervisor.reap_children(), 1)
            emitted.assert_not_called()
            exits = supervisor.drain_exits()
            self.assertTrue(emitted.called)

        mine = [e for e in exits if e.get("pid") == leader]
        self.assertEqual(len(mine), 1, "SIGSEGV leader was not reaped")
        self.assertEqual(mine[0]["signal"], "SIGSEGV")
        self.assertTrue(_wait_not_running(child),
                        "runtime crash left its helper process alive")
        self.assertFalse(os.path.exists(paths.pidfile(app_id)))
        self.assertFalse(os.path.exists(paths.pgidfile(app_id)))
        self.assertFalse(os.path.exists(paths.bootfile(app_id)))

    def test_stop_uses_persisted_pgid_after_registry_and_leader_are_gone(self):
        app_id = "adopted-stale-run"
        child_file = os.path.join(self.root, "stale-child.pid")
        body = (
            "import os, subprocess, sys, time\n"
            "c = subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(120)'])\n"
            "open(%r, 'w').write(str(c.pid))\n"
            "open(os.environ['APPMGR_READY_FILE'], 'w').write(str(os.getpid()))\n"
            "time.sleep(120)\n" % child_file)
        self._mkapp(app_id, body)

        leader = supervisor.start(app_id)
        self.groups.append(leader)
        proc = supervisor._apps[leader]
        self.procs.append(proc)
        child = self._child_pid(child_file)
        os.kill(leader, signal.SIGKILL)       # leader only, not its group
        proc.wait(timeout=3)                  # make getpgid(leader) impossible
        supervisor._apps.pop(leader, None)    # simulate a fresh appmgr registry
        self.assertTrue(_running(child), "precondition: helper must survive leader")
        self.assertEqual(int(open(paths.pgidfile(app_id)).read()), leader)

        result = supervisor.stop(app_id, grace=0.0)

        self.assertEqual(result["pgid"], leader)
        self.assertTrue(result["killed"])
        self.assertTrue(_wait_not_running(child),
                        "persisted PGID did not reach orphan helper")
        self.assertFalse(os.path.exists(paths.pidfile(app_id)))
        self.assertFalse(os.path.exists(paths.pgidfile(app_id)))
        self.assertFalse(os.path.exists(paths.bootfile(app_id)))

    def test_stop_timeout_escalates_for_term_ignoring_group(self):
        app_id = "term-ignoring-run"
        child_file = os.path.join(self.root, "term-child.pid")
        child_ready = os.path.join(self.root, "term-child.ready")
        child_code = (
            "import signal,time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "open(%r, 'w').write('ready'); time.sleep(120)" % child_ready)
        body = (
            "import os, signal, subprocess, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "c = subprocess.Popen([sys.executable, '-c', %r])\n"
            "deadline = time.monotonic() + 3\n"
            "while not os.path.exists(%r) and time.monotonic() < deadline: "
            "time.sleep(0.01)\n"
            "open(%r, 'w').write(str(c.pid))\n"
            "open(os.environ['APPMGR_READY_FILE'], 'w').write(str(os.getpid()))\n"
            "time.sleep(120)\n" % (child_code, child_ready, child_file))
        self._mkapp(app_id, body)

        leader = supervisor.start(app_id)
        self.groups.append(leader)
        child = self._child_pid(child_file)

        fenced = []

        def before_force_kill():
            fenced.append((_running(leader), _running(child)))

        result = supervisor.stop(
            app_id, grace=0.1, before_force_kill=before_force_kill)

        self.assertTrue(result["signalled"])
        self.assertTrue(result["killed"], "TERM timeout did not escalate to KILL")
        self.assertEqual(fenced, [(True, True)],
                         "authorization fence must run once before SIGKILL")
        self.assertTrue(_wait_not_running(leader))
        self.assertTrue(_wait_not_running(child))

    def test_cross_boot_dead_leader_record_does_not_signal_saved_group(self):
        """A /userdata PGID from boot A must not address a group on boot B."""
        app_id = "cross-boot-stale-run"
        child_file = os.path.join(self.root, "cross-boot-child.pid")
        body = (
            "import os, subprocess, sys, time\n"
            "c = subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(120)'])\n"
            "open(%r, 'w').write(str(c.pid))\n"
            "open(os.environ['APPMGR_READY_FILE'], 'w').write(str(os.getpid()))\n"
            "time.sleep(120)\n" % child_file)
        self._mkapp(app_id, body)

        leader = supervisor.start(app_id)
        self.groups.append(leader)
        proc = supervisor._apps[leader]
        self.procs.append(proc)
        child = self._child_pid(child_file)
        os.kill(leader, signal.SIGKILL)
        proc.wait(timeout=3)
        supervisor._apps.pop(leader, None)
        self.assertTrue(_running(child), "precondition: orphan group must exist")
        with open(self.boot_id_path, "w") as f:
            f.write("boot-B")

        result = supervisor.stop(app_id, grace=0.0)

        self.assertFalse(result["boot_verified"])
        self.assertFalse(result["killed"])
        self.assertTrue(_running(child),
                        "cross-boot numeric PGID was incorrectly signalled")
        self.assertFalse(os.path.exists(paths.pidfile(app_id)))
        self.assertFalse(os.path.exists(paths.pgidfile(app_id)))
        self.assertFalse(os.path.exists(paths.bootfile(app_id)))

    def test_legacy_dead_record_without_boot_id_is_cleanup_only(self):
        app_id = "legacy-stale-run"
        self._mkapp(app_id, "# not launched\n")
        with open(paths.pidfile(app_id), "w") as f:
            f.write("424242")
        with open(paths.pgidfile(app_id), "w") as f:
            f.write("424242")

        with mock.patch.object(supervisor, "_pid_running", return_value=False), \
                mock.patch.object(supervisor, "_killpg_id") as killpg:
            result = supervisor.stop(app_id, grace=0.0)

        self.assertFalse(result["boot_verified"])
        killpg.assert_not_called()
        self.assertFalse(os.path.exists(paths.pidfile(app_id)))
        self.assertFalse(os.path.exists(paths.pgidfile(app_id)))

    def test_live_pid_reuse_is_not_signalled_even_with_same_boot_record(self):
        app_id = "pid-reuse-run"
        self._mkapp(app_id, "# not launched\n")
        for path in (paths.pidfile(app_id), paths.pgidfile(app_id)):
            with open(path, "w") as f:
                f.write("434343")
        with open(paths.bootfile(app_id), "w") as f:
            f.write("boot-A")

        with mock.patch.object(supervisor, "_pid_running", return_value=True), \
                mock.patch.object(supervisor, "_is_ours", return_value=False), \
                mock.patch.object(supervisor, "_killpg_id") as killpg:
            result = supervisor.stop(app_id, grace=0.0)

        self.assertTrue(result["boot_verified"])
        killpg.assert_not_called()
        self.assertFalse(os.path.exists(paths.pidfile(app_id)))
        self.assertFalse(os.path.exists(paths.pgidfile(app_id)))
        self.assertFalse(os.path.exists(paths.bootfile(app_id)))

    def test_stale_sweep_clears_cross_boot_record_without_signalling(self):
        app_id = "sweep-cross-boot"
        self._mkapp(app_id, "# not launched\n")
        for path in (paths.pidfile(app_id), paths.pgidfile(app_id)):
            with open(path, "w") as f:
                f.write("444444")
        with open(paths.bootfile(app_id), "w") as f:
            f.write("boot-before-reboot")

        with mock.patch.object(supervisor, "_pid_running", return_value=False), \
                mock.patch.object(supervisor, "_killpg_id") as killpg:
            cleared = supervisor.sweep_stale()

        self.assertIn(app_id, cleared)
        killpg.assert_not_called()
        self.assertFalse(os.path.exists(paths.pidfile(app_id)))
        self.assertFalse(os.path.exists(paths.pgidfile(app_id)))
        self.assertFalse(os.path.exists(paths.bootfile(app_id)))

    def test_sweep_never_touches_record_without_pid_commit_marker(self):
        """Protect a partial record even when its writer is another process."""
        app_id = "uncommitted-cross-process-record"
        self._mkapp(app_id, "# writer has not committed run.pid yet\n")
        with open(paths.pgidfile(app_id), "w") as f:
            f.write("455555")
        with open(paths.bootfile(app_id), "w") as f:
            f.write("boot-A")

        with mock.patch.object(supervisor, "_killpg_id") as killpg:
            cleared = supervisor.sweep_stale()

        self.assertNotIn(app_id, cleared)
        killpg.assert_not_called()
        self.assertFalse(os.path.exists(paths.pidfile(app_id)))
        self.assertEqual(open(paths.pgidfile(app_id)).read(), "455555")
        self.assertEqual(open(paths.bootfile(app_id)).read(), "boot-A")


if __name__ == "__main__":
    unittest.main()

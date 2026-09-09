"""
Unit tests for supervisor app.log rotation + live pipe-drain pump.

supervisor.start() wires the app's stdout/stderr to a PIPE (not a direct file
fd) and spawns _app_log_pump to drain it into <app>/logs/app.log.  Because the
child no longer owns the app.log inode, the pump can rotate the file WHILE the
app runs: when app.log reaches APP_LOG_MAX_BYTES it shifts app.log -> .1 ->
.2 -> ... -> .N (oldest dropped), bounding the on-disk footprint of a long-
running app to (APP_LOG_BACKUPS + 1) * APP_LOG_MAX_BYTES.  A previous run's
leftover app.log is also size-gated out of the way at start() (pre-spawn
rotation, _rotate_app_log).

Two layers are covered:
  1. _rotate_app_log / _shift_log_generations -- the pure path-based rotation
     a pre-spawn call and the pump both drive.
  2. _app_log_pump -- the live drain: feed a pipe > the cap, close it, and
     assert the pump rotated mid-run without losing the most recent bytes and
     without leaving an oversized current file.

Runnable with plain stdlib: `python3 tests/test_supervisor_log_rotation.py`.
"""
import os
import sys
import tempfile
import threading
import unittest

_BASE = tempfile.mkdtemp(prefix="appmgr-logrot.")
_APPS = os.path.join(_BASE, "apps")
_APPMGR = os.path.join(_BASE, "appmgr")
os.environ["APPMGR_APPS_DIR"] = _APPS
os.environ["APPMGR_DIR"] = _APPMGR

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from appmgr import paths, supervisor  # noqa: E402

APP = "log-app"


def _logpath(app_id=APP, gen=None):
    """logdir/app.log (gen=None) or logdir/app.log.<gen>."""
    d = paths.logdir(app_id)
    os.makedirs(d, exist_ok=True)
    name = "app.log" if gen is None else "app.log.%d" % gen
    return os.path.join(d, name)


def _write(path, text):
    with open(path, "w") as f:
        f.write(text)


class LogRotationTests(unittest.TestCase):
    """_rotate_app_log (pre-spawn) and _shift_log_generations (pure shift)."""

    def setUp(self):
        for gen in range(0, 6):
            try:
                os.unlink(_logpath(APP, gen if gen else None))
            except OSError:
                pass

    def test_absent_log_is_a_noop(self):
        supervisor._rotate_app_log(APP, max_bytes=1024, backups=3)
        self.assertFalse(os.path.exists(_logpath(APP)))

    def test_under_limit_is_left_alone(self):
        _write(_logpath(APP), "x" * 100)
        supervisor._rotate_app_log(APP, max_bytes=1024, backups=3)
        with open(_logpath(APP)) as f:
            self.assertEqual(f.read(), "x" * 100)

    def test_exactly_at_limit_is_not_rotated(self):
        _write(_logpath(APP), "x" * 1024)
        supervisor._rotate_app_log(APP, max_bytes=1024, backups=3)
        with open(_logpath(APP)) as f:
            self.assertEqual(len(f.read()), 1024)

    def test_over_limit_rotates_to_dot_one(self):
        _write(_logpath(APP), "OLD" * 1000)
        supervisor._rotate_app_log(APP, max_bytes=1024, backups=3)
        with open(_logpath(APP, 1)) as f:
            self.assertEqual(f.read(), "OLD" * 1000)
        # The fresh app.log is what the CALLER (start/pump) opens next.
        self.assertFalse(os.path.exists(_logpath(APP)))

    def test_keeps_only_backups_generations(self):
        max_bytes = 1024
        for i in range(5):
            _write(_logpath(APP), ("run%d-" % i) + "x" * 2000)
            supervisor._rotate_app_log(APP, max_bytes=max_bytes, backups=2)
        # backups=2 -> only .1 and .2 survive, .2 holds the oldest kept run.
        self.assertTrue(os.path.exists(_logpath(APP, 1)))
        self.assertTrue(os.path.exists(_logpath(APP, 2)))
        self.assertFalse(os.path.exists(_logpath(APP, 3)))
        self.assertFalse(os.path.exists(_logpath(APP)))
        with open(_logpath(APP, 2)) as f:
            self.assertTrue(f.read().startswith("run3-"))

    def test_backups_zero_drops_the_log(self):
        _write(_logpath(APP), "x" * 2000)
        supervisor._rotate_app_log(APP, max_bytes=1024, backups=0)
        self.assertFalse(os.path.exists(_logpath(APP)))

    def test_max_bytes_zero_disables_rotation(self):
        _write(_logpath(APP), "x" * 2000)
        supervisor._rotate_app_log(APP, max_bytes=0, backups=3)
        with open(_logpath(APP)) as f:
            self.assertEqual(len(f.read()), 2000)

    def test_missing_logdir_is_a_noop(self):
        # Fresh install: the logdir does not exist yet. Must not raise.
        supervisor._rotate_app_log("never-installed", max_bytes=1024, backups=3)
        self.assertFalse(os.path.exists(paths.logdir("never-installed")))


class LogPumpTests(unittest.TestCase):
    """_app_log_pump drains a pipe into app.log, rotating live mid-run."""

    def setUp(self):
        for gen in range(0, 6):
            try:
                os.unlink(_logpath(APP, gen if gen else None))
            except OSError:
                pass
        supervisor._log_pumps.pop(APP, None)

    def tearDown(self):
        supervisor._log_pumps.pop(APP, None)

    def _drain(self, write_fn, *, max_bytes, backups):
        """Run the pump against a pipe we feed from the calling thread."""
        read_fd, write_fd = os.pipe()
        logpath = _logpath(APP)
        pump = threading.Thread(
            target=supervisor._app_log_pump,
            args=(read_fd, logpath),
            kwargs={"max_bytes": max_bytes, "backups": backups},
            name="test-log-pump",
            daemon=True,
        )
        supervisor._log_pumps[APP] = pump
        pump.start()
        try:
            write_fn(write_fd)
        finally:
            os.close(write_fd)          # EOF -> pump drains the rest and exits
        pump.join(5.0)
        self.assertFalse(pump.is_alive(),
                         "pump did not reach EOF within 5s -- pipe leak?")

    def test_under_cap_is_one_file(self):
        def w(fd):
            os.write(fd, b"hello\n" * 10)
        self._drain(w, max_bytes=4096, backups=3)
        with open(_logpath(APP), "rb") as f:
            self.assertEqual(f.read(), b"hello\n" * 10)
        self.assertFalse(os.path.exists(_logpath(APP, 1)))

    def test_over_cap_rotates_live_and_keeps_recent(self):
        # 200 KiB of distinct lines into a 4 KiB cap, 2 backups.  The pump MUST
        # rotate while draining; the oldest generation is dropped, but the
        # NEWEST bytes must survive in the current app.log.
        cap = 4096
        lines = [b"line-%07d\n" % i for i in range(20000)]
        payload = b"".join(lines)

        def w(fd):
            mv = memoryview(payload)
            for i in range(0, len(mv), 8192):
                os.write(fd, mv[i:i + 8192])

        self._drain(w, max_bytes=cap, backups=2)

        # No more than `backups` historical generations, and no .(backups+1).
        self.assertFalse(os.path.exists(_logpath(APP, 3)))
        # Every present file is bounded to the cap (the pump rotates BEFORE
        # crossing it, so not even chunk-slack leaks through).
        for gen in (None, 1, 2):
            p = _logpath(APP, gen)
            if os.path.exists(p):
                self.assertLessEqual(os.path.getsize(p), cap,
                                     "app.log%s exceeds cap" % ("" if gen is None else ".%d" % gen))
        # The most recent bytes written must be the tail of the current app.log
        # (proves the pump did not silently drop the live tail on rotation).
        with open(_logpath(APP), "rb") as f:
            current = f.read()
        self.assertTrue(current.endswith(payload[-len(current):]),
                        "current app.log tail does not match the most recent bytes written")

    def test_disabled_cap_is_unbounded_single_file(self):
        payload = b"x" * 100000

        def w(fd):
            os.write(fd, payload)

        self._drain(w, max_bytes=0, backups=3)
        with open(_logpath(APP), "rb") as f:
            self.assertEqual(len(f.read()), len(payload))
        self.assertFalse(os.path.exists(_logpath(APP, 1)))

    def test_join_log_pump_pops_and_joins(self):
        read_fd, write_fd = os.pipe()
        pump = threading.Thread(
            target=supervisor._app_log_pump,
            args=(read_fd, _logpath(APP)),
            kwargs={"max_bytes": 4096, "backups": 3},
            daemon=True,
        )
        supervisor._log_pumps[APP] = pump
        pump.start()
        os.close(write_fd)
        supervisor._join_log_pump(APP, timeout=5.0)
        self.assertFalse(pump.is_alive())
        self.assertNotIn(APP, supervisor._log_pumps)
        # A second join is a no-op (already popped).
        supervisor._join_log_pump(APP, timeout=5.0)

    def test_join_log_pump_unknown_app_is_a_noop(self):
        supervisor._join_log_pump("no-such-app", timeout=1.0)


class LogSpanTests(unittest.TestCase):
    """read_log_span keeps the web window full across a rotation boundary."""

    def setUp(self):
        for gen in range(0, 6):
            try:
                os.unlink(_logpath(APP, gen if gen else None))
            except OSError:
                pass

    def test_no_logs_returns_empty(self):
        self.assertEqual(supervisor.read_log_span(APP, 512), b"")

    def test_current_alone_fills_budget(self):
        # app.log alone >= budget -> app.log.1 is never touched.
        _write(_logpath(APP), "NEW" * 1000)         # 3000 bytes
        _write(_logpath(APP, 1), "OLD" * 1000)      # .1 exists but must be ignored
        data = supervisor.read_log_span(APP, 512)
        # only the last 512 bytes of app.log, no "OLD"
        self.assertEqual(len(data), 512)
        self.assertNotIn(b"OLD", data)
        self.assertTrue(data.endswith(b"NEW"))

    def test_current_small_is_topped_up_from_dot_one(self):
        # Right after a rotation: app.log is tiny, app.log.1 holds the bulk.
        _write(_logpath(APP), "NEW!\n")              # 5 bytes (current run)
        _write(_logpath(APP, 1), "OLD" * 1000)      # 3000 bytes (previous run)
        data = supervisor.read_log_span(APP, 512)
        # Spanning: [app.log.1 tail (507 bytes)] + [app.log (5 bytes)] = 512
        self.assertEqual(len(data), 512)
        # Older (.1) first, newer (app.log) last.
        self.assertTrue(data.startswith(b"OLD"))
        self.assertTrue(data.endswith(b"NEW!\n"))

    def test_missing_current_reads_only_dot_one(self):
        # The sub-ms rotation gap: app.log absent, app.log.1 fills the budget.
        _write(_logpath(APP, 1), "OLD" * 1000)      # 3000 bytes
        # app.log does not exist right now
        data = supervisor.read_log_span(APP, 512)
        self.assertEqual(len(data), 512)
        self.assertTrue(data.endswith(b"OLD"))

    def test_log_tail_stays_single_file(self):
        # _log_tail (startup-failure path) must NOT span -> no cross-run mixing.
        _write(_logpath(APP), "NEW!\n")
        _write(_logpath(APP, 1), "OLD" * 1000)
        tail = supervisor._log_tail(APP, 512)
        self.assertIn("NEW!", tail)
        self.assertNotIn("OLD", tail)


if __name__ == "__main__":
    unittest.main()
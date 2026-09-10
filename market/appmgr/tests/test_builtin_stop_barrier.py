"""Deterministic tests for the built-in inference teardown barrier.

The firmware acknowledges POST /model/inference before its NPU thread runs
rc_model_deinit().  These tests pin the transition appmgr is allowed to trust:
external inference may proceed only after a readback reports enable=0,
status=stopped and actual-fps=0.
"""
import json
import os
import sys
import tempfile
import unittest

_BASE = tempfile.mkdtemp(prefix="appmgr-builtin-barrier.")
os.environ.setdefault("APPMGR_APPS_DIR", os.path.join(_BASE, "apps"))
os.environ.setdefault("APPMGR_DIR", os.path.join(_BASE, "appmgr"))

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from appmgr import builtin, paths, server, state  # noqa: E402


class _Clock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class BuiltinStopBarrierTests(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        self.real = {
            "set": builtin.set_inference,
            "get": builtin.get_inference,
            "monotonic": builtin.time.monotonic,
            "sleep": builtin.time.sleep,
        }
        builtin.time.monotonic = self.clock.monotonic
        builtin.time.sleep = self.clock.sleep
        self.addCleanup(self._restore)

    def _restore(self):
        builtin.set_inference = self.real["set"]
        builtin.get_inference = self.real["get"]
        builtin.time.monotonic = self.real["monotonic"]
        builtin.time.sleep = self.real["sleep"]

    def test_stop_waits_through_stopping_and_nonzero_actual_fps(self):
        calls = []
        snapshots = iter([
            {"iEnable": 0, "sStatus": "stopping", "iActualFPS": 7},
            {"iEnable": 0, "sStatus": "stopped", "iActualFPS": 1},
            {"iEnable": 0, "sStatus": "stopped", "iActualFPS": 0},
        ])
        builtin.set_inference = lambda **kw: (calls.append(kw) or {"code": 0})
        builtin.get_inference = lambda: next(snapshots)

        result = builtin.stop(timeout=2.0, poll_interval=0.25)

        self.assertEqual(calls, [{"enable": False}])
        self.assertTrue(result["stop_confirmed"])
        self.assertEqual(result["confirmed_status"]["sStatus"], "stopped")
        self.assertEqual(result["confirmed_status"]["iActualFPS"], 0)
        self.assertEqual(self.clock.sleeps, [0.25, 0.25])

    def test_timeout_is_fail_closed_and_reports_last_observation(self):
        builtin.set_inference = lambda **kw: {"code": 0}
        builtin.get_inference = lambda: {
            "iEnable": 0, "sStatus": "stopping", "iActualFPS": 0,
        }

        with self.assertRaises(builtin.BuiltinError) as cm:
            builtin.stop(timeout=1.0, poll_interval=0.25)

        message = str(cm.exception)
        self.assertIn("not confirmed within 1.000s", message)
        self.assertIn("fail-closed", message)
        self.assertIn('"sStatus": "stopping"', message)
        self.assertIn("external RKNN was not started", message)

    def test_missing_actual_fps_never_counts_as_stopped(self):
        builtin.set_inference = lambda **kw: {"code": 0}
        builtin.get_inference = lambda: {"iEnable": 0, "sStatus": "stopped"}

        with self.assertRaises(builtin.BuiltinError) as cm:
            builtin.stop(timeout=0.0, poll_interval=0.1)

        self.assertIn("missing fields: iActualFPS", str(cm.exception))

    def test_lost_post_response_can_be_proven_safe_by_readback(self):
        def lost_response(**_kw):
            raise builtin.BuiltinError("connection reset after write")

        builtin.set_inference = lost_response
        builtin.get_inference = lambda: {
            "iEnable": 0, "sStatus": "stopped", "iActualFPS": 0,
        }

        result = builtin.stop(timeout=0.0, poll_interval=0.1)

        self.assertTrue(result["stop_confirmed"])
        self.assertIn("response lost", result["warning"])


class ExternalStartFailClosedTests(unittest.TestCase):
    APP = "barrier-target"

    def setUp(self):
        apps = os.path.join(_BASE, "apps")
        appmgr = os.path.join(_BASE, "appmgr")
        os.makedirs(os.path.join(apps, self.APP), exist_ok=True)
        os.makedirs(appmgr, exist_ok=True)
        with open(os.path.join(apps, self.APP, "manifest.json"), "w") as f:
            json.dump({"id": self.APP, "version": "1.0.0"}, f)

        pins = {
            "APPS_DIR": apps,
            "APPMGR_DIR": appmgr,
            "STATE_FILE": os.path.join(apps, "state.json"),
            "BUSY_FILE": os.path.join(appmgr, "busy.lock"),
            "AUDIT_LOG": os.path.join(appmgr, "audit.log"),
        }
        self.path_real = {name: getattr(paths, name) for name in pins}
        for name, value in pins.items():
            setattr(paths, name, value)

        self.real = {
            "builtin_stop": server.builtin.stop,
            "builtin_running": server._builtin_running,
            "sup_start": server.supervisor.start,
            "sup_stop": server.supervisor.stop,
            "sup_running": server.supervisor.is_running,
        }
        self.started = []
        self.stopped = []
        server._builtin_running = lambda: True
        server.supervisor.start = lambda app_id, **kw: (self.started.append(app_id) or 4321)
        server.supervisor.stop = lambda app_id, **kw: (self.stopped.append(app_id) or {})
        server.supervisor.is_running = lambda app_id: None
        self.coordinator = server._coordinator()
        self.real_ipc_probe = self.coordinator.ipc_dependency_probe
        self.coordinator.ipc_dependency_probe = lambda _plan: {"available": True}
        state.set_active(None, None)
        self.addCleanup(self._restore)

    def _restore(self):
        # Do not leak this test's active selection into later host suites.
        self.coordinator.ipc_dependency_probe = self.real_ipc_probe
        state.set_active(None, None)
        server.builtin.stop = self.real["builtin_stop"]
        server._builtin_running = self.real["builtin_running"]
        server.supervisor.start = self.real["sup_start"]
        server.supervisor.stop = self.real["sup_stop"]
        server.supervisor.is_running = self.real["sup_running"]
        for name, value in self.path_real.items():
            setattr(paths, name, value)

    def test_switch_timeout_never_calls_external_start_or_stop(self):
        server.builtin.stop = lambda *a, **k: (_ for _ in ()).throw(
            builtin.BuiltinError(
                "built-in inference stop was not confirmed; sStatus=stopping"))

        with self.assertRaises(builtin.BuiltinError) as cm:
            server.do_switch(self.APP)

        self.assertIn("external RKNN was not started", str(cm.exception))
        self.assertEqual(self.started, [])
        self.assertEqual(self.stopped, [],
                         "barrier failure must occur before mutating external apps")
        self.assertIsNone(state.get_active())

    def test_confirmed_barrier_precedes_external_process_mutation(self):
        events = []
        server.builtin.stop = lambda *a, **k: (
            events.append("builtin-confirmed") or {"stop_confirmed": True})
        server.supervisor.stop = lambda app_id, **kw: (
            events.append("external-stop") or {})
        server.supervisor.start = lambda app_id, **kw: (
            events.append(("external-start", kw)) or 4321)

        result = server.do_switch(self.APP)

        self.assertEqual(events[:2], ["builtin-confirmed", "external-stop"])
        self.assertEqual(events[2][0], "external-start")
        launch = events[2][1]
        self.assertTrue(launch["npu_managed"])
        self.assertTrue(launch["instance_id"])
        self.assertGreaterEqual(launch["instance_generation"], 1)
        self.assertTrue(launch["result_gateway_sock"].endswith(
            "appmgr-results.sock"))
        self.assertEqual(result["active_app"], self.APP)

    def test_unconfirmed_return_value_cannot_mint_launch_marker(self):
        server.builtin.stop = lambda *a, **k: {"code": 0}
        with self.assertRaises(builtin.BuiltinError):
            server.do_switch(self.APP)
        self.assertEqual(self.started, [])
        self.assertEqual(self.stopped, [])


if __name__ == "__main__":
    unittest.main()

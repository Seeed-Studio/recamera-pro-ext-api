"""Targeted tests for appmgr's broker-vs-barrier NPU routing.

When the authoritative broker socket exists, appmgr must NOT pre-stop the
built-in detector. It launches the child with RECAMERA_NPU_BROKER_REQUIRED=1
and leaves lease acquisition to the child, which then fails closed if the
broker is unusable.

Without the broker socket, appmgr preserves the legacy strict CGI stop barrier
and launches the child with RECAMERA_NPU_MANAGED=appmgr-v1.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

_BASE = tempfile.mkdtemp(prefix="appmgr-npu-broker-route.")
os.environ.setdefault("APPMGR_APPS_DIR", os.path.join(_BASE, "apps"))
os.environ.setdefault("APPMGR_DIR", os.path.join(_BASE, "appmgr"))
os.environ.setdefault("APPMGR_INFERENCE_CONTROL_SOCK",
                      os.path.join(_BASE, "run", "recamera",
                                   "inference-control.sock"))

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from appmgr import paths, server, state  # noqa: E402


class BrokerRouteTests(unittest.TestCase):
    APP = "broker-target"

    def setUp(self):
        apps = os.path.join(_BASE, "apps")
        appmgr = os.path.join(_BASE, "appmgr")
        sock = os.path.join(_BASE, "run", "recamera", "inference-control.sock")
        os.makedirs(os.path.join(apps, self.APP), exist_ok=True)
        os.makedirs(appmgr, exist_ok=True)
        os.makedirs(os.path.dirname(sock), exist_ok=True)
        with open(os.path.join(apps, self.APP, "manifest.json"), "w") as f:
            json.dump({"id": self.APP, "version": "1.0.0"}, f)

        pins = {
            "APPS_DIR": apps,
            "APPMGR_DIR": appmgr,
            "STATE_FILE": os.path.join(apps, "state.json"),
            "BUSY_FILE": os.path.join(appmgr, "busy.lock"),
            "AUDIT_LOG": os.path.join(appmgr, "audit.log"),
            "INFERENCE_CONTROL_SOCK": sock,
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
        self.builtin_stop_calls = 0
        server.builtin.stop = lambda *a, **k: (
            setattr(self, "builtin_stop_calls", self.builtin_stop_calls + 1)
            or {"stop_confirmed": True})
        server._builtin_running = lambda: True
        server.supervisor.start = lambda app_id, **kw: (
            self.started.append((app_id, kw)) or 4321)
        server.supervisor.stop = lambda app_id, **kw: (
            self.stopped.append(app_id) or {})
        server.supervisor.is_running = lambda app_id: None
        # This unit fixture models routing with marker files and a fake
        # supervisor. Real readiness/handshake peers live in test_ipc_dependencies.
        self.coordinator = server._coordinator()
        self.real_ipc_probe = self.coordinator.ipc_dependency_probe
        self.coordinator.ipc_dependency_probe = lambda _plan: {"available": True}
        state.set_active(None, None)
        self.addCleanup(self._restore)

    def _restore(self):
        self.coordinator.ipc_dependency_probe = self.real_ipc_probe
        state.set_active(None, None)
        server.builtin.stop = self.real["builtin_stop"]
        server._builtin_running = self.real["builtin_running"]
        server.supervisor.start = self.real["sup_start"]
        server.supervisor.stop = self.real["sup_stop"]
        server.supervisor.is_running = self.real["sup_running"]
        for name, value in self.path_real.items():
            setattr(paths, name, value)

    def _set_broker(self, present: bool):
        if present:
            with open(paths.INFERENCE_CONTROL_SOCK, "wb"):
                pass
        else:
            try:
                os.remove(paths.INFERENCE_CONTROL_SOCK)
            except FileNotFoundError:
                pass

    def test_activate_prefers_broker_and_skips_builtin_barrier(self):
        self._set_broker(True)

        result = server.do_activate(self.APP)

        self.assertEqual(result["active"], self.APP)
        self.assertEqual(self.builtin_stop_calls, 0)
        self.assertEqual(self.stopped, [self.APP])
        self.assertEqual(self.started[0][0], self.APP)
        self.assertTrue(self.started[0][1]["npu_broker_required"])
        self.assertTrue(self.started[0][1]["instance_id"])
        self.assertTrue(self.started[0][1]["result_gateway_sock"])

    def test_switch_uses_legacy_barrier_without_broker(self):
        self._set_broker(False)
        state.set_active("prev-app", "0.9.0")

        result = server.do_switch(self.APP)

        self.assertEqual(result["active_app"], self.APP)
        self.assertEqual(self.builtin_stop_calls, 1)
        self.assertEqual(self.stopped, ["prev-app", self.APP])
        self.assertEqual(self.started[0][0], self.APP)
        self.assertTrue(self.started[0][1]["npu_managed"])
        self.assertTrue(self.started[0][1]["instance_id"])
        self.assertTrue(self.started[0][1]["result_gateway_sock"])

    def test_boot_restore_uses_same_broker_route(self):
        self._set_broker(True)
        state.set_active(self.APP, "1.0.0")

        server._boot_restore()

        self.assertEqual(self.builtin_stop_calls, 0)
        self.assertEqual(self.started[0][0], self.APP)
        self.assertTrue(self.started[0][1]["npu_broker_required"])
        self.assertTrue(self.started[0][1]["instance_id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

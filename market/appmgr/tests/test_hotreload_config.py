"""
Unit tests for appmgr do_set_config apply-based routing (DESIGN §1.1/§3.2).

set_config used to unconditionally stop+start. Now it inspects the `apply`
field of every CHANGED config item:

  * all changed items apply:"live"  -> write config.json + SIGHUP (reload),
                                        NO restart;
  * any item apply:"restart" (or missing the field) -> stop+start as before;
  * a LIVE change to a NOT-running app just writes config.json (nothing to
    signal) -- the values apply on the next start.

supervisor is fully stubbed (no real processes / procfs). The filesystem layout
is redirected onto throwaway temp dirs via env BEFORE the package is imported
(paths.py snapshots the env at import time), mirroring test_uninstall.py.

Runnable with plain stdlib: `python3 tests/test_hotreload_config.py` (or pytest).
"""
import json
import os
import sys
import tempfile
import unittest

_BASE = tempfile.mkdtemp(prefix="appmgr-hotcfg.")
os.environ["APPMGR_APPS_DIR"] = os.path.join(_BASE, "apps")
os.environ["APPMGR_DIR"] = os.path.join(_BASE, "appmgr")
os.environ["APPMGR_VENVS_DIR"] = os.path.join(_BASE, "venvs")
os.environ["APPMGR_MODEL_ROOTS"] = os.path.join(_BASE, "models")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from appmgr import server, paths, state, config as appconfig  # noqa: E402


class SetConfigApplyTests(unittest.TestCase):
    def setUp(self):
        paths.ensure_dirs()
        state.set_active(None, None)
        self.calls = []                 # ordered supervisor interaction log
        self.running = {}               # app_id -> pid

        def fake_is_running(app_id):
            return self.running.get(app_id)

        def fake_reload(app_id):
            self.calls.append(("reload", app_id))
            return app_id in self.running

        def fake_stop(app_id, *a, **k):
            self.calls.append(("stop", app_id))
            self.running.pop(app_id, None)
            return {"app": app_id, "signalled": True}

        def fake_start(app_id, *a, **k):
            self.calls.append(("start", app_id))
            self.running[app_id] = 9999
            return 9999

        # Patch the REAL supervisor module attributes -> must be undone, or the
        # stubs leak into every later test module in the same pytest process.
        self._orig_sup = {n: getattr(server.supervisor, n)
                          for n in ("is_running", "reload", "stop", "start")}
        self._orig_builtin_stop = server.builtin.stop
        self.addCleanup(lambda: [setattr(server.supervisor, n, v)
                                 for n, v in self._orig_sup.items()])
        self.addCleanup(lambda: setattr(
            server.builtin, "stop", self._orig_builtin_stop))
        server.supervisor.is_running = fake_is_running
        server.supervisor.reload = fake_reload
        server.supervisor.stop = fake_stop
        server.supervisor.start = fake_start
        server.builtin.stop = lambda *a, **k: {"stop_confirmed": True}

    def _make_app(self, app_id="demo"):
        d = paths.app_dir(app_id)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "manifest.json"), "w") as f:
            json.dump({
                "id": app_id, "version": "1.0.0", "name": app_id,
                "config_schema": {
                    "conf": {"type": "number", "apply": "live",
                             "default": 0.35, "min": 0.0, "max": 1.0, "step": 0.05},
                    "iou": {"type": "number", "apply": "live",
                            "default": 0.45, "min": 0.0, "max": 1.0, "step": 0.05},
                    # restart-class param
                    "input_size": {"type": "number", "apply": "restart",
                                   "default": 640, "min": 320, "max": 640, "step": 32},
                    # no apply field -> must be treated as restart (conservative)
                    "legacy": {"type": "number", "default": 1,
                               "min": 0, "max": 9, "step": 1},
                },
            }, f)
        return d

    def _saved(self, app_id="demo"):
        return appconfig.load_user_config(app_id)

    # -- all-live change on a RUNNING app -> reload, no restart ------------- #
    def test_live_change_running_reloads_no_restart(self):
        self._make_app()
        self.running["demo"] = 4321
        res = server.do_set_config("demo", {"conf": 0.7, "iou": 0.6})
        self.assertEqual(res["applied"], "live")
        self.assertTrue(res["reloaded"])
        self.assertFalse(res["restarted"])
        self.assertEqual(self.calls, [("reload", "demo")])
        self.assertNotIn(("stop", "demo"), self.calls)
        self.assertEqual(self._saved()["conf"], 0.7)

    # -- all-live change on a STOPPED app -> just persist, no signal -------- #
    def test_live_change_not_running_no_signal(self):
        self._make_app()
        res = server.do_set_config("demo", {"conf": 0.5})
        self.assertEqual(res["applied"], "live")
        self.assertFalse(res["reloaded"])
        self.assertFalse(res["restarted"])
        self.assertEqual(self.calls, [], "no reload/stop/start when not running")
        self.assertEqual(self._saved()["conf"], 0.5)

    # -- restart-class item on the active running app -> stop+start -------- #
    def test_restart_change_bounces_active_app(self):
        self._make_app()
        state.set_active("demo", "1.0.0")
        self.running["demo"] = 4321
        res = server.do_set_config("demo", {"input_size": 320})
        self.assertEqual(res["applied"], "restart")
        self.assertTrue(res["restarted"])
        self.assertFalse(res["reloaded"])
        self.assertEqual(self.calls, [("stop", "demo"), ("start", "demo")])
        self.assertEqual(self._saved()["input_size"], 320)

    # -- item WITHOUT apply field is conservatively treated as restart ----- #
    def test_missing_apply_field_is_restart(self):
        self._make_app()
        state.set_active("demo", "1.0.0")
        self.running["demo"] = 4321
        res = server.do_set_config("demo", {"legacy": 3})
        self.assertEqual(res["applied"], "restart")
        self.assertTrue(res["restarted"])

    # -- mixed live+restart in ONE change -> restart wins ------------------ #
    def test_mixed_change_is_restart(self):
        self._make_app()
        state.set_active("demo", "1.0.0")
        self.running["demo"] = 4321
        res = server.do_set_config("demo", {"conf": 0.6, "input_size": 512})
        self.assertEqual(res["applied"], "restart")
        self.assertTrue(res["restarted"])
        self.assertFalse(res["reloaded"])
        self.assertEqual(self.calls, [("stop", "demo"), ("start", "demo")])

    # -- restart-class change to a NON-active app -> persist only ---------- #
    def test_restart_change_non_active_persists_only(self):
        self._make_app()
        state.set_active("other", "1.0.0")     # demo is not the active app
        self.running["demo"] = 4321            # running but not active
        res = server.do_set_config("demo", {"input_size": 320})
        self.assertEqual(res["applied"], "restart")
        self.assertFalse(res["restarted"])
        self.assertEqual(self.calls, [], "non-active app is not bounced")
        self.assertEqual(self._saved()["input_size"], 320)


if __name__ == "__main__":
    unittest.main(verbosity=2)

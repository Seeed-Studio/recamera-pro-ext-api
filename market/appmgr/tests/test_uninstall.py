"""
Unit tests for appmgr uninstall (server.do_uninstall + installer.uninstall).

Covers the four things the uninstall path must get right:
  * install -> uninstall removes /userdata/local/apps/<id>/ and clears active state;
  * a RUNNING app is stopped BEFORE its dir is deleted;
  * uninstalling an unknown / invalid app is a clean ValueError, not a crash;
  * SHARED models under /userdata/local/models are never touched;
  * the future per-app venv /userdata/local/venvs/<id> is removed if present.

Everything is redirected onto throwaway temp dirs via env BEFORE importing the
package (paths.py reads the layout from the env at import time). supervisor is
stubbed so no real process / procfs is involved.

Runnable with plain stdlib: `python3 tests/test_uninstall.py` (or via pytest).
"""
import json
import os
import sys
import tempfile
import unittest

# Redirect the whole appmgr filesystem layout at throwaway temp dirs BEFORE the
# package is imported (paths.py snapshots these at import time).
_BASE = tempfile.mkdtemp(prefix="appmgr-uninst.")
_APPS = os.path.join(_BASE, "apps")
_APPMGR = os.path.join(_BASE, "appmgr")
_VENVS = os.path.join(_BASE, "venvs")
_MODELS = os.path.join(_BASE, "models")
os.environ["APPMGR_APPS_DIR"] = _APPS
os.environ["APPMGR_DIR"] = _APPMGR
os.environ["APPMGR_VENVS_DIR"] = _VENVS
os.environ["APPMGR_MODEL_ROOTS"] = _MODELS

# Import the package (server.py uses relative imports, so we import it AS a
# package member, not as a bare top-level module).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from appmgr import server, paths, state, installer, visualization  # noqa: E402


class UninstallTests(unittest.TestCase):
    def setUp(self):
        # pytest imports sibling modules before executing tests, so paths may
        # already have been imported with another fixture layout.
        self._saved_paths = {
            "APPS_DIR": paths.APPS_DIR,
            "APPMGR_DIR": paths.APPMGR_DIR,
            "VENVS_DIR": paths.VENVS_DIR,
            "STATE_FILE": paths.STATE_FILE,
        }
        paths.APPS_DIR = _APPS
        paths.APPMGR_DIR = _APPMGR
        paths.VENVS_DIR = _VENVS
        paths.STATE_FILE = os.path.join(_APPS, "state.json")
        self.addCleanup(lambda: [setattr(paths, key, value)
                                 for key, value in self._saved_paths.items()])
        paths.ensure_dirs()
        os.makedirs(_VENVS, exist_ok=True)
        os.makedirs(_MODELS, exist_ok=True)
        # Reset single-active state between tests.
        state.set_active(None, None)
        # Record supervisor interactions; default: nothing running.
        self.stop_calls = []
        self.running = {}   # app_id -> pid (or absent = not running)

        def fake_is_running(app_id):
            return self.running.get(app_id)

        def fake_stop(app_id, *a, **k):
            # Capture whether the app dir still exists at stop time so we can
            # assert stop happens BEFORE the rmtree.
            self.stop_calls.append(
                {"id": app_id, "dir_existed": os.path.isdir(paths.app_dir(app_id))})
            self.running.pop(app_id, None)
            return {"app": app_id, "signalled": True}

        # Patch the REAL supervisor module attributes -> must be undone, or the
        # stubs leak into every later test module in the same pytest process.
        self._orig_sup = {n: getattr(server.supervisor, n)
                          for n in ("is_running", "stop")}
        self.addCleanup(lambda: [setattr(server.supervisor, n, v)
                                 for n, v in self._orig_sup.items()])
        server.supervisor.is_running = fake_is_running
        server.supervisor.stop = fake_stop

    # -- helpers ------------------------------------------------------------ #
    def _make_app(self, app_id, version="1.0.0"):
        d = paths.app_dir(app_id)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "manifest.json"), "w") as f:
            json.dump({"id": app_id, "version": version, "name": app_id}, f)
        with open(os.path.join(d, "app.py"), "w") as f:
            f.write("# app\n")
        return d

    # -- install -> uninstall ---------------------------------------------- #
    def test_uninstall_removes_dir_and_clears_active(self):
        d = self._make_app("fall-detection")
        state.set_active("fall-detection", "1.0.0")
        res = server.do_uninstall("fall-detection")
        self.assertTrue(res["uninstalled"])
        self.assertTrue(res["was_active"])
        self.assertFalse(res["stopped"])          # was not running
        self.assertFalse(os.path.isdir(d), "app dir must be gone")
        self.assertIsNone(state.get_active(), "active state must be cleared")

    def test_uninstall_running_app_stops_before_delete(self):
        d = self._make_app("voice-transcribe")
        state.set_active("voice-transcribe", "1.0.0")
        self.running["voice-transcribe"] = 4321       # pretend it's running
        res = server.do_uninstall("voice-transcribe")
        self.assertTrue(res["stopped"])
        self.assertEqual(len(self.stop_calls), 1)
        # stop() must have run while the dir still existed (stop-then-delete order)
        self.assertTrue(self.stop_calls[0]["dir_existed"])
        self.assertFalse(os.path.isdir(d))
        self.assertIsNone(state.get_active())

    def test_uninstall_non_active_leaves_other_active_untouched(self):
        self._make_app("qrcode-reader")
        state.set_active("some-other-app", "2.0.0")
        res = server.do_uninstall("qrcode-reader")
        self.assertFalse(res["was_active"])
        self.assertEqual(state.get_active(), "some-other-app",
                         "uninstalling a non-active app must not clear active")

    def test_uninstall_removes_only_its_stream_burn_in_source(self):
        self._make_app("osd-app")
        visualization.save({
            "osd": {"enabled": True, "sources": ["osd-app", "other-app"]},
        })
        self.addCleanup(lambda: visualization.save(visualization.defaults()))

        class Bridge:
            def __init__(self):
                self.reloaded = []

            def reload(self, value):
                self.reloaded.append(value)

        bridge = Bridge()
        previous_bridge = server._visualization_bridge_instance
        server._visualization_bridge_instance = bridge
        self.addCleanup(
            lambda: setattr(server, "_visualization_bridge_instance", previous_bridge))

        server.do_uninstall("osd-app")

        self.assertEqual(visualization.load(), {
            "osd": {"enabled": True, "sources": ["other-app"]},
        })
        self.assertEqual(bridge.reloaded[-1], visualization.load())

    # -- error / idempotency ------------------------------------------------ #
    def test_uninstall_unknown_app_errors(self):
        with self.assertRaises(ValueError):
            server.do_uninstall("never-installed")

    def test_uninstall_invalid_id_errors(self):
        for bad in ("Bad_Id", "../etc", "UPPER", "a" * 65, ""):
            with self.assertRaises(ValueError):
                server.do_uninstall(bad)

    def test_double_uninstall_second_is_clean_error(self):
        self._make_app("dupe")
        server.do_uninstall("dupe")               # first succeeds
        with self.assertRaises(ValueError):       # second: gone -> ValueError, no crash
            server.do_uninstall("dupe")

    # -- shared models must survive ---------------------------------------- #
    def test_uninstall_does_not_delete_shared_models(self):
        self._make_app("model-user")
        shared = os.path.join(_MODELS, "asr")
        os.makedirs(shared, exist_ok=True)
        model_file = os.path.join(shared, "embedding.rknn")
        with open(model_file, "wb") as f:
            f.write(b"weights" * 100)
        server.do_uninstall("model-user")
        self.assertTrue(os.path.isfile(model_file),
                        "shared model file must NOT be removed by app uninstall")
        self.assertTrue(os.path.isdir(_MODELS))

    # -- per-app venv hook -------------------------------------------------- #
    def test_uninstall_removes_per_app_venv_if_present(self):
        self._make_app("venv-app")
        venv = paths.venv_dir("venv-app")
        os.makedirs(os.path.join(venv, "bin"), exist_ok=True)
        with open(os.path.join(venv, "bin", "python"), "w") as f:
            f.write("#!/bin/sh\n")
        server.do_uninstall("venv-app")
        self.assertFalse(os.path.isdir(venv), "per-app venv must be removed")

    def test_uninstall_no_venv_is_fine(self):
        # Most (vision) apps have no venv -- uninstall must not choke on absence.
        self._make_app("no-venv-app")
        self.assertFalse(os.path.isdir(paths.venv_dir("no-venv-app")))
        res = server.do_uninstall("no-venv-app")   # must not raise
        self.assertTrue(res["uninstalled"])

    # -- installer.uninstall unit (below the busy-gate wrapper) ------------- #
    def test_installer_uninstall_idempotent_on_missing(self):
        # No dirs at all -> no-op, no error (idempotent by design).
        installer.uninstall("ghost-app")


if __name__ == "__main__":
    unittest.main(verbosity=2)

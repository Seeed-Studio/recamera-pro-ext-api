"""Unit tests: a replayed install-rollback must not discard a later config POST.

The write path is the canonical ``<appdata>/<id>/config.json``
(``config.write_user_config`` <-> ``config_path``), shared by the panel
(``POST /api/appMgr/config``, ``PUT /api/app-center/v1/apps/<id>/config``) and
``python3 -m appmgr config <id> '<json>'``.  A config POST is persisted
immediately and never waits for the app to be running.

The bug these lock down is a LATER writer of the same file: an install
transaction snapshots the config files when it starts
(``snapshot_upgrade_config``) and, when the install does not publish, the
rollback ``restore_upgrade_config`` replays that snapshot.  The replay can
happen long after it was taken -- the boot path replays a journal left behind by
an interrupted install -- so a config POST that landed in between used to be
silently destroyed: the snapshot said ``<appdata>/<id>/config.json`` was absent,
so the replay unlinked the file the user had just saved.

Observed on 192.168.10.33 (2026-09-29): a config POST audited at 14:29:15
(``action=config, noop=false, applied=restart``) was gone a minute later because
the interrupted install of that app was rolled back at 14:30:21.

Filesystem layout is redirected onto a throwaway temp dir via env BEFORE the
package is imported (paths.py snapshots the env at import time), mirroring
test_config_merge.py.  Runnable with plain stdlib:
``python3 tests/test_config_rollback_replay.py`` (or pytest).
"""
import json
import os
import sys
import tempfile
import unittest

_BASE = os.path.realpath(tempfile.mkdtemp(prefix="appmgr-cfgreplay."))
os.environ["APPMGR_APPS_DIR"] = os.path.join(_BASE, "apps")
os.environ["APPMGR_APPDATA_DIR"] = os.path.join(_BASE, "appdata")
os.environ["APPMGR_DIR"] = os.path.join(_BASE, "appmgr")
os.environ["APPMGR_VENVS_DIR"] = os.path.join(_BASE, "venvs")
os.environ["APPMGR_MODEL_ROOTS"] = os.path.join(_BASE, "models")

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(_REPO, "market"))   # `from appmgr import ...`
sys.path.insert(0, _REPO)                           # `from kit import ...`

from appmgr import config as appconfig, paths, server  # noqa: E402
from kit import config as kitconfig                    # noqa: E402

APP = "eldercare-monitor"
# One restart-class and one live-class parameter: persistence does not depend on
# the apply mode (the file is written before the apply branch picks restart vs
# SIGHUP).
RESTART_KEY = "min_silence_sec"
LIVE_KEY = "confidence"
MANIFEST = {
    "id": APP,
    "version": "1.0.0",
    "name": "Elder Care Monitor",
    "config_schema": {"groups": [{
        "key": "general", "title": "General",
        "items": [
            {"key": RESTART_KEY, "type": "number", "apply": "restart",
             "default": 0.6, "min": 0.2, "max": 3.0, "step": 0.1},
            {"key": LIVE_KEY, "type": "number", "apply": "live",
             "default": 0.4, "min": 0.05, "max": 0.95, "step": 0.05},
        ],
    }]},
}

CANONICAL = ("appdata", "config.json")


def _record(snapshot, scope, name):
    for rec in snapshot["files"]:
        if (rec.get("scope"), rec.get("name")) == (scope, name):
            return rec
    raise AssertionError("no %s/%s record in %r" % (scope, name, snapshot))


class ConfigRollbackReplayTests(unittest.TestCase):
    def setUp(self):
        paths.ensure_dirs()
        app_dir = paths.app_dir(APP)
        os.makedirs(app_dir, exist_ok=True)
        with open(os.path.join(app_dir, "manifest.json"), "w") as f:
            json.dump(MANIFEST, f)
        for pathname in (appconfig.config_path(APP),
                         appconfig.legacy_config_path(APP)):
            try:
                os.remove(pathname)
            except FileNotFoundError:
                pass
        # paths.py froze its layout at import time; point the app-side reader
        # (kit.config, used by kit/app.py _bind_params) at the same tree so this
        # module holds under a shared pytest process, where an earlier test
        # module's env is the one that got frozen.
        os.environ["APPMGR_APPDATA_DIR"] = paths.APPDATA_DIR
        # No app is running in this suite; the write path must not need one.
        original = server.supervisor.is_running
        self.addCleanup(lambda: setattr(server.supervisor, "is_running", original))
        server.supervisor.is_running = lambda app_id: None

    def _post(self, values):
        result = server.do_set_config(APP, values)
        self.assertTrue(result["noop"] is False, result)
        self.assertTrue(result["saved"], result)
        return result

    def _saved(self):
        return appconfig.load_user_config(APP)

    # -- the write path itself --------------------------------------------- #
    def test_config_post_is_persisted_on_the_path_the_app_reads(self):
        """POST -> <appdata>/<id>/config.json, and the kit read path sees it."""
        self._post({RESTART_KEY: 1.2})
        canonical = appconfig.config_path(APP)
        self.assertEqual(canonical,
                         os.path.join(paths.appdata_dir(APP), "config.json"))
        self.assertTrue(os.path.isfile(canonical))
        with open(canonical) as f:
            self.assertEqual(json.load(f), {RESTART_KEY: 1.2})
        self.assertEqual(self._saved(), {RESTART_KEY: 1.2})
        # The app-side reader (kit.config, used by kit/app.py _bind_params).
        self.assertEqual(
            kitconfig.effective_config(paths.app_dir(APP)).get(RESTART_KEY), 1.2)

    # -- the bug: a replayed rollback must not eat a later write ----------- #
    def test_replayed_rollback_keeps_a_restart_class_post(self):
        snapshot = appconfig.snapshot_upgrade_config(APP)
        self.assertEqual(_record(snapshot, *CANONICAL).get("present"), False)
        self._post({RESTART_KEY: 1.2})              # user saves via panel / CLI
        appconfig.restore_upgrade_config(snapshot)  # interrupted install undone
        self.assertEqual(self._saved().get(RESTART_KEY), 1.2)
        self.assertEqual(
            kitconfig.effective_config(paths.app_dir(APP)).get(RESTART_KEY), 1.2)

    def test_replayed_rollback_keeps_a_live_class_post(self):
        snapshot = appconfig.snapshot_upgrade_config(APP)
        self._post({LIVE_KEY: 0.8})
        appconfig.restore_upgrade_config(snapshot)
        self.assertEqual(self._saved().get(LIVE_KEY), 0.8)

    # -- the rollback keeps its byte-exact contract everywhere else -------- #
    def test_replayed_rollback_still_restores_untouched_files(self):
        legacy = appconfig.legacy_config_path(APP)
        with open(legacy, "w") as f:
            json.dump({"zone": [[0, 0], [1, 1]]}, f)
        snapshot = appconfig.snapshot_upgrade_config(APP)
        with open(legacy, "w") as f:                 # the transaction moved it
            json.dump({"zone": [[0, 0], [0, 0]]}, f)
        appconfig.restore_upgrade_config(snapshot)
        with open(legacy) as f:
            self.assertEqual(json.load(f), {"zone": [[0, 0], [1, 1]]})

    def test_replayed_rollback_still_reverts_the_transactions_own_rewrite(self):
        """A file the snapshot captured is restored even if it changed since.

        The upgrade rewrites the canonical file itself (it drops keys the new
        manifest no longer declares); that rewrite MUST be reverted, so this
        transition is deliberately not guarded.
        """
        appconfig.write_user_config(APP, {RESTART_KEY: 0.6})
        snapshot = appconfig.snapshot_upgrade_config(APP)
        self.assertEqual(_record(snapshot, *CANONICAL).get("present"), True)
        appconfig.write_user_config(APP, {RESTART_KEY: None})  # upgrade pruned
        self.assertEqual(self._saved(), {})
        appconfig.restore_upgrade_config(snapshot)
        self.assertEqual(self._saved().get(RESTART_KEY), 0.6)


if __name__ == "__main__":
    unittest.main(verbosity=2)

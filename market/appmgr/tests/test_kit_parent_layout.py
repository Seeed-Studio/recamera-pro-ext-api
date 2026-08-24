"""KIT_PARENT must name the immutable platform Kit package parent.

Why this test exists
--------------------
`paths.KIT_PARENT` defaulted to `/userdata/local/kit` for a long time, but that
is the kit PACKAGE, not the directory containing it. Nothing caught the mistake
because every `app.py` shipped a ~40-line sys.path bootstrap that probed for the
real location and silently rescued it.

When that bootstrap was removed (KIT_APP_SHAPE_SPEC §5.1) the wrong value became
fatal: on device, all 9 apps died at once with

    ModuleNotFoundError: No module named 'kit'

The production firmware now installs Kit in system site-packages.  The historic
release/kit-extra sideload still targets /userdata and is deliberately not the
production default; APPMGR_KIT_PARENT remains its explicit compatibility hook.
"""
import os
import re
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_MARKET = os.path.dirname(os.path.dirname(_HERE))
_REPO = os.path.dirname(_MARKET)
if _MARKET not in sys.path:
    sys.path.insert(0, _MARKET)

from appmgr import paths, supervisor            # noqa: E402

INSTALL_SH = os.path.join(_REPO, "release", "kit-extra", "INSTALL.sh")


class KitParentLayoutTests(unittest.TestCase):

    def test_platform_default_is_immutable_system_site_packages(self):
        self.assertEqual(paths.KIT_PARENT, "/usr/lib/python3.11/site-packages")
        self.assertEqual(paths.KIT_DIR,
                         "/usr/lib/python3.11/site-packages/kit")

    def test_legacy_sideload_is_not_the_platform_default(self):
        """The old writable installer remains explicit compatibility only."""
        with open(INSTALL_SH, encoding="utf-8") as f:
            src = f.read()
        m = re.search(r'^KIT_DST=(\S+)', src, re.M)
        self.assertIsNotNone(m, "INSTALL.sh no longer defines KIT_DST")
        kit_dst = m.group(1).strip().strip('"').strip("'")

        self.assertEqual(kit_dst, "/userdata/local/kit")
        self.assertNotEqual(paths.KIT_DIR, kit_dst)

    def test_kit_parent_is_the_parent_not_the_package(self):
        self.assertEqual(os.path.basename(paths.KIT_DIR), "kit")
        self.assertNotEqual(
            os.path.basename(paths.KIT_PARENT), "kit",
            "KIT_PARENT points AT the kit package; it must point at its parent "
            "(this is the exact off-by-one that killed all 9 apps on device).")

    def test_launch_is_self_locating_not_dash_m(self):
        """The command must run run.py by path, so a bad PYTHONPATH is survivable."""
        manifest = {"entry": "app.py", "models": [{"file": "models/x.rknn"}]}
        cmd = supervisor._build_cmd("yolo-detector", manifest)

        self.assertNotIn("-m", cmd,
                         "`-m kit.run` resolves through PYTHONPATH; one wrong "
                         "KIT_PARENT then kills every app at once")
        run_py = os.path.join(paths.KIT_DIR, "run.py")
        self.assertIn(run_py, cmd, f"expected {run_py} in {cmd}")
        # run.py recovers KIT_PARENT from __file__, so <KIT_PARENT>/kit/run.py
        # is exactly the layout it assumes.
        self.assertEqual(
            os.path.dirname(os.path.dirname(run_py)), paths.KIT_PARENT)

    def test_repo_kit_run_makes_the_same_assumption(self):
        """kit/run.py derives KIT_PARENT as dirname(dirname(__file__))."""
        run_py = os.path.join(_REPO, "kit", "run.py")
        self.assertTrue(os.path.isfile(run_py), "kit/run.py is missing")
        self.assertEqual(os.path.dirname(os.path.dirname(run_py)), _REPO,
                         "repo layout no longer matches <KIT_PARENT>/kit/run.py")


if __name__ == "__main__":
    unittest.main()

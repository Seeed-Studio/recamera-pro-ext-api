"""
The kit compatibility gate (`appmgr/kitversion.py`).

The scenario these tests exist for is concrete, not theoretical: face-analysis
0.2.0 imports `kit.logic.attributes`, a module that first shipped in kit 1.6.4.
An app package carries no kit, so before this gate that app installed cleanly
onto a 1.6.3 device and died at startup with ModuleNotFoundError, with the App
Center showing no error at any point.

Three behaviours carry the whole design and each has a test that fails if it
regresses:

  * a >=1.6.4 app is REFUSED on a kit that cannot state its version (that is
    what "pre-1.6.4" looks like from the outside);
  * a >=0.1.0 app -- i.e. every app shipped before the gate existed -- is still
    ALLOWED on that same kit, because failing those closed would brick the App
    Center on precisely the devices that have not upgraded yet;
  * a malformed requirement is REFUSED rather than ignored, since a typo
    degrading to "anything goes" would quietly restore the original bug.

Run: python3 -m pytest market/appmgr/tests/test_kitversion.py -q
"""
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_MARKET = os.path.dirname(os.path.dirname(_HERE))
if _MARKET not in sys.path:
    sys.path.insert(0, _MARKET)

from appmgr import kitversion as kv            # noqa: E402


def _kit_dir(version_line: str | None) -> str:
    """A throwaway kit package dir whose __init__.py carries `version_line`."""
    d = tempfile.mkdtemp(prefix="kit.")
    with open(os.path.join(d, "__init__.py"), "w", encoding="utf-8") as f:
        f.write('"""fake kit."""\n')
        if version_line is not None:
            f.write(version_line + "\n")
    return d


class VersionMathTests(unittest.TestCase):
    def test_missing_components_are_zero(self):
        self.assertEqual(kv.compare((1, 6), (1, 6, 0)), 0)
        self.assertEqual(kv.compare((1, 6, 4), (1, 6)), 1)
        self.assertEqual(kv.compare((1, 6), (1, 6, 1)), -1)

    def test_numeric_not_lexicographic(self):
        # The bug a string compare would introduce: "1.10" < "1.9" as text.
        self.assertEqual(kv.compare(kv.parse_version("1.10.0"),
                                    kv.parse_version("1.9.0")), 1)

    def test_parse_version_rejects_suffixes(self):
        self.assertIsNone(kv.parse_version("1.6.4-rc1"))
        self.assertIsNone(kv.parse_version("v1.6.4"))
        self.assertIsNone(kv.parse_version(""))
        self.assertEqual(kv.parse_version("2"), (2,))

    def test_bare_requirement_means_at_least(self):
        self.assertEqual(kv.parse_requirement("1.6.4"), (">=", (1, 6, 4)))
        self.assertEqual(kv.parse_requirement(">=1.6.4"), (">=", (1, 6, 4)))
        self.assertEqual(kv.parse_requirement("  >= 1.6.4 "), (">=", (1, 6, 4)))

    def test_parse_requirement_rejects_unsupported_syntax(self):
        for bad in ("^1.6", "~=1.6", ">=1.6.4-rc1", "latest", ">= 1.6.x", "*"):
            self.assertIsNone(kv.parse_requirement(bad), bad)


class InstalledVersionTests(unittest.TestCase):
    def test_reads_the_literal(self):
        d = _kit_dir('__version__ = "1.6.4"')
        self.assertEqual(kv.installed_version(d), "1.6.4")

    def test_single_quotes_and_spacing(self):
        self.assertEqual(kv.installed_version(_kit_dir("__version__='2.0'")), "2.0")
        self.assertEqual(
            kv.installed_version(_kit_dir('__version__   =   "3.1.2"')), "3.1.2")

    def test_pre_versioning_kit_reads_as_unknown(self):
        self.assertIsNone(kv.installed_version(_kit_dir(None)))

    def test_absent_kit_reads_as_unknown(self):
        self.assertIsNone(kv.installed_version("/nonexistent/kit"))

    def test_does_not_import_the_kit(self):
        """A kit that would blow up on import must still yield its version."""
        d = tempfile.mkdtemp(prefix="kit.")
        with open(os.path.join(d, "__init__.py"), "w", encoding="utf-8") as f:
            f.write('__version__ = "9.9.9"\n')
            f.write('raise SystemExit("importing the kit ran its code")\n')
        self.assertEqual(kv.installed_version(d), "9.9.9")


class GateTests(unittest.TestCase):
    # -- the case the gate exists for ------------------------------------- #
    def test_new_app_is_refused_on_a_pre_versioning_kit(self):
        old_kit = _kit_dir(None)
        with self.assertRaises(kv.KitIncompatible) as cm:
            kv.check({"id": "face-analysis", "kit": ">=1.6.4"}, old_kit)
        msg = str(cm.exception)
        self.assertIn("1.6.4", msg)
        self.assertIn("recamera-ext-kit-v1.6.4.tar.gz", msg,
                      "the refusal must name the artifact that fixes it")

    def test_new_app_is_refused_on_an_older_versioned_kit(self):
        with self.assertRaises(kv.KitIncompatible) as cm:
            kv.check({"kit": ">=1.6.4"}, _kit_dir('__version__ = "1.6.3"'))
        self.assertIn("1.6.3", str(cm.exception),
                      "the refusal must state what IS installed")

    # -- and the regression it must not cause ----------------------------- #
    def test_pre_gate_apps_still_install_on_a_pre_versioning_kit(self):
        """Every app shipped before the gate declares ">=0.1.0"."""
        old_kit = _kit_dir(None)
        for req in (">=0.1.0", ">=1.0", "1.6.3", ">=1.6.3"):
            self.assertIsNone(kv.check({"kit": req}, old_kit), req)

    def test_no_requirement_is_allowed(self):
        self.assertIsNone(kv.check({}, _kit_dir(None)))
        self.assertIsNone(kv.check({"kit": ""}, _kit_dir('__version__ = "1.6.4"')))

    # -- ordinary passes --------------------------------------------------- #
    def test_exact_and_newer_kit_pass(self):
        self.assertEqual(kv.check({"kit": ">=1.6.4"},
                                  _kit_dir('__version__ = "1.6.4"')), "1.6.4")
        self.assertEqual(kv.check({"kit": ">=1.6.4"},
                                  _kit_dir('__version__ = "1.7.0"')), "1.7.0")
        self.assertEqual(kv.check({"kit": ">=1.6.4"},
                                  _kit_dir('__version__ = "1.10.0"')), "1.10.0")

    def test_other_operators(self):
        k = _kit_dir('__version__ = "1.6.4"')
        self.assertEqual(kv.check({"kit": "==1.6.4"}, k), "1.6.4")
        with self.assertRaises(kv.KitIncompatible):
            kv.check({"kit": ">1.6.4"}, k)
        self.assertEqual(kv.check({"kit": "<=1.6.4"}, k), "1.6.4")

    # -- fail closed on nonsense ------------------------------------------ #
    def test_malformed_requirement_is_refused_not_ignored(self):
        k = _kit_dir('__version__ = "1.6.4"')
        for bad in ("^1.6", "latest", ">=abc", "*"):
            with self.assertRaises(kv.KitIncompatible, msg=bad):
                kv.check({"kit": bad}, k)

    def test_malformed_installed_version_is_refused(self):
        with self.assertRaises(kv.KitIncompatible):
            kv.check({"kit": ">=1.6.4"}, _kit_dir('__version__ = "nightly"'))


class ShippedManifestTests(unittest.TestCase):
    """The real manifests must satisfy the gate against the real kit."""

    def setUp(self):
        self.repo = os.path.dirname(_MARKET)
        self.kit_dir = os.path.join(self.repo, "kit")

    def test_repo_kit_declares_a_parsable_version(self):
        v = kv.installed_version(self.kit_dir)
        self.assertIsNotNone(v, "kit/__init__.py carries no __version__")
        self.assertIsNotNone(kv.parse_version(v), f"unparsable kit version {v!r}")

    def test_every_shipped_manifest_passes_against_the_repo_kit(self):
        import glob
        import json
        seen = 0
        for path in sorted(glob.glob(os.path.join(self.repo, "apps", "*",
                                                  "manifest.json"))):
            with open(path, encoding="utf-8") as f:
                manifest = json.load(f)
            seen += 1
            try:
                kv.check(manifest, self.kit_dir)
            except kv.KitIncompatible as e:
                self.fail(f"{os.path.basename(os.path.dirname(path))}: {e}")
        self.assertGreater(seen, 0, "no manifests found")

    def test_face_analysis_actually_pins_the_kit_it_needs(self):
        """It imports kit.logic.attributes, so it must require >= that kit."""
        import json
        p = os.path.join(self.repo, "apps", "face-analysis", "manifest.json")
        with open(p, encoding="utf-8") as f:
            manifest = json.load(f)
        parsed = kv.parse_requirement(manifest.get("kit", ""))
        self.assertIsNotNone(parsed, "face-analysis declares no usable kit req")
        _op, want = parsed
        self.assertGreaterEqual(
            kv.compare(want, kv.KIT_VERSIONING_SINCE), 0,
            "face-analysis needs kit.logic.attributes (kit 1.6.4+), so its "
            "manifest requirement must be >= 1.6.4 or the gate cannot protect it")


class StoreVisibilityTests(unittest.TestCase):
    """The two halves the STORE needs to pre-check, before any download.

    The gate in `installer.inspect()` is the enforcement, and it is enough for
    correctness. But it only fires after the browser has fetched a whole package
    (50 MB for face-analysis) and pushed it to the device. For the store to say
    "upgrade the kit first" up front it needs both halves visible: the app's
    requirement (catalog `kit`) and the device's version (`/list.kit_version`).
    Either one silently dropped puts the refusal back at the end of a download.
    """

    def test_list_reports_the_device_kit_version(self):
        from appmgr import paths as p
        d = _kit_dir('__version__ = "1.6.4"')
        old_dir = p.KIT_DIR
        p.KIT_DIR = d
        try:
            from appmgr import server
            self.assertEqual(server.do_list().get("kit_version"), "1.6.4")
        finally:
            p.KIT_DIR = old_dir

    def test_list_reports_none_on_a_pre_versioning_kit(self):
        from appmgr import paths as p
        d = _kit_dir(None)
        old_dir = p.KIT_DIR
        p.KIT_DIR = d
        try:
            from appmgr import server
            self.assertIsNone(server.do_list().get("kit_version"))
        finally:
            p.KIT_DIR = old_dir

    def test_generated_catalog_carries_each_app_kit_requirement(self):
        """gen_catalog must forward `kit`; without it the store is blind."""
        import json
        repo = os.path.dirname(_MARKET)
        cat = os.path.join(repo, "market", "catalog", "catalog.json")
        if not os.path.exists(cat):
            self.skipTest("catalog.json not generated")
        with open(cat, encoding="utf-8") as f:
            c = json.load(f)
        for a in c.get("apps", []):
            self.assertIn("kit", a,
                          f"catalog entry {a['id']!r} carries no kit requirement")
            self.assertIsNotNone(kv.parse_requirement(a["kit"]),
                                 f"{a['id']}: unparsable kit requirement {a['kit']!r}")

    def test_catalog_and_gate_agree_for_face_analysis(self):
        """The store's pre-check and the device's gate must reach one verdict."""
        import json
        repo = os.path.dirname(_MARKET)
        cat = os.path.join(repo, "market", "catalog", "catalog.json")
        if not os.path.exists(cat):
            self.skipTest("catalog.json not generated")
        with open(cat, encoding="utf-8") as f:
            entry = next(a for a in json.load(f)["apps"]
                         if a["id"] == "face-analysis")
        old_kit = _kit_dir(None)
        # store's view: requirement from the catalog, device version from /list
        op, want = kv.parse_requirement(entry["kit"])
        store_would_block = kv.compare(want, kv.KIT_VERSIONING_SINCE) >= 0
        # device's view: the gate on the same requirement
        with self.assertRaises(kv.KitIncompatible):
            kv.check({"kit": entry["kit"]}, old_kit)
        self.assertTrue(store_would_block,
                        "the gate refuses but the store would have let it "
                        "through -- the pre-check and the gate disagree")


if __name__ == "__main__":
    unittest.main()

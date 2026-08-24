"""LD_LIBRARY_PATH handed to apps must carry no empty element.

Why this test exists
--------------------
Observed on device (2026-08-15), on a running app:

    LD_LIBRARY_PATH=/oem/usr/lib:/oem/lib:/oem/usr/lib:/oem/lib:...:/oem/lib:

Four duplicate pairs and a trailing empty segment. The duplicates were only
noise -- each deploy re-execs `appmgr serve` from a shell that already had the
variable, and supervisor prepended to whatever it inherited.

The empty segment is the part that matters: glibc (2.38 on this device) reads an
empty element of LD_LIBRARY_PATH as THE CURRENT DIRECTORY. Apps run with
cwd=/userdata/local (root-owned), but /userdata itself is 0777, so an app that
chdir'd into a writable subdir would be searching a world-writable directory for
shared objects -- a local library-injection point for uid 1000.

`_join_pathlist` drops empties and later duplicates while keeping order. Search
order is semantic: /usr/lib must precede OEM compatibility copies, while the
OEM dirs must remain visible ahead of app/runtime paths appended later.
"""
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_MARKET = os.path.dirname(os.path.dirname(_HERE))
if _MARKET not in sys.path:
    sys.path.insert(0, _MARKET)

from appmgr import supervisor            # noqa: E402

SEP = os.pathsep


class JoinPathListTests(unittest.TestCase):

    def test_empty_elements_are_dropped(self):
        """The whole point: glibc would read '' as the cwd."""
        got = supervisor._join_pathlist(["/a", "", "/b", ""])
        self.assertEqual(got, f"/a{SEP}/b")
        self.assertNotIn(f"{SEP}{SEP}", got)
        self.assertFalse(got.endswith(SEP))

    def test_a_trailing_colon_in_the_inherited_value_cannot_survive(self):
        """The exact device shape: inherited value ends with a separator."""
        inherited = f"/oem/usr/lib{SEP}/oem/lib{SEP}"
        got = supervisor._join_pathlist(
            ["/oem/usr/lib", "/oem/lib"] + inherited.split(SEP))
        self.assertEqual(got, f"/oem/usr/lib{SEP}/oem/lib")

    def test_duplicates_collapse_but_order_is_kept(self):
        """First occurrence wins -- loader search order is semantic."""
        got = supervisor._join_pathlist(
            ["/oem/usr/lib", "/oem/lib", "/oem/usr/lib", "/oem/lib", "/userdata/lib"])
        self.assertEqual(got, f"/oem/usr/lib{SEP}/oem/lib{SEP}/userdata/lib")

    def test_controlled_dirs_stay_ahead_of_appended_ones(self):
        got = supervisor._join_pathlist(
            ["/usr/lib", "/oem/usr/lib", "/oem/lib", "/userdata/lib"])
        self.assertLess(got.index("/usr/lib"), got.index("/oem/usr/lib"))
        self.assertLess(got.index("/oem/usr/lib"), got.index("/userdata/lib"))

    def test_empty_input_yields_empty_string_not_a_bare_separator(self):
        self.assertEqual(supervisor._join_pathlist([]), "")
        self.assertEqual(supervisor._join_pathlist(["", ""]), "")


class BuiltEnvTests(unittest.TestCase):
    """End-to-end through the real env builder, with a dirty inherited value."""

    def _env_for(self, inherited):
        prev = os.environ.get("LD_LIBRARY_PATH")
        os.environ["LD_LIBRARY_PATH"] = inherited
        try:
            return supervisor._build_env("yolo-detector", {})
        finally:
            if prev is None:
                os.environ.pop("LD_LIBRARY_PATH", None)
            else:
                os.environ["LD_LIBRARY_PATH"] = prev

    def test_dirty_inherited_value_is_cleaned(self):
        env = self._env_for(f"/oem/usr/lib{SEP}/oem/lib{SEP}/oem/usr/lib{SEP}/oem/lib{SEP}")
        val = env["LD_LIBRARY_PATH"]
        self.assertNotIn(f"{SEP}{SEP}", val)
        self.assertFalse(val.endswith(SEP))
        self.assertEqual(val.split(SEP).count("/oem/usr/lib"), 1)
        self.assertEqual(val.split(SEP).count("/usr/lib"), 1)

    def test_system_and_oem_dirs_are_still_present(self):
        """Cleaning must not drop what the variable exists for."""
        env = self._env_for("")
        parts = env["LD_LIBRARY_PATH"].split(SEP)
        self.assertEqual(parts[0], "/usr/lib")
        self.assertIn("/oem/usr/lib", parts)
        self.assertIn("/oem/lib", parts)

    def test_an_unrelated_inherited_dir_is_preserved(self):
        env = self._env_for("/opt/vendor/lib")
        self.assertIn("/opt/vendor/lib", env["LD_LIBRARY_PATH"].split(SEP))

    def test_system_then_oem_dirs_come_before_inherited_paths(self):
        """Assert the production call-site order, not only join behaviour."""
        parts = self._env_for("/opt/vendor/lib")["LD_LIBRARY_PATH"].split(SEP)
        self.assertEqual(parts[0], "/usr/lib")
        self.assertLess(parts.index("/usr/lib"), parts.index("/oem/usr/lib"))
        self.assertLess(parts.index("/oem/usr/lib"), parts.index("/opt/vendor/lib"))
        self.assertLess(parts.index("/oem/lib"), parts.index("/opt/vendor/lib"))

    def test_inherited_oem_first_order_cannot_shadow_system_libraries(self):
        """Regression: OEM FreeType/PNG must not break the system cv2 ABI."""
        inherited = f"/oem/usr/lib{SEP}/oem/lib{SEP}/usr/lib"
        parts = self._env_for(inherited)["LD_LIBRARY_PATH"].split(SEP)
        self.assertEqual(parts[:3], ["/usr/lib", "/oem/usr/lib", "/oem/lib"])

    def test_npu_managed_marker_is_never_inherited(self):
        previous = os.environ.get("RECAMERA_NPU_MANAGED")
        os.environ["RECAMERA_NPU_MANAGED"] = "spoofed"
        try:
            env = supervisor._build_env("yolo-detector", {})
        finally:
            if previous is None:
                os.environ.pop("RECAMERA_NPU_MANAGED", None)
            else:
                os.environ["RECAMERA_NPU_MANAGED"] = previous
        self.assertNotIn("RECAMERA_NPU_MANAGED", env)

    def test_npu_marker_is_added_only_for_explicit_managed_launch(self):
        env = supervisor._build_env(
            "yolo-detector", {}, npu_managed=True)
        self.assertEqual(env["RECAMERA_NPU_MANAGED"], "appmgr-v1")


if __name__ == "__main__":
    unittest.main()

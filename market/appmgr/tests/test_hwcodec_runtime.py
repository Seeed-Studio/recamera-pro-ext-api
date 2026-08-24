"""File-shaped on-demand runtime -- presence is a probe, and the env goes to the
app that asked for it and to nobody else.

Why this test exists
--------------------
The audio runtime taught appmgr to install wheels. The GStreamer RK hardware
codec runtime ships three `.so` files and three environment variables instead,
and copying voiceruntime.py's shape onto it gets three things wrong in ways that
look fine on a dev box (RUNTIME_BUNDLE_SPEC §5):

  * PRESENCE IS THE PROBE, NOT THE FILE. A plugin built against the wrong gst
    ABI, or one whose libgstcodecparsers dependency never made it into the
    bundle, sits in /userdata/lib exactly where an os.path.exists() check wants
    it, while GStreamer reports `No such element or plugin 'mppvideodec'`. The
    fake gst-inspect below therefore refuses EMPTY files and refuses a plugin
    whose dependency is not on LD_LIBRARY_PATH -- so "the .so is on disk" can
    never be enough to make these tests pass.
  * APPEND IS NOT ASSIGN. `export LD_LIBRARY_PATH=/userdata/lib` wipes the
    device's ordered /usr/lib:/oem/usr/lib:/oem/lib base and
    librockchip_mpp.so.1 stops resolving -- observed on device, which is why
    there is an explicit assertion that the system/OEM order survives injection,
    and another that a second injection does not grow the variable.
  * ONLY THE DECLARING APP. GST_PLUGIN_PATH on all nine vision apps would make a
    plugin problem everyone's problem. The supervisor test at the bottom launches
    two real processes -- one manifest declaring `hwcodec`, one not -- and reads
    the environment each of them actually got.

The `voice` entry must be unaffected by all of this: it carries no `kind`, and
test_voice_runtime.py is the regression baseline.

No device, no GStreamer: the "venv"/plugin loader is a shell script on PATH, and
/userdata is a temp dir.
"""
import os
import shutil
import stat
import sys
import tarfile
import tempfile
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_MARKET = os.path.dirname(os.path.dirname(_HERE))
if _MARKET not in sys.path:
    sys.path.insert(0, _MARKET)

from appmgr import installer, paths, supervisor, voiceruntime      # noqa: E402


# A stand-in for `gst-inspect-1.0 mppvideodec`. It answers the question the real
# one answers -- "can the plugin actually be loaded" -- from the environment it
# is given, so nothing in these tests can pass by putting a file somewhere.
FAKE_GST_INSPECT = r"""#!/bin/sh
# arg1 = element name (mppvideodec)
[ -n "$GST_PLUGIN_PATH" ] || { echo "no GST_PLUGIN_PATH set" >&2; exit 255; }
plugin=""
old_ifs=$IFS
IFS=:
for d in $GST_PLUGIN_PATH; do
  # -s: exists AND is non-empty. A truncated .so is a broken plugin.
  [ -s "$d/libgstrockchipmpp.so" ] && plugin="$d/libgstrockchipmpp.so"
done
dep=""
for d in $LD_LIBRARY_PATH; do
  [ -s "$d/libgstcodecparsers-1.0.so.0" ] && dep="$d/libgstcodecparsers-1.0.so.0"
done
IFS=$old_ifs
[ -n "$plugin" ] || { echo "No such element or plugin '$1'" >&2; exit 255; }
[ -n "$dep" ] || {
  echo "libgstcodecparsers-1.0.so.0: cannot open shared object file" >&2; exit 255; }
exit 0
"""


class HwcodecTestBase(unittest.TestCase):
    """A temp /userdata, a fake gst-inspect on PATH, and a rebased registry entry."""

    def setUp(self):
        self.ud = tempfile.mkdtemp(prefix="userdata.")
        self.addCleanup(shutil.rmtree, self.ud, ignore_errors=True)
        self.dest = os.path.join(self.ud, "lib")

        # fake gst-inspect-1.0 on PATH
        bindir = os.path.join(self.ud, "bin")
        os.makedirs(bindir)
        gi = os.path.join(bindir, "gst-inspect-1.0")
        with open(gi, "w") as f:
            f.write(FAKE_GST_INSPECT)
        os.chmod(gi, os.stat(gi).st_mode | stat.S_IXUSR | stat.S_IXGRP)
        self._patch_env("PATH", bindir + os.pathsep + os.environ.get("PATH", ""))

        # containment root + registry entry rebased into the temp tree. The
        # SHAPE is the real one -- only the absolute prefixes move.
        self._patch_attr(voiceruntime, "USERDATA_ROOT", self.ud)
        real = voiceruntime.RUNTIMES["hwcodec"]
        spec = dict(real)
        spec["dest"] = self.dest
        spec["env"] = {
            "GST_PLUGIN_PATH": {"append": os.path.join(self.dest, "gstreamer-1.0")},
            "LD_LIBRARY_PATH": {"append": self.dest},
            "GST_REGISTRY": {"set": os.path.join(self.ud, "gst-registry.bin")},
        }
        self._patch_registry("hwcodec", spec)

        # staging dir for uploaded bundles, inside the allowed package roots
        stage = os.path.join(self.ud, "appstage")
        self._patch_attr(paths, "APPSTAGE_DIR", stage)
        prev_roots = paths.ALLOWED_PKG_ROOTS
        paths.ALLOWED_PKG_ROOTS = tuple(
            set(prev_roots) | {os.path.realpath(self.ud)})
        self.addCleanup(setattr, paths, "ALLOWED_PKG_ROOTS", prev_roots)

        # These tests build UNSIGNED bundles to exercise install/env mechanics,
        # not authenticity (release-signature verification has its own suite,
        # test_runtime_signature.py). Opt out of the require-signature policy so
        # the unsigned fixtures reach extraction.
        self._patch_attr(paths, "REQUIRE_SIGNATURE", False)

    # -- patch helpers -------------------------------------------------------
    def _patch_env(self, key, value):
        prev = os.environ.get(key)
        os.environ[key] = value
        self.addCleanup(lambda: os.environ.__setitem__(key, prev)
                        if prev is not None else os.environ.pop(key, None))

    def _patch_attr(self, mod, name, value):
        prev = getattr(mod, name)
        setattr(mod, name, value)
        self.addCleanup(setattr, mod, name, prev)

    def _patch_registry(self, key, spec):
        prev = voiceruntime.RUNTIMES[key]
        voiceruntime.RUNTIMES[key] = spec
        self.addCleanup(voiceruntime.RUNTIMES.__setitem__, key, prev)

    # -- bundle --------------------------------------------------------------
    def _bundle(self, plugin_bytes=b"\x7fELF fake mpp plugin",
                dep_bytes=b"\x7fELF fake codecparsers",
                version="1.0.0"):
        """A gst-hwcodec bundle whose files/ tree mirrors dest, as built by
        release/build-gst-hwcodec.sh."""
        d = tempfile.mkdtemp(prefix="bundle.", dir=paths.ensure_appstage())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        root = os.path.join(d, "gst-hwcodec")
        plugdir = os.path.join(root, "files", "gstreamer-1.0")
        os.makedirs(plugdir)
        with open(os.path.join(plugdir, "libgstrockchipmpp.so"), "wb") as f:
            f.write(plugin_bytes)
        with open(os.path.join(plugdir, "libgstvideoparsersbad.so"), "wb") as f:
            f.write(b"\x7fELF fake videoparsers")
        with open(os.path.join(root, "files", "libgstcodecparsers-1.0.so.0"), "wb") as f:
            f.write(dep_bytes)
        tgz = os.path.join(d, f"gst-hwcodec-{version}.tar.gz")
        with tarfile.open(tgz, "w:gz") as tar:
            tar.add(root, arcname="gst-hwcodec")
        return tgz


class RegistryShapeTests(unittest.TestCase):
    """The registry itself, unpatched -- §2."""

    def test_voice_entry_has_no_kind_and_still_means_wheels(self):
        """The audio runtime must not have been touched by this change."""
        voice = voiceruntime.RUNTIMES["voice"]
        self.assertNotIn("kind", voice)
        self.assertEqual(voiceruntime.kind_of(voice), "wheels")
        self.assertEqual(voice["capability"], "audio")

    def test_hwcodec_entry_matches_the_spec(self):
        spec = voiceruntime.RUNTIMES["hwcodec"]
        self.assertEqual(spec["kind"], "files")
        self.assertEqual(spec["capability"], "hwcodec")
        self.assertEqual(spec["dest"], "/userdata/lib")
        self.assertEqual(spec["probe"], ["gst-inspect-1.0", "mppvideodec"])
        # LD_LIBRARY_PATH must be an append -- a "set" here is the device bug
        # this whole distinction exists for.
        self.assertEqual(spec["env"]["LD_LIBRARY_PATH"], {"append": "/userdata/lib"})
        self.assertEqual(spec["env"]["GST_PLUGIN_PATH"],
                         {"append": "/userdata/lib/gstreamer-1.0"})
        self.assertIn("set", spec["env"]["GST_REGISTRY"])

    def test_capability_lookup_reaches_it(self):
        """The store asks by capability, exactly as it does for audio."""
        self.assertEqual(voiceruntime._spec("hwcodec")["name"], "hwcodec")
        self.assertEqual(voiceruntime._spec(" HWCODEC ")["name"], "hwcodec")

    def test_encoder_is_not_claimed(self):
        """§6: the encoder ships in the same .so but is unverified. If someone
        adds mpph264enc to the probe or the blurb, this fails and they have to
        go test it against rkipc's VEPU first."""
        spec = voiceruntime.RUNTIMES["hwcodec"]
        self.assertNotIn("enc", " ".join(spec["probe"]))
        self.assertIn("UNVERIFIED", spec["about"])


class StatusTests(HwcodecTestBase):

    def test_absent_when_nothing_is_installed(self):
        st = voiceruntime.status("hwcodec")
        self.assertFalse(st["present"])
        self.assertEqual(st["kind"], "files")
        self.assertIn("mppvideodec", st["error"])
        self.assertEqual(st["files"], [])

    def test_present_after_install(self):
        res = voiceruntime.install("hwcodec", self._bundle())
        self.assertTrue(res["installed"])
        self.assertFalse(res["already_present"])
        self.assertTrue(res["present"], res)
        # files landed where the bundle said, not where appmgr guessed
        self.assertTrue(os.path.isfile(
            os.path.join(self.dest, "gstreamer-1.0", "libgstrockchipmpp.so")))
        self.assertTrue(os.path.isfile(
            os.path.join(self.dest, "gstreamer-1.0", "libgstvideoparsersbad.so")))
        self.assertTrue(os.path.isfile(
            os.path.join(self.dest, "libgstcodecparsers-1.0.so.0")))
        self.assertTrue(voiceruntime.status("hwcodec")["present"])

    def test_nothing_was_installed_into_the_venv(self):
        """A .so is not a wheel: the shared venv must be untouched."""
        voiceruntime.install("hwcodec", self._bundle())
        self.assertFalse(os.path.exists(os.path.join(paths.RKNNENV_DIR)),
                         "file-shaped runtime must not create/populate the venv")

    def test_empty_so_is_not_present_even_though_the_file_exists(self):
        """§5: replace the .so with an empty file -- the probe must fail.

        This is the assertion that stops presence from degrading into
        os.path.exists(): the inventory is identical, only loadability changed.
        """
        voiceruntime.install("hwcodec", self._bundle())
        so = os.path.join(self.dest, "gstreamer-1.0", "libgstrockchipmpp.so")
        open(so, "wb").close()                      # truncate to 0 bytes
        self.assertTrue(os.path.isfile(so))
        st = voiceruntime.status("hwcodec")
        self.assertFalse(st["present"], st)
        self.assertIn("mppvideodec", st["error"])
        # ...and the answer still lists what IS on disk, so the failure can be
        # diagnosed without an ssh session.
        self.assertIn("gstreamer-1.0/libgstrockchipmpp.so",
                      [f["file"] for f in st["files"]])

    def test_missing_dependency_is_not_present(self):
        """The plugin alone is not enough -- libgstcodecparsers must resolve."""
        voiceruntime.install("hwcodec", self._bundle())
        os.remove(os.path.join(self.dest, "libgstcodecparsers-1.0.so.0"))
        st = voiceruntime.status("hwcodec")
        self.assertFalse(st["present"])
        self.assertIn("libgstcodecparsers", st["error"])

    def test_missing_probe_binary_says_so(self):
        self._patch_env("PATH", "/nonexistent-bin")
        st = voiceruntime.status("hwcodec")
        self.assertFalse(st["present"])
        self.assertIn("probe binary not found", st["error"])


class InstallTests(HwcodecTestBase):

    def test_second_install_does_not_unpack_again(self):
        """Idempotence, asserted the hard way: the bundle is DELETED between the
        two calls, so an install() that tried to extract would raise."""
        pkg = self._bundle()
        first = voiceruntime.install("hwcodec", pkg)
        self.assertTrue(first["installed"])
        os.remove(pkg)
        second = voiceruntime.install("hwcodec", pkg)
        self.assertTrue(second["already_present"])
        self.assertFalse(second["installed"])
        self.assertTrue(second["present"])

    def test_install_without_bundle_names_what_is_missing(self):
        with self.assertRaises(ValueError) as cm:
            voiceruntime.install("hwcodec", pkg_path=None)
        msg = str(cm.exception)
        self.assertIn("gst-hwcodec", msg)
        self.assertIn("mppvideodec", msg)

    def test_install_refuses_a_bundle_outside_the_allowed_roots(self):
        bad = tempfile.mkdtemp(prefix="badroot.")
        self.addCleanup(shutil.rmtree, bad, ignore_errors=True)
        pkg = os.path.join(bad, "gst-hwcodec-1.0.0.tar.gz")
        with open(pkg, "wb") as f:
            f.write(b"not really a tarball")
        with self.assertRaises(installer.InstallError):
            voiceruntime.install("hwcodec", pkg_path=pkg)

    def test_bundle_without_files_tree_is_named_as_such(self):
        d = tempfile.mkdtemp(prefix="bundle.", dir=paths.ensure_appstage())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        root = os.path.join(d, "gst-hwcodec")
        os.makedirs(root)
        with open(os.path.join(root, "README.md"), "w") as f:
            f.write("no payload here\n")
        tgz = os.path.join(d, "gst-hwcodec-9.9.9.tar.gz")
        with tarfile.open(tgz, "w:gz") as tar:
            tar.add(root, arcname="gst-hwcodec")
        with self.assertRaises(voiceruntime.RuntimeError_) as cm:
            voiceruntime.install("hwcodec", tgz)
        self.assertIn("files/", str(cm.exception))

    def test_probe_failure_after_unpack_reports_the_inventory(self):
        """A bundle that unpacks but does not work names the files it left."""
        pkg = self._bundle(plugin_bytes=b"")        # zero-byte plugin in the bundle
        with self.assertRaises(voiceruntime.RuntimeError_) as cm:
            voiceruntime.install("hwcodec", pkg)
        msg = str(cm.exception)
        self.assertIn("libgstrockchipmpp.so (0 B)", msg)
        self.assertIn("mppvideodec", msg)


class DestSafetyTests(HwcodecTestBase):
    """`dest` decides where a downloaded archive is written -- §5 path safety."""

    def _rebase(self, dest):
        spec = dict(voiceruntime.RUNTIMES["hwcodec"])
        spec["dest"] = dest
        self._patch_registry("hwcodec", spec)

    def test_dest_outside_userdata_is_refused(self):
        self._rebase("/etc/gstreamer-1.0")
        with self.assertRaises(voiceruntime.RuntimeError_) as cm:
            voiceruntime.status("hwcodec")
        self.assertIn("escapes", str(cm.exception))
        with self.assertRaises(voiceruntime.RuntimeError_):
            voiceruntime.install("hwcodec", self._bundle())

    def test_dest_traversing_out_of_userdata_is_refused(self):
        self._rebase(os.path.join(self.ud, "..", "etc"))
        with self.assertRaises(voiceruntime.RuntimeError_):
            voiceruntime.status("hwcodec")

    def test_sibling_prefix_is_not_inside_userdata(self):
        """/userdata-evil must not pass as /userdata (the trailing-slash bug)."""
        self._rebase(self.ud.rstrip("/") + "-evil/lib")
        with self.assertRaises(voiceruntime.RuntimeError_):
            voiceruntime.status("hwcodec")

    def test_relative_dest_is_refused(self):
        self._rebase("lib")
        with self.assertRaises(voiceruntime.RuntimeError_):
            voiceruntime.status("hwcodec")


class EnvMergeTests(HwcodecTestBase):
    """§3 -- the part that is easiest to get subtly wrong."""

    def _install(self):
        voiceruntime.install("hwcodec", self._bundle())

    def test_env_goes_to_the_app_that_declares_the_capability(self):
        self._install()
        env = {"LD_LIBRARY_PATH": "/usr/lib:/oem/usr/lib:/oem/lib"}
        applied = voiceruntime.apply_runtime_env(env, ["hwcodec"])
        self.assertEqual(applied, ["hwcodec"])
        self.assertEqual(env["GST_PLUGIN_PATH"],
                         os.path.join(self.dest, "gstreamer-1.0"))
        self.assertEqual(env["GST_REGISTRY"],
                         os.path.join(self.ud, "gst-registry.bin"))

    def test_env_is_untouched_for_an_app_that_does_not_declare_it(self):
        self._install()
        for caps in ([], None, ["audio"], ["vision"]):
            env = {"LD_LIBRARY_PATH": "/usr/lib:/oem/usr/lib:/oem/lib"}
            before = dict(env)
            self.assertEqual(voiceruntime.apply_runtime_env(env, caps), [])
            self.assertEqual(env, before, f"caps={caps!r} changed the environment")

    def test_absent_runtime_injects_nothing_and_does_not_raise(self):
        """No bundle installed: the app must still be launchable."""
        env = {"LD_LIBRARY_PATH": "/usr/lib:/oem/usr/lib:/oem/lib"}
        before = dict(env)
        self.assertEqual(voiceruntime.apply_runtime_env(env, ["hwcodec"]), [])
        self.assertEqual(env, before)

    def test_append_keeps_system_first_and_oem_dirs(self):
        """Appending preserves system-first order and OEM-only MPP lookup."""
        self._install()
        env = {"LD_LIBRARY_PATH": "/usr/lib:/oem/usr/lib:/oem/lib"}
        voiceruntime.apply_runtime_env(env, ["hwcodec"])
        parts = env["LD_LIBRARY_PATH"].split(os.pathsep)
        self.assertIn("/usr/lib", parts)
        self.assertIn("/oem/usr/lib", parts)
        self.assertIn("/oem/lib", parts)
        self.assertIn(self.dest, parts)
        # order matters: the pre-existing entries stay in front
        self.assertEqual(parts[:3], ["/usr/lib", "/oem/usr/lib", "/oem/lib"])

    def test_append_is_deduped(self):
        """Re-applying must not grow the variable -- a restart loop would
        otherwise build an ever-longer LD_LIBRARY_PATH."""
        self._install()
        env = {"LD_LIBRARY_PATH": "/usr/lib:/oem/usr/lib:/oem/lib"}
        voiceruntime.apply_runtime_env(env, ["hwcodec"])
        once = env["LD_LIBRARY_PATH"]
        voiceruntime.apply_runtime_env(env, ["hwcodec"])
        voiceruntime.apply_runtime_env(env, ["hwcodec"])
        self.assertEqual(env["LD_LIBRARY_PATH"], once)
        self.assertEqual(env["LD_LIBRARY_PATH"].split(os.pathsep).count(self.dest), 1)
        self.assertEqual(
            env["GST_PLUGIN_PATH"].split(os.pathsep).count(
                os.path.join(self.dest, "gstreamer-1.0")), 1)

    def test_append_into_an_unset_variable(self):
        self._install()
        env = {}
        voiceruntime.apply_runtime_env(env, ["hwcodec"])
        self.assertEqual(env["LD_LIBRARY_PATH"], self.dest)

    def test_set_overwrites_where_append_would_not(self):
        self._install()
        env = {"GST_REGISTRY": "/root/.cache/gstreamer-1.0/registry.bin"}
        voiceruntime.apply_runtime_env(env, ["hwcodec"])
        self.assertEqual(env["GST_REGISTRY"],
                         os.path.join(self.ud, "gst-registry.bin"))

    def test_unknown_capability_is_ignored(self):
        env = {"PATH": "/usr/bin"}
        self.assertEqual(voiceruntime.apply_runtime_env(env, ["quantum-npu"]), [])
        self.assertEqual(env, {"PATH": "/usr/bin"})

    def test_malformed_env_rule_is_named(self):
        spec = dict(voiceruntime.RUNTIMES["hwcodec"])
        spec["env"] = {"GST_PLUGIN_PATH": {"prepend": "/x"}}
        self._patch_registry("hwcodec", spec)
        with self.assertRaises(voiceruntime.RuntimeError_) as cm:
            voiceruntime.merge_env({}, spec["env"])
        self.assertIn("GST_PLUGIN_PATH", str(cm.exception))


class SupervisorInjectionTests(HwcodecTestBase):
    """End to end: what the launched PROCESS actually got in its environment.

    Asserting on apply_runtime_env() alone would not catch supervisor forgetting
    to call it, or constructing the base LD_LIBRARY_PATH in the wrong order.
    So these two apps are really launched.
    """

    def setUp(self):
        super().setUp()
        apps = os.path.join(self.ud, "apps")
        os.makedirs(apps)
        self._patch_attr(paths, "APPS_DIR", apps)
        self._patch_attr(paths, "APPDATA_DIR", os.path.join(self.ud, "appdata"))
        kit_parent = os.path.join(self.ud, "local")
        kit = os.path.join(kit_parent, "kit")
        os.makedirs(kit)
        self._patch_attr(paths, "KIT_PARENT", kit_parent)
        self._patch_attr(paths, "KIT_DIR", kit)
        # The "app" is launched as `<interpreter> <KIT_DIR>/run.py <app>/app.py`,
        # so with /bin/sh as the interpreter this shell script IS the app: it
        # dumps its environment next to the entry file and exits.
        with open(os.path.join(kit, "run.py"), "w") as f:
            f.write('env > "$(dirname "$1")/env.dump"\n')

    def _make_app(self, app_id, capabilities):
        d = os.path.join(paths.APPS_DIR, app_id)
        os.makedirs(d)
        with open(os.path.join(d, "app.py"), "w") as f:
            f.write("# marker\n")
        manifest = {"id": app_id, "entry": "app.py", "interpreter": "/bin/sh"}
        if capabilities is not None:
            manifest["capabilities"] = capabilities
        with open(os.path.join(d, "manifest.json"), "w") as f:
            import json
            json.dump(manifest, f)
        return d

    def _run_and_read_env(self, app_id, capabilities):
        d = self._make_app(app_id, capabilities)
        dump = os.path.join(d, "env.dump")
        # This fake "app" (a /bin/sh one-liner) dumps its env and exits at once;
        # it never signals READY, so start without the readiness gate -- these
        # tests assert on the injected ENVIRONMENT, not on lifecycle.
        supervisor.start(app_id, wait_ready=False)
        deadline = time.time() + 10
        # The shell creates ``env.dump`` as soon as redirection is opened, then
        # writes it incrementally.  Waiting for pathname existence alone races
        # the writer and can observe only the first few variables.  The process
        # exit is the completion barrier for both the write and close.
        while time.time() < deadline and supervisor.is_running(app_id):
            time.sleep(0.05)
        supervisor.reap_children()
        self.assertTrue(os.path.isfile(dump), "app never wrote its environment")
        env = {}
        with open(dump) as f:
            for line in f:
                if "=" in line:
                    k, v = line.rstrip("\n").split("=", 1)
                    env[k] = v
        return env

    def test_declaring_app_gets_the_three_variables(self):
        voiceruntime.install("hwcodec", self._bundle())
        env = self._run_and_read_env("hw-app", ["hwcodec"])
        self.assertEqual(env["GST_PLUGIN_PATH"],
                         os.path.join(self.dest, "gstreamer-1.0"))
        self.assertEqual(env["GST_REGISTRY"],
                         os.path.join(self.ud, "gst-registry.bin"))
        parts = env["LD_LIBRARY_PATH"].split(os.pathsep)
        self.assertIn(self.dest, parts)
        # System libraries must win; OEM-only and runtime libraries stay visible.
        self.assertEqual(parts[0], "/usr/lib")
        self.assertIn("/oem/usr/lib", parts)
        self.assertIn("/oem/lib", parts)
        self.assertLess(parts.index("/usr/lib"), parts.index("/oem/usr/lib"))
        self.assertLess(parts.index("/oem/usr/lib"), parts.index(self.dest))
        self.assertEqual(parts.count(self.dest), 1)

    def test_non_declaring_app_environment_is_unchanged(self):
        voiceruntime.install("hwcodec", self._bundle())
        env = self._run_and_read_env("plain-app", None)
        self.assertNotIn("GST_PLUGIN_PATH", env)
        self.assertNotIn("GST_REGISTRY", env)
        self.assertNotIn(self.dest, env["LD_LIBRARY_PATH"].split(os.pathsep))
        parts = env["LD_LIBRARY_PATH"].split(os.pathsep)
        self.assertEqual(parts[0], "/usr/lib")
        self.assertIn("/oem/usr/lib", parts)

    def test_declaring_app_starts_when_the_runtime_is_absent(self):
        """Nothing installed: the app must still launch (it decides whether to
        fall back to software decode), just without the variables."""
        env = self._run_and_read_env("hw-app-noruntime", ["hwcodec"])
        self.assertNotIn("GST_PLUGIN_PATH", env)
        parts = env["LD_LIBRARY_PATH"].split(os.pathsep)
        self.assertEqual(parts[0], "/usr/lib")
        self.assertIn("/oem/usr/lib", parts)


class BundleShapeTests(unittest.TestCase):
    """The build script and the installer must agree on the payload layout.

    They live in different trees (release/build-gst-hwcodec.sh vs
    appmgr/voiceruntime.py) and nothing at runtime cross-checks them: a plugin
    packed at files/ instead of files/gstreamer-1.0/ installs "successfully" and
    then GST_PLUGIN_PATH points at an empty directory.
    """

    SCRIPT = os.path.join(os.path.dirname(_MARKET), "release",
                          "build-gst-hwcodec.sh")

    def setUp(self):
        with open(self.SCRIPT, encoding="utf-8") as f:
            self.src = f.read()

    def test_script_packs_the_plugins_under_gstreamer_1_0(self):
        self.assertIn('mkdir -p "$ROOT/files/gstreamer-1.0"', self.src)
        for name in ("libgstrockchipmpp.so", "libgstvideoparsersbad.so"):
            self.assertIn(name, self.src)
        self.assertIn("libgstcodecparsers-1.0.so.0", self.src)

    def test_script_pins_the_verified_plugin_md5(self):
        self.assertIn("78152ef4982d0fef1ae3d44dc4fc3d7e", self.src)

    def test_script_documents_the_encoder_as_unverified(self):
        self.assertIn("未验证", self.src)

    def test_catalog_ships_the_bundle_under_the_capability_name(self):
        sys.path.insert(0, os.path.join(os.path.dirname(_MARKET), "market", "catalog"))
        import gen_catalog
        self.assertEqual(gen_catalog.RUNTIME_BUNDLES["hwcodec"],
                         "gst-hwcodec-*.tar.gz")
        # the audio bundle must still be there
        self.assertEqual(gen_catalog.RUNTIME_BUNDLES["audio"],
                         "voice-runtime-*.tar.gz")


if __name__ == "__main__":
    unittest.main()

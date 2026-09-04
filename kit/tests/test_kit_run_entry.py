"""
Unit + subprocess tests for `kit.run` -- the single app launcher that replaced
the ~40-line sys.path bootstrap every app.py used to carry
(internal/KIT_APP_SHAPE_SPEC.md §5.1).

Three launch modes must all keep working, and each is exercised as a REAL
subprocess (not a monkeypatched import), because what is being tested IS the
interpreter's module resolution:

  1. `python3 -m kit.run <app_dir>` -- how appmgr launches apps
     (supervisor._build_cmd), resolved via the PYTHONPATH it exports.
  2. `python3 <KIT_PARENT>/kit/run.py <app_dir>` with an EMPTY PYTHONPATH --
     the developer-by-hand case the deleted bootstrap used to serve. This is
     the one that proves run.py derives KIT_PARENT from its own location.
  3. `python3 app.py` from inside the app dir with kit already importable --
     the `if __name__ == "__main__": run_app(...)` tail every app kept.

Plus the two things that block a whole class of silent breakage: the real
apps/*/app.py files must no longer contain any sys.path probing, and kit.run
must be able to locate exactly one App subclass in each of them.

Run: python3 -m pytest kit/tests/test_kit_run_entry.py -q
"""
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from kit import run as kitrun                                        # noqa: E402
from kit.app import App                                              # noqa: E402

APPS_DIR = os.path.join(_REPO, "apps")
RUN_PY = os.path.join(_REPO, "kit", "run.py")

# A minimal loop-owning app: it overrides start/finish so run_app never opens a
# camera, and prints markers the parent asserts on.
TINY_APP = textwrap.dedent('''\
    """tiny probe app"""
    import os

    from kit.app import App, run_app


    class TinyProbeApp(App):
        id = "tiny-probe"
        name = "Tiny Probe"
        owns_loop = True
        needs_model = False

        def start(self, *a, **kw):
            print("TINY-START", flush=True)

        def run(self):
            print("TINY-RUN cwd=%s" % os.getcwd(), flush=True)
            print("TINY-MODULE %s" % __name__, flush=True)

        def finish(self):
            print("TINY-FINISH", flush=True)


    if __name__ == "__main__":
        run_app(TinyProbeApp())
    ''')

MANIFEST = '{"id": "tiny-probe", "version": "0.1.0", "entry": "app.py"}'


def _mkapp(tmp, entry="app.py", body=TINY_APP, manifest=MANIFEST):
    d = os.path.join(tmp, "tiny-probe")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, entry), "w") as f:
        f.write(body)
    if manifest is not None:
        with open(os.path.join(d, "manifest.json"), "w") as f:
            f.write(manifest)
    return d


def _run(argv, cwd, pythonpath):
    """Spawn a child with a PRECISELY controlled PYTHONPATH (may be absent)."""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("KIT_PARENT", None)      # the deleted bootstrap's env hook: gone
    env.pop("KIT_DIR", None)
    if pythonpath is not None:
        env["PYTHONPATH"] = pythonpath
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True,
                          text=True, timeout=120)


class TestLaunchModes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kitrun.")
        self.app_dir = _mkapp(self.tmp)

    def _assert_ran(self, p):
        self.assertEqual(p.returncode, 0, f"stdout={p.stdout}\nstderr={p.stderr}")
        self.assertIn("TINY-START", p.stdout)
        self.assertIn("TINY-RUN", p.stdout)
        self.assertIn("TINY-FINISH", p.stdout)
        return p.stdout

    def test_module_entry_runs_the_app(self):
        """`python3 -m kit.run <app_dir>` -- the appmgr launch mode."""
        p = _run([sys.executable, "-m", "kit.run", self.app_dir,
                  "--sink", "stdout", "--quiet"],
                 cwd=_REPO, pythonpath=_REPO)
        out = self._assert_ran(p)
        # kit.run chdir's into the app dir (relative --model / config.json)
        cwd = next(l.split("cwd=", 1)[1] for l in out.splitlines()
                   if l.startswith("TINY-RUN"))
        self.assertEqual(os.path.realpath(cwd), os.path.realpath(self.app_dir))
        # loaded under its own module name, NOT __main__ (so the app's own
        # `if __name__ == "__main__"` tail cannot fire a second run_app)
        self.assertIn("TINY-MODULE recamera_app_tiny_probe_app", out)

    def test_file_entry_runs_with_no_pythonpath_at_all(self):
        """★The reason the per-app bootstrap could be deleted★.

        Invoked by absolute path from an unrelated cwd with PYTHONPATH unset and
        KIT_PARENT/KIT_DIR scrubbed, run.py must still find `kit` -- purely from
        its own __file__. If the derivation were wrong this dies on
        `ModuleNotFoundError: No module named 'kit'`.
        """
        p = _run([sys.executable, RUN_PY, self.app_dir, "--sink", "stdout", "--quiet"],
                 cwd=self.tmp, pythonpath=None)
        self._assert_ran(p)

    def test_bare_app_py_still_works_when_kit_is_importable(self):
        """`python3 app.py` (the `__main__` tail) is unchanged and still runs."""
        p = _run([sys.executable, "app.py", "--sink", "stdout", "--quiet"],
                 cwd=self.app_dir, pythonpath=_REPO)
        self._assert_ran(p)

    def test_bare_app_py_without_kit_on_the_path_fails_loudly(self):
        """Anti-vacuity for the test above: with no PYTHONPATH the bare form
        MUST fail now (that is exactly the 40 lines we deleted). The supported
        answer for that situation is kit.run, covered above."""
        p = _run([sys.executable, "app.py", "--sink", "stdout"],
                 cwd=self.app_dir, pythonpath=None)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("No module named 'kit'", p.stderr)

    def test_entry_from_manifest_is_honoured(self):
        d = _mkapp(self.tmp, entry="main.py",
                   manifest='{"id": "tiny-probe", "entry": "main.py"}')
        p = _run([sys.executable, "-m", "kit.run", d, "--sink", "stdout", "--quiet"],
                 cwd=_REPO, pythonpath=_REPO)
        self._assert_ran(p)

    def test_entry_file_may_be_passed_directly(self):
        p = _run([sys.executable, "-m", "kit.run",
                  os.path.join(self.app_dir, "app.py"),
                  "--sink", "stdout", "--quiet"],
                 cwd=_REPO, pythonpath=_REPO)
        self._assert_ran(p)

    def test_missing_target_is_a_clean_error_not_a_traceback(self):
        p = _run([sys.executable, "-m", "kit.run",
                  os.path.join(self.tmp, "nope")], cwd=_REPO, pythonpath=_REPO)
        self.assertEqual(p.returncode, 2)
        self.assertIn("kit.run:", p.stderr)
        self.assertNotIn("Traceback", p.stderr)

    def test_no_argument_prints_usage(self):
        p = _run([sys.executable, "-m", "kit.run"], cwd=_REPO, pythonpath=_REPO)
        self.assertEqual(p.returncode, 2)
        self.assertIn("usage: python3 -m kit.run", p.stderr)


class TestResolveAndFind(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kitrun-unit.")

    def test_resolve_entry_defaults_to_app_py(self):
        d = _mkapp(self.tmp, manifest=None)
        app_dir, entry = kitrun.resolve_entry(d)
        self.assertEqual(app_dir, os.path.abspath(d))
        self.assertEqual(entry, os.path.join(os.path.abspath(d), "app.py"))

    def test_resolve_entry_rejects_escaping_manifest_entry(self):
        d = _mkapp(self.tmp, manifest='{"entry": "../../etc/passwd"}')
        with self.assertRaises(kitrun.RunError):
            kitrun.resolve_entry(d)
        d2 = _mkapp(self.tmp, manifest='{"entry": "/etc/passwd"}')
        with self.assertRaises(kitrun.RunError):
            kitrun.resolve_entry(d2)

    def test_resolve_entry_rejects_unknown_target(self):
        with self.assertRaises(kitrun.RunError):
            kitrun.resolve_entry(os.path.join(self.tmp, "absent"))

    def _mod(self, src, name):
        class M:
            pass
        m = M()
        ns = {"App": App}
        exec(src, ns)
        m.__dict__.update(ns)
        m.__name__ = name
        m.__file__ = name + ".py"
        return m

    def test_find_app_picks_the_single_subclass(self):
        m = self._mod("class Foo(App):\n    id='foo'\n", "m1")
        for c in vars(m).values():
            if isinstance(c, type) and issubclass(c, App) and c is not App:
                c.__module__ = "m1"
        self.assertEqual(kitrun.find_app(m).id, "foo")

    def test_find_app_prefers_the_leaf_of_a_local_hierarchy(self):
        m = self._mod("class Base(App):\n    id='base'\n"
                      "class Leaf(Base):\n    id='leaf'\n", "m2")
        for c in vars(m).values():
            if isinstance(c, type) and issubclass(c, App) and c is not App:
                c.__module__ = "m2"
        self.assertEqual(kitrun.find_app(m).id, "leaf")

    def test_find_app_refuses_to_guess_between_two_apps(self):
        m = self._mod("class A1(App):\n    id='a1'\n"
                      "class A2(App):\n    id='a2'\n", "m3")
        for c in vars(m).values():
            if isinstance(c, type) and issubclass(c, App) and c is not App:
                c.__module__ = "m3"
        with self.assertRaises(kitrun.RunError) as cm:
            kitrun.find_app(m)
        self.assertIn("APP = ", str(cm.exception))

    def test_find_app_honours_explicit_APP(self):
        m = self._mod("class A1(App):\n    id='a1'\n"
                      "class A2(App):\n    id='a2'\n", "m4")
        for c in vars(m).values():
            if isinstance(c, type) and issubclass(c, App) and c is not App:
                c.__module__ = "m4"
        m.APP = m.A2
        self.assertEqual(kitrun.find_app(m).id, "a2")

    def test_find_app_errors_when_there_is_none(self):
        m = self._mod("x = 1\n", "m5")
        with self.assertRaises(kitrun.RunError):
            kitrun.find_app(m)


def _release_app_lists():
    """Read APPS / UNSHIPPED_APPS out of release/deploy/build-packages.py.

    As TEXT, not by importing it: that module runs packaging at import time in
    some paths and this test has no business triggering it.
    """
    src = open(os.path.join(_REPO, "release", "deploy",
                            "build-packages.py"), encoding="utf-8").read()
    out = {}
    for name in ("APPS", "UNSHIPPED_APPS"):
        m = re.search(rf"^{name} = \[(.*?)^\]", src, re.S | re.M)
        assert m, f"{name} not found in build-packages.py"
        out[name] = sorted(re.findall(r'"([^"]+)"', m.group(1)))
    return out["APPS"], out["UNSHIPPED_APPS"]


_SHIPPED, _UNSHIPPED = _release_app_lists()
_PRESENT = sorted(d for d in os.listdir(APPS_DIR)
                  if os.path.isfile(os.path.join(APPS_DIR, d, "app.py")))
_SHIPPED_PRESENT = [a for a in _PRESENT if a in _SHIPPED]


class TestShippedApps(unittest.TestCase):
    """Every app that ships is launchable by kit.run, and nothing ships by
    accident.

    The inventory used to be pinned to the literal number nine, which stopped
    telling the truth the moment a tenth app was committed -- and it failed on
    apps that are in the tree but deliberately NOT released, dragging the suite
    down with an unrelated missing dependency. The shipped set now comes from
    release/deploy/build-packages.py, which is what actually builds the release,
    and an app in apps/ must appear in one of its two lists.
    """

    SHIPPED, UNSHIPPED = _SHIPPED, _UNSHIPPED
    PRESENT = _PRESENT
    # Only shipped apps are held to the launch contract: an unreleased app is
    # allowed to be mid-refactor, and its dependencies are not installed here.
    APPS = _SHIPPED_PRESENT

    def test_every_app_in_the_tree_is_declared_shipped_or_not(self):
        """★The sentinel★ -- adding an app must be a conscious release decision."""
        undeclared = [a for a in self.PRESENT
                      if a not in self.SHIPPED and a not in self.UNSHIPPED]
        self.assertEqual(
            undeclared, [],
            "apps/ holds app(s) missing from BOTH APPS and UNSHIPPED_APPS in "
            "release/deploy/build-packages.py; add them to one of the two")

    def test_every_shipped_app_exists(self):
        missing = [a for a in self.SHIPPED if a not in self.PRESENT]
        self.assertEqual(missing, [], "build-packages.py ships app(s) that are "
                                      "not in apps/")

    def test_no_app_probes_for_kit_on_sys_path_any_more(self):
        """★The deletion, pinned★ -- one regression here and 40 lines come back."""
        offenders = {}
        for app in self.APPS:
            src = open(os.path.join(APPS_DIR, app, "app.py")).read()
            bad = [tok for tok in ("sys.path.insert", "KIT_PARENT", "KIT_DIR",
                                   "/userdata/local/apps\"", "_kit_parent_env")
                   if tok in src]
            if bad:
                offenders[app] = bad
        self.assertEqual(offenders, {})

    def test_every_app_exposes_exactly_one_app_class_to_kit_run(self):
        for app in self.APPS:
            with self.subTest(app=app):
                entry = os.path.join(APPS_DIR, app, "app.py")
                mod = kitrun.load_app_module(entry)
                instance = kitrun.find_app(mod)
                self.assertIsInstance(instance, App)
                self.assertEqual(instance.id, app)

    def test_every_app_keeps_its_main_tail(self):
        for app in self.APPS:
            with self.subTest(app=app):
                src = open(os.path.join(APPS_DIR, app, "app.py")).read()
                self.assertIn('if __name__ == "__main__":', src)
                self.assertIn("run_app(", src)


if __name__ == "__main__":
    unittest.main()

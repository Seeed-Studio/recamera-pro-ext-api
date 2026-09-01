#!/usr/bin/env python3
"""
kit.run -- the ONE launcher for a reCamera Pro app (internal/KIT_APP_SHAPE_SPEC.md §5.1).

    python3 -m kit.run <app_dir|app.py> [--model ... --sink ... --port ...]
    python3 /userdata/local/kit/kit/run.py <app_dir>        # no PYTHONPATH needed

Why this file exists
--------------------
Every app.py used to carry ~40 identical lines that guessed where `kit/` lives
(KIT_PARENT / KIT_DIR env, `..`, `../..`, `/userdata/local/apps`, ...) and
pushed it onto sys.path. That knowledge is DEPLOYMENT LAYOUT, not application
logic, and 9 apps had 9 byte-identical copies of it. It is also dead in
production: `market/appmgr/supervisor.py:start()` already exports PYTHONPATH +
KIT_PARENT before exec'ing the app. It only ever served "developer ssh's onto
the device and runs the app by hand".

So the knowledge moves HERE, to one place that cannot be wrong: this module is
`<KIT_PARENT>/kit/run.py`, therefore KIT_PARENT is two dirname() calls up. No
probing, no env vars, no device fallbacks. That derivation runs before any
`kit.*` import below, so `python3 path/to/kit/run.py` works with an empty
PYTHONPATH, and `python3 -m kit.run` (how appmgr launches apps) works too.

What it does
------------
1. put KIT_PARENT on sys.path (from this file's own location);
2. put the APP DIR on sys.path, so an app may `import` its own sibling modules;
3. import `<app_dir>/<entry>` (entry from manifest.json, default `app.py`)
   under a unique module name -- NOT `__main__`, so the app's own
   `if __name__ == "__main__": run_app(...)` tail stays inert here;
4. find the single `kit.app.App` subclass defined in it;
5. hand the remaining argv to `kit.app.run_app` -- identical CLI to before
   (`--model / --sink / --port / --source / --url / --quiet / ...`).

`python3 app.py` keeps working unchanged when kit is already importable (that
tail is still there, and appmgr's PYTHONPATH still makes it resolvable).
"""
import os
import sys

# --- step 1: the only place that knows where kit/ lives --------------------- #
# <KIT_PARENT>/kit/run.py  ->  dirname twice  ->  <KIT_PARENT>
KIT_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if KIT_PARENT not in sys.path:
    sys.path.insert(0, KIT_PARENT)

import importlib.util                                                # noqa: E402
import json                                                          # noqa: E402
import re                                                            # noqa: E402
from typing import Any, List, Optional, Tuple                        # noqa: E402

from kit.app import App, run_app                                     # noqa: E402
from kit.errors import ConfigurationError                            # noqa: E402


USAGE = (
    "usage: python3 -m kit.run <app_dir|app.py> [app options]\n"
    "       (app options are the usual --model/--sink/--port/--source/"
    "--url/--quiet/...; run with an app and --help to list them)\n"
)


class RunError(ConfigurationError):
    """Bad target / unloadable app -- reported as a one-line error, not a traceback."""


# --------------------------------------------------------------------------- #
def resolve_entry(target: str) -> Tuple[str, str]:
    """`target` (an app dir OR an entry file) -> (app_dir, entry_path).

    For a directory we honour the manifest's `entry` field (same contract the
    supervisor uses), defaulting to `app.py`.
    """
    target = os.path.abspath(target)
    if os.path.isfile(target):
        return os.path.dirname(target), target
    if not os.path.isdir(target):
        raise RunError(f"no such app dir or entry file: {target}")

    entry = "app.py"
    try:
        with open(os.path.join(target, "manifest.json")) as f:
            declared = (json.load(f) or {}).get("entry")
        if isinstance(declared, str) and declared:
            entry = declared
    except (OSError, ValueError):
        pass                       # no/broken manifest -> plain app.py
    if entry.startswith("/") or ".." in entry.split("/"):
        raise RunError(f"unsafe manifest entry {entry!r}")

    path = os.path.join(target, entry)
    if not os.path.isfile(path):
        raise RunError(f"entry not found: {path}")
    return target, path


def _module_name(entry_path: str) -> str:
    """A unique, import-safe module name for the app.

    Never plain `app`: keeping it distinct avoids any chance of shadowing and
    makes the app identifiable in tracebacks. `kit.config.app_dir_of()` looks
    the module up in sys.modules by this name, so the loader must register it.
    """
    app_dir = os.path.basename(os.path.dirname(entry_path)) or "app"
    stem = os.path.splitext(os.path.basename(entry_path))[0]
    safe = re.sub(r"\W", "_", f"{app_dir}_{stem}")
    return f"recamera_app_{safe}"


def load_app_module(entry_path: str):
    """Import the app's entry file with its own directory on sys.path."""
    app_dir = os.path.dirname(entry_path)
    if app_dir not in sys.path:
        # So an app can `import` helper modules sitting next to its app.py.
        # (Its directory name usually has a hyphen, so it is not a package.)
        sys.path.insert(0, app_dir)

    name = _module_name(entry_path)
    spec = importlib.util.spec_from_file_location(name, entry_path)
    if spec is None or spec.loader is None:
        raise RunError(f"cannot load {entry_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod        # register BEFORE exec: app_dir_of() needs it
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return mod


def find_app(mod) -> App:
    """Return the App INSTANCE this module wants run.

    Preference order:
      1. an explicit `APP` attribute (instance or class) -- the escape hatch;
      2. the single App subclass DEFINED in this module;
      3. the single App subclass visible in it (covers a re-exported class).
    Several leaf candidates is an error, not a coin flip.
    """
    explicit = getattr(mod, "APP", None)
    if explicit is not None:
        if isinstance(explicit, App):
            return explicit
        if isinstance(explicit, type) and issubclass(explicit, App):
            return explicit()
        raise RunError(f"{mod.__name__}.APP is not an App subclass or instance")

    visible = [v for v in vars(mod).values()
               if isinstance(v, type) and issubclass(v, App) and v is not App]
    own = [c for c in visible if c.__module__ == mod.__name__]
    cands = own or visible
    # Drop base classes an app defines on the way to its real entry class.
    leaves = [c for c in cands if not any(o is not c and issubclass(o, c)
                                          for o in cands)]
    # de-dup while keeping order (`class X` may be bound to two names)
    uniq: List[Any] = []
    for c in leaves:
        if c not in uniq:
            uniq.append(c)

    if not uniq:
        raise RunError(f"no kit.app.App subclass found in {mod.__file__}")
    if len(uniq) > 1:
        names = ", ".join(sorted(c.__name__ for c in uniq))
        raise RunError(
            f"{mod.__file__} defines several App subclasses ({names}); "
            f"set `APP = <TheOne>()` to disambiguate")
    return uniq[0]()


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        sys.stderr.write(USAGE)
        return 2
    if argv[0] in ("-h", "--help"):
        sys.stderr.write(USAGE)
        return 0
    target, app_argv = argv[0], argv[1:]
    try:
        app_dir, entry_path = resolve_entry(target)
        app = find_app(load_app_module(entry_path))
    except RunError as e:
        sys.stderr.write(f"kit.run: {e}\n")
        return 2
    # cwd = app dir is the contract every app already relies on (relative
    # --model paths, config.json lookup); appmgr launches that way, so match it
    # when a developer runs from somewhere else.
    if os.path.isdir(app_dir):
        os.chdir(app_dir)
    # So `--help` prints `usage: kit.run <id> ...` instead of `usage: run.py`.
    sys.argv[0] = f"kit.run {os.path.basename(app_dir)}"
    run_app(app, app_argv)
    return 0


if __name__ == "__main__":
    sys.exit(main())

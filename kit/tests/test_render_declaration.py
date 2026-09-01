"""
Unit tests for the render display declaration (internal/RENDER_DECLARATION_SPEC).

Covers the two backend halves of the chain:

  * `effective_render()` -- manifest `render` defaults merged with the running
    config (§1/§2), with layout/skeleton/`as` frozen against config tampering;
  * `App.emit()` -- injects the EFFECTIVE block as `envelope.render` (§3), skips
    the key entirely for an app that declares nothing (backward compat), and
    recomputes only when the config version moves (per-frame cost = one compare).

The headline case is `test_live_tune_changes_next_frame_envelope`: edit
config.json -> SIGHUP -> the very next frame's envelope carries the new dot
radius. No repackage, no release. That is the whole point of §2.

Hardware-free: no model, no camera, a recording stub sink.

Run:  python3 -m pytest kit/tests/test_render_declaration.py
"""
import json
import os
import signal
import sys
import tempfile
import unittest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from kit.app import App, effective_render      # noqa: E402
from kit import config as kcfg                 # noqa: E402


FACEMESH_RENDER = {
    "keypoints": {"layout": "facemesh468", "point_radius": 1, "skeleton": []}
}


class _StubSink:
    """Minimal ResultSink stand-in: records every published envelope."""

    def __init__(self):
        self.published = []

    def set_frame_size(self, w, h):
        pass

    def emit(self, payload, ts):
        self.published.append(payload)

    def on_config_reload(self, config):
        pass


def _write(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f)


class EffectiveRenderTests(unittest.TestCase):
    """§1/§2 merge semantics, independent of the App runtime."""

    def test_no_declaration_returns_none(self):
        self.assertIsNone(effective_render({}, {"point_radius": 9}))
        self.assertIsNone(effective_render({"render": {}}, {}))
        self.assertIsNone(effective_render(None, None))

    def test_declared_defaults_pass_through(self):
        out = effective_render({"render": FACEMESH_RENDER}, {})
        self.assertEqual(out, FACEMESH_RENDER)

    def test_config_overrides_same_named_visual_param(self):
        out = effective_render({"render": FACEMESH_RENDER}, {"point_radius": 4})
        self.assertEqual(out["keypoints"]["point_radius"], 4)
        # the declaration dict itself must not be mutated (it is the manifest)
        self.assertEqual(FACEMESH_RENDER["keypoints"]["point_radius"], 1)

    def test_none_valued_config_never_wipes_a_default(self):
        out = effective_render({"render": FACEMESH_RENDER}, {"point_radius": None})
        self.assertEqual(out["keypoints"]["point_radius"], 1)

    def test_undeclared_config_keys_do_not_leak_in(self):
        out = effective_render({"render": FACEMESH_RENDER},
                               {"conf": 0.9, "max_faces": 3})
        self.assertEqual(set(out["keypoints"]), set(FACEMESH_RENDER["keypoints"]))

    def test_layout_and_skeleton_are_frozen(self):
        out = effective_render({"render": FACEMESH_RENDER},
                               {"layout": "coco17", "skeleton": [[0, 1]]})
        self.assertEqual(out["keypoints"]["layout"], "facemesh468")
        self.assertEqual(out["keypoints"]["skeleton"], [])

    def test_event_primitive_as_is_frozen(self):
        man = {"render": {"events": {"line_cross": {"as": "toast",
                                                    "duration_sec": 2}}}}
        out = effective_render(man, {"as": "panel"})
        self.assertEqual(out["events"]["line_cross"]["as"], "toast")

    def test_prefixed_key_beats_bare_key(self):
        man = {"render": {"events": {
            "line_cross": {"as": "toast", "duration_sec": 2},
            "alarm": {"as": "toast", "duration_sec": 2},
        }}}
        out = effective_render(man, {"duration_sec": 5,
                                     "line_cross_duration_sec": 9})
        self.assertEqual(out["events"]["line_cross"]["duration_sec"], 9,
                         "specific <kind>_<key> wins")
        self.assertEqual(out["events"]["alarm"]["duration_sec"], 5,
                         "bare key still reaches the other declaration")

    def test_section_prefix_for_boxes(self):
        man = {"render": {"boxes": {"color_by": "cls", "line_width": 2},
                          "keypoints": {"layout": "coco17", "line_width": 2}}}
        out = effective_render(man, {"boxes_line_width": 6, "line_width": 3})
        self.assertEqual(out["boxes"]["line_width"], 6)
        self.assertEqual(out["keypoints"]["line_width"], 3)


class _StartedApp(App):
    """App wired up enough to call emit() -- no model, no frames, stub sink."""
    needs_model = False
    needs_frames = False
    owns_loop = True
    id = "unit-render"

    def run(self):
        pass


class EmitInjectionTests(unittest.TestCase):
    """§3: the effective block rides on every envelope."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="kit-render.")
        self._orig_app_dir_of = kcfg.app_dir_of
        kcfg.app_dir_of = lambda app: self.dir

    def tearDown(self):
        kcfg.app_dir_of = self._orig_app_dir_of
        try:
            signal.signal(signal.SIGHUP, signal.SIG_DFL)
        except (ValueError, OSError):
            pass

    def _manifest(self, render=None, schema_items=None):
        man = {"id": "unit-render"}
        if render is not None:
            man["render"] = render
        man["config_schema"] = {"groups": [{"key": "display", "items":
                                            list(schema_items or [])}]}
        _write(os.path.join(self.dir, "manifest.json"), man)
        return man

    def _app(self, man):
        app = _StartedApp()
        self.sink = _StubSink()
        app._manifest = man
        app._rt = {"sink": self.sink}
        app._warmed = True
        app.config = kcfg.effective_config(self.dir, manifest=man)
        return app

    def test_declared_app_injects_render(self):
        man = self._manifest(FACEMESH_RENDER)
        app = self._app(man)
        app.emit(events=[])
        self.assertEqual(self.sink.published[-1]["render"], FACEMESH_RENDER)

    def test_undeclared_app_has_no_render_key(self):
        """Backward compat: the front end must fall through to shape-driven."""
        man = self._manifest(None)
        app = self._app(man)
        app.emit(events=[])
        self.assertNotIn("render", self.sink.published[-1])

    def test_geometry_is_opt_in_and_explicit_empty_is_preserved(self):
        """Existing emitters keep their byte shape; explicit geometry opts in."""
        man = self._manifest(None)
        app = self._app(man)

        app.emit(events=[])
        self.assertNotIn("geometry", self.sink.published[-1])

        app.emit(events=[], geometry=[])
        self.assertEqual(self.sink.published[-1]["geometry"], [])

        app.emit(events=[], geometry=[{
            "type": "point", "points": [[12, 34]],
            "style": {"color": "#00ff00"},
        }])
        self.assertEqual(self.sink.published[-1]["geometry"], [{
            "type": "point", "points": [[12.0, 34.0]],
            "style": {"color": "#00ff00"},
        }])

    def test_broken_declaration_degrades_to_no_key(self):
        man = self._manifest(["not", "a", "dict"])
        app = self._app(man)
        app.emit(events=[])
        self.assertNotIn("render", self.sink.published[-1])

    def test_block_is_cached_between_frames(self):
        man = self._manifest(FACEMESH_RENDER)
        app = self._app(man)
        for _ in range(3):
            app.emit(events=[])
        first = self.sink.published[0]["render"]
        self.assertIs(self.sink.published[1]["render"], first,
                      "unchanged config must reuse the merged block")
        self.assertIs(self.sink.published[2]["render"], first)

    # ---- the headline case: zero-release visual tuning (§2) -------------- #
    def test_live_tune_changes_next_frame_envelope(self):
        man = self._manifest(FACEMESH_RENDER, schema_items=[
            {"key": "point_radius", "type": "integer", "apply": "live",
             "default": 1},
        ])
        app = self._app(man)
        app.emit(events=[])
        self.assertEqual(
            self.sink.published[-1]["render"]["keypoints"]["point_radius"], 1)

        # user drags the slider -> appmgr writes config.json -> SIGHUP
        _write(os.path.join(self.dir, "config.json"), {"point_radius": 4})
        app._on_sighup(signal.SIGHUP, None)

        app.tick()          # the loop's per-frame reload check
        app.emit(events=[])
        self.assertEqual(
            self.sink.published[-1]["render"]["keypoints"]["point_radius"], 4,
            "next frame carries the new radius -- no repackage, no release")
        # ...and the cache followed the config version rather than going stale
        self.assertIsNot(self.sink.published[-1]["render"],
                         self.sink.published[0]["render"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

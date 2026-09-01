"""
Manifest hygiene tests for the in-repo apps.

Pins the invariants the kit/appmgr schema code now relies on:

  * `config_schema` is the CANONICAL GROUPED form (`groups[].items[]`) in every
    app. The flat `{key: spec}` form still parses (deprecated compat branch in
    `kit.config._flat_to_grouped` / `appmgr.config._flat_to_grouped`) but no app
    in this repo may reintroduce it -- two parallel shapes is what forced every
    reader to carry a double branch.
  * no `pipeline` key: nothing reads it, so a published one silently misleads.
  * every primary sample declares a bounded raster icon that is packaged with
    the release, while retaining its catalog `image` URL for old clients.

Run:  python3 -m pytest apps/test_manifest_schema.py
"""
import glob
import json
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from kit import config as kitconfig                                # noqa: E402
from market.appmgr import manifest as manifest_contract            # noqa: E402

_MANIFESTS = sorted(glob.glob(os.path.join(_ROOT, "apps", "*", "manifest.json")))
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_MAX_ICON_BYTES = 1024 * 1024


def _load(path):
    with open(path) as f:
        return json.load(f)


class ManifestSchemaShapeTests(unittest.TestCase):

    def test_manifests_found(self):
        self.assertGreaterEqual(len(_MANIFESTS), 9, _MANIFESTS)

    def test_every_shipped_manifest_passes_the_production_v2_validator(self):
        for path in _MANIFESTS:
            with self.subTest(app=os.path.basename(os.path.dirname(path))):
                self.assertEqual(manifest_contract.validate_manifest(_load(path)), 2)

    def test_every_shipped_manifest_declares_a_safe_packaged_icon(self):
        """Examples must work after install without a front-end asset rebuild."""
        for path in _MANIFESTS:
            app_dir = os.path.dirname(path)
            app_id = os.path.basename(app_dir)
            manifest = _load(path)
            with self.subTest(app=app_id):
                self.assertEqual(manifest.get("icon"), {
                    "path": "icon.png",
                    "media_type": "image/png",
                })
                # Keep the catalog thumbnail for old catalog clients while the
                # declared package asset is authoritative after installation.
                self.assertEqual(
                    manifest.get("image"), f"/appcenter/apps/{app_id}.png")
                icon_path = os.path.join(app_dir, manifest["icon"]["path"])
                self.assertFalse(os.path.islink(icon_path))
                self.assertTrue(os.path.isfile(icon_path))
                size = os.path.getsize(icon_path)
                self.assertGreater(size, len(_PNG_SIGNATURE))
                self.assertLessEqual(size, _MAX_ICON_BYTES)
                with open(icon_path, "rb") as icon_file:
                    self.assertEqual(icon_file.read(len(_PNG_SIGNATURE)),
                                     _PNG_SIGNATURE)

    def test_config_schema_is_grouped(self):
        for path in _MANIFESTS:
            with self.subTest(app=os.path.basename(os.path.dirname(path))):
                cs = _load(path).get("config_schema")
                self.assertIsInstance(cs, dict)
                self.assertIn("groups", cs,
                              "flat config_schema is deprecated; publish "
                              "config_schema.groups[].items[]")
                for g in cs["groups"]:
                    self.assertIn("items", g)
                    for it in g["items"]:
                        self.assertIn("key", it)

    def test_no_dead_pipeline_field(self):
        for path in _MANIFESTS:
            with self.subTest(app=os.path.basename(os.path.dirname(path))):
                self.assertNotIn("pipeline", _load(path),
                                 "`pipeline` has no consumer; drop it")

    def test_schema_items_reads_every_declared_key(self):
        """The single grouped reader sees the same keys the JSON declares."""
        for path in _MANIFESTS:
            man = _load(path)
            declared = {it["key"]
                        for g in man["config_schema"].get("groups", [])
                        for it in g.get("items", [])}
            with self.subTest(app=os.path.basename(os.path.dirname(path))):
                self.assertEqual(set(kitconfig.schema_items(man)), declared)


# --------------------------------------------------------------------------- #
# integer-semantics controls
# --------------------------------------------------------------------------- #
# Keys whose value is a COUNT / INDEX / FRAME INTERVAL: they must bind as `int`
# because they end up in slices (`results[:max_faces]`), modulo arithmetic and
# event payloads where `12.0` is the wrong wire value.
_INTEGER_KEYS = {
    "face-analysis": {"max_faces", "emotion_interval"},
    "facemesh-reader": {"yawn_consecutive_frames", "yawn_count_threshold"},
    "fall-detection": {"min_suspected_features"},
    "ppocr-reader": {"max_boxes"},
    "fitness-trainer": {"target_reps", "target_sets"},
}

# Keys with an INTEGER-LOOKING default that are semantically FLOAT -- angles the
# user may set to 55.5, seconds, px/s speeds. Deliberately left `type:"number"`;
# a blanket "integral default => integer" sweep would have silently truncated
# these the first time somebody typed a decimal.
_DELIBERATE_NUMBER_KEYS = {
    "fall-detection": {"torso_angle_threshold_deg", "recovery_torso_angle_deg"},
    "fitness-trainer": {"idle_reset_seconds"},
    "retail-vision": {"dwell_assist", "dwell_speed", "window_duration"},
}


class RuntimeApplySemanticsTests(unittest.TestCase):

    def test_voice_graph_parameters_require_restart(self):
        """These values are captured by source/VAD/KWS/SM constructors."""

        man = _load(os.path.join(_ROOT, "apps", "voice-transcribe",
                                 "manifest.json"))
        specs = kitconfig.schema_items(man)
        captured = {
            "wakeword", "min_silence_sec", "max_utterance_sec",
            "preroll_ms", "listen_timeout_sec", "audio_filter",
        }
        self.assertEqual(
            {key: specs[key]["apply"] for key in captured},
            {key: "restart" for key in captured},
        )


class IntegerSemanticsTests(unittest.TestCase):

    def _specs(self, app):
        man = _load(os.path.join(_ROOT, "apps", app, "manifest.json"))
        return kitconfig.schema_items(man)

    def test_declared_integer_keys(self):
        for app, keys in _INTEGER_KEYS.items():
            specs = self._specs(app)
            for k in keys:
                with self.subTest(app=app, key=k):
                    self.assertEqual(specs[k]["type"], "integer")
                    self.assertIsInstance(specs[k]["default"], int)
                    self.assertNotIsInstance(specs[k]["default"], bool)

    def test_float_semantics_keys_stay_number(self):
        for app, keys in _DELIBERATE_NUMBER_KEYS.items():
            specs = self._specs(app)
            for k in keys:
                with self.subTest(app=app, key=k):
                    self.assertEqual(specs[k]["type"], "number")

    def test_integer_type_binds_to_int(self):
        """`_coerce` is what the auto-bind runs; it must produce a real int."""
        from kit.app import _coerce
        self.assertIsInstance(_coerce("integer", 5), int)
        self.assertIsInstance(_coerce("integer", 5.0), int)
        self.assertIsInstance(_coerce("integer", "7"), int)
        self.assertIsInstance(_coerce("number", 5), float)

    def test_no_other_integer_looking_number_key_slipped_in(self):
        """A new `type:"number"` with an integral default is a review prompt.

        Either it is a count (-> `integer`) or a genuine float that happens to
        default to a round value (-> add it to _DELIBERATE_NUMBER_KEYS)."""
        unclassified = []
        for path in _MANIFESTS:
            app = os.path.basename(os.path.dirname(path))
            known = _DELIBERATE_NUMBER_KEYS.get(app, set())
            for k, spec in kitconfig.schema_items(_load(path)).items():
                d = spec.get("default")
                if (spec.get("type") == "number" and isinstance(d, int)
                        and not isinstance(d, bool) and k not in known):
                    unclassified.append(f"{app}.{k}")
        self.assertEqual(unclassified, [], unclassified)


class RenderDeclarationTests(unittest.TestCase):
    """Shipped `render` blocks (RENDER_DECLARATION_SPEC §1/§7).

    The overlay is a generic renderer: it only knows `layout` names and the five
    display primitives. A typo here silently drops the app back to the
    shape-driven fallback, which is exactly the class of bug this pins.
    """

    _LAYOUTS = {"coco17", "facemesh468", "hand21", "custom"}
    _PRIMITIVES = {"subtitle", "panel", "toast", "badge", "none"}

    # app -> expected keypoint layout (§7)
    _EXPECTED_LAYOUT = {
        "facemesh-reader": "facemesh468",
        "fall-detection": "coco17",
        "fitness-trainer": "coco17",
    }
    # app -> {event kind: expected primitive} (§7)
    _EXPECTED_EVENTS = {
        "voice-transcribe": {"transcript": "subtitle"},
        "retail-vision": {"line_cross": "toast", "metrics": "panel"},
        "ppocr-reader": {"text": "badge"},
    }

    def _render(self, app):
        return _load(os.path.join(_ROOT, "apps", app, "manifest.json")).get("render")

    def test_expected_keypoint_layouts(self):
        for app, layout in self._EXPECTED_LAYOUT.items():
            with self.subTest(app=app):
                self.assertEqual(self._render(app)["keypoints"]["layout"], layout)

    def test_expected_event_primitives(self):
        for app, kinds in self._EXPECTED_EVENTS.items():
            events = (self._render(app) or {}).get("events") or {}
            for kind, prim in kinds.items():
                with self.subTest(app=app, kind=kind):
                    self.assertEqual(events[kind]["as"], prim)

    def test_facemesh_draws_no_skeleton_with_1px_dots(self):
        kp = self._render("facemesh-reader")["keypoints"]
        self.assertEqual(kp["point_radius"], 1)
        self.assertEqual(kp["skeleton"], [], "468 landmarks: dots only")

    def test_yolo_colours_boxes_by_emitted_class_name(self):
        self.assertEqual(self._render("yolo-detector")["boxes"]["color_by"],
                         "cls_name")

    def test_every_declaration_uses_known_vocabulary(self):
        for path in _MANIFESTS:
            app = os.path.basename(os.path.dirname(path))
            render = _load(path).get("render")
            if render is None:
                continue            # declaring nothing is legal (§3 fallback)
            with self.subTest(app=app):
                self.assertIsInstance(render, dict)
                kp = render.get("keypoints")
                if kp is not None:
                    self.assertIn(kp.get("layout"), self._LAYOUTS)
                for kind, spec in (render.get("events") or {}).items():
                    self.assertIn(spec.get("as"), self._PRIMITIVES,
                                  f"{app}.events.{kind}")

    def test_facemesh_point_radius_is_live_tunable(self):
        """§2: at least one shipped app proves visual params need no release."""
        man = _load(os.path.join(_ROOT, "apps", "facemesh-reader",
                                 "manifest.json"))
        spec = kitconfig.schema_items(man)["point_radius"]
        self.assertEqual(spec["apply"], "live")
        self.assertEqual(spec["type"], "integer")
        self.assertEqual(spec["default"],
                         man["render"]["keypoints"]["point_radius"],
                         "schema default must match the declared default")


class FlatCompatBranchTests(unittest.TestCase):
    """The deprecated flat form must still parse (third-party packages)."""

    def test_flat_schema_still_flattens(self):
        man = {"id": "legacy-flat",
               "config_schema": {"conf": {"type": "number", "default": 0.3}}}
        self.assertEqual(kitconfig.schema_items(man)["conf"]["default"], 0.3)
        self.assertEqual(kitconfig.flatten_schema(man), {"conf": 0.3})


if __name__ == "__main__":
    unittest.main(verbosity=2)

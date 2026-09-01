"""
Unit tests for the injected `output` config_schema group (OUTPUT_SINK_SPEC §3).

`effective_manifest` injects the 8 output keys only for apps that declare
`capabilities:["output"]`; it must be pure + idempotent, and every consumer
(schema_specs / get_config / validate_config / apply-mode) must agree on the
keys. Apps without the capability are untouched (legacy bypass).

Runnable with plain stdlib: `python3 tests/test_output_config.py` (or pytest).
"""
import json
import os
import sys
import tempfile
import unittest

_BASE = tempfile.mkdtemp(prefix="appmgr-outcfg.")
os.environ["APPMGR_APPS_DIR"] = os.path.join(_BASE, "apps")
os.environ["APPMGR_DIR"] = os.path.join(_BASE, "appmgr")
os.environ["APPMGR_VENVS_DIR"] = os.path.join(_BASE, "venvs")
os.environ["APPMGR_MODEL_ROOTS"] = os.path.join(_BASE, "models")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from appmgr import config as appconfig  # noqa: E402


_OUTPUT_KEYS = {"output_channels", "iMode", "dMqtt", "dHttp", "dUart",
                "dTemplate", "template_mode", "output_mapping", "output_filters"}


def _manifest(capabilities, config_schema=None, output=None):
    m = {"name": "T", "capabilities": capabilities}
    if config_schema is not None:
        m["config_schema"] = config_schema
    if output is not None:
        m["output"] = output
    return m


class OutputSchemaInjectionTests(unittest.TestCase):

    def test_bypass_when_no_capability(self):
        man = _manifest([], config_schema={"conf": {"type": "number", "default": 0.25}})
        specs = appconfig.schema_specs(man)
        self.assertIn("conf", specs)
        self.assertFalse(_OUTPUT_KEYS & set(specs))    # nothing injected

    def test_injected_keys_present_when_opted_in(self):
        man = _manifest(["output"],
                        config_schema={"conf": {"type": "number", "default": 0.25}},
                        output={"default_channel": ["mqtt"], "default_mode": "custom"})
        specs = appconfig.schema_specs(man)
        self.assertTrue(_OUTPUT_KEYS <= set(specs))    # all 8 present
        self.assertIn("conf", specs)                   # original preserved
        # defaults flow from the manifest output block
        self.assertEqual(specs["output_channels"]["default"], ["mqtt"])
        self.assertEqual(specs["iMode"]["default"], "custom")

    def test_apply_modes(self):
        man = _manifest(["output"])
        specs = appconfig.schema_specs(man)
        self.assertEqual(specs["dMqtt"]["apply"], "restart")
        self.assertEqual(specs["output_filters"]["apply"], "live")
        self.assertEqual(specs["dTemplate"]["apply"], "live")
        self.assertEqual(specs["template_mode"]["apply"], "live")

    def test_effective_manifest_idempotent(self):
        man = _manifest(["output"], output={"default_channel": ["ws"]})
        once = appconfig.effective_manifest(man)
        twice = appconfig.effective_manifest(once)
        self.assertEqual(len(appconfig.schema_specs(once)),
                         len(appconfig.schema_specs(twice)))
        # original manifest object not mutated
        self.assertNotIn("config_schema", man)

    def test_grouped_schema_gets_output_group(self):
        cs = {"groups": [{"title": "Detection",
                          "items": [{"key": "conf", "type": "number", "default": 0.25}]}]}
        man = _manifest(["output"], config_schema=cs)
        em = appconfig.effective_manifest(man)
        titles = [g.get("title") for g in em["config_schema"]["groups"]]
        self.assertIn("Output", titles)
        specs = appconfig.schema_specs(man)
        self.assertTrue(_OUTPUT_KEYS <= set(specs))

    def test_get_config_exposes_injected_schema(self):
        man = _manifest(["output"], config_schema={"conf": {"type": "number", "default": 0.25}})
        os.makedirs(appconfig.paths.app_dir("t-app"), exist_ok=True)
        resp = appconfig.get_config(man, "t-app")
        # the returned config_schema carries the injected output keys, and their
        # defaults are visible in `values`/`defaults`
        specs = appconfig.schema_specs({"config_schema": resp["config_schema"]})
        self.assertTrue(_OUTPUT_KEYS <= set(specs))
        self.assertIn("dMqtt", resp["defaults"])
        self.assertIn("output_filters", resp["values"])

    def test_validate_accepts_structured_output_values(self):
        man = _manifest(["output"])
        clean, errors = appconfig.validate_config(man, {
            "iMode": "raw",
            "dMqtt": {"sURL": "127.0.0.1", "iPort": 1883},
            "output_filters": {"only_on_detection": True, "rate_limit_hz": 5},
            "output_channels": ["mqtt", "http"],
        })
        self.assertEqual(errors, [])
        self.assertEqual(clean["iMode"], "raw")
        self.assertEqual(clean["dMqtt"]["sURL"], "127.0.0.1")
        self.assertEqual(clean["output_channels"], ["mqtt", "http"])

    def test_validate_rejects_opaque_complex_values(self):
        man = _manifest(["output"])
        for key, value in {
            "dMqtt": "broker:1883",
            "dHttp": "https://example.test",
            "dTemplate": "{{ detection.count }}",
            "output_mapping": "detection.count -> count",
            "output_filters": "all",
            "output_channels": "mqtt",
        }.items():
            _, errors = appconfig.validate_config(man, {key: value})
            self.assertTrue(any(key in error for error in errors),
                            (key, errors))

    def test_template_mode_is_explicit_and_typed(self):
        man = _manifest(["output"], output={"default_mapping": [{
            "source": "detection.count", "target": "count", "topic": "t"}]})
        self.assertEqual(appconfig.schema_defaults(man)["template_mode"], "mapping")
        clean, errors = appconfig.validate_config(
            man, {"template_mode": "template"})
        self.assertEqual(errors, [])
        self.assertEqual(clean, {"template_mode": "template"})
        _, errors = appconfig.validate_config(man, {"template_mode": "auto"})
        self.assertTrue(errors)

    def test_legacy_empty_mapping_keeps_historical_template_fallback(self):
        man = _manifest(["output"], output={"default_mapping": [{
            "source": "detection.count", "target": "count", "topic": "t"}]})
        app_id = "legacy-template-mode"
        os.makedirs(appconfig.paths.app_dir(app_id), exist_ok=True)
        appconfig.write_user_config(app_id, {
            "output_mapping": [],
            "dTemplate": {"sDetection": "{{ detection.count }}"},
        })
        effective = appconfig.effective_values(man, app_id)
        self.assertEqual(effective["template_mode"], "template")

        appconfig.write_user_config(app_id, {"template_mode": "mapping"})
        explicit = appconfig.effective_values(man, app_id)
        self.assertEqual(explicit["template_mode"], "mapping")

    def test_validate_rejects_bad_enum_and_unknown(self):
        man = _manifest(["output"])
        _, errors = appconfig.validate_config(man, {"iMode": "bogus"})
        self.assertTrue(any("iMode" in e for e in errors))
        _, errors2 = appconfig.validate_config(man, {"not_a_key": 1})
        self.assertTrue(any("not_a_key" in e for e in errors2))

    def test_output_keys_rejected_without_capability(self):
        man = _manifest([])          # not opted in -> output keys are unknown
        _, errors = appconfig.validate_config(man, {"dMqtt": {"sURL": "x"}})
        self.assertTrue(any("dMqtt" in e for e in errors))


class IntegerControlTests(unittest.TestCase):
    """`type:"integer"` validates to a real int and enforces min/max."""

    SPEC = {"groups": [{"key": "g", "items": [
        {"key": "max_faces", "type": "integer", "default": 5,
         "min": 1, "max": 16, "step": 1}]}]}

    def _man(self):
        return _manifest([], config_schema=self.SPEC)

    def test_accepts_int_and_keeps_it_int(self):
        clean, errors = appconfig.validate_config(self._man(), {"max_faces": 8})
        self.assertEqual(errors, [])
        self.assertIsInstance(clean["max_faces"], int)
        self.assertEqual(clean["max_faces"], 8)

    def test_accepts_integral_float_and_narrows(self):
        clean, errors = appconfig.validate_config(self._man(), {"max_faces": 8.0})
        self.assertEqual(errors, [])
        self.assertIsInstance(clean["max_faces"], int)

    def test_rejects_fractional(self):
        _, errors = appconfig.validate_config(self._man(), {"max_faces": 8.5})
        self.assertTrue(any("max_faces" in e for e in errors), errors)

    def test_enforces_range(self):
        for bad in (0, 17):
            _, errors = appconfig.validate_config(self._man(), {"max_faces": bad})
            self.assertTrue(any("max_faces" in e for e in errors), (bad, errors))

    def test_rejects_invalid_step_schema_at_runtime_boundary(self):
        manifest = self._man()
        manifest["config_schema"]["groups"][0]["items"][0]["step"] = 0
        _, errors = appconfig.validate_config(manifest, {"max_faces": 7})
        self.assertTrue(any("step" in e for e in errors), errors)

    def test_step_is_ui_increment_not_server_quantization_grid(self):
        manifest = _manifest([], config_schema={"groups": [{"key": "g", "items": [{
            "key": "window", "type": "number", "apply": "live",
            "min": 0, "max": 5, "step": 0.1, "default": 0.75,
        }]}]})
        clean, errors = appconfig.validate_config(manifest, {"window": 0.73})
        self.assertEqual(errors, [])
        self.assertEqual(clean["window"], 0.73)


class DropdownControlTests(unittest.TestCase):
    def test_labelled_options_preserve_typed_value(self):
        manifest = _manifest([], config_schema={"groups": [{"key": "g", "items": [{
            "key": "backend", "type": "select", "apply": "restart",
            "default": 1,
            "options": [
                {"value": 1, "label": "Fast", "label_zh": "快速"},
                {"value": 2, "label": "Accurate", "label_zh": "精确"},
            ],
        }]}]})
        clean, errors = appconfig.validate_config(manifest, {"backend": 2})
        self.assertEqual(errors, [])
        self.assertEqual(clean["backend"], 2)
        clean_float, errors = appconfig.validate_config(manifest, {"backend": 2.0})
        self.assertEqual(errors, [])
        self.assertEqual(clean_float["backend"], 2.0)
        _, errors = appconfig.validate_config(manifest, {"backend": "2"})
        self.assertTrue(errors, "browser string must not silently change enum type")

    def test_numeric_enum_options_cannot_duplicate_across_int_float(self):
        manifest = _manifest([], config_schema={"groups": [{"key": "g", "items": [{
            "key": "mode", "type": "enum", "apply": "live",
            "options": [1, 1.0], "default": 1,
        }]}]})
        _, errors = appconfig.validate_config(manifest, {"mode": 1})
        self.assertTrue(any("duplicates" in error for error in errors), errors)

    def test_null_is_preserved_as_reset_marker(self):
        manifest = _manifest([], config_schema={"groups": [{"key": "g", "items": [{
            "key": "name", "type": "string", "apply": "live", "default": "x",
        }]}]})
        clean, errors = appconfig.validate_config(manifest, {"name": None})
        self.assertEqual(errors, [])
        self.assertEqual(clean, {"name": None})

    def test_password_array_and_object_are_structurally_typed(self):
        manifest = _manifest([], config_schema={"groups": [{"key": "g", "items": [
            {"key": "token", "type": "password", "apply": "restart", "default": ""},
            {"key": "labels", "type": "array", "apply": "live", "default": []},
            {"key": "metadata", "type": "object", "apply": "live", "default": {}},
        ]}]})
        clean, errors = appconfig.validate_config(manifest, {
            "token": "secret", "labels": ["a"], "metadata": {"k": 1},
        })
        self.assertEqual(errors, [])
        self.assertEqual(clean["labels"], ["a"])
        for key, opaque in (("labels", "a,b"), ("metadata", "k=1")):
            _, errors = appconfig.validate_config(manifest, {key: opaque})
            self.assertTrue(errors, (key, errors))

    def test_real_manifest_roundtrip(self):
        """The shipped face-analysis schema really produces ints."""
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))))),
            "apps", "face-analysis", "manifest.json")
        with open(path) as f:
            man = json.load(f)
        clean, errors = appconfig.validate_config(man, {"max_faces": 7,
                                                        "emotion_interval": 2})
        self.assertEqual(errors, [])
        self.assertIsInstance(clean["max_faces"], int)
        self.assertIsInstance(clean["emotion_interval"], int)


if __name__ == "__main__":
    unittest.main()

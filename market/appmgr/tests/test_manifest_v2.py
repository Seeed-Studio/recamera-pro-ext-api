"""Contract tests for the production manifest-v2 validator and release BOM."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sys

import pytest
from jsonschema import Draft202012Validator

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from appmgr import manifest as contract


def minimal_manifest(**updates):
    value = {
        "manifest_version": 2,
        "id": "demo-app",
        "name": "Demo App",
        "version": "1.2.3",
        "type": "self-hosted",
        "entry": "src/main.py",
        "release": {"sequence": 7, "channel": "stable"},
        "compatibility": {
            "platform_profile": contract.DEFAULT_PLATFORM_PROFILE,
            "arch": "aarch64",
            "python": "==3.11.*",
            "kit_api": ">=0.2,<0.3",
        },
        "python": {
            "runtime_profile": "system-cp311-rknn232",
            "isolation": "per-release",
            "wheels": [],
            "imports": [],
        },
        "artifacts": [],
        "config_schema": {"revision": 1, "groups": []},
        "resources": {"claims": [], "limits": {"memory_mb": 128}},
        "permissions": {
            "sdk": [],
            "filesystem": {"read": ["app"], "write": ["appdata", "tmp"]},
            "network": {"listen": [], "outbound": []},
        },
        "health": {
            "protocol": "kit-health-v1",
            "startup_timeout_sec": 30,
            "stabilization_sec": 2,
            "liveness_interval_sec": 10,
            "liveness_failures": 3,
            "restart": {
                "policy": "on-failure",
                "max_attempts": 3,
                "window_sec": 60,
                "backoff_sec": [1, 2, 5],
            },
        },
        "instances": {
            "max": 1,
            "config_scope": "app",
            "data_scope": "app",
            "endpoint_mode": "allocated",
        },
        "capabilities": [],
    }
    value.update(updates)
    return value


def records_for(value, extra=None):
    raw_manifest = contract.canonical_json(value)
    payload = {
        "manifest.json": {
            "sha256": hashlib.sha256(raw_manifest).hexdigest(),
            "size": len(raw_manifest),
        },
        value["entry"]: {
            "sha256": hashlib.sha256(b"# entry\n").hexdigest(),
            "size": len(b"# entry\n"),
        },
    }
    payload.update(extra or {})
    return payload


def test_minimal_v2_is_valid_and_input_is_not_mutated():
    value = minimal_manifest(**{"x-vendor-note": {"opaque": True}})
    before = copy.deepcopy(value)
    assert contract.validate_manifest(value) == 2

    assert value == before


def test_v1_is_explicitly_compatible_but_can_be_disabled():
    legacy = {"id": "legacy-app", "version": "old-version", "entry": "app.py"}
    assert contract.validate_manifest(legacy) == 1
    with pytest.raises(contract.ManifestValidationError, match="legacy v1"):
        contract.validate_manifest(legacy, allow_v1=False)


def test_v2_icon_declaration_binds_safe_bounded_package_file():
    icon_bytes = b"\x89PNG\r\n\x1a\n" + b"icon"
    value = minimal_manifest(icon={
        "path": "assets/card.png", "media_type": "image/png"})
    records = records_for(value, {
        "assets/card.png": {
            "sha256": hashlib.sha256(icon_bytes).hexdigest(),
            "size": len(icon_bytes),
        },
    })

    assert contract.validate_manifest(value) == 2
    schema_path = os.path.join(
        os.path.dirname(contract.__file__), "schema", "manifest-v2.schema.json")
    with open(schema_path, encoding="utf-8") as source:
        validator = Draft202012Validator(json.load(source))
    assert list(validator.iter_errors(value)) == []
    contract.validate_package_files(value, records)

    with pytest.raises(contract.ManifestValidationError, match="package is missing"):
        contract.validate_package_files(value, records_for(value))
    oversized = copy.deepcopy(records)
    oversized["assets/card.png"]["size"] = contract.MAX_ICON_BYTES + 1
    with pytest.raises(contract.ManifestValidationError, match="exceeds"):
        contract.validate_package_files(value, oversized)


@pytest.mark.parametrize("icon", [
    {"path": "../card.png", "media_type": "image/png"},
    {"path": "/card.png", "media_type": "image/png"},
    {"path": "assets\\card.png", "media_type": "image/png"},
    {"path": "assets//card.png", "media_type": "image/png"},
    {"path": "keys/icon.png", "media_type": "image/png"},
    {"path": ".ssh/icon.png", "media_type": "image/png"},
    {"path": "assets/card.jpg", "media_type": "image/png"},
    {"path": "assets/card.png", "media_type": "image/svg+xml"},
    {"path": "assets/card.PNG", "media_type": "image/png"},
    {"path": "assets/card.png"},
    {"path": "assets/card.png", "media_type": "image/png", "extra": True},
])
def test_v2_icon_python_and_json_schema_reject_same_unsafe_shapes(icon):
    value = minimal_manifest(icon=icon)
    with pytest.raises(contract.ManifestValidationError):
        contract.validate_manifest(value)

    schema_path = os.path.join(
        os.path.dirname(contract.__file__), "schema", "manifest-v2.schema.json")
    with open(schema_path, encoding="utf-8") as source:
        validator = Draft202012Validator(json.load(source))
    assert list(validator.iter_errors(value))


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (lambda m: m.pop("health"), "manifest: missing required"),
        (lambda m: m.update(manifest_version=3), "unsupported version"),
        (lambda m: m.update(id="Bad_ID"), "id: must match"),
        (lambda m: m.update(version="1.2"), "version: manifest v2 requires SemVer"),
        (lambda m: m.update(entry="../app.py"), "entry: must be normalized"),
        (lambda m: m.update(unknown_core=True), "manifest: unknown field"),
        (lambda m: m["compatibility"].update(python=">=3.11"),
         "compatibility.python: must be exactly"),
        (lambda m: m["instances"].update(max=0), "instances.max: must be a positive"),
        (lambda m: m["instances"].update(max=2), "instances.max: must be <= 1"),
        (lambda m: m.update(output={"port": 8124}), "output.port: fixed ports"),
    ],
)
def test_v2_rejects_invalid_core_contract(mutation, match):
    value = minimal_manifest()
    mutation(value)
    with pytest.raises(contract.ManifestValidationError, match=match):
        contract.validate_manifest(value)


def test_wheel_descriptor_binds_name_version_tag_digest_and_package_path():
    wheel_bytes = b"wheel"
    wheel = {
        "name": "demo-dep",
        "version": "1.0.0",
        "filename": "demo_dep-1.0.0-py3-none-any.whl",
        "file": "wheels/demo_dep-1.0.0-py3-none-any.whl",
        "sha256": hashlib.sha256(wheel_bytes).hexdigest(),
        "size": len(wheel_bytes),
        "tags": ["py3-none-any"],
        "source": "bundled",
    }
    value = minimal_manifest()
    value["python"]["wheels"] = [wheel]
    path = wheel["file"]
    records = records_for(value, {
        path: {"sha256": wheel["sha256"], "size": wheel["size"]},
    })
    contract.validate_package_files(value, records)

    bad = copy.deepcopy(value)
    bad["python"]["wheels"][0]["filename"] = "other-1.0.0-py3-none-any.whl"
    with pytest.raises(contract.ManifestValidationError, match="distribution does not match"):
        contract.validate_manifest(bad)

    records[path]["size"] += 1
    with pytest.raises(contract.ManifestValidationError, match="digest/size"):
        contract.validate_package_files(value, records)


def test_aarch64_py3_none_platform_wheel_is_supported():
    value = minimal_manifest()
    value["python"]["wheels"] = [{
        "name": "sherpa-onnx-core",
        "version": "1.13.5",
        "filename": "sherpa_onnx_core-1.13.5-py3-none-manylinux2014_aarch64.whl",
        "file": "wheels/sherpa_onnx_core-1.13.5-py3-none-manylinux2014_aarch64.whl",
        "sha256": "a" * 64,
        "size": 123,
        "tags": ["py3-none-manylinux2014_aarch64"],
        "source": "bundled",
    }]
    assert contract.validate_manifest(value) == 2


def test_config_keys_and_resource_profile_references_are_strict():
    value = minimal_manifest()
    value["config_schema"]["groups"] = [{
        "key": "runtime",
        "title": "Runtime",
        "items": [{
            "key": "backend", "type": "enum", "apply": "reschedule",
            "options": ["cpu", "rk"], "default": "rk",
        }],
    }]
    value["resources"] = {"profiles": [{
        "when": {"backend": "rk"},
        "claims": [{
            "name": "npu.rknn", "mode": "brokered", "required": True,
        }],
    }]}
    value["permissions"]["sdk"] = ["npu.infer"]
    assert contract.validate_manifest(value) == 2
    value["config_schema"]["groups"][0]["items"][0]["apply"] = "live"
    with pytest.raises(
            contract.ManifestValidationError,
            match="profile selector config must restart or reschedule"):
        contract.validate_manifest(value)
    value["config_schema"]["groups"][0]["items"][0]["apply"] = "reschedule"
    value["resources"]["profiles"][0]["when"] = {"missing": True}
    with pytest.raises(contract.ManifestValidationError, match="unknown config key"):
        contract.validate_manifest(value)


def test_config_schema_supports_ranges_and_labelled_typed_dropdowns():
    value = minimal_manifest()
    value["config_schema"]["groups"] = [{
        "key": "runtime", "title": "Runtime", "items": [
            {"key": "threshold", "type": "number", "apply": "live",
             "min": 0.0, "max": 1.0, "step": 0.05, "default": 0.35},
            {"key": "backend", "type": "select", "apply": "restart",
             "options": [
                 {"value": 1, "label": "Fast", "label_zh": "快速"},
                 {"value": 2, "label": "Accurate", "label_zh": "精确"},
             ], "default": 1},
        ],
    }]
    assert contract.validate_manifest(value) == 2

    numeric_wire_default = copy.deepcopy(value)
    numeric_wire_default["config_schema"]["groups"][0]["items"][1]["default"] = 1.0
    assert contract.validate_manifest(numeric_wire_default) == 2

    bad_step = copy.deepcopy(value)
    bad_step["config_schema"]["groups"][0]["items"][0]["step"] = 0
    with pytest.raises(contract.ManifestValidationError, match="must be positive"):
        contract.validate_manifest(bad_step)

    bad_typed_default = copy.deepcopy(value)
    bad_typed_default["config_schema"]["groups"][0]["items"][1]["default"] = "1"
    with pytest.raises(contract.ManifestValidationError,
                       match="typed option values"):
        contract.validate_manifest(bad_typed_default)

    duplicate_number = copy.deepcopy(value)
    duplicate_number["config_schema"]["groups"][0]["items"][1]["options"] = [1, 1.0]
    with pytest.raises(contract.ManifestValidationError,
                       match="duplicates an earlier option value"):
        contract.validate_manifest(duplicate_number)


def test_config_schema_password_array_and_object_defaults_are_typed():
    value = minimal_manifest()
    value["config_schema"]["groups"] = [{
        "key": "advanced", "title": "Advanced", "items": [
            {"key": "token", "type": "password", "apply": "restart",
             "default": ""},
            {"key": "labels", "type": "array", "apply": "live",
             "default": ["person"]},
            {"key": "metadata", "type": "object", "apply": "live",
             "default": {"enabled": True}},
        ],
    }]
    assert contract.validate_manifest(value) == 2
    for index, opaque in ((1, "person"), (2, "enabled=true")):
        bad = copy.deepcopy(value)
        bad["config_schema"]["groups"][0]["items"][index]["default"] = opaque
        with pytest.raises(contract.ManifestValidationError, match="must match type"):
            contract.validate_manifest(bad)


@pytest.mark.parametrize("config_type,opaque", [
    ("zone", "0,0;1,0;1,1"),
    ("line", "0,0 -> 1,1"),
    ("field_mapping", "detection.count -> count"),
    ("output_filters", "all"),
])
def test_config_schema_rejects_opaque_complex_defaults(config_type, opaque):
    value = minimal_manifest()
    value["config_schema"]["groups"] = [{
        "key": "runtime", "title": "Runtime", "items": [{
            "key": "complex_value", "type": config_type, "apply": "live",
            "default": opaque,
        }],
    }]
    with pytest.raises(contract.ManifestValidationError, match="must be"):
        contract.validate_manifest(value)


def test_resource_claim_requires_permission_and_scheduled_is_npu_only():
    value = minimal_manifest()
    value["resources"] = {"claims": [{
        "name": "camera.frames", "mode": "shared", "required": True,
    }]}
    with pytest.raises(contract.ManifestValidationError, match="requires permission 'frame.read'"):
        contract.validate_manifest(value)
    value["permissions"]["sdk"] = ["frame.read"]
    assert contract.validate_manifest(value) == 2
    value["resources"]["claims"][0]["mode"] = "scheduled"
    with pytest.raises(contract.ManifestValidationError, match="reserved for npu.rknn"):
        contract.validate_manifest(value)


def test_typed_output_and_render_contract_is_strict_but_legacy_remains_compatible():
    value = minimal_manifest(
        capabilities=["output"],
        output={
            "contract_version": 2,
            "sink": "ws",
            "schema": "results[]{box,score,label}",
            "default_channel": ["ws", "mqtt"],
            "default_mode": "raw",
            "fields": [
                {
                    "name": "box", "from": "results[].box",
                    "type": "bbox<float>[4]", "coord": "pixel_xyxy",
                    "description": "Original-frame box",
                },
                {
                    "name": "label", "from": "results[].label",
                    "type": "string", "description": "Class label",
                },
            ],
            "default_mapping": [{
                "source": "results | length", "target": "count",
                "topic": "recamera/{{ app }}/count", "task": "detection",
            }],
        },
        render={
            "schema_version": 1,
            "boxes": {"label": "label", "color_by": "label", "line_width": 2},
            "stream_osd": {"supported": ["boxes"], "default": False},
        },
    )
    assert contract.validate_manifest(value) == 2

    osd_only = copy.deepcopy(value)
    del osd_only["render"]["boxes"]
    assert contract.validate_manifest(osd_only) == 2

    same_space_alias = copy.deepcopy(osd_only)
    same_space_alias["output"]["fields"].append({
        "name": "box_copy", "from": "results[].box",
        "type": "bbox<float>[4]", "coord": "pixel_xyxy",
        "description": "Equivalent box alias",
    })
    assert contract.validate_manifest(same_space_alias) == 2

    conflicting_alias = copy.deepcopy(same_space_alias)
    conflicting_alias["output"]["fields"][-1]["coord"] = \
        "normalized_xyxy"
    with pytest.raises(contract.ManifestValidationError,
                       match="one consistent pixel_xyxy or normalized_xyxy"):
        contract.validate_manifest(conflicting_alias)

    derived_only = copy.deepcopy(osd_only)
    derived_only["output"]["fields"][0]["derived"] = True
    with pytest.raises(contract.ManifestValidationError,
                       match="direct field named box"):
        contract.validate_manifest(derived_only)

    legacy = copy.deepcopy(value)
    legacy["output"] = {"sink": "ws", "vendor_extension": {"old": True}}
    legacy["render"] = {"vendor_shape": {"old": True}}
    assert contract.validate_manifest(legacy) == 2

    bad = copy.deepcopy(value)
    bad["output"]["fields"][0]["coord"] = "guessed"
    with pytest.raises(contract.ManifestValidationError,
                       match="unsupported coordinate space"):
        contract.validate_manifest(bad)

    bad = copy.deepcopy(value)
    del bad["output"]["fields"][0]["coord"]
    with pytest.raises(contract.ManifestValidationError,
                       match="is required for bbox"):
        contract.validate_manifest(bad)

    bad = copy.deepcopy(value)
    bad["render"]["stream_osd"]["default"] = True
    with pytest.raises(contract.ManifestValidationError,
                       match="must default to false"):
        contract.validate_manifest(bad)

    bad = copy.deepcopy(value)
    bad["render"]["boxes"]["label"] = "undeclared"
    with pytest.raises(contract.ManifestValidationError,
                       match="must reference a declared results"):
        contract.validate_manifest(bad)

    bad = copy.deepcopy(value)
    bad["render"]["events"] = {"fall": {"as": "toast"}}
    with pytest.raises(contract.ManifestValidationError,
                       match="same event_kind"):
        contract.validate_manifest(bad)


def test_geometry_output_and_render_policy_are_closed_and_bounded():
    value = minimal_manifest(
        capabilities=["output"],
        output={
            "contract_version": 2, "sink": "ws",
            "schema": "geometry[]{type,points,style}",
            "default_channel": ["ws"], "default_mode": "raw",
            "fields": [{
                "name": "geometry", "from": "geometry[]", "type": "geometry[]",
                "coord": "normalized_points",
                "description": "Canonical drawing primitives",
            }],
            "default_mapping": [],
        },
        render={
            "schema_version": 1,
            "geometry": {
                "types": ["point", "line", "polyline", "polygon"],
                "max_items": 64, "max_points": 128,
                "style": {"color": "#00ff00", "line_width": 2,
                          "point_radius": 4, "fill": False,
                          "fill_color": "#00ff0080", "opacity": 0.8},
            },
        },
    )
    assert contract.validate_manifest(value) == 2

    for mutate, match in [
        (lambda m: m["output"]["fields"][0].update(coord="pixel_xyxy"),
         "geometry.*requires pixel_points"),
        (lambda m: m["output"]["fields"][0].update(type="object[]"),
         "requires type geometry"),
        (lambda m: m["output"]["fields"][0].update(derived=True),
         "cannot be derived"),
        (lambda m: m["render"]["geometry"].update(types=["circle"]),
         "point, line, polyline or polygon"),
        (lambda m: m["render"]["geometry"]["style"].update(color="red"),
         "#RRGGBB"),
        (lambda m: m["render"]["geometry"].update(max_items=257),
         "must be <= 256"),
        (lambda m: m["render"]["geometry"].update(types=["line"], max_points=1),
         "must be at least 2"),
        (lambda m: m["render"]["geometry"].update(types=["polygon"], max_points=2),
         "must be at least 3"),
    ]:
        bad = copy.deepcopy(value)
        mutate(bad)
        with pytest.raises(contract.ManifestValidationError, match=match):
            contract.validate_manifest(bad)

    missing = copy.deepcopy(value)
    missing["output"]["fields"] = []
    with pytest.raises(contract.ManifestValidationError,
                       match="requires exactly one declared"):
        contract.validate_manifest(missing)

    missing_output = copy.deepcopy(value)
    del missing_output["output"]
    with pytest.raises(contract.ManifestValidationError,
                       match="requires output.contract_version=2"):
        contract.validate_manifest(missing_output)

    loose_output = copy.deepcopy(value)
    loose_output["output"] = {"sink": "ws"}
    with pytest.raises(contract.ManifestValidationError,
                       match="requires output.contract_version=2"):
        contract.validate_manifest(loose_output)

    missing_render = copy.deepcopy(value)
    del missing_render["render"]
    with pytest.raises(contract.ManifestValidationError,
                       match="geometry.*requires render.schema_version=1"):
        contract.validate_manifest(missing_render)

    loose_render = copy.deepcopy(value)
    loose_render["render"] = {"geometry": {"types": ["point"]}}
    with pytest.raises(contract.ManifestValidationError,
                       match="geometry.*requires render.schema_version=1"):
        contract.validate_manifest(loose_render)


def test_release_lock_and_bom_are_deterministic_and_exact():
    value = minimal_manifest()
    records = records_for(value, {
        "data/labels.txt": {
            "sha256": hashlib.sha256(b"cat\ndog\n").hexdigest(),
            "size": len(b"cat\ndog\n"),
        },
    })
    lock1, bom1 = contract.make_release_metadata(value, records)
    lock2, bom2 = contract.make_release_metadata(value, dict(reversed(list(records.items()))))
    assert (lock1, bom1) == (lock2, bom2)
    assert contract.verify_release_metadata(value, lock1, bom1, records) == lock1
    assert lock1["release_id"].startswith("1.2.3-")

    changed = copy.deepcopy(records)
    changed["data/labels.txt"]["size"] += 1
    with pytest.raises(contract.ManifestValidationError, match="does not match payload"):
        contract.verify_release_metadata(value, lock1, bom1, changed)


def test_schema_document_is_valid_json_and_tracks_version():
    schema_path = os.path.join(
        os.path.dirname(contract.__file__), "schema", "manifest-v2.schema.json")
    with open(schema_path, encoding="utf-8") as source:
        schema = json.load(source)
    assert schema["properties"]["manifest_version"]["const"] == 2
    documented_types = schema["$defs"]["configGroup"]["properties"][
        "items"]["items"]["properties"]["type"]["enum"]
    assert set(documented_types) == contract._CONFIG_TYPES


def test_schema_geometry_constraints_match_runtime_validator():
    """Keep the public packaging schema equivalent to runtime admission."""
    schema_path = os.path.join(
        os.path.dirname(contract.__file__), "schema", "manifest-v2.schema.json")
    with open(schema_path, encoding="utf-8") as source:
        schema = json.load(source)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)

    value = minimal_manifest(
        capabilities=["output"],
        output={
            "contract_version": 2, "sink": "ws",
            "schema": "geometry[]{type,points,style}",
            "default_channel": ["ws"], "default_mode": "raw",
            "fields": [{
                "name": "geometry", "from": "geometry[]",
                "type": "geometry[]", "coord": "normalized_points",
                "description": "Canonical drawing primitives",
            }],
            "default_mapping": [],
        },
        render={
            "schema_version": 1,
            "geometry": {
                "types": ["point", "line", "polyline", "polygon"],
                "max_items": 64, "max_points": 128,
                "style": {"color": "#00ff00", "line_width": 2},
            },
        },
    )
    assert contract.validate_manifest(value) == 2
    assert list(validator.iter_errors(value)) == []

    invalid_mutations = [
        lambda m: m.pop("output"),
        lambda m: m.update(output={"sink": "ws"}),
        lambda m: m["output"]["fields"].append(
            copy.deepcopy(m["output"]["fields"][0])),
        lambda m: m.pop("render"),
        lambda m: m.update(render={"geometry": {"types": ["point"]}}),
        lambda m: m["render"]["geometry"].update(
            types=["line"], max_points=1),
        lambda m: m["render"]["geometry"].update(
            types=["polygon"], max_points=2),
    ]
    for mutate in invalid_mutations:
        bad = copy.deepcopy(value)
        mutate(bad)
        with pytest.raises(contract.ManifestValidationError):
            contract.validate_manifest(bad)
        assert list(validator.iter_errors(bad)), bad

    extended = copy.deepcopy(value)
    extended["render"]["geometry"]["x-vendor-mode"] = "diagnostic"
    extended["render"]["geometry"]["style"]["x-vendor-color-space"] = "srgb"
    assert contract.validate_manifest(extended) == 2
    assert list(validator.iter_errors(extended)) == []


def test_schema_stream_osd_constraints_match_runtime_validator():
    schema_path = os.path.join(
        os.path.dirname(contract.__file__), "schema", "manifest-v2.schema.json")
    with open(schema_path, encoding="utf-8") as source:
        schema = json.load(source)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)

    value = minimal_manifest(
        capabilities=["output"],
        output={
            "contract_version": 2, "sink": "ws",
            "schema": "results[]{box}",
            "default_channel": ["ws"], "default_mode": "raw",
            "fields": [{
                "name": "box", "from": "results[].box",
                "type": "bbox<float>[4]", "coord": "pixel_xyxy",
                "description": "OSD box",
            }],
            "default_mapping": [],
        },
        render={
            "schema_version": 1,
            "stream_osd": {"supported": ["boxes"], "default": False},
        },
    )

    same_space_alias = copy.deepcopy(value)
    same_space_alias["output"]["fields"].append({
        "name": "box_copy", "from": "results[].box",
        "type": "bbox<float>[4]", "coord": "pixel_xyxy",
        "description": "Equivalent alias",
    })
    for good in (value, same_space_alias):
        assert contract.validate_manifest(good) == 2
        assert list(validator.iter_errors(good)) == []

    invalid = []
    missing_output = copy.deepcopy(value)
    missing_output.pop("output")
    invalid.append(missing_output)
    derived_only = copy.deepcopy(value)
    derived_only["output"]["fields"][0]["derived"] = True
    invalid.append(derived_only)
    conflicting = copy.deepcopy(same_space_alias)
    conflicting["output"]["fields"][1]["coord"] = "normalized_xyxy"
    invalid.append(conflicting)
    wrong_path = copy.deepcopy(value)
    wrong_path["output"]["fields"][0]["from"] = "results[].bbox"
    invalid.append(wrong_path)
    boolean_render_version = copy.deepcopy(value)
    boolean_render_version["render"]["schema_version"] = True
    invalid.append(boolean_render_version)

    for bad in invalid:
        with pytest.raises(contract.ManifestValidationError):
            contract.validate_manifest(bad)
        assert list(validator.iter_errors(bad)), bad

"""Contract tests for the production manifest-v2 validator and release BOM."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sys

import pytest

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
    value["resources"]["profiles"][0]["when"] = {"missing": True}
    with pytest.raises(contract.ManifestValidationError, match="unknown config key"):
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

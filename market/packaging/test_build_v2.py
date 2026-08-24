"""Manifest-v2 build tests: shared validation plus deterministic lock/BOM."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import tarfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

import pytest

from appmgr import manifest as contract


_SPEC = importlib.util.spec_from_file_location("packaging_build_v2", os.path.join(_HERE, "build.py"))
build_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build_mod)


def manifest_v2(**updates):
    value = {
        "manifest_version": 2,
        "id": "demo-app",
        "name": "Demo App",
        "version": "1.0.0",
        "type": "self-hosted",
        "entry": "src/main.py",
        "release": {"sequence": 1, "channel": "stable"},
        "compatibility": {
            "platform_profile": contract.DEFAULT_PLATFORM_PROFILE,
            "arch": "aarch64", "python": "==3.11.*",
        },
        "python": {
            "runtime_profile": "system-cp311-rknn232", "isolation": "per-release",
            "wheels": [], "imports": [],
        },
        "artifacts": [],
        "config_schema": {"revision": 1, "groups": []},
        "resources": {"claims": []},
        "permissions": {
            "sdk": [],
            "filesystem": {"read": ["app"], "write": ["appdata", "tmp"]},
            "network": {"listen": [], "outbound": []},
        },
        "health": {
            "protocol": "kit-health-v1", "startup_timeout_sec": 30,
            "stabilization_sec": 0, "liveness_interval_sec": 10,
            "liveness_failures": 3,
            "restart": {"policy": "on-failure", "max_attempts": 3,
                        "window_sec": 60, "backoff_sec": [1, 2]},
        },
        "instances": {"max": 1, "config_scope": "app", "data_scope": "app",
                      "endpoint_mode": "allocated"},
        "capabilities": [],
    }
    value.update(updates)
    return value


def make_source(root, value, files=None):
    os.makedirs(root)
    for relative, data in {"src/main.py": b"# entry\n", **(files or {})}.items():
        path = os.path.join(root, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as output:
            output.write(data)
    with open(os.path.join(root, "manifest.json"), "w", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False)
    return root


def tar_payload(package):
    with tarfile.open(package, "r:gz") as archive:
        members = archive.getmembers()
        contents = {
            member.name: archive.extractfile(member).read()
            for member in members if member.isfile()
        }
    return members, contents


def test_v2_build_embeds_one_canonical_release_lock_and_bom(tmp_path):
    source = make_source(str(tmp_path / "app"), manifest_v2(), {
        "data/labels.txt": b"cat\ndog\n",
    })
    package = build_mod.build(source, str(tmp_path / "dist"))
    members, contents = tar_payload(package)
    names = [member.name for member in members]
    assert names.count(contract.RELEASE_LOCK_PATH) == 1
    assert names.count(contract.BOM_PATH) == 1
    assert "src/main.py" in names and "app.py" not in names

    records = {
        name: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        for name, data in contents.items() if name not in contract.RESERVED_PACKAGE_PATHS
    }
    value = json.loads(contents["manifest.json"])
    lock = json.loads(contents[contract.RELEASE_LOCK_PATH])
    assert contract.verify_release_metadata(
        value, lock, contents[contract.BOM_PATH], records) == lock


def test_invalid_v2_fails_before_output_directory_is_created(tmp_path):
    value = manifest_v2()
    value["instances"]["max"] = 0
    source = make_source(str(tmp_path / "app"), value)
    output = tmp_path / "does-not-exist"
    with pytest.raises(SystemExit, match="invalid manifest"):
        build_mod.build(source, str(output))
    assert not output.exists()


def test_excluding_declared_entry_is_rejected(tmp_path):
    value = manifest_v2(package={"exclude": ["src/main.py"]})
    source = make_source(str(tmp_path / "app"), value)
    with pytest.raises(SystemExit, match="required package file is missing"):
        build_mod.build(source, str(tmp_path / "dist"))


def test_bundled_wheel_digest_and_size_are_checked(tmp_path):
    wheel_data = b"not-even-a-wheel"
    wheel = {
        "name": "demo-dep", "version": "1.0.0",
        "filename": "demo_dep-1.0.0-py3-none-any.whl",
        "file": "wheels/demo_dep-1.0.0-py3-none-any.whl",
        "sha256": "0" * 64, "size": len(wheel_data),
        "tags": ["py3-none-any"], "source": "bundled",
    }
    value = manifest_v2()
    value["python"]["wheels"] = [wheel]
    source = make_source(str(tmp_path / "app"), value, {wheel["file"]: wheel_data})
    with pytest.raises(SystemExit, match="digest/size"):
        build_mod.build(source, str(tmp_path / "dist"))


def test_source_symlink_is_never_followed_into_package(tmp_path):
    source = make_source(str(tmp_path / "app"), manifest_v2())
    os.symlink("src/main.py", os.path.join(source, "alias.py"))
    with pytest.raises(SystemExit, match=r"regular file \(no links\)"):
        build_mod.build(source, str(tmp_path / "dist"))


def test_v2_build_is_byte_for_byte_deterministic(tmp_path):
    source = make_source(str(tmp_path / "app"), manifest_v2())
    one = build_mod.build(source, str(tmp_path / "one"))
    two = build_mod.build(source, str(tmp_path / "two"))
    with open(one, "rb") as first, open(two, "rb") as second:
        assert first.read() == second.read()


def external_payload_manifest(wheel_data, model_data):
    value = manifest_v2()
    wheel_path = "wheels/demo_dep-1.0.0-py3-none-any.whl"
    model_path = "models/demo.rknn"
    value["python"]["wheels"] = [{
        "name": "demo-dep", "version": "1.0.0",
        "filename": os.path.basename(wheel_path), "file": wheel_path,
        "sha256": hashlib.sha256(wheel_data).hexdigest(), "size": len(wheel_data),
        "tags": ["py3-none-any"], "source": "bundled",
    }]
    value["artifacts"] = [{
        "id": "demo-model", "kind": "rknn", "source": "bundled",
        "file": model_path, "mount": model_path,
        "sha256": hashlib.sha256(model_data).hexdigest(), "size": len(model_data),
        "required": True, "share_scope": "content",
    }]
    return value, wheel_path, model_path


def write_bytes(root, relative, data):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_repeatable_payload_roots_fill_only_declared_missing_assets(tmp_path):
    wheel_data = b"controlled wheel bytes"
    model_data = b"controlled model bytes"
    value, wheel_path, model_path = external_payload_manifest(wheel_data, model_data)
    source = make_source(str(tmp_path / "app"), value)
    wheel_root = tmp_path / "wheel-assets"
    model_root = tmp_path / "model-assets"
    write_bytes(wheel_root, wheel_path, wheel_data)
    write_bytes(model_root, model_path, model_data)
    write_bytes(wheel_root, "wheels/not-declared.whl", b"must not ship")
    write_bytes(model_root, "models/not-declared.rknn", b"must not ship")

    package = build_mod.build(
        source, str(tmp_path / "dist"),
        payload_roots=[str(wheel_root), str(model_root)])
    _, contents = tar_payload(package)
    assert contents[wheel_path] == wheel_data
    assert contents[model_path] == model_data
    assert "wheels/not-declared.whl" not in contents
    assert "models/not-declared.rknn" not in contents


def test_payload_root_never_overrides_existing_source_file(tmp_path):
    good_wheel = b"good wheel"
    model_data = b"model"
    value, wheel_path, model_path = external_payload_manifest(good_wheel, model_data)
    source = make_source(str(tmp_path / "app"), value, {wheel_path: b"bad wheel"})
    assets = tmp_path / "assets"
    write_bytes(assets, wheel_path, good_wheel)
    write_bytes(assets, model_path, model_data)
    output = tmp_path / "dist"
    with pytest.raises(SystemExit, match="digest/size"):
        build_mod.build(source, str(output), payload_roots=[str(assets)])
    assert not output.exists()


def test_payload_root_digest_mismatch_fails_before_output(tmp_path):
    wheel_data = b"wheel"
    model_data = b"model"
    value, wheel_path, model_path = external_payload_manifest(wheel_data, model_data)
    source = make_source(str(tmp_path / "app"), value)
    assets = tmp_path / "assets"
    write_bytes(assets, wheel_path, wheel_data + b"tamper")
    write_bytes(assets, model_path, model_data)
    output = tmp_path / "dist"
    with pytest.raises(SystemExit, match="digest/size"):
        build_mod.build(source, str(output), payload_roots=[str(assets)])
    assert not output.exists()


def test_payload_root_symlink_is_rejected(tmp_path):
    wheel_data = b"wheel"
    model_data = b"model"
    value, wheel_path, model_path = external_payload_manifest(wheel_data, model_data)
    source = make_source(str(tmp_path / "app"), value)
    assets = tmp_path / "assets"
    target = write_bytes(tmp_path, "outside.whl", wheel_data)
    link = assets / wheel_path
    link.parent.mkdir(parents=True)
    os.symlink(target, link)
    write_bytes(assets, model_path, model_data)
    with pytest.raises(SystemExit, match="may not use symlinks"):
        build_mod.build(source, str(tmp_path / "dist"), payload_roots=[str(assets)])


def test_builder_refuses_embedded_trust_key_material(tmp_path):
    source = make_source(
        str(tmp_path / "app"), manifest_v2(),
        {"keys/release_pub.pem": b"not package-owned trust"})
    output = tmp_path / "dist"
    with pytest.raises(SystemExit, match="must not contain signing keys"):
        build_mod.build(source, str(output))
    assert not output.exists()

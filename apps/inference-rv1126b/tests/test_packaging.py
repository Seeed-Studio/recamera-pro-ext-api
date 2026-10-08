"""App bundle checks with mock wheels and the real SDK validators.

Set RECAMERA_SDK_ROOT to the ext-api checkout for the integration cases.
These cases do not import native wheels, call devices, or create signatures.
"""

import hashlib
import importlib.util
import io
import json
import os
import tarfile
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("INFERENCE_ENGINE_ROOT"),
    reason="Set INFERENCE_ENGINE_ROOT to the pinned engine checkout",
)


@pytest.fixture
def builder():
    path = Path(__file__).resolve().parents[1] / "build_app.py"
    spec = importlib.util.spec_from_file_location("edge_app_builder_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original = module.build_app
    module.build_app = lambda *a, **kw: original(
        *a, engine_root=os.environ["INFERENCE_ENGINE_ROOT"], **kw)
    return module


@pytest.fixture
def sdk_root():
    return Path(__file__).resolve().parents[3]


def make_wheel(directory, name="inference_rv1126b", *, extra=None, requirement=None):
    path = directory / f"{name}-0.1.0-py3-none-any.whl"
    info = f"{name}-0.1.0.dist-info"
    metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: 0.1.0\n"
    if requirement:
        metadata += f"Requires-Dist: {requirement}\n"
    files = {
        f"{info}/METADATA": metadata,
        f"{info}/WHEEL": "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{info}/RECORD": "",
        "inference_edge_probe.py": "# mocked install import probe\n",
        **(extra or {}),
    }
    with zipfile.ZipFile(path, "w") as archive:
        for member, value in files.items():
            archive.writestr(member, value)
    return path


@pytest.fixture
def payload(tmp_path):
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    make_wheel(wheels)
    models = tmp_path / "models"
    directory = models / "detector/1"
    directory.mkdir(parents=True)
    blob = b"mock-rknn"
    (directory / "model.rknn").write_bytes(blob)
    metadata = {
        "schema_version": 1,
        "model_id": "detector/1",
        "platform": "rv1126b",
        "format": "rknn",
        "task": "object-detection",
        "model_file": "model.rknn",
        "sha256": hashlib.sha256(blob).hexdigest(),
        "labels": ["person"],
        "input": {
            "name": "images",
            "shape": [1, 32, 32, 3],
            "layout": "NHWC",
            "dtype": "uint8",
            "color_format": "RGB",
            "normalization": "baked",
        },
        "outputs": [
            {"name": "output", "shape": [1, 5, 10], "layout": "BCN", "dtype": "float32"}
        ],
        "postprocess": {
            "kind": "yolo-decoded",
            "box_format": "xywh",
            "scores": "probabilities",
        },
    }
    (directory / "model.json").write_text(json.dumps(metadata))
    return wheels, models, tmp_path / "out"


def test_wheel_record_hash_and_metadata(builder, tmp_path):
    path = make_wheel(tmp_path)
    record = builder.wheel_record(path)
    assert record["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert record["tags"] == ["py3-none-any"]
    assert record["name"] == "inference_rv1126b"
    assert record["size"] == path.stat().st_size


def test_wheel_record_refuses_symlink(builder, tmp_path):
    path = make_wheel(tmp_path)
    linked = tmp_path / "linked.whl"
    linked.symlink_to(path)
    with pytest.raises(ValueError, match="regular"):
        builder.wheel_record(linked)


def test_sdk_bundle_contains_matching_artifact_bom_and_permissions(
    builder, sdk_root, payload
):
    wheels, models, out = payload
    record = builder.build_app(sdk_root, wheels, models, out)
    package = out / record["file"]
    with tarfile.open(package) as archive:
        manifest = json.load(archive.extractfile("manifest.json"))
        lock = json.load(archive.extractfile("release.lock.json"))
        assert "files.sha256" in archive.getnames()
        artifact = manifest["artifacts"][0]
        blob = archive.extractfile(artifact["file"]).read()
        assert artifact["sha256"] == hashlib.sha256(blob).hexdigest()
        assert artifact["size"] == len(blob)
        assert manifest["models"][0]["file"] == artifact["file"]
        assert manifest["models"][0]["input"] == [1, 32, 32, 3]
        assert "npu.infer" in manifest["permissions"]["sdk"]
        assert lock["python"] == manifest["python"]
    assert record["signed"] is False
    assert record["wheel_count"] == 1
    assert record["sha256"] == builder.digest(package)
    assert not list(out.glob("*.sig"))
    repeated = builder.build_app(sdk_root, wheels, models, out)
    assert repeated["sha256"] == record["sha256"]


def test_missing_offline_dependency_fails_before_output(builder, sdk_root, payload):
    wheels, models, out = payload
    make_wheel(wheels, requirement="not-bundled>=1")
    with pytest.raises(ValueError, match="Unresolved device dependency"):
        builder.build_app(sdk_root, wheels, models, out)
    assert not out.exists()


def test_final_archive_rejects_corrupted_payload(builder, sdk_root, payload):
    wheels, models, out = payload
    record = builder.build_app(sdk_root, wheels, models, out)
    original = out / record["file"]
    tampered = out / "tampered.tar.gz"
    with tarfile.open(original) as source, tarfile.open(tampered, "w:gz") as target:
        manifest = json.load(source.extractfile("manifest.json"))
        for member in source.getmembers():
            content = source.extractfile(member).read()
            if member.name == "app.py":
                content += b"\n# altered after packaging\n"
            member.size = len(content)
            target.addfile(member, io.BytesIO(content))
    with pytest.raises(ValueError, match="does not match payload"):
        builder.verify_archive(builder.load_sdk(sdk_root), tampered, manifest)


def test_sdk_rejects_unsupported_data_layout_before_output(builder, sdk_root, payload):
    wheels, models, out = payload
    make_wheel(
        wheels, extra={"inference_rv1126b-0.1.0.data/data/something.txt": "unused"}
    )
    with pytest.raises((ValueError, RuntimeError), match="data"):
        builder.build_app(sdk_root, wheels, models, out)
    assert not out.exists()


def test_sdk_wheel_path_collision_is_detected(builder, sdk_root, payload):
    wheels, models, out = payload
    make_wheel(wheels, name="second")
    with pytest.raises(RuntimeError, match="collision"):
        builder.build_app(sdk_root, wheels, models, out)
    assert not out.exists()


def test_model_hash_failure_preserves_previous_output(builder, sdk_root, payload):
    wheels, models, out = payload
    out.mkdir()
    previous = out / "inference-rv1126b-0.1.0-arm64.tar.gz"
    previous.write_bytes(b"previous-package")
    (models / "detector/1/model.rknn").write_bytes(b"changed")
    with pytest.raises(Exception, match="SHA-256"):
        builder.build_app(sdk_root, wheels, models, out)
    assert previous.read_bytes() == b"previous-package"


def test_compressed_size_failure_does_not_publish(
    builder, sdk_root, payload, monkeypatch
):
    wheels, models, out = payload
    sdk = builder.load_sdk(sdk_root)
    monkeypatch.setattr(sdk.paths, "MAX_PKG_BYTES", 1)
    with pytest.raises(ValueError, match="compressed limit"):
        builder.build_app(sdk_root, wheels, models, out)
    assert not out.exists()

"""Workflow release regression tests: standard tools, no engine checkout required."""
import hashlib
import io
import json
import shutil
import tarfile
import zipfile
from pathlib import Path

import pytest

from market.packaging.test_fetch_assets import archive_bytes, fetch, load, pin, serve


build = load("workflow_standard_build", "build.py")
publish = load("workflow_standard_publish", "publish_app.py")
APP = Path(__file__).resolve().parents[2] / "apps/inference-rv1126b"


def wheel_bytes(name, version, module):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as wheel:
        wheel.writestr(f"{name}-{version}.dist-info/METADATA",
                       f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
        wheel.writestr(module + ".py", "raise RuntimeError('must never execute in packaging')\n")
    return stream.getvalue()


def test_repository_manifest_is_a_complete_standard_release():
    manifest = json.loads((APP / "manifest.json").read_text())
    lock = json.loads((APP / "engine-source.lock.json").read_text())
    wheels = manifest["python"]["wheels"]
    engine = next(w for w in wheels if w["name"].replace("_", "-") == "inference-rv1126b")
    assert engine["version"] == lock["wheel_version"]
    assert {"fastapi", "uvicorn", "pydantic"} <= {w["name"].lower() for w in wheels}
    assert manifest["python"]["imports"] == ["inference_edge_probe"]
    artifacts = {a["file"]: a for a in manifest["artifacts"]}
    assert manifest["models"]
    for model in manifest["models"]:
        assert artifacts[model["file"]]["kind"] == "rknn"
        assert str(Path(model["file"]).with_name("model.json")) in artifacts
    provenance = artifacts["engine-source.json"]
    assert provenance["sha256"] == hashlib.sha256((APP / "engine-source.lock.json").read_bytes()).hexdigest()
    assert manifest["release"]["sequence"] > 15
    assert manifest["version"] != "0.3.10"
    assert manifest["x-asset-bundle"]["size"] > sum(w["size"] for w in wheels)


@pytest.fixture
def staged(tmp_path, monkeypatch):
    root = tmp_path / "app"
    root.mkdir()
    # Current source stays authoritative; the archive deliberately has an old entry.
    for name in ("app.py", "icon.jpg", "build.py", "build_app.py", "engine-source.lock.json"):
        shutil.copyfile(APP / name, root / name)
    (root / "tests").mkdir()
    (root / "tests/test_private.py").write_text("not a runtime file")
    manifest = json.loads((APP / "manifest.json").read_text())
    files = {"app.py": b"old source must not overwrite current source",
             "manifest.json": b"old manifest must not overwrite current manifest"}
    for index, wheel in enumerate(manifest["python"]["wheels"]):
        module = "inference_edge_probe" if wheel["name"].replace("_", "-") == "inference-rv1126b" else f"dependency_{index}"
        data = wheel_bytes(wheel["name"], wheel["version"], module)
        wheel.update(sha256=hashlib.sha256(data).hexdigest(), size=len(data))
        files[wheel["file"]] = data
    for artifact in manifest["artifacts"]:
        data = (APP / "engine-source.lock.json").read_bytes() if artifact["file"] == "engine-source.json" else b"controlled model fixture"
        artifact.update(sha256=hashlib.sha256(data).hexdigest(), size=len(data))
        files[artifact["file"]] = data
    archive = archive_bytes(files)
    manifest["x-asset-bundle"] = pin(archive)
    (root / "manifest.json").write_text(json.dumps(manifest))
    calls = serve(monkeypatch, archive)
    return root, manifest, files, calls


def test_standard_fetch_build_and_publish_inspection_use_current_source(staged, tmp_path):
    root, manifest, files, calls = staged
    fetched = fetch.fetch_app_assets(str(root))
    assert len(fetched) == len(manifest["python"]["wheels"]) + len(manifest["artifacts"])
    package = build.build(str(root), str(tmp_path / "dist"))
    assert publish.inspect_package(package)["version"] == manifest["version"]
    with tarfile.open(package) as archive:
        names = archive.getnames()
        assert archive.extractfile("app.py").read() == (APP / "app.py").read_bytes()
        assert json.load(archive.extractfile("manifest.json")) == manifest
        assert "files.sha256" in names and "release.lock.json" in names
        assert not {"build.py", "build_app.py", "tests/test_private.py", "engine-source.lock.json"} & set(names)
        for relative, data in files.items():
            if relative not in {"app.py", "manifest.json"}:
                assert archive.extractfile(relative).read() == data
    second = build.build(str(root), str(tmp_path / "again"))
    assert Path(package).read_bytes() == Path(second).read_bytes()
    assert len(calls) == 1


def test_standard_builder_refuses_a_clean_checkout_without_assets(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    for name in ("manifest.json", "app.py", "icon.jpg"):
        shutil.copyfile(APP / name, root / name)
    with pytest.raises(SystemExit, match="package is missing 'wheels/"):
        build.build(str(root), str(tmp_path / "dist"))
    assert not (tmp_path / "dist").exists()


def test_builder_and_publisher_reject_the_previous_empty_template(tmp_path):
    manifest = json.loads((APP / "manifest.json").read_text())
    manifest["python"]["wheels"] = []
    manifest["artifacts"] = []
    manifest["models"] = []
    root = tmp_path / "app"
    root.mkdir()
    (root / "manifest.json").write_text(json.dumps(manifest))
    shutil.copyfile(APP / "app.py", root / "app.py")
    shutil.copyfile(APP / "icon.jpg", root / "icon.jpg")
    with pytest.raises(SystemExit, match="inference_edge_probe"):
        build.build(str(root), str(tmp_path / "dist"))

    # Build an intentionally invalid external input with a valid BOM/lock. The
    # publisher must independently reject it, even when the builder was bypassed.
    files = {p.name: p.read_bytes() for p in root.iterdir()}
    records = {name: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
               for name, data in files.items()}
    lock, bom = publish.contract.make_release_metadata(manifest, records)
    files["files.sha256"] = bom
    files["release.lock.json"] = publish.contract.canonical_json(lock)
    package = tmp_path / "broken.tar.gz"
    package.write_bytes(archive_bytes(files))
    with pytest.raises(publish.PublishError, match="inference_edge_probe"):
        publish.inspect_package(str(package))


def test_missing_probe_inside_declared_engine_wheel_is_rejected(staged, tmp_path):
    root, manifest, _, _ = staged
    fetch.fetch_app_assets(str(root))
    engine = next(w for w in manifest["python"]["wheels"] if w["name"].replace("_", "-") == "inference-rv1126b")
    data = wheel_bytes(engine["name"], engine["version"], "wrong_module")
    (root / engine["file"]).write_bytes(data)
    engine.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
    (root / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(SystemExit, match="inference_edge_probe"):
        build.build(str(root), str(tmp_path / "dist"))


def test_scheduled_app_without_model_is_rejected(staged, tmp_path):
    root, manifest, _, _ = staged
    fetch.fetch_app_assets(str(root))
    for artifact in list(manifest["artifacts"]):
        if artifact["kind"] == "rknn":
            (root / artifact["file"]).unlink()
            manifest["artifacts"].remove(artifact)
    manifest["models"] = []
    (root / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(SystemExit, match="bundled RKNN artifact"):
        build.build(str(root), str(tmp_path / "dist"))

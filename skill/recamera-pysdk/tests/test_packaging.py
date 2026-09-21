import hashlib
import json
import shutil
import tarfile
import zipfile

import pytest

from package_app import build, PackagingError
from sdk_contract import bundled_sdk_root


def test_deterministic_installable_archive(make_app, tmp_path):
    app = make_app()
    archive, report_path = build(app, tmp_path / "out")
    first = archive.read_bytes()
    report = json.loads(report_path.read_text())
    assert report["sha256"] == hashlib.sha256(first).hexdigest()
    assert report["signature"]["required_by_device_policy"] is None
    assert report["final_archive"]["source_contract"]["state"] == "resolved"
    assert report["builder"]["files_sha256"]
    with tarfile.open(archive) as tf:
        assert {"app.py", "manifest.json", "files.sha256", "release.lock.json"} <= set(tf.getnames())
        manifest = json.load(tf.extractfile("manifest.json"))
        assert manifest["python"]["wheels"] == []
    build(app, tmp_path / "out")
    assert archive.read_bytes() == first


def test_private_wheel_remains_original(make_app, tmp_path, manifest):
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    wheel = wheels / "private_demo-1.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as zf:
        zf.writestr("private_demo/__init__.py", "VALUE = 1\n")
        zf.writestr("private_demo-1.0.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: private-demo\nVersion: 1.0.0\n")
        zf.writestr("private_demo-1.0.0.dist-info/WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        zf.writestr("private_demo-1.0.0.dist-info/RECORD", "")
    manifest["python"]["imports"] = ["private_demo"]
    archive, report_path = build(make_app(), tmp_path / "out", wheelhouse=wheels)
    with tarfile.open(archive) as tf:
        assert tf.extractfile("wheels/" + wheel.name).read() == wheel.read_bytes()
    assert len(json.loads(report_path.read_text())["bundled_packages"]) == 1


def test_final_archive_checks_source_even_with_valid_bom(make_app, tmp_path, monkeypatch):
    import package_app
    app = make_app()
    original_copy = package_app.copy_app
    def replace_entry(app_dir, staging):
        result = original_copy(app_dir, staging)
        (staging / "app.py").write_text("print('standalone; cannot be loaded as a Kit App')\n")
        return result
    monkeypatch.setattr(package_app, "copy_app", replace_entry)
    with pytest.raises(PackagingError, match="final archive source contract"):
        build(app, tmp_path / "out")


def test_failed_builder_does_not_accept_stale_archive(make_app, tmp_path):
    app = make_app()
    archive, _ = build(app, tmp_path / "out")
    sdk = tmp_path / "sdk"
    shutil.copytree(bundled_sdk_root(), sdk)
    (sdk / "market/packaging/build.py").write_text("raise SystemExit(0)\n")
    with pytest.raises(PackagingError, match="did not produce expected archive"):
        build(app, tmp_path / "out", sdk_root=sdk)
    assert not archive.exists()

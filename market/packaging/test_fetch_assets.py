"""Pinned asset bundles use the standard downloader without executing app code."""
import hashlib
import importlib.util
import io
import json
import tarfile
from pathlib import Path

import pytest

from market.packaging.test_build_v2 import manifest_v2, make_source


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fetch = load("fetch_assets_test", "fetch_assets.py")


def archive_bytes(files):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as archive:
        for name, value in files.items():
            member = tarfile.TarInfo(name)
            if isinstance(value, tarfile.TarInfo):
                archive.addfile(value)
            else:
                member.size = len(value)
                archive.addfile(member, io.BytesIO(value))
    return data.getvalue()


def asset(relative, data):
    return {"id": "fixture-data", "kind": "data", "source": "bundled",
            "file": relative, "mount": relative,
            "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
            "required": True, "share_scope": "content"}


def pin(data):
    return {"url": "https://assets.example.invalid/runtime.tar.gz",
            "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def serve(monkeypatch, data):
    calls = []

    class Response(io.BytesIO):
        def geturl(self):
            return "https://assets.example.invalid/runtime.tar.gz"

    def urlopen(request, **kwargs):
        calls.append(request.full_url)
        return Response(data)

    monkeypatch.setattr(fetch.urllib.request, "urlopen", urlopen)
    return calls


def source(tmp_path, data, files=None):
    value = manifest_v2(artifacts=[asset("models/data.json", b"data")])
    value["x-asset-bundle"] = pin(data)
    root = tmp_path / "app"
    make_source(str(root), value, files)
    return root, value


def test_bundle_only_supplies_declared_assets_and_reuses_verified_cache(tmp_path, monkeypatch):
    data = archive_bytes({"models/data.json": b"data", "src/main.py": b"old code",
                          "manifest.json": b"old metadata", "undeclared": b"ignore"})
    root, value = source(tmp_path, data)
    calls = serve(monkeypatch, data)
    original = (root / "manifest.json").read_bytes()
    assert fetch.fetch_app_assets(str(root)) == [str(root / "models/data.json")]
    assert (root / "models/data.json").read_bytes() == b"data"
    assert (root / "src/main.py").read_bytes() == b"# entry\n"
    assert (root / "manifest.json").read_bytes() == original
    assert not (root / "undeclared").exists()
    assert fetch.fetch_app_assets(str(root)) == []
    assert len(calls) == 1
    (root / "models/data.json").write_bytes(b"bad!")
    fetch.fetch_app_assets(str(root))
    assert (root / "models/data.json").read_bytes() == b"data"
    assert len(calls) == 2


@pytest.mark.parametrize("failure", ["archive_hash", "archive_size", "member_hash", "member_size", "missing"])
def test_bad_bundle_preserves_existing_files(tmp_path, monkeypatch, failure):
    payload = {"models/data.json": b"data"}
    if failure == "member_hash":
        payload["models/data.json"] = b"nope"
    elif failure == "member_size":
        payload["models/data.json"] = b"too long"
    elif failure == "missing":
        payload = {"unrelated": b"hello"}
    data = archive_bytes(payload)
    root, value = source(tmp_path, data, {"models/data.json": b"keep"})
    if failure == "archive_hash":
        value["x-asset-bundle"]["sha256"] = "0" * 64
    elif failure == "archive_size":
        value["x-asset-bundle"]["size"] -= 1
    (root / "manifest.json").write_text(json.dumps(value))
    serve(monkeypatch, data)
    with pytest.raises(ValueError):
        fetch.fetch_app_assets(str(root))
    assert (root / "models/data.json").read_bytes() == b"keep"
    assert not list(root.glob(".asset-bundle-*"))


@pytest.mark.parametrize("kind", ["traversal", "symlink", "duplicate", "fifo"])
def test_bundle_rejects_unsafe_members_before_any_write(tmp_path, monkeypatch, kind):
    bad = tarfile.TarInfo("malicious")
    if kind == "traversal":
        bad.name = "../escaped"
    elif kind == "symlink":
        bad.type, bad.linkname = tarfile.SYMTYPE, "models/data.json"
    elif kind == "duplicate":
        bad.name = "models/data.json"
    else:
        bad.type = tarfile.FIFOTYPE
    data = archive_bytes({"models/data.json": b"data", "bad": bad})
    root, _ = source(tmp_path, data)
    serve(monkeypatch, data)
    with pytest.raises(ValueError):
        fetch.fetch_app_assets(str(root))
    assert not (root / "models/data.json").exists()
    assert not (tmp_path / "escaped").exists()


def test_bundle_rejects_existing_destination_symlinks(tmp_path, monkeypatch):
    data = archive_bytes({"models/data.json": b"data"})
    root, _ = source(tmp_path, data)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "models").symlink_to(outside, target_is_directory=True)
    calls = serve(monkeypatch, data)
    with pytest.raises(ValueError, match="symlinks"):
        fetch.fetch_app_assets(str(root))
    assert not calls and not list(outside.iterdir())


def test_bundle_requires_pinned_https_and_bounded_size(tmp_path, monkeypatch):
    data = archive_bytes({"models/data.json": b"data"})
    root, value = source(tmp_path, data)
    calls = serve(monkeypatch, data)
    for field, wrong in [("url", "http://example.invalid/x"), ("sha256", "latest"),
                         ("size", fetch.paths.MAX_PKG_BYTES + 1)]:
        value["x-asset-bundle"] = {**pin(data), field: wrong}
        (root / "manifest.json").write_text(json.dumps(value))
        with pytest.raises(ValueError):
            fetch.fetch_app_assets(str(root))
    assert not calls

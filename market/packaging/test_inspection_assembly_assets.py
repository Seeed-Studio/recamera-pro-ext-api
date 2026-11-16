"""The inspection app's wheel closure must be fetchable without an OSS copy."""
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from market.packaging.test_fetch_assets import fetch


APP = Path(__file__).resolve().parents[2] / "apps/inspection-assembly"


def test_every_inspection_wheel_has_an_exact_pypi_download_url():
    manifest = json.loads((APP / "manifest.json").read_text())
    wheels = manifest["python"]["wheels"]
    assert wheels
    for wheel in wheels:
        url = urlsplit(wheel["x-url"])
        assert url.scheme == "https"
        assert url.netloc == "files.pythonhosted.org"
        assert url.path.startswith("/packages/")
        assert url.path.rsplit("/", 1)[1] == wheel["filename"]
        assert not url.query and not url.fragment


@pytest.fixture
def wheel_sources(tmp_path, monkeypatch):
    manifest = json.loads((APP / "manifest.json").read_text())
    root = tmp_path / "inspection-assembly"
    root.mkdir()
    # Models are already staged in this fixture; only wheel retrieval is under
    # test. Keep the real app's filenames, source URLs and target wheel tags.
    for artifact in manifest["artifacts"]:
        data = b"controlled model fixture"
        artifact.update(sha256=hashlib.sha256(data).hexdigest(), size=len(data))
        path = root / artifact["file"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    responses = {}
    for wheel in manifest["python"]["wheels"]:
        data = wheel["filename"].encode()
        wheel.update(sha256=hashlib.sha256(data).hexdigest(), size=len(data))
        responses[wheel["x-url"]] = data
    (root / "manifest.json").write_text(json.dumps(manifest))
    calls = []

    def download(url, destination):
        # Any fallback to the missing CDN wheel pool fails this regression.
        assert url in responses, f"unexpected download: {url}"
        calls.append(url)
        Path(destination).write_bytes(responses[url])
        return True

    monkeypatch.setattr(fetch, "_download_url", download)
    monkeypatch.setattr(fetch, "_has_ossutil", lambda: pytest.fail("unexpected OSS fallback"))
    return root, manifest, responses, calls


def test_standard_fetch_uses_wheel_urls_and_verified_cache(wheel_sources):
    root, manifest, responses, calls = wheel_sources
    expected = {str(root / wheel["file"]) for wheel in manifest["python"]["wheels"]}
    assert set(fetch.fetch_app_assets(str(root))) == expected
    assert len(calls) == len(responses)
    for wheel in manifest["python"]["wheels"]:
        assert (root / wheel["file"]).read_bytes() == responses[wheel["x-url"]]
    assert fetch.fetch_app_assets(str(root)) == []
    assert len(calls) == len(responses)


@pytest.mark.parametrize("failure", ["hash", "size"])
def test_explicit_wheel_urls_still_enforce_integrity(wheel_sources, failure):
    root, manifest, responses, _ = wheel_sources
    first = min(manifest["python"]["wheels"], key=lambda wheel: wheel["file"])
    data = responses[first["x-url"]]
    responses[first["x-url"]] = b"x" * len(data) if failure == "hash" else data + b"extra"
    with pytest.raises(ValueError, match="verification failed"):
        fetch.fetch_app_assets(str(root))
    assert not (root / first["file"]).exists()

"""Bounded device-side store downloads, without network or a camera."""
from __future__ import annotations

import copy
import errno
import hashlib
import io
import json
import os
import socket
import ssl
import sys
import threading
import urllib.error
import urllib.request
from unittest.mock import Mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from appmgr import store_download as store


SIGNATURE = "MEUCIQCEK1MpkON9KiFlhHwiiZQzA5NMUgA18E4hdZLMsuvwlQIgTd84stOaZHPZ+Kf1pO+Czvcgzs+f85yLu0UGxgpKcH8="


def app_entry(data=b"abc", **changes):
    return {
        "id": "demo", "name": "Demo", "name_zh": "示例", "version": "1.0.0",
        "description": "Example", "description_zh": "示例应用", "arch": "arm64",
        "icon_url": store.STORE_ROOT + "icons/demo.png", "models": [],
        "package": {
            "url": store.STORE_ROOT + "packages/demo-1.0.0-arm64.tar.gz",
            "filename": "demo-1.0.0-arm64.tar.gz", "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "signature": SIGNATURE, "signature_alg": "ecdsa-sha256",
        }, **changes,
    }


class Response:
    def __init__(self, chunks, *, url=store.CATALOG_URL, headers=None, status=200,
                 before_read=None):
        self.chunks = list(chunks)
        self.url = url
        self.headers = headers or {}
        self.status = status
        self.closed = False
        self.read_sizes = []
        self.before_read = before_read

    def getcode(self):
        return self.status

    def geturl(self):
        return self.url

    def read1(self, amount):
        self.read_sizes.append(amount)
        if self.before_read:
            self.before_read()
        if not self.chunks:
            return b""
        chunk = self.chunks.pop(0)
        if isinstance(chunk, BaseException):
            raise chunk
        if len(chunk) > amount:
            self.chunks.insert(0, chunk[amount:])
            return chunk[:amount]
        return chunk

    def close(self):
        self.closed = True


def serve(monkeypatch, response=None, error=None):
    opener = Mock()
    opener.open.side_effect = error
    opener.open.return_value = response
    monkeypatch.setattr(store, "_make_opener", lambda: opener)
    return opener


def catalog_response(apps=None, **kwargs):
    value = {"schema": 1, "apps": [app_entry()] if apps is None else apps}
    return Response([json.dumps(value).encode("utf-8")], **kwargs)


def package_response(chunks=None, **kwargs):
    return Response([b"a", b"bc"] if chunks is None else chunks,
                    url=app_entry()["package"]["url"], **kwargs)


def assert_error(code, action):
    with pytest.raises(store.StoreDownloadError) as failure:
        action()
    assert failure.value.code == code


def test_catalog_preserves_localized_copy_and_trusted_icons_without_device_headers(monkeypatch):
    response = catalog_response()
    opener = serve(monkeypatch, response)
    catalog = store.fetch_catalog()
    assert catalog["apps"][0]["name_zh"] == "示例"
    assert catalog["apps"][0]["icon_url"] == store.STORE_ROOT + "icons/demo.png"
    assert catalog["apps"][0]["unavailableReason"] == ""
    request = opener.open.call_args.args[0]
    assert request.full_url == store.CATALOG_URL
    assert request.get_header("Authorization") is None
    assert request.get_header("Cookie") is None
    assert request.get_header("Referer") is None
    assert request.get_header("Accept-encoding") == "identity"
    assert 0 < opener.open.call_args.kwargs["timeout"] <= store.SOCKET_TIMEOUT_SEC
    assert response.closed


def test_opener_verifies_tls_disables_proxies_and_refuses_redirects(monkeypatch):
    captured = []
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: captured.extend(handlers))
    store._make_opener()
    proxy = next(handler for handler in captured if isinstance(handler, urllib.request.ProxyHandler))
    https = next(handler for handler in captured if isinstance(handler, urllib.request.HTTPSHandler))
    redirect = next(handler for handler in captured if isinstance(handler, store._NoRedirect))
    assert proxy.proxies == {}
    assert https._context.verify_mode == ssl.CERT_REQUIRED
    assert https._context.check_hostname is True
    assert redirect.redirect_request(None, None, 302, "", {}, "http://127.0.0.1/") is None


def test_bad_catalog_item_does_not_hide_valid_apps_and_duplicate_ids_cannot_install():
    broken = app_entry(id="broken")
    catalog = store.normalize_catalog({"schema": 1, "apps": [broken, app_entry()]})
    assert len(catalog["apps"]) == 2
    assert catalog["apps"][0]["unavailableReason"] == "storePackageInvalid"
    assert catalog["apps"][1]["unavailableReason"] == ""
    duplicate = store.normalize_catalog({"schema": 1, "apps": [app_entry(), app_entry()]})
    assert duplicate["apps"][0]["unavailableReason"] == "storePackageInvalid"


@pytest.mark.parametrize("value", [
    {"schema": True, "apps": []}, {"schema": 2, "apps": []}, {"schema": 1, "apps": {}},
    {"schema": 1, "apps": [app_entry()] * (store.MAX_CATALOG_APPS + 1)},
])
def test_invalid_catalog_envelopes(value):
    assert_error("storeCatalogInvalid", lambda: store.normalize_catalog(value))


@pytest.mark.parametrize("body", [b"not JSON", b"\xff", b'{"schema":1,"schema":2,"apps":[]}'])
def test_bad_json_is_rejected_and_response_closed(monkeypatch, body):
    response = Response([body])
    serve(monkeypatch, response)
    assert_error("storeCatalogInvalid", store.fetch_catalog)
    assert response.closed


@pytest.mark.parametrize("declared", [None, "3", "999999999"])
def test_catalog_size_is_bounded_even_without_or_with_false_content_length(monkeypatch, declared):
    headers = {} if declared is None else {"Content-Length": declared}
    response = Response([b"x" * 8], headers=headers)
    monkeypatch.setattr(store, "MAX_CATALOG_BYTES", 4)
    serve(monkeypatch, response)
    assert_error("storeCatalogInvalid", store.fetch_catalog)
    assert response.closed


@pytest.mark.parametrize("changes", [
    {"url": "http://sensecraft-statics.seeed.cc/solution-app/recamera_pro/packages/demo-1.0.0-arm64.tar.gz"},
    {"url": "https://sensecraft-statics.seeed.cc.evil.example/pkg.tar.gz"},
    {"url": store.STORE_ROOT + "packages/../packages/demo-1.0.0-arm64.tar.gz"},
    {"url": store.STORE_ROOT + "packages/demo-1.0.0-arm64.tar.gz?url=http://127.0.0.1/"},
    {"filename": "../demo.tar.gz"}, {"sha256": "no"}, {"size": True}, {"size": 0},
])
def test_invalid_package_metadata_never_opens_network_or_files(monkeypatch, tmp_path, changes):
    app = app_entry()
    app["package"].update(changes)
    opener = serve(monkeypatch)
    assert_error("storePackageInvalid", lambda: store.download_package(
        app, tmp_path / "package", tmp_path / "signature"))
    opener.open.assert_not_called()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("changes", [
    {"signature": ""}, {"signature": "YWJj"}, {"signature": "!bad!"},
    {"signature_alg": "none"},
])
def test_cloud_packages_never_downgrade_to_unsigned(monkeypatch, tmp_path, changes):
    app = app_entry()
    app["package"].update(changes)
    opener = serve(monkeypatch)
    assert_error("storeSignatureInvalid", lambda: store.download_package(
        app, tmp_path / "package", tmp_path / "signature"))
    opener.open.assert_not_called()
    assert list(tmp_path.iterdir()) == []


def test_external_models_are_explicitly_unavailable(monkeypatch, tmp_path):
    app = app_entry(models=[{
        "url": store.STORE_ROOT + "models/demo/model.rknn", "filename": "model.rknn",
        "size": 3, "sha256": app_entry()["package"]["sha256"],
        "target_path": "/userdata/local/models/demo",
    }])
    normalized = store.normalize_catalog({"schema": 1, "apps": [app]})["apps"][0]
    assert normalized["hasExternalModels"]
    assert normalized["models"][0]["filename"] == "model.rknn"
    opener = serve(monkeypatch)
    assert_error("storeExternalModelsUnsupported", lambda: store.download_package(
        normalized, tmp_path / "package", tmp_path / "signature"))
    opener.open.assert_not_called()


def test_unsupported_architecture_is_not_downloaded(monkeypatch, tmp_path):
    app = app_entry(arch="x86_64")
    app["package"]["filename"] = "demo-1.0.0-x86_64.tar.gz"
    app["package"]["url"] = store.STORE_ROOT + "packages/" + app["package"]["filename"]
    opener = serve(monkeypatch)
    assert_error("storeArchitectureUnsupported", lambda: store.download_package(
        app, tmp_path / "package", tmp_path / "signature"))
    opener.open.assert_not_called()


def test_resolve_binds_identity_version_and_hash_from_the_device_catalog(monkeypatch):
    serve(monkeypatch, catalog_response())
    app = store.resolve_app("demo", "1.0.0", package_sha256=app_entry()["package"]["sha256"])
    assert app["id"] == "demo"
    for identity, version, digest in [
        ("other", "1.0.0", app["package"]["sha256"]),
        ("demo", "1.0.1", app["package"]["sha256"]),
        ("demo", "1.0.0", "0" * 64),
    ]:
        serve(monkeypatch, catalog_response())
        assert_error("storeCatalogChanged", lambda: store.resolve_app(
            identity, version, package_sha256=digest))


def test_resolve_rejects_an_arbitrary_url_selection_before_network(monkeypatch):
    opener = serve(monkeypatch)
    assert_error("storePackageInvalid", lambda: store.resolve_app(
        "http://127.0.0.1/", "1.0.0", package_sha256="0" * 64))
    opener.open.assert_not_called()


def test_package_streams_in_bounded_chunks_and_writes_signature_with_private_permissions(monkeypatch, tmp_path):
    data = b"abc123" * (store.CHUNK_BYTES // 2)
    app = app_entry(data)
    response = package_response([data])
    serve(monkeypatch, response)
    progress = []
    package = tmp_path / "package.tar.gz"
    signature = tmp_path / "package.tar.gz.sig"
    result = store.download_package(app, package, signature,
                                    on_progress=lambda loaded, total: progress.append((loaded, total)))
    assert package.read_bytes() == data
    assert signature.read_text() == SIGNATURE + "\n"
    assert os.stat(package).st_mode & 0o777 == 0o600
    assert os.stat(signature).st_mode & 0o777 == 0o600
    assert result["sha256"] == hashlib.sha256(data).hexdigest()
    assert progress[0] == (0, len(data))
    assert progress[-1] == (len(data), len(data))
    assert max(response.read_sizes) <= store.CHUNK_BYTES
    assert response.closed


@pytest.mark.parametrize("chunks,headers", [
    ([b"ab"], {}), ([b"abcd"], {}), ([b"abcd"], {"Content-Length": "3"}),
    ([b"abc"], {"Content-Length": "4"}), ([b"abc"], {"Content-Length": "invalid"}),
])
def test_truncated_oversized_and_misdeclared_bodies_remove_partial_files(monkeypatch, tmp_path, chunks, headers):
    response = package_response(chunks, headers=headers)
    serve(monkeypatch, response)
    assert_error("storePackageSizeMismatch", lambda: store.download_package(
        app_entry(), tmp_path / "package", tmp_path / "signature"))
    assert response.closed
    assert list(tmp_path.iterdir()) == []


def test_hash_mismatch_removes_all_outputs(monkeypatch, tmp_path):
    response = package_response([b"abd"])
    serve(monkeypatch, response)
    assert_error("storePackageHashMismatch", lambda: store.download_package(
        app_entry(), tmp_path / "package", tmp_path / "signature"))
    assert response.closed
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("limit,code", [
    ({"max_package_bytes": 2}, "storePackageTooLarge"),
    ({"max_signature_bytes": 2}, "storeSignatureInvalid"),
])
def test_limits_apply_before_any_network_or_file_write(monkeypatch, tmp_path, limit, code):
    opener = serve(monkeypatch)
    assert_error(code, lambda: store.download_package(
        app_entry(), tmp_path / "package", tmp_path / "signature", **limit))
    opener.open.assert_not_called()
    assert list(tmp_path.iterdir()) == []


def test_cancellation_before_connection_has_no_side_effects(monkeypatch, tmp_path):
    event = threading.Event()
    event.set()
    opener = serve(monkeypatch)
    assert_error("store_cancelled", lambda: store.fetch_catalog(cancelled=event.is_set))
    assert_error("store_cancelled", lambda: store.download_package(
        app_entry(), tmp_path / "package", tmp_path / "signature", cancelled=event.is_set))
    opener.open.assert_not_called()
    assert list(tmp_path.iterdir()) == []


def test_cancellation_during_download_closes_network_and_removes_partial_files(monkeypatch, tmp_path):
    event = threading.Event()
    response = package_response()
    serve(monkeypatch, response)
    assert_error("store_cancelled", lambda: store.download_package(
        app_entry(), tmp_path / "package", tmp_path / "signature", cancelled=event.is_set,
        on_progress=lambda loaded, total: event.set() if loaded else None))
    assert response.closed
    assert len(response.read_sizes) == 1
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("error,code", [
    (socket.timeout("idle"), "storeDownloadTimeout"),
    (ConnectionResetError("peer reset"), "storePackageFetchFailed"),
    (urllib.error.URLError(ssl.SSLCertVerificationError("bad cert")), "storePackageFetchFailed"),
])
def test_network_and_certificate_failures_cleanup(monkeypatch, tmp_path, error, code):
    response = package_response([b"a", error])
    serve(monkeypatch, response)
    assert_error(code, lambda: store.download_package(
        app_entry(), tmp_path / "package", tmp_path / "signature"))
    assert response.closed
    assert list(tmp_path.iterdir()) == []


def test_http_redirect_is_refused_and_error_response_closed(monkeypatch, tmp_path):
    body = io.BytesIO(b"redirect")
    error = urllib.error.HTTPError(app_entry()["package"]["url"], 302, "redirect",
                                   {"Location": "http://127.0.0.1/"}, body)
    opener = serve(monkeypatch, error=error)
    assert_error("storePackageFetchFailed", lambda: store.download_package(
        app_entry(), tmp_path / "package", tmp_path / "signature"))
    assert opener.open.call_count == 1
    assert body.closed
    assert list(tmp_path.iterdir()) == []


def test_trickling_data_cannot_bypass_total_deadline(monkeypatch, tmp_path):
    clock = [0.0]
    monkeypatch.setattr(store.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(store, "DOWNLOAD_TIMEOUT_SEC", 1.0)
    response = package_response(before_read=lambda: clock.__setitem__(0, clock[0] + 0.6))
    serve(monkeypatch, response)
    assert_error("storeDownloadTimeout", lambda: store.download_package(
        app_entry(), tmp_path / "package", tmp_path / "signature"))
    assert response.closed
    assert len(response.read_sizes) == 2
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("existing", ["package", "signature"])
def test_existing_files_are_never_overwritten_or_deleted(monkeypatch, tmp_path, existing):
    response = package_response()
    serve(monkeypatch, response)
    (tmp_path / existing).write_bytes(b"user data")
    assert_error("storePackageWriteFailed", lambda: store.download_package(
        app_entry(), tmp_path / "package", tmp_path / "signature"))
    assert (tmp_path / existing).read_bytes() == b"user data"
    assert {entry.name for entry in tmp_path.iterdir()} == {existing}


def test_symlink_output_is_never_followed(monkeypatch, tmp_path):
    target = tmp_path / "user-data"
    target.write_bytes(b"untouched")
    package = tmp_path / "package"
    package.symlink_to(target)
    opener = serve(monkeypatch)
    assert_error("storePackageWriteFailed", lambda: store.download_package(
        app_entry(), package, tmp_path / "signature"))
    assert target.read_bytes() == b"untouched"
    assert package.is_symlink()
    opener.open.assert_not_called()


def test_disk_failure_removes_created_package(monkeypatch, tmp_path):
    serve(monkeypatch, package_response())
    monkeypatch.setattr(store.os, "fsync", Mock(side_effect=OSError(errno.ENOSPC, "disk full")))
    assert_error("storePackageWriteFailed", lambda: store.download_package(
        app_entry(), tmp_path / "package", tmp_path / "signature"))
    assert list(tmp_path.iterdir()) == []


def test_untrusted_icon_urls_are_omitted():
    app = app_entry(icon_url="https://evil.example/track.png")
    assert store.normalize_catalog({"schema": 1, "apps": [app]})["apps"][0]["icon_url"] == ""


def test_revalidation_blocks_mutated_package_urls(monkeypatch, tmp_path):
    app = store.normalize_catalog({"schema": 1, "apps": [app_entry()]})["apps"][0]
    app = copy.deepcopy(app)
    app["package"]["url"] = "http://169.254.169.254/latest/meta-data/"
    opener = serve(monkeypatch)
    assert_error("storePackageInvalid", lambda: store.download_package(
        app, tmp_path / "package", tmp_path / "signature"))
    opener.open.assert_not_called()

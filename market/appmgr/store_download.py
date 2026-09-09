"""Fixed-origin, bounded cloud downloads for App Center staging.

This module only acquires bytes. The caller reserves upload staging quota and
must run the existing authenticated package preflight before offering install.
In particular, a catalog hash is not a replacement for the device's signature
verification or manifest/permission checks.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from typing import Callable, Optional

from . import paths


CATALOG_URL = "https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/catalog.json"
STORE_ROOT = CATALOG_URL.rsplit("/", 1)[0] + "/"
MAX_CATALOG_BYTES = 2 * 1024 * 1024
MAX_CATALOG_APPS = 500
MAX_PACKAGE_BYTES = 200 * 1024 * 1024
MAX_SIGNATURE_BYTES = 16 * 1024
CHUNK_BYTES = 64 * 1024
SOCKET_TIMEOUT_SEC = 10.0
CATALOG_TIMEOUT_SEC = 30.0
DOWNLOAD_TIMEOUT_SEC = 600.0

_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_VERSION = re.compile(r"[0-9][0-9A-Za-z.+_-]{0,63}\Z")
_ARCH = re.compile(r"[a-z0-9_-]{1,32}\Z")
_HASH = re.compile(r"[0-9a-fA-F]{64}\Z")
_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\Z")
# Match uploads.PACKAGE_FILENAME_PATTERN: every downloaded file must also pass
# the ordinary local-Web staging and preflight contracts.
_PACKAGE_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.tar\.gz\Z")
_IMAGE_FILE = re.compile(r"[a-z0-9][a-z0-9_-]*\.(?:png|jpe?g|webp)\Z", re.I)
Cancelled = Optional[Callable[[], bool]]


class StoreDownloadError(ValueError):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


def _fail(code: str, message: str = ""):
    raise StoreDownloadError(code, message)


def _text(value, limit=4096):
    return value.strip()[:limit] if isinstance(value, str) else ""


def _positive_size(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _package_limit(limit=None):
    limits = [MAX_PACKAGE_BYTES, paths.MAX_PKG_BYTES]
    if limit is not None:
        limits.append(limit)
    if not all(_positive_size(value) for value in limits):
        _fail("storePackageInvalid", "invalid package limit")
    return min(limits)


def _check_active(cancelled: Cancelled, deadline=None):
    if cancelled is not None and cancelled():
        _fail("store_cancelled", "store download was cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        _fail("storeDownloadTimeout", "store download timed out")


def _signature(value):
    if not isinstance(value, str) or len(value) > MAX_SIGNATURE_BYTES:
        _fail("storeSignatureInvalid", "a detached package signature is required")
    value = value.strip()
    try:
        der = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        _fail("storeSignatureInvalid", "invalid signature encoding")
    if (len(der) < 8 or len(der) > 72 or der[0] != 0x30
            or der[1] != len(der) - 2 or der[2] != 0x02):
        _fail("storeSignatureInvalid", "invalid ECDSA signature structure")
    first_length = der[3]
    second_offset = 4 + first_length
    if (first_length < 1 or first_length > 33
            or second_offset + 2 > len(der)
            or der[second_offset] != 0x02):
        _fail("storeSignatureInvalid", "invalid ECDSA signature structure")
    second_length = der[second_offset + 1]
    if (second_length < 1 or second_length > 33
            or second_offset + 2 + second_length != len(der)):
        _fail("storeSignatureInvalid", "invalid ECDSA signature structure")
    return value


def _file(source, prefix):
    if not isinstance(source, dict):
        _fail("storePackageInvalid", "invalid file metadata")
    filename = source.get("filename")
    digest = source.get("sha256")
    size = source.get("size")
    if (not isinstance(filename, str) or not _FILE.fullmatch(filename)
            or ".." in filename
            or source.get("url") != f"{STORE_ROOT}{prefix}/{filename}"
            or not isinstance(digest, str) or not _HASH.fullmatch(digest)
            or not _positive_size(size)):
        _fail("storePackageInvalid", "invalid file URL, filename, size or digest")
    return {"url": source["url"], "filename": filename,
            "sha256": digest.lower(), "size": size}


def _package(source, expected_filename=None):
    result = _file(source, "packages")
    if (not _PACKAGE_FILE.fullmatch(result["filename"])
            or (expected_filename is not None
                and result["filename"] != expected_filename)):
        _fail("storePackageInvalid", "package filename does not match the app")
    if source.get("signature_alg") != "ecdsa-sha256":
        _fail("storeSignatureInvalid", "unsupported signature algorithm")
    result.update(signature=_signature(source.get("signature")),
                  signature_alg="ecdsa-sha256")
    return result


def _image(value, prefix):
    if not isinstance(value, str) or not value.startswith(prefix):
        return ""
    return value if _IMAGE_FILE.fullmatch(value[len(prefix):]) else ""


def _normalize_app(source):
    if (not isinstance(source, dict) or not isinstance(source.get("id"), str)
            or not _ID.fullmatch(source["id"])):
        return None
    app = {
        "id": source["id"],
        "name": _text(source.get("name"), 200) or source["id"],
        "name_zh": _text(source.get("name_zh"), 200),
        "description": _text(source.get("description")),
        "description_zh": _text(source.get("description_zh")),
        "scene": _text(source.get("scene"), 100),
        "scene_zh": _text(source.get("scene_zh"), 100),
        "author": _text(source.get("author"), 200),
        "version": _text(source.get("version"), 64),
        "arch": _text(source.get("arch"), 32),
        "kit": _text(source.get("kit"), 100),
        "image": _image(source.get("image"), "/appcenter/apps/"),
        "icon_url": _image(source.get("icon_url"), f"{STORE_ROOT}icons/"),
        "capabilities": [],
        "package": None,
        "models": [],
        "hasExternalModels": bool(source.get("models")),
        "unavailableReason": "",
    }
    if isinstance(source.get("capabilities"), list):
        app["capabilities"] = [value for value in source["capabilities"]
                               if isinstance(value, str)
                               and re.fullmatch(r"[a-z][a-z0-9._-]{0,63}", value)][:32]
    try:
        if (not _VERSION.fullmatch(app["version"])
                or not _ARCH.fullmatch(app["arch"])
                or not isinstance(source.get("models", []), list)):
            _fail("storePackageInvalid", "invalid app version, architecture or models")
        app["package"] = _package(
            source.get("package"), f"{app['id']}-{app['version']}-{app['arch']}.tar.gz")
        for model in source.get("models", []):
            file = _file(model, f"models/{app['id']}")
            if model.get("target_path") != f"/userdata/local/models/{app['id']}":
                _fail("storePackageInvalid", "invalid external model target")
            app["models"].append({**file, "target_path": model["target_path"]})
        if app["arch"] != "arm64":
            _fail("storeArchitectureUnsupported", "application architecture is unsupported")
        if app["hasExternalModels"]:
            _fail("storeExternalModelsUnsupported", "external models cannot be installed by this flow")
        if app["package"]["size"] > _package_limit():
            _fail("storePackageTooLarge", "package exceeds the device limit")
    except StoreDownloadError as exc:
        app["unavailableReason"] = exc.code
    return app


def normalize_catalog(source):
    if (not isinstance(source, dict) or type(source.get("schema")) is not int
            or source["schema"] != 1 or not isinstance(source.get("apps"), list)
            or len(source["apps"]) > MAX_CATALOG_APPS):
        _fail("storeCatalogInvalid", "invalid application catalog")
    apps = {}
    for item in source["apps"]:
        app = _normalize_app(item)
        if app is None:
            continue
        if app["id"] in apps:
            apps[app["id"]]["unavailableReason"] = "storePackageInvalid"
        else:
            apps[app["id"]] = app
    if source["apps"] and not apps:
        _fail("storeCatalogInvalid", "catalog contains no valid application identities")
    runtimes = {}
    if isinstance(source.get("runtimes"), dict):
        for name, package in source["runtimes"].items():
            if not isinstance(name, str) or not _ID.fullmatch(name):
                continue
            try:
                runtimes[name] = _package(package)
            except StoreDownloadError:
                continue
    return {"schema": 1, "generated": _text(source.get("generated"), 64),
            "apps": list(apps.values()), "runtimes": runtimes}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _make_opener():
    # Do not inherit an HTTP proxy, cookies, Basic Auth or device credentials.
    # HTTPS verifies the fixed CDN's certificate and hostname using system CAs.
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()))


def _network_call(action, *, cancelled, deadline, error_code):
    try:
        return action()
    except urllib.error.HTTPError as exc:
        exc.close()
        _check_active(cancelled, deadline)
        _fail(error_code, f"store returned HTTP {exc.code}")
    except (TimeoutError, socket.timeout) as exc:
        _check_active(cancelled)
        raise StoreDownloadError("storeDownloadTimeout", "store connection timed out") from exc
    except urllib.error.URLError as exc:
        _check_active(cancelled, deadline)
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise StoreDownloadError("storeDownloadTimeout", "store connection timed out") from exc
        raise StoreDownloadError(error_code, "cannot connect securely to the application store") from exc
    except (OSError, ValueError) as exc:
        _check_active(cancelled, deadline)
        raise StoreDownloadError(error_code, "cannot read the application store response") from exc


@contextmanager
def _open_response(url, *, cancelled, deadline, error_code):
    _check_active(cancelled, deadline)
    request = urllib.request.Request(url, headers={
        "Accept": "application/json" if url == CATALOG_URL else "application/octet-stream",
        "Accept-Encoding": "identity",
        "User-Agent": "reCamera-AppCenter/1",
    }, method="GET")
    opener = _make_opener()
    _check_active(cancelled, deadline)
    response = _network_call(lambda: opener.open(
        request, timeout=min(SOCKET_TIMEOUT_SEC, deadline - time.monotonic())),
        cancelled=cancelled, deadline=deadline, error_code=error_code)
    try:
        _check_active(cancelled, deadline)
        if response.getcode() != 200 or response.geturl() != url:
            _fail(error_code, "unexpected store response or redirect")
        if response.headers.get("Content-Encoding", "identity").lower() not in ("", "identity"):
            _fail(error_code, "encoded store responses are unsupported")
        yield response
    finally:
        response.close()


def _response_chunks(response, *, limit, expected=None, cancelled, deadline, error_code,
                     fetch_error):
    declared = response.headers.get("Content-Length")
    if declared is not None:
        try:
            declared = int(declared)
        except (TypeError, ValueError):
            _fail(error_code, "invalid response size")
        if declared < 0 or declared > limit or (expected is not None and declared != expected):
            _fail(error_code, "response size does not match the catalog")
    # HTTPResponse.read1 performs at most one buffered/socket read per call;
    # unlike read(n), a trickle cannot keep it filling n bytes past the deadline.
    read = getattr(response, "read1", None)
    if read is None:
        _fail(error_code, "streaming store responses are required")
    loaded = 0
    while True:
        _check_active(cancelled, deadline)
        chunk = _network_call(lambda: read(min(CHUNK_BYTES, limit - loaded + 1)),
                              cancelled=cancelled, deadline=deadline, error_code=fetch_error)
        _check_active(cancelled, deadline)
        if not chunk:
            break
        loaded += len(chunk)
        if loaded > limit or (expected is not None and loaded > expected):
            _fail(error_code, "response exceeds the declared size")
        yield chunk
    if expected is not None and loaded != expected:
        _fail(error_code, "response was truncated")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate catalog field")
        result[key] = value
    return result


def fetch_catalog(*, cancelled: Cancelled = None):
    deadline = time.monotonic() + CATALOG_TIMEOUT_SEC
    with _open_response(CATALOG_URL, cancelled=cancelled, deadline=deadline,
                        error_code="storeCatalogFetchFailed") as response:
        data = bytearray()
        for chunk in _response_chunks(
                response, limit=MAX_CATALOG_BYTES, cancelled=cancelled,
                deadline=deadline, error_code="storeCatalogInvalid",
                fetch_error="storeCatalogFetchFailed"):
            data.extend(chunk)
    try:
        source = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise StoreDownloadError("storeCatalogInvalid", "invalid catalog JSON") from exc
    _check_active(cancelled, deadline)
    return normalize_catalog(source)


def resolve_app(app_id, version, *, package_sha256, cancelled: Cancelled = None):
    """Resolve a selection against the device-fetched catalog, including its hash."""
    if (not isinstance(app_id, str) or not _ID.fullmatch(app_id)
            or not isinstance(version, str) or not _VERSION.fullmatch(version)
            or not isinstance(package_sha256, str) or not _HASH.fullmatch(package_sha256)):
        _fail("storePackageInvalid", "invalid application selection")
    catalog = fetch_catalog(cancelled=cancelled)
    for app in catalog["apps"]:
        if app["id"] != app_id:
            continue
        if app["unavailableReason"]:
            _fail(app["unavailableReason"], "application is unavailable in the store")
        if app["version"] != version or app["package"]["sha256"] != package_sha256.lower():
            _fail("storeCatalogChanged", "application changed; refresh the store")
        return app
    _fail("storeCatalogChanged", "application is no longer in the store")


@contextmanager
def _exclusive_output(path, created):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         getattr(os, "O_NOFOLLOW", 0), 0o600)
    created.append(path)
    try:
        output = os.fdopen(descriptor, "wb")
    except BaseException:
        os.close(descriptor)
        raise
    with output:
        yield output
        output.flush()
        os.fsync(output.fileno())


def download_package(app, package_path, signature_path, *,
                     max_package_bytes=None, max_signature_bytes=MAX_SIGNATURE_BYTES,
                     cancelled: Cancelled = None, on_progress=None):
    """Write verified-size/hash package bytes and its mandatory detached signature.

    The caller owns and reserves the containing private staging directory. Both
    output paths must be absent; no user-supplied destination is accepted by the
    HTTP API. Failed and cancelled transfers remove only files created here.
    """
    _check_active(cancelled)
    validated = _normalize_app(app)
    if validated is None:
        _fail("storePackageInvalid", "invalid application")
    if app.get("unavailableReason") or validated["unavailableReason"]:
        _fail(app.get("unavailableReason") or validated["unavailableReason"],
              "application is unavailable in the store")
    package = validated["package"]
    limit = _package_limit(max_package_bytes)
    if package["size"] > limit:
        _fail("storePackageTooLarge", "package exceeds the device limit")
    signature = (package["signature"] + "\n").encode("ascii")
    if (not _positive_size(max_signature_bytes)
            or len(signature) > min(max_signature_bytes, MAX_SIGNATURE_BYTES)):
        _fail("storeSignatureInvalid", "signature exceeds the device limit")
    package_path, signature_path = os.fspath(package_path), os.fspath(signature_path)
    deadline = time.monotonic() + DOWNLOAD_TIMEOUT_SEC
    created = []
    try:
        with _exclusive_output(package_path, created) as output:
            with _open_response(package["url"], cancelled=cancelled, deadline=deadline,
                                error_code="storePackageFetchFailed") as response:
                digest = hashlib.sha256()
                loaded = 0
                if on_progress is not None:
                    on_progress(0, package["size"])
                for chunk in _response_chunks(
                        response, limit=limit, expected=package["size"], cancelled=cancelled,
                        deadline=deadline, error_code="storePackageSizeMismatch",
                        fetch_error="storePackageFetchFailed"):
                    output.write(chunk)
                    digest.update(chunk)
                    loaded += len(chunk)
                    if on_progress is not None:
                        on_progress(loaded, package["size"])
                if digest.hexdigest() != package["sha256"]:
                    _fail("storePackageHashMismatch", "package digest does not match the catalog")
        _check_active(cancelled, deadline)
        with _exclusive_output(signature_path, created) as output:
            output.write(signature)
        _check_active(cancelled, deadline)
        return {"package_path": package_path, "signature_path": signature_path,
                "app_id": validated["id"], "version": validated["version"],
                "filename": package["filename"], "size": package["size"],
                "sha256": package["sha256"]}
    except BaseException as exc:
        for path in reversed(created):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        if isinstance(exc, OSError):
            raise StoreDownloadError("storePackageWriteFailed", "cannot stage the store package") from exc
        raise

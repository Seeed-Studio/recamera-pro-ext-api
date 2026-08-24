"""Bounded streaming multipart uploads for the Web App Center API."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from email.message import Message
from typing import BinaryIO, Optional

from . import paths


MAX_MULTIPART_OVERHEAD = 256 * 1024
MAX_SIGNATURE_BYTES = 16 * 1024
MAX_HEADER_BYTES = 32 * 1024
CHUNK = 64 * 1024
_UPLOAD_ID = re.compile(r"[0-9a-f]{32}\Z")
_PACKAGE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.tar\.gz\Z")
_UPLOAD_LOCK = threading.RLock()
_ACTIVE_STATUSES = frozenset(("install_queued", "installing"))


class MultipartError(ValueError):
    pass


class StagingQuotaError(MultipartError):
    """The request is valid, but the bounded upload staging area is full."""


def _remove_entry(path: str) -> bool:
    """Remove one direct staging child without following a planted symlink."""
    try:
        if os.path.islink(path) or not os.path.isdir(path):
            os.unlink(path)
        else:
            shutil.rmtree(path)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def _tree_size(path: str) -> int:
    total = 0
    try:
        entries = list(os.scandir(path))
    except OSError:
        return 0
    for entry in entries:
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if entry.is_file(follow_symlinks=False):
            total += max(0, info.st_size)
        elif entry.is_dir(follow_symlinks=False):
            total += _tree_size(entry.path)
    return total


def _metadata_for_gc(directory: str) -> tuple[Optional[str], float]:
    """Return (status, last_activity) without trusting paths from metadata."""
    metadata_path = _metadata_path(directory)
    try:
        info = os.stat(metadata_path, follow_symlinks=False)
        if not os.path.isfile(metadata_path) or os.path.islink(metadata_path):
            raise OSError("unsafe metadata")
        with open(metadata_path, "r") as source:
            value = json.load(source)
        if not isinstance(value, dict):
            raise ValueError("metadata is not an object")
        timestamp = value.get("updated_at", value.get("created_at", info.st_mtime))
        return value.get("status"), float(timestamp)
    except (OSError, TypeError, ValueError):
        try:
            return None, os.stat(directory, follow_symlinks=False).st_mtime
        except OSError:
            return None, 0.0


def _gc_expired_locked(root: str, *, now: float,
                       include_active: bool) -> list[str]:
    removed = []
    try:
        entries = list(os.scandir(root))
    except OSError:
        return removed
    ttl = max(1, int(paths.UPLOAD_TTL_SEC))
    for entry in entries:
        if not entry.is_dir(follow_symlinks=False):
            # The upload root owns only private directories. Quarantine neither
            # storage nor traversal surprises: remove direct non-directories.
            if entry.is_symlink():
                if _remove_entry(entry.path):
                    removed.append(entry.name)
            continue
        status, timestamp = _metadata_for_gc(entry.path)
        if status in _ACTIVE_STATUSES and not include_active:
            continue
        age = max(0.0, now - timestamp)
        if age >= ttl:
            if _remove_entry(entry.path):
                removed.append(entry.name)
    return removed


def gc_expired(*, now: Optional[float] = None,
               include_active: bool = False) -> list[str]:
    """Delete expired upload directories and return their direct-child names.

    Runtime collection preserves queued/running installs.  Service startup may
    pass ``include_active=True`` because no callback from the previous process
    can still own those records.
    """
    with _UPLOAD_LOCK:
        root = paths.ensure_uploads()
        return _gc_expired_locked(
            root, now=time.time() if now is None else float(now),
            include_active=include_active)


def _enforce_staging_quota(root: str, incoming_length: int) -> None:
    try:
        entries = [entry for entry in os.scandir(root)
                   if entry.is_dir(follow_symlinks=False)]
    except OSError as exc:
        raise StagingQuotaError(f"cannot inspect upload staging area: {exc}") from exc
    if len(entries) >= max(1, int(paths.MAX_STAGED_UPLOADS)):
        raise StagingQuotaError(
            "upload staging count limit reached: %d >= %d" %
            (len(entries), paths.MAX_STAGED_UPLOADS))
    used = _tree_size(root)
    total_cap = max(1, int(paths.MAX_UPLOAD_STAGING_BYTES))
    if used + incoming_length > total_cap:
        raise StagingQuotaError(
            "upload staging byte limit exceeded: %d + %d > %d" %
            (used, incoming_length, total_cap))
    try:
        free = shutil.disk_usage(root).free
    except OSError as exc:
        raise StagingQuotaError(f"cannot determine upload free space: {exc}") from exc
    reserve = max(0, int(paths.MIN_UPLOAD_FREE_BYTES))
    if free - incoming_length < reserve:
        raise StagingQuotaError(
            "insufficient free space for upload while preserving device reserve")


def _header(value: str, name: str) -> Message:
    message = Message()
    message[name] = value
    return message


def _boundary(content_type: str) -> bytes:
    message = _header(content_type or "", "content-type")
    if message.get_content_type().lower() != "multipart/form-data":
        raise MultipartError("Content-Type must be multipart/form-data")
    value = message.get_param("boundary", header="content-type")
    if not isinstance(value, str) or not value:
        raise MultipartError("multipart boundary is missing")
    try:
        raw = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise MultipartError("multipart boundary must be ASCII") from exc
    if len(raw) > 70 or any(ch < 0x21 or ch > 0x7e for ch in raw):
        raise MultipartError("multipart boundary is invalid")
    return raw


class _LimitedMultipartReader:
    """Small scanner that never buffers a complete binary part."""

    def __init__(self, stream: BinaryIO, length: int):
        self.stream = stream
        self.remaining = int(length)
        self.buffer = bytearray()

    def _more(self) -> bool:
        if self.remaining <= 0:
            return False
        chunk = self.stream.read(min(CHUNK, self.remaining))
        if not chunk:
            raise MultipartError("multipart request body is truncated")
        self.remaining -= len(chunk)
        self.buffer.extend(chunk)
        return True

    def _ensure(self, count: int) -> None:
        while len(self.buffer) < count:
            if not self._more():
                raise MultipartError("multipart request body is truncated")

    def line(self, limit: int = 8192) -> bytes:
        while True:
            index = self.buffer.find(b"\r\n")
            if index >= 0:
                if index > limit:
                    raise MultipartError("multipart header line is too long")
                value = bytes(self.buffer[:index])
                del self.buffer[:index + 2]
                return value
            if len(self.buffer) > limit:
                raise MultipartError("multipart header line is too long")
            if not self._more():
                raise MultipartError("unterminated multipart line")

    def body(self, output: BinaryIO, marker: bytes, cap: int) -> tuple[int, bool]:
        written = 0
        keep = len(marker) + 4
        while True:
            index = self.buffer.find(marker)
            if index >= 0:
                chunk = bytes(self.buffer[:index])
                if written + len(chunk) > cap:
                    raise MultipartError("multipart field exceeds its size limit")
                output.write(chunk)
                written += len(chunk)
                del self.buffer[:index + len(marker)]
                self._ensure(2)
                if self.buffer.startswith(b"--"):
                    del self.buffer[:2]
                    # RFC 2046 permits an epilogue.  Drain it without retaining
                    # it; browsers normally send exactly one trailing CRLF.
                    if len(self.buffer) < 2 and self.remaining:
                        self._more()
                    if self.buffer.startswith(b"\r\n"):
                        del self.buffer[:2]
                    while self.remaining:
                        self._more()
                        self.buffer.clear()
                    return written, True
                if self.buffer.startswith(b"\r\n"):
                    del self.buffer[:2]
                    return written, False
                raise MultipartError("malformed multipart boundary suffix")

            if len(self.buffer) > keep:
                count = len(self.buffer) - keep
                if written + count > cap:
                    raise MultipartError("multipart field exceeds its size limit")
                output.write(self.buffer[:count])
                written += count
                del self.buffer[:count]
            if not self._more():
                raise MultipartError("multipart body has no closing boundary")


def _disposition(value: str) -> tuple[str, Optional[str]]:
    message = _header(value or "", "content-disposition")
    if message.get_content_disposition() != "form-data":
        raise MultipartError("multipart part must use form-data disposition")
    name = message.get_param("name", header="content-disposition")
    filename = message.get_filename()
    if not isinstance(name, str) or not name:
        raise MultipartError("multipart part has no name")
    return name, filename


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _metadata_path(directory: str) -> str:
    return os.path.join(directory, "upload.json")


def _save_metadata(directory: str, value: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".upload-meta.", dir=directory)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(value, output, indent=2, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, _metadata_path(directory))
        temporary = None
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _receive_locked(stream: BinaryIO, content_length: int, content_type: str) -> dict:
    """Stream ``package`` and optional ``signature`` parts into a private dir."""
    try:
        length = int(content_length)
    except (TypeError, ValueError) as exc:
        raise MultipartError("Content-Length is required") from exc
    maximum = paths.MAX_PKG_BYTES + MAX_MULTIPART_OVERHEAD
    if length <= 0:
        raise MultipartError("multipart request body is empty")
    if length > maximum:
        raise MultipartError("multipart request is too large: %d > %d" %
                             (length, maximum))
    boundary = _boundary(content_type)
    reader = _LimitedMultipartReader(stream, length)
    expected = b"--" + boundary
    if reader.line() != expected:
        raise MultipartError("multipart body does not start with its boundary")

    root = paths.ensure_uploads()
    _gc_expired_locked(root, now=time.time(), include_active=False)
    _enforce_staging_quota(root, length)
    staging = tempfile.mkdtemp(prefix=".incoming-", dir=root)
    fields: dict[str, dict] = {}
    final = False
    try:
        while not final:
            headers = {}
            header_bytes = 0
            while True:
                line = reader.line()
                header_bytes += len(line) + 2
                if header_bytes > MAX_HEADER_BYTES:
                    raise MultipartError("multipart part headers are too large")
                if not line:
                    break
                if line[:1] in (b" ", b"\t") or b":" not in line:
                    raise MultipartError("malformed multipart part header")
                key, value = line.split(b":", 1)
                try:
                    headers[key.decode("ascii").strip().lower()] = \
                        value.decode("utf-8").strip()
                except UnicodeDecodeError as exc:
                    raise MultipartError("multipart headers must be UTF-8/ASCII") from exc
            name, filename = _disposition(headers.get("content-disposition", ""))
            if name not in ("package", "signature"):
                raise MultipartError("unexpected multipart field %r" % name)
            if name in fields:
                raise MultipartError("duplicate multipart field %r" % name)
            if name == "package":
                base = os.path.basename(filename or "")
                if not _PACKAGE_NAME.fullmatch(base) or base != filename:
                    raise MultipartError(
                        "package filename must be a bare <name>.tar.gz")
                target = os.path.join(staging, "package.tar.gz")
                cap = paths.MAX_PKG_BYTES
            else:
                target = os.path.join(staging, "signature.txt")
                cap = MAX_SIGNATURE_BYTES
            with open(target, "wb") as output:
                size, final = reader.body(
                    output, b"\r\n--" + boundary, cap)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(target, 0o600)
            if size <= 0:
                raise MultipartError("multipart field %r is empty" % name)
            fields[name] = {"path": target, "size": size, "filename": filename}

        if "package" not in fields:
            raise MultipartError("multipart field 'package' is required")
        if reader.remaining:
            raise MultipartError("multipart parser did not consume the request")

        signature = None
        if "signature" in fields:
            try:
                with open(fields["signature"]["path"], "rb") as source:
                    signature = source.read(MAX_SIGNATURE_BYTES + 1).decode("ascii").strip()
            except UnicodeDecodeError as exc:
                raise MultipartError("signature must contain base64 ASCII text") from exc
            if not signature:
                raise MultipartError("signature is empty")

        upload_id = uuid.uuid4().hex
        destination = os.path.join(root, upload_id)
        package = fields["package"]
        metadata = {
            "schema_version": 1,
            "upload_id": upload_id,
            "created_at": time.time(),
            "filename": package["filename"],
            "size": package["size"],
            "sha256": _sha256(package["path"]),
            "signature": signature,
            "status": "uploaded",
        }
        _save_metadata(staging, metadata)
        os.rename(staging, destination)
        staging = None
        return load(upload_id)
    finally:
        if staging and os.path.isdir(staging):
            shutil.rmtree(staging, ignore_errors=True)


def receive(stream: BinaryIO, content_length: int, content_type: str) -> dict:
    """Stream one bounded upload while atomically reserving aggregate quota."""
    # Serialising only upload receivers is intentional: it makes the quota check
    # and the subsequently consumed bytes one transaction. Other App Center
    # requests continue on independent ThreadingHTTPServer handler threads.
    with _UPLOAD_LOCK:
        return _receive_locked(stream, content_length, content_type)


def _directory(upload_id: str) -> str:
    if not isinstance(upload_id, str) or not _UPLOAD_ID.fullmatch(upload_id):
        raise ValueError("invalid upload_id")
    return os.path.join(paths.uploads_dir(), upload_id)


def load(upload_id: str) -> dict:
    directory = _directory(upload_id)
    try:
        with open(_metadata_path(directory)) as source:
            metadata = json.load(source)
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise ValueError("unknown upload_id") from exc
    if not isinstance(metadata, dict) or metadata.get("upload_id") != upload_id:
        raise ValueError("invalid upload metadata")
    metadata = dict(metadata)
    metadata["package_path"] = os.path.join(directory, "package.tar.gz")
    return metadata


def update(upload_id: str, **fields) -> dict:
    metadata = load(upload_id)
    directory = _directory(upload_id)
    metadata.pop("package_path", None)
    metadata.update(fields)
    metadata["updated_at"] = time.time()
    _save_metadata(directory, metadata)
    return load(upload_id)


def verify(upload_id: str) -> dict:
    """Re-bind finalize to the root-owned bytes that were preflighted."""
    metadata = load(upload_id)
    package = metadata["package_path"]
    try:
        size = os.path.getsize(package)
    except OSError as exc:
        raise ValueError("uploaded package is missing") from exc
    if size != metadata.get("size") or _sha256(package) != metadata.get("sha256"):
        raise ValueError("uploaded package changed after preflight")
    return metadata


def remove(upload_id: str) -> bool:
    directory = _directory(upload_id)
    with _UPLOAD_LOCK:
        return _remove_entry(directory)

"""Bounded streaming multipart uploads for the Web App Center API."""
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager, nullcontext
from email.message import Message
from typing import BinaryIO, Optional

from . import paths


MAX_MULTIPART_OVERHEAD = 256 * 1024
MAX_SIGNATURE_BYTES = 16 * 1024
MAX_HEADER_BYTES = 32 * 1024
CHUNK = 64 * 1024
_UPLOAD_ID = re.compile(r"[0-9a-f]{32}\Z")
# The public spelling is deliberately ECMAScript-compatible so the Web client
# can apply the exact same filename gate.  Python still uses ``fullmatch`` as
# the authoritative server-side check.
PACKAGE_FILENAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.tar\.gz$"
_PACKAGE_NAME = re.compile(PACKAGE_FILENAME_PATTERN)
_UPLOAD_LOCK = threading.RLock()
_RECORD_LOCKS: dict[str, threading.RLock] = {}
_LIVE_INCOMING: set[str] = set()
_ORPHANED_ACTIVE: set[str] = set()
_ACTIVE_STATUSES = frozenset(("receiving", "install_queued", "installing"))
_CANCELLABLE_STATUSES = frozenset((
    "uploaded", "preflighted", "rejected", "failed", "installed",
    "interrupted",
))
_UNSUPPORTED_DIR_FSYNC = frozenset(filter(None, (
    errno.EINVAL,
    getattr(errno, "ENOTSUP", None),
    getattr(errno, "EOPNOTSUPP", None),
)))


class MultipartError(ValueError):
    pass


class StagingQuotaError(MultipartError):
    """The request is valid, but the bounded upload staging area is full."""


class UploadConflictError(RuntimeError):
    """An upload exists but its current state cannot safely be deleted."""


def _fsync_dir(path: str) -> None:
    """Flush one directory entry update when the backing filesystem permits."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError as exc:
        if exc.errno in _UNSUPPORTED_DIR_FSYNC:
            return
        raise
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno not in _UNSUPPORTED_DIR_FSYNC:
            raise
    finally:
        os.close(fd)


def _ensure_upload_root() -> str:
    """Create the private root and durably publish its first parent entry."""
    stage = paths.APPSTAGE_DIR
    stage_existed = os.path.lexists(stage)
    paths.ensure_appstage()
    if not stage_existed:
        _fsync_dir(os.path.dirname(stage) or ".")
    root = paths.uploads_dir()
    existed = os.path.lexists(root)
    root = paths.ensure_uploads()
    if not existed:
        _fsync_dir(os.path.dirname(root))
    return root


@contextmanager
def _record_guard(upload_id: str):
    """Serialize operations on one published upload without blocking peers."""
    with _UPLOAD_LOCK:
        lock = _RECORD_LOCKS.setdefault(upload_id, threading.RLock())
    lock.acquire()
    try:
        yield
    finally:
        lock.release()
        # Upload ids are never reused.  Forget absent records so invalid-id probes
        # and successfully removed uploads cannot grow this process-local map.
        if not os.path.lexists(os.path.join(paths.uploads_dir(), upload_id)):
            with _UPLOAD_LOCK:
                if _RECORD_LOCKS.get(upload_id) is lock:
                    _RECORD_LOCKS.pop(upload_id, None)
                    _ORPHANED_ACTIVE.discard(upload_id)


def _remove_entry(path: str) -> bool:
    """Remove one direct staging child without following a planted symlink."""
    try:
        if os.path.islink(path) or not os.path.isdir(path):
            os.unlink(path)
        else:
            shutil.rmtree(path)
        _fsync_dir(os.path.dirname(path))
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


def _accounted_size(directory: str, *, actual: Optional[int] = None) -> int:
    """Return committed bytes or a receiving request's full reservation."""
    actual = _tree_size(directory) if actual is None else max(0, int(actual))
    try:
        metadata_path = _metadata_path(directory)
        info = os.stat(metadata_path, follow_symlinks=False)
        if (not os.path.isfile(metadata_path) or os.path.islink(metadata_path)
                or info.st_size <= 0):
            return actual
        with open(metadata_path, "r") as source:
            value = json.load(source)
        if not isinstance(value, dict):
            return actual
        reserved = value.get("reserved_bytes")
        if (value.get("status") == "receiving"
                and isinstance(reserved, int) and not isinstance(reserved, bool)
                and reserved > 0):
            return max(actual, reserved)
    except (OSError, TypeError, ValueError):
        pass
    return actual


def _staging_accounting(root: str):
    try:
        entries = [entry for entry in os.scandir(root)
                   if entry.is_dir(follow_symlinks=False)]
    except OSError as exc:
        raise StagingQuotaError(
            f"cannot inspect upload staging area: {exc}") from exc
    accounting = []
    for entry in entries:
        actual = _tree_size(entry.path)
        accounted = _accounted_size(entry.path, actual=actual)
        accounting.append((entry, actual, accounted))
    return accounting


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
            # The root owns only upload directories.  A crash/corruption-created
            # file or a planted symlink must not consume quota forever.
            if _remove_entry(entry.path):
                removed.append(entry.name)
            continue
        incoming = entry.name.startswith(".incoming-")
        if incoming:
            with _UPLOAD_LOCK:
                live = entry.name in _LIVE_INCOMING
            if live and not include_active:
                continue
        guard = (_record_guard(entry.name)
                 if _UPLOAD_ID.fullmatch(entry.name) else nullcontext())
        with guard:
            if not os.path.isdir(entry.path) or os.path.islink(entry.path):
                continue
            status, timestamp = _metadata_for_gc(entry.path)
            with _UPLOAD_LOCK:
                known_orphan = entry.name in _ORPHANED_ACTIVE
            if (not incoming and not known_orphan
                    and status in _ACTIVE_STATUSES and not include_active):
                continue
            age = max(0.0, now - timestamp)
            if known_orphan or age >= ttl:
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
    root = _ensure_upload_root()
    return _gc_expired_locked(
        root, now=time.time() if now is None else float(now),
        include_active=include_active)


def _enforce_staging_quota(root: str, incoming_length: int) -> None:
    accounting = _staging_accounting(root)
    if len(accounting) >= max(1, int(paths.MAX_STAGED_UPLOADS)):
        raise StagingQuotaError(
            "upload staging count limit reached: %d >= %d" %
            (len(accounting), paths.MAX_STAGED_UPLOADS))
    used = sum(accounted for _, _, accounted in accounting)
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
    # disk_usage() already reflects bytes written so far, but not the unpaid
    # portion of another handler's full reservation. Deduct that outstanding
    # balance as well or two slow concurrent uploads can jointly consume the
    # device free-space reserve even though each check passed independently.
    outstanding = sum(max(0, accounted - actual)
                      for _, actual, accounted in accounting)
    if free - outstanding - incoming_length < reserve:
        raise StagingQuotaError(
            "insufficient free space for upload while preserving device reserve")


def _enforce_metadata_quota(root: str, directory: str, metadata: dict,
                            payload_size: int) -> None:
    """Reserve an atomic metadata rewrite and its final staging growth."""
    accounting = _staging_accounting(root)
    target = next((item for item in accounting
                   if os.path.abspath(item[0].path)
                   == os.path.abspath(directory)), None)
    if target is None:
        raise ValueError("unknown upload_id")
    _, old_actual, old_accounted = target
    try:
        old_metadata_size = os.stat(
            _metadata_path(directory), follow_symlinks=False).st_size
    except OSError as exc:
        raise ValueError("unknown upload_id") from exc
    new_actual = max(0, old_actual - old_metadata_size) + payload_size
    reserved = metadata.get("reserved_bytes")
    if (metadata.get("status") == "receiving"
            and isinstance(reserved, int) and not isinstance(reserved, bool)
            and reserved > 0):
        new_accounted = max(new_actual, reserved)
    else:
        new_accounted = new_actual

    used = sum(accounted for _, _, accounted in accounting)
    projected = used - old_accounted + new_accounted
    total_cap = max(1, int(paths.MAX_UPLOAD_STAGING_BYTES))
    if projected > total_cap:
        raise StagingQuotaError(
            "upload staging byte limit exceeded after metadata update: "
            "%d > %d" % (projected, total_cap))

    try:
        free = shutil.disk_usage(root).free
    except OSError as exc:
        raise StagingQuotaError(
            f"cannot determine upload free space: {exc}") from exc
    reserve = max(0, int(paths.MIN_UPLOAD_FREE_BYTES))
    current_outstanding = sum(max(0, accounted - actual)
                              for _, actual, accounted in accounting)
    projected_outstanding = (
        current_outstanding - max(0, old_accounted - old_actual)
        + max(0, new_accounted - new_actual))
    final_free = free - (new_actual - old_actual)
    # Atomic replace temporarily needs the complete new upload.json alongside
    # the old one; afterwards the final bytes and all unpaid reservations must
    # still preserve the device floor.
    if (free - current_outstanding - payload_size < reserve
            or final_free - projected_outstanding < reserve):
        raise StagingQuotaError(
            "insufficient free space for upload metadata while preserving "
            "device reserve")


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

    def __init__(self, stream: BinaryIO, length: int, *,
                 deadline: Optional[float] = None):
        self.stream = stream
        self.remaining = int(length)
        self.buffer = bytearray()
        self.deadline = deadline

    def _check_deadline(self) -> None:
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise MultipartError("multipart upload timed out")

    def _more(self) -> bool:
        if self.remaining <= 0:
            return False
        self._check_deadline()
        chunk = self.stream.read(min(CHUNK, self.remaining))
        self._check_deadline()
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


def _encode_metadata(value: dict) -> bytes:
    return json.dumps(value, indent=2, sort_keys=True).encode("utf-8")


def _save_metadata(directory: str, value: dict, *,
                   encoded: Optional[bytes] = None) -> None:
    payload = _encode_metadata(value) if encoded is None else encoded
    fd, temporary = tempfile.mkstemp(prefix=".upload-meta.", dir=directory)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, _metadata_path(directory))
        temporary = None
        _fsync_dir(directory)
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _request_shape(content_length: int, content_type: str) -> tuple[int, bytes]:
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
    return length, _boundary(content_type)


def _reserve_upload(length: int, *, source: str,
                    channel: str) -> tuple[str, str, str, float]:
    """Atomically reserve count/bytes, then return a private incoming path."""
    root = _ensure_upload_root()
    with _UPLOAD_LOCK:
        _enforce_staging_quota(root, length)
        while True:
            upload_id = uuid.uuid4().hex
            staging = os.path.join(root, ".incoming-" + upload_id)
            destination = os.path.join(root, upload_id)
            if not os.path.lexists(staging) and not os.path.lexists(destination):
                break
        os.mkdir(staging, mode=0o700)
        created_at = time.time()
        try:
            # Persist the declared request length, not the bytes written so far.
            # Concurrent quota checks therefore account for this upload even
            # while its handler is blocked waiting for the next network chunk.
            _save_metadata(staging, {
                "schema_version": 1,
                "upload_id": upload_id,
                "created_at": created_at,
                "updated_at": created_at,
                "source": source,
                "channel": channel,
                "status": "receiving",
                "reserved_bytes": length,
            })
            _LIVE_INCOMING.add(os.path.basename(staging))
            _fsync_dir(root)
        except BaseException:
            if not _remove_entry(staging):
                if os.path.lexists(staging):
                    _ORPHANED_ACTIVE.add(os.path.basename(staging))
            raise
    return root, upload_id, staging, created_at


def _receive_reserved(stream: BinaryIO, length: int, boundary: bytes, *,
                      root: str, upload_id: str, staging: str,
                      created_at: float, source: str, channel: str,
                      deadline: Optional[float]) -> dict:
    """Stream one already-accounted request without holding the global lock."""
    fields: dict[str, dict] = {}
    final = False
    try:
        reader = _LimitedMultipartReader(stream, length, deadline=deadline)
        expected = b"--" + boundary
        if reader.line() != expected:
            raise MultipartError(
                "multipart body does not start with its boundary")

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
        if "signature" in fields:
            expected_signature = fields["package"]["filename"] + ".sig"
            if fields["signature"]["filename"] != expected_signature:
                raise MultipartError(
                    "signature filename must match package filename: %s" %
                    expected_signature)
        if reader.remaining:
            raise MultipartError("multipart parser did not consume the request")

        signature = None
        if "signature" in fields:
            try:
                with open(fields["signature"]["path"], "rb") as signature_file:
                    signature = signature_file.read(
                        MAX_SIGNATURE_BYTES + 1).decode("ascii").strip()
            except UnicodeDecodeError as exc:
                raise MultipartError("signature must contain base64 ASCII text") from exc
            if not signature:
                raise MultipartError("signature is empty")

        destination = os.path.join(root, upload_id)
        package = fields["package"]
        metadata = {
            "schema_version": 1,
            "upload_id": upload_id,
            "created_at": created_at,
            "updated_at": time.time(),
            # These values are minted by the HTTP route, never accepted as
            # multipart fields.  Finalize uses them to distinguish a package
            # that crossed the authenticated same-origin Web route from a
            # direct/API upload whose caller merely *claims* to be local.
            "source": source,
            "channel": channel,
            "filename": package["filename"],
            "size": package["size"],
            "sha256": _sha256(package["path"]),
            "signature": signature,
            "status": "uploaded",
        }
        _save_metadata(staging, metadata)
        with _UPLOAD_LOCK:
            if os.path.islink(staging) or not os.path.isdir(staging):
                raise MultipartError("upload reservation disappeared")
            if os.path.lexists(destination):
                raise MultipartError("upload id collision")
            os.rename(staging, destination)
            staging = None
            _fsync_dir(root)
        return load(upload_id)
    finally:
        with _UPLOAD_LOCK:
            _LIVE_INCOMING.discard(".incoming-" + upload_id)
        if staging:
            if not _remove_entry(staging):
                with _UPLOAD_LOCK:
                    if os.path.lexists(staging):
                        _ORPHANED_ACTIVE.add(os.path.basename(staging))


def receive(stream: BinaryIO, content_length: int, content_type: str, *,
            source: str = "unspecified", channel: str = "direct",
            total_timeout: Optional[float] = None) -> dict:
    """Stream one bounded upload while atomically reserving aggregate quota.

    ``source`` and ``channel`` are trusted control-plane metadata supplied by
    the route handler, not form fields.  Keep conservative defaults for local
    tools/tests that call this storage primitive directly; such records are not
    eligible for the Web-only unsigned-install exception.
    """
    for label, value in (("source", source), ("channel", channel)):
        if (not isinstance(value, str) or not value
                or len(value) > 64
                or re.fullmatch(r"[a-z0-9][a-z0-9._-]*", value) is None):
            raise MultipartError("invalid upload %s" % label)
    length, boundary = _request_shape(content_length, content_type)
    timeout = (paths.UPLOAD_TOTAL_TIMEOUT_SEC
               if total_timeout is None else total_timeout)
    try:
        timeout = max(0.001, float(timeout))
    except (TypeError, ValueError) as exc:
        raise MultipartError("invalid multipart upload timeout") from exc
    # Opportunistic runtime GC happens before the short reservation transaction.
    # It never removes a receiving/queued/running record.
    gc_expired()
    root, upload_id, staging, created_at = _reserve_upload(
        length, source=source, channel=channel)
    return _receive_reserved(
        stream, length, boundary, root=root, upload_id=upload_id,
        staging=staging, created_at=created_at, source=source,
        channel=channel, deadline=time.monotonic() + timeout)


def _directory(upload_id: str) -> str:
    if not isinstance(upload_id, str) or not _UPLOAD_ID.fullmatch(upload_id):
        raise ValueError("invalid upload_id")
    return os.path.join(paths.uploads_dir(), upload_id)


def _load_unlocked(upload_id: str) -> dict:
    directory = _directory(upload_id)
    metadata_path = _metadata_path(directory)
    if (os.path.islink(directory) or not os.path.isdir(directory)
            or os.path.islink(metadata_path)):
        raise ValueError("unknown upload_id")
    try:
        with open(metadata_path) as source:
            metadata = json.load(source)
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise ValueError("unknown upload_id") from exc
    if not isinstance(metadata, dict) or metadata.get("upload_id") != upload_id:
        raise ValueError("invalid upload metadata")
    metadata = dict(metadata)
    metadata["package_path"] = os.path.join(directory, "package.tar.gz")
    return metadata


def load(upload_id: str) -> dict:
    _directory(upload_id)
    with _record_guard(upload_id):
        return _load_unlocked(upload_id)


def update(upload_id: str, **fields) -> dict:
    # Status transitions participate in the same per-record lock as
    # cancellation.  In
    # particular, a preflighted upload must not be deleted between finalize's
    # transition to ``install_queued`` and the cancellation state check.
    _directory(upload_id)
    with _record_guard(upload_id):
        metadata = _load_unlocked(upload_id)
        directory = _directory(upload_id)
        metadata.pop("package_path", None)
        metadata.update(fields)
        metadata["updated_at"] = time.time()
        encoded = _encode_metadata(metadata)
        # A preflight can expand a highly compressed manifest into a much larger
        # JSON projection. Serialize quota decisions with incoming reservations
        # so that growth cannot bypass either the staging cap or free-space
        # floor after the network body has already been accepted.
        with _UPLOAD_LOCK:
            _enforce_metadata_quota(
                paths.uploads_dir(), directory, metadata, len(encoded))
            _save_metadata(directory, metadata, encoded=encoded)
        return _load_unlocked(upload_id)


def verify(upload_id: str) -> dict:
    """Re-bind finalize to the root-owned bytes that were preflighted."""
    _directory(upload_id)
    with _record_guard(upload_id):
        metadata = _load_unlocked(upload_id)
        package = metadata["package_path"]
        try:
            size = os.path.getsize(package)
        except OSError as exc:
            raise ValueError("uploaded package is missing") from exc
        if (size != metadata.get("size")
                or _sha256(package) != metadata.get("sha256")):
            raise ValueError("uploaded package changed after preflight")
        return metadata


def remove(upload_id: str) -> bool:
    directory = _directory(upload_id)
    with _record_guard(upload_id):
        return _remove_entry(directory)


def mark_orphaned(upload_id: str) -> None:
    """Let runtime GC retry a record known to have no callback owner."""
    directory = _directory(upload_id)
    with _UPLOAD_LOCK:
        if os.path.lexists(directory):
            _ORPHANED_ACTIVE.add(upload_id)
        else:
            _ORPHANED_ACTIVE.discard(upload_id)


def cancel(upload_id: str) -> dict:
    """Idempotently delete one inactive browser upload.

    A syntactically valid but absent id is a successful no-op.  The status read
    and directory removal are one per-record transaction, and unknown or active
    states fail closed instead of deleting bytes an install worker may still
    own.  The server additionally serializes this call with upload finalize so
    ``preflighted -> install_queued`` cannot race cancellation.
    """
    directory = _directory(upload_id)
    with _record_guard(upload_id):
        if not os.path.lexists(directory):
            return {
                "upload_id": upload_id,
                "deleted": False,
                "state": "absent",
            }
        if os.path.islink(directory) or not os.path.isdir(directory):
            raise UploadConflictError(
                "upload staging entry is not a safe directory")

        metadata = _load_unlocked(upload_id)
        status = metadata.get("status")
        if status in _ACTIVE_STATUSES:
            raise UploadConflictError(
                "upload is active (%s) and cannot be deleted" % status)
        if status not in _CANCELLABLE_STATUSES:
            raise UploadConflictError(
                "upload has unknown state %r and cannot be safely deleted" % status)
        if not _remove_entry(directory):
            raise OSError("cannot delete upload staging directory")
        return {
            "upload_id": upload_id,
            "deleted": True,
            "state": "deleted",
            "previous_status": status,
        }


def recover_startup() -> dict[str, list[str]]:
    """Reclaim upload work that cannot have a live owner after a restart.

    Private incoming directories are never published and are removed
    unconditionally.  Published records left in an active state are first
    persisted as ``interrupted`` so a failed cleanup remains cancellable and
    ordinary TTL collection can reclaim it on a later pass.
    """
    root = _ensure_upload_root()
    with _UPLOAD_LOCK:
        _LIVE_INCOMING.clear()
        _ORPHANED_ACTIVE.clear()
    removed: list[str] = []
    interrupted: list[str] = []
    try:
        entries = list(os.scandir(root))
    except OSError:
        return {"removed": removed, "interrupted": interrupted}

    for entry in entries:
        if entry.name.startswith(".incoming-"):
            with _UPLOAD_LOCK:
                _ORPHANED_ACTIVE.add(entry.name)
            if _remove_entry(entry.path):
                removed.append(entry.name)
                with _UPLOAD_LOCK:
                    _ORPHANED_ACTIVE.discard(entry.name)
            elif not os.path.lexists(entry.path):
                with _UPLOAD_LOCK:
                    _ORPHANED_ACTIVE.discard(entry.name)
            continue
        if _UPLOAD_ID.fullmatch(entry.name) is None:
            continue
        if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
            if _remove_entry(entry.path):
                removed.append(entry.name)
            continue
        with _record_guard(entry.name):
            try:
                metadata = _load_unlocked(entry.name)
            except ValueError:
                # A valid-looking id with no parseable root-owned metadata can
                # neither be finalized nor have a live owner after restart.
                # Reclaim it immediately; if deletion transiently fails, mark
                # it so the following runtime GC retries without waiting TTL.
                with _UPLOAD_LOCK:
                    _ORPHANED_ACTIVE.add(entry.name)
                if _remove_entry(entry.path):
                    removed.append(entry.name)
                continue
            if metadata.get("status") not in _ACTIVE_STATUSES:
                continue
            with _UPLOAD_LOCK:
                _ORPHANED_ACTIVE.add(entry.name)
            interrupted.append(entry.name)
            metadata.pop("package_path", None)
            metadata.update({
                "status": "interrupted",
                "updated_at": time.time(),
                "error": "appmgr restarted before upload processing completed",
            })
            try:
                _save_metadata(entry.path, metadata)
            except OSError:
                # Deletion is still safe: after the single-instance daemon took
                # over there cannot be a callback owning this record. If it also
                # fails, the process-local orphan mark lets runtime TTL GC retry
                # instead of preserving the stale active status forever.
                pass
            if _remove_entry(entry.path):
                removed.append(entry.name)
    return {"removed": removed, "interrupted": interrupted}

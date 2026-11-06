"""Bounded, restart-safe capture delivery. No camera or native SDK imports."""
from __future__ import annotations

import hashlib
import errno
import json
import math
import os
from pathlib import Path
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


APP_VERSION = "1.1.1"
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_SPOOL_BYTES = 512 * 1024 * 1024
MAX_SPOOL_IMAGES = 10000
MAX_HEADER_BYTES = 2048


class DeliveryError(RuntimeError):
    """A sanitized failure: response bodies, URLs and credentials stay private."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DeliveryError("server_redirect_refused")


def normalize_server(value):
    url = urllib.parse.urlsplit(str(value).strip())
    if (url.scheme not in ("http", "https") or not url.hostname
            or url.username or url.password or url.query or url.fragment):
        raise ValueError("server_url must be an HTTP(S) origin or base path")
    try:
        url.port
    except ValueError as exc:
        raise ValueError("server_url has an invalid port") from exc
    return urllib.parse.urlunsplit((url.scheme, url.netloc, url.path.rstrip("/"), "", ""))


class EdgeClient:
    max_response_bytes = 65536

    def __init__(self, server_url, token, timeout=3):
        self.server_url = normalize_server(server_url)
        self._token = str(token).strip()
        prefix = "cvsprout_device_"
        if (not self._token.startswith(prefix) or len(self._token) <= len(prefix)
                or len(self._token) > 256
                or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
                       for c in self._token)):
            raise ValueError("device_token_invalid: copy the complete cvsprout_device_... token "
                             "from CVSprout Edge Devices; do not use a device ID or administrator token")
        self.timeout = timeout
        # Explicitly avoid environment proxies on a device; they may route a
        # local camera credential to an unrelated proxy. TLS stays verified.
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, path, payload=None, content_type="application/json"):
        headers = {"Authorization": "Bearer " + self._token,
                   "Accept": "application/json", "User-Agent": "CVSprout-Capture/" + APP_VERSION}
        if payload is not None:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(self.server_url + "/api/edge/v1/" + path,
                                     data=payload, headers=headers)
        try:
            with self._opener.open(req, timeout=self.timeout) as response:
                data = response.read(self.max_response_bytes + 1)
                if len(data) > self.max_response_bytes:
                    raise DeliveryError("server_response_too_large")
                result = json.loads(data)
                if not isinstance(result, dict):
                    raise DeliveryError("invalid_server_response")
                return result
        except urllib.error.HTTPError as exc:
            raise DeliveryError("http_%s" % exc.code) from None
        except urllib.error.URLError as exc:
            reason = exc.reason
            code = ("server_timeout" if isinstance(reason, TimeoutError) else
                    "server_connection_refused" if isinstance(reason, ConnectionRefusedError) else
                    "server_dns_failed" if type(reason).__name__ == "gaierror" else
                    "server_tls_verification_failed" if type(reason).__name__ == "SSLCertVerificationError" else
                    "server_unreachable_or_invalid_response")
            raise DeliveryError(code) from None
        except TimeoutError:
            raise DeliveryError("server_timeout") from None
        except (OSError, ValueError):
            raise DeliveryError("server_unreachable_or_invalid_response") from None

    def configuration(self):
        return self.request("config")

    def heartbeat(self, values):
        return self.request("heartbeat", json.dumps(values).encode())

    def upload(self, capture):
        boundary = "cvsprout-" + uuid.uuid4().hex
        chunks = []
        for key in ("capture_id", "captured_at", "batch_id", "model_release"):
            value = capture.get(key)
            if value is not None:
                chunks.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\""
                               "\r\n\r\n%s\r\n" % (boundary, key, value)).encode())
        chunks.extend([
            ("--%s\r\nContent-Disposition: form-data; name=\"file\"; "
             "filename=\"capture.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n" % boundary).encode(),
            capture["jpeg"], ("\r\n--%s--\r\n" % boundary).encode()])
        return self.request("captures", b"".join(chunks),
                            "multipart/form-data; boundary=" + boundary)


class Spool:
    """A bounded, single-writer file queue, with one atomic record per capture.

    AppMgr permits one app instance; a lock serializes its sampling and upload
    threads. An index rebuilt on startup avoids scanning thousands of files on
    every frame. Each immutable record contains metadata and JPEG bytes together,
    so a crash cannot leave a published image without its delivery identity.
    """
    def __init__(self, path, max_bytes=128 * 1024 * 1024, max_images=1000):
        self.path = Path(path)
        self.max_bytes = min(int(max_bytes), MAX_SPOOL_BYTES)
        self.max_images = min(int(max_images), MAX_SPOOL_IMAGES)
        if self.max_bytes < 1 or self.max_images < 1:
            raise ValueError("spool limits must be positive")
        self.lock = threading.RLock()
        self._records = {}
        self._bytes = 0
        self._sequence = 0
        if self.path.is_symlink():
            raise DeliveryError("capture_spool_invalid_directory")
        missing_directories = []
        ancestor = self.path
        while not ancestor.exists():
            missing_directories.append(ancestor)
            ancestor = ancestor.parent
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        for directory in missing_directories:
            os.chmod(directory, 0o700)
        os.chmod(self.path, 0o700)
        temporary = []
        for entry in self.path.iterdir():
            if (entry.name.startswith(".pending-") and entry.name.endswith(".tmp")
                    and len(entry.name) == 45
                    and all(c in "0123456789abcdef" for c in entry.name[9:-4])):
                if not stat.S_ISREG(entry.lstat().st_mode):
                    raise DeliveryError("capture_spool_corrupt")
                temporary.append(entry)
                continue
            record = self._read_record(entry, payload=False)
            capture_id = record["capture_id"]
            if capture_id in self._records:
                raise DeliveryError("capture_spool_corrupt")
            self._records[capture_id] = (entry, record)
            self._bytes += record["size"]
            self._sequence = max(self._sequence, record["sequence"])
            os.chmod(entry, 0o600)
        if self._bytes > MAX_SPOOL_BYTES or len(self._records) > MAX_SPOOL_IMAGES:
            raise DeliveryError("capture_spool_exceeds_storage_limit")
        # Only unpublished temporary records are discarded. Any corrupt
        # published record above stops startup and leaves every file intact.
        for entry in temporary:
            entry.unlink()
        self._sync_directory()
        # On first launch both appdata/<id> and capture-spool can be new.
        # Persist each new directory's entry in its parent, up to the existing
        # ancestor; syncing only capture-spool's immediate parent is insufficient.
        for directory in missing_directories:
            parent_fd = os.open(directory.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)

    def _sync_directory(self):
        fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _text(value, maximum, optional=False):
        return ((optional and value is None) or
                (isinstance(value, str) and 0 < len(value) <= maximum
                 and all(ord(char) >= 32 and ord(char) != 127 for char in value)))

    @staticmethod
    def _filename(record):
        return "%020d-%s.capture" % (record["sequence"], record["capture_id"])

    def _read_record(self, path, payload):
        try:
            if not stat.S_ISREG(path.lstat().st_mode):
                raise ValueError("record is not a regular file")
            with path.open("rb") as stream:
                header = stream.readline(MAX_HEADER_BYTES + 1)
                if len(header) > MAX_HEADER_BYTES or not header.endswith(b"\n"):
                    raise ValueError("invalid record header")
                record = json.loads(header)
                if (not isinstance(record, dict)
                        or set(record) != {"version", "sequence", "capture_id", "owner", "captured_at",
                                          "batch_id", "model_release", "size", "sha256"}
                        or type(record["version"]) is not int or record["version"] != 1
                        or type(record["sequence"]) is not int or not 0 < record["sequence"] < 10 ** 20
                        or not isinstance(record["capture_id"], str)
                        or str(uuid.UUID(record["capture_id"])) != record["capture_id"]
                        or not self._text(record["owner"], 128)
                        or not self._text(record["captured_at"], 64)
                        or not self._text(record["batch_id"], 128, optional=True)
                        or not self._text(record["model_release"], 128, optional=True)
                        or type(record["size"]) is not int or not 0 < record["size"] <= MAX_IMAGE_BYTES
                        or not isinstance(record["sha256"], str) or len(record["sha256"]) != 64
                        or any(c not in "0123456789abcdef" for c in record["sha256"])
                        or self._filename(record) != path.name
                        or os.fstat(stream.fileno()).st_size != len(header) + record["size"]):
                    raise ValueError("invalid record metadata")
                if payload:
                    jpeg = stream.read(record["size"] + 1)
                    if len(jpeg) != record["size"] or hashlib.sha256(jpeg).hexdigest() != record["sha256"]:
                        raise ValueError("invalid record payload")
                    return record, jpeg
                return record
        except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
            raise DeliveryError("capture_spool_corrupt") from None

    def stats(self):
        with self.lock:
            return {"pending_images": len(self._records), "pending_bytes": self._bytes}

    def enqueue(self, jpeg, owner, captured_at, batch_id=None, model_release=None):
        if not isinstance(jpeg, bytes) or not jpeg or len(jpeg) > MAX_IMAGE_BYTES:
            raise ValueError("invalid JPEG payload size")
        if (not self._text(owner, 128) or not self._text(captured_at, 64)
                or not self._text(batch_id, 128, optional=True)
                or not self._text(model_release, 128, optional=True)):
            raise ValueError("invalid capture metadata")
        with self.lock:
            if (len(self._records) >= min(self.max_images, MAX_SPOOL_IMAGES)
                    or self._bytes + len(jpeg) > min(self.max_bytes, MAX_SPOOL_BYTES)):
                return None
            capture_id = str(uuid.uuid4())
            self._sequence += 1
            record = {"version": 1, "sequence": self._sequence, "capture_id": capture_id,
                      "owner": owner, "captured_at": captured_at, "batch_id": batch_id,
                      "model_release": model_release, "size": len(jpeg),
                      "sha256": hashlib.sha256(jpeg).hexdigest()}
            header = json.dumps(record, ensure_ascii=True, separators=(",", ":")).encode() + b"\n"
            if len(header) > MAX_HEADER_BYTES:
                raise ValueError("capture metadata is too large")
            temporary = self.path / (".pending-" + uuid.uuid4().hex + ".tmp")
            destination = self.path / self._filename(record)
            published = False
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(header)
                    stream.write(jpeg)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, destination)
                published = True
                # Track the record before syncing the directory: if fsync fails,
                # it still occupies quota and must never become an orphan.
                self._records[capture_id] = (destination, record)
                self._bytes += len(jpeg)
                self._sync_directory()
            except OSError as exc:
                if not published:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        # Restart cleans a bounded unpublished temp record.
                        raise DeliveryError("spool_storage_error") from None
                if not published and exc.errno in (errno.ENOSPC, errno.EDQUOT):
                    return None
                # A published record remains tracked, but durability is not
                # confirmed if directory sync failed. Stop this sampling run
                # instead of reporting a rejected frame and bypassing the
                # finite session capture quota. Restart recovers this same ID.
                raise DeliveryError("spool_storage_error") from None
            return capture_id

    def oldest(self, owner):
        with self.lock:
            candidates = [item for item in self._records.values() if item[1]["owner"] == owner]
            if not candidates:
                return None
            path, indexed = min(candidates, key=lambda item: item[1]["sequence"])
            record, jpeg = self._read_record(path, payload=True)
            if record != indexed:
                raise DeliveryError("capture_spool_corrupt")
            return {**record, "jpeg": jpeg}

    def acknowledge(self, capture, receipt):
        if (not isinstance(receipt, dict) or receipt.get("received") is not True
                or receipt.get("capture_id") != capture["capture_id"]
                or type(receipt.get("asset_id")) is not int or receipt["asset_id"] <= 0):
            raise DeliveryError("capture_acknowledgement_mismatch")
        with self.lock:
            item = self._records.get(capture["capture_id"])
            if item is None:
                return  # a repeated ACK is harmless
            path, indexed = item
            if capture["owner"] != indexed["owner"]:
                raise DeliveryError("capture_acknowledgement_mismatch")
            if self._read_record(path, payload=False) != indexed:
                raise DeliveryError("capture_spool_corrupt")
            try:
                path.unlink()
                self._records.pop(capture["capture_id"])
                self._bytes -= indexed["size"]
                self._sync_directory()
            except OSError as exc:
                # A failed unlink preserves the entry. A failed directory sync
                # after unlink may replay the same server-acknowledged ID after
                # power loss, which the server accepts idempotently.
                raise DeliveryError("spool_storage_full" if exc.errno in (errno.ENOSPC, errno.EDQUOT)
                                    else "spool_storage_error") from None


class Uploader:
    """Deliver independently of frame acquisition and publish truthful progress."""
    FRAME_STALE_SECONDS = 15
    HEARTBEAT_SECONDS = 15
    SAFE_CODES = frozenset({
        "waiting_for_server_configuration", "server_configuration_received",
        "capture_enabled", "capture_paused_by_server", "server_confirmed",
        "first_frame_received", "first_image_queued", "first_image_confirmed", "recovered",
        "waiting_config", "waiting_frames", "capturing", "paused", "session_limit",
        "queue_full", "capture_error", "capture_stopped", "jpeg_encode_failed",
        "camera_frame_error", "image_exceeds_server_limit", "spool_full_capture_paused",
        "spool_storage_full", "spool_storage_error", "capture_spool_corrupt",
        "capture_acknowledgement_mismatch", "heartbeat_acknowledgement_mismatch",
        "invalid_device_configuration", "invalid_capture_interval", "invalid_capture_size_limit",
        "invalid_capture_queue_limit", "server_redirect_refused", "server_response_too_large",
        "invalid_server_response", "server_unreachable_or_invalid_response", "server_timeout",
        "server_connection_refused", "server_dns_failed", "server_tls_verification_failed",
        "upload_worker_error", "unexpected_capture_error",
    })

    def __init__(self, client, spool, clock=time.monotonic):
        self.client, self.spool, self.clock = client, spool, clock
        self.local_max_bytes, self.local_max_images = spool.max_bytes, spool.max_images
        self.stop = threading.Event()
        self.lock = threading.RLock()
        self.owner = None
        self.enabled = False
        self.interval = 10.0
        self.max_image_bytes = MAX_IMAGE_BYTES
        self.valid_until = 0.0
        self.next_config = self.next_heartbeat = self.next_upload = 0.0
        self.failures = 0
        self.uploaded = 0
        self.frames_received = self.captured_images = 0
        self.last_frame_at = None
        self.session_complete = False
        self.capture_error = ""
        self.stopped = False
        self.schema_version = 1
        self._errors = {}
        self._reported_state = None
        self._last_heartbeat = float("-inf")
        self._last_heartbeat_signature = None
        self._logs = {}
        self._configuration_signature = None
        self._heartbeat_confirmed = False
        self._config_snapshot = {}
        self.log("starting", "waiting_for_server_configuration")

    @classmethod
    def _safe_code(cls, value):
        code = str(value)
        http_code = (len(code) == 8 and code.startswith("http_") and
                     code[5:].isdigit() and 100 <= int(code[5:]) <= 599)
        return (code if code in cls.SAFE_CODES or http_code
                else "unexpected_capture_error")

    def log(self, phase, code):
        """Only our small event/error vocabulary enters firmware app logs."""
        code = self._safe_code(code)
        now = self.clock()
        with self.lock:
            previous = self._logs.get(phase)
            if previous and previous[0] == code and now - previous[1] < 60:
                return
            self._logs[phase] = (code, now)
        print("[cvsprout-capture] %s: %s" % (phase, code), flush=True)

    def _error(self, phase, error):
        code = self._safe_code(error)
        with self.lock:
            self._errors[phase] = code
        self.log(phase, code)

    def _recovered(self, phase):
        with self.lock:
            previous = self._errors.pop(phase, None)
        if previous:
            self.log(phase, "recovered")

    @property
    def last_error(self):
        with self.lock:
            return (self.capture_error or self._errors.get("upload") or
                    self._errors.get("heartbeat") or self._errors.get("configuration") or
                    self._errors.get("worker") or "")

    def frame_received(self):
        with self.lock:
            first = self.frames_received == 0
            self.frames_received += 1
            self.last_frame_at = self.clock()
        if first:
            self.log("camera", "first_frame_received")

    def capture_failed(self, code):
        with self.lock:
            self.capture_error = self._safe_code(code)
        self.log("capture", self.capture_error)

    def capture_succeeded(self, session_complete=False):
        with self.lock:
            previous = self.capture_error
            self.capture_error = ""
            self.captured_images += 1
            self.session_complete = session_complete
            first = self.captured_images == 1
        if previous:
            self.log("capture", "recovered")
        elif first:
            self.log("capture", "first_image_queued")

    def capture_state(self):
        now = self.clock()
        with self.lock:
            enabled = self.enabled and now < self.valid_until
            age = None if self.last_frame_at is None else max(0.0, now - self.last_frame_at)
            status = ("capture_stopped" if self.stopped else
                      "waiting_config" if self.owner is None or now >= self.valid_until else
                      "paused" if not enabled else
                      "session_limit" if self.session_complete else
                      "queue_full" if self.capture_error == "spool_full_capture_paused" else
                      "capture_error" if self.capture_error else
                      "waiting_frames" if age is None or age >= self.FRAME_STALE_SECONDS else
                      "capturing")
            return {"owner": self.owner, "enabled": enabled,
                    "interval": self.interval, "max_image_bytes": self.max_image_bytes,
                    "state": status, "last_frame_age_seconds": age}

    def deployment_configuration(self):
        with self.lock:
            return dict(self._config_snapshot) if self.clock() < self.valid_until else {}

    def _configuration(self, now):
        config = self.client.configuration()
        device, project = config.get("device_id"), config.get("project_id")
        if not isinstance(device, (str, int)) or not device or type(project) is not int or project <= 0:
            raise DeliveryError("invalid_device_configuration")
        interval = config.get("capture_interval_seconds", 10)
        if type(interval) not in (int, float) or not math.isfinite(interval) or not 1 <= interval <= 86400:
            raise DeliveryError("invalid_capture_interval")
        limit = config.get("max_image_bytes", MAX_IMAGE_BYTES)
        if type(limit) is not int or limit <= 0:
            raise DeliveryError("invalid_capture_size_limit")
        pending_bytes = config.get("max_pending_bytes", self.local_max_bytes)
        pending_images = config.get("max_pending_images", self.local_max_images)
        if (type(pending_bytes) is not int or pending_bytes <= 0
                or type(pending_images) is not int or pending_images <= 0):
            raise DeliveryError("invalid_capture_queue_limit")
        owner = hashlib.sha256(json.dumps([self.client.server_url, str(device), project]).encode()).hexdigest()
        with self.lock:
            first_connection = self.owner is None
            self.owner = owner
            self.enabled = config.get("capture_enabled") is True and not config.get("project_archived", False)
            self.interval = float(interval)
            self.max_image_bytes = min(limit, MAX_IMAGE_BYTES)
            self.spool.max_bytes = min(pending_bytes, self.local_max_bytes)
            self.spool.max_images = min(pending_images, self.local_max_images)
            version = config.get("heartbeat_schema_version", 1)
            self.schema_version = version if type(version) is int else 1
            # A restart must handshake; offline captures expire after 5 minutes.
            self.valid_until = now + 300
            self._config_snapshot = dict(config)
            signature = (owner, self.enabled, self.interval, self.max_image_bytes)
            changed = signature != self._configuration_signature
            self._configuration_signature = signature
        self._recovered("configuration")
        if first_connection:
            self.log("connection", "server_configuration_received")
        if changed:
            self.log("configuration", "capture_enabled" if self.enabled else "capture_paused_by_server")
        self.next_config = now + 30

    def heartbeat_payload(self):
        state = self.capture_state()
        with self.lock:
            payload = {"app_version": APP_VERSION, "state": state["state"],
                       **self.spool.stats(), "uploaded_images": self.uploaded,
                       "last_error": self.last_error[:120]}
            if self.schema_version >= 2:
                payload.update(frames_received=self.frames_received, captured_images=self.captured_images,
                               last_frame_age_seconds=state["last_frame_age_seconds"],
                               capture_error=self.capture_error,
                               upload_error=(self._errors.get("upload") or self._errors.get("heartbeat") or
                                             self._errors.get("configuration") or self._errors.get("worker") or ""),
                               session_complete=self.session_complete)
            return payload

    def send_status(self, force=False):
        """Send on the upload thread, including its best-effort final heartbeat."""
        now = self.clock()
        state = self.capture_state()
        if state["state"] != self._reported_state:
            self.log("state", state["state"])
            self._reported_state = state["state"]
        if not state["owner"]:
            return
        payload = self.heartbeat_payload()
        # Queue changes can be frequent. Push only meaningful status/error
        # changes and the first ACK early; counters otherwise ride the heartbeat.
        signature = (payload["state"], payload["last_error"], payload["uploaded_images"] > 0)
        changed = signature != self._last_heartbeat_signature
        if not force and (now < self.next_heartbeat and (not changed or now - self._last_heartbeat < 3)):
            return
        self._last_heartbeat = now
        self._last_heartbeat_signature = signature
        try:
            receipt = self.client.heartbeat(payload)
            if receipt.get("received") is not True:
                raise DeliveryError("heartbeat_acknowledgement_mismatch")
            self._recovered("heartbeat")
            if not self._heartbeat_confirmed:
                self.log("heartbeat", "server_confirmed")
                self._heartbeat_confirmed = True
        except DeliveryError as exc:
            self._error("heartbeat", exc)
            self._invalidate_on_auth_error(str(exc), now)
        self.next_heartbeat = now + self.HEARTBEAT_SECONDS

    def _invalidate_on_auth_error(self, code, now):
        if code in ("http_401", "http_403", "http_404", "http_410"):
            with self.lock:
                self.enabled = False
                self.valid_until = 0
            self.next_config = min(self.next_config, now + 15)

    def step(self):
        now = self.clock()
        if now >= self.next_config:
            try:
                self._configuration(now)
            except DeliveryError as exc:
                self._error("configuration", exc)
                self.next_config = now + 15
                self._invalidate_on_auth_error(str(exc), now)
        if self.stop.is_set():
            return 1
        self.send_status()
        state = self.capture_state()
        if self.stop.is_set() or not state["owner"] or now < self.next_upload:
            return 1
        capture = None
        try:
            # Reading/verifying a queued record can fail too; report it without
            # silently killing the only heartbeat thread while frames wait.
            capture = self.spool.oldest(state["owner"])
            if capture:
                receipt = self.client.upload(capture)
                self.spool.acknowledge(capture, receipt)
                self.failures = 0
                self.uploaded += 1
                self._recovered("upload")
                if self.uploaded == 1:
                    self.log("upload", "first_image_confirmed")
                self.next_upload = now + 0.1
        except DeliveryError as exc:
            self.failures += 1
            self._error("upload", exc)
            self.next_upload = now + min(60, 2 ** min(self.failures, 6))
            self._invalidate_on_auth_error(str(exc), now)
        return 0.1 if capture and not self.failures else 1

    def run(self):
        try:
            while not self.stop.is_set():
                try:
                    delay = self.step()
                    self._recovered("worker")
                except Exception:
                    # Exceptions from optional/native runtime components are
                    # not safe to stringify; use a fixed diagnostic code.
                    self._error("worker", "upload_worker_error")
                    delay = 5
                self.stop.wait(delay)
        finally:
            # Best effort in this daemon thread. The app joins using one shared
            # ten-second deadline, so slow DNS/drip HTTP cannot block Kit cleanup
            # or start a second unbounded request on the main thread.
            try:
                self.send_status(force=True)
            except Exception:
                self._error("worker", "upload_worker_error")

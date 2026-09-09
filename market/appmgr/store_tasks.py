"""Durable, bounded store downloads independent of the lifecycle worker.

Only package retrieval and preflight run here. Installation is always submitted
through the existing v1 confirmation/authorization pipeline. Closing a browser
does not cancel a task; explicit cancellation reclaims its staged package.
"""
from __future__ import annotations

import copy
import json
import os
import queue
import re
import tempfile
import threading
import time
import uuid

from . import operations, paths, signing, store_download, uploads


TERMINAL = frozenset(("succeeded", "failed", "cancelled"))
DOWNLOADING = frozenset(("queued", "downloading", "preflighting"))
MAX_HISTORY = 50
MAX_ACTIVE = 4
MAX_JOURNAL_BYTES = 2 * 1024 * 1024
_TASK_ID = re.compile(r"[0-9a-f]{32}\Z")


class StoreTaskConflict(RuntimeError):
    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code


class StoreTaskError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class StoreTaskManager:
    def __init__(self, *, preflight, finalize, operation_manager, snapshot, discard=None,
                 journal=None, max_active=MAX_ACTIVE):
        self.journal = journal or os.path.join(paths.APPMGR_DIR, "store-tasks.json")
        self.preflight = preflight
        self.finalize = finalize
        self.operations = operation_manager
        self.snapshot = snapshot
        self.discard = discard or uploads.cancel
        self.max_active = max(1, int(max_active))
        self._lock = threading.RLock()
        self._queue = queue.Queue(maxsize=self.max_active)
        self._cancel = {}
        self._worker = None
        self._closed = False
        self._last_progress = {}
        self._records = self._load()
        changed = False
        for record in self._records:
            if record["status"] in DOWNLOADING:
                record.update(status="failed", error={
                    "code": "storeInterrupted", "message": "Download interrupted by appmgr restart"},
                    updated_at=time.time())
                self._discard(record)
                changed = True
            changed = self._sync(record) or changed
        if changed:
            self._save()

    def _load(self):
        try:
            with open(self.journal, "rb") as source:
                raw = source.read(MAX_JOURNAL_BYTES + 1)
            if len(raw) > MAX_JOURNAL_BYTES:
                return []
            value = json.loads(raw)
        except (OSError, ValueError):
            return []
        records = value.get("tasks") if isinstance(value, dict) else None
        if not isinstance(records, list):
            return []
        return [record for record in records[-MAX_HISTORY:]
                if isinstance(record, dict)
                and isinstance(record.get("id"), str) and _TASK_ID.fullmatch(record["id"])
                and isinstance(record.get("app_id"), str) and paths.valid_app_id(record["app_id"])
                and record.get("status") in TERMINAL | DOWNLOADING | {
                    "awaiting_confirmation", "installing"}]

    def _save(self):
        directory = os.path.dirname(self.journal)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        data = json.dumps({"schema_version": 1, "tasks": self._records},
                          separators=(",", ":")).encode("utf-8")
        if len(data) > MAX_JOURNAL_BYTES:
            raise StoreTaskConflict("store task journal is full")
        fd, temporary = tempfile.mkstemp(prefix=".store-tasks.", dir=directory)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.journal)
            temporary = None
            uploads._fsync_dir(directory)
        finally:
            if temporary:
                os.unlink(temporary)

    def _find(self, task_id):
        if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
            raise ValueError("invalid store task id")
        for record in self._records:
            if record["id"] == task_id:
                return record
        raise FileNotFoundError("store task not found")

    def _discard(self, record):
        upload_id = record.get("upload_id")
        if upload_id:
            try:
                self.discard(upload_id)
            except (ValueError, uploads.UploadConflictError, StoreTaskConflict):
                pass
            except OSError:
                uploads.mark_orphaned(upload_id)

    def _sync(self, record, snapshot=None):
        if not record.get("upload_id"):
            return False
        snapshot = self.snapshot(record["upload_id"]) if snapshot is None else snapshot
        operation = snapshot["operation"]
        if operation is not None:
            status = (operation["status"] if operation["status"] in operations.TERMINAL
                      else "installing")
            changed = record["status"] != status or record.get("operation_id") != operation["id"]
            if changed:
                record.update(status=status, operation_id=operation["id"], updated_at=time.time())
                if status == "failed":
                    record["error"] = {"code": "storeInstallFailed", "message": operation.get("error") or "Installation failed"}
                elif status == "succeeded":
                    record["error"] = None
            return changed
        if record["status"] in TERMINAL:
            return False
        if record["status"] == "installing":
            record.update(status="failed", error={"code": "storeInterrupted",
                "message": "Installation operation could not be recovered"}, updated_at=time.time())
            self._discard(record)
            return True
        if record["status"] == "awaiting_confirmation":
            upload = snapshot["upload"]
            if (upload is None or upload.get("status") != "preflighted"
                    or not isinstance(upload.get("preflight"), dict)):
                record.update(status="failed", error={"code": "storeTaskExpired",
                    "message": "Staged package expired; download it again"}, updated_at=time.time())
                return True
        return False

    def _public(self, record):
        snapshot = self.snapshot(record["upload_id"]) if record.get("upload_id") else {}
        if self._sync(record, snapshot):
            self._save()
        value = copy.deepcopy(record)
        if record["status"] == "awaiting_confirmation":
            upload = snapshot["upload"]
            value["inspection"] = {"upload_id": record["upload_id"], "preflight": upload["preflight"]}
        if record.get("operation_id"):
            value["operation"] = snapshot["operation"]
        return value

    def list(self):
        with self._lock:
            changed = False
            for record in self._records:
                changed = self._sync(record) or changed
            if changed:
                self._save()
            return [self._public(record) for record in reversed(self._records)]

    def get(self, task_id):
        with self._lock:
            record = self._find(task_id)
            if self._sync(record):
                self._save()
            return self._public(record)

    def create(self, body):
        if not isinstance(body, dict) or set(body) != {"app_id", "version", "package_sha256"}:
            raise ValueError("store task requires app_id, version and package_sha256 only")
        app_id, version, digest = (body[key] for key in ("app_id", "version", "package_sha256"))
        if not isinstance(app_id, str) or not paths.valid_app_id(app_id):
            raise ValueError("invalid store application id")
        if not isinstance(version, str) or not re.fullmatch(r"[0-9][0-9A-Za-z.+_-]{0,63}", version):
            raise ValueError("invalid store application version")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("invalid store package digest")
        with self._lock:
            if self._closed:
                raise StoreTaskConflict("store task manager is stopping")
            self.list()
            for record in reversed(self._records):
                if record["app_id"] == app_id and (record["status"] not in TERMINAL or record["id"] in self._cancel):
                    if (record["status"] not in TERMINAL and record["version"] == version
                            and record["package_sha256"] == digest):
                        return {"task": self._public(record), "idempotent_replay": True}
                    raise StoreTaskConflict("another store task owns this application")
            active_ids = {record["id"] for record in self._records if record["status"] not in TERMINAL}
            if len(active_ids | self._cancel.keys()) >= self.max_active:
                raise StoreTaskConflict("store task limit reached", code="storeTaskLimitReached")
            now = time.time()
            record = {"id": uuid.uuid4().hex, "app_id": app_id, "version": version,
                      "package_sha256": digest, "status": "queued", "error": None,
                      "progress": {"loaded": 0, "total": 0, "percent": 0},
                      "created_at": now, "updated_at": now}
            while len(self._records) >= MAX_HISTORY:
                expired = next((item for item in self._records if item["status"] in TERMINAL and item["id"] not in self._cancel), None)
                if expired is None:
                    raise StoreTaskConflict("store task history is full")
                self._records.remove(expired)
            self._records.append(record)
            try:
                self._save()
            except BaseException:
                self._records.remove(record)
                raise
            self._cancel[record["id"]] = threading.Event()
            self._queue.put_nowait(record["id"])
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._run, daemon=True, name="appmgr-store-download")
                self._worker.start()
            return {"task": self._public(record), "idempotent_replay": False}

    def cancel(self, task_id):
        with self._lock:
            record = self._find(task_id)
            self._sync(record)
            if record["status"] == "installing":
                raise StoreTaskConflict("installation has already been submitted")
            if record["status"] in TERMINAL:
                return self._public(record)
            event = self._cancel.get(task_id)
            if event:
                event.set()
            if record["status"] == "awaiting_confirmation":
                # The v1 cancellation callback shares its finalize lock with
                # POST /apps, including callers outside the store UI.
                self.discard(record["upload_id"])
            record.update(status="cancelled", updated_at=time.time(), error=None)
            self._save()
            return self._public(record)

    def install(self, task_id, body):
        if not isinstance(body, dict) or "upload_id" in body:
            raise ValueError("store install uses its task-owned upload id")
        with self._lock:
            if self._closed:
                raise StoreTaskConflict("store task manager is stopping")
            record = self._find(task_id)
            self._sync(record)
            if record["status"] not in {"awaiting_confirmation", "installing", "succeeded"}:
                raise StoreTaskConflict("store task is not ready for installation")
            # do_v1_install re-verifies bytes, permissions, signature identity,
            # upgrade confirmations and idempotent request fingerprints.
            result = self.finalize({**body, "upload_id": record["upload_id"]})
            record.update(status="installing", operation_id=result["operation"]["id"], updated_at=time.time())
            self._sync(record)
            self._save()
            return {**result, "task": self._public(record)}

    def _update(self, task_id, **fields):
        with self._lock:
            record = self._find(task_id)
            if record["status"] == "cancelled":
                raise StoreTaskError("store_cancelled", "Download cancelled")
            record.update(fields, updated_at=time.time())
            self._save()

    def _progress(self, task_id, loaded, total):
        with self._lock:
            record = self._find(task_id)
            record["progress"] = {"loaded": loaded, "total": total,
                                  "percent": min(100, int(100 * loaded / total)) if total else 0}
            now = time.monotonic()
            if loaded == total or now - self._last_progress.get(task_id, 0) >= 1:
                record["updated_at"] = time.time()
                self._save()
                self._last_progress[task_id] = now

    def _download(self, task_id, cancelled):
        record = self._find(task_id)
        self._update(task_id, status="downloading")
        app = store_download.resolve_app(record["app_id"], record["version"],
            package_sha256=record["package_sha256"], cancelled=cancelled)
        package = app["package"]
        self._progress(task_id, 0, package["size"])
        upload = uploads.receive_download(package["filename"], package["size"], package["sha256"],
            lambda package_path, signature_path: store_download.download_package(
                app, package_path, signature_path, max_package_bytes=paths.MAX_PKG_BYTES,
                max_signature_bytes=uploads.MAX_SIGNATURE_BYTES, cancelled=cancelled,
                on_progress=lambda loaded, total: self._progress(task_id, loaded, total)),
            on_reserved=lambda upload_id: self._update(task_id, upload_id=upload_id))
        if cancelled():
            raise StoreTaskError("store_cancelled", "Download cancelled")
        self._update(task_id, status="preflighting")
        inspection = self.preflight(upload)
        preview = inspection["preflight"]
        manifest = preview["manifest"]
        if manifest.get("id") != record["app_id"] or manifest.get("version") != record["version"]:
            raise StoreTaskError("storeManifestMismatch", "Package does not match the catalog application")
        if manifest.get("manifest_version") != 2:
            raise StoreTaskError("storeManifestUnsupported", "Store installation requires a manifest v2 package")
        if (preview.get("signature") or {}).get("status") != "verified":
            raise StoreTaskError("storeSignatureUnverified", "Store package publisher signature was not verified")
        if (any(item.get("source") != "bundled" for item in manifest.get("artifacts", []))
                or any(item.get("source") != "bundled" for item in (manifest.get("python") or {}).get("wheels", []))):
            raise StoreTaskError("storeExternalModelsUnsupported", "Store package requires unsupported external dependencies")
        if any(item.get("passed") is not True for item in preview.get("checks", [])):
            raise StoreTaskError("storePreflightFailed", "Package failed device preflight")
        if cancelled():
            raise StoreTaskError("store_cancelled", "Download cancelled")
        self._update(task_id, status="awaiting_confirmation")

    def _run(self):
        while True:
            try:
                task_id = self._queue.get(timeout=0.25)
            except queue.Empty:
                if self._closed:
                    return
                continue
            event = self._cancel[task_id]
            try:
                self._download(task_id, event.is_set)
            except BaseException as exc:
                with self._lock:
                    record = self._find(task_id)
                    self._discard(record)
                    cancelled = event.is_set() and not self._closed
                    error_code = getattr(exc, "code", None)
                    if not error_code:
                        if isinstance(exc.__cause__, signing.SignatureError):
                            error_code = "storeSignatureUnverified"
                        elif isinstance(exc, (uploads.StagingQuotaError, OSError)):
                            error_code = "storePackageWriteFailed"
                        else:
                            error_code = "storePreflightFailed" if record["status"] == "preflighting" else "storeDownloadFailed"
                    record.update(status="cancelled" if cancelled else "failed", updated_at=time.time(),
                        error=None if cancelled else {"code": "storeInterrupted" if self._closed else error_code,
                                                     "message": str(exc)[:1000]})
                    try:
                        self._save()
                    except OSError:
                        pass
            finally:
                with self._lock:
                    self._cancel.pop(task_id, None)
                    self._last_progress.pop(task_id, None)
                self._queue.task_done()

    def close(self):
        with self._lock:
            self._closed = True
            for event in self._cancel.values():
                event.set()
        if self._worker is not None:
            self._worker.join(timeout=12)

"""Device defaults for browser-drawn overlays, independent of inference/OSD.

One durable document carries a global revision and per-application revisions.
Writers hold appmgr's busy gate; readers see either complete atomic snapshot.
Clearing an application's preference retains a false tombstone so a browser
opened before uninstall/reinstall cannot replay a revision from the old app.
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Any

from . import operations, paths


SCHEMA_VERSION = 1
MAX_REVISION = (1 << 53) - 1  # Exact in the browser's JSON number representation.


class BrowserOverlayError(ValueError):
    def __init__(self, field: str, detail: str):
        super().__init__("%s: %s" % (field, detail))
        self.field = field
        self.detail = detail


class RevisionConflict(Exception):
    def __init__(self, current_revision: int):
        super().__init__("revision_conflict")
        self.current_revision = current_revision


class StorageError(RuntimeError):
    """Do not reset a damaged store to revision zero and accidentally allow ABA."""


def config_path() -> str:
    return os.path.join(paths.APPMGR_DIR, "browser-overlays.json")


def _revision(value: Any) -> bool:
    return type(value) is int and 0 <= value <= MAX_REVISION


def _app_id(value: Any) -> bool:
    return isinstance(value, str) and paths.valid_app_id(value)


def validate_request(incoming: Any) -> tuple[bool, int]:
    if not isinstance(incoming, dict):
        raise BrowserOverlayError("body", "must be an object")
    for key in incoming:
        if key not in ("enabled", "revision"):
            raise BrowserOverlayError(str(key), "unknown field")
    if type(incoming.get("enabled")) is not bool:
        raise BrowserOverlayError("enabled", "must be a boolean")
    if "revision" not in incoming:
        raise BrowserOverlayError("revision", "is required")
    revision = incoming["revision"]
    if not _revision(revision):
        raise BrowserOverlayError("revision", "must be a non-negative safe integer")
    return incoming["enabled"], revision


def load() -> dict:
    """Missing means all disabled. Corruption/unreadable storage fails closed."""
    try:
        with open(config_path(), "r", encoding="utf-8") as stream:
            document = json.load(stream)
    except FileNotFoundError:
        return {"schema_version": SCHEMA_VERSION, "revision": 0,
                "preferences": {}}
    except (OSError, ValueError) as exc:
        raise StorageError("cannot read browser overlay preferences") from exc
    if (not isinstance(document, dict)
            or set(document) != {"schema_version", "revision", "preferences"}
            or type(document.get("schema_version")) is not int
            or document["schema_version"] != SCHEMA_VERSION
            or not _revision(document.get("revision"))
            or not isinstance(document.get("preferences"), dict)):
        raise StorageError("invalid browser overlay preferences document")
    for app_id, entry in document["preferences"].items():
        if (not _app_id(app_id) or not isinstance(entry, dict)
                or set(entry) != {"enabled", "revision"}
                or type(entry.get("enabled")) is not bool
                or not _revision(entry.get("revision"))
                or not 0 < entry["revision"] <= document["revision"]):
            raise StorageError("invalid browser overlay preference entry")
    return document


def _write_atomic(document: dict) -> None:
    directory = paths.APPMGR_DIR
    temporary = None
    try:
        os.makedirs(directory, mode=0o755, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=".browser-overlays.", suffix=".tmp", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(document, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, config_path())
        temporary = None
        operations._fsync_dir(directory)
    except OSError as exc:
        raise StorageError("cannot persist browser overlay preferences") from exc
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _update(document: dict, app_id: str, enabled: bool) -> dict:
    if document["revision"] == MAX_REVISION:
        raise StorageError("browser overlay revision exhausted")
    document["revision"] += 1
    document["preferences"][app_id] = {
        "enabled": enabled, "revision": document["revision"]}
    _write_atomic(document)
    return document


def replace(app_id: str, enabled: bool, expected_revision: int) -> dict:
    """Caller holds busy gate and verifies the app is installed/system-owned."""
    if not _app_id(app_id):
        raise BrowserOverlayError("app_id", "invalid app id")
    enabled, expected_revision = validate_request({
        "enabled": enabled, "revision": expected_revision})
    document = load()
    current = document["preferences"].get(app_id, {}).get("revision", 0)
    if current != expected_revision:
        raise RevisionConflict(current)
    return _update(document, app_id, enabled)


def reset_for_lifecycle(app_id: str) -> dict:
    """Under busy gate, fence a fresh install or record a completed uninstall."""
    if not _app_id(app_id):
        raise BrowserOverlayError("app_id", "invalid app id")
    return _update(load(), app_id, False)

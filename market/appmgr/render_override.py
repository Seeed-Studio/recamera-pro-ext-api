"""Device-level browser display overrides for one installed application.

The installed manifest's ``render`` block is the author default.  A user may
override a closed whitelist of presentation leaves (box colours/labels,
subtitle style, event presentation) without repackaging or re-signing the app.
The override is stored outside the replaceable app directory::

    <APPMGR_DIR>/render-overrides/<id>.json
        {"schema_version": 1, "revision": N, "override": {...}}

It never affects stream OSD, coordinate spaces, geometry limits or the render
schema version.  Upgrades stage the revalidated override as ``<id>.json.pending``
and promote it only after the package transaction commits.
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
from typing import Any, Callable, Optional

from . import manifest as appmanifest
from . import paths


SCHEMA_VERSION = 1
MAX_EVENT_KINDS = 64
PENDING_SUFFIX = ".pending"

_BOX_FIELDS = ("color_by", "label", "colors", "line_width")
_EVENT_FIELDS = ("as", "text", "position", "duration_sec", "max_lines")
_SECTIONS = ("boxes", "subtitles", "events")


class RenderOverrideError(ValueError):
    """One override field failed whitelist validation."""

    def __init__(self, field: str, detail: str):
        super().__init__("%s: %s" % (field, detail))
        self.field = field
        self.detail = detail


class RevisionConflict(Exception):
    def __init__(self, current_revision: int):
        super().__init__("revision_conflict")
        self.current_revision = int(current_revision)


def _fail(field: str, detail: str) -> None:
    raise RenderOverrideError(field, detail)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# --------------------------------------------------------------------------
# validation


def _validate_event(kind: Any, spec: Any, path: str) -> dict:
    if not isinstance(spec, dict):
        _fail(path, "must be an object")
    for key in spec:
        if key not in _EVENT_FIELDS:
            _fail(path + "." + str(key), "unknown field")
    if "as" in spec and spec["as"] not in appmanifest.RENDER_EVENT_RENDERERS:
        _fail(path + ".as", "must be one of " +
              ", ".join(appmanifest.RENDER_EVENT_RENDERERS))
    if "text" in spec:
        text = spec["text"]
        if (not isinstance(text, str) or len(text) > appmanifest.RENDER_MAX_TEXT
                or "\x00" in text):
            _fail(path + ".text", "must be a string of at most %d characters" %
                  appmanifest.RENDER_MAX_TEXT)
    if "position" in spec:
        appmanifest.validate_render_position(
            spec["position"], path + ".position", fail=_fail)
    subtitle_like = {key: spec[key] for key in ("duration_sec", "max_lines")
                     if key in spec}
    if subtitle_like:
        # Same numeric rules as subtitles.*; re-root the reported field path.
        appmanifest.validate_render_subtitles(
            subtitle_like, path, fail=_fail, allow_extensions=False)
    return dict(spec)


def validate(value: Any) -> dict:
    """Return a canonical deep copy or raise :class:`RenderOverrideError`."""
    if not isinstance(value, dict):
        _fail("override", "must be an object")
    for key in value:
        if key not in _SECTIONS:
            _fail(str(key), "unknown field")
    out: dict = {}
    if "boxes" in value:
        boxes = value["boxes"]
        if not isinstance(boxes, dict):
            _fail("boxes", "must be an object")
        for key in boxes:
            if key not in _BOX_FIELDS:
                _fail("boxes." + str(key), "unknown field")
        for key in ("color_by", "label"):
            if key in boxes:
                appmanifest.validate_render_field_path(
                    boxes[key], "boxes." + key, fail=_fail)
        if "colors" in boxes:
            appmanifest.validate_render_colors(
                boxes["colors"], "boxes.colors", fail=_fail)
        if "line_width" in boxes:
            if not _is_int(boxes["line_width"]) or \
                    not 1 <= boxes["line_width"] <= 16:
                _fail("boxes.line_width", "must be an integer in [1,16]")
        out["boxes"] = copy.deepcopy(boxes)
    if "subtitles" in value:
        out["subtitles"] = copy.deepcopy(appmanifest.validate_render_subtitles(
            value["subtitles"], "subtitles", fail=_fail,
            allow_extensions=False))
    if "events" in value:
        events = value["events"]
        if not isinstance(events, dict):
            _fail("events", "must be an object")
        if len(events) > MAX_EVENT_KINDS:
            _fail("events", "must contain at most %d kinds" % MAX_EVENT_KINDS)
        clean = {}
        for kind, spec in events.items():
            path = "events." + str(kind)
            if (not isinstance(kind, str)
                    or kind in appmanifest.RENDER_PROTOTYPE_NAMES
                    or not appmanifest._SAFE_TOKEN_RE.fullmatch(kind)):
                _fail(path, "event kind must be a safe token and not a "
                      "prototype property name")
            clean[kind] = copy.deepcopy(_validate_event(kind, spec, path))
        out["events"] = clean
    return out


def sanitize(value: Any) -> tuple[dict, list]:
    """Keep every individually valid leaf; return ``(clean, dropped_fields)``.

    Used for crash recovery and upgrades where stored data is not trusted but
    one bad leaf must not discard all of the user's other choices.
    """
    dropped: list = []
    if not isinstance(value, dict):
        return {}, ["override"] if value not in (None, {}) else []
    clean: dict = {}
    for key in value:
        if key not in _SECTIONS:
            dropped.append(str(key))
    for section in _SECTIONS:
        if section not in value:
            continue
        block = value[section]
        if not isinstance(block, dict):
            dropped.append(section)
            continue
        kept: dict = {}
        if section == "events":
            for kind, spec in block.items():
                if len(kept) >= MAX_EVENT_KINDS:
                    dropped.append("events." + str(kind))
                    continue
                if not isinstance(spec, dict):
                    dropped.append("events." + str(kind))
                    continue
                leaves = {}
                for leaf, leaf_value in spec.items():
                    try:
                        validate({"events": {kind: {leaf: leaf_value}}})
                        leaves[leaf] = copy.deepcopy(leaf_value)
                    except RenderOverrideError:
                        dropped.append("events.%s.%s" % (kind, leaf))
                if leaves:
                    kept[kind] = leaves
        else:
            for leaf, leaf_value in block.items():
                try:
                    validate({section: {leaf: leaf_value}})
                    kept[leaf] = copy.deepcopy(leaf_value)
                except RenderOverrideError:
                    dropped.append("%s.%s" % (section, leaf))
        if kept:
            clean[section] = kept
    return clean, dropped


def sanitize_for_manifest(value: Any, manifest: Any) -> tuple[dict, list]:
    """Revalidate a stored override against one installed manifest.

    Result Hub only injects render for manifest-v2 packages, so an override on
    any other package cannot take effect and is dropped entirely.
    """
    if not (isinstance(manifest, dict) and manifest.get("manifest_version") == 2):
        clean, _ = sanitize(value)
        return {}, (["override"] if clean else [])
    return sanitize(value)


# --------------------------------------------------------------------------
# merge


def merge(render: Any, override: Any) -> dict:
    """``render <- override`` by leaf; ``boxes.colors`` replaces whole table."""
    merged = copy.deepcopy(render) if isinstance(render, dict) else {}
    if not isinstance(override, dict):
        return merged
    for section in ("boxes", "subtitles"):
        block = override.get(section)
        if isinstance(block, dict) and block:
            base = merged.get(section)
            target = copy.deepcopy(base) if isinstance(base, dict) else {}
            for key, value in block.items():
                target[key] = copy.deepcopy(value)
            merged[section] = target
    events = override.get("events")
    if isinstance(events, dict) and events:
        base_events = merged.get("events")
        target_events = (copy.deepcopy(base_events)
                         if isinstance(base_events, dict) else {})
        for kind, spec in events.items():
            if not isinstance(spec, dict):
                continue
            base = target_events.get(kind)
            target = copy.deepcopy(base) if isinstance(base, dict) else {}
            target.update(copy.deepcopy(spec))
            target_events[kind] = target
        merged["events"] = target_events
    return merged


# --------------------------------------------------------------------------
# storage


def override_path(app_id: str) -> str:
    if not paths.valid_app_id(app_id):
        raise ValueError("invalid app id %r" % app_id)
    return os.path.join(paths.render_overrides_dir(), app_id + ".json")


def pending_path(app_id: str) -> str:
    return override_path(app_id) + PENDING_SUFFIX


def _empty() -> dict:
    return {"schema_version": SCHEMA_VERSION, "revision": 0, "override": {}}


def _read_document(pathname: str) -> Optional[dict]:
    try:
        with open(pathname, "r", encoding="utf-8") as stream:
            value = json.load(stream)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"schema_version": SCHEMA_VERSION, "revision": 0,
                "override": None, "corrupt": True}
    if not isinstance(value, dict):
        return {"schema_version": SCHEMA_VERSION, "revision": 0,
                "override": None, "corrupt": True}
    return value


def load(app_id: str) -> dict:
    """Return ``{schema_version, revision, override}`` with a sanitised override.

    Missing files are revision 0.  Invalid leaves in a tampered file never reach
    the renderer; they are dropped here (and rewritten at startup recovery).
    """
    document = _read_document(override_path(app_id))
    if document is None:
        return _empty()
    revision = document.get("revision")
    revision = revision if _is_int(revision) and revision >= 0 else 0
    override = document.get("override")
    if document.get("schema_version") != SCHEMA_VERSION:
        override = {}
    clean, _ = sanitize(override)
    return {"schema_version": SCHEMA_VERSION, "revision": revision,
            "override": clean}


def load_override(app_id: str) -> dict:
    """Best-effort override for render projection; never raises."""
    try:
        return load(app_id)["override"]
    except Exception:
        return {}


def _write_atomic(pathname: str, document: dict) -> None:
    directory = os.path.dirname(pathname)
    os.makedirs(directory, mode=0o755, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix="." + os.path.basename(pathname) + ".", suffix=".tmp",
        dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(document, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, pathname)
        temporary = None
        _fsync_dir(directory)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _fsync_dir(directory: str) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _document(revision: int, override: dict) -> dict:
    return {"schema_version": SCHEMA_VERSION, "revision": int(revision),
            "override": copy.deepcopy(override)}


def replace(app_id: str, expected_revision: Any, override: Any) -> dict:
    """PUT semantics: validate, compare revision, persist ``revision + 1``.

    The caller holds the appmgr mutation gate, which serialises the
    read/compare/write.
    """
    if not _is_int(expected_revision) or expected_revision < 0:
        _fail("revision", "must be a non-negative integer")
    clean = validate(override)
    current = load(app_id)
    if current["revision"] != expected_revision:
        raise RevisionConflict(current["revision"])
    document = _document(current["revision"] + 1, clean)
    _write_atomic(override_path(app_id), document)
    return document


def reset(app_id: str, expected_revision: Any) -> dict:
    """DELETE semantics: clear the override while keeping revision monotonic."""
    if not _is_int(expected_revision) or expected_revision < 0:
        _fail("revision", "must be a non-negative integer")
    current = load(app_id)
    if current["revision"] != expected_revision:
        raise RevisionConflict(current["revision"])
    document = _document(current["revision"] + 1, {})
    _write_atomic(override_path(app_id), document)
    return document


def remove(app_id: str) -> bool:
    """Delete the live and pending override (uninstall / fresh install)."""
    removed = False
    for pathname in (override_path(app_id), pending_path(app_id)):
        try:
            os.unlink(pathname)
            removed = True
        except FileNotFoundError:
            pass
    if removed:
        _fsync_dir(paths.render_overrides_dir())
    return removed


def view(app_id: str, manifest: Any, *, document: Optional[dict] = None,
         effective_render: Optional[Callable[..., dict]] = None) -> dict:
    """The GET/PUT/DELETE response body."""
    document = document if isinstance(document, dict) else load(app_id)
    override = copy.deepcopy(document.get("override") or {})
    defaults = (manifest.get("render") if isinstance(manifest, dict) else None)
    if effective_render is None:
        from . import visualization
        effective_render = visualization.effective_render
    return {
        "app_id": app_id,
        "schema_version": SCHEMA_VERSION,
        "revision": int(document.get("revision") or 0),
        "override": override,
        "defaults": copy.deepcopy(defaults) if isinstance(defaults, dict) else {},
        "effective": effective_render(manifest, override=override),
    }


# --------------------------------------------------------------------------
# upgrade transaction + crash recovery


def stage_upgrade(app_id: str, new_manifest: Any) -> Optional[dict]:
    """Step 1: write the override revalidated for the new release as pending.

    Returns the pending document, or ``None`` when there is nothing to carry.
    """
    try:
        os.unlink(pending_path(app_id))
    except FileNotFoundError:
        pass
    raw = _read_document(override_path(app_id))
    if raw is None:
        return None
    current = load(app_id)
    clean, dropped = sanitize_for_manifest(
        raw.get("override") if raw.get("schema_version") == SCHEMA_VERSION
        else {}, new_manifest)
    revision = current["revision"] + (1 if dropped else 0)
    document = _document(revision, clean)
    document["dropped"] = dropped
    _write_atomic(pending_path(app_id), {
        key: document[key] for key in ("schema_version", "revision", "override")})
    return document


def commit_upgrade(app_id: str) -> bool:
    """Step 2a: package committed -- promote pending atomically."""
    source = pending_path(app_id)
    if not os.path.exists(source):
        return False
    os.replace(source, override_path(app_id))
    _fsync_dir(paths.render_overrides_dir())
    return True


def abort_upgrade(app_id: str) -> bool:
    """Step 2b: package transaction failed -- discard pending."""
    try:
        os.unlink(pending_path(app_id))
    except FileNotFoundError:
        return False
    except ValueError:
        return False
    _fsync_dir(paths.render_overrides_dir())
    return True


def recover_startup(read_manifest: Callable[[str], Any],
                    is_installed: Callable[[str], bool]) -> dict:
    """Step 3: drop stale pending files and revalidate every live override."""
    directory = paths.render_overrides_dir()
    report = {"pending_removed": [], "orphans_removed": [], "revalidated": {}}
    try:
        names = sorted(os.listdir(directory))
    except FileNotFoundError:
        return report
    for name in names:
        pathname = os.path.join(directory, name)
        if name.endswith(".json" + PENDING_SUFFIX) or (
                name.startswith(".") and name.endswith(".tmp")):
            try:
                os.unlink(pathname)
                report["pending_removed"].append(name)
            except OSError:
                pass
    for name in names:
        if not name.endswith(".json"):
            continue
        app_id = name[:-len(".json")]
        if not paths.valid_app_id(app_id):
            continue
        if not is_installed(app_id):
            try:
                os.unlink(os.path.join(directory, name))
                report["orphans_removed"].append(app_id)
            except OSError:
                pass
            continue
        raw = _read_document(override_path(app_id))
        if raw is None:
            continue
        current = load(app_id)
        stored = (raw.get("override")
                  if raw.get("schema_version") == SCHEMA_VERSION else raw)
        clean, dropped = sanitize_for_manifest(stored, read_manifest(app_id))
        needs_rewrite = bool(
            dropped or raw.get("corrupt")
            or raw.get("schema_version") != SCHEMA_VERSION
            or raw.get("revision") != current["revision"])
        if needs_rewrite:
            _write_atomic(override_path(app_id),
                          _document(current["revision"] + 1, clean))
            report["revalidated"][app_id] = dropped or ["override"]
    return report

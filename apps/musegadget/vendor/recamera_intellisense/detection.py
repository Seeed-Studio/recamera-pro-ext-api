"""High-level detection facade: schedule + rules + events (wraps ``rule`` and ``files``)."""

from __future__ import annotations

if __name__ == "__main__" and __package__ is None:
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from recamera_intellisense._cli import main

    raise SystemExit(main())

import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import files as _files
from . import rule as _rule
from . import storage as _storage
from ._coerce import to_bool
from ._errors import RecameraError

__all__ = [
    "get_detection_schedule",
    "set_detection_schedule",
    "get_detection_rules",
    "set_detection_rules",
    "get_detection_events",
    "clear_detection_events",
]


def get_detection_schedule(device_name: Optional[str] = None) -> Optional[List[Dict[str, str]]]:
    """Alias for :func:`rule.get_schedule_rule`."""
    return _rule.get_schedule_rule(device_name)


def set_detection_schedule(
    device_name: Optional[str] = None,
    schedule: Optional[List[Dict[str, str]]] = None,
) -> None:
    """Alias for :func:`rule.set_schedule_rule`.

    `schedule` defaults to `None` (meaning "always active"), so omitting
    the key from the CLI payload is equivalent to passing `schedule: []`
    or `schedule: null` and clears any existing window.
    """
    _rule.set_schedule_rule(device_name, schedule)


def get_detection_rules(device_name: Optional[str] = None) -> List[Dict[str, Any]]:
    """Active INFERENCE_SET rules, or `[]` when the trigger is not INFERENCE_SET
    (including a retired-but-unmigrated ``SED`` selection)."""
    return list(_rule._get_inference_rules(device_name))


DEFAULT_VISION_SOURCE = "builtin"


def _validate_rules_against_sources(
    rules: List[Dict[str, Any]], sources: List[Dict[str, Any]]
) -> None:
    """Fail loudly on unknown source ids or labels the selected sources can
    never produce — a silently never-firing rule is the worst outcome. The
    ``builtin`` source's classes follow the selected vision model (unknowable
    here), so its presence skips the label check: never cry wolf."""
    by_id = {s["id"]: s for s in sources}
    for rule in rules:
        name = rule.get("name", "")
        selected = list(rule.get("source_filter") or [])
        if not selected:  # explicit empty = all sources
            selected = list(by_id)
        unknown = [sid for sid in selected if sid not in by_id]
        if unknown:
            raise ValueError(
                f"rule {name!r}: unknown source_filter id(s) {unknown}; "
                f"available: {sorted(by_id)}"
            )
        labels = list(rule.get("label_filter") or [])
        if not labels:
            continue
        picked = [by_id[sid] for sid in selected]
        if any(s["kind"] == "builtin" for s in picked):
            continue  # builtin classes follow the active vision model: unknowable
        producible = {c for s in picked for c in s["classes"]}
        bad = [lbl for lbl in labels if lbl not in producible]
        if bad:
            states = ", ".join(
                f"{s['id']}({'running' if s['running'] else 'STOPPED'}, "
                f"{len(s['classes'])} classes)" for s in picked
            )
            raise ValueError(
                f"rule {name!r}: label(s) {bad} cannot be produced by the "
                f"selected source(s) [{states}]; check get_record_sources and "
                f"the AcousticsLab/App Center state before compiling this rule"
            )


def set_detection_rules(
    device_name: Optional[str] = None,
    *,
    rules: List[Dict[str, Any]],
    ensure_writer: bool = True,
    ensure_storage: bool = True,
) -> None:
    """Install an INFERENCE_SET trigger with *rules*.

    Rules without ``source_filter`` are scoped to the ``builtin`` vision
    source — an empty filter matches EVERY source, including acoustic
    classifications. Pass ``source_filter=[\"acousticslab\"]`` for sound.
    Source ids and labels are validated against ``get_record_sources`` first.

    Also (by default):
      * enables the rule pipeline with JPG writer (`ensure_writer=True`);
      * ensures a storage slot is available (`ensure_storage=True`).
    """
    if not isinstance(rules, list):
        raise ValueError("'rules' must be a list of detection-rule dicts.")
    ensure_writer = to_bool(ensure_writer, "ensure_writer")
    ensure_storage = to_bool(ensure_storage, "ensure_storage")
    prepared: List[Dict[str, Any]] = []
    for rule in rules:
        r = dict(rule)
        if r.get("source_filter") is None:
            r["source_filter"] = [DEFAULT_VISION_SOURCE]
        prepared.append(r)
    if prepared:
        _validate_rules_against_sources(prepared, _rule.get_record_sources(device_name))
    if ensure_storage:
        _storage.ensure_storage(device_name)
    trigger = {"kind": "inference_set", "rules": prepared}
    _rule.set_record_trigger(device_name, trigger=trigger)
    if ensure_writer:
        cfg = _rule.get_record_config(device_name)
        needs_update = (
            not cfg["rule_enabled"] or cfg["writer"].get("format", "").upper() != "JPG"
        )
        if needs_update:
            _rule.set_record_config(
                device_name,
                rule_enabled=True,
                writer_format="JPG",
                writer_interval_ms=cfg["writer"].get("interval_ms", 0),
            )


_UINT64_MAX = (1 << 64) - 1


def _as_u64(value: Any) -> Optional[int]:
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= _UINT64_MAX:
        return value
    return None


def _timestamp_ms(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and (not math.isfinite(value) or not value.is_integer()):
        return None
    timestamp = int(value)
    return timestamp if -(1 << 63) <= timestamp <= _UINT64_MAX else None


def _iso_timestamp(ts_ms: int) -> str:
    # Keep the legacy negative-time behavior, but do not route u64 timestamps
    # through float or datetime's platform/year limits.
    if ts_ms < 0:
        return (
            datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    secs, millis = divmod(ts_ms, 1000)
    days, remaining = divmod(secs, 86400)
    hours, remaining = divmod(remaining, 3600)
    minutes, seconds = divmod(remaining, 60)

    # Howard Hinnant's civil-from-days algorithm, matching the Rust MCP helper.
    z = days + 719468
    era = z // 146097
    doe = z - era * 146097
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    year = yoe + era * 400
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    day = doy - (153 * mp + 2) // 5 + 1
    month = mp + 3 if mp < 10 else mp - 9
    year += 1 if month <= 2 else 0
    year_text = f"{year:04d}" if year <= 9999 else f"+{year}"
    fraction = f".{millis * 1000:06d}" if millis else ""
    return (
        f"{year_text}-{month:02d}-{day:02d}T{hours:02d}:{minutes:02d}:{seconds:02d}"
        f"{fraction}Z"
    )


def _copy_timezone(source: Dict[str, Any], target: Dict[str, Any], context: str) -> None:
    """Copy presentation metadata without changing the Unix timestamp."""
    tz = source.get("tz")
    if tz is not None:
        if not isinstance(tz, str) or not tz:
            raise RecameraError(f"{context}: 'tz' must be a nonempty string when present")
        target["tz"] = tz
    tz_offset = source.get("tz_offset")
    if tz_offset is not None:
        if (not isinstance(tz_offset, int) or isinstance(tz_offset, bool)
                or not -(1 << 63) <= tz_offset < (1 << 63)):
            raise RecameraError(f"{context}: 'tz_offset' must be a signed 64-bit integer")
        target["tz_offset"] = tz_offset
    if "dst" in source:
        if source["dst"] is not None and not isinstance(source["dst"], bool):
            raise RecameraError(f"{context}: 'dst' must be boolean or null")
        target["dst"] = source["dst"]


def _normalize_file_event(item: Any, index: int) -> Dict[str, Any]:
    context = f"invalid file event at index {index}"
    if not isinstance(item, dict):
        raise RecameraError(f"{context}: expected an object")
    ts_ms = _timestamp_ms(item.get("timestamp"))
    size = _as_u64(item.get("size"))
    event_uid = _as_u64(item.get("event_uid"))
    path = item.get("path")
    if ts_ms is None:
        raise RecameraError(f"{context}: invalid 'timestamp'")
    if size is None:
        raise RecameraError(f"{context}: 'size' must be an unsigned 64-bit integer")
    if event_uid is None:
        raise RecameraError(f"{context}: 'event_uid' must be an unsigned 64-bit integer")
    if not isinstance(path, str):
        raise RecameraError(f"{context}: 'path' must be a string")
    if "op" in item and not isinstance(item["op"], str):
        raise RecameraError(f"{context}: 'op' must be a string")
    attributes = item.get("attributes")
    if attributes is not None and not isinstance(attributes, str):
        raise RecameraError(f"{context}: 'attributes' must be a string or null")
    event: Dict[str, Any] = {
        "op": item.get("op", ""),
        "path": path,
        "size": size,
        "event_uid": event_uid,
        "event_uid_str": str(event_uid),
        "timestamp": _iso_timestamp(ts_ms),
        "timestamp_unix_ms": ts_ms,
    }
    if attributes is not None:
        event["attributes"] = attributes
    if "file_id" in item:
        file_id = _as_u64(item["file_id"])
        if file_id is None:
            raise RecameraError(f"{context}: 'file_id' must be an unsigned 64-bit integer")
        event["file_id"] = file_id
        event["file_id_str"] = str(file_id)
    _copy_timezone(item, event, context)
    return event


def _normalize_file_events(item: Dict[str, Any], event_index: int) -> List[Dict[str, Any]]:
    if "file_events" in item:
        raw_files = item["file_events"]
        if not isinstance(raw_files, list):
            raise RecameraError(
                f"invalid detection event at index {event_index}: 'file_events' must be an array"
            )
    else:
        legacy = item.get("file_event")
        if legacy is None:
            raw_files = []
        elif isinstance(legacy, dict):
            raw_files = [legacy]
        else:
            raise RecameraError(
                f"invalid detection event at index {event_index}: 'file_event' must be an object or null"
            )
    return [_normalize_file_event(value, index) for index, value in enumerate(raw_files)]


def get_detection_events(
    device_name: Optional[str] = None,
    *,
    start_unix_ms: Optional[int] = None,
    end_unix_ms: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Normalized detection events, shape-compatible with the MCP server.

    Rule and file ``timestamp`` values are ISO-8601 UTC strings; the original
    Unix milliseconds remain in ``timestamp_unix_ms``. ``file_events`` retains
    every associated file, while ``snapshot_path`` remains the first file's path.
    ``uid_str``, ``file_id_str`` and related string IDs avoid JavaScript precision
    loss. Timezone fields describe each timestamp and are never reapplied.
    A legacy ``file_event`` becomes one ``file_events`` entry. Malformed required
    fields raise :class:`RecameraError` instead of fabricating missing files.
    Use :func:`files.get_intellisense_events` for the unmodified daemon payloads.
    """
    raw = _files.get_intellisense_events(
        device_name, start_unix_ms=start_unix_ms, end_unix_ms=end_unix_ms
    )
    out: List[Dict[str, Any]] = []
    for index, item in enumerate(raw):
        context = f"invalid detection event at index {index}"
        if not isinstance(item, dict):
            raise RecameraError(f"{context}: expected an object")
        ts_ms = _timestamp_ms(item.get("timestamp"))
        if ts_ms is None:
            raise RecameraError(f"{context}: invalid 'timestamp'")
        uid = _as_u64(item.get("uid"))
        if uid is None:
            raise RecameraError(f"{context}: 'uid' must be an unsigned 64-bit integer")
        if not isinstance(item.get("type"), str):
            raise RecameraError(f"{context}: 'type' must be a string")
        rule_id = (
            (item.get("id") or "").strip() if isinstance(item.get("id"), str) else ""
        )
        rule_name = rule_id or item["type"]
        file_events = _normalize_file_events(item, index)
        snapshot_path = (file_events[0]["path"] or None) if file_events else None
        event = {
            "timestamp": _iso_timestamp(ts_ms),
            "timestamp_unix_ms": ts_ms,
            "rule_name": rule_name,
            "uid": uid,
            "uid_str": str(uid),
            "file_events": file_events,
        }
        file_ids = item.get("file_ids")
        if file_ids is not None:
            if not isinstance(file_ids, list):
                raise RecameraError(f"{context}: 'file_ids' must be an array")
            normalized_ids = [_as_u64(value) for value in file_ids]
            if any(value is None for value in normalized_ids):
                raise RecameraError(
                    f"{context}: every 'file_ids' entry must be an unsigned 64-bit integer"
                )
            event["file_ids"] = normalized_ids
            event["file_id_strs"] = [str(value) for value in normalized_ids]
        _copy_timezone(item, event, context)
        if snapshot_path is not None:
            event["snapshot_path"] = snapshot_path
        out.append(event)
    return out


def clear_detection_events(device_name: Optional[str] = None) -> None:
    """Alias for :func:`files.clear_intellisense_events`."""
    _files.clear_intellisense_events(device_name)


COMMANDS = {
    "get_detection_schedule": get_detection_schedule,
    "set_detection_schedule": set_detection_schedule,
    "get_detection_rules": get_detection_rules,
    "set_detection_rules": set_detection_rules,
    "get_detection_events": get_detection_events,
    "clear_detection_events": clear_detection_events,
}

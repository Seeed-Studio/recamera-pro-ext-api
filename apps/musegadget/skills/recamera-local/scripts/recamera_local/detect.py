"""Detection: models, rules facade, events, and the blocking `wait_event`.

Events come from the on-device intellisense daemon (`/api/v1/intellisense/events`),
normalized to `{timestamp, timestamp_unix_ms, rule_name, snapshot_path?}` —
`snapshot_path` is a local file the agent can open directly.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import _local as http
from . import storage as _storage
from . import trigger as _trigger
from ._coerce import to_bool
from ._errors import RecameraError

__all__ = [
    "get_detection_models_info",
    "get_detection_model",
    "set_detection_model",
    "get_detection_schedule",
    "set_detection_schedule",
    "get_detection_rules",
    "set_detection_rules",
    "get_detection_events",
    "clear_detection_events",
    "wait_event",
]

PATH_MODEL_LIST = "/cgi-bin/entry.cgi/model/list"
PATH_MODEL_INFERENCE = "/cgi-bin/entry.cgi/model/inference"
PATH_EVENTS = "/api/v1/intellisense/events"
PATH_EVENTS_CLEAR = "/api/v1/intellisense/events/clear"



def _parse_model(index: int, d: Dict[str, Any]) -> Dict[str, Any]:
    raw_info = d.get("modelInfo")
    info = raw_info if isinstance(raw_info, dict) else {}
    labels_raw = info.get("classes") or []
    labels = [c for c in labels_raw if isinstance(c, str)]
    return {
        "id": int(index),
        "name": d.get("model", ""),
        "algorithm": info.get("algorithm"),
        "framework": info.get("framework"),
        "version": info.get("version"),
        "labels": labels,
    }


def get_detection_models_info() -> List[Dict[str, Any]]:
    """List installed detection models (use `labels` for rule label filters)."""
    data = http.get_json(PATH_MODEL_LIST)
    if isinstance(data, list):
        models = data
    elif isinstance(data, dict):
        models = data.get("lModels") or []
    else:
        models = []
    return [_parse_model(i, m) for i, m in enumerate(models) if isinstance(m, dict)]


def get_detection_model() -> Optional[Dict[str, Any]]:
    """Currently-active detection model, or `None`."""
    data = http.get_json(PATH_MODEL_INFERENCE) or {}
    if not data.get("iEnable"):
        return None
    name = data.get("sModel")
    if not name:
        return None
    for m in get_detection_models_info():
        if m["name"] == name:
            return {
                **m,
                "fps": int(data.get("iFPS", 0) or 0),
                "status": data.get("sStatus"),
            }
    return {
        "id": -1,
        "name": name,
        "labels": [],
        "fps": int(data.get("iFPS", 0) or 0),
        "status": data.get("sStatus"),
    }


def set_detection_model(
    *,
    model_id: Optional[int] = None,
    model_name: Optional[str] = None,
    fps: int = 30,
) -> Dict[str, Any]:
    """Activate a detection model by id or by name."""
    if model_id is None and model_name is None:
        raise ValueError("set_detection_model requires 'model_id' or 'model_name'.")
    models = get_detection_models_info()
    if model_id is None:
        match = next((m for m in models if m["name"] == model_name), None)
        if match is None:
            known = ", ".join(m["name"] for m in models) or "none"
            raise ValueError(f"Model {model_name!r} is not installed (installed: {known}).")
    else:
        match = next((m for m in models if m["id"] == int(model_id)), None)
        if match is None:
            raise ValueError(f"Model id {model_id} is not installed.")
    payload = {"iEnable": 1, "iFPS": int(fps), "sModel": match["name"]}
    resp = http.post_json(PATH_MODEL_INFERENCE, params={"id": match["id"]}, payload=payload)
    http.expect_ok(resp, "set detection model")
    result = get_detection_model()
    if result is None:
        raise RecameraError(
            "Device reported detection model is not active after setting it."
        )
    return result



def get_detection_schedule() -> Optional[List[Dict[str, str]]]:
    """Alias for :func:`trigger.get_schedule_rule`."""
    return _trigger.get_schedule_rule()


def set_detection_schedule(schedule: Optional[List[Dict[str, str]]] = None) -> None:
    """Alias for :func:`trigger.set_schedule_rule`.

    `schedule` defaults to `None` (always active); each range looks like
    `{"start": "Mon 08:00:00", "end": "Mon 18:00:00"}`.
    """
    _trigger.set_schedule_rule(schedule)


def get_detection_rules() -> List[Dict[str, Any]]:
    """Active INFERENCE_SET rules, or `[]` when the trigger is not INFERENCE_SET
    (including a retired-but-unmigrated ``SED`` selection)."""
    return list(_trigger._get_inference_rules())


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
    *,
    rules: List[Dict[str, Any]],
    ensure_writer: bool = True,
    ensure_storage: bool = True,
) -> None:
    """Install an INFERENCE_SET trigger with *rules*.

    Rules without ``source_filter`` are scoped to the ``builtin`` vision
    source — an empty filter matches EVERY source, including acoustic
    classifications. Pass ``source_filter=["acousticslab"]`` for sound.
    Source ids and labels are validated against ``get_record_sources`` first.

    Also (by default):
      * enables the rule pipeline with JPG writer (`ensure_writer=True`);
      * ensures a storage slot is available (`ensure_storage=True`).

    A rule looks like::

        {"name": "person", "label_filter": ["person"],
         "confidence_range_filter": [0.25, 1.0], "debounce_times": 3,
         "region_filter": [[[0.1,0.1],[0.9,0.1],[0.9,0.9],[0.1,0.9]]]}

    `label_filter` takes label **names** from `get_detection_models_info`
    (vision) or `get_active_acoustic_model` (sound) — never numeric indexes.
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
        _validate_rules_against_sources(prepared, _trigger.get_record_sources())
    if ensure_storage:
        _storage.ensure_storage()
    _trigger.set_record_trigger(trigger={"kind": "inference_set", "rules": prepared})
    if ensure_writer:
        cfg = _trigger.get_record_config()
        needs_update = (
            not cfg["rule_enabled"] or cfg["writer"].get("format", "").upper() != "JPG"
        )
        if needs_update:
            _trigger.set_record_config(
                rule_enabled=True,
                writer_format="JPG",
                writer_interval_ms=cfg["writer"].get("interval_ms", 0),
            )



def _normalize_event(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    ts = item.get("timestamp")
    if not isinstance(ts, (int, float)):
        return None
    ts_ms = int(ts)
    rule_id = (item.get("id") or "").strip() if isinstance(item.get("id"), str) else ""
    rule_name = rule_id or str(item.get("type", ""))
    file_event = item.get("file_event")
    snapshot_path: Optional[str] = None
    if isinstance(file_event, dict):
        p = file_event.get("path")
        if isinstance(p, str) and p:
            snapshot_path = p
    event: Dict[str, Any] = {
        "timestamp": datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "timestamp_unix_ms": ts_ms,
        "rule_name": rule_name,
    }
    if snapshot_path is not None:
        event["snapshot_path"] = snapshot_path
    return event


def get_detection_events(
    *,
    start_unix_ms: Optional[int] = None,
    end_unix_ms: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Normalized detection events in an optional `[start, end]` ms window.

    Prefer :func:`wait_event` for "react when something happens" loops —
    this raw form is for draining/inspecting the buffer.
    """
    params: Dict[str, Any] = {}
    if start_unix_ms is not None:
        params["start"] = int(start_unix_ms)
    if end_unix_ms is not None:
        params["end"] = int(end_unix_ms)
    data = http.get_json(PATH_EVENTS, params=params or None)
    if isinstance(data, dict):
        data = data.get("events") if isinstance(data.get("events"), list) else []
    if not isinstance(data, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in data:
        if isinstance(item, dict):
            event = _normalize_event(item)
            if event is not None:
                out.append(event)
    return out


def clear_detection_events() -> None:
    """Clear the daemon's transient event buffer (it refills continuously).

    Ungated by design: recordings/snapshots on disk are unaffected.
    """
    resp = http.post_json(PATH_EVENTS_CLEAR)
    if isinstance(resp, dict):
        status = str(resp.get("status", "")).lower()
        if status and status != "ok":
            raise RecameraError(
                f"clear detection events failed: {resp.get('message', status)!r}"
            )


WAIT_EVENT_MAX_TIMEOUT_S = 300.0


def wait_event(
    *,
    timeout_s: float = 30.0,
    since_unix_ms: Optional[int] = None,
    poll_interval_s: float = 0.25,
) -> Dict[str, Any]:
    """Block until at least one detection event newer than the watermark arrives.

    Returns::

        {"events": [...], "watermark_unix_ms": W, "timed_out": bool}

    `watermark_unix_ms` is the value to pass back as `since_unix_ms` on the
    next call — each event is then delivered exactly once::

        mark = None
        while True:
            r = wait_event(timeout_s=60, since_unix_ms=mark)
            mark = r["watermark_unix_ms"]
            for e in r["events"]:
                ...  # e["snapshot_path"] is a local file

    When `since_unix_ms` is omitted the watermark starts at "now", i.e. only
    events that occur *after* this call are returned. `timeout_s` is clamped
    to [0.5, 300]; on timeout the call returns with `timed_out: true` and an
    unchanged watermark (safe to retry immediately).
    """
    timeout_s = min(max(float(timeout_s), 0.5), WAIT_EVENT_MAX_TIMEOUT_S)
    poll_interval_s = min(max(float(poll_interval_s), 0.1), 5.0)
    watermark = int(since_unix_ms) if since_unix_ms is not None else int(time.time() * 1000)

    deadline = time.monotonic() + timeout_s
    while True:
        # The daemon's `start` filter is inclusive; filter strictly-newer here.
        fresh = [
            e
            for e in get_detection_events(start_unix_ms=watermark)
            if e["timestamp_unix_ms"] > watermark
        ]
        if fresh:
            return {
                "events": fresh,
                "watermark_unix_ms": max(e["timestamp_unix_ms"] for e in fresh),
                "timed_out": False,
            }
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {
                "events": [],
                "watermark_unix_ms": watermark,
                "timed_out": True,
            }
        time.sleep(min(poll_interval_s, remaining))


COMMANDS = {
    "get_detection_models_info": get_detection_models_info,
    "get_detection_model": get_detection_model,
    "set_detection_model": set_detection_model,
    "get_detection_schedule": get_detection_schedule,
    "set_detection_schedule": set_detection_schedule,
    "get_detection_rules": get_detection_rules,
    "set_detection_rules": set_detection_rules,
    "get_detection_events": get_detection_events,
    "clear_detection_events": clear_detection_events,
    "wait_event": wait_event,
}

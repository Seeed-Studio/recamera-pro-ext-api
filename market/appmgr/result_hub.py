"""Authenticated AI result fan-in and canonical v2 WebSocket service.

The hub is deliberately owned by ``appmgr`` but is separate from the legacy
application gateway:

* applications keep publishing ``recamera-result-gateway@1`` NDJSON and keep
  the byte-compatible WebSocket on :8124;
* the built-in inference publisher uses a separate, strict system UDS;
* both inputs are normalized before they reach the canonical :8125 stream;
* templates create an additional *formatted view*.  They never replace or
  mutate the authenticated raw envelope.

Only stdlib sockets/threads are used on the data plane.  Jinja rendering reuses
the Kit's sandboxed environment and bounded formatter implementation.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import re
import socket
import stat
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from kit.adapters.output_sink import (
    EDGE_KINDS,
    MAX_RENDER_LEN,
    MAX_TEMPLATE_LEN,
    RawJsonFormatter,
    _bounded_render,
    build_formatter,
    make_restricted_env,
    resolve_output_config,
    is_edge_event,
)
from kit.adapters.result_sink import (
    WS_CLIENT_QUEUE,
    WS_LAG_LIMIT,
    WS_MAX_CLIENTS,
    WS_MAX_PER_IP,
    WS_SEND_TIMEOUT,
    effective_bind_host,
)

from . import config as appconfig
from . import paths


SCHEMA = "recamera.ai.result"
SCHEMA_VERSION = 2
SYSTEM_PROTOCOL = "recamera-system-result@1"
SYSTEM_HELLO_KEYS = frozenset(("type", "protocol", "source"))
DATA_TYPES = frozenset(("frame", "event", "status", "metrics"))
VIEWS = frozenset(("raw", "formatted"))
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# Conservative bundled-app delivery classification.  These are user-visible
# edges worth short replay; unlisted per-frame observations are state and use a
# source+kind latest-wins slot.  Explicit boolean edge markers allow compatible
# third-party apps to opt in until manifest delivery declarations are standard.
EDGE_EVENT_KINDS = EDGE_KINDS


class ResultHubError(RuntimeError):
    """A protocol, identity, or lifecycle error at the Result Hub boundary."""


def _compact_json(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False,
                      default=str).encode("utf-8")


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return int(default)


def _positive_dimension(value):
    result = _as_int(value, 0)
    return result if result > 0 else None


def _wall_ms(payload: dict) -> int:
    value = payload.get("timestamp_ms")
    if value is None:
        value = payload.get("timestamp")
    try:
        number = float(value)
        # App envelopes use epoch milliseconds; a seconds-valued timestamp is
        # accepted only when it is recognizably an epoch value.
        if number >= 100_000_000_000:
            return int(number)
        if number >= 100_000_000:
            return int(number * 1000.0)
    except (TypeError, ValueError, OverflowError):
        pass
    gateway = payload.get("gateway_ts")
    try:
        if gateway is not None:
            return int(float(gateway) * 1000.0)
    except (TypeError, ValueError, OverflowError):
        pass
    return int(time.time() * 1000.0)


def _app_wall_ms(payload: dict) -> int:
    """Use gateway receipt time for app ordering; payload time is untrusted."""
    receipt = int(time.time() * 1000.0)
    try:
        gateway = int(float(payload.get("gateway_ts")) * 1000.0)
        # A gateway clock jump must not pin browser latest-state indefinitely.
        return gateway if abs(gateway - receipt) <= 5 * 60 * 1000 else receipt
    except (TypeError, ValueError, OverflowError):
        return receipt


def _pts_us(payload: dict) -> int:
    frame = payload.get("frame") if isinstance(payload.get("frame"), dict) else {}
    value = payload.get("pts_us", frame.get("pts_us"))
    if value is not None:
        return max(0, _as_int(value, 0))
    value = payload.get("pts", frame.get("pts"))
    try:
        return max(0, int(float(value) * 1_000_000.0))
    except (TypeError, ValueError, OverflowError):
        return 0


_GEOMETRY_SUFFIX = {
    "box": "xyxy", "bbox": "xyxy",
    "pixel_xyxy": "xyxy", "normalized_xyxy": "xyxy",
    "quad": "quad",
    "pixel_quad": "quad", "normalized_quad": "quad",
    "points": "points", "keypoints": "points", "landmarks": "points",
    "mesh": "points", "skeleton": "points",
    "pixel_points": "points", "normalized_points": "points",
}
_COORD_SPACES = frozenset(
    "%s_%s" % (basis, suffix)
    for basis in ("pixel", "normalized")
    for suffix in ("xyxy", "quad", "points"))
_RESULT_FIELD_RE = re.compile(r"^results\[\]\.([A-Za-z_][A-Za-z0-9_]*)$")
_EVENT_FIELD_RE = re.compile(
    r"^events\[(?:kind=([A-Za-z0-9_.-]+))?\]\.([A-Za-z_][A-Za-z0-9_]*)$")


def _compile_geometry_contract(manifest: dict) -> dict:
    """Compile signed manifest field declarations into a tiny hot-path map.

    Only direct ``results[]`` and ``events[]`` fields are accepted.  Derived or
    complex paths never authorize payload geometry.  Conflicting declarations
    fail closed to ``unknown`` even though installed manifests are normally
    schema-validated before this control-plane cache is populated.
    """
    contract = {"results": {}, "events": {}}
    output = manifest.get("output") if isinstance(manifest, dict) else {}
    fields = output.get("fields") if isinstance(output, dict) else []
    if not isinstance(fields, list):
        return contract

    def declare(target: dict, name: str, coord: object) -> None:
        suffix = _GEOMETRY_SUFFIX.get(name)
        value = str(coord or "")
        if (suffix is None or value not in _COORD_SPACES
                or not value.endswith("_" + suffix)):
            value = "unknown"
        previous = target.get(name)
        target[name] = value if previous in (None, value) else "unknown"

    for value in fields:
        if not isinstance(value, dict) or value.get("derived") is True:
            continue
        path = value.get("from")
        if not isinstance(path, str):
            continue
        match = _RESULT_FIELD_RE.fullmatch(path)
        if match:
            declare(contract["results"], match.group(1), value.get("coord"))
            continue
        match = _EVENT_FIELD_RE.fullmatch(path)
        if not match:
            continue
        event_kind = str(match.group(1) or value.get("event_kind") or "*").lower()
        target = contract["events"].setdefault(event_kind, {})
        declare(target, match.group(2), value.get("coord"))
    return contract


def _compile_stream_contract(manifest: dict) -> dict:
    """Map the current Kit camera claim to its authoritative preview stream.

    The v2 manifest does not yet carry a selectable stream profile.  On this
    firmware ``camera.frames`` is the shared frame.sock VI pipe0/ch1 feed that
    corresponds to the browser's main preview (go2rtc ``/live/0``).  A manifest
    without that signed claim has no video stream association; app-reported
    labels such as ``camera-0`` remain diagnostic only.
    """
    resources = manifest.get("resources") if isinstance(manifest, dict) else {}
    claims = resources.get("claims") if isinstance(resources, dict) else []
    if not isinstance(claims, list):
        claims = []
    has_frames = any(
        isinstance(value, dict) and value.get("name") == "camera.frames"
        for value in claims)
    if not has_frames:
        return {"id": "", "kind": "none"}
    return {"id": "main", "kind": "frame.sock", "path": "/live/0"}


def _copy_shapes(values: object, geometry: Optional[dict] = None,
                 *, trusted_basis: Optional[str] = None,
                 event_contracts: Optional[dict] = None) -> List[dict]:
    """Copy shapes and inject only control-plane-authorized coordinates."""
    if not isinstance(values, list):
        return []
    basis = (trusted_basis if trusted_basis in ("pixel", "normalized")
             else None)
    copied: List[dict] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        item = dict(value)
        # Payload-level `space(s)` crosses the untrusted data plane.  Discard it
        # before adding declarations compiled from the signed manifest/system
        # adapter; consumers must never infer normalized coordinates by value.
        item.pop("space", None)
        item.pop("spaces", None)
        selected = dict(geometry or {})
        if event_contracts is not None:
            wildcard = event_contracts.get("*") or {}
            selected.update(wildcard if isinstance(wildcard, dict) else {})
            specific = event_contracts.get(_event_kind(item)) or {}
            selected.update(specific if isinstance(specific, dict) else {})
        present = {}
        for key, suffix in _GEOMETRY_SUFFIX.items():
            if key not in item or item.get(key) is None:
                continue
            if basis is not None:
                space = "%s_%s" % (basis, suffix)
            else:
                space = selected.get(key, "unknown")
            present[key] = space if space in _COORD_SPACES else "unknown"
        if present:
            item["spaces"] = dict(present)
            unique = set(present.values())
            if len(unique) == 1:
                # Compatibility convenience for a single-shape result.  Mixed
                # pose/face records use `spaces` so box and points are never
                # falsely described as sharing an xyxy representation.
                item["space"] = next(iter(unique))
            else:
                item.pop("space", None)
        copied.append(item)
    return copied


def _coordinate_space(*collections: List[dict]) -> str:
    spaces = []
    for collection in collections:
        for item in collection:
            declared = item.get("spaces") if isinstance(item, dict) else None
            if isinstance(declared, dict):
                spaces.extend(str(value) for value in declared.values())
    if not spaces or "unknown" in spaces:
        return "unknown"
    bases = {value.split("_", 1)[0] for value in spaces}
    if len(bases) != 1 or next(iter(bases)) not in ("pixel", "normalized"):
        return "unknown"
    # `stream.coordinate_space` states the frame basis; exact shape encodings
    # remain authoritative in each item's `spaces` map.
    return "%s_xyxy" % next(iter(bases))


def _summary(payload: dict) -> dict:
    value = payload.get("summary")
    if isinstance(value, dict):
        result = dict(value)
    elif value is None:
        result = {}
    else:
        result = {"value": value}
    if payload.get("state") is not None and "state" not in result:
        result["state"] = payload.get("state")
    return result


def _metrics(payload: dict) -> dict:
    value = payload.get("metrics")
    result = dict(value) if isinstance(value, dict) else {}
    for key in ("fps", "inference_time_ms", "pipeline_ms", "latency_ms",
                "preprocess_ms", "postprocess_ms", "dropped"):
        if payload.get(key) is not None:
            result[key] = payload.get(key)
    return result


def _stream(payload: dict, *, coordinate_space: str,
            trusted_stream: Optional[dict] = None) -> dict:
    frame = payload.get("frame") if isinstance(payload.get("frame"), dict) else {}
    if trusted_stream is None:
        stream_id = (payload.get("stream_id") or frame.get("stream_id")
                     or frame.get("id") or "camera-0")
    else:
        stream_id = trusted_stream.get("id") or ""
    return {
        "id": str(stream_id),
        "width": _positive_dimension(frame.get("width", payload.get("width"))),
        "height": _positive_dimension(frame.get("height", payload.get("height"))),
        "coordinate_space": coordinate_space,
    }


def _base_envelope(*, message_type: str, message_id: str, source: dict,
                   seq: int, wall_ms: int, pts_us: int, stream: dict,
                   results: Optional[List[dict]] = None,
                   events: Optional[List[dict]] = None,
                   metrics: Optional[dict] = None, summary: Optional[dict] = None,
                   render: Optional[dict] = None,
                   extensions: Optional[dict] = None) -> dict:
    """Build the stable outer envelope shared by raw and formatted views."""
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "type": message_type,
        "id": str(message_id),
        "source": dict(source),
        "seq": int(seq),
        "time": {"wall_ms": int(wall_ms), "pts_us": int(pts_us)},
        "stream": dict(stream),
        "results": list(results or []),
        "events": list(events or []),
        "metrics": dict(metrics or {}),
        "summary": dict(summary or {}),
        "render": dict(render or {}),
        "extensions": dict(extensions or {}),
    }


def _event_kind(event: dict) -> str:
    value = event.get("kind") or event.get("type") or event.get("name") or "event"
    return str(value).strip().lower()[:96] or "event"


def _event_delivery(event: dict) -> str:
    return "edge" if is_edge_event(event) else "state"


def _event_id(source: dict, seq: int, pts_us: int, index: int,
              event: dict, *, use_producer: bool = True
              ) -> Tuple[str, Optional[object]]:
    supplied = event.get("event_id")
    semantic = dict(event)
    if not use_producer:
        # Producer counters are diagnostic for state, not part of the observed
        # QR/OCR/pose value.  Otherwise a per-frame counter defeats semantic
        # coalescing even though canonical IDs are producer-independent.
        semantic.pop("event_id", None)
    stable = _compact_json(semantic)
    digest = hashlib.sha256(stable).hexdigest()[:16]
    generation = _as_int(source.get("generation"), 0)
    producer = supplied if isinstance(supplied, (str, int)) and not isinstance(
        supplied, bool) else None
    if producer is not None and use_producer:
        producer_text = str(producer)
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,96}", producer_text):
            producer_text = "p-" + hashlib.sha256(
                producer_text.encode("utf-8", "replace")).hexdigest()[:16]
        marker = producer_text
    elif use_producer:
        # Repeated true edges with identical content are still distinct.  For
        # example the same wake phrase/transcript may legitimately occur twice
        # inside the replay TTL, so bind the fallback to its frame position.
        marker = "e-%s-%s-%s-%s" % (seq, pts_us, index, digest)
    else:
        # Per-frame state uses semantic content identity.  Stable QR/OCR/
        # drowsiness observations therefore coalesce without consuming the
        # edge FIFO; producer ids such as 0 remain available diagnostically.
        marker = "h-%s" % digest
    return ("evt:%s:%s:%s" % (
        source.get("id", "unknown"), generation, marker), producer)


def normalize_app_payload(payload: dict, identity: dict,
                          *, fallback_seq: int = 0,
                          trusted_render: Optional[dict] = None,
                          trusted_geometry: Optional[dict] = None,
                          trusted_stream: Optional[dict] = None) -> List[dict]:
    """Normalize one authenticated legacy application envelope.

    Identity and rendering declarations supplied by the payload are ignored.
    ``identity`` must be the appmgr coordinator result derived from SO_PEERCRED
    and the active generation.  ``trusted_render`` is copied from the installed
    manifest by :class:`ResultHub`; an application cannot change the browser/OSD
    rendering or coordinate contract dynamically. Frame data and each event
    become independent messages so a latest-wins video queue can never erase
    the event replay record.
    """
    if not isinstance(payload, dict) or not isinstance(identity, dict):
        raise ResultHubError("application result must be an object")
    app_id = str(identity.get("app_id") or "")
    instance = str(identity.get("instance_id") or "")
    generation = _as_int(identity.get("generation"), -1)
    if not app_id or not instance or generation < 0:
        raise ResultHubError("application identity is incomplete")
    source = {
        "kind": "app",
        "id": app_id,
        "app_id": app_id,
        "instance": instance,
        "generation": generation,
        "trust": "peercred",
    }
    seq = _as_int(payload.get("seq"), fallback_seq)
    wall = _app_wall_ms(payload)
    pts = _pts_us(payload)
    geometry = trusted_geometry if isinstance(trusted_geometry, dict) else {}
    result_geometry = geometry.get("results")
    event_geometry = geometry.get("events")
    results = _copy_shapes(
        payload.get("results"),
        result_geometry if isinstance(result_geometry, dict) else {})
    events = _copy_shapes(
        payload.get("events"), event_contracts=(
            event_geometry if isinstance(event_geometry, dict) else {}))
    stream = _stream(payload, coordinate_space=_coordinate_space(results, events),
                     trusted_stream=(trusted_stream
                                     if isinstance(trusted_stream, dict) else {}))
    if stream["width"] is None or stream["height"] is None:
        # Pixel coordinates without their reference dimensions are unusable.
        # Preserve normalized geometry, but fail every pixel shape closed.
        for collection in (results, events):
            for item in collection:
                spaces = item.get("spaces") if isinstance(item, dict) else None
                if isinstance(spaces, dict):
                    for key, value in list(spaces.items()):
                        if str(value).startswith("pixel_"):
                            spaces[key] = "unknown"
                    unique = set(spaces.values())
                    if len(unique) == 1:
                        item["space"] = next(iter(unique))
                    else:
                        item.pop("space", None)
        stream["coordinate_space"] = _coordinate_space(results, events)
    coordinate_space = stream["coordinate_space"]
    metrics = _metrics(payload)
    summary = _summary(payload)
    # `payload.render` crosses the untrusted app data plane and is deliberately
    # discarded.  Only the generation-bound installed-manifest cache may supply
    # this field.
    render = copy.deepcopy(trusted_render) if isinstance(trusted_render, dict) else {}
    legacy_type = str(payload.get("type") or "results").lower()
    known = {
        "type", "app", "instance", "generation", "source", "source_id",
        "seq", "pts", "pts_us", "timestamp", "timestamp_ms", "gateway_ts",
        "frame", "stream_id", "width", "height", "results", "events",
        "metrics", "summary", "render", "fps", "inference_time_ms",
        "pipeline_ms", "latency_ms", "preprocess_ms", "postprocess_ms",
        "dropped",
    }
    extra = {key: value for key, value in payload.items() if key not in known}
    common_ext = {
        "legacy_type": legacy_type,
        "coordinate_space": coordinate_space,
    }
    reported_stream = (payload.get("stream_id")
                       or ((payload.get("frame") or {}).get("stream_id")
                           if isinstance(payload.get("frame"), dict) else None))
    if reported_stream is not None:
        common_ext["reported_stream_id"] = str(reported_stream)
    if payload.get("timestamp") is not None:
        common_ext["reported_timestamp"] = payload.get("timestamp")
    if payload.get("timestamp_ms") is not None:
        common_ext["reported_timestamp_ms"] = payload.get("timestamp_ms")
    if isinstance(trusted_stream, dict):
        common_ext["stream_source"] = copy.deepcopy(trusted_stream)
    if extra:
        common_ext["payload"] = extra

    envelopes: List[dict] = []
    is_metrics = legacy_type in ("metric", "metrics", "meta")
    is_status = legacy_type in ("status", "summary", "state")
    wants_frame = bool(results) or (
        legacy_type in ("result", "results", "frame") and not events)
    if wants_frame:
        envelopes.append(_base_envelope(
            message_type="frame", message_id=f"{app_id}:{generation}:{seq}:frame",
            source=source, seq=seq, wall_ms=wall, pts_us=pts, stream=stream,
            results=results, metrics=metrics, summary=summary, render=render,
            extensions=common_ext,
        ))
    if is_metrics:
        envelopes.append(_base_envelope(
            message_type="metrics", message_id=f"{app_id}:{generation}:{seq}:metrics",
            source=source, seq=seq, wall_ms=wall, pts_us=pts, stream=stream,
            metrics=metrics or extra, summary=summary, render=render,
            extensions=common_ext,
        ))
    if is_status or (summary and not results and not events and not is_metrics):
        envelopes.append(_base_envelope(
            message_type="status", message_id=f"{app_id}:{generation}:{seq}:status",
            source=source, seq=seq, wall_ms=wall, pts_us=pts, stream=stream,
            metrics=metrics, summary=summary, render=render,
            extensions=common_ext,
        ))
    # A summary is durable state even when it accompanied an event/frame.  Emit a
    # separate status record so a later empty video frame cannot erase it.
    if summary and not any(item["type"] == "status" for item in envelopes):
        envelopes.append(_base_envelope(
            message_type="status", message_id=f"{app_id}:{generation}:{seq}:status",
            source=source, seq=seq, wall_ms=wall, pts_us=pts, stream=stream,
            metrics=metrics, summary=summary, render=render,
            extensions=common_ext,
        ))
    state_groups: Dict[str, List[dict]] = {}
    event_order = []
    for index, raw_event in enumerate(events):
        event = dict(raw_event)
        delivery = _event_delivery(event)
        event_id, producer_event_id = _event_id(
            source, seq, pts, index, event, use_producer=(delivery == "edge"))
        if producer_event_id is not None:
            event["producer_event_id"] = producer_event_id
        event["event_id"] = event_id
        event_kind = _event_kind(event)
        if delivery == "state":
            if event_kind not in state_groups:
                event_order.append(("state", event_kind))
            state_groups.setdefault(event_kind, []).append(event)
            continue
        event_ext = dict(common_ext)
        event_ext["delivery"] = delivery
        event_ext["event_kind"] = event_kind
        event_order.append(_base_envelope(
            message_type="event", message_id=event_id,
            source=source, seq=seq, wall_ms=wall, pts_us=pts, stream=stream,
            events=[event], metrics=metrics, summary=summary, render=render,
            extensions=event_ext,
        ))
    # Per-frame observations such as multiple QR/OCR detections are one state
    # snapshot, not independent FIFO entries.  Replacing the next group removes
    # vanished observations without dropping siblings from the same frame.
    grouped_envelopes = {}
    for event_kind, grouped in state_groups.items():
        if len(grouped) == 1:
            group_id = grouped[0]["event_id"]
        else:
            semantic_group = []
            for value in grouped:
                semantic = dict(value)
                semantic.pop("event_id", None)
                semantic.pop("producer_event_id", None)
                semantic_group.append(semantic)
            digest = hashlib.sha256(
                _compact_json(semantic_group)).hexdigest()[:16]
            group_id = "evt:%s:%s:h-%s" % (app_id, generation, digest)
        event_ext = dict(common_ext)
        event_ext["delivery"] = "state"
        event_ext["event_kind"] = event_kind
        grouped_envelopes[event_kind] = _base_envelope(
            message_type="event", message_id=group_id,
            source=source, seq=seq, wall_ms=wall, pts_us=pts, stream=stream,
            events=grouped, metrics=metrics, summary=summary, render=render,
            extensions=event_ext,
        )
    for value in event_order:
        if isinstance(value, tuple):
            envelopes.append(grouped_envelopes[value[1]])
        else:
            envelopes.append(value)
    # A malformed/novel legacy message is still observable instead of vanishing.
    if not envelopes:
        envelopes.append(_base_envelope(
            message_type="status", message_id=f"{app_id}:{generation}:{seq}:status",
            source=source, seq=seq, wall_ms=wall, pts_us=pts, stream=stream,
            metrics=metrics, summary=summary, render=render,
            extensions=common_ext,
        ))
    return envelopes


def _system_results(payload: dict, coordinate_space: str) -> Tuple[List[dict], str]:
    task = str(payload.get("task_type_name") or "unknown").lower()
    task_key = "keypoints" if task in ("keypoint", "keypoints") else task
    section = payload.get(task_key)
    if not isinstance(section, dict):
        # Be tolerant of a correct task payload whose enum/name is new.
        section = next((payload.get(key) for key in (
            "detection", "classification", "segmentation", "tracking", "keypoints")
                        if isinstance(payload.get(key), dict)), {})
        if section:
            task_key = next(key for key in (
                "detection", "classification", "segmentation", "tracking", "keypoints")
                            if payload.get(key) is section)
    values = section.get("instances") if task_key == "keypoints" else section.get("entries")
    basis = ("normalized" if coordinate_space.startswith("normalized")
             else "pixel" if coordinate_space.startswith("pixel") else None)
    results = _copy_shapes(values, trusted_basis=basis)
    normalized: List[dict] = []
    for value in results:
        item = dict(value)
        item.setdefault("kind", task_key)
        if item.get("class_name") is not None:
            item.setdefault("label", item.get("class_name"))
        if item.get("class_id") is not None:
            item.setdefault("cls", item.get("class_id"))
        if task_key == "keypoints" and item.get("points") is not None:
            item.setdefault("keypoints", item.get("points"))
            # ``keypoints`` is the canonical browser-facing alias.  Geometry
            # consumers deliberately require a per-field trusted space and do
            # not fall back to payload or stream hints, so mirror the space
            # assigned to the parser's authoritative ``points`` field.
            spaces = item.get("spaces")
            if isinstance(spaces, dict) and spaces.get("points"):
                spaces = dict(spaces)
                spaces["keypoints"] = spaces["points"]
                item["spaces"] = spaces
        normalized.append(item)
    return normalized, task_key


def normalize_system_payload(payload: dict, identity: dict,
                             *, fallback_seq: int = 0) -> List[dict]:
    """Normalize a pre-template built-in inference parser dictionary."""
    if not isinstance(payload, dict) or not isinstance(identity, dict):
        raise ResultHubError("system result must be an object")
    source_id = str(identity.get("id") or identity.get("source_id") or "")
    if source_id != "builtin":
        raise ResultHubError("system identity resolver must authorize builtin")
    source = {
        "kind": "builtin",
        "id": "builtin",
        "trust": str(identity.get("trust") or "peercred"),
    }
    model_id = payload.get("model_id")
    if model_id is not None:
        source["model_id"] = model_id
    seq = _as_int(payload.get("seq"), fallback_seq)
    wall = _wall_ms(payload)
    pts = _pts_us(payload)
    coordinate_space = "normalized_xyxy"
    stream = _stream(payload, coordinate_space=coordinate_space)
    results, task = _system_results(payload, coordinate_space)
    extensions = {
        "task_type": payload.get("task_type"),
        "task_type_name": task,
        "coordinate_space": coordinate_space,
    }
    # source_id is diagnostic input only.  The authenticated source above is
    # assigned by the resolver and cannot be overridden by this value.
    if payload.get("source_id") is not None:
        extensions["reported_source_id"] = payload.get("source_id")
    return [_base_envelope(
        message_type="frame", message_id=f"builtin:{seq}:frame",
        source=source, seq=seq, wall_ms=wall, pts_us=pts, stream=stream,
        results=results, metrics=_metrics(payload), summary=_summary(payload),
        render=(payload.get("render") if isinstance(payload.get("render"), dict) else {}),
        extensions=extensions,
    )]


def _stat_signature(path: str) -> Tuple[object, ...]:
    try:
        value = os.stat(path)
        return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
    except OSError:
        return (None,)


class ResultViewFormatter:
    """Config-aware formatter for the canonical WS's optional formatted view."""

    _BUILTIN_TEMPLATE_KEYS = {
        "detection": ("sDetection", "detection"),
        "classification": ("sClassification", "classification"),
        "segmentation": ("sSegmentation", "segmentation"),
        "tracking": ("sTracking", "tracking"),
        "keypoints": ("sKeypoints", "sKeypoint", "keypoints"),
        "default": ("sDefault", "default", "template"),
    }

    def __init__(self, *, notify_config: Optional[str] = None):
        self.notify_config = notify_config or paths.NOTIFY_CONFIG
        self._lock = threading.Lock()
        self._app_cache: Dict[str, Tuple[Tuple[object, ...], object, str]] = {}
        self._builtin_cache: Optional[Tuple[Tuple[object, ...], dict]] = None
        try:
            from kit.adapters.mqtt_sink import device_identifier
            self._node = str(device_identifier() or "")
        except Exception:
            self._node = ""

    @staticmethod
    def _legacy_view(envelope: dict) -> dict:
        batch = envelope.get("_legacy_batch")
        if isinstance(batch, dict):
            return copy.deepcopy(batch)
        source = envelope.get("source") or {}
        stream = envelope.get("stream") or {}
        time_info = envelope.get("time") or {}
        view = {
            "type": "results",
            "app": source.get("app_id") or source.get("id"),
            "timestamp": time_info.get("wall_ms"),
            "seq": envelope.get("seq"),
            "frame": {
                "width": stream.get("width"),
                "height": stream.get("height"),
                "pts": float(time_info.get("pts_us") or 0) / 1_000_000.0,
            },
            "results": envelope.get("results") or [],
            "events": envelope.get("events") or [],
            "metrics": envelope.get("metrics") or {},
            "summary": envelope.get("summary") or {},
            "render": envelope.get("render") or {},
        }
        extra = ((envelope.get("extensions") or {}).get("payload"))
        if isinstance(extra, dict):
            for key, value in extra.items():
                view.setdefault(key, value)
        return view

    def _app_formatter(self, app_id: str):
        manifest_path = os.path.join(paths.app_dir(app_id), "manifest.json")
        config_path = appconfig.config_path(app_id)
        signature = (_stat_signature(manifest_path), _stat_signature(config_path))
        with self._lock:
            cached = self._app_cache.get(app_id)
            if cached is not None and cached[0] == signature:
                return cached[1], cached[2]
        with open(manifest_path, encoding="utf-8") as source:
            manifest = json.load(source)
        effective = appconfig.effective_values(manifest, app_id)
        config = resolve_output_config(manifest, effective)
        mode = str(config.get("mode") or "raw")
        mqtt = config.get("dMqtt") or {}
        base_topic = str(mqtt.get("sTopic") or "recamera").rstrip("/") or "recamera"
        formatter = build_formatter(
            mode, config, app_id=app_id, node=self._node,
            base_topic=base_topic, entities=manifest.get("ha_entities") or [],
            device_name=manifest.get("name") or app_id,
        )
        with self._lock:
            self._app_cache[app_id] = (signature, formatter, mode)
        return formatter, mode

    @staticmethod
    def _first_text(section: dict, keys: Sequence[str]) -> str:
        for key in keys:
            value = section.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def _builtin_templates(self) -> dict:
        signature = _stat_signature(self.notify_config)
        with self._lock:
            cached = self._builtin_cache
            if cached is not None and cached[0] == signature:
                return cached[1]
        try:
            with open(self.notify_config, encoding="utf-8") as source:
                config = json.load(source)
        except (OSError, ValueError):
            config = {}
        section = config.get("dTemplate") if isinstance(config, dict) else {}
        if not isinstance(section, dict):
            section = {}
        compiled = {}
        try:
            environment = make_restricted_env()
            for task, keys in self._BUILTIN_TEMPLATE_KEYS.items():
                value = self._first_text(section, keys)
                if value and len(value) <= MAX_TEMPLATE_LEN:
                    try:
                        compiled[task] = environment.from_string(value)
                    except Exception:
                        continue
        except Exception:
            compiled = {}
        with self._lock:
            self._builtin_cache = (signature, compiled)
        return compiled

    @staticmethod
    def _formatted(raw: dict, *, payload: str, content_type: str,
                   profile: str, index: int, topic=None, retain=False,
                   metadata=None) -> dict:
        extensions = {
            "raw_id": raw.get("id"),
            "raw_type": raw.get("type"),
        }
        raw_extensions = raw.get("extensions") or {}
        if raw_extensions.get("batch_id"):
            extensions["batch_id"] = raw_extensions.get("batch_id")
            extensions["batch_types"] = list(
                raw_extensions.get("batch_types") or [])
            extensions["projection"] = "authenticated_ingress_batch"
            batch_delivery = str(raw_extensions.get("delivery") or "")
            if batch_delivery in ("edge", "state"):
                extensions["delivery"] = batch_delivery
        if topic is not None:
            extensions["topic"] = topic
        if retain:
            extensions["retain"] = True
        if metadata:
            extensions["metadata"] = metadata
        result = _base_envelope(
            message_type="formatted",
            message_id="%s:formatted:%s" % (raw.get("id"), index),
            source=raw.get("source") or {}, seq=_as_int(raw.get("seq"), 0),
            wall_ms=_as_int((raw.get("time") or {}).get("wall_ms"), _wall_ms({})),
            pts_us=_as_int((raw.get("time") or {}).get("pts_us"), 0),
            stream=raw.get("stream") or {}, metrics=raw.get("metrics") or {},
            summary=raw.get("summary") or {}, render=raw.get("render") or {},
            extensions=extensions,
        )
        result["payload"] = payload
        result["content_type"] = content_type
        result["profile"] = profile
        return result

    def _fallback(self, raw: dict, value: object, *, profile: str) -> List[dict]:
        body = _compact_json(value)
        if len(body) > MAX_RENDER_LEN:
            body = _compact_json({
                "schema": SCHEMA,
                "schema_version": SCHEMA_VERSION,
                "id": raw.get("id"),
                "error": "formatted payload exceeds limit",
            })
        return [self._formatted(
            raw, payload=body.decode("utf-8", "replace"),
            content_type="application/json", profile=profile, index=0,
        )]

    def _format_builtin(self, raw: dict) -> List[dict]:
        task = str((raw.get("extensions") or {}).get("task_type_name") or "unknown")
        # Recreate the parser-facing context from the canonical typed entries.
        parser_view = {
            "task_type": (raw.get("extensions") or {}).get("task_type"),
            "task_type_name": task,
            "timestamp_ms": (raw.get("time") or {}).get("wall_ms"),
            "timestamp": (raw.get("time") or {}).get("wall_ms"),
            "model_id": (raw.get("source") or {}).get("model_id"),
        }
        section_key = "keypoints" if task in ("keypoint", "keypoints") else task
        if section_key == "keypoints":
            parser_view[section_key] = {
                "instances": raw.get("results") or [],
                "count": len(raw.get("results") or []),
            }
        else:
            parser_view[section_key] = {
                "entries": raw.get("results") or [],
                "count": len(raw.get("results") or []),
            }
        templates = self._builtin_templates()
        template = templates.get(section_key) or templates.get("default")
        if template is None:
            return self._fallback(raw, parser_view, profile="builtin:raw")
        context = dict(parser_view)
        context["data"] = parser_view
        context["inference"] = parser_view
        try:
            body = _bounded_render(template, **context).encode("utf-8")
            if not body.strip() or len(body) > MAX_RENDER_LEN:
                raise ValueError("empty or oversized template output")
        except Exception:
            return self._fallback(raw, parser_view, profile="builtin:raw-fallback")
        content_type = "text/plain; charset=utf-8"
        try:
            json.loads(body.decode("utf-8"))
            content_type = "application/json"
        except (UnicodeDecodeError, ValueError):
            pass
        return [self._formatted(
            raw, payload=body.decode("utf-8", "replace"),
            content_type=content_type, profile="builtin:%s" % section_key,
            index=0,
        )]

    def format(self, raw: dict) -> List[dict]:
        source = raw.get("source") or {}
        if source.get("kind") == "builtin":
            return self._format_builtin(raw)
        app_id = str(source.get("app_id") or source.get("id") or "")
        legacy = self._legacy_view(raw)
        public_raw = {key: value for key, value in raw.items()
                      if not str(key).startswith("_")}
        try:
            formatter, mode = self._app_formatter(app_id)
            messages = formatter.format(legacy, channel="ws")
        except Exception:
            return self._fallback(raw, public_raw, profile="app:raw-fallback")
        formatted = []
        for index, message in enumerate(messages or []):
            try:
                body = bytes(message.body)
                if not body.strip() or len(body) > MAX_RENDER_LEN:
                    continue
                formatted.append(self._formatted(
                    raw, payload=body.decode("utf-8", "replace"),
                    content_type=str(message.content_type or "application/octet-stream"),
                    profile="app:%s" % mode, index=index, topic=message.topic,
                    retain=bool(message.retain), metadata=message.metadata,
                ))
            except Exception:
                continue
        return formatted or self._fallback(
            raw, public_raw, profile="app:raw-fallback")


@dataclass
class _FormatBatch:
    """One authenticated ingress batch and its lazy immutable projection."""

    batch_id: str
    projection: dict
    delivery: str = "data"
    formatted: Tuple[dict, ...] = ()
    formatted_bytes: Tuple[bytes, ...] = ()
    formatted_ready: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False,
                                 compare=False)


@dataclass(frozen=True)
class _Record:
    raw: dict
    raw_bytes: bytes
    format_batch: _FormatBatch

    @property
    def batch_id(self) -> str:
        return self.format_batch.batch_id

    @property
    def formatted(self) -> Tuple[dict, ...]:
        return self.format_batch.formatted

    @property
    def formatted_bytes(self) -> Tuple[bytes, ...]:
        return self.format_batch.formatted_bytes

    @property
    def source_id(self) -> str:
        return str((self.raw.get("source") or {}).get("id") or "")

    @property
    def message_type(self) -> str:
        return str(self.raw.get("type") or "status")

    @property
    def event_delivery(self) -> str:
        if self.message_type != "event":
            return ""
        value = str((self.raw.get("extensions") or {}).get("delivery") or "state")
        return "edge" if value == "edge" else "state"

    @property
    def event_kind(self) -> str:
        value = (self.raw.get("extensions") or {}).get("event_kind")
        if value:
            return str(value)
        events = self.raw.get("events") or []
        return _event_kind(events[0]) if events and isinstance(events[0], dict) else "event"


def _record(raw: dict, format_batch: _FormatBatch) -> _Record:
    return _Record(raw=raw, raw_bytes=_compact_json(raw),
                   format_batch=format_batch)


@dataclass(frozen=True)
class _Subscription:
    view: str = "raw"
    sources: frozenset = frozenset(("*",))
    types: frozenset = frozenset(DATA_TYPES)

    def accepts(self, record: _Record) -> bool:
        return (("*" in self.sources or record.source_id in self.sources)
                and record.message_type in self.types)


def _parse_subscription(value: object) -> _Subscription:
    if not isinstance(value, dict) or value.get("type") != "subscribe":
        raise ResultHubError("expected subscribe object")
    view = value.get("view", "raw")
    if view not in VIEWS:
        raise ResultHubError("subscribe.view must be raw or formatted")
    sources = value.get("sources", ["*"])
    if (not isinstance(sources, list) or not sources or len(sources) > 64
            or any(not isinstance(item, str) or not item or len(item) > 128
                   for item in sources)):
        raise ResultHubError("subscribe.sources must be a non-empty string list")
    types = value.get("types", sorted(DATA_TYPES))
    if (not isinstance(types, list) or not types or len(types) > len(DATA_TYPES)
            or any(item not in DATA_TYPES for item in types)):
        raise ResultHubError("subscribe.types contains an unsupported type")
    return _Subscription(view=view, sources=frozenset(sources), types=frozenset(types))


def _ws_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    header = bytearray([0x80 | (opcode & 0x0F)])
    size = len(payload)
    if size < 126:
        header.append(size)
    elif size < 65536:
        header.append(126)
        header += struct.pack(">H", size)
    else:
        header.append(127)
        header += struct.pack(">Q", size)
    return bytes(header) + payload


def _recv_exact(conn: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = conn.recv(size - len(data))
        if not chunk:
            raise ConnectionError("websocket client closed")
        data.extend(chunk)
    return bytes(data)


def _read_client_frame(conn: socket.socket, max_payload: int = 16 * 1024):
    first, second = _recv_exact(conn, 2)
    fin = bool(first & 0x80)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    size = second & 0x7F
    if not fin:
        raise ResultHubError("fragmented websocket messages are not supported")
    if not masked:
        raise ResultHubError("client websocket frames must be masked")
    if size == 126:
        size = struct.unpack(">H", _recv_exact(conn, 2))[0]
    elif size == 127:
        size = struct.unpack(">Q", _recv_exact(conn, 8))[0]
    if size > max_payload:
        raise ResultHubError("websocket control message is too large")
    mask = _recv_exact(conn, 4)
    payload = bytearray(_recv_exact(conn, size))
    for index in range(size):
        payload[index] ^= mask[index & 3]
    return opcode, bytes(payload)


class _HubClient:
    """One subscriber with bounded, semantic latest-wins buffering."""

    def __init__(self, conn: socket.socket, ip: str, server,
                 *, max_queue: int, send_timeout: float, lag_limit: int):
        self.conn = conn
        self.ip = ip
        self.server = server
        self.subscription = _Subscription()
        self._max_queue = max(4, int(max_queue))
        self._send_timeout = max(0.05, float(send_timeout))
        self._lag_limit = max(1, int(lag_limit))
        self._items = deque()
        self._condition = threading.Condition()
        self._alive = True
        self._drops = 0
        self._lag_strikes = 0
        self._event_drops = 0
        self._edge_drops = 0
        self._state_drops = 0
        self._state_replaced = 0
        self._formatted_seen = set()
        self._formatted_pending = set()
        self._formatted_order = deque()
        self._formatted_seen_limit = max(256, self._max_queue * 8)
        self._writer = threading.Thread(target=self._write_loop, daemon=True,
                                        name="result-hub-ws-writer")
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name="result-hub-ws-reader")

    def alive(self) -> bool:
        with self._condition:
            return self._alive

    def start(self) -> None:
        self._writer.start()
        self._reader.start()

    def _drop_frame(self) -> bool:
        for index, item in enumerate(self._items):
            if item[0] == "frame" and item[3]:
                del self._items[index]
                return True
        return False

    def _append(self, kind: str, key: str, frame: bytes, *, data: bool,
                replace: bool = False, delivery: str = "") -> bool:
        with self._condition:
            if not self._alive:
                return False
            replaceable = (kind in ("frame", "status", "metrics")
                           or (kind == "event" and delivery == "state"))
            if replace and replaceable:
                before = len(self._items)
                self._items = deque(
                    item for item in self._items
                    if not (item[3] and item[0] == kind and item[1] == key
                            and (kind != "event" or item[4] == "state")))
                if kind == "event":
                    self._state_replaced += before - len(self._items)
            saturated = len(self._items) >= self._max_queue
            if saturated:
                dropped = False
                incoming_state = kind == "event" and delivery == "state"
                if not incoming_state:
                    dropped = self._drop_frame()
                if not dropped and not incoming_state:
                    # After stale frames, status/metrics are the only disposable
                    # data.  A live frame/status must NEVER evict a queued event.
                    for index, item in enumerate(self._items):
                        if item[3] and item[0] in ("status", "metrics"):
                            del self._items[index]
                            dropped = True
                            break
                if not dropped:
                    # State events are lower priority than edges and live video.
                    # They may replace/evict another state slot, but never an
                    # edge.  An incoming edge may reclaim a state slot.
                    for index, item in enumerate(self._items):
                        if item[3] and item[0] == "event" and item[4] == "state":
                            del self._items[index]
                            self._state_replaced += 1
                            dropped = True
                            break
                if not dropped:
                    # Only edge/control records remain.  Keep those; a dropped
                    # edge remains recoverable from the Hub replay ring.
                    if kind == "event":
                        self._event_drops += 1
                        if delivery == "edge":
                            self._edge_drops += 1
                        else:
                            self._state_drops += 1
                    self._drops += 1
                    self._lag_strikes += 1
                    if self._lag_strikes > self._lag_limit:
                        self._alive = False
                        self._condition.notify_all()
                    return False
                self._drops += 1
                self._lag_strikes += 1
                if self._lag_strikes > self._lag_limit:
                    self._alive = False
                    self._condition.notify_all()
                    return False
            else:
                self._lag_strikes = 0
            self._items.append((kind, key, frame, data, delivery))
            self._condition.notify()
            return True

    def offer_control(self, envelope: dict) -> None:
        self._append(str(envelope.get("type") or "status"), "control",
                     _ws_frame(_compact_json(envelope)), data=False)

    def offer_pong(self, payload: bytes) -> None:
        self._append("pong", "control", _ws_frame(payload, opcode=0xA), data=False)

    def reserve_replay(self, record_count: int) -> None:
        """Ensure one atomic snapshot can be queued without semantic drops.

        The configured queue is the steady-state floor.  Replay can contain the
        complete bounded edge ring plus latest frame/status records and occurs
        before the initial writer starts, so it needs a temporary/state-derived
        capacity rather than applying live-client lag policy to history.
        """
        with self._condition:
            required = len(self._items) + max(0, int(record_count)) + 1
            if required > self._max_queue:
                self._max_queue = required
                self._formatted_seen_limit = max(
                    self._formatted_seen_limit, self._max_queue * 8)

    def offer_record(self, record: _Record) -> None:
        subscription = self.subscription
        if not subscription.accepts(record):
            return
        if subscription.view == "raw":
            values = (record.raw_bytes,)
        else:
            # Canonical raw records are split for delivery semantics, while the
            # template sees their one original authenticated ingress batch.
            # A client therefore receives that projection once even when the
            # same batch survives as frame + status + several replay events.
            with self._condition:
                if (record.batch_id in self._formatted_seen
                        or record.batch_id in self._formatted_pending):
                    return
                self._formatted_pending.add(record.batch_id)
            if not self.server.hub.request_formatted(self, record):
                self.cancel_formatted(record.batch_id)
            return
        if record.message_type == "frame":
            key = "%s:%s" % (
                record.source_id,
                str((record.raw.get("stream") or {}).get("id") or ""))
        elif record.message_type == "event":
            key = ("%s:%s" % (record.source_id, record.event_kind)
                   if record.event_delivery == "state"
                   else str(record.raw.get("id") or record.source_id))
        else:
            key = record.source_id
        for index, value in enumerate(values):
            self._append(record.message_type, key, _ws_frame(value), data=True,
                         replace=(index == 0), delivery=record.event_delivery)

    def cancel_formatted(self, batch_id: str) -> None:
        with self._condition:
            self._formatted_pending.discard(batch_id)

    def offer_formatted_ready(self, record: _Record) -> bool:
        with self._condition:
            if record.batch_id not in self._formatted_pending:
                return False
            self._formatted_pending.discard(record.batch_id)
            subscription = self.subscription
            if (subscription.view != "formatted"
                    or not subscription.accepts(record) or not self._alive):
                return False
        delivery = record.format_batch.delivery
        if delivery == "edge":
            kind, key, queue_delivery = "event", record.batch_id, "edge"
        elif delivery == "state":
            kind, key, queue_delivery = "event", record.source_id, "state"
        elif record.message_type == "frame":
            kind = "frame"
            key = "%s:%s" % (
                record.source_id,
                str((record.raw.get("stream") or {}).get("id") or ""))
            queue_delivery = ""
        else:
            kind, key, queue_delivery = record.message_type, record.source_id, ""
        accepted = not record.formatted_bytes
        for index, value in enumerate(record.formatted_bytes):
            accepted = self._append(
                kind, key, _ws_frame(value), data=True, replace=(index == 0),
                delivery=queue_delivery) or accepted
        if accepted:
            with self._condition:
                self._formatted_seen.add(record.batch_id)
                self._formatted_order.append(record.batch_id)
                while len(self._formatted_order) > self._formatted_seen_limit:
                    self._formatted_seen.discard(self._formatted_order.popleft())
        return accepted

    def replace_subscription(self, subscription: _Subscription) -> None:
        with self.server.hub._publish_fence:
            with self._condition:
                self.subscription = subscription
                self._items = deque(item for item in self._items if not item[3])
                self._formatted_seen.clear()
                self._formatted_pending.clear()
                self._formatted_order.clear()
            self.server.replay(self)

    def purge_source(self, source_id: str) -> int:
        """Remove queued data from an app whose generation just changed."""
        prefix = str(source_id) + ":"
        event_prefix = "evt:%s:" % source_id
        batch_prefix = "batch:%s:" % source_id
        with self._condition:
            before = len(self._items)
            self._items = deque(
                item for item in self._items
                if not (item[3] and (
                    item[1] == source_id or item[1].startswith(prefix)
                    or item[1].startswith(event_prefix)
                    or item[1].startswith(batch_prefix))))
            self._formatted_order = deque(
                value for value in self._formatted_order
                if not value.startswith(batch_prefix))
            self._formatted_seen = set(self._formatted_order)
            self._formatted_pending = {
                value for value in self._formatted_pending
                if not value.startswith(batch_prefix)}
            return before - len(self._items)

    def _write_loop(self) -> None:
        try:
            while True:
                with self._condition:
                    while self._alive and not self._items:
                        self._condition.wait(timeout=0.5)
                    if not self._alive and not self._items:
                        break
                    _kind, _key, frame, _data, _delivery = self._items.popleft()
                self.conn.settimeout(self._send_timeout)
                self.conn.sendall(frame)
        except Exception:
            pass
        finally:
            self.close()

    def _read_loop(self) -> None:
        try:
            self.conn.settimeout(None)
            while self.alive():
                opcode, payload = _read_client_frame(self.conn)
                if opcode == 0x8:
                    break
                if opcode == 0x9:
                    self.offer_pong(payload[:125])
                    continue
                if opcode != 0x1:
                    raise ResultHubError("only text subscribe messages are accepted")
                try:
                    subscription = _parse_subscription(
                        json.loads(payload.decode("utf-8")))
                except (UnicodeDecodeError, ValueError, ResultHubError) as exc:
                    self.server.subscription_rejected(self, str(exc))
                    continue
                self.replace_subscription(subscription)
        except Exception:
            pass
        finally:
            self.close()

    def close(self) -> None:
        with self._condition:
            self._alive = False
            self._condition.notify_all()
        try:
            self.conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.conn.close()
        except OSError:
            pass

    def status(self) -> dict:
        with self._condition:
            return {
                "alive": self._alive,
                "view": self.subscription.view,
                "queued": len(self._items),
                "dropped": self._drops,
                "lag_strikes": self._lag_strikes,
                "event_dropped": self._event_drops,
                "edge_dropped": self._edge_drops,
                "state_dropped": self._state_drops,
                "state_replaced": self._state_replaced,
            }


class _HubWebSocketServer:
    def __init__(self, hub, *, host: str, port: int,
                 max_clients: int = WS_MAX_CLIENTS,
                 max_per_ip: int = WS_MAX_PER_IP,
                 client_queue: int = WS_CLIENT_QUEUE,
                 send_timeout: float = WS_SEND_TIMEOUT,
                 lag_limit: int = WS_LAG_LIMIT):
        self.hub = hub
        self.host = effective_bind_host(host)
        self.port = int(port)
        self.max_clients = max(1, int(max_clients))
        self.max_per_ip = max(1, int(max_per_ip))
        self.client_queue = max(4, int(client_queue))
        self.send_timeout = float(send_timeout)
        self.lag_limit = int(lag_limit)
        self._server: Optional[socket.socket] = None
        self._clients: List[_HubClient] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.rejected = 0

    def start(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self.host, self.port))
        self.port = server.getsockname()[1]
        server.listen(16)
        server.settimeout(0.5)
        self._server = server
        self._stop.clear()
        self._thread = threading.Thread(target=self._accept_loop, daemon=True,
                                        name="result-hub-ws-accept")
        self._thread.start()
        return self

    @staticmethod
    def _handshake(conn: socket.socket) -> None:
        conn.settimeout(5.0)
        request = bytearray()
        while b"\r\n\r\n" not in request:
            chunk = conn.recv(2048)
            if not chunk:
                raise ConnectionError("client closed during websocket handshake")
            request.extend(chunk)
            if len(request) > 65536:
                raise ResultHubError("websocket handshake is too large")
        lines = bytes(request).split(b"\r\n")
        if not lines or not lines[0].startswith(b"GET "):
            raise ResultHubError("invalid websocket request line")
        headers = {}
        for line in lines[1:]:
            if b":" in line:
                key, value = line.split(b":", 1)
                headers[key.strip().lower()] = value.strip()
        key = headers.get(b"sec-websocket-key")
        if (not key or headers.get(b"sec-websocket-version") != b"13"
                or b"websocket" not in headers.get(b"upgrade", b"").lower()):
            raise ResultHubError("invalid websocket upgrade headers")
        accept = base64.b64encode(
            hashlib.sha1(key + _WS_GUID.encode("ascii")).digest())
        conn.sendall(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n")

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, address = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            ip = str(address[0] if address else "?")
            try:
                with self._lock:
                    self._clients = [item for item in self._clients if item.alive()]
                    if len(self._clients) >= self.max_clients:
                        raise ResultHubError("websocket client limit reached")
                    if sum(1 for item in self._clients if item.ip == ip) >= self.max_per_ip:
                        raise ResultHubError("websocket per-IP client limit reached")
                self._handshake(conn)
                client = _HubClient(
                    conn, ip, self, max_queue=self.client_queue,
                    send_timeout=self.send_timeout, lag_limit=self.lag_limit)
                # Queue hello before making the client visible to broadcasts: it
                # is therefore always the first WebSocket message.
                client.offer_control(self.hub.hello_envelope())
                # Snapshot capture + all replay offers share ResultHub's final
                # publication fence, so a live latest-wins record cannot be
                # queued and then overwritten by an older replay copy.
                with self.hub._publish_fence:
                    with self._lock:
                        self._clients.append(client)
                    self.replay(client)
                client.start()
            except Exception:
                self.rejected += 1
                try:
                    conn.close()
                except OSError:
                    pass

    def replay(self, client: _HubClient) -> None:
        records = self.hub.snapshot_records()
        subscription = client.subscription
        matching = [item for item in records if subscription.accepts(item)]
        count = (len({item.batch_id for item in matching})
                 if subscription.view == "formatted" else len(matching))
        # Reserve before enqueueing the snapshot control.  In the default raw
        # view every matching record is queued synchronously; this guarantees
        # the announced count and actual initial replay cannot diverge merely
        # because event_replay (128) exceeds the steady client queue (64).
        client.reserve_replay(len(matching))
        client.offer_control(self.hub.snapshot_envelope(
            subscription, count))
        for record in matching:
            client.offer_record(record)

    def subscription_rejected(self, client: _HubClient, error: str) -> None:
        self.rejected += 1
        client.offer_control(self.hub.control_status(
            {"subscription": "rejected", "error": error[:256]}))

    def broadcast(self, record: _Record) -> None:
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            client.offer_record(record)
        if any(not item.alive() for item in clients):
            with self._lock:
                self._clients = [item for item in self._clients if item.alive()]

    def purge_source(self, source_id: str) -> int:
        with self._lock:
            clients = list(self._clients)
        return sum(client.purge_source(source_id) for client in clients)

    def status(self) -> dict:
        with self._lock:
            self._clients = [item for item in self._clients if item.alive()]
            clients = list(self._clients)
        return {
            "subscribers": len(clients),
            "raw_subscribers": sum(item.subscription.view == "raw" for item in clients),
            "formatted_subscribers": sum(
                item.subscription.view == "formatted" for item in clients),
            "client_dropped": sum(item.status()["dropped"] for item in clients),
            "client_event_dropped": sum(
                item.status()["event_dropped"] for item in clients),
            "client_edge_dropped": sum(
                item.status()["edge_dropped"] for item in clients),
            "client_state_dropped": sum(
                item.status()["state_dropped"] for item in clients),
            "client_state_replaced": sum(
                item.status()["state_replaced"] for item in clients),
            "rejected": self.rejected,
        }

    def stop(self) -> None:
        self._stop.set()
        server, self._server = self._server, None
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
        with self._lock:
            clients, self._clients = self._clients, []
        for client in clients:
            client.close()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)
        self._thread = None


def _peer_credentials(conn: socket.socket) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    option = getattr(socket, "SO_PEERCRED", None)
    if option is None:
        return None, None, None
    try:
        raw = conn.getsockopt(socket.SOL_SOCKET, option, struct.calcsize("3i"))
        pid, uid, gid = struct.unpack("3i", raw)
        return (int(pid) if pid > 1 else None, int(uid), int(gid))
    except (OSError, struct.error):
        return None, None, None


def _secure_pidfile(path: str, owner_uid: int) -> Optional[Tuple[int, tuple]]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != owner_uid
                or before.st_mode & 0o022):
            return None
        content = os.read(descriptor, 65)
        if len(content) > 64 or not re.fullmatch(rb"[1-9][0-9]*\s*", content):
            return None
        after = os.fstat(descriptor)
        path_value = os.lstat(path)
        signature = (before.st_dev, before.st_ino, before.st_uid,
                     stat.S_IMODE(before.st_mode), before.st_size,
                     before.st_mtime_ns)
        if ((after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
                or (path_value.st_dev, path_value.st_ino)
                != (before.st_dev, before.st_ino)
                or stat.S_ISLNK(path_value.st_mode)):
            return None
        return int(content.strip()), signature
    except (OSError, ValueError):
        return None
    finally:
        os.close(descriptor)


def _proc_starttime(proc_dir: str) -> Optional[int]:
    try:
        with open(os.path.join(proc_dir, "stat"), encoding="utf-8") as source:
            value = source.read(64 * 1024)
        end = value.rfind(")")
        fields = value[end + 2:].split() if end >= 0 else []
        # Fields after comm begin at proc stat field 3; starttime is field 22.
        return int(fields[19]) if len(fields) > 19 else None
    except (OSError, ValueError):
        return None


def resolve_builtin_notify_identity(
        peer_pid: Optional[int], hello: dict, *, proc_root: str = "/proc",
        pidfile: str = "/var/run/notify_server.pid", owner_uid: int = 0
        ) -> Optional[dict]:
    """Production identity resolver for ``recamera_notify.notify_server``.

    The hello claim is never sufficient.  SO_PEERCRED PID must match the
    root-owned, non-writable service pidfile and a stable /proc instance; all
    real/effective/saved/fs UIDs, Python executable and exact installed module
    argv are checked.  The pidfile and starttime are read again after argv so a
    concurrent PID reuse or service restart fails closed.
    """
    if (peer_pid is None or not isinstance(hello, dict)
            or frozenset(hello) != SYSTEM_HELLO_KEYS
            or hello.get("type") != "hello"
            or hello.get("protocol") != SYSTEM_PROTOCOL
            or hello.get("source") != "builtin"):
        return None
    authoritative = _secure_pidfile(pidfile, int(owner_uid))
    if authoritative is None or authoritative[0] != peer_pid:
        return None
    proc_dir = os.path.join(proc_root, str(peer_pid))
    starttime = _proc_starttime(proc_dir)
    if starttime is None:
        return None
    try:
        with open(os.path.join(proc_dir, "status"), encoding="utf-8") as source:
            status_text = source.read(64 * 1024)
        uid_line = next(line for line in status_text.splitlines() if line.startswith("Uid:"))
        uids = [_as_int(value, -1) for value in uid_line.split()[1:]]
        if len(uids) != 4 or any(value != int(owner_uid) for value in uids):
            return None
        with open(os.path.join(proc_dir, "cmdline"), "rb") as source:
            argv = [part.decode("utf-8", "replace") for part in source.read(64 * 1024).split(b"\0") if part]
        executable = os.readlink(os.path.join(proc_dir, "exe"))
    except (OSError, StopIteration):
        return None
    if not re.fullmatch(r"/(?:oem/)?usr/bin/python3(?:\.[0-9]+)?", executable):
        return None
    if (len(argv) < 3 or os.path.basename(argv[0]) not in (
            "python3", "python3.11")
            or argv[1:3] != ["-m", "recamera_notify.notify_server"]):
        return None
    repeated = _secure_pidfile(pidfile, int(owner_uid))
    if (repeated is None or repeated != authoritative
            or _proc_starttime(proc_dir) != starttime):
        return None
    return {"kind": "builtin", "id": "builtin",
            "trust": "peercred+pidfile+proc", "pid": int(peer_pid),
            "starttime": int(starttime)}


class _ObserverWorker:
    def __init__(self, callback: Callable[[dict], None], max_queue: int):
        self.callback = callback
        owner = getattr(callback, "__self__", None)
        invalidator = getattr(callback, "invalidate_source", None)
        if not callable(invalidator) and owner is not None:
            invalidator = getattr(owner, "invalidate_source", None)
        self._invalidator = invalidator if callable(invalidator) else None
        self._max_queue = max(1, int(max_queue))
        self._condition = threading.Condition()
        # Entries are ("data", envelope).  Source revocation removes matching
        # entries under this condition and synchronously calls the explicitly
        # non-blocking owner hook from ResultHub's publish fence.  That hook
        # advances the observer's source epoch while an old callback is still
        # in flight; the callback must recheck the epoch before side effects.
        self._items = deque()
        self._closed = False
        self.delivered = 0
        self.dropped = 0
        self.errors = 0
        self.invalidations = 0
        self.invalidated_queued = 0
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="result-hub-observer")
        self._thread.start()

    def offer(self, envelope: dict) -> None:
        with self._condition:
            if self._closed:
                return
            if len(self._items) >= self._max_queue:
                # Drop oldest data under pressure; lifecycle revocation is not
                # represented as a queue entry and therefore cannot be lost.
                victim = next((index for index, item in enumerate(self._items)
                               if item[0] == "data"), None)
                if victim is None:
                    self.dropped += 1
                    return
                del self._items[victim]
                self.dropped += 1
            self._items.append(("data", envelope))
            self._condition.notify()

    @staticmethod
    def _matches_source(envelope: dict, source_id: str) -> bool:
        source = envelope.get("source") if isinstance(envelope, dict) else None
        return (isinstance(source, dict)
                and str(source.get("app_id") or source.get("id") or "")
                == source_id)

    def invalidate_source(self, source_id: str, *, identity=None,
                          capability=None) -> None:
        """Synchronously advance an observer's non-blocking revoke fence.

        Queued records for the source are removed.  The observer owner's
        optional ``invalidate_source(source_id, identity=..., capability=...)``
        hook executes before refresh returns and before a subsequently offered
        record.  The hook contract is deliberately strict: it may only update
        in-memory epoch/cache state and signal its own worker; it must not do
        I/O or other blocking work.  Observer callback failures remain isolated.
        """
        source_id = str(source_id or "")
        if not source_id:
            return
        with self._condition:
            if self._closed:
                return
            kept = deque()
            removed = 0
            for kind, value in self._items:
                if kind == "data" and self._matches_source(value, source_id):
                    removed += 1
                    continue
                kept.append((kind, value))
            self._items = kept
            self.invalidated_queued += removed
        if self._invalidator is None:
            return
        try:
            self._invalidator(
                source_id, identity=copy.deepcopy(identity),
                capability=copy.deepcopy(capability))
            self.invalidations += 1
        except Exception:
            self.errors += 1

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._closed and not self._items:
                    self._condition.wait(timeout=0.5)
                if self._closed and not self._items:
                    return
                kind, value = self._items.popleft()
            try:
                self.callback(copy.deepcopy(value))
                self.delivered += 1
            except Exception:
                self.errors += 1

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._items.clear()
            self._condition.notify_all()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)

    def status(self) -> dict:
        with self._condition:
            queued = len(self._items)
        return {"delivered": self.delivered, "dropped": self.dropped,
                "errors": self.errors, "queued": queued,
                "invalidations": self.invalidations,
                "invalidated_queued": self.invalidated_queued}


class ResultHub:
    """Canonical result state, WebSocket, and authenticated system ingress."""

    def __init__(self, *, ws_host: Optional[str] = None,
                 ws_port: Optional[int] = None,
                 system_uds_path: Optional[str] = None,
                 system_identity_resolver: Optional[Callable[..., Optional[dict]]] = None,
                 formatter: Optional[ResultViewFormatter] = None,
                 max_line: int = 512 * 1024, max_system_publishers: int = 2,
                 ingress_queue: int = 256, event_replay: int = 128,
                 event_ttl_s: float = 30.0, observer_queue: int = 64,
                 ws_client_queue: int = WS_CLIENT_QUEUE):
        self.ws_host = effective_bind_host(ws_host or paths.RESULT_HUB_HOST)
        if self.ws_host != "127.0.0.1":
            raise ResultHubError(
                "canonical Result Hub WebSocket must bind loopback")
        self.ws_port = paths.RESULT_HUB_PORT if ws_port is None else int(ws_port)
        self.system_uds_path = system_uds_path or paths.SYSTEM_RESULT_SOCK
        self.system_identity_resolver = system_identity_resolver
        self.formatter = formatter or ResultViewFormatter()
        self.max_line = max(4096, int(max_line))
        self.max_system_publishers = max(1, int(max_system_publishers))
        self.ingress_queue = max(8, int(ingress_queue))
        self.event_replay = max(1, int(event_replay))
        self.event_ttl_s = max(1.0, float(event_ttl_s))
        self.observer_queue = max(1, int(observer_queue))
        self.ws_client_queue = max(4, int(ws_client_queue))
        self._stop = threading.Event()
        self._ws: Optional[_HubWebSocketServer] = None
        self._system_server: Optional[socket.socket] = None
        self._system_socket_identity = None
        self._system_thread: Optional[threading.Thread] = None
        self._system_publishers = set()
        self._state_lock = threading.RLock()
        self._ingress_condition = threading.Condition()
        self._ingress = deque()
        self._ingress_thread: Optional[threading.Thread] = None
        self._format_condition = threading.Condition()
        self._format_jobs = deque()
        self._format_queue_max = max(16, min(256, self.ingress_queue))
        self._format_thread: Optional[threading.Thread] = None
        self._latest_frames: Dict[Tuple[str, str], _Record] = {}
        self._latest_status: Dict[Tuple[str, str], _Record] = {}
        self._events = deque()
        self._event_ids = set()
        self._observers: Dict[Callable[[dict], None], _ObserverWorker] = {}
        # Serializes the final app-generation CAS, state commit and bounded WS
        # queue offer with generation refresh/purge.  Normalization and lazy
        # template work stay outside this lock.
        self._publish_fence = threading.RLock()
        # Current control-plane-authorized (instance, generation) per app.  The
        # gateway hello refreshes this before acknowledging the publisher.
        self._app_generations: Dict[str, Tuple[str, int]] = {}
        # app_id -> (instance_id, generation, immutable manifest render copy,
        #            compiled geometry contract, trusted stream contract).
        # This cache is populated only after the gateway has authenticated the
        # exact live coordinator identity.  Result ingress never stats or opens
        # a manifest, and an old generation cannot inherit a new declaration.
        self._app_render_cache: Dict[
            str, Tuple[str, int, dict, dict, dict]] = {}
        self._source_seq: Dict[str, int] = {}
        self._control_seq = 0
        self._batch_seq = 0
        self._received_app = 0
        self._received_system = 0
        self._published = {key: 0 for key in DATA_TYPES}
        self._rejected = 0
        self._oversize = 0
        self._ingress_dropped = 0
        self._ingress_edge_dropped = 0
        self._ingress_state_dropped = 0
        self._ingress_data_dropped = 0
        self._event_duplicates = 0
        self._event_state_dropped = 0
        self._event_state_replaced = 0
        self._event_state_evicted = 0
        self._event_edge_evicted = 0
        self._format_errors = 0
        self._formatted_batches = 0
        self._format_queue_dropped = 0
        self._format_queue_edge_dropped = 0
        self._observer_errors = 0
        self._generation_records_purged = 0
        self._generation_ingress_purged = 0
        self._generation_client_purged = 0
        self._stale_app_rejected = 0

    def _next_source_seq(self, source_id: str) -> int:
        with self._state_lock:
            value = self._source_seq.get(source_id, 0) + 1
            self._source_seq[source_id] = value
            return value

    def _control(self, message_type: str, *, summary=None, extensions=None) -> dict:
        with self._state_lock:
            self._control_seq += 1
            seq = self._control_seq
        return _base_envelope(
            message_type=message_type, message_id=f"result-hub:{seq}:{message_type}",
            source={"kind": "builtin", "id": "result-hub", "trust": "local"},
            seq=seq, wall_ms=int(time.time() * 1000), pts_us=0,
            stream={"id": "", "width": None, "height": None,
                    "coordinate_space": "unknown"},
            summary=summary, extensions=extensions,
        )

    def hello_envelope(self) -> dict:
        return self._control("hello", extensions={
            "views": sorted(VIEWS),
            "source_kinds": ["builtin", "app"],
            "types": sorted(DATA_TYPES),
            "subscribe": {"type": "subscribe", "view": "raw",
                          "sources": ["*"], "types": sorted(DATA_TYPES)},
            "event_replay": {"max": self.event_replay, "ttl_s": self.event_ttl_s},
        })

    def snapshot_envelope(self, subscription: _Subscription, count: int) -> dict:
        return self._control("snapshot", extensions={
            "view": subscription.view,
            "sources": sorted(subscription.sources),
            "types": sorted(subscription.types),
            "records": int(count),
        })

    def control_status(self, summary: dict) -> dict:
        return self._control("status", summary=summary)

    def _prepare_system_path(self) -> None:
        parent = os.path.dirname(self.system_uds_path)
        os.makedirs(parent, mode=0o755, exist_ok=True)
        try:
            value = os.lstat(self.system_uds_path)
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(value.st_mode):
            raise ResultHubError("refusing to replace non-socket path %s" %
                                 self.system_uds_path)
        os.unlink(self.system_uds_path)

    def start(self):
        if self._system_server is not None:
            return self
        self._stop.clear()
        self._ingress_thread = threading.Thread(
            target=self._ingress_loop, daemon=True, name="result-hub-ingress")
        self._ingress_thread.start()
        self._format_thread = threading.Thread(
            target=self._format_loop, daemon=True, name="result-hub-formatter")
        self._format_thread.start()
        try:
            self._ws = _HubWebSocketServer(
                self, host=self.ws_host, port=self.ws_port,
                client_queue=self.ws_client_queue).start()
            self.ws_port = self._ws.port
            self._prepare_system_path()
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            # Assign immediately so every bind/chmod/listen failure closes the
            # descriptor through stop(); capture the pathname inode directly
            # after bind so that same failure cleanup can remove only our socket.
            self._system_server = server
            server.bind(self.system_uds_path)
            value = os.lstat(self.system_uds_path)
            self._system_socket_identity = (value.st_dev, value.st_ino)
            os.chmod(self.system_uds_path, 0o660)
            server.listen(self.max_system_publishers)
            server.settimeout(0.5)
            self._system_thread = threading.Thread(
                target=self._system_accept_loop, daemon=True,
                name="result-hub-system-accept")
            self._system_thread.start()
            return self
        except Exception:
            self.stop()
            raise

    def _readline(self, stream) -> bytes:
        line = stream.readline(self.max_line + 2)
        if len(line) > self.max_line or (line and not line.endswith(b"\n")):
            self._oversize += 1
            raise ResultHubError("system result exceeds %d bytes" % self.max_line)
        return line

    @staticmethod
    def _system_ack(conn: socket.socket, ok: bool, error: str = "") -> None:
        value = {"type": "hello_ack", "protocol": SYSTEM_PROTOCOL, "ok": bool(ok)}
        if error:
            value["error"] = error[:256]
        conn.sendall(_compact_json(value) + b"\n")

    def _system_accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _address = self._system_server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._state_lock:
                if len(self._system_publishers) >= self.max_system_publishers:
                    self._rejected += 1
                    conn.close()
                    continue
                self._system_publishers.add(conn)
            threading.Thread(target=self._serve_system, args=(conn,), daemon=True,
                             name="result-hub-system-publisher").start()

    def _serve_system(self, conn: socket.socket) -> None:
        peer_pid, _peer_uid, _peer_gid = _peer_credentials(conn)
        stream = None
        acked = False
        try:
            conn.settimeout(5.0)
            stream = conn.makefile("rb")
            line = self._readline(stream)
            if not line:
                raise ResultHubError("system publisher closed before hello")
            hello = json.loads(line.decode("utf-8"))
            if (not isinstance(hello, dict) or frozenset(hello) != SYSTEM_HELLO_KEYS
                    or hello.get("type") != "hello"
                    or hello.get("protocol") != SYSTEM_PROTOCOL
                    or hello.get("source") != "builtin"):
                raise ResultHubError("invalid system publisher hello")
            if self.system_identity_resolver is None:
                raise ResultHubError("system identity resolver is not configured")
            identity = self.system_identity_resolver(peer_pid, hello)
            if (not isinstance(identity, dict)
                    or str(identity.get("id") or identity.get("source_id")) != "builtin"):
                raise ResultHubError("system publisher identity rejected")
            self._system_ack(conn, True)
            acked = True
            conn.settimeout(None)
            while not self._stop.is_set():
                line = self._readline(stream)
                if not line:
                    break
                try:
                    payload = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    self._rejected += 1
                    continue
                if not isinstance(payload, dict) or payload.get("type") == "hello":
                    self._rejected += 1
                    continue
                self.submit_system(payload, identity)
        except Exception as exc:
            self._rejected += 1
            if not acked:
                try:
                    self._system_ack(conn, False, str(exc))
                except Exception:
                    pass
        finally:
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
            try:
                conn.close()
            except OSError:
                pass
            with self._state_lock:
                self._system_publishers.discard(conn)

    @staticmethod
    def _ingress_delivery(payload: dict) -> str:
        events = payload.get("events")
        if isinstance(events, list) and events:
            return ("edge" if any(is_edge_event(event) for event in events)
                    else "state")
        return ("state" if str(payload.get("type") or "").lower() == "event"
                else "data")

    def _submit(self, kind: str, payload: dict, identity: dict) -> bool:
        if self._stop.is_set() or self._ingress_thread is None:
            return False
        delivery = self._ingress_delivery(payload)
        item = (kind, dict(payload), dict(identity), delivery)
        with self._ingress_condition:
            if len(self._ingress) >= self.ingress_queue:
                replace = None
                source_id = str(identity.get("app_id") or identity.get("id") or "")
                # First coalesce non-edge work from the same source.  If an
                # edge arrives, it may reclaim any data/state slot but never a
                # queued edge.  A state update may reclaim another state slot;
                # ordinary frames do not displace another source's backlog.
                preferences = (("data", "state") if delivery == "edge"
                               else (delivery,))
                for preferred in preferences:
                    for index, old in enumerate(self._ingress):
                        old_source = str(
                            old[2].get("app_id") or old[2].get("id") or "")
                        if old[3] == preferred and (
                                delivery == "edge" or old_source == source_id):
                            replace = index
                            break
                    if replace is not None:
                        break
                if replace is None and delivery == "state":
                    for index, old in enumerate(self._ingress):
                        if old[3] == "state":
                            replace = index
                            break
                if replace is None:
                    self._ingress_dropped += 1
                    if delivery == "edge":
                        self._ingress_edge_dropped += 1
                    elif delivery == "state":
                        self._ingress_state_dropped += 1
                    else:
                        self._ingress_data_dropped += 1
                    return False
                del self._ingress[replace]
                self._ingress_dropped += 1
            self._ingress.append(item)
            self._ingress_condition.notify()
            return True

    def submit_app(self, payload: dict, identity: dict) -> bool:
        if not self._is_current_app_identity(identity):
            self._stale_app_rejected += 1
            return False
        return self._submit("app", payload, identity)

    def submit_system(self, payload: dict, identity: dict) -> bool:
        return self._submit("system", payload, identity)

    @staticmethod
    def _record_is_app(record: _Record, app_id: str) -> bool:
        source = record.raw.get("source") or {}
        return (source.get("kind") == "app"
                and str(source.get("app_id") or source.get("id") or "") == app_id)

    @staticmethod
    def _record_is_generation(record: _Record, instance: str,
                              generation: int) -> bool:
        source = record.raw.get("source") or {}
        return (str(source.get("instance") or "") == instance
                and _as_int(source.get("generation"), -1) == generation)

    def _purge_app_records_locked(self, app_id: str, current=None) -> int:
        def stale(record: _Record) -> bool:
            return (self._record_is_app(record, app_id)
                    and (current is None or not self._record_is_generation(
                        record, current[0], current[1])))

        purged = 0
        for key, record in list(self._latest_frames.items()):
            if stale(record):
                del self._latest_frames[key]
                purged += 1
        for key, record in list(self._latest_status.items()):
            if stale(record):
                del self._latest_status[key]
                purged += 1
        kept = deque()
        for timestamp, record in self._events:
            if stale(record):
                self._event_ids.discard(record.raw.get("id"))
                purged += 1
            else:
                kept.append((timestamp, record))
        self._events = kept
        return purged

    def _purge_app_ingress(self, app_id: str, current=None) -> int:
        with self._ingress_condition:
            before = len(self._ingress)

            def keep(item) -> bool:
                if item[0] != "app":
                    return True
                identity = item[2]
                if str(identity.get("app_id") or "") != app_id:
                    return True
                return bool(current is not None
                            and str(identity.get("instance_id") or "") == current[0]
                            and _as_int(identity.get("generation"), -1) == current[1])

            self._ingress = deque(item for item in self._ingress if keep(item))
            return before - len(self._ingress)

    def _is_current_app_identity(self, identity: dict) -> bool:
        app_id = str((identity or {}).get("app_id") or "")
        instance = str((identity or {}).get("instance_id") or "")
        generation = _as_int((identity or {}).get("generation"), -1)
        with self._state_lock:
            return self._app_generations.get(app_id) == (instance, generation)

    def refresh_app_manifest(self, identity: dict, manifest: dict) -> bool:
        """Cache trusted ``manifest.render`` for one authenticated generation.

        The caller is the appmgr control plane, after
        ``AppCoordinator.resolve_identity`` accepted the SO_PEERCRED-bound
        process.  Untrusted application messages have no path to this method.
        Manifest v2 and an exact id match are required; malformed input clears
        any prior entry for that app and fails closed to an empty render object.
        """
        app_id = str((identity or {}).get("app_id") or "")
        instance = str((identity or {}).get("instance_id") or "")
        generation = _as_int((identity or {}).get("generation"), -1)
        identity_valid = bool(app_id and instance and generation >= 0)
        valid = (
            identity_valid
            and isinstance(manifest, dict)
            and manifest.get("manifest_version") == 2
            and str(manifest.get("id") or "") == app_id
        )
        render = manifest.get("render") if valid else None
        if render is not None and not isinstance(render, dict):
            valid = False
        geometry = _compile_geometry_contract(manifest) if valid else {}
        stream_contract = _compile_stream_contract(manifest) if valid else {}
        current = (instance, generation)
        generation_changed = False
        purged_records = 0
        ws = None
        observers = []
        canonical_identity = (
            {"kind": "app", "id": app_id, "app_id": app_id,
             "instance": instance, "generation": generation}
            if identity_valid else None)
        capability = {
            "valid": bool(valid),
            "render": copy.deepcopy(render or {}) if valid else {},
            "geometry": copy.deepcopy(geometry) if valid else {},
            "stream": copy.deepcopy(stream_contract) if valid else {},
        }
        with self._publish_fence:
            with self._state_lock:
                if identity_valid:
                    generation_changed = self._app_generations.get(app_id) != current
                    self._app_generations[app_id] = current
                    purged_records = self._purge_app_records_locked(app_id, current)
                if valid:
                    self._app_render_cache[app_id] = (
                        instance, generation, copy.deepcopy(render or {}),
                        copy.deepcopy(geometry), copy.deepcopy(stream_contract))
                elif app_id:
                    self._app_render_cache.pop(app_id, None)
                ws = self._ws
                observers = list(self._observers.values())
                self._generation_records_purged += purged_records
            purged_ingress = (self._purge_app_ingress(app_id, current)
                              if identity_valid else 0)
            purged_clients = (ws.purge_source(app_id)
                              if ws is not None and generation_changed else 0)
            # This is deliberately inside the publish ordering domain but is
            # only an in-memory bounded-queue operation.  It cannot run bridge
            # code or block ingress.  New-generation records are offered after
            # this revoke because publish uses the same fence.
            if app_id:
                for observer in observers:
                    observer.invalidate_source(
                        app_id, identity=canonical_identity,
                        capability=capability)
        if purged_ingress or purged_clients:
            with self._state_lock:
                self._generation_ingress_purged += purged_ingress
                self._generation_client_purged += purged_clients
        return valid

    def invalidate_app_manifest(self, app_id: Optional[str] = None) -> None:
        """Revoke trusted generation/render state after lifecycle writes."""
        purged_apps = []
        observers = []
        with self._publish_fence:
            with self._state_lock:
                if app_id is None:
                    purged_apps = list(self._app_generations)
                    self._app_render_cache.clear()
                    self._app_generations.clear()
                    for source_id in purged_apps:
                        self._generation_records_purged += \
                            self._purge_app_records_locked(source_id)
                else:
                    source_id = str(app_id)
                    purged_apps = [source_id]
                    self._app_render_cache.pop(source_id, None)
                    self._app_generations.pop(source_id, None)
                    self._generation_records_purged += \
                        self._purge_app_records_locked(source_id)
                ws = self._ws
                observers = list(self._observers.values())
            ingress = sum(
                self._purge_app_ingress(source_id) for source_id in purged_apps)
            clients = (sum(ws.purge_source(source_id) for source_id in purged_apps)
                       if ws is not None else 0)
            for source_id in purged_apps:
                for observer in observers:
                    observer.invalidate_source(
                        source_id, identity=None,
                        capability={"valid": False, "render": {},
                                    "geometry": {}, "stream": {}})
        with self._state_lock:
            self._generation_ingress_purged += ingress
            self._generation_client_purged += clients

    def _trusted_app_contract(self, identity: dict) -> Tuple[dict, dict, dict]:
        app_id = str(identity.get("app_id") or "")
        instance = str(identity.get("instance_id") or "")
        generation = _as_int(identity.get("generation"), -1)
        with self._state_lock:
            cached = self._app_render_cache.get(app_id)
            if (cached is None or cached[0] != instance
                    or cached[1] != generation):
                return {}, {}, {}
            return (copy.deepcopy(cached[2]), copy.deepcopy(cached[3]),
                    copy.deepcopy(cached[4]))

    def _ingress_loop(self) -> None:
        while True:
            with self._ingress_condition:
                while not self._stop.is_set() and not self._ingress:
                    self._ingress_condition.wait(timeout=0.5)
                if self._stop.is_set() and not self._ingress:
                    break
                kind, payload, identity, _delivery = self._ingress.popleft()
            try:
                if kind == "app":
                    if not self._is_current_app_identity(identity):
                        self._stale_app_rejected += 1
                        continue
                    self.publish_app(payload, identity)
                else:
                    self.publish_system(payload, identity)
            except Exception:
                self._rejected += 1

    def _make_format_batch(self, envelopes: List[dict],
                           legacy_payload: Optional[dict] = None) -> _FormatBatch:
        if not envelopes:
            raise ResultHubError("cannot format an empty ingress batch")
        source = envelopes[0].get("source") or {}
        source_id = str(source.get("id") or "unknown")
        generation = _as_int(source.get("generation"), 0)
        with self._state_lock:
            self._batch_seq += 1
            batch_seq = self._batch_seq
        batch_id = "batch:%s:%s:%s" % (source_id, generation, batch_seq)
        raw_types = list(dict.fromkeys(
            str(value.get("type") or "status") for value in envelopes))
        has_edge = any(
            value.get("type") == "event"
            and (value.get("extensions") or {}).get("delivery") == "edge"
            for value in envelopes)
        has_state = any(value.get("type") == "event" for value in envelopes)
        delivery = "edge" if has_edge else "state" if has_state else "data"
        for value in envelopes:
            extensions = dict(value.get("extensions") or {})
            extensions["batch_id"] = batch_id
            extensions["batch_types"] = list(raw_types)
            value["extensions"] = extensions

        # Frame/event are the most useful stable anchors.  The projection then
        # restores the complete original batch namespace for Kit formatters.
        priority = {"frame": 0, "event": 1, "metrics": 2, "status": 3}
        anchor = min(envelopes, key=lambda item: priority.get(
            str(item.get("type") or "status"), 9))
        projection = copy.deepcopy(anchor)
        projection["id"] = batch_id
        projection["results"] = next((copy.deepcopy(value.get("results") or [])
                                      for value in envelopes
                                      if value.get("results")), [])
        projection["events"] = [
            copy.deepcopy(event)
            for value in envelopes for event in (value.get("events") or [])]
        metrics = {}
        summary = {}
        for value in envelopes:
            metrics.update(value.get("metrics") or {})
            summary.update(value.get("summary") or {})
        projection["metrics"] = metrics
        projection["summary"] = summary
        projection_ext = dict(projection.get("extensions") or {})
        projection_ext.update({
            "batch_id": batch_id,
            "batch_types": list(raw_types),
            "projection": "authenticated_ingress_batch",
        })
        if delivery in ("edge", "state"):
            projection_ext["delivery"] = delivery
        projection["extensions"] = projection_ext
        if isinstance(legacy_payload, dict) and source.get("kind") == "app":
            legacy = copy.deepcopy(legacy_payload)
            legacy.pop("source", None)
            legacy.pop("source_id", None)
            legacy["app"] = source.get("app_id") or source_id
            legacy["instance"] = source.get("instance")
            legacy["generation"] = source.get("generation")
            legacy["timestamp"] = (projection.get("time") or {}).get("wall_ms")
            legacy["seq"] = projection.get("seq")
            legacy["frame"] = {
                "width": (projection.get("stream") or {}).get("width"),
                "height": (projection.get("stream") or {}).get("height"),
                "pts": float((projection.get("time") or {}).get("pts_us") or 0)
                       / 1_000_000.0,
            }
            legacy["stream_id"] = (projection.get("stream") or {}).get("id")
            legacy["results"] = copy.deepcopy(projection["results"])
            legacy["events"] = copy.deepcopy(projection["events"])
            legacy["metrics"] = copy.deepcopy(metrics)
            legacy["summary"] = copy.deepcopy(summary)
            legacy["render"] = copy.deepcopy(projection.get("render") or {})
            legacy.pop("gateway_ts", None)
            projection["_legacy_batch"] = legacy
        return _FormatBatch(batch_id=batch_id, projection=projection,
                            delivery=delivery)

    def ensure_formatted(self, record: _Record) -> _Record:
        """Render a batch at most once, only when a formatted view needs it."""
        batch = record.format_batch
        if batch.formatted_ready:
            return record
        with batch.lock:
            if batch.formatted_ready:
                return record
            try:
                values = tuple(
                    value for value in self.formatter.format(batch.projection)
                    if isinstance(value, dict))
                encoded = tuple(_compact_json(value) for value in values)
            except Exception:
                with self._state_lock:
                    self._format_errors += 1
                values, encoded = (), ()
            batch.formatted = values
            batch.formatted_bytes = encoded
            batch.formatted_ready = True
            with self._state_lock:
                self._formatted_batches += 1
        return record

    @staticmethod
    def _format_job_replaceable(job, incoming) -> bool:
        old_client, old_record = job
        new_client, new_record = incoming
        if old_record.format_batch.delivery == "edge":
            return False
        if new_record.format_batch.delivery == "edge":
            return True
        return (old_client is new_client
                and old_record.source_id == new_record.source_id
                and old_record.format_batch.delivery
                == new_record.format_batch.delivery)

    def request_formatted(self, client: _HubClient, record: _Record) -> bool:
        """Bounded non-blocking offer to the isolated template worker."""
        job = (client, record)
        evicted = None
        with self._format_condition:
            if self._stop.is_set() or self._format_thread is None:
                return False
            if len(self._format_jobs) >= self._format_queue_max:
                index = next((
                    index for index, old in enumerate(self._format_jobs)
                    if self._format_job_replaceable(old, job)), None)
                if index is None:
                    self._format_queue_dropped += 1
                    if record.format_batch.delivery == "edge":
                        self._format_queue_edge_dropped += 1
                    return False
                evicted = self._format_jobs[index]
                del self._format_jobs[index]
                self._format_queue_dropped += 1
            self._format_jobs.append(job)
            self._format_condition.notify()
        if evicted is not None:
            evicted[0].cancel_formatted(evicted[1].batch_id)
        return True

    def _record_generation_current(self, record: _Record) -> bool:
        source = record.raw.get("source") or {}
        if source.get("kind") != "app":
            return True
        app_id = str(source.get("app_id") or source.get("id") or "")
        expected = (str(source.get("instance") or ""),
                    _as_int(source.get("generation"), -1))
        with self._state_lock:
            return self._app_generations.get(app_id) == expected

    def _format_loop(self) -> None:
        while True:
            with self._format_condition:
                while not self._stop.is_set() and not self._format_jobs:
                    self._format_condition.wait(timeout=0.5)
                if self._stop.is_set() and not self._format_jobs:
                    return
                client, record = self._format_jobs.popleft()
            self.ensure_formatted(record)
            # Formatting and manifest/config I/O happened outside both ingress
            # and the generation fence.  Only the final cheap CAS + queue offer
            # is serialized with refresh/purge.
            with self._publish_fence:
                if self._record_generation_current(record):
                    client.offer_formatted_ready(record)
                else:
                    client.cancel_formatted(record.batch_id)

    def publish_app(self, payload: dict, identity: dict) -> List[dict]:
        if not self._is_current_app_identity(identity):
            self._stale_app_rejected += 1
            return []
        fallback = self._next_source_seq(str(identity.get("app_id") or "app"))
        render, geometry, stream_contract = self._trusted_app_contract(identity)
        envelopes = normalize_app_payload(
            payload, identity, fallback_seq=fallback,
            trusted_render=render, trusted_geometry=geometry,
            trusted_stream=stream_contract)
        batch = self._make_format_batch(envelopes, payload)
        self._received_app += 1
        return [value for value in envelopes if self._publish(value, batch)]

    def publish_system(self, payload: dict, identity: dict) -> List[dict]:
        fallback = self._next_source_seq("builtin")
        envelopes = normalize_system_payload(payload, identity, fallback_seq=fallback)
        batch = self._make_format_batch(envelopes)
        self._received_system += 1
        return [value for value in envelopes if self._publish(value, batch)]

    def _prune_events_locked(self, now: Optional[float] = None) -> None:
        cutoff = (time.monotonic() if now is None else now) - self.event_ttl_s
        while self._events and self._events[0][0] < cutoff:
            _timestamp, record = self._events.popleft()
            self._event_ids.discard(record.raw.get("id"))

    @staticmethod
    def _event_state_key(value) -> Tuple[str, str]:
        envelope = value.raw if isinstance(value, _Record) else value
        source_id = str((envelope.get("source") or {}).get("id") or "")
        kind = str((envelope.get("extensions") or {}).get("event_kind") or "")
        if not kind:
            events = envelope.get("events") or []
            kind = (_event_kind(events[0])
                    if events and isinstance(events[0], dict) else "event")
        return source_id, kind

    def _remove_event_locked(self, index: int) -> _Record:
        _timestamp, record = self._events[index]
        del self._events[index]
        self._event_ids.discard(record.raw.get("id"))
        return record

    def _first_state_index_locked(self, key=None) -> Optional[int]:
        for index, (_timestamp, record) in enumerate(self._events):
            if record.event_delivery != "state":
                continue
            if key is None or self._event_state_key(record) == key:
                return index
        return None

    def _event_preflight(self, envelope: dict) -> bool:
        """Reject duplicates/full low-priority state before template work."""
        delivery = str((envelope.get("extensions") or {}).get("delivery") or "state")
        with self._state_lock:
            self._prune_events_locked()
            if envelope.get("id") in self._event_ids:
                self._event_duplicates += 1
                return False
            if (delivery != "edge" and len(self._events) >= self.event_replay
                    and self._first_state_index_locked() is None):
                # A state update cannot evict any replayable edge.  Reject it
                # before invoking Jinja; its next update may be admitted after
                # an edge expires from the short-TTL ring.
                self._event_state_dropped += 1
                return False
        return True

    def _publish(self, envelope: dict, format_batch: _FormatBatch) -> bool:
        record = _record(envelope, format_batch)
        message_type = record.message_type
        source = envelope.get("source") or {}
        with self._publish_fence:
            # Final compare-and-swap fence.  A prior ingress check is not
            # enough: restart may switch generation while normalization is in
            # flight.  Refresh/purge holds the same fence through WS cleanup.
            if source.get("kind") == "app":
                app_id = str(source.get("app_id") or source.get("id") or "")
                expected = (str(source.get("instance") or ""),
                            _as_int(source.get("generation"), -1))
                with self._state_lock:
                    if self._app_generations.get(app_id) != expected:
                        self._stale_app_rejected += 1
                        return False
            if message_type == "event" and not self._event_preflight(envelope):
                return False
            with self._state_lock:
                if message_type == "event":
                    self._prune_events_locked()
                    event_id = envelope.get("id")
                    if event_id in self._event_ids:
                        self._event_duplicates += 1
                        return False
                    if record.event_delivery == "state":
                        state_index = self._first_state_index_locked(
                            self._event_state_key(record))
                        if state_index is not None:
                            self._remove_event_locked(state_index)
                            self._event_state_replaced += 1
                        elif len(self._events) >= self.event_replay:
                            state_index = self._first_state_index_locked()
                            if state_index is None:
                                self._event_state_dropped += 1
                                return False
                            self._remove_event_locked(state_index)
                            self._event_state_evicted += 1
                    elif len(self._events) >= self.event_replay:
                        # Edges reclaim state first.  Only an all-edge ring
                        # evicts its oldest edge, preserving bounded replay.
                        state_index = self._first_state_index_locked()
                        if state_index is not None:
                            self._remove_event_locked(state_index)
                            self._event_state_evicted += 1
                        else:
                            self._remove_event_locked(0)
                            self._event_edge_evicted += 1
                    self._event_ids.add(event_id)
                    self._events.append((time.monotonic(), record))
                elif message_type == "frame":
                    key = (record.source_id, str(
                        (envelope.get("stream") or {}).get("id") or ""))
                    self._latest_frames[key] = record
                elif message_type in ("status", "metrics"):
                    self._latest_status[(record.source_id, message_type)] = record
                self._published[message_type] = \
                    self._published.get(message_type, 0) + 1
                observers = list(self._observers.values())
                ws = self._ws
            # Both offers are bounded/non-blocking.  Keeping only the small
            # generation fence here gives refresh a total ordering: it either
            # purges this old offer afterwards or completes before this CAS.
            if ws is not None:
                ws.broadcast(record)
            for observer in observers:
                try:
                    observer.offer(envelope)
                except Exception:
                    self._observer_errors += 1
            return True

    def snapshot_records(self) -> List[_Record]:
        with self._state_lock:
            self._prune_events_locked()
            return (list(self._latest_frames.values())
                    + list(self._latest_status.values())
                    + [record for _timestamp, record in self._events])

    def add_observer(self, callback: Callable[[dict], None]):
        if not callable(callback):
            raise TypeError("observer must be callable")
        with self._state_lock:
            if callback not in self._observers:
                self._observers[callback] = _ObserverWorker(
                    callback, self.observer_queue)
        return callback

    def remove_observer(self, callback) -> bool:
        with self._state_lock:
            worker = self._observers.pop(callback, None)
        if worker is None:
            return False
        worker.close()
        return True

    def status(self) -> dict:
        with self._state_lock:
            self._prune_events_locked()
            observers = list(self._observers.values())
            system_publishers = len(self._system_publishers)
            latest_frames = len(self._latest_frames)
            latest_status = len(self._latest_status)
            replay_events = len(self._events)
            replay_edges = sum(
                record.event_delivery == "edge"
                for _timestamp, record in self._events)
            replay_states = replay_events - replay_edges
            trusted_app_renders = len(self._app_render_cache)
            active_app_generations = len(self._app_generations)
            published = dict(self._published)
            ws = self._ws
        with self._ingress_condition:
            ingress_queued = len(self._ingress)
        with self._format_condition:
            format_queued = len(self._format_jobs)
        ws_status = ws.status() if ws is not None else {
            "subscribers": 0, "raw_subscribers": 0,
            "formatted_subscribers": 0, "client_dropped": 0,
            "client_event_dropped": 0, "client_edge_dropped": 0,
            "client_state_dropped": 0, "client_state_replaced": 0,
            "rejected": 0,
        }
        observer_status = [item.status() for item in observers]
        return {
            "running": self._system_server is not None and not self._stop.is_set(),
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "ws_host": self.ws_host,
            "ws_port": self.ws_port,
            "system_uds": self.system_uds_path,
            "system_publishers": system_publishers,
            "received": {"app": self._received_app, "system": self._received_system},
            "published": published,
            "latest": {"frames": latest_frames, "status": latest_status},
            "trusted_app_renders": trusted_app_renders,
            "active_app_generations": active_app_generations,
            "generation_fence": {
                "stale_rejected": self._stale_app_rejected,
                "records_purged": self._generation_records_purged,
                "ingress_purged": self._generation_ingress_purged,
                "client_queued_purged": self._generation_client_purged,
            },
            "event_replay": {"size": replay_events, "edge": replay_edges,
                             "state": replay_states, "max": self.event_replay,
                             "ttl_s": self.event_ttl_s,
                             "duplicates": self._event_duplicates,
                             "state_dropped": self._event_state_dropped,
                             "state_replaced": self._event_state_replaced,
                             "state_evicted": self._event_state_evicted,
                             "edge_evicted": self._event_edge_evicted},
            "ingress": {"queued": ingress_queued,
                        "max": self.ingress_queue,
                        "dropped": self._ingress_dropped,
                        "edge_dropped": self._ingress_edge_dropped,
                        "state_dropped": self._ingress_state_dropped,
                        "data_dropped": self._ingress_data_dropped},
            "rejected": self._rejected,
            "oversize": self._oversize,
            "format_errors": self._format_errors,
            "formatted_batches": self._formatted_batches,
            "format_queue": {"queued": format_queued,
                             "max": self._format_queue_max,
                             "dropped": self._format_queue_dropped,
                             "edge_dropped": self._format_queue_edge_dropped},
            "observers": {"count": len(observers),
                          "delivered": sum(item["delivered"] for item in observer_status),
                          "dropped": sum(item["dropped"] for item in observer_status),
                          "invalidations": sum(
                              item["invalidations"] for item in observer_status),
                          "invalidated_queued": sum(
                              item["invalidated_queued"] for item in observer_status),
                          "errors": (self._observer_errors
                                     + sum(item["errors"] for item in observer_status))},
            **ws_status,
        }

    def stop(self) -> None:
        self._stop.set()
        server, self._system_server = self._system_server, None
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
        with self._state_lock:
            publishers = list(self._system_publishers)
            self._system_publishers.clear()
            observers = list(self._observers.values())
            self._observers.clear()
        for conn in publishers:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
        if self._ws is not None:
            self._ws.stop()
            self._ws = None
        with self._ingress_condition:
            self._ingress.clear()
            self._ingress_condition.notify_all()
        with self._format_condition:
            pending, self._format_jobs = list(self._format_jobs), deque()
            self._format_condition.notify_all()
        for client, record in pending:
            client.cancel_formatted(record.batch_id)
        for thread in (self._system_thread, self._ingress_thread,
                       self._format_thread):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=1.0)
        self._system_thread = None
        self._ingress_thread = None
        self._format_thread = None
        for observer in observers:
            observer.close()
        try:
            value = os.lstat(self.system_uds_path)
            if self._system_socket_identity == (value.st_dev, value.st_ino):
                os.unlink(self.system_uds_path)
        except FileNotFoundError:
            pass
        self._system_socket_identity = None

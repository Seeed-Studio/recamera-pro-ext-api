"""
Unified configurable inference-result output (internal/OUTPUT_SINK_SPEC.md).

`ConfigurableSink` is a `ResultSink` that turns the kit's per-frame result into
configurable, concurrent outputs without any transport/formatting code in apps.
It builds ONE canonical envelope per frame, filters once, formats once per
channel, and best-effort publishes to every channel with per-channel failure
isolation.

Layout (why this is a separate module, not more of result_sink.py)
------------------------------------------------------------------
The three small ABCs (`OutputChannel`/`OutputFormatter`/`OutputMessage`) live in
`result_sink.py` beside `ResultSink`. Everything heavy -- `ConfigurableSink`,
the four channels, and the three formatters -- lives here so the legacy sink
path stays untouched. MQTT/LWT/discovery primitives are REUSED from
`mqtt_sink.py` (`_MqttConnection`, `ha_discovery_topic`, `ha_discovery_payload`),
never re-implemented.

Canonical envelope (spec §1)::

    {"app": "...", "timestamp": <epoch ms>, "seq": <int>,
     "frame": {"width": W, "height": H, "pts": <camera ts>},
     "results": [...], "events": [...]}
"""
from __future__ import annotations

import json
import math
import os
import re
import socket
import threading
import time
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

from kit.adapters.result_sink import (
    OutputChannel,
    OutputFormatter,
    OutputMessage,
    ResultSink,
    WsResultSink,
)
# MqttSink is reused for HA state aggregation (`_build_state`). Imported at
# module level (mqtt_sink has no back-dependency on output_sink, so no cycle)
# so HaDiscoveryFormatter.format does not re-import it on every frame.
from kit.adapters.mqtt_sink import MqttSink

# Edge/business events that may bypass rate limiting (spec §2.3).  This list
# covers every event edge emitted by the nine bundled v2 applications.  Status
# telemetry remains rate-limited; discrete user/business events must not vanish
# merely because they shared a frame with a recently-published result.
EDGE_KINDS = frozenset({
    "fall", "line_cross", "blink", "yawn", "wake", "transcript",
    "listen_timeout", "rep_completed", "set_completed", "workout_complete",
})
_EDGE_BOOLEAN_KEYS = frozenset(("edge", "is_edge", "edge_event"))
_WORKOUT_EDGE_KEYS = frozenset(
    ("rep_completed", "set_completed", "workout_complete"))


def is_edge_event(event: object) -> bool:
    """Classify a discrete business edge across bundled/compatible apps."""
    if not isinstance(event, dict):
        return False
    kind = str(event.get("kind") or event.get("type") or "").strip().lower()
    return (kind in EDGE_KINDS
            or any(event.get(key) is True for key in _EDGE_BOOLEAN_KEYS)
            or (kind == "workout" and any(
                event.get(key) is True for key in _WORKOUT_EDGE_KEYS)))
# Events that do NOT count as "a detection" for only_on_detection (spec §2.1).
_METRICS_KINDS = frozenset({"metrics"})

# Safety caps for the restricted template environment (spec §4).
MAX_TEMPLATE_LEN = 16 * 1024
MAX_RENDER_LEN = 256 * 1024
MAX_NS_ITEMS = 2000
MAX_TEMPLATE_AST_NODES = 2048
MAX_TEMPLATE_LITERAL_LEN = 4096
MAX_TEMPLATE_LOOPS = 8
MAX_TEMPLATE_LOOP_DEPTH = 1
MAX_TEMPLATE_REPEAT_CHARS = 64 * 1024
MAX_TEMPLATE_REPEAT_ITEMS = 4096
MAX_TEMPLATE_INTEGER_BITS = 4096
MAX_TEMPLATE_POW_EXPONENT = 16


# --------------------------------------------------------------------------- #
# canonical envelope + jinja namespace
# --------------------------------------------------------------------------- #
class _EventsView(list):
    """A list of events that also answers `events.<kind>` and `events.all`.

    Iterates exactly like the raw event list (so top-level `events` is a list),
    while attribute access returns the per-kind sublist -- one object serving
    both spec §4 requirements without a name clash."""

    def __getattr__(self, name: str):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        if name == "all":
            return list(self)
        return [e for e in self
                if isinstance(e, dict) and e.get("kind") == name]


def _is_detection(r: dict) -> bool:
    return isinstance(r, dict) and ("box" in r or "bbox" in r)


def _detection_entry(r: dict) -> dict:
    return {
        "box": r.get("box") or r.get("bbox"),
        "score": r.get("score"),
        "class_id": r.get("cls", r.get("class_id")),
        "label": r.get("cls_name") or r.get("label"),
        "raw": r,
    }


def build_namespace(envelope: dict, *, app_id: str, device_id: str = "") -> dict:
    """Build the restricted jinja namespace (spec §4) from a canonical envelope."""
    results = list(envelope.get("results") or [])[:MAX_NS_ITEMS]
    raw_events = list(envelope.get("events") or [])[:MAX_NS_ITEMS]
    events = _EventsView(raw_events)

    dets = [r for r in results if _is_detection(r)]
    detection = SimpleNamespace(
        count=len(dets),
        entries=[_detection_entry(r) for r in dets],
    )
    keypoints = [r for r in results if isinstance(r, dict) and r.get("keypoints")]
    classification = [r for r in results if isinstance(r, dict)
                      and (r.get("kind") == "classification"
                           or ("label" in r and "box" not in r and "bbox" not in r))]
    segmentation = [r for r in results if isinstance(r, dict)
                    and (r.get("mask") is not None or r.get("segmentation") is not None
                         or r.get("polygon") is not None)]
    tracking = [x for x in (list(results) + list(raw_events))
                if isinstance(x, dict) and x.get("track_id") is not None]

    return {
        "app": app_id,
        "device_id": device_id,
        "timestamp": envelope.get("timestamp"),
        "seq": envelope.get("seq"),
        "frame": envelope.get("frame") or {},
        "results": results,
        "events": events,
        "geometry": list(envelope.get("geometry") or [])[:MAX_NS_ITEMS],
        "metrics": dict(envelope.get("metrics") or {}),
        "summary": dict(envelope.get("summary") or {}),
        "render": dict(envelope.get("render") or {}),
        "stream_id": envelope.get("stream_id"),
        "inference_time_ms": envelope.get("inference_time_ms"),
        "pipeline_ms": envelope.get("pipeline_ms"),
        "latency_ms": envelope.get("latency_ms"),
        "preprocess_ms": envelope.get("preprocess_ms"),
        "postprocess_ms": envelope.get("postprocess_ms"),
        "dropped": envelope.get("dropped"),
        "detection": detection,
        "keypoints": keypoints,
        "classification": classification,
        "segmentation": segmentation,
        "tracking": tracking,
    }


# --------------------------------------------------------------------------- #
# restricted jinja2 environment (spec §4)
# --------------------------------------------------------------------------- #
_ALLOWED_FILTERS = ("tojson", "default", "length", "selectattr", "reject",
                    "map", "min", "max", "sum", "list", "join", "int", "float",
                    "round", "lower", "upper", "trim", "abs", "first", "last")

_TOPIC_RE = re.compile(r"^[^+#\x00]+$")


def _validate_template_ast(root) -> None:
    """Reject template structures that can buffer or multiply work unseen.

    Jinja's sandbox controls attribute/call access, but it is not a CPU/memory
    quota.  This separate compile-time policy bounds syntax and loop nesting;
    runtime binary operators and streamed output are bounded below.
    """
    from jinja2 import nodes

    forbidden = (
        nodes.Macro, nodes.CallBlock, nodes.Import, nodes.FromImport,
        nodes.Include, nodes.Extends, nodes.Block, nodes.AssignBlock,
        nodes.FilterBlock,
    )
    count = 0
    loops = 0

    def visit(node, loop_depth=0):
        nonlocal count, loops
        count += 1
        if count > MAX_TEMPLATE_AST_NODES:
            raise ValueError("template exceeds AST node limit")
        if isinstance(node, forbidden):
            raise ValueError("template construct is not allowed")
        if isinstance(node, nodes.For):
            loops += 1
            loop_depth += 1
            if loops > MAX_TEMPLATE_LOOPS:
                raise ValueError("template exceeds loop count limit")
            if loop_depth > MAX_TEMPLATE_LOOP_DEPTH:
                raise ValueError("nested template loops are not allowed")
        literal = None
        if isinstance(node, nodes.Const):
            literal = node.value
        elif isinstance(node, nodes.TemplateData):
            literal = node.data
        if isinstance(literal, (str, bytes)):
            if len(literal) > MAX_TEMPLATE_LITERAL_LEN:
                raise ValueError("template literal exceeds length limit")
        elif isinstance(literal, int) and not isinstance(literal, bool):
            if literal.bit_length() > MAX_TEMPLATE_INTEGER_BITS:
                raise ValueError("template integer exceeds bit limit")
        for child in node.iter_child_nodes():
            visit(child, loop_depth)

    visit(root)


def _bounded_render(template, *args, max_bytes=MAX_RENDER_LEN, **kwargs) -> str:
    """Stream a compiled template and stop before its output grows unbounded."""
    chunks = []
    total = 0
    for value in template.generate(*args, **kwargs):
        chunk = str(value)
        total += len(chunk.encode("utf-8"))
        if total > max(1, int(max_bytes)):
            raise ValueError("template output exceeds render limit")
        chunks.append(chunk)
    return "".join(chunks)


def make_restricted_env():
    """A sandboxed jinja2 Environment: StrictUndefined, autoescape off, no
    loader/imports, whitelisted filters only. Raises RuntimeError if jinja2 is
    unavailable so callers can degrade to Raw mode."""
    try:
        from jinja2 import StrictUndefined
        from jinja2.sandbox import SandboxedEnvironment
        from jinja2.utils import Namespace
    except Exception as e:  # pragma: no cover - import guard
        raise RuntimeError(f"jinja2 unavailable: {e}")
    class RestrictedSandbox(SandboxedEnvironment):
        # SandboxedEnvironment does not intercept arithmetic by default.  A
        # short expression such as ``'x' * 20000000`` would otherwise allocate
        # before the post-render size check ever runs.
        intercepted_binops = frozenset(("+", "*", "**", "%"))

        def _parse(self, source, name, filename):
            root = super()._parse(source, name, filename)
            _validate_template_ast(root)
            return root

        def is_safe_attribute(self, obj, attr, value):
            # Bound methods can allocate before streamed output accounting
            # sees a byte (str.ljust/center/zfill/replace/format, list helpers,
            # or a callable smuggled in result data).  Templates use filters
            # for transformations, so callable attributes fail closed.
            return (not callable(value)
                    and super().is_safe_attribute(obj, attr, value))

        def is_safe_callable(self, obj):
            # The generated output-mapping template needs exactly one call:
            # namespace(items=[]).  Filters are compiler-dispatched through
            # the explicit whitelist and do not pass this object-call path.
            return obj is Namespace

        def call_binop(self, context, operator, left, right):
            if operator == "+":
                if isinstance(left, (str, bytes, list, tuple)) and isinstance(
                        right, type(left)):
                    limit = (MAX_TEMPLATE_REPEAT_CHARS
                             if isinstance(left, (str, bytes))
                             else MAX_TEMPLATE_REPEAT_ITEMS)
                    if len(left) + len(right) > limit:
                        raise ValueError("template concatenation exceeds limit")
            elif operator == "*":
                sequence = count = None
                if isinstance(left, (str, bytes, list, tuple)) and isinstance(
                        right, int):
                    sequence, count = left, right
                elif isinstance(right, (str, bytes, list, tuple)) and isinstance(
                        left, int):
                    sequence, count = right, left
                if sequence is not None:
                    limit = (MAX_TEMPLATE_REPEAT_CHARS
                             if isinstance(sequence, (str, bytes))
                             else MAX_TEMPLATE_REPEAT_ITEMS)
                    if max(0, count) * len(sequence) > limit:
                        raise ValueError("template repetition exceeds limit")
                elif (isinstance(left, int) and not isinstance(left, bool)
                      and isinstance(right, int) and not isinstance(right, bool)
                      and left.bit_length() + right.bit_length()
                      > MAX_TEMPLATE_INTEGER_BITS):
                    raise ValueError("template integer product exceeds limit")
            elif operator == "**":
                if (not isinstance(right, int) or isinstance(right, bool)
                        or abs(right) > MAX_TEMPLATE_POW_EXPONENT):
                    raise ValueError("template exponent exceeds limit")
                if (isinstance(left, int) and not isinstance(left, bool)
                        and right > 0
                        and left.bit_length() * right
                        > MAX_TEMPLATE_INTEGER_BITS):
                    raise ValueError("template power exceeds bit limit")
                if isinstance(left, float) and not math.isfinite(left):
                    raise ValueError("non-finite template power")
            elif operator == "%" and isinstance(left, (str, bytes)):
                # printf field widths can allocate huge strings without using
                # the intercepted repetition operator.
                raise ValueError("template string modulo is not allowed")
            value = super().call_binop(context, operator, left, right)
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("non-finite template arithmetic")
            return value

    env = RestrictedSandbox(
        autoescape=False,
        undefined=StrictUndefined,
        loader=None,
        # Constant folding uses Python operators during compilation and would
        # bypass call_binop (including allocating a giant constant repeat).
        optimized=False,
        keep_trailing_newline=False,
    )
    allowed = {}
    for name in _ALLOWED_FILTERS:
        if name in env.filters:
            allowed[name] = env.filters[name]
    # tojson is always present in modern jinja2; ensure it survives the prune.
    if "tojson" in env.filters:
        allowed["tojson"] = env.filters["tojson"]
    env.filters = allowed
    # Drop range/dict/etc. callables but keep `namespace` -- the generated
    # mapping templates use a namespace accumulator to build always-valid JSON.
    env.globals = {"namespace": Namespace}
    return env


# --------------------------------------------------------------------------- #
# field-mapping -> jinja template generator (spec §4)
# --------------------------------------------------------------------------- #
def _jinja_str_literal(s: str) -> str:
    """A jinja single-quoted string literal whose VALUE is exactly `s`."""
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def generate_mapping_templates(rows: List[dict]) -> List[dict]:
    """Group visual mapping rows by rendered-topic template and emit one
    JSON-object body template per topic.

    Returns a list of render specs: ``[{"topic": <topic_tmpl>, "body":
    <jinja_str>, "task": <task>, "generated_from_mapping": True}]``. Row order is
    preserved for deterministic diffs. Optional rows (``omit_if_none`` default
    True) are wrapped in ``{% if <source> is defined and <source> is not none %}``.

    The body is built with a `namespace` accumulator joined at the end, so the
    result is ALWAYS valid JSON regardless of which optional rows drop -- no
    dangling commas, and the JSON object's literal `{` never collides with a
    jinja `{%`/`{{` delimiter. Target names are JSON-escaped keys; values use
    the `tojson` filter.
    """
    # preserve first-seen topic order
    order: List[str] = []
    groups: Dict[str, List[dict]] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        topic = row.get("topic")
        source = row.get("source")
        target = row.get("target")
        if not topic or not source or not target:
            continue
        if topic not in groups:
            groups[topic] = []
            order.append(topic)
        groups[topic].append(row)

    specs: List[dict] = []
    for topic in order:
        rows_t = groups[topic]
        parts: List[str] = ["{%- set _p = namespace(items=[]) -%}"]
        for row in rows_t:
            src = str(row["source"])
            key_json = json.dumps(str(row["target"]))          # e.g. "fall\"s"
            key_lit = _jinja_str_literal(key_json)
            item = f"{key_lit} ~ ': ' ~ ({src} | tojson)"
            append = "{%- set _p.items = _p.items + [" + item + "] -%}"
            if row.get("omit_if_none", True):
                parts.append("{%- if " + src + " is defined and " + src
                             + " is not none -%}" + append + "{%- endif -%}")
            else:
                parts.append(append)
        parts.append("{{ '{' ~ _p.items | join(', ') ~ '}' }}")
        specs.append({
            "topic": topic,
            "body": "".join(parts),
            "task": rows_t[0].get("task"),
            "generated_from_mapping": True,
        })
    return specs


# --------------------------------------------------------------------------- #
# formatters
# --------------------------------------------------------------------------- #
class RawJsonFormatter(OutputFormatter):
    """Serialize the canonical envelope compactly, no field loss (spec §4)."""

    def format(self, envelope: dict, *, channel: str = None) -> List[OutputMessage]:
        body = json.dumps(envelope, separators=(",", ":"),
                          default=str).encode("utf-8")
        return [OutputMessage(body=body, topic=None)]


class Jinja2Formatter(OutputFormatter):
    """Render per-topic JSON bodies from compiled jinja2 templates.

    `specs` is a list of ``{"topic": <topic_tmpl_str or None>, "body":
    <body_tmpl_str>, "task": ...}``. The topic template interpolates only `app`
    and a sanitized `device_id` (spec §4) in a separate tiny env; wildcards
    (`+`/`#`), NUL and empty topics are rejected. A render error or a body that
    renders empty drops only that message.
    """

    def __init__(self, specs: List[dict], *, app_id: str, device_id: str = "",
                 env=None):
        self.app_id = app_id
        self.device_id = device_id
        self._env = env or make_restricted_env()
        self._topic_env = make_restricted_env()
        self._compiled: List[dict] = []
        for spec in specs or []:
            body_src = str(spec.get("body", ""))
            if len(body_src) > MAX_TEMPLATE_LEN:
                raise ValueError("template exceeds MAX_TEMPLATE_LEN")
            entry = {
                "body": self._env.from_string(body_src),
                "topic": None,
                "task": spec.get("task"),
            }
            topic_src = spec.get("topic")
            if topic_src:
                entry["topic"] = self._topic_env.from_string(str(topic_src))
            self._compiled.append(entry)

    def _render_topic(self, tmpl) -> Optional[str]:
        if tmpl is None:
            return None
        topic = _bounded_render(
            tmpl, app=self.app_id, device_id=self.device_id,
            max_bytes=MAX_TEMPLATE_LITERAL_LEN).strip()
        if not topic or not _TOPIC_RE.match(topic):
            raise ValueError(f"invalid topic {topic!r}")
        return topic

    def format(self, envelope: dict, *, channel: str = None) -> List[OutputMessage]:
        ns = build_namespace(envelope, app_id=self.app_id,
                             device_id=self.device_id)
        out: List[OutputMessage] = []
        for entry in self._compiled:
            try:
                body = _bounded_render(entry["body"], ns)
            except Exception:
                continue  # bad template drops only this message
            if not body.strip():
                continue
            data = body.encode("utf-8")
            if len(data) > MAX_RENDER_LEN:
                continue
            try:
                topic = self._render_topic(entry["topic"])
            except Exception:
                continue
            out.append(OutputMessage(body=data, topic=topic))
        return out


class HaDiscoveryFormatter(OutputFormatter):
    """Home Assistant MQTT-Discovery + availability formatter (spec §5).

    WRAPS the reusable `ha_discovery_topic`/`ha_discovery_payload` helpers and
    `MqttSink._build_state` -- it does not re-implement discovery or state
    aggregation. Owns all availability semantics: `on_channel_ready` returns
    retained `online` + retained discovery configs; the kit (not apps/templates)
    controls the status topic/payloads.
    """

    def __init__(self, *, app_id: str, node: str, base_topic: str = "recamera",
                 discovery_prefix: str = "homeassistant",
                 entities: Optional[List[dict]] = None,
                 device_name: str = "reCamera Pro"):
        self.app_id = app_id
        self.node = node
        self.base_topic = (base_topic or "recamera").rstrip("/") or "recamera"
        self.discovery_prefix = (discovery_prefix
                                 or "homeassistant").rstrip("/") or "homeassistant"
        self.entities = entities or []
        self.device_name = device_name
        self.state_topic = f"{self.base_topic}/{self.app_id}/state"
        self.status_topic = f"{self.base_topic}/{self.app_id}/status"
        self._seq = 0

    def format(self, envelope: dict, *, channel: str = None) -> List[OutputMessage]:
        self._seq += 1
        state = MqttSink._build_state(
            self.app_id, self._seq, (envelope.get("frame") or {}).get("pts", 0.0),
            envelope.get("results") or [], envelope.get("events") or [],
        )
        body = json.dumps(state, separators=(",", ":"), default=str).encode("utf-8")
        return [OutputMessage(body=body, topic=self.state_topic, retain=False)]

    def on_channel_ready(self, channel: OutputChannel) -> List[OutputMessage]:
        from kit.adapters.mqtt_sink import ha_discovery_payload, ha_discovery_topic
        msgs = [OutputMessage(body=b"online", topic=self.status_topic,
                              retain=True, content_type="text/plain")]
        for ent in self.entities:
            if "object_id" not in ent:
                continue
            topic = ha_discovery_topic(self.discovery_prefix, self.node,
                                       self.app_id, ent)
            payload = json.dumps(
                ha_discovery_payload(self.node, self.app_id, self.state_topic,
                                     self.status_topic, self.device_name, ent),
                separators=(",", ":")).encode("utf-8")
            msgs.append(OutputMessage(body=payload, topic=topic, retain=True))
        return msgs


# --------------------------------------------------------------------------- #
# channels
# --------------------------------------------------------------------------- #
class WsChannel(OutputChannel):
    """Adapts an existing WsResultSink (reuse -- no second RFC6455 server).

    The wrapped sink is constructed with ``preserve_envelope=True`` so the
    canonical envelope built once upstream is broadcast verbatim (no second
    seq/timestamp)."""

    name = "ws"

    def __init__(self, ws: Optional[WsResultSink] = None, *, host: str = "127.0.0.1",
                 port: int = 8124, app_id: str = "app", own: Optional[bool] = None):
        if ws is None:
            ws = WsResultSink(host=host, port=port, app_id=app_id,
                              preserve_envelope=True)
            own = True if own is None else own
        self._ws = ws
        self._own = bool(own)

    def publish(self, message: OutputMessage) -> None:
        try:
            obj = json.loads(message.body.decode("utf-8"))
        except Exception:
            return
        self._ws.publish_envelope(obj)

    def client_count(self) -> int:
        try:
            return self._ws.client_count()
        except Exception:
            return 0

    def close(self) -> None:
        if self._own:
            try:
                self._ws.close()
            except Exception:
                pass


class MqttChannel(OutputChannel):
    """Publish-only MQTT channel over the reused `_MqttConnection` primitive.

    Owns a background connect/keepalive/reconnect thread mirroring
    `MqttSink._run/_open`. On every (re)connect it invokes `on_ready()` (wired by
    ConfigurableSink to the formatter's `on_channel_ready`) and publishes the
    returned messages retained -- this is how HA discovery + `online` re-arm
    after a drop. LWT (`will_topic`/`will_payload`) covers unexpected death.
    """

    name = "mqtt"

    def __init__(self, *, host: str, port: int = 1883, client_id: str = "recamera",
                 username: str = "", password: str = "", keepalive: int = 30,
                 default_topic: str = "", will_topic: str = "",
                 will_payload: bytes = b"offline", will_retain: bool = True,
                 offline_on_close: bool = True,
                 on_ready: Optional[Callable[[], List[OutputMessage]]] = None,
                 verbose: bool = False, autostart: bool = True):
        self.host = host
        self.port = int(port)
        self.client_id = client_id
        self.username = username or ""
        self.password = password or ""
        self.keepalive = max(10, int(keepalive))
        self.default_topic = default_topic
        self.will_topic = will_topic
        self.will_payload = will_payload
        self.will_retain = will_retain
        self.offline_on_close = offline_on_close
        self.on_ready = on_ready
        self.verbose = verbose

        self._conn = None
        self._lock = threading.Lock()
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        if autostart:
            self.start()

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    # -- connect / keepalive / reconnect (mirrors MqttSink) --------------- #
    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            if not self._connected.is_set():
                try:
                    self._open()
                    backoff = 1.0
                except Exception as e:
                    if self.verbose:
                        print(f"[mqttch] connect failed: {e} (retry {backoff:.0f}s)",
                              flush=True)
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2, 30.0)
                    continue
            self._stop.wait(self.keepalive / 2.0)
            if self._stop.is_set():
                break
            with self._lock:
                conn = self._conn
            if conn is not None:
                try:
                    conn.ping()
                except Exception:
                    self._drop()

    def _open(self) -> None:
        from kit.adapters.mqtt_sink import _MqttConnection
        conn = _MqttConnection(
            self.host, self.port, client_id=self.client_id,
            keepalive=self.keepalive, username=self.username,
            password=self.password, will_topic=self.will_topic,
            will_payload=self.will_payload, will_retain=self.will_retain,
        )
        conn.connect()
        with self._lock:
            self._conn = conn
            self._connected.set()
        # re-arm availability + discovery on every (re)connect
        if self.on_ready is not None:
            try:
                for msg in self.on_ready() or []:
                    self._safe_publish(msg.topic or self.default_topic,
                                       msg.body, retain=msg.retain)
            except Exception:
                pass

    def _drop(self) -> None:
        with self._lock:
            conn, self._conn = self._conn, None
            self._connected.clear()
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def _safe_publish(self, topic: str, payload: bytes, retain: bool = False) -> bool:
        if not topic:
            return False
        with self._lock:
            conn = self._conn
        if conn is None:
            return False
        try:
            conn.publish(topic, payload, retain=retain)
            return True
        except Exception:
            self._drop()
            return False

    def publish(self, message: OutputMessage) -> None:
        self._safe_publish(message.topic or self.default_topic, message.body,
                           retain=message.retain)

    def client_count(self) -> int:
        return 1 if self._connected.is_set() else 0

    def close(self) -> None:
        self._stop.set()
        if self.offline_on_close and self.will_topic:
            self._safe_publish(self.will_topic, b"offline", retain=True)
        self._drop()


class HttpChannel(OutputChannel):
    """POST each message to a URL via stdlib urllib on a background worker.

    A bounded queue (drop-oldest) guarantees `publish()` never blocks inference.
    Retries only network errors and 429/5xx with capped backoff; other 4xx are
    dropped. `Authorization: Bearer <token>` is sent when a token is set.
    """

    name = "http"

    def __init__(self, *, url: str, token: str = "", timeout: float = 5.0,
                 queue_size: int = 32, max_retries: int = 2,
                 autostart: bool = True):
        self.url = url
        self.token = token or ""
        self.timeout = float(timeout)
        self.max_retries = int(max_retries)
        import queue as _q
        self._q: "_q.Queue" = _q.Queue(maxsize=max(1, int(queue_size)))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        if autostart:
            self.start()

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def publish(self, message: OutputMessage) -> None:
        import queue as _q
        try:
            self._q.put_nowait(message)
        except _q.Full:
            # drop the oldest, keep the latest frame
            try:
                self._q.get_nowait()
            except _q.Empty:
                pass
            try:
                self._q.put_nowait(message)
            except _q.Full:
                pass

    def _run(self) -> None:
        import queue as _q
        while not self._stop.is_set():
            try:
                msg = self._q.get(timeout=0.5)
            except _q.Empty:
                continue
            self._send(msg)

    def _send(self, message: OutputMessage) -> None:
        import urllib.error
        import urllib.request
        backoff = 0.5
        for attempt in range(self.max_retries + 1):
            if self._stop.is_set():
                return
            req = urllib.request.Request(
                self.url, data=message.body, method="POST")
            req.add_header("Content-Type", message.content_type)
            if self.token:
                req.add_header("Authorization", f"Bearer {self.token}")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    if 200 <= getattr(resp, "status", 200) < 300:
                        return
                return
            except urllib.error.HTTPError as e:
                if e.code == 429 or 500 <= e.code < 600:
                    pass  # retryable
                else:
                    return  # other 4xx: do not retry
            except Exception:
                pass  # network error: retryable
            if attempt < self.max_retries:
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 5.0)

    def close(self) -> None:
        self._stop.set()


class UartChannel(OutputChannel):
    """Newline-delimited writes to an allow-listed UART (spec §2, feature-gated).

    Production enablement is gated (`enabled=False` -> no-op) until baud/parity/
    ownership are verified on-device (spec §10). A test may inject a writable
    file descriptor (`fd=`) to exercise framing without hardware; otherwise the
    device path must match `/dev/ttyS*`.
    """

    name = "uart"
    _ALLOW_RE = re.compile(r"^/dev/ttyS\d+$")

    def __init__(self, *, port_dev: str = "", fd: Optional[int] = None,
                 enabled: bool = False, max_payload: int = 4096):
        self.port_dev = port_dev
        self.enabled = bool(enabled) or fd is not None
        self.max_payload = int(max_payload)
        self._fd: Optional[int] = None
        self._own_fd = False
        self._lock = threading.Lock()
        if fd is not None:
            self._fd = fd
            self._own_fd = False
        elif self.enabled:
            self._open_device()

    def _open_device(self) -> None:
        if not self._ALLOW_RE.match(self.port_dev or ""):
            raise ValueError(f"UART path not allow-listed: {self.port_dev!r}")
        self._fd = os.open(self.port_dev, os.O_WRONLY | os.O_NONBLOCK)
        self._own_fd = True

    def publish(self, message: OutputMessage) -> None:
        if not self.enabled or self._fd is None:
            return
        data = message.body[:self.max_payload]
        if not data.endswith(b"\n"):
            data = data + b"\n"
        with self._lock:
            try:
                os.write(self._fd, data)
            except (BlockingIOError, OSError):
                pass  # bounded, best-effort: never block inference

    def close(self) -> None:
        if self._fd is not None and self._own_fd:
            try:
                os.close(self._fd)
            except OSError:
                pass
        self._fd = None


# --------------------------------------------------------------------------- #
# ConfigurableSink
# --------------------------------------------------------------------------- #
class ConfigurableSink(ResultSink):
    """A ResultSink that builds one canonical envelope per frame, filters once,
    formats once per channel, and best-effort publishes with per-channel failure
    isolation (spec §2)."""

    def __init__(self, *, app_id: str, channels: List[OutputChannel],
                 formatter: OutputFormatter, filters: Optional[dict] = None,
                 device_id: str = "", verbose: bool = False,
                 formatter_builder: Optional[Callable[[dict], OutputFormatter]] = None):
        self.app_id = app_id
        self.channels = [c for c in (channels or []) if c is not None]
        self.formatter = formatter
        self.device_id = device_id
        self.verbose = verbose
        # assemble_output_sink supplies a closure over the immutable manifest and
        # app metadata.  SIGHUP then carries only the ordinary effective config;
        # the sink can rebuild its restricted formatter without an undocumented
        # `_formatter` object being smuggled through config.json.
        self._formatter_builder = formatter_builder
        self._lock = threading.Lock()
        self._seq = 0
        self._frame_w: Optional[int] = None
        self._frame_h: Optional[int] = None
        self._legacy_sinks: List[ResultSink] = []
        self.set_filters(filters)
        self._rate_last: Dict[str, float] = {}
        self._err_last: Dict[str, float] = {}
        # wire channel (re)connect callbacks to the formatter BEFORE any channel
        # opens its connection -- otherwise the very first connect can fire with
        # on_ready still None and HA discovery/`online` would only arm on a later
        # reconnect (init-time race, P3b). Channels constructed with
        # autostart=False stay dormant until wiring is done, then we start them.
        for ch in self.channels:
            if hasattr(ch, "on_ready") and getattr(ch, "on_ready") is None:
                ch.on_ready = (lambda c=ch: self.formatter.on_channel_ready(c))
        for ch in self.channels:
            start = getattr(ch, "start", None)
            if callable(start):
                try:
                    start()  # idempotent (guards self._thread); no-op if running
                except Exception:
                    pass

    # -- config ----------------------------------------------------------- #
    def set_filters(self, filters: Optional[dict]) -> None:
        f = filters or {}
        self._only_on_detection = bool(f.get("only_on_detection", False))
        self._classes = set(str(c) for c in (f.get("classes") or []))
        try:
            self._rate_hz = float(f.get("rate_limit_hz", 0) or 0)
        except (TypeError, ValueError):
            self._rate_hz = 0.0
        self._preserve_edge = bool(f.get("preserve_edge_events", True))

    def on_config_reload(self, config: dict) -> None:
        """Live-apply filter/template changes. Structural channel changes are
        apply:"restart" and never reach here (spec §3)."""
        self.set_filters((config or {}).get("output_filters"))
        new_fmt = config.get("_formatter") if isinstance(config, dict) else None
        if new_fmt is None and self._formatter_builder is not None:
            try:
                new_fmt = self._formatter_builder(config or {})
            except Exception as e:
                # Compilation/config failures retain the known-good formatter;
                # raw/primary output and every other channel continue unaffected.
                self._log_channel_error("formatter-reload", e)
                return
        if new_fmt is not None:
            with self._lock:
                self.formatter = new_fmt

    def set_frame_size(self, w: int, h: int) -> None:
        if w and h and w > 0 and h > 0:
            self._frame_w = int(w)
            self._frame_h = int(h)
        for s in self._legacy_sinks:
            try:
                s.set_frame_size(w, h)
            except Exception:
                pass

    # -- envelope + filter ------------------------------------------------ #
    def _build_envelope(self, payload: dict, pts: float) -> dict:
        env = {
            "type": "results",
            "app": self.app_id,
            "timestamp": int(time.time() * 1000),
            "seq": self._seq,
            "frame": {"width": self._frame_w, "height": self._frame_h, "pts": pts},
            "results": payload.get("results") or [],
            "events": payload.get("events") or [],
        }
        for k, v in (payload or {}).items():
            if k not in env and k not in ("results", "events"):
                env[k] = v
        return env

    def _class_match(self, item: dict) -> bool:
        for key in ("cls_name", "label", "cls"):
            v = item.get(key)
            if v is not None and str(v) in self._classes:
                return True
        return False

    def _has_class(self, item: dict) -> bool:
        return any(item.get(k) is not None for k in ("cls_name", "label", "cls"))

    def _apply_filters(self, env: dict):
        """Return (env, has_edge) or None if the frame is suppressed."""
        results = env["results"]
        events = env["events"]
        if self._classes:
            results = [r for r in results
                       if isinstance(r, dict) and self._class_match(r)]
            events = [e for e in events if isinstance(e, dict)
                      and (not self._has_class(e) or self._class_match(e))]
            env = dict(env)
            env["results"] = results
            env["events"] = events
        if self._only_on_detection:
            non_metric = [e for e in events if isinstance(e, dict)
                          and e.get("kind") not in _METRICS_KINDS]
            if not results and not non_metric:
                return None
        has_edge = any(is_edge_event(event) for event in events)
        return env, has_edge

    def _rate_ok(self, channel: str, has_edge: bool) -> bool:
        if self._rate_hz <= 0:
            return True
        if has_edge and self._preserve_edge:
            return True
        now = time.monotonic()
        last = self._rate_last.get(channel, 0.0)
        if now - last >= 1.0 / self._rate_hz:
            self._rate_last[channel] = now
            return True
        return False

    def _log_channel_error(self, name: str, exc: Exception) -> None:
        now = time.monotonic()
        if now - self._err_last.get(name, 0.0) >= 5.0:
            self._err_last[name] = now
            if self.verbose:
                print(f"[output:{self.app_id}] channel {name} failed: {exc}",
                      flush=True)

    # -- ResultSink ------------------------------------------------------- #
    def emit(self, payload: dict, pts: float) -> None:
        self._seq += 1
        env = self._build_envelope(payload, pts)
        decision = self._apply_filters(env)
        if decision is None:
            return
        env, has_edge = decision
        # Which channels pass their rate gate this frame (rate_ok arms the token
        # as a side effect, so evaluate it once per channel, in order).
        passing = [ch for ch in self.channels
                   if self._rate_ok(ch.name, has_edge)]
        if not passing:
            return
        # Format ONCE per frame: no formatter reads `channel`, and the formatter
        # may carry per-frame state (HaDiscoveryFormatter._seq) that must advance
        # once per frame -- not once per channel. Fan the resulting messages out
        # to every rate-passing channel.
        try:
            msgs = self.formatter.format(env, channel=None)
        except Exception as e:
            # A format failure hits all channels alike; log once (throttled on
            # the first channel's key) and drop this frame.
            self._log_channel_error(passing[0].name, e)
            return
        for ch in passing:
            try:
                for m in msgs:
                    ch.publish(m)
            except Exception as e:  # isolate one channel's publish failure
                self._log_channel_error(ch.name, e)

    def emit_meta(self, payload: dict) -> None:
        # Metrics/meta rides only the legacy overlay sinks (if any); external
        # channels do not want a state doc every second (spec parity with MqttSink).
        for s in self._legacy_sinks:
            try:
                s.emit_meta(payload)
            except Exception:
                pass

    def client_count(self) -> int:
        total = 0
        for ch in self.channels:
            try:
                total += ch.client_count()
            except Exception:
                pass
        return total

    def close(self) -> None:
        for ch in self.channels:
            try:
                ch.close()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# config assembly (spec §3)
# --------------------------------------------------------------------------- #
_MODE_ALIASES = {0: "ha", 1: "custom", 2: "raw", "0": "ha", "1": "custom",
                 "2": "raw", "ha": "ha", "custom": "custom", "raw": "raw"}


def _as_list(v) -> List[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    return [str(x) for x in v]


def resolve_output_config(manifest: dict, eff: dict) -> dict:
    """Merge the manifest `output` block defaults with persisted config.json
    values (eff). Kit-side mirror of appmgr's injected `output` schema group."""
    mout = (manifest or {}).get("output") or {}
    eff = eff or {}

    channels = _as_list(eff.get("output_channels"))
    if not channels:
        channels = _as_list(mout.get("default_channel")) or ["ws"]

    mode = eff.get("iMode", mout.get("default_mode", "raw"))
    mode = _MODE_ALIASES.get(mode, "raw")

    templates = dict(mout.get("templates") or {})
    dTemplate = eff.get("dTemplate") or {}
    mapping = eff.get("output_mapping")
    if mapping is None:
        mapping = mout.get("default_mapping") or []
    template_mode = eff.get("template_mode")
    if template_mode not in ("mapping", "template"):
        # Compatibility for packages/config files created before the selector:
        # preserve the historical mapping-first rule.
        template_mode = "mapping" if mapping else "template"

    return {
        "channels": channels,
        "mode": mode,
        "dMqtt": eff.get("dMqtt") or {},
        "dHttp": eff.get("dHttp") or {},
        "dUart": eff.get("dUart") or {},
        "templates": templates,
        "dTemplate": dTemplate,
        "mapping": mapping,
        "template_mode": template_mode,
        "filters": eff.get("output_filters") or {},
    }


def build_formatter(mode: str, cfg: dict, *, app_id: str, node: str,
                    base_topic: str, entities: List[dict], device_name: str,
                    discovery_prefix: str = "homeassistant",
                    fallback_raw: bool = True) -> OutputFormatter:
    """Pick and construct the formatter for the resolved mode (spec §3/§4/§5)."""
    if mode == "ha":
        return HaDiscoveryFormatter(
            app_id=app_id, node=node, base_topic=base_topic,
            discovery_prefix=discovery_prefix, entities=entities,
            device_name=device_name)
    if mode == "custom":
        mapping = cfg.get("mapping") or []
        template_mode = cfg.get("template_mode")
        if template_mode not in ("mapping", "template"):
            template_mode = "mapping" if mapping else "template"
        if template_mode == "mapping":
            specs = generate_mapping_templates(mapping)
        else:
            # one per-task template with a default topic
            tmpls = dict(cfg.get("templates") or {})
            dT = cfg.get("dTemplate") or {}
            task_map = {"detection": "sDetection", "classification": "sClassification",
                        "keypoint": "sKeypoint", "segmentation": "sSegmentation",
                        "tracking": "sTracking"}
            specs = []
            for task, dkey in task_map.items():
                body = dT.get(dkey) or tmpls.get(task)
                if body:
                    specs.append({"topic": f"{base_topic}/{{{{ app }}}}/{task}",
                                  "body": body, "task": task})
        try:
            return Jinja2Formatter(specs, app_id=app_id, device_id=node)
        except Exception:
            if fallback_raw:
                return RawJsonFormatter()
            raise
    return RawJsonFormatter()


def assemble_output_sink(app, app_dir: str, manifest: dict, eff: dict, *,
                         base_topic: str = "recamera",
                         discovery_prefix: str = "homeassistant",
                         verbose: bool = False):
    """Build a ConfigurableSink for apps that declare `capabilities:["output"]`.

    Returns ``(sink_or_None, opted_in)``. When the app has NOT opted in,
    ``opted_in`` is False and the caller keeps the legacy sink path entirely
    unchanged (bypass, spec §3.1). When opted in but no external channel is
    configured (e.g. WS-only, covered by the primary overlay), the sink is None
    but ``opted_in`` is True, so the legacy MQTT path is NOT engaged.
    """
    caps = (manifest or {}).get("capabilities") or []
    if "output" not in caps:
        return None, False

    from kit.adapters.mqtt_sink import device_identifier
    node = device_identifier()
    app_id = getattr(app, "id", "app")
    device_name = manifest.get("name") or getattr(app, "name", None) or "reCamera Pro"
    entities = manifest.get("ha_entities") or []

    cfg = resolve_output_config(manifest, eff)
    base_topic = ((cfg.get("dMqtt") or {}).get("sTopic")
                  or base_topic).rstrip("/") or "recamera"
    mode = cfg["mode"]
    selected = set(cfg["channels"])

    formatter = build_formatter(
        mode, cfg, app_id=app_id, node=node, base_topic=base_topic,
        entities=entities, device_name=device_name,
        discovery_prefix=discovery_prefix)

    def rebuild_formatter(next_effective: dict) -> OutputFormatter:
        """Re-resolve live templates/mapping against the saved manifest.

        Structural channel changes remain apply:"restart"; this closure only
        replaces the formatter used by the already-open channels.
        """
        next_cfg = resolve_output_config(manifest, next_effective or {})
        next_mqtt = next_cfg.get("dMqtt") or {}
        next_base = (next_mqtt.get("sTopic") or base_topic).rstrip("/") or "recamera"
        return build_formatter(
            next_cfg["mode"], next_cfg, app_id=app_id, node=node,
            base_topic=next_base, entities=entities, device_name=device_name,
            discovery_prefix=discovery_prefix, fallback_raw=False)

    state_topic = f"{base_topic}/{app_id}/state"
    status_topic = f"{base_topic}/{app_id}/status"

    channels: List[OutputChannel] = []

    if "mqtt" in selected:
        dm = cfg.get("dMqtt") or {}
        host = dm.get("sURL") or dm.get("sUrl") or ""
        if host:
            channels.append(MqttChannel(
                host=host, port=int(dm.get("iPort", 1883) or 1883),
                client_id=(dm.get("sClientId")
                           or f"recamera-{app_id}-{node}")[:23] + str(os.getpid() % 1000),
                username=dm.get("sUsername", ""), password=dm.get("sPassword", ""),
                default_topic=state_topic,
                will_topic=status_topic if mode == "ha" else "",
                verbose=verbose,
                # Defer the connect thread until ConfigurableSink has wired
                # on_ready, so HA discovery arms on the FIRST connect (P3b race).
                autostart=False))

    if "http" in selected:
        dh = cfg.get("dHttp") or {}
        url = dh.get("sUrl") or ""
        if url:
            channels.append(HttpChannel(url=url, token=dh.get("sToken", "")))

    if "uart" in selected:
        du = cfg.get("dUart") or {}
        uart_on = str(os.environ.get("RECAMERA_UART_ENABLE", "")).strip().lower() \
            in ("1", "true", "yes", "on")
        try:
            channels.append(UartChannel(port_dev=du.get("sPortDev", ""),
                                        enabled=uart_on))
        except Exception:
            pass  # bad path / gated off: skip the channel, never abort

    if not channels:
        return None, True

    sink = ConfigurableSink(app_id=app_id, channels=channels, formatter=formatter,
                            filters=cfg.get("filters"), device_id=node,
                            verbose=verbose,
                            formatter_builder=rebuild_formatter)
    return sink, True

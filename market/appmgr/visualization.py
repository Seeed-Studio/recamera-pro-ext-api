"""Device-wide AI result visualisation policy and detection OSD bridge.

The browser overlay is a per-client concern.  Stream OSD is different: rkipc
burns it into every encoded stream, recording and snapshot, so it is governed
by one explicit, durable device policy.  Managed apps never connect to
``result-in.sock`` for this feature.  Instead this module consumes already
authenticated Result-Hub v2 envelopes and publishes one bounded union of the
selected applications' boxes through the dedicated platform-owned OSD sink.

Keeping the bridge single-writer also avoids the legacy result-in identity
limitation where all root-run applications collapse to ``uid:0``.
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
import tempfile
import threading
import time
from typing import Any, Callable, Dict, Iterable, Optional

from . import paths


_APP_ID_RE = re.compile(r"[a-z0-9-]{1,64}")
MAX_OSD_SOURCES = 8
MAX_OSD_BOXES = 64
DEFAULT_TTL_SEC = 1.5


class VisualizationError(ValueError):
    """The requested device visualisation policy is invalid."""


def supports_detection_stream_osd(manifest: Any) -> bool:
    """Return a trusted manifest's effective detection-box OSD capability.

    Current manifests opt in explicitly through ``render.stream_osd``.  Early
    manifest-v2 packages predated that additive field, however, and some of
    them already made the same box contract unambiguous for the browser.  Keep
    explicit capability requires strict output/render contracts, a direct field
    named ``box``, and one consistent pixel/normalised xyxy space across every
    non-derived ``results[].box`` alias.  A package predating ``stream_osd`` is
    projected as compatible only when it additionally declares a browser
    ``render.boxes`` policy and exactly one such runtime box field.

    Payload data, application id/version and numeric value ranges are never
    used to infer this capability.  If ``stream_osd`` is present, even as an
    explicit empty/negative declaration, it always overrides legacy inference.
    """
    if not isinstance(manifest, dict) or manifest.get("manifest_version") != 2:
        return False
    render = manifest.get("render")
    if (not isinstance(render, dict)
            or isinstance(render.get("schema_version"), bool)
            or render.get("schema_version") != 1):
        return False
    output = manifest.get("output")
    if (not isinstance(output, dict)
            or output.get("contract_version") != 2):
        return False
    fields = output.get("fields")
    if not isinstance(fields, list):
        return False
    boxes = [
        field for field in fields
        if isinstance(field, dict)
        and field.get("from") == "results[].box"
        and field.get("derived") is not True
    ]
    coordinates = [field.get("coord") for field in boxes]
    coordinated_boxes = (
        bool(coordinates)
        and all(isinstance(coord, str) for coord in coordinates)
        and len(set(coordinates)) == 1
        and coordinates[0] in ("pixel_xyxy", "normalized_xyxy")
    )
    if "stream_osd" in render:
        stream_osd = render.get("stream_osd")
        return (
            coordinated_boxes
            and any(field.get("name") == "box" for field in boxes)
            and isinstance(stream_osd, dict)
            and stream_osd.get("supported") == ["boxes"]
            and stream_osd.get("default") is False
        )
    # Only the compatibility projection for packages predating stream_osd
    # needs a browser boxes renderer as corroborating intent.  An explicit,
    # valid stream_osd declaration is independently authoritative and may be
    # used by an application that deliberately has no browser box renderer.
    return (len(boxes) == 1 and coordinated_boxes
            and isinstance(render.get("boxes"), dict))


def effective_render(manifest: Any) -> dict:
    """Copy the trusted render declaration and add only the strict legacy shim.

    The installed manifest remains byte-identical.  This projection is used by
    the Result Hub and Web API so both data-plane enforcement and presentation
    make the same decision during an OTA that preserves older app packages.
    """
    render = manifest.get("render") if isinstance(manifest, dict) else None
    projected = copy.deepcopy(render) if isinstance(render, dict) else {}
    supported = supports_detection_stream_osd(manifest)
    if "stream_osd" in projected:
        advertised = projected.get("stream_osd")
        advertised_boxes = (
            isinstance(advertised, dict)
            and isinstance(advertised.get("supported"), list)
            and "boxes" in advertised["supported"]
        )
        # Do not forward a positive OSD declaration that escaped an older,
        # loose manifest validator.  Explicit negative declarations remain
        # visible and continue to override the compatibility projection.
        if advertised_boxes and not supported:
            projected.pop("stream_osd", None)
    elif supported:
        projected["stream_osd"] = {
            "supported": ["boxes"],
            "default": False,
        }
    return projected


def _config_path() -> str:
    return os.environ.get(
        "APPMGR_VISUALIZATION_CONFIG",
        os.path.join(paths.APPMGR_DIR, "visualization.json"),
    )


def defaults() -> dict:
    return {"osd": {"enabled": False, "sources": []}}


def validate(incoming: Any, *, current: Optional[dict] = None) -> dict:
    """Validate a partial policy and return the complete canonical value."""
    if not isinstance(incoming, dict):
        raise VisualizationError("visualization config must be an object")
    unknown = sorted(set(incoming) - {"osd"})
    if unknown:
        raise VisualizationError("unknown visualization field(s): %s" %
                                 ", ".join(unknown))
    base = load() if current is None else current
    out = defaults()
    if isinstance(base, dict) and isinstance(base.get("osd"), dict):
        out["osd"].update(base["osd"])
    if "osd" not in incoming:
        return out
    osd = incoming["osd"]
    if not isinstance(osd, dict):
        raise VisualizationError("osd must be an object")
    unknown = sorted(set(osd) - {"enabled", "sources"})
    if unknown:
        raise VisualizationError("unknown osd field(s): %s" % ", ".join(unknown))
    if "enabled" in osd:
        if not isinstance(osd["enabled"], bool):
            raise VisualizationError("osd.enabled must be boolean")
        out["osd"]["enabled"] = osd["enabled"]
    if "sources" in osd:
        sources = osd["sources"]
        if not isinstance(sources, list):
            raise VisualizationError("osd.sources must be an array")
        clean = []
        for index, value in enumerate(sources):
            if not isinstance(value, str) or not _APP_ID_RE.fullmatch(value):
                raise VisualizationError(
                    "osd.sources[%d] must match [a-z0-9-]{1,64}" % index)
            if value == "builtin":
                raise VisualizationError(
                    "builtin OSD is controlled by the system AI overlay setting")
            if value not in clean:
                clean.append(value)
        if len(clean) > MAX_OSD_SOURCES:
            raise VisualizationError(
                "osd.sources supports at most %d applications" % MAX_OSD_SOURCES)
        out["osd"]["sources"] = clean
    if out["osd"]["enabled"] and not out["osd"]["sources"]:
        raise VisualizationError(
            "osd.sources must contain at least one application when enabled")
    return out


def load() -> dict:
    out = defaults()
    try:
        with open(_config_path(), "r", encoding="utf-8") as stream:
            stored = json.load(stream)
        if isinstance(stored, dict) and isinstance(stored.get("osd"), dict):
            candidate = stored["osd"]
            enabled = candidate.get("enabled")
            sources = candidate.get("sources")
            if isinstance(enabled, bool):
                out["osd"]["enabled"] = enabled
            if isinstance(sources, list):
                clean = [value for value in sources
                         if isinstance(value, str)
                         and _APP_ID_RE.fullmatch(value)
                         and value != "builtin"]
                out["osd"]["sources"] = list(dict.fromkeys(clean))[
                    :MAX_OSD_SOURCES]
    except (OSError, ValueError):
        pass
    # Older front ends could persist the contradictory state
    # ``enabled=true,sources=[]``.  Treat it as disabled on read so an OTA does
    # not claim that burn-in is active while the bridge can only send a clear.
    if out["osd"]["enabled"] and not out["osd"]["sources"]:
        out["osd"]["enabled"] = False
    return out


def save(value: dict) -> dict:
    clean = validate(value, current=defaults())
    destination = _config_path()
    directory = os.path.dirname(destination)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".visualization.", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(clean, stream, separators=(",", ":"), sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        try:
            dirfd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
        except OSError:
            pass
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return clean


def _default_sink_factory():
    from recamera_ext import OsdSink
    return OsdSink()


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _normalise_box(item: dict, stream: dict) -> Optional[tuple]:
    box = item.get("box")
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return None
    values = [_finite(value) for value in box]
    if any(value is None for value in values):
        return None
    x1, y1, x2, y2 = values
    # Result Hub's canonicalizer owns the coordinate contract. Prefer its
    # per-shape annotation, then the authoritative stream declaration. Never
    # let an app-controlled legacy ``item.space`` override either.
    spaces = item.get("spaces")
    shape_space = spaces.get("box") if isinstance(spaces, dict) else None
    space = str(shape_space or stream.get("coordinate_space") or "")
    if space in ("normalized", "normalized_xyxy", "frame_normalized"):
        pass
    elif space in ("pixel", "pixel_xyxy", "frame_pixel"):
        width = _finite(stream.get("width"))
        height = _finite(stream.get("height"))
        if not width or not height or width <= 0 or height <= 0:
            return None
        x1, x2 = x1 / width, x2 / width
        y1, y2 = y1 / height, y2 / height
    else:
        # Never infer the coordinate system from value magnitude.  A tiny pixel
        # box and a normalised box can contain exactly the same numeric values.
        return None
    x1, y1, x2, y2 = (max(0.0, min(1.0, value))
                      for value in (x1, y1, x2, y2))
    if x1 > x2 or y1 > y2:
        return None
    score = _finite(item.get("score", 0.0))
    score = max(0.0, min(1.0, score if score is not None else 0.0))
    label = str(item.get("cls_name") or item.get("label") or
                item.get("text") or item.get("kind") or "")
    label = "".join(character for character in label
                    if ord(character) >= 0x20 and character != "\x7f")[:63]
    class_id = item.get("cls", item.get("class_id", 0))
    if isinstance(class_id, bool):
        class_id = 0
    try:
        class_id = int(class_id)
    except (TypeError, ValueError, OverflowError):
        class_id = 0
    class_id = max(-(1 << 31), min((1 << 31) - 1, class_id))
    return (x1, y1, x2, y2, score, label, class_id)


class DetectionOsdBridge:
    """Non-blocking observer that sends a selected multi-app box union.

    ``observe`` is safe on a Result-Hub publisher thread: it only validates a
    bounded list, updates in-memory state and wakes the worker.  Native socket
    opening/sends and retries happen exclusively on the bridge worker.
    """

    def __init__(self, *, sink_factory: Optional[Callable[[], Any]] = None,
                 config_loader: Callable[[], dict] = load,
                 ttl_sec: float = DEFAULT_TTL_SEC,
                 clock: Callable[[], float] = time.monotonic):
        self._sink_factory = sink_factory or _default_sink_factory
        self._config_loader = config_loader
        self._ttl = max(0.1, float(ttl_sec))
        self._clock = clock
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._sink = None
        self._latest: Dict[str, dict] = {}
        self._enabled = False
        self._sources: tuple[str, ...] = ()
        # Invalidates observations that started under an older policy.  A
        # simple enabled/source re-check is insufficient for a disable ->
        # enable ABA transition because those values can become identical
        # again while a frame is being normalised outside the lock.
        self._policy_epoch = 0
        # Control-plane generation/manifest changes invalidate both cached and
        # in-flight frames for one logical app without disturbing other apps.
        self._source_epochs: Dict[str, int] = {}
        self._sent = 0
        self._send_errors = 0
        self._dropped = 0
        self._last_error = ""
        self._last_signature = None
        self.reload()

    def start(self) -> "DetectionOsdBridge":
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="appmgr-detection-osd")
        self._thread.start()
        return self

    def reload(self, config: Optional[dict] = None) -> None:
        cfg = config if config is not None else self._config_loader()
        try:
            clean = validate(cfg, current=defaults())
        except VisualizationError:
            clean = defaults()
        osd = clean["osd"]
        with self._lock:
            self._policy_epoch += 1
            self._enabled = bool(osd["enabled"])
            self._sources = tuple(osd["sources"])
            self._latest = (
                {source: value for source, value in self._latest.items()
                 if source in self._sources}
                if self._enabled else {}
            )
        self._wake.set()

    def invalidate_source(self, source_id: str, **_identity: Any) -> None:
        """Revoke cached and in-flight observations for one logical app.

        Result Hub calls this from its generation/manifest ordering domain.
        Identity details are accepted for an additive observer contract, but
        the revocation intentionally applies to the whole logical app: a new
        generation must never inherit a frame from the previous one.
        """
        if not isinstance(source_id, str) or not _APP_ID_RE.fullmatch(source_id):
            return
        with self._lock:
            self._source_epochs[source_id] = (
                self._source_epochs.get(source_id, 0) + 1)
            self._latest.pop(source_id, None)
        # Wake even when no cached entry existed: the worker may already have
        # captured an older snapshot and must promptly follow it with a clear.
        self._wake.set()

    def observe(self, envelope: Any) -> None:
        if not isinstance(envelope, dict):
            return
        # Result Hub splits one legacy app message into independent frame,
        # event and status records.  Only frame records own the OSD snapshot;
        # an edge event must never erase the boxes emitted immediately before
        # it from the same source.
        if envelope.get("type") != "frame":
            return
        source = envelope.get("source")
        if not isinstance(source, dict) or source.get("kind") != "app":
            return
        source_id = str(source.get("app_id") or source.get("id") or "")
        # ResultHub injects render only from the exact installed manifest bound
        # to this app instance/generation. Re-check the capability on every
        # frame so an upgrade/reinstall cannot inherit a durable app-id policy
        # after the new release removed stream OSD permission.
        render = envelope.get("render")
        stream_osd = (render.get("stream_osd")
                      if isinstance(render, dict) else None)
        supported = (stream_osd.get("supported")
                     if isinstance(stream_osd, dict) else None)
        if not isinstance(supported, list) or "boxes" not in supported:
            self.invalidate_source(source_id)
            return
        with self._lock:
            if not self._enabled or source_id not in self._sources:
                return
            policy_epoch = self._policy_epoch
            source_epoch = self._source_epochs.get(source_id, 0)
        stream = envelope.get("stream")
        if not isinstance(stream, dict):
            stream = {}
        results = envelope.get("results")
        if not isinstance(results, list):
            results = []
        boxes = []
        for item in results[:MAX_OSD_BOXES]:
            if isinstance(item, dict):
                box = _normalise_box(item, stream)
                if box is not None:
                    boxes.append(box)
        dropped = max(0, len(results) - MAX_OSD_BOXES)
        time_block = envelope.get("time")
        pts_us = time_block.get("pts_us", 0) if isinstance(time_block, dict) else 0
        try:
            pts_us = max(0, int(pts_us or 0))
        except (TypeError, ValueError, OverflowError):
            pts_us = 0
        now = self._clock()
        with self._lock:
            if (policy_epoch != self._policy_epoch or
                    source_epoch != self._source_epochs.get(source_id, 0) or
                    not self._enabled or source_id not in self._sources):
                return
            self._dropped += dropped
            self._latest[source_id] = {
                "at": now, "boxes": boxes, "pts_us": pts_us,
                "instance": source.get("instance"),
                "generation": source.get("generation"),
            }
        self._wake.set()

    def _snapshot(self) -> tuple[bool, list, int, tuple]:
        now = self._clock()
        with self._lock:
            enabled = self._enabled
            sources = self._sources
            expired = [source for source, value in self._latest.items()
                       if now - value["at"] > self._ttl]
            for source in expired:
                self._latest.pop(source, None)
            boxes = []
            pts_us = 0
            signature_parts = []
            for source in sources:
                value = self._latest.get(source)
                if value is None:
                    continue
                available = max(0, MAX_OSD_BOXES - len(boxes))
                if available == 0:
                    self._dropped += len(value["boxes"])
                    break
                selected = value["boxes"][:available]
                if len(selected) < len(value["boxes"]):
                    self._dropped += len(value["boxes"]) - len(selected)
                boxes.extend(selected)
                pts_us = max(pts_us, value["pts_us"])
                # Include the update instant even when boxes are byte-identical.
                # rkipc clears inference OSD after roughly 1.1 s without a new
                # draw, so a stationary object must still refresh the canvas.
                signature_parts.append((source, value["instance"],
                                        value["generation"], value["at"],
                                        value["pts_us"], tuple(selected)))
            signature = (enabled, tuple(signature_parts))
        return enabled, boxes, pts_us, signature

    def _close_sink(self) -> None:
        sink, self._sink = self._sink, None
        if sink is not None:
            try:
                sink.close()
            except Exception:
                pass

    def _send_snapshot(self) -> None:
        enabled, boxes, pts_us, signature = self._snapshot()
        if signature == self._last_signature:
            return
        if not enabled:
            if self._sink is not None:
                try:
                    self._sink.send_detections(0, [])
                except Exception:
                    pass
            self._close_sink()
            self._last_signature = signature
            return
        try:
            if self._sink is None:
                self._sink = self._sink_factory()
            self._sink.send_detections(pts_us, boxes)
            self._sent += 1
            self._last_error = ""
            self._last_signature = signature
        except Exception as exc:
            self._send_errors += 1
            self._last_error = str(exc)[:512]
            self._close_sink()

    def _run(self) -> None:
        interval = min(0.25, self._ttl / 2.0)
        while not self._stop.is_set():
            self._wake.wait(interval)
            self._wake.clear()
            self._send_snapshot()
        # Exact best-effort clear on daemon shutdown.
        if self._sink is not None:
            try:
                self._sink.send_detections(0, [])
            except Exception:
                pass
        self._close_sink()

    def status(self) -> dict:
        with self._lock:
            return {
                "running": bool(self._thread and self._thread.is_alive()),
                "enabled": self._enabled,
                "sources": list(self._sources),
                "active_sources": sorted(self._latest),
                "sent": self._sent,
                "send_errors": self._send_errors,
                "dropped": self._dropped,
                "last_error": self._last_error,
            }

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
            # The worker exclusively owns the native sink. If a pathological
            # native call outlives this bounded join, let that worker close its
            # handle instead of racing close against an in-flight send.
            if not thread.is_alive():
                self._thread = None
        elif thread is None:
            self._close_sink()


def public_view(bridge: Optional[DetectionOsdBridge] = None) -> dict:
    cfg = load()
    status = bridge.status() if bridge is not None else {
        "running": False, "enabled": cfg["osd"]["enabled"],
        "sources": list(cfg["osd"]["sources"]), "active_sources": [],
        "sent": 0, "send_errors": 0, "dropped": 0, "last_error": "",
    }
    return {
        "browser": {"enabled": True},
        "osd": {
            "enabled": cfg["osd"]["enabled"],
            "sources": list(cfg["osd"]["sources"]),
            "supported_tasks": ["detection"],
            "max_sources": MAX_OSD_SOURCES,
            "max_boxes": MAX_OSD_BOXES,
            "status": status,
        },
    }

"""Trusted managed-application results to recording-rule bridge.

Applications publish only to appmgr's authenticated Result Gateway.  This
bridge observes the canonical, generation-fenced Result Hub stream and projects
only capabilities declared by the installed, signed manifest into rkipc's
private recording ingress.  The ingress is recording-only: it never fans out to
notification, WebSocket or OSD paths.
"""
from __future__ import annotations

from collections import deque
import math
import re
import threading
from typing import Any, Callable, Dict, Optional

from . import manifest as appmanifest
from . import visualization


_APP_ID_RE = re.compile(r"[a-z0-9-]{1,64}")
MAX_RECORD_ITEMS = 64
DEFAULT_QUEUE_SIZE = 128
RESET_WAIT_SECONDS = 2.0


class _ResetToken:
    """Completion token for a receiver-confirmed lifecycle reset."""

    __slots__ = ("event", "ok", "_lock")

    def __init__(self):
        self.event = threading.Event()
        self.ok: Optional[bool] = None
        self._lock = threading.Lock()

    def complete(self, ok: bool) -> None:
        with self._lock:
            if self.event.is_set():
                return
            self.ok = bool(ok)
            self.event.set()


def _default_sink_factory():
    from recamera_ext import RecordSink
    return RecordSink()


def _pts_us(envelope: dict) -> int:
    block = envelope.get("time")
    value = block.get("pts_us", 0) if isinstance(block, dict) else 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _score(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(result):
        return default
    return max(0.0, min(1.0, result))


def _class_id(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(-(1 << 31), min((1 << 31) - 1, result))


def _label(item: dict) -> str:
    value = item.get("cls_name")
    if value is None:
        value = item.get("label")
    if value is None:
        value = item.get("kind")
    text = str(value or "")
    return "".join(character for character in text
                   if ord(character) >= 0x20 and ord(character) != 0x7f)[:128]


def _identity(source: dict) -> tuple[str, int]:
    instance = str(source.get("instance") or "")
    generation = source.get("generation")
    try:
        generation = int(generation)
    except (TypeError, ValueError, OverflowError):
        generation = -1
    return instance, generation


def _compile_capability(value: Any) -> Optional[dict]:
    """Compile the already validated manifest declaration for the hot path."""
    if not isinstance(value, dict) or value.get("version") != 1:
        return None
    raw_signals = value.get("signals")
    if (not isinstance(raw_signals, list) or not raw_signals
            or len(raw_signals) > 32):
        return None
    detection_classes = set()
    classification_classes = set()
    event_kinds = set()
    supports_roi = False
    signals = []
    signal_ids = set()
    authorized_labels = set()
    frame_kinds = set()
    for raw in raw_signals:
        if not isinstance(raw, dict):
            return None
        kind = raw.get("type")
        signal_id = raw.get("id")
        if (kind not in ("detection", "classification", "event")
                or not isinstance(signal_id, str) or not signal_id
                or signal_id in signal_ids
                or not isinstance(raw.get("supports_roi"), bool)):
            return None
        signal_ids.add(signal_id)
        signal = {
            "id": signal_id,
            "type": kind,
            "supports_roi": bool(raw.get("supports_roi", False)),
        }
        if kind == "event":
            event_kind = raw.get("event_kind")
            if (not isinstance(event_kind, str) or not event_kind
                    or event_kind != event_kind.lower()
                    or event_kind in authorized_labels
                    or signal["supports_roi"]):
                return None
            event_kinds.add(event_kind)
            authorized_labels.add(event_kind)
            signal["event_kind"] = event_kind
        else:
            classes = raw.get("classes")
            if (not isinstance(classes, list) or not classes
                    or len(classes) > 256 or any(
                    not isinstance(label, str) or not label for label in classes)):
                return None
            clean_classes = tuple(dict.fromkeys(classes))
            if (len(clean_classes) != len(classes)
                    or any(label in authorized_labels for label in clean_classes)):
                return None
            authorized_labels.update(clean_classes)
            frame_kinds.add(kind)
            if len(frame_kinds) > 1:
                return None
            signal["classes"] = list(clean_classes)
            if kind == "detection":
                detection_classes.update(clean_classes)
                supports_roi = supports_roi or signal["supports_roi"]
            else:
                if signal["supports_roi"]:
                    return None
                classification_classes.update(clean_classes)
        signals.append(signal)
    return {
        "signals": signals,
        "detection_classes": frozenset(detection_classes),
        "classification_classes": frozenset(classification_classes),
        "event_kinds": frozenset(event_kinds),
        "supports_roi": supports_roi,
    }


def source_view(app: dict) -> Optional[dict]:
    """Return the stable recording-source API projection for one app record."""
    manifest = app.get("manifest") if isinstance(app, dict) else None
    app_id = str((app or {}).get("id") or "")
    if (not isinstance(manifest, dict)
            or str(manifest.get("id") or "") != app_id):
        return None
    declaration = appmanifest.effective_record_trigger(manifest)
    compiled = _compile_capability(declaration)
    if compiled is None or not _APP_ID_RE.fullmatch(app_id) or app_id == "builtin":
        return None
    return {
        "id": app_id,
        "kind": "app",
        "name": (app or {}).get("name") or manifest.get("name") or app_id,
        "name_zh": (app or {}).get("name_zh") or manifest.get("name_zh"),
        "version": (app or {}).get("version") or manifest.get("version"),
        "installed": True,
        "running": bool((app or {}).get("running")),
        "status": (app or {}).get("status") or (
            "running" if (app or {}).get("running") else "stopped"),
        "supports_roi": compiled["supports_roi"],
        "signals": compiled["signals"],
    }


class RecordingTriggerBridge:
    """Bounded, generation-aware Result Hub observer for recording triggers."""

    def __init__(self, *, sink_factory: Optional[Callable[[], Any]] = None,
                 max_queue: int = DEFAULT_QUEUE_SIZE):
        self._sink_factory = sink_factory or _default_sink_factory
        self._max_queue = max(1, int(max_queue))
        self._condition = threading.Condition()
        self._queue = deque()
        self._capabilities: Dict[str, dict] = {}
        self._epochs: Dict[str, int] = {}
        self._recent_ids: Dict[str, deque] = {}
        self._recent_id_sets: Dict[str, set] = {}
        self._closing = False
        self._thread: Optional[threading.Thread] = None
        self._sink = None
        self._sink_sources = set()
        self._sent = 0
        self._frames = 0
        self._events = 0
        self._resets = 0
        self._dropped = 0
        self._frame_dropped = 0
        self._event_dropped = 0
        self._frame_coalesced = 0
        self._duplicates = 0
        self._send_errors = 0
        self._last_error = ""

    def start(self) -> "RecordingTriggerBridge":
        with self._condition:
            if self._thread is not None and self._thread.is_alive():
                return self
            self._closing = False
            self._thread = threading.Thread(
                target=self._run, daemon=True, name="appmgr-record-trigger")
            self._thread.start()
        return self

    def _purge_source_locked(self, source_id: str) -> None:
        kept = deque()
        for item in self._queue:
            if item[1] == source_id:
                if item[0] == "reset":
                    item[5].complete(False)
                else:
                    self._dropped += 1
                    if item[0] == "events":
                        self._event_dropped += 1
                    else:
                        self._frame_dropped += 1
                continue
            kept.append(item)
        self._queue = kept

    def _append_locked(self, action: tuple) -> None:
        kind, source_id, epoch, _pts, _payload, token = action
        if kind == "reset":
            # Reset is a lifecycle fence and must never be dropped.  Coalesce
            # older work for the same source before appending it.
            self._purge_source_locked(source_id)
            while len(self._queue) >= self._max_queue:
                victim = next((index for index, item in enumerate(self._queue)
                               if item[0] not in ("reset", "events")), None)
                if victim is None:
                    victim = next((index for index, item in enumerate(self._queue)
                                   if item[0] != "reset"), None)
                if victim is None:
                    # At most one pending reset exists per source.  Installed
                    # app count bounds this exceptional lifecycle-only tail;
                    # dropping a reset would be unsafe.
                    break
                removed = self._queue[victim]
                del self._queue[victim]
                self._dropped += 1
                if removed[0] == "events":
                    self._event_dropped += 1
                else:
                    self._frame_dropped += 1
            # Lifecycle work is a receiver-confirmed fence, not ordinary data.
            # Keep FIFO ordering among resets while placing it ahead of every
            # frame/event snapshot from other sources. Otherwise a full event
            # backlog could consume the complete bounded ACK wait before this
            # reset is even attempted.
            insert_at = 0
            for index, queued in enumerate(self._queue):
                if queued[0] == "reset":
                    insert_at = index + 1
            self._queue.insert(insert_at, action)
            return

        if kind != "events":
            # Frame observations are snapshots.  Keep only the newest pending
            # snapshot for this source/generation; this both bounds latency and
            # leaves queue capacity for one-shot events.
            for index in range(len(self._queue) - 1, -1, -1):
                queued = self._queue[index]
                if (queued[0] not in ("reset", "events")
                        and queued[1] == source_id and queued[2] == epoch):
                    self._queue[index] = action
                    self._dropped += 1
                    self._frame_dropped += 1
                    self._frame_coalesced += 1
                    return
        while len(self._queue) >= self._max_queue:
            victim = next((index for index, item in enumerate(self._queue)
                           if item[0] not in ("reset", "events")), None)
            if victim is None:
                self._dropped += 1
                if kind == "events":
                    self._event_dropped += 1
                else:
                    self._frame_dropped += 1
                return
            del self._queue[victim]
            self._dropped += 1
            self._frame_dropped += 1
        if kind == "events":
            # Lifecycle resets remain ordered first, but an edge should not sit
            # behind replaceable frame snapshots from any source. Preserve FIFO
            # order among event occurrences: reversing two QR/fall transitions
            # would change their meaning even though both are one-shot.
            insert_at = 0
            for index, queued in enumerate(self._queue):
                if queued[0] in ("reset", "events"):
                    insert_at = index + 1
            self._queue.insert(insert_at, action)
        else:
            self._queue.append(action)

    def invalidate_source(self, source_id: str, *, identity=None,
                          capability=None):
        """Advance the epoch without I/O and return a reset barrier token."""
        source_id = str(source_id or "")
        if (not _APP_ID_RE.fullmatch(source_id) or source_id == "builtin"):
            return None
        record_capability = (capability or {}).get("record_trigger") \
            if isinstance(capability, dict) and capability.get("valid") else None
        compiled = _compile_capability(record_capability)
        expected_identity = _identity(identity) if isinstance(identity, dict) else ("", -1)
        if not expected_identity[0] or expected_identity[1] < 0:
            compiled = None
        token = _ResetToken()
        with self._condition:
            if self._closing:
                token.complete(False)
                return token
            epoch = self._epochs.get(source_id, 0) + 1
            self._epochs[source_id] = epoch
            self._recent_ids.pop(source_id, None)
            self._recent_id_sets.pop(source_id, None)
            if compiled is None:
                self._capabilities.pop(source_id, None)
            else:
                self._capabilities[source_id] = {
                    "identity": expected_identity,
                    "epoch": epoch,
                    **compiled,
                }
            self._append_locked(("reset", source_id, epoch, 0, (), token))
            self._condition.notify()
        return token

    @staticmethod
    def wait_invalidation(token, timeout: float = RESET_WAIT_SECONDS) -> bool:
        """Wait outside Result Hub's publish fence for reset acknowledgement."""
        if token is None or not isinstance(token, _ResetToken):
            return token is None
        if not token.event.wait(max(0.0, float(timeout))):
            return False
        return token.ok is True

    def _is_duplicate_locked(self, source_id: str, message_id: str) -> bool:
        if not message_id:
            return False
        seen = self._recent_id_sets.setdefault(source_id, set())
        if message_id in seen:
            self._duplicates += 1
            return True
        order = self._recent_ids.setdefault(source_id, deque())
        order.append(message_id)
        seen.add(message_id)
        while len(order) > 256:
            seen.discard(order.popleft())
        return False

    def observer_priority(self, envelope: Any) -> bool:
        """Tell Result Hub which state-style events are recording edges.

        Result Hub already protects globally classified edge events.  Some
        recording contracts intentionally treat a canonical state event (for
        example a newly decoded QR value) as one-shot.  This in-memory check
        lets only manifest-authorized event kinds receive observer-queue
        priority; unrelated per-frame metrics remain replaceable.
        """
        if not isinstance(envelope, dict) or envelope.get("type") != "event":
            return False
        source = envelope.get("source")
        if not isinstance(source, dict) or source.get("kind") != "app":
            return False
        source_id = str(source.get("app_id") or source.get("id") or "")
        with self._condition:
            capability = self._capabilities.get(source_id)
            allowed = (capability.get("event_kinds", frozenset())
                       if capability is not None
                       and capability.get("identity") == _identity(source)
                       else frozenset())
        events = envelope.get("events")
        if not isinstance(events, list):
            return False
        return any(
            isinstance(item, dict)
            and str(item.get("kind") or item.get("type") or "").lower() in allowed
            for item in events
        )

    def observer_accepts(self, envelope: Any) -> bool:
        """Reject records that cannot affect this recording observer.

        Result Hub also serves WebSocket, replay, and visualization consumers,
        so its canonical stream legitimately contains status, metrics, and
        events outside a recording manifest. Keeping those records out of this
        observer's bounded queue prevents an unrelated app/event kind from
        evicting an authorized frame or one-shot event before :meth:`observe`
        gets a chance to apply its final capability check.
        """
        if not isinstance(envelope, dict):
            return False
        source = envelope.get("source")
        if not isinstance(source, dict) or source.get("kind") != "app":
            return False
        source_id = str(source.get("app_id") or source.get("id") or "")
        with self._condition:
            capability = self._capabilities.get(source_id)
            if (self._closing or capability is None
                    or capability.get("identity") != _identity(source)):
                return False
            message_type = envelope.get("type")
            if message_type == "frame":
                return bool(capability.get("detection_classes")
                            or capability.get("classification_classes"))
            allowed = (capability.get("event_kinds", frozenset())
                       if message_type == "event" else frozenset())
        events = envelope.get("events")
        if not isinstance(events, list) or not allowed:
            return False
        return any(
            isinstance(item, dict)
            and str(item.get("kind") or item.get("type") or "").lower() in allowed
            for item in events
        )

    def observe(self, envelope: Any) -> None:
        if not isinstance(envelope, dict) or envelope.get("type") not in (
                "frame", "event"):
            return
        source = envelope.get("source")
        if not isinstance(source, dict) or source.get("kind") != "app":
            return
        source_id = str(source.get("app_id") or source.get("id") or "")
        with self._condition:
            capability = self._capabilities.get(source_id)
            if (self._closing or capability is None
                    or capability["identity"] != _identity(source)):
                return
            epoch = capability["epoch"]
            capability = dict(capability)

        kind = envelope["type"]
        pts = _pts_us(envelope)
        payload = []
        action_kind = ""
        if kind == "frame" and capability["detection_classes"]:
            action_kind = "detections"
            stream = envelope.get("stream")
            stream = stream if isinstance(stream, dict) else {}
            results = envelope.get("results")
            results = results if isinstance(results, list) else []
            for raw in results[:MAX_RECORD_ITEMS]:
                if not isinstance(raw, dict):
                    continue
                label = _label(raw)
                if label not in capability["detection_classes"]:
                    continue
                box = visualization._normalise_box(raw, stream)
                if box is not None:
                    # Geometry normalization also returns presentation label/
                    # score fields for OSD.  Do not reuse those here: recording
                    # authorization was checked against _label(), whose field
                    # precedence intentionally excludes OCR text.  Rebuild the
                    # wire tuple from exactly the values that passed this
                    # capability check.
                    payload.append((
                        *box[:4],
                        _score(raw.get("score", raw.get("confidence"))),
                        label,
                        _class_id(raw.get("cls", raw.get("class_id"))),
                    ))
            if len(results) > MAX_RECORD_ITEMS:
                with self._condition:
                    self._dropped += len(results) - MAX_RECORD_ITEMS
        elif kind == "frame" and capability["classification_classes"]:
            action_kind = "classifications"
            results = envelope.get("results")
            results = results if isinstance(results, list) else []
            for raw in results[:MAX_RECORD_ITEMS]:
                if not isinstance(raw, dict):
                    continue
                label = _label(raw)
                if label not in capability["classification_classes"]:
                    continue
                payload.append((_score(raw.get("score", raw.get("confidence"))),
                                label,
                                _class_id(raw.get("cls", raw.get("class_id")))))
        elif kind == "event" and capability["event_kinds"]:
            action_kind = "events"
            events = envelope.get("events")
            events = events if isinstance(events, list) else []
            for raw in events[:MAX_RECORD_ITEMS]:
                if not isinstance(raw, dict):
                    continue
                event_kind = str(raw.get("kind") or raw.get("type") or "").lower()
                if event_kind not in capability["event_kinds"]:
                    continue
                payload.append((_score(raw.get("score", raw.get("confidence")), 1.0),
                                event_kind, 0))
        # Empty FRAME snapshots are meaningful: they clear/deassert frame-rule
        # debounce. EVENT is a one-shot occurrence and an envelope containing
        # no authorized kind must have no recording-side effect at all.
        if not action_kind or (action_kind == "events" and not payload):
            return

        message_id = str(envelope.get("id") or "")
        with self._condition:
            current = self._capabilities.get(source_id)
            if (self._closing or current is None or current["epoch"] != epoch
                    or current["identity"] != _identity(source)
                    or self._is_duplicate_locked(source_id, message_id)):
                return
            self._append_locked((action_kind, source_id, epoch, pts,
                                 tuple(payload), None))
            self._condition.notify()

    def _close_sink(self) -> None:
        sink, self._sink = self._sink, None
        self._sink_sources.clear()
        if sink is not None:
            try:
                sink.close()
            except Exception:
                pass

    def _prepare_sink(self) -> bool:
        if self._sink is not None:
            return True
        try:
            # Opening may include a bounded connect/handshake.  Keep it outside
            # the recording worker lock so lifecycle invalidation never waits on a
            # connection attempt; _dispatch() rechecks the epoch afterwards.
            self._sink = self._sink_factory()
            return True
        except Exception as exc:
            self._send_errors += 1
            self._last_error = str(exc)[:512]
            self._close_sink()
            return False

    def _action_is_current_locked(self, action: tuple) -> bool:
        kind, source_id, epoch, _pts, _payload, _token = action
        if self._epochs.get(source_id) != epoch:
            return False
        if kind == "reset":
            return True
        capability = self._capabilities.get(source_id)
        return capability is not None and capability["epoch"] == epoch

    def _send(self, action: tuple) -> None:
        kind, source_id, epoch, pts, payload, token = action
        ok = False
        try:
            if kind == "reset":
                self._sink.reset(source_id)
                self._sink_sources.add(source_id)
                self._resets += 1
                with self._condition:
                    if (self._epochs.get(source_id) == epoch
                            and source_id not in self._capabilities):
                        self._epochs.pop(source_id, None)
                        self._recent_ids.pop(source_id, None)
                        self._recent_id_sets.pop(source_id, None)
                        self._sink_sources.discard(source_id)
                ok = True
            elif kind == "detections":
                if source_id not in self._sink_sources:
                    self._sink.reset(source_id)
                    self._sink_sources.add(source_id)
                    self._resets += 1
                self._sink.send_detections(source_id, pts, payload)
                self._frames += 1
                self._sent += 1
                ok = True
            elif kind == "classifications":
                if source_id not in self._sink_sources:
                    self._sink.reset(source_id)
                    self._sink_sources.add(source_id)
                    self._resets += 1
                self._sink.send_classifications(source_id, pts, payload)
                self._frames += 1
                self._sent += 1
                ok = True
            elif kind == "events":
                if source_id not in self._sink_sources:
                    self._sink.reset(source_id)
                    self._sink_sources.add(source_id)
                    self._resets += 1
                self._sink.send_events(source_id, pts, payload)
                self._events += 1
                self._sent += 1
                ok = True
            self._last_error = ""
        except Exception as exc:
            self._send_errors += 1
            self._last_error = str(exc)[:512]
            # Closing the connection makes the firmware endpoint fail-safe
            # reset every source observed on that connection.
            self._close_sink()
        finally:
            if token is not None:
                token.complete(ok)

    def _dispatch(self, action: tuple) -> None:
        if not self._prepare_sink():
            if action[5] is not None:
                action[5].complete(False)
            return
        with self._condition:
            if not self._action_is_current_locked(action):
                self._dropped += 1
                if action[0] == "reset":
                    action[5].complete(False)
                elif action[0] == "events":
                    self._event_dropped += 1
                else:
                    self._frame_dropped += 1
                return
        # A source invalidation may race immediately after this check.  The
        # single worker serializes that old send before the queued reset, and
        # Result Hub waits for the reset acknowledgement outside its publish
        # fence before completing the lifecycle operation.
        self._send(action)

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._queue and not self._closing:
                    self._condition.wait(timeout=0.5)
                if not self._queue and self._closing:
                    break
                action = self._queue.popleft()
            self._dispatch(action)
        self._close_sink()

    def status(self) -> dict:
        with self._condition:
            return {
                "running": bool(self._thread and self._thread.is_alive()
                                and not self._closing),
                "active_sources": sorted(self._capabilities),
                "queued": len(self._queue),
                "sent": self._sent,
                "frames": self._frames,
                "events": self._events,
                "resets": self._resets,
                "dropped": self._dropped,
                "frame_dropped": self._frame_dropped,
                "event_dropped": self._event_dropped,
                "frame_coalesced": self._frame_coalesced,
                "duplicates": self._duplicates,
                "send_errors": self._send_errors,
                "last_error": self._last_error,
            }

    def close(self) -> bool:
        with self._condition:
            if self._closing:
                thread = self._thread
            else:
                for source_id in list(self._capabilities):
                    epoch = self._epochs.get(source_id, 0) + 1
                    self._epochs[source_id] = epoch
                    self._append_locked(("reset", source_id, epoch, 0, (),
                                         _ResetToken()))
                self._capabilities.clear()
                self._closing = True
                self._condition.notify_all()
                thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
            if not thread.is_alive():
                self._thread = None
            else:
                self._send_errors += 1
                self._last_error = "recording bridge worker did not stop within 2 seconds"
                return False
        elif thread is None:
            self._close_sink()
        return True

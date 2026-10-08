"""MQTT publisher for ``sensecraft.detection/1`` / ``sensecraft.status/1``.

Ported from ``edge-security-kit/platforms/rknn/esk_rknn/detector.py``. The
RK3588 original interleaves the MQTT surface with its GStreamer capture loop;
on reCamera Pro the loop belongs to ``kit.app.App``, so the wire protocol is
lifted out into this class and the app owns nothing but the pipeline.

The parts that are contract, not implementation detail, and are therefore kept
identical to the RK3588 and generic platforms:

* the LWT is registered at CONNECT with ``online: false`` and no ``streams``;
* a CLEAN exit publishes that same shape itself, because a DISCONNECT makes the
  broker discard the will and the retained ``online: true`` heartbeat would
  otherwise stay the device's permanent last word (MQTT.md, "Status and LWT");
* ``session_id`` is generated once per process start and appears in every
  message kind;
* ``cmd/snapshot`` is the only downlink and is answered with raw JPEG bytes on
  ``snapshot/<event_id>``, capped at 200 KB;
* detections are QoS0/no-retain, status is QoS1/retained, snapshots are QoS1.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time

import paho.mqtt.client as mqtt

from .preview import SNAPSHOT_MAX_BYTES, encode_jpeg

LOG = logging.getLogger("esk.recamera")

DETECTION_SCHEMA = "sensecraft.detection/1"
STATUS_SCHEMA = "sensecraft.status/1"


def now_ms() -> int:
    """Epoch milliseconds. The contract forbids seconds or floats."""
    return int(time.time() * 1000)


def model_identifier(path: str) -> str:
    """``<file stem>@<sha256 prefix>`` for status.versions.model."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    stem = os.path.splitext(os.path.basename(path))[0]
    return f"{stem}@{digest.hexdigest()[:12]}"


class Publisher:
    """Owns the MQTT connection, the status heartbeat and the snapshot downlink.

    ``health_fn`` returns the ``health`` block, ``frame_store`` supplies the
    JPEG for a snapshot request, and ``stream_state_fn`` reports the stream
    state. All three are callables so the app can keep the authoritative state
    and this class never holds a stale copy.
    """

    def __init__(
        self,
        *,
        device_id: str,
        stream_id: str,
        host: str,
        port: int = 1883,
        username: str = "",
        password: str = "",
        keepalive: int = 30,
        topic_prefix: str = "sensecraft/security",
        status_interval_s: float = 30.0,
        app_version: str = "0.1.0",
        model_path: str = "",
        health_fn=None,
        fps_fn=None,
        stream_state_fn=None,
        frame_store=None,
        preview_url: str = "",
        live_url: str = "",
    ) -> None:
        self.device_id = device_id
        self.stream_id = stream_id
        self.host = host
        self.port = int(port)
        self.keepalive = int(keepalive)
        self.status_interval_s = float(status_interval_s)
        self.app_version = app_version
        self.model_id = model_identifier(model_path) if model_path else "unknown"
        self.health_fn = health_fn or (lambda: {})
        self.fps_fn = fps_fn or (lambda: 0.0)
        self.stream_state_fn = stream_state_fn or (lambda: "running")
        self.frame_store = frame_store
        self.preview_url = preview_url
        self.live_url = live_url

        self.session_id = str(now_ms())
        self.started_at = time.monotonic()
        self.snapshot_count = 0
        self.published = 0
        self._last_status = 0.0

        base = f"{topic_prefix}/{device_id}"
        self.topic_detections = f"{base}/detections/{stream_id}"
        self.topic_status = f"{base}/status"
        self.topic_snapshot = f"{base}/snapshot"
        self.topic_cmd_snapshot = f"{base}/cmd/snapshot"

        self.client = mqtt.Client(client_id=f"esk-{device_id}-{self.session_id}")
        if username:
            self.client.username_pw_set(username, password)
        # Registered at CONNECT, so its timestamp is necessarily connect time;
        # consumers use broker receipt time as the offline instant.
        self.client.will_set(
            self.topic_status,
            json.dumps(
                {
                    "schema": STATUS_SCHEMA,
                    "timestamp": now_ms(),
                    "session_id": self.session_id,
                    "device_id": device_id,
                    "online": False,
                }
            ),
            qos=1,
            retain=True,
        )
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    # ----------------------------------------------------------- lifecycle

    def connect(self) -> None:
        self.client.connect(self.host, self.port, self.keepalive)
        self.client.loop_start()

    def shutdown(self) -> None:
        """Goodbye then DISCONNECT, in that order. See the module docstring."""
        try:
            self.publish_status(online=False)
        except Exception as exc:  # pragma: no cover - teardown only
            LOG.warning("goodbye status failed: %s", exc)
        try:
            self.client.disconnect()
            self.client.loop_stop()
        except Exception:  # pragma: no cover - teardown only
            pass

    # ---------------------------------------------------------------- MQTT

    def _on_connect(self, client, _userdata, _flags, rc) -> None:
        if rc != 0:
            LOG.error("mqtt connect failed rc=%s", rc)
            return
        LOG.info("mqtt connected, subscribing %s", self.topic_cmd_snapshot)
        client.subscribe(self.topic_cmd_snapshot, qos=1)
        self.publish_status()

    def _on_message(self, _client, _userdata, msg) -> None:
        if msg.topic != self.topic_cmd_snapshot:
            return
        try:
            request = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            LOG.warning("bad cmd/snapshot payload: %s", exc)
            return
        event_id = request.get("event_id")
        stream_id = request.get("stream_id")
        if not event_id:
            LOG.warning("cmd/snapshot without event_id, ignored")
            return
        if stream_id and stream_id != self.stream_id:
            return
        self.publish_snapshot(str(event_id))

    def publish_snapshot(self, event_id: str) -> bool:
        frame = self.frame_store.get() if self.frame_store is not None else None
        if frame is None:
            LOG.warning("snapshot %s requested before the first frame", event_id)
            return False
        payload = encode_jpeg(frame, SNAPSHOT_MAX_BYTES)
        if payload is None:
            LOG.error("snapshot %s could not be squeezed under the size cap", event_id)
            return False
        self.client.publish(f"{self.topic_snapshot}/{event_id}", payload, qos=1)
        self.snapshot_count += 1
        LOG.info("snapshot %s published, %d bytes", event_id, len(payload))
        return True

    # -------------------------------------------------------------- status

    def publish_status(self, online: bool = True) -> dict:
        payload = {
            "schema": STATUS_SCHEMA,
            "timestamp": now_ms(),
            "session_id": self.session_id,
            "device_id": self.device_id,
            "online": online,
        }
        if online:
            health = self.health_fn()
            stream = {
                "stream_id": self.stream_id,
                "state": self.stream_state_fn(),
                "fps": round(self.fps_fn(), 2),
                "decode": health.get("decode", "sw"),
            }
            if self.preview_url:
                stream["preview_url"] = self.preview_url
            if self.live_url:
                stream["live_url"] = self.live_url
            payload["streams"] = [stream]
            payload["health"] = health
            payload["versions"] = {"app": self.app_version, "model": self.model_id}
            payload["uptime_s"] = round(time.monotonic() - self.started_at, 1)
        self.client.publish(self.topic_status, json.dumps(payload), qos=1, retain=True)
        self._last_status = time.monotonic()
        return payload

    def status_due(self) -> bool:
        return time.monotonic() - self._last_status >= self.status_interval_s

    # ---------------------------------------------------------- detections

    def publish_detections(self, payload: dict) -> None:
        self.client.publish(
            self.topic_detections, json.dumps(payload), qos=0, retain=False
        )
        self.published += 1

    def detection_payload(
        self,
        *,
        frame_id: int,
        src_w: int,
        src_h: int,
        items: list,
        inference_time_ms: float,
        pipeline_ms: float,
        health: dict,
    ) -> dict:
        return {
            "schema": DETECTION_SCHEMA,
            "timestamp": now_ms(),
            "session_id": self.session_id,
            "frame_id": frame_id,
            "device_id": self.device_id,
            "stream_id": self.stream_id,
            "coordinate_space": "frame_norm",
            "frame": {"w": int(src_w), "h": int(src_h)},
            "inference_time_ms": round(inference_time_ms, 3),
            "pipeline_ms": round(pipeline_ms, 3),
            "detections": items,
            "health": health,
        }

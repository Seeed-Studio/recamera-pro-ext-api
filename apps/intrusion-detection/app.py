#!/usr/bin/env python3
"""
intrusion-detection -- edge-security-kit HUB-MODE detector for reCamera Pro.

This is the third detector platform behind the ``sensecraft.detection/1``
contract (`edge-security-kit/contracts/MQTT.md`), after ``platforms/generic``
(ONNX, x86) and ``platforms/rknn`` (RK3588). Its job is to prove the contract is
platform-independent, so the parts the contract pins down are shared code, not
re-implementations:

* ``esk/letterbox.py`` and ``esk/tracker.py`` are byte-identical copies of the
  RK3588 modules -- the coordinate inverse and the ``track_id >= 1`` semantics
  are exactly what a second implementation would get subtly wrong;
* ``esk/zoo_head.py`` keeps the RK3588 NumPy decode of the
  ``airockchip/rknn_model_zoo`` nine-output YOLOv8 head verbatim, and only
  swaps the runtime handle (kit already loaded the model);
* ``esk/publisher.py`` is the RK3588 MQTT surface lifted out of its capture
  loop.

**Hub mode**: this app publishes detections + status only. It runs no zone,
loitering or line rule; the hub judges those and asks for evidence over
``cmd/snapshot``. That is why the tracker is mandatory here -- the hub keeps
per-track rule state only for ``track_id >= 1`` (HUB_SPEC §2.1), so a detector
without tracking would have to run single-box mode instead.

What is reCamera-specific:

* the frame source is the device's own sensor via the rkipc/go2rtc RTSP sub
  stream, decoded in software by ffmpeg (there is no MPP decoder reachable from
  userspace on this firmware -- see ``kit/adapters/frame_source.py``), so
  ``health.decode`` is reported as ``sw``, truthfully;
* inference is the RV1126B's single-core NPU through ``rknnlite``;
* JPEG work uses Pillow, not OpenCV, which is not on this Buildroot image.

Run on device::

    /userdata/rknnenv/bin/python -m kit.run \\
        /userdata/local/apps/intrusion-detection --sink stdout --quiet

Configuration comes from the manifest's ``config_schema`` (auto-bound onto
``self`` by ``kit.app.App.start``), overridden by ``ESK_*`` environment
variables for headless/CI runs. ``ESK_SOURCE_FILE`` swaps the camera for a local
video file so the shared truth clip can be replayed -- see ``esk/file_source.py``
for why that path exists at all.
"""

from __future__ import annotations

import collections
import os
import signal
import time

import numpy as np

from kit.app import App, run_app

from esk.letterbox import (
    LetterboxTransform,
    frame_norm_to_pixels,
    pad_into_canvas,
)
from esk.preview import FrameStore, start_preview_server
from esk.publisher import Publisher
from esk.tracker import IoUTracker
from esk.zoo_head import PERSON_CLASS_NAME, PersonDetector

APP_VERSION = "0.1.0"

# The only decode path this firmware offers from userspace. Kept as a named
# constant rather than a literal so `health.fallback_active` compares against
# something declared, exactly like the RK3588 detector's `decode_primary`.
DECODE_PRIMARY = "sw"


def _env(name: str, default=None):
    value = os.environ.get(name)
    return default if value is None or value == "" else value


class IntrusionDetectionApp(App):
    id = "intrusion-detection"
    name = "Intrusion Detection"
    owns_loop = True
    # The letterbox is done here, not by `self.pre()`: the contract requires the
    # published coordinates to be an EXACT inverse of the forward transform, and
    # `esk/letterbox.py` derives the inverse from the integer geometry actually
    # applied (a separate scale per axis) while the kit's LetterboxInfo carries
    # the ideal `min()` ratio. The two differ by up to half a source pixel,
    # which is small but is precisely the class of error the truth clip exists
    # to catch. Sharing the RK3588 module also means a coordinate bug cannot
    # hide on one platform.
    model_frame = "cpu"

    # config_schema fallbacks (the manifest supplies a default for each).
    confidence = 0.35
    track_iou = 0.2
    track_max_lost = 0.75
    device_id = "recamera-pro-01"
    stream_id = "cam-0"
    mqtt_host = "127.0.0.1"
    mqtt_port = 1883
    mqtt_username = ""
    mqtt_password = ""
    status_interval = 30.0
    preview_port = 8099
    preview_advertise_host = ""
    source_file = ""
    annotate_path = ""

    # ------------------------------------------------------------------ setup

    def setup(self, config):
        super().setup(config)

        # Environment overrides. appmgr drives the config_schema; a headless or
        # CI run has no panel, so the same knobs are reachable as ESK_* vars.
        self.device_id = _env("ESK_DEVICE_ID", self.device_id)
        self.stream_id = _env("ESK_STREAM_ID", self.stream_id)
        self.mqtt_host = _env("ESK_MQTT_HOST", self.mqtt_host)
        self.mqtt_port = int(_env("ESK_MQTT_PORT", self.mqtt_port))
        self.mqtt_username = _env("ESK_MQTT_USER", self.mqtt_username)
        self.mqtt_password = _env("ESK_MQTT_PASS", self.mqtt_password)
        self.confidence = float(_env("ESK_CONF", self.confidence))
        self.status_interval = float(_env("ESK_STATUS_INTERVAL", self.status_interval))
        self.preview_port = int(_env("ESK_PREVIEW_PORT", self.preview_port))
        self.preview_advertise_host = _env(
            "ESK_PREVIEW_HOST", self.preview_advertise_host
        )
        self.source_file = _env("ESK_SOURCE_FILE", self.source_file)
        self._annotate_path = _env("ESK_ANNOTATE", self.annotate_path)
        self._max_seconds = float(_env("ESK_SECONDS", 0.0))
        self._max_frames = int(_env("ESK_FRAMES", 0))
        # Replay mode drives its own source, so the kit must not ALSO open the
        # camera -- two ffmpeg decoders on a 2 GB board for one consumed stream.
        # start() reads `needs_frames` after setup() returns, which is the only
        # window where a config-driven answer is still in time.
        if self.source_file:
            self.needs_frames = False

        # start() resolved the model input side from the manifest before
        # calling setup(); the class attribute is only the fallback.
        self.net_size = self._pre_size or self.input_size
        self.detector = PersonDetector(
            self.models.det,
            conf_threshold=self.confidence,
            iou_threshold=self.iou,
            input_size=self.net_size,
        )
        self.tracker = IoUTracker(float(self.track_iou), float(self.track_max_lost))

        self.frame_id = 0
        self.stream_state = "starting"
        self.decode_path = DECODE_PRIMARY
        self.frame_times = collections.deque(maxlen=60)
        self.inference_times = collections.deque(maxlen=600)
        self.pipeline_times = collections.deque(maxlen=600)
        self.store = FrameStore()
        self._stop = False
        self._annotated_written = False
        self._file_source = None

        # Preview/live endpoints. Advertised in status only when a reachable
        # host was configured: the device cannot guess which address a browser
        # will use, and a wrong URL is worse for the hub UI than an absent one.
        self.preview_server = start_preview_server(
            self.store, self.stream_id, "0.0.0.0", self.preview_port,
            debug_source=lambda: self._debug_source(),
        )
        preview_url = live_url = ""
        if self.preview_advertise_host:
            base = f"http://{self.preview_advertise_host}:{self.preview_port}"
            preview_url = f"{base}/preview/{self.stream_id}.jpg"
            live_url = f"{base}/live/{self.stream_id}"

        self.pub = Publisher(
            device_id=self.device_id,
            stream_id=self.stream_id,
            host=self.mqtt_host,
            port=self.mqtt_port,
            username=self.mqtt_username,
            password=self.mqtt_password,
            status_interval_s=self.status_interval,
            app_version=APP_VERSION,
            model_path=self.models.det.path,
            health_fn=self.health,
            fps_fn=self.measured_fps,
            stream_state_fn=lambda: self.stream_state,
            frame_store=self.store,
            preview_url=preview_url,
            live_url=live_url,
        )

        print(
            f"[intrusion] device={self.device_id} stream={self.stream_id} "
            f"mqtt={self.mqtt_host}:{self.mqtt_port} conf={self.confidence} "
            f"iou={self.iou} track(iou={self.track_iou} lost={self.track_max_lost}s) "
            f"preview=:{self.preview_port} backend={self.detector.backend} "
            f"session={self.pub.session_id}",
            flush=True,
        )

    def on_params_changed(self, changed):
        """Mirror the live-tunable thresholds into the objects that hold copies.

        The tracker is mutated in place rather than rebuilt: it owns one entry
        per live identity, and a fresh instance would renumber every track,
        which the hub reads as every person in the scene vanishing and a new set
        appearing.
        """
        if "confidence" in changed:
            self.detector.conf_threshold = float(self.confidence)
        if "iou" in changed:
            self.detector.iou_threshold = float(self.iou)
        if "track_iou" in changed:
            self.tracker.threshold = float(self.track_iou)
        if "track_max_lost" in changed:
            self.tracker.max_lost_sec = float(self.track_max_lost)

    # ----------------------------------------------------------------- health

    def measured_fps(self) -> float:
        if len(self.frame_times) < 2:
            return 0.0
        span = self.frame_times[-1] - self.frame_times[0]
        return 0.0 if span <= 0 else (len(self.frame_times) - 1) / span

    def health(self) -> dict:
        return {
            "fps": round(self.measured_fps(), 2),
            "decode": self.decode_path,
            "backend": self.detector.backend,
            # There is no CPU inference path -- the app refuses to start without
            # the NPU, because rknnlite is the only way it has to run the model.
            # Decode is the only field that can move, and on this firmware its
            # primary path is already the software one (no MPP decoder is
            # reachable from userspace), so `decode: sw` is the honest report
            # and not a fallback from anything.
            "fallback_active": self.decode_path != DECODE_PRIMARY,
        }

    def _debug_source(self):
        """The live source object, for /debug/decode."""
        if self._file_source is not None:
            return self._file_source
        rt = self._rt
        return None if rt is None else rt.get("src")

    # --------------------------------------------------------------- pipeline

    def _letterbox(self, rgb: np.ndarray, tf: LetterboxTransform) -> np.ndarray:
        """Forward transform matching ``tf`` exactly.

        ``tf`` is built from :func:`fit_geometry`, so the resize target and the
        pad offsets here come from the same integer arithmetic the inverse will
        use. Producing the canvas from anything else -- PIL's own rounding, a
        second ``min()`` -- is how the two halves drift apart by a pixel.
        """
        if (tf.scaled_w, tf.scaled_h) == (rgb.shape[1], rgb.shape[0]):
            scaled = rgb
        else:
            from PIL import Image

            scaled = np.asarray(
                Image.fromarray(rgb).resize(
                    (tf.scaled_w, tf.scaled_h), Image.BILINEAR
                ),
                dtype=np.uint8,
            )
        return pad_into_canvas(scaled, tf.dst)

    def _input_frames(self):
        """The camera (kit-owned) or a replayed file (ESK_SOURCE_FILE)."""
        path = self.source_file
        if not path:
            yield from self.frames()
            return
        from esk.file_source import FfmpegFileSource

        loop = _env("ESK_SOURCE_LOOP", "1") != "0"
        self._file_source = FfmpegFileSource(path, loop=loop, realtime=True)
        print(
            f"[intrusion] replay source {path} "
            f"{self._file_source.w}x{self._file_source.h} loop={loop}",
            flush=True,
        )
        # The kit's warm-up frame is skipped for the camera path inside
        # frames(); replay gets the same treatment here so the first inference's
        # cold-start latency never lands in the published numbers.
        warmed = False
        with self._file_source as source:
            for frame in source.frames():
                if self._stop:
                    break
                if not warmed:
                    warmed = True
                    tf = LetterboxTransform.for_source(
                        frame.w, frame.h, self.net_size
                    )
                    try:
                        self.detector.detect(self._letterbox(frame.data, tf), tf)
                    except Exception as exc:
                        print(f"[intrusion] warm-up skipped ({exc})", flush=True)
                    self.inference_times.clear()
                    continue
                self._cur_frame = frame
                self.tick()
                yield frame

    def run(self):
        self.pub.connect()
        self._install_stop_handlers()
        deadline = (
            time.monotonic() + self._max_seconds if self._max_seconds > 0 else float("inf")
        )
        self.stream_state = "running"
        try:
            for frame in self._input_frames():
                if self._stop or time.monotonic() >= deadline:
                    break
                # The clock starts after the read returns. read() blocks until
                # the next frame exists, so timing from before it folds the idle
                # wait for a 5 fps source into pipeline_ms and reports a steady
                # ~200 ms no matter how fast the board is -- which would disable
                # the diagnostic that pipeline_ms is there to provide.
                captured_at = time.monotonic()
                self.frame_times.append(captured_at)

                tf = LetterboxTransform.for_source(frame.w, frame.h, self.net_size)
                canvas = self._letterbox(frame.data, tf)
                self.store.put(frame.data)

                detections = self.detector.detect(canvas, tf)
                self.inference_times.append(self.detector.last_inference_ms)
                tracked = self.tracker.update(detections, time.monotonic())

                items = []
                osd = []
                for track, det in tracked:
                    x1, y1, x2, y2 = det.box  # frame_norm xyxy
                    items.append(
                        {
                            "track_id": track.track_id,
                            "class": PERSON_CLASS_NAME,
                            "score": round(det.score, 4),
                            "bbox": [
                                round((x1 + x2) / 2, 6),
                                round((y1 + y2) / 2, 6),
                                round(x2 - x1, 6),
                                round(y2 - y1, 6),
                            ],
                        }
                    )
                    # The kit sink normalizes by the frame size itself, so the
                    # OSD/WS copy is handed ORIGINAL-frame pixels.
                    osd.append(
                        {
                            "box": [
                                x1 * frame.w, y1 * frame.h, x2 * frame.w, y2 * frame.h
                            ],
                            "cls": 0,
                            "cls_name": PERSON_CLASS_NAME,
                            "score": float(det.score),
                            "track_id": track.track_id,
                        }
                    )

                self.frame_id += 1
                pipeline_ms = (time.monotonic() - captured_at) * 1000.0
                self.pipeline_times.append(pipeline_ms)
                payload = self.pub.detection_payload(
                    frame_id=self.frame_id,
                    src_w=tf.src_w,
                    src_h=tf.src_h,
                    items=items,
                    inference_time_ms=self.detector.last_inference_ms,
                    pipeline_ms=pipeline_ms,
                    health=self.health(),
                )
                self.pub.publish_detections(payload)
                # Local fan-out too: the /appcenter overlay and any WS consumer
                # see the same frame the hub does.
                self.emit(events=[], results=osd, ts=frame.pts)

                if self._annotate_path and items and not self._annotated_written:
                    write_annotated(frame.data, payload, self._annotate_path)
                    self._annotated_written = True

                if self.pub.status_due():
                    self.pub.publish_status()

                if self._max_frames and self.pub.published >= self._max_frames:
                    break
        finally:
            self.stream_state = "stopped"
            # A clean exit sends a DISCONNECT, which makes the broker DISCARD
            # the will -- so the goodbye has to be published here or the last
            # retained status stays `online: true` forever and the hub shows a
            # zombie device (MQTT.md, "Status and LWT").
            self.pub.shutdown()
            if self.preview_server is not None:
                self.preview_server.shutdown()
            self._print_summary()

    def _install_stop_handlers(self):
        def handler(_signum, _frame):
            print("[intrusion] stop requested", flush=True)
            self._stop = True

        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)

    def _print_summary(self):
        def pct(values, q):
            if not values:
                return 0.0
            ordered = sorted(values)
            return ordered[min(len(ordered) - 1, int(q * (len(ordered) - 1)))]

        print(
            f"[intrusion] published {self.pub.published} detection messages, "
            f"{self.measured_fps():.2f} fps, "
            f"inference p50={pct(self.inference_times, 0.5):.2f}ms "
            f"p95={pct(self.inference_times, 0.95):.2f}ms, "
            f"pipeline p50={pct(self.pipeline_times, 0.5):.2f}ms "
            f"p95={pct(self.pipeline_times, 0.95):.2f}ms, "
            f"snapshots={self.pub.snapshot_count}",
            flush=True,
        )


def write_annotated(rgb: np.ndarray, payload: dict, path: str) -> None:
    """Draw boxes decoded from the PUBLISHED frame_norm values.

    Reconstructing from the payload rather than from the internal pixel boxes is
    what makes the image evidence: a wrong letterbox inverse shows up as a
    squashed or offset rectangle instead of as a plausible-looking number.
    """
    from PIL import Image, ImageDraw

    img = Image.fromarray(np.ascontiguousarray(rgb), mode="RGB")
    draw = ImageDraw.Draw(img)
    for item in payload["detections"]:
        x1, y1, x2, y2 = frame_norm_to_pixels(item["bbox"], img.width, img.height)
        draw.rectangle([x1, y1, x2, y2], outline=(0, 220, 0), width=2)
        draw.text(
            (x1 + 2, max(2, y1 - 12)),
            f"id{item['track_id']} {item['class']} {item['score']:.2f}",
            fill=(0, 220, 0),
        )
    draw.text(
        (8, img.height - 14),
        f"src {payload['frame']['w']}x{payload['frame']['h']} "
        f"decode={payload['health']['decode']} frame_id={payload['frame_id']}",
        fill=(0, 200, 255),
    )
    img.save(path, format="JPEG", quality=92)
    print(f"[intrusion] annotated evidence frame written to {path}", flush=True)


if __name__ == "__main__":
    run_app(IntrusionDetectionApp())

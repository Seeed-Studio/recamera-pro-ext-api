"""Connect reCamera to CVSprout for image capture and system model deployment."""
from datetime import datetime, timezone
from pathlib import Path
import threading
import time
import uuid

import cv2
import numpy as np
from kit.app import App, run_app
from kit.config import appdata_root

from transport import DeliveryError, EdgeClient, Spool, Uploader
from system_models import DeploymentClient, ModelDeploymentWorker


def encode_original(frame, quality):
    """Encode synchronously while the borrowed frame remains valid.

    Kit CPU frame sources expose full-resolution RGB (or explicitly BGR).
    The owned BGR copy and resulting immutable bytes never borrow DMA storage.
    """
    if frame.fmt not in ("RGB", "BGR"):
        raise ValueError("capture requires an original RGB/BGR Kit frame")
    pixels = np.asarray(frame.data)
    if pixels.shape != (frame.h, frame.w, 3):
        raise ValueError("capture frame is not at original camera resolution")
    bgr = np.array(pixels[..., ::-1] if frame.fmt == "RGB" else pixels,
                   copy=True, order="C")
    ok, result = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    if not ok:
        raise RuntimeError("JPEG encoding failed")
    return result.tobytes()


class CVSproutCaptureApp(App):
    id = "cvsprout-capture"
    name = "CVSprout Link"
    owns_loop = True
    needs_model = False
    model_frame = "cpu"

    def __init__(self):
        super().__init__()
        self.uploader = None
        self.upload_thread = None
        self.model_worker = None
        self.model_thread = None
        self._upload_shutdown_deadline = None
        self.capture_count = 0

    def setup(self, config):
        super().setup(config)
        client = EdgeClient(config.get("server_url", ""), config.get("device_token", ""))
        self.jpeg_quality = min(95, max(40, int(config.get("jpeg_quality", 90))))
        self.max_session_images = max(0, int(config.get("max_session_images", 0)))
        self.batch_id = str(uuid.uuid4())
        self.capture_count = 0
        data_dir = Path(appdata_root()) / self.id
        # Do not silently orphan a v1.0.0 SQLite queue. Its existence is checked
        # without importing optional native Python modules on the device.
        if any((data_dir / name).exists() or (data_dir / name).is_symlink()
               for name in ("captures.sqlite3", "captures.sqlite3-journal",
                            "captures.sqlite3-wal", "captures.sqlite3-shm")):
            raise DeliveryError("legacy_spool_migration_required: preserve captures.sqlite3; "
                                "see app README before upgrading")
        spool = Spool(data_dir / "capture-spool",
                      max_bytes=min(512, max(16, int(config.get("spool_size_mb", 128)))) * 1024 * 1024,
                      max_images=min(10000, max(10, int(config.get("spool_max_images", 1000)))))
        self.uploader = Uploader(client, spool)
        self.model_worker = ModelDeploymentWorker(
            DeploymentClient(config.get("server_url", ""), config.get("device_token", "")),
            data_dir / "system-models", configuration=self.uploader.deployment_configuration)

    def run(self):
        self.upload_thread = threading.Thread(target=self.uploader.run,
                                              name="cvsprout-upload", daemon=True)
        self.model_thread = threading.Thread(target=self.model_worker.run,
                                             name="cvsprout-models", daemon=True)
        next_capture = 0.0
        next_status = 0.0
        try:
            self.upload_thread.start()
            self.model_thread.start()
            for frame in self.frames():
                if not self.upload_thread.is_alive():
                    raise RuntimeError("capture uploader stopped unexpectedly")
                self.uploader.frame_received()
                state = self.uploader.capture_state()
                now = time.monotonic()
                if now >= next_status:
                    status = self.uploader.heartbeat_payload()
                    self.emit([], frame.pts, results=[], extra={
                        "capture_state": status["state"],
                        "pending_images": status["pending_images"],
                        "pending_bytes": status["pending_bytes"],
                        "uploaded_images": status["uploaded_images"],
                        "last_error": status["last_error"],
                    })
                    next_status = now + 5
                if not state["enabled"] or now < next_capture:
                    continue
                if self.max_session_images and self.capture_count >= self.max_session_images:
                    continue
                next_capture = now + state["interval"]
                captured_at = datetime.now(timezone.utc).isoformat()
                try:
                    jpeg = encode_original(frame, self.jpeg_quality)
                except Exception:
                    self.uploader.capture_failed("jpeg_encode_failed")
                    continue
                if len(jpeg) > state["max_image_bytes"]:
                    self.uploader.capture_failed("image_exceeds_server_limit")
                    continue
                try:
                    capture_id = self.uploader.spool.enqueue(jpeg, state["owner"], captured_at, self.batch_id)
                except DeliveryError as exc:
                    self.uploader.capture_failed(exc)
                    # Published-but-unsynced records must stop sampling: the
                    # finite session quota cannot be advanced ambiguously.
                    raise
                if capture_id:
                    self.capture_count += 1
                    self.uploader.capture_succeeded(bool(self.max_session_images and
                                                         self.capture_count >= self.max_session_images))
                else:
                    self.uploader.capture_failed("spool_full_capture_paused")
        except Exception:
            if not self.uploader.capture_error:
                self.uploader.capture_failed("camera_frame_error")
            raise
        finally:
            if not self.uploader.capture_error:
                self.uploader.stopped = True
            self._stop_uploader()

    def _stop_uploader(self):
        if self.uploader:
            self.uploader.stop.set()
        if self.model_worker:
            self.model_worker.stop.set()
        failure = None
        if self.upload_thread and self.upload_thread.ident is not None:
            # One total budget across run() and Kit's repeated finish() calls.
            # HTTP/DNS can outlive a socket timeout; only the daemon worker may
            # attempt the terminal heartbeat, never this cleanup thread.
            if self._upload_shutdown_deadline is None:
                self._upload_shutdown_deadline = time.monotonic() + 10
            remaining = max(0.0, self._upload_shutdown_deadline - time.monotonic())
            self.upload_thread.join(timeout=remaining)
            if self.upload_thread.is_alive():
                failure = RuntimeError("capture uploader did not stop within its deadline")
            else:
                self.upload_thread = None
        if self.model_thread and self.model_thread.ident is not None:
            if self._upload_shutdown_deadline is None:
                self._upload_shutdown_deadline = time.monotonic() + 10
            self.model_thread.join(timeout=max(0.0, self._upload_shutdown_deadline - time.monotonic()))
            if self.model_thread.is_alive():
                failure = failure or RuntimeError("model worker did not stop within its deadline")
            else:
                self.model_thread = None
        if failure:
            raise failure

    def finish(self):
        try:
            self._stop_uploader()
        finally:
            super().finish()


if __name__ == "__main__":
    run_app(CVSproutCaptureApp())

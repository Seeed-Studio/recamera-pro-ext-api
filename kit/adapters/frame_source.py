"""
FrameSource adapter for reCamera Pro (Rockchip RV1126B).

L0 adapter layer (see docs/guide/kit-design.md §0 / docs/guide/adapter-bootstrap.md §2.1).
The application only ever sees the `FrameSource` ABC + `Frame`; the concrete
decode backend is swappable. When the official R1 frame broker
(`/run/recamera/frame.sock`, dma-buf) arrives, only a new FrameSource
implementation is added and selected by the capability registry -- no
application code changes.

Concrete implementations here
-----------------------------
* `FfmpegRtspSource` (PRIMARY, verified on device):
      Pulls the go2rtc sub stream `rtsp://admin:admin@127.0.0.1:5554/live/1`
      (640x480 H.265) through an `ffmpeg` subprocess that decodes to raw
      `rgb24` frames on stdout. ffmpeg does the H.265 decode + NV12->RGB color
      convert; Python just reads fixed-size frame chunks. Zero extra Python
      dependencies (ffmpeg + numpy already on the device).

* `SnapshotSource` (FALLBACK, low fps, simplest):
      Grabs single JPEG frames via a one-shot ffmpeg pull. Useful when a full
      streaming decoder is undesirable.

Why not "more native" MPP / V4L2 here
-------------------------------------
Probed on device (RV1126B, firmware 6.1.157):
  - No V4L2 stateful decoder node exists (all /dev/videoN are ISP/CIF/VPSS
    capture + scaler nodes), so ffmpeg `hevc_v4l2m2m` reports
    "Could not find a valid device".
  - No GStreamer `mppvideodec` plugin installed, and ffmpeg has no `hevc_rkmpp`.
  - The MPP HW decoder is only reachable via `/oem/usr/lib/librockchip_mpp.so`
    + `/dev/mpp_service` (a ctypes MppApi loop), which is a large surface area.
Measured: ffmpeg *software* HEVC decode of the 640x480 sub stream costs only
~17% of one core (of 4) -- decode is NOT the bottleneck -- so the software
ffmpeg path is the correct minimal-dependency v1. A future `MppFrameSource`
(ctypes) or `OfficialFrameSource` (dma-buf socket) can drop in behind this same
ABC when HW zero-copy is actually needed.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterator, Optional

import numpy as np

DEFAULT_SUB_STREAM = "rtsp://admin:admin@127.0.0.1:5554/live/1"   # 640x480 H.265
DEFAULT_MAIN_STREAM = "rtsp://admin:admin@127.0.0.1:5554/live/0"  # 4K H.265


@dataclass
class Frame:
    """One decoded frame.

    `data` is a contiguous HWC numpy array. For `fmt="RGB"` it is uint8
    [H, W, 3] ready to hand straight to preprocess.letterbox().  An official
    broker may set ``model_info`` and put a model-sized, already-letterboxed
    RGB image in ``data``; in that case ``w``/``h`` remain the *original camera
    geometry* and the transform is used by post-processing to map detections
    back to that geometry.  Keeping the original dimensions here is important
    for the result sink's normalized-coordinate contract.  ``fmt`` may later
    be "NV12" when a zero-copy backend yields planar YUV; downstream code
    should branch on ``fmt``.
    """
    data: np.ndarray
    w: int
    h: int
    fmt: str            # "RGB" | "NV12"
    pts: float          # capture timestamp, seconds (monotonic clock)
    # Optional model-space image/letterbox metadata supplied by an optimized
    # source.  Kept as ``object`` to avoid importing runtime.preprocess from
    # this low-level adapter module (and to remain backwards compatible with
    # callers constructing Frame positionally).
    model_info: object = None
    # Optional model-sized, already-letterboxed RGB produced by the source
    # (hardware path) while ``data`` keeps ORIGINAL-resolution pixels.  This is
    # what lets an app crop source pixels (ROI/perspective) and still skip the
    # Python letterbox.  When the source instead letterboxes *into* ``data``
    # (no original pixels retained) this stays None and only ``model_info`` is
    # set.  Consumers should prefer ``model_data`` when present, else fall back
    # to ``data`` + ``model_info``, else letterbox themselves.
    model_data: object = None
    # Optional per-frame hardware ROI cropper (``hw-roi`` mode). When a source
    # keeps the camera's NV12 dma-buf available for on-demand cropping instead of
    # pre-converting a full-resolution RGB frame, it attaches an object exposing
    #   crop_square(box, out_size, pad) -> (roi_uint8_HWC_RGB, roi_map)
    # bound to THIS frame's borrowed buffer (valid only for the current loop
    # step). Cascade apps reach it through ``App.crop_roi_hw``; None on every
    # other source (RTSP/snapshot, cpu/hw/hw-direct modes, or after an RGA
    # latch-off), where ``crop_roi_hw`` falls back to the numpy crop.
    roi_cropper: object = None


class FrameSource(ABC):
    """Abstract frame producer. Applications depend only on this."""

    @abstractmethod
    def frames(self) -> Iterator[Frame]:
        """Yield decoded frames until the stream ends or close() is called."""
        raise NotImplementedError

    def close(self) -> None:  # optional override
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FfmpegRtspSource(FrameSource):
    """Continuous RTSP -> rgb24 frames via an ffmpeg subprocess.

    The stream resolution is auto-probed with ffprobe (falls back to
    `width`/`height` if ffprobe is unavailable), so the same class works for the
    sub (640x480) or main (4K) stream.
    """

    def __init__(
        self,
        url: str = DEFAULT_SUB_STREAM,
        width: Optional[int] = None,
        height: Optional[int] = None,
        rtsp_transport: str = "tcp",
        low_latency: bool = True,
        ffmpeg_bin: str = "ffmpeg",
        **_ignored,
    ):
        self.url = url
        self.rtsp_transport = rtsp_transport
        self.low_latency = low_latency
        self.ffmpeg_bin = ffmpeg_bin or "ffmpeg"
        self._proc: Optional[subprocess.Popen] = None

        if width and height:
            self.w, self.h = int(width), int(height)
        else:
            self.w, self.h = self._probe_size(url, rtsp_transport)

        self._frame_bytes = self.w * self.h * 3

    # -- helpers ---------------------------------------------------------- #
    @staticmethod
    def _probe_size(url: str, transport: str) -> tuple[int, int]:
        ffprobe = shutil.which("ffprobe")
        if ffprobe:
            try:
                out = subprocess.check_output(
                    [ffprobe, "-v", "error", "-rtsp_transport", transport,
                     "-select_streams", "v:0", "-show_entries",
                     "stream=width,height", "-of", "csv=p=0:s=x", url],
                    stderr=subprocess.STDOUT, timeout=15,
                ).decode().strip().splitlines()
                for line in out:
                    if "x" in line:
                        w, h = line.split("x")[:2]
                        return int(w), int(h)
            except Exception:
                pass
        return 640, 480  # sub-stream default

    def _build_cmd(self) -> list[str]:
        cmd = [self.ffmpeg_bin, "-hide_banner", "-loglevel", "error"]
        if self.low_latency:
            cmd += ["-fflags", "nobuffer", "-flags", "low_delay"]
        cmd += ["-rtsp_transport", self.rtsp_transport, "-i", self.url,
                "-an", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
        return cmd

    def _read_exact(self, n: int) -> Optional[bytes]:
        assert self._proc and self._proc.stdout
        buf = bytearray()
        stdout = self._proc.stdout
        while len(buf) < n:
            chunk = stdout.read(n - len(buf))
            if not chunk:
                return None  # EOF / decoder died
            buf.extend(chunk)
        return bytes(buf)

    # -- FrameSource ------------------------------------------------------ #
    def frames(self) -> Iterator[Frame]:
        self._proc = subprocess.Popen(
            self._build_cmd(),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=self._frame_bytes,
        )
        try:
            while True:
                raw = self._read_exact(self._frame_bytes)
                if raw is None:
                    break
                arr = np.frombuffer(raw, dtype=np.uint8).reshape(self.h, self.w, 3)
                yield Frame(data=arr, w=self.w, h=self.h, fmt="RGB",
                            pts=time.monotonic())
        finally:
            self.close()

    def close(self) -> None:
        proc = self._proc
        self._proc = None
        if proc and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


class SnapshotSource(FrameSource):
    """Low-fps fallback: repeatedly grab one decoded JPEG frame via ffmpeg.

    Reuses the same RTSP stream but tears the decoder down each grab, so fps is
    low; kept simple and dependency-free (PIL decodes the JPEG). Demonstrates a
    second implementation behind the identical ABC.
    """

    def __init__(self, url: str = DEFAULT_SUB_STREAM, interval: float = 0.0,
                 rtsp_transport: str = "tcp", ffmpeg_bin: str = "ffmpeg",
                 **_ignored):
        self.url = url
        self.interval = interval
        self.rtsp_transport = rtsp_transport
        self.ffmpeg_bin = ffmpeg_bin or "ffmpeg"
        self._stop = False

    def _grab_one(self) -> Optional[np.ndarray]:
        from PIL import Image
        import io
        cmd = [self.ffmpeg_bin, "-hide_banner", "-loglevel", "error",
               "-rtsp_transport", self.rtsp_transport, "-i", self.url,
               "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "-"]
        try:
            out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL,
                                          timeout=15)
            if not out:
                return None
            # RTSP 断流时 ffmpeg 可能 exit 0 却只吐出半张 JPEG。完整 JPEG 必以
            # EOI (FF D9) 结尾；半张交给 PIL 要么抛 truncated，要么（若进程里
            # 有人开了 ImageFile.LOAD_TRUNCATED_IMAGES）被静默补成大片黑——
            # 黑帧喂进模型即"检测不出来"。这里直接丢弃并计数，绝不下传。
            if not out.rstrip(b"\0").endswith(b"\xff\xd9"):
                self._drop_count = getattr(self, "_drop_count", 0) + 1
                if self._drop_count % 30 == 1:
                    print(f"[SnapshotSource] truncated JPEG dropped "
                          f"(x{self._drop_count}); check RTSP stability",
                          flush=True)
                return None
            img = Image.open(io.BytesIO(out)).convert("RGB")
            arr = np.asarray(img, dtype=np.uint8)
            # 黑帧守卫：均值亮度过低的整帧不是相机画面（解码补黑/信号丢失）。
            if arr.mean() < 2.0:
                self._black_count = getattr(self, "_black_count", 0) + 1
                if self._black_count % 30 == 1:
                    print(f"[SnapshotSource] near-black frame dropped "
                          f"(x{self._black_count})", flush=True)
                return None
            return arr
        except Exception:
            return None

    def frames(self) -> Iterator[Frame]:
        while not self._stop:
            arr = self._grab_one()
            if arr is not None:
                h, w = arr.shape[:2]
                yield Frame(data=arr, w=w, h=h, fmt="RGB", pts=time.monotonic())
            if self.interval:
                time.sleep(self.interval)

    def close(self) -> None:
        self._stop = True


def open_frame_source(url: str = DEFAULT_SUB_STREAM, prefer: str = "ffmpeg",
                      **kw) -> FrameSource:
    """Factory. `prefer` = "ffmpeg" (streaming) | "snapshot" (fallback).

    Delegates to the capability registry, which probes for the official R1
    frame broker (`/run/recamera/frame.sock`) and returns an
    `OfficialFrameSource` when present. On today's firmware the socket does not
    exist, so the registry falls back to the workaround backend selected by
    `prefer` and behaviour is unchanged.
    """
    from .registry import select_frame_source
    return select_frame_source(url=url, prefer=prefer, **kw)

"""reCamera Pro 的取帧口：ffmpeg 子进程解 rkipc 的 H.265 子码流，吐 BGR 原始帧。

`core_inspection.sources.open_capture` 用 `cv2.VideoCapture`，在这块板上打不开
RTSP：设备自带的 OpenCV 4.6.0 没有 ffmpeg 后端，`isOpened()` 直接 False。这里换掉
的只是「怎么拿到一帧 BGR」，`FrameSource` 的线程模型、丢帧策略、失败重试全部沿用
core——`InspectionApp(capture_factory=...)` 就是给这种平台差异留的口子。

**为什么不是 go2rtc 的 `/api/frame.jpeg`。** 那个端点每次请求都现做一次
H.265→JPEG 转码，同一台设备实测单次 4.9-10.0 s，接进常驻链路是 0.1 fps。抓一张
图排查它够用，做常驻取帧不行。

**读管道必须读满一整帧。** `subprocess` 的管道 `read(n)` 只保证「至多 n 字节」，
一帧 640x480x3 = 921 600 字节远超一次管道读的量。按「少于 n 就当流结束」写，表现
是一帧都收不到且没有任何报错。

**取帧不停自带应用。** 这路 RTSP 的来源正是 rkipc；appmgr 的单活切换只关别的推理
应用，不关供帧链路。
"""
from __future__ import annotations

import logging
import subprocess

import numpy as np

LOG = logging.getLogger("inspection-assembly.rtsp")

#: rkipc 的子码流。`live/0` 是 4K 主码流；检测输入是 640x640，拉主码流纯属浪费
#: 解码时间。
DEFAULT_SUB_STREAM = "rtsp://127.0.0.1:5554/live/1"
DEFAULT_WIDTH = 640
DEFAULT_HEIGHT = 480


class FfmpegCapture:
    """`cv2.VideoCapture` 接口的一个子集，够 `core_inspection.sources.FrameSource` 用。"""

    def __init__(self, url: str = DEFAULT_SUB_STREAM,
                 width: int = DEFAULT_WIDTH, height: int = DEFAULT_HEIGHT,
                 transport: str = "tcp", fps: float = 0.0,
                 read_timeout_s: float = 5.0) -> None:
        self.url = url
        self.width = int(width)
        self.height = int(height)
        self.fps = float(fps or 0.0)
        self._frame_bytes = self.width * self.height * 3
        # `core_inspection.sources` 的节拍限速对 live 源不生效（它只给文件源补
        # sleep），所以 RTSP 这一路的限速必须在解码这一层做：ffmpeg 的 fps 滤镜
        # 直接少吐帧，进 NPU 的量与 CPU 上的拷贝量一起降下来。
        vf = "scale=%d:%d" % (self.width, self.height)
        if self.fps > 0:
            vf = "fps=%g,%s" % (self.fps, vf)
        cmd = [
            "ffmpeg", "-loglevel", "error",
            "-rtsp_transport", transport,
            "-i", url,
            "-an",
            "-vf", vf,
            "-f", "rawvideo", "-pix_fmt", "bgr24", "-",
        ]
        # 直接继承 supervisor 的 stderr，ffmpeg 的启动/解码错误才能进入 app.log；
        # 不使用 PIPE，避免无人 drain 时把子进程堵死。
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=None,
            bufsize=self._frame_bytes)
        LOG.info("ffmpeg capture started: %s -> %dx%d bgr24",
                 url, self.width, self.height)

    def isOpened(self) -> bool:  # noqa: N802 - cv2.VideoCapture 的接口名
        return self._proc is not None and self._proc.poll() is None

    def set(self, *_args) -> bool:  # noqa: A003 - 尺寸由构造时的 scale 决定
        return False

    def _read_exact(self):
        """读满一整帧。管道 `read(n)` 只保证至多 n 字节，见模块 docstring。"""
        buf = bytearray()
        stdout = self._proc.stdout
        while len(buf) < self._frame_bytes:
            chunk = stdout.read(self._frame_bytes - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def read(self):
        if self._proc is None:
            return False, None
        try:
            raw = self._read_exact()
        except (OSError, ValueError):
            return False, None
        if raw is None:
            return False, None
        frame = np.frombuffer(bytes(raw), dtype=np.uint8)
        return True, frame.reshape(self.height, self.width, 3)

    def release(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001 - 收尾失败不影响退出码
            pass
        finally:
            if proc.stdout is not None:
                try:
                    proc.stdout.close()
                except OSError:
                    pass


def capture_factory(spec):
    """`core_inspection.sources.FrameSource` 要的工厂。宽高取 `sources[]` 里的声明。

    只有 RTSP 走 ffmpeg 子进程。指向本地文件时交回 core 的 `open_capture`：设备的
    OpenCV 打不开 RTSP，但打得开本地视频文件，而回放验收（拿一段已标注的素材喂
    这条常驻链路）需要的正是后者。
    """
    from core_inspection.sources import SourceError, open_capture

    if not str(spec.uri).lower().startswith(("rtsp://", "rtsps://")):
        return open_capture(spec)

    capture = FfmpegCapture(spec.uri,
                            width=spec.width or DEFAULT_WIDTH,
                            height=spec.height or DEFAULT_HEIGHT,
                            fps=float(getattr(spec, "fps", 0) or 0))
    if not capture.isOpened():
        capture.release()
        raise SourceError("cannot open source %s: %s (ffmpeg exited immediately)"
                          % (spec.stream_id, spec.uri))
    return capture

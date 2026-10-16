"""reCamera Pro 的取帧口：ffmpeg 子进程解 H.265 子码流，吐 BGR 原始帧。

`core_waste.sources.open_capture` 用 `cv2.VideoCapture`，在这块板上打不开 RTSP：
设备自带的 OpenCV 4.6.0 没有 ffmpeg 后端，`isOpened()` 直接 False（`rtsp://127.0.0.1:5554/live/1`
与 go2rtc 的 `rtsp://127.0.0.1:554/sub` 都试过）。所以这里换掉的只是"怎么拿到一帧
BGR"，`FrameSource` 的线程模型、丢帧策略、失败重试全部沿用 core——
`WasteApp(capture_factory=...)` 就是给这种平台差异留的口子。

**为什么不是 go2rtc 的 `/api/frame.jpeg`。** 那个端点每次请求都现做一次 H.265→JPEG
转码：设备实测 `curl` 单次 4.9 / 10.0 / 10.0 s，接进这条常驻链路是 0.1 fps
（40 s 只采到 4 帧）。抓一张图做排查它很好用（`gotchas-recamera-pro` #18 说的就是
这个场景），做常驻取帧不行。同一台设备上 ffmpeg 拉 RTSP 子码流实测 40 帧 / 3.46 s
≈ 11.5 fps，与 kit 自己的 `FfmpegRtspSource` 是同一条路子。

**读管道必须读满一整帧再交出去。** `subprocess` 的管道 `read(n)` 只保证"至多 n
字节"，一帧 640x480x3 = 921 600 字节远超一次管道读的量。按"少于 n 就当流结束"写，
表现是**一帧都收不到**且没有任何报错——排查时看起来像相机没出图。

**软解码不是瓶颈。** 640x480 H.265 软解在这颗 4 核 CPU 上约占一个核的 17%
（kit 的实测），而 NPU 推理是 5.8 ms/帧。真正的上限是子码流本身的帧率。
"""
from __future__ import annotations

import logging
import subprocess

import numpy as np

LOG = logging.getLogger("waste-sorting.rtsp")

#: rkipc 的子码流。`live/0` 是 4K 主码流，分类只需要 224x224，拉主码流纯属浪费。
DEFAULT_SUB_STREAM = "rtsp://127.0.0.1:5554/live/1"
#: 解码后交给分类器的尺寸。短边 480 > 256，`crop_rgb_u8` 的短边缩放照常成立。
DEFAULT_WIDTH = 640
DEFAULT_HEIGHT = 480


class FfmpegCapture:
    """`cv2.VideoCapture` 接口的一个子集，够 `core_waste.sources.FrameSource` 用。"""

    def __init__(self, url: str = DEFAULT_SUB_STREAM,
                 width: int = DEFAULT_WIDTH, height: int = DEFAULT_HEIGHT,
                 transport: str = "tcp") -> None:
        self.url = url
        self.width = int(width)
        self.height = int(height)
        self._frame_bytes = self.width * self.height * 3
        cmd = [
            "ffmpeg", "-loglevel", "error",
            "-rtsp_transport", transport,
            "-i", url,
            "-an",                                   # 子码流带一路音频，丢掉
            "-vf", "scale=%d:%d" % (self.width, self.height),
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
    """`core_waste.sources.FrameSource` 要的工厂。宽高取 `sources[]` 里的声明。"""
    from core_waste.sources import SourceError

    capture = FfmpegCapture(spec.uri,
                            width=spec.width or DEFAULT_WIDTH,
                            height=spec.height or DEFAULT_HEIGHT)
    if not capture.isOpened():
        capture.release()
        raise SourceError("cannot open source %s: %s (ffmpeg exited immediately)"
                          % (spec.stream_id, spec.uri))
    return capture

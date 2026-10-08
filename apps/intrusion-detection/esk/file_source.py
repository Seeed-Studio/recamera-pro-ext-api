"""A local-file replay FrameSource, so the truth video can be asserted on.

Why this exists at all: the two other edge-security-kit detector platforms take
an RTSP URL, so the harness points them at a synthetic clip whose person
trajectory is known frame by frame, and the assertions are about exact
instants. reCamera Pro's input is its own sensor -- there is no URL to swap --
so without a replay path the third platform could only be checked by walking in
front of the camera, which proves the wiring and nothing about the geometry.

The kit's ``FfmpegRtspSource`` cannot be reused for a file: it always emits
``-rtsp_transport tcp`` ahead of ``-i``, and ffmpeg rejects that option for a
non-RTSP demuxer. Everything else is the same shape -- an ffmpeg subprocess
decoding to ``rgb24`` on stdout, read in fixed-size chunks -- and the yielded
object is the kit's own ``Frame``, so the app's loop cannot tell the two apart.

``-re`` paces the read at the file's own frame rate. Without it ffmpeg delivers
the whole clip as fast as the pipe drains and the detector sees a 5 fps
trajectory arriving at 40 fps, which silently rescales every dwell and cooldown
the hub is about to judge.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from typing import Iterator, Optional

import numpy as np

from kit.adapters.frame_source import Frame, FrameSource


class FfmpegFileSource(FrameSource):
    """Replay a local video file as RGB frames, optionally looping."""

    def __init__(
        self,
        path: str,
        *,
        loop: bool = True,
        realtime: bool = True,
        ffmpeg_bin: str = "ffmpeg",
    ) -> None:
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        self.path = path
        self.loop = bool(loop)
        self.realtime = bool(realtime)
        self.ffmpeg_bin = ffmpeg_bin or "ffmpeg"
        self._proc: Optional[subprocess.Popen] = None
        self.w, self.h = self._probe_size(path)
        self._frame_bytes = self.w * self.h * 3
        # Reported verbatim on /debug/decode. This source is unambiguously the
        # CPU decoder and says so; no hardware element is even requested.
        self.decode_path = "sw"
        self.decoder_cmd = " ".join(self._build_cmd())
        self.decoder_report = "ffmpeg software decode of a local file (replay mode)"
        self.source_size = [self.w, self.h]

    @staticmethod
    def _probe_size(path: str) -> tuple:
        ffprobe = shutil.which("ffprobe")
        if ffprobe:
            try:
                out = subprocess.check_output(
                    [ffprobe, "-v", "error", "-select_streams", "v:0",
                     "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", path],
                    stderr=subprocess.STDOUT, timeout=15,
                ).decode().strip().splitlines()
                for line in out:
                    if "x" in line:
                        w, h = line.split("x")[:2]
                        return int(w), int(h)
            except Exception:
                pass
        raise RuntimeError(f"could not probe the frame size of {path}")

    def _build_cmd(self) -> list:
        cmd = [self.ffmpeg_bin, "-hide_banner", "-loglevel", "error"]
        if self.realtime:
            cmd += ["-re"]
        if self.loop:
            cmd += ["-stream_loop", "-1"]
        cmd += ["-i", self.path, "-an", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
        return cmd

    def _read_exact(self, n: int) -> Optional[bytes]:
        assert self._proc and self._proc.stdout
        buf = bytearray()
        stdout = self._proc.stdout
        while len(buf) < n:
            chunk = stdout.read(n - len(buf))
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)

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

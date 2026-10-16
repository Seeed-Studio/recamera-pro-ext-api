import importlib.util
from pathlib import Path

import numpy as np


APP_DIR = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("inspection_rtsp_source", APP_DIR / "rtsp_source.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class FakeStdout:
    def __init__(self, payload):
        self.payload = bytearray(payload)
        self.closed = False

    def read(self, size):
        chunk = bytes(self.payload[: max(1, min(size, 2))])
        del self.payload[: len(chunk)]
        return chunk

    def close(self):
        self.closed = True


class FakeProcess:
    def __init__(self, payload=b"", running=True):
        self.stdout = FakeStdout(payload)
        self._running = running
        self.killed = False
        self.wait_timeout = None

    def poll(self):
        return None if self._running else 1

    def kill(self):
        self.killed = True
        self._running = False

    def wait(self, timeout=None):
        self.wait_timeout = timeout


def test_device_ffmpeg_command_omits_unsupported_timeout_and_inherits_stderr(monkeypatch):
    calls = {}
    proc = FakeProcess(running=False)

    def fake_popen(cmd, **kwargs):
        calls["cmd"] = cmd
        calls["kwargs"] = kwargs
        return proc

    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)
    capture = module.FfmpegCapture("rtsp://camera/live/1", width=2, height=1,
                                   read_timeout_s=5)

    assert "-rw_timeout" not in calls["cmd"]
    assert calls["kwargs"]["stderr"] is None
    assert not capture.isOpened()
    capture.release()
    assert proc.killed


def test_read_collects_a_full_frame_and_release_closes_pipe(monkeypatch):
    raw = bytes(range(6))
    proc = FakeProcess(raw)
    monkeypatch.setattr(module.subprocess, "Popen", lambda *_args, **_kwargs: proc)
    capture = module.FfmpegCapture("rtsp://camera/live/1", width=2, height=1)

    ok, frame = capture.read()
    assert ok
    np.testing.assert_array_equal(frame, np.frombuffer(raw, dtype=np.uint8).reshape(1, 2, 3))
    capture.release()
    assert proc.killed
    assert proc.wait_timeout == 5
    assert proc.stdout.closed

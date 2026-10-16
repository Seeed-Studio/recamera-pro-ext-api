import importlib.util
from pathlib import Path

import numpy as np


APP_DIR = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("waste_rtsp_source", APP_DIR / "rtsp_source.py")
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
    def __init__(self, payload=b""):
        self.stdout = FakeStdout(payload)
        self.killed = False
        self.wait_timeout = None

    def poll(self):
        return None

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.wait_timeout = timeout


def test_ffmpeg_command_inherits_stderr_for_diagnostics(monkeypatch):
    calls = {}
    proc = FakeProcess()

    def fake_popen(cmd, **kwargs):
        calls["cmd"] = cmd
        calls["kwargs"] = kwargs
        return proc

    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)
    capture = module.FfmpegCapture("rtsp://camera/live/1", width=2, height=1)

    assert calls["kwargs"]["stderr"] is None
    assert "-rw_timeout" not in calls["cmd"]
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

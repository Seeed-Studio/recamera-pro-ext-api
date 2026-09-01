"""
Offline unit tests for the audio sources -- focus: `AiAsrAudioSource`
(official ALSA `ai_asr` path) command assembly + registry selection.

Run:  python3 -m kit.adapters.test_audio_source   (from repo root)

No device, no real audio: subprocess.Popen is monkeypatched with an in-memory
fake, so nothing ever touches /dev/snd. Constructors never open a device.
"""
import os
import socket
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from kit.adapters import audio_source as A
from kit.adapters import registry
from kit.adapters.audio_source import (AiAsrAudioSource, RtspAudioSource,
                                       AlsaTakeoverSource, PcmFrame,
                                       AudioDeviceBusy, DEFAULT_AUDIO_FILTER)


# --- fakes ------------------------------------------------------------------- #
class _FakeStdout:
    def __init__(self, data: bytes):
        self._buf = bytearray(data)

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            out, self._buf = bytes(self._buf), bytearray()
            return out
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def close(self):
        pass


class _FakeStderr:
    def __init__(self, data: bytes = b""):
        self._data = data

    def read(self, *a) -> bytes:
        return self._data


class _FakeProc:
    def __init__(self, stdout_data=b"", stderr_data=b""):
        self.stdout = _FakeStdout(stdout_data)
        self.stderr = _FakeStderr(stderr_data)
        self._terminated = False

    def poll(self):
        return None if not self._terminated else 0

    def send_signal(self, *a):
        self._terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self._terminated = True


def _patch_popen(monkey_data):
    """Return a fake Popen factory. `monkey_data` is a list consumed in Popen
    call order (arecord first, then ffmpeg). Each entry: (stdout_bytes, stderr).
    """
    calls = {"cmds": []}
    seq = list(monkey_data)

    def _factory(cmd, **kw):
        calls["cmds"].append(cmd)
        stdout_data, stderr_data = seq.pop(0) if seq else (b"", b"")
        return _FakeProc(stdout_data, stderr_data)

    return _factory, calls


# --- command assembly (pure, no subprocess) ---------------------------------- #
def test_command_assembly_defaults():
    """Default: native 4ch arecord from ai_asr, ffmpeg pan ch0 + loudnorm."""
    src = AiAsrAudioSource()
    assert src.device == "ai_asr"
    assert src.capture_rate == 16000        # dsnoop slave is fixed 16k
    assert src.capture_ch == 4              # native 4ch (ch0=Mic1..ch3=Ref)
    assert src.mic_channel == 0

    arec = src._arecord_cmd()
    assert arec == ["arecord", "-D", "ai_asr", "-f", "S16_LE",
                    "-r", "16000", "-c", "4", "-t", "raw", "-q", "-"], arec

    fg = src._ffmpeg_filtergraph()
    # deterministic Mic1 selection (NOT an average) + loudnorm gain preserved
    assert fg == "pan=mono|c0=c0,loudnorm=I=-16:TP=-1.5", fg
    assert src.audio_filter == DEFAULT_AUDIO_FILTER

    ff = src._ffmpeg_cmd()
    assert ff[:9] == ["ffmpeg", "-loglevel", "error", "-nostdin",
                      "-f", "s16le", "-ar", "16000", "-ac"], ff
    assert "-af" in ff and fg in ff
    assert ff[-7:] == ["-ac", "1", "-ar", "16000", "-f", "s16le", "-"], ff
    assert src._needs_ffmpeg() is True
    print("PASS test_command_assembly_defaults (arecord 4ch | ffmpeg pan+loudnorm)")


def test_mic_channel_override():
    """mic_channel selects a different hw channel in the pan filter."""
    src = AiAsrAudioSource(mic_channel=1)     # Mic 2
    assert src._ffmpeg_filtergraph().startswith("pan=mono|c0=c1,"), \
        src._ffmpeg_filtergraph()
    print("PASS test_mic_channel_override (pan c0=c1)")


def test_gain_disabled_still_pans():
    """Filter off but 4ch capture -> ffmpeg still needed (channel selection)."""
    src = AiAsrAudioSource(audio_filter="none")
    assert src.audio_filter is None
    assert src._ffmpeg_filtergraph() == "pan=mono|c0=c0"  # pan only, no loudnorm
    assert src._needs_ffmpeg() is True
    print("PASS test_gain_disabled_still_pans (pan only, no ffmpeg skip)")


def test_mono_capture_unity_skips_ffmpeg():
    """capture_ch=1 + unity gain -> arecord already mono 16k -> no ffmpeg hop."""
    src = AiAsrAudioSource(capture_ch=1, audio_filter="off")
    assert src._needs_ffmpeg() is False
    arec = src._arecord_cmd()
    assert arec[7:9] == ["-c", "1"], arec       # asks plug for 1ch downmix
    print("PASS test_mono_capture_unity_skips_ffmpeg (arecord-direct, no ffmpeg)")


def test_custom_filter_passthrough():
    """Operator override filter is passed through verbatim after the pan."""
    src = AiAsrAudioSource(audio_filter="dynaudnorm=f=150:g=11")
    assert src._ffmpeg_filtergraph() == "pan=mono|c0=c0,dynaudnorm=f=150:g=11"
    print("PASS test_custom_filter_passthrough")


# --- open()/read() with mocked subprocess ------------------------------------ #
def test_open_read_frames_mocked():
    """Mocked arecord|ffmpeg pipeline yields 16k mono PcmFrames of chunk size."""
    src = AiAsrAudioSource()                      # default -> uses ffmpeg
    chunk = src._chunk_bytes                       # 100ms @ 16k mono S16 = 3200
    assert chunk == 3200, chunk
    payload = bytes(chunk * 3)                     # 3 chunks of silence
    factory, calls = _patch_popen([(b"", b""),     # arecord: not read directly
                                   (payload, b"")])  # ffmpeg: the tail we read
    orig = A.subprocess.Popen
    orig_which = A.shutil.which
    A.subprocess.Popen = factory
    A.shutil.which = lambda name: "/usr/bin/" + name   # pretend tools present
    try:
        with src as s:
            f1 = s.read()
            f2 = s.read()
            assert isinstance(f1, PcmFrame)
            assert f1.rate == 16000 and f1.ch == 1
            assert len(f1.pcm) == chunk and len(f2.pcm) == chunk
        # two Popen calls: arecord then ffmpeg, in order
        assert len(calls["cmds"]) == 2, calls["cmds"]
        assert calls["cmds"][0][0] == "arecord"
        assert calls["cmds"][1][0] == "ffmpeg"
    finally:
        A.subprocess.Popen = orig
        A.shutil.which = orig_which
    print("PASS test_open_read_frames_mocked (2-stage pipe -> 16k mono frames)")


def test_open_permission_error_raises_busy():
    """arecord permission failure -> actionable AudioDeviceBusy (root/audio grp)."""
    src = AiAsrAudioSource(capture_ch=1, audio_filter="off")  # arecord-only path
    factory, _ = _patch_popen([
        (b"", b"arecord: main:831: audio open error: No such file or directory")])
    orig = A.subprocess.Popen
    orig_which = A.shutil.which
    A.subprocess.Popen = factory
    A.shutil.which = lambda name: "/usr/bin/" + name
    try:
        raised = None
        try:
            src.open()
        except AudioDeviceBusy as e:
            raised = e
        assert raised is not None, "expected AudioDeviceBusy on permission error"
        assert "audio" in str(raised).lower() and "root" in str(raised).lower()
    finally:
        A.subprocess.Popen = orig
        A.shutil.which = orig_which
    print("PASS test_open_permission_error_raises_busy (root/audio guidance)")


# --- registry selection ------------------------------------------------------ #
def _clear_env():
    for k in ("RECAMERA_AUDIO_SOCK", "RECAMERA_ADAPTER_PREFER"):
        os.environ.pop(k, None)


def test_registry_default_is_ai_asr():
    """No official broker -> default selection is the ai_asr official path."""
    _clear_env()
    os.environ["RECAMERA_AUDIO_SOCK"] = "/nonexistent/audio.sock.absent"
    registry.capabilities(refresh=True)
    src = registry.select_audio_source()          # default prefer
    assert isinstance(src, AiAsrAudioSource), type(src)
    rtsp = registry.select_audio_source(prefer="rtsp")
    assert isinstance(rtsp, RtspAudioSource), type(rtsp)
    alsa = registry.select_audio_source(prefer="alsa")
    assert isinstance(alsa, AlsaTakeoverSource), type(alsa)
    _clear_env()
    print("PASS test_registry_default_is_ai_asr (ai_asr default; rtsp/alsa opt-in)")


def test_registry_audio_socket_is_unknown_until_explicit_opt_in():
    """A filesystem audio socket stays UNKNOWN; prefer=official must opt in."""
    _clear_env()
    from kit.adapters.official import OfficialPcmSource
    with tempfile.TemporaryDirectory(prefix="audio-sock-") as directory:
        endpoint = os.path.join(directory, "audio.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(endpoint)
        os.environ["RECAMERA_AUDIO_SOCK"] = endpoint
        registry.capabilities(refresh=True)
        src = registry.select_audio_source()
        assert isinstance(src, AiAsrAudioSource), type(src)

        os.environ["RECAMERA_ADAPTER_PREFER"] = "official"
        registry.capabilities(refresh=True)
        forced = registry.select_audio_source()
        assert isinstance(forced, OfficialPcmSource), type(forced)
        server.close()
    _clear_env()
    registry.capabilities(refresh=True)
    print("PASS test_registry_audio_socket_is_unknown_until_explicit_opt_in "
          "(socket probe stays workaround; prefer=official selects OfficialPcmSource)")


if __name__ == "__main__":
    test_command_assembly_defaults()
    test_mic_channel_override()
    test_gain_disabled_still_pans()
    test_mono_capture_unity_skips_ffmpeg()
    test_custom_filter_passthrough()
    test_open_read_frames_mocked()
    test_open_permission_error_raises_busy()
    test_registry_default_is_ai_asr()
    test_registry_audio_socket_is_unknown_until_explicit_opt_in()
    _clear_env()
    print("ALL AUDIO SOURCE TESTS PASSED")

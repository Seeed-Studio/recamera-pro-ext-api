"""
Offline unit tests for the L0 capability registry (docs/guide/adapter-bootstrap.md §3).

Run:  python3 -m kit.adapters.test_registry     (from repo root)
No device, no network: FfmpegRtspSource is constructed with explicit
width/height so it never probes the RTSP stream.
"""
import os
import socket
import sys
import tempfile

# Allow running as a plain script from the repo root.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from kit.adapters import registry
from kit.adapters.frame_source import FfmpegRtspSource, SnapshotSource, open_frame_source
from kit.adapters.result_sink import StdoutSink, WsResultSink, open_result_sink
from kit.adapters.official import OfficialFrameSource, OsdInjectResultSink
from kit.errors import CapabilityError, ConfigurationError


def _clear_env():
    for k in ("RECAMERA_FRAME_SOCK", "RECAMERA_RESULT_SOCK", "RECAMERA_AUDIO_SOCK",
              "RECAMERA_RESULT_INGRESS", "RECAMERA_RESULT_OSD", "RECAMERA_CONTROL_API",
              "RECAMERA_FRAME_SOURCE", "RECAMERA_RESULT_GATEWAY_SOCK",
              "RECAMERA_ADAPTER_PREFER"):
        os.environ.pop(k, None)


def test_no_official_selects_workaround():
    """No official endpoints -> workaround implementations, behaviour unchanged."""
    _clear_env()
    # Point the frame-broker probe at a path guaranteed absent.
    os.environ["RECAMERA_FRAME_SOCK"] = "/nonexistent/frame.sock.absent"
    os.environ["RECAMERA_RESULT_SOCK"] = "/nonexistent/result-in.sock.absent"
    os.environ["RECAMERA_AUDIO_SOCK"] = "/nonexistent/audio.sock.absent"
    caps = registry.capabilities(refresh=True)
    assert caps.frame_broker is False, caps
    assert caps.result_ingress is False, caps

    src = open_frame_source(url="rtsp://x", prefer="ffmpeg", width=640, height=480)
    assert isinstance(src, FfmpegRtspSource), type(src)
    src.close()

    snap = open_frame_source(url="rtsp://x", prefer="snapshot")
    assert isinstance(snap, SnapshotSource), type(snap)
    snap.close()

    dbg = open_result_sink("stdout")
    assert isinstance(dbg, StdoutSink), type(dbg)

    ws = open_result_sink("ws", host="127.0.0.1", port=0, app_id="t")
    assert isinstance(ws, WsResultSink), type(ws)
    ws.close()
    print("PASS test_no_official_selects_workaround "
          "(FfmpegRtspSource / SnapshotSource / StdoutSink / WsResultSink)")


def test_simulated_official_selects_official():
    """Unknown socket does not auto-select; explicit verified policy does."""
    _clear_env()
    with tempfile.NamedTemporaryFile(prefix="frame-", suffix=".sock") as tf:
        os.environ["RECAMERA_FRAME_SOCK"] = tf.name  # regular file, not socket
        caps = registry.capabilities(refresh=True)
        assert caps.frame_broker is False, caps

        src = open_frame_source(url="rtsp://x", prefer="ffmpeg")
        assert isinstance(src, FfmpegRtspSource), type(src)
        src.close()

    os.environ.pop("RECAMERA_FRAME_SOCK", None)
    os.environ["RECAMERA_ADAPTER_PREFER"] = "official"
    src = open_frame_source(url="rtsp://x", prefer="ffmpeg")
    assert isinstance(src, OfficialFrameSource), type(src)
    assert src.sock == "/run/recamera/frame.sock", src.sock
    # explicit snapshot fallback is still honoured verbatim
    snap = open_frame_source(url="rtsp://x", prefer="snapshot")
    assert isinstance(snap, SnapshotSource), type(snap)
    snap.close()
    print("PASS test_simulated_official_selects_official "
          "(unknown endpoint fails closed; explicit policy selects official)")


def test_result_sink_defaults_to_ws_even_when_socket_present():
    """★S1: AI results default to SOFTWARE overlay.★

    Even when the official result-ingress socket exists (result_ingress probes
    True), select_result_sink must return WsResultSink by default -- OSD burn-in
    is opt-in only, never auto-selected on socket presence. This replaces the
    batch-1 RECAMERA_RESULT_SOCK=/run/recamera/__no_ingress__ env hack.
    """
    _clear_env()
    os.environ["RECAMERA_FRAME_SOCK"] = "/nonexistent/frame.sock.absent"
    with tempfile.TemporaryDirectory(prefix="result-in-") as directory:
        endpoint = os.path.join(directory, "result-in.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(endpoint)
        os.environ["RECAMERA_RESULT_SOCK"] = endpoint
        caps = registry.capabilities(refresh=True)
        assert caps.result_ingress is True, caps      # probe sees it...

        # ...yet the default sink is still the software WS overlay.
        ws = open_result_sink("ws", host="127.0.0.1", port=0, app_id="t")
        assert isinstance(ws, WsResultSink), type(ws)
        ws.close()

        # Explicit opt-in via RECAMERA_RESULT_OSD flips it to OSD burn-in.
        os.environ["RECAMERA_RESULT_OSD"] = "1"
        try:
            open_result_sink("ws", host="127.0.0.1", port=0, app_id="t")
            raise AssertionError("native custom result endpoint must be rejected")
        except CapabilityError:
            pass
        os.environ.pop("RECAMERA_RESULT_OSD", None)

        server.close()
    os.environ.pop("RECAMERA_RESULT_SOCK", None)
    # kind="osd" forces the canonical native endpoint without any env.
    osd2 = open_result_sink("osd", app_id="t")
    assert isinstance(osd2, OsdInjectResultSink), type(osd2)
    print("PASS test_result_sink_defaults_to_ws_even_when_socket_present "
          "(default WS; RECAMERA_RESULT_OSD / kind='osd' opt in to OSD)")


def test_prefer_override():
    """RECAMERA_ADAPTER_PREFER forces selection regardless of probe."""
    _clear_env()
    os.environ["RECAMERA_FRAME_SOCK"] = "/nonexistent/frame.sock.absent"
    os.environ["RECAMERA_RESULT_INGRESS"] = "1"  # pretend ingress probes true
    os.environ["RECAMERA_ADAPTER_PREFER"] = "workaround"
    registry.capabilities(refresh=True)
    ws = open_result_sink("ws", host="127.0.0.1", port=0, app_id="t")
    assert isinstance(ws, WsResultSink), type(ws)  # forced workaround despite ingress
    ws.close()

    os.environ["RECAMERA_ADAPTER_PREFER"] = "official"
    registry.capabilities(refresh=True)
    sink = open_result_sink("ws", host="127.0.0.1", port=0, app_id="t")
    assert isinstance(sink, OsdInjectResultSink), type(sink)  # forced official
    print("PASS test_prefer_override (workaround-force + official-force)")


def test_dedicated_frame_source_policy_does_not_change_result_sink():
    """The managed opt-in selects frame.sock without opting into OSD."""
    _clear_env()
    os.environ["RECAMERA_FRAME_SOURCE"] = "official"
    os.environ["RECAMERA_ADAPTER_PREFER"] = "workaround"
    registry.capabilities(refresh=True)

    src = open_frame_source(url="rtsp://sub", prefer="ffmpeg")
    assert isinstance(src, OfficialFrameSource), type(src)
    assert src.sock == "/run/recamera/frame.sock", src.sock
    # A managed launch's dedicated frame contract cannot be bypassed by an app
    # requesting the low-resolution snapshot workaround.
    explicit_snapshot = open_frame_source(url="rtsp://sub", prefer="snapshot")
    assert isinstance(explicit_snapshot, OfficialFrameSource), type(explicit_snapshot)

    ws = open_result_sink("ws", host="127.0.0.1", port=0, app_id="managed")
    assert isinstance(ws, WsResultSink), type(ws)
    ws.close()

    os.environ["RECAMERA_FRAME_SOURCE"] = "workaround"
    os.environ["RECAMERA_ADAPTER_PREFER"] = "official"
    fallback = open_frame_source(
        url="rtsp://sub", prefer="ffmpeg", width=640, height=480)
    assert isinstance(fallback, FfmpegRtspSource), type(fallback)
    fallback.close()

    os.environ["RECAMERA_FRAME_SOURCE"] = "auto"
    inherited = open_frame_source(url="rtsp://sub", prefer="ffmpeg")
    assert isinstance(inherited, OfficialFrameSource), type(inherited)
    _clear_env()
    print("PASS test_dedicated_frame_source_policy_does_not_change_result_sink")


def test_invalid_dedicated_frame_source_policy_fails_closed():
    _clear_env()
    os.environ["RECAMERA_FRAME_SOURCE"] = "offical"  # intentional typo
    try:
        open_frame_source(
            url="rtsp://sub", prefer="ffmpeg", width=640, height=480)
        raise AssertionError("invalid frame-source policy must be rejected")
    except ConfigurationError as exc:
        assert "RECAMERA_FRAME_SOURCE" in str(exc), str(exc)
    _clear_env()
    print("PASS test_invalid_dedicated_frame_source_policy_fails_closed")


if __name__ == "__main__":
    test_no_official_selects_workaround()
    test_simulated_official_selects_official()
    test_result_sink_defaults_to_ws_even_when_socket_present()
    test_prefer_override()
    test_dedicated_frame_source_policy_does_not_change_result_sink()
    test_invalid_dedicated_frame_source_policy_fails_closed()
    _clear_env()
    print("ALL REGISTRY TESTS PASSED")

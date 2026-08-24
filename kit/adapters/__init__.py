"""
Lazy adapter exports (PEP 562).

Importing this package no longer eagerly pulls in every adapter submodule.
Each public name is mapped to its submodule and imported on first attribute
access, so an **audio-only app** running in a venv WITHOUT cv2/paho (the device
`/userdata/rknnenv` sherpa venv) can do::

    from kit.adapters import RtspAudioSource

without dragging in the vision `frame_source` chain -- while vision apps see the
exact same names and behaviour (`from kit.adapters import FfmpegRtspSource`
transparently imports `frame_source` on first touch).

The previous eager form imported frame_source + result_sink + audio_source +
registry at package import time. That is preserved semantically (every name is
still reachable) but each submodule is now only imported when one of its names
is first used. Submodule access (`from kit.adapters.frame_source import X`, as
the base loop does) is unaffected -- that path never went through this module's
namespace.
"""
from __future__ import annotations

import importlib

# public name -> submodule that defines it
_EXPORTS = {
    # frame_source (vision; pulls numpy, and cv2 lazily deeper down)
    "Frame": "frame_source",
    "FrameSource": "frame_source",
    "FfmpegRtspSource": "frame_source",
    "SnapshotSource": "frame_source",
    "open_frame_source": "frame_source",
    "DEFAULT_SUB_STREAM": "frame_source",
    "DEFAULT_MAIN_STREAM": "frame_source",
    # result_sink (stdlib only)
    "ResultSink": "result_sink",
    "ResultPublisher": "result_sink",
    "StdoutSink": "result_sink",
    "WsResultSink": "result_sink",
    "GatewayResultSink": "result_sink",
    "MultiSink": "result_sink",
    "open_result_sink": "result_sink",
    # audio_source (stdlib only; numpy lazy inside methods)
    "PcmFrame": "audio_source",
    "AudioSource": "audio_source",
    "AlsaTakeoverSource": "audio_source",
    "RtspAudioSource": "audio_source",
    "WavFileAudioSource": "audio_source",
    "AudioDeviceBusy": "audio_source",
    "pcm_stats": "audio_source",
    "open_audio_source": "audio_source",
    # registry (stdlib only; concrete backends imported lazily inside factories)
    "Capabilities": "registry",
    "Capability": "registry",
    "CapabilityStatus": "registry",
    "capabilities": "registry",
    "probe_capabilities": "registry",
    "select_frame_source": "registry",
    "select_result_sink": "registry",
    "select_audio_source": "registry",
    "select_control": "registry",
    "select_probe": "registry",
    # control plane (official ABC + CgiControl workaround)
    "ControlPlane": "official",
    "OfficialControl": "official",
    "CgiControl": "cgi_control",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    """PEP 562: import the owning submodule on first access, then cache."""
    mod = _EXPORTS.get(name)
    if mod is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    submod = importlib.import_module(f".{mod}", __name__)
    value = getattr(submod, name)
    globals()[name] = value  # cache so subsequent access skips __getattr__
    return value


def __dir__():
    return sorted(set(list(globals().keys()) + __all__))

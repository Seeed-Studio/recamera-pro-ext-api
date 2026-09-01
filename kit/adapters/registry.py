"""
Capability registry for the L0 adapter layer (docs/guide/kit-design.md §L0,
docs/guide/adapter-bootstrap.md §3).

At startup the kit records whether official endpoint socket inodes exist.
Filesystem evidence is only ``UNKNOWN`` -- it is discovery, not a protocol
handshake -- so ``auto`` keeps the workaround path. Matching patched firmware
must therefore opt in explicitly (for example
``RECAMERA_ADAPTER_PREFER=official``); a future native handshake may safely
mark a capability ``AVAILABLE`` and let ``auto`` switch on its own.

    FrameSource  = dedicated policy / caps.frame_broker
                   ? OfficialFrameSource : FfmpegRtspSource
    ResultSink   = WsResultSink (DEFAULT, software overlay :8124);
                   OsdInjectResultSink (burn into码流) is EXPLICIT opt-in only
                   (RECAMERA_ADAPTER_PREFER=official | RECAMERA_RESULT_OSD=1 |
                   kind="osd") -- NOT auto-selected on result-in.sock presence.
    AudioSource  = caps.audio_broker   ? OfficialPcmSource     : (workaround TBD)
    ControlPlane = caps.control_api    ? OfficialControl       : CgiControl
    ProbeSource  = ProbeSource (SDK)   -- v1 baseline (probe@1), no workaround alt

Without an explicit opt-in or verified handshake, every factory selects the
existing workaround.  Socket presence alone never changes the data path.

Overrides (both for real deployments and for testing the switch logic)
----------------------------------------------------------------------
* `RECAMERA_*_SOCK` values affect filesystem diagnostics only.  The current
  native ABI fixes endpoint paths; official adapters reject a non-default path
  rather than pretending to route it.
* `RECAMERA_RESULT_INGRESS` marks result ingress present for diagnostics only;
  it still remains ``UNKNOWN`` and does not auto-select OSD burn-in.
* `RECAMERA_CONTROL_API=1` is an explicit control-plane opt-in: selection uses
  `OfficialControl` even though the capability is still reported as
  ``UNKNOWN`` until a versioned handshake exists.
* `RECAMERA_FRAME_SOURCE` = `official` | `workaround` | `auto` is the dedicated
  frame-source policy.  appmgr sets `official` only for a managed app whose
  installed, validated manifest claims `camera.frames`; no result/audio/control adapter reads
  this variable.  `auto` (and an unset variable) retains the existing global
  preference / verified-capability policy.  Invalid values are rejected rather
  than silently falling back to RTSP.
* `RECAMERA_ADAPTER_PREFER` = `auto` (default) | `official` | `workaround`
  -- a global manifest-style override of the per-capability auto selection
  (docs/guide/adapter-bootstrap.md §3: "可留 manifest 里 prefer: official|workaround 供覆盖").
"""
from __future__ import annotations

import os

from kit.capabilities import (
    Capabilities,
    Capability,
    CapabilityStatus,
    audio_socket_path,
    capabilities,
    frame_socket_path,
    probe_capabilities,
    probe_socket_path,
    result_socket_path,
)
from kit.errors import ConfigurationError


# -- capability probing ------------------------------------------------------- #
# Canonical extension-API socket paths (docs/api/spec.md §1: all live
# under /run/recamera/). These are the *real* names the shipped librecamera_ext
# uses -- singular `frame.sock` and `result-in.sock`.
def _frame_sock_path() -> str:
    return frame_socket_path()


def _result_sock_path() -> str:
    return result_socket_path()


def _audio_sock_path() -> str:
    return audio_socket_path()


def _probe_sock_path() -> str:
    return probe_socket_path()


def _env_bool(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in ("1", "true", "yes", "on")


# -- selection policy --------------------------------------------------------- #
def _prefer_official(capability: Capability) -> bool:
    """Select official only by explicit policy or a verified handshake.

    A Unix-socket inode is useful discovery evidence but remains ``UNKNOWN``;
    auto-selection must not route an application into a stale/incompatible
    protocol.  Until the native capability handshake is recovered, deployments
    with the matching patched firmware opt in explicitly.
    """
    pref = str(os.environ.get("RECAMERA_ADAPTER_PREFER", "auto")).strip().lower()
    if pref == "official":
        return True
    if pref == "workaround":
        return False
    return capability.status is CapabilityStatus.AVAILABLE


def _prefer_official_frame(capability: Capability) -> bool:
    """Resolve the frame-only policy before consulting the global policy.

    ``RECAMERA_FRAME_SOURCE`` is deliberately separate from
    ``RECAMERA_ADAPTER_PREFER``.  A managed camera application needs the native
    frame geometry without also opting its result sink into device OSD (or
    changing its audio/control adapters).  Unset/``auto`` preserves the legacy
    selection rule; explicit ``official``/``workaround`` affects only this
    factory.  Treat typos as configuration errors so a managed launch cannot
    silently fall back to the 640x480 RTSP substream.
    """
    raw = os.environ.get("RECAMERA_FRAME_SOURCE")
    if raw is None:
        return _prefer_official(capability)
    pref = str(raw).strip().lower()
    if pref == "official":
        return True
    if pref == "workaround":
        return False
    if pref == "auto":
        return _prefer_official(capability)
    raise ConfigurationError(
        "RECAMERA_FRAME_SOURCE must be 'official', 'workaround', or 'auto' "
        f"(got {raw!r})",
        operation="frame.select",
        details={
            "environment": "RECAMERA_FRAME_SOURCE",
            "value": str(raw),
            "allowed": ["official", "workaround", "auto"],
        },
    )


# -- factories ---------------------------------------------------------------- #
def select_frame_source(url: str | None = None, prefer: str = "ffmpeg", **kw):
    """Pick a FrameSource implementation.

    `prefer` selects the *workaround backend* ("ffmpeg" streaming | "snapshot"
    low-fps fallback). An explicit ``RECAMERA_FRAME_SOURCE=official`` is a
    launch-time integrity contract and therefore supersedes both values.  With
    the dedicated policy unset/``auto``, an explicit ``prefer="snapshot"``
    remains the caller's debug fallback as before.
    """
    caps = capabilities()
    use_official = _prefer_official_frame(caps.get("frame"))
    if url is None:
        from .frame_source import DEFAULT_SUB_STREAM
        url = DEFAULT_SUB_STREAM
    dedicated_official = str(os.environ.get(
        "RECAMERA_FRAME_SOURCE", "")).strip().lower() == "official"
    if use_official and (prefer != "snapshot" or dedicated_official):
        from .official import OfficialFrameSource
        return OfficialFrameSource(url=url, sock=_frame_sock_path(), **kw)
    from .frame_source import FfmpegRtspSource, SnapshotSource
    if prefer == "snapshot":
        return SnapshotSource(url=url, **kw)
    return FfmpegRtspSource(url=url, **kw)


def _result_osd_opt_in() -> bool:
    """Whether to burn AI results INTO the RTSP码流 via the official OSD ingress.

    ★S1 decision: AI results are SOFTWARE-overlaid by DEFAULT.★ Unlike
    select_frame_source (which auto-switches to the official zero-copy broker the
    moment `caps.frame_broker` probes present), the result sink does NOT auto-
    switch to OSD burn-in just because `result-in.sock` exists. The default is
    always the software overlay (`WsResultSink` on :8124), even when the official
    result-ingress socket is present.

    Burning results into the encoded RTSP stream (`OsdInjectResultSink`) is an
    EXPLICIT opt-in, reserved for the "must be baked into RTSP" scenario (线A's
    hardware-OSD switch will drive this later). Opt-in channels:
      * RECAMERA_ADAPTER_PREFER = official  -- global manifest-style override
        (docs/guide/adapter-bootstrap.md §3 `prefer: official`).
      * RECAMERA_RESULT_OSD    = 1/true/... -- dedicated result-sink opt-in, so
        one app can burn into码流 without flipping every other adapter to official.
    RECAMERA_ADAPTER_PREFER=workaround still force-selects software. Any other
    value ("auto" / unset) means software overlay -- socket presence is ignored.
    """
    pref = str(os.environ.get("RECAMERA_ADAPTER_PREFER", "auto")).strip().lower()
    if pref == "official":
        return True
    if pref == "workaround":
        return False
    return _env_bool("RECAMERA_RESULT_OSD")  # auto -> software unless opted in


def select_result_sink(kind: str = "ws", **kw):
    """Pick a ResultSink implementation.

    `kind` = "ws" (broadcast overlay, DEFAULT) | "osd" (force OSD burn-in) |
    "stdout" (debug).

    Default (S1): the SOFTWARE overlay (`WsResultSink` on :8124). The official
    OSD ingress is NOT auto-selected on socket presence -- it is explicit opt-in
    only (see `_result_osd_opt_in`, or pass kind="osd"). The "stdout" debug sink
    is always honoured verbatim.
    """
    if kind != "stdout" and (kind == "osd" or _result_osd_opt_in()):
        from .official import OsdInjectResultSink
        kw.setdefault("sock", _result_sock_path())
        return OsdInjectResultSink(**kw)
    from .result_sink import GatewayResultSink, StdoutSink, WsResultSink
    if kind == "stdout":
        return StdoutSink(**kw)
    gateway_sock = os.environ.get("RECAMERA_RESULT_GATEWAY_SOCK", "").strip()
    if gateway_sock:
        # Managed launch: fail closed inside GatewayResultSink if the UDS or
        # instance identity is unavailable.  Never fall back to a child-owned
        # :8124 listener, which would reintroduce the multi-app port race.
        kw["sock"] = gateway_sock
        return GatewayResultSink(**kw)
    return WsResultSink(**kw)


def select_audio_source(prefer: str = "ai_asr", **kw):
    """Pick an AudioSource implementation (16k mono PCM).

    Selection order:
      1. Official R8 clean-PCM broker (`/run/recamera/audio.sock`) when probed
         present (or forced via RECAMERA_ADAPTER_PREFER=official). This is the
         future VQE-clean broker; still a stub on shipping firmware.
      2. `AiAsrAudioSource` (DEFAULT, `prefer="ai_asr"`) -- the OFFICIAL audio
         path: capture the firmware's reserved ALSA `ai_asr` dsnoop PCM via
         arecord (+ ffmpeg loudnorm/ch0-select). Shares the mic with rkipc (no
         takeover, no EBUSY, video + RTSP audio untouched). Needs root or the
         `audio` group -- satisfied because appmgr runs extensions as root. This
         is cleaner than the RTSP workaround (no rkipc encode/transcode hop) and
         is therefore the default. NOTE: raw mic, no VQE -- app does its own
         denoise/AEC (see docs/guide/audio-pcm.md; AEC reference is in ch2/ch3).
      3. `RtspAudioSource` (fallback / A-B comparison, `prefer="rtsp"`) --
         demuxes the audio track from rkipc's combined RTSP stream via ffmpeg.
         Also non-invasive; kept as a fallback for hosts where `/dev/snd` access
         is unavailable (e.g. running as the SSH `admin` user, not root).
      4. `AlsaTakeoverSource` (degraded fallback, `prefer="alsa"`) -- direct
         `hw:0,0` takeover via arecord. On shipping firmware the raw mic is held
         exclusively by rkipc, so its `open()` raises `AudioDeviceBusy` unless
         the caller opts into freeing it (which blips video). Prefer ai_asr,
         which shares the mic instead of fighting for it.

    `prefer` selects the workaround backend when no official broker is present:
    "ai_asr" (default) | "rtsp" | "alsa". The official broker, when present,
    supersedes all three.
    """
    caps = capabilities()
    if _prefer_official(caps.get("audio")):
        from .official import OfficialPcmSource
        return OfficialPcmSource(sock=_audio_sock_path(), **kw)
    if prefer == "rtsp":
        from .audio_source import RtspAudioSource
        return RtspAudioSource(**kw)
    if prefer == "alsa":
        from .audio_source import AlsaTakeoverSource
        return AlsaTakeoverSource(**kw)
    from .audio_source import AiAsrAudioSource
    return AiAsrAudioSource(**kw)


def select_control(**kw):
    """Pick a ControlPlane implementation.

    `RECAMERA_CONTROL_API=1` is an explicit opt-in for the migration stub and
    therefore force-selects `OfficialControl` even though the capability probe
    can only report ``UNKNOWN`` today. Otherwise the official versioned control
    API is selected only by a verified handshake or the global
    `RECAMERA_ADAPTER_PREFER=official` override. On today's firmware this
    usually falls back to `CgiControl`, the workaround plane that drives the
    device's existing `entry.cgi` endpoints (localhost, no JWT) for
    set_inference and proxies a FrameSource frame for snapshot.
    """
    caps = capabilities()
    if _env_bool("RECAMERA_CONTROL_API") or _prefer_official(caps.get("control")):
        from .official import OfficialControl
        return OfficialControl(**kw)
    from .cgi_control import CgiControl
    return CgiControl(**kw)


def select_probe(stages, **kw):
    """Pick a ProbeSource implementation (spec §4 observability tap).

    Unlike select_frame_source / select_result_sink / select_audio_source /
    select_control -- each of which chooses between an `Official*` backend and a
    reverse-engineered workaround -- probe has NO workaround: the SDK's
    `recamera_ext.ProbeSource` (probe@1, the ABI v1 baseline) is the only
    implementation, present on any extension-API firmware. So this factory does
    not branch on `_prefer_official`; it always returns the SDK ProbeSource.
    `caps.probe` (probed by capabilities()) is informational only -- appmgr can
    read it to log or skip when the socket is absent, but it does not change the
    selection.

    `stages` is the non-empty list of stage ids to subscribe (e.g. ["metrics"],
    ["npu"]); extra kwargs (`sample_every`, `timeout_ms`, `lib_path`) are
    forwarded to ProbeSource verbatim. Imported lazily so this module stays
    importable off-device (recamera_ext + librecamera_ext.so.1 only exist on the
    device with the extension-API firmware).
    """
    from recamera_ext import ProbeSource
    return ProbeSource(stages=stages, **kw)

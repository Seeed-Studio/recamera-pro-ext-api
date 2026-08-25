"""
Capability registry for the L0 adapter layer (docs/guide/kit-design.md §L0,
docs/guide/adapter-bootstrap.md §3).

At startup the kit probes whether the *official* firmware endpoints exist, and
every adapter factory picks its implementation from the resulting capability
set:

    FrameSource  = caps.frame_broker   ? OfficialFrameSource  : FfmpegRtspSource
    ResultSink   = WsResultSink (DEFAULT, software overlay :8124);
                   OsdInjectResultSink (burn into码流) is EXPLICIT opt-in only
                   (RECAMERA_ADAPTER_PREFER=official | RECAMERA_RESULT_OSD=1 |
                   kind="osd") -- NOT auto-selected on result-in.sock presence.
    AudioSource  = caps.audio_broker   ? OfficialPcmSource     : (workaround TBD)
    ControlPlane = caps.control_api    ? OfficialControl       : CgiControl
    ProbeSource  = ProbeSource (SDK)   -- v1 baseline (probe@1), no workaround alt

On today's firmware (6.1.157) none of the official endpoints exist, so every
factory selects the existing verified workaround and behaviour is byte-for-byte
unchanged. When a firmware upgrade adds an official endpoint, the next probe
hits, the factory returns the `Official*` implementation, and **no application
code and no repackaging is required** -- exactly the "smooth migration" contract
in docs/guide/adapter-bootstrap.md §3.

Overrides (both for real deployments and for testing the switch logic)
----------------------------------------------------------------------
* `RECAMERA_FRAME_SOCK` / `RECAMERA_AUDIO_SOCK` -- point the probe at a custom
  socket path (lets a test create a fake socket and prove the switch).
* `RECAMERA_RESULT_INGRESS` / `RECAMERA_CONTROL_API` -- "1"/"0" to force those
  (currently un-probeable) capabilities on/off.
* `RECAMERA_ADAPTER_PREFER` = `auto` (default) | `official` | `workaround`
  -- a global manifest-style override of the per-capability auto selection
  (docs/guide/adapter-bootstrap.md §3: "可留 manifest 里 prefer: official|workaround 供覆盖").
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


# -- capability probing ------------------------------------------------------- #
# Canonical extension-API socket paths (docs/api/spec.md §1: all live
# under /run/recamera/). These are the *real* names the shipped librecamera_ext
# uses -- singular `frame.sock` and `result-in.sock`.
def _frame_sock_path() -> str:
    return os.environ.get("RECAMERA_FRAME_SOCK", "/run/recamera/frame.sock")


def _result_sock_path() -> str:
    return os.environ.get("RECAMERA_RESULT_SOCK", "/run/recamera/result-in.sock")


def _audio_sock_path() -> str:
    return os.environ.get("RECAMERA_AUDIO_SOCK", "/run/recamera/audio.sock")


def _probe_sock_path() -> str:
    return os.environ.get("RECAMERA_PROBE_SOCK", "/run/recamera/probe.sock")


def _env_bool(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Capabilities:
    """Result of one capability probe (mirrors docs/guide/adapter-bootstrap.md §3 `caps`)."""
    frame_broker: bool = False
    result_ingress: bool = False
    audio_broker: bool = False
    control_api: bool = False
    # probe@1 is the ABI v1 baseline observability tap (spec §4). Unlike the
    # capabilities above it has no reverse-engineered workaround -- the SDK's
    # ProbeSource is the only implementation. The probe here is informational
    # (lets appmgr log/skip when the socket is absent); selection never branches.
    probe: bool = False


def probe_capabilities() -> Capabilities:
    """Probe the official firmware endpoints. Cheap + side-effect free."""
    return Capabilities(
        frame_broker=os.path.exists(_frame_sock_path()),
        # Both frame + result sockets are filesystem-probeable now that the real
        # paths are known. RECAMERA_RESULT_INGRESS still force-overrides (used by
        # tests, and to opt in before the socket-perms are relaxed).
        result_ingress=(os.path.exists(_result_sock_path())
                        or _env_bool("RECAMERA_RESULT_INGRESS")),
        audio_broker=os.path.exists(_audio_sock_path()),
        control_api=_env_bool("RECAMERA_CONTROL_API"),
        probe=os.path.exists(_probe_sock_path()),
    )


_CACHED: Optional[Capabilities] = None


def capabilities(refresh: bool = False) -> Capabilities:
    """Return the cached capability probe (probed once per process).

    `refresh=True` re-probes -- used by tests that mutate the environment, and
    available to appmgr if it ever needs to re-evaluate after a firmware event.
    """
    global _CACHED
    if refresh or _CACHED is None:
        _CACHED = probe_capabilities()
    return _CACHED


# -- selection policy --------------------------------------------------------- #
def _prefer_official(cap_present: bool) -> bool:
    """Apply the global prefer override on top of the auto (cap-present) choice."""
    pref = str(os.environ.get("RECAMERA_ADAPTER_PREFER", "auto")).strip().lower()
    if pref == "official":
        return True
    if pref == "workaround":
        return False
    return cap_present  # auto


# -- factories ---------------------------------------------------------------- #
def select_frame_source(url: str, prefer: str = "ffmpeg", **kw):
    """Pick a FrameSource implementation.

    `prefer` selects the *workaround backend* ("ffmpeg" streaming | "snapshot"
    low-fps fallback). The official broker, when present, supersedes both --
    except when the caller explicitly asks for the "snapshot" debug fallback,
    which is honoured verbatim.
    """
    caps = capabilities()
    if prefer != "snapshot" and _prefer_official(caps.frame_broker):
        from .official import OfficialFrameSource
        return OfficialFrameSource(url=url, sock=_frame_sock_path(), **kw)
    from .frame_source import FfmpegRtspSource, SnapshotSource
    # 走到这里 = 官方 frame.sock 零拷贝通路不可用（固件扩展 API 未装/未起），
    # 应用将跑在 RTSP 回退路径上：低帧率、重解码、且受流稳定性影响。
    # 这必须在日志里大声可见，否则现场"部署成功但效果差/检不出"极难定位。
    print("[adapters] WARNING: official frame.sock unavailable -- falling back "
          f"to {'SnapshotSource' if prefer == 'snapshot' else 'FfmpegRtspSource'} "
          "(RTSP). Check /run/recamera/frame.sock and the ext-api install.",
          flush=True)
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
        return OsdInjectResultSink(**kw)
    from .result_sink import StdoutSink, WsResultSink
    if kind == "stdout":
        return StdoutSink(**kw)
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
    if _prefer_official(caps.audio_broker):
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

    The official versioned control API (`OfficialControl`) is preferred when
    probed present (or forced via RECAMERA_ADAPTER_PREFER=official). On today's
    firmware it is not present, so this falls back to `CgiControl`, the
    workaround plane that drives the device's existing `entry.cgi` endpoints
    (localhost, no JWT) for set_inference and proxies a FrameSource frame for
    snapshot.
    """
    caps = capabilities()
    if _prefer_official(caps.control_api):
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

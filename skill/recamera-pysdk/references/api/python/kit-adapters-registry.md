# kit.adapters.registry

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/adapters/registry.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/adapters/registry.py)；签名由 AST 提取，不导入硬件依赖。

按能力与显式偏好选择后端。auto 只采纳已验证能力；存在 socket 文件不等于 AVAILABLE。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

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

## kit.adapters.registry.select_frame_source

```python
def select_frame_source(url: str | None=None, prefer: str='ffmpeg', **kw)
```

Pick a FrameSource implementation.

`prefer` selects the *workaround backend* ("ffmpeg" streaming | "snapshot"
low-fps fallback). An explicit ``RECAMERA_FRAME_SOURCE=official`` is a
launch-time integrity contract and therefore supersedes both values.  With
the dedicated policy unset/``auto``, an explicit ``prefer="snapshot"``
remains the caller's debug fallback as before.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/adapters/registry.py#L138)

## kit.adapters.registry.select_result_sink

```python
def select_result_sink(kind: str='ws', **kw)
```

Pick a ResultSink implementation.

`kind` = "ws" (broadcast overlay, DEFAULT) | "osd" (force OSD burn-in) |
"stdout" (debug).

Default (S1): the SOFTWARE overlay (`WsResultSink` on :8124). The official
OSD ingress is NOT auto-selected on socket presence -- it is explicit opt-in
only (see `_result_osd_opt_in`, or pass kind="osd"). The "stdout" debug sink
is always honoured verbatim.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/adapters/registry.py#L198)

## kit.adapters.registry.select_audio_source

```python
def select_audio_source(prefer: str='ai_asr', **kw)
```

Pick an AudioSource implementation (16k mono PCM).

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

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/adapters/registry.py#L226)

## kit.adapters.registry.select_control

```python
def select_control(**kw)
```

Pick a ControlPlane implementation.

`RECAMERA_CONTROL_API=1` is an explicit opt-in for the migration stub and
therefore force-selects `OfficialControl` even though the capability probe
can only report ``UNKNOWN`` today. Otherwise the official versioned control
API is selected only by a verified handshake or the global
`RECAMERA_ADAPTER_PREFER=official` override. On today's firmware this
usually falls back to `CgiControl`, the workaround plane that drives the
device's existing `entry.cgi` endpoints (localhost, no JWT) for
set_inference and proxies a FrameSource frame for snapshot.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/adapters/registry.py#L269)

## kit.adapters.registry.select_probe

```python
def select_probe(stages, **kw)
```

Pick a ProbeSource implementation (spec §4 observability tap).

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

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/adapters/registry.py#L289)

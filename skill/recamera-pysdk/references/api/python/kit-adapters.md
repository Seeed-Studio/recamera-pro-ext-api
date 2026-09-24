# kit.adapters

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/adapters/__init__.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/adapters/__init__.py)；签名由 AST 提取，不导入硬件依赖。

适配器惰性导出；普通托管应用优先使用 App 提供的帧、模型与 emit，不能自行重建网关。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

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

## 公开导出 / 别名

| 导入名称 | 定义与完整说明 |
| --- | --- |
| `AlsaTakeoverSource` | [kit.adapters.audio_source.AlsaTakeoverSource](kit-adapters-audio_source.md) |
| `AudioDeviceBusy` | [kit.adapters.audio_source.AudioDeviceBusy](kit-adapters-audio_source.md) |
| `AudioSource` | [kit.adapters.audio_source.AudioSource](kit-adapters-audio_source.md) |
| `Capabilities` | [kit.capabilities.Capabilities](kit-capabilities.md) |
| `Capability` | [kit.capabilities.Capability](kit-capabilities.md) |
| `CapabilityStatus` | [kit.capabilities.CapabilityStatus](kit-capabilities.md) |
| `CgiControl` | [kit.adapters.cgi_control.CgiControl](kit-adapters-cgi_control.md) |
| `ControlPlane` | [kit.adapters.official.ControlPlane](kit-adapters-official.md) |
| `DEFAULT_MAIN_STREAM` | [kit.adapters.frame_source.DEFAULT_MAIN_STREAM](kit-adapters-frame_source.md) |
| `DEFAULT_SUB_STREAM` | [kit.adapters.frame_source.DEFAULT_SUB_STREAM](kit-adapters-frame_source.md) |
| `FfmpegRtspSource` | [kit.adapters.frame_source.FfmpegRtspSource](kit-adapters-frame_source.md) |
| `Frame` | [kit.frame.Frame](kit-frame.md) |
| `FrameSource` | [kit.adapters.frame_source.FrameSource](kit-adapters-frame_source.md) |
| `GatewayResultSink` | [kit.adapters.result_sink.GatewayResultSink](kit-adapters-result_sink.md) |
| `MultiSink` | [kit.adapters.result_sink.MultiSink](kit-adapters-result_sink.md) |
| `OfficialControl` | [kit.adapters.official.OfficialControl](kit-adapters-official.md) |
| `PcmFrame` | [kit.adapters.audio_source.PcmFrame](kit-adapters-audio_source.md) |
| `ResultPublisher` | [kit.adapters.result_sink.ResultPublisher](kit-adapters-result_sink.md) |
| `ResultSink` | [kit.adapters.result_sink.ResultSink](kit-adapters-result_sink.md) |
| `RtspAudioSource` | [kit.adapters.audio_source.RtspAudioSource](kit-adapters-audio_source.md) |
| `SnapshotSource` | [kit.adapters.frame_source.SnapshotSource](kit-adapters-frame_source.md) |
| `StdoutSink` | [kit.adapters.result_sink.StdoutSink](kit-adapters-result_sink.md) |
| `WavFileAudioSource` | [kit.adapters.audio_source.WavFileAudioSource](kit-adapters-audio_source.md) |
| `WsResultSink` | [kit.adapters.result_sink.WsResultSink](kit-adapters-result_sink.md) |
| `capabilities` | [kit.capabilities.capabilities](kit-capabilities.md) |
| `open_audio_source` | [kit.adapters.audio_source.open_audio_source](kit-adapters-audio_source.md) |
| `open_frame_source` | [kit.adapters.frame_source.open_frame_source](kit-adapters-frame_source.md) |
| `open_result_sink` | [kit.adapters.result_sink.open_result_sink](kit-adapters-result_sink.md) |
| `pcm_stats` | [kit.adapters.audio_source.pcm_stats](kit-adapters-audio_source.md) |
| `probe_capabilities` | [kit.capabilities.probe_capabilities](kit-capabilities.md) |
| `select_audio_source` | [kit.adapters.registry.select_audio_source](kit-adapters-registry.md) |
| `select_control` | [kit.adapters.registry.select_control](kit-adapters-registry.md) |
| `select_frame_source` | [kit.adapters.registry.select_frame_source](kit-adapters-registry.md) |
| `select_probe` | [kit.adapters.registry.select_probe](kit-adapters-registry.md) |
| `select_result_sink` | [kit.adapters.registry.select_result_sink](kit-adapters-registry.md) |

# kit.adapters.frame_source

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/adapters/frame_source.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/frame_source.py)；签名由 AST 提取，不导入硬件依赖。

取帧协议和 RTSP／快照兼容源。软件解码需要 FFmpeg，普通相机 AI 优先受管 native frame source。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

FrameSource adapter for reCamera Pro (Rockchip RV1126B).

L0 adapter layer (see docs/guide/kit-design.md §0 / docs/guide/adapter-bootstrap.md §2.1).
The application only ever sees the `FrameSource` ABC + `Frame`; the concrete
decode backend is swappable. When the official R1 frame broker
(`/run/recamera/frame.sock`, dma-buf) arrives, only a new FrameSource
implementation is added and selected by the capability registry -- no
application code changes.

Concrete implementations here
-----------------------------
* `FfmpegRtspSource` (PRIMARY, verified on device):
      Pulls the go2rtc sub stream `rtsp://admin:admin@127.0.0.1:5554/live/1`
      (640x480 H.265) through an `ffmpeg` subprocess that decodes to raw
      `rgb24` frames on stdout. ffmpeg does the H.265 decode + NV12->RGB color
      convert; Python just reads fixed-size frame chunks. Zero extra Python
      dependencies (ffmpeg + numpy already on the device).

* `SnapshotSource` (FALLBACK, low fps, simplest):
      Grabs single JPEG frames via a one-shot ffmpeg pull. Useful when a full
      streaming decoder is undesirable.

Why not "more native" MPP / V4L2 here
-------------------------------------
Probed on device (RV1126B, firmware 6.1.157):
  - No V4L2 stateful decoder node exists (all /dev/videoN are ISP/CIF/VPSS
    capture + scaler nodes), so ffmpeg `hevc_v4l2m2m` reports
    "Could not find a valid device".
  - No GStreamer `mppvideodec` plugin installed, and ffmpeg has no `hevc_rkmpp`.
  - The MPP HW decoder is only reachable via `/oem/usr/lib/librockchip_mpp.so`
    + `/dev/mpp_service` (a ctypes MppApi loop), which is a large surface area.
Measured: ffmpeg *software* HEVC decode of the 640x480 sub stream costs only
~17% of one core (of 4) -- decode is NOT the bottleneck -- so the software
ffmpeg path is the correct minimal-dependency v1. A future `MppFrameSource`
(ctypes) or `OfficialFrameSource` (dma-buf socket) can drop in behind this same
ABC when HW zero-copy is actually needed.

## 常量与类型别名

以下为该版本源码值；默认地址不等于所有固件都开放该服务。

```python
DEFAULT_SUB_STREAM = 'rtsp://admin:admin@127.0.0.1:5554/live/1'
```


```python
DEFAULT_MAIN_STREAM = 'rtsp://admin:admin@127.0.0.1:5554/live/0'
```


## kit.adapters.frame_source.FrameSource

```python
class FrameSource(ABC)
```

Abstract frame producer. Applications depend only on this.

### kit.adapters.frame_source.FrameSource.frames

```python
@abstractmethod
def frames(self) -> Iterator[Frame]
```

Yield decoded frames until the stream ends or close() is called.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/frame_source.py#L61)

### kit.adapters.frame_source.FrameSource.close

```python
def close(self) -> None
```

关闭本对象持有的连接/线程/设备等资源。具体幂等性、在途任务及失败处理见该类生命周期说明；不要在关闭后继续发送或读取。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/frame_source.py#L65)

### kit.adapters.frame_source.FrameSource.__enter__

```python
def __enter__(self)
```

进入上下文管理器，返回其受管对象；与 __exit__ 配对使用，避免异常路径遗留资源。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/frame_source.py#L68)

### kit.adapters.frame_source.FrameSource.__exit__

```python
def __exit__(self, *exc)
```

离开上下文并执行本类的清理方法；异常传播/清理失败语义见类说明，不把退出视作任务已完成。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/frame_source.py#L71)

## kit.adapters.frame_source.FfmpegRtspSource

```python
class FfmpegRtspSource(FrameSource)
```

Continuous RTSP -> rgb24 frames via an ffmpeg subprocess.

The stream resolution is auto-probed with ffprobe (falls back to
`width`/`height` if ffprobe is unavailable), so the same class works for the
sub (640x480) or main (4K) stream.

### kit.adapters.frame_source.FfmpegRtspSource.frames

```python
def frames(self) -> Iterator[Frame]
```

迭代 FFmpeg 解码出的 CPU RGB Kit Frame。流结束停止；解码、拷贝和网络缓冲会增加延迟，不是 native DMA 取帧。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/frame_source.py#L146)

### kit.adapters.frame_source.FfmpegRtspSource.close

```python
def close(self) -> None
```

关闭本对象持有的连接/线程/设备等资源。具体幂等性、在途任务及失败处理见该类生命周期说明；不要在关闭后继续发送或读取。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/frame_source.py#L164)

### kit.adapters.frame_source.FfmpegRtspSource.__enter__

```python
def __enter__(self)
```

进入上下文管理器，返回其受管对象；与 __exit__ 配对使用，避免异常路径遗留资源。

此方法定义于基类 `FrameSource`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/frame_source.py#L68)

### kit.adapters.frame_source.FfmpegRtspSource.__exit__

```python
def __exit__(self, *exc)
```

离开上下文并执行本类的清理方法；异常传播/清理失败语义见类说明，不把退出视作任务已完成。

此方法定义于基类 `FrameSource`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/frame_source.py#L71)

### kit.adapters.frame_source.FfmpegRtspSource.__init__

```python
def __init__(self, url: str=DEFAULT_SUB_STREAM, width: Optional[int]=None, height: Optional[int]=None, rtsp_transport: str='tcp', low_latency: bool=True, ffmpeg_bin: str='ffmpeg', **_ignored)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/frame_source.py#L83)

## kit.adapters.frame_source.SnapshotSource

```python
class SnapshotSource(FrameSource)
```

Low-fps fallback: repeatedly grab one decoded JPEG frame via ffmpeg.

Reuses the same RTSP stream but tears the decoder down each grab, so fps is
low; kept simple and dependency-free (PIL decodes the JPEG). Demonstrates a
second implementation behind the identical ABC.

### kit.adapters.frame_source.SnapshotSource.frames

```python
def frames(self) -> Iterator[Frame]
```

反复读取快照并产出 Kit Frame；适合兼容/诊断路径，时延和帧率受快照操作限制。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/frame_source.py#L230)

### kit.adapters.frame_source.SnapshotSource.close

```python
def close(self) -> None
```

关闭本对象持有的连接/线程/设备等资源。具体幂等性、在途任务及失败处理见该类生命周期说明；不要在关闭后继续发送或读取。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/frame_source.py#L239)

### kit.adapters.frame_source.SnapshotSource.__enter__

```python
def __enter__(self)
```

进入上下文管理器，返回其受管对象；与 __exit__ 配对使用，避免异常路径遗留资源。

此方法定义于基类 `FrameSource`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/frame_source.py#L68)

### kit.adapters.frame_source.SnapshotSource.__exit__

```python
def __exit__(self, *exc)
```

离开上下文并执行本类的清理方法；异常传播/清理失败语义见类说明，不把退出视作任务已完成。

此方法定义于基类 `FrameSource`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/frame_source.py#L71)

### kit.adapters.frame_source.SnapshotSource.__init__

```python
def __init__(self, url: str=DEFAULT_SUB_STREAM, interval: float=0.0, rtsp_transport: str='tcp', ffmpeg_bin: str='ffmpeg', **_ignored)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/frame_source.py#L186)

## kit.adapters.frame_source.open_frame_source

```python
def open_frame_source(url: str=DEFAULT_SUB_STREAM, prefer: str='ffmpeg', **kw) -> FrameSource
```

Factory. `prefer` = "ffmpeg" (streaming) | "snapshot" (fallback).

Delegates to the capability registry, which probes for the official R1
frame broker (`/run/recamera/frame.sock`) and returns an
`OfficialFrameSource` when present. On today's firmware the socket does not
exist, so the registry falls back to the workaround backend selected by
`prefer` and behaviour is unchanged.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/frame_source.py#L243)

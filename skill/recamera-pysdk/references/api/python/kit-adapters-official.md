# kit.adapters.official

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/adapters/official.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/adapters/official.py)；签名由 AST 提取，不导入硬件依赖。

Native SDK 适配层。OfficialFrameSource/OfficialResultSink 已实现；OfficialPcmSource、OfficialControl 是未实现占位，不能据此宣称设备有统一音频/control socket API。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

Official extension-API adapters for reCamera Pro (Rockchip RV1126B).

*** This module is the production-grade reference for wiring a self-hosted
    Python app onto the official reCamera Pro extension API. ***

It is the L0 "official" backend described in docs/guide/adapter-bootstrap.md §2 and
docs/guide/kit-design.md §L0. Where the workaround backends decode the go2rtc RTSP
sub-stream (FfmpegRtspSource) and publish results on our own WebSocket
(WsResultSink), these adapters use the shipped SDK `librecamera_ext`:

  * OfficialFrameSource  -- zero-copy frames from the M2 frame proxy
                            (`/run/recamera/frame.sock`, NV12 dma-buf), via the
                            SDK's `recamera_ext.FrameSource`.
  * OfficialResultSink   -- inject results into the M1 result sink
                            (`/run/recamera/result-in.sock`), via the SDK's
                            `recamera_ext.ResultSink`. Results then flow through
                            rkipc's *own* pipeline -> OSD burn-in + recording +
                            push -- which the WebSocket workaround could never do.

Both classes implement the exact kit ABCs (`FrameSource` / `ResultSink`) with
the same `Frame` contract as the workaround, so the capability registry
(registry.py) swaps them in with ZERO application changes (docs/guide/adapter-bootstrap.md §3:
"registry 一切换，9 个应用一行不改").

Interface source of truth (verified against the authoritative SDK, not guessed)
-------------------------------------------------------------------------------
* SDK Python:  sdk/python/recamera_ext/__init__.py  (authoritative; the old
               recamera_rk/m2_scratch/sdk_work copy is DEPRECATED). Provides:
                 FrameSource(config, timeout_ms, lib_path) iterable
                     -> Frame(.array / .to_bgr() / .pts_us / .planes / .fd)
                 ResultSink(source_id) with the FULL v1 result API:
                     .send_detections(pts_us, boxes)
                     .send_classification(pts_us, items)
                     .send_segmentation(pts_us, items)
                     .send_tracking(pts_us, items)
                     .send_keypoints(pts_us, instances)
* SDK C ABI:   sdk/include/recamera_ext.h -- rc_ext_frame_* (96-byte header +
               SCM_RIGHTS dma-buf fd) and rc_ext_result_send_*.
* API spec:    docs/api/spec.md §2/§3, docs/guide/README.md §3/§4.

RESULT ROUTING (which app output -> which SDK channel; see OfficialResultSink)
-----------------------------------------------------------------------------
    detection boxes  (yolo-detector, ppocr text, qrcode)  -> send_detections
    pose keypoints   (fall-detection, fitness, facemesh)  -> send_keypoints
    face/emotion     (face-analysis)                       -> send_classification
    tracked objects  (retail-vision)                       -> send_tracking
    segmentation masks (none shipped yet; mapping ready)   -> send_segmentation

Contract references
-------------------
* OfficialFrameSource  -> API spec §2 (M2 frame proxy), docs/guide/adapter-bootstrap.md §2.1
* OfficialResultSink    -> API spec §3 (M1 result injection), docs/guide/adapter-bootstrap.md §2.2
* OfficialPcmSource     -> docs/guide/adapter-bootstrap.md §2.3 (R8 clean 16k PCM broker; stub)
* OfficialControl       -> docs/guide/adapter-bootstrap.md §2.4 (R4 versioned control API; stub)

## 常量与类型别名

以下为该版本源码值；默认地址不等于所有固件都开放该服务。

```python
OFFICIAL_FRAME_SOCK = '/run/recamera/frame.sock'
```


```python
OFFICIAL_RESULT_SOCK = '/run/recamera/result-in.sock'
```


```python
OFFICIAL_AUDIO_SOCK = '/run/recamera/audio.sock'
```


```python
OsdInjectResultSink = OfficialResultSink
```


## kit.adapters.official.OfficialFrameSource

```python
class OfficialFrameSource(FrameSource)
```

Zero-copy frame source over the M2 frame proxy (`/run/recamera/frame.sock`).

Wraps the SDK's `recamera_ext.FrameSource`, which hands back borrowed NV12
dma-buf frames. For each frame we produce a standalone RGB ``Frame``.  The
default is full-resolution (the historical contract); an explicit
``direct_preprocess`` opt-in instead performs model-aspect NV12 resize and
RGB conversion in RGA, then fills only the small square border in Python.
In that mode ``Frame.w``/``h`` remain the original camera geometry and
``Frame.model_info`` carries the LetterboxInfo-compatible mapping.

Design decisions a vendor copying this MUST understand
------------------------------------------------------
1. FULL-RESOLUTION BY DEFAULT; MODEL-SIZED ON EXPLICIT OPT-IN.
   Existing apps and callers continue to receive a full-resolution RGB
   frame.  The direct path is selected only for apps that never inspect
   source pixels.  Original dimensions are retained for result routing and
   post-processing receives the exact letterbox transform.

2. PREPROCESS PATH: RGA (fast) with an OpenCV FALLBACK -- ONE switch point.
   NV12->RGB is the per-frame hot spot. On the RV1126B the RGA 2D engine
   does it in hardware, reading the dma-buf fd directly (near-zero CPU). If
   librga is missing / unusable, we fall back to the SDK's `frame.to_bgr()`
   (cv2 NV12->BGR) + a numpy channel flip. The selection is latched on the
   first frame (see `_convert`) so the branch is decided once, not per frame.

3. dma-buf RELEASE. The SDK's Frame is *borrowed*: valid only for the
   current loop step and released when the loop advances. Every conversion
   returns a COPY (RGA writes a fresh RGB buffer; the OpenCV path copies via
   to_bgr), so `Frame.data` is safe to hold after release. We never keep a
   zero-copy view alive across iterations.

   Kit's additional ``deferred_preprocess`` opt-in may defer conversion
   until model invocation and write directly into private RKNN DMA input.
   Explicit .data access still materializes an independent RGB copy;
   unmaterialized borrowed frames must be copied within their iteration.

4. PTS ALIGNMENT. `frame.pts_us` (CLOCK_MONOTONIC microseconds, the VI PTS)
   is carried as `Frame.pts = pts_us / 1e6` seconds. OfficialResultSink
   converts it back with `round(pts * 1e6)` -- an exact integer round-trip
   -- so injected results align to the frame the OSD burns onto.

Signature mirrors `FfmpegRtspSource.__init__` (accepts `url` + misc kw and
ignores them) so the registry constructs it identically. Extra optional
knobs (`width`/`height`/`fps_divisor`/`input_size`/`direct_preprocess`/
`prefer_rga`/`lib_path`) are honoured when supplied but never required.

继承接口：[kit.adapters.frame_source.FrameSource](kit-adapters-frame_source.md)。

### kit.adapters.official.OfficialFrameSource.__init__

```python
def __init__(self, url: Optional[str]=None, sock: str=OFFICIAL_FRAME_SOCK, width: int=0, height: int=0, fps_divisor: int=0, input_size: int=0, direct_preprocess: bool=False, hw_letterbox: bool=False, hw_roi: bool=False, deferred_preprocess: bool=False, timeout_ms: int=1000, prefer_rga: bool=True, lib_path: Optional[str]=None, verbose: bool=True, **_ignored)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/adapters/official.py#L133)

### kit.adapters.official.OfficialFrameSource.frames

```python
def frames(self) -> Iterator[Frame]
```

把 native lease 适配为 Kit Frame；按 model_frame 配置走原图、hw-direct 或 hw-roi。借用数组不得跨迭代保存。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/adapters/official.py#L477)

### kit.adapters.official.OfficialFrameSource.close

```python
def close(self) -> None
```

关闭本对象持有的连接/线程/设备等资源。具体幂等性、在途任务及失败处理见该类生命周期说明；不要在关闭后继续发送或读取。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/adapters/official.py#L590)

## kit.adapters.official.OfficialResultSink

```python
class OfficialResultSink(ResultSink)
```

Inject results into the M1 result sink (`/run/recamera/result-in.sock`).

Wraps the SDK's `recamera_ext.ResultSink` and ROUTES each frame's payload to
the correct SDK channel by inspecting the result/event fields our apps
already produce -- so rkipc runs it through the SAME three-way dispatch as
its built-in inference -> **OSD burn-in + recording + push** (API spec §3),
the capability the WebSocket workaround (WsResultSink) fundamentally lacks.

Result-type routing (the mapping a vendor copies)
-------------------------------------------------
The base loop hands us `payload = {"results": [...], "events": [...]}`.
`results` carries the per-object output each app leaves after on_results();
tracking is the exception -- retail-vision keeps its tracked boxes in
`events` (kind="track"), so we look there for track_id. Per-frame routing:

  | our field(s) on the item          | app(s)                    | SDK call            |
  |-----------------------------------|---------------------------|---------------------|
  | event has `track_id` + `box`      | retail-vision             | send_tracking       |
  | result has non-empty `keypoints`  | fall / fitness / facemesh | send_keypoints      |
  | result has `mask`/`mask_bytes`    | (none shipped; ready)     | send_segmentation   |
  | result has face attrs (gender/    | face-analysis             | send_classification |
  |   age/emotion) OR label w/o box   |                           |                     |
  | result has `box` (or `quad`)      | yolo / ppocr / qrcode     | send_detections     |

Coordinate normalization (★ the critical contract ★)
----------------------------------------------------
recamera_ext.h v1.2.0: EVERY box coordinate (detection / classification ROI
/ segmentation ROI / tracking / keypoint object box) AND every keypoint
point x/y is a NORMALIZED [0,1] fraction of frame width/height. The OSD
renderer clamps to [0,1] then multiplies by frame size, so PIXEL values
collapse to an invisible 1px box. Our postprocess emits ORIGINAL full-res
PIXELS, so this sink divides x by frame width and y by frame height (clamped)
for every coordinate before sending -- see set_frame_size() + _norm_box().
Non-coordinate fields (score, class_id, label, track_id, keypoint_id,
keypoint score, segmentation mask bytes) are passed through unchanged.

Field mapping to the SDK tuples/dicts (verified vs sdk/python/recamera_ext;
all coordinates below are the NORMALIZED [0,1] values, not pixels)
--------------------------------------------------------------------------
* detections   : (x1,y1,x2,y2, score, label, class_id)
                   label = cls_name | text | label ; class_id = cls|0 ;
                   box from `box`, else derived from `quad` (qrcode/ppocr).
* keypoints    : instance dict {"points":[(x,y,score,keypoint_id)...],
                   "box":(x1,y1,x2,y2), "score", "class_id", "label"}.
                   Our keypoints are [[x,y,conf]...] (pose, 17) or [[x,y]...]
                   (facemesh landmarks, 468) -> conf defaults to 1.0 when
                   absent; keypoint_id is the list index (COCO order for pose).
* classification: (score, class_id, label[, (x1,y1,x2,y2)]). face-analysis's
                   per-face attributes become a composite label
                   ("Male,30-39,Happiness"); since SDK v1.1.0 the entry
                   carries an optional normalized ROI box (4th element) so we
                   attach the face box -> the OSD can localize the label.
* tracking     : (x1,y1,x2,y2, score, class_id, label, track_id).
* segmentation : (x1,y1,x2,y2, score, class_id, label, mask_bytes, mask_w,
                   mask_h). ROI box normalized; mask bytes untouched. No
                   shipped app emits masks yet; mapping is ready.

source_id + pts_us (vendor gotchas)
-----------------------------------
* `source_id` identifies this app's result stream to rkipc (advisory: the
  server may override it from the connection's peer-credential identity, spec
  §1.1). We default it to the app id, so multiple extension apps stay
  distinguishable in the OSD/registry.
* `pts_us` associates the result with a specific camera frame so the OSD
  overlays it on the right image. We reconstruct it from the frame's
  `pts` (seconds) that the base loop threads through `emit(payload, pts)`:
  `pts_us = round(pts * 1e6)`, the exact inverse of OfficialFrameSource's
  `pts_us / 1e6`. Passing 0 means "no frame association".

Signature mirrors `WsResultSink.__init__` (host/port/app_id) so the registry
builds it identically; `source_id`/`lib_path` are optional extras.

继承接口：[kit.adapters.result_sink.ResultSink](kit-adapters-result_sink.md)。

### kit.adapters.official.OfficialResultSink.__init__

```python
def __init__(self, host: Optional[str]=None, port: Optional[int]=None, app_id: str='app', source_id: Optional[str]=None, sock: str=OFFICIAL_RESULT_SOCK, lib_path: Optional[str]=None, verbose: bool=True, **_ignored)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/adapters/official.py#L726)

### kit.adapters.official.OfficialResultSink.set_frame_size

```python
def set_frame_size(self, w: int, h: int) -> None
```

Record the current frame's pixel size (base loop calls this per frame).

★THE FIX★ The extension-API OSD renderer treats every box/keypoint
coordinate as a NORMALIZED [0,1] fraction of frame width/height (header
recamera_ext.h v1.2.0: it clamps to [0,1] then multiplies by frame size,
so a pixel value like 240 collapses to a 1px box). Our postprocess emits
ORIGINAL full-res-frame PIXELS, so we must divide by this frame size
before sending. We store it here and apply it in the per-item mappers.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/adapters/official.py#L763)

### kit.adapters.official.OfficialResultSink.emit

```python
def emit(self, payload: dict, pts: float) -> None
```

Legacy best-effort publish; failures are logged and counted.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/adapters/official.py#L898)

### kit.adapters.official.OfficialResultSink.emit_checked

```python
def emit_checked(self, payload: dict, pts: float) -> None
```

Publish while surfacing native open/send failures to typed callers.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/adapters/official.py#L903)

### kit.adapters.official.OfficialResultSink.stats

```python
def stats(self) -> dict
```

Delivery diagnostics: this adapter's local tallies (frames handed in,
SDK send attempts, failures, oversize rejections) merged with the SDK
ResultSink's authoritative wire counters under `wire`. Local only --
a frame accepted by the socket that the server later drops is not
reflected until the server-ACK protocol lands (docs/guide/result-push.md).

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/adapters/official.py#L1007)

### kit.adapters.official.OfficialResultSink.emit_meta

```python
def emit_meta(self, payload: dict) -> None
```

按该 sink 的能力处理配置/元信息；不是一次模型推理，也不证明前端已经收到配置。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/adapters/official.py#L1025)

### kit.adapters.official.OfficialResultSink.close

```python
def close(self) -> None
```

关闭本对象持有的连接/线程/设备等资源。具体幂等性、在途任务及失败处理见该类生命周期说明；不要在关闭后继续发送或读取。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/adapters/official.py#L1035)

## kit.adapters.official.OfficialPcmSource

```python
class OfficialPcmSource(AudioSource)
```

R8 clean-PCM broker source.

Connects to `/var/run/recamera/audio.sock` and reads VQE-clean 16k mono PCM
directly -- no need to take over/close rkipc audio, AEC/denoise handled by
the firmware (docs/guide/adapter-bootstrap.md §2.3, §5). Upper STT/VAD logic is unchanged
vs the (future) `AlsaTakeoverSource` workaround.

继承接口：[kit.adapters.audio_source.AudioSource](kit-adapters-audio_source.md)。

### kit.adapters.official.OfficialPcmSource.__init__

```python
def __init__(self, sock: str=OFFICIAL_AUDIO_SOCK, **_ignored)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/adapters/official.py#L1066)

### kit.adapters.official.OfficialPcmSource.read

```python
def read(self) -> Optional[PcmFrame]
```

当前明确抛 NotImplementedError；类名存在不代表 audio.sock 已实现，请用共享 ALSA ai_asr 路径。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/adapters/official.py#L1069)

### kit.adapters.official.OfficialPcmSource.close

```python
def close(self) -> None
```

关闭本对象持有的连接/线程/设备等资源。具体幂等性、在途任务及失败处理见该类生命周期说明；不要在关闭后继续发送或读取。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/adapters/official.py#L1075)

## kit.adapters.official.ControlPlane

```python
class ControlPlane(ABC)
```

Abstract device control (R4). Applications call these abstract methods;
they never know whether the backend is the reverse-engineered CGI or the
official versioned API.

### kit.adapters.official.ControlPlane.set_inference

```python
@abstractmethod
def set_inference(self, *, enable: bool, model: Optional[str]=None, fps: Optional[int]=None) -> None
```

控制面协议占位：配置内置推理 enable/model/fps，实际后端实现决定支持程度。不是取得 NPU lease 的接口。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/adapters/official.py#L1086)

### kit.adapters.official.ControlPlane.snapshot

```python
@abstractmethod
def snapshot(self) -> bytes
```

控制面协议占位：请求一幅 JPEG；返回值由实现提供。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/adapters/official.py#L1091)

## kit.adapters.official.OfficialControl

```python
class OfficialControl(ControlPlane)
```

R4 versioned control API (docs/guide/adapter-bootstrap.md §2.4). Migration target for
the reverse-engineered `CgiControl` workaround.

### kit.adapters.official.OfficialControl.set_inference

```python
def set_inference(self, *, enable: bool, model: Optional[str]=None, fps: Optional[int]=None) -> None
```

当前抛 NotImplementedError；现有固件兼容实现是 CgiControl。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/adapters/official.py#L1102)

### kit.adapters.official.OfficialControl.snapshot

```python
def snapshot(self) -> bytes
```

当前抛 NotImplementedError；使用 CgiControl 的取帧编码实现，不能编造 native snapshot RPC。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/adapters/official.py#L1109)

### kit.adapters.official.OfficialControl.__init__

```python
def __init__(self, **_ignored)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/adapters/official.py#L1099)

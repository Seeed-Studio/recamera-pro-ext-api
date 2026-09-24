# recamera_ext

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[sdk/python/recamera_ext/__init__.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py)；签名由 AST 提取，不导入硬件依赖。

Native Python SDK：帧/probe 借用、五类结果注入、硬件 mask、exclusive NPU 租约。OsdSink/RecordSink 仅限 AppMgr。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

reCamera Pro extension SDK -- thin ctypes wrapper over librecamera_ext.so.1.

Core facilities mirror the C ABI v1 without reimplementing wire protocols
(spec §0: "Python is a thin wrapper of the C library"):

  ResultSink     -- inject inference results        (rc_ext_result_*)
  OsdSink        -- appmgr-only OSD snapshots       (rc_ext_osd_*)
  RecordSink     -- appmgr-only recording triggers  (rc_ext_record_*)
  FrameSource    -- receive zero-copy camera frames (rc_ext_frame_*)
  ProbeSource    -- observe built-in pipeline data  (rc_ext_probe_*)
  InferenceLease -- arbitrate external RKNN ownership

Result injection:

    from recamera_ext import ResultSink
    with ResultSink(source_id="face-app") as sink:
        sink.send_detections(pts_us=0, boxes=[(x1, y1, x2, y2, score, "label")])

Frame receiving (spec §2.5 "5 lines to the first frame"):

    from recamera_ext import FrameSource
    with FrameSource() as src:
        for frame in src:            # frame.array: zero-copy np.ndarray (Y plane)
            infer(frame.array)        # released automatically on the next iteration

## 公开导出 / 别名

| 导入名称 | 定义与完整说明 |
| --- | --- |
| `AcquireTimeoutError` | [recamera_ext.errors.AcquireTimeoutError](recamera_ext-errors.md) |
| `AuthError` | [recamera_ext.errors.AuthError](recamera_ext-errors.md) |
| `AuthenticationError` | [recamera_ext.errors.AuthenticationError](recamera_ext-errors.md) |
| `BackpressureError` | [recamera_ext.errors.BackpressureError](recamera_ext-errors.md) |
| `BorrowedBuffer` | [recamera_ext.buffer.BorrowedBuffer](recamera_ext-buffer.md) |
| `BufferReleasedError` | [recamera_ext.errors.BufferReleasedError](recamera_ext-errors.md) |
| `BusyError` | [recamera_ext.errors.BusyError](recamera_ext-errors.md) |
| `CapabilityUnavailableError` | [recamera_ext.errors.CapabilityUnavailableError](recamera_ext-errors.md) |
| `ErrorCode` | [recamera_ext.errors.ErrorCode](recamera_ext-errors.md) |
| `FormatError` | [recamera_ext.errors.FormatError](recamera_ext-errors.md) |
| `FrameTimeoutError` | [recamera_ext.errors.FrameTimeoutError](recamera_ext-errors.md) |
| `HandleClosedError` | [recamera_ext.errors.HandleClosedError](recamera_ext-errors.md) |
| `InternalError` | [recamera_ext.errors.InternalError](recamera_ext-errors.md) |
| `LibraryLoadError` | [recamera_ext.errors.LibraryLoadError](recamera_ext-errors.md) |
| `PlaneLayout` | [recamera_ext.buffer.PlaneLayout](recamera_ext-buffer.md) |
| `RateLimitError` | [recamera_ext.errors.RateLimitError](recamera_ext-errors.md) |
| `RecameraError` | [recamera_ext.errors.RecameraError](recamera_ext-errors.md) |
| `RecameraRuntimeError` | [recamera_ext.errors.RecameraRuntimeError](recamera_ext-errors.md) |
| `ResourceBusyError` | [recamera_ext.errors.ResourceBusyError](recamera_ext-errors.md) |
| `ResultTooLarge` | [recamera_ext.errors.ResultTooLarge](recamera_ext-errors.md) |
| `UnknownNativeError` | [recamera_ext.errors.UnknownNativeError](recamera_ext-errors.md) |
| `VersionError` | [recamera_ext.errors.VersionError](recamera_ext-errors.md) |

## 常量与类型别名

以下为该版本源码值；默认地址不等于所有固件都开放该服务。

```python
PROBE_MASK_PREPROC = 1
```


```python
PROBE_MASK_NPU = 2
```


```python
PROBE_MASK_POSTPROC = 4
```


```python
PROBE_MASK_METRICS = 8
```


```python
FOURCC_NV12 = 842094158
```


## recamera_ext.Box

```python
class Box(Structure)
```

Mirror of rc_ext_box_t.

### C 结构字段

```python
_fields_ = [('x1', c_float), ('y1', c_float), ('x2', c_float), ('y2', c_float), ('score', c_float), ('label', c_char_p), ('class_id', c_int)]
```

## recamera_ext.Classification

```python
class Classification(Structure)
```

Mirror of rc_ext_class_t.

### C 结构字段

```python
_fields_ = [('score', c_float), ('class_id', c_int), ('label', c_char_p), ('has_box', c_int), ('x1', c_float), ('y1', c_float), ('x2', c_float), ('y2', c_float)]
```

## recamera_ext.Segmentation

```python
class Segmentation(Structure)
```

Mirror of rc_ext_seg_t (ROI box + row-major mask).

### C 结构字段

```python
_fields_ = [('x1', c_float), ('y1', c_float), ('x2', c_float), ('y2', c_float), ('score', c_float), ('class_id', c_int), ('label', c_char_p), ('mask', c_char_p), ('mask_w', c_int), ('mask_h', c_int)]
```

## recamera_ext.Tracking

```python
class Tracking(Structure)
```

Mirror of rc_ext_track_t.

### C 结构字段

```python
_fields_ = [('x1', c_float), ('y1', c_float), ('x2', c_float), ('y2', c_float), ('score', c_float), ('class_id', c_int), ('label', c_char_p), ('track_id', c_int)]
```

## recamera_ext.Point

```python
class Point(Structure)
```

Mirror of rc_ext_point_t (a single keypoint).

### C 结构字段

```python
_fields_ = [('x', c_float), ('y', c_float), ('score', c_float), ('keypoint_id', c_int)]
```

## recamera_ext.KeypointInstance

```python
class KeypointInstance(Structure)
```

Mirror of rc_ext_kpinstance_t (one detected object + its keypoints).

### C 结构字段

```python
_fields_ = [('has_box', c_int), ('x1', c_float), ('y1', c_float), ('x2', c_float), ('y2', c_float), ('score', c_float), ('class_id', c_int), ('label', c_char_p), ('points', POINTER(Point)), ('n_points', c_size_t)]
```

## recamera_ext.MaskRect

```python
class MaskRect
```

A normalized hardware privacy-mask rectangle. id selects the slot
([0,6)); x/y/w/h are [0,1] fractions of frame width/height.

### recamera_ext.MaskRect.__init__

```python
def __init__(self, id, x, y, w, h)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L433)

## recamera_ext.InferenceState

```python
class InferenceState(IntEnum)
```

Authoritative rkipc NPU-owner state from the lease broker.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
BUILTIN = 0
PREPARING_EXTERNAL = 1
EXTERNAL_ACQUIRED = 2
EXTERNAL_READY = 3
NONE = 4
FAULT = 5
```

## recamera_ext.InferenceStatus

```python
@dataclass(frozen=True)
class InferenceStatus
```

Immutable snapshot returned by :meth:`InferenceLease.status`.

``state`` is normally :class:`InferenceState`.  A newer server may add an
enum value before this Python package is upgraded; in that case the raw
integer is preserved so status inspection remains forward-compatible.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
state: InferenceState | int
lease_id: int
epoch: int
generation: int
actual_fps: int
peer_pid: int
builtin_enabled: bool
handle_present: bool
fallback_builtin: bool
builtin_state: str
source_id: str
```

## recamera_ext.FrameConfig

```python
class FrameConfig
```

Optional subscription config; omit for the NPU-matched defaults.

### recamera_ext.FrameConfig.__init__

```python
def __init__(self, width=0, height=0, fourcc=0, fps_divisor=0)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L558)

## recamera_ext.InferenceLease

```python
class InferenceLease(_Handle)
```

Own rkipc's crash-safe, connection-lifetime external NPU lease.

Construction performs the broker ``ACQUIRE`` transaction and does not
return until rkipc has drained its built-in RKNN handle.  Keep this object
alive for the complete lifetime of every external RKNN context protected by
it.  Call :meth:`ready` only after model/runtime initialization succeeds,
and call :meth:`alive` immediately before each inference admission.

The native connection is the lease.  :meth:`close`/:meth:`release` is
idempotent; process death also closes the connection and lets rkipc reclaim
ownership.  A lease inherited through ``fork()`` cannot safely be operated
by the child and is rejected locally.

Parameters:
    app_id: Non-empty diagnostic application id (UTF-8, at most 64 bytes).
    instance_id: Non-empty diagnostic instance id (same limit).  Defaults
        to the creating process id.
    timeout_ms: Built-in drain deadline.  Zero selects the server default;
        the current ABI accepts at most 30 seconds.
    fallback_builtin: Restore built-in inference when the connection is
        released or lost.
    lib_path: Optional explicit ``librecamera_ext`` path.

### recamera_ext.InferenceLease.close

```python
def close(self)
```

Release broker ownership and close the native handle once.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L991)

### recamera_ext.InferenceLease.closed

```python
@property
def closed(self)
```

Whether the owning native handle has already been closed.

此方法定义于基类 `_Handle`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L754)

### recamera_ext.InferenceLease.__enter__

```python
def __enter__(self)
```

进入上下文管理器，返回其受管对象；与 __exit__ 配对使用，避免异常路径遗留资源。

此方法定义于基类 `_Handle`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L766)

### recamera_ext.InferenceLease.__exit__

```python
def __exit__(self, exc_type, exc_value, _traceback)
```

离开上下文并执行本类的清理方法；异常传播/清理失败语义见类说明，不把退出视作任务已完成。

此方法定义于基类 `_Handle`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L769)

### recamera_ext.InferenceLease.__init__

```python
def __init__(self, app_id='python', instance_id=None, timeout_ms=0, fallback_builtin=True, lib_path=None)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L811)

### recamera_ext.InferenceLease.acquired

```python
@property
def acquired(self)
```

Whether this process still owns an open local lease handle.

This is local lifecycle state.  Use :meth:`alive` for an authoritative
non-blocking connection check.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L861)

### recamera_ext.InferenceLease.ready

```python
def ready(self)
```

Mark successful external model/runtime initialization at rkipc.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L888)

### recamera_ext.InferenceLease.set_fallback

```python
def set_fallback(self, fallback_builtin)
```

Change whether built-in inference is restored on disconnect.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L931)

### recamera_ext.InferenceLease.status

```python
def status(self)
```

Fetch an immutable authoritative broker status snapshot.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L941)

### recamera_ext.InferenceLease.alive

```python
def alive(self)
```

Return whether the live connection still fences this generation.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L971)

### recamera_ext.InferenceLease.release

```python
def release(self)
```

Compatibility alias for :meth:`close`.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L1013)

## recamera_ext.ResultSink

```python
class ResultSink(_Handle)
```

Injects detection results into rkipc via /run/recamera/result-in.sock.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
MAX_MESSAGE_BYTES = 64 * 1024
```

### recamera_ext.ResultSink.close

```python
def close(self)
```

关闭本对象持有的连接/线程/设备等资源。具体幂等性、在途任务及失败处理见该类生命周期说明；不要在关闭后继续发送或读取。

此方法定义于基类 `_Handle`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L739)

### recamera_ext.ResultSink.closed

```python
@property
def closed(self)
```

Whether the owning native handle has already been closed.

此方法定义于基类 `_Handle`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L754)

### recamera_ext.ResultSink.__enter__

```python
def __enter__(self)
```

进入上下文管理器，返回其受管对象；与 __exit__ 配对使用，避免异常路径遗留资源。

此方法定义于基类 `_Handle`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L766)

### recamera_ext.ResultSink.__exit__

```python
def __exit__(self, exc_type, exc_value, _traceback)
```

离开上下文并执行本类的清理方法；异常传播/清理失败语义见类说明，不把退出视作任务已完成。

此方法定义于基类 `_Handle`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L769)

### recamera_ext.ResultSink.__init__

```python
def __init__(self, source_id, lib_path=None)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L1165)

### recamera_ext.ResultSink.stats

```python
def stats(self)
```

Local send counters (best-effort visibility). `sent` = the C send
returned success; `oversize_rejected` = refused locally by the wire-size
guard before the C call; `send_error` = the C send returned a negative
rc. These are LOCAL only: a `sent` frame the server later drops
(rate-limit / auth / decode) is not visible until the server-ACK
protocol lands (docs/guide/result-push.md).

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L1430)

### recamera_ext.ResultSink.send_detections

```python
def send_detections(self, pts_us, boxes)
```

boxes: iterable of (x1, y1, x2, y2, score, label[, class_id]).

Coordinates are normalized [0,1] (top-left x1/y1, bottom-right x2/y2, as
a fraction of frame width/height). The OSD renderer clamps to [0,1] and
multiplies by frame size, so pixel values collapse to a 1px box -- always
send fractions, e.g. (0.05, 0.07, 0.62, 0.94, 0.92, "person").

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L1441)

### recamera_ext.ResultSink.send_classification

```python
def send_classification(self, pts_us, items)
```

items: iterable of (score, class_id, label[, box]).

The optional 4th element is a box (x1, y1, x2, y2); omit it or pass
None to leave the entry box-less (original behaviour). A box attaches
a source ROI to the entry (e.g. per-face attributes). When present, the
box coordinates are normalized [0,1] (fraction of frame width/height),
e.g. (0.30, 0.20, 0.55, 0.60).

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L1470)

### recamera_ext.ResultSink.send_segmentation

```python
def send_segmentation(self, pts_us, items)
```

items: iterable of
(x1, y1, x2, y2, score, class_id, label, mask_bytes, mask_w, mask_h).
The ROI box x1/y1/x2/y2 is normalized [0,1] (fraction of frame
width/height), e.g. (0.05, 0.07, 0.62, 0.94). mask_bytes is raw
row-major bytes (not coordinates) and may be None/empty (with
mask_w=mask_h=0).

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L1502)

### recamera_ext.ResultSink.send_tracking

```python
def send_tracking(self, pts_us, items)
```

items: iterable of (x1, y1, x2, y2, score, class_id, label, track_id).

Coordinates are normalized [0,1] (fraction of frame width/height), same
contract as send_detections, e.g. (0.05, 0.07, 0.62, 0.94, 0.92, 0,
"person", 7).

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L1548)

### recamera_ext.ResultSink.send_keypoints

```python
def send_keypoints(self, pts_us, instances)
```

instances: iterable of dicts (or tuples) describing one object each:
    {
      "points": [(x, y, score, keypoint_id), ...],   # required
      "box": (x1, y1, x2, y2),   # optional; omit -> no object box
      "score": float, "class_id": int, "label": str,  # object-level
    }
Both the point x/y and the optional object box x1/y1/x2/y2 are
normalized [0,1] (fraction of frame width/height), same contract as
send_detections. A missing "box" leaves the whole object_info group
unset on the wire.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L1576)

## recamera_ext.OsdSink

```python
class OsdSink(ResultSink)
```

Appmgr-only detection overlay sink over ``/run/recamera/osd-in.sock``.

The device authenticates the process using SO_PEERCRED, the root-owned
appmgr pidfile and ``/proc``.  This class has no source-id argument because
client-provided identity is deliberately irrelevant.  Accepted snapshots
update only OSD; they do not enter recording, notification, rules or legacy
WebSocket paths.  An empty snapshot explicitly clears the overlay.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
MAX_BOXES = 64
```

### recamera_ext.OsdSink.close

```python
def close(self)
```

关闭本对象持有的连接/线程/设备等资源。具体幂等性、在途任务及失败处理见该类生命周期说明；不要在关闭后继续发送或读取。

此方法定义于基类 `_Handle`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L739)

### recamera_ext.OsdSink.closed

```python
@property
def closed(self)
```

Whether the owning native handle has already been closed.

此方法定义于基类 `_Handle`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L754)

### recamera_ext.OsdSink.__enter__

```python
def __enter__(self)
```

进入上下文管理器，返回其受管对象；与 __exit__ 配对使用，避免异常路径遗留资源。

此方法定义于基类 `_Handle`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L766)

### recamera_ext.OsdSink.__exit__

```python
def __exit__(self, exc_type, exc_value, _traceback)
```

离开上下文并执行本类的清理方法；异常传播/清理失败语义见类说明，不把退出视作任务已完成。

此方法定义于基类 `_Handle`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L769)

### recamera_ext.OsdSink.__init__

```python
def __init__(self, lib_path=None)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L1661)

### recamera_ext.OsdSink.stats

```python
def stats(self)
```

Local send counters (best-effort visibility). `sent` = the C send
returned success; `oversize_rejected` = refused locally by the wire-size
guard before the C call; `send_error` = the C send returned a negative
rc. These are LOCAL only: a `sent` frame the server later drops
(rate-limit / auth / decode) is not visible until the server-ACK
protocol lands (docs/guide/result-push.md).

此方法定义于基类 `ResultSink`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L1430)

### recamera_ext.OsdSink.send_detections

```python
def send_detections(self, pts_us, boxes)
```

AppMgr-only：发送最多 64 个归一化检测框；空列表清屏。仅更新 OSD，不进入录像或通知路径。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L1683)

### recamera_ext.OsdSink.send_classification

```python
def send_classification(self, pts_us, items)
```

明确拒绝：OSD-only ABI 不支持 classification。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L1703)

### recamera_ext.OsdSink.send_segmentation

```python
def send_segmentation(self, pts_us, items)
```

明确拒绝：OSD-only ABI 不支持 segmentation。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L1706)

### recamera_ext.OsdSink.send_tracking

```python
def send_tracking(self, pts_us, items)
```

明确拒绝：OSD-only ABI 不支持 tracking。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L1709)

### recamera_ext.OsdSink.send_keypoints

```python
def send_keypoints(self, pts_us, instances)
```

明确拒绝：OSD-only ABI 不支持 keypoints。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L1712)

## recamera_ext.RecordSink

```python
class RecordSink(ResultSink)
```

Appmgr-only FRAME / explicit recording request sink.

One authenticated connection multiplexes managed applications. Every
ordered datagram carries the stable manifest app id and reaches only Vigil
recording rules; it is not published to OSD, notifications, or public
result streams. Native open/handshake and every send/reset have a one-second
socket-I/O timeout, so this class's serialization lock and ``close()`` are
not held indefinitely by a stalled server. Ordinary application processes
cannot open this endpoint.

### recamera_ext.RecordSink.close

```python
def close(self)
```

关闭本对象持有的连接/线程/设备等资源。具体幂等性、在途任务及失败处理见该类生命周期说明；不要在关闭后继续发送或读取。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L1945)

### recamera_ext.RecordSink.closed

```python
@property
def closed(self)
```

Whether the owning native handle has already been closed.

此方法定义于基类 `_Handle`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L754)

### recamera_ext.RecordSink.__enter__

```python
def __enter__(self)
```

进入上下文管理器，返回其受管对象；与 __exit__ 配对使用，避免异常路径遗留资源。

此方法定义于基类 `_Handle`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L766)

### recamera_ext.RecordSink.__exit__

```python
def __exit__(self, exc_type, exc_value, _traceback)
```

离开上下文并执行本类的清理方法；异常传播/清理失败语义见类说明，不把退出视作任务已完成。

此方法定义于基类 `_Handle`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L769)

### recamera_ext.RecordSink.__init__

```python
def __init__(self, lib_path=None)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L1738)

### recamera_ext.RecordSink.stats

```python
def stats(self)
```

Local send counters (best-effort visibility). `sent` = the C send
returned success; `oversize_rejected` = refused locally by the wire-size
guard before the C call; `send_error` = the C send returned a negative
rc. These are LOCAL only: a `sent` frame the server later drops
(rate-limit / auth / decode) is not visible until the server-ACK
protocol lands (docs/guide/result-push.md).

此方法定义于基类 `ResultSink`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L1430)

### recamera_ext.RecordSink.send_detections

```python
def send_detections(self, app_id, pts_us, boxes)
```

Send detection triggers for ``app_id`` using ResultSink tuples.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L1818)

### recamera_ext.RecordSink.send_classification

```python
def send_classification(self, app_id, pts_us, classes)
```

Singular-name alias for :meth:`send_classifications`.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L1900)

### recamera_ext.RecordSink.send_segmentation

```python
@staticmethod
def send_segmentation(app_id, pts_us, items)
```

明确拒绝：segmentation 不受当前录像通道支持。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L1924)

### recamera_ext.RecordSink.send_tracking

```python
def send_tracking(self, app_id, pts_us, items)
```

AppMgr-only：按 app_id 发送归一化跟踪结果到录像专用通道；不会向 UI/OSD/通知发布。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L1905)

### recamera_ext.RecordSink.send_keypoints

```python
def send_keypoints(self, app_id, pts_us, instances)
```

AppMgr-only：按 app_id 发送关键点及可选对象框到录像专用通道。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L1914)

### recamera_ext.RecordSink.send_classifications

```python
def send_classifications(self, app_id, pts_us, classes)
```

Send ``(score, label[, class_id[, box]])`` trigger tuples.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L1829)

### recamera_ext.RecordSink.send_events

```python
def send_events(self, app_id, pts_us, events)
```

Send event-kind tuples through the distinct record event channel.

Entries use the same ``(score, label[, class_id[, box]])`` shape as
:meth:`send_classifications` for caller compatibility. A non-empty call
produces ONE payload-free EVENT; labels/scores/ROI are not rule filters.
An empty call is a no-op. The caller already decided to request recording.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L1861)

### recamera_ext.RecordSink.reset

```python
def reset(self, app_id)
```

AppMgr-only：有序清除 app_id 的排队结果，返回成功才说明收到 ACK；不取消已消费事件或正在进行的录像。错误后关闭并重开 handle。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L1935)

## recamera_ext.FrameLease

```python
class FrameLease
```

An explicit lease on one borrowed camera dma-buf.

The lease is valid until any of these events occurs:

* :meth:`release` is called;
* its source advances/acquires another frame;
* its source is closed or leaves a context manager; or
* the lease is garbage-collected while still current.

``release()`` is idempotent and returns whether it performed the native
release.  Metadata such as ``seq`` and ``width`` remains readable afterwards,
but fd/mapping/array access raises :class:`BufferReleasedError`.  A NumPy view
retained before release cannot be revoked by Python; call :meth:`copy` while
the lease is alive when data must outlive this scope.

``Frame`` below is a compatibility subclass, so every historical object is
also a ``FrameLease`` without changing ``from recamera_ext import Frame``.

### recamera_ext.FrameLease.__init__

```python
def __init__(self, src, cbuf)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L1970)

### recamera_ext.FrameLease.released

```python
@property
def released(self)
```

Whether the source has returned this native frame buffer.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L2056)

### recamera_ext.FrameLease.release_reason

```python
@property
def release_reason(self)
```

Diagnostic reason for release (explicit, source close, next frame…).

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L2062)

### recamera_ext.FrameLease.fd

```python
@property
def fd(self)
```

Borrowed dma-buf fd; never close or retain it beyond this lease.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L2100)

### recamera_ext.FrameLease.plane_array

```python
def plane_array(self, i)
```

Plane ``i`` as a checked zero-copy ``(vstride, stride)`` view.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L2147)

### recamera_ext.FrameLease.array

```python
@property
def array(self)
```

Zero-copy valid Y pixels as ``(height, width)`` uint8.

The property rechecks the lease even when the view was previously cached,
so accessing ``frame.array`` after automatic/explicit release is rejected.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L2153)

### recamera_ext.FrameLease.copy

```python
def copy(self)
```

Return an owned copy of the valid Y plane that survives release.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L2173)

### recamera_ext.FrameLease.to_bgr

```python
def to_bgr(self)
```

Return an owned contiguous BGR image using OpenCV NV12 conversion.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L2178)

### recamera_ext.FrameLease.release

```python
def release(self)
```

Return this frame to its source; idempotent and exception-safe.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L2214)

### recamera_ext.FrameLease.__enter__

```python
def __enter__(self)
```

进入上下文管理器，返回其受管对象；与 __exit__ 配对使用，避免异常路径遗留资源。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L2221)

### recamera_ext.FrameLease.__exit__

```python
def __exit__(self, exc_type, exc_value, _traceback)
```

离开上下文并执行本类的清理方法；异常传播/清理失败语义见类说明，不把退出视作任务已完成。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L2225)

## recamera_ext.Frame

```python
class Frame(FrameLease)
```

Backward-compatible name for :class:`FrameLease`.

``FrameSource`` continues to yield this exact class, preserving existing
imports and ``isinstance(frame, Frame)`` checks while exposing the new lease
API through inheritance.

### recamera_ext.Frame.__init__

```python
def __init__(self, src, cbuf)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

此方法定义于基类 `FrameLease`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L1970)

### recamera_ext.Frame.released

```python
@property
def released(self)
```

Whether the source has returned this native frame buffer.

此方法定义于基类 `FrameLease`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L2056)

### recamera_ext.Frame.release_reason

```python
@property
def release_reason(self)
```

Diagnostic reason for release (explicit, source close, next frame…).

此方法定义于基类 `FrameLease`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L2062)

### recamera_ext.Frame.fd

```python
@property
def fd(self)
```

Borrowed dma-buf fd; never close or retain it beyond this lease.

此方法定义于基类 `FrameLease`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L2100)

### recamera_ext.Frame.plane_array

```python
def plane_array(self, i)
```

Plane ``i`` as a checked zero-copy ``(vstride, stride)`` view.

此方法定义于基类 `FrameLease`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L2147)

### recamera_ext.Frame.array

```python
@property
def array(self)
```

Zero-copy valid Y pixels as ``(height, width)`` uint8.

The property rechecks the lease even when the view was previously cached,
so accessing ``frame.array`` after automatic/explicit release is rejected.

此方法定义于基类 `FrameLease`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L2153)

### recamera_ext.Frame.copy

```python
def copy(self)
```

Return an owned copy of the valid Y plane that survives release.

此方法定义于基类 `FrameLease`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L2173)

### recamera_ext.Frame.to_bgr

```python
def to_bgr(self)
```

Return an owned contiguous BGR image using OpenCV NV12 conversion.

此方法定义于基类 `FrameLease`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L2178)

### recamera_ext.Frame.release

```python
def release(self)
```

Return this frame to its source; idempotent and exception-safe.

此方法定义于基类 `FrameLease`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L2214)

### recamera_ext.Frame.__enter__

```python
def __enter__(self)
```

进入上下文管理器，返回其受管对象；与 __exit__ 配对使用，避免异常路径遗留资源。

此方法定义于基类 `FrameLease`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L2221)

### recamera_ext.Frame.__exit__

```python
def __exit__(self, exc_type, exc_value, _traceback)
```

离开上下文并执行本类的清理方法；异常传播/清理失败语义见类说明，不把退出视作任务已完成。

此方法定义于基类 `FrameLease`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L2225)

## recamera_ext.FrameSource

```python
class FrameSource(_BorrowIterator)
```

Zero-copy frame receiver over /run/recamera/frame.sock (spec §2.5).

Iterating yields Frame objects; each is released automatically when the loop
advances to the next frame or the context exits.

### recamera_ext.FrameSource.close

```python
def close(self)
```

关闭本对象持有的连接/线程/设备等资源。具体幂等性、在途任务及失败处理见该类生命周期说明；不要在关闭后继续发送或读取。

此方法定义于基类 `_Handle`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L739)

### recamera_ext.FrameSource.closed

```python
@property
def closed(self)
```

Whether the owning native handle has already been closed.

此方法定义于基类 `_Handle`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L754)

### recamera_ext.FrameSource.__enter__

```python
def __enter__(self)
```

进入上下文管理器，返回其受管对象；与 __exit__ 配对使用，避免异常路径遗留资源。

此方法定义于基类 `_Handle`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L766)

### recamera_ext.FrameSource.__exit__

```python
def __exit__(self, exc_type, exc_value, _traceback)
```

离开上下文并执行本类的清理方法；异常传播/清理失败语义见类说明，不把退出视作任务已完成。

此方法定义于基类 `_Handle`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L769)

### recamera_ext.FrameSource.__iter__

```python
def __iter__(self)
```

返回本对象定义的迭代器；迭代元素与借用有效期见类说明。

此方法定义于基类 `_BorrowIterator`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L1080)

### recamera_ext.FrameSource.acquire

```python
def acquire(self, timeout_ms=None)
```

Strictly acquire one record or raise a typed exception.

Unlike iteration, this method performs exactly one native wait.  A
timeout raises :class:`AcquireTimeoutError`; a negative native result is
mapped to its typed SDK error.  Acquiring a new record first releases the
previous single outstanding borrow, matching the historical iterator
ownership model.

此方法定义于基类 `_BorrowIterator`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L1108)

### recamera_ext.FrameSource.__next__

```python
def __next__(self)
```

Compatibility iterator: retry timeouts and end on native errors.

Existing ``for frame in source`` applications historically observed a
plain end-of-stream for every negative return code.  Preserve that shape
while recording the typed cause in ``source.last_error``.  New code that
needs to distinguish protocol/backpressure/internal failures should use
:meth:`acquire`.

此方法定义于基类 `_BorrowIterator`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L1128)

### recamera_ext.FrameSource.__init__

```python
def __init__(self, config=None, timeout_ms=1000, lib_path=None)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L2269)

## recamera_ext.ProbeSample

```python
class ProbeSample
```

A borrowed probe sample. Valid only inside the current iteration step;
the underlying buffer (inline copy or memfd mmap) is released when the loop
advances or exits.

### recamera_ext.ProbeSample.__init__

```python
def __init__(self, src, csample)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L2301)

### recamera_ext.ProbeSample.released

```python
@property
def released(self)
```

此 probe sample 的借用是否已释放/失效。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L2330)

### recamera_ext.ProbeSample.release_reason

```python
@property
def release_reason(self)
```

返回借用失效原因，供生命周期诊断使用。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L2334)

### recamera_ext.ProbeSample.release

```python
def release(self)
```

Release this sample early; idempotent like :class:`FrameLease`.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L2353)

### recamera_ext.ProbeSample.__enter__

```python
def __enter__(self)
```

进入上下文管理器，返回其受管对象；与 __exit__ 配对使用，避免异常路径遗留资源。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L2360)

### recamera_ext.ProbeSample.__exit__

```python
def __exit__(self, *_exc)
```

离开上下文并执行本类的清理方法；异常传播/清理失败语义见类说明，不把退出视作任务已完成。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L2364)

### recamera_ext.ProbeSample.payload

```python
@property
def payload(self)
```

The sample bytes (a copy). Valid only for this iteration step.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L2375)

### recamera_ext.ProbeSample.array

```python
@property
def array(self)
```

Zero-copy numpy view over the payload. When meta is present the view
is typed/shaped by the TensorMeta (dtype + shape); otherwise a flat
uint8 array. The view is valid only until the loop advances.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L2383)

## recamera_ext.ProbeSource

```python
class ProbeSource(_BorrowIterator)
```

Probe observability tap over /run/recamera/probe.sock (spec §4).

Iterating yields ProbeSample objects; each is released automatically when
the loop advances to the next sample or the context exits.

    from recamera_ext import ProbeSource
    with ProbeSource(stages=["metrics"]) as probe:
        for s in probe:
            print(s.stage_id, s.seq, s.payload_len)

### recamera_ext.ProbeSource.close

```python
def close(self)
```

关闭本对象持有的连接/线程/设备等资源。具体幂等性、在途任务及失败处理见该类生命周期说明；不要在关闭后继续发送或读取。

此方法定义于基类 `_Handle`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L739)

### recamera_ext.ProbeSource.closed

```python
@property
def closed(self)
```

Whether the owning native handle has already been closed.

此方法定义于基类 `_Handle`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L754)

### recamera_ext.ProbeSource.__enter__

```python
def __enter__(self)
```

进入上下文管理器，返回其受管对象；与 __exit__ 配对使用，避免异常路径遗留资源。

此方法定义于基类 `_Handle`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L766)

### recamera_ext.ProbeSource.__exit__

```python
def __exit__(self, exc_type, exc_value, _traceback)
```

离开上下文并执行本类的清理方法；异常传播/清理失败语义见类说明，不把退出视作任务已完成。

此方法定义于基类 `_Handle`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L769)

### recamera_ext.ProbeSource.__iter__

```python
def __iter__(self)
```

返回本对象定义的迭代器；迭代元素与借用有效期见类说明。

此方法定义于基类 `_BorrowIterator`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L1080)

### recamera_ext.ProbeSource.acquire

```python
def acquire(self, timeout_ms=None)
```

Strictly acquire one record or raise a typed exception.

Unlike iteration, this method performs exactly one native wait.  A
timeout raises :class:`AcquireTimeoutError`; a negative native result is
mapped to its typed SDK error.  Acquiring a new record first releases the
previous single outstanding borrow, matching the historical iterator
ownership model.

此方法定义于基类 `_BorrowIterator`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L1108)

### recamera_ext.ProbeSource.__next__

```python
def __next__(self)
```

Compatibility iterator: retry timeouts and end on native errors.

Existing ``for frame in source`` applications historically observed a
plain end-of-stream for every negative return code.  Preserve that shape
while recording the typed cause in ``source.last_error``.  New code that
needs to distinguish protocol/backpressure/internal failures should use
:meth:`acquire`.

此方法定义于基类 `_BorrowIterator`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L1128)

### recamera_ext.ProbeSource.__init__

```python
def __init__(self, stages, sample_every=1, timeout_ms=1000, lib_path=None)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L2422)

## recamera_ext.MaskControl

```python
class MaskControl(_Handle)
```

Hardware privacy-mask control -- a thin wrapper over rc_ext_mask_* (no
logic of its own). Talks to rkipc's /var/tmp/rkipc control socket.

    from recamera_ext import MaskControl, MaskRect
    with MaskControl() as mc:
        mc.set([MaskRect(0, 0.1, 0.1, 0.3, 0.2)])   # create one block
        for x in drift():                            # incremental move, no flicker
            mc.update(MaskRect(0, x, 0.1, 0.3, 0.2))

### recamera_ext.MaskControl.close

```python
def close(self)
```

关闭本对象持有的连接/线程/设备等资源。具体幂等性、在途任务及失败处理见该类生命周期说明；不要在关闭后继续发送或读取。

此方法定义于基类 `_Handle`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L739)

### recamera_ext.MaskControl.closed

```python
@property
def closed(self)
```

Whether the owning native handle has already been closed.

此方法定义于基类 `_Handle`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L754)

### recamera_ext.MaskControl.__enter__

```python
def __enter__(self)
```

进入上下文管理器，返回其受管对象；与 __exit__ 配对使用，避免异常路径遗留资源。

此方法定义于基类 `_Handle`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L766)

### recamera_ext.MaskControl.__exit__

```python
def __exit__(self, exc_type, exc_value, _traceback)
```

离开上下文并执行本类的清理方法；异常传播/清理失败语义见类说明，不把退出视作任务已完成。

此方法定义于基类 `_Handle`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L769)

### recamera_ext.MaskControl.__init__

```python
def __init__(self, lib_path=None)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L2471)

### recamera_ext.MaskControl.set

```python
def set(self, rects)
```

Full set of active blocks (list[MaskRect], <=6). Persisted. Returns
the number of blocks actually applied.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L2491)

### recamera_ext.MaskControl.update

```python
def update(self, rect)
```

Incrementally move a single block (no flicker, not persisted). The
block must already exist. Returns 0; raises on error (caller may fall
back to set()).

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L2506)

### recamera_ext.MaskControl.clear

```python
def clear(self)
```

Clear all masks (persisted).

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L2517)

### recamera_ext.MaskControl.query

```python
def query(self)
```

Return the current active masks as list[MaskRect].

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/__init__.py#L2525)

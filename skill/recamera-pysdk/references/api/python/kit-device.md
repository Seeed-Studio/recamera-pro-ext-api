# kit.device

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/device.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/device.py)；签名由 AST 提取，不导入硬件依赖。

资源工厂与生命周期聚合；按创建逆序关闭。rknn_session 在受管 scheduled 模式选择远端服务，不接管相机媒体管线。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

Single entry point for composing reCamera AI workflow services.

## 常量与类型别名

以下为该版本源码值；默认地址不等于所有固件都开放该服务。

```python
DeviceCloseReport = CloseReport
```


## kit.device.CloseReport

```python
@dataclass(frozen=True)
class CloseReport
```

Result of closing resources owned by a :class:`Device`.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
closed: int
errors: tuple[str, ...] = ()
```

### kit.device.CloseReport.ok

```python
@property
def ok(self) -> bool
```

Whether every registered resource closed successfully.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/device.py#L24)

## kit.device.Device

```python
class Device
```

Capability-aware factory and lifetime owner for AI services.

``Device`` does not make HTTP CGI the media/data plane.  Camera frames,
result injection, RGA, and RKNN use their native adapters; the CGI backend
remains only a temporary control-plane fallback for settings.

Factories are injectable for tests and vendor backends.  Resources returned
by this object are closed in reverse creation order when the device context
exits.

### kit.device.Device.__init__

```python
def __init__(self, capability_snapshot: Capabilities, *, factories: Optional[Mapping[str, Callable[..., Any]]]=None) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/device.py#L47)

### kit.device.Device.open

```python
@classmethod
def open(cls, *, refresh: bool=True, require_verified: Iterable[str]=(), factories: Optional[Mapping[str, Callable[..., Any]]]=None) -> 'Device'
```

Discover the device and optionally require negotiated capabilities.

Current firmware discovery is filesystem/environment based, so socket
presence remains ``UNKNOWN`` rather than verified.  Passing a name in
``require_verified`` therefore fails closed until a native handshake
reports that capability.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/device.py#L65)

### kit.device.Device.frame_source

```python
def frame_source(self, **kwargs)
```

Create the selected camera frame source.

On extension firmware this is the dma-buf broker.  The existing RTSP
decoder remains a compatibility fallback and is reported through the
capability snapshot rather than disguised as zero-copy.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/device.py#L130)

### kit.device.Device.result_publisher

```python
def result_publisher(self, kind: str='ws', **kwargs)
```

Create a result publisher.

``kind="osd"`` explicitly selects native result ingress; merely seeing
its socket never changes the existing software-overlay default.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/device.py#L152)

### kit.device.Device.result_batch_publisher

```python
def result_batch_publisher(self, kind: str='ws', *, model_to_pixel=None, **kwargs)
```

Create a typed :class:`kit.ai.ResultBatchPublisher`.

The underlying legacy/native sink is owned by this ``Device`` and will
be closed with it.  The wrapper converts explicit coordinate spaces and
uses the sink's checked path, so synchronous native rejection cannot be
reported as a successful :class:`~kit.ai.PublishReport`.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/device.py#L174)

### kit.device.Device.image_ops

```python
def image_ops(self, **kwargs)
```

Open the public RGA image-operation context.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/device.py#L195)

### kit.device.Device.rknn_session

```python
def rknn_session(self, model, **kwargs)
```

Open a typed RKNN session using the managed service when selected.

appmgr injects the service endpoint only for a manifest-v2 scheduled
NPU application.  Direct/developer callers keep the existing guarded
local RKNN session unless they pass an explicit factory.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/device.py#L212)

### kit.device.Device.control

```python
def control(self, **kwargs)
```

Create the device-settings control plane.

Control is the only compatibility surface that may use entry.cgi.  It
is not used for frame transport, RGA, inference, or result delivery.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/device.py#L241)

### kit.device.Device.close

```python
def close(self) -> CloseReport
```

Close every owned resource in reverse order; never stop midway.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/device.py#L262)

### kit.device.Device.close_report

```python
@property
def close_report(self) -> Optional[CloseReport]
```

Report produced by the first close, including context-manager exit.

``__exit__`` cannot return a report because Python reserves its return
value for exception suppression.  Applications that use ``with`` can
inspect this property after the block to detect best-effort cleanup
failures.  It is ``None`` until closing begins.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/device.py#L329)

### kit.device.Device.__enter__

```python
def __enter__(self) -> 'Device'
```

进入上下文管理器，返回其受管对象；与 __exit__ 配对使用，避免异常路径遗留资源。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/device.py#L341)

### kit.device.Device.__exit__

```python
def __exit__(self, exc_type, exc, traceback) -> None
```

离开上下文并执行本类的清理方法；异常传播/清理失败语义见类说明，不把退出视作任务已完成。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/device.py#L345)

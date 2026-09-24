# kit.runtime.remote

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/runtime/remote.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/remote.py)；签名由 AST 提取，不导入硬件依赖。

scheduled 推理服务 client。支持共享 IO/兼容传输及 DMA prepared input，模型授权来自 AppMgr 分配身份和 manifest 工件。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

Client sessions for the reCamera multi-model inference daemon.

``RemoteRknnSession`` mirrors the small public surface of :class:`RknnSession`
but never imports ``rknnlite`` and never acquires the device NPU lease.  The
platform daemon owns the managed applications' RKNN contexts. Applications use
validated tensor messages or negotiated private DMA buffers over a Unix socket;
the built-in IPC model remains a separate, coordinated driver client.

## 常量与类型别名

以下为该版本源码值；默认地址不等于所有固件都开放该服务。

```python
DEFAULT_INFERENCE_SOCKET = '/run/recamera/inferenced.sock'
```


```python
INFERENCE_SOCKET_ENV = 'RECAMERA_INFERENCE_SERVICE_SOCK'
```


## kit.runtime.remote.configured_inference_socket

```python
def configured_inference_socket() -> Optional[str]
```

Return the appmgr-authorized service endpoint, if one was injected.

Managed applications receive ``RECAMERA_INFERENCE_SERVICE_SOCK``.  The
older, briefly documented variable remains a read-only compatibility alias
so developer images made during the API transition continue to run.
Merely having the default socket on disk never opts a hand-launched process
into the service.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/remote.py#L42)

## kit.runtime.remote.RemoteRknnSession

```python
class RemoteRknnSession
```

One remotely hosted model context.

Model contexts with the same digest are shared by the daemon while this
object retains a per-client alias.  Calls on one session are serialized so
request/response framing cannot interleave.  Calls from different clients
may be submitted concurrently and are fairly scheduled by the daemon.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
close = release
```

### kit.runtime.remote.RemoteRknnSession.__init__

```python
def __init__(self, model: str | ModelSpec, core_mask: Optional[int]=None, *, socket_path: Optional[str]=None, model_sha256: Optional[str]=None, verify_model: bool=False, memory_mb: int=64, priority: int=50, max_fps: float=0.0, connect_timeout: float=10.0, strict_inputs: bool=True, app_id: Optional[str]=None, instance_id: Optional[str]=None, generation: Optional[int]=None, shared_io: bool=True) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/remote.py#L141)

### kit.runtime.remote.RemoteRknnSession.released

```python
@property
def released(self) -> bool
```

返回 client session 是否已释放。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/remote.py#L358)

### kit.runtime.remote.RemoteRknnSession.stats

```python
@property
def stats(self) -> InferenceStats
```

返回 InferenceStats 快照，包含 calls/failures/total_ms/last_ms；总调用耗时包含 client/service 交互，不能当作纯 NPU 时间。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/remote.py#L362)

### kit.runtime.remote.RemoteRknnSession.io_transport

```python
@property
def io_transport(self)
```

返回协商出的共享 IO 版本或 tensor-v1 兼容传输标识。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/remote.py#L372)

### kit.runtime.remote.RemoteRknnSession.last_timings_ms

```python
@property
def last_timings_ms(self)
```

返回最近一次调用的阶段耗时字典副本，单位毫秒；字段以实际 backend 返回为准。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/remote.py#L376)

### kit.runtime.remote.RemoteRknnSession.infer

```python
def infer(self, inputs: Any, *, timeout: float=30.0) -> list[np.ndarray]
```

同步执行一次推理并返回输出数组列表；输入可为普通张量或支持的 prepared DMA 对象，timeout 为秒。保持借用输入有效直到返回。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/remote.py#L466)

### kit.runtime.remote.RemoteRknnSession.infer_prepared

```python
def infer_prepared(self, prepare, fallback, *, timeout=30.0)
```

Internal Kit hook: prepare this call's private input while locked.

``prepare(descriptor)`` synchronously writes the DMA input and returns
True, or returns False without submitting work to request the ndarray
fallback. No borrowed camera FD crosses the process boundary.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/remote.py#L469)

### kit.runtime.remote.RemoteRknnSession.release

```python
def release(self) -> None
```

释放该 client session 及本地共享 IO/连接资源，返回 None；不会赋予应用停止其他模型/服务的权限。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/remote.py#L576)

### kit.runtime.remote.RemoteRknnSession.__enter__

```python
def __enter__(self) -> 'RemoteRknnSession'
```

进入上下文管理器，返回其受管对象；与 __exit__ 配对使用，避免异常路径遗留资源。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/remote.py#L609)

### kit.runtime.remote.RemoteRknnSession.__exit__

```python
def __exit__(self, exc_type, exc, traceback) -> bool
```

离开上下文并执行本类的清理方法；异常传播/清理失败语义见类说明，不把退出视作任务已完成。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/remote.py#L618)

## kit.runtime.remote.RemoteRknnModel

```python
class RemoteRknnModel(RemoteRknnSession)
```

Compatibility wrapper matching the permissive legacy ``RknnModel``.

### kit.runtime.remote.RemoteRknnModel.__init__

```python
def __init__(self, path: str, core_mask: Optional[int]=None, **kwargs) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/remote.py#L636)

### kit.runtime.remote.RemoteRknnModel.released

```python
@property
def released(self) -> bool
```

返回 client session 是否已释放。

此方法定义于基类 `RemoteRknnSession`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/remote.py#L358)

### kit.runtime.remote.RemoteRknnModel.stats

```python
@property
def stats(self) -> InferenceStats
```

返回 InferenceStats 快照，包含 calls/failures/total_ms/last_ms；总调用耗时包含 client/service 交互，不能当作纯 NPU 时间。

此方法定义于基类 `RemoteRknnSession`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/remote.py#L362)

### kit.runtime.remote.RemoteRknnModel.io_transport

```python
@property
def io_transport(self)
```

返回协商出的共享 IO 版本或 tensor-v1 兼容传输标识。

此方法定义于基类 `RemoteRknnSession`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/remote.py#L372)

### kit.runtime.remote.RemoteRknnModel.last_timings_ms

```python
@property
def last_timings_ms(self)
```

返回最近一次调用的阶段耗时字典副本，单位毫秒；字段以实际 backend 返回为准。

此方法定义于基类 `RemoteRknnSession`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/remote.py#L376)

### kit.runtime.remote.RemoteRknnModel.infer

```python
def infer(self, inputs: Any, *, timeout: float=30.0) -> list[np.ndarray]
```

同步执行一次推理并返回输出数组列表；输入可为普通张量或支持的 prepared DMA 对象，timeout 为秒。保持借用输入有效直到返回。

此方法定义于基类 `RemoteRknnSession`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/remote.py#L466)

### kit.runtime.remote.RemoteRknnModel.infer_prepared

```python
def infer_prepared(self, prepare, fallback, *, timeout=30.0)
```

Internal Kit hook: prepare this call's private input while locked.

``prepare(descriptor)`` synchronously writes the DMA input and returns
True, or returns False without submitting work to request the ndarray
fallback. No borrowed camera FD crosses the process boundary.

此方法定义于基类 `RemoteRknnSession`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/remote.py#L469)

### kit.runtime.remote.RemoteRknnModel.release

```python
def release(self) -> None
```

释放该 client session 及本地共享 IO/连接资源，返回 None；不会赋予应用停止其他模型/服务的权限。

此方法定义于基类 `RemoteRknnSession`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/remote.py#L576)

### kit.runtime.remote.RemoteRknnModel.__enter__

```python
def __enter__(self) -> 'RemoteRknnSession'
```

进入上下文管理器，返回其受管对象；与 __exit__ 配对使用，避免异常路径遗留资源。

此方法定义于基类 `RemoteRknnSession`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/remote.py#L609)

### kit.runtime.remote.RemoteRknnModel.__exit__

```python
def __exit__(self, exc_type, exc, traceback) -> bool
```

离开上下文并执行本类的清理方法；异常传播/清理失败语义见类说明，不把退出视作任务已完成。

此方法定义于基类 `RemoteRknnSession`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/remote.py#L618)

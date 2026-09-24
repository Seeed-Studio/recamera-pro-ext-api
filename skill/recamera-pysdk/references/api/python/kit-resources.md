# kit.resources

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/resources.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/resources.py)；签名由 AST 提取，不导入硬件依赖。

资源种类、lease 协议与 legacy exclusive NPU broker 租约；scheduled App 不应自行取得 exclusive lease。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

Process-level hardware resource guards used by Python AI sessions.

The normal device backend is rkipc's versioned, connection-lifetime NPU broker.
It drains built-in inference before granting external ownership and reclaims the
lease on socket HUP.  The older advisory ``flock`` backend remains available
only when a caller explicitly supplies a custom/test lock path (or the legacy
``RECAMERA_NPU_LOCK`` override); it does not coordinate built-in inference.

## 常量与类型别名

以下为该版本源码值；默认地址不等于所有固件都开放该服务。

```python
DEFAULT_NPU_BROKER = '/run/recamera/inference-control.sock'
```


```python
DEFAULT_NPU_LOCK = '/run/recamera/npu-external.lock'
```


```python
NPU_MANAGED_MARKER = 'appmgr-v1'
```


```python
NPU_BROKER_REQUIRED_ENV = 'RECAMERA_NPU_BROKER_REQUIRED'
```


## kit.resources.ResourceKind

```python
class ResourceKind(str, Enum)
```

Hardware resources an AI workflow may declare.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
CAMERA = 'camera'
NPU = 'npu'
RGA = 'rga'
RESULT_INGRESS = 'result_ingress'
VEPU = 'vepu'
AUDIO = 'audio'
```

## kit.resources.ResourceLease

```python
@runtime_checkable
class ResourceLease(Protocol)
```

Minimal backwards-compatible lease contract consumed by sessions.

Broker-aware implementations may additionally expose ``ready()`` and
``alive()``.  :class:`RknnSession` feature-detects those hooks so existing
third-party acquire/release-only leases remain structurally compatible.

### kit.resources.ResourceLease.acquire

```python
def acquire(self, timeout: Optional[float]=None) -> 'ResourceLease'
```

Acquire ownership or raise a typed resource error.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/resources.py#L60)

### kit.resources.ResourceLease.release

```python
def release(self) -> None
```

Release ownership; the operation must be idempotent.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/resources.py#L63)

## kit.resources.ExternalNpuLease

```python
class ExternalNpuLease
```

Exclusive RKNN ownership, brokered by rkipc on normal device paths.

With no ``path`` argument (and no ``RECAMERA_NPU_LOCK`` override), acquire
uses :class:`recamera_ext.InferenceLease`.  The broker atomically drains the
built-in model and its connection fences one ownership generation.  Lease
objects in the same process/configuration share that one native connection
through reference counting.

Passing a path explicitly selects the compatibility ``flock`` backend for
host tests or legacy deployments.  That backend coordinates cooperating
Python processes only.  If the explicit path is the historical device lock,
the appmgr marker/session-leader guard is retained to prevent accidental
uncoordinated startup.

### kit.resources.ExternalNpuLease.__init__

```python
def __init__(self, path: Optional[str]=None, *, app_id: Optional[str]=None, instance_id: Optional[str]=None, fallback_builtin: bool=True, lib_path: Optional[str]=None, broker_factory: Optional[Callable[..., Any]]=None) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/resources.py#L249)

### kit.resources.ExternalNpuLease.acquired

```python
@property
def acquired(self) -> bool
```

Whether this lease instance currently holds a local reference.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/resources.py#L309)

### kit.resources.ExternalNpuLease.acquire

```python
def acquire(self, timeout: Optional[float]=None) -> 'ExternalNpuLease'
```

Acquire NPU ownership or raise a typed resource/capability error.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/resources.py#L348)

### kit.resources.ExternalNpuLease.ready

```python
def ready(self) -> None
```

Mark runtime initialization complete; a no-op for legacy locks.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/resources.py#L545)

### kit.resources.ExternalNpuLease.alive

```python
def alive(self) -> bool
```

Check that the ownership fence is still live before inference.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/resources.py#L579)

### kit.resources.ExternalNpuLease.release

```python
def release(self) -> None
```

Release this instance's reference; safe to call repeatedly.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/resources.py#L593)

### kit.resources.ExternalNpuLease.__enter__

```python
def __enter__(self) -> 'ExternalNpuLease'
```

进入上下文管理器，返回其受管对象；与 __exit__ 配对使用，避免异常路径遗留资源。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/resources.py#L670)

### kit.resources.ExternalNpuLease.__exit__

```python
def __exit__(self, exc_type, exc, traceback) -> None
```

离开上下文并执行本类的清理方法；异常传播/清理失败语义见类说明，不把退出视作任务已完成。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/resources.py#L673)

# kit.capabilities

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/capabilities.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/capabilities.py)；签名由 AST 提取，不导入硬件依赖。

能力状态与发现：AVAILABLE/UNAVAILABLE/UNKNOWN/DEGRADED。当前文件系统探测一般只能证明 UNKNOWN，不能替代协议握手。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

Conservative device-capability discovery for Python AI applications.

There is an important distinction between *seeing a socket path* and proving
that its protocol, version, permissions, and limits are usable.  The legacy
boolean attributes are retained for adapter selection, while ``details`` makes
that confidence explicit.  New applications should call :meth:`require`
before relying on a capability that needs a negotiated contract.

## kit.capabilities.CapabilityStatus

```python
class CapabilityStatus(str, Enum)
```

Confidence in a device capability.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
AVAILABLE = 'available'
UNAVAILABLE = 'unavailable'
UNKNOWN = 'unknown'
DEGRADED = 'degraded'
```

## kit.capabilities.Capability

```python
@dataclass(frozen=True)
class Capability
```

One named, versioned capability and its negotiated limits.

``source`` states how the information was obtained.  A filesystem probe is
deliberately reported as ``UNKNOWN`` when a path exists, because no
protocol handshake has occurred yet.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
name: str
status: CapabilityStatus
version: Optional[int] = None
limits: Mapping[str, Any] = field(default_factory=dict)
source: str = 'unknown'
reason: Optional[str] = None
```

### kit.capabilities.Capability.usable

```python
@property
def usable(self) -> bool
```

Whether the capability has been positively verified.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/capabilities.py#L53)

## kit.capabilities.Capabilities

```python
@dataclass(frozen=True)
class Capabilities
```

Snapshot of device capabilities.

The five booleans preserve the original adapter-registry API.  They mean
only that a legacy probe selected that backend; they do *not* imply that a
versioned handshake succeeded.  ``get``/``require`` expose the richer and
safer contract for new code.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
frame_broker: bool = False
result_ingress: bool = False
audio_broker: bool = False
control_api: bool = False
probe: bool = False
details: Mapping[str, Capability] = field(default_factory=dict)
```

### kit.capabilities.Capabilities.get

```python
def get(self, name: str) -> Capability
```

Return a capability, or an explicit ``UNKNOWN`` record.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/capabilities.py#L101)

### kit.capabilities.Capabilities.require

```python
def require(self, name: str, *, min_version: Optional[int]=None, allow_degraded: bool=False) -> Capability
```

Return a verified capability or raise :class:`CapabilityError`.

A filesystem-only ``UNKNOWN`` result is rejected.  This fail-closed
behavior prevents applications from treating a stale socket file as a
valid frame broker or NPU lease service.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/capabilities.py#L115)

## kit.capabilities.frame_socket_path

```python
def frame_socket_path() -> str
```

Frame-broker path used for diagnostics (native routing is fixed).

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/capabilities.py#L160)

## kit.capabilities.result_socket_path

```python
def result_socket_path() -> str
```

Result-ingress path used for diagnostics (native routing is fixed).

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/capabilities.py#L166)

## kit.capabilities.audio_socket_path

```python
def audio_socket_path() -> str
```

Audio-broker path used for diagnostics (native routing is fixed).

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/capabilities.py#L172)

## kit.capabilities.probe_socket_path

```python
def probe_socket_path() -> str
```

Observability path used for diagnostics (native routing is fixed).

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/capabilities.py#L178)

## kit.capabilities.probe_capabilities

```python
def probe_capabilities() -> Capabilities
```

Perform side-effect-free legacy discovery.

This function does not connect to any endpoint.  Consequently an existing
socket is recorded as ``UNKNOWN`` rather than ``AVAILABLE``.  A future
native capability getter can replace these records with negotiated version
and limit data without changing the public Python API.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/capabilities.py#L223)

## kit.capabilities.capabilities

```python
def capabilities(refresh: bool=False) -> Capabilities
```

Return a process-cached capability snapshot.

This spelling is retained inside ``kit.capabilities`` and in the legacy
adapter registry.  New application code should import
:func:`get_capabilities`; unlike the old top-level ``kit.capabilities()``
spelling, it cannot collide with Python's ``kit.capabilities`` submodule.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/capabilities.py#L277)

## kit.capabilities.get_capabilities

```python
def get_capabilities(refresh: bool=False) -> Capabilities
```

Return the cached device capability snapshot.

``get_capabilities`` is the stable package-level spelling.  A function
cannot safely share the name ``capabilities`` with its Python submodule:
importing another public class that depends on the submodule would replace
``kit.capabilities`` with that module object.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/capabilities.py#L292)

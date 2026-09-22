# kit.errors

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/errors.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py)；签名由 AST 提取，不导入硬件依赖。

Kit 结构化异常：operation、code、details 与 cause。与 recamera_ext 的 native 错误体系分别处理。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

Public exception hierarchy for the reCamera Python AI kit.

The kit is expected to run unattended on an embedded device.  A caller must be
able to distinguish a bad model/input from a transient transport failure or a
busy hardware resource without parsing human-readable strings.  Every public
exception therefore carries a stable ``code`` and an ``operation`` while its
message remains useful in logs.

The hierarchy is intentionally small.  Backend-specific details (an errno, an
HTTP status, or a native SDK return code) belong in ``details`` and should not
become new application-facing exception classes.

## kit.errors.ErrorContext

```python
@dataclass(frozen=True)
class ErrorContext
```

Machine-readable context attached to :class:`KitError`.

``details`` is copied and made read-only so code catching an exception can
safely pass the context to another thread or serialize it for diagnostics.
Values should be JSON-compatible and must not contain credentials.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
operation: str
code: str
retryable: bool
details: Mapping[str, Any]
```

## kit.errors.KitError

```python
class KitError(Exception)
```

Base class for all documented kit failures.

Parameters:
    message: Human-readable diagnosis.  It is safe to show this to an app
        developer, but it is not a stable value for program logic.
    operation: Stable name of the operation that failed, for example
        ``"model.load"`` or ``"frame.acquire"``.
    code: Stable short error code.  Subclasses provide a useful default.
    retryable: Whether retrying later *may* succeed without changing input.
    details: Optional non-secret backend diagnostics.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
default_code = 'kit_error'
```

### kit.errors.KitError.__init__

```python
def __init__(self, message: str, *, operation: str='unknown', code: Optional[str]=None, retryable: bool=False, details: Optional[Mapping[str, Any]]=None) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L121)

### kit.errors.KitError.operation

```python
@property
def operation(self) -> str
```

Stable operation name associated with the failure.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L140)

### kit.errors.KitError.code

```python
@property
def code(self) -> str
```

Stable error code suitable for application branching.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L146)

### kit.errors.KitError.retryable

```python
@property
def retryable(self) -> bool
```

Whether a later retry may succeed without changing the request.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L152)

### kit.errors.KitError.details

```python
@property
def details(self) -> Mapping[str, Any]
```

Read-only, non-secret backend details.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L158)

### kit.errors.KitError.as_dict

```python
def as_dict(self) -> dict[str, Any]
```

Return a JSON-compatible representation for status endpoints.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L163)

## kit.errors.ConfigurationError

```python
class ConfigurationError(KitError, ValueError)
```

An application manifest, option, or model declaration is invalid.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
default_code = 'invalid_configuration'
```

### kit.errors.ConfigurationError.__init__

```python
def __init__(self, message: str, *, operation: str='unknown', code: Optional[str]=None, retryable: bool=False, details: Optional[Mapping[str, Any]]=None) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L121)

### kit.errors.ConfigurationError.operation

```python
@property
def operation(self) -> str
```

Stable operation name associated with the failure.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L140)

### kit.errors.ConfigurationError.code

```python
@property
def code(self) -> str
```

Stable error code suitable for application branching.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L146)

### kit.errors.ConfigurationError.retryable

```python
@property
def retryable(self) -> bool
```

Whether a later retry may succeed without changing the request.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L152)

### kit.errors.ConfigurationError.details

```python
@property
def details(self) -> Mapping[str, Any]
```

Read-only, non-secret backend details.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L158)

### kit.errors.ConfigurationError.as_dict

```python
def as_dict(self) -> dict[str, Any]
```

Return a JSON-compatible representation for status endpoints.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L163)

## kit.errors.AdapterError

```python
class AdapterError(KitError, RuntimeError)
```

A frame, result, control, audio, or image backend failed.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
default_code = 'adapter_error'
```

### kit.errors.AdapterError.__init__

```python
def __init__(self, message: str, *, operation: str='unknown', code: Optional[str]=None, retryable: bool=False, details: Optional[Mapping[str, Any]]=None) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L121)

### kit.errors.AdapterError.operation

```python
@property
def operation(self) -> str
```

Stable operation name associated with the failure.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L140)

### kit.errors.AdapterError.code

```python
@property
def code(self) -> str
```

Stable error code suitable for application branching.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L146)

### kit.errors.AdapterError.retryable

```python
@property
def retryable(self) -> bool
```

Whether a later retry may succeed without changing the request.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L152)

### kit.errors.AdapterError.details

```python
@property
def details(self) -> Mapping[str, Any]
```

Read-only, non-secret backend details.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L158)

### kit.errors.AdapterError.as_dict

```python
def as_dict(self) -> dict[str, Any]
```

Return a JSON-compatible representation for status endpoints.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L163)

## kit.errors.ImageOperationError

```python
class ImageOperationError(AdapterError)
```

RGA or software image transformation failed.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
default_code = 'image_operation_failed'
```

### kit.errors.ImageOperationError.__init__

```python
def __init__(self, message: str, *, operation: str='unknown', code: Optional[str]=None, retryable: bool=False, details: Optional[Mapping[str, Any]]=None) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L121)

### kit.errors.ImageOperationError.operation

```python
@property
def operation(self) -> str
```

Stable operation name associated with the failure.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L140)

### kit.errors.ImageOperationError.code

```python
@property
def code(self) -> str
```

Stable error code suitable for application branching.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L146)

### kit.errors.ImageOperationError.retryable

```python
@property
def retryable(self) -> bool
```

Whether a later retry may succeed without changing the request.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L152)

### kit.errors.ImageOperationError.details

```python
@property
def details(self) -> Mapping[str, Any]
```

Read-only, non-secret backend details.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L158)

### kit.errors.ImageOperationError.as_dict

```python
def as_dict(self) -> dict[str, Any]
```

Return a JSON-compatible representation for status endpoints.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L163)

## kit.errors.TransportError

```python
class TransportError(AdapterError)
```

A local socket, HTTP compatibility endpoint, or subprocess failed.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
default_code = 'transport_error'
```

### kit.errors.TransportError.__init__

```python
def __init__(self, message: str, *, operation: str='unknown', code: Optional[str]=None, retryable: bool=False, details: Optional[Mapping[str, Any]]=None) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L121)

### kit.errors.TransportError.operation

```python
@property
def operation(self) -> str
```

Stable operation name associated with the failure.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L140)

### kit.errors.TransportError.code

```python
@property
def code(self) -> str
```

Stable error code suitable for application branching.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L146)

### kit.errors.TransportError.retryable

```python
@property
def retryable(self) -> bool
```

Whether a later retry may succeed without changing the request.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L152)

### kit.errors.TransportError.details

```python
@property
def details(self) -> Mapping[str, Any]
```

Read-only, non-secret backend details.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L158)

### kit.errors.TransportError.as_dict

```python
def as_dict(self) -> dict[str, Any]
```

Return a JSON-compatible representation for status endpoints.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L163)

## kit.errors.DeviceControlError

```python
class DeviceControlError(AdapterError)
```

A device-control request was rejected or returned invalid state.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
default_code = 'device_control_error'
```

### kit.errors.DeviceControlError.__init__

```python
def __init__(self, message: str, *, operation: str='unknown', code: Optional[str]=None, retryable: bool=False, details: Optional[Mapping[str, Any]]=None) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L121)

### kit.errors.DeviceControlError.operation

```python
@property
def operation(self) -> str
```

Stable operation name associated with the failure.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L140)

### kit.errors.DeviceControlError.code

```python
@property
def code(self) -> str
```

Stable error code suitable for application branching.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L146)

### kit.errors.DeviceControlError.retryable

```python
@property
def retryable(self) -> bool
```

Whether a later retry may succeed without changing the request.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L152)

### kit.errors.DeviceControlError.details

```python
@property
def details(self) -> Mapping[str, Any]
```

Read-only, non-secret backend details.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L158)

### kit.errors.DeviceControlError.as_dict

```python
def as_dict(self) -> dict[str, Any]
```

Return a JSON-compatible representation for status endpoints.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L163)

## kit.errors.CapabilityError

```python
class CapabilityError(KitError)
```

A required device capability is absent or has not been verified.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
default_code = 'capability_unavailable'
```

### kit.errors.CapabilityError.__init__

```python
def __init__(self, message: str, *, operation: str='unknown', code: Optional[str]=None, retryable: bool=False, details: Optional[Mapping[str, Any]]=None) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L121)

### kit.errors.CapabilityError.operation

```python
@property
def operation(self) -> str
```

Stable operation name associated with the failure.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L140)

### kit.errors.CapabilityError.code

```python
@property
def code(self) -> str
```

Stable error code suitable for application branching.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L146)

### kit.errors.CapabilityError.retryable

```python
@property
def retryable(self) -> bool
```

Whether a later retry may succeed without changing the request.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L152)

### kit.errors.CapabilityError.details

```python
@property
def details(self) -> Mapping[str, Any]
```

Read-only, non-secret backend details.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L158)

### kit.errors.CapabilityError.as_dict

```python
def as_dict(self) -> dict[str, Any]
```

Return a JSON-compatible representation for status endpoints.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L163)

## kit.errors.ResourceBusyError

```python
class ResourceBusyError(KitError, RuntimeError)
```

A hardware resource is owned by another workflow.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
default_code = 'resource_busy'
```

### kit.errors.ResourceBusyError.__init__

```python
def __init__(self, message: str, *, operation: str='unknown', code: Optional[str]=None, retryable: bool=False, details: Optional[Mapping[str, Any]]=None) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L121)

### kit.errors.ResourceBusyError.operation

```python
@property
def operation(self) -> str
```

Stable operation name associated with the failure.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L140)

### kit.errors.ResourceBusyError.code

```python
@property
def code(self) -> str
```

Stable error code suitable for application branching.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L146)

### kit.errors.ResourceBusyError.retryable

```python
@property
def retryable(self) -> bool
```

Whether a later retry may succeed without changing the request.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L152)

### kit.errors.ResourceBusyError.details

```python
@property
def details(self) -> Mapping[str, Any]
```

Read-only, non-secret backend details.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L158)

### kit.errors.ResourceBusyError.as_dict

```python
def as_dict(self) -> dict[str, Any]
```

Return a JSON-compatible representation for status endpoints.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L163)

## kit.errors.ResourceTimeoutError

```python
class ResourceTimeoutError(ResourceBusyError, TimeoutError)
```

A resource did not become safe to acquire before the deadline.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
default_code = 'resource_timeout'
```

### kit.errors.ResourceTimeoutError.__init__

```python
def __init__(self, message: str, *, operation: str='unknown', code: Optional[str]=None, retryable: bool=False, details: Optional[Mapping[str, Any]]=None) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L121)

### kit.errors.ResourceTimeoutError.operation

```python
@property
def operation(self) -> str
```

Stable operation name associated with the failure.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L140)

### kit.errors.ResourceTimeoutError.code

```python
@property
def code(self) -> str
```

Stable error code suitable for application branching.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L146)

### kit.errors.ResourceTimeoutError.retryable

```python
@property
def retryable(self) -> bool
```

Whether a later retry may succeed without changing the request.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L152)

### kit.errors.ResourceTimeoutError.details

```python
@property
def details(self) -> Mapping[str, Any]
```

Read-only, non-secret backend details.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L158)

### kit.errors.ResourceTimeoutError.as_dict

```python
def as_dict(self) -> dict[str, Any]
```

Return a JSON-compatible representation for status endpoints.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L163)

## kit.errors.InferenceError

```python
class InferenceError(KitError, RuntimeError)
```

Base class for model loading, input validation, and inference errors.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
default_code = 'inference_error'
```

### kit.errors.InferenceError.__init__

```python
def __init__(self, message: str, *, operation: str='unknown', code: Optional[str]=None, retryable: bool=False, details: Optional[Mapping[str, Any]]=None) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L121)

### kit.errors.InferenceError.operation

```python
@property
def operation(self) -> str
```

Stable operation name associated with the failure.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L140)

### kit.errors.InferenceError.code

```python
@property
def code(self) -> str
```

Stable error code suitable for application branching.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L146)

### kit.errors.InferenceError.retryable

```python
@property
def retryable(self) -> bool
```

Whether a later retry may succeed without changing the request.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L152)

### kit.errors.InferenceError.details

```python
@property
def details(self) -> Mapping[str, Any]
```

Read-only, non-secret backend details.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L158)

### kit.errors.InferenceError.as_dict

```python
def as_dict(self) -> dict[str, Any]
```

Return a JSON-compatible representation for status endpoints.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L163)

## kit.errors.ModelLoadError

```python
class ModelLoadError(InferenceError)
```

An RKNN model could not be loaded or its runtime could not initialize.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
default_code = 'model_load_failed'
```

### kit.errors.ModelLoadError.__init__

```python
def __init__(self, message: str, *, operation: str='unknown', code: Optional[str]=None, retryable: bool=False, details: Optional[Mapping[str, Any]]=None) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L121)

### kit.errors.ModelLoadError.operation

```python
@property
def operation(self) -> str
```

Stable operation name associated with the failure.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L140)

### kit.errors.ModelLoadError.code

```python
@property
def code(self) -> str
```

Stable error code suitable for application branching.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L146)

### kit.errors.ModelLoadError.retryable

```python
@property
def retryable(self) -> bool
```

Whether a later retry may succeed without changing the request.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L152)

### kit.errors.ModelLoadError.details

```python
@property
def details(self) -> Mapping[str, Any]
```

Read-only, non-secret backend details.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L158)

### kit.errors.ModelLoadError.as_dict

```python
def as_dict(self) -> dict[str, Any]
```

Return a JSON-compatible representation for status endpoints.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L163)

## kit.errors.InputValidationError

```python
class InputValidationError(InferenceError, ValueError)
```

An inference input does not match the model/session contract.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
default_code = 'invalid_inference_input'
```

### kit.errors.InputValidationError.__init__

```python
def __init__(self, message: str, *, operation: str='unknown', code: Optional[str]=None, retryable: bool=False, details: Optional[Mapping[str, Any]]=None) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L121)

### kit.errors.InputValidationError.operation

```python
@property
def operation(self) -> str
```

Stable operation name associated with the failure.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L140)

### kit.errors.InputValidationError.code

```python
@property
def code(self) -> str
```

Stable error code suitable for application branching.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L146)

### kit.errors.InputValidationError.retryable

```python
@property
def retryable(self) -> bool
```

Whether a later retry may succeed without changing the request.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L152)

### kit.errors.InputValidationError.details

```python
@property
def details(self) -> Mapping[str, Any]
```

Read-only, non-secret backend details.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L158)

### kit.errors.InputValidationError.as_dict

```python
def as_dict(self) -> dict[str, Any]
```

Return a JSON-compatible representation for status endpoints.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L163)

## kit.errors.BufferReleasedError

```python
class BufferReleasedError(KitError, RuntimeError)
```

A borrowed or explicitly released image buffer was accessed.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
default_code = 'buffer_released'
```

### kit.errors.BufferReleasedError.__init__

```python
def __init__(self, message: str, *, operation: str='unknown', code: Optional[str]=None, retryable: bool=False, details: Optional[Mapping[str, Any]]=None) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L121)

### kit.errors.BufferReleasedError.operation

```python
@property
def operation(self) -> str
```

Stable operation name associated with the failure.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L140)

### kit.errors.BufferReleasedError.code

```python
@property
def code(self) -> str
```

Stable error code suitable for application branching.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L146)

### kit.errors.BufferReleasedError.retryable

```python
@property
def retryable(self) -> bool
```

Whether a later retry may succeed without changing the request.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L152)

### kit.errors.BufferReleasedError.details

```python
@property
def details(self) -> Mapping[str, Any]
```

Read-only, non-secret backend details.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L158)

### kit.errors.BufferReleasedError.as_dict

```python
def as_dict(self) -> dict[str, Any]
```

Return a JSON-compatible representation for status endpoints.

此方法定义于基类 `KitError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L163)

## kit.errors.wrap_error

```python
def wrap_error(exc: BaseException, error_type: type[KitError], message: str, *, operation: str, code: Optional[str]=None, retryable: bool=False, details: Optional[Mapping[str, Any]]=None) -> KitError
```

Create a typed public error while retaining the backend exception.

Use it as ``raise wrap_error(...) from exc``.  Keeping ``__cause__`` gives
detailed tracebacks to developers without exposing backend-specific types
as part of the public API.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/errors.py#L248)

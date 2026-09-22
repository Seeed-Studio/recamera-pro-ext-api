# recamera_ext.errors

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[sdk/python/recamera_ext/errors.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py)；签名由 AST 提取，不导入硬件依赖。

native 返回码到 Python typed errors 的映射；保留 operation/detail，区分超时、权限、背压、格式与 capability 缺失。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

Typed exceptions for :mod:`recamera_ext`.

The native ABI reports a small, stable error-code set.  Historically the
Python wrapper formatted those values into generic ``RuntimeError`` strings,
which made callers parse text and caused the iterator API to hide transport
failures as an ordinary ``StopIteration``.  This module keeps the old Python
exception *families* (``RuntimeError``, ``OSError`` and ``ValueError``) while
adding machine-readable attributes:

``code``
    The negotiated :class:`ErrorCode`, or ``None`` for a local/unknown error.
``rc``
    The exact integer returned by the C ABI.  Open functions normally report a
    positive code through ``int *err``; operation functions return its negative.
``operation``
    The native operation that failed, such as ``"rc_ext_frame_next"``.
``detail``
    Optional local context that is safe to show in diagnostics.

Compatibility matters here.  For example, ``BusyError`` remains catchable as a
``RuntimeError``, ``LibraryLoadError`` remains catchable as an ``OSError``, and
``ResultTooLarge`` remains catchable as a ``ValueError``.

## 常量与类型别名

以下为该版本源码值；默认地址不等于所有固件都开放该服务。

```python
AuthError = AuthenticationError
```


```python
ResourceBusyError = BusyError
```


```python
FrameTimeoutError = AcquireTimeoutError
```


## recamera_ext.errors.ErrorCode

```python
class ErrorCode(IntEnum)
```

Frozen extension-API error codes (``docs/api/spec.md`` section 1.3).

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
OK = 0
EVERSION = 1
EAUTH = 2
EBUSY = 3
EFORMAT = 4
EBACKPRESSURE = 5
ERATELIMIT = 6
EINTERNAL = 7
```

## recamera_ext.errors.RecameraError

```python
class RecameraError(Exception)
```

Base class carrying structured extension-API failure information.

### recamera_ext.errors.RecameraError.__init__

```python
def __init__(self, message: Optional[str]=None, *, operation: Optional[str]=None, code: Optional[ErrorCode]=None, rc: Optional[int]=None, detail: Optional[str]=None) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L82)

### recamera_ext.errors.RecameraError.code_value

```python
@property
def code_value(self) -> Optional[int]
```

Numeric error code, including unknown native return values.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L100)

### recamera_ext.errors.RecameraError.retryable

```python
@property
def retryable(self) -> bool
```

Whether retrying later is generally reasonable.

This is a hint, not a retry policy.  Backpressure requires releasing old
leases first, and an internal error may still be permanent.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L110)

## recamera_ext.errors.RecameraRuntimeError

```python
class RecameraRuntimeError(RecameraError, RuntimeError)
```

Base for runtime failures; preserves ``except RuntimeError`` callers.

### recamera_ext.errors.RecameraRuntimeError.__init__

```python
def __init__(self, message: Optional[str]=None, *, operation: Optional[str]=None, code: Optional[ErrorCode]=None, rc: Optional[int]=None, detail: Optional[str]=None) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

此方法定义于基类 `RecameraError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L82)

### recamera_ext.errors.RecameraRuntimeError.code_value

```python
@property
def code_value(self) -> Optional[int]
```

Numeric error code, including unknown native return values.

此方法定义于基类 `RecameraError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L100)

### recamera_ext.errors.RecameraRuntimeError.retryable

```python
@property
def retryable(self) -> bool
```

Whether retrying later is generally reasonable.

This is a hint, not a retry policy.  Backpressure requires releasing old
leases first, and an internal error may still be permanent.

此方法定义于基类 `RecameraError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L110)

## recamera_ext.errors.VersionError

```python
class VersionError(RecameraRuntimeError)
```

Client/server API version ranges do not overlap.

### recamera_ext.errors.VersionError.__init__

```python
def __init__(self, message: Optional[str]=None, *, operation: Optional[str]=None, code: Optional[ErrorCode]=None, rc: Optional[int]=None, detail: Optional[str]=None) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

此方法定义于基类 `RecameraError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L82)

### recamera_ext.errors.VersionError.code_value

```python
@property
def code_value(self) -> Optional[int]
```

Numeric error code, including unknown native return values.

此方法定义于基类 `RecameraError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L100)

### recamera_ext.errors.VersionError.retryable

```python
@property
def retryable(self) -> bool
```

Whether retrying later is generally reasonable.

This is a hint, not a retry policy.  Backpressure requires releasing old
leases first, and an internal error may still be permanent.

此方法定义于基类 `RecameraError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L110)

## recamera_ext.errors.AuthenticationError

```python
class AuthenticationError(RecameraRuntimeError)
```

Peer identity, app token, or reserved source id was rejected.

### recamera_ext.errors.AuthenticationError.__init__

```python
def __init__(self, message: Optional[str]=None, *, operation: Optional[str]=None, code: Optional[ErrorCode]=None, rc: Optional[int]=None, detail: Optional[str]=None) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

此方法定义于基类 `RecameraError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L82)

### recamera_ext.errors.AuthenticationError.code_value

```python
@property
def code_value(self) -> Optional[int]
```

Numeric error code, including unknown native return values.

此方法定义于基类 `RecameraError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L100)

### recamera_ext.errors.AuthenticationError.retryable

```python
@property
def retryable(self) -> bool
```

Whether retrying later is generally reasonable.

This is a hint, not a retry policy.  Backpressure requires releasing old
leases first, and an internal error may still be permanent.

此方法定义于基类 `RecameraError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L110)

## recamera_ext.errors.BusyError

```python
class BusyError(RecameraRuntimeError)
```

An endpoint subscription or device resource is currently unavailable.

### recamera_ext.errors.BusyError.__init__

```python
def __init__(self, message: Optional[str]=None, *, operation: Optional[str]=None, code: Optional[ErrorCode]=None, rc: Optional[int]=None, detail: Optional[str]=None) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

此方法定义于基类 `RecameraError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L82)

### recamera_ext.errors.BusyError.code_value

```python
@property
def code_value(self) -> Optional[int]
```

Numeric error code, including unknown native return values.

此方法定义于基类 `RecameraError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L100)

### recamera_ext.errors.BusyError.retryable

```python
@property
def retryable(self) -> bool
```

Whether retrying later is generally reasonable.

This is a hint, not a retry policy.  Backpressure requires releasing old
leases first, and an internal error may still be permanent.

此方法定义于基类 `RecameraError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L110)

## recamera_ext.errors.FormatError

```python
class FormatError(RecameraRuntimeError)
```

A request, buffer layout, or received wire record is malformed.

### recamera_ext.errors.FormatError.__init__

```python
def __init__(self, message: Optional[str]=None, *, operation: Optional[str]=None, code: Optional[ErrorCode]=None, rc: Optional[int]=None, detail: Optional[str]=None) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

此方法定义于基类 `RecameraError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L82)

### recamera_ext.errors.FormatError.code_value

```python
@property
def code_value(self) -> Optional[int]
```

Numeric error code, including unknown native return values.

此方法定义于基类 `RecameraError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L100)

### recamera_ext.errors.FormatError.retryable

```python
@property
def retryable(self) -> bool
```

Whether retrying later is generally reasonable.

This is a hint, not a retry policy.  Backpressure requires releasing old
leases first, and an internal error may still be permanent.

此方法定义于基类 `RecameraError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L110)

## recamera_ext.errors.BackpressureError

```python
class BackpressureError(RecameraRuntimeError)
```

The server disconnected a consumer that held data for too long.

### recamera_ext.errors.BackpressureError.__init__

```python
def __init__(self, message: Optional[str]=None, *, operation: Optional[str]=None, code: Optional[ErrorCode]=None, rc: Optional[int]=None, detail: Optional[str]=None) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

此方法定义于基类 `RecameraError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L82)

### recamera_ext.errors.BackpressureError.code_value

```python
@property
def code_value(self) -> Optional[int]
```

Numeric error code, including unknown native return values.

此方法定义于基类 `RecameraError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L100)

### recamera_ext.errors.BackpressureError.retryable

```python
@property
def retryable(self) -> bool
```

Whether retrying later is generally reasonable.

This is a hint, not a retry policy.  Backpressure requires releasing old
leases first, and an internal error may still be permanent.

此方法定义于基类 `RecameraError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L110)

## recamera_ext.errors.RateLimitError

```python
class RateLimitError(RecameraRuntimeError)
```

An endpoint rejected work because its rate quota was exceeded.

### recamera_ext.errors.RateLimitError.__init__

```python
def __init__(self, message: Optional[str]=None, *, operation: Optional[str]=None, code: Optional[ErrorCode]=None, rc: Optional[int]=None, detail: Optional[str]=None) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

此方法定义于基类 `RecameraError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L82)

### recamera_ext.errors.RateLimitError.code_value

```python
@property
def code_value(self) -> Optional[int]
```

Numeric error code, including unknown native return values.

此方法定义于基类 `RecameraError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L100)

### recamera_ext.errors.RateLimitError.retryable

```python
@property
def retryable(self) -> bool
```

Whether retrying later is generally reasonable.

This is a hint, not a retry policy.  Backpressure requires releasing old
leases first, and an internal error may still be permanent.

此方法定义于基类 `RecameraError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L110)

## recamera_ext.errors.InternalError

```python
class InternalError(RecameraRuntimeError)
```

The server, transport, or native wrapper encountered an internal error.

### recamera_ext.errors.InternalError.__init__

```python
def __init__(self, message: Optional[str]=None, *, operation: Optional[str]=None, code: Optional[ErrorCode]=None, rc: Optional[int]=None, detail: Optional[str]=None) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

此方法定义于基类 `RecameraError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L82)

### recamera_ext.errors.InternalError.code_value

```python
@property
def code_value(self) -> Optional[int]
```

Numeric error code, including unknown native return values.

此方法定义于基类 `RecameraError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L100)

### recamera_ext.errors.InternalError.retryable

```python
@property
def retryable(self) -> bool
```

Whether retrying later is generally reasonable.

This is a hint, not a retry policy.  Backpressure requires releasing old
leases first, and an internal error may still be permanent.

此方法定义于基类 `RecameraError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L110)

## recamera_ext.errors.UnknownNativeError

```python
class UnknownNativeError(RecameraRuntimeError)
```

A native return code is not part of the frozen public error enum.

### recamera_ext.errors.UnknownNativeError.__init__

```python
def __init__(self, message: Optional[str]=None, *, operation: Optional[str]=None, code: Optional[ErrorCode]=None, rc: Optional[int]=None, detail: Optional[str]=None) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

此方法定义于基类 `RecameraError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L82)

### recamera_ext.errors.UnknownNativeError.code_value

```python
@property
def code_value(self) -> Optional[int]
```

Numeric error code, including unknown native return values.

此方法定义于基类 `RecameraError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L100)

### recamera_ext.errors.UnknownNativeError.retryable

```python
@property
def retryable(self) -> bool
```

Whether retrying later is generally reasonable.

This is a hint, not a retry policy.  Backpressure requires releasing old
leases first, and an internal error may still be permanent.

此方法定义于基类 `RecameraError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L110)

## recamera_ext.errors.CapabilityUnavailableError

```python
class CapabilityUnavailableError(RecameraRuntimeError)
```

The loaded ``librecamera_ext`` lacks an optional API capability.

### recamera_ext.errors.CapabilityUnavailableError.__init__

```python
def __init__(self, message: Optional[str]=None, *, operation: Optional[str]=None, code: Optional[ErrorCode]=None, rc: Optional[int]=None, detail: Optional[str]=None) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

此方法定义于基类 `RecameraError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L82)

### recamera_ext.errors.CapabilityUnavailableError.code_value

```python
@property
def code_value(self) -> Optional[int]
```

Numeric error code, including unknown native return values.

此方法定义于基类 `RecameraError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L100)

### recamera_ext.errors.CapabilityUnavailableError.retryable

```python
@property
def retryable(self) -> bool
```

Whether retrying later is generally reasonable.

This is a hint, not a retry policy.  Backpressure requires releasing old
leases first, and an internal error may still be permanent.

此方法定义于基类 `RecameraError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L110)

## recamera_ext.errors.BufferReleasedError

```python
class BufferReleasedError(RecameraRuntimeError)
```

A borrowed dma-buf or probe payload was accessed after release.

### recamera_ext.errors.BufferReleasedError.__init__

```python
def __init__(self, message: Optional[str]=None, *, operation: Optional[str]=None, code: Optional[ErrorCode]=None, rc: Optional[int]=None, detail: Optional[str]=None) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

此方法定义于基类 `RecameraError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L82)

### recamera_ext.errors.BufferReleasedError.code_value

```python
@property
def code_value(self) -> Optional[int]
```

Numeric error code, including unknown native return values.

此方法定义于基类 `RecameraError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L100)

### recamera_ext.errors.BufferReleasedError.retryable

```python
@property
def retryable(self) -> bool
```

Whether retrying later is generally reasonable.

This is a hint, not a retry policy.  Backpressure requires releasing old
leases first, and an internal error may still be permanent.

此方法定义于基类 `RecameraError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L110)

## recamera_ext.errors.HandleClosedError

```python
class HandleClosedError(RecameraRuntimeError)
```

An operation was attempted after its owning native handle was closed.

### recamera_ext.errors.HandleClosedError.__init__

```python
def __init__(self, message: Optional[str]=None, *, operation: Optional[str]=None, code: Optional[ErrorCode]=None, rc: Optional[int]=None, detail: Optional[str]=None) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

此方法定义于基类 `RecameraError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L82)

### recamera_ext.errors.HandleClosedError.code_value

```python
@property
def code_value(self) -> Optional[int]
```

Numeric error code, including unknown native return values.

此方法定义于基类 `RecameraError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L100)

### recamera_ext.errors.HandleClosedError.retryable

```python
@property
def retryable(self) -> bool
```

Whether retrying later is generally reasonable.

This is a hint, not a retry policy.  Backpressure requires releasing old
leases first, and an internal error may still be permanent.

此方法定义于基类 `RecameraError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L110)

## recamera_ext.errors.AcquireTimeoutError

```python
class AcquireTimeoutError(RecameraError, TimeoutError)
```

A strict ``acquire()`` call reached its timeout without a record.

### recamera_ext.errors.AcquireTimeoutError.__init__

```python
def __init__(self, message: Optional[str]=None, *, operation: Optional[str]=None, code: Optional[ErrorCode]=None, rc: Optional[int]=None, detail: Optional[str]=None) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

此方法定义于基类 `RecameraError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L82)

### recamera_ext.errors.AcquireTimeoutError.code_value

```python
@property
def code_value(self) -> Optional[int]
```

Numeric error code, including unknown native return values.

此方法定义于基类 `RecameraError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L100)

### recamera_ext.errors.AcquireTimeoutError.retryable

```python
@property
def retryable(self) -> bool
```

Whether retrying later is generally reasonable.

This is a hint, not a retry policy.  Backpressure requires releasing old
leases first, and an internal error may still be permanent.

此方法定义于基类 `RecameraError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L110)

## recamera_ext.errors.LibraryLoadError

```python
class LibraryLoadError(RecameraError, OSError)
```

No compatible ``librecamera_ext.so.1`` could be loaded and bound.

### recamera_ext.errors.LibraryLoadError.__init__

```python
def __init__(self, message: Optional[str]=None, *, operation: Optional[str]=None, code: Optional[ErrorCode]=None, rc: Optional[int]=None, detail: Optional[str]=None) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

此方法定义于基类 `RecameraError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L82)

### recamera_ext.errors.LibraryLoadError.code_value

```python
@property
def code_value(self) -> Optional[int]
```

Numeric error code, including unknown native return values.

此方法定义于基类 `RecameraError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L100)

### recamera_ext.errors.LibraryLoadError.retryable

```python
@property
def retryable(self) -> bool
```

Whether retrying later is generally reasonable.

This is a hint, not a retry policy.  Backpressure requires releasing old
leases first, and an internal error may still be permanent.

此方法定义于基类 `RecameraError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L110)

## recamera_ext.errors.ResultTooLarge

```python
class ResultTooLarge(RecameraError, ValueError)
```

A result datagram would exceed the local conservative wire budget.

### recamera_ext.errors.ResultTooLarge.__init__

```python
def __init__(self, message: Optional[str]=None, *, operation: Optional[str]=None, code: Optional[ErrorCode]=None, rc: Optional[int]=None, detail: Optional[str]=None) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

此方法定义于基类 `RecameraError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L82)

### recamera_ext.errors.ResultTooLarge.code_value

```python
@property
def code_value(self) -> Optional[int]
```

Numeric error code, including unknown native return values.

此方法定义于基类 `RecameraError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L100)

### recamera_ext.errors.ResultTooLarge.retryable

```python
@property
def retryable(self) -> bool
```

Whether retrying later is generally reasonable.

This is a hint, not a retry policy.  Backpressure requires releasing old
leases first, and an internal error may still be permanent.

此方法定义于基类 `RecameraError`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L110)

## recamera_ext.errors.error_from_rc

```python
def error_from_rc(operation: str, rc: int, *, detail: Optional[str]=None) -> RecameraRuntimeError
```

Build the typed exception for a native error return.

``rc`` may be a positive ``*err`` value from an ``open`` function or the
negative value returned by an operation.  Zero is rejected because it means
success and converting it into an exception almost certainly masks a wrapper
bug.  Unknown values remain available through ``exc.rc``/``code_value``.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/sdk/python/recamera_ext/errors.py#L221)

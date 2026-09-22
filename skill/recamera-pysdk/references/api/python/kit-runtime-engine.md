# kit.runtime.engine

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/runtime/engine.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/runtime/engine.py)；签名由 AST 提取，不导入硬件依赖。

TensorSpec/ModelSpec、推理统计及 legacy 本地 RKNN session；布局是契约，不会隐式转置。scheduled 托管进程禁止直接本地 session。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

Typed RKNN inference sessions for the RV1126B NPU.

``RknnSession`` is the new public interface.  ``RknnModel`` remains as a
backwards-compatible subclass for existing applications.  Both acquire NPU
ownership *before* constructing ``RKNNLite``, mark the shared broker lease ready
only after native initialization, check that ownership is still alive before
every inference, and release the final lease reference only after every
protected RKNN context has been destroyed.

## kit.runtime.engine.TensorSpec

```python
@dataclass(frozen=True)
class TensorSpec
```

Expected tensor name, shape, dtype, and layout.

A shape dimension of ``-1`` accepts any positive size.  Layout is metadata
used for validation/documentation; the RKNN runtime receives arrays in the
supplied order and does not transpose them implicitly.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
name: str
shape: tuple[int, ...]
dtype: str = 'uint8'
layout: str = 'NHWC'
```

### kit.runtime.engine.TensorSpec.validate

```python
def validate(self, value: np.ndarray, operation: str) -> np.ndarray
```

Validate one array and return it unchanged.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/runtime/engine.py#L123)

## kit.runtime.engine.ModelSpec

```python
@dataclass(frozen=True)
class ModelSpec
```

Declared RKNN model contract.

``inputs``/``outputs`` may initially be empty for legacy models whose
metadata is not exported.  New applications should declare them in their
manifest so invalid dtype/layout/shape fails before entering the driver.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
path: str
inputs: tuple[TensorSpec, ...] = ()
outputs: tuple[TensorSpec, ...] = ()
core_mask: Optional[int] = None
name: Optional[str] = None
```

## kit.runtime.engine.InferenceStats

```python
@dataclass(frozen=True)
class InferenceStats
```

Cumulative session statistics suitable for a health endpoint.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
calls: int = 0
failures: int = 0
total_ms: float = 0.0
last_ms: float = 0.0
```

### kit.runtime.engine.InferenceStats.average_ms

```python
@property
def average_ms(self) -> float
```

Mean successful/failed call duration, or zero before the first call.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/runtime/engine.py#L233)

## kit.runtime.engine.RknnSession

```python
class RknnSession
```

Own one RKNN runtime context and its process-level NPU lease.

Parameters:
    model: A model path or :class:`ModelSpec`.
    core_mask: Optional RKNN core mask.  RV1126B normally uses the runtime
        default because it has a single NPU core.
    lease: Resource lease implementation.  The default is
        :class:`~kit.resources.ExternalNpuLease`.
    lease_timeout: Seconds to wait for another Python process to release
        the NPU.  ``None`` selects rkipc's bounded server default (and
        remains an indefinite wait only for an explicit legacy lock).
    runtime_factory: Test/vendor injection point returning an RKNNLite-like
        object.  Applications normally leave it unset.
    strict_inputs: Validate declared TensorSpec contracts without implicit
        dtype conversion.  Defaults to true for this new interface.

### kit.runtime.engine.RknnSession.__init__

```python
def __init__(self, model: str | ModelSpec, core_mask: Optional[int]=None, *, lease: Optional[ResourceLease]=None, lease_timeout: Optional[float]=30.0, runtime_factory: Optional[Callable[[], Any]]=None, strict_inputs: bool=True) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/runtime/engine.py#L306)

### kit.runtime.engine.RknnSession.released

```python
@property
def released(self) -> bool
```

Whether the native runtime and lease have been released.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/runtime/engine.py#L407)

### kit.runtime.engine.RknnSession.stats

```python
@property
def stats(self) -> InferenceStats
```

Return an immutable snapshot of inference timing/counters.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/runtime/engine.py#L414)

### kit.runtime.engine.RknnSession.infer

```python
def infer(self, inputs: Any) -> List[np.ndarray]
```

Run one synchronous forward pass and return raw output arrays.

``inputs`` may be one ndarray, a sequence for a declared multi-input
model, or a mapping keyed by ``TensorSpec.name``.  Driver exceptions are
wrapped as :class:`InferenceError` with the original exception retained
as ``__cause__``.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/runtime/engine.py#L519)

### kit.runtime.engine.RknnSession.release

```python
def release(self) -> None
```

Destroy the RKNN context and release the NPU guard exactly once.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/runtime/engine.py#L688)

### kit.runtime.engine.RknnSession.__enter__

```python
def __enter__(self) -> 'RknnSession'
```

进入上下文管理器，返回其受管对象；与 __exit__ 配对使用，避免异常路径遗留资源。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/runtime/engine.py#L786)

### kit.runtime.engine.RknnSession.__exit__

```python
def __exit__(self, exc_type, exc, traceback) -> bool
```

离开上下文并执行本类的清理方法；异常传播/清理失败语义见类说明，不把退出视作任务已完成。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/runtime/engine.py#L795)

## kit.runtime.engine.RknnModel

```python
class RknnModel(RknnSession)
```

Compatibility wrapper preserving the original permissive input rules.

Existing applications may continue to pass float/other arrays; they are
converted to uint8 as before.  New code should use ``RknnSession`` with a
declared ``ModelSpec`` so dtype/shape mistakes fail explicitly.

### kit.runtime.engine.RknnModel.__init__

```python
def __init__(self, path: str, core_mask: Optional[int]=None, **kwargs) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/runtime/engine.py#L838)

### kit.runtime.engine.RknnModel.released

```python
@property
def released(self) -> bool
```

Whether the native runtime and lease have been released.

此方法定义于基类 `RknnSession`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/runtime/engine.py#L407)

### kit.runtime.engine.RknnModel.stats

```python
@property
def stats(self) -> InferenceStats
```

Return an immutable snapshot of inference timing/counters.

此方法定义于基类 `RknnSession`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/runtime/engine.py#L414)

### kit.runtime.engine.RknnModel.infer

```python
def infer(self, inputs: Any) -> List[np.ndarray]
```

Run one synchronous forward pass and return raw output arrays.

``inputs`` may be one ndarray, a sequence for a declared multi-input
model, or a mapping keyed by ``TensorSpec.name``.  Driver exceptions are
wrapped as :class:`InferenceError` with the original exception retained
as ``__cause__``.

此方法定义于基类 `RknnSession`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/runtime/engine.py#L519)

### kit.runtime.engine.RknnModel.release

```python
def release(self) -> None
```

Destroy the RKNN context and release the NPU guard exactly once.

此方法定义于基类 `RknnSession`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/runtime/engine.py#L688)

### kit.runtime.engine.RknnModel.__enter__

```python
def __enter__(self) -> 'RknnSession'
```

进入上下文管理器，返回其受管对象；与 __exit__ 配对使用，避免异常路径遗留资源。

此方法定义于基类 `RknnSession`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/runtime/engine.py#L786)

### kit.runtime.engine.RknnModel.__exit__

```python
def __exit__(self, exc_type, exc, traceback) -> bool
```

离开上下文并执行本类的清理方法；异常传播/清理失败语义见类说明，不把退出视作任务已完成。

此方法定义于基类 `RknnSession`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/runtime/engine.py#L795)

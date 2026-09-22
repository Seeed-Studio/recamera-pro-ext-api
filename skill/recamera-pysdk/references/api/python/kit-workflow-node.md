# kit.workflow.node

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/workflow/node.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/workflow/node.py)；签名由 AST 提取，不导入硬件依赖。

Stage、WorkflowContext 与协作取消/超时。资源声明只是显式对象依赖，超时在阶段边界检查，不能强制打断 native 调用。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

Workflow stages, context and typed execution failures.

This module defines a deliberately small synchronous workflow contract.  A
``ResourceKind`` declaration means only that the caller must place a concrete
object in :class:`WorkflowContext.resources`; it does not acquire an rkipc
lease, arbitrate the NPU, start worker threads or infer device availability.

## 公开导出 / 别名

| 导入名称 | 定义与完整说明 |
| --- | --- |
| `ResourceKind` | [kit.resources.ResourceKind](kit-resources.md) |

## kit.workflow.node.WorkflowError

```python
class WorkflowError(KitError, RuntimeError)
```

Base class for structured synchronous-workflow failures.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
default_code = 'workflow_error'
```

继承接口：[kit.errors.KitError](kit-errors.md)。

## kit.workflow.node.WorkflowCancelled

```python
class WorkflowCancelled(WorkflowError)
```

Execution stopped because its explicit cancellation token was set.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
default_code = 'workflow_cancelled'
```

## kit.workflow.node.WorkflowTimeout

```python
class WorkflowTimeout(WorkflowError, TimeoutError)
```

An item exceeded its monotonic deadline at a stage boundary.

A sequential Python callable cannot be preempted safely.  The runtime checks
before and after each stage, so a blocking stage is reported immediately
after it returns; cooperative stages can also inspect the context token and
deadline themselves.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
default_code = 'workflow_timeout'
```

## kit.workflow.node.WorkflowResourceError

```python
class WorkflowResourceError(WorkflowError)
```

A stage's declared resource was absent from the explicit context.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
default_code = 'workflow_resource_missing'
```

## kit.workflow.node.WorkflowClosedError

```python
class WorkflowClosedError(WorkflowError)
```

Execution was attempted after its owning pipeline was closed.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
default_code = 'workflow_closed'
```

## kit.workflow.node.WorkflowCleanupError

```python
class WorkflowCleanupError(WorkflowError)
```

One or more stage closers failed during context-manager teardown.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
default_code = 'workflow_cleanup_failed'
```

## kit.workflow.node.StageError

```python
class StageError(WorkflowError)
```

A stage callable raised an ordinary :class:`Exception`.

The original exception is always retained as ``__cause__``.  Process
control flow deriving directly from :class:`BaseException`, such as
``KeyboardInterrupt`` and ``SystemExit``, is never converted to this type.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
default_code = 'workflow_stage_failed'
```

## kit.workflow.node.CancellationToken

```python
class CancellationToken
```

Thread-safe, cooperative cancellation signal shared by workflow stages.

``cancel`` is idempotent and the first non-empty reason wins.  The token
cannot forcibly interrupt a Python callable; a stage doing long work should
inspect ``cancelled`` or call :meth:`raise_if_cancelled` at safe points.

### kit.workflow.node.CancellationToken.__init__

```python
def __init__(self) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/workflow/node.py#L107)

### kit.workflow.node.CancellationToken.cancelled

```python
@property
def cancelled(self) -> bool
```

Whether cancellation has been requested.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/workflow/node.py#L113)

### kit.workflow.node.CancellationToken.reason

```python
@property
def reason(self) -> str
```

Stable first cancellation reason, or an empty string.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/workflow/node.py#L119)

### kit.workflow.node.CancellationToken.cancel

```python
def cancel(self, reason: str='') -> bool
```

Request cancellation; return ``True`` only for the first request.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/workflow/node.py#L125)

### kit.workflow.node.CancellationToken.wait

```python
def wait(self, timeout: float | None=None) -> bool
```

Wait for cancellation using :class:`threading.Event` semantics.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/workflow/node.py#L141)

### kit.workflow.node.CancellationToken.raise_if_cancelled

```python
def raise_if_cancelled(self, *, stage: str | None=None, item_index: int | None=None, elapsed_ms: float=0.0) -> None
```

Raise :class:`WorkflowCancelled` with machine-readable context.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/workflow/node.py#L148)

## kit.workflow.node.WorkflowContext

```python
@dataclass(frozen=True, slots=True)
class WorkflowContext
```

Explicit resources and per-run state passed to each stage.

``resources`` is copied into a read-only mapping keyed by
:class:`~kit.resources.ResourceKind`; a key with value ``None`` is treated
as unavailable.  The runtime never acquires or releases these objects.
``metadata`` is caller-owned descriptive state and is shallow-copied.
``deadline`` is an absolute monotonic timestamp for the current item, not a
wall-clock time.  ``item_index`` is filled by :class:`Pipeline`.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
resources: Mapping[ResourceKind, Any] = field(default_factory=dict)
metadata: Mapping[str, Any] = field(default_factory=dict)
cancellation: CancellationToken = field(default_factory=CancellationToken)
deadline: float | None = None
item_index: int | None = None
```

### kit.workflow.node.WorkflowContext.for_item

```python
def for_item(self, *, item_index: int, deadline: float | None) -> 'WorkflowContext'
```

Return a context view for one item, sharing resources and token.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/workflow/node.py#L228)

### kit.workflow.node.WorkflowContext.get_resource

```python
def get_resource(self, kind: ResourceKind | str, default: Any=None) -> Any
```

Return an explicitly supplied resource without acquiring anything.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/workflow/node.py#L238)

### kit.workflow.node.WorkflowContext.require_resource

```python
def require_resource(self, kind: ResourceKind | str) -> Any
```

Return one resource or fail closed with ``WorkflowResourceError``.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/workflow/node.py#L249)

### kit.workflow.node.WorkflowContext.remaining

```python
def remaining(self, clock: Callable[[], float]=time.monotonic) -> float | None
```

Return non-negative seconds remaining, or ``None`` without deadline.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/workflow/node.py#L269)

### kit.workflow.node.WorkflowContext.raise_if_cancelled

```python
def raise_if_cancelled(self, *, stage: str | None=None, elapsed_ms: float=0.0) -> None
```

Delegate a contextual cooperative-cancellation check to the token.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/workflow/node.py#L276)

## kit.workflow.node.Stage

```python
@dataclass(frozen=True, slots=True)
class Stage
```

One named synchronous transform in a deterministic pipeline.

``callable`` may accept either ``item`` or ``(item, context)`` (including a
keyword-only ``context``).  Its signature is inspected once at construction
so an internal ``TypeError`` is never mistaken for arity negotiation.
``requires`` is declarative and checked before every invocation.  ``closer``
is optional; otherwise a callable ``close`` attribute on the transform is
used by :meth:`Pipeline.close`.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
name: str
callable: Callable[..., Any]
requires: frozenset[ResourceKind] = frozenset()
closer: Callable[[], Any] | None = field(default=None, repr=False, compare=False)
```

### kit.workflow.node.Stage.invoke

```python
def invoke(self, item: Any, context: WorkflowContext) -> Any
```

Invoke the transform using its prevalidated context calling mode.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/workflow/node.py#L391)

### kit.workflow.node.Stage.close

```python
def close(self) -> bool
```

Close an unowned stage exactly once.

Once a stage is placed in a :class:`Pipeline`, that pipeline owns its
lifetime and direct closure is rejected.  This avoids a second
pipeline or caller invalidating a resource during execution.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/workflow/node.py#L429)

### kit.workflow.node.Stage.__or__

```python
def __or__(self, other: Any)
```

Compose ``Stage | Stage`` or ``Stage | Pipeline`` in order.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/workflow/node.py#L454)

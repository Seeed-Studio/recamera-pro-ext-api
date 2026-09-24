# kit.workflow.runtime

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/workflow/runtime.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/workflow/runtime.py)；签名由 AST 提取，不导入硬件依赖。

同步 Pipeline 的组合、运行、统计与逆序清理；Stage 所有权只能转移一次，关闭等待在途任务。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

Deterministic synchronous workflow pipeline runtime.

``Pipeline`` executes exactly one stage after another in the calling thread.
It is not an async DAG executor, thread pool, rkipc lease manager or NPU
scheduler.  Deadline and cancellation checks occur at stage boundaries; a
callable that needs mid-stage cancellation must cooperate through the supplied
context.

## 常量与类型别名

以下为该版本源码值；默认地址不等于所有固件都开放该服务。

```python
CloseReport = WorkflowCloseReport
```


## kit.workflow.runtime.StageCloseFailure

```python
@dataclass(frozen=True, slots=True)
class StageCloseFailure
```

One ordinary exception raised while closing a named stage.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
stage: str
error: Exception
```

### kit.workflow.runtime.StageCloseFailure.message

```python
@property
def message(self) -> str
```

Stable human-readable error summary.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/workflow/runtime.py#L47)

## kit.workflow.runtime.WorkflowCloseReport

```python
@dataclass(frozen=True, slots=True)
class WorkflowCloseReport
```

Outcome of one reverse-order pipeline close pass.

Ordinary close exceptions are retained in ``failures`` after all remaining
stages have been attempted.  A direct :class:`BaseException` control-flow
signal is re-raised after cleanup and therefore never appears as a
successful report.  Repeated ``close`` calls return the original outcome
with ``already_closed=True`` and do not call a closer twice.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
attempted: int
closed: int
skipped: int
failures: tuple[StageCloseFailure, ...] = ()
already_closed: bool = False
```

### kit.workflow.runtime.WorkflowCloseReport.ok

```python
@property
def ok(self) -> bool
```

Whether every attempted closer completed without an exception.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/workflow/runtime.py#L71)

### kit.workflow.runtime.WorkflowCloseReport.errors

```python
@property
def errors(self) -> tuple[str, ...]
```

Compact ``stage: exception`` summaries for status reporting.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/workflow/runtime.py#L77)

### kit.workflow.runtime.WorkflowCloseReport.as_dict

```python
def as_dict(self) -> dict[str, Any]
```

Return a JSON-compatible close summary.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/workflow/runtime.py#L82)

## kit.workflow.runtime.Pipeline

```python
class Pipeline
```

Linearly owned stage order with a synchronous execution loop.

``Pipeline | Stage`` and ``Stage | Stage`` return a new pipeline; execution
order is always left to right.  ``run_one`` processes one value, while
``run`` lazily yields one result for each input in order.  ``timeout`` is a
fresh relative deadline per item; ``deadline`` is an optional absolute
monotonic deadline shared by the invocation.  When both are provided the
earlier boundary wins.

Composition transfers stage ownership into the returned pipeline.  The
source pipeline becomes closed and cannot run or close those stages.  This
move-like rule deliberately prevents the same closer-backed ``Stage`` from
being shared by two independently closed pipelines.

A pipeline owns only stage *close calls*.  Resource objects in
:class:`WorkflowContext` remain caller-owned, and this class performs no
hidden concurrency, acquisition, scheduling or retry.

### kit.workflow.runtime.Pipeline.__init__

```python
def __init__(self, stages: Iterable[Stage]=(), *, clock: Callable[[], float]=time.monotonic, logger: logging.Logger | None=None, _claim: bool=True) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/workflow/runtime.py#L166)

### kit.workflow.runtime.Pipeline.stages

```python
@property
def stages(self) -> tuple[Stage, ...]
```

Stages in their exact deterministic execution order.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/workflow/runtime.py#L202)

### kit.workflow.runtime.Pipeline.closed

```python
@property
def closed(self) -> bool
```

Whether close has started and no further items may run.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/workflow/runtime.py#L208)

### kit.workflow.runtime.Pipeline.__len__

```python
def __len__(self) -> int
```

返回同步 Pipeline 的 Stage 数量，不是运行线程数或队列长度。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/workflow/runtime.py#L214)

### kit.workflow.runtime.Pipeline.__iter__

```python
def __iter__(self) -> Iterator[Stage]
```

返回本对象定义的迭代器；迭代元素与借用有效期见类说明。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/workflow/runtime.py#L217)

### kit.workflow.runtime.Pipeline.__or__

```python
def __or__(self, other: Any) -> 'Pipeline'
```

Move stages into a new left-to-right composition.

A closer-backed stage is a linear resource, not a reusable value.  On
success the source pipeline(s) are marked transferred and only the
returned pipeline may execute or close the combined stages.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/workflow/runtime.py#L220)

### kit.workflow.runtime.Pipeline.run_one

```python
def run_one(self, item: Any, context: WorkflowContext | None=None, *, timeout: float | None=None, deadline: float | None=None, item_index: int=0) -> Any
```

Run one item through every stage in order and return the final value.

``timeout`` is relative to this call.  ``deadline`` and a deadline
already present on ``context`` are absolute values from the pipeline's
monotonic clock.  Ordinary stage exceptions become :class:`StageError`
with cause/context; direct ``BaseException`` control flow passes through.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/workflow/runtime.py#L460)

### kit.workflow.runtime.Pipeline.run

```python
def run(self, items: Iterable[Any], context: WorkflowContext | None=None, *, timeout: float | None=None, deadline: float | None=None) -> Iterator[Any]
```

Yield results in input order, applying ``timeout`` freshly per item.

The iterator is lazy and runs entirely on its consumer's thread.  An
absolute ``deadline`` applies to every item and can therefore serve as a
whole-loop boundary; ``timeout`` is recomputed inside each ``run_one``.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/workflow/runtime.py#L574)

### kit.workflow.runtime.Pipeline.close

```python
def close(self) -> WorkflowCloseReport
```

Drain active calls, then close stages in reverse order exactly once.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/workflow/runtime.py#L598)

### kit.workflow.runtime.Pipeline.close_report

```python
@property
def close_report(self) -> WorkflowCloseReport | None
```

The first close outcome, including context-manager teardown.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/workflow/runtime.py#L688)

### kit.workflow.runtime.Pipeline.__enter__

```python
def __enter__(self) -> 'Pipeline'
```

进入上下文管理器，返回其受管对象；与 __exit__ 配对使用，避免异常路径遗留资源。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/workflow/runtime.py#L694)

### kit.workflow.runtime.Pipeline.__exit__

```python
def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None
```

离开上下文并执行本类的清理方法；异常传播/清理失败语义见类说明，不把退出视作任务已完成。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/workflow/runtime.py#L698)

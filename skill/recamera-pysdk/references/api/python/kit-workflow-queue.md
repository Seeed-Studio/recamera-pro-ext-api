# kit.workflow.queue

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/workflow/queue.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/workflow/queue.py)；签名由 AST 提取，不导入硬件依赖。

线程安全有界 FIFO；block/drop_oldest/drop_newest 均有明确结果，丢弃项由调用者释放。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

Bounded thread-safe input queue with explicit backpressure outcomes.

This queue may connect producer and consumer threads, but it does not make
:class:`~kit.workflow.Pipeline` concurrent and does not schedule NPU work.  Its
capacity, wait timeouts and drop policy are explicit so a live-camera producer
cannot accumulate unbounded latency or silently discard frames.

## 常量与类型别名

以下为该版本源码值；默认地址不等于所有固件都开放该服务。

```python
T = TypeVar('T')
```


```python
BackpressureTimeoutError = WorkflowBackpressureError
```


## kit.workflow.queue.DropPolicy

```python
class DropPolicy(str, Enum)
```

Behavior when an :class:`InputQueue` has reached capacity.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
BLOCK = 'block'
DROP_OLDEST = 'drop_oldest'
DROP_NEWEST = 'drop_newest'
```

## kit.workflow.queue.PutStatus

```python
class PutStatus(str, Enum)
```

Exact disposition of one queue ``put`` attempt.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
ENQUEUED = 'enqueued'
DROPPED_OLDEST = 'dropped_oldest'
DROPPED_NEWEST = 'dropped_newest'
```

## kit.workflow.queue.QueueError

```python
class QueueError(WorkflowError)
```

Base class for typed bounded-input queue failures.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
default_code = 'workflow_queue_error'
```

继承接口：[kit.workflow.node.WorkflowError](kit-workflow-node.md)。

## kit.workflow.queue.QueueClosedError

```python
class QueueClosedError(QueueError)
```

An operation cannot continue because the queue is closed and drained.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
default_code = 'workflow_queue_closed'
```

## kit.workflow.queue.QueueTimeoutError

```python
class QueueTimeoutError(QueueError, TimeoutError)
```

A blocking consumer timed out waiting for an input item.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
default_code = 'workflow_queue_timeout'
```

## kit.workflow.queue.WorkflowBackpressureError

```python
class WorkflowBackpressureError(QueueError, TimeoutError)
```

A ``BLOCK`` producer timed out while the bounded queue remained full.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
default_code = 'workflow_backpressure_timeout'
```

## kit.workflow.queue.PutResult

```python
@dataclass(frozen=True, slots=True)
class PutResult(Generic[T])
```

Non-exception result of one accepted or deliberately dropped put.

``dropped_item`` is the evicted oldest item for ``DROP_OLDEST`` and the
rejected offered item for ``DROP_NEWEST``.  It may legitimately be ``None``;
callers should use ``status``/``dropped`` to determine disposition.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
status: PutStatus
size_after: int
waited_seconds: float = 0.0
dropped_item: T | None = None
```

### kit.workflow.queue.PutResult.accepted

```python
@property
def accepted(self) -> bool
```

Whether the offered item entered the queue.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/workflow/queue.py#L87)

### kit.workflow.queue.PutResult.dropped

```python
@property
def dropped(self) -> bool
```

Whether either an old or the offered item was discarded.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/workflow/queue.py#L93)

## kit.workflow.queue.QueueStats

```python
@dataclass(frozen=True, slots=True)
class QueueStats
```

Atomic snapshot of all queue counters and current state.

``put_attempts``/``get_attempts`` include calls rejected after close.
``put_waits``/``get_waits`` count operations that encountered a full/empty
queue and entered the blocking path, including zero-timeout attempts; they
do not count condition-variable wakeups.  For a queue that never clears
items, ``enqueued == dequeued + size + dropped_oldest`` always holds.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
capacity: int
size: int
high_watermark: int
closed: bool
put_attempts: int
get_attempts: int
enqueued: int
dequeued: int
dropped_oldest: int
dropped_newest: int
put_waits: int
get_waits: int
put_timeouts: int
get_timeouts: int
closed_puts: int
closed_gets: int
```

### kit.workflow.queue.QueueStats.dropped

```python
@property
def dropped(self) -> int
```

Total items discarded by either drop policy.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/workflow/queue.py#L128)

### kit.workflow.queue.QueueStats.accepted

```python
@property
def accepted(self) -> int
```

Alias for the total number of successfully enqueued items.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/workflow/queue.py#L134)

### kit.workflow.queue.QueueStats.as_dict

```python
def as_dict(self) -> dict[str, Any]
```

Return a JSON-compatible metrics mapping.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/workflow/queue.py#L139)

## kit.workflow.queue.InputQueue

```python
class InputQueue(Generic[T])
```

A bounded FIFO with precise blocking and latest/oldest drop policies.

``BLOCK`` waits for space and raises :class:`WorkflowBackpressureError` on
timeout.  ``DROP_OLDEST`` atomically evicts the oldest queued item before
accepting the new one.  ``DROP_NEWEST`` rejects the offered item.  Drop
policies never hide that decision: every call returns :class:`PutResult`.

``close`` wakes all waiters and prevents further puts.  Existing items may
still be drained; a get on a closed, empty queue raises
:class:`QueueClosedError`.  Iteration follows that drain-until-closed rule.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
snapshot = stats
```

### kit.workflow.queue.InputQueue.__init__

```python
def __init__(self, capacity: int, policy: DropPolicy | str=DropPolicy.BLOCK, *, clock=time.monotonic) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/workflow/queue.py#L195)

### kit.workflow.queue.InputQueue.closed

```python
@property
def closed(self) -> bool
```

Whether no further items may be put.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/workflow/queue.py#L237)

### kit.workflow.queue.InputQueue.qsize

```python
def qsize(self) -> int
```

Return the current item count under the queue lock.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/workflow/queue.py#L243)

### kit.workflow.queue.InputQueue.empty

```python
def empty(self) -> bool
```

Whether the queue currently contains no items.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/workflow/queue.py#L249)

### kit.workflow.queue.InputQueue.full

```python
def full(self) -> bool
```

Whether the queue is currently at capacity.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/workflow/queue.py#L254)

### kit.workflow.queue.InputQueue.stats

```python
def stats(self) -> QueueStats
```

Return one internally consistent snapshot of all counters.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/workflow/queue.py#L280)

### kit.workflow.queue.InputQueue.put

```python
def put(self, item: T, timeout: float | None=None) -> PutResult[T]
```

Put one item according to policy, or raise a typed blocking failure.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/workflow/queue.py#L300)

### kit.workflow.queue.InputQueue.put_nowait

```python
def put_nowait(self, item: T) -> PutResult[T]
```

Put without waiting; a full BLOCK queue raises backpressure.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/workflow/queue.py#L386)

### kit.workflow.queue.InputQueue.get

```python
def get(self, timeout: float | None=None) -> T
```

Remove and return the oldest item, waiting up to ``timeout``.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/workflow/queue.py#L391)

### kit.workflow.queue.InputQueue.get_nowait

```python
def get_nowait(self) -> T
```

Get without waiting; an empty open queue raises ``QueueTimeoutError``.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/workflow/queue.py#L432)

### kit.workflow.queue.InputQueue.close

```python
def close(self) -> QueueStats
```

Prevent puts, wake every waiter and return the resulting snapshot.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/workflow/queue.py#L437)

### kit.workflow.queue.InputQueue.__len__

```python
def __len__(self) -> int
```

返回当前 qsize() 快照；并发生产/消费可立即改变长度，不应以 len(queue) 代替 get()/put() 的原子结果。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/workflow/queue.py#L446)

### kit.workflow.queue.InputQueue.__iter__

```python
def __iter__(self) -> Iterator[T]
```

Drain items until the closed queue becomes empty.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/workflow/queue.py#L449)

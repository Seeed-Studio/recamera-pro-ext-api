# kit.ai.publisher

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/ai/publisher.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/ai/publisher.py)；签名由 AST 提取，不导入硬件依赖。

将 typed ResultBatch 转为旧 sink 的原图像素协议。PublishReport 证明本地接受，不是远端确认或录像成功。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

Publish typed AI results through the existing legacy result-sink contract.

The current kit sinks accept ``emit(payload: dict, pts: float)`` and treat all
coordinates in that payload as original-frame pixels.  In particular,
``OfficialResultSink`` calls ``set_frame_size`` and performs the final
pixel-to-normalized conversion required by the rkipc/OSD ABI.  This module is a
strict compatibility boundary between that contract and :mod:`kit.ai.results`:

* pixel coordinates pass through unchanged;
* normalized coordinates are multiplied by the batch frame width/height;
* model-input coordinates require an explicit caller-supplied mapping and are
  never guessed from frame/model dimensions or a presumed letterbox policy;
* tracking objects are placed in ``events`` because that is the route consumed
  by the existing OSD sink, while other result types remain in ``results``.

Batch and result metadata are copied into the legacy payload.  Structural
fields such as ``box`` and ``keypoints`` are rebuilt from validated typed data,
so an old normalized alias can never coexist with the pixel value the sink
will consume.

## 常量与类型别名

以下为该版本源码值；默认地址不等于所有固件都开放该服务。

```python
ModelToPixel: TypeAlias = Callable[[float, float], tuple[float, float]]
```
Explicit model-input to original-frame pixel point mapping.

The callback receives one ``(x, y)`` point in :class:`CoordinateSpace.MODEL`
and must return the corresponding original-frame pixel point.  A closure may
capture the exact crop/resize/letterbox transform produced by preprocessing.
Scaling from model and frame dimensions alone is intentionally not provided:
it would silently mishandle padding, crops and non-square inputs.


## kit.ai.publisher.LegacyResultSink

```python
class LegacyResultSink(Protocol)
```

Structural type implemented by the existing result/OSD sinks.

### kit.ai.publisher.LegacyResultSink.emit

```python
def emit(self, payload: dict[str, Any], pts: float) -> None
```

Publish one legacy result payload at a timestamp in seconds.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/ai/publisher.py#L73)

### kit.ai.publisher.LegacyResultSink.set_frame_size

```python
def set_frame_size(self, w: int, h: int) -> None
```

Set the original-frame pixel extent before ``emit``.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/ai/publisher.py#L76)

### kit.ai.publisher.LegacyResultSink.emit_checked

```python
def emit_checked(self, payload: dict[str, Any], pts: float) -> None
```

Optional strict counterpart that surfaces local delivery failures.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/ai/publisher.py#L79)

### kit.ai.publisher.LegacyResultSink.set_frame_size_checked

```python
def set_frame_size_checked(self, w: int, h: int) -> None
```

Optional strict counterpart for frame geometry.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/ai/publisher.py#L82)

## kit.ai.publisher.PublishReport

```python
@dataclass(frozen=True, slots=True)
class PublishReport
```

Summary returned only after the underlying sink accepts ``emit`` locally.

``input_results`` is the number of typed objects supplied.  Tracks become
legacy events, hence ``payload_results`` and ``payload_events`` describe the
actual lists sent to the sink.  Coordinate counters count x/y *pairs* that
were transformed; a normalized box contributes two, and one normalized
keypoint contributes one.  Pixel pairs are not counted because they pass
through unchanged.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
source_id: str
pts_us: int
frame_width: int | None
frame_height: int | None
input_results: int
payload_results: int
payload_events: int
track_events: int
normalized_pairs: int
model_pairs: int
sink_type: str
elapsed_ms: float
locally_accepted: bool = True
server_acknowledged: bool = False
```

### kit.ai.publisher.PublishReport.converted_pairs

```python
@property
def converted_pairs(self) -> int
```

Total normalized/model coordinate pairs converted to pixels.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/ai/publisher.py#L114)

### kit.ai.publisher.PublishReport.as_dict

```python
def as_dict(self) -> dict[str, Any]
```

Return JSON-compatible structured telemetry for health endpoints.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/ai/publisher.py#L119)

## kit.ai.publisher.to_legacy_payload

```python
def to_legacy_payload(batch: ResultBatch, *, model_to_pixel: ModelToPixel | None=None) -> dict[str, Any]
```

Convert a typed batch to the pixel-coordinate payload existing sinks use.

This function is side-effect free and is useful for inspection or custom
transport integration.  It applies exactly the same strict coordinate and
metadata rules as :class:`ResultBatchPublisher` but does not call a sink.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/ai/publisher.py#L482)

## kit.ai.publisher.ResultBatchPublisher

```python
class ResultBatchPublisher
```

Compatibility adapter that publishes :class:`ResultBatch` to a sink.

Parameters:
    sink: Existing duck-typed result sink.  It must provide ``emit`` and may
        provide ``set_frame_size``; all built-in kit sinks provide both.
    model_to_pixel: Exact preprocess inverse for model-space coordinates.
        Omitting it is valid until a model-space result is encountered, at
        which point publication raises :class:`ConfigurationError`.
    logger: Optional logger override, primarily for host integrations.  The
        default is ``recamera.ai.publisher`` and import has no logging side
        effects.

Conversion/configuration failures happen before the sink is called.
Exceptions from ``set_frame_size`` or ``emit`` are logged with structured
context and re-raised as :class:`AdapterError` or :class:`TransportError`,
retaining the original exception as ``__cause__``.  No publish failure is
converted into a successful report.

### kit.ai.publisher.ResultBatchPublisher.__init__

```python
def __init__(self, sink: LegacyResultSink, *, model_to_pixel: ModelToPixel | None=None, logger: logging.Logger | None=None) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/ai/publisher.py#L518)

### kit.ai.publisher.ResultBatchPublisher.sink_type

```python
@property
def sink_type(self) -> str
```

Concrete sink class name used in reports, logs and error context.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/ai/publisher.py#L535)

### kit.ai.publisher.ResultBatchPublisher.publish

```python
def publish(self, batch: ResultBatch) -> PublishReport
```

Convert and synchronously publish one batch, returning its report.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/ai/publisher.py#L601)

## kit.ai.publisher.publish_result_batch

```python
def publish_result_batch(sink: LegacyResultSink, batch: ResultBatch, *, model_to_pixel: ModelToPixel | None=None, logger: logging.Logger | None=None) -> PublishReport
```

One-shot convenience wrapper around :class:`ResultBatchPublisher`.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/ai/publisher.py#L661)

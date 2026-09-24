# kit.ai.results

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/ai/results.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/ai/results.py)；签名由 AST 提取，不导入硬件依赖。

检测、分类、关键点、姿态、跟踪、分割及批量结果；构造时校验有限数值、坐标空间和置信度，支持规范与 legacy 字典互转。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

Typed AI results with explicit coordinates and lossless legacy adapters.

The first application generation exchanged loosely structured dictionaries.
Those dictionaries remain accepted at the migration boundary, but new code can
now state whether coordinates are source-image pixels, normalized fractions or
model-input pixels.  Every public model validates itself at construction time;
invalid inference output therefore fails close to the decoder instead of much
later in a result transport.

``to_dict``/``from_dict`` implement a JSON-safe canonical representation.
``to_legacy_dict``/``from_legacy_dict`` bridge the existing nine applications.
When an object originates from a legacy mapping, the original mapping is kept
privately and returned by ``to_legacy_dict`` so unknown business fields and the
original aliases (``cls`` versus ``class_id``, for example) are not lost.

## 常量与类型别名

以下为该版本源码值；默认地址不等于所有固件都开放该服务。

```python
AIResult: TypeAlias = Detection | Classification | Pose | Track | Segmentation
```


## kit.ai.results.CoordinateSpace

```python
class CoordinateSpace(str, Enum)
```

Meaning of every x/y coordinate carried by a result.

``PIXEL``
    Coordinates in the original camera frame.  Values are non-negative;
    :class:`ResultBatch` can additionally check them against ``frame_size``.
``NORMALIZED``
    Fractions of an image extent.  Both axes are strictly constrained to
    the closed interval ``[0, 1]``.
``MODEL``
    Pixels in the tensor/model input before inverse letterbox or crop
    mapping.  These must never be sent as source pixels accidentally;
    :class:`ResultBatch` can check them against ``model_size``.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
PIXEL = 'pixel'
NORMALIZED = 'normalized'
MODEL = 'model'
```

### kit.ai.results.CoordinateSpace.parse

```python
@classmethod
def parse(cls, value: 'CoordinateSpace | str') -> 'CoordinateSpace'
```

Return a coordinate-space enum or raise ``ConfigurationError``.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/ai/results.py#L187)

## kit.ai.results.Box

```python
@dataclass(frozen=True, slots=True)
class Box
```

An axis-aligned ``xyxy`` rectangle in an explicit coordinate space.

Coordinates must be finite, non-negative and ordered.  Normalized boxes
are additionally bounded by one.  A zero-area box is representable because
the native segmentation ABI uses it for an absent ROI; object-bearing
models such as :class:`Detection` and :class:`Track` reject zero area.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
x1: float
y1: float
x2: float
y2: float
space: CoordinateSpace = CoordinateSpace.PIXEL
```

### kit.ai.results.Box.width

```python
@property
def width(self) -> float
```

Rectangle width in this box's declared space.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/ai/results.py#L233)

### kit.ai.results.Box.height

```python
@property
def height(self) -> float
```

Rectangle height in this box's declared space.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/ai/results.py#L239)

### kit.ai.results.Box.is_empty

```python
@property
def is_empty(self) -> bool
```

Whether either rectangle dimension is zero.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/ai/results.py#L245)

### kit.ai.results.Box.to_dict

```python
def to_dict(self) -> dict[str, Any]
```

Return the canonical JSON-compatible box representation.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/ai/results.py#L250)

### kit.ai.results.Box.from_dict

```python
@classmethod
def from_dict(cls, data: Mapping[str, Any]) -> 'Box'
```

Decode a canonical box mapping.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/ai/results.py#L262)

### kit.ai.results.Box.from_legacy

```python
@classmethod
def from_legacy(cls, coordinates: Sequence[Any], *, space: CoordinateSpace | str=CoordinateSpace.PIXEL) -> 'Box'
```

Decode a historical ``[x1, y1, x2, y2]`` sequence.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/ai/results.py#L275)

### kit.ai.results.Box.to_legacy

```python
def to_legacy(self) -> list[float]
```

Return the historical four-element ``xyxy`` list.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/ai/results.py#L288)

## kit.ai.results.Detection

```python
@dataclass(frozen=True, slots=True)
class Detection
```

One localized class prediction.

``box`` identifies the object and owns its coordinate-space declaration.
``score`` is a finite probability in ``[0, 1]``; ``class_id`` is a
non-negative integer.  ``attributes`` carries JSON-compatible app-specific
annotations without weakening the core schema.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
box: Box
score: float
class_id: int = 0
label: str = ''
attributes: Mapping[str, Any] = field(default_factory=dict, hash=False)
```

### kit.ai.results.Detection.to_dict

```python
def to_dict(self) -> dict[str, Any]
```

Return a canonical JSON-compatible detection mapping.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/ai/results.py#L328)

### kit.ai.results.Detection.from_dict

```python
@classmethod
def from_dict(cls, data: Mapping[str, Any]) -> 'Detection'
```

Decode the canonical detection representation.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/ai/results.py#L341)

### kit.ai.results.Detection.from_legacy_dict

```python
@classmethod
def from_legacy_dict(cls, data: Mapping[str, Any], *, space: CoordinateSpace | str=CoordinateSpace.PIXEL) -> 'Detection'
```

Decode and retain a legacy detection dictionary losslessly.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/ai/results.py#L354)

### kit.ai.results.Detection.to_legacy_dict

```python
def to_legacy_dict(self) -> dict[str, Any]
```

Return the original legacy mapping or an equivalent flat mapping.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/ai/results.py#L381)

## kit.ai.results.Classification

```python
@dataclass(frozen=True, slots=True)
class Classification
```

One image-level or optional ROI-localized class prediction.

A classification always has a bounded ``score`` and non-negative
``class_id``.  ``box`` is optional for whole-image classifiers; when
present it is non-empty and carries the ROI coordinate space explicitly.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
score: float
class_id: int = 0
label: str = ''
box: Box | None = None
attributes: Mapping[str, Any] = field(default_factory=dict, hash=False)
```

### kit.ai.results.Classification.to_dict

```python
def to_dict(self) -> dict[str, Any]
```

Return a canonical JSON-compatible classification mapping.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/ai/results.py#L421)

### kit.ai.results.Classification.from_dict

```python
@classmethod
def from_dict(cls, data: Mapping[str, Any]) -> 'Classification'
```

Decode the canonical classification representation.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/ai/results.py#L434)

### kit.ai.results.Classification.from_legacy_dict

```python
@classmethod
def from_legacy_dict(cls, data: Mapping[str, Any], *, space: CoordinateSpace | str=CoordinateSpace.PIXEL) -> 'Classification'
```

Decode and retain a legacy classification dictionary losslessly.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/ai/results.py#L448)

### kit.ai.results.Classification.to_legacy_dict

```python
def to_legacy_dict(self) -> dict[str, Any]
```

Return the original legacy mapping or an equivalent flat mapping.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/ai/results.py#L474)

## kit.ai.results.Keypoint

```python
@dataclass(frozen=True, slots=True)
class Keypoint
```

One named/indexed landmark with confidence and coordinate space.

``id`` is a non-negative stable index (for example a COCO-17 joint index).
``score`` is the visibility/confidence probability in ``[0, 1]``.  Pixel
and model coordinates are non-negative; normalized coordinates are also
bounded by one.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
x: float
y: float
score: float = 1.0
id: int = 0
space: CoordinateSpace = CoordinateSpace.PIXEL
```

### kit.ai.results.Keypoint.to_dict

```python
def to_dict(self) -> dict[str, Any]
```

Return a canonical JSON-compatible landmark mapping.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/ai/results.py#L518)

### kit.ai.results.Keypoint.from_dict

```python
@classmethod
def from_dict(cls, data: Mapping[str, Any]) -> 'Keypoint'
```

Decode a canonical landmark mapping.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/ai/results.py#L530)

### kit.ai.results.Keypoint.to_legacy

```python
def to_legacy(self) -> list[float]
```

Return the historical ``[x, y, confidence]`` representation.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/ai/results.py#L542)

## kit.ai.results.Pose

```python
@dataclass(frozen=True, slots=True)
class Pose
```

A scored keypoint instance, optionally localized by an object box.

The pose must contain at least one :class:`Keypoint`; point IDs must be
unique and every point (and optional box) must use the same coordinate
space.  This catches the common error of mixing model-input joints with an
already un-letterboxed source-image box.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
keypoints: tuple[Keypoint, ...]
score: float = 1.0
class_id: int = 0
label: str = 'person'
box: Box | None = None
attributes: Mapping[str, Any] = field(default_factory=dict, hash=False)
```

### kit.ai.results.Pose.space

```python
@property
def space(self) -> CoordinateSpace
```

The common coordinate space of the instance's points and box.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/ai/results.py#L593)

### kit.ai.results.Pose.to_dict

```python
def to_dict(self) -> dict[str, Any]
```

Return a canonical JSON-compatible pose mapping.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/ai/results.py#L598)

### kit.ai.results.Pose.from_dict

```python
@classmethod
def from_dict(cls, data: Mapping[str, Any]) -> 'Pose'
```

Decode the canonical pose representation.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/ai/results.py#L612)

### kit.ai.results.Pose.from_legacy_dict

```python
@classmethod
def from_legacy_dict(cls, data: Mapping[str, Any], *, space: CoordinateSpace | str=CoordinateSpace.PIXEL) -> 'Pose'
```

Decode legacy ``keypoints`` arrays and retain all original fields.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/ai/results.py#L628)

### kit.ai.results.Pose.to_legacy_dict

```python
def to_legacy_dict(self) -> dict[str, Any]
```

Return the original mapping or the app-compatible keypoint shape.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/ai/results.py#L693)

## kit.ai.results.Track

```python
@dataclass(frozen=True, slots=True)
class Track
```

One tracked object with a stable non-negative track ID.

``box`` must have positive area and declares the coordinate space.  Scores
and class IDs follow :class:`Detection`; ``attributes`` is the place for
legacy state such as ``state``, ``in_zone`` or ``speed_px_s``.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
track_id: int
box: Box
score: float
class_id: int = 0
label: str = ''
attributes: Mapping[str, Any] = field(default_factory=dict, hash=False)
```

### kit.ai.results.Track.to_dict

```python
def to_dict(self) -> dict[str, Any]
```

Return a canonical JSON-compatible tracking mapping.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/ai/results.py#L739)

### kit.ai.results.Track.from_dict

```python
@classmethod
def from_dict(cls, data: Mapping[str, Any]) -> 'Track'
```

Decode the canonical tracking representation.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/ai/results.py#L753)

### kit.ai.results.Track.from_legacy_dict

```python
@classmethod
def from_legacy_dict(cls, data: Mapping[str, Any], *, space: CoordinateSpace | str=CoordinateSpace.PIXEL) -> 'Track'
```

Decode and retain a legacy tracking event losslessly.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/ai/results.py#L767)

### kit.ai.results.Track.to_legacy_dict

```python
def to_legacy_dict(self) -> dict[str, Any]
```

Return the original tracking event or an equivalent flat mapping.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/ai/results.py#L797)

## kit.ai.results.Segmentation

```python
@dataclass(frozen=True, slots=True)
class Segmentation
```

A row-major one-byte-per-pixel segmentation mask and optional ROI.

Non-empty masks require positive ``width`` and ``height`` and exactly
``width * height`` bytes.  Empty masks are represented only as ``b""`` with
both dimensions zero, matching the native extension ABI.  ``box`` is an
optional ROI whose coordinate space is explicit; mask pixels themselves
are indexed in the mask grid, not in an implicit image coordinate space.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
mask: bytes
width: int
height: int
score: float = 1.0
class_id: int = 0
label: str = ''
box: Box | None = None
attributes: Mapping[str, Any] = field(default_factory=dict, hash=False)
```

### kit.ai.results.Segmentation.to_dict

```python
def to_dict(self) -> dict[str, Any]
```

Return a JSON-safe mapping with the mask encoded as base64.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/ai/results.py#L915)

### kit.ai.results.Segmentation.from_dict

```python
@classmethod
def from_dict(cls, data: Mapping[str, Any]) -> 'Segmentation'
```

Decode a canonical base64 segmentation representation.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/ai/results.py#L932)

### kit.ai.results.Segmentation.from_legacy_dict

```python
@classmethod
def from_legacy_dict(cls, data: Mapping[str, Any], *, space: CoordinateSpace | str=CoordinateSpace.PIXEL) -> 'Segmentation'
```

Decode bytes or nested legacy masks and retain the mapping exactly.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/ai/results.py#L959)

### kit.ai.results.Segmentation.to_legacy_dict

```python
def to_legacy_dict(self) -> dict[str, Any]
```

Return the original mapping or the SDK-compatible flat mask shape.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/ai/results.py#L997)

## kit.ai.results.ResultBatch

```python
@dataclass(frozen=True, slots=True)
class ResultBatch
```

Validated results associated with one frame or inference invocation.

``pts_us`` is a non-negative monotonic frame timestamp (zero means
unassociated, matching the extension API).  ``frame_size`` bounds pixel
coordinates and ``model_size`` bounds model coordinates when supplied;
normalized coordinates are always checked at their own construction.
Mixed task types and coordinate spaces are allowed because multi-head
workflows may publish them together, but every individual value remains
explicit and validated.

The canonical ``to_dict`` representation is versioned and JSON-safe,
including base64 segmentation masks.  Legacy payloads can be read with
:meth:`from_legacy_dict` and returned without dropping unknown fields.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
results: tuple[AIResult, ...] = ()
pts_us: int = 0
source_id: str = ''
frame_size: tuple[int, int] | None = None
model_size: tuple[int, int] | None = None
attributes: Mapping[str, Any] = field(default_factory=dict, hash=False)
SCHEMA_VERSION = 1
```

### kit.ai.results.ResultBatch.to_dict

```python
def to_dict(self) -> dict[str, Any]
```

Return the versioned, JSON-compatible canonical batch mapping.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/ai/results.py#L1158)

### kit.ai.results.ResultBatch.from_dict

```python
@classmethod
def from_dict(cls, data: Mapping[str, Any]) -> 'ResultBatch'
```

Decode a canonical batch and reject unknown schema versions.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/ai/results.py#L1172)

### kit.ai.results.ResultBatch.to_json

```python
def to_json(self, **json_kwargs: Any) -> str
```

Serialize the canonical representation with :func:`json.dumps`.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/ai/results.py#L1191)

### kit.ai.results.ResultBatch.from_json

```python
@classmethod
def from_json(cls, payload: str | bytes | bytearray) -> 'ResultBatch'
```

Deserialize JSON text into a validated batch.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/ai/results.py#L1197)

### kit.ai.results.ResultBatch.from_legacy_dict

```python
@classmethod
def from_legacy_dict(cls, payload: Mapping[str, Any], *, space: CoordinateSpace | str=CoordinateSpace.PIXEL) -> 'ResultBatch'
```

Decode a historical payload while preserving it for exact output.

The historical ``results`` list is typed.  Business ``events`` remain
in batch attributes because many are temporal state transitions rather
than inference results; callers may convert track events explicitly
with :meth:`Track.from_legacy_dict`.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/ai/results.py#L1208)

### kit.ai.results.ResultBatch.to_legacy_dict

```python
def to_legacy_dict(self) -> dict[str, Any]
```

Return the original payload or an explicit app-compatible payload.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/ai/results.py#L1249)

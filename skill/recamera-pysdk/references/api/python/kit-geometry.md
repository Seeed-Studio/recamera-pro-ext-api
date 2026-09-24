# kit.geometry

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/geometry.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/geometry.py)；签名由 AST 提取，不导入硬件依赖。

规范绘制图元与 GeometryBuilder；严格校验形状、style 和 JSON 数据。图元通过 manifest 输出契约选择 renderer。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

Validated drawing primitives for the canonical Result Hub overlay.

Applications describe *what* to draw; the installed manifest remains the
authority for the coordinate space and render policy.  Consequently these
builders deliberately do not accept a ``space`` argument.  Managed Result Hub
ingress discards any payload-supplied space and injects the generation-bound
``output.fields[from="geometry[]"].coord`` declaration instead.

The wire representation is intentionally small and uniform::

    {"type": "polygon", "points": [[10.0, 20.0], ...],
     "style": {"color": "#00ff00", "line_width": 2}}

``box``/``quad`` are compatibility helpers that produce polygons;
``keypoints``/``pose`` produce point and line primitives.  Existing
``results[].box/quad/keypoints`` remain unchanged and can coexist with this
top-level ``geometry`` array.

## 常量与类型别名

以下为该版本源码值；默认地址不等于所有固件都开放该服务。

```python
PRIMITIVE_TYPES = frozenset(('point', 'line', 'polyline', 'polygon'))
```


```python
STYLE_FIELDS = frozenset(('color', 'line_width', 'point_radius', 'fill', 'fill_color', 'opacity'))
```


```python
MAX_GEOMETRY_ITEMS = 256
```


```python
MAX_POINTS_PER_PRIMITIVE = 256
```


```python
MAX_TOTAL_POINTS = 4096
```


```python
MAX_LABEL_CHARS = 128
```


```python
MAX_ID_CHARS = 64
```


## kit.geometry.GeometryError

```python
class GeometryError(ValueError)
```

A primitive cannot be represented by the bounded wire contract.

## kit.geometry.sanitize_style

```python
def sanitize_style(value: Any, *, strict: bool=False) -> dict[str, Any]
```

Return the safe style subset.

Hub ingress uses ``strict=False`` so one malformed optional style cannot
erase otherwise valid geometry; application builders use ``strict=True``
and fail early.  Unknown keys are never copied.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/geometry.py#L91)

## kit.geometry.primitive

```python
def primitive(kind: str, points: Sequence[Sequence[float]], *, style: Optional[Mapping[str, Any]]=None, id: Optional[str]=None, label: Optional[str]=None, score: Optional[float]=None, **style_values: Any) -> dict[str, Any]
```

Build one validated canonical primitive.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/geometry.py#L140)

## kit.geometry.point

```python
def point(x: float, y: float, **kwargs: Any) -> dict[str, Any]
```

构造点图元，输入点坐标和可选样式/标签，返回经过校验的规范 geometry 字典。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/geometry.py#L170)

## kit.geometry.line

```python
def line(a: Sequence[float], b: Sequence[float], **kwargs: Any) -> dict[str, Any]
```

构造恰有两个端点的线段图元并校验，返回规范 geometry 字典。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/geometry.py#L174)

## kit.geometry.polyline

```python
def polyline(points: Sequence[Sequence[float]], **kwargs: Any) -> dict[str, Any]
```

构造连续折线图元并校验点数、有限坐标及样式，返回规范字典。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/geometry.py#L178)

## kit.geometry.polygon

```python
def polygon(points: Sequence[Sequence[float]], **kwargs: Any) -> dict[str, Any]
```

构造闭合多边形图元并校验，返回规范字典；坐标空间由输出字段声明。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/geometry.py#L182)

## kit.geometry.box

```python
def box(xyxy: Sequence[float], **kwargs: Any) -> dict[str, Any]
```

Compatibility adapter: ``[x1,y1,x2,y2]`` -> polygon.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/geometry.py#L186)

## kit.geometry.quad

```python
def quad(points: Sequence[Sequence[float]], **kwargs: Any) -> dict[str, Any]
```

Compatibility adapter: four OCR/OBB points -> polygon.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/geometry.py#L197)

## kit.geometry.keypoints

```python
def keypoints(points: Sequence[Sequence[float]], *, labels: Optional[Sequence[str]]=None, conf_min: float=0.0, id_prefix: str='keypoint', **kwargs: Any) -> list[dict[str, Any]]
```

Compatibility adapter: keypoint tuples -> point primitives.

Input points may be ``[x,y]`` or ``[x,y,score]``.  Scores below
``conf_min`` are omitted.  IDs remain stable by input index.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/geometry.py#L204)

## kit.geometry.pose

```python
def pose(points: Sequence[Sequence[float]], skeleton: Iterable[Sequence[int]], *, conf_min: float=0.0, point_style: Optional[Mapping[str, Any]]=None, line_style: Optional[Mapping[str, Any]]=None, labels: Optional[Sequence[str]]=None, id_prefix: str='pose') -> list[dict[str, Any]]
```

Compatibility adapter: pose keypoints+skeleton -> lines then points.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/geometry.py#L234)

## kit.geometry.sanitize_geometry

```python
def sanitize_geometry(values: Any, *, space: str, allowed_types: Iterable[str]=PRIMITIVE_TYPES, max_items: int=64, max_points: int=128, default_style: Optional[Mapping[str, Any]]=None, frame_size: Optional[Sequence[int]]=None) -> list[dict[str, Any]]
```

Validate untrusted wire primitives and inject a trusted coordinate space.

Invalid primitives are omitted independently.  Unknown keys/styles are
discarded.  Pixel coordinates are bounded by ``frame_size`` when available;
without reference dimensions they remain diagnostic with ``space=unknown``.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/geometry.py#L277)

## kit.geometry.GeometryBuilder

```python
class GeometryBuilder
```

Small fluent accumulator accepted directly by :meth:`kit.app.App.emit`.

### kit.geometry.GeometryBuilder.__init__

```python
def __init__(self) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/geometry.py#L354)

### kit.geometry.GeometryBuilder.add

```python
def add(self, item: Mapping[str, Any]) -> 'GeometryBuilder'
```

校验并追加一个已构造图元，返回 builder 本身以便链式调用。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/geometry.py#L358)

### kit.geometry.GeometryBuilder.primitive

```python
def primitive(self, kind: str, points: Sequence[Sequence[float]], **kwargs: Any) -> 'GeometryBuilder'
```

按显式 kind 和参数构造/追加图元，返回 builder；非法 kind 或字段抛 GeometryError。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/geometry.py#L380)

### kit.geometry.GeometryBuilder.point

```python
def point(self, x: float, y: float, **kwargs: Any) -> 'GeometryBuilder'
```

追加点图元并返回 builder。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/geometry.py#L384)

### kit.geometry.GeometryBuilder.line

```python
def line(self, a: Sequence[float], b: Sequence[float], **kwargs: Any) -> 'GeometryBuilder'
```

追加线段图元并返回 builder。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/geometry.py#L387)

### kit.geometry.GeometryBuilder.polyline

```python
def polyline(self, points: Sequence[Sequence[float]], **kwargs: Any) -> 'GeometryBuilder'
```

追加折线图元并返回 builder。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/geometry.py#L390)

### kit.geometry.GeometryBuilder.polygon

```python
def polygon(self, points: Sequence[Sequence[float]], **kwargs: Any) -> 'GeometryBuilder'
```

追加多边形图元并返回 builder。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/geometry.py#L393)

### kit.geometry.GeometryBuilder.box

```python
def box(self, xyxy: Sequence[float], **kwargs: Any) -> 'GeometryBuilder'
```

追加 xyxy 矩形图元并返回 builder。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/geometry.py#L396)

### kit.geometry.GeometryBuilder.quad

```python
def quad(self, points: Sequence[Sequence[float]], **kwargs: Any) -> 'GeometryBuilder'
```

追加四边形图元并返回 builder，保留点序。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/geometry.py#L399)

### kit.geometry.GeometryBuilder.keypoints

```python
def keypoints(self, points: Sequence[Sequence[float]], **kwargs: Any) -> 'GeometryBuilder'
```

追加关键点及可选连接关系图元并返回 builder。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/geometry.py#L402)

### kit.geometry.GeometryBuilder.pose

```python
def pose(self, points: Sequence[Sequence[float]], skeleton: Iterable[Sequence[int]], **kwargs: Any) -> 'GeometryBuilder'
```

追加姿态图元并返回 builder；索引和连接关系必须与模型一致。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/geometry.py#L406)

### kit.geometry.GeometryBuilder.extend

```python
def extend(self, items: Iterable[Mapping[str, Any]]) -> 'GeometryBuilder'
```

逐项校验并追加图元序列，返回 builder。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/geometry.py#L411)

### kit.geometry.GeometryBuilder.build

```python
def build(self) -> list[dict[str, Any]]
```

返回规范图元列表，可交给 App.emit(geometry=...)；构造结果本身不会触发渲染。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/geometry.py#L416)

### kit.geometry.GeometryBuilder.__iter__

```python
def __iter__(self)
```

返回本对象定义的迭代器；迭代元素与借用有效期见类说明。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/geometry.py#L426)

### kit.geometry.GeometryBuilder.__len__

```python
def __len__(self) -> int
```

返回当前构建器中的基础图元数量。复合 box/pose 等可能展开为多个基础图元。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/geometry.py#L429)

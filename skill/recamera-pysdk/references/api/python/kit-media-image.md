# kit.media.image

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/media/image.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/media/image.py)；签名由 AST 提取，不导入硬件依赖。

RgaContext/ImageOps：NV12 转 RGB、resize、letterbox、crop。输入可借用 DMA，当前公开输出是 owned CPU RGB，并非端到端零拷贝。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

Stable public RGA image operations for borrowed NV12 dma-buf frames.

Only operations proven by the existing RV1126B librga shim are exposed:
NV12->RGB conversion, resize, letterbox, and crop+resize.  Rotation, blending,
drawing, fences, and destination dma-buf pools are intentionally absent until
their ABI and hardware behavior have device tests.

The API consumes a small structural protocol (public ``fd``, dimensions, and
producer-supplied plane descriptors), not a Rockchip ctypes structure.  The
returned buffers are owned CPU RGB arrays and remain valid after the source
frame lease is released.

## 常量与类型别名

以下为该版本源码值；默认地址不等于所有固件都开放该服务。

```python
NV12_FOURCC = 842094158
```


```python
ImageOps = RgaContext
```


## kit.media.image.Size

```python
@dataclass(frozen=True)
class Size
```

Positive image dimensions in pixels.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
width: int
height: int
```

## kit.media.image.Rect

```python
@dataclass(frozen=True)
class Rect
```

Half-open pixel rectangle ``[x1, y1, x2, y2)``.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
x1: int
y1: int
x2: int
y2: int
```

### kit.media.image.Rect.width

```python
@property
def width(self) -> int
```

返回 x2-x1 的矩形宽度。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/media/image.py#L62)

### kit.media.image.Rect.height

```python
@property
def height(self) -> int
```

返回 y2-y1 的矩形高度。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/media/image.py#L66)

### kit.media.image.Rect.as_tuple

```python
def as_tuple(self) -> tuple[int, int, int, int]
```

Return the tuple accepted by the native RGA shim.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/media/image.py#L69)

## kit.media.image.TransformMapping

```python
@dataclass(frozen=True)
class TransformMapping
```

Exact affine mapping between a source rectangle and output window.

Pixels outside ``output_rect`` are padding.  ``to_source`` is useful for
mapping model detections back to camera coordinates after letterbox/crop.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
source_size: Size
output_size: Size
source_rect: Rect
output_rect: Rect
```

### kit.media.image.TransformMapping.to_source

```python
def to_source(self, x: float, y: float) -> tuple[float, float]
```

Map one output-space point to source-camera pixels.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/media/image.py#L88)

### kit.media.image.TransformMapping.box_to_source

```python
def box_to_source(self, box: Sequence[float]) -> tuple[float, float, float, float]
```

Map an ``(x1,y1,x2,y2)`` output box to source-camera pixels.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/media/image.py#L98)

## kit.media.image.TransformResult

```python
@dataclass(frozen=True)
class TransformResult
```

Owned transform output plus its coordinate mapping.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
image: ImageBuffer
mapping: TransformMapping
```

## kit.media.image.RgaCapabilities

```python
@dataclass(frozen=True)
class RgaCapabilities
```

Operations supported by the loaded librga build.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
nv12_to_rgb: bool
resize: bool
crop: bool
zero_copy_source: bool = True
```

## kit.media.image.DmaBufFrame

```python
@runtime_checkable
class DmaBufFrame(Protocol)
```

Public subset required from ``recamera_ext.FrameLease``.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
fd: int
width: int
height: int
fourcc: int
planes: Sequence[Any]
buf_size: int
released: bool
```

## kit.media.image.RgaContext

```python
class RgaContext
```

Bound librga context with checked, typed NV12 operations.

Construct once per workflow and close it with a context manager.  The
current native shim owns no persistent hardware handles, so ``close`` only
invalidates this Python object; it is present now to keep the contract
compatible with future pooled/fenced implementations.

### kit.media.image.RgaContext.__init__

```python
def __init__(self, backend: Optional[Any]=None) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/media/image.py#L162)

### kit.media.image.RgaContext.convert_nv12

```python
def convert_nv12(self, frame: DmaBufFrame) -> ImageBuffer
```

Convert a borrowed NV12 dma-buf frame to owned RGB pixels.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/media/image.py#L359)

### kit.media.image.RgaContext.resize_nv12

```python
def resize_nv12(self, frame: DmaBufFrame, size: Size | tuple[int, int]) -> TransformResult
```

Resize a full NV12 frame to owned RGB, without preserving aspect.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/media/image.py#L389)

### kit.media.image.RgaContext.letterbox_nv12

```python
def letterbox_nv12(self, frame: DmaBufFrame, size: Size | tuple[int, int], *, pad_value: int=114) -> TransformResult
```

Aspect-preserving NV12 resize into an owned padded RGB canvas.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/media/image.py#L443)

### kit.media.image.RgaContext.crop_nv12

```python
def crop_nv12(self, frame: DmaBufFrame, rect: Rect | Sequence[int], size: Size | tuple[int, int], *, pad_value: int=114) -> TransformResult
```

Crop an NV12 source rectangle and resize it to owned RGB.

The current librga shim accepts a square destination.  A non-square
target is rejected explicitly instead of stretching or silently using
one dimension.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/media/image.py#L490)

### kit.media.image.RgaContext.close

```python
def close(self) -> None
```

Invalidate the context.  Safe to call repeatedly.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/media/image.py#L588)

### kit.media.image.RgaContext.__enter__

```python
def __enter__(self) -> 'RgaContext'
```

进入上下文管理器，返回其受管对象；与 __exit__ 配对使用，避免异常路径遗留资源。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/media/image.py#L594)

### kit.media.image.RgaContext.__exit__

```python
def __exit__(self, *_exc) -> None
```

离开上下文并执行本类的清理方法；异常传播/清理失败语义见类说明，不把退出视作任务已完成。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/media/image.py#L603)

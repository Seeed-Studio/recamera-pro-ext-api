# kit.buffer

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/buffer.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/buffer.py)；签名由 AST 提取，不导入硬件依赖。

CPU／DMA-BUF／其他后端图像缓冲描述与所有权。borrowed 不能跨源 lease 保存；owned copy 才能独立持有。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

Backend-neutral image-buffer contracts for AI workflows.

``ImageBuffer`` intentionally does not expose Rockchip C structures.  A buffer
may be an owned NumPy array or a short-lived view backed by a native frame
lease, but users interact through the same checked methods.  Releasing a
borrowed buffer invalidates every future access instead of returning stale
memory that may already have been reused by VI/RGA.

## kit.buffer.PixelFormat

```python
class PixelFormat(str, Enum)
```

Pixel formats supported by the public Python buffer contract.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
RGB = 'RGB'
BGR = 'BGR'
RGBA = 'RGBA'
BGRA = 'BGRA'
GRAY8 = 'GRAY8'
NV12 = 'NV12'
```

## kit.buffer.MemoryKind

```python
class MemoryKind(str, Enum)
```

Where the pixels live.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
CPU = 'cpu'
DMABUF = 'dmabuf'
BACKEND = 'backend'
```

## kit.buffer.Ownership

```python
class Ownership(str, Enum)
```

Whether the workflow owns the storage or temporarily borrows it.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
OWNED = 'owned'
BORROWED = 'borrowed'
```

## kit.buffer.PlaneLayout

```python
@dataclass(frozen=True)
class PlaneLayout
```

Byte layout of one image plane.

The values must come from the producer.  NV12 layout is never inferred from
width/height because Rockchip buffers may contain aligned stride/vstride.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
offset: int
stride: int
vstride: int
```

### kit.buffer.PlaneLayout.__iter__

```python
def __iter__(self)
```

返回本对象定义的迭代器；迭代元素与借用有效期见类说明。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/buffer.py#L79)

## kit.buffer.BufferBackend

```python
@runtime_checkable
class BufferBackend(Protocol)
```

Minimal protocol implemented by a native borrowed-buffer wrapper.

### kit.buffer.BufferBackend.map

```python
def map(self) -> Any
```

Map the current lease and return a buffer-protocol object.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/buffer.py#L88)

### kit.buffer.BufferBackend.release

```python
def release(self) -> None
```

Release the lease; the operation must be idempotent.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/buffer.py#L91)

### kit.buffer.BufferBackend.released

```python
@property
def released(self) -> bool
```

Whether the producer has invalidated this borrowed buffer.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/buffer.py#L95)

## kit.buffer.ImageBuffer

```python
class ImageBuffer
```

Checked image storage used by frame, RGA, and inference interfaces.

Construct CPU buffers with :meth:`from_numpy`.  Native adapters should use
:meth:`from_backend` and supply the exact plane descriptors received from
the producer.

Access rules:
  * ``numpy(copy=False)`` may return a view whose lifetime is this object.
  * ``copy()`` always returns independent, owned CPU storage.
  * after ``release()``, every data-bearing operation raises
    :class:`~kit.errors.BufferReleasedError`.

### kit.buffer.ImageBuffer.__init__

```python
def __init__(self, *, width: int, height: int, format: PixelFormat | str, memory: MemoryKind | str, ownership: Ownership | str, planes: Iterable[PlaneLayout | tuple[int, int, int]]=(), array: Optional[np.ndarray]=None, backend: Optional[BufferBackend]=None, release_callback: Optional[Callable[[], None]]=None) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/buffer.py#L139)

### kit.buffer.ImageBuffer.from_numpy

```python
@classmethod
def from_numpy(cls, array: np.ndarray, *, format: PixelFormat | str=PixelFormat.RGB, width: Optional[int]=None, height: Optional[int]=None, copy: bool=False) -> 'ImageBuffer'
```

Create an owned CPU buffer from an array.

``copy=False`` adopts the supplied array as application-owned storage;
it does not imply a native borrowed lease.  Use ``copy=True`` when the
producer may mutate or free the original array.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/buffer.py#L198)

### kit.buffer.ImageBuffer.from_backend

```python
@classmethod
def from_backend(cls, backend: BufferBackend, *, width: int, height: int, format: PixelFormat | str, planes: Iterable[PlaneLayout | tuple[int, int, int]], memory: MemoryKind | str=MemoryKind.DMABUF, release_callback: Optional[Callable[[], None]]=None) -> 'ImageBuffer'
```

Create a borrowed buffer around a native lease.

``planes`` is mandatory and is copied verbatim.  The high-level API
never guesses an aligned plane layout.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/buffer.py#L247)

### kit.buffer.ImageBuffer.released

```python
@property
def released(self) -> bool
```

Whether this object or its native producer invalidated the lease.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/buffer.py#L282)

### kit.buffer.ImageBuffer.owned

```python
@property
def owned(self) -> bool
```

Whether this object owns its storage.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/buffer.py#L302)

### kit.buffer.ImageBuffer.numpy

```python
def numpy(self, *, copy: bool=False) -> np.ndarray
```

Return pixels as a NumPy array.

Native backends may expose a one-dimensional mapped byte view; format-
specific reshaping remains the backend's responsibility.  Requesting a
copy is the portable way to keep data after the lease is released.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/buffer.py#L320)

### kit.buffer.ImageBuffer.copy

```python
def copy(self) -> 'ImageBuffer'
```

Return independent, owned CPU storage with the same pixel format.

A native backend is allowed to expose a flat raw mapping containing
aligned planes.  Such a mapping cannot be reconstructed as HWC without
guessing its format/stride, so the owned copy deliberately preserves
the original dimensions and producer plane descriptors while keeping
the copied NumPy array in its backend-provided shape.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/buffer.py#L348)

### kit.buffer.ImageBuffer.release

```python
def release(self) -> None
```

Invalidate this object and release a native lease exactly once.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/buffer.py#L372)

### kit.buffer.ImageBuffer.__enter__

```python
def __enter__(self) -> 'ImageBuffer'
```

进入上下文管理器，返回其受管对象；与 __exit__ 配对使用，避免异常路径遗留资源。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/buffer.py#L421)

### kit.buffer.ImageBuffer.__exit__

```python
def __exit__(self, exc_type, exc, traceback) -> bool
```

离开上下文并执行本类的清理方法；异常传播/清理失败语义见类说明，不把退出视作任务已完成。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/buffer.py#L425)

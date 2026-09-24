# recamera_ext.buffer

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[sdk/python/recamera_ext/buffer.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/buffer.py)；签名由 AST 提取，不导入硬件依赖。

Native BorrowedBuffer 与平面布局：源 lease 控制有效期，释放后访问报错，跨迭代持有必须 copy。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

Safe views over a borrowed native image buffer.

``BorrowedBuffer`` deliberately does not know about ctypes or Rockchip structs.
Its owner supplies three small callbacks (alive check, fd lookup, and full-buffer
mapping), while this module owns the public plane-layout and NumPy-view rules.
That separation keeps the buffer contract unit-testable without loading the
aarch64 shared library.

The object never owns the dma-buf.  Calling :meth:`release` delegates to the
owning :class:`recamera_ext.FrameLease`; advancing/closing the source may also
release it.  Every data-bearing operation checks the lease first, so a stale
``frame.array`` property access fails deterministically instead of returning a
new view over unmapped memory.  A NumPy view already retained by user code cannot
be revoked by Python; callers that need data beyond the lease must call
``copy()`` while it is alive.

## recamera_ext.buffer.PlaneLayout

```python
class PlaneLayout(NamedTuple)
```

One image plane's byte layout inside a shared buffer.

A ``NamedTuple`` is intentional: new code can use named attributes while old
callers can still unpack or index it exactly like the historical
``(offset, stride, vstride)`` tuple.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
offset: int
stride: int
vstride: int
```

## recamera_ext.buffer.BorrowedBuffer

```python
class BorrowedBuffer
```

A non-owning, lease-checked dma-buf view.

Parameters are copied metadata.  ``owner`` is expected to implement the
private callback surface used by :class:`recamera_ext.FrameLease`:
``_ensure_alive()``, ``_buffer_fd()``, ``_buffer_map()`` and ``release()``.
Holding only a weak reference avoids a ``lease -> buffer -> lease`` finalizer
cycle, so dropping the last frame reference can promptly return the native
buffer.

### recamera_ext.buffer.BorrowedBuffer.__init__

```python
def __init__(self, owner, *, size: int, width: int, height: int, fourcc: int, planes: Sequence[PlaneLayout]) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/buffer.py#L63)

### recamera_ext.buffer.BorrowedBuffer.size

```python
@property
def size(self) -> int
```

Total mapped byte length reported by the native producer.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/buffer.py#L81)

### recamera_ext.buffer.BorrowedBuffer.width

```python
@property
def width(self) -> int
```

Valid image width; plane stride may be larger.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/buffer.py#L87)

### recamera_ext.buffer.BorrowedBuffer.height

```python
@property
def height(self) -> int
```

Valid image height; plane vstride may be larger.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/buffer.py#L93)

### recamera_ext.buffer.BorrowedBuffer.fourcc

```python
@property
def fourcc(self) -> int
```

Producer-supplied V4L2 fourcc value.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/buffer.py#L99)

### recamera_ext.buffer.BorrowedBuffer.planes

```python
@property
def planes(self)
```

Immutable tuple of producer-supplied :class:`PlaneLayout` values.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/buffer.py#L105)

### recamera_ext.buffer.BorrowedBuffer.released

```python
@property
def released(self) -> bool
```

Whether the owning frame lease has already been released.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/buffer.py#L122)

### recamera_ext.buffer.BorrowedBuffer.fd

```python
@property
def fd(self) -> int
```

Borrowed dma-buf fd, valid only until release.

The fd must not be closed by Python code and must not be cached for use
after the lease.  Use it synchronously with RGA/GStreamer while the frame
is alive.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/buffer.py#L129)

### recamera_ext.buffer.BorrowedBuffer.map

```python
def map(self)
```

Return a zero-copy 1-D ``uint8`` view of the complete buffer.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/buffer.py#L139)

### recamera_ext.buffer.BorrowedBuffer.plane_array

```python
def plane_array(self, index: int)
```

Return plane ``index`` as a zero-copy ``(vstride, stride)`` view.

Plane dimensions come exclusively from the server-provided descriptor;
they are never inferred from image width/height.  The complete described
byte range is validated before constructing the strided view.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/buffer.py#L144)

### recamera_ext.buffer.BorrowedBuffer.copy

```python
def copy(self)
```

Copy the entire mapped buffer into independently owned memory.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/buffer.py#L181)

### recamera_ext.buffer.BorrowedBuffer.release

```python
def release(self) -> bool
```

Release the owning frame; returns ``True`` only on the first release.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/buffer.py#L186)

### recamera_ext.buffer.BorrowedBuffer.__enter__

```python
def __enter__(self)
```

进入上下文管理器，返回其受管对象；与 __exit__ 配对使用，避免异常路径遗留资源。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/buffer.py#L192)

### recamera_ext.buffer.BorrowedBuffer.__exit__

```python
def __exit__(self, exc_type, exc_value, _traceback)
```

离开上下文并执行本类的清理方法；异常传播/清理失败语义见类说明，不把退出视作任务已完成。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/sdk/python/recamera_ext/buffer.py#L196)

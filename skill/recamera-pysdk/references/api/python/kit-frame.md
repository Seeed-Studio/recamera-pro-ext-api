# kit.frame

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/frame.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/frame.py)；签名由 AST 提取，不导入硬件依赖。

后端无关 Kit Frame，pts 为秒、pts_us 为微秒；与 recamera_ext.FrameLease 不同。copy 会脱离原 DMA 所有权。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

Canonical high-level frame object for reCamera AI workflows.

## 公开导出 / 别名

| 导入名称 | 定义与完整说明 |
| --- | --- |
| `ImageBuffer` | [kit.buffer.ImageBuffer](kit-buffer.md) |
| `PixelFormat` | [kit.buffer.PixelFormat](kit-buffer.md) |

## kit.frame.Frame

```python
class Frame
```

One image and its capture metadata.

The legacy constructor remains supported::

    Frame(data, width, height, "RGB", monotonic_seconds)

New code may pass ``buffer=ImageBuffer(...)`` and an exact integer
``pts_us``.  ``w``/``h`` describe the original camera coordinate space;
optimized preprocessors may place a model-sized image in ``data`` while
retaining the original geometry for result mapping.

``model_info``, ``model_data``, and ``roi_cropper`` are compatibility fields
used by the existing applications.  They will be superseded by a typed
transform result, but are not removed in this compatibility release.

### kit.frame.Frame.__init__

```python
def __init__(self, data: Optional[np.ndarray]=None, w: Optional[int]=None, h: Optional[int]=None, fmt: Optional[str]=None, pts: Optional[float]=None, model_info: object=None, model_data: object=None, roi_cropper: object=None, *, buffer: Optional[ImageBuffer]=None, pts_us: Optional[int]=None, metadata: Optional[Mapping[str, Any]]=None) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/frame.py#L34)

### kit.frame.Frame.data

```python
@property
def data(self) -> np.ndarray
```

Return the current image as a NumPy view.

Access after :meth:`release` raises ``BufferReleasedError``.  Call
``frame.copy()`` when pixels must outlive a borrowed source iteration.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/frame.py#L139)

### kit.frame.Frame.owned

```python
@property
def owned(self) -> bool
```

Whether the frame owns its image storage.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/frame.py#L149)

### kit.frame.Frame.released

```python
@property
def released(self) -> bool
```

Whether the underlying image buffer has been released.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/frame.py#L155)

### kit.frame.Frame.copy

```python
def copy(self) -> 'Frame'
```

Return an owned frame safe to retain after the source advances.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/frame.py#L160)

### kit.frame.Frame.release

```python
def release(self) -> None
```

Release/invalidate the underlying buffer; safe to call repeatedly.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/frame.py#L189)

### kit.frame.Frame.__enter__

```python
def __enter__(self) -> 'Frame'
```

进入上下文管理器，返回其受管对象；与 __exit__ 配对使用，避免异常路径遗留资源。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/frame.py#L198)

### kit.frame.Frame.__exit__

```python
def __exit__(self, exc_type, exc, traceback) -> None
```

离开上下文并执行本类的清理方法；异常传播/清理失败语义见类说明，不把退出视作任务已完成。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/frame.py#L203)

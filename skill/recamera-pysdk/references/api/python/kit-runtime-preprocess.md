# kit.runtime.preprocess

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/runtime/preprocess.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/preprocess.py)；签名由 AST 提取，不导入硬件依赖。

CPU 图像读取、letterbox 与模型输入构造；返回映射信息，RGB/BGR 和归一化由模型约定决定。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

Preprocessing for reCamera Pro (RV1126B) YOLO detection kit.

Pure numpy + PIL (no OpenCV). Because normalization (mean=0, std=255) is baked
into the RKNN model at convert time, this module returns RAW uint8 RGB pixels
letterboxed to the network input size. DO NOT divide by 255 here.

## kit.runtime.preprocess.LetterboxInfo

```python
@dataclass
class LetterboxInfo
```

Parameters needed to map boxes from network space back to the original image.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
scale: float
pad_w: float
pad_h: float
orig_w: int
orig_h: int
```

## kit.runtime.preprocess.load_image

```python
def load_image(path: str) -> np.ndarray
```

Load an image file as an HWC uint8 RGB numpy array.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/preprocess.py#L35)

## kit.runtime.preprocess.letterbox

```python
def letterbox(img: np.ndarray, new_shape: int | Tuple[int, int]=640, color: int=114) -> Tuple[np.ndarray, LetterboxInfo]
```

Resize + pad an HWC uint8 RGB image to `new_shape`, preserving aspect ratio.

Returns (padded_uint8_HWC, LetterboxInfo). Pure numpy nearest/bilinear-free
resize via PIL when available (higher quality), else numpy nearest-neighbour.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/preprocess.py#L43)

## kit.runtime.preprocess.preprocess

```python
def preprocess(path_or_array, new_shape: int | Tuple[int, int]=640)
```

Convenience: load (if a path) + letterbox.

Returns (input_uint8_1HWC, LetterboxInfo). The array is shaped [1, H, W, 3]
ready to hand to RknnModel.infer().

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/preprocess.py#L83)

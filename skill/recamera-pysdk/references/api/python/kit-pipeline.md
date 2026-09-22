# kit.pipeline

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/pipeline.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/pipeline.py)；签名由 AST 提取，不导入硬件依赖。

检测→ROI→第二模型的级联工具；不是 kit.workflow.Pipeline。返回精确 ROI 映射，CPU 路径需 NumPy/PIL 或 OpenCV。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

Cascade (two-stage) pipeline scaffold for reCamera Pro. Pure numpy + PIL.

This is the shared skeleton for the "detect -> crop ROI -> second model" family
of apps (facemesh-reader, and later face-analysis / anything that runs a
per-object second network). The generic Kit base loop (kit.app.App) already
does stage-1 (letterbox 640 -> detector -> boxes in ORIGINAL-frame pixels). An
app then, inside run(), hands the full frame + stage-1 boxes to a
CascadePipeline which:

    for each target box (top-K by score):
        1. crop a padded SQUARE ROI around the box from the ORIGINAL frame,
           edge-padding when the square runs past the frame border,
        2. resize the ROI to the second model's input size (e.g. 192),
        3. run the second RKNN model on the raw uint8 ROI,
        4. decode its outputs and map results back to ORIGINAL-frame pixels
           via the exact crop transform (ox, oy, sx, sy).

Coordinate mapping (kept linear & exact for the integer crop actually taken):

    original_x = ox + roi_x * sx
    original_y = oy + roi_y * sy

    where (ox, oy) is the top-left of the integer square in frame pixels and
    (sx, sy) = crop_side / model_input_side.

The square-crop + center-pad + resize mirrors the first-gen C++
FacemeshPipeline::cropAndResize so landmark geometry matches the reference.

## 常量与类型别名

以下为该版本源码值；默认地址不等于所有固件都开放该服务。

```python
RoiMap = Tuple[float, float, float, float]
```


```python
SquareGeometry = Tuple[RoiMap, Optional[Tuple[int, int, int, int]], Optional[Tuple[int, int, int, int]], Tuple[int, int, int]]
```


## kit.pipeline.square_roi_geometry

```python
def square_roi_geometry(frame_h: int, frame_w: int, box: Sequence[float], out_size: int, pad: float=0.25) -> SquareGeometry
```

Compute the padded-centered-square crop geometry for `box`.

This is the ONE place the "pad the box, square it around its center, round to
integers" math lives.  Both the numpy crop (`crop_square_roi`) and the
hardware dma-buf crop (kit.adapters.official's RGA ROI path) consume it, so
the two produce byte-for-byte identical `roi_map`s (hence identical
coordinate mapping back to original-frame pixels) even though they fill the
out-of-frame margin differently (numpy edge-replicates, RGA gray-fills).

Returns a `SquareGeometry` tuple -- see the constant above for the fields.
The `roi_map` is `(ix1, iy1, iside/out_size, iside/out_size)`: the full
integer square's top-left and its (isotropic) frame-px-per-output-px scale.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/pipeline.py#L57)

## kit.pipeline.crop_square_roi

```python
def crop_square_roi(frame: np.ndarray, box: Sequence[float], out_size: int, pad: float=0.25) -> Tuple[np.ndarray, RoiMap]
```

Cut a padded, centered SQUARE ROI around `box` and resize to out_size.

frame : HWC uint8 RGB (original frame).
box   : [x1,y1,x2,y2] in original-frame pixels.
Returns (roi_uint8 [out_size,out_size,3], roi_map for coordinate mapping).

The square geometry is delegated to `square_roi_geometry` so the numpy crop
and the hardware dma-buf crop share one contract.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/pipeline.py#L115)

## kit.pipeline.perspective_crop

```python
def perspective_crop(frame: np.ndarray, quad, pad_v: float=0.12, pad_h: float=0.06) -> np.ndarray
```

Warp a detected text quad out of the frame into an upright text strip.

Port of the first-gen C++ OcrPipeline::cropTextRegion. `quad` is 4 points
ordered TL,TR,BR,BL in ORIGINAL-frame pixels. Returns an HWC uint8 RGB crop
at the quad's natural size (rotated upright if it reads vertical). Feed the
result to `fit_rec_input` before the rec model.

cv2 (getPerspectiveTransform/warpPerspective) is used; it is present on the
device system python (opencv 4.6.0) so no extra dependency is bundled.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/pipeline.py#L160)

## kit.pipeline.fit_rec_input

```python
def fit_rec_input(crop: np.ndarray, out_h: int=48, out_w: int=320, pad_value: int=128) -> np.ndarray
```

Resize a text crop to the rec model input (48x320), PP-OCR style.

Scale to height `out_h` keeping aspect ratio, clamp width to `out_w`, then
right-pad with gray (`pad_value`, maps to ~0 after the baked [-1,1] norm).
Returns HWC uint8 RGB [out_h, out_w, 3]. Port of TextRecognizer::preprocess.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/pipeline.py#L223)

## kit.pipeline.CascadePipeline

```python
class CascadePipeline
```

Stage-2 model runner: crop ROI per stage-1 box, infer, decode, map back.

decode_fn(outputs, roi_map, input_size) -> app-defined decoded object
(e.g. kit.runtime.postprocess.landmark.decode returns (landmarks, presence)).
Kept model-agnostic so face-analysis can reuse the same scaffold with a
different second model + decode_fn.

### kit.pipeline.CascadePipeline.__init__

```python
def __init__(self, model_path: Optional[str]=None, input_size: int=192, decode_fn: Optional[Callable]=None, pad: float=0.25, max_targets: int=1, model: Any=None)
```

`model_path` loads its own RKNN; `model` adopts an ALREADY-loaded one.

The new app shape (KIT_APP_SHAPE_SPEC §2) preloads every manifest
`models[]` entry into `App.models`, so a migrated cascade app passes
`model=self.models.<id>` and this class never loads (nor releases) a
second copy of the same rknn. Exactly one of the two must be given.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/pipeline.py#L260)

### kit.pipeline.CascadePipeline.process

```python
def process(self, frame_data: np.ndarray, detections: List[dict]) -> List[dict]
```

Run stage-2 on the top-`max_targets` detections.

Returns a list of {"box","score","decoded","roi_map"} dicts, one per
processed detection (detections are assumed already score-sorted).

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/pipeline.py#L288)

### kit.pipeline.CascadePipeline.release

```python
def release(self) -> None
```

Release the stage-2 model -- only if this pipeline loaded it itself.

A pipeline built with `model=<App.models handle>` does NOT own the
model; `App.finish()` releases it once, and releasing here too would be
a double free.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/pipeline.py#L309)

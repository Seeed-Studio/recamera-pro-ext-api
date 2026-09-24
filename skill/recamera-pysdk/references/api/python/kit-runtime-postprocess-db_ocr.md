# kit.runtime.postprocess.db_ocr

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/runtime/postprocess/db_ocr.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/postprocess/db_ocr.py)；签名由 AST 提取，不导入硬件依赖。

DB OCR 文本检测解码：二值化、轮廓、置信过滤和扩框；依赖 OpenCV。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

DBNet (PP-OCR text detection) post-processing for reCamera Pro.

Port of the first-gen C++ `TextDetector::postprocess` /
`unclipPolygon` / `orderBoxPointsTLTRBRBL`
(sscma-example-sg200x/solutions/ppocr-reader/main/text_detector.cpp) to Python.

The DB detector rknn takes a letterboxed uint8 RGB frame (ImageNet mean/std
baked in at convert time) and emits a single (1,1,H,W) sigmoid probability map
(H=W=480 for PP-OCRv3 det). We:

    1. threshold the map -> binary mask,
    2. findContours -> minAreaRect -> 4 corner points (map/input space),
    3. score each box by mean probability inside its contour,
    4. unclip (dilate) the quad outward (PaddleOCR db_unclip_ratio),
    5. map the quad from letterbox(480) space back to ORIGINAL-frame pixels
       using the kit LetterboxInfo,
    6. order the 4 points TL -> TR -> BR -> BL.

cv2 (findContours / minAreaRect) is used here and for the perspective crop --
it is already present on the device system python (opencv 4.6.0), so NO new
dependency is bundled. unclip uses the pure-numpy edge-offset method from the
reference (no pyclipper needed).

## 常量与类型别名

以下为该版本源码值；默认地址不等于所有固件都开放该服务。

```python
DEFAULT_MIN_SIZE = 3.0
```


## kit.runtime.postprocess.db_ocr.decode

```python
def decode(outputs, info, *, det_thresh: float=0.3, box_thresh: float=0.5, unclip_ratio: float=2.0, min_size: float=DEFAULT_MIN_SIZE, max_boxes: int=32) -> List[dict]
```

Decode the DB probability map into text-box quads in ORIGINAL pixels.

outputs : raw rknn outputs (list); outputs[0] is the (1,1,H,W) prob map.
info    : kit.runtime.preprocess.LetterboxInfo (scale/pad_w/pad_h/orig_w/h).
Returns a list of {"quad": [[x,y]x4], "score": float}, score-descending,
each quad ordered TL,TR,BR,BL and clipped to the original frame.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/postprocess/db_ocr.py#L117)

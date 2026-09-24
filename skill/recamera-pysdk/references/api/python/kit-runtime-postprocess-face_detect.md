# kit.runtime.postprocess.face_detect

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/runtime/postprocess/face_detect.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/postprocess/face_detect.py)；签名由 AST 提取，不导入硬件依赖。

人脸检测专用后处理；按该导出的张量形状解析，不能直接代替通用 YOLO decoder。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

Single-class YOLOv8-face detection post-processing for reCamera Pro. Pure numpy.

This is a thin specialisation of the generic YOLOv8/11 DFL decoder in
`detect.py` for the 1-class face detector (yolov8n-face rawhead):

    per FPN stride s in {8, 16, 32}:
        box branch  [1, 64, {80,40,20}^2]   -> DFL(reg_max=16) -> l,t,r,b
        cls branch  [1,  1, {80,40,20}^2]    -> face score (single class)

The rawhead export carries NO keypoints (box-only), so `detect._decode_dfl`
already handles this layout verbatim when `nc=1`: it pairs a 64-channel box
branch with a 1-channel class branch per stride and runs the same "score-first,
DFL-decode-survivors" path. We keep a dedicated entry point so the cascade
pipeline / face apps read clearly and get a face-shaped result:

    [{ "box":[x1,y1,x2,y2], "score":float }]   (boxes in ORIGINAL-image pixels)

## kit.runtime.postprocess.face_detect.postprocess

```python
def postprocess(outputs, info, conf_thres: float=0.5, iou_thres: float=0.45, input_size: int=640) -> List[dict]
```

Decode the 6-tensor yolov8n-face rawhead into face boxes.

outputs : list of raw RKNN tensors (3x box-DFL [1,64,g,g] + 3x cls [1,1,g,g]).
info    : preprocess.LetterboxInfo (scale + padding for un-letterboxing).
Returns face dicts sorted by score descending, boxes in original-frame px.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/postprocess/face_detect.py#L28)

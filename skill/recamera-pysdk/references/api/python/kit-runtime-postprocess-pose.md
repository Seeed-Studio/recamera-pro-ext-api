# kit.runtime.postprocess.pose

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/runtime/postprocess/pose.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/runtime/postprocess/pose.py)；签名由 AST 提取，不导入硬件依赖。

YOLO pose 框和关键点解码；关键点数量/索引与模型一致。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

YOLO-pose (YOLOv8-pose / YOLO11-pose) post-processing for reCamera Pro. Pure numpy.

Mirrors the "filter first, decode second" style of detect.py. Handles the raw
multi-branch head produced by extracting the 9 leaf Conv outputs before the
in-graph concat/decode (see models/convert/export_pose.py):

    per FPN stride s in {8, 16, 32}:
        box branch  [1, 64, H, W]   -> DFL(reg_max=16) -> l,t,r,b distances
        cls branch  [1,  1, H, W]   -> person score (single class)
        kpt branch  [1, 51, H, W]   -> 17 keypoints, each (x, y, conf)

Keypoint decode (ultralytics convention):
    kx = (raw_x * 2.0 + (gx - 0.5)) * stride
    ky = (raw_y * 2.0 + (gy - 0.5)) * stride
    kc = sigmoid(raw_conf)

Output: list of dicts
    {box:[x1,y1,x2,y2], score:float, keypoints:[[x,y,conf] * 17]}
with boxes AND keypoints mapped back to the ORIGINAL image via LetterboxInfo.

## 常量与类型别名

以下为该版本源码值；默认地址不等于所有固件都开放该服务。

```python
N_KPT = 17
```


## kit.runtime.postprocess.pose.postprocess

```python
def postprocess(outputs, info, conf_thres: float=0.4, iou_thres: float=0.45, input_size: int=640, kpt_thres: float=0.5)
```

outputs : list of raw RKNN output tensors (9 branches).
info    : preprocess.LetterboxInfo (scale + padding for un-letterboxing).
Returns list of person dicts sorted by score descending:
    {box:[x1,y1,x2,y2], score, keypoints:[[x,y,conf]*17]}
Boxes and keypoints are in ORIGINAL-image pixel coordinates. Keypoints
below `kpt_thres` keep their coordinates but the caller should treat their
confidence as the visibility gate.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/runtime/postprocess/pose.py#L102)

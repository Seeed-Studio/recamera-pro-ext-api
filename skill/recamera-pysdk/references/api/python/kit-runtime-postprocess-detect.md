# kit.runtime.postprocess.detect

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/runtime/postprocess/detect.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/runtime/postprocess/detect.py)；签名由 AST 提取，不导入硬件依赖。

YOLO 检测 DFL/head 解码与 NMS，输出框映回原图；支持配置输入尺寸/类别，不能默认所有模型均为 COCO 640。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

YOLOv8 / YOLO11 detection post-processing for reCamera Pro. Pure numpy.

Handles two RKNN output layouts automatically:

1. Concatenated & decoded head  -> a single tensor [1, 84, 8400]
   (4 box coords already regressed to pixel xywh + 80 class scores).
   This is what ultralytics' default ONNX export produces; the DFL
   integral is baked into the graph. We only reshape, threshold, NMS.

2. Raw multi-branch head -> several tensors, per FPN stride, split into a
   box-distribution branch ([1, 64, H, W], reg_max=16) and a class branch
   ([1, 80, H, W]). Here we perform the DFL decode ourselves:
   softmax over the 16 bins per side, expectation -> distance (l,t,r,b),
   grid decode -> xyxy.

Output: list of dicts {box:[x1,y1,x2,y2], cls:int, cls_name:str, score:float}
with boxes mapped back to the ORIGINAL image via LetterboxInfo.

## 常量与类型别名

以下为该版本源码值；默认地址不等于所有固件都开放该服务。

```python
COCO80 = ['person', 'bicycle', 'car', 'motorcycle', 'airplane', 'bus', 'train', 'truck', 'boat', 'traffic light', 'fire hydrant', 'stop sign', 'parking meter', 'bench', 'bird', 'cat', 'dog', 'horse', 'sheep', 'cow', 'elephant', 'bear', 'zebra', 'giraffe', 'backpack', 'umbrella', 'handbag', 'tie', 'suitcase', 'frisbee', 'skis', 'snowboard', 'sports ball', 'kite', 'baseball bat', 'baseball glove', 'skateboard', 'surfboard', 'tennis racket', 'bottle', 'wine glass', 'cup', 'fork', 'knife', 'spoon', 'bowl', 'banana', 'apple', 'sandwich', 'orange', 'broccoli', 'carrot', 'hot dog', 'pizza', 'donut', 'cake', 'chair', 'couch', 'potted plant', 'bed', 'dining table', 'toilet', 'tv', 'laptop', 'mouse', 'remote', 'keyboard', 'cell phone', 'microwave', 'oven', 'toaster', 'sink', 'refrigerator', 'book', 'clock', 'vase', 'scissors', 'teddy bear', 'hair drier', 'toothbrush']
```


## kit.runtime.postprocess.detect.nms

```python
def nms(boxes: np.ndarray, scores: np.ndarray, iou_thres: float) -> List[int]
```

Standard greedy NMS on xyxy boxes. Returns kept indices.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/runtime/postprocess/detect.py#L53)

## kit.runtime.postprocess.detect.postprocess

```python
def postprocess(outputs, info, conf_thres: float=0.25, iou_thres: float=0.45, input_size: int=640, class_names: Sequence[str]=COCO80)
```

outputs : list of raw RKNN output tensors.
info    : preprocess.LetterboxInfo (scale + padding for un-letterboxing).
Returns list of detection dicts, sorted by score descending.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/runtime/postprocess/detect.py#L178)

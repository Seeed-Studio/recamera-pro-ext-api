# kit.runtime.postprocess.landmark

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/runtime/postprocess/landmark.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/postprocess/landmark.py)；签名由 AST 提取，不导入硬件依赖。

人脸 landmark/face score 解码，按第二阶段 ROI 映射回原画面。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

FaceMesh 468-point landmark post-processing for reCamera Pro. Pure numpy.

Second stage of the face cascade: the face_landmark model runs on a 192x192
face ROI and emits

    out0 : (1,1,1,1404)  -> 468 landmarks, each (x, y, z)
    out1 : (1,1,1,1)     -> face-presence logit

MediaPipe FaceMesh convention (matches the first-gen C++ port): landmark x, y
are in INPUT-IMAGE pixel space (0..192 for a 192 input), z is a relative depth
in the same scale. This module reshapes the flat 1404 tensor, then maps x, y
from ROI/192 space back to ORIGINAL-frame pixels using the crop transform the
pipeline recorded when it cut the ROI (see kit.pipeline.crop_square_roi).

    orig_x = roi_ox + lm_x * roi_sx
    orig_y = roi_oy + lm_y * roi_sy

Output: (landmarks_xyz float32 [468,3] in original-frame px, presence float).

## 常量与类型别名

以下为该版本源码值；默认地址不等于所有固件都开放该服务。

```python
N_LMK = 468
```


## kit.runtime.postprocess.landmark.decode

```python
def decode(outputs, roi_map, input_size: int=192)
```

Decode raw landmark tensors into original-frame coordinates.

outputs  : list of raw RKNN output tensors.
roi_map  : (ox, oy, sx, sy) crop transform from kit.pipeline.crop_square_roi
           such that original = (ox + lm_x*sx, oy + lm_y*sy).
input_size : landmark model input side (192).

Returns (landmarks float32 [468,3], presence float in [0,1]).

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/postprocess/landmark.py#L52)

# kit.logic.geometry

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/logic/geometry.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/geometry.py)；签名由 AST 提取，不导入硬件依赖。

姿态和区域几何工具；区分归一化跟踪坐标与原图像素输入，阈值/可见性随模型契约配置。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

COCO-17 keypoint semantics + geometric helpers for pose apps. Pure numpy/math.

Ported from the first-gen fall-detection C++ (main/pose.{h,cpp} + makeObservation
in main.cpp). Keep all keypoint access through the named indices below -- raw
integer indices are easy to misread when a model uses a different landmark
convention (MediaPipe 11 == left shoulder, COCO 11 == left hip).

## 常量与类型别名

以下为该版本源码值；默认地址不等于所有固件都开放该服务。

```python
NOSE = 0
```


```python
LEFT_EYE = 1
```


```python
RIGHT_EYE = 2
```


```python
LEFT_EAR = 3
```


```python
RIGHT_EAR = 4
```


```python
LEFT_SHOULDER = 5
```


```python
RIGHT_SHOULDER = 6
```


```python
LEFT_ELBOW = 7
```


```python
RIGHT_ELBOW = 8
```


```python
LEFT_WRIST = 9
```


```python
RIGHT_WRIST = 10
```


```python
LEFT_HIP = 11
```


```python
RIGHT_HIP = 12
```


```python
LEFT_KNEE = 13
```


```python
RIGHT_KNEE = 14
```


```python
LEFT_ANKLE = 15
```


```python
RIGHT_ANKLE = 16
```


```python
N_KPT = 17
```


```python
Point = Tuple[float, float]
```


```python
COCO_SKELETON: List[Tuple[int, int]] = [(LEFT_ANKLE, LEFT_KNEE), (LEFT_KNEE, LEFT_HIP), (RIGHT_ANKLE, RIGHT_KNEE), (RIGHT_KNEE, RIGHT_HIP), (LEFT_HIP, RIGHT_HIP), (LEFT_SHOULDER, LEFT_HIP), (RIGHT_SHOULDER, RIGHT_HIP), (LEFT_SHOULDER, RIGHT_SHOULDER), (LEFT_SHOULDER, LEFT_ELBOW), (RIGHT_SHOULDER, RIGHT_ELBOW), (LEFT_ELBOW, LEFT_WRIST), (RIGHT_ELBOW, RIGHT_WRIST), (NOSE, LEFT_EYE), (NOSE, RIGHT_EYE), (LEFT_EYE, LEFT_EAR), (RIGHT_EYE, RIGHT_EAR)]
```


## kit.logic.geometry.visible

```python
def visible(kpts: Sequence[Sequence[float]], j: int, thres: float) -> bool
```

判断给定关键点置信度是否满足门限，返回 bool；输入关键点布局必须匹配调用约定。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/geometry.py#L37)

## kit.logic.geometry.midpoint

```python
def midpoint(kpts: Sequence[Sequence[float]], a: int, b: int, thres: float) -> Optional[Point]
```

Midpoint of joints a,b. If both visible -> average; if only one -> that
one; if neither -> None. Mirrors the first-gen `midpoint()` helper.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/geometry.py#L41)

## kit.logic.geometry.torso_angle_deg

```python
def torso_angle_deg(shoulders: Point, hips: Point) -> Optional[float]
```

Angle of the torso away from vertical, in degrees (0 upright, 90 flat).

atan2(|dx|, |dy|) where d = hips - shoulders. Returns None if the two
midpoints coincide (would be a meaningless angle).

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/geometry.py#L56)

## kit.logic.geometry.Observation

```python
class Observation
```

One frame's fall features for a single subject (mirrors FallObservation).

Coordinates normalised to the inference frame; hip_y increases downward,
torso_angle_deg is degrees from vertical, aspect = box_w / box_h.

### kit.logic.geometry.Observation.__init__

```python
def __init__(self, timestamp_sec: float)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/geometry.py#L80)

## kit.logic.geometry.make_observation

```python
def make_observation(person: Optional[dict], timestamp_sec: float, frame_h: int, kpt_thres: float) -> Observation
```

Build an Observation from a pose result dict (box + keypoints in pixels).

`person` is one entry of kit.runtime.postprocess.pose output, or None when no
subject was detected (-> invalid observation, lets a suspicion expire).
`frame_h` is the ORIGINAL frame height (pixels) used to normalise hip_y.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/geometry.py#L89)

## kit.logic.geometry.joint_angle

```python
def joint_angle(a: Optional[Point], b: Optional[Point], c: Optional[Point]) -> Optional[float]
```

Interior angle at vertex b, in degrees, range [0,180].

Ported from the first-gen fitness-trainer `jointAngle` (main/pose.cpp).
Returns None ( == the C++ NaN) when either limb has zero length
(coincident keypoints) or an input is missing -- callers MUST treat that as
"no reading" rather than as 0 degrees.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/geometry.py#L125)

## kit.logic.geometry.point

```python
def point(kpts: Sequence[Sequence[float]], j: int) -> Optional[Point]
```

(x, y) of joint j, or None if the index is out of range.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/geometry.py#L147)

## kit.logic.geometry.side_score

```python
def side_score(kpts: Sequence[Sequence[float]], joints: Sequence[int], thres: float) -> float
```

Mean confidence over `joints`; 0.0 when ANY joint is below `thres`.

Ported from first-gen `Pose::sideScore` -- used to pick the better-facing
side of a two-sided exercise.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/geometry.py#L154)

## kit.logic.geometry.point_in_polygon

```python
def point_in_polygon(px: float, py: float, poly: Sequence[Sequence[float]]) -> bool
```

Ray-casting (crossing-number) point-in-polygon; handles non-convex
polygons. Points exactly on an edge may fall on either side -- acceptable
for occupancy counting. `poly` is a sequence of (x, y). <3 points -> False.
Faithful port of retail_vision::geom::point_in_polygon.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/geometry.py#L175)

## kit.logic.geometry.line_side

```python
def line_side(ax: float, ay: float, bx: float, by: float, px: float, py: float) -> float
```

Signed side of point p relative to the directed line a -> b (2-D cross
product). > 0 : p is LEFT of a->b, < 0 : RIGHT, == 0 : collinear.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/geometry.py#L196)

## kit.logic.geometry.segment_crossing

```python
def segment_crossing(ax: float, ay: float, bx: float, by: float, p0x: float, p0y: float, p1x: float, p1y: float) -> int
```

Did the movement segment p0 -> p1 cross the finite segment a -> b?

Returns 0 (no crossing), +1 (crossed from the LEFT of a->b to the RIGHT),
or -1 (RIGHT -> LEFT). Requires BOTH segments to strictly straddle each
other, so touching an endpoint or moving parallel past the line does not
count. Faithful port of retail_vision::geom::segment_crossing -- the sign
convention (left->right = +1) is what LineCounter's `ab_in` keys off.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/geometry.py#L203)

## kit.logic.geometry.iou_xyxy

```python
def iou_xyxy(a: Sequence[float], b: Sequence[float]) -> float
```

IoU of two axis-aligned boxes in [x1,y1,x2,y2] form (any consistent
unit). Returns 0.0 for non-overlapping or degenerate boxes.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/geometry.py#L224)

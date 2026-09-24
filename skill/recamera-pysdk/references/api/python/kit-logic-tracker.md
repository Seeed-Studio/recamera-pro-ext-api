# kit.logic.tracker

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/logic/tracker.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/tracker.py)；签名由 AST 提取，不导入硬件依赖。

轻量 IoU 跟踪；track ID 为此跟踪器生命周期内身份，不能当永久人员身份。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

Multi-object tracker (IoU association + velocity prediction + track lifecycle).

Ported from the first-gen retail-vision C++ `person_tracker.cpp`
(PersonTracker::update / matchDetections / updateVelocity). Model-free and
app-agnostic: it consumes plain detection boxes (pixel xyxy) and emits stable
per-object `Track`s with an id, a velocity estimate and a foot point, frame over
frame. Zone / line / dwell counting lives in `kit.logic.zones`; this module only
owns identity and motion so any "detect + track + count" app reuses it.

Boxes are normalised to [0,1] internally (centre form cx,cy,w,h) so the same
IoU / distance thresholds behave the same at any capture resolution. `speed_px_s`
is expressed in a nominal 640x640 frame so the first-gen dwell thresholds
(px/s) carry over unchanged.

Association strategy (two passes, faithful to the C++):
  1. IoU match, tracks tried oldest-first, each detection used once. Lost tracks
     have their box predicted forward by their velocity before matching.
  2. Centre-distance fallback for still-unmatched, recently-lost tracks.
Unmatched detections spawn new tracks; unmatched tracks age out (edge tracks
faster than centre tracks, since edge losses are usually real exits).

## kit.logic.tracker.TrackerConfig

```python
@dataclass
class TrackerConfig
```

轻量 IoU 跟踪器配置数据类，包含匹配/丢失/平滑等阈值，clamp 原地限制范围。

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
iou_threshold: float = 0.2
dist_threshold: float = 0.15
max_lost_frames_center: int = 90
max_lost_frames_edge: int = 15
dist_fallback_max_lost: int = 5
vel_alpha: float = 0.08
vel_alpha_sudden: float = 0.6
velocity_zero_threshold: float = 3.0
edge_margin: float = 0.15
min_frames_for_count: int = 10
assumed_fps: float = 15.0
```

### kit.logic.tracker.TrackerConfig.clamp

```python
def clamp(self) -> 'TrackerConfig'
```

原地限制配置字段到实现允许范围，并返回自身；字段单位及默认值见配置类。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/tracker.py#L49)

## kit.logic.tracker.Track

```python
@dataclass
class Track
```

One tracked object. Coordinates normalised to [0,1]; foot = bbox
bottom-centre (where the person stands), used for zone / line tests.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
track_id: int
cx: float
cy: float
w: float
h: float
score: float = 0.0
velocity_x: float = 0.0
velocity_y: float = 0.0
speed_px_s: float = 0.0
first_seen: float = 0.0
last_seen: float = 0.0
frames_tracked: int = 0
lost_frames: int = 0
foot: Tuple[float, float] = (0.0, 0.0)
prev_foot: Tuple[float, float] = (0.0, 0.0)
near_edge: bool = False
det_index: int = -1
```

### kit.logic.tracker.Track.xyxy_norm

```python
@property
def xyxy_norm(self) -> List[float]
```

返回当前跟踪框的归一化 xyxy 坐标；不同于 App.emit 常用的原图像素框。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/tracker.py#L87)

## kit.logic.tracker.Tracker

```python
class Tracker
```

Greedy IoU tracker with velocity prediction and a two-pass matcher.

### kit.logic.tracker.Tracker.__init__

```python
def __init__(self, config: Optional[TrackerConfig]=None) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/tracker.py#L99)

### kit.logic.tracker.Tracker.update

```python
def update(self, dets: Sequence[dict], t: float, frame_w: int, frame_h: int) -> List[Track]
```

Advance the tracker one frame.

dets      : detections to track this frame, each {"box":[x1,y1,x2,y2] in
            ORIGINAL pixels, "score": float}. Pre-filter to the class you
            want (e.g. person) before calling.
t         : monotonic timestamp (seconds).
frame_w/h : original frame size, to normalise boxes.

Returns the list of currently-VISIBLE tracks (lost_frames == 0), each
carrying a stable `track_id`, velocity and current/previous foot point.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/tracker.py#L207)

### kit.logic.tracker.Tracker.active_tracks

```python
def active_tracks(self) -> List[Track]
```

返回当前活跃 Track 列表，生命周期由跟踪器管理。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/tracker.py#L288)

### kit.logic.tracker.Tracker.track_count

```python
@property
def track_count(self) -> int
```

返回当前跟踪状态中记录的数量。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/tracker.py#L292)

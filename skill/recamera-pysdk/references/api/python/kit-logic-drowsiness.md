# kit.logic.drowsiness

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/logic/drowsiness.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/drowsiness.py)；签名由 AST 提取，不导入硬件依赖。

FaceMesh 468 点疲劳指标、打哈欠计数与 PERCLOS 状态机；使用单调帧时间（秒），仅为应用启发式逻辑。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

Drowsiness / fatigue logic for the facemesh-reader app. Pure numpy, CPU-only.

Direct Python port of the first-gen SSCMA C++ modules
(facial_metrics.cpp / yawn_detector.cpp / drowsiness_detector.cpp). The 468-pt
MediaPipe FaceMesh index sets and all thresholds are carried over verbatim so
behaviour matches the reference. The only structural change: the C++ used
std::chrono::steady_clock; here every stateful window is driven by the frame
timestamp `t` (kit Frame.pts, monotonic seconds) passed in per update, so the
logic is deterministic and stream-clock aligned.

Three cooperating pieces, combined by `DrowsinessLogic`:

  * FaceMetrics.compute(landmarks) -> EAR (per eye + avg), MAR, closed/open flags
  * YawnTracker.update(mar, t)     -> is_yawning + 5-min yawn count (event-debounced)
  * DrowsinessTracker.update(ear, t, yawn_count) -> continuous-closure + PERCLOS
                                                    + composite level + state

## 常量与类型别名

以下为该版本源码值；默认地址不等于所有固件都开放该服务。

```python
LEFT_EYE_IDX = (33, 160, 158, 133, 153, 144)
```


```python
RIGHT_EYE_IDX = (362, 385, 387, 263, 373, 380)
```


```python
MOUTH_IDX = (61, 39, 0, 269, 291, 17)
```


```python
EAR_THRESHOLD = 0.21
```


```python
MAR_THRESHOLD = 0.65
```


## kit.logic.drowsiness.FaceMetrics

```python
@dataclass
class FaceMetrics
```

FaceMesh 单帧眼口指标数据类：EAR/MAR、睁闭状态等；字段默认值见下表。

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
left_ear: float = 0.0
right_ear: float = 0.0
avg_ear: float = 0.0
mar: float = 0.0
eyes_closed: bool = False
mouth_open: bool = False
valid: bool = False
```

## kit.logic.drowsiness.compute_metrics

```python
def compute_metrics(landmarks, ear_threshold: float=EAR_THRESHOLD, mar_threshold: float=MAR_THRESHOLD) -> FaceMetrics
```

landmarks: indexable of >=468 (x, y, ...) points (original-frame px).

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/drowsiness.py#L80)

## kit.logic.drowsiness.YawnState

```python
@dataclass
class YawnState
```

打哈欠检测状态数据类，保存当前状态与时间窗计数。

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
is_yawning_now: bool = False
yawn_count_5min: int = 0
```

## kit.logic.drowsiness.YawnTracker

```python
class YawnTracker
```

Yawn = MAR > threshold for >= consecutive_frames frames in a row.

Each yawn instance is counted exactly once (event-debounce). Returns
(state, event) where event is True only on the yawn-onset frame.

### kit.logic.drowsiness.YawnTracker.__init__

```python
def __init__(self, mar_threshold: float=MAR_THRESHOLD, consecutive_frames: int=5, window_sec: float=300.0)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/drowsiness.py#L113)

### kit.logic.drowsiness.YawnTracker.reset

```python
def reset(self) -> None
```

清空打哈欠状态及历史时间窗，返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/drowsiness.py#L122)

### kit.logic.drowsiness.YawnTracker.update

```python
def update(self, mar: float, t: float) -> Tuple[YawnState, bool]
```

输入当前口部 MAR 和单调秒级时间 t，更新持续/去抖状态并返回 YawnState。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/drowsiness.py#L127)

## kit.logic.drowsiness.DrowsinessConfig

```python
@dataclass
class DrowsinessConfig
```

疲劳启发式配置数据类，定义 EAR、闭眼持续、PERCLOS 和打哈欠阈值；需按场景评估。

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
ear_threshold: float = 0.21
ear_continuous_sec: float = 2.0
perclos_window_sec: float = 60.0
perclos_warning_pct: float = 15.0
perclos_critical_pct: float = 20.0
alert_cooldown_sec: float = 5.0
yawn_count_threshold: int = 3
```

## kit.logic.drowsiness.DrowsinessState

```python
@dataclass
class DrowsinessState
```

疲劳状态快照，包括闭眼/PERCLOS/组合等级等字段；不是医学诊断。

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
is_eyes_closed: bool = False
continuous_closure_sec: float = 0.0
perclos_pct: float = 0.0
perclos_window_samples: int = 0
drowsy_by_ear: bool = False
drowsy_by_perclos: bool = False
drowsy_by_yawn: bool = False
drowsiness_level: float = 0.0
state: str = 'Alert'
alert_active: bool = False
```

## kit.logic.drowsiness.DrowsinessTracker

```python
class DrowsinessTracker
```

根据时间序列 EAR 和打哈欠计数计算疲劳状态；一个实例跟踪一个连续对象。

### kit.logic.drowsiness.DrowsinessTracker.__init__

```python
def __init__(self, cfg: DrowsinessConfig=None)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/drowsiness.py#L176)

### kit.logic.drowsiness.DrowsinessTracker.reset

```python
def reset(self) -> None
```

清空眼部时间窗与疲劳累计状态，返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/drowsiness.py#L186)

### kit.logic.drowsiness.DrowsinessTracker.update

```python
def update(self, ear: float, t: float, yawn_count_5min: int) -> DrowsinessState
```

输入 EAR、秒级时间 t 和 yawn_count，更新闭眼持续/PERCLOS 并返回 DrowsinessState。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/drowsiness.py#L189)

## kit.logic.drowsiness.DrowsinessLogic

```python
class DrowsinessLogic
```

One-call-per-frame façade: landmarks -> metrics + yawn + drowsiness.

When no valid face is present, still ticks the trackers with neutral inputs
(MAR=0, EAR=1.0) so the PERCLOS window keeps shrinking and stale closures do
not accumulate -- exactly as the first-gen pipeline did.

### kit.logic.drowsiness.DrowsinessLogic.__init__

```python
def __init__(self, drowsy_cfg: DrowsinessConfig=None, mar_threshold: float=MAR_THRESHOLD, yawn_consecutive_frames: int=5, yawn_window_sec: float=300.0, ear_threshold: float=EAR_THRESHOLD)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/drowsiness.py#L270)

### kit.logic.drowsiness.DrowsinessLogic.update

```python
def update(self, landmarks, t: float)
```

Returns (FaceMetrics, YawnState, DrowsinessState, yawn_event:bool).

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/drowsiness.py#L280)

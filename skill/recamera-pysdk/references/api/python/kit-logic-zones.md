# kit.logic.zones

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/logic/zones.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/zones.py)；签名由 AST 提取，不导入硬件依赖。

区域占用、越线、停留与滚动统计；区域坐标归一化，停留速度阈值按名义 640 像素坐标计算。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

Spatial & temporal counting on top of `kit.logic.tracker` tracks.

Ported from the first-gen retail-vision C++ (`zone_metrics.cpp` +
the dwell / entry-line parts of `person_tracker.cpp`). All are model-free and
operate on `Track` objects (or any object exposing `.foot`, `.prev_foot`,
`.track_id`, `.speed_px_s`) so any "detect + track + count" app reuses them.

  ZoneCounter  -- occupancy inside a normalised polygon (foot-point test).
  LineCounter  -- directed entry/exit counting across a normalised segment
                  (cross-product sign decides in vs out), one count per crossing.
  Dwell        -- per-track stationary state machine (browsing / engaged /
                  assistance) with the C++ decay-tolerant stationary counter.
  RollingWindow-- occupancy median-smoothing + peak / averages over a time window.

Coordinates are normalised to [0,1]; dwell speed thresholds are px/s in the
nominal 640 frame (matching `Track.speed_px_s`), so first-gen defaults carry over.

## 常量与类型别名

以下为该版本源码值；默认地址不等于所有固件都开放该服务。

```python
BROWSING = 'browsing'
```


```python
ENGAGED = 'engaged'
```


```python
ASSISTANCE = 'assistance'
```


## kit.logic.zones.ZoneCounter

```python
class ZoneCounter
```

Counts tracks whose FOOT point lies inside a normalised polygon.

An empty / <3-point polygon means "whole frame" (every track counts), which
is the default so an unconfigured app still reports total occupancy.

### kit.logic.zones.ZoneCounter.__init__

```python
def __init__(self, polygon: Optional[Sequence[Sequence[float]]]=None) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/zones.py#L44)

### kit.logic.zones.ZoneCounter.set_polygon

```python
def set_polygon(self, polygon: Optional[Sequence[Sequence[float]]]) -> None
```

设置归一化多边形；空值或不足三点代表不限制区域，返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/zones.py#L48)

### kit.logic.zones.ZoneCounter.enabled

```python
@property
def enabled(self) -> bool
```

区域是否包含至少三个顶点。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/zones.py#L55)

### kit.logic.zones.ZoneCounter.contains

```python
def contains(self, foot: Tuple[float, float]) -> bool
```

判断归一化 foot 点是否在区域内；区域未启用时返回 True。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/zones.py#L58)

### kit.logic.zones.ZoneCounter.inside

```python
def inside(self, tracks: Sequence) -> List
```

返回 foot 点落在区域内的 Track 列表。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/zones.py#L63)

### kit.logic.zones.ZoneCounter.count

```python
def count(self, tracks: Sequence) -> int
```

返回区域内 Track 数量。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/zones.py#L66)

## kit.logic.zones.LineCounter

```python
class LineCounter
```

Directed entry/exit counting across a finite normalised segment a->b.

Each frame, a track's previous foot -> current foot segment is tested for a
genuine crossing of a->b (both segments must strictly straddle). A left->right
crossing (cross-product sign +1) counts as an ENTRY when `ab_in` is True,
else an EXIT; the sign flips for the opposite direction. Because it compares
consecutive foot points, each physical crossing is counted exactly once.

### kit.logic.zones.LineCounter.__init__

```python
def __init__(self, a: Optional[Sequence[float]]=None, b: Optional[Sequence[float]]=None, ab_in: bool=True) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/zones.py#L82)

### kit.logic.zones.LineCounter.set_line

```python
def set_line(self, a: Sequence[float], b: Sequence[float], ab_in: bool=True) -> None
```

设置归一化有向线段 a→b 与 ab_in 入方向，返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/zones.py#L92)

### kit.logic.zones.LineCounter.enabled

```python
@property
def enabled(self) -> bool
```

是否已经同时设置线段两个端点。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/zones.py#L99)

### kit.logic.zones.LineCounter.update

```python
def update(self, tracks: Sequence) -> List[dict]
```

Test every track for a crossing this frame. Returns a list of
{"track_id","dir"} events ("in"/"out") and updates the counters.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/zones.py#L102)

## kit.logic.zones.DwellConfig

```python
@dataclass
class DwellConfig
```

停留状态配置；速度为名义 640 画面像素/秒，engaged/assistance 时间单位为秒。

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
speed_threshold: float = 10.0
min_frames: int = 5
engaged_sec: float = 1.5
assistance_sec: float = 20.0
stable_threshold: int = 30
decay_slow: int = 2
decay_fast: int = 5
```

## kit.logic.zones.Dwell

```python
class Dwell
```

Tracks per-track stationary dwell time and classifies browsing / engaged /
assistance. Mirrors PersonTracker::updateDwellState + updateStationaryFrames
(decay-tolerant so brief gestures don't reset a confirmed dwell).

### kit.logic.zones.Dwell.__init__

```python
def __init__(self, config: Optional[DwellConfig]=None) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/zones.py#L155)

### kit.logic.zones.Dwell.update

```python
def update(self, track, t: float) -> str
```

Advance one track's dwell state; returns its current state string.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/zones.py#L167)

### kit.logic.zones.Dwell.duration

```python
def duration(self, track_id: int) -> float
```

返回指定 track 的累计停留秒数。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/zones.py#L196)

### kit.logic.zones.Dwell.prune

```python
def prune(self, live_ids: Sequence[int]) -> None
```

Forget state for tracks that no longer exist (call each frame).

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/zones.py#L200)

## kit.logic.zones.StateCount

```python
@dataclass
class StateCount
```

单个停留状态的计数/统计数据类，字段见下表。

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
total: int = 0
browsing: int = 0
engaged: int = 0
assistance: int = 0
```

## kit.logic.zones.WindowSnapshot

```python
@dataclass
class WindowSnapshot
```

滚动统计窗快照，包含人数、峰值、平均或状态统计字段。

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
occupancy: int = 0
browsing: int = 0
engaged: int = 0
assistance: int = 0
peak: int = 0
entry_count: int = 0
exit_count: int = 0
```

## kit.logic.zones.RollingWindow

```python
class RollingWindow
```

Median-smooths occupancy and reports peak over a sliding time window.

Port of ZoneMetrics: a 5-sample median filter suppresses single-frame
occupancy jitter, occupancy is sampled once per second for the peak, and
samples older than `window_sec` are pruned.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
SMOOTH = 5
```

### kit.logic.zones.RollingWindow.__init__

```python
def __init__(self, window_sec: float=60.0) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/zones.py#L238)

### kit.logic.zones.RollingWindow.update

```python
def update(self, counts: StateCount, entry_count: int, exit_count: int, t: float) -> None
```

输入 StateCount、累计进入/离开人数及秒级时间 t，更新五点中值平滑和滑动窗口；最多每秒采样一次，并清除过期样本。原地更新状态，返回 None；使用 snapshot() 读取结果。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/zones.py#L253)

### kit.logic.zones.RollingWindow.snapshot

```python
def snapshot(self) -> WindowSnapshot
```

取得指定时刻的 WindowSnapshot；不会触发模型或网络发送。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/zones.py#L266)

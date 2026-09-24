# kit.logic.rep_counter

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/logic/rep_counter.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/rep_counter.py)；签名由 AST 提取，不导入硬件依赖。

运动次数计数与动作状态；基于关节角、可见性、平滑和迟滞，含 squat/push-up/hammer-curl。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

Exercise rep counting for pose apps -- joint-angle relaxation/flexion state
machines that turn a stream of keypoints into a rep count.

Ported faithfully from the first-gen fitness-trainer C++
(solutions/fitness-trainer/main/{exercise.h,exercise.cpp,pose.cpp}). The
thresholds, hysteresis band, debounce, EMA smoothing, two-sided pairing and
"count on the way back up" behaviour are all the original's -- see exercise.h's
header comment for WHY each differs from the even-earlier Python original.

Pure math + kit.logic.geometry; no numpy, no model coupling. Everything operates
on decoded pose dicts (kit.runtime.postprocess.pose output: {box, score,
keypoints:[[x,y,conf]*17]}) so fall-detection and fitness-trainer share one
keypoint convention.

## kit.logic.rep_counter.RepCounter

```python
class RepCounter
```

Hysteresis rep counter shared by every exercise (port of C++ RepCounter).

Phase is "extended" above up_threshold and "flexed" below down_threshold,
with the band between them holding the previous phase -- that band is what
stops keypoint jitter from counting reps. A rep completes on
flexed -> extended, no sooner than `min_interval` after the last one.

### kit.logic.rep_counter.RepCounter.__init__

```python
def __init__(self, up_threshold: float, down_threshold: float, min_interval: float=0.4)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/rep_counter.py#L52)

### kit.logic.rep_counter.RepCounter.reset

```python
def reset(self) -> None
```

重置角度平滑、伸展/屈曲状态与次数累计，返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/rep_counter.py#L60)

### kit.logic.rep_counter.RepCounter.miss

```python
def miss(self, now_sec: float) -> None
```

Feed a frame with no usable reading (joints hidden). After
_LOST_SECONDS of these the phase resets, so an athlete who walks away
and comes back does not resume mid-rep.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/rep_counter.py#L71)

### kit.logic.rep_counter.RepCounter.update

```python
def update(self, angle: Optional[float], now_sec: float) -> bool
```

Feed one angle reading. Returns True on the frame a rep completes.

`angle` is None ( == C++ NaN) when the joint triplet is not readable.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/rep_counter.py#L80)

### kit.logic.rep_counter.RepCounter.smoothed

```python
@property
def smoothed(self) -> float
```

返回平滑后的当前关节角度（度）。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/rep_counter.py#L123)

### kit.logic.rep_counter.RepCounter.has_reading

```python
def has_reading(self) -> bool
```

当前是否有可使用的角度读数。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/rep_counter.py#L126)

### kit.logic.rep_counter.RepCounter.extended

```python
def extended(self) -> bool
```

当前是否满足伸展状态的角度/迟滞条件。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/rep_counter.py#L129)

### kit.logic.rep_counter.RepCounter.flexed

```python
def flexed(self) -> bool
```

当前是否满足屈曲状态的角度/迟滞条件。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/rep_counter.py#L132)

### kit.logic.rep_counter.RepCounter.last_rep_min_angle

```python
def last_rep_min_angle(self) -> float
```

返回最近一次完成动作的最小关节角度（度）。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/rep_counter.py#L135)

### kit.logic.rep_counter.RepCounter.ever_read

```python
def ever_read(self) -> bool
```

是否曾接收过有效角度读数。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/rep_counter.py#L138)

## kit.logic.rep_counter.ExerciseState

```python
class ExerciseState
```

What the app reads each frame (mirror of the C++ ExerciseState struct).

### kit.logic.rep_counter.ExerciseState.__init__

```python
def __init__(self, two_sided: bool=False)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/rep_counter.py#L148)

### kit.logic.rep_counter.ExerciseState.as_dict

```python
def as_dict(self) -> Dict
```

将动作计数、组数与状态输出为可序列化字典。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/rep_counter.py#L164)

## kit.logic.rep_counter.Exercise

```python
class Exercise
```

Base exercise. Subclasses implement `_track` + `_on_reset`.

`update(person, now_sec)` advances the state machine by one frame; pass
person == None when nobody was detected. `person` is one pose result dict
({box, score, keypoints}); keypoint visibility is gated by kpt_thres.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
id = 'exercise'
display_name = 'Exercise'
```

### kit.logic.rep_counter.Exercise.__init__

```python
def __init__(self, kpt_thres: float=0.5)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/rep_counter.py#L194)

### kit.logic.rep_counter.Exercise.set_targets

```python
def set_targets(self, target_reps: int, target_sets: int) -> None
```

更新每组次数与目标组数，不执行模型推理，返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/rep_counter.py#L203)

### kit.logic.rep_counter.Exercise.reset

```python
def reset(self) -> None
```

重置当前动作会话的计数/状态，返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/rep_counter.py#L207)

### kit.logic.rep_counter.Exercise.update

```python
def update(self, person: Optional[dict], now_sec: float) -> ExerciseState
```

输入单个人体姿态字典或 None、单调秒级 now_sec，更新动作状态并返回 ExerciseState。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/rep_counter.py#L220)

## kit.logic.rep_counter.Squat

```python
class Squat(Exercise)
```

深蹲动作实现，通过指定腿部关键点和关节角驱动 RepCounter。

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
id = 'squat'
display_name = 'Squat'
```

### kit.logic.rep_counter.Squat.__init__

```python
def __init__(self, kpt_thres: float=0.5)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/rep_counter.py#L259)

### kit.logic.rep_counter.Squat.set_targets

```python
def set_targets(self, target_reps: int, target_sets: int) -> None
```

更新每组次数与目标组数，不执行模型推理，返回 None。

此方法定义于基类 `Exercise`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/rep_counter.py#L203)

### kit.logic.rep_counter.Squat.reset

```python
def reset(self) -> None
```

重置当前动作会话的计数/状态，返回 None。

此方法定义于基类 `Exercise`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/rep_counter.py#L207)

### kit.logic.rep_counter.Squat.update

```python
def update(self, person: Optional[dict], now_sec: float) -> ExerciseState
```

输入单个人体姿态字典或 None、单调秒级 now_sec，更新动作状态并返回 ExerciseState。

此方法定义于基类 `Exercise`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/rep_counter.py#L220)

## kit.logic.rep_counter.PushUp

```python
class PushUp(Exercise)
```

俯卧撑动作实现，通过手臂/身体关键点角度驱动 RepCounter。

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
id = 'push_up'
display_name = 'Push-up'
```

### kit.logic.rep_counter.PushUp.__init__

```python
def __init__(self, kpt_thres: float=0.5)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/rep_counter.py#L307)

### kit.logic.rep_counter.PushUp.set_targets

```python
def set_targets(self, target_reps: int, target_sets: int) -> None
```

更新每组次数与目标组数，不执行模型推理，返回 None。

此方法定义于基类 `Exercise`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/rep_counter.py#L203)

### kit.logic.rep_counter.PushUp.reset

```python
def reset(self) -> None
```

重置当前动作会话的计数/状态，返回 None。

此方法定义于基类 `Exercise`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/rep_counter.py#L207)

### kit.logic.rep_counter.PushUp.update

```python
def update(self, person: Optional[dict], now_sec: float) -> ExerciseState
```

输入单个人体姿态字典或 None、单调秒级 now_sec，更新动作状态并返回 ExerciseState。

此方法定义于基类 `Exercise`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/rep_counter.py#L220)

## kit.logic.rep_counter.HammerCurl

```python
class HammerCurl(Exercise)
```

锤式弯举动作实现，通过手臂角度驱动 RepCounter。

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
id = 'hammer_curl'
display_name = 'Hammer Curl'
```

### kit.logic.rep_counter.HammerCurl.__init__

```python
def __init__(self, kpt_thres: float=0.5)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/rep_counter.py#L355)

### kit.logic.rep_counter.HammerCurl.set_targets

```python
def set_targets(self, target_reps: int, target_sets: int) -> None
```

更新每组次数与目标组数，不执行模型推理，返回 None。

此方法定义于基类 `Exercise`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/rep_counter.py#L203)

### kit.logic.rep_counter.HammerCurl.reset

```python
def reset(self) -> None
```

重置当前动作会话的计数/状态，返回 None。

此方法定义于基类 `Exercise`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/rep_counter.py#L207)

### kit.logic.rep_counter.HammerCurl.update

```python
def update(self, person: Optional[dict], now_sec: float) -> ExerciseState
```

输入单个人体姿态字典或 None、单调秒级 now_sec，更新动作状态并返回 ExerciseState。

此方法定义于基类 `Exercise`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/rep_counter.py#L220)

## kit.logic.rep_counter.create_exercise

```python
def create_exercise(mode: str, kpt_thres: float=0.5) -> Optional[Exercise]
```

按 exercise_id 和参数创建对应动作对象；未知标识按源码错误路径处理，不自动选任意动作。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/rep_counter.py#L452)

## kit.logic.rep_counter.exercise_ids

```python
def exercise_ids() -> List[str]
```

返回当前实现支持的动作标识集合/列表，供 UI 枚举。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/rep_counter.py#L457)

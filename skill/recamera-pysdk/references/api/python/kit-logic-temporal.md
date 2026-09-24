# kit.logic.temporal

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/logic/temporal.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/temporal.py)；签名由 AST 提取，不导入硬件依赖。

跌倒时序状态机：normal/suspected/fallen/recovering；默认需要有效当前姿态与 learned temporal-positive 才确认。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

Temporal fall state machine, ported from the first-gen C++
(solutions/fall-detection/main/fall_detector.cpp).

Design faithful to the production first-gen/Jetson detector. Geometry and
motion arm ``suspected``; by default only a learned temporal-positive result on
a valid current pose can confirm ``fallen``.  ``temporal_confirmation_required``
may be set false explicitly for legacy geometry-only bring-up.

State: Normal -> Suspected -> Fallen -> Recovering -> Normal.
No fall is declared from a single feature or a single frame: evidence is scored
(hip speed, torso angle, box aspect) and must persist for `confirmation_sec`.

## 常量与类型别名

以下为该版本源码值；默认地址不等于所有固件都开放该服务。

```python
NORMAL = 'normal'
```


```python
SUSPECTED = 'suspected'
```


```python
FALLEN = 'fallen'
```


```python
RECOVERING = 'recovering'
```


## kit.logic.temporal.FallConfig

```python
@dataclass
class FallConfig
```

跌倒状态机配置：归一化髋部移动速度/距离、角度、框比例及秒级确认/恢复窗口。

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
temporal_confirmation_required: bool = True
hip_drop_speed_threshold: float = 0.25
hip_drop_distance_threshold: float = 0.02
motion_window_sec: float = 0.75
torso_angle_threshold_deg: float = 55.0
bbox_aspect_ratio_threshold: float = 1.25
min_suspected_features: int = 2
confirmation_sec: float = 0.8
suspected_timeout_sec: float = 1.5
occlusion_grace_sec: float = 0.75
recovery_torso_angle_deg: float = 35.0
recovery_aspect_ratio: float = 1.1
recovery_window_sec: float = 2.0
cooldown_sec: float = 3.0
```

### kit.logic.temporal.FallConfig.clamp

```python
def clamp(self) -> 'FallConfig'
```

原地限制配置字段到实现允许范围，并返回自身；字段单位及默认值见配置类。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/temporal.py#L44)

## kit.logic.temporal.FallOutput

```python
@dataclass
class FallOutput
```

返回状态、持续 fall_detected、边沿 fall_event、event_id 及 diagnostics；持续状态不等同于每帧新事件。

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
state: str = NORMAL
fall_detected: bool = False
fall_event: bool = False
event_id: int = 0
diagnostics: Dict[str, float] = field(default_factory=dict)
```

## kit.logic.temporal.FallDetector

```python
class FallDetector
```

有状态跌倒判定器；默认需要 learned temporal 确认，不应把单帧几何异常直接报告为跌倒。

### kit.logic.temporal.FallDetector.__init__

```python
def __init__(self, config: FallConfig | None=None)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/temporal.py#L71)

### kit.logic.temporal.FallDetector.set_config

```python
def set_config(self, config: FallConfig) -> None
```

校验/截断并替换状态机配置，返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/temporal.py#L75)

### kit.logic.temporal.FallDetector.reset

```python
def reset(self) -> None
```

重置时序状态、事件计数、基准姿态和冷却窗口，返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/temporal.py#L78)

### kit.logic.temporal.FallDetector.update

```python
def update(self, o: Observation, *, temporal_available: bool=False, temporal_positive: bool=False, temporal_probability: float=0.0) -> FallOutput
```

消费当前 Observation、秒级时间及可选 temporal-positive 证据，返回 FallOutput；无效/遮挡姿态按配置宽限处理。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/temporal.py#L136)

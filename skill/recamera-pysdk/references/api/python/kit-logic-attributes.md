# kit.logic.attributes

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/logic/attributes.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/attributes.py)；签名由 AST 提取，不导入硬件依赖。

人脸属性概率平滑、置信门控及跟踪/时间窗聚合；需对应模型类别与输出头顺序。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

Per-identity face-attribute evidence: quality gating, temporal voting and
once-per-person demographic counting.

A second-stage attribute classifier (FairFace age/gender/race, an emotion head)
produces one softmax per FRAME. Reading that softmax straight out is wrong in
three separate ways, and this module owns the fix for all three:

  1. **Quality.** A 30x30 face upsampled to 224, or a detection that barely
     cleared the score threshold, still yields a confident-looking softmax. The
     gate (`AttributeConfig.min_face_px` / `min_det_score`) rejects those ROIs
     BEFORE the classifier runs, so they cost no inference and contribute no
     evidence -- rather than emitting a coin-toss dressed up as a prediction.

  2. **Single-frame argmax.** The same person flips label frame to frame. A
     `TrackAttributes` accumulator sums the per-frame probability vectors for
     one `track_id` and argmaxes the SUM, so the verdict is a vote over every
     frame that passed the gate. `stable` says whether enough frames have
     accumulated (`min_track_frames`) for the verdict to mean anything.

  3. **Counting.** A demographic histogram bumped once per face per frame does
     not measure people; it measures dwell-weighted face-frames -- someone
     standing still for a minute outvotes sixty people walking past. `Aggregator`
     folds each `track_id` into the histogram EXACTLY ONCE, when its evidence
     first becomes stable, so the window reports unique faces. The raw
     face-frame count is kept alongside it for anyone who wants the old number.

Softmax **temperature** is a deployment-calibration knob, not a property of the
model: a ResNet classification head is systematically overconfident, and the
correction is fitted per head on a held-out set. `AttributeConfig` owns the
policy (which head gets which temperature); the arithmetic lives one layer down
in `kit.runtime.postprocess.classify.softmax`, which the caller reaches through
`fairface_decode(outputs, temperature=cfg.temperature)`. Default 1.0 everywhere
= exact no-op, so turning it on is a deliberate, measured act. `min_conf`
likewise defaults to 0 (off): suppressing a label is a product decision that
needs a measured threshold behind it, and this module only supplies the
mechanism.

The module is model-free and app-agnostic: it consumes `(head, probability
vector)` pairs keyed by a track id and knows nothing about FairFace, RKNN or
which labels exist. `kit.logic.tracker` supplies the ids.

## kit.logic.attributes.AttributeConfig

```python
@dataclass
class AttributeConfig
```

Gating / voting / calibration policy for one cascade app.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
min_face_px: float = 64.0
min_det_score: float = 0.0
min_track_frames: int = 3
max_frames: int = 0
decay: float = 1.0
temperature: Dict[str, float] = field(default_factory=dict)
min_conf: Dict[str, float] = field(default_factory=dict)
```

`min_face_px`：Shorter side of the face box in ORIGINAL frame pixels. Below this the ROI
    is mostly upsampling artefact: the crop is blown up to the classifier's 224
    input and the head reports whatever texture the interpolator invented.

`min_det_score`：Extra detector-score floor on top of the detector's own threshold, for
    when attributes should be stricter than detection. 0 disables it.

`min_track_frames`：Gate-passing frames a track needs before its verdict counts as `stable`
    and is folded into the demographic histogram.

`max_frames`：Cap on accumulated frames per track (0 = whole track life). A nonzero cap
    makes the accumulator a sliding weight: older evidence is decayed by
    `decay` instead of kept forever, which matters if one track id can outlive
    the person it started on (an identity swap through an occlusion).

`decay`：Per-frame multiplier applied to accumulated evidence before adding the
    new frame. 1.0 = plain sum (every frame equal). <1.0 = exponential
    forgetting, so a mid-track identity swap recovers instead of being
    outvoted by history.

`temperature`：Softmax temperature per head name. >1 softens, <1 sharpens, 1.0 = no-op.

`min_conf`：Per-head confidence floor. A verdict below its floor reports label None
    rather than a guess. 0 / missing = no suppression.

### kit.logic.attributes.AttributeConfig.clamp

```python
def clamp(self) -> 'AttributeConfig'
```

原地限制配置字段到实现允许范围，并返回自身；字段单位及默认值见配置类。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/attributes.py#L90)

### kit.logic.attributes.AttributeConfig.temp

```python
def temp(self, head: str) -> float
```

返回指定分类 head 的温度系数，未配置时为 1.0。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/attributes.py#L102)

### kit.logic.attributes.AttributeConfig.floor

```python
def floor(self, head: str) -> float
```

返回指定 head 的最低置信度，未配置时为 0.0。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/attributes.py#L105)

## kit.logic.attributes.passes_gate

```python
def passes_gate(box: Sequence[float], score: float, cfg: AttributeConfig) -> bool
```

Is this detection worth running an attribute classifier on?

`box` is [x1,y1,x2,y2] in ORIGINAL frame pixels -- the gate is a physical
resolution test, so it must NOT be fed letterboxed or normalised
coordinates. Called before the crop, so a rejected face costs nothing.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/attributes.py#L109)

## kit.logic.attributes.TrackAttributes

```python
class TrackAttributes
```

Accumulated per-head probability evidence for ONE track id.

### kit.logic.attributes.TrackAttributes.__init__

```python
def __init__(self, cfg: AttributeConfig) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/attributes.py#L131)

### kit.logic.attributes.TrackAttributes.add

```python
def add(self, head: str, probs: Sequence[float]) -> None
```

Fold one frame's probability vector for `head` into the evidence.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/attributes.py#L138)

### kit.logic.attributes.TrackAttributes.bump_frame

```python
def bump_frame(self, t: float) -> None
```

Count one gate-passing frame. Call once per frame, after `add()`s.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/attributes.py#L149)

### kit.logic.attributes.TrackAttributes.stable

```python
@property
def stable(self) -> bool
```

返回累计帧数是否达到 min_track_frames 的 bool。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/attributes.py#L157)

### kit.logic.attributes.TrackAttributes.verdict

```python
def verdict(self, head: str, labels: Optional[Sequence[str]]=None) -> dict
```

Argmax of the ACCUMULATED evidence for one head.

`confidence` is the vote share (accumulated probability mass of the
winner / total), not a single frame's softmax -- so it reads as "how
much of the evidence points here", which is what a caller thresholding
it actually wants. Falls back to a label-less empty verdict when the
head has no evidence yet.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/attributes.py#L160)

## kit.logic.attributes.Aggregator

```python
class Aggregator
```

Track-scoped attribute store + a once-per-identity demographic window.

Owns one `TrackAttributes` per live track id and the running histogram.
`sweep()` drops the state of tracks the tracker has retired, so memory is
bounded by the number of CONCURRENT faces, not by the number of people
seen since boot.

### kit.logic.attributes.Aggregator.__init__

```python
def __init__(self, cfg: Optional[AttributeConfig]=None, heads: Sequence[str]=()) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/attributes.py#L194)

### kit.logic.attributes.Aggregator.track

```python
def track(self, track_id: int) -> TrackAttributes
```

取得或创建该 track_id 的 TrackAttributes 状态；ID 只在当前跟踪实例内有意义。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/attributes.py#L202)

### kit.logic.attributes.Aggregator.sweep

```python
def sweep(self, removed_ids: Sequence[int]) -> None
```

Forget retired tracks (ids the tracker dropped this frame).

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/attributes.py#L209)

### kit.logic.attributes.Aggregator.reset_window

```python
def reset_window(self, t: float) -> None
```

重置当前聚合时间窗的计数，不是重新加载模型，返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/attributes.py#L215)

### kit.logic.attributes.Aggregator.note_face_frame

```python
def note_face_frame(self) -> None
```

记录本帧包含的人脸信息用于时间窗统计，返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/attributes.py#L221)

### kit.logic.attributes.Aggregator.maybe_count

```python
def maybe_count(self, track_id: int, labels_by_head: Dict[str, Optional[str]]) -> bool
```

Fold a track into the histogram if it is stable and not yet counted.

Returns True when this call actually counted the track. Counting happens
at the moment the evidence FIRST becomes stable, not at track exit, so a
person who lingers is reported in the window they arrived in rather than
being withheld until they leave.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/attributes.py#L224)

### kit.logic.attributes.Aggregator.elapsed

```python
def elapsed(self, t: float) -> float
```

返回当前统计窗已经经历的秒数。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/attributes.py#L245)

### kit.logic.attributes.Aggregator.snapshot

```python
def snapshot(self, t: float) -> dict
```

The demographics event body for the window ending at `t`.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/attributes.py#L248)

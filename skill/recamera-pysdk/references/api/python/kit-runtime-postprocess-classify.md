# kit.runtime.postprocess.classify

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/runtime/postprocess/classify.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/postprocess/classify.py)；签名由 AST 提取，不导入硬件依赖。

分类 softmax、top-k、多头属性与表情解码；不要对已归一化概率重复 softmax。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

Generic classification post-processing for reCamera Pro. Pure numpy.

Second-stage classifiers in the cascade family (face-analysis: FairFace
age/gender/race + emotion) emit a flat logit vector per face ROI. This module
turns raw RKNN outputs into calibrated class picks:

    * softmax / argmax / topk               -- the numeric primitives
    * classify_head(logits, labels)         -- one softmax head -> pick + probs
    * split_heads(vec, segments)            -- slice a multi-head vector, one
                                               softmax+argmax per contiguous head
    * fairface_decode(outputs)              -- (1,18) -> race[0:7] / gender[7:9]
                                               / age[9:18], each its own head
    * emotion_decode(outputs)               -- (1,8) single 8-class head

The FairFace layout and label order are the ground truth from the model
conversion script (models/convert/fix_face_cls.py) and match the first-gen C++
runner (age_gender_race_runner.cpp): a single 18-vector split race(7) +
gender(2) + age(9). Normalization (ImageNet mean/std) is baked into the rknn,
so the caller feeds a raw uint8 224x224 RGB ROI to the engine and hands the raw
outputs here.

## 常量与类型别名

以下为该版本源码值；默认地址不等于所有固件都开放该服务。

```python
RACE_LABELS = ['White', 'Black', 'Latino_Hispanic', 'East Asian', 'Southeast Asian', 'Indian', 'Middle Eastern']
```


```python
GENDER_LABELS = ['Male', 'Female']
```


```python
AGE_LABELS = ['0-2', '3-9', '10-19', '20-29', '30-39', '40-49', '50-59', '60-69', '70+']
```


```python
EMOTION_LABELS = ['Anger', 'Contempt', 'Disgust', 'Fear', 'Happiness', 'Neutral', 'Sadness', 'Surprise']
```


```python
FAIRFACE_SEGMENTS = [('race', 0, 7), ('gender', 7, 2), ('age', 9, 9)]
```


## kit.runtime.postprocess.classify.softmax

```python
def softmax(logits: Sequence[float], temperature: float=1.0) -> np.ndarray
```

Numerically-stable 1-D softmax, optionally temperature-scaled.

`temperature` divides the logits before exponentiating: >1 softens the
distribution, <1 sharpens it, and 1.0 (the default) is the plain softmax.
The calibration POLICY -- which temperature each head gets -- lives in
`kit.logic.attributes.AttributeConfig`; this layer only does the arithmetic.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/postprocess/classify.py#L45)

## kit.runtime.postprocess.classify.argmax

```python
def argmax(logits: Sequence[float]) -> int
```

将 logits 展平，返回最大值的整数索引；空输入返回 -1。不返回分数，不执行 softmax。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/postprocess/classify.py#L63)

## kit.runtime.postprocess.classify.topk

```python
def topk(logits: Sequence[float], k: int=3, labels: Optional[Sequence[str]]=None) -> List[Tuple]
```

Return the top-k (label_or_index, probability) pairs, prob-descending.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/postprocess/classify.py#L68)

## kit.runtime.postprocess.classify.classify_head

```python
def classify_head(logits: Sequence[float], labels: Optional[Sequence[str]]=None, temperature: float=1.0) -> dict
```

Softmax + argmax one head. Returns index / label / confidence / probs.

`temperature` divides the logits before the softmax (1.0 = plain softmax,
the historical behaviour). It exists because a ResNet classification head is
systematically overconfident, so a deployment that thresholds `confidence`
needs the correction fitted on a held-out set; see `kit.logic.attributes`,
which owns the per-head calibration policy.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/postprocess/classify.py#L84)

## kit.runtime.postprocess.classify.split_heads

```python
def split_heads(vec: Sequence[float], segments: Sequence[Tuple[str, int, int]], labels_by_head: Optional[dict]=None, temperature: Optional[dict]=None) -> dict
```

Slice a flat logit vector into contiguous heads and classify each.

segments : [(name, start, length), ...]
labels_by_head : optional {name: [labels]} to attach string labels.
temperature : optional {name: float} per-head softmax temperature; a head
              absent from the dict keeps 1.0 (plain softmax).
Returns {name: classify_head(...)}.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/postprocess/classify.py#L108)

## kit.runtime.postprocess.classify.logits_from

```python
def logits_from(outputs, size: Optional[int]=None) -> np.ndarray
```

Extract the classifier logit vector from a list of raw RKNN outputs.

If `size` is given, prefer the tensor whose element count matches it;
otherwise fall back to the largest tensor (single-output models).

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/postprocess/classify.py#L130)

## kit.runtime.postprocess.classify.fairface_decode

```python
def fairface_decode(outputs, temperature: Optional[dict]=None) -> dict
```

(1,18) FairFace head -> {race,gender,age} each a classify_head dict.

`temperature` is an optional {"race"/"gender"/"age": float} calibration map;
omitted or 1.0 reproduces the plain softmax exactly.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/postprocess/classify.py#L147)

## kit.runtime.postprocess.classify.emotion_decode

```python
def emotion_decode(outputs, temperature: float=1.0) -> dict
```

(1,8) emotion head -> single classify_head dict.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/postprocess/classify.py#L160)

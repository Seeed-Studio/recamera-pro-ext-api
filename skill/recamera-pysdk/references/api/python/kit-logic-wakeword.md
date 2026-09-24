# kit.logic.wakeword

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/logic/wakeword.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/wakeword.py)；签名由 AST 提取，不导入硬件依赖。

KWS 或 ASR 关键词唤醒接口；Sherpa 后端依赖额外 runtime/模型，不能假定固件默认包含。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

Wake-word / keyword-spotting for reCamera Pro voice apps.

P2 of the voice pipeline (docs/guide/voice-app.md §2/§3). Sits in front of the
state machine's `idle` state: continuously consumes the `PcmFrame` stream and
fires a `WakeEvent` when the configured wake word is heard. Hidden behind the
`WakeWord` ABC so the state machine never cares which backend detected the wake
-- when the official R8 firmware exposes RK's built-in AAD/wakeup, a new backend
drops in here and nothing downstream changes.

Two interchangeable backends ship here:

1. `SherpaKwsWakeWord` -- PREFERRED. sherpa-onnx `KeywordSpotter` (a tiny
   streaming zipformer transducer). Always-listening, low CPU, no transcription
   in the idle loop. Needs a KWS model (encoder/decoder/joiner + tokens) and a
   `keywords_file`. We ship the gigaspeech 3.3M English KWS model and a custom
   keyword "HELLO CAMERA" (BPE: `▁HE LL O ▁CAME RA`).
   Model artifacts (staged on device, reuse verbatim):
       /userdata/tmp/asr/kws/encoder.int8.onnx   (~4.6 MB)
       /userdata/tmp/asr/kws/decoder.int8.onnx
       /userdata/tmp/asr/kws/joiner.int8.onnx
       /userdata/tmp/asr/kws/tokens.txt
       /userdata/tmp/asr/kws/keywords.txt

2. `AsrKeywordWakeWord` -- FALLBACK, zero extra models. Runs the already-proven
   VAD + SenseVoice ASR: endpoint a short utterance, transcribe it, and wake if
   the configured wake phrase is a substring of the transcript. Slower/heavier
   in idle (it transcribes every utterance) but rock-solid and trivially
   reconfigurable to any phrase / language ("你好小西", "hey camera", ...).

sherpa-onnx KeywordSpotter contract (verified on device, 1.13.4, 2026-08-09):
    KeywordSpotter(tokens, encoder, decoder, joiner, keywords_file, ...)
    .create_stream() -> stream
    stream.accept_waveform(sample_rate, float32[])
    .is_ready(stream) / .decode_stream(stream)
    .get_result(stream) -> str  (non-empty == a keyword just fired)
    .reset_stream(stream)       (call after a hit to re-arm)

## 常量与类型别名

以下为该版本源码值；默认地址不等于所有固件都开放该服务。

```python
DEFAULT_KWS_DIR = '/userdata/tmp/asr/kws'
```


## kit.logic.wakeword.WakeEvent

```python
@dataclass
class WakeEvent
```

Emitted the moment a wake word is detected.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
keyword: str
backend: str
score: float = 1.0
transcript: str = ''
```

## kit.logic.wakeword.WakeWord

```python
class WakeWord(ABC)
```

Abstract wake-word detector. Fed the raw PcmFrame stream in `idle`.

### kit.logic.wakeword.WakeWord.accept

```python
@abstractmethod
def accept(self, frame: Union[bytes, 'object']) -> Optional[WakeEvent]
```

Consume one chunk; return a WakeEvent iff the wake word just fired.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/wakeword.py#L69)

### kit.logic.wakeword.WakeWord.reset

```python
def reset(self) -> None
```

Re-arm / drop partial state (state machine calls on entering idle).

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/wakeword.py#L73)

## kit.logic.wakeword.SherpaKwsWakeWord

```python
class SherpaKwsWakeWord(WakeWord)
```

Always-listening KWS via a small streaming transducer. No transcription.

### kit.logic.wakeword.SherpaKwsWakeWord.accept

```python
def accept(self, frame: Union[bytes, 'object']) -> Optional[WakeEvent]
```

输入音频采样与采样率，推进 Sherpa 在线关键词识别；返回唤醒事件或无事件结果。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/wakeword.py#L106)

### kit.logic.wakeword.SherpaKwsWakeWord.reset

```python
def reset(self) -> None
```

重置在线 KWS 流状态，返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/wakeword.py#L120)

### kit.logic.wakeword.SherpaKwsWakeWord.__init__

```python
def __init__(self, keywords_file: str=f'{DEFAULT_KWS_DIR}/keywords.txt', *, tokens: str=f'{DEFAULT_KWS_DIR}/tokens.txt', encoder: str=f'{DEFAULT_KWS_DIR}/encoder.int8.onnx', decoder: str=f'{DEFAULT_KWS_DIR}/decoder.int8.onnx', joiner: str=f'{DEFAULT_KWS_DIR}/joiner.int8.onnx', num_threads: int=1, sample_rate: int=16000, keywords_score: float=1.5, keywords_threshold: float=0.25, provider: str='cpu')
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/wakeword.py#L81)

## kit.logic.wakeword.AsrKeywordWakeWord

```python
class AsrKeywordWakeWord(WakeWord)
```

Wake when an endpointed utterance's transcript contains the wake phrase.

Reuses the proven VAD + ASR stack, so it needs no extra model and works for
any phrase/language out of the box. `asr` is a `kit.asr.Asr`; `vad` is an
owned `VadSegmenter` (separate from the state machine's listening VAD).

### kit.logic.wakeword.AsrKeywordWakeWord.accept

```python
def accept(self, frame: Union[bytes, 'object']) -> Optional[WakeEvent]
```

消费音频块，按 ASR 结果匹配关键词并返回 WakeEvent 或 None；时延取决于 ASR 分段。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/wakeword.py#L146)

### kit.logic.wakeword.AsrKeywordWakeWord.reset

```python
def reset(self) -> None
```

清空累计音频/关键词状态，返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/wakeword.py#L157)

### kit.logic.wakeword.AsrKeywordWakeWord.__init__

```python
def __init__(self, asr, keywords: Union[str, List[str]], *, vad: Optional[VadSegmenter]=None, vad_kwargs: Optional[dict]=None)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/wakeword.py#L133)

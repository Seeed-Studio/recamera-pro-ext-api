# kit.logic.vad

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/logic/vad.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/vad.py)；签名由 AST 提取，不导入硬件依赖。

PCM 语音活动分段器；采样率、块长、门限决定切分，输出语音段而非识别文本。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

Voice Activity Detection (endpointing) for reCamera Pro voice apps.

P2 of the voice pipeline (docs/guide/voice-app.md §2/§3). Wraps sherpa-onnx's
built-in **silero VAD** (`sherpa_onnx.VoiceActivityDetector`) behind a small,
app-facing interface so the state machine (`kit.logic.voice_sm`) only ever sees
"a stream of `PcmFrame` in, complete `SpeechSegment`s out". Its job is
endpointing: turn a continuous 16 kHz mono PCM stream into utterance segments,
cutting on trailing silence (`min_silence_duration`) or a hard cap
(`max_speech_duration`).

Model artifact (staged on device in the SHARED model dir, reuse verbatim):
    /userdata/local/models/asr/silero_vad.onnx   silero VAD v5 ONNX (~2.2 MB)

sherpa-onnx VAD contract (verified on device, sherpa_onnx 1.13.4, 2026-08-09):
    VoiceActivityDetector.accept_waveform(float32[])   # feed any-length chunk
    .empty()/.front/.pop()                             # drain finished segments
    .is_speech_detected()                              # live "in speech" flag
    .flush()                                           # force-close last segment
    front -> SpeechSegment{ samples: float32[], start: int (sample index) }

sherpa is imported lazily in __init__ so importing this module for type hints on
a host without sherpa does not fail.

## 常量与类型别名

以下为该版本源码值；默认地址不等于所有固件都开放该服务。

```python
DEFAULT_VAD_MODEL = '/userdata/local/models/asr/silero_vad.onnx'
```


## kit.logic.vad.SpeechSegment

```python
@dataclass
class SpeechSegment
```

One endpointed utterance emitted by the VAD.

`pcm` is little-endian int16 16 kHz mono bytes -- exactly what `Asr.transcribe`
and `PcmFrame.pcm` expect, so a segment feeds straight into transcription.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
pcm: bytes
start_sec: float
duration_sec: float
```

## kit.logic.vad.VadSegmenter

```python
class VadSegmenter
```

Silero-VAD endpointer: PcmFrame stream in -> SpeechSegment stream out.

Typical use (driven by the state machine)::

    vad = VadSegmenter()
    for frame in audio_source:
        vad.accept(frame)
        for seg in vad.segments():      # 0..n finished utterances
            text = asr.transcribe(seg.pcm).text
    vad.flush()                          # at stream end, close trailing speech
    for seg in vad.segments():
        ...

### kit.logic.vad.VadSegmenter.__init__

```python
def __init__(self, model: str=DEFAULT_VAD_MODEL, *, sample_rate: int=16000, threshold: float=0.5, min_silence_duration: float=0.6, min_speech_duration: float=0.25, max_speech_duration: float=15.0, window_size: int=512, num_threads: int=1, buffer_seconds: float=30.0, preroll_ms: float=300.0)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/vad.py#L75)

### kit.logic.vad.VadSegmenter.accept

```python
def accept(self, pcm: Union[bytes, 'object']) -> None
```

Feed one chunk (PcmFrame / int16 bytes / ndarray). Any length is fine.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/vad.py#L122)

### kit.logic.vad.VadSegmenter.segments

```python
def segments(self) -> Iterator[SpeechSegment]
```

Yield every finished utterance currently buffered, oldest first.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/vad.py#L147)

### kit.logic.vad.VadSegmenter.is_speech

```python
def is_speech(self) -> bool
```

True while the model currently believes speech is ongoing.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/vad.py#L165)

### kit.logic.vad.VadSegmenter.flush

```python
def flush(self) -> None
```

Force-close any in-progress speech (call at end-of-stream).

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/vad.py#L169)

### kit.logic.vad.VadSegmenter.reset

```python
def reset(self) -> None
```

Drop all state/buffered segments (call when entering a fresh listen).

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/vad.py#L173)

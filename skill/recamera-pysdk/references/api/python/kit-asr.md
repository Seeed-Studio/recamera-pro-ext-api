# kit.asr

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/asr.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/asr.py)；签名由 AST 提取，不导入硬件依赖。

语音识别统一接口，结果含文本与时间信息；实际 backend、模型、音源需要匹配目标环境。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

Offline ASR wrapper for reCamera Pro (Rockchip RV1126B) -- voxedge consumer.

Path B (voxedge-consumer) refactor: the `Asr` class is now a THIN WRAPPER around
a voxedge `ASRBackend` (voxedge.backends.base). Instead of calling sherpa-onnx
`from_sense_voice` directly, it delegates transcription to a swappable voxedge
backend selected by the `backend=` argument:

    backend="sherpa"  (default)  CPU SenseVoice-int8 via voxedge SherpaASRBackend
    backend="rk"                 NPU w4a16 SenseVoice via kit.asr_rknn_backend
                                 (a lightweight voxedge ASRBackend subclass that
                                  reuses our verified rknnlite + fbank + CTC decode)

Everything downstream is unchanged: `transcribe(pcm_16k_mono) -> AsrResult`
(still unpackable as `(text, info)`), and we keep computing `elapsed / rtf /
audio_sec` ourselves (voxedge's `TranscriptionResult` carries only text +
language). The heavy model load happens once in `__init__`; `transcribe()` is
the hot path fed by an `AudioSource`.

Why a thin kit-side subclass for the CPU path
---------------------------------------------
voxedge's stock `SherpaASRBackend` exposes only `transcribe(wav_bytes)` (which
needs `soundfile`/libsndfile -- awkward on the musl device) and resolves the
SenseVoice model via a `{model_root}/sensevoice/sherpa-onnx-sense-voice-*` glob.
Our device stages a FLAT layout (`/userdata/tmp/asr/model.int8.onnx`) and the
venv has no libsndfile. So `_SenseVoiceCPUBackend` below subclasses the voxedge
backend to (a) load the offline recognizer from our explicit model/tokens paths
with `num_threads=4` (byte-identical to the previously-verified spike), and
(b) add `transcribe_array(float32_16k)` -- a numpy-in, soundfile-free entry that
plugs into voxedge's `supports_offline_streaming` contract. It reuses voxedge's
ABC, `SherpaASRConfig`, capability reporting and `resolve_reported_language`.

Feasibility (verified on device <device-host>, firmware 6.1.157, 2026-08-09)
------------------------------------------------------------------------------
sherpa-onnx CPU + SenseVoice int8 ONNX decodes 16k mono correctly.
Known-good baseline: `asr_example_zh.wav` ->
"欢迎大家来体验达摩院推出的语音识别模型。".

Runtime dependency
------------------
`voxedge` (pure python core, +numpy) plus `sherpa_onnx` live in the device venv
`/userdata/rknnenv`. voxedge + sherpa are imported LAZILY inside `Asr.__init__`
so importing this module (e.g. for the RtspAudioSource half) does NOT require
them on hosts that only exercise the audio path.

Model artifacts (staged on device by the feasibility spike, reuse verbatim):
    /userdata/tmp/asr/model.int8.onnx   SenseVoice int8 ONNX
    /userdata/tmp/asr/tokens.txt        token table

## 常量与类型别名

以下为该版本源码值；默认地址不等于所有固件都开放该服务。

```python
DEFAULT_MODEL = '/userdata/tmp/asr/model.int8.onnx'
```


```python
DEFAULT_TOKENS = '/userdata/tmp/asr/tokens.txt'
```


## kit.asr.AsrResult

```python
@dataclass
class AsrResult
```

Structured transcription result (also unpackable as `(text, info)`).

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
text: str
elapsed: float
audio_sec: float
rtf: float
language: str = ''
```

### kit.asr.AsrResult.__iter__

```python
def __iter__(self)
```

返回本对象定义的迭代器；迭代元素与借用有效期见类说明。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/asr.py#L71)

## kit.asr.Asr

```python
class Asr
```

voxedge-backed SenseVoice offline recognizer, loaded once.

Example:
    asr = Asr()                       # CPU SenseVoice via voxedge, loads once
    asr = Asr(backend="rk")           # NPU w4a16 via voxedge (kit rk backend)
    text, info = asr.transcribe(pcm)  # pcm = int16 ndarray | bytes, 16k mono

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
unload = close
```

### kit.asr.Asr.__init__

```python
def __init__(self, model: str=DEFAULT_MODEL, tokens: str=DEFAULT_TOKENS, *, backend: str='sherpa', num_threads: int=4, use_itn: bool=True, language: str='auto', sample_rate: int=16000, debug: bool=False, rknn_backend=None)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/asr.py#L172)

### kit.asr.Asr.close

```python
def close(self) -> None
```

Release backend resources exactly once.

The RK backend destroys every RKNN context before releasing its shared
broker lease.  CPU/third-party voxedge backends are feature-detected so
they remain source compatible.  A cleanup error leaves ``_closed``
false, allowing a retry while the RK backend retains its lease
fail-closed.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/asr.py#L222)

### kit.asr.Asr.__enter__

```python
def __enter__(self) -> 'Asr'
```

进入上下文管理器，返回其受管对象；与 __exit__ 配对使用，避免异常路径遗留资源。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/asr.py#L244)

### kit.asr.Asr.__exit__

```python
def __exit__(self, exc_type, exc, traceback) -> bool
```

离开上下文并执行本类的清理方法；异常传播/清理失败语义见类说明，不把退出视作任务已完成。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/asr.py#L249)

### kit.asr.Asr.backend

```python
@property
def backend(self)
```

The underlying voxedge `ASRBackend` (for capability introspection).

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/asr.py#L258)

### kit.asr.Asr.transcribe

```python
def transcribe(self, pcm: Union[bytes, 'object'], sample_rate: Optional[int]=None) -> AsrResult
```

Transcribe one utterance of 16k mono PCM through the voxedge backend.

`pcm` may be a little-endian int16 `bytes` buffer (as carried by
`PcmFrame.pcm`) or a numpy int16/float32 array. Returns `AsrResult`,
which also unpacks as `(text, info_dict)`. We keep our own timing
(elapsed / audio_sec / rtf); voxedge supplies text + language.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/asr.py#L279)

### kit.asr.Asr.transcribe_wav

```python
def transcribe_wav(self, path: str) -> AsrResult
```

Convenience: read a 16k(-ish) WAV file and transcribe it.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/asr.py#L307)

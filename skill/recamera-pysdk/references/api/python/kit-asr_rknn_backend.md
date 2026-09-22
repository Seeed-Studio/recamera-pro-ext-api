# kit.asr_rknn_backend

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/asr_rknn_backend.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/asr_rknn_backend.py)；签名由 AST 提取，不导入硬件依赖。

RKNN SenseVoice ASR 后端装配；需 tokenizer、模型工件和声明的 NPU 资源，不自动提供语音模型。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

NPU (rv1126b) SenseVoice w4a16 ASR backend -- voxedge ASRBackend subclass.

Phase-2(a) of the voxedge-consumer refactor (see kit/asr.py). This is a
LIGHTWEIGHT voxedge `ASRBackend` implementation that reuses our already-verified
on-device decode:

    kaldi_native_fbank (80-bin, hamming)  ->  LFR (m=7,n=6, 560-dim)  ->  CMVN
    -> 4 SenseVoice prompt frames (lang / event / itn embeddings)
    -> RKNNLite encoder (w4a16, single-core init_runtime -- NO core_mask)
    -> greedy CTC collapse -> sentencepiece detokenize

Why NOT voxedge's stock `RKASRBackend` (voxedge.backends.rk.asr): that adapter
wraps the full `rkvoice_stream` stack (rknn-toolkit-lite2 + spm + kaldi_fbank +
rkvoice_stream, ~50-100 MB, and its `sensevoice_rknn.py` hard-codes
`core_mask=NPU_CORE_0` which is a 3576/3588 multi-core concept that ERRORS on the
single-core rv1126b). On a 2 GB device we instead implement the voxedge
`ASRBackend` interface directly over the exact decode we already proved works.
The backend is fully swappable via `Asr(backend="rk")` and satisfies the same
`transcribe_array(float32_16k) -> TranscriptionResult` contract as the CPU path,
so downstream (VAD / wake / state machine / app) is untouched.

Ported verbatim (numerically) from the device spike scripts
    /userdata/tmp/asr/device_e2e_rv.py
    /userdata/tmp/asr/device_decode_lowmem.py

Runtime deps (device venv /userdata/rknnenv): rknn-toolkit-lite2 (rknnlite),
kaldi_native_fbank, sentencepiece, numpy. All imported LAZILY in `preload()` so
this module imports on a Mac / CPU host without the NPU runtime.

Model + assets (staged on device):
    sensevoice_rv1126b_w4a16.rknn   the w4a16 encoder (127 MB)
    am.mvn                          CMVN stats (two 560-dim vectors)
    embedding.npy                   SenseVoice prompt embeddings
    chn_jpn_yue_eng_ko_spectok.bpe.model   sentencepiece model

## 常量与类型别名

以下为该版本源码值；默认地址不等于所有固件都开放该服务。

```python
T_SHORT = 100
```


```python
T_LONG = 344
```


```python
T_FIXED = T_LONG
```


```python
LFR_DIM = 560
```


```python
BLANK_ID = 0
```


```python
DEFAULT_RKNN_NAME = 'sensevoice_rv1126b_w4a16.rknn'
```


```python
DEFAULT_RKNN_SHORT_NAME = 'sensevoice_rv1126b_w4a16_t100.rknn'
```


```python
DEFAULT_CMVN_NAME = 'am.mvn'
```


```python
DEFAULT_EMB_NAME = 'embedding.npy'
```


```python
DEFAULT_BPE_NAME = 'chn_jpn_yue_eng_ko_spectok.bpe.model'
```


## kit.asr_rknn_backend.RknnSenseVoiceBackend

```python
class RknnSenseVoiceBackend(ASRBackend)
```

voxedge `ASRBackend` over the rv1126b w4a16 SenseVoice RKNN encoder.

Offline backend that opts into ``supports_offline_streaming`` so it gets the
generic voxedge offline->streaming adapter + STREAMING capability for free,
exactly like the CPU SenseVoice path.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
supports_offline_streaming = True
supports_hot_reload = True
```

### kit.asr_rknn_backend.RknnSenseVoiceBackend.__init__

```python
def __init__(self, rknn_model: str, cmvn_path: str, embedding_path: str, bpe_path: str, *, rknn_model_short: Optional[str]=None, language: str='auto', textnorm: str='withitn', debug: bool=False, lease=None, lease_factory: Optional[Callable[[], Any]]=None, lease_timeout: Optional[float]=30.0, runtime_factory: Optional[Callable[[], Any]]=None, sentencepiece_factory: Optional[Callable[[], Any]]=None)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/asr_rknn_backend.py#L184)

### kit.asr_rknn_backend.RknnSenseVoiceBackend.name

```python
@property
def name(self) -> str
```

返回后端名称标识，供能力与诊断报告使用。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/asr_rknn_backend.py#L254)

### kit.asr_rknn_backend.RknnSenseVoiceBackend.capabilities

```python
@property
def capabilities(self) -> set
```

返回后端声明的能力集合；不能用它替代模型实际加载和语音识别验证。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/asr_rknn_backend.py#L258)

### kit.asr_rknn_backend.RknnSenseVoiceBackend.sample_rate

```python
@property
def sample_rate(self) -> int
```

返回后端要求的 PCM 采样率（Hz）；输入 samples 必须与之匹配。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/asr_rknn_backend.py#L266)

### kit.asr_rknn_backend.RknnSenseVoiceBackend.is_ready

```python
def is_ready(self) -> bool
```

返回后端是否已具备执行识别的就绪状态；不证明某段音频的识别准确率。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/asr_rknn_backend.py#L269)

### kit.asr_rknn_backend.RknnSenseVoiceBackend.preload

```python
def preload(self) -> None
```

Load assets and RKNN contexts as one broker-owned transaction.

CPU-side assets are validated first so a missing BPE/CMVN file never
pauses the built-in detector.  The external lease is then acquired
*before* constructing any RKNNLite object.  READY is emitted only after
every required initialization step succeeds (the optional short tier
may explicitly degrade after its partial context has been released).

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/asr_rknn_backend.py#L275)

### kit.asr_rknn_backend.RknnSenseVoiceBackend.unload

```python
def unload(self) -> None
```

Destroy every RKNN context, then release the shared lease once.

Idempotent after success.  A native release failure is deliberately
fail-closed: the live/uncertain context and lease are retained and the
exception is surfaced so a later call may retry.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/asr_rknn_backend.py#L496)

### kit.asr_rknn_backend.RknnSenseVoiceBackend.transcribe

```python
def transcribe(self, audio_bytes: bytes, language: str='auto') -> TranscriptionResult
```

One-shot offline transcription of WAV bytes (satisfies the ABC).

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/asr_rknn_backend.py#L579)

### kit.asr_rknn_backend.RknnSenseVoiceBackend.transcribe_array

```python
def transcribe_array(self, samples: np.ndarray, language: str='auto') -> TranscriptionResult
```

对 16 kHz PCM NumPy samples 执行 SenseVoice 识别，language 默认 auto，返回 TranscriptionResult；先 preload()。同一 context 的并发调用串行等待，递归调用或关闭中的 context 被拒绝；模型/权限/推理失败传播异常。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/asr_rknn_backend.py#L586)

## kit.asr_rknn_backend.build_rknn_backend

```python
def build_rknn_backend(model: Optional[str]=None, tokens: Optional[str]=None, *, language: str='auto', use_itn: bool=True, debug: bool=False, **_kw) -> RknnSenseVoiceBackend
```

Construct + preload the NPU backend. Called by ``kit.asr.Asr(backend='rk')``.

``tokens`` (the CPU sherpa tokens path) is ignored -- the NPU decode uses
the sentencepiece bpe model resolved alongside the rknn model.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/asr_rknn_backend.py#L827)

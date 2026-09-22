# kit.logic.voice_sm

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/logic/voice_sm.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/voice_sm.py)；签名由 AST 提取，不导入硬件依赖。

唤醒、聆听与识别的语音应用状态机；由给定音源、VAD、wakeword 和 ASR 组成。main 是命令行辅助入口。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

Voice interaction state machine for reCamera Pro (docs/guide/voice-app.md §0/§3).

This is the one piece of business logic the voice app owns -- everything else
(audio capture, VAD, KWS, ASR) is a swappable kit building block. It wires them
into the core interaction:

    idle ──wake word──► listening ──(silence N s | max len)──► transcribing
     ▲                    (VAD collects the utterance)              │
     └──────────────────────── transcript emitted ─────────────────┘

States (voice-app §0):
    idle          feed every PcmFrame to the WakeWord detector; wait for a hit.
    listening     wake fired -> feed frames to the VAD until it endpoints one
                  utterance (trailing silence >= vad.min_silence_duration) or a
                  hard `listen_timeout_sec` elapses with no speech.
    transcribing  run Asr.transcribe on the captured utterance, emit the text,
                  return to idle.

Events are pushed to an optional `on_event(dict)` callback (and, if given, a
kit ResultSink) so the app / debug panel / test harness can observe every
transition and the final transcript. The class is transport-agnostic: it just
pulls `PcmFrame`s from any `AudioSource` (live `RtspAudioSource` on device, or
`WavFileAudioSource` for injection tests).

## 常量与类型别名

以下为该版本源码值；默认地址不等于所有固件都开放该服务。

```python
IDLE = 'idle'
```


```python
LISTENING = 'listening'
```


```python
TRANSCRIBING = 'transcribing'
```


## kit.logic.voice_sm.VoiceStateMachine

```python
class VoiceStateMachine
```

把唤醒、VAD、ASR 和事件回调串联为语音状态机；音源和模型需调用方提供。

### kit.logic.voice_sm.VoiceStateMachine.__init__

```python
def __init__(self, audio_source, wakeword, vad, asr, *, on_event: Optional[Callable[[dict], None]]=None, listen_timeout_sec: float=8.0, verbose: bool=True)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/voice_sm.py#L43)

### kit.logic.voice_sm.VoiceStateMachine.open

```python
def open(self) -> 'VoiceStateMachine'
```

Open and probe the audio source exactly once.

Voice applications call this during their pre-READY transaction.  The
main loop calls it again defensively, but the second call is a no-op so
an ALSA/RTSP subprocess is never replaced or opened twice.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/voice_sm.py#L64)

### kit.logic.voice_sm.VoiceStateMachine.close

```python
def close(self) -> None
```

Close the pre-opened source; repeated calls are harmless.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/voice_sm.py#L90)

### kit.logic.voice_sm.VoiceStateMachine.run

```python
def run(self, *, max_wakes: int=0) -> int
```

Drive the machine over the audio source until it ends.

`max_wakes>0` stops after that many completed wake->transcript cycles
(used by the injection test); 0 runs until the stream ends. Returns the
number of transcripts emitted.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/voice_sm.py#L116)

## kit.logic.voice_sm.main

```python
def main(argv=None)
```

本模块独立语音演示/诊断 CLI 入口；不是应用中心的 manifest entry 契约。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/voice_sm.py#L239)

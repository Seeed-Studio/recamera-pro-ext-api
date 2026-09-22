# kit.adapters.audio_source

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/adapters/audio_source.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/audio_source.py)；签名由 AST 提取，不导入硬件依赖。

PCM 音源与音频帧。默认共享 ai_asr；显式 takeover 会涉及设备音频所有权，不适合作为普通应用默认行为。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

AudioSource adapter for reCamera Pro (Rockchip RV1126B).

L0 adapter layer (see docs/guide/voice-app.md §4/§5/§6, docs/guide/adapter-bootstrap.md §2.3).
The application only ever sees the `AudioSource` ABC + `PcmFrame` (16k mono PCM);
the concrete capture backend is swappable. When the official **R8 clean-PCM
broker** (`/var/run/recamera/audio.sock`, VQE-clean 16k mono) arrives, only a new
AudioSource implementation (`OfficialPcmSource`) is selected by the capability
registry -- no application (KWS / VAD / ASR / state-machine) code changes.

This module is the canonical home of the audio contract: `PcmFrame` and the
`AudioSource` ABC are defined here and re-exported by `official.py` so both the
workaround (`AlsaTakeoverSource`) and the migration stub (`OfficialPcmSource`)
implement the exact same interface.


P0 FEASIBILITY VERDICT (firmware 6.1.157, verified on device root@... 2026-08-09)
================================================================================
The single hardware mic (`/dev/snd/pcmC0D0c`, card0 = rockchip,rv1126b-acodec,
ONE capture subdevice) is **held exclusively** by `rkipc` (pid 939):

    $ arecord -D hw:0,0 -d 2 -f S16_LE -c 2 -r 22050 /tmp/t.wav
    arecord: main:831: audio open error: Device or resource busy   <-- EBUSY
    $ fuser /dev/snd/pcmC0D0c
    939                                                             <-- rkipc

ALSA is NOT configured with a `dsnoop` capture-sharing PCM, and rkipc opens the
raw `hw` device, so no second reader can attach while rkipc holds it.

`rkipc` is a SINGLE process that runs BOTH the camera/encoder/video pipeline AND
audio-in (`RK_MPI_AI`). Its RTSP server (`127.0.0.1:5554/live/*`) publishes a
*combined* stream -- `video H265` + `audio PCMA/22050/2` (the mic, G711A) -- that
`go2rtc` pulls and forwards to the app-market WebRTC viewer (verified live via
`http://127.0.0.1:1984/api/streams`). The rkipc binary itself contains the
string `"/oem/usr/etc/init.d/S50go2rtc restart"` and a `ser_rk_audio_restart`
handler: **changing rkipc's audio configuration cascades a go2rtc restart**,
because the audio RTP track it serves changes.

Consequences (the honest cost of "freeing the mic" today):
  1. There is NO runtime path to release ONLY the mic while leaving the exact
     video stream untouched. Every way to free `pcmC0D0c` either
       (a) sets `[audio.0] enable=0` + audio-deinit -> rkipc restarts go2rtc
           (video *blip* for live viewers) and drops the RTSP audio track, OR
       (b) kills/restarts rkipc -> video goes DOWN entirely.
  2. Taking over the mic also forfeits the hardware VQE (AEC/denoise/AGC in
     librkaudio) -- the app must bring its own software denoise (VOICE_APP §5.2).
  3. The official R8 PCM broker (`/var/run/recamera/audio.sock`) does not exist
     on this firmware, so `OfficialPcmSource` is still a stub.

=> **Mic takeover WITHOUT disturbing video is NOT feasible on 6.1.157.**
   `AlsaTakeoverSource` below is technically correct and *will* capture 16k mono
   PCM the moment the mic is free -- but on shipping firmware the mic is never
   free unless rkipc audio is disabled first, which blips video. Therefore the
   takeover lifecycle here **does NOT auto-disable rkipc audio**; it refuses with
   a clear, actionable error on EBUSY. The clean path is R8 (`OfficialPcmSource`);
   the only workaround that yields the mic is a product decision to accept a
   one-time go2rtc/video blip + permanent loss of the RTSP audio track & VQE
   while the voice app is active (opt-in, see `AlsaTakeoverSource(...,
   release_rkipc_audio=<callback>)`).


Capture backend
---------------
`AlsaTakeoverSource` shells out to `arecord` (already on device; zero extra
Python deps for the primary path) reading `plughw:0,0`, letting the ALSA `plug`
plugin downmix stereo->mono and resample 22050->16000 in-kernel, and streams raw
S16 mono 16k PCM off arecord's stdout in fixed chunks -> `PcmFrame`. A pure-numpy
resample/downmix path (`_downmix_to_mono` / `_resample_linear`) is provided for
the raw-`hw` variant and is unit-testable off-device.

## 常量与类型别名

以下为该版本源码值；默认地址不等于所有固件都开放该服务。

```python
DEFAULT_AUDIO_FILTER = 'loudnorm=I=-16:TP=-1.5'
```


## kit.adapters.audio_source.PcmFrame

```python
@dataclass
class PcmFrame
```

One chunk of PCM audio (docs/guide/adapter-bootstrap.md §2.3 contract).

`pcm` is little-endian signed 16-bit samples. For the app-facing contract
this is always 16 kHz mono, so `len(pcm) // 2` samples == duration * 16000.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
pcm: bytes
rate: int = 16000
ch: int = 1
pts: float = 0.0
```

### kit.adapters.audio_source.PcmFrame.n_samples

```python
@property
def n_samples(self) -> int
```

返回每声道样本数：len(pcm)/(2*ch)，PCM 为 signed 16-bit little-endian。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/audio_source.py#L99)

## kit.adapters.audio_source.AudioSource

```python
class AudioSource(ABC)
```

Abstract PCM producer. Applications depend only on this (16 kHz mono).

Usage:
    with open_audio_source() as src:
        while True:
            frame = src.read()      # -> PcmFrame | None (None == stream end)
            if frame is None:
                break
            feed_kws_and_asr(frame.pcm)

### kit.adapters.audio_source.AudioSource.read

```python
@abstractmethod
def read(self) -> Optional[PcmFrame]
```

Return the next PCM chunk, or None at end-of-stream.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/audio_source.py#L116)

### kit.adapters.audio_source.AudioSource.open

```python
def open(self) -> 'AudioSource'
```

打开所选后端并准备读取；设备忙、依赖缺失或连接失败时可能抛异常。之后按该音源的 read 契约读取，并配对 close。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/audio_source.py#L120)

### kit.adapters.audio_source.AudioSource.close

```python
def close(self) -> None
```

关闭本对象持有的连接/线程/设备等资源。具体幂等性、在途任务及失败处理见该类生命周期说明；不要在关闭后继续发送或读取。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/audio_source.py#L123)

### kit.adapters.audio_source.AudioSource.__enter__

```python
def __enter__(self)
```

进入上下文管理器，返回其受管对象；与 __exit__ 配对使用，避免异常路径遗留资源。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/audio_source.py#L126)

### kit.adapters.audio_source.AudioSource.__exit__

```python
def __exit__(self, *exc)
```

离开上下文并执行本类的清理方法；异常传播/清理失败语义见类说明，不把退出视作任务已完成。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/audio_source.py#L129)

## kit.adapters.audio_source.AudioDeviceBusy

```python
class AudioDeviceBusy(ResourceBusyError)
```

Raised when the ALSA capture device is held exclusively (rkipc).

继承接口：[kit.errors.ResourceBusyError](kit-errors.md)。

## kit.adapters.audio_source.pcm_stats

```python
def pcm_stats(pcm: bytes) -> dict
```

Cheap energy/liveness stats to prove a WAV is real audio, not silence.

Returns mean, peak, RMS and a crude dBFS. Silence -> rms ~ 0; a real mic in
a quiet room -> rms typically > ~30 (‑60 dBFS). Used by the on-device
verification and by callers wanting a sanity gate before ASR.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/audio_source.py#L209)

## kit.adapters.audio_source.AlsaTakeoverSource

```python
class AlsaTakeoverSource(AudioSource)
```

Capture 16 kHz mono PCM from the RV1126B mic via an `arecord` subprocess.

Primary path (`use_plug=True`, default): reads `plughw:CARD,DEV` asking
arecord for `-c1 -r16000`; the ALSA `plug` plugin does the stereo->mono
downmix + 22050->16000 resample, so Python just reads raw S16 mono bytes.

Raw path (`use_plug=False`): reads native `hw:CARD,DEV` at 22050/2ch and
downmixes+resamples in numpy (`_downmix_to_mono` + `_resample_linear`).

IMPORTANT (see module verdict): on firmware 6.1.157 the mic is held by
rkipc, so `open()` will hit `Device or resource busy`. We do NOT silently
disable rkipc audio (that blips the live video). If the product explicitly
accepts that cost, pass `release_rkipc_audio` -- a caller-provided callback
that frees the mic (e.g. flips `[audio.0] enable=0` and triggers rkipc audio
re-init) and returns a `restore` callable invoked on `close()`. Absent that
callback, EBUSY raises `AudioDeviceBusy` with guidance to use R8 instead.

### kit.adapters.audio_source.AlsaTakeoverSource.read

```python
def read(self) -> Optional[PcmFrame]
```

读取一块独占 ALSA 输入并按配置重采样/混合声道，返回 PcmFrame；无有效数据时返回 None。该后端可能停用原有音频服务，不作为普通共享音频应用默认选择。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/audio_source.py#L345)

### kit.adapters.audio_source.AlsaTakeoverSource.open

```python
def open(self) -> 'AlsaTakeoverSource'
```

打开所选后端并准备读取；设备忙、依赖缺失或连接失败时可能抛异常。之后按该音源的 read 契约读取，并配对 close。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/audio_source.py#L295)

### kit.adapters.audio_source.AlsaTakeoverSource.close

```python
def close(self) -> None
```

关闭本对象持有的连接/线程/设备等资源。具体幂等性、在途任务及失败处理见该类生命周期说明；不要在关闭后继续发送或读取。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/audio_source.py#L366)

### kit.adapters.audio_source.AlsaTakeoverSource.__enter__

```python
def __enter__(self)
```

进入上下文管理器，返回其受管对象；与 __exit__ 配对使用，避免异常路径遗留资源。

此方法定义于基类 `AudioSource`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/audio_source.py#L126)

### kit.adapters.audio_source.AlsaTakeoverSource.__exit__

```python
def __exit__(self, *exc)
```

离开上下文并执行本类的清理方法；异常传播/清理失败语义见类说明，不把退出视作任务已完成。

此方法定义于基类 `AudioSource`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/audio_source.py#L129)

### kit.adapters.audio_source.AlsaTakeoverSource.__init__

```python
def __init__(self, device: str='hw:0,0', *, target_rate: int=16000, target_ch: int=1, capture_rate: int=22050, capture_ch: int=2, chunk_ms: int=100, use_plug: bool=True, arecord_bin: str='arecord', release_rkipc_audio: Optional[Callable[[], Callable[[], None]]]=None)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/audio_source.py#L247)

## kit.adapters.audio_source.RtspAudioSource

```python
class RtspAudioSource(AudioSource)
```

Capture 16 kHz mono PCM by demuxing the audio track from rkipc's combined
RTSP stream -- WITHOUT touching the mic, the video, or /dev/snd.

rkipc publishes video+audio on `rtsp://admin:admin@127.0.0.1:5554/live/1`
(the mic, G711A/PCMA @ 22050/2). We pull that stream over TCP, drop video
(`-vn`) and let ffmpeg decode+downmix+resample the audio track to raw S16LE
16k mono on stdout, streamed off in fixed chunks -> `PcmFrame`.

This is the DEFAULT audio source on firmware 6.1.157 (verified feasible
2026-08-09): it needs no mic takeover (which would blip video, see
AlsaTakeoverSource) and no extra Python deps (ffmpeg already on device).
Trade-off: the mic still runs through rkipc's hardware VQE and the audio is
whatever rkipc encodes (22050 PCMA upstream). `AlsaTakeoverSource` remains
the mic-takeover fallback for when direct capture is explicitly wanted.

### kit.adapters.audio_source.RtspAudioSource.read

```python
def read(self) -> Optional[PcmFrame]
```

从 FFmpeg 解码 stdout 读取一块目标采样率的 PCM，返回 PcmFrame；流结束返回 None。可能阻塞等待数据。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/audio_source.py#L478)

### kit.adapters.audio_source.RtspAudioSource.open

```python
def open(self) -> 'RtspAudioSource'
```

打开所选后端并准备读取；设备忙、依赖缺失或连接失败时可能抛异常。之后按该音源的 read 契约读取，并配对 close。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/audio_source.py#L441)

### kit.adapters.audio_source.RtspAudioSource.close

```python
def close(self) -> None
```

关闭本对象持有的连接/线程/设备等资源。具体幂等性、在途任务及失败处理见该类生命周期说明；不要在关闭后继续发送或读取。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/audio_source.py#L488)

### kit.adapters.audio_source.RtspAudioSource.__enter__

```python
def __enter__(self)
```

进入上下文管理器，返回其受管对象；与 __exit__ 配对使用，避免异常路径遗留资源。

此方法定义于基类 `AudioSource`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/audio_source.py#L126)

### kit.adapters.audio_source.RtspAudioSource.__exit__

```python
def __exit__(self, *exc)
```

离开上下文并执行本类的清理方法；异常传播/清理失败语义见类说明，不把退出视作任务已完成。

此方法定义于基类 `AudioSource`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/audio_source.py#L129)

### kit.adapters.audio_source.RtspAudioSource.__init__

```python
def __init__(self, url: str='rtsp://admin:admin@127.0.0.1:5554/live/1', *, target_rate: int=16000, target_ch: int=1, chunk_ms: int=100, rtsp_transport: str='tcp', ffmpeg_bin: str='ffmpeg', audio_filter: Optional[str]=DEFAULT_AUDIO_FILTER)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/audio_source.py#L403)

## kit.adapters.audio_source.AiAsrAudioSource

```python
class AiAsrAudioSource(AudioSource)
```

Capture 16 kHz mono PCM from the firmware's reserved ALSA `ai_asr` PCM.

THIS IS THE OFFICIAL, RECOMMENDED audio path on reCamera Pro (RV1126B) --
see docs/guide/audio-pcm.md (authoritative, derived from the firmware's
/etc/asound.conf). It supersedes both workarounds (`RtspAudioSource`,
`AlsaTakeoverSource`) because it is clean and non-invasive:

  * The mic hardware (`hw:0,0`) is shared across processes via an ALSA
    `dsnoop` plugin. The firmware pre-declares four named capture PCMs that
    all read the SAME 6ch/16k dsnoop stream through their own cursor:
        ai_main  -> held by rkipc (camera+encoder+audio main program)
        ai_kws   -> held by the official keyword-detection service
        ai_asr   -> RESERVED for third-party ASR/audio apps  <-- we use this
        ai_debug -> reserved for debug recording
    Because dsnoop shares a ring buffer, `ai_asr` does NOT conflict with and
    does NOT need to stop rkipc: no mic takeover, no `Device or resource
    busy`, video and the RTSP audio track are untouched (unlike
    AlsaTakeoverSource, which fights rkipc for `hw:0,0`).

  * Hardware side is fixed at 16 kHz / S16_LE (dsnoop slave). Any higher
    `-r` would only trigger a software resample of the same 16 kHz source --
    so we always request exactly 16000 (no extra CPU, no extra information).

Channel layout (asound.conf `ai_2mic_2ref` route -> 4 output channels):
    ch0 = Mic 1        ch1 = Mic 2
    ch2 = Reference    ch3 = Reference (fill)
The reference channels (ch2/ch3) are the AEC playback-loopback reference,
carried IN the same stream -- reserved here for a FUTURE software AEC
(speexdsp / WebRTC AEC: feed ch0 as near-end, ch2 as far-end reference).
We do NOT use them for the ASR feed.

IMPORTANT -- NO VQE: `ai_asr` delivers the RAW microphone signal. rkipc's
hardware VQE (AEC / ANS / AGC in librkaudio) runs inside its own RK_MPI_AI
path and does NOT reach the dsnoop taps. Any denoise / AEC / AGC is the
app's responsibility (the loudnorm gain below is our only current step).

PERMISSIONS -- needs root or the `audio` group: dsnoop's IPC key is 0666,
but every client still opens `/dev/snd/pcmC0D0c` + `/dev/snd/controlC0`,
which are `root:audio 0660`. An SSH `admin` user (not in `audio`) fails with
`Cannot get card index` / `audio open error`. This is satisfied in the real
deployment: appmgr launches extension processes as root. Off-device / as a
non-audio user, `open()` surfaces a clear, actionable error.

Channel-selection policy (default `capture_ch=4`, `mic_channel=0`):
    We capture the NATIVE 4 channels and deterministically select Mic 1
    (hw ch0) with ffmpeg `pan=mono|c0=c0`. This is the robust path: the
    plug layer's own N->1 downmix behaviour is NOT verified in the firmware
    docs (it might average in the near-silent reference channels, halving
    mic energy). Set `capture_ch=1` to instead let the ALSA plug layer do
    the downmix (documented but unverified) and skip ffmpeg's pan.

GAIN -- loudnorm preserved from RtspAudioSource: the RV1126B mic is very
quiet (~-49 dBFS raw; see DEFAULT_AUDIO_FILTER above). Wake-word detection
empirically depends on lifting the level -- `loudnorm=I=-16:TP=-1.5` was the
only filter that still fired the KeywordSpotter at a stricter threshold. We
therefore run the identical ffmpeg loudnorm here (arecord | ffmpeg) so the
downstream KWS / VAD / ASR see the SAME normalized 16k-mono frames as the
RTSP path -- migrating the source is invisible to the pipeline. Set
`audio_filter` to ""/"none" for unity gain.

Pipeline:
    arecord -D ai_asr -f S16_LE -r 16000 -c <capture_ch> -t raw -q -
      | ffmpeg -f s16le -ar 16000 -ac <capture_ch> -i -
               -af "pan=mono|c0=c<mic>,loudnorm=I=-16:TP=-1.5"
               -ac 1 -ar 16000 -f s16le -
When no ffmpeg processing is needed (`capture_ch==1` AND filter disabled),
arecord already yields 16k mono and is read directly with no ffmpeg hop.

ON-DEVICE VERIFICATION TODO (blocked off-device / as non-audio user):
  1. Run as root (or add the run user to the `audio` group).
  2. Prove the tap is live & non-silent:
       arecord -D ai_asr -f S16_LE -r 16000 -c 4 -d 5 /tmp/ai_asr.wav
     then check ch0 RMS with pcm_stats() (> ~30 / > -60 dBFS = real audio).
  3. Confirm it does NOT disturb rkipc (video + RTSP audio keep running;
     no EBUSY -- dsnoop shared).
  4. End-to-end: run the voice app on `audio_source=ai_asr`, speak the wake
     word, confirm idle -> listening -> transcribing (loudnorm gives the KWS
     the same margin it had on the RTSP path).
  5. (Future AEC) Capture 4ch while the speaker plays; verify ch2/ch3 carry
     the playback reference (non-zero during playback) before wiring AEC.

### kit.adapters.audio_source.AiAsrAudioSource.read

```python
def read(self) -> Optional[PcmFrame]
```

从固件共享 ai_asr ALSA 源读取一块 PCM，返回 PcmFrame；读取结束返回 None。需可访问的音频设备和 arecord。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/audio_source.py#L730)

### kit.adapters.audio_source.AiAsrAudioSource.open

```python
def open(self) -> 'AiAsrAudioSource'
```

打开所选后端并准备读取；设备忙、依赖缺失或连接失败时可能抛异常。之后按该音源的 read 契约读取，并配对 close。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/audio_source.py#L659)

### kit.adapters.audio_source.AiAsrAudioSource.close

```python
def close(self) -> None
```

关闭本对象持有的连接/线程/设备等资源。具体幂等性、在途任务及失败处理见该类生命周期说明；不要在关闭后继续发送或读取。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/audio_source.py#L740)

### kit.adapters.audio_source.AiAsrAudioSource.__enter__

```python
def __enter__(self)
```

进入上下文管理器，返回其受管对象；与 __exit__ 配对使用，避免异常路径遗留资源。

此方法定义于基类 `AudioSource`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/audio_source.py#L126)

### kit.adapters.audio_source.AiAsrAudioSource.__exit__

```python
def __exit__(self, *exc)
```

离开上下文并执行本类的清理方法；异常传播/清理失败语义见类说明，不把退出视作任务已完成。

此方法定义于基类 `AudioSource`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/audio_source.py#L129)

### kit.adapters.audio_source.AiAsrAudioSource.__init__

```python
def __init__(self, device: str='ai_asr', *, target_rate: int=16000, target_ch: int=1, capture_rate: int=16000, capture_ch: int=4, mic_channel: int=0, chunk_ms: int=100, arecord_bin: str='arecord', ffmpeg_bin: str='ffmpeg', audio_filter: Optional[str]=DEFAULT_AUDIO_FILTER)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/audio_source.py#L585)

## kit.adapters.audio_source.WavFileAudioSource

```python
class WavFileAudioSource(AudioSource)
```

Replay a WAV file as a `PcmFrame` stream (16 kHz mono), for offline
injection tests of the voice pipeline WITHOUT a live mic/RTSP.

Reads the WAV, downmixes to mono + resamples to `target_rate` (reusing the
same numpy helpers as the raw-hw capture path), then hands it out in fixed
`chunk_ms` `PcmFrame`s exactly like a live source. Used by the on-device
state-machine verification: feed a "<wake word> + <sentence>" WAV in and
watch idle -> listening -> transcribing flow.

`pad_silence_sec` appends trailing silence so the VAD reliably endpoints the
final utterance even without an explicit flush(). `realtime=True` sleeps
`chunk_ms` between reads to emulate a live capture cadence.

### kit.adapters.audio_source.WavFileAudioSource.read

```python
def read(self) -> Optional[PcmFrame]
```

读取/转换下一块 WAV 样本，返回 PcmFrame；文件结束且不循环时返回 None，适合可控输入测试。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/audio_source.py#L808)

### kit.adapters.audio_source.WavFileAudioSource.open

```python
def open(self) -> 'WavFileAudioSource'
```

打开所选后端并准备读取；设备忙、依赖缺失或连接失败时可能抛异常。之后按该音源的 read 契约读取，并配对 close。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/audio_source.py#L793)

### kit.adapters.audio_source.WavFileAudioSource.close

```python
def close(self) -> None
```

关闭本对象持有的连接/线程/设备等资源。具体幂等性、在途任务及失败处理见该类生命周期说明；不要在关闭后继续发送或读取。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/audio_source.py#L818)

### kit.adapters.audio_source.WavFileAudioSource.__enter__

```python
def __enter__(self)
```

进入上下文管理器，返回其受管对象；与 __exit__ 配对使用，避免异常路径遗留资源。

此方法定义于基类 `AudioSource`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/audio_source.py#L126)

### kit.adapters.audio_source.WavFileAudioSource.__exit__

```python
def __exit__(self, *exc)
```

离开上下文并执行本类的清理方法；异常传播/清理失败语义见类说明，不把退出视作任务已完成。

此方法定义于基类 `AudioSource`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/audio_source.py#L129)

### kit.adapters.audio_source.WavFileAudioSource.__init__

```python
def __init__(self, path: str, *, target_rate: int=16000, target_ch: int=1, chunk_ms: int=100, pad_silence_sec: float=1.0, realtime: bool=False)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/audio_source.py#L773)

## kit.adapters.audio_source.open_audio_source

```python
def open_audio_source(**kw) -> AudioSource
```

Factory. Delegates to the capability registry, which returns
`OfficialPcmSource` when the R8 broker socket exists and otherwise the
`AlsaTakeoverSource` workaround. On today's firmware the socket is absent,
so the ALSA workaround is selected (and will raise `AudioDeviceBusy` on
shipping firmware -- see module verdict).

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/audio_source.py#L823)

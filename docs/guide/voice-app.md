# 语音应用设计:唤醒词 → 采集音频 → 转录(reCamera Pro)

> 新增一个语音应用 `voice-transcribe`:**本地唤醒词唤醒 → 采集本地音频 → STT 转录 → 出文本**。复用本地 `seeed-local-voice`(OpenVoiceStream)的引擎。
> 关联:`adapter-bootstrap.md`(AudioSource 适配层)、`kit-design.md`(应用分层)。R8 音频代理/VAD 的上游需求属内部设计文档,不在公开仓。

## 0. 一句话
`voice-transcribe` = 一个 self-hosted 应用:kit 的 **AudioSource** 拿 16k 单声道 PCM → **sherpa KWS** 常听 → **silero VAD** 断句 → 默认由 `kit.asr_rknn_backend` 通过平台 `inferenced` 调度服务在 NPU 上运行 SenseVoice w4a16 → 文本经 ResultSink WS 到 `/appcenter`。manifest v2 包把经签名校验的模型放在应用根目录 `models/asr`，服务只授权该安装路径；`sherpa` CPU ASR 是需要额外 ONNX 模型的可选后端。

## 1. 复用 seeed-local-voice(OpenVoiceStream)哪部分
它跨三仓,**我们只取最轻的引擎层**:
- ✅ **voxedge**(`../voxedge`,纯 Python/numpy,pip 可装 `pip install --pre voxedge`):ASR/VAD 的 ABC + 后端 `backends/{rk(RKNN),sherpa(sherpa-onnx CPU)}`、conversation loop。**这是我们要嵌的**。
- ⚪ **third_party/rkvoice-stream**(参考实现):voxedge 的 stock RK 后端可使用它，但当前 RV1126B 路径不依赖该包；本项目用 `kit.asr_rknn_backend` 直接封装已验证的 rknnlite + fbank + CTC decode。
- ❌ **server/**(FastAPI + Docker):完整产品服务,太重,**不用**——我们要的是"嵌进 app 的库",不是"起个语音服务器"。
- ❌ **agent/**(ovs_agent mic→speaker apps):是它自己的对话 agent,我们只要 ASR 转录,不要 TTS/对话回路(先不做)。

> 当前取舍:默认使用已验证的 **RKNN SenseVoice w4a16**；应用中心管理的 manifest v2 实例走 `inferenced` 多模型调度，独立/旧版直连才保留 `ExternalNpuLease` 兼容路径；KWS/VAD 留在 CPU。`sherpa-onnx` CPU ASR 仅作为显式可选后端，设备需另装 `model.int8.onnx` 与 `tokens.txt`。

### rkvoice-stream 静态可行性核实(2026-08-15 真机)

**结论:内存/磁盘不是瓶颈,瓶颈是 `rkvoice-stream` 根本没在设备上,也没有现成的离线安装包。**

- **设备上不存在**:`find / -xdev -iname '*rkvoice*'` 无命中;`/userdata/rknnenv/bin/python3 -c "import rkvoice_stream"` → `ModuleNotFoundError`。
- **内存余量**(`free -m`,`retail-vision` 正在跑时):total **1985 MB**,used 920,buff/cache 889,**available 1021 MB**,swap 0。
- **磁盘余量**:`/userdata` 11.3 G,已用 2.2 G,**可用 8.4 G**。RKNN venv `/userdata/rknnenv` 88.3 MB,ASR 模型目录 `/userdata/local/models/asr` 134.9 MB。
- **voice-transcribe 实际内存占用:未测**。该 app 当前未运行,`logs/app.log` 只记到 `ready -- listening`,不含 RSS/内存字段,无从回溯。要数字需单独跑一轮并采 `/proc/<pid>/status`。
- **顺带订正**:线上部署的 voice-transcribe **已经在跑 RKNN 版 SenseVoice**(日志:`rknn-toolkit-lite2 2.3.2` / `librknnrt 2.3.2` / `target platform: rv1126b` / `model inference type: static_shape`),而不是 sherpa-onnx CPU 后端;sherpa 只用在 KWS(`sherpa KeywordSpotter`)。也就是说"RKNN 提速"这条路已经用另一种方式走通了,`rkvoice-stream` 未必是必需项。

**方案商要用 rkvoice-stream 需自己解决**:交叉编译/取得 aarch64 的 `rkvoice-stream` 运行时并离线装进 `/userdata/rknnenv`(设备无外网包管理,本轮按约束未做任何安装),再补对应的 RKNN ASR 模型。

## 2. 三段管线 + 归属

```
mic ──(AudioSource 16k mono)──► KWS 常听 ──唤醒──► 录音窗口 ──► ASR(voxedge) ──► 文本 ──► ResultSink WS ──► /appcenter
      ▲ kit 适配层(ai_asr/RTSP)      ▲ 轻量常驻        ▲ VAD 判结束        ▲ 复用 voxedge      ▲ 复用现有
```

| 段 | 归属 | 今天做法 | 官方/未来 |
|---|---|---|---|
| **取音频** | kit `AudioSource` | 默认官方 ALSA `ai_asr` dsnoop 共享采集(不关闭 rkipc、不接管设备)；RTSP 音轨为回退 | 后续可切 VQE-clean PCM 代理，应用状态机不变 |
| **唤醒词 KWS** | kit `logic/wakeword` | 轻量常听 KWS(见 §3) | R8 暴露 RK 内置 AAD/wakeup(`fw_aad_aivad`)→ 直接订阅唤醒事件 |
| **VAD 断句** | voxedge VAD 后端 | voxedge 自带 VAD(sherpa/silero) | R8 暴露 RK VAD(`rkvad`) |
| **ASR 转录** | voxedge ABC + kit RK 后端 | **默认 RKNN SenseVoice w4a16**；`ExternalNpuLease`/rkipc broker 协调 NPU | 可选 sherpa-onnx CPU；后续可做流式 |
| **出结果** | kit `ResultSink` | WS 文本事件 → `/appcenter` 显示 | R2 若要叠加到视频另说 |

## 3. 唤醒词 KWS 选型(待定,给方案)
seeed-local-voice 侧重 ASR/TTS,**未确认自带 KWS**。候选(轻量、常听、CPU):
1. **openWakeWord**(tflite/onnx,自定义唤醒词,几 MB,CPU 几乎无压力)——推荐首选,自定义词方便。
2. **sherpa-onnx KWS**(和 ASR 同栈,一套 sherpa 依赖搞定 KWS+ASR+VAD)——依赖统一,优。
3. **RK 内置 AAD/wakeup**(`fw_aad_aivad.bin`+`wakeup_words`,VQE 里已有)——最省算力,但**没暴露 API**,要 R8 或改 rkipc,现在拿不到 → 归"官方将来"。
> 建议:**先用 sherpa-onnx KWS**(和 voxedge ASR 同栈,依赖最省),或 openWakeWord。做成 kit `logic/wakeword.py` 藏在接口后,将来换 RK 内置只换实现。

## 4. 应用形态(对齐 app-center)
`apps/voice-transcribe/`:
- `manifest.json`:`type: self-hosted`,`needs_model:false`,`capabilities:["audio","output"]`,`config_schema`:{唤醒词、录音最长时长、静音断句阈值、语言、ASR 后端}。这里 `needs_model:false` 只表示不走 kit 的**视频帧** `RknnModel` 自动加载器；默认 RK ASR 仍使用 NPU。
- `app.py`(薄):重写主循环用 `AudioSource` 而非 FrameSource → KWS → 触发录音 → voxedge ASR → `on_transcript` 出文本事件。**业务逻辑独有部分**:唤醒→录音→转录的状态机(idle/listening/transcribing)。
- kit 侧新增:`kit/adapters/audio_source.py`(BOOTSTRAP 里已规划,此应用是首个消费者)、`kit/logic/wakeword.py`、`kit/asr.py`(voxedge 封装,统一 `transcribe(pcm)->text`)。
- 依赖:manifest v2 的 per-release venv 中安装 **voxedge + kaldi-native-fbank + sentencepiece + sherpa-onnx**，平台提供兼容的 RKNN 运行时；RK ASR/KWS/VAD 资产作为签名包的 `artifacts[]` 安装到应用根目录 `models/asr`。选择 CPU `sherpa` ASR 时还需单独提供 ONNX ASR 模型。

## 5. reCamera Pro 上的硬约束(实测项)
1. **音频共存**:默认 `ai_asr` 是 ALSA dsnoop 共享采集，不关闭 rkipc、不接管 `/dev/snd`；RTSP 音轨解复用保留为回退路径。
2. **音频质量**:当前以 `audio_filter` 做麦克风增益；边播 TTS 边听仍需要 AEC，因此暂不做 TTS 回路。
3. **NPU 协调**:默认 RK ASR 必须由 appmgr 以 `npu.rknn=scheduled` 启动；应用只把张量发给唯一持有 RKNN context 的 `inferenced`。ASR 远程模型会话、VAD/KWS 构造和音频源首块 PCM 探测全部位于 `App.start()` 事务中；任一步失败都不会发送 APPMGR_READY。模型路径固定解析为已安装应用下的 bundled artifact，显式指向共享/外部目录会在应用侧先报 `unauthorized_model_path`，不会等到服务端模糊拒绝。
4. **依赖与模型体积**:per-release venv 与应用包需要容纳音频依赖、KWS/VAD 和约 135 MB 的 ASR 资产；CPU sherpa 后端另需 ONNX 模型。`/userdata/local/models/asr` 仅保留给独立运行和旧版部署兼容。
5. **中英文**:选支持中英的 ASR(SenseVoice 中英俱佳)。

## 6. 迁移到官方(R8)——只换适配器
- `AudioSource` 从当前 `ai_asr`/RTSP 实现换成后续 VQE-clean PCM 代理，应用状态机不变;
- KWS 换成 R8 暴露的 RK 内置 wakeup;VAD 换 RK `rkvad`;
- **KWS/ASR/状态机/出结果的应用逻辑不改**。这就是适配层的价值(同视觉侧)。

## 7. 分步(实施时)
- P0(已完成):kit `AudioSource` 通过 `ai_asr` dsnoop 与 rkipc 共享采集，并保留 RTSP/WAV 输入。
- P1(已完成):默认 RKNN SenseVoice 非流式转录，使用 rkipc broker 租约；CPU sherpa 保留为显式可选后端。
- P2(已完成):sherpa-onnx KWS 常听 → 唤醒触发 → silero VAD 断句 → 转录；状态机 idle/listening/transcribing。
- P3(已完成):打包成 app，`/appcenter` 显示转录文本，`config_schema` 可调。
- P4(可选):流式转录、进一步压缩模型、TTS 回应(要解 AEC)。

## 8. 一句话
应用复用 **voxedge 的 ASR 接口**，默认实现是针对 RV1126B 的 `kit.asr_rknn_backend`；manifest v2 管理实例在 appmgr READY 前用包内绝对模型路径建立 `inferenced` 会话。Voice App 随后在同一启动事务中构造 KWS/VAD、打开并探测共享 `AudioSource`，在所有结束路径按音频→ASR 的顺序释放。状态机(唤醒→录音→转录)保持后端无关；CPU sherpa ASR 只在用户显式选择并提供额外模型时启用。

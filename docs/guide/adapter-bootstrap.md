# 先行自建、平滑切官方 — 路径设计

> 目标：**不等 Seeed 官方开口**，现在就在 V1.0.10 闭源固件上把**应用市场 + 示例应用**搭起来；同时保证**官方 SDK（R1–R8）落地后，迁移 = 换适配器实现 + 翻开关，不动上层应用逻辑**。
> 关联文档：`app-center-publishing.md`（应用中心 / 上架）。官方需求分析、迁移目标与调试面板复用属内部设计文档，不在公开仓。

## 0. 核心策略：防腐层(ACL) + 能力协商

**一句话**：每一处"今天的绕路"都藏在一个**接口**后面，接口的**形状 = 未来官方契约的形状**。官方到了就写第二个实现、翻个开关。应用逻辑永不重写。

三个支柱：
1. **适配器接口层**（§2）：`FrameSource / ResultSink / AudioSource / ControlPlane / EventSource`——上层应用只依赖接口。
2. **能力注册表 + 协商**（§3）：appmgr 启动时探测"官方有没有 R1/R2/R8"，有则用官方适配器，无则用绕路适配器 → **固件升级新增能力后，应用自动切换，零改动**。
3. **数据契约对齐官方**（贯穿）：结果用**官方同款 protobuf**、帧用 **dma-buf**、音频用 **PCM 16k 单声道**——今天即使只自己消费，也按官方形状产出，迁移时不用转换。

## 1. 今天能自建的 vs 必须绕路的（现状盘点）

| 能力 | 今天怎么做（无官方） | 绕路代价 | 官方到了怎样(目标) |
|---|---|---|---|
| 市场/分发 | appmgr + ext_appmgr.conf + tar 包 + 清单 + catalog + 签名（基本全是我们自己的） | 低，本就自建 | 迁到官方 R6 catalog（清单前向兼容即可） |
| 视频帧 | RTSP sub 流(640×480) → MPP 硬解 → dma-buf | +1~2 帧延迟、一次硬解 | R1 帧代理 socket（零拷贝） |
| 结果显示 | `/appcenter` 客户端 canvas 叠加 | 不在官方视频/录像里 | R2 结果注入官方 OSD |
| 推理 | 自建进程 + 自带 RKNN（关官方推理释放 NPU） | 需管关/恢复官方推理 | R3 Tier B 契约 |
| 控制面 | 逆向调 entry.cgi（推理/OSD/快照/录制） | 固件升级易碎 | R4 版本化 API |
| **音频/STT** | **接管 mic**（关 rkipc 音频→自己开 ALSA），自跑降噪 | **失去官方 VQE、独占 mic、无官方 RTSP 音轨** | R8 PCM 代理（VQE 干净音频，可共存） |
| 事件 | 订阅 `:8123`(推理) / VGDS event-socket(录像事件) | — | 同上，官方化 |
| 忽略区 | 自己后处理多边形过滤 | — | R4.1（已决定走后处理，不依赖） |

> **视频/结果/推理/控制**四条绕路都干净（代价小、可适配器化）。**音频是唯一有实质妥协的一条**（§5 专门处理）。

## 2. 适配器接口层（迁移的核心投资）

上层应用**只依赖这些接口**；每个接口有"今天实现"和"官方实现"两套，靠 §3 注册表选。

### 2.1 `FrameSource`（对应 R1）
```
Frame { dmabuf_fd, width, height, format=NV12, pts, stride }
open(cfg) / acquire() -> Frame / release(Frame) / close()
```
- **今天**：`RtspDecodeSource` — 拉 sub 流 → MPP VDEC 输出 MB(本就是 dma-buf) → 包成 Frame。
- **官方**：`OfficialFrameSource` — 连 `/run/recamera/frame.sock` recv SCM_RIGHTS fd。
- **迁移**：Frame 结构已是 dma-buf → 下游 RGA/RKNN **一行不改**，只换 source 实现。

### 2.2 `ResultSink`（对应 R2）
```
push(InferenceResult)   // 官方同款 protobuf schema (inference_pb2)
```
- **今天**：`ClientOverlaySink` — 结果经自有 WS 推 `/appcenter` 前端，canvas 叠在 go2rtc 流上。
- **官方**：`OsdInjectSink` — 把同一条 protobuf 发官方入站口 → 服务端 OSD 烧进流。
- **迁移关键**：**今天就用官方 protobuf 形状产出结果**（哪怕只客户端画），迁移=多加一个 sink，零转换。

### 2.3 `AudioSource`（对应 R8，最需要藏细节）
```
PcmFrame { pcm, rate=16000, ch=1, pts }
open() / read() -> PcmFrame / close()
```
- **今天**：`AlsaTakeoverSource` — appmgr 先关 rkipc 音频释放 mic → 直开 ALSA(2ch/22050) → 自跑 降噪/AGC + 重采样到 16k 单声道。
- **官方**：`OfficialPcmSource` — 连 R8 的 `/var/run/recamera/audio.sock`，直接拿 VQE 干净 16k PCM，**且不再关 rkipc 音频**。
- **迁移**：应用只见 `PcmFrame(16k mono)`；换 source + 去掉"关 rkipc 音频"这步生命周期动作即可。

### 2.4 `ControlPlane`（对应 R4）
```
setInference(enable/model/fps) / setOsd(...) / snapshot() / record(...) / storageInfo()
```
- **今天**：`CgiControl` — 封装逆向的 `/cgi-bin/entry.cgi/...` 调用，**集中在一处**，带固件版本 pin + 失败降级。
- **官方**：`OfficialControl` — 换成 R4 版本化 API。
- **迁移**：只换这一个封装类；应用调的是抽象方法名，不感知底层是逆向还是官方。

### 2.5 `EventSource`
```
subscribe(topic) -> stream of events   // inference / recording / gpio ...
```
- **今天**：连 `:8123`(推理结果) + VGDS event-report-socket(录像/规则/GPIO 事件)。
- **官方**：同源，官方化后换订阅端点。

## 3. 能力注册表 + 协商（fail-closed 迁移）
进程启动时做无副作用 **capability probe**，把“路径发现”和“协议已验证”
明确区分：
```
caps = {
  frame_broker:  filesystem_socket ? UNKNOWN : UNAVAILABLE,
  result_ingress: handshake_result(),
  audio_broker:  filesystem_socket ? UNKNOWN : UNAVAILABLE,
  control_api:   handshake_result(),
}
```
- 每个适配器工厂只在 capability 为 `AVAILABLE` 时自动选 Official；单纯存在
  socket inode 只得到 `UNKNOWN`，继续使用 workaround，避免 stale socket、权限
  错误或协议版本不匹配把应用导向不可用路径。
- 当前还没有 native capability handshake，已确认使用匹配补丁固件的部署必须
  显式设置 `RECAMERA_ADAPTER_PREFER=official`；`workaround` 仍可强制回退。
- 将来版本化握手落地后，固件才能把状态升级为 `AVAILABLE` 并安全自动迁移。
  平滑切换的机械边界仍收敛在**注册表协商 + SDK adapter 实现**，应用代码不变。

## 4. 三个示例应用（验证全链路 + 递进暴露绕路）

### App-1 · 视觉·托管模型（最简，零绕路，先出成果）
- 一个自定义检测模型 `rknn+json`，走**官方模型上传+选中**（rkipc 模式）→ 官方叠加/UI 免费。
- 验证：catalog → 安装 → 官方推理跑起来 → dashboard 看到框。**不碰任何绕路**，最快见效、风险最低。

### App-2 · 视觉·自建流水线（人脸识别，验证 FrameSource+ResultSink）
- `FrameSource(RtspDecode)` → 检测→对齐→embedding→比对注册库 → `ResultSink(ClientOverlay, protobuf 形状)` → `/appcenter` 调试视图(叠加/注册库 CRUD/参数热调)。
- appmgr 生命周期：激活关官方推理释放 NPU、退出恢复。
- 验证：全自建链 + 两个适配器 + 调试面板。

### App-3 · 语音·STT（验证 AudioSource + 接管音频）
- `AudioSource(AlsaTakeover)` → VAD(自跑 webrtc/silero) → STT → 结果；可选 TTS 回放。
- appmgr 生命周期：激活时关 rkipc 音频、退出恢复。
- 验证：音频链 + 接管妥协的边界（§5）。**明确文档化"此 app 独占音频、无官方 VQE"**。

## 5. 音频这条唯一实质妥协 — 专门设计
这是整条路径里**最需要小心**的一环。

**问题**：mic 被 rkipc `RK_MPI_AI` 独占；对外只有 G711A 8k（不能 STT）；直开 ALSA 需先让 rkipc 放手，且放手就**丢了官方 VQE(AEC/降噪)**。

**今天的做法（`AlsaTakeoverSource` + appmgr）**：
1. 语音 app 激活 → appmgr **关 rkipc 音频**（`audio.0 enable=0` 或经 entry.cgi，**需上机验证能否只关音频不影响视频、是否要重启音频链**）。
2. app 直开 ALSA（2ch/22050/S16）→ **自带软件降噪/AEC**（rnnoise / webrtc-apm）→ 重采样 16k 单声道 → STT。
3. app 退出 → appmgr 恢复 rkipc 音频。

**妥协点（要跟方案商讲清）**：
- 语音 app **独占 mic**，期间**没有官方 RTSP 音轨**；
- **失去官方 VQE**，回声/远场效果不如官方（尤其边放 TTS 边听时的 AEC）→ 自带 AEC 缓解，但不如硬件链；
- 与视觉自建 app 并存时，NPU 上 STT 模型也占算力（§NPU 预算）。

**迁移到 R8 后**：换 `OfficialPcmSource` → 拿 VQE 干净 16k PCM、**不再关 rkipc 音频**（可与官方音频共存）、AEC 交给官方 → **上面 STT/VAD/应用逻辑一行不改**。妥协随适配器切换一起消失。

## 6. 风险与对策

| 风险 | 对策 |
|---|---|
| **OTA 洗掉我们的挂载/入口**（/oem、ext_conf、init 脚本） | 全落 /userdata + 回注 hook（见 APP_CENTER §4.7）；appmgr 启动自检并回注 |
| **逆向的 entry.cgi 接口随固件变** | 全封在 `CgiControl` 一处 + 固件版本 pin + 启动 capability probe，变了则降级/告警，不让应用直接依赖 |
| **音频接管影响官方功能 / 释放不干净** | §5 单独处理；上机验证"只关音频"可行性；语音 app 标注独占；失败回滚恢复 |
| **NPU 争用**（自建推理 + STT + 官方） | 单 active + NPU 预算；self-hosted 关官方推理释放算力（§APP_CENTER §2.3） |
| **我们的市场 vs 官方将来的应用中心** | manifest 设计成官方**前向兼容超集**；catalog/签名可迁移；appmgr 能力可被官方 supervisor 接管 |
| **切换回归**（换适配器引入 bug） | 每个适配器有契约测试；App-2/3 作回归基准；能力注册表支持 `prefer` 强制回退 |

## 7. 分阶段路线

- **P0 · 地基（市场 + SDK 骨架）**：appmgr + ext_appmgr.conf + 清单/catalog/签名 + 前端外壳(早期 `/appcenter` SPA,现役已换官方 React `/app-center`) + 鉴权复用 + **定义 5 个适配器接口 + 能力注册表**（← 迁移的核心投资，务必先做）。
- **P1 · App-1 托管模型**：跑通 catalog→装→官方推理→dashboard 见效。零绕路，最快交付。
- **P2 · App-2 自建流水线(FR)**：FrameSource+ResultSink+调试面板+注册库；跑通自建全链 + 适配器验证。
- **P3 · App-3 语音 STT**：AudioSource+接管音频+VAD+STT；验证音频妥协边界。
- **P4 · 硬化 + 官方对接**：契约测试、OTA 回注、能力 probe 完善；把 PRD 作为"这些适配器的官方目标"随 SDK 附带（官方/绕路两套适配器，官方那套先留 stub）。

## 8. 迁移时到底改什么（换官方那天的改动清单）
- **SDK 层**：为命中的能力**新增官方适配器实现**（FrameSource/ResultSink/AudioSource/ControlPlane 各一个类）+ 能力 probe 命中它们。
- **appmgr**：注册表 probe 到官方口 → 工厂切换；去掉 self-hosted 的"关官方推理/关官方音频"生命周期步骤（官方支持共存后不再需要）。
- **应用逻辑**：**不改**（只依赖接口 + 官方 protobuf/PCM 形状）。
- **清单/catalog**：若迁官方 R6，清单字段做一次前向兼容映射。
- → **改动集中在 SDK 的适配器实现 + appmgr 的路由**，示例应用与业务逻辑零改。这就是"先自建、后切官方"的低成本迁移。

## 9. 待上机验证（开工前坐实几个假设）
1. **音频接管**：能否只关 rkipc 音频而不影响视频/RTSP？关/恢复是否需重启、是否干净？（P3 前必验）
2. **帧源延迟**：sub 流 MPP 硬解端到端延迟/CPU 实测（决定 FrameSource 是否够用）。
3. **关官方推理**：entry.cgi `inference enable=0` 释放 NPU 是否彻底、恢复是否可靠。
4. **ext_conf 挂载 + OTA**：ext_appmgr.conf 是否被 nginx 自动加载、OTA 后回注是否生效。
5. **NPU 并跑预算**：自建检测 + STT + (可选官方) 合计负载余量。

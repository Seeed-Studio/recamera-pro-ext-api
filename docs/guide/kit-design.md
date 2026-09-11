# reCamera Pro 应用套件 — Python 实现 & 通用/应用分层设计

> 决策：**Python 实现**。目标：把"通用层"和"应用独立层"切干净——通用的一次做好、所有应用复用；应用只写自己独有的业务逻辑。**适配层用我们现在的曲折方式（解码/客户端叠加/接管音频），官方接口(R1/R2/R8)到了只换适配层，应用一行不改。**
> 关联：`adapter-bootstrap.md`(适配器)。应用移植与上游能力分析属内部设计文档，不在公开仓。

## 0. 一张图看清分层（通用 vs 应用独立）

```
┌───────────────────────────── 应用独立层 (每个 app ~20%) ─────────────────────────────┐
│  manifest.json (声明模型/pipeline/后处理类型/config_schema/元数据)                     │
│  app.py: 只实现"业务逻辑"钩子 —— 状态机/计数/聚合/领域解码 (fall逻辑, 计数, 客群聚合…) │
└───────────────────────────────────────────────────────────────────────────────────┘
                                   ▲ 只依赖通用层接口
┌───────────────────────────── 通用层 / Kit (一次做好, ~80%) ──────────────────────────┐
│ L3 应用框架   App 基类(生命周期/配置/调试钩子) · Pipeline(级联/ROI裁剪) · 共享逻辑库   │
│               (Tracker多目标跟踪 · ZoneCounter/LineCounter · TemporalSM · geometry)   │
│ L2 输出/管理  MqttPublisher(HA Discovery) · ResultWS · OverlayEmitter · ConfigLoader  │
│               · ManifestLoader · ModelManager · DebugHooks(FPS/延迟/中间结果)          │
│ L1 运行时     RknnModel(封 rknnlite) · Preprocess(letterbox/归一/RGA色转)             │
│               · Postprocessors 注册表(detect/pose/classify/db/ctc/landmark…) ← 复用大头│
│ L0 适配层     FrameSource · ResultSink · AudioSource · ControlPlane · EventSource     │  ← 唯一"曲折/官方"切换点
│               (能力注册表按官方有无自动选实现; 应用永不直接碰)                          │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

**划分原则**：应用只提供"**用哪些模型 + 怎么串 + 检测结果之上的业务逻辑 + 输出什么字段 + 可调参数**"；其余（取帧、推理、标准前后处理、跟踪/几何、输出管道、配置/清单/模型管理、调试、平台适配）**全在通用层**。

## 0.5 贯穿原则：复用官方优先 + 适配层对齐 RK 开源结构

1. **能用官方 dashboard/rkipc 现成能力的，一律复用，不重造**：
   - **设备/网络/固件/录制/快照/存储/OSD/打码/推流**等——`ControlPlane` 直接封装官方 `entry.cgi` 端点，appmgr **不重实现**这些；
   - **视频预览**用 go2rtc、**终端**用 ttyd(:7681)、**系统日志**用 WS(:8765)、**结果流**用 WS(:8123)——`/appcenter` 面板**内嵌/订阅**官方这些，不自建播放器/终端/日志；
   - 只有官方**确实没有**的（自建推理编排、我们应用的结果叠加、STT）才自己做。
2. **适配层的数据结构/协议对齐 RK 开源**（Seeed 只是对 RK 的薄抽象，未来官方口大概率就是 RK 那套）：
   - `Frame` 对齐 RK 的 **dma-buf/MB_BLK + VPSS 通道**模型（参考 `reference/rkipc-rockchip-ref/.../video.c` 的 `VPSS_GetChnFrame`/`MB_Handle2Fd`）；
   - `ResultSink` 结果对齐 RK 的 **`InferenceResult` protobuf** + rkipc **OSD/RGN** 叠加接口（`common/osd`、socket_server 协议）；
   - `AudioSource` 对齐 **`RK_MPI_AI`/AENC + VQE**；
   - `ControlPlane`/`EventSource` 对齐 rkipc socket_server / 事件 JSON 形状。
   - **收益**：官方 R1/R2/R8 落地时，我们的适配器几乎是"换个后端连上去"，而不是重新适配数据模型——因为我们一开始就照 RK 的形状设计。
3. **参考胜过臆造**：没有 Seeed Pro 源码时，**先读 `reference/rkipc-rockchip-ref/`(RK 官方 rkipc) 对齐结构**，再落我们的实现。

## 0.6 依赖与体积：原生 RKNN + 砍依赖 + 每包尽量小（硬约束）

1. **推理只用原生 RKNN**：`RknnModel` = `rknnlite`(rknn_toolkit_lite2，librknnrt 的薄绑定) 或直接 **ctypes 调设备 `librknnrt.so`**。**禁止**引入 onnxruntime/torch/tflite 等重框架。
2. **系统已有的 native 库不 bundle**：`librknnrt.so`、`librga.so`(RGA 硬件缩放/色转/裁剪)、`librockchip_mpp.so`(硬解) 设备上都在 → 直接 load，**不打进任何包**。
3. **前处理走 RGA/native，慎用 opencv**：letterbox/resize/crop/NV12→RGB 用 **RGA**(硬件、零额外依赖)；后处理(NMS/softmax/解码/CTC/DB)用 **numpy**。**能不用 opencv-python(几十 MB) 就不用**；确需时用 `opencv-python-headless` 且只装在 Kit venv 一份。
   → app 侧开启方式(`App.model_frame = "hw"` / `"hw-direct"`，一行类属性)与实测数据见 [hw-preprocess.md](./hw-preprocess.md)。
4. **共享依赖装 Kit venv 一次，不进 app 包**：`kit` + numpy + rknnlite 装在一个共享 venv(uv 管理，落 `/userdata`)；**app 的 tar 只含 `model.rknn + app.py + manifest.json`**（几十~几百 KB 级），import 共享 kit。
5. **体积红线**：app 包 = 模型体积 + 几 KB 代码；公共运行时(kit venv)一份共享。目标：装 10 个 app ≈ 10 份模型 + 10 份薄代码，不是 10 份 numpy/opencv。

## 1. 整体 = 应用市场 + Kit + 应用（一个仓库，三根支柱）

一个仓库同时装下三块，边界清晰：

| 支柱 | 是什么 | 归属 |
|---|---|---|
| **应用市场 (market/)** | 应用中心：appmgr(进程管理/安装/卸载) + catalog(云端目录) + 打包签名 + nginx 接入 + 自启动。现役前端为官方 web 原生 React `/app-center`(`recamera_web_react`);`market/spa` 是早期 vanilla SPA,已 LEGACY(见 `market/spa/DEPRECATED.md`) | 平台基座(通用) |
| **Kit (kit/)** | 运行时通用层(适配/推理/后处理/跟踪/输出/管理)——见 §0 分层 | 通用 |
| **应用 (apps/)** | 9 个应用，每个 = manifest + 薄 app.py | 应用独立 |

**三者关系**：appmgr(市场)负责**装/起/停/管** apps；每个 app 进程 **import kit** 跑推理；app 的结果经 kit 的 ResultSink → 官方 React `/app-center` 页(市场)展示。**市场是"容器与分发"，Kit 是"运行时",应用是"内容"。**

## 2. 目录结构

```
recamera-pro-apps/
├── market/                       # ★应用市场 / 应用中心 (平台基座)
│   ├── appmgr/                   #   常驻服务(Python): 安装/卸载/启停/看护/状态机/单实例锁
│   │   ├── server.py             #     /api/appMgr/* (list/install/uninstall/switch/storage)
│   │   ├── supervisor.py         #     进程管理(进程组/TERM→KILL/退避/孤儿认领)
│   │   ├── installer.py          #     tar 校验(zip-slip防护)+解压+manifest校验+版本保留
│   │   └── state.py              #     state.json(active_app) + 事务/回滚
│   ├── catalog/                  #   云端目录 + 浏览器代取(catalog.json schema, sha256校验)
│   ├── packaging/                #   打包签名: <id>-<ver>-arm64.tar.gz + 公钥验签
│   ├── spa/                      #   LEGACY 早期 vanilla SPA(已被官方 React /app-center 取代,非现役前端)
│   ├── deploy/                   #   ext_appmgr.conf(nginx) + S93/S94(OEM自启动) + 兼容运维入口
│   └── auth/                     #   复用 sensecraft_token; nginx auth_request /_jwt_verify
├── kit/                          # 通用层 (一个 Python 包, 所有应用 import)
│   ├── adapters/                 # L0 —— 曲折/官方 切换点
│   │   ├── frame_source.py       #   FrameSource: RtspDecode(今) / OfficialBroker(未来)
│   │   ├── result_sink.py        #   ResultSink: ClientOverlay(今) / OsdInject(未来)
│   │   ├── audio_source.py       #   AudioSource: AlsaTakeover(今) / OfficialPcm(未来)
│   │   ├── control_plane.py      #   ControlPlane: CgiControl(今) / OfficialApi(未来)
│   │   ├── event_source.py       #   EventSource: Ws8123/VgdsSock(今) / Official(未来)
│   │   └── registry.py           #   能力探测 → 选实现
│   ├── runtime/                  # L1
│   │   ├── engine.py             #   RknnModel(rknnlite 封装: load/infer→tensors)
│   │   ├── preprocess.py         #   letterbox/normalize/RGA color-convert
│   │   └── postprocess/          #   ← 通用后处理注册表(复用大头)
│   │       ├── detect.py         #     yolo/nanodet 解码+NMS
│   │       ├── pose.py           #     关键点解码
│   │       ├── classify.py       #     softmax/topk
│   │       ├── db_ocr.py         #     DB 文本检测后处理
│   │       ├── ctc.py            #     CTC 贪心解码
│   │       └── landmark.py       #     人脸 landmark 解码
│   ├── io/                       # L2
│   │   ├── mqtt.py  ws.py  overlay.py  config.py  manifest.py  models.py  debug.py
│   ├── logic/                    # L3 共享逻辑库(可复用的算法, 非某应用独有)
│   │   ├── tracker.py            #   多目标跟踪(yolo/retail/fall 共用)
│   │   ├── zones.py              #   ZoneCounter / LineCounter(方向) / Dwell
│   │   ├── temporal.py           #   TemporalStateMachine 基类(去抖/持续确认)
│   │   └── geometry.py           #   角度/IoU/点在多边形…
│   ├── app.py                    #   App 基类 + Pipeline 编排
│   └── main.py                   #   通用入口: 读 manifest → 建 pipeline → 跑
├── apps/                         # 应用独立层 (每个 app 一薄目录)
│   ├── yolo-detector/{manifest.json, app.py, models/}
│   ├── fall-detection/{manifest.json, app.py(FallSM), models/}
│   └── …(9 个)
├── models/convert/               # 模型转换(onnx→rknn, rknn-toolkit2, Python)
└── pyproject.toml                # uv 管理
```

## 3. 关键接口（Python 抽象，应用只面对这些）

```python
# L0 适配 —— 应用不直接用, 由 App 基类经 registry 注入
class FrameSource(ABC):
    def frames(self) -> Iterator[Frame]: ...        # Frame{ndarray|dmabuf, w,h,fmt,pts}
class ResultSink(ABC):
    def emit(self, overlay: OverlayResults, pts): ...  # 今=客户端叠加; 未来=官方OSD
class AudioSource(ABC):
    def pcm(self) -> Iterator[PcmFrame]: ...        # 16k mono

# L1 运行时
class RknnModel:
    def __init__(self, path): ...
    def infer(self, x) -> list[np.ndarray]: ...     # rknnlite
class Postprocessor(ABC):
    def process(self, tensors, meta) -> Results: ...  # 注册表按 task 取

# L3 应用基类 —— 应用自己写循环, 基类提供原语
class App(ABC):
    owns_loop = True                                 # 唯一形态, 显式声明
    def setup(self, config): ...                     # 可选: 由已绑定的参数派生对象
    def run(self):                                   # ★整条流水线就是普通 Python
        for frame in self.frames():                  #   取帧/跳灰帧/热更/预热=基类
            x = self.pre(frame)                      #   letterbox(可走 RGA)
            outs = self.models.det.infer(x)          #   支持常驻 DMA 输入；旧 Kit 也兼容
            self.emit(events, frame.pts, results=r)  #   输出扇出=基类
```

一个应用 = `manifest.json`(声明) + `app.py`(继承 App, 写 `run()` 业务循环, 需要时加 `setup`)。
`config_schema` 里的参数由基类自动绑定为 `self.<key>`(SIGHUP 热更同一路径), 应用不解析 config。

### 循环处理耗时与性能指标

使用 `for frame in self.frames()` 的应用约每秒发送一条 `type: "metrics"` 消息。
`latency_ms.loop` 是该统计窗口内**每轮应用循环体总耗时的平均值**，单位毫秒：
从 Kit 将帧交给应用前开始，到本轮应用循环体返回时结束，包含前处理、模型调用、
后处理/业务逻辑、同步结果发送，以及 `emit()` 返回后继续执行的业务工作。
异步发送只计当前线程的入队开销，不等待后台网络投递完成。
取帧等待、源端在交帧前完成的处理、Kit 自身周期 metrics 发送不计入 `loop`。
此值直接累计完整循环计时，不由已舍入的分阶段数据相加得出。

原有字段保持兼容：`pre`、`infer`、`post`、`app`、`emit` 仍在 `latency_ms` 中；
`post` 与 `app` 是同一业务时间桶，不应重复相加。结果消息的 `pipeline_ms` 仍只计到
`emit()` 入口，`inference_time_ms` 仍是本帧模型调用累计耗时，均不替代 `loop`。
规范 Result Hub 消息会将这些统计放在 `metrics.latency_ms` 中，包括新增的 `loop`。

`fps` 继续表示窗口内完成的应用循环帧数除以墙钟时间，并非消息接收速率；
应用循环内部 `continue` 跳过推理的帧仍沿用原有计数行为。
CPU-only 应用即使 `infer=0`，也会有完整的 `loop` 耗时。
显式启用并协商 DMA IO 后，硬件模式的延迟 RGA 前处理发生在循环体内，计入 `pre` 与 `loop`，
不重复计入 `infer`。访问 `x.data` 仍会得到可修改的独立数组；直接传 `infer(x)`
可省掉这次图像物化。详见[共享推理 IO](./shared-inference-io.md)。
`needs_frames=False` 的音频应用不生成这组帧循环指标；语音应查看其转写事件中的
`audio_sec` 和 `rtf`，不能将未提供的循环耗时显示为有效的 0 毫秒。

## 4. 适配层 = "曲折现在 / 官方将来" 的唯一切换点
- `kit/adapters/registry.py` 启动探测官方口(frame.sock/audio.sock/结果注入/版本化API)是否存在 → 每个适配器工厂选 `Official*` 或 `Workaround*`。
- **应用只经 App 基类拿帧/发结果，从不直接引用适配器** → 官方到了，改的只有 `adapters/` 下几个实现类 + registry 命中，`apps/` 全不动。
- 对应 adapter-bootstrap 的迁移契约：Frame 用 ndarray/dmabuf、结果用官方 protobuf 形状、PCM 用 16k mono —— **今天就按官方形状产出**，切换零转换。

## 5. 通用 vs 应用独立 —— 明确划分表

| 能力 | 归属 | 说明 |
|---|---|---|
| 取帧 / 出流 / 音频 / 控制 / 事件（平台适配） | **通用(L0)** | 曲折→官方 只在这切 |
| RKNN 推理封装、前处理 | **通用(L1)** | rknnlite + letterbox/RGA |
| 标准后处理(检测/姿态/分类/DB/CTC/landmark) | **通用(L1)** | 注册表，多应用共用 —— **复用最大头** |
| 多目标跟踪、区域/进出线计数、时序状态机基类、几何 | **通用(L3 logic)** | yolo/retail/fall/fitness 都用 |
| MQTT(HA Discovery)/结果WS/叠加/配置/清单/模型管理/调试指标 | **通用(L2)** | 输出与管理管道 |
| Pipeline 编排(单模型/级联ROI裁剪) | **通用(L3)** | face/ppocr 的级联走这 |
| **用哪些模型 + 怎么串 + config_schema + 元数据** | **应用(manifest)** | 声明式，不写代码 |
| **业务逻辑**(跌倒判定/计数规则/客群聚合/困倦指标/OCR编排/领域解码) | **应用(app.py)** | 每个应用唯一独有的部分 |
| **结果字段映射**(往 MQTT/WS/叠加放什么) | **应用** | 薄 |
| **非标后处理**(极少数) | 应用 | 仅当标准注册表没有时 |

> 结论：**~80% 沉到通用层；应用独立的只有"声明 + 业务逻辑 + 输出字段"**。

## 6. 逐应用映射（哪些用通用、哪些自己写）

| 应用 | 模型(转rknn) | 后处理(通用) | 业务逻辑(应用独立, app.py) | config_schema |
|---|---|---|---|---|
| yolo-detector | yolo11n(可切) | detect | 复用 Tracker + 事件格式 → 极薄 | conf/tracking/zone/line |
| weather-classifier | mobilenetv3 | classify | top-1 → 发布, 近乎空 | — |
| qrcode-reader | 无(quirc) | (QrDecoder 工具, 归通用) | 结果格式 → 极薄 | — |
| retail-vision | yolo11n(person) | detect | Tracker+ZoneCounter+LineCounter+Dwell **多为通用库**, app 只配置+串 | 区域/进出线/驻留/窗口 |
| fall-detection | yolo11n-pose | pose | 每轨 48 帧冻结学习型 gate + 几何候选/恢复状态机；默认严格确认 | 13 参数 |
| fitness-trainer | yolo11n-pose | pose | **RepCounter**(关节角状态机) | mode/target/conf |
| facemesh-reader | 人脸检测+landmark468 | detect+landmark(级联) | **DrowsinessMetrics**(EAR/MAR/眨眼/哈欠时序) | — |
| face-analysis | 人脸检测+fairface+情绪 | detect+classify×2(级联+ROI裁剪对齐) | **DemographicAggregator**(分时段聚合) | — |
| ppocr-reader | ppocr_det+ppocr_rec | db_ocr+ctc(级联) | **OcrOrchestrator**(框→透视裁剪→识别→字典解码) + 字典 | — |

**观察**：跟踪/计数/区域类(yolo/retail/fall/fitness)业务逻辑多能落到**通用 logic 库**，app 只是配置+串；真正每个应用独写的核心是那几个**状态机/聚合器/编排器**(FallSM/RepCounter/Drowsiness/Demographic/OcrOrchestrator)——**这就是应用独立层的本质,通常 50~200 行 Python**。

## 7. 一个应用长什么样（fall-detection 示意）
```python
# apps/fall-detection/app.py  —— 应用独立层就这么薄
from kit.app import App
from kit.runtime.postprocess.pose import postprocess
class FallApp(App):
    owns_loop = True
    def setup(self, cfg):
        self.sm = FallStateMachine(self)          # 唯一独有逻辑(读已绑定的 self.<参数>)
    def run(self):
        for frame in self.frames():
            x = self.pre(frame)
            kps = postprocess(self.models.pose.infer(x.data), x.info)  # 通用 pose 解码
            ev = self.sm.update(kps, frame.pts)   # 髋降速+躯干角时序判定
            self.emit([Event("fall", ev.box)] if ev.fallen else [],
                      frame.pts, results=kps)
```
`manifest.json` 声明 `models:[yolo11n-pose] · postproc:pose · config_schema:{...}`。取帧/推理/pose解码/输出/叠加/MQTT 全通用层。**移植 = 把一代 `fall_detector.cpp` 的状态机逻辑翻成这 ~100 行 Python**，其余不写。

## 8. 迁移顺序（市场 + Kit 先行, 再逐应用）
- **P0 · 市场骨架 + Kit**：
  - 市场：appmgr(装/起/停/单实例锁) + tar 打包 + `ext_appmgr.conf` + `S94appmgr` 自启动 + `/appcenter` SPA 外壳 + 鉴权复用（对齐 `app-center-publishing.md`）。
  - Kit：L0 适配(RtspDecode/ClientOverlay/registry) + L1(RknnModel + detect/pose/classify 后处理) + L2 输出 + L3 App基类/Pipeline/Tracker + 转换脚本。**先转 yolo11n。**
- **P1 · yolo-detector 端到端打样**：经 appmgr 装 → 起 → 取帧→rknn→detect→跟踪→ResultSink→`/appcenter` 看到框。**一条链跑通 = 市场+Kit 一起验证完成。**
- **P2 · 托管类批量**：weather / qrcode / retail / fall / fitness（复用 detect/pose/classify + logic 库，各写薄 app.py），逐个打包上架本地 catalog。
- **P3 · 流水线类**：补 db/ctc/landmark 后处理 + Pipeline 级联 → facemesh → face-analysis → ppocr。
- **P4 · 市场收尾**：catalog 云端目录 + 签名 + 调试面板(指标/热调/回放) + OTA 回注 + 与一代行为回归对照。
- 并行：`models/convert` 逐个 onnx→rknn（先盘点 ONNX 来源）。

## 9. 官方接口到了怎么切
- 只改 `kit/adapters/` 下实现 + registry 探测命中官方口；
- 去掉 self-hosted 的"关官方推理/接管音频"生命周期步骤；
- `apps/` 与所有业务逻辑、后处理、输出 **零改动**。
- 这就是"先用曲折的、官方来了换官方"的机械保证 —— **切换面收敛到一个 adapters 目录**。

## 10. 一句话
一个仓库三支柱：**应用市场(market/ 装分发) + Kit(kit/ 运行时通用层) + 应用(apps/ 9 个)**。Python 实现，**通用层吃掉 ~80%**(适配/推理/标准后处理/跟踪计数/输出/管理)，应用只剩 **声明式 manifest + 50~200 行业务逻辑**。适配层是唯一的"曲折/官方"开关，官方 R1/R2/R8 到了只换 `adapters/`，应用不动。先建 市场骨架+Kit + yolo 端到端打样，再托管类批量、流水线类逐个。

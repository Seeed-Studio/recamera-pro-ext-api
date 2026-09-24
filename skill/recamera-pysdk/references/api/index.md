# Public API reference

按任务只读需要的模块。以下文件随 skill 安装，可离线查阅。

源码基线：`635ffc3c51d596dd2e8139f297798162e6be62b9`；打包器版本仍由 `sdk-contract-lock.json` 单独固定。

先读 [接口特性、权限、生命周期及示例](features.md)。

| Python 模块 | 说明 |
| --- | --- |
| [kit](python/kit.md) | Kit 顶层惰性导出。下表列出所有公共名称及定义位置；导入本身不启动摄像头或 NPU。 |
| [kit.adapters](python/kit-adapters.md) | 适配器惰性导出；普通托管应用优先使用 App 提供的帧、模型与 emit，不能自行重建网关。 |
| [kit.adapters.audio_source](python/kit-adapters-audio_source.md) | PCM 音源与音频帧。默认共享 ai_asr；显式 takeover 会涉及设备音频所有权，不适合作为普通应用默认行为。 |
| [kit.adapters.cgi_control](python/kit-adapters-cgi_control.md) | 现有固件的控制面兼容适配器：设置内置推理和抓 JPEG。snapshot 是取帧编码，不是 CGI 原生抓图接口。 |
| [kit.adapters.frame_source](python/kit-adapters-frame_source.md) | 取帧协议和 RTSP／快照兼容源。软件解码需要 FFmpeg，普通相机 AI 优先受管 native frame source。 |
| [kit.adapters.mqtt_sink](python/kit-adapters-mqtt_sink.md) | MQTT 结果与 Home Assistant Discovery；依赖 paho、可达 broker 及匹配的应用权限。 |
| [kit.adapters.official](python/kit-adapters-official.md) | Native SDK 适配层。OfficialFrameSource/OfficialResultSink 已实现；OfficialPcmSource、OfficialControl 是未实现占位，不能据此宣称设备有统一音频/control socket API。 |
| [kit.adapters.output_sink](python/kit-adapters-output_sink.md) | 声明式输出格式化与通道组件：raw JSON、受限 Jinja、HA、WS、MQTT、HTTP、UART。托管 App 的输出配置交给平台装配。 |
| [kit.adapters.registry](python/kit-adapters-registry.md) | 按能力与显式偏好选择后端。auto 只采纳已验证能力；存在 socket 文件不等于 AVAILABLE。 |
| [kit.adapters.result_sink](python/kit-adapters-result_sink.md) | 旧字典结果协议、严格发送、输出网关及兼容 WS。emit 的 pts 为秒，box 为原图像素；受管应用使用 App.emit。 |
| [kit.ai](python/kit-ai.md) | 强类型结果及几何图元的公共导出；字段语义和坐标空间必须显式匹配。 |
| [kit.ai.publisher](python/kit-ai-publisher.md) | 将 typed ResultBatch 转为旧 sink 的原图像素协议。PublishReport 证明本地接受，不是远端确认或录像成功。 |
| [kit.ai.results](python/kit-ai-results.md) | 检测、分类、关键点、姿态、跟踪、分割及批量结果；构造时校验有限数值、坐标空间和置信度，支持规范与 legacy 字典互转。 |
| [kit.app](python/kit-app.md) | 应用中心推荐入口：App 子类 owns_loop=True、run(self)，经 AppMgr 生命周期运行。包含模型注册、前处理、emit、显式录像和配置热更新。 |
| [kit.asr](python/kit-asr.md) | 语音识别统一接口，结果含文本与时间信息；实际 backend、模型、音源需要匹配目标环境。 |
| [kit.asr_rknn_backend](python/kit-asr_rknn_backend.md) | RKNN SenseVoice ASR 后端装配；需 tokenizer、模型工件和声明的 NPU 资源，不自动提供语音模型。 |
| [kit.buffer](python/kit-buffer.md) | CPU／DMA-BUF／其他后端图像缓冲描述与所有权。borrowed 不能跨源 lease 保存；owned copy 才能独立持有。 |
| [kit.capabilities](python/kit-capabilities.md) | 能力状态与发现：AVAILABLE/UNAVAILABLE/UNKNOWN/DEGRADED。当前文件系统探测一般只能证明 UNKNOWN，不能替代协议握手。 |
| [kit.config](python/kit-config.md) | manifest、用户配置、schema 展平和有效配置解析；应用路径来自 AppMgr 上下文，避免按 __file__ 猜安装根目录。 |
| [kit.device](python/kit-device.md) | 资源工厂与生命周期聚合；按创建逆序关闭。rknn_session 在受管 scheduled 模式选择远端服务，不接管相机媒体管线。 |
| [kit.diagnostics](python/kit-diagnostics.md) | 结构化日志、敏感 URL 脱敏及重复告警限流；日志级别不等同于应用健康状态。 |
| [kit.errors](python/kit-errors.md) | Kit 结构化异常：operation、code、details 与 cause。与 recamera_ext 的 native 错误体系分别处理。 |
| [kit.events](python/kit-events.md) | 生成 legacy 检测、文本、跟踪、属性与指标字典；构造字典不负责发送、叠加或录像。 |
| [kit.frame](python/kit-frame.md) | 后端无关 Kit Frame，pts 为秒、pts_us 为微秒；与 recamera_ext.FrameLease 不同。copy 会脱离原 DMA 所有权。 |
| [kit.geometry](python/kit-geometry.md) | 规范绘制图元与 GeometryBuilder；严格校验形状、style 和 JSON 数据。图元通过 manifest 输出契约选择 renderer。 |
| [kit.logic](python/kit-logic.md) | 应用侧 CPU 业务逻辑组件命名空间；状态机依赖正确模型输出、坐标与时间戳，不替代模型。 |
| [kit.logic.attributes](python/kit-logic-attributes.md) | 人脸属性概率平滑、置信门控及跟踪/时间窗聚合；需对应模型类别与输出头顺序。 |
| [kit.logic.drowsiness](python/kit-logic-drowsiness.md) | FaceMesh 468 点疲劳指标、打哈欠计数与 PERCLOS 状态机；使用单调帧时间（秒），仅为应用启发式逻辑。 |
| [kit.logic.geometry](python/kit-logic-geometry.md) | 姿态和区域几何工具；区分归一化跟踪坐标与原图像素输入，阈值/可见性随模型契约配置。 |
| [kit.logic.qrcode](python/kit-logic-qrcode.md) | 二维码 CPU 解码器，运行时后端可用性决定支持范围；decode 输入为图像，返回业务结果。 |
| [kit.logic.recording](python/kit-logic-recording.md) | 把显式业务事件交给 App.request_recording；必须有 manifest 录像声明与 AppMgr 授权。 |
| [kit.logic.rep_counter](python/kit-logic-rep_counter.md) | 运动次数计数与动作状态；基于关节角、可见性、平滑和迟滞，含 squat/push-up/hammer-curl。 |
| [kit.logic.temporal](python/kit-logic-temporal.md) | 跌倒时序状态机：normal/suspected/fallen/recovering；默认需要有效当前姿态与 learned temporal-positive 才确认。 |
| [kit.logic.tracker](python/kit-logic-tracker.md) | 轻量 IoU 跟踪；track ID 为此跟踪器生命周期内身份，不能当永久人员身份。 |
| [kit.logic.vad](python/kit-logic-vad.md) | PCM 语音活动分段器；采样率、块长、门限决定切分，输出语音段而非识别文本。 |
| [kit.logic.voice_sm](python/kit-logic-voice_sm.md) | 唤醒、聆听与识别的语音应用状态机；由给定音源、VAD、wakeword 和 ASR 组成。main 是命令行辅助入口。 |
| [kit.logic.wakeword](python/kit-logic-wakeword.md) | KWS 或 ASR 关键词唤醒接口；Sherpa 后端依赖额外 runtime/模型，不能假定固件默认包含。 |
| [kit.logic.zones](python/kit-logic-zones.md) | 区域占用、越线、停留与滚动统计；区域坐标归一化，停留速度阈值按名义 640 像素坐标计算。 |
| [kit.media](python/kit-media.md) | RGA 图像操作、尺寸、矩形与精确映射的公共导出。 |
| [kit.media.image](python/kit-media-image.md) | RgaContext/ImageOps：NV12 转 RGB、resize、letterbox、crop。输入可借用 DMA，当前公开输出是 owned CPU RGB，并非端到端零拷贝。 |
| [kit.pipeline](python/kit-pipeline.md) | 检测→ROI→第二模型的级联工具；不是 kit.workflow.Pipeline。返回精确 ROI 映射，CPU 路径需 NumPy/PIL 或 OpenCV。 |
| [kit.resources](python/kit-resources.md) | 资源种类、lease 协议与 legacy exclusive NPU broker 租约；scheduled App 不应自行取得 exclusive lease。 |
| [kit.run](python/kit-run.md) | AppMgr 的 Python 入口加载器与 CLI。用于 host smoke／启动契约检查，业务 App 继承 App，不直接重写启动器。 |
| [kit.runtime](python/kit-runtime.md) | 模型前后处理与 session 命名空间。 |
| [kit.runtime.ctypes_rknn](python/kit-runtime-ctypes_rknn.md) | 平台推理后端实现及 RKNN ABI 结构。服务拥有 context/绑定 IO；普通 scheduled App 通过 self.models 调用，不能直接绕过调度。 |
| [kit.runtime.engine](python/kit-runtime-engine.md) | TensorSpec/ModelSpec、推理统计及 legacy 本地 RKNN session；布局是契约，不会隐式转置。scheduled 托管进程禁止直接本地 session。 |
| [kit.runtime.postprocess](python/kit-runtime-postprocess.md) | 模型专用后处理命名空间；必须匹配导出 head、类别数、输入尺寸与前处理映射。 |
| [kit.runtime.postprocess.classify](python/kit-runtime-postprocess-classify.md) | 分类 softmax、top-k、多头属性与表情解码；不要对已归一化概率重复 softmax。 |
| [kit.runtime.postprocess.ctc](python/kit-runtime-postprocess-ctc.md) | OCR CTC 字典加载、blank/重复折叠与置信度解码；字典顺序必须匹配模型。 |
| [kit.runtime.postprocess.db_ocr](python/kit-runtime-postprocess-db_ocr.md) | DB OCR 文本检测解码：二值化、轮廓、置信过滤和扩框；依赖 OpenCV。 |
| [kit.runtime.postprocess.detect](python/kit-runtime-postprocess-detect.md) | YOLO 检测 DFL/head 解码与 NMS，输出框映回原图；支持配置输入尺寸/类别，不能默认所有模型均为 COCO 640。 |
| [kit.runtime.postprocess.face_detect](python/kit-runtime-postprocess-face_detect.md) | 人脸检测专用后处理；按该导出的张量形状解析，不能直接代替通用 YOLO decoder。 |
| [kit.runtime.postprocess.landmark](python/kit-runtime-postprocess-landmark.md) | 人脸 landmark/face score 解码，按第二阶段 ROI 映射回原画面。 |
| [kit.runtime.postprocess.pose](python/kit-runtime-postprocess-pose.md) | YOLO pose 框和关键点解码；关键点数量/索引与模型一致。 |
| [kit.runtime.preprocess](python/kit-runtime-preprocess.md) | CPU 图像读取、letterbox 与模型输入构造；返回映射信息，RGB/BGR 和归一化由模型约定决定。 |
| [kit.runtime.remote](python/kit-runtime-remote.md) | scheduled 推理服务 client。支持共享 IO/兼容传输及 DMA prepared input，模型授权来自 AppMgr 分配身份和 manifest 工件。 |
| [kit.runtime.rknnlite](python/kit-runtime-rknnlite.md) | 平台 RKNNLite 兼容包装器：复制输出为独立 NumPy 数组，在初始化、推理返回/异常及释放时回收厂商循环缓冲；ctypes 后端不执行这些操作。普通应用使用 App 模型工厂，不直接实例化本后端。 |
| [kit.workflow](python/kit-workflow.md) | 同步 typed workflow 与有界队列公共导出；组合阶段不自动创建线程，也不自动仲裁 NPU。 |
| [kit.workflow.node](python/kit-workflow-node.md) | Stage、WorkflowContext 与协作取消/超时。资源声明只是显式对象依赖，超时在阶段边界检查，不能强制打断 native 调用。 |
| [kit.workflow.queue](python/kit-workflow-queue.md) | 线程安全有界 FIFO；block/drop_oldest/drop_newest 均有明确结果，丢弃项由调用者释放。 |
| [kit.workflow.runtime](python/kit-workflow-runtime.md) | 同步 Pipeline 的组合、运行、统计与逆序清理；Stage 所有权只能转移一次，关闭等待在途任务。 |
| [recamera_ext](python/recamera_ext.md) | Native Python SDK：帧/probe 借用、五类结果注入、硬件 mask、exclusive NPU 租约。OsdSink/RecordSink 仅限 AppMgr。 |
| [recamera_ext.buffer](python/recamera_ext-buffer.md) | Native BorrowedBuffer 与平面布局：源 lease 控制有效期，释放后访问报错，跨迭代持有必须 copy。 |
| [recamera_ext.errors](python/recamera_ext-errors.md) | native 返回码到 Python typed errors 的映射；保留 operation/detail，区分超时、权限、背压、格式与 capability 缺失。 |

其他公开边界：

- [完整 C ABI](c-abi.md)
- [HTTP、SSE、WebSocket 与平台接口](http.md)

## 覆盖定义

覆盖 `recamera_ext` 和 `kit` 非私有源码模块的公开定义、公开重导出、构造器、属性、类字段、枚举和本模块继承的方法；跨模块继承链接到基类。
包括兼容/平台实现供诊断，但不把它们视作普通应用可任意调用的接口。下划线私有模块、测试、构建缓存、示例 CLI 不作为公开开发契约。
参数签名、类型注解、默认值来自 AST；数据类的构造字段和原始文档一并保留。没有类型注解并不表示返回 None，返回语义以逐项说明为准。
源码中的旧示例仅用于理解该模块；托管应用的入口、NPU 调度和结果路由以 `features.md`、`managed-runtime.md` 为准。
接口存在不代表目标固件已部署，也不代表模型精度、吞吐或外设已通过真机验收。

## 维护检查

```text
python scripts/api_reference.py --sdk-root <checkout> --check
```

检查模块集合、源码散列、导出/签名/字段和生成文档。新模块或无说明的定义会失败。
审阅源码变化及 `api-notes.json` 后，使用 `--write --revision <full-commit>` 更新；命令不会更改 SDK 或打包器版本。

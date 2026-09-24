# API features and application contracts

[完整索引](index.md) · [HTTP/WS](http.md) · [C ABI](c-abi.md)

本说明与离线逐模块参考共同使用：索引覆盖全部公开定义/导出，逐项页面保留
参数顺序、关键字参数、类型、默认值、构造字段、返回语义和原始 docstring。
这里说明跨接口必须一致的单位、资源与调用边界。类型注解不是自动转换承诺。
未标注返回类型的函数也不能直接假定返回 None。

## Choose the application layer

| 场景 | 推荐入口 | 特性 / 前置条件 |
| --- | --- | --- |
| 应用中心安装与持续运行 | `kit.app.App` | `owns_loop=True`，`run(self)`，模型/资源/output/权限由 manifest 与 AppMgr 分配 |
| CPU 图像处理、直接结果注入、只读 probe | `recamera_ext` | native library、握手与端点授权；普通独立脚本如需打包进应用中心仍要 Kit 入口 |
| 统一创建图像/结果/模型资源 | `kit.Device` | 工厂+逆序释放，不拥有 RKIPC 的 VI/VPSS/VENC；只会选择环境中实际存在的适配器 |
| 类型明确的业务结果 | `kit.ai` / `kit.geometry` | 构造时校验，坐标显式，发送/渲染/录像仍是不同阶段 |
| 同步多阶段算法 | `kit.workflow` | 同步执行、协作取消、有界队列；不会自动建立线程或获取 NPU |
| 模型前后处理与跟踪状态机 | `kit.runtime` / `kit.logic` | CPU 算法工具，模型 head、字典、阈值和坐标必须匹配 |
| 设备管理与应用管理 UI | 公开 HTTP/WS 边界 | 通过 nginx 鉴权/同源入口；App 不通过内部 RPC 管理整机 |

包根 `kit` 是惰性导出，`App` 的导入路径仍为 `from kit.app import App`。
`kit.Frame` 与 `recamera_ext.Frame` 是不同类；`kit.pipeline.CascadePipeline`
与 `kit.workflow.Pipeline` 也不是同一抽象。按索引中的“定义与完整说明”确认来源。

## Frames, buffers and capabilities

参考：[native 帧](python/recamera_ext.md)、[BorrowedBuffer](python/recamera_ext-buffer.md)、
[Kit Frame](python/kit-frame.md)、[ImageBuffer](python/kit-buffer.md)、
[Device](python/kit-device.md)、[Capabilities](python/kit-capabilities.md)。

- `FrameSource.acquire(timeout_ms)` 是显式借用：成功返回 `FrameLease`，超时使用
  typed timeout error；必须 `release()` 或 `with`。迭代接口会在下一步释放上一帧。
  不要把 iterator 的结束语义与 acquire 的显式超时异常混淆。
- native 帧是 NV12；`array` 为 Y 平面，`to_bgr()` 才产生 BGR 图像。每个 plane
  有实际 offset/stride/vstride；不能假定 buffer 长度就是 `w*h*3/2` 或裸 RGB。
- `BorrowedBuffer` 的 owner 是源 lease。关闭 source、释放 lease 或推进迭代后，
  新访问会报释放错误；此前取出的裸 NumPy view 不能被 Python 包装器追溯保护。
  排队、跨线程或保留到下一帧前要复制，或显式保持有效 lease 且及时释放。
- `ImageBuffer` 用 `PixelFormat`、`MemoryKind`、`Ownership` 和 `PlaneLayout`
  描述数据。`from_numpy(copy=False)` 可以共享数组；`numpy(copy=False)` 可能借用。
  `copy()` 产生独立 owned CPU buffer，`release()` 幂等；不要依赖析构时机回收帧。
- `kit.Frame.copy()` 保留画面和时间元信息，但脱离源 DMA／roi_cropper 绑定。
  Kit 的 `pts` 为秒，native 的 `pts_us` 为微秒；二者不是 epoch 日历时间。
- `get_capabilities()` 返回能力快照。当前 filesystem probe 看到 socket 仅为
  `UNKNOWN`；`require()` 要求经过验证的能力时会拒绝它。不要把状态强制改成
  `AVAILABLE`，也不要用“目录里有 socket”替代实际握手/数据测试。
- `Device.open()` 不自动验证所有硬件；`require_verified` 要求的能力未被证明时
  失败。工厂资源被 Device 持有并逆序关闭，`close()` 返回关闭报告，应检查错误。

离线可运行的 buffer 所有权示例（需要 host NumPy/Kit）：

```python
import numpy as np
from kit import Frame

frame = Frame(data=np.zeros((16, 16, 3), dtype=np.uint8), fmt="RGB", pts_us=123)
saved = frame.copy()
frame.release()
assert saved.data.shape == (16, 16, 3)
saved.release()
```

## RGA geometry and image cost

参考：[RgaContext / ImageOps](python/kit-media-image.md)、
[App 前处理](../kit-app-patterns.md#frame-cost-and-throughput)、[级联](python/kit-pipeline.md)。

| 操作 | 输入 | 返回 / 映射 |
| --- | --- | --- |
| `convert_nv12(frame)` | 有效 NV12 DMA frame | owned RGB `ImageBuffer`，全图转换 |
| `resize_nv12(frame, Size(w,h))` | 有效 NV12 frame 与目标尺寸 | `TransformResult`，允许改变宽高比 |
| `letterbox_nv12(frame, Size(w,h), pad_value=114)` | 有效 frame 与填充值 | `TransformResult`，保持比例加 padding |
| `crop_nv12(frame, Rect(...), Size(...), pad_value=114)` | 原图矩形和目标尺寸 | `TransformResult`，按硬件限制对齐/裁剪；当前 ROI 路径要求方形目标 |

输入 frame 必须包含 fd、width、height、fourcc、planes 和有效性信息。
公开 `ImageOps` 是 `RgaContext` 的别名。当前这组接口的输出是 **CPU 可访问的
owned RGB**；DMA 输入可以减少输入拷贝，但不能宣传为端到端输出零拷贝。

`TransformMapping.source_rect/output_rect` 描述实际源采样窗口和目标有效图像区。
`to_source(x,y)` / `box_to_source(box)` 映回原图像素；不要用理想浮点 crop
替代硬件实际对齐矩形。`to_source` 不自动 clamp，落在 padding 的检测结果需过滤。
RGB 模型不应再做 BGR→RGB 翻转。使用 OpenCV BGR 数据时，明确在边界转换一次。

App 的 `hw-direct + model_dma_input=True` 是另一条面向模型的优化路径；把
`prepared = self.pre(frame)` 直接交给 `self.models.<id>.infer(prepared)`。
提前解包/访问 CPU 数组可能触发转换或拷贝。需要原图像素、ROI 二阶段推理时使用
合适的 `hw-roi`/CPU 路径；保持 prepared/frame 有效直到同步 infer 返回。

## Model sessions, scheduling and timing

参考：[本地 session](python/kit-runtime-engine.md)、[远端 session](python/kit-runtime-remote.md)、
[lease](python/kit-resources.md)、[ctypes 后端](python/kit-runtime-ctypes_rknn.md)。

- `TensorSpec(name, shape, dtype, layout)` 校验输入。`-1` 维度接受正尺寸；layout
  不是转置指令。不要把 uint8 NHWC 模型擅自改成浮点 NCHW，归一化要与转换时一致。
- `ModelSpec` 记录模型路径及输入/输出契约。文件存在、RKNN build 成功和实际推理
  精度正确是三个独立结论。模型工件仍需 manifest 中的 hash/size/mount 授权。
- **多输入与后端：**完整声明多个 `TensorSpec` 的 typed session 在默认 `auto`
  下选择 RKNNLite；远端调用使用普通张量协议。强制 `ctypes` 会拒绝多输入，
  当前共享 DMA/延迟 prepared input 路径也只支持单输入。输入数量不是 batch。
  普通 App Center 的 `models[].input` 授权与 `self.models` 兼容加载路径尚未完整
  传递多输入契约；不能仅凭底层 session 测试就承诺安装后可运行。具体声明、dtype、
  服务端配置和适配前提见[多输入与后端选择](../kit-app-patterns.md#multi-input-models-and-backend-selection)。
- **单输入特征与内存：**静态 uint8/int8/float16/float32 非图像输入可使用 ctypes
  普通 IO，例如 float32 NTF 语音特征；这不等于支持图像 DMA 前处理。RKNNLite
  兼容路径会复制输出并回收厂商循环缓冲，保持输出独立有效，有额外复制和 GC 开销。
- 普通应用使用 scheduled/brokered NPU，由 AppMgr 和推理服务持有 context。
  `self.models.<id>.infer(...)` 或 `Device.rknn_session(...)` 选择受管 client；
  `RemoteRknnSession` 直接构造也需要真实的受管身份和分配，不能伪造环境变量。
- `RemoteRknnSession.infer` 同步等待服务返回，含排队/传输/调度等成本。
  `stats.last_ms/total_ms` 是调用耗时；`last_timings_ms` 用于观察实际后端阶段。
  应用循环耗时还应在取帧、前处理、后处理和 emit 外围单独测量。
- 共享 IO、绑定缓冲与 DMA prepared input 是否使用，由协议协商和后端能力决定。
  `io_transport` 可检查当前通道；共享内存不意味着完全没有 IPC、同步或拷贝。
- `RknnSession` / 兼容 `RknnModel` 是 **legacy exclusive** 本地执行路径；
  先取得 `ExternalNpuLease`/native `InferenceLease`，初始化后 READY，释放 context
  后再释放最后一个租约引用。scheduled App 直接构造本地 session 会被拒绝。
  NPU 锁/租约用于平台所有权与生命周期协调，不能以“驱动支持多 context”绕过。
- `CtypesRknnModel` 和 `RknnIOBuffer` 是服务后端接口：native 张量属性、stride、
  fd 和指针需严格匹配 runtime ABI。普通应用优先走受管模型；不得自行关闭后端 fd。
- 多模型内存预算包含常驻模型、共享 IO 及应用工作内存，不能只相加模型文件大小。
  使用 [转换与模型声明](../model-conversion.md) 的预算与数值验证流程。

## App lifecycle and configuration

参考：[App 全部方法](python/kit-app.md)、[配置](python/kit-config.md)、
[受管启动](../managed-runtime.md)、[manifest](../manifest-contract.md)。

`setup(config)` 覆盖时调用 `super().setup(config)`；`run()` 不接收额外位置参数。
`frames()` 提供受管迭代，`pre()` 与模型尺寸一致，`emit()` 输出业务结果。
`on_config_reload`/`on_params_changed` 在约定的热更新点处理配置；不是任意字段修改
后都可以无重启生效。`finish()` 负责生命周期清理。旧 `on_results/process_frame/
run_postproc` 回调已移除，不能照历史示例生成。

CPU-only 设 `needs_model=False`；不取相机帧的应用同时设 `needs_frames=False`。
`run_app` 与 `kit.run` 用于启动/loader，正常应用包只提供 App 子类或显式 APP。
公开包的依赖、通道、权限和资源由 manifest 约束，API 存在并不能绕过权限。

最小 CPU 应用可被 AppMgr 加载，以下是一次性任务（执行后退出），持续应用需实现
自己的可停止循环；它不是保证常驻 running 的监控例子：

```python
from kit.app import App

class Demo(App):
    owns_loop = True
    needs_model = False
    needs_frames = False

    def run(self):
        self.emit(results=[], extra={"status": "done"})
```

## Typed results, geometry and delivery

参考：[数据类型](python/kit-ai-results.md)、[发布器](python/kit-ai-publisher.md)、
[几何](python/kit-geometry.md)、[结果 sink](python/kit-adapters-result_sink.md)。

- `Box/Detection/Classification/Keypoint/Pose/Track/Segmentation` 是构造时校验的
  业务对象；`to_dict/from_dict` 是规范 JSON，`to_legacy_dict/from_legacy_dict`
  保留原有业务扩展字段。`AIResult` 是类型联合，不是可实例化模型。
- `CoordinateSpace.PIXEL` 为原图像素，`NORMALIZED` 为 [0,1]，`MODEL` 为模型输入像素。
  `ResultBatch` 的 `frame_size/model_size` 用于边界验证；`pts_us` 为微秒。
- `to_legacy_payload` / `ResultBatchPublisher` 将归一化坐标乘画面尺寸；MODEL
  必须提供精确 `model_to_pixel`（例如预处理 mapping.to_source），不猜 letterbox。
  Track 转入 legacy events，其余类型进入 results，保持现有 sink 路由。
- `PublishReport.locally_accepted=True` 只代表 sink 本地接受；
  `server_acknowledged=False` 不应被业务当成远端已确认。检查实际 WS/录像/订阅端。
- 普通 managed App 用 `self.emit`，不要为 typed publisher 另建一个平台结果监听端口。
  新 typed API 与原有 App 入口是组合关系；需要发布器时接入受管 sink 的约定。
- native `ResultSink` 的五类结果全部使用归一化框/点，`pts_us` 为微秒。
  Kit legacy sink 使用原图像素和秒级 pts；避免重复除以宽高或重复乘 1e6。
- `GeometryBuilder` 的 point/line/polyline/polygon/box/quad/keypoints/pose
  生成规范图元列表。`geometry=None` 保持旧 envelope，`geometry=[]` 明确发空列表。
  style、点数和坐标由 sanitizer 校验，输出字段还要声明对应 coord。
- 生成结果、浏览器叠加、编码流 OSD、录像和通知是不同消费者。参照
  [叠加配置](../result-overlay.md) 和 [录像契约](../recording.md)；分割 mask 不进入
  当前 stream OSD/录像触发，不能因为有 Segmentation 类型就假定可用。

## Workflow, queues and cleanup

参考：[Stage/Context](python/kit-workflow-node.md)、[Pipeline](python/kit-workflow-runtime.md)、
[InputQueue](python/kit-workflow-queue.md)。

- Stage 封装 `fn`、名称、依赖资源、可选 closer。按源码签名组合阶段；Pipeline
  按顺序同步执行，不产生执行线程，不保证并行提速，也不替代 NPU 调度服务。
- `WorkflowContext.resources` 是调用方传入的对象映射；声明 `ResourceKind.NPU`
  不会自动取得 lease。context 不接管这些外部资源的关闭职责。
- `CancellationToken` 是协作取消。timeout/deadline 在阶段边界检查，不会安全地
  强制中止一个阻塞 Python/native 函数。deadline 是单调时间，不是日历 timestamp。
- Stage 只能归属一个 Pipeline；组合成功转移所有权，不能重复使用带 closer 的
  Stage 建两个 pipeline。close 阻止新任务、等待在途任务、逆序调用 closer；
  读取 `WorkflowCloseReport`，不要吞掉 cleanup error 或覆盖原始业务异常。
- InputQueue 是有界、线程安全队列：BLOCK 可等待并超时报错；DROP_OLDEST 逐出旧项；
  DROP_NEWEST 拒绝新项。`put()` 返回 `PutResult`，检查 accepted/status/dropped_item。
  队列不会替你释放丢弃帧；最好传入 owned copy，并回收被丢弃/清空的资源。
- close 唤醒等待者并禁止新 put；已入队项仍可排空，closed+empty 的 get 报错。
  `QueueStats` 是统计快照，不能仅用调用前的 size 做并发正确性判断。

## Pre/postprocessing and application logic

| 模块族 | 输入契约 | 输出及限制 |
| --- | --- | --- |
| `runtime.preprocess` | 图像、目标尺寸、模型归一化要求 | 模型输入 + LetterboxInfo；CPU 路径，确认 RGB/BGR |
| `postprocess.detect/pose/face_detect` | 对应导出的原始 RKNN outputs、尺寸、阈值、类别/关键点 | 原图结果；DFL、head 排列、关键点布局不可跨模型照搬 |
| `postprocess.classify` | logits/probabilities、多头布局 | softmax/top-k/属性；不要对概率重复 softmax |
| `postprocess.ctc/db_ocr` | OCR 输出、字典/阈值 | 文本或区域；字符表顺序与模型必须一致 |
| `postprocess.landmark` | 第二模型输出、ROI 映射 | 原图关键点；不能把模型坐标作为原图坐标 |
| `pipeline` | 原始图像、检测框、第二模型 | ROI/透视裁剪、补边与准确映射；CPU 与 RGA 边界填充可能不同 |
| `logic.tracker/geometry/zones` | 同一坐标约定的检测/关键点、秒级帧时间 | 局部 track、区域/越线/停留统计；不能当永久身份 |
| `logic.attributes/drowsiness/rep_counter/temporal` | 匹配模型的类别概率/FaceMesh/pose | 应用启发式状态；多对象要隔离状态，丢帧/遮挡需正确输入 |
| `logic.vad/wakeword/voice_sm` | PCM 采样率、声道、分块 | 语音段/唤醒/识别状态；额外模型与 backend 按声明准备 |

完整函数参数与返回说明见索引中每个模块。逻辑组件在 CPU 上工作，不能因为设备有
NPU 就假定跟踪、NMS、字符串解码或任意 Python 循环被硬件加速。

## Adapters, control and audio

- `select_*` 是后端选择器；`OfficialPcmSource` 和 `OfficialControl` 是占位实现。
  它们被导出是兼容设计的一部分，不是“已实现音频 socket/统一控制接口”的证据。
- `CgiControl.set_inference` 修改的是内置模型配置，不授予外部 NPU 所有权；
  `snapshot` 通过帧源 + JPEG 编码实现。该旧 wrapper 的 loopback TLS 行为是兼容
  实现细节，不应用作远程 HTTPS 客户端模板。
- `AiAsrAudioSource` 使用共享 ALSA ai_asr；RTSP/WAV 是其他音源；takeover
  模式有独占/停服务副作用。`PcmFrame` 是 s16le 数据、采样率、声道和秒级时间。
  `read()` 没有数据可返回 None；长期读取需有关闭/退出逻辑。
- ASR、Sherpa KWS、FFmpeg、二维码后端并不保证在所有固件中可用，检查目标依赖。
  公共 SDK 没有统一 speaker/player；播放走 [ALSA 条件路径](../audio-playback.md)。
- 输出 formatter 只编码，channel 才传输。Jinja 环境受限；HTTP/MQTT/UART
  各有网络/串口权限和依赖。`emit` 通常为 best-effort，strict/checked 方法用于
  观察本地失败；都不自动证明最终消费者已处理。避免业务热循环直接阻塞慢网络。

## Errors and performance reporting

`recamera_ext.errors` 将 native rc 映射为 typed Python 错误，包含 `operation/detail`；
Kit 错误包含 `operation/code/details` 和原始 cause。不要只匹配英文错误文本。
`InputValidationError` 优先检查 shape/dtype/坐标，`CapabilityError` 检查实际功能，
`ResourceBusyError` 检查分配与生命周期，`BufferReleasedError` 检查借用有效期。
超时/断连之后不得假定服务端完全没执行；重试管理操作前查询状态。

分别报告取帧等待、前处理、模型调用、后处理、输出和循环总耗时；
调用耗时包含排队/IPC 等成本，不能与纯 NPU backend 时间混称。
`get_logger/configure_logging/redact_url/WarningLimiter` 用于诊断；日志里不输出访问口令。

<a id="native-and-platform"></a>
## Native and platform-only surfaces

[C ABI](c-abi.md) 是精确的结构体布局、枚举、错误码和函数声明；它不是额外的
Python 应用权限。result/frame/probe/mask/inference 各有配对 open/close、
borrow/release、超时和端点限制，按头文件逐项遵守。

`OsdSink` / `rc_ext_osd_*` 仅供 AppMgr visualization bridge，当前仅接受检测框；
`RecordSink` / `rc_ext_record_*` 仅供 AppMgr 录像桥接。记录 reset 清理排队结果，
不取消已发生的录像。普通 App 用 emit/request_recording，通过 manifest 授权。

HTTP API 是应用中心管理边界，不把 server.py 的 `do_*` Python 函数作为 App SDK。
模型服务的私有 socket 协议、ctypes RKNN 内部结构与 RKIPC 私有 RPC 不能成为绕过
公开 client 的应用接口。后台服务导出的兼容/管理能力不等于普通 App 自动获准调用。

# reCamera Pro 扩展 API：方案商开发总入口

> 适用设备：reCamera Pro（RV1126B / recamera_v2）。
> 本文是方案商开发的**汇总入口 + 快速上手 + 两个核心 API（帧代理 / 结果注入）详解**；
> 其余能力各有专篇，见下方能力地图链接。
>
> 事实来源：
> - 规格：`../api/spec.md`（§1 握手/身份/错误码、§2 帧代理、§3 结果注入、§8 架构）
> - C ABI：`sdk/include/recamera_ext.h`
> - Python 封装：`sdk/python/recamera_ext/__init__.py`
> - proto：本仓 `sdk/proto/ext_api.proto`、`sdk/proto/inference.proto`；固件侧
>   对应 `recamera_ipc/protobufs/`，其中 `inference.proto` 还与嵌套 Vigil
>   仓的协议副本做构建期哈希校验

---

## 1. 概览

reCamera Pro 的固件（rkipc 主程序 + 官方推理 + Web 后端）通过一组**运行时接口**向第三方进程开放。核心前提贯穿全文：

> **这些 API 是运行时接口，但前提是设备已经安装与 SDK 匹配、且包含扩展
> endpoint 的补丁固件。** 在这种设备上，方案商应用可作为独立进程接入，
> 无需为每个应用重编固件。当前工作树已经从 v1.6.0 handoff 恢复 native
> client 与 rkipc 服务端源码，并增加了源码构建门禁；但这些改动尚需提交到
> manifest 所固定的仓库版本并完成 RV1126B 真机验收。因此仍不能把“仓库里
> 有预编译文件”理解为任意出厂固件都可直接使用。

扩展 API 能做什么：

- **拿帧（帧代理）**：从摄像头零拷贝取到 NV12 帧，喂给你自己的模型/算法。
- **回注结果（结果注入）**：把你算出的检测框送回固件，叠加到编码视频，并分发到 WS/MQTT/HTTP/UART；保留无 `dSource` 旧规则的录像行为，但不能伪造显式 APP 来源。
- **应用触发录像**：托管应用在 manifest v2 声明 `record_trigger`，由 appmgr 将获准的结果送入受保护的 Vigil 录制入口。
- **音频**：从预留的 ALSA PCM 通道取麦克风原始音频。
- **GPIO 触发**：用推理结果驱动引脚（继电器/LED/告警）。
- **前端扩展**：把自己的网页和后端挂到官方 Web 入口下，复用官方登录会话。
- **结果推送**：只要分发、不要叠加时，向 legacy notify 通道注入结果。

### 1.1 能力地图

| 能力 | 状态 | 对接方式 | 文档 |
|---|---|---|---|
| **Python AI 工作流 API**（typed errors / buffer / RGA / RKNN / lifecycle） | Python + native 源码已接通；host/交叉构建通过，待目标板完整 E2E | `recamera_ext` + `kit` + appmgr managed launch | [python-ai-api.md](./python-ai-api.md) |
| **帧代理**（零拷贝取帧） | 服务端/client 源码已恢复；重启屏障与 host 测试已补，待目标板回归 | `FrameSource` / C ABI `rc_ext_frame_*`，`/run/recamera/frame.sock` | 本文 §3 |
| **结果注入**（OSD+推送+旧规则录像） | 服务端/client 源码已恢复；Python `send_*` 已封装，待目标板回归 | `ResultSink` / C ABI `rc_ext_result_*`，`/run/recamera/result-in.sock` | 本文 §4 |
| **托管应用触发录像** | manifest 校验、appmgr 过滤桥接、Vigil 按来源规则已实现，待目标板回归 | manifest v2 `record_trigger` → appmgr → 受保护的 `/run/recamera/record-in.sock` | [app-package-v2.md](./app-package-v2.md#managed-recording-triggers) |
| **NPU 独占仲裁** | `inference-control@1` broker + Python lease + appmgr 路由已实现；host kill/HUP 测试通过，待目标板压力测试 | `InferenceLease` / `ExternalNpuLease`，`/run/recamera/inference-control.sock` | [python-ai-api.md](./python-ai-api.md#6-externalnpulease-与-rkipc-broker) |
| **音频 PCM** | 现成可用 | `arecord -D ai_asr`（ALSA dsnoop 共享） | [audio-pcm.md](./audio-pcm.md) |
| **GPIO 结果触发** | 现成可用（组合现有零件） | notify WS + gmgr API，无需固件新功能 | [gpio-result-trigger.md](./gpio-result-trigger.md) |
| **前端扩展挂载** | 现成可用 | `ext_<name>.conf` + `/extension/<name>/`（复用 JWT 会话） | [frontend-extension.md](./frontend-extension.md) |
| **结果推送（notify）** | 现成可用 | 向 `/var/tmp/notify` 写 `InferenceResult`（仅分发，不上 OSD） | [result-push.md](./result-push.md) |
| **统一 AI Result Hub v2** | 后端协议/订阅/重放已实现 | `/ws/ai/results/v2`（app + builtin，raw/formatted）；legacy 8123/8124 保留 | [result-hub-v2.md](./result-hub-v2.md) |
| **硬件隐私遮罩**（COVER 增量控制） | 固件 + SDK 就绪；线 B 冷启动真机验证通过 | `MaskControl` / C ABI `rc_ext_mask_*`（rkipc RPC，M4） | [hw-mask-api.md](./hw-mask-api.md) |
| **输出组件**（声明式结果输出） | 现成可用；真机 + 本地 broker 验证（P3b） | manifest `capabilities:["output"]` + `output` 块，`ConfigurableSink`（零 app 代码） | [output-sink.md](./output-sink.md) |
| **硬件预处理加速**（RGA letterbox） | `hw-direct` 真机 A/B **+55%**；`hw` 实测无收益（+0.8%），默认不开 | `App.model_frame = "hw-direct"`（一行类属性，零 RGA 代码） | [hw-preprocess.md](./hw-preprocess.md) |
| **RK 硬件编解码**（补 GStreamer 插件） | 出厂镜像无；自行补齐后**硬解**真机验证通过，**编码未测** | 交叉编译 `libgstrockchipmpp.so` + buildroot 现成的 `h265parse`，只写 `/userdata` | [hw-codec-gstreamer.md](./hw-codec-gstreamer.md) |
| **rkipc RPC / 配置类** | 走 HTTP API | entry.cgi HTTP API（`/var/tmp/rkipc` 是内部接口，勿直连） | [rkipc-rpc-status.md](./rkipc-rpc-status.md) |
| **观测面（M3）** | 已实现（真机验证）；SDK client `ProbeSource`（v1.2.0） | `probe.sock`：preproc/npu.raw/postproc/metrics 采样 | 见本文 §4.8 与规格 §4 |
| 控制面（M4）/ 显示（M5）/ 生态（M6）/ 沙箱分发 | 规划中 | — | 见本文 §8 与规格 |

> **当前发布边界**：本工作树现在可以从源码交叉构建
> `librecamera_ext.so.1` 与带 frame/result/probe/inference-control 服务端的
> `rkipc`，但 `release/pkg/sdk` 仍是旧的 sideload 快照，不能作为权威输入。
> 正式发布前还必须提交并锁定 recamera_ipc、Vigil 与本外部仓的匹配 commit，
> 由顶层固件构建生成产物，再完成真机 kill/restart/OTA 矩阵。详细状态见
> [Python AI 工作流 API 与生命周期](./python-ai-api.md#0-先读当前可用范围)。

> **帧代理 vs 结果注入 vs 应用触发录像 vs notify 的选择**：
> - 要拿摄像头画面自己推理 → **帧代理**（§3）。
> - 要让你的结果出现在视频叠加 + 推送 → **结果注入**（§4，`result-in.sock`）；保留无 `dSource` 旧规则录像兼容。
> - 要让已安装的托管应用按 AI 结果启动录像 → 在 manifest 声明 **`record_trigger`**；应用不能直接访问内部录制 socket。
> - 只要把结果推给外部消费者、不需要叠加/录像 → **notify**（`result-push.md`）。

### 接入方式：两种，同一套契约

扩展 SDK 是**一套 C ABI 契约 + 一层 Python ctypes 薄封装**，不是两套独立实现。

- **C / C++**：include `sdk/include/recamera_ext.h`（带 `extern "C"` 保护，C 与 C++ 都可直接 include），链接 `librecamera_ext.so.1`。函数族 `rc_ext_frame_*` / `rc_ext_result_*` / `rc_ext_probe_*` / `rc_ext_inference_lease_*`。
- **Python**：`import recamera_ext`（ctypes 薄封装，运行时加载**同一个 `.so`**）。类 `FrameSource` / `ResultSink` / `ProbeSource` / `InferenceLease`。

二者**同 socket、同 wire 协议、同契约**；Python 不是独立实现，是 C ABI 的封装。选 C/C++ 还是 Python 只取决于你的应用语言。

| 能力 | C ABI（`recamera_ext.h`） | Python（`recamera_ext`） |
|---|---|---|
| 帧代理 | `rc_ext_frame_*` | `FrameSource` |
| 结果注入 | `rc_ext_result_*`（`rc_ext_result_send_*`） | `ResultSink`（`send_*`） |
| 观测面 probe | `rc_ext_probe_*` | `ProbeSource` |
| NPU 独占租约 | `rc_ext_inference_lease_*` | `InferenceLease`（kit 默认包装为 `ExternalNpuLease`） |

### 1.2 前置条件（对所有 socket API 通用）

- socket 位于 `/run/recamera/`。**v1 权限模型为 root-only**：目录
  `0750 root:root`；公开的 frame/result/probe/inference-control socket 为
  `0660`，appmgr 专用的 osd-in/record-in socket 为 `0600`。进程需以 root
  运行（麦克风/摄像头/`/dev/mpi` 设备节点均 root 属主，扩展应用经启动脚本以 root 拉起）。
  > 按组隔离的方案（`recamera-ext` 组、socket 0660 组可写）随 P1 沙箱一并落地，v1 未建组——见规格 §1.1 / §6 上机实证。v1 扩展全 root，组隔离无实际意义。
- C 客户端链接 `librecamera_ext.so.1`；Python 客户端 `import recamera_ext`（ctypes 薄封装，运行时加载同一 `.so`）。
- 六条 socket（`frame.sock` / `result-in.sock` / `osd-in.sock` /
  `record-in.sock` / `probe.sock` / `inference-control.sock`）连接后都先走一次
  **Hello/HelloAck 握手**（§5），
  SDK 内部自动完成，无需手写 protobuf。

---

## 2. 快速上手（5 分钟跑通）

以下两段代码从 SDK Python 源码逐行核实，可直接复制到设备上运行（需 `pip`/`uv` 装 `numpy`；`to_bgr()` 另需 `opencv`）。

### 2.1 拿帧（5 行）

```python
from recamera_ext import FrameSource

with FrameSource() as src:          # 默认订阅 NPU 同款分辨率/格式（NV12）
    for frame in src:               # frame.array: 零拷贝 Y 平面视图 (height, width)
        infer(frame.array)          # 你的推理；离开本次迭代帧自动释放
```

`frame.array` 是**灰度 Y 平面**的零拷贝视图（形状 `(height, width)`，`uint8`）。多数检测/关键点模型可直接吃灰度或在你侧转换；需要彩色 BGR 时用 `frame.to_bgr()`（返回拷贝，见 §3.3）。

### 2.2 注入结果（5 行）

```python
from recamera_ext import ResultSink

with ResultSink(source_id="face-app") as sink:
    # boxes: (x1, y1, x2, y2, score, label[, class_id]) 归一化 [0,1] 坐标，score 0..1
    sink.send_detections(pts_us=0, boxes=[(0.16, 0.17, 0.38, 0.63, 0.92, "person")])
```

`pts_us=0` 表示不与具体帧关联（结果照常叠加/推送）。要与某一帧对齐叠加时，传该帧的 `frame.pts_us`（见 §4）。

### 2.3 拿帧 + 推理 + 回注（组合）

```python
from recamera_ext import FrameSource, ResultSink

with FrameSource() as src, ResultSink(source_id="my-app") as sink:
    for frame in src:
        boxes = my_model(frame.array)          # -> [(x1,y1,x2,y2,score,label), ...]
        sink.send_detections(pts_us=frame.pts_us, boxes=boxes)
```

`frame.pts_us` 与固件内建推理同一时钟源（VI 帧 PTS，`CLOCK_MONOTONIC` 微秒），OSD 侧按 pts 做就近帧匹配。

---

## 3. 帧代理 API（M2 — `frame.sock`）

从摄像头取零拷贝 NV12 帧。服务端在同一 VI pipe 新开一路空闲通道（chn1）+ 私有 DMABUF 池，与内建推理隔离——订阅者行为不影响官方推理帧率（规格 §2.1，门禁 G1 已真机 PASS）。

### 3.1 C ABI（`recamera_ext.h`，v1 冻结）

签名逐一核实自 `sdk/include/recamera_ext.h`：

```c
// 打开：连接 /run/recamera/frame.sock，完成握手 + FrameSubscribe。
// cfg=NULL 用 NPU 同款默认；失败返回 NULL 并置 *err 为 rc_ext_err_t。
rc_ext_frame_t *rc_ext_frame_open(const rc_ext_frame_cfg_t *cfg, int *err);

// 读回实际生效的订阅几何（握手后有效）；任意 out 指针可为 NULL。
int rc_ext_frame_geometry(rc_ext_frame_t *h, uint32_t *width, uint32_t *height,
                          uint32_t *fourcc, uint32_t *pool_depth,
                          uint32_t *max_outstanding);

// 等下一帧，最多 timeout_ms。返回:
//   0  -> *out 有一帧（fd 有效，未 map）
//   1  -> 超时无帧（重试）
//  <0  -> -rc_ext_err_t：EOF/传输错误或协议违规，应停止并 close 句柄
int rc_ext_frame_next(rc_ext_frame_t *h, rc_ext_frame_buf_t *out, int timeout_ms);

// mmap(PROT_READ) + DMA_BUF_IOCTL_SYNC(START|READ)，返回 plane[0](Y) 首地址。
void *rc_ext_frame_map(rc_ext_frame_t *h, rc_ext_frame_buf_t *f);

// SYNC(END|READ) + munmap + 回发 8 字节 release seq + close fd。NULL 安全、幂等。
void rc_ext_frame_release(rc_ext_frame_t *h, rc_ext_frame_buf_t *f);

void rc_ext_frame_close(rc_ext_frame_t *h);
```

典型循环（头文件注释给出的用法）：

```c
int err;
rc_ext_frame_t *h = rc_ext_frame_open(NULL, &err);
rc_ext_frame_buf_t f;
int rc;
while ((rc = rc_ext_frame_next(h, &f, 1000)) >= 0) {
    if (rc == 1) continue;                 // 超时，重试
    void *y = rc_ext_frame_map(h, &f);     // 内部做 dma-buf cache 同步
    // 用 f.plane[i].offset/stride/vstride、f.pts_us、f.width/height ...
    rc_ext_frame_release(h, &f);           // 必须：SYNC END + 归还 buffer + close fd
}
rc_ext_frame_close(h);
```

关键结构体（核实自 `recamera_ext.h`）：

```c
typedef struct {
    uint32_t offset;   // 相对 dma-buf 起始的字节偏移
    uint32_t stride;   // 行跨度（字节）
    uint32_t vstride;  // 行数（含对齐补齐）
} rc_ext_plane_t;

typedef struct {
    uint64_t seq;       // 单调递增；出现 gap = 服务端丢过帧
    uint64_t pts_us;    // VI PTS（CLOCK_MONOTONIC 微秒）
    uint32_t width;     // 有效像素
    uint32_t height;
    uint32_t fourcc;    // RC_EXT_FOURCC_NV12
    uint32_t buf_size;  // dma-buf 有效总长
    uint16_t flags;     // bit0: 此帧之前发生过丢帧
    uint8_t  chn_id;
    uint8_t  n_planes;  // NV12 = 2
    rc_ext_plane_t plane[3]; // NV12: plane[0]=Y, plane[1]=UV
    int fd;             // dma-buf fd（借用期间 >=0，释放后 -1）
    void *_base;        // 内部：mmap base
    size_t _map_len;    // 内部：mmap 长度
} rc_ext_frame_buf_t;

typedef struct {         // 可选订阅配置，NULL = NPU 同款默认
    uint32_t width;      // 0 = 默认
    uint32_t height;     // 0 = 默认
    uint32_t fourcc;     // 0 = NV12
    uint32_t fps_divisor;// 0/1 = 每帧, 2 = 隔帧, ...
} rc_ext_frame_cfg_t;
```

常量：`RC_EXT_FRAME_MAGIC = 0x52434652`（"RCFR"）、`RC_EXT_FRAME_VER = 1`、`RC_EXT_FOURCC_NV12 = 0x3231564E`。

### 3.2 Python（`FrameSource` / `Frame`）

核实自 `python/recamera_ext/__init__.py`：

```python
FrameSource(config=None, timeout_ms=1000, lib_path=None)
```

打开后暴露的属性（握手回填）：`src.width`、`src.height`、`src.fourcc`、`src.pool_depth`、`src.max_outstanding`。迭代 `for frame in src:` 产出 `Frame`。可选自定义订阅：

```python
from recamera_ext import FrameSource, FrameConfig
# 隔帧订阅（约半帧率），其余默认
with FrameSource(FrameConfig(fps_divisor=2)) as src:
    print(src.width, src.height, src.pool_depth, src.max_outstanding)
    for frame in src:
        ...
```

> **v1 生效字段**：`fps_divisor` **已实现**——服务端按 divisor 抽帧（`2` = 隔帧交付，实测到达帧率≈半，`seq` 每被采集帧递增、交付的是每第 N 个），`0`/`1` = 每帧。`width`/`height`/`fourcc` 在 v1 **固定为 NPU 同款几何**（`FrameConfig` 里这三项即使传入也被忽略），实际生效值以握手/订阅 ack 回填的 `src.width`/`src.height`/`src.fourcc` 为准。自定义分辨率/格式随后续里程碑开放。

`Frame` 对象字段（全部核实自源码 `Frame.__init__`）：`seq`、`pts_us`、`width`、`height`、`fourcc`、`buf_size`、`flags`、`chn_id`、`n_planes`、`dropped`（= `flags & 1`）、`planes`（`[(offset, stride, vstride), ...]`）。方法：`array`（属性）、`plane_array(i)`、`to_bgr()`。

### 3.3 零拷贝语义与生命周期陷阱（务必读）

以下语义核实自源码 `Frame`（docstring + `array`/`plane_array`/`to_bgr`）与 `FrameSource.__next__`：

- **`frame.array` 是零拷贝视图，不是拷贝。** 它是对底层 dma-buf `mmap` 内存的 numpy 视图（`plane_array(0)` 经 `np.lib.stride_tricks.as_strided` 构造，再裁到 `[:height, :width]`）。读它 = 直接读硬件刚写入的那块内存。
- **只在当前迭代有效。** `FrameSource.__next__` 每次进入先调 `_release_cur()`——把上一帧 `rc_ext_frame_release`：`munmap` + 回发 release seq + close fd，buffer 归还硬件池。因此**上一次迭代拿到的 `array` 在下一次迭代开始后失效**，其内存可能已被后续采集帧覆写。
- **跨帧保留必须 `.copy()`。** 要把某帧留到循环之外（存盘、放进队列、跨线程传、攒 batch），必须 `frame.array.copy()` 得到独立拷贝。直接存 `frame.array` 引用 = 存了个会被覆写的窗口。
- **`frame.to_bgr()` 返回拷贝。** 它把 NV12（裁掉 stride padding）转成连续 BGR 新数组（`cv2.cvtColor`），可安全保留到迭代之外。代价是一次 NV12→BGR 转换 + 一次拷贝。
- **不要按 width/height 自行推导布局。** plane 的 `offset/stride/vstride` 由服务端按 MPI 实际分配填写，`stride ≥ width`、`vstride ≥ height`（含对齐补齐）。必须用 `frame.planes` / `plane[i]` 的真实值，SDK 的 `plane_array` 已按此构造。
- **cache 同步已由 SDK 内置。** `frame.array` 首次访问触发 `_map()` → `rc_ext_frame_map` → `DMA_BUF_IOCTL_SYNC(START|READ)`；release 时 `SYNC(END|READ)`。裸用 fd 自实现协议者必须自己做这对同步，否则 CPU 读到的可能是脏数据。

**何时必须 copy：**

| 用法 | 是否要 copy | 原因 |
|---|---|---|
| 当前迭代内读 `frame.array` 做推理 | 不用 | 视图在本次迭代有效 |
| 把帧存盘 / 编码 / 上传 | 要 `.copy()` 或 `to_bgr()` | 迭代推进后视图失效 |
| 放进队列给另一线程处理 | 要 `.copy()` 或 `to_bgr()` | 消费时视图可能已被覆写 |
| 攒多帧做 batch / 光流 / 帧差 | 每帧都要 `.copy()` | 只有最新一帧的视图有效 |
| `to_bgr()` 的返回值 | 不用（已是拷贝） | `to_bgr` 内部 new 数组 |

### 3.4 线格式（自实现协议者参考）

每帧一条 SEQPACKET 消息：ancillary data 携带**恰好 1 个** dma-buf fd（`SCM_RIGHTS`），payload 为定长 **96 字节头**（C 布局、小端；数据面不用 protobuf）。字段与上面 `rc_ext_frame_buf_t` 一致（wire 头另含 `magic`/`ver`/`reserved`）。接收纪律（规格 §2.3）：

- `recvmsg` 一律带 `MSG_CMSG_CLOEXEC`；
- 收到 `MSG_CTRUNC`、fd 数 ≠ 1、magic/ver 不符、长度 ≠ 96 → **close 全部收到的 fd** + 计协议错误 + 断开重连；
- CPU 访问前后必须 `DMA_BUF_IOCTL_SYNC(START|READ)` / `(END|READ)`；
- 处理完发回 8 字节 `uint64_t seq` release 消息（无 fd）。

背压（规格 §2.4）：发送端 `MSG_DONTWAIT` 永不阻塞；每连接可同时持有的帧数上限 = `max_outstanding`（握手返回，默认 2）；多订阅者上限 4。慢消费者（持续 5 秒 outstanding 满且零 release）被服务端断开（EBACKPRESSURE）。**及时 release 是你的责任**——Python 迭代自动 release，C 侧必须显式 `rc_ext_frame_release`。

---

## 4. 结果注入 API（M1 — `result-in.sock`）

把你算出的推理结果（检测 / 分类 / 分割 / 跟踪 / 关键点）送回 rkipc，进入公开结果链路的下游：

1. **OSD 叠加**：`osd_manager_draw_infer()` 画进 RTSP/预览叠加层，按 `source_id` 哈希分配颜色；
2. **推送**：`rc_notify_send_inference()` 转发 WS（本机 `127.0.0.1:8123` / 外部 `/ws/inference/results`）/ MQTT / HTTP / UART。

3. **旧规则录像**：经身份规范化和校验后进入 legacy-only Vigil，仅匹配未设置 `dSource` 的传统规则；分割不参与规则。

> **安全边界**：公开 `result-in.sock` 保留传统规则的既有录像权限，但不能匹配
> 显式 BUILTIN/APP，即使外部 peer id 与 app id 相同。规则省略 `dSource` 时
> 编辑/保存仍省略，不静默转成内建来源。托管应用要成为显式 APP 录像来源，应在 manifest v2 声明
> `record_trigger`，由 appmgr 过滤后桥接到仅 appmgr 可访问的
> `record-in.sock`；应用进程不得直接连接该内部端点。

SDK 覆盖全部五种任务类型：`send_detections` / `send_classification` / `send_segmentation` / `send_tracking` / `send_keypoints`（C ABI 与 Python 一一对应）。每个 `send_*` 打包对应的 `InferenceResult` oneof 分支，发一条 datagram；`pts_us` 语义一致（`CLOCK_MONOTONIC` 微秒，`0` = 不关联帧）；返回 0 成功，负值 = `-rc_ext_err_t`。所有 `const char *label` 均接受 `NULL`（当 `""`）。

> **坐标契约（务必遵守）**：所有 box 坐标（检测 / 分类 ROI / 分割 ROI / 跟踪 / 关键点对象框）以及关键点 point 的 `x/y` 均为**归一化 [0,1]**——相对画面宽高的比例（左上 `x1/y1`、右下 `x2/y2`，`0..1`，如 `0.5` = 居中）。OSD 渲染器（`osd_infer.c`）先 clamp 到 [0,1] 再乘画面宽高，**传像素值会被压成 1px 隐形框**，务必发比例。分割的 `mask` 是行主序原始字节（非坐标），不受此约束。

### 4.1 C ABI（`sdk/include/recamera_ext.h`，v1 冻结）

打开/关闭（对所有任务类型通用）：

```c
// 连接 /run/recamera/result-in.sock 并握手。source_id 为建议值（服务端按
// peercred 身份可能改写）；失败返回 NULL 并置 *err。
rc_ext_result_t *rc_ext_result_open(const char *source_id, int *err);

void rc_ext_result_close(rc_ext_result_t *h);  // NULL 安全
```

**检测（DETECTION）** — `recamera_ext.h:22-42`：

```c
typedef struct {
    float x1, y1, x2, y2;  // 归一化 [0,1] 坐标（左上 / 右下，相对画面宽高）
    float score;           // 置信度 0..1
    const char *label;     // 类名；NULL -> ""
    int class_id;
} rc_ext_box_t;

int rc_ext_result_send_detections(rc_ext_result_t *h, uint64_t pts_us,
                                  const rc_ext_box_t *boxes, size_t n);
```

**分类（CLASSIFICATION）** — 可表示整幅分类，也可附一个来源 ROI（见 §4.5）：

```c
typedef struct {
    float score;       // 置信度 0..1
    int class_id;
    const char *label; // 类名；NULL -> ""
    int has_box;       // 0=整幅分类；1=x1/y1/x2/y2 有效
    float x1, y1, x2, y2; // 可选 ROI，归一化 [0,1]
} rc_ext_class_t;

int rc_ext_result_send_classification(rc_ext_result_t *h, uint64_t pts_us,
                                      const rc_ext_class_t *items, size_t n);
```

**分割（SEGMENTATION）** — `recamera_ext.h:63-78`。每条 = 可选 ROI box + 行主序 mask（`mask` 指向 `mask_w*mask_h` 字节，可为 `NULL` 且 `mask_w=mask_h=0`）：

```c
typedef struct {
    float x1, y1, x2, y2;
    float score;
    int class_id;
    const char *label;
    const uint8_t *mask; // 行主序，mask_w*mask_h 字节；NULL -> 空
    int mask_w;
    int mask_h;
} rc_ext_seg_t;

int rc_ext_result_send_segmentation(rc_ext_result_t *h, uint64_t pts_us,
                                    const rc_ext_seg_t *items, size_t n);
```

**跟踪（TRACKING）** — `recamera_ext.h:81-94`。检测框 + 持久 `track_id`：

```c
typedef struct {
    float x1, y1, x2, y2;
    float score;
    int class_id;
    const char *label;
    int track_id;
} rc_ext_track_t;

int rc_ext_result_send_tracking(rc_ext_result_t *h, uint64_t pts_us,
                                const rc_ext_track_t *items, size_t n);
```

**关键点（KEYPOINTS）** — `recamera_ext.h:97-121`。每个实例 = 可选对象框（`has_box=0` 时整组 object_info 省略，与 proto 一致）+ 一组关键点：

```c
typedef struct {
    float x, y;
    float score;      // 关键点置信度（非对象置信度）
    int keypoint_id;  // 调用方自定义关键点 schema 里的索引
} rc_ext_point_t;

typedef struct {
    int has_box;         // 0 -> 整组对象框/score/class/label 省略
    float x1, y1, x2, y2;
    float score;         // 对象置信度
    int class_id;
    const char *label;
    const rc_ext_point_t *points;
    size_t n_points;
} rc_ext_kpinstance_t;

int rc_ext_result_send_keypoints(rc_ext_result_t *h, uint64_t pts_us,
                                 const rc_ext_kpinstance_t *instances, size_t n);
```

### 4.2 Python（`ResultSink`）

核实自 `sdk/python/recamera_ext/__init__.py`。构造与方法：

```python
ResultSink(source_id, lib_path=None)

# 检测：boxes 每条 (x1, y1, x2, y2, score, label[, class_id])   __init__.py:241
sink.send_detections(pts_us, boxes)

# 分类：items 每条 (score, class_id, label)，或附 ROI：
#       (score, class_id, label, (x1, y1, x2, y2))
sink.send_classification(pts_us, items)

# 分割：items 每条 (x1, y1, x2, y2, score, class_id, label,
#                   mask_bytes, mask_w, mask_h)                   __init__.py:280
#      mask_bytes 可为 None/空（配 mask_w=mask_h=0）
sink.send_segmentation(pts_us, items)

# 跟踪：items 每条 (x1, y1, x2, y2, score, class_id, label, track_id)  __init__.py:307
sink.send_tracking(pts_us, items)

# 关键点：instances 每条为一个 dict（一个对象）：                  __init__.py:328
#   {"points": [(x, y, score, keypoint_id), ...],   # 必填
#    "box": (x1, y1, x2, y2),                        # 可选；省略 -> 无对象框
#    "score": float, "class_id": int, "label": str}  # 对象级，配合 box
sink.send_keypoints(pts_us, instances)
```

`label` 可为 str（自动 UTF-8 编码，支持中文）或 bytes。检测的 `class_id` 可省略（默认 0）。任一方法失败抛 `RuntimeError`（消息含 `rc=`）。

### 4.3 每种任务的最小示例

均可直接复制（`source_id` 换成你的应用名，坐标为归一化 [0,1]）。OSD 叠加效果见 §4.4。

```python
from recamera_ext import ResultSink

with ResultSink(source_id="my-app") as sink:
    # 检测：画框 + label（坐标归一化 [0,1]）
    sink.send_detections(0, [(0.16, 0.17, 0.38, 0.63, 0.92, "person", 0)])

    # 分类：top-k 标签（无位置，见 §4.5）
    sink.send_classification(0, [(0.87, 3, "cat"), (0.09, 5, "dog")])

    # 分割：ROI box（归一化）+ 行主序 mask（此处传空 mask）
    sink.send_segmentation(0, [(0.16, 0.17, 0.38, 0.63, 0.9, 0, "person", None, 0, 0)])

    # 跟踪：检测框（归一化）+ 持久 track_id
    sink.send_tracking(0, [(0.16, 0.17, 0.38, 0.63, 0.92, 0, "person", 7)])

    # 关键点：一个带框的实例 + 3 个点（box/point 均归一化，COCO 式 keypoint_id）
    sink.send_keypoints(0, [{
        "box": (0.16, 0.17, 0.38, 0.63), "score": 0.92, "class_id": 0, "label": "person",
        "points": [(0.24, 0.22, 0.9, 0), (0.27, 0.22, 0.9, 1), (0.25, 0.30, 0.8, 2)],
    }])
    # 关键点也可省略 box（纯骨架，不画对象框）：
    sink.send_keypoints(0, [{"points": [(0.24, 0.22, 0.9, 0), (0.25, 0.30, 0.8, 2)]}])
```

### 4.4 OSD 渲染现状（诚实标注）

> ⚠️ **坐标契约（务必遵守）：所有 box 坐标与 keypoint 点坐标均为归一化 `[0,1]`（相对画面宽高的比例），不是像素。** 设备 OSD 渲染器（`osd_infer.c`：`osd_infer_box_to_rect` / `osd_infer_norm_to_pixel`）对坐标 `clamp(0,1)` 后再乘画面宽高。**若传像素值（如 240、300），会被 clamp 到 1.0 → 框缩成右下角 1 像素 → 画面上看不见框。** 早期 header 曾误标"pixels"（v1.2.0 已更正），按像素接入的框不显示即此原因。把你的像素结果除以画面宽高转成 `[0,1]` 再注入。

各任务类型注入后进入 OSD 叠加与 WS·MQTT·HTTP·UART 推送。**注入链路（`send_*` 返回 0）与推送链路（WS 能收到）对所有任务类型一致**；除分割外还可匹配无 `dSource` 旧 Vigil 规则。OSD 画面渲染的真机端到端验证程度不同：

| 任务类型 | 注入 + WS 推送 | OSD 画面渲染 | 端到端真机验证 |
|---|---|---|---|
| 检测 | 是 | 画检测框 + label（按 `source_id` 哈希配色） | **已验证**（2026-08-12 真机：归一化坐标注入→RTSP `live/0` 抓帧确认框 + label 上屏；WS + SDK C ABI 端到端，CHANGES §4） |
| 跟踪 | 是 | 画框 + `track_id`（复用同一 `osd_infer_box_to_rect`） | 渲染走已验证的 `osd_infer_box_to_rect`（检测/关键点框均真机验证同一函数）；跟踪单类型专项截图未单独抓（多类型同注时被单槽覆盖，见下） |
| 关键点 | 是 | 画对象框 + 关键点圆点（`osd_infer.c` TASK_TYPE_KEYPOINTS，`osd_infer_norm_to_pixel`，点坐标同为归一化） | **已验证**（2026-08-12 内建关闭下真机：KPINST 对象框 + 关键点圆点按归一化坐标精确上屏） |
| 分类 | 是 | 画标签文本 + 可选 ROI 框（见 §4.5，box 可选，走 `osd_infer_box_to_rect`） | 渲染走已验证的 `osd_infer_box_to_rect`；带 box 的分类单类型专项截图未单独抓（同上单槽覆盖） |
| 分割 | 是（注入/WS 可用） | **不上 OSD** —— `osd_infer.c` 的 task_type switch 只枚举 检测/分类/跟踪/关键点四类，SEGMENTATION 命中 `default` 返回 `-EOPNOTSUPP`，不写任何像素（`osd_infer.h` 标 `segmentation // future` 未实现） | **OSD 不渲染 seg**（源码确认 2026-08-12）；seg 结果仍走 WS 推送，但不触发录像 |

诚实结论：**注入能通、WS 能收,对全部五种任务成立**；OSD 画面渲染方面,**检测框（INJTEST）与关键点（KPINST 框+圆点）均已真机端到端复验（2026-08-12，坐标修正后确认按归一化坐标上屏）**；跟踪/分类走同一套已验证的 `osd_infer_box_to_rect`（单类型专项截图未单独抓）；**分割不上 OSD**（渲染器无 seg 分支）。

> **OSD 单槽行为（务必知悉）**：非检测类结果（跟踪/分类/关键点）在 OSD 侧共用**单个 INFER 覆盖槽,后写覆盖前写**——同一时刻只显示最近一次注入的那一类。实测:一轮里连发 tracking→classification→keypoints,画面只留 keypoints(最后写的)。且**内建推理活跃时其检测 OSD ~19fps 持续重绘,会盖过 5Hz 外部注入**。检测类是例外:`rc_result_osd` 对 DETECTION 按 `source_id` 做并集,外部检测与内建检测可共存。**结论:要在 OSD 上稳定显示外部结果,单一 source 单一类型注入,或关掉内建推理;需要多路叠加请以 WS 结构化结果为准。** 坐标一律归一化 `[0,1]`（见上方警示）。

### 4.5 分类通道可选 ROI box（v1.1.0+）

`InferenceClassificationEntry` 自 v1.1.0 起带**可选** `InferenceBox box`（proto tag 4，向后兼容；detection/segmentation/tracking 也都带 `InferenceBox`）。多目标场景（如画面里多张脸各自的属性）可用它表达"某属性属于哪个目标"。

- **带位置的分类**（每条属性绑定一个 ROI）：`send_classification` 的 item 传第 4 元素 `(x1,y1,x2,y2)`（归一化 `[0,1]`，见 §4.4 坐标契约），OSD 会在该 ROI 画框 + 标签。
- **整幅分类**（无位置）：省略 box，照常输出 top-k 标签文本。
- C 侧 `rc_ext_class_t` 的 `has_box` + `x1/y1/x2/y2` 承载该字段（`has_box=0` 时省略）；Python `send_classification` item 为 `(score, class_id, label)` 或 `(score, class_id, label, (x1,y1,x2,y2))`。

（早期版本无此字段时的变通是把属性拼进 detection 的 `label`；v1.1.0 起可直接用分类 box，不再需要变通。）

### 4.6 约束

- **`source_id` 不能用保留字 `"builtin"`**——那是内建推理专用，外部使用被拒（EAUTH，规格 §1.1）。服务端按连接的 peercred 身份校验；无注册条目时会把 source_id 改写为 `"uid:<n>"`。
- **限速**：两级 token bucket——每连接 **60 msg/s**（burst 15）+ 全局 120 msg/s（burst 30）；单条 payload ≤ 64 KB。超限**丢弃 + 计数**（不断连接）。控制你的发送速率不要超过帧率。
- **并发**：≤ 4 个结果注入连接。
- **pts 对齐**：传 `frame.pts_us` 让 OSD 按就近帧匹配（容差 1 帧周期）；传 0 则不关联，照常叠加/推送。

### 4.7 结果去向 vs notify 的区别

`result-in.sock`（本 API）= OSD + 推送 + 无 `dSource` 旧规则录像兼容；`/var/tmp/notify`（[result-push.md](./result-push.md)）= **只推送、不叠加、不录像**、0666 无鉴权的 legacy 通道。要框出现在画面里就用本 API；托管应用要成为显式 APP 录像来源则声明 [`record_trigger`](./app-package-v2.md#managed-recording-triggers)。

### 4.8 观测面 probe（`rc_ext_probe_*` / `ProbeSource`，v1.2.0）

订阅内建流水线的 tap 点，观测 rkipc 实际喂进/送出各级的数据（调预处理、核对 NPU 输入、抓后处理张量）。与帧代理（§3.1 C ABI / §3.2 Python）、结果注入（§4.1 C ABI / §4.2 Python）一样，probe 也是 C ABI + Python 成对提供，连 `/run/recamera/probe.sock`。

#### C ABI（`sdk/include/recamera_ext.h`，v1 冻结）

签名逐一核实自 `recamera_ext.h`：

```c
// 打开：连接 /run/recamera/probe.sock，握手 + ProbeSubscribe（stage id 列表 +
// sample_every：每 N 次推理采 1 次，0 => 1）。失败返回 NULL 并置 *err
// （无任何 stage 被接受时为 EFORMAT）。
rc_ext_probe_t *rc_ext_probe_open(const char *const *stage_ids, size_t n_stages,
                                  uint32_t sample_every, int *err);

// 读回实际生效的订阅信息（握手后有效）；任意 out 指针可为 NULL。返回 0。
// subscribed_mask 用 RC_EXT_PROBE_MASK_{PREPROC=0x1,NPU=0x2,POSTPROC=0x4,METRICS=0x8}。
int rc_ext_probe_info(rc_ext_probe_t *h, uint32_t *sample_every,
                      uint32_t *subscribed_mask);

// 等下一条服务端推来的样本，最多 timeout_ms。返回:
//   0  -> *out 有一条样本（必须 release）
//   1  -> 超时无样本（重试）
//  <0  -> -rc_ext_err_t：EOF/传输错误或协议违规，应停止并 close
int rc_ext_probe_next(rc_ext_probe_t *h, rc_ext_probe_sample_t *out, int timeout_ms);

// 结束一条样本：memfd 样本做 munmap + close memfd；inline 样本为 no-op。
// 释放后 s.payload/s.stage_id 失效。NULL 安全、幂等。
void rc_ext_probe_release(rc_ext_probe_t *h, rc_ext_probe_sample_t *s);

void rc_ext_probe_close(rc_ext_probe_t *h);
```

样本结构体（核实自 `recamera_ext.h`）：

```c
typedef struct {
    const char *stage_id;   // "preproc.out"/"npu.raw"/"postproc.out"/"metrics"
    uint64_t seq;           // 单调递增推理 seq；出现 gap = 丢过样本
    uint64_t pts_us;        // CLOCK_MONOTONIC 微秒（与帧同一时钟）
    const uint8_t *payload; // 样本字节（inline 或 memfd 支撑），仅 release 前有效
    size_t payload_len;
    uint32_t flags;         // bit0: 此样本之前发生过丢样本
    int has_meta;           // != 0 时下列张量描述符有效（preproc/npu stage）
    uint32_t shape[8];
    uint32_t n_shape;
    uint32_t dtype;         // 0=uint8 1=int8 2=uint16 3=int16 4=float32 5=float16
    uint32_t layout;        // 0=unknown 1=NCHW 2=NHWC
    uint32_t fourcc, width, height, stride;
    float scale; int32_t zero_point;  // 量化参数
    // 其余为内部记账字段（_fd/_base/_map_len/_pb），调用方不用碰
} rc_ext_probe_sample_t;
```

- **inline vs memfd**：小样本（`metrics` / `postproc.out`）**inline** 在 `payload`（`_fd == -1`）；大张量（`preproc.out` / `npu.raw`）走 **memfd**（`SCM_RIGHTS`），SDK 内部 `mmap(PROT_READ)`（无需 dma-buf cache sync）。`payload` 指针仅在 `rc_ext_probe_release` 之前有效；跨样本保留必须自行拷贝。

典型循环（头文件注释给出的用法）：

```c
const char *stages[] = {"metrics", "preproc.out"};
int err;
rc_ext_probe_t *h = rc_ext_probe_open(stages, 2, 1, &err);
rc_ext_probe_sample_t s;
int rc;
while ((rc = rc_ext_probe_next(h, &s, 1000)) >= 0) {
    if (rc == 1) continue;                 // 超时，重试
    // 用 s.stage_id、s.seq、s.pts_us、s.payload[0..s.payload_len)
    // s.has_meta -> s.width/s.height/s.shape/s.dtype ...（张量 stage）
    rc_ext_probe_release(h, &s);           // 必须：munmap/close memfd；inline 为 no-op
}
rc_ext_probe_close(h);
```

#### Python（`ProbeSource`）

SDK client `ProbeSource` 自 v1.2.0 起可用，连 `/run/recamera/probe.sock`：

```python
from recamera_ext import ProbeSource

with ProbeSource(stages=["metrics"]) as ps:   # 也可 stages=["preproc.out","npu.raw","postproc.out"]
    for s in ps:
        print(s.stage_id, s.seq, len(s.payload))
```

`ProbeSource(stages=[...], sample_every=1, timeout_ms=1000)` 是迭代器，产出 `ProbeSample`：`.stage_id` / `.seq` / `.pts_us` / `.payload`（原始字节）/ `.meta` / `.array`（零拷贝 ndarray）。

- **stages**：`metrics`（流水线计数/耗时等小数据，**inline** 随消息带回）、`preproc.out`（预处理输出图）、`npu.raw`（NPU 原始输出）、`postproc.out`（后处理张量）。
- **inline vs memfd**：`metrics` 走 inline；`preproc.out` / `npu.raw` / `postproc.out` 是大张量，服务端经 **memfd** 传递，`.array` 对该内存做**零拷贝** ndarray 视图（生命周期同帧代理，跨迭代保留需 `.copy()`）。
- 真机已验证 metrics + preproc.out 双路通过。`letterbox padding` 之类"喂 NPU 的图不对"的 bug 就是靠订阅 `preproc.out` 抓实际输入图发现的。

---

## 5. 握手与版本协商（规格 §1.2）

六条 socket 连接后都先做一次 protobuf 握手（定义在 `ext_api.proto`），
**SDK 内部自动完成**，方案商通常无需手写；这里说明其语义，便于自适应与
自实现协议。

```proto
message Hello {
  uint32 version_min = 1;   // 客户端支持的版本区间（含端点），v1 客户端两者都填 1
  uint32 version_max = 2;
  string client_name = 3;   // 诊断用，如 "face-app"
  string auth = 4;          // peercred 模式忽略；token 模式启用
}
message Capability {
  string name = 1;                 // frame/result/osd/record/probe/inference-control
  uint32 version = 2;
  map<string, uint32> limits = 3;  // 如 {"max_msg_rate":60,"max_sources":8,"pool_depth":6}
}
message HelloAck {
  uint32 api_version = 1;          // 服务端在 [version_min, version_max] 内选的最高共同版本
  uint32 server_build = 2;
  string auth_mode = 3;            // v1 = "peercred"
  repeated Capability capabilities = 4;
  int32 error = 5;                 // 0=OK；非 0（EVERSION）时服务端关闭连接
  string error_msg = 6;
}
```

- **协商规则**：服务端在客户端 `[version_min, version_max]` 与自身支持集合的**交集**内取最大值；交集为空 → `error = EVERSION` 并关闭连接（不是 `min(client, server)`）。
- **认证模式**：v1 `auth_mode = "peercred"`（用 `SO_PEERCRED` 取连接 pid/uid/gid 做身份）。将来 app token 作为**新增模式**并行提供，peercred 模式保留，老客户端不断。
- **按 limits 自适应，不要硬编码**：并发数、速率、池深都在 `Capability.limits` 里返回，可能随固件变化。例如结果注入按 `limits["max_msg_rate"]` 控发送速率、帧代理按 `max_outstanding` 控持帧数。Python 侧 `src.pool_depth` / `src.max_outstanding` 即来自握手回填。
- **v1 baseline 承诺**：能力 `frame@1` / `result@1` / `osd@1` / `record@1` /
  `probe@1` / `inference-control@1` 一经发布不可移除。socket inode 存在仍不代表兼容，
  客户端必须完成 Hello/HelloAck。

---

## 6. 错误码表（规格 §1.3，SDK 统一暴露）

C ABI 中 `open` 失败置 `*err` 为正值码，`send`/`next` 失败返回其负值（`-code`）。

| 码 | 名称 | 含义 / 典型场景 |
|---|---|---|
| 0 | OK | 成功 |
| 1 | EVERSION | 版本区间无交集 |
| 2 | EAUTH | source_id 冒充（如用了 `"builtin"`）/ token 无效 |
| 3 | EBUSY | 订阅数达上限 / NPU 通道未启用 |
| 4 | EFORMAT | 请求的格式/分辨率不支持 / 消息解析失败 |
| 5 | EBACKPRESSURE | 慢消费者被服务端断开（帧代理背压，见 §3.4） |
| 6 | ERATELIMIT | 超出消息速率/配额（丢弃计数在 metrics 可见） |
| 7 | EINTERNAL | 服务端内部错误，附 `error_msg` |

Python 侧这些码经 `RuntimeError` 抛出（消息含 `err=` / `rc=`）；帧迭代遇负码返回时 `FrameSource.__next__` 抛 `StopIteration`（EOF/服务端错误，迭代自然结束）。

---

## 7. 其他现成通路

各自有专篇，此处一句话定位 + 链接：

- **音频 PCM** — [audio-pcm.md](./audio-pcm.md)：`arecord -D ai_asr -r 16000 -c 4 -f S16_LE` 从预留的 ALSA `ai_asr` 通道取 4 通道麦克风原始 PCM（软件取 ch0，勿用 `-c 1`），dsnoop 共享、不与 rkipc 音频冲突、无 VQE（AEC/NS 自理）。
- **GPIO 结果触发** — [gpio-result-trigger.md](./gpio-result-trigger.md)：订阅 notify WS 拿结果 + gmgr API 写引脚（`GPIO3_B2`=pin106 / `GPIO3_B3`=pin107），检测到目标即拉高/拉低引脚。仅数字 0/1，无 PWM。不需改固件。
- **前端扩展挂载** — [frontend-extension.md](./frontend-extension.md)：放一个 `ext_<name>.conf` 到 nginx 配置目录，把你的页面/后端挂到 `/extension/<name>/`，复用官方 dashboard 的 JWT 登录会话。
- **AI 消息发送设置** — [output-settings.md](./output-settings.md)：结果输出配置、草稿预览和 HTTP/MQTT 发送测试，以及网页结果与实际投递状态的区别。
- **结果推送（notify）** — [result-push.md](./result-push.md)：向 `/var/tmp/notify` 写 `<le32 len><InferenceResult>`，分发到 WS/MQTT/HTTP/UART。仅分发、不上 OSD、无鉴权、受全局限速。要叠加请改用本文 §4 的结果注入；托管应用要按结果启动录像则声明 [`record_trigger`](./app-package-v2.md#managed-recording-triggers)。
- **rkipc RPC 现状** — [rkipc-rpc-status.md](./rkipc-rpc-status.md)：`/var/tmp/rkipc` 是 rkipc↔entry.cgi 的内部 RPC，不承诺稳定、勿直连；配置类需求走 entry.cgi HTTP API，等 M4 版本化控制面。
- **统一 AI 结果与软件叠加** — [result-hub-v2.md](./result-hub-v2.md) / [ai-result-overlay.md](./ai-result-overlay.md)：`/ws/ai/results/v2` 以稳定 v2 envelope 合并 builtin 与多 app，支持 raw/formatted、latest frame、status 保留与 event 短重放；8123/8124 只作 legacy 兼容。浏览器叠加不进码流。
- **模型上板（zero-to-deployed）** — [model-onboarding.md](./model-onboarding.md)：方案商把自己的模型跑到设备上的端到端主线——ONNX 导出 → 检查 IR/opset → RKNN 转换（`rknn-toolkit2` 2.3.x，target `rv1126b`）→ 量化校准 → 放进 app `models/` + manifest 声明 → 打包/装/激活/验证。深度转换细节指向 `models/convert/` 项目。
- **推理即应用** — [inference-as-app.md](./inference-as-app.md)：内建推理经 `builtin.py` driver 变一等 app、`activate` 单活互斥切换、`config_schema` 的 `apply:live|restart` 热更（SIGHUP → kit 自动重绑 + `on_params_changed`）、`SchemaForm` 动态配置面板。
- **硬件隐私遮罩** — [hw-mask-api.md](./hw-mask-api.md)：`rc_ext_mask_*` / `MaskControl` 控制 VI 层硬件 COVER 遮块，增量移动不闪、不落盘；auto/manual 配额 `[3,6)`/`[0,3)`。
- **RK 硬件编解码** — [hw-codec-gstreamer.md](./hw-codec-gstreamer.md)：出厂镜像缺的是 `gstreamer-rockchip` 插件层而非芯片能力（MPP/RGA 库与 `/dev/mpp_service` 都在）。零源码修改交叉编译出 `libgstrockchipmpp.so`，`h265parse` 取 buildroot 现成产物，只写 `/userdata`，硬件 H.265 **解码**端到端实测通过（`gi` + `cv2.CAP_GSTREAMER`）；**编码器未测**，且会与 rkipc 抢 VEPU。含三条坑（`LD_LIBRARY_PATH` 必须追加、registry 缓存）。
- **输出组件（声明式结果输出）** — [output-sink.md](./output-sink.md)：manifest 声明 `capabilities:["output"]` + `output` 块（fields/映射/模板），kit 的 `ConfigurableSink` 把每帧结果发到 MQTT/HTTP/UART/WS，含 Home Assistant Discovery + 上下线 LWT，**app.py 零输出代码**；不声明则 app 自己发。
- **应用日志（app.log 轮转 + Web 查看）** — [app-log.md](./app-log.md)：appmgr 把每个 app 的 stdout/stderr 接到 pipe、由后台 drain 线程写进 `app.log` 并**运行期实时轮转**（单文件默认 2 MiB、留 3 代、单应用 ≤ 8 MiB），长期运行也不无限增长；Web 日志页跨 `app.log`+`app.log.1` 补足 512 KiB 窗口，轮转后也不会看到空。

---

## 8. 能力现状与路线

| 能力 | 里程碑 | 状态 |
|---|---|---|
| 结果注入（OSD+推送+旧规则录像） | M1 | client/server 源码已恢复并可交叉构建；历史补丁固件已验证，当前整合版待真机回归 |
| 托管应用触发录像（manifest + appmgr + source-aware Vigil） | M1.5 | host/交叉构建与自动化测试通过，待目标板端到端回归 |
| 帧代理（零拷贝取帧 + C ABI） | M2 | client/server 源码已恢复；runtime video restart 屏障与 host 生命周期测试通过，待真机回归 |
| 音频 PCM / notify / 前端挂载 / rkipc 文档化 | M0 | 现成可用 |
| 观测面（`probe.sock`：preproc/npu.raw/postproc/metrics 采样） | M3 | client/server 源码已恢复并可构建；历史补丁固件已验证，当前整合版待真机压力测试 |
| NPU 独占 broker（`inference-control.sock`） | M4 | lease/epoch/generation/READY/HUP fallback 已实现并通过 host kill 测试；数据端点 fencing 待下一版 |
| 控制面（`/api/v1/ext/*` capabilities） | M4 | capabilities 已接入；CGI 仅查询/配置，不代理 NPU 或媒体数据面 |
| 显示（M5）/ 生态框架接入 gstreamer/ffmpeg（M6）/ 沙箱与打包签名分发 | M5/M6 | 规划中 |

细节以 `../api/spec.md` 为准（§2 帧代理、§3 结果注入、§4 观测面、§8 架构与扩展模型）。

**对方案商的兼容性承诺（规格 §8.2 扩展五规则）**：
1. **加法优先**：新能力 = 新 socket 路径 + 新 Capability + 新 proto 消息，现有端点线格式/路径/语义不动。
2. **数量演进走 limits**：配额变化只改 `Capability.limits` 数值，客户端按握手返回值自适应（不得硬编码）。
3. **结构演进走保留位**：`frame_hdr` 的 `ver` + `reserved` 承载新字段；重排/删字段才升 `ver`，旧 `ver` 至少再支持两个固件版本。
4. **任务类型演进走 oneof 追加**：`InferenceResult` 新任务 = 新 oneof 分支（tag 15+），旧读者跳过未知分支。
5. **只增不减**：`/run/recamera/` 存在期间 v1 baseline 能力与线格式永不移除。

C ABI 交付物 soname `librecamera_ext.so.1`（semver，libabigail 做符号级兼容检查）。

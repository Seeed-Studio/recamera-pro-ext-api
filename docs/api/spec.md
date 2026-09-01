# reCamera Pro 扩展 API 规格（v0.3）

> **文档性质**：实施规格。本文把上游能力分析（内部设计文档）的 6.2/6.3 落成可开发的契约定义与实现方案。
> **代码基**：`recamera_v2` manifest main 分支（2026-08-10 sync，82 仓库），路径基于 `project/app/`。
> **状态**：v0.1 评审 REDESIGN-NEEDED（7 条）→ v0.2 复审 **SHIP-WITH-FIXES**（3 CLOSED / 4 PARTIAL / 3 新发现）→ v0.3 吸收全部剩余项（两轮修订记录见文末）→ **v0.4 增补 §8 架构与扩展模型**（服务端核心库、扩展五规则、兼容性工程）。字段编号、socket 路径、结构体布局已冻结。
>
> **交付现状**：M1 结果注入、M2 帧代理、M3 probe 与 M4 硬件遮罩曾在
> 匹配的 v1.x 补丁固件上运行；v1.6.0 handoff 已把相应 client/server 源码
> 补回当前工作树。本轮另新增 `inference-control@1`。host/交叉构建结果不
> 等同目标板发布，当前整合版仍须完成 RV1126B 真机 kill/restart/OTA 门禁。
> 下文保留的早期规划口吻应以各小节“当前状态”与 guide 为准。

---

## 0. 范围与分期

| 里程碑 | 内容 | 依赖 |
|---|---|---|
| **M0 存量文档化** | `ai_asr` PCM、notify 入站格式、`ext_*.conf` 挂载、`/var/tmp/rkipc` RPC 现状说明 | 无（纯文档） |
| **M1 结果回注** | rkipc 入站结果 socket + OSD/录像/WS 三路分发 + proto 扩展 | 门禁 G3/G4 |
| **M2 帧代理** | 零拷贝帧扇出子系统 + C ABI SDK | 门禁 G1/G2/G5/G6 |
| **M3 观测面** | rc_infer stage tap + 预处理回显 + metrics | M1（复用其 socket 骨架） |
| **M4 控制面** | `/api/v1/ext/*` 版本化域 + capabilities + app token | M1-M3 落定后收口 |
| 后续 | Python SDK 完整版、VQE-PCM 扇出、file-source 注入、沙箱、打包签名 | M2 |

本规格详述 M1/M2/M3，M0/M4 给要点。**沙箱、打包分发、声明式后处理不在本版范围**（设计文档 P1/P2）。
**v1 交付物以 C ABI 为准**；Python 是 C 库的薄封装，行为不独立定义（评审发现 7）。

**两种接入面，同一套契约**：扩展 SDK 对外提供 C ABI（`sdk/include/recamera_ext.h`，带 `extern "C"`，C 与 C++ 均可 include）+ Python 封装（`recamera_ext`，ctypes 薄封装，运行时加载同一 `.so`）。两者共用本规格定义的 socket 路径、握手与 wire 协议；本文主体即该 wire/ABI 契约。

---

## 1. 公共约定

### 1.1 传输、路径与身份

- 所有新增 IPC 走 **unix domain socket（SOCK_SEQPACKET）**，路径统一 `/run/recamera/`：
  - `/run/recamera/frame.sock` — 帧订阅（M2）
  - `/run/recamera/result-in.sock` — 结果回注（M1）
  - `/run/recamera/probe.sock` — 观测面（M3）
  - `/run/recamera/inference-control.sock` — external RKNN 单 owner 仲裁（M4，连接即租约）
- 选 SEQPACKET：天然消息边界；fd 传递（`SCM_RIGHTS`）与消息原子绑定。
- **权限（v1 实测：root-only，共享 root）：目录 `0750 root:root`，socket 文件 `0660`**。原设计曾拟用 `recamera-ext` 组做粗粒度授权，但上机（2026-08-10，V1.0.10）确认扩展应用**实际以 root 运行**（媒体设备节点均 root 属主，非 root 开不了），故 v1 落地为共享 root、无独立组；身份区分靠下条的 SO_PEERCRED + appmgr 注册表（见 §1.1 校准说明）。拆分组做多级授权留作后续演进。
- **身份（v1 即有，不推迟）**：服务端对每个连接取 `SO_PEERCRED`（pid/uid/gid），作为连接身份记录并用于：
  - `source_id` 防冒充：`"builtin"` 为保留字，外部连接使用一律拒绝（EAUTH）。
  - **source_id ↔ uid 绑定注册表**：`/run/recamera/apps.d/<name>.conf`（内容：`uid=<n>`，root 只写）。连接的 peercred uid 在注册表中有对应条目 → 允许使用该 `<name>` 作为 `source_id`；无条目 → 强制改写为 `"uid:<n>"`，`Hello.client_name` 仅作诊断标注。v1 注册文件由管理员手写，打包分发落地后由安装器生成——机制不变，来源变。
  - 限速与配额按 (uid, socket) 记账。
- **认证模式协商**：`HelloAck.auth_mode` 返回当前模式（v1 = `"peercred"`）。将来 app token 作为**新增模式**并行提供，`peercred` 模式保留——老客户端不因沙箱上线而断（评审发现 5）。
- 存量 0666 端点（`/var/tmp/notify` 等）**不动、不增强**：永久定位为无特权 legacy 通道，服务端对其加全局限速（复用 3.3 的 token bucket），不再赋予新能力。

> **上机实证（2026-08-10，V1.0.10 固件）对身份模型的校准**：
> - **扩展应用的真实运行身份是 root**，不是 uid 1000 的普通用户。设备上现存音频扩展经 `appmgr serve` 以 root 启动（实证 pid 2948 `python app.py`）。原因：媒体设备节点 `/dev/snd/*`、`/dev/video*`、`/dev/mpi/*` 均 `root` 属主（`0660 root:audio` 等），非 root 用户即便 dsnoop IPC 是 0666 也开不了硬件节点。**因此 §1.1 的 peercred uid 绑定在 v1 实际区分的是"哪个 root 扩展"而非"哪个非特权 uid"**——SO_PEERCRED 仍取 pid/uid，但 apps.d 注册表的 key 应是 appmgr 分配的 app 标识（经 pid→appmgr 反查或约定的凭据文件），不能假设一个扩展一个独立 uid。这项在打包分发（appmgr 集成）里定案；v1 手写注册表阶段按 pid 白名单兜底。
> - **推论**：真正的 source_id 防冒充在 v1（全 root）下只能防"手滑"不能防"恶意"——恶意 root 扩展能直接写任意 socket。这与"沙箱是平台责任、v1 未做"一致（设计文档 6.1 原则 5）；文档需对方案商明示"v1 阶段扩展间无强隔离"。

### 1.2 握手与版本协商

连接后客户端先发 `Hello`，服务端回 `HelloAck`，之后进入各自协议。protobuf
编码；公共 SDK 的权威副本是 `sdk/proto/ext_api.proto`，固件侧对应
`recamera_ipc/protobufs/ext_api.proto`（构建门禁要求两份 generated wire
定义一致）：

```proto
message Hello {
  uint32 version_min = 1;   // 客户端支持的版本区间（含端点）
  uint32 version_max = 2;   // 当前均为 1
  string client_name = 3;   // 诊断用，如 "face-app"
  string auth = 4;          // peercred 模式下忽略；token 模式启用
}
message Capability {
  string name = 1;              // 如 "frame", "result.osd", "probe.stage"
  uint32 version = 2;           // 能力自身版本
  map<string, uint32> limits = 3; // 如 {"max_subscribers":4, "pool_depth":6, "max_msg_rate":60}
}
message HelloAck {
  uint32 api_version = 1;   // 服务端在 [version_min, version_max] 内选的最高共同版本
  uint32 server_build = 2;
  string auth_mode = 3;     // "peercred" | "token"
  repeated Capability capabilities = 4;
  int32 error = 5;          // 0=OK；非 0（如 EVERSION）时服务端关闭连接
  string error_msg = 6;
}
```

- **协商规则**：服务端在客户端声明的 `[version_min, version_max]` 与自身支持集合的交集内取最大值；交集为空 → EVERSION + 关闭（评审发现 4：不再用 `min(client, server)`）。
- **v1 baseline 承诺**：能力 `frame@1` / `result@1` / `probe@1` /
  `inference-control@1` 一经发布**不可移除**。能力演进 = 新增 Capability
  或提升 version，limits 数值可变（客户端必须按 limits 自适应）。socket
  inode 存在不代表协商成功，客户端仍须完成 Hello/HelloAck。
- **schema 演进纪律**：proto tag 永不复用；删除字段必须 `reserved`；**中转组件（notify-server 等）转发外来 payload 时透传原始字节，禁止 decode→re-encode**（防旧端丢未知字段）；CI 加 v1 客户端 ↔ v2 服务端双向 round-trip 测试。

### 1.3 错误码（SDK 层统一暴露）

| 码 | 含义 | 典型场景 |
|---|---|---|
| 0 | OK | |
| 1 | EVERSION | 版本区间无交集 |
| 2 | EAUTH | source_id 冒充 / token 无效 |
| 3 | EBUSY | 订阅数达上限 / NPU 通道未启用 |
| 4 | EFORMAT | 请求的格式/分辨率不支持 / 消息解析失败 |
| 5 | EBACKPRESSURE | 慢消费者被服务端断开（见 2.4） |
| 6 | ERATELIMIT | 超出消息速率/配额（丢弃计数在 metrics 可见） |
| 7 | EINTERNAL | 服务端内部错误，附 error_msg |

---

## 2. M2 帧代理（`frame.sock`）

### 2.1 服务端架构

新增独立线程模块 `recamera_ipc/src/rv1126b_ipc/video/frame_export.{c,h}`，不侵入现有 NPU 取帧循环：

- **帧源**：同 VI pipe 新开一路 chn（现用 0/2/3/4/5，**chn1 空闲**，即第 2 个物理 ISP 通道；单 pipe 上限 2 phy + 6 ext + 1 vir，`rk_defines.h:55-58`），**私有 DMABUF 池**，与 NPU chn4 隔离——订阅者行为不影响内建推理。
  > **G1 已 PASS（2026-08-11 真机实证）**：独立进程 `SetChnAttr(0,1)`/`EnableChn(0,1)` ret=0，连取 45/45 帧全成功；rkipc ISP 帧率无系统性掉帧（37→36 落在 6-8fps 自然抖动内），dmesg 零 VPSS 崩溃。通道 bookkeeping 每进程私有——外部进程无法从 rkipc 的 chn4 取帧（ret 0xa0088003），但能新开自己的 chn1。**"帧代理必须编进 rkipc 进程内"的硬约束解除**（见 §8.1）。
- 分辨率/格式默认对齐 NPU 通道当前配置，支持订阅时请求 sub-stream 分辨率（chn 独立缩放能力属门禁 G1）。
- 取帧：`RK_MPI_VI_GetChnFrame(pipe, 1, &frame, timeout)` → `RK_MPI_MB_Handle2Fd(frame.stVFrame.pMbBlk)` 导出 dma-buf fd（SDK 已提供该 API，旁路 demo `uvc_app_tiny/uvc/nn_process.cpp:252` 有用例）。
- **无订阅者时不取帧**（chn disable），零常驻开销。

### 2.2 订阅协议

```proto
message FrameSubscribe {
  uint32 width = 1;        // 0 = 默认（NPU 同款）
  uint32 height = 2;
  uint32 fourcc = 3;       // 0 = NV12
  uint32 fps_divisor = 4;  // 1=满帧, 2=隔帧, ... 0 视为 1
}
message FrameSubscribeAck {
  int32 error = 1;
  string error_msg = 2;
  uint32 width = 3;        // 实际生效值
  uint32 height = 4;
  uint32 fourcc = 5;
  uint32 pool_depth = 6;       // 服务端池深度
  uint32 max_outstanding = 7;  // 本连接可同时持有的帧数上限（见 2.4）
}
```

### 2.3 帧消息（线格式）

每帧一条 SEQPACKET 消息：ancillary data 携带 **恰好 1 个** dma-buf fd（`SCM_RIGHTS`），payload 为定长 **96 字节**头（C 布局，小端；数据面不用 protobuf，免序列化开销）：

```c
struct frame_plane {              // 12 bytes
    uint32_t offset;              // 相对 dma-buf 起始
    uint32_t stride;              // 行跨度（字节）
    uint32_t vstride;             // 垂直跨度（行数，含对齐补齐）
};
struct frame_hdr {                // 96 bytes, packed, little-endian
    uint32_t magic;               // 0x52434652 "RCFR"
    uint16_t ver;                 // =1
    uint16_t flags;               // bit0: 自上一条消息以来发生过丢帧
    uint64_t seq;                 // 单调递增，含被丢弃的帧（gap 可检测）
    uint64_t pts_us;              // 与 InferenceResult.pts_us 同一时钟源（见 3.2，门禁 G3）
    uint32_t width, height;       // 有效像素
    uint32_t fourcc;              // V4L2 fourcc，NV12='NV12'
    uint32_t buf_size;            // dma-buf 总有效长度
    uint8_t  chn_id;
    uint8_t  n_planes;            // NV12=2
    uint16_t reserved0;
    struct frame_plane plane[3];  // 36 bytes；NV12: plane[0]=Y, plane[1]=UV
    uint8_t  reserved[16];
};
_Static_assert(sizeof(struct frame_hdr) == 96, "frame_hdr ABI");
```

（v0.1 声称 64B 实际 60B，评审发现 1；v0.2 扩为 96B 并携带完整 plane descriptor——offset/stride/vstride 由服务端按 MPI 实际分配填写，客户端**禁止**自行按 w/h 推导布局，评审发现 3。）

**接收纪律**（SDK 内置，自实现协议者必须遵守）：
- `recvmsg` 一律带 `MSG_CMSG_CLOEXEC`；
- 收到 `MSG_CTRUNC`、fd 数 ≠ 1、magic/ver 不符、长度 ≠ 96 → **close 全部收到的 fd**、计协议错误、断开重连；
- **CPU 访问前后必须做 dma-buf cache 同步**：`DMA_BUF_IOCTL_SYNC(SYNC_START|READ)` / `(SYNC_END|READ)`。SDK 的 `frame.array` / `rc_ext_frame_map()` 内部完成，裸用 fd 者自理（评审发现 3）。

客户端处理完发回释放消息：8 字节 payload `uint64_t seq`，无 fd。

### 2.4 所有权、背压与生命周期（v0.2 重写）

**所有权模型（v0.3 按复审修正：单次持帧 + 订阅者计数）**：

- **MPI 层每帧只有一次 Get/Release**：服务端 `GetChnFrame` 后为该帧建 `frame_rec{mpi_frame, subscribers_left}`；出借给 k 个订阅者 = `subscribers_left = k`（不是 k 次 MPI 引用——MPI 无公开增引用接口，规格不假设它有）。`subscribers_left` 归零 → 恰好一次 `RK_MPI_VI_ReleaseChnFrame`。
  > **G2 已 PASS（2026-08-11 真机实证）**：持有一帧 fd 不 Release，强制池回绕 39 帧（13× 池深），被持有 fd 从不再现于轮转，CRC 前后一致——"fd 存活期间硬件不复用该块"成立，本模型（单次持帧 + 计数、无防御性 memcpy）实测支撑，无需服务端持帧兜底拷贝。
- 每连接维护 `generation`（断线递增）与 `outstanding` 集合（seq → frame_rec）。release 仅当 `seq ∈ outstanding` 时生效（移除 + `subscribers_left--`）；重复/未知/已释放 seq → **幂等忽略 + 计数**。
- 断线清理与 release 在**同一把锁/同一事件循环内串行化**：断线遍历该连接 outstanding，逐个 `subscribers_left--`，恰好一次；此后该 generation 的消息全部丢弃。
- **发送路径错误处理（原子预留 + 回滚，复审新发现 3）**：对每个订阅者按序执行——①检查两级配额（不过 → 跳过，不计入）②`dup(fd)`（失败 → 跳过 + 计数）③登记 outstanding、`subscribers_left++` ④`sendmsg(MSG_DONTWAIT)`。**④失败（EAGAIN 或任何错误）→ 立即回滚**：close dup 的 fd、撤销 outstanding 登记、`subscribers_left--`。非 EAGAIN 的持久性 socket 错误（EPIPE/ECONNRESET）→ 按断线清理处理。全部订阅者跳过时 `subscribers_left==0` → 立即 Release，帧不滞留。

**背压（评审发现 2）——三条铁律**：

1. **发送永不阻塞**：`sendmsg(MSG_DONTWAIT)`；`EAGAIN` → 本订阅者跳过该帧（置 flags.bit0 + seq gap），无每连接发送队列。
2. **全局预留按物理 buffer 记账（v0.3 明确）**：池深 N（初值 6，门禁 G6 定终值）。记账对象是**物理 buffer**：`held = 服务端持有的 frame_rec 数（subscribers_left>0）+ 在途采集帧（GetChnFrame 已返回、尚未决定出借或释放的，恒 ≤1）`。**采集前判定 `held ≤ N-3` 才调 GetChnFrame**——保证包含在途帧在内任何时刻至少 2 个 buffer 空闲给 VI。判定与登记在同一把锁内完成（原子预留）。
3. **两级配额**：每连接 `max_outstanding` 默认 2（按该连接 outstanding 集合大小算）；全局按第 2 条。任一超限即跳过该订阅者。

持续 5 秒 outstanding 满且零 release → 判定挂死，服务端断开（EBACKPRESSURE）并按断线清理回收。

**多订阅者**：上限 4（EBUSY）。同帧 `dup` fd 分发，每订阅者一次 `sendmsg`——**每帧成本 = 订阅者数次 sendmsg，不是一次**（评审发现 7 更正）。

**性能验收改为基准门禁**（不再作断言）：池深 {4,6,8} × 订阅者 {0..4} × 恶意负载（不读 socket / 不 release / kill -9）矩阵，通过标准见 §6 DoD 与门禁 G5/G6。

### 2.5 SDK 接口

**C ABI（v1 冻结对象）**：

```c
rc_ext_frame_t *h = rc_ext_frame_open(NULL /*默认配置*/, &err);
while (rc_ext_frame_next(h, &frame, 1000 /*ms*/) == 0) {
    void *y = rc_ext_frame_map(h, &frame);   // 内部做 DMA_BUF_IOCTL_SYNC
    // frame.hdr.plane[i].offset/stride/vstride, frame.hdr.pts_us ...
    rc_ext_frame_release(h, &frame);          // 内部 SYNC_END + 发 release + close fd
}
rc_ext_frame_close(h);
```

**Python（C 库 ctypes/cffi 薄封装，行为不独立定义）**：

```python
from recamera_ext import FrameSource
with FrameSource() as src:          # 默认 NPU 同款格式
    for frame in src:               # frame.array: np.ndarray（按 plane descriptor 构造，零拷贝 mmap）
        infer(frame.array)          # 离开迭代自动 release
```

验收标准维持"5 行拿到第一帧"，但正确性以 C ABI 测试为准。

---

## 3. M1 结果回注（`result-in.sock`）

### 3.1 proto 扩展

`common/vigil/protocol/inference.proto` 的 `InferenceResult` 现有字段 1/2/3 + oneof 10-14。**新增**（向后兼容）：

```proto
  string source_id = 4;    // 结果来源；"builtin" 保留给内建推理，外部使用被拒（§1.1）
  uint64 pts_us = 5;       // 对应帧的 pts（来自 frame_hdr.pts_us）；0 = 无帧关联
```

同步重新生成三份代码：`rc_notify/inference.pb-c.*`、`rc_infer/src/utils/inference.pb-c.*`、`notify-server/protobufs/inference_pb2.py`。内建推理路径补填 `source_id="builtin"` 与 `pts_us`。
按 §1.2 演进纪律：notify-server 对入站 payload **透传原始字节**，不 decode→re-encode。

### 3.2 时钟源约定

`pts_us` 统一用 VI 帧的 `stVFrame.u64PTS`（MPI 内部时基）。帧代理原样透传；回注结果带回同一值；OSD 侧按 pts 做**就近帧匹配**（容差 1 帧周期，超差仍渲染但计入 metrics `pts_mismatch`）。不引入第二种时钟。
**前置门禁 G3**：实测该 PTS 单调性与 VI/VENC 同源性；若不同源，退路是帧代理侧自打 `CLOCK_MONOTONIC` 时间戳并在头里同时携带两种（`reserved` 空间足够），OSD 按自打钟匹配。

### 3.3 服务端行为

新增 `recamera_ipc/common/rc_result_in/`：监听 `result-in.sock`，收 `<InferenceResult>` 消息后走**与内建推理完全相同的三路分发**（`video.c:648-657` 的逻辑抽出为 `rc_result_dispatch()`，内建路径与入站路径共同调用——设计文档 6.3"官方推理吃自己的狗粮"的第一步）：

1. `osd_manager_draw_infer()` — 叠加渲染。按 `source_id` 分配颜色（哈希到调色板）。
   > **G4 已 PASS 定案（2026-08-11）**：RGN 每通道 attach 上限 8、内建占 1。两种实现——**（推荐）单 canvas 合成**：所有 source（含 builtin）画进同一 overlay region、按 source_id 哈希调色，`max_sources` 不受 RGN 硬限，仅受视觉/带宽约束，capability 报 `max_sources=8`（软上限）；**（退路）独立 region**：每 source 一个 RGN，`max_sources=6`（8 − 内建 1 − 1 余量），超出的 source 只走录像+notify 两路 + ERATELIMIT 告警。执行时优先单 canvas；若 osd_manager 现结构改动过大则退独立 region。
2. vigil 录像队列 — 结果进录像。
3. `rc_notify_send_inference()` — 转发 WS/MQTT/HTTP/UART。

**限速（评审发现 7 更正）**：两级 token bucket——每连接 60 msg/s（burst 15）+ **全局 120 msg/s（burst 30）**；单条 payload ≤ 64 KB（EFORMAT）。超限丢弃 + 计数（metrics 可见），不断连接。
**并发**：≤ 4 连接。`source_id` 防冒充见 §1.1。

### 3.4 与存量 notify 入站的关系

`/var/tmp/notify`（0666 直写 notify-server）保持不动，定位**永久降为** "只要分发、不要叠加"的无特权 legacy 通道，并纳入全局 token bucket 记账。文档明确：**要上 OSD/录像 → `result-in.sock`；只要推送 → `/var/tmp/notify`**。

### 3.5 SDK

```python
from recamera_ext import ResultSink
sink = ResultSink(source_id="face-app")
sink.send_detections(pts_us=frame.pts_us,
                     boxes=[(x1,y1,x2,y2,score,"张三"), ...])
```

C 侧对应 `rc_ext_result_open/send/close`。SDK 内部完成 proto 组包，方案商不接触 protobuf。

> **坐标契约**：所有 box 坐标（检测/分类 ROI/分割 ROI/跟踪/关键点对象框）及关键点 point 的 x/y 均为**归一化 [0,1]**（相对画面宽高的比例），与 `docs/guide/README.md` §4.4 一致。OSD 先 clamp 到 [0,1] 再乘画面宽高——传像素值会被压成 1px 隐形框。分割 mask 是行主序原始字节（非坐标）。

---

## 4. M3 观测面（`probe.sock`）

### 4.1 服务端

`rc_infer` 流水线插 tap 点，发送/序列化由**独立低优先级 worker + 有界
队列**完成。无订阅时 tap 只有原子 gate；有订阅时当前实现会在推理线程持
队列 mutex，并把样本复制到最多 8 MiB 的预留 slab。队列满立即丢弃，不会
等待消费者或 socket，但这段 mutex+memcpy 并非“绝不阻塞”，必须纳入真机
延迟/带宽门禁；后续可改为预分配 try-lock/先丢后拷。

| stage_id | 位置 | 数据 |
|---|---|---|
| `preproc.out` | 预处理后、喂 NPU 前 | 实际输入张量（可还原为图，对齐设计文档 5.3"30 秒的事"） |
| `npu.raw` | 模型原始输出 | 各输出层张量 + shape/dtype/量化参数 |
| `postproc.out` | 后处理后 | 结构化结果（proto 同格式） |
| `metrics` | 周期上报 | 各级耗时 p50/p99、fps、丢帧计数、pts_mismatch、各 socket 的限速丢弃计数 |

### 4.2 协议

```proto
message ProbeSubscribe { repeated string stage_ids = 1; uint32 sample_every = 2; /* 每 N 帧采 1 帧，0=1 */ }
message ProbeData {
  string stage_id = 1; uint64 seq = 2; uint64 pts_us = 3;
  bytes payload = 4;             // 小数据内联（≤ 32 KB）
  TensorMeta meta = 5;           // shape/dtype/scale/zero_point/layout
}
```

大张量（preproc.out 一帧 NV12 ≈ 数百 KB）走 **memfd + SCM_RIGHTS**（复用帧代理的 fd 传递与释放代码路径）。
**probe 发送路径与帧代理同纪律（v0.3 补明）**：worker 内 `sendmsg(MSG_DONTWAIT)`，`EAGAIN` → 丢该条采样 + 计数（worker 永不因单个消费者卡住）；连续 EAGAIN 触发对数退避（自动提高实际 sample_every）并在 metrics 标注；退避到底仍无改善 → 断开（EBACKPRESSURE）。memfd 的 fd 发送失败同样立即 close 回滚。**不无限缓冲**。

### 4.3 前端呈现

**移出 M3 范围，随后续批次交付**（评审发现 7：v1 优先冻结数据口）。届时 Web 面板挂 `/extension/probe/`，走 M0 文档化的挂载约定，后端经 skt2ws 桥到 `probe.sock`——全部复用存量机制，不新增前端基建。

### 4.4 M4 external NPU 所有权（`inference-control.sock`）

需要 RKNN 的方案商进程不得以 CGI `POST 200` 或 pidfile 作为资源已释放的
证明。真正持有 builtin RKNN context 的 rkipc 提供 `inference-control@1`：

1. client 完成 Hello 后发送 `ACQUIRE{request_id,app_id,instance_id,timeout_ms,
   fallback_builtin}`；
2. rkipc 设置 external hold，驱动内建 NPU 线程 deinit；只有
   `handle==NULL && state==stopped && actual_fps==0` 时返回 GRANT；
3. GRANT 返回非零 `lease_id`、本次 rkipc `epoch` 和递增 `generation`；
4. external 完成 `load_rknn/init_runtime` 后发送 READY；STATUS、READY、
   SET_FALLBACK、RELEASE 都校验 live `lease_id+epoch`；
5. AF_UNIX 连接本身是租约。显式 RELEASE 或 HUP/EOF 都撤销 owner；已获
   GRANT 的连接按 `fallback_builtin` 恢复/保持关闭，未获 GRANT 的断线不改
   原先 builtin intent；
6. 同时只授予一个 external owner。fork child 只 close 自己继承的 fd 副本，
   不发送 RELEASE；所有 client/listener fd 均 CLOEXEC。

控制 v1 的 generation 尚未写入 frame/result/probe Hello，因此当前只对 NPU
所有权本身做 fencing；数据端点拒绝旧 generation（ESTALE）留给协议下一版。
HTTP `/api/v1/ext/capabilities` 可声明这项能力，但 acquire/release 不经 CGI。

---

## 5. M0 存量文档化（清单）

交付四篇短文档（放 `../guide/`，随固件发布）：

1. **音频接入**：`arecord -D ai_asr -r 16000 -c 4 -f S16_LE` 采 4 通道后软件取 ch0（Mic1）——**不要 `-c 1`**：route 层输出是 [Mic1, Mic2, Ref, Ref]，单声道下混会把扬声器参考混进人声（M0 文档实核修正）。说明 dsnoop 共享原理、与 rkipc 的 `ai_main` 互不干扰、无 VQE 的边界（AEC/NS 需自理；ch2/ch3 就是做 AEC 的参考信号）。
2. **结果推送接入**：`/var/tmp/notify` 的 `<le32 len><InferenceResult>` 帧格式 + proto 文件下载 + 四种 notifier 的配置；明确其"不上 OSD、无特权、受全局限速"的定位。
3. **前端扩展挂载**：`ext_<name>.conf` + `/extension/<name>/` 约定，以 acousticslabd 的 conf 为模板；JWT 会话如何复用（`auth_request` 机制说明）。
4. **rkipc RPC 现状**：`/var/tmp/rkipc` 是内部接口、不承诺稳定，方案商应走 entry.cgi HTTP API；为 M4 版本化做铺垫。

### 5.1 M4 控制面要点（本版只定方向）

- entry.cgi 路由表新增 `ext` 域：`/api/v1/ext/capabilities`（GET，返回与 HelloAck 一致的能力集）、`/api/v1/ext/subscriptions`（GET，当前帧/结果/观测连接的诊断视图，含 peercred 身份与限速计数）。
- 现有 18 域 API 挑方案商必需子集（model/video/osd/system）做文档化 + 冻结承诺，其余标注 internal。
- localhost JWT 直通（`rest_api.cpp:69-72`）保留至沙箱阶段，届时 app token 作为新增认证模式并行提供（§1.1）。

---

## 6. 实现改动点汇总（file 级）与 DoD

| 模块 | 改动 | 新增/修改 |
|---|---|---|
| `recamera_ipc/protobufs/inference.proto` + `common/vigil/protocol/inference.proto` | +`source_id`/`pts_us`；两份由 Vigil CMake 哈希门禁保持一致 | 修改（兼容） |
| `sdk/proto/ext_api.proto` + `recamera_ipc/protobufs/ext_api.proto` | Hello/Capability/Subscribe/Probe/InferenceLease 等；SDK/固件各持一份协议发布源 | **新增** |
| **`common/rc_ext_core/`** | **服务端核心库（§8.1）：传输/握手/身份/配额/所有权/fd 收发，三端点共用** | **新增（先行）** |
| `src/rv1126b_ipc/video/frame_export.{c,h}` | 帧代理端点（core 之上的薄层：VI 取帧 + plane 填充） | **新增** |
| `src/rv1126b_ipc/video/video.c` | 三路分发抽为 `rc_result_dispatch()`；新 VI chn1 初始化 | 修改（~几十行） |
| `common/rc_result_in/` | 入站结果端点（core 之上的薄层：proto 校验 + 分发调用） | **新增** |
| `common/rc_infer/`（流水线各级） | tap 点 + probe 端点（core 之上的薄层） | 修改 + **新增** |
| `recamera_web_backend/src/rest_api.cpp` + `ext_api.{h,cpp}` | ext 域 | 修改（2 行）+ **新增** |
| `sdk/librecamera_ext/` | C ABI（v1 冻结对象）+ Python 薄封装 | **新增**（发布形态属门禁 G7） |
| `common/rc_ext_core/rc_inference_control.{c,h}` + `common/rc_model/` | connection-lifetime NPU broker + builtin drain barrier | **新增 + 修改** |
| init 脚本 | `/run/recamera` 目录（`0750 root:root`）+ socket `0660`（root-only，无独立组） | 修改 |

**DoD（每里程碑验收）**：
- M1：外部脚本注入假检测框 → RTSP 流里看到框和标签 → 录像回放看到 → WS:8123 收到带 `source_id` 的结果；冒充 `"builtin"` 被拒；超速消息被丢且计数可查。
- M2：C 测试程序在板上实测 ≥ NPU 通道帧率、`top` 中 rkipc CPU 增量 < 3%；**恶意客户端三连测**（连接后不读 socket / 收帧不 release / kill -9）各跑 1 小时，相机线程零阻塞、VI 池零泄漏、内建推理帧率不掉；Python 5 行示例可跑。
- M3：故意喂错归一化的模型，通过 `preproc.out` 回显图 + `npu.raw` 张量在 10 分钟内定位问题（复刻设计文档 5.3 场景）；开满 probe 订阅时推理帧率下降 < 5%。

---

## 7. 实施前门禁（gate，v0.2 重构）

等级：**塌方级** = 结论为否则设计要改，**阻塞对应里程碑开工**；**核实级** = 与实现并行验证，**阻塞对应里程碑发布**（G5/G6 的验证本身依赖 M2 代码存在，逻辑上不可能是开工门禁——v0.3 修正 v0.2 的循环依赖表述，复审新发现 2）。

| # | 假设/风险 | 等级 | 验证方法 | 退出条件 | 阻塞 |
|---|---|---|---|---|---|
| ~~G1~~ ✅ | VI 同 pipe 多 chn：chn1 可独立配置分辨率/私有池，且 enable/disable 不影响 chn4 | 塌方级 | ~~板上双 chn 实测~~ | **PASS（2026-08-11）**：独立进程 chn1 取帧 45/45，rkipc 无系统性掉帧，跨进程可行 | ~~M2~~ 已解 |
| ~~G2~~ ✅ | dma-buf 出借期间（MPI frame 未归还）硬件不复用该 buffer | 塌方级 | ~~逐帧 CRC 对比~~ | **PASS（2026-08-11）**：13× 池深回绕，持有 fd 内容不变，单次持帧模型成立 | ~~M2~~ 已解 |
| ~~G3~~ ✅ | `u64PTS` 单调且 VI/VENC 同源 | 塌方级 | ~~双路 PTS 对比~~ | **PASS（2026-08-11）**：u64PTS = CLOCK_MONOTONIC 微秒，40 帧单调、漂移 <0.5ms、全链路同源。主方案成立，自打钟退路无需启用 | ~~M1~~ 已解 |
| ~~G4~~ ✅ | RGN 通道数支持 ≥ 2 个外部 source 同时叠加 | 塌方级 | ~~查上限+实测~~ | **PASS（2026-08-11）**：RGN handle 每进程私有（池 128），每通道 attach 上限 8、内建占 1。`max_sources` 见 §3.3 | ~~M1~~ 已解 |
| G5 | 非阻塞发送路径在恶意负载下相机线程零阻塞 | 核实级 | §6 M2 恶意三连测 | 三项各 1h 通过 | M2 发布 |
| G6 | 池深与全局预留的最坏情况 | 核实级 | 池深 {4,6,8} × 订阅者 {0..4} 基准 | 选定 N 与 N-2 预留验证通过 | M2 发布 |
| G7 | SDK 发布形态（独立仓库 vs 随固件 .so） | 核实级 | 与打包分发方案对齐后决策 | C ABI 版本对齐策略成文 | SDK 发布 |

> **门禁验证载体（2026-08-10 实证）**：原厂 V1.0.10 固件即可获 root（`admin` 可写 `/etc`；`telnetd -F`/`adbd` 以 root 运行；`RkLunch.sh` 的 `/userdata/config/system/etc/custom_shadow` 开机注入 shadow——userdata 可写、root 每启重放）。`/userdata` 为 `ext4 rw` 无 `noexec`，可放置并执行交叉编译的测试二进制。**因此 G1-G4 无需先刷自编固件，在现有设备上即可验**——把 M1/M2 的塌方级门禁从"依赖编译闭环+刷机"解耦，可与编译线并行推进。刷自编固件仅在 M1 代码改动需要上板运行时才必要。

---

## 8. 架构与扩展模型（v0.4 增补）

> §1-§7 最初定义三条数据 socket；同一核心现已长出第四条
> `inference-control`，本章定义**怎么增加端点而不复制一套实现**，以及兼容性
> 如何被工程化保证而不是靠自觉。

### 8.1 组件分层：一个核心库，N 个薄端点

```
rkipc 进程内
┌─────────────────────────────────────────────────────┐
│  frame_export      rc_result_in      probe_export   │   ← 端点层（每个 ~200-400 行）
│  (VI取帧+plane)    (proto校验+分发)   (tap采样+meta)  │      只含媒体特有逻辑
├─────────────────────────────────────────────────────┤
│              common/rc_ext_core/  （核心库）          │   ← 传输层（实现一次）
│  · SEQPACKET 服务端骨架（accept/事件循环/锁模型）      │
│  · Hello/HelloAck 握手 + 版本区间协商 + Capability    │
│  · SO_PEERCRED + apps.d 身份注册表                   │
│  · 两级 token bucket / 按物理资源记账的配额框架        │
│  · fd 出借所有权状态机（generation/outstanding/回滚）  │
│  · MSG_DONTWAIT 发送 + 统一错误路径 + metrics 计数    │
└─────────────────────────────────────────────────────┘
客户端（镜像分层）
┌─────────────────────────────────────────────────────┐
│  Python binding（ctypes 薄封装，无独立行为）           │
│  未来：Node / Rust binding（同一 C ABI 之上）         │
├─────────────────────────────────────────────────────┤
│  librecamera_ext.so  （C ABI，v1 冻结对象）           │
│  · rc_ext_frame_* / rc_ext_result_* / rc_ext_probe_* │
│  · 连接/握手/重连/fd 接收/cache sync 全部内置          │
└─────────────────────────────────────────────────────┘
```

**规则**：端点层禁止直接碰 socket/fd/配额——全部经 core。收益：① 所有权/背压这类最难写对的代码只存在一份，评审 1/2/3 类 bug 只修一处；② 新媒体类型（VQE-PCM、file-source 注入）= 新端点薄层 + 新 Capability，**核心零改动**；③ core 可单独做单元测试（mock 端点），不用整机跑。
**开发顺序因此调整**：`rc_ext_core` 先行（M1 开工即写，M1 的 result-in 是它的第一个消费者，M2 帧代理是第二个——所有权状态机在 M1 阶段就被真实代码路径覆盖，而不是等 M2 才首次上场）。

### 8.2 扩展五规则（对外承诺，写进方案商文档）

1. **加法优先**：新能力 = 新 socket 路径 + 新 Capability 条目 + 新 proto 消息。现有端点的线格式、路径、语义不动。
2. **数量演进走 limits**：并发数、速率、池深这类配额变化只改 `Capability.limits` 数值，客户端必须按握手返回值自适应，不得硬编码。
3. **结构演进走保留位**：`frame_hdr` 的 `ver` + `reserved[16]`（如 G3 失败需加第二时间戳，占 reserved 8 字节 + flags 一位标识，`ver` 不变）；重排/删字段才升 `ver`，且旧 `ver` 服务端至少再支持两个固件版本。
4. **任务类型演进走 oneof 追加**：`InferenceResult` 新任务 = 新 oneof 分支（tag 15+），旧读者跳过未知分支；与 rc_infer 后处理注册表的字符串名一一对应，注册表加算法不需要动 proto。
5. **只增不减**：`/run/recamera/` 存在期间，v1 baseline 能力
   （`frame@1`/`result@1`/`probe@1`/`inference-control@1`）与其线格式永不
   移除。废弃流程：`Capability.limits["deprecated"]=1` 标记 ≥ 2 个固件版本
   → 从 capabilities 列表消失（但端点仍应答 EVERSION 类错误而非消失式断连）。

### 8.3 兼容性工程（CI 化，不靠自觉）

- **golden corpus**：每个发布版的 proto 消息样本（各消息类型 × 边界值）入库；CI 用当前代码解析全部历史 corpus + 用历史生成器解析当前消息（双向）。
- **跨版本矩阵**：v1 SDK ↔ 当前服务端、当前 SDK ↔ v1 服务端两条链路的握手 + 一次完整业务往返，进固件 CI。
- **ABI 检查**：`librecamera_ext.so` 用 libabigail（`abidiff`）对上一发布版做符号级对比，破坏性变更 = CI 红灯；soname 规则 `librecamera_ext.so.1`，semver。
- 三条全部作为 M4 收口时的 CI 交付物；M1/M2 期间先手工执行并留档。

### 8.4 性能预算集中表（散落各节的数字收口，DoD 实测校准）

| 维度 | 预算 | 校准方式 |
|---|---|---|
| rkipc CPU 增量（4 订阅者满载） | < 3% | M2 DoD `top` 实测 |
| 帧路径新增延迟 | < 1 帧周期 | pts 对比实测 |
| 帧路径内存 | 池深 N × buffer（N∈{4,6,8}，G6 定） | 基准矩阵 |
| result-in 分发延迟（socket→OSD 调用） | < 10ms p99 | probe metrics |
| probe 全开时推理帧率损失 | < 5% | M3 DoD |
| 无订阅者时的常驻开销 | 0（chn disable + tap 原子读） | 空载对比 |

---

## 附：v0.1 → v0.2 修订记录（对应评审 7 条发现）

| # | 评审发现 | v0.2 处置 |
|---|---|---|
| 1 | `frame_hdr` 64B 实为 60B；release 可伪造/重放；无所有权状态机；断线清理竞态；fd 异常路径未定义 | §2.3 重定义 96B 头 + `_Static_assert`；§2.4 connection generation + outstanding 集合 + 幂等 release + 串行化清理；接收纪律（MSG_CMSG_CLOEXEC/MSG_CTRUNC/多 fd 全 close） |
| 2 | 阻塞 sendmsg 可死锁相机线程；单客户端记账挡不住多客户端占满池 | §2.4 三条铁律：MSG_DONTWAIT + EAGAIN 丢帧；全局 outstanding ≤ N-2 预留；两级（全局+连接）按 buffer 记账 |
| 3 | NV12 无 plane/vstride/offset 定义；无 dma-buf cache 同步；Python 零拷贝承诺无正确性基础 | §2.3 plane descriptor 进头部（客户端禁止自行推导）；DMA_BUF_IOCTL_SYNC 纪律；Python 降为 C ABI 薄封装 |
| 4 | `min()` 不是版本协商；capability 无版本无 limits；无 baseline 承诺；中转 re-encode 丢字段 | §1.2 区间交集协商 + 结构化 Capability{name,version,limits} + v1 baseline 不可移除 + 透传原始字节纪律 + round-trip CI |
| 5 | 共享组权限过粗；auth 全推迟；source_id 可冒充；0666 旁路仍未鉴权 | §1.1 v1 即用 SO_PEERCRED；`"builtin"` 保留字拒绝；auth_mode 协商（token 后加不断老客户端）；0666 端点永久降为受限 legacy |
| 6 | 开放问题实为 5 项且未分级；漏 dma-buf 所有权与全局池最坏情况 | §7 重构为 7 项门禁表（等级/验证方法/退出条件/阻塞关系），补 G2/G6 |
| 7 | "每帧一次 sendmsg"错误（应为订阅者数次）；60/s 是每连接、无全局与 payload 上限；tap"零开销"不实；probe 慢消费者可阻塞；性能数字无依据 | §2.4 更正成本模型并改为基准门禁；§3.3 两级 token bucket + 64KB payload 上限；§4.1 独立低优先级 worker + 有界队列 + 退避断开；§4.3 Web 面板移出 M3；性能断言全部改为 DoD 实测 |

### v0.2 → v0.3 修订记录（对应复审 4 条 PARTIAL + 3 条新发现）

| 复审项 | v0.3 处置 |
|---|---|
| 1-PARTIAL：dup 失败、非 EAGAIN 错误、发送失败后 fd/outstanding/引用回滚未定义 | §2.4 发送路径四步原子序列 + 失败回滚（close fd / 撤销登记 / 计数减）；EPIPE/ECONNRESET 按断线清理 |
| 2-PARTIAL：记账对象（物理 buffer vs 订阅者引用）不明；在途采集帧可击穿 2 个空闲的保证 | §2.4 铁律 2 重写：按物理 buffer 记账，`held` 含在途帧，**采集前判定 `held ≤ N-3`**，判定与登记同锁原子 |
| 5-PARTIAL：注册 app 的 source_id 与 uid 无绑定机制 | §1.1 增 `/run/recamera/apps.d/<name>.conf`（uid 绑定注册表，root 只写；v1 手写、打包落地后安装器生成）。0666 legacy 端点匿名注入维持"接受的风险"定位不变（降权 + 限速 + 永不增强） |
| 7-PARTIAL：probe 发送路径未明确非阻塞 | §4.2 补明：worker 内 MSG_DONTWAIT + EAGAIN 丢采样 + 退避 + 断开；memfd 发送失败回滚 |
| 新-1：同帧 k 个 MPI 引用无机制支撑 | §2.4 所有权模型重写：MPI 每帧单次 Get/Release，订阅者以 `subscribers_left` 计数，归零后恰好一次释放——不假设 MPI 有增引用接口 |
| 新-2：G5/G6 作为 M2 开工门禁构成循环依赖 | §7 总则改为：塌方级阻开工、核实级阻发布 |
| 新-3：全局额度原子预留时点与回滚未定义 | 并入 §2.4 四步序列与同锁判定（同 1-PARTIAL 处置） |

---

# M5 显示扩展:DSI 屏自定义显示(方案 A,2026-08-11 纳入)

> 目标:**方案商不拿固件源码,自定义 MIPI DSI 屏上显示的内容**(相机画面 + 任意 GUI/叠加/交互)。
> 选定方案 A(整屏出租 + 帧代理自绘),与整个扩展 API 哲学一致:进程边界、方案商跑自己的进程、不 fork 固件。

## 现状(调研结论)
- DSI 显示通路开启(`rkipc.ini [video.source] enable_vo=1`),默认 VO 直显 VI 裸相机画面(1920×1080),不带 OSD。
- OSD RGN 全部 attach 到 VENC(RTSP/录像),**不上屏**。
- LVGL/DRM/VO/触摸(gt911)移植层全在 `recamera_ipc/common/lvgl/`,但产品 app 未编译进去。参考实现:`testdemo/yoloworld_demo`(检测+LVGL UI 同屏)、`cvr`(多页 UI+触摸)。
- `common/lvgl/drivers/disp.c` 有两条上屏路径(编译期选):`DRAW_UI_BY_VO`(LVGL 画到 VO 图层,与相机分层叠加)/ DRM 独占 `/dev/dri/card0`(纯 GUI)。

## 方案 A:整屏出租 + 帧代理自绘
- **官方(Seeed)提供**:① 可配置关闭 rkipc 的 VO(`enable_vo=0`)让出 DSI;② 确保 `/dev/dri/card0`(DRM)对扩展进程可访问(权限模型与帧代理身份一致);③ 打包 LVGL 库 + 移植层给方案商(可选,方案商也可自带 GUI 栈)。
- **方案商**:用 **M2 帧代理**拿相机帧 → 自己用 LVGL/DRM 把"相机画面 + 自定义叠加 + GUI 控件"合成画到 DSI 屏;gt911 触摸经 evdev 驱动自己的 UI。
- **收益**:方案商完全控制屏显内容(不受官方预设控件限制),不碰固件源码;复用已验证的 M2 帧代理,官方侧只做"让出屏 + 开放 DRM"两件轻事。检测框上屏也顺带解决(方案商 LVGL 自绘,不依赖 RV1126B VO 是否支持 RGN 叠加)。

## 依赖
- **M2 帧代理**(2a✅ / 2b 进行中)——方案 A 的画面来源,做扎实后 M5 即水到渠成。

## 待实测(需接屏 RPi 7寸 DSI)
1. RV1126B VOP 能否一层给 rkipc(相机)、一层给方案商(GUI)——决定"整屏出租"还是"分层共存(方案 B)"
2. `/dev/dri/card0` 权限:扩展进程能否访问(与帧代理 root 身份一致?)
3. rkipc `enable_vo=0` 让出 DSI 后,DRM 资源能否被外部进程接管(VOP/CRTC 归属)
4. gt911 触摸事件从 `/dev/input/event*` 获取 → LVGL indev 驱动 UI

## DoD(M5)
- 方案商进程(经 M2 拿帧 + LVGL/DRM)在 DSI 屏显示:相机画面 + 一个自定义叠加(如检测框/文字/按钮),不改固件源码
- 触摸点击自定义控件有响应
- rkipc 让出 VO 后其余功能(RTSP/推理/结果)不回归

## M5 附:DSI 屏兼容性与换屏(2026-08-11 调研)

**核心区分:显示内容 vs 换屏硬件,对源码依赖不同。**
- **显示内容**(画面/GUI/叠加):方案 A(帧代理+LVGL/DRM),**方案商无需源码**。
- **换屏硬件**(换不同屏):**当前需固件源码**——panel 配置在 `rv1126b-recamera2-disp.dtsi`,编译期 `#include` 进板级 dts 编成 dtb,换屏要改 DTS + 重编 dtb + 重烧,方案商做不了。

### 支持的屏范围(不止 7 寸)
| 维度 | 结论 | 依据 |
|---|---|---|
| 显示上限 | **1920×1080**(卡在 VOP,非 DSI) | `rockchip_vop_reg.c:2072` rv1126b_vop.max_output |
| DSI PHY | 4 lane × 1.5 Gbps = 6 Gbps(远富余) | `dw-mipi-dsi-rockchip.c:1933-1934` |
| 当前默认屏 | 720×1280 ILI9881C(旋转 90°横显 1280×720)+ GT911 触摸 | `panel-ilitek-ili9881c.c:2428` |
| 现成 panel 驱动 | kernel 60+ 个 DSI 驱动(ILI9881C/ST7701/NT35510…) | `drivers/gpu/drm/panel/` |
| 通用屏 | `simple-panel-dsi`——无特殊初始化命令的屏,DTS 配时序+lane 即可,免写 C 驱动 | `panel-simple.c:5064` |
| 触摸换 IC | GT911/FT5x06/ILITEK 等驱动现成,换 IC 纯 DTS | `drivers/input/touchscreen/` |

**结论:1080p 以内常见 5-7 寸 DSI 屏(720×1280 / 1280×720 / 800×480 / 1024×600 等)基本都能上**,主要工作是配 DTS(时序 + 背光电源接线 + 触摸)。

### 换屏纯 DTS 改动清单(现成驱动时,不改 C 代码)
1. `&dsi/panel@0` 的 `compatible`(现成驱动 or `simple-panel-dsi`+`display-timings`+lane)
2. 背光/电源:当前走 RPi 显示 MCU(i2c5 0x45),换非 RPi 屏改成 `pwm-backlight`+GPIO 复位
3. 触摸节点 compatible/地址/GPIO(换 IC 时)
4. 重编板级 dtb 重烧(disp.dtsi 编译期 include,不涉 dtbo)
仅当屏是内核没有的全新型号且需初始化命令序列时才写 panel 驱动。

### 待核实
- `1.5 Gbps/lane` 是驱动侧限制,芯片 DPHY 物理上限以 RV1126B TRM 为准(docs 无 DSI 章节 PDF)

### 若要"方案商不拿源码也能换屏"(可选,Seeed 侧工作)
把 panel 从编译期 include 改成**运行时可选 dtbo**(RK dtbo overlay 机制,baseboard/extboard 已在用):预置常见屏 dtbo + 启动按 `/userdata` 配置选加载。属 Seeed 预先工作,非方案商自助。

---

# M6 生态集成:接现成框架 + 处理后接回显示(完整数据闭环,2026-08-11 纳入)

> 目标:方案商用现成框架(GStreamer / OpenCV / FFmpeg)完成 **拿帧 → 处理 → 接回显示/输出** 的完整闭环,不从 socket 撸起。**双向都要集成**:入(帧进框架)+ 出(处理结果/画面接回 reCamera 的显示与输出)。

## 双向桥接

### 入:帧进框架(基于 M2 帧代理 dma-buf)
| 适配 | 零拷贝 | 通吃 | 谁做 |
|---|---|---|---|
| v4l2loopback 通用层(`/dev/videoX`,gst v4l2src / ffmpeg -f v4l2 / cv2.VideoCapture 全标准接口) | 可能牺牲(简单实现走一次拷贝) | ✅ 一次补全通吃 | Seeed |
| GStreamer source element `recameraframesrc`(dmabuf) | ✅ | 单框架 | Seeed |
| FFmpeg DRM_PRIME helper(dma-buf→AVFrame) | ✅ | 单框架 | Seeed/示例 |
| OpenCV | — | — | **基本不用补**,SDK 的 `frame.array`/`bgr` 直喂 cv2 |

### 出:处理结果/画面接回(三个出口)
| 出口 | 用途 | 现状 |
|---|---|---|
| ① 结果(框/标签/事件)回注 → 官方 OSD/RTSP/录像 | 只叠加结果,原画面不变 | **✅ M1 result-in 已做** |
| ② 处理后**整帧画面**回注 → VENC 编码 → RTSP/录像 | 方案商处理后的完整帧(滤镜/融合/GUI) | ❌ 需新"帧回注→VENC"入口(M2 反向) |
| ③ 直接上屏 → DSI | 处理后画面显示在屏 | M5 方案 A(LVGL/DRM)或 gst `kmssink` |

## 目标形态(方案商一条 pipeline 走完闭环)
```
recameraframesrc ! <方案商处理:推理/滤镜> ! tee ! recamerah265sink   (→ RTSP/go2rtc)
                                               ! kmssink              (→ DSI 屏)
        检测结果 ──────────────────────────────────────────────────→ result-in (→ 官方 OSD)
```

## 现状与待补
- ✅ 入口帧代理(M2)、结果注入(M1)已做
- ❌ 待做:整帧回注编码入口(②)、GStreamer src/sink element、v4l2loopback 通用层、ffmpeg helper
- 设备已确认:GStreamer 运行时 + 插件在(buildroot `GSTREAMER1=y`)、RK 硬件加速库在(`librockchip_mpp.so`/`librga.so`)

## 已核实(2026-08-15 真机复测,固件 kernel 6.1.157)

### 1. GStreamer 插件清单 —— 无任何 RK 硬件编解码 plugin,无 kmssink
`gst-inspect-1.0 | grep -iE 'mpp|rockchip|rga'` 唯一命中是 `typefindfunctions: audio/x-musepack: mpc, mpp, mp+`(字符串巧合,与 Rockchip MPP 无关)。逐个查:

| element | 结果 |
|---|---|
| `mpph265enc` / `mpph264enc` / `mppvideodec` | ❌ `No such element or plugin` |
| `appsrc` / `appsink` | ✅ 在(rank none) |
| `v4l2src` | ✅ 在(rank primary 256) |
| `kmssink` | ❌ `No such element or plugin` |

> **可补,已实测**(2026-08-15):这三个 RK element 缺的是**插件层**而非芯片能力 —— `librockchip_mpp.so`/`librga.so`/`librockit.so` 与 `/dev/mpp_service` 都在设备上。从 `gstreamer-rockchip` 零源码修改交叉编译出 `libgstrockchipmpp.so` 后,`mppvideodec` 硬件 H.265 解码端到端跑通(Python `gi` 与 `cv2.CAP_GSTREAMER` 两条路)。`h265parse` 取 buildroot 已预编但未打进镜像的 `libgstvideoparsersbad.so`,不用编。做法、前提与坑见 [`../guide/hw-codec-gstreamer.md`](../guide/hw-codec-gstreamer.md)。**编码器 `mpph264enc`/`mpph265enc` 虽同时编出但未测**,且会与 rkipc 抢 VEPU。上表描述的是**出厂镜像**状态,不受影响。

`kmssink` 缺失有第二重原因:**设备上根本没有 `/dev/dri/`**(`ls /dev/dri/*` → No such file or directory),即 DRM/KMS 用户态节点未暴露,DSI 上屏走不了标准 DRM sink。
`v4l2src` 虽在,但设备上 28 个 `/dev/video*` 全部是 ISP/CIF/VPSS 采集与统计节点(`rkisp_mainpath`、`rkvpss_scale0..5`、`stream_cif_mipi_id0..3` 等),**没有一个是通用 UVC 或 M2M 编解码节点**,且权限 `root:video 0660`。

### 2. ffmpeg 4.4.4 —— 未编 rkmpp;v4l2m2m wrapper 编进来了但无设备可绑
`ffmpeg -version` 的 configure 串里**没有 `--enable-rkmpp`**,有 `--enable-libdrm`、`--enable-libv4l2`;`-hwaccels` 只列 `drm`。
`-encoders` / `-decoders` **确实列出** `h264_v4l2m2m` / `hevc_v4l2m2m` / `vp8_v4l2m2m` 等 —— 但这只是 FFmpeg 内建的 V4L2 M2M wrapper 被编进来,**内核侧没有对应的 M2M 编解码设备节点**:RV1126B 的硬件编码器挂在 `/dev/mpp_service`(`crw------- root:root 243,0`,私有 MPP 接口,非标准 V4L2),`/sys/class/video4linux/*/name` 里无任何 codec 节点。
结论订正:**"`h264_v4l2m2m` 不可用"成立,但原因不是"没编进来",而是"编了却没有设备节点可绑"**。方案商看 `-encoders` 输出会误以为有硬编,必须说明这一点。

### 3. v4l2loopback —— 内核未提供,且设备侧无法自行补
- `find / -iname '*v4l2loopback*'` → 空;`/oem/usr/ko/` 下 40+ 个 `.ko` 无一相关(`ls /oem/usr/ko/ | grep -i loop` 空)
- 设备**没有 `/lib/modules/` 目录**、没有 `modinfo`/`depmod`,`modprobe` 二进制虽在(`/sbin/modprobe`)但无模块数据库可查;`.ko` 全部集中在 `/oem/usr/ko/` 靠启动脚本 `insmod` 显式加载
- 没有内核头文件/build 树,**设备上无法编译 out-of-tree 模块**

**结论:v4l2loopback 路线在当前固件上不可用。** 要启用需 Seeed 侧重编内核(打开 `CONFIG_V4L2_LOOPBACK` 或随固件附带 `.ko`)并整包 OTA;方案商自己解决不了。
(未执行 `modprobe` —— 模块文件不存在,无对象可加载。dmesg 前后无新增异常。)

### 4. 整帧回注 VENC 能否复用 go2rtc/RTSP 编码入口 —— 未核实
本轮未涉及。仍需判断走 ①rkipc 官方 VENC 新入口 还是 ②新开 MPI VENC 通道。

## DoD(M6,分框架)
- OpenCV:cv2 拿帧处理 + 结果经 result-in 上 OSD(端到端,已具备零件)
- GStreamer:`recameraframesrc ! ... ! recamerah265sink` 一条 pipeline 拿帧→处理→推回 RTSP
- (可选)v4l2loopback:`gst v4l2src` / `ffmpeg -f v4l2` 标准接口读到帧

## M6 实测结论(2026-08-11 真机;2026-08-15 复测核对)
- **GStreamer 1.22.6**:`appsrc`+`GstDmaBufAllocator`+`GstVideoMeta` 齐全(C 和 Python gi)。**dma-buf 零拷贝拿帧实测 PASS**:`appsrc!videoconvert!pngenc!filesink` 出 1280×720 正确 PNG;`!fakesink` 吞吐 29.8fps 满帧。示例 `../guide/gstreamer-integration.md`。**但无 RK 硬件编码 plugin(mpph264enc/rockchipmpp)、无 kmssink**。
- **FFmpeg 4.4.4**:有 `drm_prime`(DRM_PRIME import 编入)。rawvideo→mjpeg 实测 PASS。**但未编 rkmpp**,`h264_v4l2m2m` 不可用(RK 编码器走 `/dev/mpp_service` MPP,非标准 V4L2 M2M)。示例 `../guide/ffmpeg-integration.md`。
- **含义**:方案商**拿帧+软件处理**用标准 gst/ffmpeg 零改源码即可(实测通)。**硬件编码回推**(闭环的"出")标准框架里缺——要走 ①rkipc 官方 VENC(整帧回注入口,待补)②或方案商自带 rkmpp-enabled ffmpeg / gstreamer1-rockchip plugin。OpenCV 直接 `frame.to_bgr()`→cv2 无需桥接。

### 2026-08-15 复测核对结果:结论整体成立,两处需订正
1. **成立**:GStreamer 1.22.6、无 RK 硬编 plugin、无 kmssink、ffmpeg 4.4.4 未编 rkmpp、`drm` hwaccel 在。
   - 补充(同日另一轮):"无 RK plugin"是**出厂镜像**的事实,方案商可自行补齐 —— `libgstrockchipmpp.so` 交叉编译后硬件 H.265 **解码**已实测端到端可用,详见 [`../guide/hw-codec-gstreamer.md`](../guide/hw-codec-gstreamer.md)。**编码侧未测**,故上文"硬件编码回推标准框架里缺"的结论**暂不改动**。
2. **订正 A**:"`h264_v4l2m2m` 不可用"的**表述容易误读**。`ffmpeg -encoders` **会列出** `h264_v4l2m2m`/`hevc_v4l2m2m`,wrapper 是编进来的;不可用的原因是内核侧无 V4L2 M2M codec 设备节点(28 个 `/dev/video*` 全是 ISP/CIF/VPSS 采集节点)。方案商照 `-encoders` 输出选编码器会踩空。
3. **订正 B**:`drm` 虽列在 `-hwaccels`、`--enable-libdrm` 也在 configure 串里,但设备上**没有 `/dev/dri/`**,DRM 相关路径(含 kmssink 上屏)在当前固件上无节点可用,不只是"缺 plugin"。

---

# 架构决断（2026-08-11，负责人已拍板）

## R6 帧代理进程归属：v1 留 rkipc 进程内，P1 沙箱阶段再拆 frame-exportd
- **决定**：v1 不拆，帧代理端点留在 rkipc 进程内（现状）。
- **理由**：frame_export 对 rkipc 内部零耦合（不碰全局/不读 rk_param/自建私有池，仅搭 rkipc 已 SYS_Init 的便车），G1 已证可独立进程运行——**推迟拆分不产生返工**。现在拆要吃"独立进程 SYS_Init 触碰内核拒绝的全局 VPSS 配置"（G1 标注脆弱）+ PTS 同源重验的新风险，换一个 v1 阶段无人依赖的隔离能力，不划算。rc_result_in/rc_probe 因 tap/分发 rkipc 内部数据，本就必须留进程内。
- **P1 拆分触发**：沙箱开工时以 frame-exportd 为第一个沙箱化独立宿主。前置门禁：独立进程 ≥1h 运行 dmesg 无 VPSS 异常 + rkipc 帧率不掉。

## R7 身份模型：v1 诚实砍"多 app 强区分" + exe 路径低成本兜底，token 留 P1
- **决定**：v1 能力清单明示"外部源 OSD 归属/配色为尽力而为，多 root 扩展并存时可能合并归属"；实现上补 exe 路径兜底——`ext_identity.c` 在 uid 命中失败（uid:0）时读 `/proc/<pid>/exe`（pid 已由 SO_PEERCRED captured）比对 `apps.d/<name>.conf` 的 `exe=<path>` 字段。约 20-40 行，不动 ABI、不动三端点调用方。
- **理由**：现阶段服务少数可信方案商，威胁模型防手滑不防恶意；任何 v1 手段都防不住恶意 root（能直接写 socket/改 cmdline）。exe 路径比裸 pid 稳（pid 会回收复用）。
- **订正**：限速记账是 **per-connection + 进程全局**（`ext_ratelimit.h`），**不受全 root 影响**；R7 真正受影响的只有 source_id 归属与 OSD 配色。
- **P1 演进**：`HelloAck.auth_mode` 已是协商字段，token 将来作为新增模式并行，peercred/exe 兜底保留为诊断或退役，不构成技术债。

---

# 已解决(v1.1.0):分类通道支持可选 box(应用方 2026-08-12 反馈)

> **状态:已实现(v1.1.0)。** `rc_ext_class_t` 增 `has_box` + `x1/y1/x2/y2`;`send_classification` 支持可选第 4 元素 box `(score, class_id, label[, (x1,y1,x2,y2)])`;OSD 渲染 classification 时若有 box 画在框位置。box 坐标同为归一化 [0,1]。`InferenceClassificationEntry` 加 `InferenceBox box=4`(proto3 optional,向后兼容,老消费端忽略;`has_box=0` 保持原 box-less 行为)。以下为原始反馈脉络,保留备查。

**缺陷(原始反馈)**:`InferenceClassificationEntry { score, class_id, class_name }` 曾**无 box 字段**(而 detection/segmentation/tracking 都有 InferenceBox)。face-analysis 多张脸的属性(如 "Male,30-39,Happy")注入时丢位置信息——不知道属性属于画面里哪张脸。

**根因**:proto 设计限制,classification entry 本就无位置概念。

**修法(已落地)**:
- 短期绕过(不改 proto):face-analysis 这类"检测+属性"本质用 `send_detections`,属性拼进 class_name label → 天然有框有属性。
- 正式修(v1.1.0 已实现):`InferenceClassificationEntry` 加 `InferenceBox box=4`(proto3 optional,向后兼容,老消费端忽略);SDK `send_classification` 加可选 box 参数;OSD 渲染 classification 时若有 box 画在框位置。支持"按区域分类"场景(人脸属性、区域识别:某区域=货架/通道)。
- 属低成本兼容改动。改动面:vigil/inference.proto + pb-c 三份重生成 + SDK + osd_infer 渲染分支。

# frame.sock 交互时序（rkipc ↔ librecamera_ext ↔ Python）

> 本文把帧代理通路的完整交互画成 Mermaid 时序图，作为
> [`architecture.md`](./architecture.md) §4.1（帧代理数据流）与 `spec.md` §2
> 的配套图示。所有步骤的代码出处标注在各节末尾；行号以当前工作树为准。

参与方（从左到右）：

| 参与方 | 实体 | 职责 |
|---|---|---|
| 业务脚本 | `app.py` / `examples/*` | `for frame in src` 消费帧，跑算法 |
| ctypes 层 | `sdk/python/recamera_ext/__init__.py` | `FrameSource` / `Frame`，借用迭代器 |
| native 库 | `sdk/src/frame_recv.c` → `librecamera_ext.so.1` | connect/握手/recvmsg/mmap/release |
| 服务端 | `recamera_ipc` `frame_export.c` 线程 | poll + 采集 + fan-out + 所有权状态机 |
| VI | pipe0/chn1（硬件 + Rockit MPI） | 帧写入私有 DMABUF 池（池深 6） |

跨进程传输的只有两条控制消息：**96 字节帧头 + 1 个 dma-buf fd**（SCM_RIGHTS）
和 **8 字节 release seq**。像素永远不进 socket——fd 两端的进程持有各自的
fd 数字，但指向同一个 `struct file` / 同一块物理内存（详见
`architecture.md` §5.5）。

---

## 1. 连接建立：握手 + 订阅（一次性）

```mermaid
sequenceDiagram
    autonumber
    participant App as 业务脚本 (app.py)
    participant Py as recamera_ext (ctypes)
    participant Lib as librecamera_ext.so (frame_recv.c)
    participant Srv as rkipc frame_export (frame_export.c)
    participant VI as VI pipe0/chn1 (硬件+MPI)

    App->>Py: FrameSource()
    Py->>Lib: dlopen("librecamera_ext.so.1")
    Py->>Lib: rc_ext_frame_open(cfg)
    Lib->>Srv: connect(/run/recamera/frame.sock)
    Srv->>Srv: accept() + getsockopt(SO_PEERCRED)
    Note over Srv: 按 peercred 钉 source_id；<br/>保留字 "builtin" 直接拒绝 (EAUTH)
    Lib->>Srv: Hello{ver_min, ver_max, client_name}
    Note over Srv: rc_ext_handshake_serve():<br/>版本区间取交集最大值
    Srv-->>Lib: HelloAck{frame@1, limits: max_subscribers=4,<br/>pool_depth=6, max_outstanding=2}
    Note over Lib: 客户端按握手返回的 limits 自适应，<br/>不得硬编码
    Lib->>Srv: FrameSubscribe{fps_divisor=1}
    Note over Srv: fps_divisor 非零——0 字节 SEQPACKET 数据报<br/>与服务端 EOF 不可区分
    Srv->>Srv: unpack → subscribed=1
    Srv-->>Lib: FrameSubscribeAck{1280x720 NV12,<br/>pool_depth, max_outstanding}
    Srv->>VI: chn_enable(): SetChnAttr(DMABUF 池=6, NV12) + EnableChn
    Note over Srv,VI: 通道跟随订阅者：无订阅者时 chn_disable，<br/>零常驻开销
    Lib-->>Py: 句柄 h{width, height, fourcc, pool_depth, ...}
    Py-->>App: FrameSource 对象
```

代码出处：`frame_recv.c:79-153`（open + 订阅）、`frame_export.c:517-546`
（accept + 身份）、`:395-417`（握手分支）、`:419-436`（订阅分支）、
`:181-209`（chn_enable）。

---

## 2. 稳态：一帧的完整生命周期（无限循环）

服务端是**推**模式（自己的事件循环里采集并扇出），客户端是**拉**模式
（`rc_ext_frame_next` 里 poll + recvmsg）。两者靠 SEQPACKET 队列解耦。

```mermaid
sequenceDiagram
    autonumber
    participant App as 业务脚本
    participant Py as recamera_ext (ctypes)
    participant Lib as librecamera_ext.so
    participant Srv as rkipc frame_export
    participant VI as VI chn1

    loop 每帧
        rect rgb(235, 240, 250)
        Note over Srv,VI: 服务端事件循环（推模式，单线程零锁）
        Srv->>Srv: poll(timeout=0)
        Srv->>Srv: g_held ≤ 池深-3 ?（任何时刻给 VI 留 ≥2 空闲 buffer）
        Srv->>VI: RK_MPI_VI_GetChnFrame(200ms)
        VI-->>Srv: VIDEO_FRAME_INFO（像素已由硬件 DMA 进 dma-buf）
        Srv->>Srv: fanout()：Handle2Fd → 填 96B frame_hdr（只构建一次）
        Note over Srv: 四步原子预约：<br/>① n_outstanding < 2 配额检查<br/>② dup(dma-buf fd)<br/>③ 登记 outstanding + subscribers_left++（先登记后发送）<br/>④ sendmsg 非阻塞，失败回滚 ③
        Srv->>Lib: sendmsg(96B frame_hdr + 1 fd，SCM_RIGHTS，MSG_DONTWAIT)
        Note over Srv: close(dup)——SCM_RIGHTS 已给对端<br/>自己的 struct file 引用
        end

        rect rgb(240, 245, 235)
        Note over App,Lib: 客户端（拉模式）
        App->>Py: for frame in src → __next__()
        Note over Py: 迭代器先 release 上一帧的借用，<br/>再获取下一帧
        Py->>Lib: rc_ext_frame_next(h, timeout_ms)
        Lib->>Lib: poll(socket) + recvmsg + 解析 cmsg
        Note over Lib: 拿到新 fd：数字与服务端不同，<br/>但指向同一个 struct file / dma-buf；<br/>校验 magic/ver/96B
        Lib-->>Py: FrameBuf{seq, pts_us, planes, fd}
        App->>Py: frame.array
        Py->>Lib: rc_ext_frame_map(h, f)
        Note over Lib: mmap(fd, PROT_READ, MAP_SHARED)——零拷贝落点；<br/>ioctl DMA_BUF_IOCTL_SYNC(START|READ)<br/>（VI 是设备直写，读前必须刷 cache）
        Lib-->>Py: base + plane[0].offset
        Note over Py: (c_ubyte * size).from_address(base) →<br/>np.ctypeslib.as_array → 只读 numpy 视图
        Py-->>App: (H, W) uint8 Y 平面视图（仅本迭代内有效）
        Note over App: ▼ 推理 / 算法<br/>（跨帧保留必须 .copy()）
        end

        rect rgb(250, 243, 235)
        Note over App,VI: 归还（下一次迭代自动触发，或显式 frame.release()）
        App->>Py: 迭代推进 → release 帧N
        Py->>Lib: rc_ext_frame_release(h, f)
        Lib->>Lib: ioctl SYNC(END|READ) + munmap
        Lib->>Srv: send(8B seq)
        Lib->>Lib: close(fd)（引用归还内核）
        Srv->>Srv: handle_release()：按 seq 移出 outstanding，<br/>--subscribers_left
        Srv->>VI: subscribers_left 归零 → RK_MPI_VI_ReleaseChnFrame<br/>（每物理帧恰好一次）
        end
    end
```

代码出处：采集与预算 `frame_export.c:612-618`、`:63`（`FE_HELD_CAP`）；
fanout 四步 `:281-374`；接收 `frame_recv.c:172-219`；mmap + cache sync
`:221-237`；numpy 包装 `__init__.py:2105-2141`、`array` `:2153-2171`；
归还 `frame_recv.c:239-256` → `frame_export.c:249-261`（release）、
`:162-170`（`rec_release`）。

---

## 3. 背压、异常与关闭（旁路）

```mermaid
sequenceDiagram
    autonumber
    participant App as 业务脚本
    participant Lib as librecamera_ext.so
    participant Srv as rkipc frame_export
    participant VI as VI chn1

    alt 客户端慢（未及时 release）
        Srv->>Srv: n_outstanding ≥ 2 → 跳过本帧（置 dropped 标记）
        Srv-->>Lib: 下一帧 hdr.flags.bit0=1（丢帧告知，seq gap 可检测）
        Srv->>Srv: outstanding 持续满 5s 且零 release → EBACKPRESSURE
        Srv->>App: 强制断开（走正常 want_close 路径）
        Srv->>VI: 该连接 outstanding 全部 ReleaseChnFrame 还池
    else 客户端崩溃（SIGKILL）
        Note over Lib,Srv: 内核自动 close 全部 fd + socket HUP——<br/>崩溃回收零用户态代码参与
        Srv->>Srv: recv() ≤ 0 → disconnect_cleanup
        Srv->>VI: ReleaseChnFrame（每连接 generation 防跨代重放 release）
    else 正常关闭（with 退出 / FrameSource.close）
        App->>Lib: rc_ext_frame_close(h)
        Lib->>Srv: close(socket)
        Srv->>Srv: disconnect_cleanup → nsub == 0
        Srv->>VI: chn_disable()——无订阅者时零常驻开销
    end
```

代码出处：背压 `frame_export.c:74`（5s 超时）、`:326-331`（满额丢帧）、
`:584-596`（收割）；崩溃清理 `:263-278`；通道跟随订阅 `:604-609`。

---

## 4. 线上消息汇总

| 方向 | 消息 | 大小 | 载体 |
|---|---|---|---|
| client → rkipc | Hello | protobuf | SEQPACKET |
| rkipc → client | HelloAck（frame@1 + limits） | protobuf | SEQPACKET |
| client → rkipc | FrameSubscribe{fps_divisor} | protobuf | SEQPACKET |
| rkipc → client | FrameSubscribeAck{几何/池深/配额} | protobuf | SEQPACKET |
| rkipc → client | **帧头 + dma-buf fd** | **96 B + 1 fd** | SEQPACKET + SCM_RIGHTS |
| client → rkipc | **release seq** | **8 B** | SEQPACKET |

约束速查（详见 `spec.md` §2 与 `AGENTS.md` 核心约定）：

- 96 B 帧头 `_Static_assert(sizeof==96)`，FROZEN，演进走 `ver` + `reserved[16]`；
  plane 的 `offset/stride/vstride` 按服务端实际分配填写，客户端禁止按宽高推导。
- `pts_us` 与 VI `u64PTS` 同源（CLOCK_MONOTONIC 微秒），结果注入
  `result-in.sock` 传同一时钟的 `pts_us` 可让 OSD 按帧对齐叠加。
- 每连接 ≤ 2 帧在途（outstanding）+ 全局 `held ≤ 池深-3`；发送永不阻塞，
  `EAGAIN` 丢帧置 `flags.bit0`；`source_id="builtin"` 保留字拒绝。
- 借用迭代器保证"下一迭代自动归还"；泄漏帧由 GC 终结器兜底 release
  （`__init__.py:2233`），release 后访问抛 `BufferReleasedError`。

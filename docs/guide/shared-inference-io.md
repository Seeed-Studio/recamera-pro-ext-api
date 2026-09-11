# 托管推理的常驻 DMA 输入输出

推理服务继续负责模型缓存、应用授权、公平调度和生命周期管理。静态视觉模型可协商
`rknn-dma-v1`，每个连接/alias 拥有独立的输入输出 DMA 缓存，大块张量不再逐帧走 socket。

## 选择与兼容

- ctypes 的 `ESK_RKNN_IO_MODE=auto` 默认探测 native tensor 属性，绑定 UINT8/NHWC
  输入及 FLOAT32 输出，保持普通 API 的逻辑输出形状。不支持的布局或绑定操作会销毁
  该 context，再建立普通 API context；不能在同一 context 混用两套 API。
  `legacy` 强制普通 API，`bound` 要求绑定成功。此开关与 `ESK_RKNN_BACKEND` 独立。
- 多输入、音频等不满足条件的模型保持原 backend。新客户端仅在 hello 声明支持时申请
  DMA 通路；旧服务、旧客户端以及不支持的模型保持原 version-1 张量消息。
- `RemoteRknnSession(..., shared_io=False)` 可关闭共享传输做 A/B。
  `session.io_transport` 返回 `rknn-dma-v1` 或 `tensor-v1`。
  `session.last_timings_ms` 包含 queue，DMA 路径另有 driver_wait 和 runtime，单位毫秒。
  runtime 包括绑定、同步、RKNN 调用及厂商输出转换，不等于纯 NPU 执行时间。

## 线格式与所有权

hello 新增 `capabilities.shared_io = "rknn-dma-v1"`。客户端完成授权和模型 load 后，
发送 `open_shared_io`，携带 `alias`、`version`。普通响应的 `shared_io` 对象包含：

- `version`、连接所属 `token`；
- 单个 `input` 描述及 `outputs` 描述列表；
- 每个描述中的 `offset`、`size`、`shape`、字节 `strides`、`dtype`。

响应后紧跟一个字节 `F` 和 SCM_RIGHTS，FD 顺序为输入、所有输出；数字 FD 仅在接收
进程有意义。缓存由服务端分配，服务端不接受客户端提交的任意指针或 DMA 描述符。
额外预算不足或可恢复的分配失败返回 `shared_io: null`，已加载模型仍可正常推理。

`infer_shared` 携带 `alias`、`token`、严格递增的 `sequence`、`timeout_ms`，不带张量。
服务端在排队后再次验证 app/instance/generation 和缓存所有权，在驱动协调锁内切换到
该连接的输入输出，执行并同步后回复相同 token/sequence。不同应用即使缓存了同一个
模型 context，也不会共享可写的 IO 缓冲区。

CPU 访问通过 DMA-BUF START/END 同步，native 侧继续执行 RKNN 输入输出同步。
`infer()` 仍返回独立的 FLOAT32 NumPy 数组：保留最后一次客户端结果复制，允许旧应用
跨后续推理、停止或卸载持有输出，不改变它们的所有权契约。

额外缓存同时计入服务总预算和应用预算，分配前预估、分配后按实际大小复核。
回滚确认成功才回退；native 销毁不确定时保留已有的 fail-closed 驱动隔离。
推理完成状态不确定时，客户端关闭连接并禁止缓存复用。服务端取消排队任务，等已进入
驱动的任务结束后才释放缓存；排队任务在执行前复核 channel，避免释放后访问。

## RGA 前处理

应用显式设置 `model_dma_input = True` 且使用 `hw-direct`，在主模型成功协商 DMA IO、
RGA ABI 与操作均支持时启用延迟前处理。未声明的新旧应用保留原有提前生成 RGB 的行为。

```python
model_frame = "hw-direct"
model_dma_input = True

x = self.pre(frame)
outputs = self.models.det.infer(x)
results = postprocess(outputs, x.info)
```

`infer(x)` 与已有支持 PreparedInput 的旧 Kit 兼容。传 `x.data` 仍正常工作，但要求
生成普通数组。YOLO、retail、fall、fitness、depth 示例采用前一种写法。

RGA 保留原 NV12 缩放、RGB 转换的两阶段顺序、居中补边和灰度 114，只复用一个 NV12
中间缓存并直接写入私有模型输入。目标非零 offset、形状/stride 不兼容、操作缺失或
RGA 执行失败时走原数组路径。`hw-roi`、原图消费者和 CPU-only 的帧源行为保持不变。

读取 `frame.data`/`x.data` 会生成独立的模型尺寸数组；原位编辑或替换 `x.data` 均会被
后续推理使用。未物化的延迟帧不能保留到源迭代之外；保留图像请在当前迭代内调用
`frame.copy()`。相机 FD 始终留在应用进程，推进或关闭源会等同步 RGA 读取结束后才
释放相机租约。延迟 RGA 计入 `pre` 和 `loop`，从 `infer` 扣除，避免重复计时。
旧的提前前处理仍发生在 loop 计时之外，因此性能比较还需看墙钟 FPS。

## IPC 锁范围与验证

配套 IPC 改动保留完整流水线的实例锁，将全局文件锁缩小到输入同步、RKNN 执行和
输出同步；加载、销毁保护不变。RGA、后处理可与其他进程的工作重叠。这一步保留
单 worker 公平调度器，也不允许同一 context 被并发修改。

主机测试覆盖协议兼容、FD 回收、连接隔离、结果保留、超时/撤销/卸载竞争、预算回退、
native 故障隔离、帧生命周期和应用结果等价性。硬件验证应使用同一模型和相同输入，
比较 legacy、绑定缓存+普通传输、共享 DMA 三种路径的输出、分段耗时、FPS，以及
反复启动/停止后的资源占用。主机测试不能提供真实 NPU 性能提升比例。

# Python AI 工作流 API 与生命周期

本文面向在 reCamera Pro（RV1126B / recamera_v2）设备上开发 Python AI
应用的用户，说明当前仓库里**已经存在的 Python 接口**、它们的生命周期，
以及当前仍需目标板验证的固件侧边界。

相关源码：

- 低层扩展绑定：[`sdk/python/recamera_ext`](../../sdk/python/recamera_ext/)
- 高层 kit：[`kit/`](../../kit/)
- RGA 图像操作：[`kit/media/image.py`](../../kit/media/image.py)
- RKNN 会话：[`kit/runtime/engine.py`](../../kit/runtime/engine.py)
- NPU broker/legacy 兼容层：[`kit/resources.py`](../../kit/resources.py)
- 托管启动：[`market/appmgr`](../../market/appmgr/)

## 0. 先读：当前可用范围

> [!WARNING]
> v1.6.0 handoff 已补回扩展 API 原生 client/server 源码；当前工作树能够从
> 源码构建 `librecamera_ext.so.1` 和带 frame/result/probe/
> inference-control 的 `rkipc`。**这不等于已经发布到所有设备**：相关仓库
> commit 与嵌套 Vigil proto 仍需由 manifest 固定，`release/pkg/sdk` 仍是旧
> 快照，并且 kill/restart/OTA 矩阵还需在 RV1126B 真机执行。

当前状态如下。

| 层 | 当前状态 | 可依赖的结论 |
|---|---|---|
| `recamera_ext` typed errors、`FrameLease`、严格 `acquire()` | Python + native client 源码与 ABI 门禁已存在 | 可作为公共契约；设备仍须安装同版 `.so` 和服务端 |
| `kit.Frame` / `ImageBuffer` / capability status | Python 源码和 host 测试已存在 | 可在 host 与设备应用中使用 |
| `RgaContext` | Python 封装已存在 | 设备上还需兼容的 `librga`；当前只开放已验证的 NV12 操作 |
| `RknnSession` | Python 封装已存在 | 设备上还需 `rknnlite`/`librknnrt` 和可用 NPU |
| `ExternalNpuLease` | 默认使用 rkipc `inference-control@1`；显式路径才走 legacy flock | broker 授权前会等待 builtin handle 真正销毁，连接断开自动回收 |
| appmgr managed launch | broker/legacy 双路路由、READY、进程组清理已存在 | 新固件走 broker；旧固件保留严格 CGI stopped-state 屏障 |
| native ext client 源码 | **已恢复** | `sdk/src/`、本地 proto/generated、CMake/ABI/source-closure 门禁齐全 |
| native ext server 源码 | **已恢复并接线** | frame/result/probe/inference-control 编入 rkipc；host 集成与 aarch64 构建通过 |
| versioned rkipc NPU lease/generation | **控制租约已实现** | 单 owner、lease_id/epoch/generation、READY、HUP fallback；数据端点 generation 绑定仍待后续 |

仓库仍含历史预编译 `.so`、补丁版 `rkipc` 和 `release/pkg/sdk` 副本；它们
不是新的权威输入，也不应覆盖源码构建产物。制作设备包时应从当前 `sdk/`
构建 native library 与两个 Python wheel/包，统一记录 SHA-256、SONAME、协议
版本和 commit；OTA 替换 `/oem` 后重新执行握手与 kill/restart 验收。

因此，下文的 frame/result/probe 设备示例有这些前置条件：

1. 设备上的 `librecamera_ext.so.1` 与 Python wrapper 来自同一发布版本；
2. `/run/recamera/` 下有相应 socket，且连接后的版本握手成功；
3. 使用 NPU 的进程成功取得 `inference-control@1` lease；旧固件只能由
   appmgr 走明确标记的 legacy 屏障；
4. RGA/RKNN 示例分别具备匹配的 `librga`、`rknnlite` 运行时。

## 1. 两层 Python API，不要混淆两个 `Frame`

| API | 角色 | 典型对象 |
|---|---|---|
| `recamera_ext` | C ABI 的低层 ctypes 封装 | `FrameSource`、`FrameLease`、`ResultSink`、native 错误码 |
| `kit` | 面向 AI 工作流的后端无关接口 | `Frame`、`ImageBuffer`、`RgaContext`、`RknnSession`、capability |

两个包都导出名为 `Frame` 的类，但含义不同：

- `recamera_ext.Frame` 是 `FrameLease` 的兼容子类，代表服务端借出的一个
  dma-buf；只在 lease 存活期间可读。
- `kit.Frame` 是工作流中的高层帧，内部持有 `ImageBuffer`。它可以包装拥有
  所有权的 NumPy 数组，也可以包装某个 native backend。

建议在同一文件同时使用两者时显式改名：

```python
from recamera_ext import Frame as NativeFrame
from kit import Frame as WorkflowFrame
```

同理，`recamera_ext.BufferReleasedError` 和 `kit.BufferReleasedError` 属于两套
异常层级。捕获时应从实际调用的包导入，不要依靠同名判断。

## 2. `recamera_ext`：错误、严格取帧和显式 lease

### 2.1 Typed errors

native ABI 的错误码现在映射为稳定类型：

| `ErrorCode` | 异常 | 含义 |
|---|---|---|
| `EVERSION(1)` | `VersionError` | client/server 版本区间不相交 |
| `EAUTH(2)` | `AuthenticationError` | 身份、token 或保留 source id 被拒 |
| `EBUSY(3)` | `BusyError` | endpoint 或资源忙 |
| `EFORMAT(4)` | `FormatError` | 请求、协议记录或 buffer 布局非法 |
| `EBACKPRESSURE(5)` | `BackpressureError` | 慢消费者被断开 |
| `ERATELIMIT(6)` | `RateLimitError` | 超过端点速率/配额 |
| `EINTERNAL(7)` | `InternalError` | 服务端、传输或 native wrapper 内部错误 |

所有 native 运行时错误继承 `RecameraError`，并提供：

- `operation`：失败的 native 操作；
- `code`：已知的 `ErrorCode`，未知码为 `None`；
- `rc`：C ABI 原始正/负返回值；
- `code_value`：包括未知错误在内的绝对数值；
- `detail`：额外诊断信息；
- `retryable`：是否通常值得稍后重试的提示，不是自动重试策略。

兼容性基类仍保留：native 运行时错误可被 `RuntimeError` 捕获，
`LibraryLoadError` 也是 `OSError`，`ResultTooLarge` 也是 `ValueError`。
严格取帧超时抛 `AcquireTimeoutError`，它是 `TimeoutError`。

推荐按具体类型处理，而不是解析字符串：

```python
from recamera_ext import (
    AcquireTimeoutError,
    BackpressureError,
    BusyError,
    FormatError,
    RecameraError,
)

try:
    # FrameSource()/acquire()/ResultSink.send_* 等操作
    ...
except AcquireTimeoutError:
    # 本次没有数据，可以回到事件循环；没有隐式再次等待。
    pass
except BusyError as exc:
    print("资源忙", exc.operation, exc.rc)
except BackpressureError as exc:
    # 旧连接已经失效；先释放/关闭，再按应用退避策略重连。
    print("消费者背压", exc.detail)
except FormatError as exc:
    # 不要盲目重试同一份非法输入或 buffer descriptor。
    raise
except RecameraError as exc:
    print(exc.operation, exc.code, exc.rc, exc.retryable)
    raise
```

### 2.2 严格 `FrameSource.acquire()`

新代码应优先调用：

```python
frame = source.acquire(timeout_ms=1000)
```

它只执行一次 native wait：

- 成功：返回一个 `recamera_ext.Frame`，它同时也是 `FrameLease`；
- native 返回 `1`：抛 `AcquireTimeoutError`；
- native 返回负错误码：抛对应 typed error；
- source 已关闭：抛 `HandleClosedError`。

再次调用 `acquire()` 之前会先释放当前 outstanding frame。也就是说，即使
下一次等待最终超时，上一帧也已经归还。一个 source 当前只维护一个 Python
borrow，不要用连续 `acquire()` 实现多帧并行持有。

以下示例在具备匹配 native client/server 的设备上取一帧，并返回可跨 lease
使用的 Y 平面副本：

```python
from recamera_ext import (
    AcquireTimeoutError,
    BackpressureError,
    FrameSource,
)


def acquire_owned_y(source):
    while True:
        try:
            with source.acquire(timeout_ms=1000) as frame:
                # frame.copy() 复制有效 Y 像素，形状为 (height, width)。
                return frame.pts_us, frame.copy()
        except AcquireTimeoutError:
            continue
        except BackpressureError:
            # 当前连接应关闭并由外层重新建立。
            raise


with FrameSource(timeout_ms=1000) as source:
    pts_us, owned_y = acquire_owned_y(source)
    print(pts_us, owned_y.shape, owned_y.dtype)
```

旧的 `for frame in source` 仍兼容：超时会继续等待，native 终止错误仍表现为
`StopIteration`，真实 typed error 保存在 `source.last_error`。需要区分超时、
背压、格式和传输错误时，不要使用这个兼容迭代入口。

### 2.3 `FrameLease` / `BorrowedBuffer` 生命周期

`FrameLease` 的 lease 在以下任一时刻结束：

1. `frame.release()`；
2. `frame.buffer.release()`；
3. source 获取/迭代到下一帧；
4. source 被关闭或退出 context manager；
5. live frame 被垃圾回收时的 best-effort 兜底。

`release()` 幂等：第一次真正归还时返回 `True`，之后返回 `False`。不要把
`__del__` 当正确性机制；生产代码必须使用 context manager 或显式
`try/finally`。

lease 结束以后：

- 元数据 `seq`、`pts_us`、`width`、`height`、`planes` 仍可用于日志；
- `fd`、`array`、`plane_array()`、`buffer.map()`、`buffer.copy()` 会抛
  `recamera_ext.BufferReleasedError`；
- `release_reason` 可用于诊断是 explicit、next acquire 还是 source close；
- fd 是借用值，应用绝不能自行 `close(fd)`。

需要特别区分三种复制：

| 调用 | 返回内容 | lease 结束后是否安全 |
|---|---|---|
| `frame.array` | 有效 Y 像素的只读零拷贝视图 | 否 |
| `frame.copy()` | 有效 Y 像素 `(height, width)` | 是 |
| `frame.buffer.copy()` | 包含 padding/各 plane 的完整 raw buffer | 是 |
| `frame.to_bgr()` | owned、连续 BGR 图像 | 是 |

`frame.planes` 中每项是 `PlaneLayout(offset, stride, vstride)`，仍兼容旧式
tuple 解包。必须使用生产者给出的 layout，不能从 width/height 猜 stride。

Python 无法撤销用户已经保存的 NumPy view。SDK 能拒绝 release 之后的**新**
属性访问，却不能让先前保存的 `view = frame.array` 立即变成异常对象；底层
buffer 归还后，这个旧 view 可能指向被复用或已解除映射的内存。需要跨帧、
跨线程或入队时，必须在 lease 内复制。

## 3. `kit.Frame`、`ImageBuffer` 与 capability status

### 3.1 后端无关的 `ImageBuffer`

`ImageBuffer` 统一表达 CPU、dma-buf 或其他 backend 存储：

- `PixelFormat`：`RGB`、`BGR`、`RGBA`、`BGRA`、`GRAY8`、`NV12`；
- `MemoryKind`：`CPU`、`DMABUF`、`BACKEND`；
- `Ownership`：`OWNED` 或 `BORROWED`；
- `planes`：不可从几何尺寸推导的 producer layout。

主要入口：

```python
ImageBuffer.from_numpy(array, format="RGB", copy=False)
ImageBuffer.from_backend(
    backend,
    width=...,
    height=...,
    format="NV12",
    planes=[...],
)
```

`numpy(copy=False)` 可以返回与 buffer 同生命周期的 view；`copy()` 返回新的
owned CPU `ImageBuffer`。`release()` 幂等，release 后所有新的数据访问抛
`kit.BufferReleasedError`。

这个 host 上即可运行的例子展示 owned copy：

```python
import numpy as np
from kit import BufferReleasedError, Frame

pixels = np.zeros((480, 640, 3), dtype=np.uint8)
frame = Frame(data=pixels, fmt="RGB", pts_us=123456)
saved = frame.copy()

frame.release()
try:
    frame.data
except BufferReleasedError:
    print("原 frame 已释放")

print(saved.data.shape, saved.owned, saved.pts_us)
saved.release()
```

`kit.Frame.copy()` 会复制 `model_data`（若它是 NumPy 数组），保留 metadata，
并丢弃与原 dma-buf 绑定的 `roi_cropper`。

### 3.2 Capability status 是置信度，不是 socket 布尔值

`kit.get_capabilities()` 返回 `Capabilities` 快照。每个 `Capability` 有：

| 状态 | 含义 |
|---|---|
| `AVAILABLE` | 已被版本化协议正向验证 |
| `UNAVAILABLE` | 已知不存在 |
| `UNKNOWN` | 看见路径或环境开关，但没有完成握手 |
| `DEGRADED` | 可用但能力/限制不完整 |

当前 `probe_capabilities()` 是**无副作用 filesystem probe**。看到 socket 只会
得到 `UNKNOWN`，不会假装为 `AVAILABLE`。`frame_broker`、`result_ingress` 等
旧 boolean 只用于兼容 adapter 选择，不能证明协议版本、权限和限制。

```python
from kit import CapabilityError, get_capabilities

caps = get_capabilities(refresh=True)
frame_cap = caps.get("frame")
print(caps.frame_broker)       # 旧选择信号
print(frame_cap.status.value)  # unavailable 或 unknown；不是握手结果
print(frame_cap.reason)

try:
    caps.require("frame", min_version=1)
except CapabilityError as exc:
    # 当前只有 filesystem 证据时应 fail closed。
    print(exc.as_dict())
```

长期 native capability getter 落地后，可以在不改变 `get()`/`require()` 调用
方式的前提下，把 `UNKNOWN` 替换为带 version/limits 的 `AVAILABLE`。

当前 adapter 的 `auto` 模式只接受经过正向验证的 `AVAILABLE`；filesystem
得到的 `UNKNOWN` 不会自动切换到 native endpoint。已确认设备固件与 SDK
匹配时，可显式设置 `RECAMERA_ADAPTER_PREFER=official`。`RECAMERA_FRAME_SOCK`
和 `RECAMERA_RESULT_SOCK` 目前只参与探测诊断；native ABI 固定使用默认端点，
非默认路径会 fail closed，不能把它们当成路由配置。

## 4. RGA：`RgaContext` 与 `TransformMapping`

`RgaContext` 接收结构化 dma-buf frame，而不是 Rockchip ctypes struct。输入
对象需要公开：`fd`、`width`、`height`、`fourcc`、`planes`、`released`；
`recamera_ext.FrameLease` 满足这个协议。

当前开放的操作：

| 方法 | 结果 | 说明 |
|---|---|---|
| `convert_nv12(frame)` | owned RGB `ImageBuffer` | 全尺寸 NV12 → RGB |
| `resize_nv12(frame, Size(w,h))` | `TransformResult` | 非等比全图 resize |
| `letterbox_nv12(frame, Size(w,h), pad_value=114)` | `TransformResult` | 等比 resize + padding |
| `crop_nv12(frame, Rect(...), Size(n,n), pad_value=114)` | `TransformResult` | 裁剪、偶数对齐、resize 到方形 |

`ImageOps` 当前是 `RgaContext` 的别名。输出都是 owned CPU RGB 数组，source
读取可以是 dma-buf，但这不是端到端 destination zero-copy。

`resize`、`letterbox`、`crop` 返回的 `TransformResult` 含：

- `image`：owned `ImageBuffer`；
- `mapping`：精确的 `TransformMapping`；
- `mapping.source_rect`：硬件实际采样的源区域；
- `mapping.output_rect`：模型图中真正图像区域，letterbox padding 不在其中；
- `mapping.to_source(x, y)` / `box_to_source(box)`：把模型坐标映回源画面像素。

矩形使用半开区间 `[x1, y1, x2, y2)`。`to_source()` 做仿射映射但不自动
clamp；模型结果落在 letterbox padding 时，应用应先过滤或按业务规则裁剪。

设备示例：在 lease 内让 RGA 读取 dma-buf，退出 lease 后继续使用 owned
letterbox 图像。

```python
from recamera_ext import AcquireTimeoutError, FrameSource
from kit import RgaContext, Size

with RgaContext() as rga, FrameSource() as source:
    try:
        with source.acquire(timeout_ms=1000) as frame:
            transformed = rga.letterbox_nv12(frame, Size(640, 640))
            pts_us = frame.pts_us
    except AcquireTimeoutError:
        transformed = None

if transformed is not None:
    # frame lease 已结束，但 image 是 owned RGB，仍然有效。
    rgb = transformed.image.numpy()
    print(pts_us, rgb.shape, transformed.mapping.output_rect)
    print(transformed.mapping.box_to_source((100, 120, 300, 400)))
    transformed.image.release()
```

当前约束：只接受 NV12（fourcc 为 0 或 `NV12`），Y plane offset 必须为 0，
stride/vstride 必须来自 producer；crop destination 必须为方形，source rect
会 clip 到画面并按 NV12 chroma 做偶数对齐。rotation、blend、draw、fence、
destination dma-buf pool 尚未成为公共 API。backend 不支持 resize/crop 时抛
`CapabilityError`，输入非法抛 `InputValidationError`，执行失败抛
`ImageOperationError`。

当前 ctypes 快速路径只允许真机已验证的 `RGA_api v1.10.5_[11]`、96-byte
`rga_buffer_t` ABI。初始化会先通过 `querystring()` 校验版本，再绑定任何
by-value struct 函数；缺少探针或版本不匹配会 fail closed。要支持其他
`librga` 版本，应提供用目标版本 headers 编译的 C shim，不能仅扩大 `.so`
搜索范围后继续假定 96-byte ABI。

## 5. RKNN：`RknnSession`、`ModelSpec` 与兼容 `RknnModel`

### 5.1 声明模型契约

```python
from kit import ModelSpec, TensorSpec

spec = ModelSpec(
    path="/userdata/local/models/detector.rknn",
    name="detector",
    inputs=(
        TensorSpec("image", (1, 640, 640, 3), "uint8", "NHWC"),
    ),
    # 不知道精确输出 shape/dtype 时可以先留空；声明后会严格验证。
    outputs=(),
)
```

`TensorSpec` 的 shape 维度 `-1` 表示接受任意正长度。`layout` 是验证和文档
元数据，runtime 不会自动 transpose。严格会话不做隐式 dtype 转换；shape 或
dtype 不符会在进入 driver 前抛 `InputValidationError`。

`RknnSession` 接受单 ndarray、多输入 sequence，或按 `TensorSpec.name` 命名的
mapping。常见 HWC 图像会自动补一个 NHWC batch 维；传给 runtime 前会变为
contiguous。`infer()` 返回 raw output ndarray list。声明了 outputs 时，会验证
输出数量，并在 strict 模式验证 shape/dtype。

### 5.2 会话生命周期

`RknnSession` 的构造顺序是：

1. 先取得 `ResourceLease`（默认 `ExternalNpuLease`）；
2. 再创建 `RKNNLite`；
3. `load_rknn()`；
4. `init_runtime()`；
5. 初始化成功后把共享 broker lease 标记为 READY（同一进程只发送一次）；
6. 每次进入 RKNN driver 前检查同一连接仍然 alive；
7. 整个 native context 生命周期持续持有 lease，先销毁最后一个 runtime，
   再关闭最后一个进程内 lease 引用。

load/init 失败时会尝试按 runtime → lease 的顺序回滚。`release()` 正常成功时
幂等，之后 `infer()` 抛 `InferenceError(code="session_released")`。如果 native
`runtime.release()` 失败，代码会 fail closed：保留 runtime 引用和 NPU lease
并将 session 标记为 quarantined，避免一个可能仍存活的 RKNN context 与新
owner 并发。底层 broker `release()` 抛错时也会保留 native lease 指针和全局
引用，供显式 `release()` 重试。两种情况都会抛 typed `InferenceError`，不能把
它当作已释放成功。推荐始终使用 context：

```python
from kit import (
    InferenceError,
    InputValidationError,
    ModelLoadError,
    RknnSession,
)

try:
    with RknnSession(spec, lease_timeout=30.0) as model:
        outputs = model.infer(rgb)  # rgb: uint8 HWC 或 1HWC
        print([out.shape for out in outputs])
        print(model.stats.calls, model.stats.average_ms)
except ModelLoadError as exc:
    print("模型加载失败", exc.as_dict())
except InputValidationError as exc:
    print("输入契约错误", exc.details)
except InferenceError as exc:
    print("推理失败", exc.operation, exc.retryable)
```

`RknnModel(path)` 是兼容类：它默认 `strict_inputs=False`，保留旧行为，把非
`uint8` 输入转换为 `uint8` 并为 HWC 补 batch。新应用应使用声明了
`ModelSpec`/`TensorSpec` 的 `RknnSession`；兼容类不能发现静默 dtype 错误。

> [!CAUTION]
> `RknnSession` 默认会在创建 RKNN context **之前**取得 rkipc 原子 lease；
> broker 不存在、握手失败或 builtin 未在 deadline 内完全释放时一律
> fail closed。不要通过传自定义 `lease` 或 `RECAMERA_NPU_LOCK` 绕过它；
> flock 只为 host 测试和明确的旧固件兼容保留。

## 6. `ExternalNpuLease` 与 rkipc broker

该公共接口从 `recamera-ext` Python distribution 1.4.0、native SDK 1.3.0
起提供；`recamera-pro-kit` 要求 `recamera-ext>=1.4,<2`。native SONAME 仍为
`librecamera_ext.so.1`。

无参数构造 `ExternalNpuLease()` 时，默认连接
`/run/recamera/inference-control.sock`，底层使用
`recamera_ext.InferenceLease`。连接本身就是 lease：不是 pidfile，也不是
Python finalizer。多个相同配置的 session 在同一进程内通过引用计数共享一个
native 连接，最后一个 RKNN runtime 成功销毁后才关闭它。

broker 的 ACQUIRE 过程由 rkipc 完成以下原子边界：设置 external hold，驱动
builtin NPU 线程停止并 deinit，只在 `handle == NULL`、`state == stopped`、
`actual_fps == 0` 三者同时成立后返回 GRANT。随后 `RknnSession` 才会调用
`load_rknn()` / `init_runtime()`，成功后发送 READY。每次 GRANT 返回新的
`lease_id` 与递增 `generation`；`epoch` 在 rkipc 重启后变化。

超时含义在两个后端不同：broker 下 `timeout=None` 选择服务端有界默认值，
`timeout=0` 映射为最小的 1 ms 非阻塞意图，显式值最大为 30 秒；legacy flock
下 `None` 才表示无限等待。超时、busy、能力缺失和传输故障都映射为 kit typed
errors，不会静默切换后端。

它能保证：

- builtin 与 external 的所有权切换由同一个 rkipc 状态机完成；
- 同时最多一个 external owner；并发申请者得到 typed busy/timeout；
- client `SIGKILL`/崩溃后 kernel 产生 socket HUP，rkipc 撤销 owner 并按
  `fallback_builtin` 恢复或保持关闭，不依赖 Python `finally`；
- `fork()` 子进程只关闭自己继承的 fd 副本，不发送 RELEASE，不会误撤销父
  进程 generation；exec 不会继承（CLOEXEC）；
- `RknnSession.infer()` 每次进入 driver 前检查 lease liveness，失联后拒绝
  新推理；同一 context 的 infer 串行，release 等待在途 infer 结束。

当前仍不保证：

- 恶意 root 进程绕过公开 SDK 直接访问 `/dev/rknpu`；v1 尚无内核级 sandbox；
- NPU/内存/温控的细粒度配额和多模型调度；
- frame/result/probe 连接携带并校验 lease generation。当前 generation 已保护
  NPU 控制连接，但数据端点仍依赖连接断开、appmgr 进程组清理与 Python
  liveness 检查；完整旧-generation `ESTALE` 是下一协议版本工作。

显式传 `path=` 或设置 `RECAMERA_NPU_LOCK` 才选择 legacy `flock`。历史设备上，
appmgr 在严格轮询 CGI 到 stopped 后注入
`RECAMERA_NPU_MANAGED=appmgr-v1`，才允许使用默认旧锁；它不具备上述原子性。
新固件 appmgr 注入 `RECAMERA_NPU_BROKER_REQUIRED=1`，此时任何 lock override
都会被拒绝，broker 失败也不会降级到 legacy 并发运行。

## 7. 当前推荐：appmgr managed launch

生产环境不要直接运行 `python app.py`、`python -m kit.run ...`，也不要直接
调用内部的 `supervisor.start()`；这些路径本身不会跨 builtin teardown
屏障。使用 appmgr 的统一入口：

```sh
# 已安装 app 的统一激活入口。
python3 -m appmgr activate my-ai-app

# 查看 running/active/last_exit。
python3 -m appmgr list

# 停止外部 app，保持无推理 owner。
python3 -m appmgr activate none

# 恢复内建推理。
python3 -m appmgr activate builtin
```

设备 loopback HTTP 也走同一逻辑：

```sh
curl -sS -X POST http://127.0.0.1:8130/api/appMgr/activate \
  -H 'Content-Type: application/json' \
  -d '{"id":"my-ai-app"}'
```

公网/浏览器入口必须经过 nginx/JWT；`127.0.0.1:8130` 只用于设备本机管理。

新固件激活 external app 的实际顺序是：

1. appmgr busy gate 串行化 activate/switch/stop；
2. 发现 broker socket 后选择 broker 路由，不先用 CGI 猜测资源是否释放；
3. supervisor 用新 session/process group 启动 app，并只给这次授权 launch
   注入 `RECAMERA_NPU_BROKER_REQUIRED=1`；
4. child 的 `RknnSession` 发 ACQUIRE；rkipc 设置 hold、销毁 builtin RKNN，
   到达严格 drained 状态后才 GRANT；
5. child 加载模型/初始化 RKNN，发送 lease READY，再完成 frame/sink 等初始化；
6. `kit.run_app` 写应用 READY；appmgr 收到后才提交 active；
7. 启动失败或运行期崩溃会清理保存的 PGID 及后代；broker 从 HUP 回收 NPU，
   并按 fallback 恢复 builtin。

当 broker socket 不存在时，appmgr 才进入旧固件兼容路由：对 entry.cgi
disable 后持续 GET，只有 `iEnable=0`、`sStatus=stopped`、`iActualFPS=0` 同时
成立才注入 `RECAMERA_NPU_MANAGED=appmgr-v1` 并启动；缺字段、传输失败或超时
都 fail closed。

app leader 运行期崩溃时，supervisor 使用启动时保存的 PGID 直接清理同组
后代；即使 leader 已经消失，也不会只依赖 `getpgid(dead_pid)`。显式 stop
先发整组 TERM，超时后整组 KILL。boot restore 重新启动 remembered external
app 前也会走同一个 builtin stop barrier。

broker 路径不把“socket 文件存在”当成功：最终以 child 的 Hello/ACQUIRE/GRANT
为准。若 socket inode 陈旧或协议不匹配，应用启动失败并回滚，不会偷偷走
legacy 路径。

## 8. 已实现的 lease 契约与下一版本边界

当前 `inference-control@1` 已实现：

1. external client 与 rkipc 协商 lease protocol version/capability；
2. rkipc 原子完成 builtin teardown 并授予 connection-lifetime lease；
3. lease 带不可复用的 `lease_id`/`generation`；
4. client crash/socket HUP 时 rkipc 回收 lease，并按策略恢复 builtin；
5. STATUS/READY/SET_FALLBACK/RELEASE 都校验 live lease_id + epoch；
6. appmgr 按新旧固件能力分流，broker 路径不使用 CGI 作为 NPU 正确性边界。

下一协议版本需要把 lease_id/epoch/generation 带入 frame/result/probe Hello，
使旧 generation 数据在服务端以 `ESTALE` fail closed，并增加资源配额、健康
查询与 cgroup 级隔离。当前版本不得宣称已完成这部分。

## 9. 小型 typed workflow 与背压

`kit.workflow` 提供一个有意保持简单的**同步、顺序** pipeline，以及一个可在
生产者/消费者线程之间使用的有界 `InputQueue`。它不是 DAG 调度器，也不会
自动申请 camera/NPU/RGA；`Stage.requires` 只校验调用方是否把对应对象放进
`WorkflowContext.resources`。

```python
from kit import ResourceKind, Size, Stage, WorkflowContext

preprocess = Stage(
    "preprocess",
    lambda frame, ctx: ctx.resources[ResourceKind.RGA].letterbox_nv12(
        frame, Size(640, 640)
    ),
    requires={ResourceKind.RGA},
)
infer = Stage(
    "infer",
    lambda transformed, ctx: ctx.resources[ResourceKind.NPU].infer(
        transformed.image.numpy()
    ),
    requires={ResourceKind.NPU},
)

context = WorkflowContext(resources={
    ResourceKind.RGA: image_ops,
    ResourceKind.NPU: model,
})
with (preprocess | infer) as pipeline:
    outputs = pipeline.run_one(frame, context, timeout=0.2)
```

deadline 在 stage 边界检查；Python callable 无法被安全抢占，长耗时 stage 应
主动检查 `context.cancellation`。普通 stage 异常包装为带 `__cause__` 的
`StageError`，`KeyboardInterrupt`/`SystemExit` 不会被吞掉。

Stage 是线性生命周期资源：组合成功后由最终 Pipeline 独占，继续组合会把
源 Pipeline 的所有权转移给新对象；不要复用同一个带 closer 的 Stage 构造
两个 Pipeline。`close()` 会先阻止新任务、等待在途 `run_one()` 完成，再逆序
exact-once 关闭 stage，并返回 `WorkflowCloseReport`。stage/closer 内重入调用
所属 pipeline 的 `close()` 会抛 typed `reentrant_close`，避免自等待死锁。

实时帧入口必须有界：

```python
from kit import DropPolicy, InputQueue, PutStatus

queue = InputQueue(capacity=2, policy=DropPolicy.DROP_OLDEST)
result = queue.put(frame)
if result.status is PutStatus.DROPPED_OLDEST:
    # 被丢弃的 borrowed frame 仍由应用负责及时归还。
    result.dropped_item.release()
```

`BLOCK` 可配置 timeout 并在队满时抛 `WorkflowBackpressureError`；另外两种策略
通过 `PutResult` 明确返回丢弃的是旧 item 还是本次新 item，绝不静默丢帧。
关闭 queue 会唤醒所有等待者，并允许消费者先 drain 已入队 item。

## 10. 一个完整的 managed workflow 片段

下面代码适合放进由 appmgr 启动的应用生命周期中。它展示 lease 内 RGA、
lease 外 owned RGB 推理，以及坐标映射。示例假定设备已满足第 0 节前置条件。

```python
from recamera_ext import AcquireTimeoutError, FrameSource
from kit import ModelSpec, RgaContext, RknnSession, Size, TensorSpec

model_spec = ModelSpec(
    "/userdata/local/models/detector.rknn",
    inputs=(TensorSpec("image", (1, 640, 640, 3), "uint8", "NHWC"),),
)

with FrameSource(timeout_ms=1000) as frames, \
     RgaContext() as image_ops, \
     RknnSession(model_spec, lease_timeout=30.0) as model:
    while True:
        try:
            with frames.acquire(timeout_ms=1000) as native_frame:
                transformed = image_ops.letterbox_nv12(
                    native_frame, Size(640, 640))
                pts_us = native_frame.pts_us
        except AcquireTimeoutError:
            continue

        # transformed.image 为 owned RGB；native frame 已经归还。
        outputs = model.infer(transformed.image.numpy())
        print(pts_us, [out.shape for out in outputs])

        # 后处理若产出模型空间 box，可映回原图像素：
        # source_box = transformed.mapping.box_to_source(model_box)
        transformed.image.release()
```

真实应用还应在循环中处理停止信号、typed transport errors、结果归一化与
`ResultSink` 回注。所有要跨 iteration 保存的图像都应是 owned copy；所有
native handle、frame lease、RGA context、RKNN session 都应通过 context
manager 或显式 `try/finally` 关闭。

### 10.1 随结果发送通用绘制图元

应用需要画任意点、线或多边形时，用 `GeometryBuilder` 构建 canonical `geometry[]`，
再交给 `App.emit()`；builder 也接受常见 NumPy 数值/数组，并在发送前拒绝 NaN、负坐标、
非法颜色和超限图元：

```python
from kit import GeometryBuilder

drawing = (GeometryBuilder()
    .point(320, 180, id="nose", color="#00ff00", point_radius=4)
    .line((100, 100), (400, 100), line_width=2)
    .box((40, 60, 180, 260), label="person", color="#ffaa00"))

self.emit(
    events,
    frame.pts,
    results=results,
    geometry=drawing,
)
```

也可从 `kit.ai` 导入 `geometry_point/geometry_line/geometry_polygon`，或用
`geometry_quad/geometry_keypoints/geometry_pose` 将 OCR、关键点和骨架转成基础图元。
应用 API 故意不接受 `space`：必须在已安装且通过结构/内容校验的 manifest 中以
`output.fields[from="geometry[]"].coord` 声明 `pixel_points` 或
`normalized_points`，并用 `render.schema_version=1` 的 `render.geometry` 限定允许类型与
默认样式。Result Hub 按当前 instance/generation 注入空间；payload 冒充的身份、stream、
render 或 space 都会被忽略。

## 11. 上机前检查清单

- [ ] Python wheel/package 中包含 `recamera_ext/errors.py` 和 `buffer.py`；
- [ ] Python wrapper、header、`.so`、rkipc endpoint 来自同一版本；
- [ ] 不以 socket 文件存在代替真实握手，`UNKNOWN` 不当作 `AVAILABLE`；
- [ ] 外部 RKNN app 经 `appmgr activate` 启动，不直跑脚本；
- [ ] builtin stop timeout 时 external app 确实未启动；
- [ ] frame 只在 lease 内使用，跨帧数据在 release 前 copy；
- [ ] RGA 使用 producer stride/vstride，模型框经 `TransformMapping` 回映；
- [ ] `RknnSession` 声明正确的 input dtype/shape/layout；
- [ ] app crash 后 PGID 后代被清理，broker 观察到 HUP、lease generation 被回收；
- [ ] OTA/重新打包后重新验证 native binary 哈希、socket 和真机端到端链路；
- [ ] 不把控制 lease generation 误写成数据端点已经具备完整 ESTALE fencing。

## 12. API 版本与 NPU backend 说明

当前 kit 同时支持 API `0.2.0` 与 legacy API `1.6.5`：v1 manifest 按 legacy
元数据校验；v2 manifest 显式按 `__api_version__`，安装和启动时都会拒绝不满足版本
范围的 kit。新源码 manifest 中的版本不等于 catalog 中已经发布的包版本；新包
必须先由 v2 builder 生成 lock、BOM 和签名，再更新 catalog。

ctypes backend 接受调用方的静态单输入 `uint8/NHWC` 合约。native graph 内部
仍可能是 NCHW 或 int8，这不改变调用方合约。auto 模式遇到不支持的 spec 使用
`rknnlite`；可用 `ESK_RKNN_BACKEND=rknnlite` 强制选择，但 native 初始化失败后
不会隐式切换 backend。managed 模式的上游 lease 与 driver lock 仍然生效；早期
RSS 数字来自 8 月 19 日的旧路径，新 daemon 仍需在设备上验证。

### 12. API versions and NPU backend

The kit supports API `0.2.0` and legacy API `1.6.5` in parallel. A v1 manifest is checked against
legacy metadata. A v2 manifest is checked against `__api_version__` explicitly and rejects an
incompatible kit during both installation and startup. A source manifest version
does not identify an already published catalog package. A new package must go
through the v2 builder to produce its lock file, BOM, and signature before the
catalog is updated.

The ctypes backend accepts a caller contract with one static `uint8/NHWC` input.
The native graph may still use NCHW or int8 internally; that does not change the
caller contract. In auto mode, an unsupported spec uses `rknnlite`. Set
`ESK_RKNN_BACKEND=rknnlite` to force that backend; a native initialization failure
never causes an implicit backend switch. The managed upstream lease and driver
lock remain active. The earlier RSS figures came from the August 19 legacy path;
the new daemon still requires device validation.

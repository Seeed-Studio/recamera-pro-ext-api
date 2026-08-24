# reCamera Pro 扩展 API 示例工程

适用设备：reCamera Pro（RV1126B / recamera_v2）。

这些示例演示方案商如何在设备上运行**自己的进程**对接固件的扩展 API：拿摄像头帧、把结果回注到 OSD/RTSP/录像/推送、用结果驱动 GPIO。**全部运行在设备上，是方案商进程，不改固件、不重编固件、不刷自编固件。**

事实来源（本目录所有 API 签名逐一核实自这两份文件）：

- C ABI：`sdk/librecamera_ext/include/recamera_ext.h`
- Python 封装：`sdk/librecamera_ext/python/recamera_ext/__init__.py`
- 概念/约束：`docs/guide/README.md`（总入口）+ 各分篇

## 示例索引

| 目录 | 语言 | 用到的 API | 一句话 |
|---|---|---|---|
| [`01-hello-frame/`](./01-hello-frame/) | Python | `FrameSource` | 拿几帧存成图，最小取帧示例 |
| [`02-inject-result/`](./02-inject-result/) | Python | `ResultSink` | 注入一个框/分类/跟踪/关键点，出现在 OSD/RTSP/WS |
| [`03-frame-to-inference-to-osd/`](./03-frame-to-inference-to-osd/) | Python | `FrameSource` + `ResultSink` | 完整闭环：取帧 → 自带算法（帧差运动检测）→ 回注 OSD |
| [`04-gpio-trigger/`](./04-gpio-trigger/) | Python | notify WS + gmgr | 结果命中即拉高/拉低引脚（继电器/LED/告警） |
| [`05-cpp-frame/`](./05-cpp-frame/) | C | `rc_ext_frame_*` | C ABI 拿一帧写盘 + 交叉编译 Makefile |
| [`06-probe/`](./06-probe/) | Python | `ProbeSource` | 观测内建推理流水线各级张量/指标（只读） |
| [`07-shared-model/`](./07-shared-model/) | JSON/发布流 | catalog `models[]` + `/putModel` | **可用/已实现**：共享大模型不打进包，走 `models[]`+`target_path` 由浏览器代取下发（活样本 voice-transcribe） |
| [`08-app-with-deps/`](./08-app-with-deps/) | JSON/skeleton | manifest `deps` + per-app venv | **skeleton/未实现**：app 独有 Python 依赖（如 PyAV）随包分发、装进 per-app venv 的设计示范（见设计文档） |
| [`09-declarative-output/`](./09-declarative-output/) | JSON/manifest | manifest `capabilities:["output"]` + `output` 块 | **可用/已实现**：声明式把结果发到 MQTT/HTTP/UART/WS + HA Discovery，app.py 零输出代码（`ConfigurableSink`，活样本 yolo-detector） |
| [`10-video-backends/`](./10-video-backends/) | Python | kit `FrameSource` ABC + GStreamer/FFmpeg/OpenCV | 第三方视频框架旁路拉 RTSP 取帧，与 kit 原生 `frames()` 对照（取舍：丢 RGA 硬件 letterbox） |

建议阅读顺序：01 → 02 → 03（03 是"方案商自带流水线"的核心示例），04/05/06 按需。

> **07/08/09 与 01–06 不同层次**：01–06 是「设备上跑自己的进程对接扩展 SDK」的运行时示例；
> 07/08/09 是「**应用中心声明 / 上架**」角度——app 包怎么声明并分发**共享模型**（07，已实现）、
> **per-app 依赖**（08，设计中未实现）、**声明式结果输出**（09，已实现，改 manifest 不写代码）。
> 09 的事实来源是 `kit/adapters/output_sink.py` + `internal/OUTPUT_SINK_SPEC.md` +
> `docs/guide/output-sink.md`；07/08 不涉及扩展 SDK 调用，事实来源是
> `market/{catalog/gen_catalog.py,catalog/models.json,appmgr/server.py,appmgr/modelstore.py}`
> 与 `docs/guide/per-app-dependencies.md`。

### 应用生命周期（install / uninstall）

07/08 提到的下发发生在**安装**前后。安装 / 卸载入口（`market/appmgr`，CLI 与 HTTP 共用
`server.py` 同一实现）：

| 动作 | CLI | HTTP |
|---|---|---|
| 安装 | `python3 -m appmgr install <pkg.tar.gz>` | `POST /api/appMgr/install {path: "/userdata/.../x.tar.gz"}` |
| 卸载 | `python3 -m appmgr uninstall <id>` | `POST /api/appMgr/uninstall {id}` |

- **install 传的是包路径**（先 `/upload` 原始 tar.gz 拿到设备路径，再 `/install`），
  **uninstall 传的是 app id**。
- 卸载序列：running → 先 `stop`；是 single-active → 清 active；再删
  `/userdata/local/apps/<id>/`（**共享模型 `/userdata/local/models` 不删**，跨 app 资产）。
  `do_uninstall` 已预留「if present 连带删 per-app venv `/userdata/local/venvs/<id>`」钩子
  （等 08 的 `deps` 落地生效）。

### 生产级适配层范本（区别于上面的最小示例）

上面 01–03 是**最小、单文件**示例，便于逐行读懂 SDK 调用。如果你要把扩展 API
接进一个**完整的多应用框架**（而不是单脚本），参考 kit 的 L0 适配层：

- [`kit/adapters/official.py`](../kit/adapters/official.py) — `OfficialFrameSource`
  （`frame.sock` 零拷贝取帧 → 全分辨率 RGB）+ `OfficialResultSink`
  （按结果类型路由到 `result-in.sock` → OSD/录像/推送）。实现统一的 `FrameSource`/
  `ResultSink` 基类，被 [`kit/adapters/registry.py`](../kit/adapters/registry.py)
  按 socket 探测自动选中——**socket 在就用官方 API，不在就回退 RTSP/WS 兜底，9 个
  应用一行不改**。`OfficialResultSink` 按各应用产出把结果分发到全套 `send_*` 通道：
  检测框→`send_detections`、pose 关键点(健身/跌倒/facemesh)→`send_keypoints`(带真实
  17/468 关键点)、人脸属性/表情→`send_classification`、跟踪→`send_tracking`、分割
  →`send_segmentation`。重点注释了方案商最容易踩的点：RGA vs OpenCV 预处理开关、
  dma-buf 归还、`pts_us` 对齐、`source_id`、以及各结果字段→SDK 元组的映射。
- [`kit/adapters/_rga.py`](../kit/adapters/_rga.py) — 可选的 librga（RK 2D 硬件）
  NV12→RGB ctypes 薄封装，缺库/ABI 不符时优雅降级到 OpenCV。

> **端侧验证 TODO（设备在用，实机验证待放行）**：`official.py` / `_rga.py` 的
> 全链路验证依赖设备上装好 `librecamera_ext.so`(新版 `sdk/`) + `librga.so` + 含
> 扩展 API 的固件。本地仅通过 mock 做了接口一致性/结果路由/降级逻辑单测
> （`kit/adapters/test_official.py`，含 detections/keypoints/classification/tracking
> 五类映射断言）。放行后需在设备上逐一确认：
> - **keypoints / classification 的 OSD 绘制**是否符合预期（关键点连线、属性文字叠加）
>   —— rkipc 对这两类的绘制样式端到端尚未实测；
> - 空结果帧是否正确清屏（当前用空 `send_detections` 清屏，跨任务类型清屏行为待确认）；
> - `_rga.py` 的 `rga_buffer_t` 布局 / `RK_FORMAT_*` / `imcvtcolor_t` 符号与设备
>   librga 一致。

## 通用前置条件

以下对所有示例通用（详见 `docs/guide/README.md` §1.2）：

1. **固件必须包含扩展 API。** 扩展 socket（`/run/recamera/frame.sock`、`result-in.sock`）只在**含扩展 API 的 rkipc 固件**里存在。原厂 rkipc 没有这些端点，示例会连接失败。判断方法：

   ```sh
   ls -l /run/recamera/
   # 期望看到 frame.sock / result-in.sock（0660 root:root）
   ```

2. **socket 权限（v1 = root-only，共享 root）。** 实测 RV1126B 上 `/run/recamera/` 目录为 `0750 root:root`、socket 文件 `0660`，**没有** `recamera-ext` 组。扩展应用经 `appmgr serve` 以 **root** 启动（麦克风/摄像头/`/dev/mpi` 设备节点均 root 属主，非 root 开不了硬件节点），身份区分靠 SO_PEERCRED + appmgr 注册表而非独立 uid/组。**直接以 root 跑即可，无需创建或加入任何组。**

3. **C 客户端** 链接 `librecamera_ext.so.1`（设备上应已随固件安装到 `/lib` 或 `/usr/lib`）。
   **Python 客户端** `import recamera_ext`（ctypes 薄封装，运行时 `dlopen` 同一个 `.so`）。Python 端还需：
   - `numpy`（`frame.array` 依赖）
   - `opencv-python`（仅 `frame.to_bgr()` 需要；示例 03 用；示例 01 默认走无依赖的 PGM 存盘）

4. **握手 SDK 内部自动完成。** frame/result/probe/inference-control 四条 socket
   连接后都先走 Hello/HelloAck，无需手写 protobuf。

## 部署到设备

示例都是单文件脚本 / 单个 C 源文件，拷进设备即可。

reCamera Pro（root 用 adb）：

```sh
# Python 示例
adb push examples/01-hello-frame/hello_frame.py /root/
adb shell 'cd /root && python3 hello_frame.py'

# C 示例（先在开发机交叉编译，见 05-cpp-frame/README.md）
adb push frame_dump /root/
adb shell '/root/frame_dump'
```

或用 scp（若设备开了 SSH）：

```sh
scp examples/03-frame-to-inference-to-osd/motion_to_osd.py root@<设备IP>:/root/
ssh root@<设备IP> 'python3 /root/motion_to_osd.py'
```

Python 的 `recamera_ext` 包：把 `sdk/librecamera_ext/python/recamera_ext/` 拷到设备上脚本同级目录，或装进 `site-packages`。

## 常见问题（跨示例通用）

- **`rc_ext_frame_open failed` / `rc_ext_result_open failed`**：多半是固件不含扩展 API（`/run/recamera/` 无对应 socket），或权限不足（非 root——socket 是 `root:root` root-only）。先 `ls -l /run/recamera/`。
- **`librecamera_ext.so.1 not found`**：`.so` 未随固件安装。设 `LD_LIBRARY_PATH` 指向它所在目录，或给 `FrameSource(lib_path=...)` / `ResultSink(lib_path=...)` 传绝对路径。
- **`ModuleNotFoundError: numpy`**：`pip install numpy`（或设备上用 `uv`/`opkg` 对应包）。
- **注入的框不出现在画面**：确认用的是 `result-in.sock`（本套示例）而非 legacy notify；`source_id` 不能是保留字 `"builtin"`；发送速率别超过 60 msg/s。

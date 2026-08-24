# AGENTS.md

给基于本 SDK / kit 开发 reCamera Pro（RV1126B / recamera_v2）应用的人及其编码 agent。本仓是**公开仓**：native client、Python SDK、kit、示例和历史发布记录。设备必须先运行含匹配 endpoint 的固件；此后方案商应用作为独立进程开发，无需为每个 app 重编固件。固件服务端实现在相邻 `recamera_ipc` 仓，本仓不能用预编译 sideload 快照代替源码发布。

## 项目结构

| 路径 | 是什么 |
|---|---|
| `docs/api/` | 规格与架构：`spec.md`（socket 路径 / protobuf schema / ABI 版本 / 坐标契约）、`architecture.md`（分层与数据流）。**事实来源。** |
| `docs/guide/` | 开发指南：`README.md`（总入口 + §4 结果注入契约）+ 分篇（control-api / result-push / gpio / audio-pcm / frontend-extension / ffmpeg / gstreamer / app-center-publishing / deploy-ops / kit-design / adapter-bootstrap / voice-app）。 |
| `sdk/` | 设备侧 SDK：`src/`（native client）+ `include/recamera_ext.h`（C ABI）+ `proto/generated` + `python/recamera_ext`（ctypes 薄封装）+ `VERSION`。**权威 SDK 源。** |
| `kit/` | 可复用 Python 推理套件：`app.py`（App 基类 + 主循环）、`adapters/`（L0 适配层：frame/result/audio/mqtt/cgi + registry 探测）、`runtime/`（前后处理）、`logic/`（追踪/区域等）。 |
| `apps/` | 示例应用（yolo-detector / face-analysis / fall-detection / facemesh-reader / voice-transcribe 等），继承 kit。 |
| `examples/` | SDK 最小单文件用法示例（01 取帧 / 02 注入 / 03 帧→算法→OSD / 04 GPIO / 05 C ABI / 06 probe）。 |
| `release/` | 历史发布记录；当前 sideload 已 fail-closed，不能部署。下一发布必须从 manifest 固定源码构建。 |
| `market/` | 应用中心控制面、推理调度服务、manifest-v2 打包与 catalog 工具。 |

## 扩展 API 模型：四条 socket

方案商进程通过 `/run/recamera/` 下四条 Unix domain socket 与固件交互（契约 = 进程边界）：

| socket | 客户端 | 作用 |
|---|---|---|
| `frame.sock` | `FrameSource` | 零拷贝拿相机原始帧（全分辨率 NV12，不预 letterbox），自己推理 |
| `result-in.sock` | `ResultSink` | 把结果回注官方 OSD / 录像 / 推送三路分发 |
| `probe.sock` | `ProbeSource` | 只读观测内建推理流水线各级张量/指标 |
| `inference-control.sock` | `InferenceLease` / `ExternalNpuLease` | 停妥内建模型后授予 external RKNN 单 owner 的连接生命周期租约 |

握手 Hello/HelloAck 由 SDK 内部完成，不接触 protobuf。契约细节见 `docs/api/spec.md` 与 `docs/guide/README.md` §4。

## 核心约定（最易踩，动手前先读）

- **坐标一律归一化 `[0,1]`**：所有 box 坐标（检测/分类 ROI/分割 ROI/跟踪/关键点对象框）及关键点 point 的 x/y 均为相对画面宽高的比例。**传像素值会被 OSD clamp 成 1px 隐形框**——手头是像素就除以帧宽/帧高。分割 mask 是行主序原始字节（非坐标）。这是最常见的 BUG。
- **Python 用 uv，不裸 `pip install`**：`uv run pytest` / `uv add`。
- **OSD 单槽后写覆盖**：同一 `source_id` 的结果后写覆盖前写；空 `send_detections` 用于清屏。
- **seg 不上 OSD**：分割 mask 不渲染到 OSD（只走推送/录像元数据）。
- **`source_id` 不能用保留字 `"builtin"`**（内建推理专用，外部用被拒 EAUTH）。
- **限速 60 msg/s/连接**（burst 15）+ 全局 120（burst 30），单条 payload ≤ 64KB，并发注入连接 ≤ 4。超限丢弃+计数，别超过帧率发。
- **`pts_us`**：要与某帧对齐叠加时传该帧的 `frame.pts_us`（同 VI 帧 PTS 时钟）；`0` 表示不与具体帧关联。
- **零拷贝视图跨帧要 `.copy()`**：`frame.array` / `ProbeSample.array` 下一次迭代即失效。

## 开发一个 app

1. 继承 `kit.app.App`，覆盖 `setup(config)`（读 config_schema 参数）和 `on_results(results, frame)`（业务逻辑：原始检测 → app 级事件）。CPU-only app 设 `needs_model=False` 并覆盖 `process_frame`。
2. 写 `manifest.json`（`id` / `version` / `entry: app.py` / `models` / `config_schema` 等，参考 `apps/*/manifest.json`）。
3. 直接用 SDK：`from recamera_ext import FrameSource, ResultSink, ProbeSource`。需要 RKNN 的生产 app 必须由 appmgr 启动，并让 `RknnSession`/`ExternalNpuLease` 取得 broker 租约；不能用 CGI 成功响应、pidfile 或普通 flock 冒充 NPU 所有权。

## 构建 / 测试

```sh
# kit 单元测试（mock，不需要设备）：
uv run pytest kit/adapters/

# 跑 examples（设备上，或指向已装 SDK 的路径）：
export PYTHONPATH=/userdata/sdk/python:$PYTHONPATH
export LD_LIBRARY_PATH=/userdata/sdk/lib:/oem/usr/lib:/usr/lib:$LD_LIBRARY_PATH
python3 examples/02-inject-result/inject_result.py --task detection
```

## 设备部署 / 验证

- 当前源码构建把 SDK/kit 装入固件 Python 3.11 site-packages，native library 装入 `/usr/lib`；`/userdata/sdk` 仅是历史手工布局。
- 前置：固件必须含四个扩展 socket，并完成协议握手；仅 `ls` 看 inode 不足以证明版本兼容。
- **NPU 推理 (rknnlite)**：`RknnSession` 必须先从 `inference-control@1` 取得 lease，初始化成功后 READY；broker 缺失或失联一律 fail closed。旧固件只允许 appmgr 走明确标记的 CGI stopped-state 兼容屏障。
- 烟雾 demo：`examples/02-inject-result`，然后 RTSP（`rtsp://<ip>:8554/...`）或 WS（`127.0.0.1:8123 /ws/inference/results`）看注入的框。
- 端到端自检清单见 `docs/guide/deploy-ops.md` §5。

## release（发布物）

- 仓内 v1.x tar/pkg 是历史快照，hash/身份集合不一致且不含当前 broker；安装和固件部署脚本已禁用。
- 下一发布必须固定 `recamera_ipc`、Vigil、本仓和 Web backend commit，从源码构建 rkipc/entry.cgi/native/Python，生成统一 BOM 后做真机 kill/restart/OTA 门禁。
- `release/build-release.sh` 只是 legacy artifact assembler，不是源码可复现 builder。

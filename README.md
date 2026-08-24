# reCamera Pro 扩展 API

reCamera Pro（RV1126B / recamera_v2）扩展 API：设备先安装包含扩展 endpoint、
且与 SDK 协议匹配的固件；此后方案商无需为每个 AI 应用重编固件，可把应用
作为独立进程运行，通过 `/run/recamera/` 下的 Unix domain socket 拿帧、取得
NPU 租约、回注结果并观测流水线。进程边界即契约。

四条 socket 端点：

| 端点 | socket | 作用 |
|------|--------|------|
| 帧代理 | `/run/recamera/frame.sock` | 零拷贝拿相机原始帧，自己推理 |
| 结果注入 | `/run/recamera/result-in.sock` | 把结果回注官方 OSD / 录像 / WS 三路分发 |
| 观测面 | `/run/recamera/probe.sock` | 采样内建推理流水线各级张量 |
| NPU 仲裁 | `/run/recamera/inference-control.sock` | 停妥内建模型后授予单 external owner 的连接生命周期租约 |

## 目录导航

| 目录 | 作用 |
|------|------|
| [`docs/api/`](docs/api/) | 规格与架构：[`spec.md`](docs/api/spec.md)（socket 路径 / protobuf schema / ABI 版本）、[`architecture.md`](docs/api/architecture.md)（系统分层与数据流）。 |
| [`docs/guide/`](docs/guide/) | 开发指南：[总入口](docs/guide/README.md) + 各专题手册（控制 API、结果推送、GPIO、音频 PCM、前端扩展、FFmpeg / GStreamer 集成、应用中心上架、部署运维）+ Kit 设计 / 适配器路径 / 语音应用设计。 |
| [`sdk/`](sdk/) | 设备侧 SDK：`librecamera_ext.so.1` + Python 包 `recamera_ext`（ctypes 薄封装）+ C 头文件。 |
| [`kit/`](kit/) | 可复用 Python 推理套件：L0 适配器、runtime 前后处理、logic 库、`app.py`。 |
| [`apps/`](apps/) | 示例应用（yolo-detector / face-analysis / fall-detection / voice-transcribe 等）。 |
| [`examples/`](examples/) | SDK 最小用法示例（拿帧 / 注入结果 / 帧→推理→OSD / GPIO 触发 / C 帧）。 |
| [`release/`](release/) | 历史交付快照；当前 `release/pkg` **不可部署**，正式产物必须由已固定 commit 的源码构建重新生成。 |

## 快速上手

拿帧 + 推理 + 回注（Python，完整说明见 [docs/guide/README.md](docs/guide/README.md) §2）：

```python
from recamera_ext import FrameSource, ResultSink

with FrameSource() as src, ResultSink(source_id="my-app") as sink:
    for frame in src:                          # frame.array: 零拷贝帧视图
        boxes = my_model(frame.array)          # -> [(x1,y1,x2,y2,score,label), ...] 归一化 [0,1]
        sink.send_detections(pts_us=frame.pts_us, boxes=boxes)
```

坐标一律为归一化 `[0,1]`（相对画面宽高的比例）。更多示例见 [`examples/`](examples/)。

## 安装 / 环境

通过当前顶层源码构建生成的固件会把 native library 安装到 `/usr/lib`，把
Python 包安装到 Python 3.11 的系统 site-packages，正常无需手工设置搜索路径。
只有调试历史 `/userdata/sdk` 手工副本时才需要：

```sh
export PYTHONPATH=/userdata/sdk/python:$PYTHONPATH
export LD_LIBRARY_PATH=/userdata/sdk/lib:$LD_LIBRARY_PATH
```

固件集成应走本 SDK 的顶层 `project/app` 源码构建；当前仓内
[`release/pkg`](release/pkg/) 是校验值互相矛盾、且不含新 NPU broker 的历史
快照，`install.sh` 已 fail-closed，**不要 sideload**。前置条件（socket 权限、
握手）见 [docs/guide/README.md](docs/guide/README.md)；发布边界见
[docs/guide/deploy-ops.md](docs/guide/deploy-ops.md)。

## 发布物 / Release

`release/` 下保留的包仅供历史追溯：

| 包 | 用途 | 说明 |
|---|---|---|
| `recamera-ext-api-v*.tar` / [`release/pkg`](release/pkg/) | **已禁用的历史 sideload** | 二进制、manifest 与安装脚本不一致，且缺 `inference-control@1`；不可安装。 |
| `recamera-ext-kit-v*.tar.gz` | 历史 kit 分享包 | 不代表当前 Python 1.4 API/native/server 的一致发布集。 |

新的 release 必须从 manifest 固定的 `recamera_ipc`、Vigil、本仓和 Web backend
源码构建，生成统一 BOM/hash 后再启用安装。现有
[`release/build-release.sh`](release/build-release.sh) 仍属于 legacy artifact
assembler，不能作为“源码可复现”证明。

## 版本

两条版本轴，勿混淆：

- **产品发布 train**：当前工作树尚未生成可部署的新 train；历史 v1.x 包已归档禁用。
- **Python distribution**：`recamera-ext 1.4.0`，包含 broker-backed `InferenceLease`。
- **native SDK / C ABI**：`sdk/VERSION` 为 **1.3.0**，soname 仍为
  `librecamera_ext.so.1`；能力为 `frame@1 / result@1 / probe@1 /
  inference-control@1`。Python 包版本与 C ABI/SONAME 是不同版本轴。

## License

[Apache License 2.0](LICENSE)。

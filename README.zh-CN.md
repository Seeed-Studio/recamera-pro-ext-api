# reCamera Pro 扩展 API

<div align="center">

**基于瑞芯微 Rockchip RV1126B（3 TOPS NPU）的高性能模块化边缘 AI 摄像头平台**

*无需为每个应用重编固件，以独立用户态进程构建、运行和扩展端侧视觉 AI 应用。*

[English](README.md) | **简体中文**

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![C ABI](https://img.shields.io/badge/C%20ABI-1.5.0-green.svg)](sdk/VERSION)
[![Python](https://img.shields.io/badge/Python-3.11-blue.svg)](sdk/python)
[![SoC](https://img.shields.io/badge/SoC-Rockchip%20RV1126B-orange.svg)](https://www.seeedstudio.com/reCamera-Pro-2GB.html)
[![NPU](https://img.shields.io/badge/NPU-3.0%20TOPS-red.svg)](https://www.seeedstudio.com/reCamera-Pro-2GB.html)
[![Docs](https://img.shields.io/badge/Docs-Wiki-brightgreen.svg)](https://wiki.seeedstudio.com/recamera_pro_getting_started/)
[![Community](https://img.shields.io/discord/862602258452578314?label=Discord&logo=discord&color=7289da)](https://discord.gg/agHdBMYGZK)

</div>

---

**reCamera Pro**（瑞芯微 RV1126B / `recamera_v2`）扩展 API 面向方案集成商、边缘算法开发者与行业用户：设备先安装包含扩展端点且与 SDK 协议匹配的固件；此后方案商无需为每个 AI 应用重编系统固件，可将算法作为独立用户态进程运行。

应用程序通过 `/run/recamera/` 下的专用 Unix domain socket 与底层固件及服务通信，完成零拷贝原始帧获取、硬件 NPU 独占租约协商、检测与分类结果回注（OSD/通知）、以及流水线张量实时观测。**进程边界即契约。**

---

## 🛒 硬件设备与官方配件

本仓库专属于 **reCamera Pro**。搭载高性能瑞芯微 RV1126B SoC，具备 3.0 TOPS 端侧 NPU 算力、4K 星光级成像与全套工业级接口，专为企业级边缘 AI 视觉系统打造。

### 🌟 硬件设备：reCamera Pro 2GB

| 项目 | 参数详情 |
|------|---------|
| **产品型号** | **reCamera Pro 2GB** 开源边缘 AI 摄像头 |
| **主控芯片（SoC）** | 瑞芯微 Rockchip RV1126B（四核 ARM Cortex-A53 @ 1.5 GHz） |
| **NPU 算力加速** | **3.0 TOPS NPU**（支持 INT8 / INT16 混合精度），端侧流畅运行 CV、VLM、LLM 及语音 AI 模型 |
| **内存与存储** | 2GB LPDDR4 运行内存，16GB eMMC 5.1 高速板载闪存 |
| **图像传感器** | 800 万超高清星光级 CMOS（支持最高 3840×2160 @ 30 FPS） |
| **出厂镜头** | 标准可更换 M12 卡口，123° ± 5° 对角视场角（DFOV），F2.4 光圈 |
| **音频系统** | 板载双麦克风阵列 + NS4150B D类功放（支持外接 8Ω / 1W 扬声器，具备双向语音对讲能力） |
| **显示接口** | 板载 MIPI DSI 接口，支持外接触摸屏与实时预览监视器 |
| **工业接口与传感器** | 千兆以太网口（支持 PoE 供电）、Wi-Fi/蓝牙、CAN 总线、调试串口（UART）、扩展 GPIO、板载 6 轴 IMU 振动感知 |
| **多模供电能力** | DC 12V/2A 圆口、标准工业级 PoE（IEEE 802.3af/at）、USB Type-C、7.4V 锂电池接口 |
| **标配包装清单** | reCamera Pro 主机 × 1、全球多规格 12V 电源适配器 × 1、USB Type-C 数据线 × 1 |
| **官方售价** | **$299.90** |
| **购买与指南** | [🛒 **前往 Seeed 官方商城购买**](https://www.seeedstudio.com/reCamera-Pro-2GB.html) \| [📖 **快速入门开发指南**](https://wiki.seeedstudio.com/recamera_pro_getting_started/) |

### 🔍 官方认证 M12 可更换镜头选件

reCamera Pro 采用工业标准 **M12 可更换镜头卡口**，可根据安装高度与场景距离选配不同焦段的认证光学镜头：

- **1/2.9” M12 长焦镜头（500 万像素，20° 视场角）**
  专为远距离周界安防、车牌识别（LPR）与长走廊过道监控设计，提供高像素密度与视场放大倍率。
  👉 [Seeed Bazaar 官方购买 ($11.90)](https://www.seeedstudio.com/5-p-6645.html)

- **1/2.9” M12 超长焦镜头（200 万像素，15° 视场角）**
  提供更极致的长焦放大倍率，适用于远距离设备仪表读数、工业微距外观缺陷检测等专业场景。
  👉 [Seeed Bazaar 官方购买 ($13.90)](https://www.seeedstudio.com/22mm-B-p-6646.html)

---

## ⚡ 扩展 API 架构：六条 Socket 端点

应用通过 `/run/recamera/` 下六条 Unix domain socket 与固件服务解耦交互：

| 端点名称 | Socket 路径 | 核心作用与契约 |
|---------|------------|---------------|
| **帧代理** | `/run/recamera/frame.sock` | 零拷贝获取相机全分辨率 NV12 原始帧（共享内存），供应用自行推理。 |
| **结果注入** | `/run/recamera/result-in.sock` | 将应用推理结果回注平台 OSD、通知及公共结果流（不直接触发录像）。 |
| **平台 OSD** | `/run/recamera/osd-in.sock` | `appmgr` 专用；仅将已安装并授权的应用检测/分类结果绘制到视频流。 |
| **录像触发** | `/run/recamera/record-in.sock` | `appmgr` 专用；将 manifest 声明的应用检测事件交由 Vigil 录像规则系统。 |
| **观测面** | `/run/recamera/probe.sock` | 实时采样内置推理流水线各阶段的张量数据及性能指标。 |
| **NPU 仲裁** | `/run/recamera/inference-control.sock` | 在内置模型完全停妥后，授予外部应用单一独占 NPU 租约（生命周期与连接绑定）。 |

---

## 🚀 快速上手

以 Python 编写的完整“取帧 + 推理 + 结果回注”流水线（详见 [docs/guide/README.md](docs/guide/README.md) §2）：

```python
from recamera_ext import FrameSource, ResultSink

# 建立帧输入流与结果回注通道
with FrameSource() as src, ResultSink(source_id="my-app") as sink:
    for frame in src:
        # frame.array 包含 Y 平面（亮度分量）的零拷贝 NumPy 数组视图
        # 若模型需要完整彩色图像，可使用 frame.to_bgr() 或 frame.plane_array(i)
        boxes = my_model(frame.array)  # -> [(x1, y1, x2, y2, score, label), ...]

        # 注入目标检测框，坐标必须归一化到 [0, 1] 区间
        sink.send_detections(pts_us=frame.pts_us, boxes=boxes)
```

> [!IMPORTANT]
> **坐标归一化契约**：传给底层 `ResultSink` 的所有边界框坐标 `(x1, y1, x2, y2)` **必须归一化到 `[0, 1]`**（即相对于图像宽高的百分比）。若传递绝对像素坐标，OSD 会将其 clamp 为 1 像素隐形框。如果模型输出为像素坐标，请务必除以图像宽度和高度后再注入。

更多单文件最小用例参见 [`examples/`](examples/) 目录。

---

## 📂 仓库目录导航

| 目录 | 说明 |
|------|------|
| [`docs/api/`](docs/api/) | 规格与架构：[`spec.md`](docs/api/spec.md)（Socket 路径 / Protobuf 协议定义 / ABI 版本）、[`architecture.md`](docs/api/architecture.md)（系统分层与数据流）。 |
| [`docs/guide/`](docs/guide/) | [开发者综合指南](docs/guide/README.md) 及各专题手册（控制 API、结果推送、GPIO 控制、音频 PCM、前端扩展、FFmpeg / GStreamer 集成、应用中心上架、部署运维、Kit 设计、适配器路径、语音应用开发等）；AI 结果叠加样式自定义见 [`overlay-customization.md`](docs/guide/overlay-customization.md)，交接说明见 [`overlay-customization-handoff.md`](docs/guide/overlay-customization-handoff.md)。 |
| [`sdk/`](sdk/) | 设备侧基础 SDK：C ABI 动态库 `librecamera_ext.so.1`、Python 封装包 `recamera_ext`（基于 ctypes 薄封装）及 C/C++ 头文件。 |
| [`kit/`](kit/) | 可复用 Python 推理套件：L0 适配器层、前处理/后处理 runtime 库、业务逻辑 logic 库、应用主循环基类 `app.py`。 |
| [`apps/`](apps/) | 丰富生产级示例应用（yolo-detector、face-analysis、fall-detection、voice-transcribe 等）及 [Inference Workflows](apps/inference-rv1126b/README.md)。 |
| [`examples/`](examples/) | SDK 最小用法示例（取帧、结果注入、帧→推理→OSD 全流程、GPIO 触发联动、C ABI 访问、浏览器叠加层自由绘制 `11-overlay-geometry`）。 |
| [`market/`](market/) | 应用中心控制面、推理调度管理服务、manifest-v2 打包与应用商店 catalog 工具。 |
| [`release/`](release/) | 历史交付归档；注意当前 `release/pkg` **不可部署**，正式生产产物须通过固定 commit 的统一源码构建生成。 |

---

## 🛠️ 安装与运行环境

通过顶层统一源码构建生成的官方固件已将 native 动态库安装至 `/usr/lib`，并将 Python 模块部署在系统 Python 3.11 `site-packages` 下，正常无需手工配置环境变量。

仅在调试历史 `/userdata/sdk` 手工拷贝目录时才需手动指定路径：

```sh
export PYTHONPATH=/userdata/sdk/python:$PYTHONPATH
export LD_LIBRARY_PATH=/userdata/sdk/lib:$LD_LIBRARY_PATH
```

> [!WARNING]
> 固件集成请使用本 SDK 顶层 `project/app` 源码构建。仓库现存的 [`release/pkg`](release/pkg/) 属于历史快照，存在校验哈希不一致且未集成新版 NPU broker，其 `install.sh` 已 fail-closed 禁用，**切勿尝试 sideload 安装**。前置条件与验证请阅读 [docs/guide/README.md](docs/guide/README.md) 与 [docs/guide/deploy-ops.md](docs/guide/deploy-ops.md)。

---

## 📦 发布交付物说明

`release/` 目录保留的压缩包仅供追溯参考：

| 归档包名 | 原始用途 | 现状与安全说明 |
|---------|---------|--------------|
| `recamera-ext-api-v*.tar` / [`release/pkg`](release/pkg/) | 历史 sideload 安装包 | **已禁用**：二进制、manifest 与安装脚本存在差异，且缺少 `inference-control@1` 端点，严禁安装。 |
| `recamera-ext-kit-v*.tar.gz` | 历史 kit 分享包 | **仅供历史参考**：不代表当前 Python 1.4 API、native client 及服务端的完整一致发布集。 |

新版本发布必须固定 `recamera_ipc`、Vigil、本仓库及 Web 后端源码 commit，从源码构建生成统一物料清单（BOM）及哈希校验后方可分发。仓库内现存的 [`release/build-release.sh`](release/build-release.sh) 仅为遗留组装脚本，不作为可复现构建依据。

---

## 🏷️ 版本划分机制

请明确区分以下两条独立版本轴：

- **产品发布版本（Release Train）**：当前源码工作树尚未产出新 train；历史 v1.x 包已封存停用。
- **Python Distribution 版本**：`recamera-ext 1.5.0`，包含支持 broker 的 `InferenceLease` 及仅供 appmgr 使用的 `OsdSink` / `RecordSink`。
- **Native SDK / C ABI 版本**：`sdk/VERSION` 为 **1.5.0**，动态库 SONAME 保持为 `librecamera_ext.so.1`；具备 `frame@1 / result@1 / osd@1 / record@1 / probe@1 / inference-control@1` 契约能力。

---

## 🤝 开发者社区与支持

- **官方知识库**: [Seeed Studio Wiki 指南](https://wiki.seeedstudio.com/recamera_pro_getting_started/)
- **技术论坛**: [Seeed Studio 官方开发者论坛](https://forum.seeedstudio.com/)
- **开发者交流群**: [Discord 社区](https://discord.gg/agHdBMYGZK)
- **问题反馈**: 如有缺陷或功能建议，欢迎在 GitHub Issues 提交

---

## 📄 开源许可证

除 [`sdk/NOTICE`](sdk/NOTICE) 明确注明的 native client 源码文件外，本仓库遵循 [Apache License 2.0](LICENSE) 协议。native client 保留 [BSD-3-Clause](sdk/LICENSE) 许可证。关于混合开源许可的完整说明请查阅 [`NOTICE`](NOTICE)。

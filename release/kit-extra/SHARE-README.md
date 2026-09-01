# reCamera Pro 扩展 API — kit + SDK 分享包 (v1.2.0)

面向已经刷好**扩展 API 固件**的 reCamera Pro（RV1126B / recamera_v2）。装完即可在设备上跑自己的进程：拿摄像头帧、把结果回注 OSD/RTSP/录像/推送、用结果驱动 GPIO。**不含任何固件**，也不改固件。

## 包内容

| 目录/文件 | 说明 |
|---|---|
| `kit/` | 共享运行时（L0 适配层 + 通用主循环 + 后处理/追踪/区域等 logic）。装到 `/userdata/local/kit` |
| `sdk/` | librecamera_ext SDK 1.2.0：`include/recamera_ext.h` + `python/recamera_ext` + `lib/librecamera_ext.so*`（含软链）+ `VERSION` + `README.md`。装到 `/userdata/sdk` |
| `examples/` | SDK 用法示例（01 取帧 / 02 注入结果 / 03 取帧→算法→OSD / 04 GPIO / 05 C ABI / 06 probe 观测） |
| `INSTALL.sh` | 一键安装器（幂等 + 备份旧版） |

## ① 前提：设备已装扩展 API 固件

本包**不含固件**，且本目录本身也是早于当前 Python 1.4/broker 契约的历史
分享快照。当前设备不仅要有 frame/result/probe，还必须有版本匹配的 NPU
broker，并完成真实握手（仅检查 inode 不足以证明兼容）：

```sh
ls -l /run/recamera/frame.sock /run/recamera/result-in.sock \
  /run/recamera/probe.sock /run/recamera/inference-control.sock
```

不要再刷历史 `recamera-ext-api-v1.2.0.tar`。应从 manifest 固定的当前源码
构建并安装完整固件，再使用与该固件同一发布 train 的 SDK/Python 包。

## ② 安装

把整个目录拷到设备（如 `/userdata/tmp/recamera-ext-kit-v1.2.0`），然后：

```sh
sh INSTALL.sh
```

安装器会：
- kit → `/userdata/local/kit`（这样 `/userdata/local` 在 `sys.path`，`import kit` 生效）
- sdk → `/userdata/sdk`（python 包、`.so` + 软链、头文件、VERSION）
- 已存在的旧 kit/sdk 会先备份成 `*.bak.<时间戳>`，重复运行安全

## ③ 设置环境变量

```sh
export PYTHONPATH=/userdata/local:/userdata/sdk/python
export LD_LIBRARY_PATH=/userdata/sdk/lib:/oem/usr/lib:/usr/lib:$LD_LIBRARY_PATH
```

- `PYTHONPATH` 里 `/userdata/local` 让 `import kit` 找到共享 kit；`/userdata/sdk/python` 让 `import recamera_ext` 找到 SDK。
- `LD_LIBRARY_PATH` 里 `/userdata/sdk/lib` 让 SDK 加载到 `librecamera_ext.so.1`。

## ④ 跑烟雾 demo

```sh
cd examples/02-inject-result
python3 inject_result.py --task detection
```

然后在 RTSP（`rtsp://<设备IP>:8554/...`）或 WS（`127.0.0.1:8123 /ws/inference/results`）里应看到持续注入的框。取帧最小示例见 `examples/01-hello-frame`。

## ⑤ 坐标契约（务必归一化 [0,1]）

所有 box 坐标（检测/分类 ROI/分割 ROI/跟踪/关键点对象框）以及关键点 point 的 x/y 均为**归一化 [0,1]**（相对画面宽高的比例，左上 x1/y1、右下 x2/y2，0.5=居中）。OSD 会先 clamp 到 [0,1] 再乘画面宽高——**传像素值会被压成 1px 隐形框**。手头是像素坐标就除以画面宽/高。分割 mask 是行主序原始字节（非坐标）。

限速：每连接 60 msg/s（burst 15），单条 payload ≤ 64KB；`source_id` 不能用保留字 `builtin`。

## ⑥ 开发指南

- 概念/约束总入口：项目仓 `recamera_pro/docs/guide/README.md`（及各分篇：kit 设计、适配层、GPIO、音频、前端扩展等）。本分享包不含 docs/，开发指南在 recamera_pro 仓获取
- C ABI 事实来源：`sdk/include/recamera_ext.h`
- Python 封装事实来源：`sdk/python/recamera_ext/__init__.py`
- 生产级适配层范本：`kit/adapters/official.py`（`OfficialFrameSource` / `OfficialResultSink`，socket 在就用官方 API，不在回退 RTSP/WS）

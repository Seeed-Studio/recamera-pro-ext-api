# Changelog

本文件记录 reCamera Pro 扩展 API 与 SDK 面向用户的版本变更，格式遵循
[Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，版本号遵循语义化版本。

SDK soname 为 `librecamera_ext.so.1`；API 版本为 `frame@1 / result@1 / probe@1 /
inference-control@1`。
除非另有说明，各次发布均保持 ABI 向后兼容（新增符号，soname 不变）。

## [1.3.0] - 2026-08-19

### 新增
- **NPU 单 owner broker client**：新增 `rc_ext_inference_lease_open/ready/status/alive/
  set_fallback/close/abandon_after_fork`，连接
  `/run/recamera/inference-control.sock`。rkipc 只在内建 RKNN handle 真正销毁且
  `state=stopped`、`actual_fps=0` 后授予连接生命周期 lease。
- lease 状态包含 `lease_id`、`epoch`、`generation` 和 authoritative builtin
  状态；连接 HUP 自动回收，fork child 可 close-only abandon，不误发 RELEASE。
- Python distribution `recamera-ext 1.4.0` 新增 typed `InferenceLease`；高层
  `ExternalNpuLease`/`RknnSession` 默认使用 broker，并在 broker-required 模式下
  fail closed，不回退 legacy flock。

### 兼容性
- 仅新增 C 符号和 wire capability，`librecamera_ext.so.1` SONAME 不变。
- 当前 generation 保护 NPU 控制租约；frame/result/probe 数据连接绑定留给下一
  协议版本。目标板 kill/restart/OTA 矩阵仍是正式发布门禁。

## [1.2.0]

### 新增
- **观测面 client（ProbeSource / `rc_ext_probe_*`）**：连接 `/run/recamera/probe.sock`，
  可订阅 `preproc.out` / `npu.raw` / `postproc.out` / `metrics` 各级张量。小样本走 inline
  payload，大张量经 memfd（`SCM_RIGHTS`）传递。纯新增符号，对旧消费者 ABI 向后兼容。

### 变更
- **坐标契约归一化**：明确所有 box 坐标（检测 / 分类 ROI / 分割 ROI / 跟踪 / 关键点对象框）
  及关键点 point 的 `x/y` 均为归一化 `[0,1]`（相对画面宽高的比例）。header 注释、Python
  docstring 与示例已统一。纯文档修正，不改 ABI、不重编 `.so`。
- **CgiControl 控制适配器**：经 entry.cgi HTTP API 做配置 / 控制，`/var/tmp/rkipc` 内部
  接口不直连。

### 修复
- **RGA / letterbox 相关修复**：letterbox padding 填充值对齐（`114` → `0x727272`），修正此前
  按像素坐标发框被压成 1px 隐形框的问题。

## [1.1.0]

### 新增
- **分类结果可选 ROI box**：`rc_ext_class_t` 增加 `has_box` + `x1/y1/x2/y2`。`has_box=0`
  时保持原无框行为，向后兼容，soname 不变。

## [1.0.0]

### 新增
- **帧代理**（`/run/recamera/frame.sock`）：零拷贝 dma-buf 拿相机原始帧。
- **结果注入**（`/run/recamera/result-in.sock`）：把外部推理结果回注官方 OSD / 录像 / WS
  三路分发，与固件内建推理共用同一条分发链。
- **观测面**（`/run/recamera/probe.sock`）：内建推理流水线各级张量采样。
- **初版 SDK**：C 库 `librecamera_ext.so.1` + Python 封装 `recamera_ext`（ctypes，运行时
  加载同一 `.so`）；结果发送覆盖 detections / classification / segmentation / tracking /
  keypoints。

[1.3.0]: #130
[1.2.0]: #120
[1.1.0]: #110
[1.0.0]: #100

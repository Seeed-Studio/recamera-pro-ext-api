# reCamera Pro 应用开发与发版流程指南

> 本指南说明 reCamera Pro 应用从本地开发、测试到最终发布至应用商店的标准流程。
> 目前发版已接入 GitHub 自动化流水线，**开发者无需向运维申请云权限，也无需配置签名私钥**。

---

## 一、应用开发与本地测试

### 1. 目录结构规范
每个应用统一放置在 `apps/<app_id>/` 目录下：
```
apps/<app_id>/
├── manifest.json     # 必需：应用配置声明（Manifest v2）
├── app.py            # 必需：应用入口，继承 kit.app.App
├── icon.png          # 推荐：应用卡片图标（≤1MB，PNG/WebP/JPG）
└── models/           # 可选：随包分发的模型文件（*.rknn）
```

### 2. Manifest v2 规范模板
`manifest.json` 必须满足以下标准格式：
```json
{
  "manifest_version": 2,
  "id": "yolo-detector",
  "name": "YOLO Detector",
  "name_zh": "YOLO 目标检测",
  "version": "1.0.1",
  "type": "self-hosted",
  "entry": "app.py",
  "release": { "sequence": 1, "channel": "stable" },
  "compatibility": {
    "platform_profile": "rv1126b-linux-gnu-cp311-rknn232-v1",
    "arch": "aarch64",
    "python": "==3.11.*",
    "kit_api": ">=0.2,<0.3"
  },
  "icon": { "path": "icon.png", "media_type": "image/png" },
  "models": [
    { "id": "det", "file": "models/yolov8n.rknn", "task": "detect" }
  ],
  "resources": {
    "claims": ["camera.frames", "npu.rknn", "result.publish"]
  },
  "permissions": {
    "sdk": ["frames", "results"],
    "filesystem": { "read": ["app"], "write": ["appdata", "tmp"] },
    "network": { "listen": [], "outbound": [] }
  },
  "health": {
    "protocol": "kit-health-v1",
    "startup_timeout_sec": 30
  },
  "config_schema": {
    "revision": 1,
    "groups": []
  }
}
```

### 3. 本地打包与免密测试
1. **本地打包**：
   ```bash
   python3 market/packaging/build.py apps/<app_id> --out dist/
   ```
   将在 `dist/` 下生成 `<app_id>-<version>-arm64.tar.gz`。
2. **免密安装测试**：
   - 浏览器打开设备后台：`http://<设备IP>/` 登录；
   - 进入「应用中心」-> 点击「上传安装」，上传刚才打好的 `.tar.gz` 包；
   - 安装完成后点击启动，验证推理、OSD 显示及配置项是否正常。

---

## 二、应用上架发布机制（CI 自动识别）

本地测试完成后，提交代码并合入 `main` 分支。**CI 会自动判断是否需要发版，以及发哪个应用**。

### 1. 什么时候会自动发版？
当且仅当满足以下条件时，CI 会自动发版：
1. **改动了 `apps/<app_id>/` 目录**；
2. **且在 `apps/<app_id>/manifest.json` 中递增了 `version` 版本号**（例如从 `1.0.0` 递增到 `1.0.1`）。

> **多应用同时发布支持**：如果一次提交同时修改了 3 个应用，并且这 3 个应用都在 `manifest.json` 里递增了版本号，CI 会自动识别并**逐个打包、逐个增量发布到云端商店**。

### 2. 什么时候绝对不会触发发版？
1. **底层或文档提交**：修改了 `sdk/`、`kit/`、`docs/`、根目录配置等非 `apps/` 目录时，**CI 完全不触发，执行次数为 0**。
2. **日常代码修改但未改版本号**：如果只修改了 `apps/<app_id>/app.py` 的代码逻辑，但**没有修改 `manifest.json` 里的 `version`**，CI 会自动对比线上已发布的版本，识别为“日常开发提交”，**直接跳过发版（Skip）**。

### 3. 备用方式：打 Tag 精准发版
如果你想显式、指定时间点发布某个应用，直接打 Tag 即可（CI 只会处理该 Tag 指定的应用）：
```bash
# 格式：release/<app_id>-v<version>
git tag release/yolo-detector-v1.0.1
git push origin release/yolo-detector-v1.0.1
```

---

## 三、开发避坑要点

1. **坐标必须归一化**：
   所有检测框、ROI 坐标必须传 `[0.0, 1.0]` 比例。如果传像素绝对值，会被系统截断为 1px 的隐形框。
2. **切勿打包平台库**：
   固件已自带 `kit` 套件及 `rknnlite`/`numpy`/`cv2` 等基础库，**切勿将 `kit/` 源码打入应用包**。
3. **图标文件要求**：
   图标必须为 PNG、WebP 或 JPEG 格式，文件大小 ≤ 1 MiB。扩展名与文件真实魔数必须一致，禁止 SVG。

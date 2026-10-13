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
    "kit_api": ">=0.2,<1"
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

### 3. 模型与大资源文件（免上传 Git）
- **模型文件不随 Git 仓库提交**（`.gitignore` 已自动排除 `*.rknn` / `*.onnx` / `*.whl` 等大文件）。
- **云端模型池自动对齐**：CI 流水线打包时会自动从官方模型池拉取所需模型，开发者无需将模型提交到 Git。
- **自定义模型链接（可选）**：如果应用使用了外部自训练模型，可在 `manifest.json` 中配置 `"x-url"`（公网可直接下载的直链，支持 HuggingFace、GitHub Releases、自建 OSS 等）：
  ```json
  "models": [
    {
      "id": "det",
      "file": "models/my_model.rknn",
      "task": "detect",
      "x-url": "https://huggingface.co/my-org/my-model/resolve/main/my_model.rknn"
    }
  ]
  ```
- **本地快速同步**：若本地开发缺少模型，可一键同步：
  ```bash
  python3 market/packaging/fetch_assets.py apps/<app_id>
  ```

### 4. 本地打包与设备端测试（Owner Key 信任机制）
设备端 AppMgr 默认采用签名验证机制。为了实现本地开发与旁加载（Sideloading），推荐使用 **Owner Key（开发者所有者密钥）** 进行本地签名与安装：

1. **生成本地开发私钥与公钥（仅需一次）**：
   ```bash
   openssl ecparam -genkey -name prime256v1 -noout -out owner_priv.pem
   openssl ec -in owner_priv.pem -pubout -out owner_pub.pem
   ```
2. **在测试设备上登记 Owner 公钥（每台设备仅需一次）**：
   ```bash
   ssh root@<设备IP> "mkdir -p /userdata/local/appmgr/keys/owners && chmod 755 /userdata/local/appmgr/keys/owners"
   scp owner_pub.pem root@<设备IP>:/userdata/local/appmgr/keys/owners/<developer_name>.pem
   ssh root@<设备IP> "chmod 644 /userdata/local/appmgr/keys/owners/<developer_name>.pem"
   ```
   > 登记完成后，设备将永久信任由该私钥签署的所有应用包。

3. **本地打包与签名**：
   ```bash
   # 1. 确定性打包
   python3 market/packaging/build.py apps/<app_id> --out dist/

   # 2. 用本地 Owner 私钥签名
   python3 market/packaging/sign.py --dist dist/ --pkg <app_id>-<version>-arm64.tar.gz --key owner_priv.pem
   ```
4. **上传安装测试**：
   - **方式一：通过 Web 后台上传（推荐）**
     浏览器登录设备后台 `http://<设备IP>/`，进入「应用中心」-> 点击「上传安装」，选择刚才生成的 `.tar.gz` 安装包及配套的 `.tar.gz.sig` 签名文件。Web 界面会自动完成上传预检（Preflight）并引导安装。
   - **方式二：通过 REST API 自动化上传并安装**
     API 采用安全的流式上传预检与显式权限确认机制：
     ```bash
     # 1. 上传包与分离签名，获取 upload_id 及预检权限声明
     UPLOAD_RESP=$(curl -s -X POST http://<设备IP>/api/app-center/v1/uploads \
       -H "Authorization: Bearer <TOKEN>" \
       -F "package=@dist/<app_id>-<version>-arm64.tar.gz" \
       -F "signature=@dist/<app_id>-<version>-arm64.tar.gz.sig")

     UPLOAD_ID=$(echo "$UPLOAD_RESP" | jq -r .upload_id)
     PERMS=$(echo "$UPLOAD_RESP" | jq .preflight.manifest.permissions)

     # 2. 显式确认权限并提交安装（如需升级/重装，可按需添加 "running_upgrade_confirmed": true 等）
     curl -s -X POST http://<设备IP>/api/app-center/v1/apps \
       -H "Authorization: Bearer <TOKEN>" \
       -H "Content-Type: application/json" \
       -d "$(jq -n \
         --arg uid "$UPLOAD_ID" \
         --argjson perms "$PERMS" \
         '{upload_id: $uid, permissions_confirmed: true, permissions: $perms}')"
     ```
   - **方式三：通过 SSH 本地 CLI 直接安装（开发调试推荐）**
     将包及签名传输至设备，直接调用设备上安装的 `appmgr` 模块：
     ```bash
     scp dist/<app_id>-<version>-arm64.tar.gz* root@<设备IP>:/tmp/
     ssh root@<设备IP> "PYTHONPATH=/usr/lib/recamera python3 -m appmgr install /tmp/<app_id>-<version>-arm64.tar.gz"
     ```
   - 安装完成后启动应用，验证功能、推理及配置项。

---

## 二、源码应用上架发布机制（CI 自动识别）

本地测试完成后，提交代码并合入 `main` 分支。**CI 会自动判断是否需要发版，以及发哪个应用**。

### 1. 什么时候会自动发版？
当且仅当满足以下条件时，CI 会自动发版：
1. **改动了 `apps/<app_id>/` 目录**；
2. **且在 `apps/<app_id>/manifest.json` 中递增了 `version` 版本号**（例如从 `1.0.0` 递增到 `1.0.1`）。

> **多应用同时发布支持**：如果一次提交同时修改了多个应用且均递增了版本号，CI 会自动识别并**逐个打包、逐个增量发布到云端商店**。

### 2. 什么时候绝对不会触发发版？
1. **底层或文档提交**：修改了 `sdk/`、`kit/`、`docs/`、根目录配置等非 `apps/` 目录时，**CI 完全不触发，执行次数为 0**。
2. **日常代码修改但未改版本号**：如果只修改了代码逻辑但未修改 `version`，CI 会自动对比线上已发布版本并**直接跳过发版（Skip）**。

### 3. 备用方式：手动精准发布（打 Tag 或网页点击）
- **打 Tag 发布**：
  ```bash
  # 格式：release/<app_id>-v<version>
  git tag release/yolo-detector-v1.0.1
  git push origin release/yolo-detector-v1.0.1
  ```
- **网页一键发布**：
  进入仓库 [Actions 页面](https://github.com/Seeed-Studio/recamera-pro-ext-api/actions/workflows/publish-app.yml)，选择 **Publish App to CDN Store** -> 点击 **Run workflow** -> 输入应用名即可运行。

### 4. 发布结果验证
- **查看日志**：进入 [GitHub Actions 列表](https://github.com/Seeed-Studio/recamera-pro-ext-api/actions) 查看实时流水线进度。
- **设备生效**：流水线成功后（约 20 秒），设备端打开或刷新「应用中心」即可看到并安装最新版本。

---

## 三、外部构建应用（External Build）的打包与上架

适用于具有复杂外部构建流程的应用（例如前端经 `bun/vite` 编译、自带大量 PyPI aarch64 预编译轮子、依赖 Git Submodules 等独立组件的复杂 AI Agent 应用，如 `nanobot` 等）。

### 1. 外部打包与 Manifest 规范
外部流水线完成前端构建与轮子解析后，产物需满足标准 Manifest v2 安装包规范：
1. **包命名规范**：最终打包为 `<app_id>-<version>-arm64.tar.gz`。
2. **Manifest 依赖声明**：
   - `python.wheels[]`：若携带额外 Python 依赖，需在 `manifest.json` 中声明每个 wheel 文件名、sha256 与 size（轮子需放于 `wheels/` 目录，适配 `cp311-manylinux_2_36_aarch64` 等目标架构）；
   - `artifacts[]`：若携带模型或只读数据，在 manifest 中声明；
   - `icon.png`：应用卡片图标（≤1MB，PNG/WebP/JPG）；
   - `release.lock.json` 与 `files.sha256` (BOM)：包内完整性凭据。
   > **推荐工具**：在外部构建目录组装完毕后，直接调用官方 `python3 market/packaging/build.py <assembled_dir> --out dist/`，该工具会自动校验规范并生成标准的 BOM 与 release lock。

### 2. 双层信任模型
- **本地测试 / 个人旁加载**：使用前述 **Owner Key** 签名（`sign.py --dist dist/ --pkg <pkg> --key owner_priv.pem`），在登记过公钥的设备上直接分发运行。
- **应用商店（App Center）发布**：官方应用商店作为公开信任锚点，统一由 Seeed 官方的 **Vendor Release Key（厂商密钥）** 重新签名，并在官方 CDN 的 `catalog.json` 目录中建立全局索引。外部开发者无需持有厂商私钥。

### 3. CI 预构建包发布途径
在官方 CI 中已开通外部预构建包的直推通道：
1. 外部开发者完成打包并发布至 GitHub Releases、S3 等公网可下载地址；
2. 进入仓库 [Publish App to CDN Store 流水线](https://github.com/Seeed-Studio/recamera-pro-ext-api/actions/workflows/publish-app.yml)，点击 **Run workflow**；
3. 在 **`package_url`** 输入框中填入预构建包直链（例如 `https://github.com/<owner>/<repo>/releases/download/vX.Y.Z/<app_id>-<version>-arm64.tar.gz`）；
4. 流水线将自动拉取外部安装包，执行 Manifest 完整性与图标规范校验，由 Seeed 厂商密钥重新签名，并自动更新发布到官方应用商店 CDN。

---

## 四、开发避坑要点

1. **坐标必须归一化**：
   所有检测框、ROI 坐标必须传 `[0.0, 1.0]` 比例。如果传像素绝对值，会被系统截断为 1px 的隐形框。
2. **切勿打包平台库**：
   固件已自带 `kit` 套件及 `rknnlite`/`numpy`/`cv2` 等基础库，**切勿将 `kit/` 源码打入应用包**。
3. **图标文件要求**：
   图标必须为 PNG、WebP 或 JPEG 格式，文件大小 ≤ 1 MiB。扩展名与文件真实魔数必须一致，禁止 SVG。
4. **NPU 推理调用边界（视觉 vs 流式音频）**：
   - **视觉流水线（Frames -> Infer -> Emit）**：必须使用 Kit 的 Model Factory（`self.models.<id>.infer(...)`），由平台调度服务（`inferenced`）统一管理，严禁在应用进程内裸启 `rknnlite`，以防止与系统视频采集/VPSS 及 OSD 流水线争抢 NPU 驱动和内存。
   - **流式音频与非视觉独立负载（如 VAD / 唤醒词 / ASR 流式识别）**：若应用自管音频环形缓冲、自驱动毫秒级节奏且不使用相机帧（`needs_frames = False`, `needs_model = False`），如需直连本地 RKNN 运行时以规避 IPC 开销，必须满足以下完整契约：
     1. 在 `manifest.json` 中声明独占模式：`"resources.claims": [ {"name": "npu.rknn", "mode": "exclusive"} ]`；
     2. 运行时必须通过 `kit.resources.ExternalNpuLease`（或 `RknnSession`）向 `inference-control.sock` 取得 broker 独占租约并完成 READY 握手（确保固件内建推理已停妥并交接 NPU 所有权）；
     3. 进程退出或异常时必须安全释放 lease 租约与 RKNN 句柄（推荐使用 `kit.runtime.rknnlite.RknnLiteRuntime` 保护层管理生命周期与输出内存回收）。

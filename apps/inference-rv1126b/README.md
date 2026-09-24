# Inference Workflows for RV1126B

应用中心的 Workflow 应用源码。应用入口、配置、图标、应用打包和生命周期测试在本目录维护。
Roboflow Workflow 引擎、RKNN/相机适配、在线画布接入和轻量依赖构建工具在
[mjq2020/inference 的 rv1126b 分支](https://github.com/mjq2020/inference/tree/rv1126b)维护。
`engine-source.lock.json` 固定引擎提交；构建不跟随分支最新版本。

## 构建安装包

在开发机安装 Git、uv，准备已适配 RV1126B 的 RKNN 模型目录。目录结构为
`<model-root>/<model-id>/<version>/model.json` 和元数据指向的 RKNN 文件。
模型契约见引擎仓库 `deploy/rv1126b/MODEL_FORMAT.md`。至少包含一个模型，
用于满足设备 scheduled NPU 的资源授权；用户后续可在应用中心绑定其他模型。

在 ext 仓库根目录执行：

```sh
uv run --python 3.11 apps/inference-rv1126b/build.py \
  --model-root /path/to/model-store \
  --out dist/workflow
```

脚本自动获取固定引擎提交、准备 ARM64 离线依赖和字体，并调用本仓库的官方
manifest-v2 builder。输出 `inference-rv1126b-0.3.9-arm64.tar.gz` 和校验报告。
安装包包含 manifest、`release.lock.json`、`files.sha256`、`engine-source.json`、
应用私有 wheels 和模型，不包含训练模块、PyTorch、ONNX Runtime 或 RKNN 转换工具。
NumPy、OpenCV、Kit、RKNN 和调度服务由固件提供。转换仍通过设备已有的在线转换流程完成。

重复构建可以复用已下载的 ARM64 wheels 与字体：

```sh
uv run --python 3.11 apps/inference-rv1126b/build.py \
  --engine-source /path/to/inference \
  --wheelhouse /path/to/arm64-wheels \
  --fonts-dir /path/to/font-assets \
  --model-root /path/to/model-store \
  --out dist/workflow
```

本地引擎必须处于锁文件指定的提交且无已跟踪文件改动；未跟踪文件不会进入源码导出。
即使复用 wheels，引擎 wheel 也会重新从固定源码生成。
安装包使用应用中心原有的上传、安装、启停流程。开发包不含签名，设备必须允许开发包；
正式分发仍使用发布方的签名流程。

## 默认访问与升级

- 新安装默认监听 `0.0.0.0:9001`，允许局域网浏览器打开编辑画布。
- 访问口令为空时，应用启动前自动生成随机口令并写入持久配置。
  可在「应用配置 → 服务访问 → 访问口令」查看或修改；不会写入安装包或日志。
- 应用卡片「编辑工作流」沿用设备已登录会话换取临时画布凭据，不需要手动复制口令。
- 直接访问端口 9001 的页面/API 仍需访问口令。
- 升级保留工作流、模型绑定和用户配置。已有用户明确保存的 `127.0.0.1` 不会被强制覆盖；
  此时需在应用配置将监听地址改为「局域网访问」并重启一次。
- 引擎自己的命令行服务仍默认仅本机访问；这里的默认值只针对受应用中心管理的应用。

## 开发与测试

修改应用入口/配置时改本目录；修改执行引擎时先提交到引擎仓库，再更新锁文件的完整提交号。
应用版本与引擎 wheel 版本独立，纯应用配置修改无需给引擎重复发版。

应用集成测试需要已准备好引擎测试依赖的 Python 3.11 虚拟环境：

```sh
INFERENCE_ENGINE_ROOT=/path/to/inference \
INFERENCE_RUNTIME_PROFILE=rv1126b \
PYTHONPATH=/path/to/inference:$PWD \
uv run --active --no-project pytest apps/inference-rv1126b/tests
```

测试覆盖默认 LAN 认证、口令持久化、编辑会话、Kit READY/退出、端口冲突、
应用资源/BOM 和固定源码导出。离线检查不替代目标固件的相机和 NPU 验收。

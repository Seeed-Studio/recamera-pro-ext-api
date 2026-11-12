# Inference Workflows for RV1126B

应用中心的 Workflow 应用源码。应用入口、配置、图标、应用打包和生命周期测试在本目录维护。
Roboflow Workflow 引擎、RKNN/相机适配、在线画布接入和轻量依赖构建工具在
[mjq2020/inference 的 rv1126b 分支](https://github.com/mjq2020/inference/tree/rv1126b)维护。
`engine-source.lock.json` 固定引擎提交；构建不跟随分支最新版本。

## 标准构建与发布

本应用直接使用项目统一的 `fetch_assets.py → build.py → publish_app.py` 流程，
CI 不需要识别应用名称、调用专用构建脚本或另行检出引擎仓库。
在仓库根目录执行：

```sh
uv run --frozen python market/packaging/fetch_assets.py apps/inference-rv1126b
uv run --frozen python market/packaging/build.py apps/inference-rv1126b --out dist/workflow
```

输出 `dist/workflow/inference-rv1126b-0.3.11-arm64.tar.gz`。可以在不签名、不上传的
情况下验证最终包：

```sh
uv run --frozen python - <<'PYTHON'
import sys
sys.path.insert(0, "market/packaging")
from publish_app import inspect_package
info = inspect_package("dist/workflow/inference-rv1126b-0.3.11-arm64.tar.gz")
print(info["id"], info["version"], info["members"])
PYTHON
```

`manifest.json` 是完整的发布声明，不再是依赖列表为空的构建模板。它固定了 64 个
ARM64/Python 3.11 兼容 wheel、默认 RKNN 模型、模型元数据和引擎来源记录，包含每个
文件的 SHA-256 与大小。`python.imports` 保留 `inference_edge_probe`；标准构建及发布
检查会确认该模块确实在 wheel 中，scheduled NPU 模型也必须以 RKNN artifact 声明。
设备安装时仍会执行实际导入检查。

资源由通用下载器读取 `x-asset-bundle` 获取。目前使用已发布的完整 `0.3.9` 包作为
固定资产归档，归档本身也按 SHA-256 和大小校验。下载器只取当前 manifest 明确声明的
资源，不复制旧包的应用入口、manifest、BOM 或 release lock；本次入口和配置始终来自
当前源码。模型和 wheel 不提交到 Git，也不依赖开发机已有文件。
**该历史归档仍是构建输入，必须保留在 CDN。** 后续可以改成独立资产归档，只需同步
更新资源及归档的 URL、哈希和大小；不得让固定 URL 指向不同内容。

完整包包含 `manifest.json`、`release.lock.json`、`files.sha256`、`engine-source.json`、
应用私有 wheels 和模型；测试与开发构建脚本由 `package.exclude` 排除。
NumPy、OpenCV、Kit、RKNN 和调度服务仍由固件提供。引擎 wheel 版本保持 `0.3.8`，
对应 `engine-source.lock.json` 固定的提交；应用版本与引擎 wheel 版本独立。

GitHub Actions 发布时选择 `app_id=inference-rv1126b`，执行与其他应用相同的标准流程。
签名和上架仍使用项目原有发布步骤。修复版本为 `0.3.11`、release sequence 为 `16`，
避免覆盖已经发布且内容不可变的 `0.3.10`。

## 更新引擎资产

`build.py` / `build_app.py` 保留用于维护者从固定引擎提交重新构建、审核 wheel 和模型，
不是标准 CI 的入口。需要更新引擎时，先准备兼容的 RKNN 模型目录
`<model-root>/<model-id>/<version>/model.json`，再运行：

```sh
uv run --python 3.11 apps/inference-rv1126b/build.py \
  --model-root /path/to/model-store \
  --out dist/workflow-assets
```

该维护路径会获取锁定提交、准备离线依赖、检查依赖闭包和模型契约。审核并发布资产后，
把新的 `python.wheels`、`artifacts`、`models` 以及固定资产归档信息更新回 manifest，
同时更新引擎来源锁文件。最后必须从干净目录重新运行上面的标准构建命令，
不能只以维护脚本的成功作为标准 CI 可用的证明。

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

标准打包回归不需要引擎仓库，会在普通 CI 和发布前运行：

```sh
uv run --frozen pytest market/packaging/ apps/test_manifest_schema.py
```

运行时集成测试另需已准备好引擎测试依赖的 Python 3.11 虚拟环境：

```sh
INFERENCE_ENGINE_ROOT=/path/to/inference \
INFERENCE_RUNTIME_PROFILE=rv1126b \
PYTHONPATH=/path/to/inference:$PWD \
uv run --active --no-project pytest apps/inference-rv1126b/tests
```

测试覆盖默认 LAN 认证、口令持久化、编辑会话、Kit READY/退出、端口冲突、
应用资源/BOM 和固定源码导出。离线检查不替代目标固件的相机和 NPU 验收。

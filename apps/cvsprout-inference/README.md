# CVSprout Inference

在 reCamera Pro 上运行 CVSprout 训练并转换的检测模型。应用通过 Kit 和 AppMgr 的 scheduled NPU 服务推理，发布原图像素坐标检测框，可在应用中心预览。默认不将框烧录到 RTSP/录像，需用户在设备输出配置中主动启用。

## 使用

1. 在 CVSprout 完成检测模型训练，转换为目标 `rv1126b` 的 RKNN，然后创建设备发布包。
2. 从 CVSprout 下载设备包在应用中心安装，或配置设备连接后由 CVSprout 部署。
3. 启动应用，可调整 `conf`（默认 0.35）和 `iou`（默认 0.45）。两项均支持实时更新。

本目录首次交付包内含实际训练的 **YOLO26n 单类别 person 模型**（CVSprout training 5 / conversion 3），用于验证应用分发通路。它不是针对所有场景验证过的通用生产模型。后续发布应由 CVSprout 替换模型、类别及契约并重新构建整个应用包，不能在设备安装目录中直接替换 RKNN 文件。

## 模型契约

`model_contract.json` 必须与 `manifest.json` 的唯一 `models[]` 条目一致：

- 输入 `uint8 RGB`，形状 `[1, input_size, input_size, 3]`，正方形尺寸为 32 的倍数；letterbox 填充值 114。
- RKNN 内已包含 `mean=0/std=255` 归一化，应用不重复除以 255。
- `classes` 是有序类别数组，模型输出类索引严格对应这个数组。
- 支持 YOLOv8 / YOLO11 的 decoded XYWH 输出；YOLO26 只有经过 `end2end=false, nms=false` 导出、输出明确为 `[1,4+类别数,候选数]` 时才接受。
- NMS 在设备应用中执行，经过反 letterbox 后返回原图像素 `xyxy`。
- 推理后端必须返回已经反量化的浮点张量。若返回原始 INT8/UINT8 输出，应用会明确拒绝，而不会把量化值误当作坐标或置信度。
- 模型必须作为 `artifacts[].kind=rknn,source=bundled` 以精确 SHA-256 和文件大小授权。不会在运行时下载或绕过 AppMgr 替换模型。

`labels.txt` 供人检查；运行时有序类别以模型契约和 manifest 为准。`model_release` 随检测结果返回，用于追溯模型版本。兼容转换不等于在板精度/性能验证，`board_validated=false` 应保持到设备验证完成。

## 构建和验收

构建和测试命令见 SDK 仓库 `tests/cvsprout/README.md`。最终包经过官方打包器校验和真实 Kit 加载器的受控推理循环测试。设备需提供匹配的 Kit、扩展帧服务、scheduled NPU 和结果网关；没有连接设备时不能认定真机启动、帧率、精度或浏览器叠加已经验收。

本应用源码使用 Apache-2.0，见 `LICENSE`。模型另有来源和许可要求，见 `MODEL_PROVENANCE.md`，应用许可证不覆盖模型权重。

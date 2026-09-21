# 模型接入：从 ONNX 到 RV1126B 应用

完整流程为：**确认模型契约 → ONNX 检查 → RKNN 转换 → 数值/任务精度验证 →
添加模型与权限声明 → 应用打包 → 经 AppMgr 进行真机验证**。

本仓已经提供可执行的转换入口和完整说明：

- [ONNX → RKNN 转换与验证流程](../../skill/recamera-pysdk/references/model-conversion.md)：
  官方示例选择、隔离环境、预处理配置、FP/INT8、校准、模拟器验证及应用接入。
- [convert_model.py](../../skill/recamera-pysdk/scripts/convert_model.py)：
  `check-env`、`inspect`、`convert` 三个命令。
- [应用打包契约](../../skill/recamera-pysdk/references/app-packaging.md)：
  完整 manifest-v2、模型 artifact 授权、依赖与打包工具。

旧版本文引用的 `models/convert/convert.py`、`inspect_onnx.py`、
`device_verify.py` 等脚本未包含在本仓中，不能直接执行。已有应用的专用外部
转换脚本仍需要其原始转换项目；新通用入口不兼容其中的 `--yolo-head` 等私有参数。

## 1. 确认输入、输出及转换环境

参考 [Rockchip Model Zoo](https://github.com/airockchip/rknn_model_zoo) 中最接近的
模型示例。skill 的流程固定了参考 revision；复用时同时检查 README、导出方式、
转换和后处理脚本，不能只看模型名称。YOLO 检测、分类、OCR、pose 的归一化、
输出及量化方法并不相同。

当前通用脚本覆盖单个静态 batch-1、NCHW 三通道 float32 图像输入的 ONNX，
支持多个输出、RGB/BGR、letterbox/stretch。NHWC 输入的 ONNX、动态形状、多输入、
音频、自定义裁剪和混合量化请采用对应模型的专用流程，不自动猜测或改写图结构。

文档验证基线为 Linux x86_64 / Python 3.11 / **RKNN-Toolkit2 2.3.2**。
目标必须是 **`rv1126b`**，不是旧平台 `rv1126`。使用独立 uv 环境或容器，
安装步骤及固定官方 wheel 见完整流程。转换工具、Torch、ONNX 及校准数据
不放进应用包，也不安装到设备共享 Python 环境。

## 2. 检查、转换与验证

以下命令从仓库根目录执行，假设已经按完整流程创建转换环境，并准备了与实际
ONNX 一致的 `recipe.json`：

```bash
uv run --no-project --python /tmp/recamera-rknn-env/bin/python \
  python skill/recamera-pysdk/scripts/convert_model.py inspect /path/to/model.onnx

uv run --no-project --python /tmp/recamera-rknn-env/bin/python \
  python skill/recamera-pysdk/scripts/convert_model.py convert \
  --onnx /path/to/model.onnx --recipe /path/to/recipe.json \
  --out /path/to/build/model-fp --quant fp --model-id det --task detect
```

先得到不量化基线，再用真实场景校准集构建 INT8：`--quant i8 --dataset
/path/to/calibration.txt`。输出目录必须不存在，防止失败后误用旧模型。校准列表
每行一个图像路径，以列表所在目录为基准；脚本统一执行 recipe 指定的缩放和填充。

追加 `--sample /path/to/held-out.jpg --atol <绝对容差> --rtol <相对容差>` 可在同次
转换中比较 ONNX Runtime 与 RKNN 模拟器输出。容差按模型确定，不自动放宽。
报告分别记录转换、数值验证、真机验证；未进行的验证明确为 `not_run`。
输出一致性仍需结合检测 mAP、分类准确率等任务指标评估，不能用“转换成功”代替。

## 3. 接入 Kit 与 manifest-v2

将生成的 `model.rknn` 复制到应用 `models/<model-id>.rknn`，把
`manifest.fragment.json` 中的 `models[]` / `artifacts[]` 条目追加到现有完整
manifest。保留原有配置、其他模型和资源声明，检查重复 ID/路径；片段不能替代
完整 manifest。设备的 scheduled 推理授权要求模型有匹配的 bundled RKNN
artifact，包含准确的 SHA-256、size 和一致的 file/mount。

需要特别核对：

- `npu.rknn: scheduled` 资源和 `npu.infer` 权限；现有 brokered/exclusive 应用
  按原有运行通道评估，不能为了转换模型悄悄改变资源所有权。
- Kit 的 `models[].input` 为 **`[1,H,W,C]`**，不能照抄 ONNX 的 NCHW 形状。
  默认主模型预处理使用其中的 H 作为方形尺寸，非方形输入需要自定义预处理。
- 模型内已包含 mean/std 时，输入按约定提供原始 uint8，避免再次归一化。
  标准 Kit/RGA 图像是 RGB；BGR 模型需要应用自行交换通道。
- 类别顺序、输出 head、DFL/decode/NMS、关键点和分割后处理必须匹配具体导出。
  为自定义类别显式填写 `models[].classes` 或标签文件，不能误用默认 COCO 类别。
- App Center 入口保留在包根目录，导出 `kit.app.App` 子类，使用 `owns_loop=True`
  和 `run(self)`；覆盖 `setup(config)` 时调用 `super().setup(config)`。
  Kit 根据 manifest 加载模型，应用通过 `self.models.<id>.infer(...)` 调用，
  不存在 `self.models.load(...)` 或旧 `on_frame` 生命周期入口。

标准 RGB 方形 letterbox 模型可以使用 `model_frame="hw-direct"`、
`model_dma_input=True` 和 `self.models.det.infer(self.pre(frame))`；其他输入契约
选择合适的 ndarray/自定义预处理路径。参考
[Kit 应用模式](../../skill/recamera-pysdk/references/kit-app-patterns.md) 和
[硬件预处理](./hw-preprocess.md)。更换模型、输入尺寸或后端需要重启应用。

## 4. 打包与真机验证

```bash
uv run --frozen python skill/recamera-pysdk/scripts/validate_app.py \
  --app-dir /path/to/app --mode package
uv run --frozen python skill/recamera-pysdk/scripts/package_app.py \
  --app-dir /path/to/app --out /path/to/dist
```

设备由 AppMgr 创建每个应用/版本独立的运行环境，并按资源声明启动，不能套用
旧的 `/userdata/rknnenv` 共享环境或“一次只能激活一个应用”的假设。设备侧平台
已提供 Kit/SDK/RKNN 运行时；应用私有依赖按当前打包契约处理。

用户要求真机测试时，经应用中心安装和启动，验证模型初始化、颜色、框/类别等
任务输出，以及循环耗时、内存、停止/重启和资源释放。不要将 Model Zoo 的
直接 NPU 测试脚本与受管应用同时运行，或绕过固件的调度/租约协议。

启动失败可用 skill 的 `diagnose_managed_app.py` 关联当前实例和日志；先核对
模型哈希、平台和运行时版本，再定位预处理、量化或应用逻辑问题。
详细部署及输出契约见 [deploy-ops.md](./deploy-ops.md)、
[output-sink.md](./output-sink.md) 和 [ai-result-overlay.md](./ai-result-overlay.md)。

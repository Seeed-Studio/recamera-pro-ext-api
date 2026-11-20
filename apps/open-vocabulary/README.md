# 开放词汇目标检测 Demo

在 reCamera Pro 的实时画面中检测用户指定的物体。应用通过 Kit 获取摄像头帧，用 CLIP 编码检测词汇，再由 YOLO-World V2.1-S 在板端 NPU 上定位目标。检测结果通过 App Center 的结果网关提交到官方预览。

## 使用

1. 在应用中心安装并启动 `open-vocabulary`。设备需要具备 RV1126B、Python 3.11、RKNN Runtime 2.3.2，以及支持多输入模型的推理调度服务。推理服务后端可用 `auto` 或 `rknnlite`。
2. 在应用设置的「检测词汇」中输入以逗号分隔的英文物体名或短名词词组。可以用 `显示名=英文提示词` 自定义标签。
3. 在应用设置开启「网页叠加」并保存。打开主视频预览的「显示 AI 叠加（当前页面）」，选择 `open-vocabulary` 结果来源。

| 输入 | 查询 |
| --- | --- |
| `helmet, cup, person` | 常见物体 |
| `安全帽=safety helmet, 杯子=cup` | 英文检测词汇配中文显示名 |
| `红帽=red helmet, blue cup, white car` | 按物体主色过滤 |

应用支持热更新，每次最多 80 项，每个提示词最多 18 个 CLIP 子词。新词汇准备好后整组切换，无需重新训练、转换模型或重启。清空词汇并保存会暂停检测和清除框。

颜色前缀支持 `red`、`orange`、`yellow`、`green`、`blue`、`purple`、`pink`、`brown`、`black`、`white`、`gray`（也可写 `grey`）。应用先检测物体，再统计框内区域的 HSV 像素，匹配到主色时优先显示该颜色词条。例如同时配置 `helmet, red helmet` 时，红色帽子显示为 `red helmet`，其余检出的帽子可显示为 `helmet`。光照、背景和多色物体会影响主色判断。建议使用包含物体名的查询，例如 `red helmet`。单个颜色词、衣着描述和完整句子的效果取决于模型，Demo 的主色过滤也不会定位人体的服装区域。

网页框显示目标名和检测置信度，例如 `red helmet 91%`。百分比是物体检测分数。结果流中的 `box` 为原图像素 `[x1, y1, x2, y2]`，检测到明确主色时也会提供 `object_color`。消费方可以结合 `vocabulary_revision` 和 `prompt` 解释动态类别。阈值和推理频率可在应用设置中修改，默认 5 次/秒是限速值。需要在 RTSP 或录像里显示框时，可开启应用的流 OSD。

## 测试与打包

在仓库根目录运行离线测试。测试使用模拟帧或推理后端，不需要模型文件和设备。

```sh
uv run pytest apps/open-vocabulary/tests apps/test_manifest_schema.py
```

`manifest.json` 声明两份已转换的 RV1126B FP16 模型和两份离线 wheel。和仓库中的 `yolo-detector` 一样，Git 保存资产声明，打包前由 `fetch_assets.py` 把资产下载到 Git 忽略的目录。设备安装后从应用包加载模型。

| 路径 | 内容 |
| --- | --- |
| `models/world_v21s.rknn` | YOLO-World V2.1-S 检测器 |
| `models/clip_v21.rknn` | CLIP 文本编码器 |
| `wheels/ftfy-6.3.1-py3-none-any.whl` | 文本清理 |
| `wheels/wcwidth-0.9.4-py3-none-any.whl` | ftfy 的运行时依赖 |

官方构建从模型池的 `models/open-vocabulary/<上表相对路径>` 下载资产。模型池提供这些文件后，可以使用仓库标准命令下载并打包。 [资产发布指南](../../docs/guide/app-publishing-and-permissions-guide.md#3-模型与大资源文件免上传-git)

```sh
uv run python market/packaging/fetch_assets.py apps/open-vocabulary
uv run python market/packaging/build.py apps/open-vocabulary --out dist/open-vocabulary
```

当前模型资产尚待发布到模型池。已有 0.1.6 开发包时，可以提取上表文件并保留相对路径，再用本地资产打包。

```sh
uv run python market/packaging/build.py apps/open-vocabulary \
  --payload-root /path/to/open-vocabulary-assets \
  --out dist/open-vocabulary
```

打包器校验 manifest 中的大小和 SHA-256，输出 `open-vocabulary-0.1.6-arm64.tar.gz`。模型和 wheel 由包携带，Kit、SDK、NumPy、RKNNLite 和 RGA 由设备提供。开发包安装所需的信任配置见[应用中心发布指南](../../docs/guide/app-center-publishing.md)。

模型与词表来源见 [SOURCES.json](SOURCES.json) 和 [THIRD_PARTY.md](THIRD_PARTY.md)。

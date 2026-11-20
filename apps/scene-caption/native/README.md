# InternVL3-1B 转换与流式预览

Scene Caption 需要 reCamera Pro 固件 1.3.3 或更高版本。使用旧固件的设备请先升级，再安装应用。

从干净的源码目录打包时，在仓库根目录运行
`python3 market/packaging/fetch_assets.py apps/scene-caption`。发布 CI 会自动执行这一步。
脚本从应用清单的 `x-asset-bundle` 下载已转换的模型，核对压缩包及各文件的大小和 SHA-256，
再将六段视觉模型和语言模型放入 `apps/scene-caption/models/`。如需重新导出权重，可使用下文的转换步骤。

使用 OpenGVLab/InternVL3-1B 修订 `4415a3b810e636d11dfa86b0e9ba40bb00535aa8` 和 Rockchip rknn-llm 修订 `cd7a68acff3992dbf63c3e31d8e332221f0858f2`。采集线程复制一张 448×448 RGB 图像后立即归还摄像头缓冲区。独立的推理线程处理这张图，将 256 个视觉 token 送入语言模型。每次只处理一个样本，下一轮直接取新帧。每帧独立推理。应用中心可以设置分析提示词（最多 128 个字符）、分析完成后的等待时间（0–120 秒）和流式显示开关。修改后从下一张采样画面生效。每次回答的输出上限可设为 8–512 个 token，默认 256，修改后应用中心会重启应用。RKLLM 回调产生的文字通过相同的帧时间戳发布到浏览器渲染层。订阅客户端可用 `results[0].final` 区分中间文本和整句，`caption` 事件每帧只发布一次。应用在等待下一条文字时以 2 Hz 刷新字幕图层，保留原采样时间戳。显示刷新不触发新的推理或完成事件。特别长的回答在视频上滚动显示最近 16 行，完整文本保存在该帧结果和完成事件中。

`SNAPSHOT` 为上述模型快照的本地目录，`APP` 为本应用源码目录，`ROCKCHIP` 为 Rockchip 仓库。语言转换使用 RKLLM Toolkit 1.2.3，视觉模型使用 RKNN Toolkit2 2.3.2。视觉编码器的24 个 Transformer 块每 4 个编译为一个 FP16 RKNN，共六段。应用用同一个独占 NPU 租约，保持解码器驻留，每次只加载一段视觉权重。

```sh
"$RKLLM_PYTHON" "$APP/native/export_vision.py" "$SNAPSHOT" vision.onnx
mkdir -p "$APP/models" "$APP/lib"
"$RKNN_PYTHON" "$APP/native/compile_vision.py" vision.onnx split-vision
cp split-vision/vision-[0-5].rknn "$APP/models/"
"$RKLLM_PYTHON" "$APP/native/export_language.py" "$SNAPSHOT" "$APP/models/language.rkllm"
```

用 SDK 的 AArch64 交叉编译器构建解码桥接库。`RKLLM_API` 指向上述 Rockchip 仓库的 `rkllm-runtime/Linux/librkllm_api`。`RKNN_API` 指向同仓库多模态示例中的 Linux librknn_api 目录，运行设备提供 librknnrt.so。

```sh
"$CXX" -O2 -std=c++14 -shared -fPIC -I"$RKLLM_API/include" -I"$RKNN_API/include" \
  "$APP/native/decoder.cpp" "$APP/native/vision.cpp" -L"$RKLLM_API/aarch64" -L"$RKNN_API/aarch64" -lrkllmrt -lrknnrt \
  '-Wl,-rpath,$ORIGIN' -o "$APP/lib/libcaption.so"
cp "$RKLLM_API/aarch64/librkllmrt.so" "$APP/lib/"
```

重新生成的文件可能因工具链而改变。用下面的命令按实际内容更新模型和运行库的校验信息，再使用本 SDK 的 v2 打包器生成安装包。`SDK` 是 recamera-pro-ext-api 根目录。

```sh
python3 - "$APP" <<'PY'
import hashlib, json, sys
from pathlib import Path
manifest = Path(sys.argv[1]) / "manifest.json"
doc = json.loads(manifest.read_text())
for item in doc["artifacts"]:
    payload = manifest.parent / item["file"]
    with payload.open("rb") as stream:
        item["sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
    item["size"] = payload.stat().st_size
manifest.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
PY
python3 "$SDK/market/packaging/build.py" "$APP" --out ./dist
```

应用由 App Center 管理，安装后启用「浏览器渲染」，启动应用，在预览页选择 Scene Caption。启动需要当前固件的独占 NPU 租约。设备已有的其他 NPU 应用可以在停止 Scene Caption 后恢复。

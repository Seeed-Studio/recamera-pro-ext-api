# 11 · 浏览器叠加自由绘制（geometry 图元）

应用在 Python 端逐帧发送 v2 `geometry[]` 图元，设备 Web 页面在视频上绘制。本示例不做推理、不占 NPU，只取帧尺寸与时间戳，画三样东西：

- 左侧：COCO-17 人体骨骼（17 个点、16 条肢体，每条肢体单独颜色，随帧摆动）
- 右上：文字面板（polygon 边框 + 6 行 point 标签，帧号和时间每帧更新）
- 底部：最近 90 帧的正弦折线，末端标注当前值

图元只在浏览器绘制，不进入 RTSP、录像和烧录 OSD。

## 文件

| 文件 | 说明 |
|---|---|
| `manifest.json` | manifest v2；声明 `camera.frames`（用于关联主码流坐标）与 `result.publish`，内存 96 MB，不声明 NPU；`render.geometry` 与 `output.fields[].coord=pixel_points` |
| `app.py` | `frames()` 循环 → `GeometryBuilder` 组装 → `emit([], pts, geometry=...)` |
| `icon.png` | 应用图标 |
| `test_overlay_geometry.py` | 校验每帧图元通过 `kit.geometry` 严格校验与 Hub 清洗（1280×720、640×360），manifest 通过设备端 v2 校验 |

## 关键代码

```python
from kit.geometry import GeometryBuilder

for frame in self.frames():
    g = GeometryBuilder()
    g.line((x1, y1), (x2, y2), id="limb-0", color="#ff3040", line_width=4)
    g.point(x, y, id="kpt-0", color="#ff3040", point_radius=4)
    g.polygon(((l, t), (r, t), (r, b), (l, b)), id="panel-bg",
              color="#ffffff", line_width=1.5, fill=True, fill_color="#10203080", opacity=0.6)
    g.point(l + 12, t + 30, id="panel-line-0", label="frame #123", color="#ffd400")
    g.polyline(points, id="chart-sine", color="#00ffa0", line_width=2.5)
    self.emit([], frame.pts, geometry=g)
```

坐标为原始帧像素（`frame.w × frame.h`），前端按视频实际显示区域缩放。

## 运行

```bash
uv run pytest examples/11-overlay-geometry -q          # 本地校验
```

设备上按 [app-center-publishing.md](../../docs/guide/app-center-publishing.md) 打包、签名、安装，然后：

```bash
curl -s -X POST http://127.0.0.1:8130/api/appMgr/start -H 'Content-Type: application/json' \
     -d '{"id":"overlay-geometry-demo"}'
```

打开 `https://<设备IP>/preview`，「结果来源」选 **Overlay Geometry Demo**。

## 可用样式与限制

| 项 | 规则 |
|---|---|
| 图元类型 | `point` / `line` / `polyline` / `polygon`（`box`、`quad`、`keypoints`、`pose` 由 helper 转换成这四种） |
| 样式字段 | `color`、`fill`、`fill_color`（`#RRGGBB[AA]`）、`opacity`（0–1）、`line_width`（0.25–16）、`point_radius`（0.5–32）；其他字段被丢弃 |
| 文字 | 只能作为图元的 `label`（≤128 字符）；字号、字体、底色由前端固定 |
| 数量 | 每帧 ≤256 个图元、单图元 ≤256 点、总点数 ≤4096；前端每帧最多绘制 48 个标签 |

2026-09-15 在 192.168.10.29（前端 recamera_web_react `origin/main` 76e0c92 + overlay 定制分支）实测：骨骼、折线、文字标签、polygon 边框均按预期绘制；polygon 的半透明填充（`fill_color=#10203080`、`opacity=0.6`）在画面上不可见，头部 5 个点显示为小圆弧而非实心点，原因未查（需核实前端 fill 与点绘制逻辑）。

需要自定义字体、字号、富文本面板或动画时，geometry 图元无法满足，见 [overlay-customization-design.md](../../docs/guide/overlay-customization-design.md) 阶段 2（沙箱 JS 插件，未实现）。

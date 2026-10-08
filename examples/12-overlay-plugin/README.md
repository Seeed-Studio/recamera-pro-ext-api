# 12 · 沙箱浏览器叠加插件（ui.overlay）

声明 `ui.overlay` 的最小示例：manifest 指向包内唯一一个自包含 HTML 文档 `web/overlay.html`，App Center 前端通过 `GET /api/app-center/v1/apps/<id>/overlay?h=<sha256>` 取到字节（服务端强制 `text/plain`），内联进 `sandbox="allow-scripts"` 的 srcdoc iframe 里绘制。应用本身每帧只发两个 geometry 图元（polygon 巡检区 + 一个标签点），**声明式画布保留为回退**——插件缺失、被卸载或路由失败时仍能看到叠加。

## 数据通路

```
app.py --emit(geometry[])--> Result Hub --> 宿主(前端) --port.postMessage--> web/overlay.html
                                                   ^                          |
                                                   +-------- ready 握手 ------+
```

- 宿主在 srcdoc 最前面注入自己的 bootstrap：在任何包代码执行前创建 `MessageChannel`，把本端暴露为 `window.__overlayPort`（只读），通道从这一刻起固定在原始文档。
- 插件启动后经该端口发**一条** `{protocol:1, channel:"recamera.overlay.plugin", type:"ready", seq:1}`，宿主收到后才开始投递数据。
- 之后宿主经端口投递 `init` / `frame` / `event` / `geometry` / `teardown` 等消息；插件只画自己应用的 `results[]`（`box` 按 `coordinate_space` 与 `viewport` 换算到屏幕）。

## 文件

| 文件 | 说明 |
|---|---|
| `manifest.json` | manifest v2；`ui.overlay = {entry: "web/overlay.html", sha256: <字节摘要>}`；`render.geometry` + `output` 保留声明式回退 |
| `app.py` | `frames()` 循环 → `GeometryBuilder` 两个图元 → `emit([], pts, geometry=...)`，无模型无 NPU |
| `web/overlay.html` | 自包含文档（内联 CSS+JS，零外部资源）：读 `window.__overlayPort`，canvas `roundRect` 画两个圆角 chip，发一次 ready，之后画到达的数据 |
| `test_overlay_plugin.py` | 校验图元通过严格构建与 Hub 清洗、manifest/`ui.overlay` 通过 v2 校验、sha256 与文件字节一致、HTML 自包含且无外部引用 |

## 安装期与运行期的闸门（服务端强制）

| 闸门 | 规则 |
|---|---|
| manifest | `ui`/`ui.overlay` 两级封闭 schema：未知字段拒绝；entry 必须匹配 `web/[A-Za-z0-9._/-]{1,120}\.html` 且禁 `..`；sha256 必须 64 位小写十六进制 |
| 安装 | entry 必须在包内、是常规文件（链接拒绝）、≤256 KiB、实际摘要与声明一致，任一不满足整个安装被拒 |
| 路由 | 未安装或未声明 → 404；无 `?h=` → 400；`?h=` 与实际字节不符 → 409；no-follow 逐级打开，拒绝穿越/符号链接/非常规文件/超限 |
| 响应头 | `Content-Type: text/plain; charset=utf-8`（顶层导航不执行）、`X-Content-Type-Options: nosniff`、`Cache-Control: no-store`、`ETag: <sha256前16位>` |

## 运行

```bash
uv run pytest examples/12-overlay-plugin -q          # 本地校验
```

设备上打包、签名、安装后，打开 `https://<设备IP>/preview`，「结果来源」选 **Overlay Plugin Demo**：挂载成功时该来源不再走声明式画布，左上角出现 "overlay plugin ready" chip；卸载或回退后恢复为 example 11 式的声明式绘制。

## 改动 web/overlay.html 之后

文档字节变了就必须同步 manifest 里的 `ui.overlay.sha256`，否则安装被拒（摘要不符）、路由返回 409：

```bash
NEW=$(shasum -a 256 web/overlay.html | awk '{print $1}')
# 更新 manifest.json 的 ui.overlay.sha256 为 $NEW
```

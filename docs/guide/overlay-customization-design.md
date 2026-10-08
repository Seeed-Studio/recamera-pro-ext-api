# AI 结果叠加用户定制：设计 spec（草案）

> 状态：设计草案，未实施。基线：
> - 后端 `recamera_pro`（appmgr / Result Hub），分支 `integrate/full-20260909`
> - 前端 `recamera_web_react` `origin/main` `76e0c92`（2026-09-14）
>
> 下文 `B/` = `market/appmgr/`，`F/` = 前端 `src/`。行号取自上述基线。

## 目标

用户在自己的设备上定制 AI 结果在浏览器中的显示方式，不需要重新打包或签名 app：

1. **阶段 1：声明式显示覆盖。** 普通用户在 UI 面板里调整颜色、字号、位置等白名单字段。
2. **阶段 2：沙箱 JS 插件。** 开发者在 app 包内附带 JS，在隔离的 iframe 中自由绘制。

非目标：改变烧进 RTSP/录像的 OSD。烧流 bridge 只调用 `send_detections`（`B/visualization.py:513`），OSD ABI 没有颜色和字幕参数（`sdk/include/recamera_ext.h:161`）。

## 现状（main 前端）

| 项 | 现状 | 证据 |
|---|---|---|
| 结果流 | `/ws/ai/results/v2`，按 source 保存 `latestFrame`（每个 source 只保留一帧） | `F/contexts/ResultStreamContext.js:214`、`F/contexts/resultProtocol.js:676` |
| 图元 | box / quad / keypoints，以及 `geometry[]` 的 point/line/polyline/polygon（可设颜色、线宽、点半径、透明度、填充） | `F/components/inference/results/resultOverlay.js:14,317,565` |
| `render.boxes` | `color_by` 为字段路径；`label` 取映射字段后回退；调色板固定（`sourcePalette`），不接受自定义颜色映射 | `resultOverlay.js:69,416,764`、`AIResultOverlay.js:12,60`；后端封闭校验 `B/manifest.py:774`（仅允许 `color_by/label/line_width`） |
| 字幕 | 按事件名称（voice/speech 等）识别；视频上只显示最新一条；字体、背景、位置写死在 CSS | `resultProtocol.js:367`、`AIResultsCenter.js:500`、`InferenceVideoPreview.js:160`、`AIResultsCenter.css:520` |
| 叠加入口 | 结果中心 `InferenceVideoPreview` 和预览页 `PreviewAIResults` | `InferenceVideoPreview.js:724`、`F/components/preview/PreviewPage.js:1685` |
| 用户设置 | `VisualizationSettings` 只管烧流（`/api/app-center/v1/visualization`）；浏览器侧显隐存 localStorage | `VisualizationSettings.js:97`、`F/contexts/browserOverlayPreferences.js:61` |
| 插件机制 | 无（源码中没有 iframe） | — |

## 阶段 1：设备级显示覆盖

### 存储与 API（appmgr）

- **路由**：`GET/PUT/DELETE /api/app-center/v1/apps/<id>/render-override`。不并入 visualization 路由，因为那两个路由的语义分别是 `osd` 和 `stream_burn_in`，保持不变。
- **存储**：`/userdata/local/appmgr/render-overrides/<id>.json`，内容为 `{schema_version:1, revision, override:{…}}`。写入时原子替换。
- **PUT**：全量替换覆盖内容，携带 `revision` 做并发校验。
- **DELETE**：恢复默认，同样携带 `revision` 校验。
- **GET**：返回 manifest 默认值、覆盖值、合并后的生效值。
- **写入约束**：复用现有 mutation gate 和已安装检查（`B/server.py:355`）。

### 白名单字段

| 路径 | 规则 |
|---|---|
| `boxes.color_by` / `boxes.label` | result 字段路径，1–64 字符，禁止原型属性名 |
| `boxes.colors` | 取值 → 颜色，最多 64 项，键 ≤128 字符 |
| `boxes.line_width` | 整数 1–16 |
| `subtitles.font_size` | 整数 8–72（CSS px） |
| `subtitles.color` / `subtitles.background_color` | 颜色 |
| `subtitles.background_opacity` | 0–1 |
| `subtitles.position` | 九宫格枚举 |
| `subtitles.max_lines` / `subtitles.duration_sec` | 整数 1–10 / 0.1–60 |
| `events.<kind>.as` | `none`/`badge`/`toast`/`panel`/`subtitle` |
| `events.<kind>.text` | ≤1024 字符，只做 `{{ 字段 }}` 替换 |
| `events.<kind>.position` / `duration_sec` / `max_lines` | 同上 |

- **颜色格式**：`^#[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?$`，参照 `kit/geometry.py:91`。
- **数值**：拒绝 boolean。
- **未知字段**：直接拒绝。
- **禁止覆盖**：坐标空间、schema_version、geometry 限额、`stream_osd`。
- **manifest schema 同步**：`boxes.colors` 和顶层 `subtitles` 同时加入 manifest v2 的 render schema，让 app 作者也能声明默认值。改动点：`B/manifest.py:765-840`、`B/schema/manifest-v2.schema.json`。

### 合并与生命周期

- **合并位置**：`B/visualization.py:98` `effective_render`，按叶字段合并 `manifest.render ← override`；`colors` 整表替换。
- **注入**：由 `ResultHub.refresh_app_manifest` 注入各 generation 的缓存（`B/result_hub.py:2482,2538`）。
- **生效时机**：保存后经 `B/server.py:681` 刷新缓存，不等待 app 重新 hello。
  - 需要补的改动：同一 generation 内更新 `_app_render_cache` 时（`B/result_hub.py:2538-2554`），现有逻辑不向 WS 客户端广播任何通知。需新增一条样式变更的控制消息（或对该 source 重发最新帧的 envelope），前端收到后用新 render 重绘已有帧。
- **安装/卸载**：新装清除历史覆盖；卸载删除覆盖（`B/server.py:1428,1718`）。
- **升级**：`B/installer.py:857` 只切换代码目录，不涉及外置的覆盖文件，覆盖文件需要单独做事务：
  1. 包切换前，按新 manifest 校验覆盖，结果写入 `<id>.json.pending`；
  2. 包切换成功后，原子 rename 为 `<id>.json`；包切换失败则删除 pending；
  3. appmgr 启动时清理残留的 `.pending`，并对已有覆盖按当前已安装 manifest 重新校验，不合法的字段丢弃并记录日志（崩溃恢复）。

### 前端改动（main）

- **客户端**：`F/components/app_center/appManagerClient.js:172` 新增 override 的读、写、删。
- **渲染器**：
  - `AIResultOverlay.js:60` `primitiveColor`：支持 `colors` 映射，字段缺失时回退调色板。
  - `resultOverlay.js:416` `entryLabel`：支持 `boxes.label`。
  - `resultOverlay.js:565` `collectOverlayPrimitives`：接收合并后的样式。
  - 处理样式变更控制消息，用新 render 重绘已有帧。
- **字幕**：`InferenceVideoPreview.js:160` 的 `VideoSubtitle` 改为读取 `subtitles.*`，不再写死 CSS；预览页 `PreviewAIResults` 补上字幕显示。
- **事件**：`resultProtocol.js:367` 改为按 `events.<kind>.as` 分派，名称识别只作为未声明时的回退。
- **设置面板**：新增按 app 的「显示设置」组件，不混入全设备烧流面板 `VisualizationSettings.js:70`；提供「恢复默认」。

### 测试

- **后端**：`B/tests/test_visualization.py`、`test_result_hub.py`、`test_uninstall.py`、`test_installer_v2.py`。覆盖校验、刷新、伪造、并发 revision、升级回滚。
- **前端（Jest，react-scripts）**：`resultOverlay.test.js`、`InferenceVideoPreview.test.js`、`videoViewport.test.js`。覆盖映射值为 0、字段缺失、透明度 0、字幕过期、多 source 交错、两个叠加入口。

## 阶段 2：沙箱 JS 插件

### manifest 与签名

- **manifest 字段**：新增 `ui.overlay = {entry: "web/overlay.html", permissions: {network: {outbound: []}}}`。`outbound` 只接受精确的 HTTPS origin。
- **校验**：改封闭 schema 和 manifest 校验（`B/manifest.py:62,1305,1396`）。
- **catalog**：`market/catalog/gen_catalog.py:375` 转发能力摘要；运行时授权只读取已安装的 manifest。
- **签名**：整包签名已覆盖包内的 `web/` 文件（`B/signing.py:308`、`B/installer.py:500`），不需要改签名流程。

### 静态服务

- **路由**：appmgr 提供 `/api/app-center/v1/apps/<id>/overlay/<release>/<file>`。
  - 需要鉴权。
  - 核对已安装版本和 `ui.overlay` 声明。
  - 只服务 `web/` 内的普通文件；拒绝路径穿越、编码分隔符、符号链接，逐级 no-follow 打开。
- **不复用 `/appcenter/apps/`**：那是包仓库的 alias（`market/deploy/ext_appmgr.conf:52`）。

响应头（`SCRIPT` 为获准脚本的精确 URL，`ORIGIN` 为设备 origin）：

```
Content-Security-Policy: default-src 'none'; script-src SCRIPT; style-src 'unsafe-inline'; img-src data:; connect-src 'none'; font-src 'none'; media-src 'none'; object-src 'none'; frame-src 'none'; worker-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors ORIGIN; sandbox allow-scripts
X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer
Cache-Control: no-store
Permissions-Policy: camera=(), microphone=(), geolocation=(), display-capture=(), usb=(), serial=()
```

manifest 显式声明 outbound 时，只放宽 `connect-src`。

### 前端宿主

- **挂载位置**：共享的插件宿主组件挂在两个叠加入口，`F/components/preview/PreviewAIResults.js:191` 和 `InferenceVideoPreview.js:724`。
- **iframe 属性**：`sandbox="allow-scripts"`（不含 `allow-same-origin`），透明，`pointer-events:none`。
- **排序**：多个插件按 app id 稳定排序，不接受插件自报 z-index。
- **数据来源**：`useResultSources.js:160,239` 的 `stream.sources` / `selectedSourceState`，从中取出该 app 的 `latestFrame` 和事件，保留 stream 维度。
- **坐标几何**：复用 `videoViewport.js:37` `calculateVideoElementViewport`。
- **失败回退**：`ready` 超时、或插件报错超过阈值时移除 iframe，回退到阶段 1 的声明式渲染。

### postMessage 协议（`protocol: 1`）

公共头：`{protocol, type, session, source, seq}`

| 方向 | type | 内容 |
|---|---|---|
| 宿主 → 插件 | `init` | 合并后的 render、locale |
| | `frame` / `event` | 该 app 的 v2 envelope（只推本 source） |
| | `geometry` | `displayRect`、`dpr`、`stream.width/height`、`coordinate_space` |
| | `visibility` / `teardown` | 可见性 / 卸载原因 |
| 插件 → 宿主 | `ready` / `error` | 首版不开放任何请求类消息 |

- **来源校验**：opaque origin 下 `event.origin` 为 `"null"`，不能用它鉴权。宿主对插件发来的每条消息依次检查：
  1. `event.source === iframe.contentWindow`；
  2. `session` 等于该 iframe 当前的 session；
  3. `protocol === 1`，`type` 属于 `ready`/`error`；
  4. 字段结构符合定义，序列化后 ≤ 8 KiB；
  5. `seq` 严格递增。

  任一项不通过即丢弃该消息并计入错误数。iframe 每次重新加载（包括插件崩溃后重建、app 升级、source generation 变化）都生成新 session，旧 session 的消息一律丢弃。
- **发送目标**：宿主发消息时 targetOrigin 用 `"*"`。
- **限流**：每个 source 只保留一个帧槽，最多 30 Hz；事件走独立的有界去重队列。

### 与现有 `/extension/<name>/` 机制的区别

`docs/guide/frontend-extension.md` 描述的 `ext_<name>.conf` + `/extension/<name>/` 已经可以挂载一个完整页面：
- 页面同源，复用 JWT；
- 可以自行订阅 `/ws/ai/results/v2` 并在自己的视频上绘制；
- 需要写 `/oem`，完全受信任，能访问全部管理 API。

阶段 2 不同：插件嵌入官方预览页内部，运行在沙箱里，无法访问 API。两者面向不同的信任级别，并存。

## 风险与待核实

1. **opaque origin 行为未实测**：`sandbox` iframe 的脚本加载、Cookie、`Origin: null` 请求在设备 nginx 下的行为都没有测过。现有 nginx 会拒绝异域 Origin（`market/deploy/ext_appmgr.conf:129`）。插件静态资源请求带 `Origin: null` 时是否被拦，需要实测。
2. **单帧存储限制**：main 前端每个 source 只存一帧，Hub 按 source+stream 存。插件推帧时如果需要多 stream，要先扩展 `resultProtocol.js`。
3. **烧流不一致**：阶段 1 的覆盖不改变烧流。UI 上必须注明「仅浏览器显示」。
4. **部署前提**（2026-09-15 观察）：`release/v1.6.5/frontend-v1.6.5.tar.gz` 的 `main.1e2af984.js` 连接 `/appcenter/ws/results`，不含 v2。设备 192.168.10.29 是否运行该构建，未直接核对 bundle hash。验证前需要先部署 main 前端。
5. **分支关系**（2026-09-15 在 WSL 仓库执行 `git fetch` 后观察）：本地分支 `feat/ext-api`（`8f7ae4d`，2026-08-17）不是 `origin/main` 的祖先，工作区有未提交的 `Applications.js` 改动。实施分支建议基于 `origin/main` 新建。

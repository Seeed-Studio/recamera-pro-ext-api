# 自定义 AI 结果显示样式

在设备 Web 页面上，按应用调整 AI 结果在浏览器中的显示方式：检测框颜色、标签、线宽、字幕样式、每类事件怎么显示。设置保存在设备上，不需要重新打包或签名应用。

设计与实现细节见 [overlay-customization-design.md](./overlay-customization-design.md)。

## 适用范围

| 项 | 说明 |
|---|---|
| 生效位置 | 「实时预览」页、应用中心「AI 推理 / 结果」页中的浏览器叠加 |
| 不生效位置 | 设备视频流烧录（OSD）、RTSP、录像、截图 |
| 作用对象 | 已安装的 manifest v2 应用，每个应用单独一份设置 |
| 可见范围 | 同一台设备的所有浏览器、所有用户 |
| 需要的组件 | appmgr（含 render-override 接口）、Web 前端（含「显示样式」面板）、nginx `ext_appmgr.conf` 中的 `/api/app-center/v1/` 与 `/ws/ai/results/v2` 两个 location |

## 在页面上设置

1. 打开 `https://<设备IP>/app-center`。
2. 在应用卡片右下角点「⋮」→「配置」。
3. 滚动到「显示样式（仅浏览器）」。输入框里的灰色文字是应用自带的默认值；留空表示沿用默认值。
4. 修改后点「保存显示样式」。已打开的预览页会立即按新样式重绘，不需要刷新。
5. 点「恢复默认」删除这台设备上对该应用的全部样式设置。

「显示样式」有自己的保存按钮，和对话框底部的「保存」（应用参数）互不影响。

### 可设置的项

**检测框**

| 字段 | 说明 | 取值 |
|---|---|---|
| 按字段着色 | 用结果里哪个字段决定颜色，支持点分路径，如 `state`、`attributes.class_name` | 1–64 字符 |
| 标签字段 | 框上显示哪个字段的值 | 1–64 字符 |
| 线宽 | 框线宽度 | 整数 1–16 |
| 取值颜色映射 | 「按字段着色」字段的某个取值 → 颜色，如 `fallen → #FF0000` | 最多 64 项；颜色 `#RRGGBB` 或 `#RRGGBBAA` |

没有写进映射的取值，沿用默认调色板的颜色。

**字幕**

| 字段 | 取值 |
|---|---|
| 字号 | 整数 8–72（px） |
| 文字颜色 / 背景颜色 | `#RRGGBB` 或 `#RRGGBBAA` |
| 背景不透明度 | 0–1 |
| 最多行数 | 整数 1–10 |
| 显示时长 | 0.1–60 秒 |
| 位置 | 九宫格：`top-left` `top-center` `top-right` `middle-left` `center` `middle-right` `bottom-left` `bottom-center` `bottom-right` |

**事件显示**（点「添加事件」，按事件 `kind` 分别设置，如 `fall`、`transcript`）

| 字段 | 取值 |
|---|---|
| 显示方式 `as` | `subtitle` 字幕 / `toast` 提示条 / `badge` 标记 / `panel` 只进事件面板 / `none` 不显示 |
| 文本 `text` | ≤1024 字符，`{{ 字段名 }}` 替换为事件里对应字段，如 `跌倒 #{{ track_id }}` |
| 位置 / 显示时长 / 最多行数 | 同字幕 |

没有设置的事件类型，按应用 manifest 的声明显示；manifest 也没声明的，语音类事件（voice、speech、transcript 等名称）显示为字幕，其他进入事件面板。

## 通过 API 设置

所有接口需要登录 Cookie（与 Web 页面相同）。

**读取**

```bash
curl -sk -b "token=<登录token>" \
  https://<设备IP>/api/app-center/v1/apps/fall-detection/render-override
```

返回：

```json
{
  "app_id": "fall-detection",
  "schema_version": 1,
  "revision": 0,
  "override": {},
  "defaults": { "...": "应用 manifest 里的 render" },
  "effective": { "...": "合并后实际使用的 render" }
}
```

**保存**：必须带上最近一次读到的 `revision`，全量替换覆盖内容。

```bash
curl -sk -b "token=<登录token>" -X PUT -H 'Content-Type: application/json' \
  https://<设备IP>/api/app-center/v1/apps/fall-detection/render-override \
  -d '{
    "revision": 0,
    "override": {
      "boxes": {"colors": {"fallen": "#FF0000", "standing": "#00FF00"}, "line_width": 4},
      "subtitles": {"font_size": 28, "position": "top-center"},
      "events": {"fall": {"as": "toast", "text": "跌倒 #{{ track_id }}", "duration_sec": 6}}
    }
  }'
```

**恢复默认**

```bash
curl -sk -b "token=<登录token>" -X DELETE \
  "https://<设备IP>/api/app-center/v1/apps/fall-detection/render-override?revision=1"
```

**错误响应**

| HTTP | 内容 | 处理 |
|---|---|---|
| 400 | `{"error":"invalid_override","field":"boxes.line_width","detail":"must be an integer in [1,16]"}` | 按 `field` 修正该字段 |
| 404 | 应用未安装 | — |
| 409 | `{"error":"revision_conflict","current_revision":2}` | 其他人已修改；重新读取后再保存 |

未列出的字段一律返回 400。坐标空间、`schema_version`、`stream_osd`、geometry 限额不能通过覆盖修改。

### 实时通知

保存或恢复默认后，`/ws/ai/results/v2` 向已连接的客户端发送一条控制消息，之后该应用的每一帧 `render` 都是合并后的值：

```json
{
  "schema": "recamera.ai.result", "schema_version": 2,
  "type": "render_updated",
  "source": {"kind": "app", "id": "fall-detection", "app_id": "fall-detection",
             "instance": "…", "generation": 1},
  "render": { "...": "合并后的 render" },
  "extensions": {"reason": "render_override", "scope": "source"}
}
```

自己写的前端可以只处理 `frame` 里的 `render`；需要立即重绘已显示的内容时再处理 `render_updated`。

## 给应用作者：自由绘制

显示样式只能调整现有结果的外观。要在画面上画任意内容（骨骼、区域、轨迹、文字面板、图表），由应用逐帧发送 v2 `geometry[]` 图元，完整示例见 [examples/11-overlay-geometry](../../examples/11-overlay-geometry/)。可用图元为 point / line / polyline / polygon，文字只能作为图元 `label`，字体和字号由前端固定。

## 给应用作者：在 manifest 里声明默认样式

manifest v2 的 `render` 支持同样的字段，用户的设置按字段覆盖在它之上：

```json
"render": {
  "schema_version": 1,
  "boxes": {
    "color_by": "state",
    "label": "state",
    "line_width": 2,
    "colors": {"fallen": "#FF0000", "standing": "#00C853"}
  },
  "subtitles": {"font_size": 20, "position": "bottom-center", "max_lines": 2},
  "events": {
    "fall": {"as": "toast", "position": "top-center", "duration_sec": 4, "text": "Fall #{{ track_id }}"}
  }
}
```

manifest 中事件的 `position` 仍接受旧值 `bottom` / `top`；通过设备覆盖设置时只接受九宫格取值。

## 生命周期

| 操作 | 覆盖设置 |
|---|---|
| 首次安装 | 清除该应用 ID 的历史设置 |
| 升级 | 按新 manifest 重新校验，保留合法字段，丢弃不合法字段（写入 `audit.log`，`op=upgrade_revalidated`）；升级失败时保持原设置 |
| 卸载 | 删除 |
| appmgr 启动 | 清理升级中断留下的临时文件，删除已卸载应用的设置，按当前 manifest 重新校验 |

存储位置：`/userdata/local/appmgr/render-overrides/<应用ID>.json`。每次修改记录在 appmgr `audit.log`（`action=v1_render_override`，`op=replace|reset|removed|upgrade_revalidated`）。

## 已知限制

- 只影响浏览器显示。视频流烧录只支持检测框，不使用这里的颜色和字幕设置。
- 保存后新连接的浏览器，在应用发出下一帧之前，回放的历史帧仍是旧样式；应用停止发帧时会一直停留在旧样式。
- 预览页的 `toast` 显示在上方居中，和预览页已有的对齐提示位置相同，可能重叠。

## 实测记录

2026-09-15，设备 192.168.10.29（reCamera Pro，RV1126B），应用 fall-detection 0.2.4：

| 检查项 | 结果 |
|---|---|
| PUT 保存 | 200，revision 递增 |
| v2 WS `render_updated` | 保存与恢复默认各收到 1 条；后续帧 `render` 含 / 不含 `colors` |
| 非法值（`line_width: 99`） | 400，`field=boxes.line_width` |
| 过期 revision | 409，返回当前 revision |
| `audit.log` | 记录 `op=replace` / `op=reset` |
| 浏览器收到的帧 `render` | 含 `colors` 与 `line_width: 4` |
| 应用中心「显示样式」面板 | 读取并显示已保存的设置 |
| geometry 自由绘制（examples/11-overlay-geometry） | 骨骼、折线、文字标签、polygon 边框在预览页绘制；polygon 半透明填充不可见、头部点显示为圆弧（原因需核实） |
| dmesg | 无 VPSS / oops / OOM |

部署前提：

- 设备原有 `ext_appmgr.conf`（v1.6.5）缺少 `/api/app-center/v1/` 与 `/ws/ai/results/v2` 两个 location，浏览器无法访问新接口；替换为仓库 `market/deploy/ext_appmgr.conf`，执行 `nginx -c /oem/usr/etc/nginx/nginx.conf -t` 通过后 `-s reload`。
- appmgr 重启需用 `/etc/init.d/S94appmgr restart`。手动 setsid 启动的进程缺少脚本导出的 `APPMGR_RELEASE_PUBKEY`，签名包安装报 `cannot open trusted public key vendor`。

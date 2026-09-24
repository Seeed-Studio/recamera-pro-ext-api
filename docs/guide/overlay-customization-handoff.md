# 交接：AI 结果叠加自定义（显示样式覆盖 + geometry 自由绘制）

面向拿到 reCamera Pro 设备和本 GitHub 仓库的开发者：部署、使用、跑示例、验证、回滚。

## 1. 交付内容

| 项 | 位置 | 状态 |
|---|---|---|
| 后端 appmgr：显示样式覆盖 API、Hub 合并与 `render_updated` 广播、生命周期 | 仓库 `suharvest/recamera-pro-ext-api`，分支 **`feat/overlay-customization`**（基于 `upstream/main` `b323972`，未合入 main） | 已实现，已在设备验证 |
| manifest v2：`render.boxes.colors`、`render.subtitles` | 同上 | 已实现 |
| 示例 `examples/11-overlay-geometry`：骨骼 + 文字面板 + 折线自由绘制 | 同上 | 已在设备运行 |
| 文档 | `docs/guide/overlay-customization.md`（使用）、`docs/guide/overlay-customization-design.md`（设计）、本文 | — |
| 前端（设置面板、颜色映射、字幕样式、`render_updated` 处理） | 内部 GitLab `rockchip/linux-app-web-recamera_web_react`，分支 `feat/overlay-customization`（基于 `origin/main` `76e0c92`，3 个提交） | 已实现并在设备验证；**分支未推送**，当前只在开发机 WSL 本地仓库 |
| 阶段 2：沙箱 JS 插件 | 设计见 `overlay-customization-design.md` | **未实现** |

分支提交（`git log upstream/main..feat/overlay-customization`）：

```
docs(overlay): add user-customizable AI result overlay design spec
feat(manifest): accept render.boxes.colors and render.subtitles
feat(appmgr): device-level render override for AI result overlay
fix(appmgr): render override audit calls passed action twice
docs(overlay): usage guide for per-app display style overrides
feat(examples): 11-overlay-geometry free-form browser overlay drawing
docs(overlay): handoff for overlay customization
```

## 2. 两种自定义方式

| | 显示样式覆盖 | geometry 自由绘制 |
|---|---|---|
| 谁来改 | 设备用户，在网页上改 | 应用作者，在 app.py 里写 |
| 改什么 | 已有结果的外观：框颜色映射、标签字段、线宽、字幕字号/颜色/位置、每类事件怎么显示 | 画任意内容：点、线、折线、多边形（可填充）、文字标签 |
| 是否要重新打包 | 否 | 是 |
| 入口 | 应用中心 → 应用「⋮」→「配置」→「显示样式（仅浏览器）」，或 `render-override` API | `kit.geometry.GeometryBuilder` + `self.emit([], pts, geometry=g)` |
| 进入码流/录像 | 否 | 否 |

## 3. 环境前提

- 设备：reCamera Pro（RV1126B），已装扩展固件与 appmgr（`/userdata/local/appmgr`，由 `/etc/init.d/S94appmgr` 启动，监听 `127.0.0.1:8130`）。
- 设备登录：`ssh admin@<设备IP>`；需要 root 的步骤用 `ssh root@<设备IP>`（2026-09-15 在 192.168.10.29 上与 admin 同密码可直接登录；该设备无 adbd）。
- 开发机：Python 3.11+、[uv](https://docs.astral.sh/uv/)、`sshpass`（可选）、`openssl`。
- 取代码：

```bash
git clone git@github.com:suharvest/recamera-pro-ext-api.git
cd recamera-pro-ext-api
git switch feat/overlay-customization
uv sync
```

## 4. 部署到设备

下面每步先备份。`<IP>` 为设备地址，命令在开发机仓库根目录执行。

### 4.0 从出厂固件开始（设备上没有 `/etc/init.d/S94appmgr`、`/userdata/local`）

2026-09-15 在出厂固件 V1.0.10 的设备（192.168.42.1，仅 USB adb，SSH 关闭）上按以下顺序完成，fall-detection 与 overlay-geometry-demo 均运行：

1. **扩展固件**：`release/v1.6.5/deploy-firmware.sh`（或按其逻辑执行包内 `install.sh`），替换 rkipc、entry.cgi、`librecamera_ext.so`，**冷启动** `reboot` 激活。包声明支持固件 V1.0.4 / V1.0.10；出厂文件备份在 `/userdata/*.factory.bak`，回滚用 `/userdata/ext-pkg/rollback.sh --reboot`。
2. **v1.6.5 应用层**：`release/v1.6.5/deploy-app.sh --host <IP>`。脚本固定 `SERIAL="${HOST}:5555"`（网络 adb）；USB 连接时复制脚本，把该行改成 USB serial（`adb devices` 查看）后运行。
3. **仓库 HEAD 的 kit 与 appmgr 必须一起替换**（HEAD appmgr 导入 HEAD kit 才有的 `kit.adapters.output_sink._bounded_render`，只换 appmgr 会启动即 ImportError）：

   ```bash
   COPYFILE_DISABLE=1 tar czf /tmp/kit.tar.gz --exclude='__pycache__' --exclude='*.pyc' --exclude='tests' --exclude='*.bak*' kit
   COPYFILE_DISABLE=1 tar czf /tmp/appmgr.tar.gz --exclude='__pycache__' --exclude='*.pyc' --exclude='tests' -C market appmgr
   COPYFILE_DISABLE=1 tar czf /tmp/inferenced.tar.gz --exclude='__pycache__' --exclude='*.pyc' --exclude='tests' -C market inferenced
   # 设备：三个包都解到 /userdata/local
   ```

4. **`/etc/init.d/S94appmgr` 追加三行**（仓库 HEAD 的默认路径面向固件集成布局 `/usr/lib/...`，`/userdata/local` 布局需指回实际位置）：

   ```sh
   export APPMGR_KIT_PARENT=/userdata/local
   export APPMGR_RELEASE_PUBKEY=/userdata/local/appmgr/keys/release_pub.pem
   export APPMGR_SDK_PYTHON=/userdata/sdk/python
   ```

   | 变量 | 缺失时的现象 |
   |---|---|
   | `APPMGR_KIT_PARENT` | appmgr 找不到 kit，应用无法启动 |
   | `APPMGR_RELEASE_PUBKEY` | 安装签名包报 `cannot open trusted public key vendor` |
   | `APPMGR_SDK_PYTHON` | 应用崩溃 `ModuleNotFoundError: No module named 'recamera_ext'` |

5. **推理调度服务 inferenced**：复制仓库 `market/deploy/S93inferenced` 到 `/etc/init.d/S93inferenced`，只改一行 `PYTHONPATH_VALUE=/userdata/local:/userdata/sdk/python:/usr/lib/python3.11/site-packages`，然后启动。缺失时声明 `npu.rknn` scheduled 的应用（如 fall-detection 0.2.4）停在 `waiting_dependency: scheduled inference service unavailable: /run/recamera/inferenced.sock`。
6. 继续 4.1（重启 appmgr）、4.2（nginx）、4.3（前端），再装应用。

**通过 adb 调 init 脚本**时必须前台执行 `adb shell "setsid /etc/init.d/S94appmgr restart </dev/null >/userdata/_deploy/s94.log 2>&1"`。v1.6.5 的 `S94appmgr` 用 `python3 -m appmgr serve &` 后台启动，adb 会话结束时进程被 HUP，几秒后退出且日志无报错。

### 4.1 appmgr

```bash
# 开发机：打包（排除测试和缓存）
COPYFILE_DISABLE=1 tar czf /tmp/appmgr.tar.gz \
  --exclude='__pycache__' --exclude='*.pyc' --exclude='tests' \
  -C market appmgr
scp /tmp/appmgr.tar.gz root@<IP>:/userdata/_deploy/

# 设备（root）
TS=$(date +%Y%m%d-%H%M%S)
mkdir -p /userdata/_deploy/backups
cp -a /userdata/local/appmgr /userdata/_deploy/backups/appmgr.$TS
tar xzf /userdata/_deploy/appmgr.tar.gz -C /userdata/local     # 只覆盖包内文件，保留 keys/ render-overrides/ 等运行数据
/etc/init.d/S94appmgr restart
```

**必须用 `S94appmgr restart` 重启。** 该脚本导出 `APPMGR_RELEASE_PUBKEY` 和 `APPMGR_KIT_PARENT`；手动 `setsid python3 -m appmgr serve` 起的进程没有这两个变量，之后安装任何签名包都会报 `cannot open trusted public key vendor`。核对：

```bash
tr '\0' '\n' < /proc/$(cat /var/run/appmgr.pid)/environ | grep APPMGR
curl -s http://127.0.0.1:8130/api/appMgr/list | head -c 300
```

重启后几十秒内 install/start 可能返回 `appmgr busy`，间隔几秒重试。

### 4.2 nginx 路由

新前端需要 `/api/app-center/v1/` 与 `= /ws/ai/results/v2` 两个 location。v1.6.5 发布包中的 `ext_appmgr.conf` 没有这两项，缺失时浏览器请求 `/api/app-center/v1/apps` 会拿到 `index.html`，「结果来源」下拉只有系统内置推理。

```bash
scp market/deploy/ext_appmgr.conf root@<IP>:/userdata/_deploy/ext_appmgr.conf.new

# 设备（root）
C=/oem/usr/etc/nginx/ext_appmgr.conf
cp -a $C /userdata/_deploy/backups/ext_appmgr.conf.$(date +%Y%m%d-%H%M%S)
cp /userdata/_deploy/ext_appmgr.conf.new $C && chmod 644 $C
/oem/usr/sbin/nginx -c /oem/usr/etc/nginx/nginx.conf -t \
  && /oem/usr/sbin/nginx -c /oem/usr/etc/nginx/nginx.conf -s reload
curl -sk -o /dev/null -w '%{http_code}\n' https://127.0.0.1/api/app-center/v1/apps   # 未登录应为 401
```

设备 nginx 以 `-c /oem/usr/etc/nginx/nginx.conf` 运行，不带 `-c` 的 `nginx -t` 会去找不存在的 `/etc/nginx/nginx.conf`。`S94appmgr` 在 `/userdata` 还保存一份该 conf 的母本，仅在 live 文件缺失时回注入；如需长期一致，同步更新母本（路径见 `/etc/init.d/S94appmgr` 的 `NGINX_` 变量）。

### 4.3 前端

前端代码不在本仓库。需要 `recamera_web_react` 的 `feat/overlay-customization` 分支构建产物（见第 1 节状态：分支尚未推送，需向维护人获取分支或 `build/` 产物）。

```bash
# 构建机
git switch feat/overlay-customization
npm ci && npm run build
cd build && COPYFILE_DISABLE=1 tar czf /tmp/frontend.tar.gz .
scp /tmp/frontend.tar.gz root@<IP>:/userdata/_deploy/

# 设备（root）
TS=$(date +%Y%m%d-%H%M%S)
cd /oem/usr && tar cf - www | gzip > /userdata/_deploy/backups/www.$TS.tar.gz
rm -rf /tmp/fe && mkdir -p /tmp/fe && tar xzf /userdata/_deploy/frontend.tar.gz -C /tmp/fe
rm -rf /oem/usr/www/static && cp -a /tmp/fe/static /oem/usr/www/static
cd /tmp/fe && for f in *.html *.json *.png *.svg; do [ -f "$f" ] && cp -a "$f" /oem/usr/www/; done
find /oem/usr/www/static -type d -exec chmod 755 {} +
find /oem/usr/www/static -type f -exec chmod 644 {} +
```

不要动 `/oem/usr/www/cgi-bin` 和 `sdcard/usb0/userdata` 软链。目录权限必须 755，否则 nginx 读不到 JS、页面回落为 HTML。

## 5. 使用显示样式覆盖

以跌倒检测（`fall-detection`，状态字段 `state`）为例。

**网页**：`https://<IP>/app-center` → 跌倒检测「⋮」→「配置」→「显示样式（仅浏览器）」：

1. 「取值颜色映射」添加 `fallen → #FF0000`、`standing → #00FF00`；「线宽」填 4。
2. 「字幕」字号 28，位置「上方居中」。
3. 点「保存显示样式」。已打开的 `https://<IP>/preview`（结果来源选 Fall Detection）立即按新样式重绘。
4. 「恢复默认」删除全部设置。

**API**（设备本机直连 appmgr，免 Cookie；外部访问走 `https://<IP>/api/...` 并带登录 Cookie）：

```bash
B=http://127.0.0.1:8130/api/app-center/v1/apps/fall-detection/render-override

curl -s $B                                        # 读取，记下 revision
curl -s -X PUT $B -H 'Content-Type: application/json' -d '{
  "revision": 0,
  "override": {
    "boxes": {"colors": {"fallen": "#FF0000", "standing": "#00FF00"}, "line_width": 4},
    "subtitles": {"font_size": 28, "position": "top-center"},
    "events": {"fall": {"as": "toast", "text": "跌倒 #{{ track_id }}", "duration_sec": 6}}
  }}'
curl -s -X DELETE "$B?revision=1"                 # 恢复默认
```

字段全集、取值范围、400/409 错误格式见 [overlay-customization.md](./overlay-customization.md)。

## 6. 跑 geometry 示例

### 6.1 本地校验

```bash
uv run pytest examples/11-overlay-geometry -q
```

### 6.2 打包

```bash
uv run python market/packaging/build.py examples/11-overlay-geometry --out /tmp/dist
# 产物 /tmp/dist/overlay-geometry-demo-0.1.0-arm64.tar.gz
```

### 6.3 签名与安装

设备默认要求签名（`APPMGR_REQUIRE_SIGNATURE=1`）。三种方式任选：

**A. 网页上传（无需密钥）**：`https://<IP>/app-center` →「上传应用」→ 选择 tar.gz。本地网页上传允许未签名包，确认权限后安装，安装完成后是停止状态，需在卡片上点「启动」。

**B. 设备所有者密钥（推荐用于自己的设备）**：

```bash
# 开发机：生成自己的密钥对（私钥不要放进仓库或设备）
mkdir -p ~/.recamera_owner_key && cd ~/.recamera_owner_key
openssl ecparam -name prime256v1 -genkey -noout -out owner_priv.pem && chmod 600 owner_priv.pem
openssl ec -in owner_priv.pem -pubout -out owner_pub.pem
cd -

# 签名
uv run python market/packaging/sign.py --dist /tmp/dist \
  --key ~/.recamera_owner_key/owner_priv.pem \
  --pub ~/.recamera_owner_key/owner_pub.pem \
  --pkg overlay-geometry-demo-0.1.0-arm64.tar.gz

# 设备（root）：放入 owner 信任目录；目录和文件属主为 appmgr 运行用户（root），不可组/其他可写
mkdir -p /userdata/local/appmgr/keys/owners && chmod 755 /userdata/local/appmgr/keys/owners
# scp owner_pub.pem 到 /userdata/local/appmgr/keys/owners/owner.pem 后：
chmod 644 /userdata/local/appmgr/keys/owners/owner.pem
```

然后上传并安装：

```bash
scp /tmp/dist/overlay-geometry-demo-0.1.0-arm64.tar.gz /tmp/dist/overlay-geometry-demo-0.1.0-arm64.tar.gz.sig \
    root@<IP>:/userdata/_deploy/

# 设备（root）
SIG=$(tr -d '\n' < /userdata/_deploy/overlay-geometry-demo-0.1.0-arm64.tar.gz.sig)
curl -s -X POST --data-binary @/userdata/_deploy/overlay-geometry-demo-0.1.0-arm64.tar.gz \
  -H 'X-Filename: overlay-geometry-demo-0.1.0-arm64.tar.gz' \
  http://127.0.0.1:8130/api/appMgr/upload          # 返回 {"path": "/userdata/appstage/..."}
curl -s -X POST http://127.0.0.1:8130/api/appMgr/install -H 'Content-Type: application/json' \
  -d "{\"path\":\"/userdata/appstage/overlay-geometry-demo-0.1.0-arm64.tar.gz\",\"signature\":\"$SIG\"}"
```

upload 请求体为原始 tar.gz 字节，文件名放在 `X-Filename` 请求头（`market/appmgr/server.py` 文件头注释）。install 成功时 install 返回 `"signature": {"verified": true, "signer_kind": "owner"}`。

**C. 厂商密钥**：持有 `~/.recamera_release_key/release_priv.pem` 的维护人按 `market/packaging/SIGNING.md` 签名，`signer_kind` 为 `vendor`。

### 6.4 启动与查看

```bash
curl -s -X POST http://127.0.0.1:8130/api/appMgr/start -H 'Content-Type: application/json' \
  -d '{"id":"overlay-geometry-demo"}'
```

- 用 `/start`，不要用 `/switch` 或 `/activate`：后两者单活，会停掉其他正在运行的应用。
- 该示例声明 96 MB 内存、不占 NPU，可与 fall-detection 同时运行。
- 打开 `https://<IP>/preview`，「结果来源」选 **Overlay Geometry Demo**：左侧骨骼、右上文字面板（帧号和时间每帧更新）、底部滚动折线。

### 6.5 自己写绘制

复制 `examples/11-overlay-geometry`，改 `manifest.json` 的 `id`/`name`，在 `app.py` 的 `frames()` 循环里组装图元：

```python
from kit.geometry import GeometryBuilder

for frame in self.frames():
    g = GeometryBuilder()
    g.pose(keypoints, skeleton_pairs, color="#ff3040", line_width=3)      # 骨骼
    g.polygon(((x1, y1), (x2, y1), (x2, y2), (x1, y2)), color="#ffffff")   # 区域/面板框
    g.point(x1 + 10, y1 + 30, label="任意文字 ≤128 字符", color="#ffd400")  # 文字
    g.polyline(points, color="#00ffa0", line_width=2)                     # 轨迹/曲线
    self.emit(results, frame.pts, geometry=g)
```

坐标为原始帧像素。限额与样式字段见 [examples/11-overlay-geometry/README.md](../../examples/11-overlay-geometry/README.md)。

## 7. 验证清单

| 检查 | 命令 / 操作 | 期望 |
|---|---|---|
| appmgr 环境 | `tr '\0' '\n' < /proc/$(cat /var/run/appmgr.pid)/environ \| grep APPMGR` | 含 `APPMGR_RELEASE_PUBKEY` |
| nginx 路由 | `curl -sk -o /dev/null -w '%{http_code}' https://127.0.0.1/api/app-center/v1/apps` | 401（未登录） |
| 覆盖读写 | 第 5 节 PUT | 200，revision +1 |
| 非法值 | PUT `"line_width": 99` | 400，`field=boxes.line_width` |
| 实时通知 | 连 `ws://127.0.0.1:8125` 后 PUT | 收到 `type=render_updated`，后续 frame 的 `render` 含新值 |
| 网页 | 预览页结果来源下拉 | 列出正在运行的应用 |
| 内核 | `dmesg \| grep -iE 'vpss\|oops\|oom'` | 无新增 |
| 后端测试 | `uv run pytest market/appmgr/tests/test_render_override.py examples/11-overlay-geometry -q` | 全部通过 |

在 macOS 上跑 `market/appmgr/tests` 全量，`upstream/main` 原样即有 35 个失败：7 个为 macOS Unix socket 路径长度上限（短 `TMPDIR` 可消除），26 个依赖 Linux 专有能力（`/proc/sys/kernel/random/boot_id`、`/proc/self/fd`、`SOCK_SEQPACKET`、`SO_PEERCRED`），2 个为 macOS socket 发送缓冲语义差异。本分支不新增失败。全量测试需在 Linux 上运行。

## 8. 回滚

```bash
# 设备（root），<TS> 为备份时间戳
rm -rf /userdata/local/appmgr && cp -a /userdata/_deploy/backups/appmgr.<TS> /userdata/local/appmgr
/etc/init.d/S94appmgr restart

cp -a /userdata/_deploy/backups/ext_appmgr.conf.<TS> /oem/usr/etc/nginx/ext_appmgr.conf
/oem/usr/sbin/nginx -c /oem/usr/etc/nginx/nginx.conf -t && /oem/usr/sbin/nginx -c /oem/usr/etc/nginx/nginx.conf -s reload

gzip -dc /userdata/_deploy/backups/www.<TS>.tar.gz | tar xf - -C /oem/usr

# 只撤销某个应用的显示样式
curl -s -X DELETE "http://127.0.0.1:8130/api/app-center/v1/apps/<app-id>/render-override?revision=<当前revision>"
```

## 9. 已知问题

| 问题 | 影响 | 状态 |
|---|---|---|
| 显示样式只作用于浏览器 | 烧录 OSD/RTSP/录像不变 | 设计如此（OSD ABI 无颜色/字幕参数） |
| 保存后新连接的浏览器，在应用下一帧前回放旧样式 | 应用停止发帧时保持旧样式 | 未处理 |
| 预览页 `toast` 在上方居中，与对齐提示位置相同 | 可能重叠 | 未处理 |
| geometry polygon 半透明填充在画面上不可见 | 面板底色不显示 | 原因需核实 |
| geometry 点在部分位置显示为小圆弧 | 视觉 | 原因需核实 |
| 标签字号、字体、底色由前端固定；每帧最多 48 个标签 | 文字排版受限 | 需要时走阶段 2 |
| 预览页出现的 `suitcase` / `chair` 等框 | 来自系统内置检测（推断为烧录进码流的 OSD，切换结果来源后仍显示），不是 overlay 应用绘制 | 需核实 |
| fall-detection 运行时 voice-transcribe 因内存准入被拒（需 768+256 MiB，可用约 996 MiB） | 两者不能同时跑 | 与本功能无关，未处理 |

## 10. 设备当前状态

### 192.168.42.1（2026-09-15，USB，出厂 V1.0.10 起装）

- 扩展固件 v1.6.5（rkipc `f683352a…`），v1.6.5 应用层，其上替换为仓库 `integrate/full-20260909` `5fc3ea4` 的 kit / appmgr / inferenced。
- `S94appmgr` 追加 4.0 节三行 env；新增 `S93inferenced`。
- nginx `ext_appmgr.conf` 为仓库版本；前端 `main.a3475a1e.js`。
- 运行中：`fall-detection`、`overlay-geometry-demo`；fall-detection 显示样式覆盖 revision 2。
- 备份：`/userdata/_deploy/backups/`（`20260915T082444Z` 固件前、`20260915-163351` 与 `20260915T164456Z` 应用层、`20260915_170217` S94appmgr）。

### 192.168.10.29（2026-09-15）

- appmgr：`integrate/full-20260909` 分支代码（与本分支后端提交相同），经 `S94appmgr` 启动。
- 前端：`recamera_web_react` `feat/overlay-customization` 构建（`main.a3475a1e.js`）。
- nginx：`ext_appmgr.conf` 已替换为仓库版本；`/userdata` 母本未同步。
- 运行中：`fall-detection`、`overlay-geometry-demo`。`f1-access` 已停止。
- fall-detection 显示样式覆盖 revision 4（`fallen` 红、`standing` 绿、线宽 4、字幕 28px 上方居中）。
- 备份：`/userdata/_deploy/backups/`（`appmgr.*`、`www.20260915-111707.tar.gz`、`ext_appmgr.conf.*`、`state.json.*`）。

## 11. 待决事项

1. 前端分支提交到官方仓库的方式（GitLab MR 或 Gerrit），以及由谁合并。
2. 阶段 2 沙箱 JS 插件是否实施；实施前先在设备验证 sandbox iframe 以 `Origin: null` 加载资源能否通过现有 nginx。
3. 本分支是否提 PR 到 `Seeed-Studio/recamera-pro-ext-api`。

# reCamera Pro v1.6.1 — 一键部署包 (application layer)

> **历史归档，禁止用于当前固件部署。** 本目录沿用 v1.6.0 的预编译固件/
> SDK，不含 `inference-control@1`，也不是当前源码构建出的统一发布集。不要
> 运行 `deploy-firmware.sh` 或按旧 md5 安装。

把本轮全部应用层改动一次性部署到设备，让设备达到 v1.6.1 完整状态。
**固件层（rkipc 遮罩固件）单独、高危、默认不在一键流程内** —— 见文末。

> **本版仅前端变更：AI overlay 事件 feed 按 kind 去重（metrics 不再堆叠），其余产物与 v1.6.0 逐字节一致。**
> 前端 bundle 从 `main.ba670e80.js` 更新为 `main.024cbb4e.js`（commit `8f7ae4d`，分支 `feat/ext-api`）。
> `appmgr` / `apps` 由确定性构建重新生成，md5 与 v1.6.0 完全相同；`recamera-ext-api` / `recamera-ext-kit` /
> 两个运行时 / App Center `catalog.json` 与 `packages/` 均未变动。

**固件层与 v1.6.0 相同**：`rkipc` 仍为 `f683352a…`（与 v1.3.0 / v1.4.0 / v1.5.0 的 `9826e9ec…` 不同）。
`entry.cgi`（`75a693c8…`）与扩展库 `librecamera_ext.so.1.0.0`（`5cebfb9e…`）未变。
仅需应用层能力的设备跑 `deploy-app.sh` 即可。

## 与 v1.6.0 的产物差异

| 产物 | 与 v1.6.0 | 说明 |
|------|-----------|------|
| `frontend-v1.6.1.tar.gz` | **不同** | React 前端重建（bundle `main.024cbb4e.js`）—— AI overlay 事件按 kind 去重 |
| `appmgr-v1.6.1.tar.gz` | **相同**（md5 `0f5388d3…`） | 源码未变，确定性重建逐字节一致 |
| `apps-v1.6.1.tar.gz` | **相同**（md5 `3e667757…`） | 9 个 app 源码未变，确定性重建逐字节一致 |
| `recamera-ext-api-v1.6.1.tar` | **相同**（md5 `ad3e2f15…`） | v1.6.0 逐字节副本重命名 |
| `recamera-ext-kit-v1.6.1.tar.gz` | **相同**（md5 `83259d7b…`） | v1.6.0 逐字节副本重命名 |
| `voice-runtime-1.0.0.tar.gz` | **相同**（md5 `ace48a68…`） | 音频运行时未改 |
| `gst-hwcodec-1.0.0.tar.gz` | **相同**（md5 `8e6d286f…`） | 硬编解码运行时未改 |
| `deploy-app.sh` | 不同（仅版本号） | `VER=1.6.1` |
| `deploy-firmware.sh` | 不同（仅版本号） | `VER=1.6.1` |

> App Center 目录不变：`catalog.json` 与 `packages/` 仍是 v1.6.0 的内容（overlay 修复在前端 bundle，不在任何 app 包内）。

## 目录内容

| 文件 | 内容 | 部署目标 |
|------|------|----------|
| `recamera-ext-kit-v1.6.1.tar.gz` | kit 运行时（含 `kit/run.py`、`kit/events.py`）+ SDK(python/lib/头文件) + 离线推理 wheels（含 rknnlite / jinja2 / markupsafe），自带 `INSTALL.sh` | `/userdata/local/kit`、`/userdata/sdk`、`/userdata/rknnenv` |
| `appmgr-v1.6.1.tar.gz` | App Center 管理器全部代码（builtin/config/server/assets/supervisor/voiceruntime…）**含签名公钥 `keys/`**；已排除 `__pycache__`/`tests`/`*.bak*` | `/userdata/local/appmgr` |
| `frontend-v1.6.1.tar.gz` | React 前端构建产物（bundle `main.024cbb4e.js`） | `/oem/usr/www` |
| `apps-v1.6.1.tar.gz` | 9 个 app 的 `manifest.json` + `app.py`（+ 小配置）。**不含大模型 `*.rknn`/`*.onnx`** | `/userdata/local/apps` |
| `voice-runtime-1.0.0.tar.gz` | 按需音频运行时（`runtimes.audio`，`kind:"pip"`），仅音频应用需要 | 由 appmgr 按需拉取安装 |
| `gst-hwcodec-1.0.0.tar.gz` | 按需硬编解码运行时（`runtimes.hwcodec`，`kind:"files"`），解包到 `/userdata/lib` | 由 appmgr 按需拉取安装 |
| `recamera-ext-api-v1.6.1.tar` | 遮罩固件（rkipc + entry.cgi + 扩展 `.so` + SDK + wheels），自带 `install.sh`/`rollback.sh`。**与 v1.6.0 逐字节一致** | `/oem`（**高危，冷启动**） |
| `deploy-app.sh` | 应用层一键部署主脚本（安全） | — |
| `deploy-firmware.sh` | 遮罩固件部署脚本（**高危，单独跑**） | — |

包的 md5 / size 在文末「校验」一节列出。

## 从 CDN 下载

全部产物发布在 SenseCraft CDN，前缀
`https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/v1.6.1/`：

| 文件 | URL |
|------|-----|
| `recamera-ext-kit-v1.6.1.tar.gz` | https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/v1.6.1/recamera-ext-kit-v1.6.1.tar.gz |
| `recamera-ext-api-v1.6.1.tar` | https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/v1.6.1/recamera-ext-api-v1.6.1.tar |
| `appmgr-v1.6.1.tar.gz` | https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/v1.6.1/appmgr-v1.6.1.tar.gz |
| `frontend-v1.6.1.tar.gz` | https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/v1.6.1/frontend-v1.6.1.tar.gz |
| `apps-v1.6.1.tar.gz` | https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/v1.6.1/apps-v1.6.1.tar.gz |
| `voice-runtime-1.0.0.tar.gz` | https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/v1.6.1/voice-runtime-1.0.0.tar.gz |
| `gst-hwcodec-1.0.0.tar.gz` | https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/v1.6.1/gst-hwcodec-1.0.0.tar.gz |
| `deploy-app.sh` | https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/v1.6.1/deploy-app.sh |
| `deploy-firmware.sh` | https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/v1.6.1/deploy-firmware.sh |
| `README.md` | https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/v1.6.1/README.md |

应用商店走另一条路径（**本版未变，仍指向 v1.6.0 目录内容**）：`catalog.json` 在
`https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/catalog.json`，
应用包与两个按需运行时在
`https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/packages/`。

```bash
# 拉全套到本地 deploy 目录，赋可执行权限后跑 deploy-app.sh
BASE=https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/v1.6.1
mkdir -p deploy && cd deploy
for f in recamera-ext-kit-v1.6.1.tar.gz recamera-ext-api-v1.6.1.tar \
         appmgr-v1.6.1.tar.gz frontend-v1.6.1.tar.gz apps-v1.6.1.tar.gz \
         voice-runtime-1.0.0.tar.gz gst-hwcodec-1.0.0.tar.gz \
         deploy-app.sh deploy-firmware.sh README.md; do
  curl -fSL "$BASE/$f" -o "$f"
done
chmod +x deploy-app.sh deploy-firmware.sh
# 下载后按文末「校验」表核对 md5
```

## 前提

- **设备**：reCamera Pro（RV1126B），已开机、网络可达（IP 易变，先确认当前地址）。
- **控制机（Mac）**：装了 `adb`。root 操作走 **adb**（设备 `adbd` 以 root 运行，`<ip>:5555`），无需 SSH 密码，无需 `sudo`/包管理器。
- **磁盘**：`/userdata` 需 ~200 MB 余量（含备份）；若装音频应用另需 ~200 MB（模型 + 运行时）。
- 幂等：脚本可重复跑；每次替换目录前按时间戳备份到 `/userdata/_deploy/backups/`。

## 一键部署（应用层，推荐）

```bash
./deploy-app.sh                       # 默认 host 192.168.42.1
./deploy-app.sh --host 192.168.10.x   # 指定设备 IP
./deploy-app.sh --skip-kit            # kit 已装，只更 appmgr/前端/apps
./deploy-app.sh --no-activate         # 不启动 app、不碰摄像头
```

> **仅前端修复的快速路径**：本版唯一变化是前端 bundle。已在 v1.6.0 的设备只需重推前端
> （`deploy-app.sh --skip-kit` 后其余步骤幂等，或手动替换 `/oem/usr/www` 的 `static/`），
> appmgr / apps / 固件均无须重装。

### 部署顺序（脚本内部，每步幂等 + dmesg 查 vpss）

1. **kit + SDK + wheels** —— push kit 包，跑其 `INSTALL.sh`：装 kit 到 `/userdata/local/kit`，SDK 到 `/userdata/sdk`，离线 wheels 到 venv `/userdata/rknnenv`。旧副本按时间戳备份。
2. **appmgr** —— 备份旧 `/userdata/local/appmgr`，merge-extract 新代码（覆盖 `.py` + `keys/`，保留运行态 `audit.log`/`mqtt.json`/锁）；**setsid 重启 `python3 -m appmgr serve`**，起来后从 `/proc` 回读真实 pid 写 pidfile，验证 `127.0.0.1:8130` 存活。
   - 停旧进程用 `/proc` cmdline 扫描 + pidfile，**不用 `pkill -f`**（会匹配到重启命令本身、误杀自己的 shell）。
3. **前端** —— 备份 `/oem/usr/www`；`static/` 整目录替换（清掉旧 hash bundle），顶层 html/json/png/svg 覆盖；**目录 chmod 755 / 文件 644**，**不碰 `cgi-bin`、不碰 `sdcard/usb0/userdata` 软链**。
4. **apps** —— 备份 `state.json`，merge-extract 9 个 app 的代码+manifest，**保留设备上已有的大模型文件**。
5. **激活 + 校验** —— 调 `POST /api/appMgr/switch` 激活一个 app（默认 `retail-vision`），跑 `ws_probe.py` 确认 `:8124` 结果流出帧；查 dmesg 无 vpss。

### 安全护栏（脚本内置）

- **绝不碰 rkipc**：部署前后各取一次 `/oem/usr/bin/rkipc` md5，收尾断言未变，变了直接 FATAL。
- 每个碰摄像头的步骤后查 `dmesg`；出现 `vpss err` / `CSIBDG fifo overflow` / `Oops` / `Unable to handle kernel` 立即 STOP。
- 大文件 push 用 **md5 校验 + 重试**（adb over Tailscale 大包偶发 `EOF`，按 md5 判定成功）。

## 按需运行时

两个运行时都不进 kit 包，由应用商店在安装声明了对应 capability 的应用时按需拉取：

| 运行时 | catalog key | kind | 落点 | 触发条件 |
|--------|-------------|------|------|----------|
| 音频（sherpa-onnx / voxedge 等 5 个 aarch64 wheel，18 MB） | `runtimes.audio` | `pip` | `/userdata/rknnenv` | 应用 `capabilities` 含 `audio`（本版为 `voice-transcribe`） |
| GStreamer RK 硬编解码（3 个 `.so`，0.4 MB） | `runtimes.hwcodec` | `files` | `/userdata/lib` | 应用 `capabilities` 含 `hwcodec` |

两个运行时在 `catalog.json` 中均带 detached 签名（`signature` / `signature_alg: ecdsa-sha256`），
设备用内置公钥 `market/appmgr/keys/release_pub.pem` 验签后才解包（默认 `require_signature=1`）。

> **硬编解码的验证边界**：`mppvideodec`（硬件解码）已在真机跑通。
> `mpph264enc` / `mpph265enc`（硬件编码）随包分发但**未做验证**，不要按可用对待。

> **不要为了装音频运行时去升级 numpy。** `/userdata/rknnenv` 是共享 venv，9 个视觉 app 靠里面的
> `rknn-toolkit-lite2 2.3.2`，设备上 numpy 是 1.23.5。离线安装走 `--no-deps`。

## 固件层部署（遮罩固件，高危，单独跑）

> ⚠️ **换 rkipc 必须冷启动（整机 `reboot`）激活。热替换会触发 `cv181x_vpss` / CSIBDG FIFO 内核 oops，可能把设备搞挂。远程执行有风险 —— 只在你能物理复位设备时才跑。**

**本版固件与 v1.6.0 逐字节一致**（`rkipc` 为 `f683352a…`，与 v1.3.0 / v1.4.0 / v1.5.0 的 `9826e9ec…` 不同）。
已在 v1.6.0 装过遮罩固件的设备无须重装；`entry.cgi` 与扩展 `.so` 未变。

`deploy-app.sh` 不依赖遮罩固件也能把 apps/前端/appmgr 跑起来。仅当需要软件叠加 OSD / 结果注入等扩展 API 能力时才装固件。

```bash
./deploy-firmware.sh --host <ip>              # 安装：md5 校验→备份原厂 rkipc→装入 /oem→停在 reboot 前
./deploy-firmware.sh --host <ip> --reboot     # 装完立即重启
./deploy-firmware.sh --host <ip> --rollback   # 回滚到原厂 rkipc（用 /userdata/rkipc.factory.bak）
```

脚本会要求输入 `I-HAVE-PHYSICAL-ACCESS` 确认。

## 回滚

- **应用层**：每次跑 `deploy-app.sh` 都会在 `/userdata/_deploy/backups/` 留时间戳备份
  （`appmgr.<ts>/`、`www.<ts>.tar.gz`、`state.json.<ts>`）；kit/SDK 旧副本在
  `/userdata/local/kit.bak.<ts>`、`/userdata/sdk/*.bak.<ts>`。手动 `cp -a` / 解 tar 回去即可。
- **版本回退**：`release/v1.6.0/` 与 `release/v1.5.0/` 两条路径在 CDN 上原样保留。
- **固件层**：`./deploy-firmware.sh --host <ip> --rollback`（内部跑包里的 `rollback.sh`，恢复原厂 rkipc 后需冷启动）。

## 重新打包（可选，确定性）

`appmgr` / `frontend` / `apps` 三个包由 `build-packages.py` 确定性生成（tar 元数据归零 + gzip mtime=0，同输入→同 md5）：

```bash
python3 build-packages.py --frontend /path/to/recamera_web_react/build --version 1.6.1
```

在 macOS 上打包前端要 `COPYFILE_DISABLE=1` 解包源目录，否则 `._*` AppleDouble 文件会混进 `/oem/usr/www`。

`recamera-ext-kit` / `recamera-ext-api` 本版为 v1.6.0 的逐字节副本重命名，未重新构建。

## 校验（本轮产物 md5 / size）

本表是本版所有产物校验值的**唯一权威来源**。

### release/v1.6.1/

| 包 | size (bytes) | md5 | 与 v1.6.0 |
|----|-------------:|-----|-----------|
| `recamera-ext-api-v1.6.1.tar` | 18708480 | `ad3e2f158218fe3f127065a498dd8836` | **相同**（逐字节副本） |
| `recamera-ext-kit-v1.6.1.tar.gz` | 2222965 | `83259d7b5387927fbf90de9d2afb59e0` | **相同**（逐字节副本） |
| `appmgr-v1.6.1.tar.gz` | 65160 | `0f5388d3aec1ddf5fed86f6a9dceec56` | **相同**（确定性重建） |
| `frontend-v1.6.1.tar.gz` | 36757258 | `d0f00b05ed4aed1f12aaae45e07514d4` | **不同**（overlay 修复，bundle `main.024cbb4e.js`） |
| `apps-v1.6.1.tar.gz` | 986788 | `3e6677572e044dc2d1fc25da7ffdda52` | **相同**（确定性重建） |
| `voice-runtime-1.0.0.tar.gz` | 18856604 | `ace48a688d41a3fc6b852a0f14ddad8d` | **相同** |
| `gst-hwcodec-1.0.0.tar.gz` | 425137 | `8e6d286fac58a5b366e8fdd1709b212f` | **相同** |
| `deploy-app.sh` | 16636 | `ac6ad51c1911696b63f4c76e0f0d3d5a` | 不同（仅版本号） |
| `deploy-firmware.sh` | 4945 | `c11d8d84f25a6863607f1d17ac503a24` | 不同（仅版本号） |

`recamera-ext-api-v1.6.1.tar` 内附固件产物（与 v1.6.0 相同）：

| 固件产物 | size (bytes) | md5 | 与 v1.5.0 |
|----------|-------------:|-----|-----------|
| `rkipc` | 15585904 | `f683352a9d062a05a3df1f8df22d7d53` | 不同（v1.6.0 起更新） |
| `entry.cgi` | 1057168 | `75a693c87c317a49c37c4dddb6b9ac7a` | 相同 |
| `sdk/lib/librecamera_ext.so.1.0.0` | 89496 | `5cebfb9e4d9c001c45b58c75daafe934` | 相同 |

内附 `MANIFEST.txt` 记录 `factory rkipc md5 9826e9ecf8ed543a6dc78e3731102e0f`（回滚目标）。

### packages/（应用商店，`catalog.json` 引用）

**本版未改动**：`catalog.json` 与 9 个应用包、两个运行时的签名和 sha256 与 v1.6.0 完全一致，App Center 仍处于 v1.6.0 状态。校验值见 `release/v1.6.0/README.md`。

# reCamera Pro v1.6.2 — 一键部署包 (application layer)

把本轮全部应用层改动一次性部署到设备，让设备达到 v1.6.2 完整状态。
**固件层（rkipc 遮罩固件）单独、高危、默认不在一键流程内** —— 见文末。

> **本版四项变更**：
> 1. **兼容固件 HTTPS 开关**（`kit/adapters/cgi_control.py`、`appmgr/builtin.py`）：entry.cgi 客户端对 301/302/307/308 跟随一跳、记住落点端点（443↔80）、443 TLS 不可达时回退 80。HTTPS 开/关两态及来回切换已真机验证（2026-08-17/18）。
> 2. **固件包回滚判据重构**（`install.sh` / `rollback.sh`）：回滚目标从 md5 白名单改为**按内容判定**（扩展标记 `/run/recamera|rc_ext_`、`ExtApiHandler`），未知出厂固件版本也能安全回滚；`deploy-firmware.sh` 透传 `--strict` / `--force`。
> 3. **前端应用卡片描述悬停显示完整文字**：卡片描述仍三行截断，悬停浮出全文（原生 `title` 提示，bundle `main.1e2af984.js`）。
> 4. **`deploy-app.sh` 默认不再预装应用**（行为变更）：`apps-v1.6.2.tar.gz` 只含代码和 manifest、**不含模型**（9 个 app 合计 987 KB），推上去的结果是 9 个 app 全显示"已安装"、一启动就 `Invalid RKNN model path`。应用中心的安装路径拉的是 catalog 上**含模型的完整包**（单个 3–53 MB），装完即可用。现在出厂设备保持干净，由用户按需安装；第 5 步的激活一并跳过（没有东西可激活）。要恢复旧行为（演示机 / 装机站预装，模型另行下发）加 `--with-apps`。

**固件产物（rkipc / entry.cgi / `.so`）与 v1.6.0、v1.6.1 逐字节相同**，变的只是包内安装/回滚脚本。
已装过 v1.6.0/v1.6.1 遮罩固件的设备**不需要重装固件**；仅需应用层能力的设备跑 `deploy-app.sh` 即可。

## 与 v1.6.1 的产物差异

| 产物 | 与 v1.6.1 | 说明 |
|------|-----------|------|
| `recamera-ext-kit-v1.6.2.tar.gz` | **不同** | `cgi_control.py` 跟随 307 / 记忆端点 / TLS 回退 |
| `appmgr-v1.6.2.tar.gz` | **不同** | `builtin.py` 同上 |
| `recamera-ext-api-v1.6.2.tar` | **不同** | 仅 `install.sh`/`rollback.sh`/`MANIFEST.txt`/`README.md` 变（回滚判据按内容判定）；rkipc/entry.cgi/`.so` 逐字节同 v1.6.1 |
| `frontend-v1.6.2.tar.gz` | **不同** | 卡片描述悬停 tooltip（bundle `main.1e2af984.js`） |
| `apps-v1.6.2.tar.gz` | **相同**（md5 `3e667757…`） | 9 个 app 源码未变 |
| `voice-runtime-1.0.0.tar.gz` | **相同**（md5 `ace48a68…`） | 音频运行时未改 |
| `gst-hwcodec-1.0.0.tar.gz` | **相同**（md5 `8e6d286f…`） | 硬编解码运行时未改 |
| `deploy-app.sh` / `deploy-firmware.sh` | 不同 | `VER=1.6.2`；**app 版默认不再预装应用（新增 `--with-apps` 才推）**；firmware 版新增透传 `--strict`/`--force` |
| `S94appmgr` / `ext_appmgr.conf` | 相同 | 与 v1.6.1 逐字节一致 |

> App Center 目录不变：`catalog.json` 与 `packages/` 仍是 v1.6.0 的内容（本版改动在 kit 运行时与 appmgr，不在任何 app 包内）。

## HTTPS 开关兼容性（本版核心变更）

固件自带 HTTPS 开关（Web UI「连接 → HTTPS」，即 `POST /cgi-bin/entry.cgi/system/secure {"sEnable": bool}`）：

- **开**（出厂默认）：80 → `307 https://`，entry.cgi 只在 443 服务
- **关**：443 → `307 http://`，entry.cgi 只在 80 服务

v1.6.1 及更早版本的 kit / appmgr 硬编码打 443 且不跟随重定向 —— HTTPS 一关，内建推理开关（builtin driver）与 kit 的 CgiControl 立即报 `HTTP 307`。本版起两种状态、以及运行期来回切换，应用层都无须任何改动。

## 目录内容

| 文件 | 内容 | 部署目标 |
|------|------|----------|
| `recamera-ext-kit-v1.6.2.tar.gz` | kit 运行时 + SDK(python/lib/头文件) + 离线推理 wheels，自带 `INSTALL.sh` | `/userdata/local/kit`、`/userdata/sdk`、`/userdata/rknnenv` |
| `appmgr-v1.6.2.tar.gz` | App Center 管理器全部代码，**含签名公钥 `keys/`** | `/userdata/local/appmgr` |
| `frontend-v1.6.2.tar.gz` | React 前端构建产物（bundle `main.1e2af984.js`） | `/oem/usr/www` |
| `apps-v1.6.2.tar.gz` | 9 个 app 的 `manifest.json` + `app.py`（+ 小配置）。**不含大模型** —— 因此 `deploy-app.sh` 默认**不推**它，见上文第 3 项 | `/userdata/local/apps`（仅 `--with-apps`） |
| `voice-runtime-1.0.0.tar.gz` | 按需音频运行时（`runtimes.audio`，`kind:"pip"`） | 由 appmgr 按需拉取安装 |
| `gst-hwcodec-1.0.0.tar.gz` | 按需硬编解码运行时（`runtimes.hwcodec`，`kind:"files"`） | `/userdata/lib` |
| `recamera-ext-api-v1.6.2.tar` | 遮罩固件（rkipc + entry.cgi + `.so` + SDK + wheels），自带**内容判定版** `install.sh`/`rollback.sh` | `/oem`（**高危，冷启动**） |
| `S94appmgr` / `ext_appmgr.conf` | 开机启动脚本 / nginx 边缘配置（`deploy-app.sh` 第 2b 步自动安装） | `/etc/init.d/`、`/oem/usr/etc/nginx/` |
| `deploy-app.sh` | 应用层一键部署主脚本（安全；默认不预装应用，`--with-apps` 才推） | — |
| `deploy-firmware.sh` | 遮罩固件部署脚本（**高危，单独跑**；支持 `--strict`/`--force`） | — |

包的 md5 / size 在文末「校验」一节列出。

## 从 CDN 下载

全部产物发布在 SenseCraft CDN，前缀
`https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/v1.6.2/`：

| 文件 | URL |
|------|-----|
| `recamera-ext-kit-v1.6.2.tar.gz` | https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/v1.6.2/recamera-ext-kit-v1.6.2.tar.gz |
| `recamera-ext-api-v1.6.2.tar` | https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/v1.6.2/recamera-ext-api-v1.6.2.tar |
| `appmgr-v1.6.2.tar.gz` | https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/v1.6.2/appmgr-v1.6.2.tar.gz |
| `frontend-v1.6.2.tar.gz` | https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/v1.6.2/frontend-v1.6.2.tar.gz |
| `apps-v1.6.2.tar.gz` | https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/v1.6.2/apps-v1.6.2.tar.gz |
| `voice-runtime-1.0.0.tar.gz` | https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/v1.6.2/voice-runtime-1.0.0.tar.gz |
| `gst-hwcodec-1.0.0.tar.gz` | https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/v1.6.2/gst-hwcodec-1.0.0.tar.gz |
| `deploy-app.sh` | https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/v1.6.2/deploy-app.sh |
| `deploy-firmware.sh` | https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/v1.6.2/deploy-firmware.sh |
| `S94appmgr` | https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/v1.6.2/S94appmgr |
| `ext_appmgr.conf` | https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/v1.6.2/ext_appmgr.conf |

## 部署

```bash
cd release/v1.6.2
./deploy-app.sh --host <设备IP>          # 应用层 5 步，不碰 rkipc
./deploy-app.sh --host <ip> --skip-kit   # kit 没变时
./deploy-app.sh --host <ip> --with-apps  # 演示机/装机站：连不含模型的 app 包一起推
```

**默认跑完设备上不会有任何应用**——这是有意的，应用由用户在应用中心按需安装（那条路径下载的是含模型的完整包）。

流程细节（备份、幂等、回滚）见 `release/deploy/DEPLOY.md`。

## 固件层部署（遮罩固件，高危，单独跑）

> ⚠️ **换 rkipc 必须冷启动（整机 `reboot`）激活。热替换会触发 `cv181x_vpss` / CSIBDG FIFO 内核 oops。只在你能物理复位设备时才跑。**

**本版固件产物与 v1.6.0/v1.6.1 逐字节一致**；变的是安装/回滚脚本：

- 回滚目标**按内容判定**：备份必须**不含**扩展标记（`/run/recamera|rc_ext_` / `ExtApiHandler`）才被认作出厂件，不再依赖出厂 md5 白名单 —— 未知固件版本的设备也能安全回滚。
- `install.sh` 对未见过的出厂基线默认**告警不拒绝**；`--strict` 改为拒绝，`--force` 跳过校验（均可经 `deploy-firmware.sh` 透传）。

```bash
./deploy-firmware.sh --host <ip>              # 安装：md5 校验→备份原厂 rkipc→装入 /oem→停在 reboot 前
./deploy-firmware.sh --host <ip> --reboot     # 装完立即重启
./deploy-firmware.sh --host <ip> --rollback   # 回滚到原厂 rkipc（按内容判定备份）
./deploy-firmware.sh --host <ip> --strict     # 未知出厂基线时拒绝安装
```

脚本会要求输入 `I-HAVE-PHYSICAL-ACCESS` 确认。

## 回滚

- **应用层**：每次跑 `deploy-app.sh` 都会在 `/userdata/_deploy/backups/` 留时间戳备份；kit/SDK 旧副本在 `/userdata/local/kit.bak.<ts>`、`/userdata/sdk/*.bak.<ts>`。
- **版本回退**：`release/v1.6.1/`、`release/v1.6.0/`、`release/v1.5.0/` 在 CDN 上原样保留。
- **固件层**：`./deploy-firmware.sh --host <ip> --rollback`（恢复原厂 rkipc 后需冷启动）。

## 重新打包（可选，确定性）

```bash
# appmgr / frontend / apps（同输入→同 md5）
python3 release/deploy/build-packages.py --frontend /path/to/web_build --version 1.6.2
# ext-api / ext-kit（rkipc/entry.cgi 从 v1.6.1 tar 解出即可，逐字节同源）
release/build-release.sh --rkipc <rkipc> --entry-cgi <entry.cgi> --version 1.6.2
```

在 macOS 上打包前端要 `COPYFILE_DISABLE=1` 解包源目录，否则 `._*` AppleDouble 文件会混进 `/oem/usr/www`。

## 校验（本轮产物 md5 / size）

本表是本版所有产物校验值的**唯一权威来源**。

### release/v1.6.2/

| 包 | size (bytes) | md5 | 与 v1.6.1 |
|----|-------------:|-----|-----------|
| `recamera-ext-api-v1.6.2.tar` | 18708480 | `5708b454a5e8f11fbfc65377d4e8cbee` | **不同**（install/rollback 脚本重构） |
| `recamera-ext-kit-v1.6.2.tar.gz` | 2223582 | `a9bfec6fc7f588299de9374d547d148a` | **不同**（cgi_control 307 兼容） |
| `appmgr-v1.6.2.tar.gz` | 65927 | `edc772159ad7643e3279e03942b1e9ac` | **不同**（builtin 307 兼容） |
| `frontend-v1.6.2.tar.gz` | 36757262 | `6a48749108747c6bed59a06fef55965e` | **不同**（描述悬停 tooltip，bundle `main.1e2af984.js`） |
| `apps-v1.6.2.tar.gz` | 986788 | `3e6677572e044dc2d1fc25da7ffdda52` | **相同**（确定性重建，逐字节一致） |
| `voice-runtime-1.0.0.tar.gz` | 18856604 | `ace48a688d41a3fc6b852a0f14ddad8d` | **相同** |
| `gst-hwcodec-1.0.0.tar.gz` | 425137 | `8e6d286fac58a5b366e8fdd1709b212f` | **相同** |
| `deploy-app.sh` | 21080 | `bce2af4f27f0cdb5178731861a84811d` | 不同（版本号 + 2b 步安装 S94/nginx conf + 默认不预装应用） |
| `deploy-firmware.sh` | 5469 | `e3a827c7cf7ee48cabcba3338b1d15f5` | 不同（版本号 + `--strict`/`--force` 透传） |
| `S94appmgr` | 8116 | `e49fcf81c715e827daeed10475f0a5b4` | **相同** |
| `ext_appmgr.conf` | 4849 | `c5e0131966b85bfce8e614afd0a55577` | **相同** |

`recamera-ext-api-v1.6.2.tar` 内附固件产物（与 v1.6.0/v1.6.1 相同）：

| 固件产物 | size (bytes) | md5 |
|----------|-------------:|-----|
| `rkipc` | 15585904 | `f683352a9d062a05a3df1f8df22d7d53` |
| `entry.cgi` | 1057168 | `75a693c87c317a49c37c4dddb6b9ac7a` |
| `sdk/lib/librecamera_ext.so.1.0.0` | 89496 | `5cebfb9e4d9c001c45b58c75daafe934` |

### packages/（应用商店，`catalog.json` 引用）

**本版未改动**：`catalog.json` 与 9 个应用包、两个运行时的签名和 sha256 与 v1.6.0 完全一致，App Center 仍处于 v1.6.0 状态。校验值见 `release/v1.6.0/README.md`。

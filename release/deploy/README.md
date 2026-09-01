# reCamera Pro v1.3.0 — 一键部署包 (application layer)

> **历史归档，禁止固件部署。** 本目录及其 CDN 链接描述旧 v1.3 包，不含
> `inference-control@1`，不代表当前源码的一致发布集。不要运行
> `deploy-firmware.sh`；只有已安装匹配旧 endpoint 固件的历史设备才可把
> 应用层步骤作为迁移参考。

> 📖 **部署流程总览见 [DEPLOY.md](./DEPLOY.md)**(装什么/用哪个脚本/什么顺序/怎么回滚,不随版本变)。
> 本文是**本版本包**的清单与校验值。

把本轮全部应用层改动一次性部署到设备，让设备达到 v1.3.0 完整状态。
**固件层（rkipc 遮罩固件）单独、高危、默认不在一键流程内** —— 见文末。

## 目录内容

| 文件 | 内容 | 部署目标 |
|------|------|----------|
| `recamera-ext-kit-v1.3.0.tar.gz` | kit 运行时 + SDK(python/lib/头文件) + 离线推理 wheels（含 rknnlite / jinja2 / markupsafe），自带 `INSTALL.sh` | `/userdata/local/kit`、`/userdata/sdk`、`/userdata/rknnenv` |
| `appmgr-v1.3.0.tar.gz` | App Center 管理器全部代码（builtin/config/server/…）**含签名公钥 `keys/`**；已排除 `__pycache__`/`tests`/`*.bak*` | `/userdata/local/appmgr` |
| `frontend-v1.3.0.tar.gz` | React 前端构建产物（`main.fc8a67e8.js` 那批，含 P2/official/webp 静态资源 + 推理页收敛/画廊单活收口 + OCR 识别文本标签修复） | `/oem/usr/www` |
| `apps-v1.3.0.tar.gz` | 9 个 app 的 `manifest.json` + `app.py`（+ 小配置）。**不含大模型 `*.rknn`/`*.onnx`**（模型是设备侧共享依赖，走 catalog） | `/userdata/local/apps` |
| `recamera-ext-api-v1.3.0.tar` | 遮罩固件（rkipc + entry.cgi + 扩展 `.so` + SDK + wheels），自带 `install.sh`/`rollback.sh` | `/oem`（**高危，冷启动**） |
| `deploy-app.sh` | 应用层一键部署主脚本（安全） | — |
| `deploy-firmware.sh` | 遮罩固件部署脚本（**高危，单独跑**） | — |
| `build-packages.py` | 确定性重打前三个包（同输入→同 md5） | — |
| `ws_probe.py` | `:8124` 结果流校验用的 WebSocket 探针 | — |

包的 md5 / size 在文末「校验」一节列出。

## 从 CDN 下载

全部产物发布在 SenseCraft CDN，前缀
`https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/v1.3.0/`：

| 文件 | URL |
|------|-----|
| `recamera-ext-kit-v1.3.0.tar.gz` | https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/v1.3.0/recamera-ext-kit-v1.3.0.tar.gz |
| `recamera-ext-api-v1.3.0.tar` | https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/v1.3.0/recamera-ext-api-v1.3.0.tar |
| `appmgr-v1.3.0.tar.gz` | https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/v1.3.0/appmgr-v1.3.0.tar.gz |
| `frontend-v1.3.0.tar.gz` | https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/v1.3.0/frontend-v1.3.0.tar.gz |
| `apps-v1.3.0.tar.gz` | https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/v1.3.0/apps-v1.3.0.tar.gz |
| `deploy-app.sh` | https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/v1.3.0/deploy-app.sh |
| `deploy-firmware.sh` | https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/v1.3.0/deploy-firmware.sh |
| `README.md` | https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/v1.3.0/README.md |

```bash
# 拉全套到本地 deploy 目录，赋可执行权限后跑 deploy-app.sh
BASE=https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/v1.3.0
mkdir -p deploy && cd deploy
for f in recamera-ext-kit-v1.3.0.tar.gz recamera-ext-api-v1.3.0.tar \
         appmgr-v1.3.0.tar.gz frontend-v1.3.0.tar.gz apps-v1.3.0.tar.gz \
         deploy-app.sh deploy-firmware.sh README.md; do
  curl -fSL "$BASE/$f" -o "$f"
done
chmod +x deploy-app.sh deploy-firmware.sh
# 下载后按文末「校验」表核对 md5
```

## 前提

- **设备**：reCamera Pro（RV1126B），已开机、网络可达（示例走 Tailscale `192.168.42.1`；IP 易变，先确认当前地址）。
- **控制机（Mac）**：装了 `adb`。root 操作走 **adb**（设备 `adbd` 以 root 运行，`<ip>:5555`），无需 SSH 密码，无需 `sudo`/包管理器。
- **磁盘**：`/userdata` 需 ~200 MB 余量（含备份）。
- 幂等：脚本可重复跑；每次替换目录前按时间戳备份到 `/userdata/_deploy/backups/`。

## 一键部署（应用层，推荐）

```bash
./deploy-app.sh                       # 默认 host 192.168.42.1
./deploy-app.sh --host 192.168.10.x   # 指定设备 IP
./deploy-app.sh --skip-kit            # kit 已装，只更 appmgr/前端/apps
./deploy-app.sh --no-activate         # 不启动 app、不碰摄像头
```

### 部署顺序（脚本内部，每步幂等 + dmesg 查 vpss）

1. **kit + SDK + wheels** —— push kit 包，跑其 `INSTALL.sh`：装 kit 到 `/userdata/local/kit`，SDK 到 `/userdata/sdk`，离线 wheels（rknnlite / jinja2 / markupsafe）到 venv `/userdata/rknnenv`。旧副本按时间戳备份。
2. **appmgr** —— 备份旧 `/userdata/local/appmgr`，merge-extract 新代码（覆盖 `.py` + `keys/`，保留运行态 `audit.log`/`mqtt.json`/锁）；**setsid 重启 `python3 -m appmgr serve`（cd 到 `/userdata/local`，无 env hack）**，验证 `127.0.0.1:8130` 存活。
   - 停旧进程用 `/proc` cmdline 扫描 + pidfile，**不用 `pkill -f`**（`pkill -f 'appmgr serve'` 会匹配到重启命令本身、误杀自己的 shell）。
3. **前端** —— 备份 `/oem/usr/www`；`static/` 整目录替换（清掉旧 hash bundle），顶层 html/json/png/svg 覆盖；**目录 chmod 755 / 文件 644**，仅作用于部署到的路径，**不碰 `cgi-bin`、不碰 `sdcard/usb0/userdata` 软链**。
4. **apps** —— 备份 `state.json`，merge-extract 9 个 app 的代码+manifest，**保留设备上已有的大模型文件**。
5. **激活 + 校验** —— 调 `POST /api/appMgr/switch` 激活一个 app（默认 `retail-vision`），跑 `ws_probe.py` 确认 `:8124` 结果流出帧；查 dmesg 无 vpss。

### 安全护栏（脚本内置）

- **绝不碰 rkipc**：部署前后各取一次 `/oem/usr/bin/rkipc` md5，收尾断言未变，变了直接 FATAL。
- 每个碰摄像头的步骤后查 `dmesg`；出现 `vpss err` / `CSIBDG fifo overflow` / `Oops` / `Unable to handle kernel` 立即 STOP。
- 大文件 push 用 **md5 校验 + 重试**（adb over Tailscale 大包偶发 `EOF`，按 md5 判定成功）。

## 固件层部署（遮罩固件，高危，单独跑）

> ⚠️ **换 rkipc 必须冷启动（整机 `reboot`）激活。热替换会触发 `cv181x_vpss` / CSIBDG FIFO 内核 oops，可能把设备搞挂。远程执行有风险 —— 只在你能物理复位设备（在设备旁 / 有电源或 reset 通路）时才跑。**

`deploy-app.sh` 不依赖遮罩固件也能把 apps/前端/appmgr 跑起来。仅当需要软件叠加 OSD/结果注入等扩展 API 能力时才装固件。

```bash
./deploy-firmware.sh --host <ip>              # 安装：md5 校验→备份原厂 rkipc→装入 /oem→停在 reboot 前
#   然后在设备控制台手动 `reboot`（推荐），或加 --reboot 让脚本自动重启
./deploy-firmware.sh --host <ip> --reboot     # 装完立即重启
./deploy-firmware.sh --host <ip> --rollback   # 回滚到原厂 rkipc（用 /userdata/rkipc.factory.bak）
```

脚本会要求输入 `I-HAVE-PHYSICAL-ACCESS` 确认。`install.sh` 会一次性把原厂 `rkipc`/`entry.cgi` 备份到 `/userdata/*.factory.bak`，回滚随时可用。

## 回滚

- **应用层**：每次跑 `deploy-app.sh` 都会在 `/userdata/_deploy/backups/` 留时间戳备份
  （`appmgr.<ts>/`、`www.<ts>.tar.gz`、`state.json.<ts>`）；kit/SDK 旧副本在
  `/userdata/local/kit.bak.<ts>`、`/userdata/sdk/*.bak.<ts>`。手动 `cp -a` / 解 tar 回去即可。
- **固件层**：`./deploy-firmware.sh --host <ip> --rollback`（内部跑包里的 `rollback.sh`，恢复原厂 rkipc 后需冷启动）。

## 重新打包（可选，确定性）

前三个包由 `build-packages.py` 确定性生成（tar 元数据归零 + gzip mtime=0，同输入→同 md5）：

```bash
python3 build-packages.py --frontend /path/to/recamera_web_react/build
# 输出 appmgr-v1.3.0 / frontend-v1.3.0 / apps-v1.3.0 三个 tar.gz 到当前目录
```

`recamera-ext-kit` / `recamera-ext-api` 由 release 流程单独产出，不由本脚本重打。

## 校验（本轮产物 md5 / size）

| 包 | size (bytes) | md5 |
|----|-------------:|-----|
| `recamera-ext-api-v1.3.0.tar` | 18626560 | `a70b47bc33ebbf77b1c78ab4ee7e924f` |
| `recamera-ext-kit-v1.3.0.tar.gz` | 2093931 | `f151ac2abc3842ca7e156d783f942072` |
| `appmgr-v1.3.0.tar.gz` | 30926 | `c3ebcc4c66ed0849446d7390a184ebd8` |
| `frontend-v1.3.0.tar.gz` | 36750969 | `0af9d76dbdec7c95156735fce9eb5948` |
| `apps-v1.3.0.tar.gz` | 985548 | `16d9db07ae13f5d812a0af987d84969e` |

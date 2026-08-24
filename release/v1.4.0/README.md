# reCamera Pro v1.4.0 — 一键部署包 (application layer)

> **历史归档，禁止用于当前固件部署。** 本目录早于
> `inference-control@1`，所引用的 sideload 固件也与当前源码/manifest 不构成
> 一致发布集。不要运行 `deploy-firmware.sh`；应用层脚本只对已经安装对应旧
> endpoint 固件的历史设备有参考意义。

把本轮全部应用层改动一次性部署到设备，让设备达到 v1.4.0 完整状态。
**固件层（rkipc 遮罩固件）单独、高危、默认不在一键流程内** —— 见文末。

**本版固件产物与 v1.3.0 相同**（`rkipc` `9826e9ec…`、`entry.cgi` `75a693c8…` 均未变），
`recamera-ext-api-v1.4.0.tar` 只是版本号与内附 `MANIFEST.txt`/`README.md` 的文本变化。
已装 v1.3.0 固件的设备**无需重装固件**，跑 `deploy-app.sh` 即可。

## 相对 v1.3.0 的变化

- **安装跳过设备已有模型** —— appmgr 安装应用前先扫描设备上的共享模型树
  （`market/appmgr/assets.py`），已经在 `/userdata/local/models/` 下、md5 一致的模型不再下载、
  不再上传。语音应用那颗 133 MB 的 `sensevoice_rv1126b_w4a16.rknn` 重复安装时代价为零。
- **按需音频运行时** —— 语音栈从基础镜像里拆出来，成为独立的 `voice-runtime-1.0.0.tar.gz`（18 MB），
  由 `catalog.json` 的 `runtimes.audio` 描述。只有 `capabilities` 含 `"audio"` 的应用
  （本版为 `voice-transcribe`）才触发下载，且装过一次后复装不再走 pip。
- **kit 新形态** —— 新增统一入口 `kit/run.py`（`python3 -m kit.run <app_dir>`）。
  过去 9 个 app.py 各自携带一份逐字节相同的 sys.path 探测样板，现已全部删除：
  部署布局的知识收敛到 `kit/run.py` 一处，由模块自身路径推导 `KIT_PARENT`，
  不再依赖 `KIT_PARENT`/`KIT_DIR` 环境变量或多级 `..` 回退。
  同时新增**渲染声明注入**：manifest 里的 `render` 声明经 kit 透传进结果 envelope，
  前端叠加层据此渲染，manifest 与 envelope 为同一来源。
  **v1.3.0 的 kit 包不含 `kit/run.py` 与 `kit/events.py`，不可与本版混用。**
- **appmgr pid 修复** —— `setsid` 启动时 `$!` 拿到的是 setsid 自身的 pid，它随即退出，
  写进 `/var/run/appmgr.pid` 的从一开始就是死 pid，导致 `S94appmgr stop/status` 长期失效。
  现改为进程起来之后从 `/proc` 回读真实 pid；`deploy-app.sh` 的重启步骤同步修正。

## 目录内容

| 文件 | 内容 | 部署目标 |
|------|------|----------|
| `recamera-ext-kit-v1.4.0.tar.gz` | kit 运行时（含新增 `kit/run.py`、`kit/events.py`）+ SDK(python/lib/头文件) + 离线推理 wheels（含 rknnlite / jinja2 / markupsafe），自带 `INSTALL.sh` | `/userdata/local/kit`、`/userdata/sdk`、`/userdata/rknnenv` |
| `appmgr-v1.4.0.tar.gz` | App Center 管理器全部代码（builtin/config/server/assets/…）**含签名公钥 `keys/`**；已排除 `__pycache__`/`tests`/`*.bak*` | `/userdata/local/appmgr` |
| `frontend-v1.4.0.tar.gz` | React 前端构建产物（含结果叠加层 / 推理页 / 应用商店） | `/oem/usr/www` |
| `apps-v1.4.0.tar.gz` | 9 个 app 的 `manifest.json` + `app.py`（+ 小配置）。**不含大模型 `*.rknn`/`*.onnx`**（模型是设备侧共享依赖，走 catalog） | `/userdata/local/apps` |
| `voice-runtime-1.0.0.tar.gz` | 按需音频运行时（`runtimes.audio`），仅音频应用需要 | 由 appmgr 按需拉取安装 |
| `recamera-ext-api-v1.4.0.tar` | 遮罩固件（rkipc + entry.cgi + 扩展 `.so` + SDK + wheels），自带 `install.sh`/`rollback.sh`。**产物同 v1.3.0** | `/oem`（**高危，冷启动**） |
| `deploy-app.sh` | 应用层一键部署主脚本（安全） | — |
| `deploy-firmware.sh` | 遮罩固件部署脚本（**高危，单独跑**） | — |

包的 md5 / size 在文末「校验」一节列出。

## 从 CDN 下载

全部产物发布在 SenseCraft CDN，前缀
`https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/v1.4.0/`：

| 文件 | URL |
|------|-----|
| `recamera-ext-kit-v1.4.0.tar.gz` | https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/v1.4.0/recamera-ext-kit-v1.4.0.tar.gz |
| `recamera-ext-api-v1.4.0.tar` | https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/v1.4.0/recamera-ext-api-v1.4.0.tar |
| `appmgr-v1.4.0.tar.gz` | https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/v1.4.0/appmgr-v1.4.0.tar.gz |
| `frontend-v1.4.0.tar.gz` | https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/v1.4.0/frontend-v1.4.0.tar.gz |
| `apps-v1.4.0.tar.gz` | https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/v1.4.0/apps-v1.4.0.tar.gz |
| `voice-runtime-1.0.0.tar.gz` | https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/v1.4.0/voice-runtime-1.0.0.tar.gz |
| `deploy-app.sh` | https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/v1.4.0/deploy-app.sh |
| `deploy-firmware.sh` | https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/v1.4.0/deploy-firmware.sh |
| `README.md` | https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/v1.4.0/README.md |

应用商店走另一条路径：`catalog.json` 在
`https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/catalog.json`，
应用包与音频运行时在
`https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/packages/`。

```bash
# 拉全套到本地 deploy 目录，赋可执行权限后跑 deploy-app.sh
BASE=https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/v1.4.0
mkdir -p deploy && cd deploy
for f in recamera-ext-kit-v1.4.0.tar.gz recamera-ext-api-v1.4.0.tar \
         appmgr-v1.4.0.tar.gz frontend-v1.4.0.tar.gz apps-v1.4.0.tar.gz \
         voice-runtime-1.0.0.tar.gz deploy-app.sh deploy-firmware.sh README.md; do
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

> `--skip-kit` 从 v1.3.0 升级时**不要用**：v1.3.0 的 kit 包缺 `kit/run.py`/`kit/events.py`，
> 跳过 kit 会让新 app.py 起不来。

### 部署顺序（脚本内部，每步幂等 + dmesg 查 vpss）

1. **kit + SDK + wheels** —— push kit 包，跑其 `INSTALL.sh`：装 kit 到 `/userdata/local/kit`，SDK 到 `/userdata/sdk`，离线 wheels（rknnlite / jinja2 / markupsafe）到 venv `/userdata/rknnenv`。旧副本按时间戳备份。
2. **appmgr** —— 备份旧 `/userdata/local/appmgr`，merge-extract 新代码（覆盖 `.py` + `keys/`，保留运行态 `audit.log`/`mqtt.json`/锁）；**setsid 重启 `python3 -m appmgr serve`（cd 到 `/userdata/local`，无 env hack）**，起来后从 `/proc` 回读真实 pid 写 pidfile，验证 `127.0.0.1:8130` 存活。
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

**本版固件产物与 v1.3.0 逐字节相同**（见 `MANIFEST.txt` 的 `rkipc` / `entry.cgi` md5）。
设备若已装 v1.3.0 固件，本步可跳过。

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

`appmgr` / `frontend` / `apps` 三个包由 `build-packages.py` 确定性生成（tar 元数据归零 + gzip mtime=0，同输入→同 md5）：

```bash
python3 build-packages.py --frontend /path/to/recamera_web_react/build
```

`recamera-ext-kit` / `recamera-ext-api` 由 release 流程单独产出，不由本脚本重打。

## 校验（本轮产物 md5 / size）

本表是本版所有产物校验值的**唯一权威来源**。

### release/v1.4.0/

| 包 | size (bytes) | md5 |
|----|-------------:|-----|
| `recamera-ext-api-v1.4.0.tar` | 18626560 | `c3625d2bda22a763c5b8b0ad35bd4cc3` |
| `recamera-ext-kit-v1.4.0.tar.gz` | 2199793 | `ccbd8352914f711f56294e59f589c68e` |
| `appmgr-v1.4.0.tar.gz` | 52726 | `2d85c1112f09fa2156f13cce97117228` |
| `frontend-v1.4.0.tar.gz` | 36755220 | `0565ac3e91517f6a7afe17ca7640a198` |
| `apps-v1.4.0.tar.gz` | 986014 | `01033bf889ae2613c858a138e955507a` |
| `voice-runtime-1.0.0.tar.gz` | 18856604 | `ace48a688d41a3fc6b852a0f14ddad8d` |
| `deploy-app.sh` | 12761 | `1442f5dec10f16d78dd36a4007c3fee3` |
| `deploy-firmware.sh` | 4945 | `2d9b525f588c383eadd54913ce2a2195` |

`recamera-ext-api-v1.4.0.tar` 的 tar md5 与 v1.3.0 不同，因为内附 `MANIFEST.txt` / `README.md`
的版本号文本变了；**tar 内的固件产物本身未变**：

| 固件产物 | size (bytes) | md5 | 与 v1.3.0 |
|----------|-------------:|-----|-----------|
| `rkipc` | 15502408 | `9826e9ecf8ed543a6dc78e3731102e0f` | 相同 |
| `entry.cgi` | 1057168 | `75a693c87c317a49c37c4dddb6b9ac7a` | 相同 |
| `sdk/lib/librecamera_ext.so.1.0.0` | 89496 | `5cebfb9e4d9c001c45b58c75daafe934` | 相同 |

### packages/（应用商店，`catalog.json` 引用）

| 包 | size (bytes) | md5 |
|----|-------------:|-----|
| `face-analysis-0.1.0-arm64.tar.gz` | 52838389 | `d8ed87ded55bcae3fadb12fb7d838590` |
| `facemesh-reader-0.1.0-arm64.tar.gz` | 6843123 | `a9a957e67033c62549cc36bccd9fd387` |
| `fall-detection-0.2.0-arm64.tar.gz` | 3053510 | `1671bc9f53e8154545f756154c18c417` |
| `fitness-trainer-0.1.0-arm64.tar.gz` | 2867151 | `43479adcf866449c88c2656f621a5d24` |
| `ppocr-reader-0.1.0-arm64.tar.gz` | 5194719 | `d0314b34b6e8f97b143bfa15e2718a32` |
| `qrcode-reader-0.1.0-arm64.tar.gz` | 922415 | `38ee1e4eb2086767614c3500bdef4ce0` |
| `retail-vision-0.1.0-arm64.tar.gz` | 3023387 | `f16c23f30d32455744412e0eed32097e` |
| `voice-transcribe-0.1.0-arm64.tar.gz` | 8158 | `cb31688b39241dc09dcb07f92e53fbc2` |
| `yolo-detector-0.1.0-arm64.tar.gz` | 3018913 | `1470e1cc06b1a4eb3e09f232ed1b76bb` |
| `voice-runtime-1.0.0.tar.gz` | 18856604 | `ace48a688d41a3fc6b852a0f14ddad8d` |

每个应用包旁有同名 `.tar.gz.sig`（ECDSA P-256 / SHA-256 detached，base64，97 B）。
设备侧用内置公钥 `market/appmgr/keys/release_pub.pem` 验签，**未签名的包会被拒装**。
`catalog.json` 中每个包的 `signature` 字段即该 sidecar 内容；`runtimes.audio.sha256` 为
`e1bccfc4f42f43478186d17119a20c4d7f0834640bf9bc324ed5d028e397f711`。

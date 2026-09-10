# reCamera Pro 扩展 API 部署与运维手册

> **读者**：把扩展 API / 应用中心部署到设备、或负责设备维护的人（Seeed 内部 / 集成商）。
> **对象设备**：reCamera Pro（RV1126B / recamera_v2），kernel 6.1.157、rootfs `/` 与 `/oem` ext4 rw、`/userdata` ext4 rw 无 noexec。
> **依据**：`release/pkg/`（install.sh / rollback.sh / MANIFEST.txt / README.md）、`../../CHANGELOG.md`、`../api/spec.md`、`app-center-publishing.md`，以及真机验证记录（G1-G4 / M1-M3）与踩坑记录。当前固件服务入口见 `market/deploy/` 与本节 §4.2。
> **状态（2026-08-19）**：本文保留了历史部署记录，但当前 checkout 的
> `release/pkg` 不是可部署发布物：实文件与 MANIFEST/install.sh 哈希不一致，
> factory/ext rkipc 哈希集合还发生重叠，并且包不含 `inference-control@1`。
> `install.sh` 已 fail-closed。下列 sideload 命令只供追溯，**不得照做**；正式
> 发布必须从 manifest 固定的源码构建完整固件并重新生成 BOM/hash。

---

## 1. 部署什么

设备安装匹配的扩展固件后，方案商无需再为每个应用修改或重编固件；应用进程
接 `/run/recamera/` 下的 Unix domain socket，即可拿帧、回注结果和观测内建
流水线。它由以下产物组成：

下表是旧包文档声称的值，**不是当前 checkout 实文件的可信校验表**；保留它只为
定位历史设备，不能据此发布或安装。

| 产物 | 历史安装目标 | 历史声明 md5 | 大小 | 必需性 |
|---|---|---|---|---|
| `rkipc`（含扩展端点：帧代理 / 结果回注 / probe）| `/oem/usr/bin/rkipc` | `9826e9ecf8ed543a6dc78e3731102e0f` | 15.5 MB | **核心必需**——扩展端点全在这个二进制里 |
| `entry.cgi`（M4 控制面）| `/oem/usr/www/cgi-bin/entry.cgi` | `75a693c87c317a49c37c4dddb6b9ac7a` | 1.05 MB | 可选（M4 控制面接口）|
| `librecamera_ext.so.1.0.0`（+ `.so.1` `.so` 软链）| `/oem/usr/lib/` | `5cebfb9e4d9c001c45b58c75daafe934` | 87 KB | 给方案商 SDK 运行时；仅当有扩展应用用到才需要 |
| `recamera_ext/`（Python ctypes 绑定）| `/userdata/sdk/python/` | — | — | 给方案商，可选（`/userdata` 不受 OTA 影响）|
| `recamera_ext.h`（C 头）| `/userdata/sdk/` | — | — | 给方案商，可选 |

安装后重启，`/run/recamera/` 下出现六个 socket 和应用身份目录：

- `frame.sock` — 帧代理（零拷贝 dma-buf 帧交接，SCM_RIGHTS 传 fd + 96 字节定长头）
- `result-in.sock` — 公开结果回注（检测 / 分类 / 分割 / 跟踪 / 关键点送入官方 OSD 与 notify/WS；不进入 Vigil、不触发录像）
- `osd-in.sock` — appmgr 专用 OSD-only 入口（只叠加，不分发、不触发录像）
- `record-in.sock` — appmgr 专用 recording-only 入口（只进入 source-aware Vigil，不叠加、不分发）
- `probe.sock` — 观测面（preproc / npu.raw / postproc / metrics tap）；SDK client `ProbeSource`（v1.2.0）已发布可用，见 `README.md` §4.8。metrics（inline）+ preproc.out（大张量走 memfd）双路已真机验证。
- `inference-control.sock` — 内建/外部 RKNN 所有权协调（connection-lifetime lease）
- `apps.d/` — 每 app 控制目录

**当前阻塞**：仓内 sideload 二进制与 manifest/hash 不一致、缺少 NPU broker，
不能部署。源码侧 frame/result/probe/inference-control 已恢复并通过 host 与
AArch64 构建门禁，但还需提交并由 manifest 固定各仓 commit、生成完整固件，
以及执行 RV1126B 真机 kill/restart/OTA 矩阵。

**应用中心**（App Center，可选，方案 B）另有三块产物，见第 4 节：
- 前端 SPA（React，静态 shell）→ `/userdata/local/appcenter/www/`（nginx `alias`，`ext_appmgr.conf:31-35`）。
  注：`market/spa/` 这份独立 SPA 已 **LEGACY**（见 `ext_appmgr.conf:25-30` 与 `market/spa/DEPRECATED.md`），
  当前前端是官方 web-native React 的 `/app-center` 页；下述 appmgr 后端与动态路由仍在用。
- `appmgr` 后端（Python 常驻服务，`python3 -m appmgr serve`，监听 `127.0.0.1:8130`）→ `/userdata/local`
- nginx 边缘配置 `ext_appmgr.conf`（静态 SPA + Web-native
  `/api/app-center/v1/` 与 legacy `/api/appMgr/` 反代）→ `/oem/usr/etc/nginx/`

---

## 2. 两种更新方式

### 2.1 历史增量 release 包（当前禁用，不可部署）

`release/pkg/` 仅保留历史流程和文件形状。它当前会在任何写设备操作前主动
退出；不要通过修改一个 md5 或删除 guard 强行安装，因为现有 rkipc 身份集合
冲突，可能把扩展二进制误当成 factory rollback 目标。以下命令块仅用于说明
旧流程，不是当前操作指南。

前提：设备可通过 adb 以 root 访问（`adb connect <ip>:5555`，adbd 以 root 跑）。

```sh
# 在持包机器（Mac / Linux）上：
adb shell "mkdir -p /userdata/ext-pkg"
adb push ./ /userdata/ext-pkg/                    # push 整个 release/pkg 目录
adb shell "sh /userdata/ext-pkg/install.sh"       # 备份原厂 + md5 校验 + 覆盖 /oem
adb reboot                                         # 或 install.sh 传 --reboot
# 等 ~1-2 分钟自检：
adb shell "ls -l /run/recamera/"                  # 期望六个 *.sock（见 §1）及 apps.d/
adb shell "md5sum /oem/usr/bin/rkipc"             # 期望 9826e9ecf8ed543a6dc78e3731102e0f
```

`install.sh` 是**幂等 + md5 校验**的（见 `release/pkg/install.sh`）：
1. `[1/6]` 校验包内三件产物 md5，不符立即 `exit 1`；
2. `[2/6]` 首次把原厂 `rkipc` 备份到 `/userdata/rkipc.factory.bak`——但**只接受经校验的干净原厂**（`VERIFIED_FACTORY_MD5S`，当前 `d5e7ca9365dae553e8c7e4c0a0f436ec`，1.9MB、0 个扩展 socket 字符串）作为回滚目标。若 `/oem` 当前 rkipc 是**已知扩展构建**（`KNOWN_EXT_BUILD_MD5S`，含本包自己的 rkipc）或未知构建，则**拒绝把它当原厂备份**并 `exit 1`（否则日后回滚会变成空操作、扩展固件留在 `/oem`）。新固件版本要先确认 `strings rkipc | grep /run/recamera` 为空、再把其 md5 追加进 `VERIFIED_FACTORY_MD5S`；`rollback.sh` 同理，备份非已验证原厂时**拒绝恢复**；
3. `[3/6]` 首次备份原厂 `entry.cgi` 到 `/userdata/entry.cgi.factory.bak`；
4. `[4/6]` `touch /oem/.wtest` 验证 `/oem` 可写；
5. `[5/6]` `cp` 三件产物进 `/oem` 并**逐个 `chmod 755`**，建立 `.so.1` / `.so` 软链，SDK python + 头拷进 `/userdata/sdk/`，`sync`；
6. `[6/6]` 提示 reboot（或 `--reboot` 自动重启）激活新 rkipc。

**边界（诚实标注）**：这是 **sideload，覆盖 `/oem`**。`/oem` 是 ext4 rw，普通 reboot 持久；但**一次完整固件 OTA / `update.img` 刷写会重写 `/oem`，把 rkipc 还原成原厂**——**OTA 后必须重跑 `install.sh`**。本包不碰分区、shadow、update.img。

回滚见第 6 节（`rollback.sh`）。

### 2.2 完整 OTA 固件（正式、重）

正式发布形态是把扩展改动全量编进 rootfs，打成 `update_ota.tar` / `update.img`，走 RKDevTool / upgrade_tool 或官方 OTA 分发。这属于**打包分发范畴，本轮暂未做**——此处仅标注方向：编译入口 `./build.sh app`（编 rkipc）→ `./build.sh firmware`（打包分区）→ `./build.sh updateimg` / `allsave`（整包），产物在 `output/image/`（见 `recamera-rk-build` skill）。做成 OTA 后，`/oem` 覆盖就成了固件的一部分，不再需要 sideload。

### 2.3 一键部署（应用层，`deploy-app.sh`，推荐）

> **历史记录**：以下是一键部署 v1.3/v1.5 应用层包的旧流程，不是当前源码
> 对应的新发布 train。它不会更新 rkipc，也不能给旧固件补上
> `inference-control@1`；只有设备已经运行匹配 endpoint 固件时才有参考价值。

`deploy-app.sh` 把本轮全部**应用层**改动一次性打到设备，让设备达到该 train 的完整状态，全程 **adb over root**，**不碰 rkipc / 固件 / cgi-bin**（部署前后各取一次 `/oem/usr/bin/rkipc` md5，收尾断言未变，变了 FATAL）。

```bash
cd release/v1.5.0                      # 或对应 train 目录
./deploy-app.sh --host <设备IP>        # 设备 IP 易变，先确认当前地址（见 §末网络）
./deploy-app.sh --host 192.168.10.x   # 局域网同网段直连示例
./deploy-app.sh --skip-kit            # kit 已装，只更 appmgr/前端/apps
./deploy-app.sh --no-activate         # 不启动 app、不碰摄像头
```

**5 步流程（每步幂等 + 时间戳备份到 `/userdata/_deploy/backups/` + dmesg 查 vpss）**：

1. **kit + SDK + wheels** — push `recamera-ext-kit-v1.3.0.tar.gz`，跑其 `INSTALL.sh`：kit→`/userdata/local/kit`，SDK→`/userdata/sdk`，离线 wheels（rknnlite / jinja2 / markupsafe）→ venv `/userdata/rknnenv`。
2. **appmgr** — 备份旧 `/userdata/local/appmgr`，merge-extract 新代码（覆盖 `.py` + `keys/`，保留运行态 `audit.log`/`mqtt.json`/锁）；**`setsid python3 -m appmgr serve`（cd 到 `/userdata/local`，无 env hack）**，验证 `127.0.0.1:8130` 存活。
3. **前端** — 备份 `/oem/usr/www`；`static/` 整目录替换（清旧 hash bundle），顶层 html/json/png/svg 覆盖，目录 755 / 文件 644，**不碰 `cgi-bin` 与 `sdcard/usb0/userdata` 软链**。
4. **apps** — 备份 `state.json`，merge-extract 9 个 app 的 `manifest.json`+`app.py`，**保留设备上已有的大模型文件**（模型走 catalog `putModel`，不在 apps 包内）。
5. **激活 + 校验** — `POST /api/appMgr/switch` 激活一个 app（默认 `retail-vision`），`ws_probe.py` 确认 `:8124` 结果流出帧，dmesg 无 vpss。

**CDN 下载**：前缀 `https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/v1.3.0/`，五个包同名可取（`recamera-ext-kit` / `recamera-ext-api` / `appmgr` / `frontend` / `apps`）。

> **校验值以随包发布的 `README.md` 为准**：`…/release/v1.3.0/README.md` 文末「校验」表列出五个包的 size/md5，它与包**同批上传**，不会漂移。
>
> 本文**不再复制**这张表 —— 曾经复制过一份，包重发了两次而这里没跟着改，五行 md5 全部过期（照它核验会误判为下载损坏）。同一份数据两处维护必然漂移，改为单一来源。

```bash
BASE=https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/v1.3.0
curl -fsSL "$BASE/README.md" | sed -n '/^## 校验/,$p'    # 查当前权威 md5
```

`deploy-app.sh` / `deploy-firmware.sh` / `README.md` 三个脚本/文档同前缀下同名可取。

### 2.4 遮罩固件冷启动（`deploy-firmware.sh`，高危，单独跑）

> ⚠️ **换 rkipc 必须冷启动（整机 `reboot`）激活。热替换会触发 `cv181x_vpss` / CSIBDG FIFO 内核 oops，可能把设备搞挂。只在你能物理复位设备（在设备旁 / 有电源或 reset 通路）时才跑。**

`deploy-app.sh` 不依赖遮罩固件也能把 apps/前端/appmgr 跑起来；仅当需要软件叠加 OSD / 结果注入等扩展 API 能力时才装固件。脚本会要求交互输入 `I-HAVE-PHYSICAL-ACCESS` 才继续，`install.sh` 一次性把原厂 `rkipc`/`entry.cgi` 备份到 `/userdata/*.factory.bak`。

```bash
./deploy-firmware.sh --host <ip>              # 安装：md5 校验→备份原厂→装入 /oem→停在 reboot 前
./deploy-firmware.sh --host <ip> --reboot     # 装完立即重启激活
./deploy-firmware.sh --host <ip> --rollback   # 回滚原厂 rkipc（/userdata/rkipc.factory.bak）
```

不传 `--reboot` 时脚本只 stage 改动并提示手动 `reboot`（推荐从设备控制台冷启动）。

---

## 3. 手动热替换流程（开发 / 单设备）

用于开发迭代或单台设备验证，不走 install.sh、不烧固件，回滚最快。核心是**逐文件覆盖 + 重启让 init 拉起**。

```sh
# 0. 备份原厂（若尚未备份）
adb shell "[ -f /userdata/rkipc.factory.bak ] || cp /oem/usr/bin/rkipc /userdata/rkipc.factory.bak"

# 1. push 新二进制
adb push ./rkipc /userdata/rkipc.new

# 2. cp 进 /oem —— cp 后【必须】chmod 755（见下方关键坑）
adb shell "cp /userdata/rkipc.new /oem/usr/bin/rkipc && chmod 755 /oem/usr/bin/rkipc"

# 3. reboot 让 SysVinit 干净拉起新 rkipc（不要 while-true 重拉，见第 7 节）
adb reboot

# 4. 验证（等 1-2 分钟）
adb shell "md5sum /oem/usr/bin/rkipc"     # 对上目标 md5
adb shell "ls -l /run/recamera/"          # 六个 *.sock（见 §1）+ apps.d/
adb shell "dmesg | grep -iE 'vpss|fifo|Oops' | tail"   # 无 VPSS 崩溃
# RTSP：rtsp://<ip>:554/...   结果 WS（本机免 JWT）：ws://127.0.0.1:8123
```

**关键坑**：`adb push` 上来的文件是 `-rw-rw-rw-`（无执行位），busybox `cp` **不保留 / 不补执行位**，拷进 `/oem/usr/bin/` 后 rkipc 会变 `-rw-r--r--`，RkLunch 执行 `rkipc &` 报 `Permission denied`、起不来（无进程、无 `/run/recamera/`）。**cp 二进制进 `/oem` 后紧跟 `chmod 755`**，entry.cgi 同理。

另一种不碰 `/oem` 的验证法（3b 即此法）：直接把自编 rkipc 放 `/userdata/rkipc.xxx` 运行（`/userdata` ext4 rw 无 noexec），原厂 `/oem` 保持不动，验完 `adb reboot` 回原厂。热替换单文件也可用 bind mount（`cp new /userdata/rkipc && mount --bind /userdata/rkipc /oem/usr/bin/rkipc`，`umount` 回滚），适合快速来回切。

> 自编 rkipc 与 entry.cgi 在 V1.0.4 / V1.0.10 两个固件 build 上都能跑（见第 9 节），热替换验证不必逐固件版本重编。

---

## 4. 应用中心部署（方案 B）

应用中心与官方推理**并行**跑：rkipc 独占相机 → go2rtc 出流（RTSP `rtsp://127.0.0.1:5554/live/0` main、`/live/1` sub；HTTP :1984），app 消费共享流 + 自己的 RKNN context。用 `tar.gz 包 + appmgr 自管进程` 取代一代的 `opkg/.deb + /etc/init.d`。

### 4.1 三块产物落位

1. **前端 SPA**：`build` 后落 `/userdata/local/appcenter/www/`（nginx `alias`，`ext_appmgr.conf:31-35`）。用户从官方 dashboard 加外链，或直接访问 `http(s)://<device>/appcenter/`。**（此独立 SPA 已 LEGACY，当前前端是官方 web-native React `/app-center`；见 §1 与 `market/spa/DEPRECATED.md`。）**
   > **设备本地服务路径（已对齐官方整合后布局）**：以设备实测的**整合后布局**为权威，repo 侧已对齐——
   > - **应用包**：URL `/appcenter/apps/<file>.tar.gz` → 设备 `/userdata/local/appcenter/apps/`（nginx `alias`，`ext_appmgr.conf` 新增 `location /appcenter/apps/`）。`gen_catalog.py` 的默认包 base 即 `/appcenter/apps/`（`DEFAULT_BASE_URL`）。
   > - **catalog**：URL `/appcenter/catalog.json` → 设备 `/userdata/local/catalog/catalog.json`（整合后布局把 catalog 单独放在 `/userdata/local/catalog/`，不在 `appcenter/` 下；`ext_appmgr.conf` 新增 `location = /appcenter/catalog.json`）。
   >
   > 早先标注的"repo conf 缺 `/appcenter/apps/`、`/appcenter/catalog.json`、`/userdata/local/catalog/`"分歧**已消除**：这些 location 已补进 repo `ext_appmgr.conf`（注明"以设备/官方 conf 为准，本块为 repo 侧对齐参考"）。
   > **两套 url**：生产分发走 **CDN（主）**——浏览器代取 CDN 上的 `catalog.json` + 包（见 publishing §6，catalog `url` 指向 `sensecraft-statics.seeed.cc/.../packages/`）；上述 `/appcenter/*` 是**设备本地服务（回退）**，对应仓库里的 `catalog.local.json`（`gen_catalog.py` 默认 base 产出，包 url→`/appcenter/apps/<f>`）。仓库里的 `catalog.json` 是 CDN 版，`catalog.local.json` 是设备本地版，二者并存、勿互相覆盖。
2. **appmgr 后端**：代码 + 状态在 `/userdata/local`（`python3 -m appmgr` 从此解析），`appmgr serve` 监听 loopback `127.0.0.1:8130`，公网面由 nginx 转发。历史 `POST /api/appMgr/putModel` 只保留 loopback 迁移用途；公网返回 410，Web v2 使用 bundled artifacts（见 publishing §6）。
3. **nginx 边缘**：`ext_appmgr.conf` 放 `/oem/usr/etc/nginx/`，被 `common_relay.conf` 的 `include ext_*.conf`（在 `server{ listen 80; }` 块内）自动加载。它**只新增 location，不改任何官方 conf**：
   - `/appcenter/` → 静态 SPA shell（**匿名**，shell 内无秘密）
   - `/appcenter/apps/` → 应用包（**匿名**，`alias /userdata/local/appcenter/apps/`；整合后布局，浏览器本地回退取包处）
   - `/appcenter/catalog.json` → 安装目录（**匿名**，`location =` → `alias /userdata/local/catalog/catalog.json`；整合后布局把 catalog 单列）
   - `/appcenter/ws/results` → 检测结果 WS 反代到 `127.0.0.1:8124`（JWT 门）
   - `/appcenter/go2rtc/` → 视频反代到 `127.0.0.1:1984`（JWT 门）
   - `/api/app-center/v1/` → Web-native manifest v2、多应用、异步操作与 SSE API
     反代到 `127.0.0.1:8130`（JWT 门；独立命名空间，不占用 SenseCraft 的 `/api/v1/`）
   - `/api/appMgr/` → legacy 管理 API 反代到 `127.0.0.1:8130`（JWT 门，剩余
     JSON body 上限 2 MiB，`proxy_read_timeout 200s` 给安装/切换留时间）
   - exact `/api/appMgr/upload`、`/api/appMgr/putModel` → 公网固定 410；两者的
     legacy 后端会全量驻内存，只保留 loopback 迁移兼容。Web 安装走
     `/api/app-center/v1/uploads` 的流式 + 总配额链路。
   - 鉴权复用官方 `entry.cgi` 的 `auth_request /_jwt_verify`（同源 cookie `token`，浏览器自动带上，WS/`<img>`/`<video>` 同样生效；auth_request 在 WebSocket Upgrade 握手前执行，不破坏 upgrade）。
   - 动态路由同时校验非空 `Origin` 必须与 `$scheme://$http_host` 相同；appmgr
     对 POST/PUT/DELETE 再做一次规范化 scheme/host/port 比较。跨站请求返回 403，
     本机无 Origin 的运维调用请走 `127.0.0.1:8130`。

### 4.2 固件服务启动与运维

当前源码把 `S93inferenced`、`S94appmgr` 安装到 `/oem/usr/etc/init.d/`，由
`RkLunch.sh` 在发起 `rkipc` 后按编号执行。迁移目录只保证 IPC 已收到启动命令，
不代表摄像头接口已经完成初始化。停止时按 `S94 → S93 → IPC` 的顺序执行。

- 服务代码位于 `/usr/lib/recamera/{appmgr,inferenced}`；nginx 配置由构建安装到
  `/oem/usr/etc/nginx/ext_appmgr.conf`。应用、虚拟环境、配置与状态仍在 `/userdata`。
- `S93` 通过 inferenced 的 Hello + Status 握手判断就绪；`S94` 请求本机
  `http://127.0.0.1:8130/health`，验证服务身份和自身初始化状态。探针及重试均有上限，
  新启动进程超时会清理并返回失败；存在 PID 或 socket 文件不算启动成功。
- appmgr 先提供管理 HTTP 接口，应用恢复在后台执行。依赖 IPC 或 inferenced 的应用
  按自身能力声明等待协议接口；未就绪时进入 `waiting_dependency` 并自动重试，
  保留启动意图、不消耗应用崩溃重启次数。无相关依赖的应用可以正常启动。
- 构建会清除旧 `/etc/init.d/S93inferenced`、`S94appmgr`，避免重复启动。
  不再使用 `/userdata` 中的脚本或 nginx 主拷贝回注固件。

检查与手动重启（设备必须已安装匹配固件）：

```sh
adb shell "/oem/usr/etc/init.d/S93inferenced status"
adb shell "/oem/usr/etc/init.d/S94appmgr status"
adb shell "/oem/usr/etc/init.d/S94appmgr restart"
```

升级时必须同时提供匹配的 rootfs 和 OEM 内容。`market/deploy/appmgr-restore.sh`
仅作为旧运维入口兼容：验证固件内两个 OEM 脚本存在后按序启动，不再复制旧 launcher。
若新固件缺少服务，需安装匹配固件，不能用历史 `/userdata` 代码补齐。

### 4.3 分步验证：先传 appmgr 核心（<1 MB），再传 app 包（81 MB）

验界面 / 验后端**不需要**先传应用包。**只传 appmgr 核心（<1 MB）就能起后端 + 验界面与 API**；真正安装应用时才需要那个 **81 MB 的 app 包**。分两步走，界面/接口出问题能立刻定位到底是"框架没起来"还是"应用装不上"，不用每次拖 81 MB。

### 4.4 运行时依赖（rknnlite / interpreter venv / recamera_ext）——**app 能跑的前提**

装好 app 包 **≠** app 能跑。appmgr 只负责分发 + 监督进程；**app 的 Python 运行时依赖不在打包/上架链路内，须由运行时侧预先 provision**。9 个 app（8 视觉 + voice）的 manifest `interpreter` 都指向 rknn venv `/userdata/rknnenv/bin/python`——app 就在这个解释器下跑。

**三件事必须为真，app 才能真正运行（核实自 `market/deploy/provision-runtime.sh` 头注 + `supervisor.py`）**：

1. **`rknnlite` / venv site-packages 可达**：app 在 NPU 上推理走的是 `rknnlite`（Python 层），**固件不自带**——固件里 rkipc 用的是 C 层 `librknnrt.so`，两者不是一回事。`rknnlite` 装在 `/userdata/rknnenv` venv 里，系统 python 看不到它；**由 manifest `interpreter=/userdata/rknnenv/bin/python` 保证**在该 venv 下启动（缺省则用 appmgr 自己的 `sys.executable`，`supervisor.py:106-113`；`interpreter` 路径设备上不存在时 `switch` 硬报错 `manifest interpreter not found on device`）。
2. **`import recamera_ext` 成立**：官方扩展 API 的 Python 绑定在 SDK 树 `/userdata/sdk/python/recamera_ext`，**不在 venv 默认 sys.path 上**。`provision-runtime.sh` 往 venv site-packages 写一个 `recamera_sdk.pth` 指向 `/userdata/sdk/python`，使该 venv 起的每个进程都能 `import recamera_ext`（取代 per-session `PYTHONPATH export` 的持久做法）。
3. **native lib `librecamera_ext.so.1` 能加载**：`recamera_ext` 会 dlopen 它，而它在 `/oem/usr/lib`——不在 musl loader 默认搜索路径上。`supervisor.start()` 给**它拉起的每个 app** 注入 `LD_LIBRARY_PATH=/oem/usr/lib:/oem/lib`，同时注入 `KIT_PARENT` + `PYTHONPATH`（指向共享 kit），并以 `<interp> -m kit.run <app_dir>/<entry>` 启动，所以 UI / HTTP API / boot-restore 启动的 app 一律继承，无需手敲 `export`。

**基础环境 provision 脚本 `market/deploy/provision-runtime.sh`**（幂等，设备上 `sh provision-runtime.sh`）：校验 (1) venv python 在、(2) SDK 绑定在 + 写 `.pth`、(3) `.so` 在，最后**在 venv 下实跑 `import recamera_ext` 自检**，PASS/FAIL 汇总，硬前提缺失即非零退出。注意 (3) 的 `.so` 与 SDK 绑定树都由**扩展 API 固件**提供（随 `/oem` OTA），本脚本只校验、不安装。

**voice 音频运行时 `market/deploy/provision-voice.sh`**（voice-transcribe 专用，视觉 app 不需要）：在 rknn venv 里从**离线 wheelhouse** 补装音频依赖（`voxedge` / `sherpa_onnx` / `kaldi_native_fbank` / `sentencepiece`，`numpy`+`rknnlite` 复用）→ 把 ASR 模型集 copy 进共享目录 `/userdata/local/models/asr/`（`sensevoice_rv1126b_w4a16.rknn` + `am.mvn` + `embedding.npy` + BPE + `silero_vad.onnx`，KWS 唤醒模型可选）→ 往 `/userdata/local/appdata/voice-transcribe/config.json` 写 `asr_backend=rk`、`wake_backend=kws|asr`（`asr_backend` 是内部键、不在 config_schema，故直接落 `config.json`；UI 改配置会丢它，需重跑本脚本；旧位置 `<app_dir>/config.json` 若存在会被脚本一并搬过去并改名 `.migrated`）。同样幂等 + 自检。

- **共享模型落位（legacy）**：旧 catalog 的 `putModel` 浏览器代取链路已从公网关闭；
  voice-transcribe 的 Web v2 包应声明/携带 authenticated bundled artifacts。
  旧设备迁移仍可在设备本机运行 `provision-voice.sh`，把模型落到
  `/userdata/local/models/asr`。设备侧确认：`ls -lh /userdata/local/models/asr/`。

> **依赖分层**：`rknnlite`/`numpy`/`cv2` 这类平台依赖来自只读系统运行时；
> app 独有 wheel 和模型由 manifest v2 精确声明并离线打包，安装到 per-release
> 隔离环境。旧 catalog `models[]`+`putModel` 仅供 loopback 迁移。

**★按需运行时（现在的默认路径，2026-08-15 起）**：音频依赖已进分发链路，不必再手工 provision。app 在 manifest 声明 `capabilities: ["audio"]`，`gen_catalog` 把它连同 `runtimes.audio` 描述符写进 catalog，应用中心装它之前先问设备 `GET /api/appMgr/runtime?name=audio`，缺则取 `voice-runtime-<ver>.tar.gz`（约 18 MB，5 个 aarch64/cp311 wheel）经 `POST /api/appMgr/runtime` 离线装进 `/userdata/rknnenv`。幂等：已就位直接跳过，不跑 pip。

> ⚠️ **离线安装用 `--no-deps`，别去满足 `voxedge` 的 `numpy>=1.24`。** 共享 venv 里 numpy 是 **1.23.5**——那是 `rknn-toolkit-lite2 2.3.2` 配套的版本，9 个视觉 app 都靠它。升 numpy 是拿 9 个能跑的换 1 个。那个下限也不是真的：voxedge 实际用到的 numpy 接口没有一个是 1.24 之后新增的。判定成败的是安装后在目标解释器里 `import voxedge, sherpa_onnx`，而不是 pip 的元数据断言。真机已验证共存：装完 numpy 仍 1.23.5、`rknnlite` 仍可导入、语音 app 能加载 SenseVoice 并进入 listening。

> **安装时跳过已有模型**：装之前先 `GET /api/appMgr/assets?paths=…` 问设备哪些模型已在、大小与 sha256 是否一致，命中的**连 CDN 都不下**。voice-transcribe 那个 133 MB 的 ASR 模型因此不再重传（原先必然撞 nginx `proxy_read_timeout 200s` 而失败）。设备侧 sha256 按 `(size, mtime_ns, inode)` 缓存，实测 133 MB 首次 0.825s、第二次 0.0066s。

> 小结：部署一个"带 venv + 共享模型"的 app（如 voice-transcribe）＝ ①`provision-runtime.sh`（venv + recamera_ext 可导入）→ ②装 app 包（应用中心会自动补音频运行时、跳过已有模型；**完全离线**时改跑 `provision-voice.sh`）→ ③switch。视觉 app 只需 ①②③。缺 provision 的那一步会在 switch/运行时失败，而非安装时。

> **卸载**：`POST /api/appMgr/uninstall {id}`（CLI `python3 -m appmgr uninstall <id>`）停→清 active→删 `/userdata/local/apps/<id>/` + per-app venv `/userdata/local/venvs/<id>`（若有），**共享模型 `/userdata/local/models` 不动**（跨 app 资产）。详见 publishing §6/§7。

### 4.5 内建开关 + activate/config API + 前端部署（本轮三线补充）

> 本轮把内建推理收敛成一等 app + 配置热更 + 结果软件叠加落地。API 语义见 [inference-as-app.md](./inference-as-app.md) 与 [ai-result-overlay.md](./ai-result-overlay.md)，此处只记部署/运维要点。

**内建检测开关（entry.cgi）**：`POST https://127.0.0.1/cgi-bin/entry.cgi/model/inference {"iEnable":0|1}`——localhost 免 JWT，**走 443 不走 80**（80→307；固件 HTTPS 关闭时 443→307 http，`builtin.py`/`cgi_control.py` 跟随一跳，见 control-api.md §1.3）。appmgr 的 `builtin.py` driver 内部就是调它 + `/model/info`（换模型时要与一次 `/model/inference` POST 配对才触发重载，`builtin.py:22-25`）。关内建后 RTSP 码流无框（软件叠加不进码流）。

**appmgr activate / config API**（loopback `127.0.0.1:8130`，nginx `/api/appMgr/` JWT 门）：

| 方法 | 路径 | body | 语义 | 源 |
|---|---|---|---|---|
| POST | `/api/appMgr/activate` | `{id}` | 单活互斥切换；`id="builtin"` 开内建关自建、自建 id 关内建起该 app、`"none"`/空 停当前自建 | `server.py:652` / `do_activate:309` |
| GET | `/api/appMgr/config` | `?id=`（含 `?id=builtin`） | 读 `{config_schema, values, defaults}`；builtin 走 driver 反组装成同构视图 | `server.py:575` / `builtin.get_config` |
| POST | `/api/appMgr/config` | `{id, config:{...}}` | merge 写 `config.json` + 按 `apply` 分流：全 `live`→`kill -HUP`，任一 `restart`→stop+start | `do_set_config:288` |

- `activate` 是把 builtin 纳入后的统一单活入口（旧 `switch` 是自建-only）。完整端点表见 [app-center-publishing.md](./app-center-publishing.md) §7。
- 配置热更纪律：`live` 项经 SIGHUP 热重读（`kit/app.py` `on_config_reload`，不重启不抢相机），`restart` 项才重启进程。用户配置存放在 `/userdata/local/appdata/<id>/config.json`（**不在**安装目录内，升级不会清掉；旧位置的文件首次读/写/安装时自动迁移）。`asr_backend` 这类内部键不在 `config_schema`，UI 改配置会丢它，须直接落 `config.json`（见 §4.4 provision-voice）。

**结果软件叠加落位**：自建 app 结果广播到本机 WS `:8124`（`WsResultSink` 默认），nginx `/appcenter/ws/results` 反代（JWT 门），官方 React `/preview` 页的 `AiResultOverlay` 订阅并 canvas 画框。烧进码流（OSD）是 opt-in（`RECAMERA_RESULT_OSD=1`/`kind=osd`/`PREFER=official`），默认不启用。

**前端部署到 `/oem/usr/www`（官方 web-native 整合后）**：App Center 现为官方 React 页（`/app-center` 路由），前端静态产物落 **`/oem/usr/www`**——

- `/oem` 是 **ext4 可写**分区，可直接铺前端产物（不必走 `/userdata` 旁挂；旧 `/appcenter` vanilla SPA 已 LEGACY）。
- **OTA 会洗 `/oem`**：A/B 刷 rootfs 后前端产物被还原成原厂，**需重新铺一遍**（与 rkipc/entry.cgi 热替换同一类 OTA-非存活问题，见 §4.2 / 第 2.1 节）。铺前先备份原厂 `www`（`www-official-backup`，§6 二级回滚已列）。
- **文件 mode 必须 644**：铺进 `/oem/usr/www` 的静态文件权限位要给读（`chmod 644`，目录 755）。**权限不对 nginx 返 500**（读不到文件），不是 404——排查"页面 500"先查 www 下文件 mode，busybox `cp` 不保留位、`scp`/解包后按 umask 可能落成非 644。

**服务启动行为与实机验证**：

- **后台恢复**：appmgr 的后台协调线程恢复期望运行的应用，HTTP 接口可用于观察等待原因与停止应用；实机需覆盖 IPC 延迟启动、不可用和重启后的应用恢复。
- **setsid 分层**：app 子进程由 supervisor 以 `start_new_session=True`（setsid，各自 session/进程组 leader，`supervisor.py:194`）拉起；而 **`appmgr serve` 自身**从 S94 只用普通后台 `&`（`S94appmgr:81-84`，因 busybox `start-stop-daemon -b` mishandle 需 cd 的 wrapper），未单独 setsid 脱离会话。当前靠 pidfile + appmgr flock 单实例兜。**TODO / 需核实**：serve 未 setsid 化在非 init 启动路径（adb shell 手动 `start` 后退出 shell）下是否被 reap——init 路径下 reparent 到 init 无碍，手动调试路径参照 §7"adb shell 一退就没了"行处理（写脚本 `exec` 或验完 reboot）。

---

## 5. 验证清单

reboot / 部署后依次核对：

- [ ] **rkipc md5**：`md5sum /oem/usr/bin/rkipc` = `9826e9ecf8ed543a6dc78e3731102e0f`（或热替换目标值）
- [ ] **六 socket**：`ls -l /run/recamera/` 有 `frame.sock` `result-in.sock` `osd-in.sock` `record-in.sock` `probe.sock` `inference-control.sock`（+ `apps.d/`）
- [ ] **RTSP 出流**：`rtsp://<ip>:554/...` 有画面
- [ ] **内建推理**：系统内置检测框正常上 OSD / RTSP（内建走同一条 `rc_result_dispatch`）
- [ ] **结果回注端到端**：外部脚本 / SDK 向 `result-in.sock` 注入高辨识度检测 → RTSP 看到框+标签 → WS 收到 `source_id≠"builtin"` 的结果；确认它不会启动 Vigil 录像；冒充 `"builtin"` 被拒；超速被丢+计数
- [ ] **托管应用录像触发**：安装带 `record_trigger` 的 app → `/api/app-center/v1/recording/sources` 可见其来源 → Vigil 选择该信号后，匹配结果能启动录像；停止/升级 app 后旧 debounce 状态已清除
- [ ] **SDK 握手**（任何人可连）：
  ```sh
  export LD_LIBRARY_PATH=/oem/usr/lib:$LD_LIBRARY_PATH
  export PYTHONPATH=/userdata/sdk/python:$PYTHONPATH
  python3 - <<'PY'
  import recamera_ext as re
  s = re.ResultSink("selftest")   # 打开 result-in.sock 握手
  print("rc =", s.send_detections(123456, [(0.05,0.07,0.62,0.94,0.9,"person",0)]))  # 归一化 [0,1]; 0 = accepted
  PY
  ```
- [ ] **应用中心**（若部署）：`/appcenter/` catalog 页面可开、`/api/appMgr/list` 经 JWT 返回正常、`appmgr` 进程在（`/oem/usr/etc/init.d/S94appmgr status`）
- [ ] **运行时 provision**（装任何 app 前）：`sh /userdata/local/appcenter/provision-runtime.sh` 打印 `RESULT: PASS`——venv python 在、`recamera_sdk.pth` 已写、`librecamera_ext.so.1` 在、venv 下 `import recamera_ext` 自检通过
- [ ] **共享模型 app**（若装 voice-transcribe 类）：`interpreter` venv 就位（`ls /userdata/rknnenv/bin/python`）、共享模型已落盘（`ls -lh /userdata/local/models/asr/` 必需 5 文件齐、rknn ~133 MB）、`rknnlite` 可导入（在该 venv 里 `python -c "import rknnlite"`）、音频依赖可导入（`python -c "import voxedge, sherpa_onnx"`）。音频运行时正常由应用中心按需补齐，查 `curl 'http://127.0.0.1:8130/api/appMgr/runtime?name=audio'` 应为 `present: true`；**完全离线**时才需要 `sh provision-voice.sh` 打印 `PASS`
- [ ] **装完 numpy 没被动过**（装过音频运行时后必查）：`/userdata/rknnenv/bin/python3 -c "import numpy; print(numpy.__version__)"` 应仍是 **1.23.5**，且 `import rknnlite` 仍通过——numpy 被顶上去会连累 9 个视觉 app
- [ ] **dmesg 无 VPSS 崩溃**：`dmesg | grep -iE 'vpss|fifo|Oops|paging request'` 空

---

## 6. 当前允许的恢复方式

仓内历史 `rollback.sh` 已禁用：它的 factory/ext 哈希集合重叠，不能证明
`/userdata/rkipc.factory.bak` 真是原厂文件。禁止手工把这类未验证备份复制到
`/oem`。

当前只允许使用经项目发布方批准的官方 `update.img` / OTA / recovery 流程，
或使用在本包之外独立验证、并有设备型号/固件版本/BOM 记录的 factory artifact。
新的发布流水线必须先建立互斥的 factory/ext 身份库并测试回滚，才能重新开放
增量回滚脚本。

---

## 7. 运维故障速查

| 现象 | 规避 / 处理 |
|---|---|
| **cp 二进制进 `/oem` 后 rkipc 起不来**（无进程、无 `/run/recamera/`，RkLunch 日志 `Permission denied`）| busybox `cp` 不保留执行位。cp 进 `/oem/usr/bin/` 后**必 `chmod 755`**（entry.cgi 同理）。热替换脚本 cp 后紧跟 chmod。|
| **整机 hang：ping 通但 SSH/adb/HTTP 全无响应** | 多半是 `while true; do rkipc; ...` 兜守护进程触发 **fork 风暴**耗尽 pid/内存（内核网络栈还应答 ICMP，用户态服务全 fork 不出会话）。**禁止 while-true 重拉 rkipc**；保活用设备自己的 SysVinit（`adb reboot` 让 init 干净拉起）。已 hang **只能物理断电重启**（fork 风暴不写盘，重启后干净）。别密集重试 SSH（加剧 + 触发 sshd MaxStartups）。|
| **热替换后双实例抢相机 → VPSS 崩溃** | `killall rkipc` 精确匹配进程名，漏杀 `rkipc.m2` 这种带后缀的自编版。一律 `for p in $(pgrep -f rkipc); do kill $p; done`（`pgrep -f` 匹配完整命令行）。**kill 后 `sleep 3` 再起新实例**，等 VPSS/VI 资源释放。|
| **dmesg 出现 `CSIBDG fifo overflow` / `vpss ... fifo overflow` / `Unable to handle kernel paging request` / `Oops`** | cv181x/rkvpss 内核驱动处理 FIFO 溢出时崩溃（驱动 bug，救不回）。一见立即 kill rkipc + `adb reboot` + 停手排查。**预防**：同一时刻只一个进程用相机、别碰 rkipc 正用的 chn、kill 干净再起。|
| **后台起 rkipc / adb shell 一退就没了** | adbd 退出会 reap 它的子进程树，`setsid` / `start-stop-daemon -b` 挡不住。改用：脚本 push 后 `adb shell 'sh run.sh'`（脚本内 `exec rkipc`，reparent 到 init）、或验完 `adb reboot` 让 init 拉起、或起进程后别退 adb shell（前台盯）。|
| **重启 appmgr 时 `pkill -f 'appmgr serve'` 把自己杀了**（重启脚本刚起就没、shell 被 SIGTERM）| `pkill -f` 按完整命令行匹配，会命中**正在执行重启的那条命令本身**（含 `appmgr serve` 子串）→ 自杀。停旧进程改为：扫 `/proc/<pid>/cmdline` 匹配 `-m appmgr serve` 且**排除当前 shell 的 `$$`**，再逐个 kill；起新进程用 `cd /userdata/local && setsid python3 -m appmgr serve </dev/null &>>log &`（reparent 到 init，无 env hack）。`deploy-app.sh` step 2 已内建这套。|
| **画了检测框但 RTSP / 编码流里看不到**（datetime 却可见）| VENC 只合成部分 RGN layer：每通道最高层不被合成。INFER overlay 用被合成的低 layer（主 **1** 子 **5**，不是 3/7）。排查"画了不显示"先怀疑 layer 值，不是坐标/颜色/SetBitMap。附带：OSD 调色板注意 ARGB/BGRA 字节序（品红写反显示成青色）。|
| **改过 osd 的自编 rkipc 起不来**（`osd_manager_init` 因 `rkipc.ini` 缺 `[osd] cfg=` 返 `-ENOENT`）| 已修：`osd_manager_load_cfg` 缺 cfg 降级为内置默认（inferenceOverlay 默认开）而非 init 失败。用含此修复的 rkipc（本 release 已含）。|
| **扩展应用连麦克风 / 相机权限拒绝** | `/dev/snd` `/dev/video` `/dev/mpi` 全 root 属主，SSH 的 admin（uid 1000）不在 audio 组、开不了硬件。**扩展应用必须以 root 运行**（appmgr / 约定的 SysVinit 脚本以 root 启动）。调试用 adb（root）而非 ssh admin。|
| **nginx 挂扩展前端 404 / 冲突** | 出厂 nginx 只显式列 `svc_*`，`ext_` 是新约定；确认 `common_relay.conf` 有 `include ext_*.conf` 且固件已安装 `ext_appmgr.conf` 到 `/oem/usr/etc/nginx/`。校验必须**用完整合成配置**：`nginx -t`（或 `nginx -T` 打印）通过再 `nginx -s reload`；reload 失败应恢复之前的配置再 reload。服务启动脚本不改写 nginx 配置。|
| **busybox 工具缺失 / 行为不同** | 设备 busybox：**无 `stat`、无 `timeout`**；`cp` 不保留执行位（cp 后 chmod）；`tar` **不认 `-z`**，解压 `.tar.gz` 用 `gunzip -c x.tar.gz \| tar -x`。校验 nginx 用 `nginx -t`（必要时 `-c` 指定合成配置路径）。复杂命令别在 `adb shell "sh -c \"...$(...)...\""` 里嵌套引号（busybox 报 `syntax error unexpected "("`）→ echo 成脚本文件 push 后 `sh`。|
| **Tailscale 慢链路传大文件超时 / 卡死** | 设备在 Tailscale（100.x）时延迟 35-82ms，adb/scp 给足 timeout、别在一条命令里死等。大文件用小包 / 逐 tar 单文件传，或走 Mac 中转（`fleet pull <src> ... <mac>` → `adb push`）。局域网同网段（192.168.x）可直连（<1ms），优先直连。|

---

## 8. 网络拓扑

- **局域网同网段直连**（设备 192.168.x，如 192.168.42.1 / 192.168.42.1）：Mac 或 wsl2-local 可直连，<1-4ms 延迟，adb / scp / ssh 直接走。首选。
- **Tailscale**（设备 100.x）：wsl2-local 可能连不上（Tailscale 不互通），走 **Mac 中转**（`fleet pull` 到 Mac → `adb push`）。延迟高，所有 adb/scp 给足 timeout。
- 设备访问凭据：SSH `admin` / `***REDACTED***`（uid 1000，**无 sudo**）；root 走 `adb connect <ip>:5555`（adbd 以 root 跑）。`/userdata` ext4 rw 无 noexec，可放交叉编译二进制直接跑。

---

## 9. 跨固件 build 兼容

基于 `recamera_v2` 源码交叉编译的 rkipc / entry.cgi 在不同固件 build 上**跨 build ABI 兼容**，一次编译可跨 build 部署：

- 实证 build：V1.0.4（`-g2288d28`）与 V1.0.10（`-g6a9166b`），rkipc md5 不同、build 串不同。
- 兼容根据：两 build kernel 同为 **6.1.157**，MPI / rockit 库在位；动态库逐行解析一致（libcgicc / libssl / libcurl / librockit / glibc 2.38 全在）。
- 含义：扩展 API 改动**不必逐固件版本重编**。M1 3b 在 V1.0.10 上直接跑自编 rkipc（握手 / 身份 / 限速 / 端到端全通、主体不回归），门禁 G1-G4 也在原厂 V1.0.10 上完成（无需先刷自编固件）。
- 热替换（cp 覆盖或 bind mount 单文件）即时可回滚，不动 bootloader / 分区，无变砖风险。

# reCamera Pro 部署说明(总览)

> **历史流程，当前禁止照此部署固件。** 这里引用的预编译 sideload 包不含
> `inference-control@1`，且不是当前源码/manifest 生成的一致发布物；不要运行
> `deploy-firmware.sh`。下一正式版本须从固定 commit 的源码构建完整固件。

> **读者**:要把扩展 API / 应用中心装到设备上的人。
> **本文回答**:装什么、用哪个脚本、什么顺序、出事怎么退。
> 与本目录 `README.md` 的分工:**README 讲某一个版本的包(含校验值),本文讲部署流程本身**,不随版本变。
> **校验值不在本文** —— 以随包发布的 `README.md` 文末「校验」表为准(它与包同批上传,不会漂移)。

---

## 0. 一句话

日常只用一条命令:

```bash
cd release/deploy && ./deploy-app.sh --host <设备IP>
```

它只动**应用层**,不碰固件。固件是另一条高危路径,单独跑、且需要你能物理复位设备。

**默认不预装应用。** `apps-v<ver>.tar.gz` 里只有代码和 manifest,**不含模型**(9 个 app 合计 987 KB);推上去的结果是 9 个 app 全部显示"已安装"、一启动就 `Invalid RKNN model path`。而应用中心的安装路径走 catalog,拉的是**含模型的完整包**(CDN 上单个 3–53 MB),装完即可用。所以出厂设备保持干净,由用户在应用中心按需安装。

要恢复旧行为(演示机 / 装机站预装,模型另行下发)加 `--with-apps`。不加时第 5 步的激活自动跳过——没有东西可激活。

---

## 1. 设备上有哪几层

从下到上,越下越危险、越少动:

| 层 | 位置 | 谁装 | 多久动一次 |
|---|---|---|---|
| **固件**(rkipc / entry.cgi / `librecamera_ext.so`) | `/oem/usr/{bin,www/cgi-bin,lib}` | `deploy-firmware.sh` | 很少;**换 rkipc 必须冷启动** |
| **kit 运行时 + SDK + venv** | `/userdata/local/kit`、`/userdata/sdk`、`/userdata/rknnenv` | `deploy-app.sh` 第 1 步 | 跟随 kit 改动 |
| **appmgr**(应用中心后端) | `/userdata/local/appmgr` | `deploy-app.sh` 第 2 步 | 跟随 appmgr 改动 |
| **nginx 边缘配置 + 开机启动**(`ext_appmgr.conf` → `/oem/usr/etc/nginx/`,`S94appmgr` → `/etc/init.d/`,master 在 `/userdata`) | 见左 | `deploy-app.sh` 第 2b 步 | 随脚本;出厂/恢复出厂后没有它们 → 浏览器装 app 报 `405`、重启后 appmgr 不起 |
| **前端**(React 静态产物) | `/oem/usr/www` | `deploy-app.sh` 第 3 步 | 跟随前端改动 |
| **应用** | `/userdata/local/apps/<id>` | **App Center 安装**(默认);`deploy-app.sh --with-apps` 才预推 | 经常 |
| **用户配置** | `/userdata/local/appdata/<id>/config.json` | 用户在 UI 改 | **不随升级丢失** |
| **共享模型** | `/userdata/local/models/` | catalog `putModel` / provision 脚本 | 很少(体积大) |

> 用户配置**不在**安装目录内。这是有意的:升级是整目录替换,任何放在 `apps/<id>/` 里的东西都活不过一次升级。旧位置的文件会在首次读/写/安装时自动迁移,并留下 `.migrated` 备份。

---

## 2. 常规部署(应用层)

```bash
cd release/deploy
./deploy-app.sh --host 192.168.x.x      # 指定设备 IP(IP 易变,先确认)
./deploy-app.sh --skip-kit              # kit 没变,只更 appmgr/前端/apps
./deploy-app.sh --no-activate           # 不启动 app、不碰摄像头
```

**前提**:控制机装了 `adb`;设备 `adbd` 以 root 跑(`adb connect <ip>:5555`),**不需要 SSH 密码,也不需要 sudo**。`/userdata` 需 ~200 MB 余量。

**5 步(每步幂等,替换前按时间戳备份到 `/userdata/_deploy/backups/`)**:

1. **kit + SDK + wheels** — 跑 kit 包自带的 `INSTALL.sh`
2. **appmgr** — merge-extract(覆盖 `.py` 与 `keys/`,保留运行态 `audit.log`/`mqtt.json`/锁),然后重启服务
3. **前端** — `static/` 整目录替换(清掉旧 hash bundle),**目录 755 / 文件 644**
4. **apps** — merge-extract 代码与 manifest,**保留设备上已有的大模型文件**
5. **激活 + 校验** — 激活一个 app,确认 `:8124` 结果流出帧,查 dmesg 无 VPSS

**内置安全护栏**:
- **绝不碰 rkipc**:部署前后各取一次 `/oem/usr/bin/rkipc` 的 md5,收尾断言未变,变了直接 FATAL
- 每个碰摄像头的步骤后查 `dmesg`,出现 `vpss err` / `CSIBDG fifo overflow` / `Oops` 立即停
- 大文件 push 用 **md5 校验 + 重试**(adb over Tailscale 大包偶发 `EOF`)

---

## 3. 固件层(高危,单独跑)

> ⚠️ **换 rkipc 必须冷启动(整机 `reboot`)激活。热替换会触发 `cv181x_vpss` / CSIBDG FIFO 内核 oops,可能把设备搞挂。只在你能物理复位设备时才跑。**

```bash
./deploy-firmware.sh --host <ip>            # 装入 /oem,停在 reboot 前
./deploy-firmware.sh --host <ip> --reboot   # 装完立即重启
./deploy-firmware.sh --host <ip> --rollback # 回滚原厂 rkipc
```

脚本会要求你手工输入 `I-HAVE-PHYSICAL-ACCESS` 才继续。

**只有需要扩展 API 能力(帧代理 / 结果回注 / 硬件遮罩 / probe)时才装固件。** `deploy-app.sh` 不依赖它也能把 apps/前端/appmgr 跑起来。

**回滚点按内容判定,不按 md5 白名单**:`install.sh` 只把**不带扩展标记**的 rkipc 存为回滚点。判据是文件内容——我们的构建带 `/run/recamera`/`rc_ext_` 符号,原厂不带(entry.cgi 对应 `ExtApiHandler`)。**不能用 `osd_rgn_cover_`**:那是 vendor 的 OSD cover 区域符号,遮罩功能建在它上面,原厂 rkipc 同样含有(真机实测:出厂 `d5e7ca93` 有 2 处,扩展 `9826e9ec` 有 8 处),拿它判会把真出厂备份误判成扩展构建。这条对**任何**固件基线成立,不需要维护 md5 列表。

早先用的 `VERIFIED_FACTORY_MD5S` 白名单已废弃:它测的是"这个文件我见过没有",而不是"这是不是原厂",结果是没收录过的官方基线在现场一律 abort(实测 `192.168.42.1` 的 2026-08 基线就被拦下),而且同一个 md5 曾同时出现在 factory 和 ext 两个列表里。

`/oem` 已是扩展构建且无备份时仍会拒绝——把扩展构建存成"原厂"会让日后回滚变空操作。这种情况可以 `--force` 强行装,代价是没有本地回滚点;此时的恢复路径是完整 OTA / update.img 刷机,它会把 `/oem` 重写回原厂。

**固件基线兼容性是独立的一项检查**,默认只**告警**不拦截:我们的 rkipc 从特定基线构建,装到别的基线上等于把相机主程序换成另一版。加 `--strict` 才会因基线未验证而中止。

---

## 4. 一次性运行时补给

这些**不在** `deploy-app.sh` 里,首装或 OTA 后需要单独跑:

| 脚本 | 解决什么 | 谁需要 |
|---|---|---|
| `market/deploy/provision-runtime.sh` | 让 `recamera_ext` 在 rknn venv 里可导入 | **所有 app** |
| `market/deploy/appmgr-restore.sh` | OTA 会冲掉 `/etc/init.d/S94appmgr`,靠它恢复(重跑 `deploy-app.sh` 也会重装) | **OTA 之后** |

装一个 app 的顺序:
```
provision-runtime.sh  →  装 app 包(应用中心或 deploy-app.sh)  →  激活
```

**音频运行时不再需要手工 provision。** `voice-transcribe` 这类声明 `capabilities: ["audio"]` 的 app,应用中心会在安装时检测运行时缺不缺,缺则从 CDN 取 `voice-runtime-<ver>.tar.gz`(约 18 MB,5 个 aarch64/cp311 wheel)装进 `/userdata/rknnenv`。它**不进 kit 包** —— 8 个视觉 app 永远用不到,而 kit 包每次更新都要重传。
手工触发:

```bash
curl -X POST http://127.0.0.1:8130/api/appMgr/runtime \
  -H 'Content-Type: application/json' \
  -d '{"name":"audio","path":"/userdata/appstage/voice-runtime-<ver>.tar.gz"}'
curl 'http://127.0.0.1:8130/api/appMgr/runtime?name=audio'   # present:true 才算装好
```

> ⚠️ **不要为了装它去升级 numpy。** `/userdata/rknnenv` 是**共享** venv,9 个视觉 app 全靠里面的 `rknn-toolkit-lite2 2.3.2`,设备上 numpy 是 **1.23.5**(正是该工具链配套版本)。`voxedge` 的 metadata 写 `numpy>=1.24`,但它实际用到的接口没有一个是 1.24 之后新增的 —— 那是作者随手写的下限。所以离线安装走 `--no-deps`,判定成败的是安装后在目标解释器里 `import voxedge, sherpa_onnx` 是否通过,而不是 pip 的元数据断言。真机已验证:装完 numpy 仍 1.23.5、`rknnlite` 仍能 import、语音 app 能加载 SenseVoice 并进入 listening。

> `market/deploy/provision-voice.sh` 仍然保留,用于**完全离线**(设备连不上 CDN)时从本地 payload 落音频依赖 + ASR 模型。它比上面这条路多落一个 `silero_vad.onnx`,细节见 `docs/guide/deploy-ops.md` §4.4。两条路二选一,不要都跑。

---

## 5. 出事怎么退

| 层 | 回滚方式 |
|---|---|
| **应用层** | `/userdata/_deploy/backups/` 下有每次部署的时间戳备份(`appmgr.<ts>/`、`www.<ts>.tar.gz`、`state.json.<ts>`);kit/SDK 旧副本在 `/userdata/local/kit.bak.<ts>` |
| **单个 app 升级** | 升级会保留上一代为 `<app_dir>.prev`(只留一代),`mv` 回去即可 |
| **用户配置** | 卸载**不删**配置(重装即恢复);旧位置迁移时留有 `config.json.migrated` |
| **固件** | `./deploy-firmware.sh --host <ip> --rollback`,恢复后**需冷启动** |

设备上 `tar` 是 busybox 版,**没有 `-z`**,解压要 `gzip -dc x.tar.gz | tar xf -`。

---

## 6. 从 CDN 取包

```bash
BASE=https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/<版本>
mkdir -p deploy && cd deploy
for f in recamera-ext-kit-*.tar.gz recamera-ext-api-*.tar appmgr-*.tar.gz \
         frontend-*.tar.gz apps-*.tar.gz deploy-app.sh deploy-firmware.sh README.md; do
  curl -fSL "$BASE/$f" -o "$f"
done
chmod +x deploy-app.sh deploy-firmware.sh
curl -fsSL "$BASE/README.md" | sed -n '/^## 校验/,$p'    # 核对 md5
```

---

## 7. 踩过的坑(照做能省事)

- **`KIT_PARENT` 是"装着 `kit/` 的那一层"**,即 `/userdata/local`,**不是** `/userdata/local/kit`。写错会让所有 app 起不来(`ModuleNotFoundError: No module named 'kit'`),且 `S94appmgr` 不设这个变量,**重启不会自愈**。现在 appmgr 用绝对路径执行 `<KIT_PARENT>/kit/run.py`(自定位),不再依赖 `PYTHONPATH` 配对。
- **前端目录权限**:`cp -a` 出来的目录可能是 700,nginx(www-data)进不去,`try_files` 会回落到 `index.html`,表现为"JS 返回了 HTML"。**目录 755 / 文件 644**。
- **在 macOS 上打包前端要 `COPYFILE_DISABLE=1`**,否则 tar 里混进 `._*` AppleDouble 文件,一路带到 `/oem/usr/www`。
- **停 appmgr 不要用 `pkill -f 'appmgr serve'`** —— 它会匹配到执行重启的那条命令本身,把自己的 shell 杀掉。扫 `/proc/<pid>/cmdline` 并排除当前 shell,再 `setsid` 起新的。
- **同一时刻只能有一个 app 用摄像头**。切换用 `POST /api/appMgr/activate`(单活互斥),它会先关掉内建推理。
- **Tailscale 上大文件 adb push 偶发 `device offline`** —— 设备通常没事,按 md5 判定并重试,或打成单个 tar 再传。

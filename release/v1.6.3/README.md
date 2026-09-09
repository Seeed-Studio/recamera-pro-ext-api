# reCamera Pro v1.6.3 — 一键部署包 (application layer)

把本轮应用层改动一次性部署到设备，让设备达到 v1.6.3 完整状态。
**固件层（rkipc 遮罩固件）单独、高危、默认不在一键流程内** —— 见文末。

> **本版两项变更**：
> 1. **kit 推理后端换成 ctypes 直驱 librknnrt**（`kit/runtime/ctypes_rknn.py` 新增、`kit/runtime/engine.py` 改写）：`rknn_toolkit_lite2` 的 Cython 扩展每次 `inference()` 有未释放内存，九输出 YOLOv8 图上实测 43.8 kB/次、18.8 fps 下约 2.2 MB/min，2 GB 板子上数小时耗尽。所有 vision app 都走 `kit.runtime.engine.RknnModel`，所以都受影响。
> 2. **`deploy-app.sh` 的 nginx 边缘自检接受 403**：JWT 网关在 HTTPS 打开时返回 403 而非 401，旧判据在装完 conf 之后才中止部署。

**固件产物（rkipc / entry.cgi / `.so`）与 v1.6.0–v1.6.2 逐字节相同**，变的只是应用层。
已装过遮罩固件的设备不需要重装固件；跑 `deploy-app.sh` 即可。

## kit 推理后端（本版核心变更）

### 换掉的是哪一层

`kit/runtime/ctypes_rknn.py` 用 ctypes 复刻扩展所做的同一段 librknnrt 序列
（`rknn_inputs_set` + `rknn_run` + `rknn_outputs_get` + `rknn_outputs_release`，
`want_float=1`，即扩展 `.dynstr` 里出现的那些入口），同图、同输入、同采样脚本：

| 后端 | 6465 次推理后 `[heap]` 位移 |
|------|---------------------------|
| `rknnlite`（Cython 扩展） | 43.78 kB / 次 |
| `ctypes`（本版默认） | 0 B |

同一份厂商代码、同一组参数，唯一差别是扩展在不在调用路径上 —— 缺的 `free` 在扩展里，
去掉它是修复而不是把泄漏摊进预算。

### 输出等价性

切默认之前先测的等价性，不是切完再补：64 组输入（真值 clip 的 60 帧实拍
+ 黑/白/灰/噪声）× 9 个输出张量 = 77 952 000 个 float32 元素，**0 个不同**，
576 个张量全部 bit-identical；解码后的 post-NMS 框位置与分数差 0.0。
换 fall-detection 的 pose 图（输出形状不同）重跑一遍，确认没有把某个 app 的
head 假设带进 runtime。

### 退路

`RknnModel` 的公开接口不变（`path` / `infer` / `release` / 上下文管理器），
`kit/app.py` 与所有 app 无需改动。

```sh
ESK_RKNN_BACKEND=rknnlite   # 回到旧实现（保留为 RknnLiteModel），可原地复测泄漏
```

ctypes 初始化失败时会**打印**回退原因再退回 rknnlite，不静默。
`release()` 销毁 `rknn_context`，`outputs_release` 放在 `finally` —— 修掉的泄漏
没有换成新的泄漏。

### 打包防回归

`kit/runtime/ctypes_rknn.py` 是新文件。kit 包用 `cp -R kit/` 整树拷贝，所以新文件
本来就会进包 —— 但在此之前没有任何东西**断言**这件事：一旦哪天布局改成显式清单、
或者修复落在 `cp -R` 覆盖不到的树里，包会缺文件却不报错，设备继续跑旧代码。
`release/build-release.sh` 现在在打包后拿 `git ls-files` 逐个核对 kit 包成员：
tracked 文件缺失、或包内 md5 与工作树不一致，直接 build 失败。
另外 `scrub()` 增补了 `*.bak*` / `*.orig` / `._*` 等 glob —— 编辑器备份文件
（如 `engine.py.bak-pre-ctypes`）此前会被 `cp -R` 一起发到设备。

## 与 v1.6.2 的产物差异

| 产物 | 与 v1.6.2 | 说明 |
|------|-----------|------|
| `recamera-ext-kit-v1.6.3.tar.gz` | **不同** | ctypes 后端（新增 `runtime/ctypes_rknn.py`，改写 `runtime/engine.py`） |
| `recamera-ext-api-v1.6.3.tar` | **不同** | 仅包内 `MANIFEST.txt` 的版本号/构建日期变；rkipc / entry.cgi / `.so` 逐字节同 v1.6.0–v1.6.2 |
| `appmgr-v1.6.3.tar.gz` | **相同**（md5 `edc77215…`） | 确定性重建逐字节一致 |
| `apps-v1.6.3.tar.gz` | **相同**（md5 `3e667757…`） | 确定性重建逐字节一致 |
| `voice-runtime-1.0.0.tar.gz` | **相同**（md5 `ace48a68…`） | 未改 |
| `gst-hwcodec-1.0.0.tar.gz` | **相同**（md5 `8e6d286f…`） | 未改 |
| `frontend-v1.6.3.tar.gz` | **相同**（md5 `6a487491…`） | 前端未改；就是 v1.6.2 那份（bundle `main.1e2af984.js`，卡片描述悬停 tooltip）的逐字节拷贝 |
| `deploy-app.sh` | 不同 | `VER=1.6.3`；边缘自检接受 403 |
| `deploy-firmware.sh` | 不同 | 仅 `VER=1.6.3` |
| `S94appmgr` / `ext_appmgr.conf` | **相同** | 与 v1.6.2 逐字节一致 |

> 本仓没有 `web/` 构建目录，前端无法从源重建。前端本版未改，所以
> `frontend-v1.6.3.tar.gz` 直接是 `release/v1.6.2/frontend-v1.6.2.tar.gz`
> 的逐字节拷贝（v1.6.2 那份在 `aa05f87` 重打过，bundle `main.1e2af984.js`，
> CDN 已覆盖并回读校验）。前端没改时部署可以直接 `--skip-frontend`。

## 目录内容

| 文件 | 内容 | 部署目标 |
|------|------|----------|
| `recamera-ext-kit-v1.6.3.tar.gz` | kit 运行时 + SDK(python/lib/头文件) + 离线推理 wheels，自带 `INSTALL.sh` | `/userdata/local/kit`、`/userdata/sdk`、`/userdata/rknnenv` |
| `appmgr-v1.6.3.tar.gz` | App Center 管理器全部代码，含签名公钥 `keys/` | `/userdata/local/appmgr` |
| `frontend-v1.6.3.tar.gz` | React 前端构建产物（bundle `main.1e2af984.js`，同 v1.6.2） | `/oem/usr/www` |
| `apps-v1.6.3.tar.gz` | 9 个 app 的 `manifest.json` + `app.py`（+ 小配置）。**不含大模型** —— `deploy-app.sh` 默认**不推**它 | `/userdata/local/apps`（仅 `--with-apps`） |
| `voice-runtime-1.0.0.tar.gz` | 按需音频运行时（`runtimes.audio`，`kind:"pip"`） | 由 appmgr 按需拉取安装 |
| `gst-hwcodec-1.0.0.tar.gz` | 按需硬编解码运行时（`runtimes.hwcodec`，`kind:"files"`） | `/userdata/lib` |
| `recamera-ext-api-v1.6.3.tar` | 遮罩固件（rkipc + entry.cgi + `.so` + SDK + wheels），自带内容判定版 `install.sh`/`rollback.sh` | `/oem`（**高危，冷启动**） |
| `S94appmgr` / `ext_appmgr.conf` | 开机启动脚本 / nginx 边缘配置（`deploy-app.sh` 第 2b 步自动安装） | `/etc/init.d/`、`/oem/usr/etc/nginx/` |
| `deploy-app.sh` | 应用层一键部署主脚本（安全；默认不预装应用） | — |
| `deploy-firmware.sh` | 遮罩固件部署脚本（**高危，单独跑**） | — |

## 部署

```bash
cd release/v1.6.3
./deploy-app.sh --host <设备IP>                    # 应用层，不碰 rkipc
./deploy-app.sh --host <ip> --skip-frontend        # 前端未改时（本版就是这种情况）
./deploy-app.sh --host <ip> --skip-kit             # kit 没变时
./deploy-app.sh --host <ip> --with-apps            # 演示机/装机站：连不含模型的 app 包一起推
```

**默认跑完设备上不会有任何应用**——由用户在应用中心按需安装（那条路径下载的是含模型的完整包）。

### 装完确认 kit 真是 ctypes 版

```sh
md5sum /userdata/local/kit/runtime/engine.py /userdata/local/kit/runtime/ctypes_rknn.py
# 期望 930aaebe00ba9388840845c0a81a5a37 / 613edab9d26037edcf9b85694df212fc

# app 跑起来后：进程里只应映射 librknnrt.so，不应出现 rknn_runtime.cpython-*.so
grep -o '[^ /]*rknn[^ ]*' /proc/$(pgrep -f kit.run)/maps | sort -u
```

日志里出现 `ctypes backend unavailable` 说明退回了 rknnlite（泄漏版），需要排查。

## 固件层部署（遮罩固件，高危，单独跑）

> ⚠️ **换 rkipc 必须冷启动（整机 `reboot`）激活。热替换会触发 `cv181x_vpss` / CSIBDG FIFO 内核 oops。只在你能物理复位设备时才跑。**

**本版固件产物与 v1.6.0–v1.6.2 逐字节一致**，装过的设备不必重装。用法见
`release/v1.6.2/README.md` 同名小节（脚本行为未变，只有 `VER` 变）。

## 回滚

- **应用层**：每次跑 `deploy-app.sh` 都会在 `/userdata/_deploy/backups/` 留时间戳备份；kit/SDK 旧副本在 `/userdata/local/kit.bak.<ts>`、`/userdata/sdk/*.bak.<ts>`。
- **推理后端**：不必回滚整包，`ESK_RKNN_BACKEND=rknnlite` 即可退回旧实现。
- **版本回退**：`release/v1.6.2/`、`release/v1.6.1/`、`release/v1.6.0/` 在 CDN 上原样保留。
- **固件层**：`./deploy-firmware.sh --host <ip> --rollback`（恢复原厂 rkipc 后需冷启动）。

## 重新打包（可选，确定性）

```bash
# ext-api / ext-kit（rkipc/entry.cgi 从 v1.6.2 的 tar 里解出，逐字节同源）
tar xf release/v1.6.2/recamera-ext-api-v1.6.2.tar ./rkipc ./entry.cgi -C /tmp/fw
release/build-release.sh --rkipc /tmp/fw/rkipc --entry-cgi /tmp/fw/entry.cgi --version 1.6.3

# appmgr / apps（同输入→同 md5；frontend 本仓无构建目录，未改时沿用上一版产物）
python3 release/deploy/build-packages.py --frontend <web build/> --version 1.6.3 --out release/v1.6.3
```

> `release/pkg/rkipc` 的 md5 是 `9826e9ec…`，既不等于本包内的 `f683352a…`，也不等于
> `RELEASING.md` 里写的 `de5b3aa4…` —— 那个仓内副本已经漂了，**别拿它当"现役二进制"**。
> 从上一版的 `recamera-ext-api-*.tar` 里解，或从设备上拉。

## 校验（本轮产物 md5 / size）

本表是本版所有产物校验值的**唯一权威来源**。

### release/v1.6.3/

| 包 | size (bytes) | md5 | 与 v1.6.2 |
|----|-------------:|-----|-----------|
| `recamera-ext-api-v1.6.3.tar` | 18708480 | `2097bfe79d01863d55a6e8467bff0c83` | 不同（仅 MANIFEST 版本/日期） |
| `recamera-ext-kit-v1.6.3.tar.gz` | 2229214 | `45197cecb3f75035442f661532b3f229` | **不同**（ctypes 推理后端） |
| `appmgr-v1.6.3.tar.gz` | 65927 | `edc772159ad7643e3279e03942b1e9ac` | **相同** |
| `apps-v1.6.3.tar.gz` | 986788 | `3e6677572e044dc2d1fc25da7ffdda52` | **相同** |
| `frontend-v1.6.3.tar.gz` | 36757262 | `6a48749108747c6bed59a06fef55965e` | **相同** |
| `voice-runtime-1.0.0.tar.gz` | 18856604 | `ace48a688d41a3fc6b852a0f14ddad8d` | **相同** |
| `gst-hwcodec-1.0.0.tar.gz` | 425137 | `8e6d286fac58a5b366e8fdd1709b212f` | **相同** |
| `deploy-app.sh` | 21613 | `d43379b88d68fab45e3105316680ef9e` | 不同（版本号 + 边缘自检接受 403） |
| `deploy-firmware.sh` | 5469 | `bfbf0ce354913b37435b20d5c1cd3e25` | 不同（仅版本号） |
| `S94appmgr` | 8116 | `e49fcf81c715e827daeed10475f0a5b4` | **相同** |
| `ext_appmgr.conf` | 4849 | `c5e0131966b85bfce8e614afd0a55577` | **相同** |

`recamera-ext-api-v1.6.3.tar` 内附固件产物（与 v1.6.0–v1.6.2 相同）：

| 固件产物 | size (bytes) | md5 |
|----------|-------------:|-----|
| `rkipc` | 15585904 | `f683352a9d062a05a3df1f8df22d7d53` |
| `entry.cgi` | 1057168 | `75a693c87c317a49c37c4dddb6b9ac7a` |
| `sdk/lib/librecamera_ext.so.1.0.0` | 89496 | `5cebfb9e4d9c001c45b58c75daafe934` |

kit 包内本版改动的两个文件：

| 文件 | md5 |
|------|-----|
| `kit/runtime/engine.py` | `930aaebe00ba9388840845c0a81a5a37` |
| `kit/runtime/ctypes_rknn.py` | `613edab9d26037edcf9b85694df212fc` |

### packages/（应用商店，`catalog.json` 引用）

**本版未改动**：`catalog.json` 与 9 个应用包、两个运行时的签名和 sha256 与 v1.6.0 完全一致。校验值见 `release/v1.6.0/README.md`。

## 真机验证（2026-08-19，recamera-pro-test / RK3576）

1. 先把设备上的 kit 退回 v1.6.2 版本（`engine.py` = `fc83b3c5…`，删掉 `ctypes_rknn.py`），确认退得干净。
2. 跑 `./deploy-app.sh --host <ip> --skip-frontend`。
3. 部署后设备上 `engine.py` = `930aaebe…`、`ctypes_rknn.py` = `613edab9…`，与仓内工作树一致；旧的 `engine.py.bak-pre-ctypes-20260819` 随 `kit.bak.<ts>` 一起被换走。
4. 激活 `fall-detection`：`active_running=true`，NPU 34%，RSS 61 452 kB，进程 maps 里只有 `librknnrt.so`，**没有** `rknn_runtime.cpython-311-*.so` —— 走的是 ctypes 路径。
5. `rkipc` md5 部署前后不变，`dmesg` 无 vpss/CSIBDG/Oops。

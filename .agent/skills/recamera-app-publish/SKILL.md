---
name: recamera-app-publish
description: 把一个预构建好的 reCamera Pro App Center 包（manifest v2 的 <id>-<ver>-arm64.tar.gz，常见来源是其他工程师交付）上架、升级到 SenseCraft CDN 应用商店，或核对商店当前上架内容时使用。设备 appmgr 商店读的就是这里发布的 catalog.json。不用于构建包本身，也不用于共享模型、runtime bundle 或固件的发布。
---

# reCamera Pro 应用上架（CDN 商店）

## 发布到哪里

| 项 | 值 |
|---|---|
| 商店目录 | `https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/catalog.json`（设备 `market/appmgr/store_download.py` 与前端默认目录都是这个地址） |
| OSS ↔ CDN | `oss://sensecraft-statics/solution-app/recamera_pro` ↔ `https://sensecraft-statics.seeed.cc/solution-app/recamera_pro` |
| 布局 | `packages/<id>-<ver>-arm64.tar.gz`、`icons/<id>.<ext>`、`models/<app_id>/<file>`、`catalog.json` |
| 签名 | `.sig` 不上传，base64 签名写在 catalog 的 `package.signature` 里。商店安装要求可信签名（vendor 或 owner key），无 unsigned 例外 |

这个 bucket 与 `solution-assets` skill 的 `upload` 是同一个；App Center 包走本 skill，不要用通用上传绕过 catalog 流程。

工具：`market/packaging/publish_app.py`（本 skill 的执行入口），它调用 `sign.py`、`gen_catalog.build_catalog`、`appmgr/manifest.py` 校验器。

## 前置条件

- `ossutil` 在 `/usr/local/bin/ossutil`，用内置 `~/.ossutilconfig`，**不要**加 `-i/-k/-e`。
- vendor 私钥 `~/.recamera_release_key/release_priv.pem`（仅 `--sign` 需要）；公钥 `market/appmgr/keys/release_pub.pem`。
- 所在分支的 `gen_catalog.py` 支持 `icon_url`（`_staged_icon`）。在不带图标支持的分支上跑，已上架条目会丢 `icon_url`，脚本的 diff 检查会拒绝。
- `openssl`、Python 3.10+。

## 步骤

### 1. 预检 + 与包作者确认

```bash
tar tzvf <pkg>.tar.gz | head                        # 结构
tar xzOf <pkg>.tar.gz manifest.json | python3 -m json.tool | less
```

向包作者确认（写进上架记录）：
- 渠道：`release.channel`（stable/beta）、`release.sequence`，版本号是否最终（同 id+版本上架后不可改字节）。
- 商店展示字段：`author`、`scene`/`scene_zh`、`name_zh`、`description_zh`、图标。manifest 缺的字段 catalog 里就缺。
- 权限：`permissions.network.outbound` / `listen`、`permissions.sdk`、文件系统写范围是否符合预期。
- 资源：内存上限（`resources`）、是否与其他应用共用摄像头/NPU（`resources.claims`）。
- 是否依赖共享模型（`models.json`）或 runtime（`capabilities` 含 `audio`/`hwcodec`）。本脚本不上传共享模型，遇到会拒绝。

### 2. 签名

- 作者给了 `<pkg>.sig`：放在包旁边，脚本用 vendor 公钥验证。
- 没有：加 `--sign`，脚本只对 staging 里的**副本**签名，不改原文件、不改 `market/packaging/dist/`。
- 设备试装需要签名文件时，从 staging 目录取 `packages/<id>-<ver>-arm64.tar.gz` 和同名 `.sig`（先跑一次第 4 步 dry run 生成）。

### 3. 设备试装（上架前）

在测试设备上装签过名的包，确认能启动、健康检查通过、内存正常、与其他应用共存。`<host>` 为测试设备地址。

```bash
scp <staging>/packages/<id>-<ver>-arm64.tar.gz{,.sig} <user>@<host>:/userdata/appstage/
# 设备上以 root 执行（CLI 读取同名 .sig sidecar 验签）
cd /userdata/local && python3 -m appmgr install /userdata/appstage/<id>-<ver>-arm64.tar.gz
python3 -m appmgr start <id>
python3 -m appmgr list                  # running / health
free -m; top -b -n1 | head -20          # 内存
tail -n 100 /var/log/appmgr.log
```

- 以上命令需要 root。从交互会话里起的常驻进程要 `setsid nohup ... &`，否则会话结束进程被杀。
- appmgr 服务重启走 `/oem/usr/etc/init.d/S94appmgr restart`（手动起进程会缺 `APPMGR_RELEASE_PUBKEY`，签名校验失败）。
- 也可通过 Web 端「上传安装」验证同一个签过名的包。
- 试完按需 `python3 -m appmgr uninstall <id>`。

### 4. Dry run，审阅 diff

```bash
python3 market/packaging/publish_app.py <pkg>.tar.gz [--sign] [--icon icon.png] [--staging /tmp/pub-<id>]
```

脚本行为：
1. 拉线上 catalog，原始字节存为 `<staging>/catalog.prev.json`（回滚用）。
2. 只按线上 catalog 组装 staging：本地 `dist/` 副本 sha256 一致就用，否则从线上 URL 下载并校验；`.sig` 用 catalog 里的签名写出。`dist/` 里不在线上的包（如 `overlay-geometry-demo`）、旧版本、零散 `.sig` 不会进入 staging。
3. 不执行地检查新包：成员路径（绝对路径、`..`、链接、设备文件、setuid）、`MAX_PKG_BYTES`/`MAX_UNPACKED_BYTES`/`MAX_MEMBERS`、manifest v2、BOM + `release.lock.json`、图标声明与字节。
4. 同 id+版本已上架：字节一致 → 退出 0 不做事；字节不同 → 拒绝。线上有更低版本 → 升级，旧版本从 staging 移除；更低或相同核心版本号 → 拒绝。
5. 图标默认取包内 `manifest.icon.path`，`--icon` 可覆盖（png/webp/jpg，≤1 MiB，文件头须与类型一致），暂存为 `icons/<id>.<ext>`。
6. 用 `build_catalog` 重新生成 catalog；runtimes 原样沿用线上值。除目标 app 外每个条目必须与线上逐字段相等，否则打印差异并退出 1。
7. 打印 unchanged/added/upgraded 计数、新条目、上传清单（包 → 图标 → catalog.json）和回滚命令。

退出码：0 成功或已上架；1 拒绝/失败；3 已上传且 OSS 回读校验通过，但 CDN catalog 在重试窗口内未刷新。

### 5. 用户确认后上传

把第 4 步打印的上传清单（通常 3 个对象）贴给用户，得到明确确认后：

```bash
python3 market/packaging/publish_app.py <pkg>.tar.gz [--sign] [--icon ...] --yes
```

按 包 → 图标 → `catalog.json` 顺序 `ossutil cp -f`，每个对象回读比对 sha256，前面任一失败就不传 catalog。最后轮询 CDN catalog（最多 12 次 × 10 s）确认新条目可见。`--yes` 会重新组装 staging，签名结果与 dry run 不同（ECDSA 签名每次不同），以本次输出为准。

### 6. 验证

```bash
curl -s "https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/catalog.json?_=$(date +%s)" \
  | python3 -c 'import json,sys;[print(a["id"],a["version"]) for a in json.load(sys.stdin)["apps"]]'
```

然后在一台设备的应用商店里找到该应用并安装、启动一次。

## 回滚

脚本打印的回滚命令即重新上传上架前的 catalog：

```bash
ossutil cp -f <staging>/catalog.prev.json oss://sensecraft-statics/solution-app/recamera_pro/catalog.json
```

默认 staging 在 `/tmp/recamera-publish-<时间戳>-*`，重启会清空；需要长期保留时把 `catalog.prev.json` 复制到别处。已上传的包和图标不删除（旧 catalog 不引用它们）。

## 其他发布耦合

- 单独上架商店应用不涉及 recamera_pro 版本发布产物（`release/`），也不需要改装机侧配置。
- 批量重发全部应用、共享模型、runtime bundle 用 `market/packaging/publish_oss.sh`，不是本脚本。

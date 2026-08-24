# 发布维护指南（当前 legacy 流程已禁用）

面向**维护者**：如何重新打包并更新 `release/` 下的两个发布物。日常开发/使用不需要读本文，只在要出新版或改了随包内容（SDK / kit / examples）时用。

> **当前发布阻断**：`release/pkg` 的实文件、MANIFEST 和 install.sh 哈希不
> 一致，历史 factory/ext rkipc 集合还互相重叠；它也不含
> `inference-control@1`。`install.sh` 已主动 fail-closed。下文记录旧包形状，
> 不能按旧步骤重打或部署。下一发布必须先把各仓提交纳入 manifest，再从
> 源码构建 rkipc、entry.cgi、native SDK 和 Python 包，并生成统一 BOM。

## 两个包，各自用途

| 包 | 内容 | 给谁 | 装到哪 |
|---|---|---|---|
| `release/recamera-ext-api-v<ver>.tar` | **固件 sideload 包**：patched `rkipc` + `entry.cgi` + SDK + **`wheels/`（rknnlite 运行时）** + `install.sh`/`rollback.sh`/`MANIFEST.txt` | 给设备刷入扩展 API 固件的人 | 覆盖 `/oem`（持久，OTA 会还原）+ provision `/userdata/rknnenv`。设备端步骤见 `release/pkg/README.md` |
| `release/recamera-ext-kit-v<ver>.tar.gz` | **kit 分享包**：`kit/` + `sdk/`（含 `.so` 软链）+ `examples/` + **`wheels/`（rknnlite 运行时）** + `INSTALL.sh` + `SHARE-README.md`。**不含固件** | 给已刷好固件、要在设备上开发 app 的方案商 | `INSTALL.sh` 装到 `/userdata/local/kit` + `/userdata/sdk` + provision `/userdata/rknnenv` |

历史上两者由 `release/build-release.sh` **从传入的预编译二进制组装**；该脚本
不是源码 builder，也不能证明产物可复现。当前流程禁用，须由新的源码发布
流水线替代后才能产出下一 train。

## Python 推理运行时 (rknnlite) 随包 provision

两个包都带 `wheels/`（源在 `release/pkg/wheels/`，`git` tracked），`install.sh` / `INSTALL.sh` 在设备上按以下配方离线装好 Python 推理运行时，vision app 开箱能跑（设备无网）：

1. `ln -sf /oem/usr/lib/librknnrt.so /usr/lib/librknnrt.so`（stock `rknnlite` 硬编码此路径）；
2. `python3 -m venv --system-site-packages /userdata/rknnenv`（numpy 用系统的，不打 wheel）；
3. `/userdata/rknnenv/bin/pip install --no-index --find-links wheels/ rknn-toolkit-lite2 psutil ruamel.yaml ruamel.yaml.clib`；
4. 自检 `from rknnlite.api import RKNNLite; RKNNLite()`。

该段是 **best-effort**：失败仅告警，不阻塞主 `rkipc`/kit 安装。设备运行 vision app：

```sh
PYTHONPATH=/userdata/sdk/python \
LD_LIBRARY_PATH=/oem/usr/lib \
/userdata/rknnenv/bin/python3 /userdata/local/kit/kit/run.py /userdata/local/apps/<id>
```

`kit/run.py` 自己从所在位置推出共享 kit 的父目录，所以 `PYTHONPATH` 里不必再写
`/userdata/local`（仍需要 `/userdata/sdk/python` —— 那是扩展 SDK 的 `recamera_ext`，
与 kit 无关）。appmgr 拉起 app 走的是等价的 `-m kit.run` 形式。

旧流程曾通过替换 `release/pkg/wheels/` 后重打；当前禁止这样发布。新流水线须把
wheel 锁文件、来源、SHA-256、目标 ABI 与离线安装测试写入统一 BOM。

## 何时重打

- 改了随 kit 包分发的内容：`sdk/`、`kit/`、`examples/`、`release/kit-extra/{SHARE-README.md,INSTALL.sh}` → **至少重打 kit 包**。
- 换了 `rkipc` / `entry.cgi`（新固件构建产物）→ 重打固件包（并核对 md5）。
- 升版本号 → 两个都重打（文件名带版本）。

## rkipc / entry.cgi 的唯一允许来源

下一发布只允许使用 manifest 固定 commit 后，由同一次 CI/SDK 构建生成的
`recamera_ipc` 和 `recamera_web_backend` 产物。禁止从设备反向拉取二进制作为
发布源，也禁止复用 `release/pkg/rkipc`、`release/pkg/entry.cgi`；后两者只是
已知不一致的历史快照。

## 重打步骤

当前没有获准的 sideload 重打命令。恢复发布前必须先实现并通过：源码 commit
BOM、同构建 train 的 rkipc/entry.cgi/lib/header/Python 一致性、factory 与
extension 哈希集合不相交断言、两次构建字节一致、RV1126B kill/restart/OTA
矩阵。不要通过删除 `install.sh` guard 或更新单个 md5 绕过这些门禁。

旧 `build-release.sh` 曾执行下列组装动作（仅供迁移新流水线时参考）：
1. 计算 `rkipc`/`entry.cgi`/`.so` 的 md5 与 size；
2. **自动写回** `release/pkg/{install.sh,rollback.sh,MANIFEST.txt,README.md}` 的 md5 常量、size、版本、构建日期（消除手工同步漂移）；
3. 确定性组装两个包（成员排序、`mtime=0`、`gzip` 去时间戳）→ 同输入得同 md5；
4. 自检：`install.sh` 的常量与实际 artifact md5 一致、固件 tar 内成员 md5 正确，否则报错退出。

`--factory-md5`：设备原厂（未打补丁）`rkipc` 的 md5，供 `install.sh`/`rollback.sh` 校验回滚目标。不传则沿用现值 `d5e7ca93…`。

## 验证

```sh
# 脚本末尾已打印两个 tar 的 size + md5；再核对随包内容：
tar tzf release/recamera-ext-kit-v<ver>.tar.gz | grep -iE 'rkipc|entry.cgi|market|models|internal'   # 应为空（kit 包不含固件/权重）
tar tf  release/recamera-ext-api-v<ver>.tar                                                          # rkipc/entry.cgi/sdk/install.sh 齐全
tar tf  release/recamera-ext-api-v<ver>.tar | grep wheels                                            # 4 个 rknnlite wheel 在包内
tar tzf release/recamera-ext-kit-v<ver>.tar.gz | grep wheels                                         # kit 包同样带 wheels

# 新流水线完成后，应以 CI 生成的 staging 输入连续构建两次并比较 SHA-256；
# 不得把 release/pkg 内的历史二进制作为输入。
```

设备端端到端验证（刷固件包后）见 `docs/guide/deploy-ops.md` §5 自检清单。

## 提交

以下是旧 assembler 的历史提交形状，不能作为当前发布步骤：

```sh
git add release/pkg/{install.sh,rollback.sh,MANIFEST.txt,README.md}   # 若 md5/版本有变
git add release/recamera-ext-api-v<ver>.tar release/recamera-ext-kit-v<ver>.tar.gz
git commit -m "chore(release): 重打 v<ver> 两包"
```

> 注：`market/` 是 gitignored，其内容不进包也不进 commit。

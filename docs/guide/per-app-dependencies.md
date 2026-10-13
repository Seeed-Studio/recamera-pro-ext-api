# reCamera Pro 应用中心：Per-App 依赖分发设计

> 适用设备：reCamera Pro（RV1126B / recamera_v2）。
> 读者：应用中心的维护者，以及需要给自己的 app 带**独有 Python 依赖**（PyAV、可用的
> gstreamer-python、某个自训模型要的特定包…）的方案商。
> 状态：**阶段 A（随包打包 bundled wheels）已在 Manifest v2 正式实现并投入生产**；安装时由 appmgr 自动构建 per-release venv 并执行离线安全安装。阶段 B（跨包 catalog 共享 wheel）为规划项。
> 针对外部应用的完整离线 wheelhouse 解析与发布指南，详见：**[app-publishing-and-permissions-guide.md](./app-publishing-and-permissions-guide.md)**。
>
> 事实来源（现有代码）：
> - appmgr 后端：`market/appmgr/{server.py,supervisor.py,installer.py,manifest.py,paths.py}`
> - 打包/目录：`market/packaging/{build.py,publish_app.py}`、`market/catalog/{gen_catalog.py,models.json}`
> - CI 自动化发布：`.github/workflows/publish-app.yml`
> - 基础环境 provision：`market/deploy/provision-runtime.sh`
> - 上游总纲：`docs/guide/app-publishing-and-permissions-guide.md`、`docs/guide/app-center-publishing.md`、`docs/guide/voice-app.md`

---

## 0. 一句话

现在 app 包 = `manifest.json + app.py + models/`（`build.py:30` 的 `INCLUDE_TOP`），Python
依赖全靠**平台侧共享基础环境** `/userdata/rknnenv`（`provision-runtime.sh`）提供
（rknnlite / numpy / cv2）。本设计补上缺的一环：**让单个 app 能随包（或随 catalog）
分发自己独有的依赖，在安装时建一个 per-app venv 从离线 wheel 装入，不必把所有人的依赖
都塞进共享基础环境。**

分两阶段落地：

- **MVP（阶段 A）** — 依赖以 wheel 形式**打进 app 包**（`bundled`），安装时建 per-app
  venv 从包内 `wheels/` 离线安装。零新增网络路径，复用现有 `/upload` + `/install`，且
  wheel 已被包签名覆盖（安全性最强）。
- **进阶（阶段 B）** — 大 wheel 走 **catalog `deps[]` + 浏览器代取**（对齐现有
  `models[]` + `/putModel` 机制），避免每个包都塞几十 MB 的 `.whl`。

---

## 1. 需求与约束

### 1.1 硬约束（来自设备形态）

| 约束 | 依据 | 后果 |
|---|---|---|
| **设备离线** | `gen_catalog.py:6-11`（usb0 默认路由指回自己，设备无公网） | 设备**不能 `pip install`**、不能连 PyPI。依赖字节只能由**浏览器代取**后推给设备，或**打进包**里。 |
| **glibc 2.36 / RV1126B aarch64** | 设备环境为 glibc（profile: `rv1126b-linux-gnu-cp311-rknn232-v1`） | 编译型 wheel 须匹配 aarch64 且为 glibc 2.36 兼容标签（如 `cp311-manylinux_2_36_aarch64`）；纯 Python (`none-any`) wheel 通用。 |
| **app 以 root 运行** | `app-center-publishing.md` §1.1 | venv 创建、pip 解包都在 root 下，无权限门槛；反过来也意味着隔离要靠目录布局而非 uid。 |
| **状态须存活 OTA** | `paths.py:4`（一切在 `/userdata` 下） | per-app venv 必须落在 `/userdata`，不能进 `/oem`、`/usr`（OTA 会覆盖）。 |
| **包体 / 解包有上限** | `paths.py:57-59`（`MAX_PKG_BYTES=200MB`、`MAX_UNPACKED_BYTES=400MB`、`MAX_MEMBERS=4096`）；nginx `client_max_body_size 256m`（`ext_appmgr.conf:90`） | bundled wheel 直接吃这些配额；大依赖必须走阶段 B。 |

### 1.2 设计目标

1. **异构依赖**：app A 要 PyAV，app B 要能用的 gstreamer-python，app C 要某训练框架 —
   互不污染，也不逼共享基础环境背上所有人的包。
2. **隔离 vs 复用平衡**：rknnlite / numpy / cv2 这类**大而通用**的仍走共享 base env；只有
   **app 独有**的增量依赖走 per-app（见 §3 分层模型）。
3. **离线可复现**：安装只依赖已经落到设备上的字节（包内 wheel 或已 `/putDep` 的 wheel），
   `pip install --no-index`，绝不联网。
4. **与现有机制对齐**：复用 `interpreter` 字段（`supervisor.py:93` 已支持 per-app 解释器）、
   复用 `models[]` + `/putModel` 的"浏览器代取 + sha256 校验"范式（`gen_catalog.py:139`、
   `server.py:178`）、复用包签名信任链（`installer.py:90` `inspect()`）。

### 1.3 与现有三个机制的关系（务必分清）

| 机制 | 现状 | 解决的问题 | 本设计的关系 |
|---|---|---|---|
| **共享 base env** `/userdata/rknnenv` | 已有，`provision-runtime.sh` provision | 大而通用的运行时（rknnlite/numpy/cv2）+ recamera_ext | per-app venv **叠在它上面**（`--system-site-packages`），不重复装 |
| **`interpreter` 字段** | 已有，`supervisor.py:106` 读 `interpreter`/`python` | 每个 app 可指向不同 python | 本设计让 appmgr 在建了 per-app venv 后**把 interpreter 指向该 venv** |
| **`models[]` + `target_path` + `/putModel`** | 已有，`modelstore.py`、`server.py:178`、`gen_catalog.py:139` | 大的**共享模型**由浏览器代取写盘 | 阶段 B 的 `deps[]` **照抄这套范式**（新 `/putDep` 端点），只是落点是 wheel 目录而非模型目录 |

> 结论：本设计不是另起炉灶，而是把"**依赖**"补进已经为"**模型**"跑通的那条离线代取管线，
> 并复用已经为"**per-app 解释器**"留好的 `interpreter` 钩子。

---

## 2. 机制设计

### 2.1 manifest 如何声明依赖

在 manifest 顶层新增可选对象 `deps`（未实现）。缺省（无 `deps`）= 现状：app 直接用 base
env，行为完全不变。

```jsonc
{
  "id": "clip-recorder",
  "version": "0.1.0",
  "entry": "app.py",
  "interpreter": "/userdata/rknnenv/bin/python",   // 仍写 base env；appmgr 建 venv 后会改指向 per-app venv
  "deps": {
    "strategy": "bundled",          // "bundled"(阶段A) | "catalog"(阶段B)
    "base_env": "rknnenv",          // 叠在哪个共享 base 上（--system-site-packages 继承其 rknnlite/numpy/cv2）
    "python": ">=3.11",             // 可选：解释器版本护栏（不满足则安装期报错，不静默降级）
    "wheels": [                     // 期望装进 per-app venv 的 wheel 清单（顺序即安装顺序）
      { "file": "wheels/av-11.0.0-cp311-cp311-linux_aarch64.whl",
        "sha256": "…64hex…", "size": 18234567 },
      { "file": "wheels/pillow-10.3.0-cp311-cp311-linux_aarch64.whl",
        "sha256": "…", "size": 1123344 }
    ],
    "pip_args": ["--no-deps"]       // 可选：透传给 pip 的白名单参数（见 §4 安全，--no-index 由 appmgr 强制注入，作者不可覆盖）
  }
}
```

字段语义：

- `strategy=bundled`：`wheels[].file` 是**包内相对路径**（`wheels/…​.whl`），字节随 app 包一起
  到设备，sha256 用于安装期二次校验（包签名已覆盖，见 §4）。
- `strategy=catalog`：`wheels[].file` 只给**文件名**；真实 `url`+`sha256`+`size` 由
  `gen_catalog.py` 从暂存目录算出并写进 catalog 的 `deps[]`（§5.2），浏览器代取。
- `base_env`：决定 `python -m venv --system-site-packages` 的基础解释器。默认 `rknnenv`
  → `/userdata/rknnenv/bin/python`。留出未来多 base（如纯 CPU 音频 base）的空间。

> **OPEN QUESTION 1（wheel 的 ABI 标签）**：rknnenv 的 Python 具体是 `cp311` 且 musl 还是
> glibc？须在设备上核实 `python -V` 与 `pip debug --verbose` 的 compatible tags，据此规定
> 方案商必须提供的 wheel 标签。带 `.so` 的 wheel 标签不匹配会在 `pip install` 阶段直接
> 失败——这正是我们要在安装期显式报错而非静默的原因。

### 2.2 依赖如何随包 / 随 catalog 分发（离线）

**阶段 A（bundled，MVP）**

1. 方案商把预下好的 `.whl` 放进 `apps/<id>/wheels/`。
2. `build.py` 把 `wheels/` 纳入包（`INCLUDE_TOP` 增加 `"wheels"`，见 §5.1）。wheel 字节
   进 tar → 被包签名（`sign.py`）覆盖 → 走现有 `/upload`+`/install`，**无任何新网络路径**。
3. 设备侧 `installer.install()`（`installer.py:131`）照常解包，`wheels/` 落到
   `/userdata/local/apps/<id>/wheels/`。

**阶段 B（catalog，进阶）**

1. 方案商把 `.whl` 暂存到 `market/packaging/deps/<app_id>/<file>.whl`（对齐
   `models.json` 的 `<models-dir>/<app_id>/` 布局）。
2. `gen_catalog.py` 对每个 wheel 算 sha256+size，产出 catalog `deps[]` 条目
   `{url, filename, sha256, size, target_path}`（照抄 `_build_models`，`gen_catalog.py:139`）。
   `target_path` = 设备上的 wheel 暂存目录，如 `/userdata/appstage/deps/<app_id>`。
3. 浏览器安装环（`cloudInstall`）在 `/install` 之前，**逐个 wheel**：download →
   sha256 校验 → `POST /api/appMgr/putDep`（新端点，§5.3），字节落到 `target_path`。
4. `/install` 时，manifest 的 `deps.wheels[].file` 指向这些已落盘的 wheel 文件名，appmgr
   从该目录 `--find-links` 装入。

两阶段**只在"wheel 字节怎么到设备"上不同**；下面的 venv 建立与生命周期两阶段共用。

### 2.3 安装时如何建 per-app venv + 从离线 wheel 装

新增模块 `market/appmgr/deps.py`（未实现），在 `do_install()`（`server.py:195`）内、
`installer.install()` 返回后、`busy_gate()` 仍持有时调用 `deps.provision(app_id, manifest)`：

```text
def provision(app_id, manifest):
    d = manifest.get("deps")
    if not d: return None                     # 无依赖 → 现状路径，直接返回
    base_py  = _base_python(d["base_env"])    # /userdata/rknnenv/bin/python
    venv_dir = paths.app_venv(app_id)         # /userdata/local/venvs/<id>  (§2.4)
    wheeldir = _resolve_wheeldir(app_id, d)   # 包内 <app>/wheels 或 /userdata/appstage/deps/<id>

    # 1) 幂等重建：卸/装/升级都从干净 venv 起（避免半装状态）
    rmtree(venv_dir, ignore_errors=True)

    # 2) 叠在 base env 上：--system-site-packages 让 venv 直接看到 rknnlite/numpy/cv2
    run([base_py, "-m", "venv", "--system-site-packages", venv_dir])

    # 3) 离线安装：--no-index 由 appmgr 强制，绝不联网；只从本地 wheel 目录找
    verify_sha256(d["wheels"], wheeldir)      # 安装期再校验一次（bundled 尤其重要）
    run([f"{venv_dir}/bin/python", "-m", "pip", "install",
         "--no-index", f"--find-links={wheeldir}",
         *safe_pip_args(d.get("pip_args")),   # 白名单过滤，禁止 --index-url/--extra-index-url
         *[w["file_basename"] for w in d["wheels"]])

    # 4) 自检：venv 能 import 关键包吗？失败 → 抛错，install 整体失败（不留半成品）
    return venv_dir
```

关键点：

- **`--no-index` 强制注入**，作者的 `pip_args` 经白名单过滤（§4），从根上堵死联网/任意源。
- **`--system-site-packages`** 是"base + 增量"分层的技术支点：venv 里只多出 av/pillow，
  rknnlite（几十 MB）不复制。
- **失败即整体失败**：venv 建不出来或 import 自检不过，`do_install` 抛错回滚，UI 报"安装
  失败"，不会留下一个装了一半、启动就崩的 app。

> **OPEN QUESTION 2（venv/pip 是否可用）**：`python -m venv` 需要 `ensurepip`；musl 精简
> 镜像常把它裁掉。须在设备上核实 `/userdata/rknnenv/bin/python -m venv --help` 与
> `ensurepip`。若无：备选 (a) 随 appmgr 部署一个 `pip`/`setuptools`/`wheel` 的
> bootstrap wheel，`venv --without-pip` 后手动引导；备选 (b) **不建 venv，直接把 wheel
> 解压（wheel 即 zip）到一个 per-app `site-packages` 目录**，用 `PYTHONPATH` 注入
> （见 §2.5），省掉 pip 依赖——对纯 Python + 预编译 `.so` wheel 足够，代价是不做依赖解析。
> **建议 MVP 落 (b) 的变体**：技术风险最低、复用 `installer` 已有的解压能力。

### 2.4 interpreter 指向 per-app venv

`supervisor._resolve_interpreter()`（`supervisor.py:93-113`）现在只认 manifest 里写死的
`interpreter`。改为**优先探测 per-app venv**（未实现）：

```text
def _resolve_interpreter(manifest, app_id):
    venv_py = paths.app_venv_python(app_id)          # /userdata/local/venvs/<id>/bin/python
    if manifest.get("deps") and os.path.exists(venv_py):
        return venv_py                                # per-app venv 优先
    # …以下为现有逻辑（manifest.interpreter → sys.executable），完全不变
```

这样：有 `deps` 且 venv 建好 → 用 per-app venv；无 `deps` → 现状（base env 或系统 python），
**零回归**。venv 是 `--system-site-packages` 建的，`recamera_ext` 的 `.pth`
（`provision-runtime.sh` 写在 base env site-packages）仍可见，无需重复 provision。

> **OPEN QUESTION 3（`.pth` 的可见性）**：`--system-site-packages` 让 venv 看到 base 的
> `site-packages`，其中的 `recamera_sdk.pth`（指向 `/userdata/sdk/python`）是否会被 venv 的
> python 执行？venv 默认加载 base site-packages 的 `.pth`，理应生效；须真机核实
> `import recamera_ext` 在 per-app venv 下可用。若不生效，退路是在建 venv 后把同一行
> `.pth` 也写进 per-app venv 的 site-packages（复用 `provision-runtime.sh:73-82` 逻辑）。

### 2.5 备选：无 venv 的"解压式"per-app 依赖（MVP 建议）

若 OPEN QUESTION 2 结论是 ensurepip 不可用，采用更轻的方案（推荐做 MVP）：

- 安装期把每个 wheel（zip）解压到 `/userdata/local/venvs/<id>/site/`（纯目录，非 venv）。
- `supervisor.start()`（`supervisor.py:164-179` 组装 env 处）把该目录**前插进
  `PYTHONPATH`**，和现有注入 `KIT_PARENT`、`LD_LIBRARY_PATH` 同一处、同一手法。
- 解释器仍是 base env python；per-app 目录只提供"增量包"。

优点：不依赖 pip/venv、复用 `installer._vet_member`（`installer.py:52`）的解压安全检查、
生命周期就是"删目录"。缺点：不做依赖解析（作者须把传递依赖也列进 `wheels[]`）、不隔离
版本冲突（与 base 同名包会被 base 覆盖或反之，取决于 PYTHONPATH 次序）。对"给一个 app 加
PyAV"这种典型场景足够。**建议：MVP 走本方案；真正需要版本隔离的复杂 app 再上 §2.3 的
真 venv。**

### 2.6 卸载清理

现状：`installer.uninstall()`（`installer.py:180`）只 `rmtree` app 目录，且**没有 HTTP/CLI
卸载入口**（`server.py` 无 uninstall 路由，`__main__.py` CLI 无 uninstall 命令——现仅靠
覆盖安装）。本设计要求：

- per-app venv 落在 **app 目录之外**（`/userdata/local/venvs/<id>`），所以卸载必须**显式**
  连带删除它，否则孤儿 venv 会堆积占盘。
- 建议本设计一并补上 uninstall 入口（`server.py` 加 `do_uninstall`，先 `supervisor.stop`
  再 `installer.uninstall` 再 `deps.cleanup(app_id)` 删 venv），并在 CLI/HTTP 暴露。
- 覆盖安装（update）：§2.3 步骤 1 已"幂等重建 venv"，天然清掉旧依赖，无需额外处理。

> 备选布局：把 venv 放 **app 目录内**（`<app>/venv`），则 `installer` 的目录整体 swap
> （`installer.py:163-173`）自动带走旧 venv，卸载也自动清。代价：每次 update 都重建 venv、
> 且 venv 里的 `.so` 会撑大 `MAX_UNPACKED_BYTES` 校验（因为 venv 在安装后才建，不占包配额，
> 实际影响可控）。**取舍留作落地决定（OPEN QUESTION 4）**，两种都自洽，推荐**目录外 +
> 显式 cleanup**（更省 update 开销）。

---

## 3. 与基础环境的分层（何时 base、何时 per-app）

推荐**"共享 base env + per-app 增量"混合模型**：

| 依赖类型 | 归属 | 例子 | 理由 |
|---|---|---|---|
| 大而通用、几乎人人要 | **共享 base env**（`provision-runtime.sh` provision 进 `/userdata/rknnenv`） | rknnlite、numpy、cv2、recamera_ext | 装一次全设备复用，省盘、省安装时间；升级由平台统一管 |
| app 独有、别人用不到 | **per-app venv/site**（本设计） | PyAV、可用的 gstreamer-python、某训练框架的 runtime 包 | 不污染 base、不逼所有 app 背这份重量；随 app 装/卸 |
| 介于两者、多个 app 共用但非全体 | 先归 per-app；若 3+ app 都要，再**提升进 base**（改 `provision-runtime.sh`） | 例如若多个音频 app 都要 PyAV | 避免过早把小众包塞进 base；用"提升"作为演进阀门 |

判定口诀：**"通用度 × 体积"高 → base；"独有性"高 → per-app。** base 由平台在
`provision-runtime.sh` 里显式 provision 并做 self-test（`provision-runtime.sh:94-106`），
per-app 由安装流程按 manifest `deps` 自动建。两者通过 `--system-site-packages`（或
PYTHONPATH 次序）**叠加**，app 代码里 `import av` 和 `import rknnlite` 一样自然。

---

## 4. 安全

依赖分发是"往设备投递可执行代码"的又一面，威胁模型与包安装一致（`installer.py:1-18`
把每个包视为敌意输入），逐项对齐：

1. **wheel 来源校验（sha256）**
   - bundled：wheel 在 tar 内，**已被包的 ECDSA-P256 签名覆盖**（`installer.inspect()`
     先验签，`installer.py:90-101`；策略 `paths.REQUIRE_SIGNATURE` 默认拒绝无签名包）。
     这是最强保证——改一个 wheel 字节 = 破坏整包签名。安装期再按 manifest `sha256`
     二次校验（§2.3 step 3），双保险。
   - catalog：浏览器下载后先 sha256 校验（照抄 `cloudInstall` 对 package 的做法，
     `index.html:1171-1181`），`/putDep` 端点再服务端复算 sha256（照抄
     `modelstore.write_model` 的 `sha256_expected` 逻辑，`modelstore.py:146-157`，不匹配
     即删文件）。**建议**：catalog `deps[]` 也纳入 catalog 级签名（若后续给整个 catalog
     签名），使 catalog 路径达到与 bundled 同级的信任。**OPEN QUESTION 5**。
2. **venv 隔离**：per-app venv/site 目录独立、随 app 生命周期删除，不写入 base env（base
   只读复用）。`--no-index` 强制离线，`pip_args` 经**白名单**过滤——显式禁止
   `--index-url`/`--extra-index-url`/`--find-links`（find-links 由 appmgr 自己给）等能引入
   外部源的参数；只放行 `--no-deps`、`--only-binary=:all:` 之类无害项。
3. **落盘路径安全**：`/putDep` 复用 `modelstore.py` 的全套硬化——目标根白名单
   （`MODEL_ROOTS` 换成 `DEPS_ROOTS`，如 `/userdata/appstage/deps`）、basename 校验、
   `..`/symlink 逃逸双重检查（词法 + realpath）、原子写（`modelstore.py:77-158`）。文件名
   须 `.whl` 后缀白名单（照 `_UPLOAD_NAME_RE`，`server.py:129`）。
4. **体积 / 资源上限**：
   - bundled wheel 直接受 `MAX_PKG_BYTES`/`MAX_UNPACKED_BYTES`/`MAX_MEMBERS`
     （`paths.py:57-59`）约束——大依赖被迫走阶段 B（正是我们想要的分流）。
   - catalog wheel 走 `/putDep`，新增 `MAX_DEP_BYTES`（单文件）与 per-app venv **总量上限**
     `MAX_APP_VENV_BYTES`（安装后统计，超限则回滚），防止一个 app 撑爆 `/userdata`。
   - venv 构建设**超时**（`subprocess` timeout），pip 卡死不拖垮 appmgr（`busy_gate`
     期间不能久占，`server.py:66-81`）。

---

## 5. 落地增量（改哪儿，file:line）

> 分阶段。MVP = 阶段 A（bundled）；进阶 = 阶段 B（catalog 代取）。每条给现有锚点。

### 5.1 打包侧 `market/packaging/build.py`

- **`INCLUDE_TOP`（`build.py:30`）** 增加 `"wheels"`：让 `apps/<id>/wheels/*.whl` 进包。
  `_members()`（`build.py:39`）的 walk 已会递归收集、排除 `__pycache__`/隐藏文件，wheel
  天然被纳入且保持确定性打包（`_reset`，`build.py:92-99`）。
- 可选：`build()`（`build.py:59`）读 manifest `deps.strategy==bundled` 时，**校验每个
  `wheels[].file` 存在且 sha256 匹配**，不匹配就 `sys.exit`（把校验左移到打包期）。

### 5.2 目录侧 `market/catalog/`（阶段 B）

- **`gen_catalog.py`**：仿照 `_build_models`（`gen_catalog.py:139-166`）新增
  `_build_deps(app_id, spec, deps_dir, deps_base)`，产出 `deps[]` 条目
  `{url, filename, sha256, size, target_path}`；在 `build_catalog` 的 app 字典里
  （`gen_catalog.py:218-228`，`models` 那几行旁边）加 `"deps": _build_deps(...)`。
- 新增 `deps.json`（仿 `models.json`）：`app_id -> {target_path, files[]}`，默认
  `target_path=/userdata/appstage/deps/<app_id>`。暂存目录默认
  `market/packaging/deps/<app_id>/`（仿 `DEFAULT_MODELS_DIR`，`gen_catalog.py:79`）。
- catalog schema 版本号从 1 升到 2（`gen_catalog.py:17` 文档 + `"schema"` 常量
  `gen_catalog.py:233`），`deps` 缺省为 `[]` 向后兼容。

### 5.3 appmgr 后端 `market/appmgr/`

- **新增 `deps.py`**：`provision(app_id, manifest)` / `cleanup(app_id)`（§2.3 / §2.5 / §2.6）。
- **`paths.py`**：加 `VENVS_DIR=/userdata/local/venvs`、`app_venv(id)`、`app_venv_python(id)`、
  `DEPS_ROOTS`、`MAX_DEP_BYTES`、`MAX_APP_VENV_BYTES`（仿 `paths.py:20-31,57-59`）。
- **`server.py`**：
  - `do_install`（`server.py:195`）在 `installer.install()` 后调用
    `deps.provision(app_id, manifest)`（仍在 `busy_gate` 内，`server.py:196`）。
  - 新增 `do_putdep()` + `do_POST` 路由 `/api/appMgr/putDep`（照抄 `/putModel` 分支，
    `server.py:459-469`，把 `modelstore` 换成 deps 落盘、`MAX_MODEL_BYTES` 换 `MAX_DEP_BYTES`）。
  - 新增 `do_uninstall()` + 路由（§2.6；`server.py` 现无卸载路由）。
- **`supervisor.py`**：`_resolve_interpreter`（`supervisor.py:93-113`）加 per-app venv 优先
  探测（§2.4）；若走 §2.5 解压式，则改在 `start()` 组装 env 处
  （`supervisor.py:164-179`）前插 per-app `site` 到 `PYTHONPATH`。
- **`installer.py`**：解压式方案可直接复用 `_vet_member`（`installer.py:52`）解压 wheel；
  真 venv 方案无需改 installer。

### 5.4 前端安装环（阶段 B）

> 现役前端已迁至官方 React `AppStore.js`；以下按早期 `market/spa/index.html`(LEGACY)描述,仅供代取循环的逻辑参考,落地时改在官方 React 侧对应实现。

- **现状缺口（本次核实）**：`cloudInstall`（`index.html:1156`）当前**只做**
  download → sha256 → `/upload` → `/install`（`index.html:1163-1192`），**根本没有遍历
  `app.models[]` 调 `/putModel` 的循环**——即 catalog 里已有的 `models[]`（`gen_catalog`
  已产出）在前端尚未被消费。阶段 B 的 `deps[]` 与 `models[]` 会共用同一个"代取循环"，
  所以要**一并补上**：
  - 在 `/upload` 之前（`index.html:1182` 那步前），插入
    `for (const m of app.models||[]) { download(m.url) → sha256 校验 → POST /putModel
    with X-Target-Path=m.target_path }`（模型），
    以及 `for (const w of app.deps||[]) { … POST /putDep with X-Target-Path=w.target_path }`
    （依赖）。sha256 校验可直接复用 `sha256Hex`（`index.html:1067`）与
    `downloadBuf`（`index.html:1165`）。
  - UI 进度条/状态文案复用现有 `stat/bar`（`index.html:1158-1169`）。

---

## 6. 端到端示例

### 6.1 方案 B（per-app 依赖）：一个需要 PyAV 的假想 app `clip-recorder`

需求：从 RTSP 抽帧存短视频，依赖 **PyAV（`av`）**——实测 rknnenv **没有** `av`（背景已确认
PyAV 无、Gst python 绑定坏），且别的 app 都用不到，是典型 per-app 依赖。

作者侧（打包，阶段 A / bundled）：

```text
apps/clip-recorder/
├── manifest.json          # deps.strategy=bundled, wheels[]={av-…-linux_aarch64.whl, sha256}
├── app.py                 # import av
└── wheels/
    └── av-11.0.0-cp311-cp311-linux_aarch64.whl   # 作者预下的 aarch64 musl wheel
```

`python3 market/packaging/build.py apps/clip-recorder` → wheel 进包（`build.py:30` 改后）→
`sign.py` 签名 → `gen_catalog.py` 产 catalog → 浏览器 `/upload`+`/install`。

设备侧（安装，appmgr 自动）：`do_install`（`server.py:195`）解包后 → `deps.provision`：
`python -m venv --system-site-packages /userdata/local/venvs/clip-recorder`（或 §2.5 解压
到 `.../venvs/clip-recorder/site`）→ `pip install --no-index --find-links
/userdata/local/apps/clip-recorder/wheels av==11.0.0` → 自检 `import av` OK。

启动：`switch clip-recorder` → `_resolve_interpreter` 返回
`/userdata/local/venvs/clip-recorder/bin/python`（§2.4）→ app 里 `import av` 成功，
`import rknnlite`（来自 base，`--system-site-packages`）也成功。

> 若依赖太大（PyAV 静态带 ffmpeg 可达数十 MB，逼近 `MAX_PKG_BYTES`），改 `deps.strategy=catalog`：
> wheel 暂存 `market/packaging/deps/clip-recorder/`，`gen_catalog` 产 `deps[]`，浏览器
> `/putDep` 代取到 `/userdata/appstage/deps/clip-recorder`，`provision` 从那里 `--find-links`。

同理：需要**可用的 gstreamer-python** 的 app（rknnenv 里 `gi` 在但 Gst 绑定坏）——用
per-app 装一份能跑的 `PyGObject`+Gst 绑定 wheel，不去动坏的 base，风险隔离在 app 内。

### 6.2 方案 A（base env + 模型 `models[]`）对照：`voice-transcribe`

`voice-transcribe`（`apps/voice-transcribe/manifest.json`）走的是**另一条**路，正好作对照：

- 它的**依赖**（sherpa-onnx 等）由**共享 base env** `/userdata/rknnenv` 提供，manifest
  `interpreter=/userdata/rknnenv/bin/python`（该文件 `interpreter` 字段），**无 `deps`**。
- 它的**模型**（SenseVoice rknn、VAD、bpe 等，`models.json` 的 `voice-transcribe` 条目）是
  **大共享资产**，走 `models[]` + `target_path=/userdata/local/models/asr` + `/putModel`，
  由浏览器代取写盘（`gen_catalog.py:139`、`server.py:178`）。

对照结论：

| | 依赖来源 | 大资产来源 |
|---|---|---|
| `voice-transcribe`（方案 A） | 共享 base env（无 per-app deps） | 共享模型 `models[]` + `/putModel` |
| `clip-recorder`（方案 B） | **per-app venv**（本设计 `deps[]`） | 无/包内自带 |

即：**通用依赖 → base env；共享大模型 → `models[]`；app 独有依赖 → per-app `deps[]`**。
三者正交，同一个 app 可同时用（例如一个既要 per-app PyAV、又要共享 ASR 模型的 app，
`deps[]` 与 `models[]` 并存）。

---

## 7. OPEN QUESTIONS 汇总（落地前须核实）

1. **wheel ABI 标签**：rknnenv Python 的 `python -V` + `pip debug` compatible tags
   （cp3x / musl vs glibc / aarch64）→ 规定方案商必须提供的 wheel 标签。
2. **venv/ensurepip 可用性**：设备上 `python -m venv` 与 `ensurepip` 是否在？决定走 §2.3
   真 venv 还是 §2.5 解压式（建议 MVP 走解压式）。
3. **`recamera_ext` 在 per-app venv 下可见性**：`--system-site-packages` 是否让 base 的
   `recamera_sdk.pth`（`provision-runtime.sh:73`）在 venv python 下生效。
4. **venv 布局**（目录外 + 显式 cleanup vs 目录内随 swap 清理）——本设计推荐目录外。
5. **catalog `deps[]` 的签名**：catalog 路径是否纳入（未来的）catalog 级签名，以达到与
   bundled 同级信任。
6. **是否需要真正的依赖解析**：MVP 要求作者把传递依赖也列进 `wheels[]`（不解析）；若日后
   需要，再引入设备侧 pip 的 resolver（依赖 OPEN QUESTION 2 结论）。

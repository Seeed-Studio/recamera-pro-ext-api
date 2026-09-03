# reCamera Pro 应用中心：应用开发与上架指南

> 适用设备：reCamera Pro（RV1126B / recamera_v2）。
> 读者：想把自己的 AI 应用做成"能装进应用中心、能一键启停、能分发"形态的方案商。
> 本文只讲**应用中心特有的这条链路**：app 结构 → 打包 → 签名 → 上架 → 安装/管理。
> app 内部怎么拿帧、回注结果、驱动 GPIO —— 那是扩展 SDK 的能力，见
> [README.md](./README.md)，本文不重复。
>
> 说明：本文描述的应用中心打包 / 签名 / 分发工具链位于 `market/`，属**发布方私有流程，不随公开仓发布**；下列路径用于说明打包链路各环节，公开仓中只保留 `apps/*/manifest.json` 这类应用侧产物。
> `market/` 现已纳入 **git 版本追踪**（源码、`catalog/*.json`、发布公钥 `keys/release_pub.pem` 入库；构建产物 `packaging/dist/`、大模型 `packaging/models/`、私钥/`*.pem` 私钥、`__pycache__` 由 `.gitignore` 忽略）。
> 打包链路各环节（发布方私有）：
> - appmgr 后端：`market/appmgr/*.py`（含共享模型写盘原语 `market/appmgr/modelstore.py`）
> - 打包/签名：`market/packaging/{build.py,sign.py,keygen.sh,SIGNING.md}`
> - CDN 发布：`market/packaging/publish_oss.sh`（ossutil → SenseCraft CDN）
> - 目录/签名策略：`market/appmgr/paths.py`
> - 目录格式：`market/catalog/{catalog.json,catalog.local.json,gen_catalog.py,models.json}`（`catalog.json`=CDN 版，`catalog.local.json`=设备本地版 base `/appcenter/apps/`）
> - 真实 manifest：`apps/*/manifest.json`（公开仓内）
> - 部署脚本：`market/deploy/{S94appmgr,ext_appmgr.conf}`

---

## 1. 应用中心是什么

应用中心（App Center）由一个设备侧进程 **appmgr** 支撑。appmgr 干两件事：

1. **编排**：安装、卸载、单活切换、启停、读写配置。核心逻辑是 `market/appmgr/server.py`
   里的一组普通函数，CLI（`python3 -m appmgr <cmd>`）和 HTTP API 共用同一份代码。
2. **最小 HTTP API**：监听 **loopback `127.0.0.1:8130`**
   （`paths.py:68-69`，`HTTP_HOST`/`HTTP_PORT`），由 nginx 边缘（`ext_appmgr.conf`）用官方 JWT
   会话把关。Web 应用中心使用独立版本域 `/api/app-center/v1/`；历史工具仍可使用
   `/api/appMgr/`。不得把 Web 应用中心重新映射到 `/api/v1/`，该路径属于固件现有的
   SenseCraft/cloud proxy。

### app 的运行形态

- **独立进程**。appmgr 就是进程监督者本身（不走 `/etc/init.d`）。
  `supervisor.start()`（`supervisor.py:146`）用 `subprocess.Popen(..., start_new_session=True)`
  以新的会话/进程组拉起 app，把 pid 写到 `<app>/run.pid`，stdout/stderr 重定向到
  `<app>/logs/app.log`。
- **以 root 运行**。appmgr 自身经 `S94appmgr` 以 root 启动，其拉起的 app 继承 root。
  原因见规格 §1.1：摄像头/麦克风/`/dev/mpi/*` 设备节点均 root 属主，非 root 开不了硬件。
- **用扩展 SDK 对接固件**：拿帧（帧代理）、回注结果（公开结果注入 → OSD/推送，
  不触发录像）、GPIO、音频，全部走扩展 SDK 的 unix domain socket，见
  [README.md](./README.md)。托管应用要触发录像，在 manifest v2 声明
  [`record_trigger`](./app-package-v2.md#managed-recording-triggers)，由 appmgr
  代理，应用本身不能连接 recording-only 内部入口。
- **单活模型**：同一时刻只有一个 app 在跑。`do_switch()`（`server.py:243`）先停掉当前
  active（和目标本身，保证干净重启），再启动目标。这与"摄像头独占"约束一致。

### 和"裸跑扩展"的区别

裸跑扩展 = 你手动把自己的进程 scp 到设备、手动起停、自己管生命周期。
应用中心 = **把同一个进程包成可发现（catalog）、可安装（签名校验 + 安全解包）、
可一键启停切换（单活监督）、可配置（config_schema）、可接 HA/MQTT 的打包形态**。
底层调用的扩展 API 完全一样；应用中心只是在外面加了一层"分发 + 生命周期"骨架。

---

## 2. 一个 app 的结构

### 目录布局（开发时）

```
apps/<id>/
├── manifest.json     # 必需：app 的全部声明（见下）
├── app.py            # 必需：入口，通常继承 kit.app.App
├── icon.png          # 推荐：随安装包分发的应用缩略图（由 manifest.icon 声明）
├── models/           # 可选：随包分发的模型文件（*.rknn 等）
│   └── <model>.rknn
├── hooks/            # 可选：安装/启停钩子（会被打进包）
└── run               # 可选：自定义启动脚本（会被打进包，装后置 0755）
```

打包器收集 app 目录的完整文件树，再排除 `__pycache__`、隐藏文件、`.pyc`、`kit/`、
构建产物以及 `package.exclude` 声明的路径。因此 `icon.png`、同级 Python 模块、模板和数据文件
都会自然进入包，不需要维护固定白名单。
**共享的 `kit` 运行时不随 app 分发**，它单独部署到设备一份（见 §3）。

### 安装后（设备上）

装到 `/userdata/local/apps/<id>/`（`paths.py:20`），运行期还会多出：
`run.pid`、`logs/app.log`。

用户配置覆盖层 `config.json` **不在安装目录内**，它落在
`/userdata/local/appdata/<id>/config.json`（`paths.APPDATA_DIR`）。升级会整目录替换
`/userdata/local/apps/<id>/`，配置放在里面会被一并删掉，所以与包生命周期解耦：
安装、升级、卸载都不动这棵树。设备上遗留在旧位置的 `config.json` 会在首次
读/写/安装时自动搬过去，旧文件改名为 `config.json.migrated` 留痕
（`market/appmgr/config.py: migrate_legacy_config`）。

升级时上一版安装目录保留为 `/userdata/local/apps/<id>.prev`（只留一代，下次升级覆盖），
可手工回滚；带点的名字不是合法 app id，因此不会出现在 `/api/appMgr/list`。

### manifest.json 逐字段说明

下表字段以真实 manifest 为准（核实自 `apps/yolo-detector`、`apps/ppocr-reader`、
`apps/face-analysis`、`apps/voice-transcribe`、`apps/qrcode-reader`、`apps/fall-detection`）。

| 字段 | 必需 | 谁在用 | 说明 |
|---|---|---|---|
| `id` | ✅ | 全链路 | app 唯一标识，正则 `[a-z0-9-]{1,64}`（`paths.py:67`）。安装时必须与请求 id 一致，也是安装目录名。 |
| `name` | ✅ | list/UI | 展示名。 |
| `name_zh` | | UI | 中文名（可选，部分 app 有）。 |
| `version` | ✅ | 打包/state | 版本号。`build.py` 用它拼包名 `<id>-<ver>-arm64.tar.gz`；无则打包报错。 |
| `image` | | catalog/UI | 目录页兼容缩略图 URL，如 `/appcenter/apps/<id>.png`；它不替代安装包内的图标资产。 |
| `icon` | | validator/installer/list | manifest v2 的安装后图标声明：`{"path":"icon.png","media_type":"image/png"}`。声明后文件必须在包内且进入 BOM；设备从已安装内容生成 `icon_url`。 |
| `type` | | list/UI | 现有样本全为 `"self-hosted"`。 |
| `scene` / `scene_zh` | | UI | 场景分类，如 `"Retail & Audience"`。 |
| `description` / `description_zh` | | list/UI/catalog | 描述文案。`gen_catalog.py` 从包内 manifest 取 `description` 写进目录。 |
| `author` | | list/UI | 作者，样本为 `"Seeed reCamera Pro"`。 |
| `entry` | | supervisor | 入口文件（相对 app 目录），默认 `"app.py"`。禁止绝对路径或含 `..`。supervisor 把它拼成绝对路径交给 kit 入口：`<interp> -m kit.run <app_dir>/<entry>`（`supervisor._build_cmd`）。 |
| `kit` | | 声明式 | 依赖的 kit 版本约束串，如 `">=0.1.0"`（当前仅声明，appmgr 未强校验）。 |
| `interpreter`（别名 `python`） | | supervisor | 可选：指定解释器绝对路径，如 voice-transcribe 的 `/userdata/rknnenv/bin/python`。缺省用 appmgr 自己的 `sys.executable`。必须是设备上存在的绝对路径，否则 switch 时硬报错（`supervisor.py:93-113`）。 |
| `capabilities` | | 声明式 | 能力声明数组，如 `["audio"]`。 |
| `needs_model` | | 声明式 | 是否需要模型，如 voice-transcribe 为 `false`。 |
| `models[]` | | supervisor/kit | 模型列表。**supervisor 只用 `models[0].file` 作为 `--model` 传入**（`supervisor._build_cmd`）；`models[]` 为空则不传 `--model`（CPU-only app，如 qrcode-reader）。每个元素常见键：`id`、`file`（相对路径，如 `models/x.rknn`）、`task`（detect/pose/classify/recognize…）、`input`（NHWC 形状）、`quant`（int8/fp16）、`classes`/`keypoints`/`heads`、`norm`、`output`、`role`、`dict`。除 `file` 外均由 kit 运行时/前端消费，appmgr 不解析。 |
| `default_model` | | kit | 默认模型 id（多模型级联时）。 |
| `postproc` | | kit | 后处理器名，如 `detect`/`pose`/`db_ocr`/`voice`。 |
| `tags[]` | | UI | 标签数组。 |
| `output{}` | | supervisor/kit | 输出通道。`sink`（现有全为 `"ws"`）、`port`（如 `8124`）、`schema`（事件结构文字说明）、`topic`（MQTT 主题）。**supervisor 仅当 `sink=="ws"` 且有 `port` 时追加 `--sink ws --port <port>`**（`supervisor._build_cmd`）。 |
| `config_schema` | | config API | 可配置项 schema，**必须用分组写法** `groups[].items[]`（每个 item 带 `key`）。扁平写法 `{key: spec}` 已弃用，仅为老包保留兼容分支并打印弃用日志（`kit/config.py:_flat_to_grouped`、`market/appmgr/config.py:_flat_to_grouped`）。正式控件类型：`number`（有限数，服务端校验 min/max；step 必须为正并作为 UI 调节粒度）、`integer`（整数语义，绑定后为 `int`）、`boolean`、`enum`/`select`（typed scalar options）、`string`/`password`、`array`/`object`、`zone`、`line`、`field_mapping`、`output_filters`。UI 据此渲染表单，appmgr 据此校验写入；复杂类型必须提交 JSON 数组/对象，不能用 opaque string 代替。为兼容已有 manifest（例如 default=0.75、step=0.1），step 不被当作服务端量化网格。 |
| `ha_entities[]` | | MQTT/HA | Home Assistant 实体声明（component/object_id/name/value_template/device_class…），app 开启 MQTT 后据此上报。 |
| `privacy_blur` | | app 逻辑 | 隐私开关声明（face-analysis 用）。 |

> **谁真正读 manifest**：appmgr 只读少数字段 ——
> `server.do_list()` 读 `name/version/type/image/description/scene/author`；
> `supervisor` 读 `entry / models[0].file / output.sink+port / interpreter`；
> `config` 读 `config_schema`。**其余字段是给 kit 运行时和前端 SPA 用的声明**，
> appmgr 原样透传、不校验。写清这点是为了让方案商知道哪些字段"填错会装不上/起不来"
> （前四类），哪些"填错只影响 UI/运行逻辑"（其余）。

下拉参数有两种向后兼容写法。简单写法继续用 `options` 加平行标签；需要把中英文标签与值放在一起时，可以使用 labelled option object：

```jsonc
{
  "key": "backend", "type": "select", "apply": "restart", "default": 1,
  "options": [
    {"value": 1, "label": "Fast", "label_zh": "快速"},
    {"value": 2, "label": "Accurate", "label_zh": "精确"}
  ]
}
```

`value` 只允许非 null 的 string/number/integer/boolean 标量，并按 JSON wire 语义比较：数值 `1` 与 `1.0` 是同一个 option（不得重复声明），数值 `2` 与字符串 `"2"` 则严格不同。原有 `options:["cpu","rk"]` 配合 `option_labels` / `option_labels_zh` 仍完全兼容。本轮 `string` 只声明并校验字符串类型，尚未承诺 `minLength` / `maxLength` / `pattern`。

### 目录缩略图与安装后图标

`image` 与 `icon` 服务于两个不同阶段：catalog 在应用安装前使用 `image` URL；安装完成后，
设备以包内 `icon.path` 为权威内容，并通过应用列表的 `icon_url` 提供图标。这样第三方应用不需要
修改或重新构建设备前端，也能显示自己的图标。若任一阶段没有可用图片，UI 才回退到内置映射或
文字占位。为兼容旧 catalog，示例应用同时保留两项声明。

图标声明示例：

```json
"image": "/appcenter/apps/my-app.png",
"icon": {"path": "icon.png", "media_type": "image/png"}
```

`icon.path` 必须是规范的包内相对路径并指向普通文件，最大 1 MiB。只允许 PNG、WebP、JPEG，
扩展名、`media_type` 与文件 magic 必须一致：`.png`=`image/png`、`.webp`=`image/webp`、
`.jpg`/`.jpeg`=`image/jpeg`。SVG/HTML/JavaScript 等主动内容不会作为图标安装或同源提供。
新增或替换图标会改变 v2 BOM 与 `release_id`，至少必须递增 `release.sequence`；正式发布的
示例建议同时递增语义化 `version`。同版本但更高 sequence 合法，安装后的 `icon_url` 会用
图标内容摘要隔离缓存；不能用同一 sequence 发布不同内容。

### 最小可用 manifest 模板

```json
{
  "id": "my-app",
  "name": "My App",
  "version": "0.1.0",
  "type": "self-hosted",
  "author": "Your Company",
  "image": "/appcenter/apps/my-app.png",
  "icon": {"path": "icon.png", "media_type": "image/png"},
  "entry": "app.py",
  "kit": ">=0.1.0",
  "models": [
    { "id": "my_model", "file": "models/my_model.rknn", "task": "detect" }
  ],
  "output": { "sink": "ws", "port": 8124 },
  "config_schema": {}
}
```

CPU-only（无模型）的最小形态把 `models` 写成 `[]` 即可（参考 qrcode-reader）。

---

## 3. 开发

app 逻辑基于**扩展 SDK + 共享 kit 运行时**。SDK 的帧代理/结果注入/GPIO/音频 API 细节
见 [README.md](./README.md) 与 `sdk/`，本文不复述。这里只讲应用中心侧要点：

### app.py 的典型结构

现有样本都是"薄壳"：取帧 / letterbox / 模型加载 / 配置热更 / 输出扇出全在共享
`kit.app.App` 基类里，app 只写自己的 `run()` 循环和业务逻辑
（`owns_loop = True`，见 `internal/KIT_APP_SHAPE_SPEC.md`）。顶部**没有** sys.path
自举代码。核实自 `apps/yolo-detector/app.py`（全文 45 行）：

```python
from kit.app import App, run_app
from kit.runtime.postprocess.detect import postprocess
from kit import events as E


class YoloDetectorApp(App):
    id = "yolo-detector"
    name = "YOLO Detector"
    owns_loop = True
    model_frame = "hw-direct"

    def run(self):
        for frame in self.frames():
            x = self.pre(frame)
            outs = self.models.det.infer(x.data)
            dets = postprocess(outs, x.info,
                               conf_thres=self.conf, iou_thres=self.iou)
            self.emit([E.detection(d) for d in dets], frame.pts, results=dets)


if __name__ == "__main__":
    run_app(YoloDetectorApp())
```

端到端"取帧 → 推理 → 回注 OSD"的完整可运行样例见
[examples/03-frame-to-inference-to-osd](../../examples/03-frame-to-inference-to-osd/)。

### kit 运行时从哪来

kit 是**一份共享副本**，部署在 `/userdata/local/kit/kit/`（`paths.py:10`，`KIT_PARENT=/userdata/local/kit`）。
**kit 不打进 app 包**，app 包只有几百 KB～几十 MB（模型占大头）。

app.py 里**没有**任何 sys.path 自举代码，`import kit.app` 由启动方式保证：

- appmgr 起 app：`<interp> -m kit.run <app_dir>/<entry>`，同时注入 `KIT_PARENT` +
  `PYTHONPATH`（`supervisor.start()`）。
- 设备上手工跑：

  ```sh
  python3 -m kit.run /userdata/local/apps/<id> --sink stdout        # 需 PYTHONPATH 含 /userdata/local/kit
  python3 /userdata/local/kit/kit/run.py /userdata/local/apps/<id>  # 不需要任何 PYTHONPATH
  ```

  第二种形式里 `kit/run.py` 从自己所在位置推出 `KIT_PARENT`，所以什么环境变量都不用设。
- `python3 app.py`（app.py 末尾的 `if __name__ == "__main__": run_app(...)`）仍然可用，
  前提是 `kit` 已经在 `PYTHONPATH` 上。

`kit.run` 还会把 app 目录放上 `sys.path` 并 `chdir` 进去，所以 app 可以 `import` 自己
目录下的同级模块，`--model models/x.rknn` 这类相对路径也照旧。

> **运行时前提（rknnlite / interpreter）**：app 要真正跑起来，设备上需有 **`rknnlite`
> Python 绑定**（NPU 推理的 Python 层，**非固件自带**——固件里 rkipc 用的是 C 层 `librknnrt.so`）
> 及对应解释器/venv。默认用 appmgr 自己的 `sys.executable`；manifest 用 `interpreter`
> 指定 per-app 解释器（如 voice-transcribe 的 `/userdata/rknnenv/bin/python`，
> `supervisor.py:93-113`，缺失则 switch 硬报错）。这些依赖由**运行时侧 provision**
> （视觉基础环境 `market/deploy/provision-runtime.sh`），部署前提详见 [deploy-ops.md](./deploy-ops.md) §4.4。
>
> **音频运行时是例外,上架链路要管**:app 在 manifest 声明 `capabilities: ["audio"]`,
> `gen_catalog` 会把它连同 `runtimes.audio` 描述符写进 catalog,应用中心据此在安装时
> 按需补齐 `voice-runtime-<ver>.tar.gz`(约 18 MB)。**发布时忘了把这个包传上 CDN,
> catalog 里就不会有 `runtimes` 段**(生成器缺包时不写占位),语音类 app 装上跑不了。
>
> **依赖分层**：`rknnlite`/`numpy`/`cv2` 这类**大而通用**的依赖走**平台共享基础环境**
> `/userdata/rknnenv`（provision 一次，8 个视觉 app 复用）；大而共享的**模型**走 catalog
> `models[]` + `putModel`（见下节）。若某个 app 需要**自己独有**的 Python 依赖（PyAV、特定训练框架…），
> 不应塞进共享基础环境——见 [per-app-dependencies.md](./per-app-dependencies.md)（**设计文档，尚未实现**：
> 每 app 建独立 venv 从离线 wheel 装入）。

### 模型放哪、怎么被加载

- 开发时放 `apps/<id>/models/`，manifest `models[].file` 用相对路径 `models/xxx.rknn`。
- 打包时 `models/` 整个进包（`build.py:28`）。
- 运行时 supervisor 以 app 安装目录为 cwd，把 `models[0].file` 作为 `--model` 传给
  `app.py`（`supervisor._build_cmd`）。
- **kit 在 `App.start()` 里预加载 manifest 的全部 `models[]`**（相对路径按安装目录
  转绝对，逐个构建 NPU 模型并挂到 `self.models`）。app **不调用 `self.models.load(...)`**
  ——没有这个 API；多模型级联直接用 `self.models.<id>.infer(...)`（或单模型
  `self.models[0].infer(...)`）。`--model` 只覆盖**第一个** manifest 模型，供临时换
  模型调试。详见 [model-onboarding.md](./model-onboarding.md) §7 与
  `internal/KIT_APP_SHAPE_SPEC.md`。

### 两种模型分发形态：随包 bundle vs 共享 `models[]`+`target_path`

上面是**随包分发**（默认，8/9 个 app 走这条）：模型跟着 tar.gz 进包，装到 `apps/<id>/models/`。

但有的模型是**大而共享**的资产——多个 app 复用同一份、又不宜塞进每个包。为此有一条并行链路
（核实自 `market/appmgr/modelstore.py`、`market/catalog/models.json`、`gen_catalog.py:40-48,135-162`）：

- 这类 app 的 **manifest `models` 写成 `[]`**（不随包带模型，supervisor 因而不传 `--model`，
  app 自己去约定目录找模型）；
- 共享文件登记在 `market/catalog/models.json`：`app id → { target_path, files[] }`，
  文件 staged 在 `market/packaging/models/<app_id>/<filename>`；
- `gen_catalog.py` 把它们哈希后，作为 **catalog 顶层 `models[]` 条目**发出
  `{url, filename, sha256, size, target_path}`（**与 manifest 里那份 per-app 的 `models[]` 不是同一个东西**：
  manifest `models[]` 描述模型元数据供 kit/supervisor 用，catalog `models[]` 描述"装机前要下载并落到哪个目录"）；
- 这是 legacy catalog 的共享模型形态；`putModel` 现在只允许设备本机 loopback
  迁移工具调用，公网 nginx 返回 410。当前 Web/manifest v2 首版把所需模型作为
  package 的 authenticated bundled artifact 一起上传和安装（流程见 §6）。

**legacy 样本 voice-transcribe**：旧 catalog 曾以 `models: []`、
`needs_model: false` 配合 `target_path=/userdata/local/models/asr` 分发共享模型。
当前 manifest v2 仍以 `needs_model: false` 表示“不走 kit 的视频模型加载器”，但 RK ASR、
VAD、BPE 与 KWS 文件均在 `artifacts[]` 中声明并随签名包安装到
`apps/voice-transcribe/models/asr/`；per-release venv 也由 `python.wheels[]` 构建。
RK 后端通过 `inferenced` 时只授权这个安装路径。

---

## 4. 打包

用 `market/packaging/build.py` 把 app 目录打成分发包：

```sh
cd market/packaging
python3 build.py ../../apps/my-app                 # 输出到 packaging/dist/
python3 build.py ../../apps/my-app --out /tmp/out  # 指定输出目录
```

产物：`<id>-<version>-arm64.tar.gz`（包名由 manifest 的 `id`+`version` 拼出）。

**包结构**（tar.gz 顶层，核实自 `build.py` 与 `dist/` 样本）：

```
manifest.json      ← 必须在顶层（installer/gen_catalog 都 getmember("manifest.json")）
app.py             ← 示例入口；实际以 manifest.entry 声明的相对路径为准
icon.png           ← 若 manifest.icon 声明则必须存在，并进入 v2 BOM
models/…           ← 若声明了模型
hooks/ run         ← 若存在
```

要点（`build.py`）：

- 收集完整 app 文件树，自动剔除 `__pycache__`/隐藏文件/`.pyc`/`kit/`/构建产物，
  并应用可选的 `manifest.package.exclude`；声明的入口、图标和 bundled payload 不能被排除。
- **完全确定性（可复现）打包**：同样的输入字节 → 同样的输出字节 → 同样的 sha256，
  从根上杜绝"catalog 里的 checksum 和实际服务的包对不上"这个 bug。两处非确定性都被钉死
  （`build.py:82-109`）：
  1. **tar 成员元数据**：`uid=gid=0`、`uname/gname=""`、`mtime=0`、成员按 arcname 排序、
     mode 归一化（有执行位 → 0755，否则 0644，抹掉宿主 umask 噪声）。
  2. **gzip 外壳**：`tarfile.open("w:gz")` 会把**当前时间和输出文件名**写进 gzip 头，
     导致同一份 tar 每次 gzip 出的字节都不同。故改为**先在内存里打不压缩的 tar
     （`GNU_FORMAT`），再自己用 `gzip.GzipFile(filename="", mtime=0)` 压**——无 FNAME、无时间戳。
  （实测：对同一 app 连打两次，两份 tar.gz 的 sha256 完全一致。）
- 打完打印成员列表、字节数、**md5**。
- **kit 不入包**（`build.py:6-9`）。
- 包大小受设备侧限制约束：≤200 MB 压缩包、≤400 MB 解包、≤4096 个成员（`paths.py:63-65`）。

---

## 5. 签名

### 为什么要签

`installer.inspect()` 在解包**之前**先验签（`installer.py:90-104` → `signing.verify_package`）。
安装 app 等于"向设备投递 root 代码"，所以每个包都当作敌意输入：先验真伪，再做
zip-slip/tar-bomb 防护，最后才解包。

### 签名方案（核实自 `signing.py`、`SIGNING.md`、`keygen.sh`）

- **算法**：ECDSA over P-256（prime256v1），摘要 SHA-256。
- **签的是**：原始 `<pkg>.tar.gz` 字节的**分离签名**。
- **分发**：base64(DER)，两种载体 —— `<pkg>.tar.gz.sig` 边车文件，和 catalog 里的
  `package.signature`（+ `signature_alg`）。
- **验签工具**：设备用自带 `openssl dgst -sha256 -verify`（兼容设备的 OpenSSL 1.1.1 与构建机 3.x，
  故不用 Ed25519）。零 Python 加密依赖。

### 信任锚与密钥

| 密钥 | 位置 | 在仓库？ | 在设备？ |
|---|---|---|---|
| 私钥 `release_priv.pem` | `~/.recamera_release_key/`（chmod 600） | 否，永不 | 否，永不 |
| Vendor 公钥 `release_pub.pem` | `market/appmgr/keys/release_pub.pem` | 是（已提交） | 是（随固件部署到只读 `/usr/lib/recamera/appmgr/keys/`） |
| Owner 公钥 `*.pem` | 设备所有者配置 | 否 | `/userdata/local/appmgr/keys/owners/` |

Vendor 公钥始终是不可变信任锚；owner trust store 只能扩展、不能替换它。公钥与
owner 目录均禁止 symlink 和 group/world writable，且受公钥数量/大小上限约束。验签结果返回
`signer_kind`（`vendor`/`owner`）及规范 SPKI 的 SHA-256 `key_fingerprint`。应用包本身禁止携带
PEM/私钥容器或 trust-store 目录。设备所有者必须通过独立的
`POST /api/app-center/v1/trust/owners` 管理操作显式确认并导入 P-256 公钥；上传、preflight、
manifest 和安装流程都不会自动导入或信任包中提供的密钥。

### 怎么签一个包

```sh
cd market/packaging
./keygen.sh                 # 一次性生成密钥对（私钥留本地，公钥落仓库；拒绝覆盖已有私钥）
python3 build.py ../../apps/my-app
python3 sign.py             # 给 dist/*.tar.gz 逐个签，写出 <pkg>.tar.gz.sig
python3 sign.py --verify    # 可选：拿公钥回验
```

### 设备侧策略（`APPMGR_REQUIRE_SIGNATURE`，`paths.py:56-57`）

- 默认 **1（开）**：direct API、future cloud、legacy/global installer 的**无签名包被拒**。
  本地 Web v1 上传是唯一产品例外：请求必须经过 nginx JWT 鉴权并通过同源 `Origin` 门禁；nginx
  在鉴权成功后覆盖写入内部 route stamp，appmgr 据此生成
  `source=local-web` / `channel=app-center-v1-same-origin`。客户端 multipart/JSON 自报
  `source`/`channel` 无效，finalize 若携带这两个字段反而直接拒绝。现役 React 同时发送
  `Authorization: Bearer ...`；只要 post-auth stamp 与严格同源 Origin 成立，该头不会把请求降级为 direct。
- 本地 Web unsigned preflight 返回 `critical` 的 `unsigned-root-code` 警告，明确提示发布者身份
  未验证且应用代码以 root 运行；前端必须展示警告，并在 finalize 提交严格布尔值
  `unsigned_risk_confirmed:true`。这是唯一的 unsigned 风险确认字段（权限确认仍独立）；旧客户端
  多发 `developer_mode` 布尔字段可兼容，但它不参与准入。缺少风险确认即拒绝。安装完成后保持
  **stopped**，即使它升级的是正在运行的应用也不会自动重启；用户须另行显式 Start。
- **签名错误/伪造的包在所有通道永远拒绝**，不会降级成 unsigned。无 `Origin` 的 API/Bearer、
  未带服务端可信 route stamp 的直连以及未来 cloud/legacy 通道均不获得本地 Web 豁免。
- `APPMGR_REQUIRE_SIGNATURE=0` 只保留为受控迁移/历史 installer 兜底开关；本地 Web 正常交互
  无需也不应修改此全局配置。Web-native v1 的 direct/future-cloud 来源即使在该历史开关关闭时
  仍硬拒 unsigned；`GET /api/app-center/v1/policy` 因此始终报告 `signature.required:true`，并用
  `signature.local_web_unsigned` 单独描述唯一例外。
- 已安装的 app 不会被重新验签，翻这个开关不会弄死在跑的设备，只影响新安装。
- 有签名但设备上没有公钥 → **fail closed**（拒装，`signing.py:128-131`）。

### 信任链现状（诚实标注）

**机制完整，生态不完整。** 逐条核实：

- 签名/验签的机制是**完整可用**的：`keygen.sh`/`sign.py`/`signing.py` 全链路能跑，
  `market/appmgr/keys/release_pub.pem` 是一枚真实的 P-256 Vendor 公钥。
- 构建生成的 `market/packaging/dist/` 被 Git 忽略，源码检出不会自带九个发布包；测试目录中的
  包也可能由某个 Owner 测试密钥签名，不能假定出厂固件会信任。正式分发必须同时发布与包字节
  匹配的 `.tar.gz.sig`，并由设备已信任的 Vendor 或 Owner 私钥签署。
- 这不是 CA / 开发者证书体系：Seeed 发布包由 vendor 私钥签名；设备所有者可显式配置自己的
  P-256 公钥，但仓库仍未定义面向第三方方案商的证书签发/吊销流程。
- 因此，方案商要让包装进"出厂设备的应用中心"，当前有三条路：
  1. **由 Seeed 侧签发**（把包交给持私钥方签名）—— 需 Seeed 配合，流程未在本仓库定义；
  2. **自管设备群**：把自己的公钥安全放进持久化 owner trust store，用自己的私钥签；vendor
     公钥保持不变且继续受信；
  3. **设备本地开发/测试**：从已登录的同源 Web 应用中心上传，阅读 root 风险警告并二次确认；
     包只会被安装为 stopped。不要为了这个流程关闭全局强制签名。

> 结论：**签名基础设施已就绪，但"第三方开发者证书 / 上架签发"这一环是半成品，需 Seeed 侧配合才能形成
> 面向生态的信任链。** 规格 §1（第 18-20 行）也把"打包分发/签名"列为 P1/P2、不在当前固件范围。

---

## 6. 上架

### catalog.json 格式（schema v1，核实自 `catalog.json` + `gen_catalog.py`）

每个 app 条目除 `package` 外，还带一个 **`models[]`**（`gen_catalog.py:214-224`）：随包分发的
app 该数组为空 `[]`；走共享模型链路的 app（voice-transcribe）在此列出装机前要下载并落盘的文件。

```json
{
  "schema": 1,
  "generated": "2026-08-09T04:16:49Z",
  "source": "recamera_pro/market/packaging/dist",
  "apps": [
    {
      "id": "fall-detection",
      "name": "Fall Detection",
      "version": "0.1.0",
      "description": "…",
      "arch": "arm64",
      "package": {
        "url": "https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/packages/fall-detection-0.1.0-arm64.tar.gz",
        "filename": "fall-detection-0.1.0-arm64.tar.gz",
        "sha256": "7124718d…",
        "size": 2865713,
        "signature": "MEUCIC…",
        "signature_alg": "ecdsa-sha256"
      },
      "models": []
    },
    {
      "id": "voice-transcribe",
      "name": "Voice Transcribe",
      "version": "0.1.0",
      "description": "…",
      "arch": "arm64",
      "package": { "url": "…/packages/voice-transcribe-0.1.0-arm64.tar.gz", "…": "…" },
      "models": [
        {
          "url": "…/models/voice-transcribe/sensevoice_rv1126b_w4a16.rknn",
          "filename": "sensevoice_rv1126b_w4a16.rknn",
          "sha256": "3fa40ad9…",
          "size": 133468923,
          "target_path": "/userdata/local/models/asr"
        }
      ]
    }
  ]
}
```

> `url` 的前缀由 `--base-url` 决定，对应**两套 url**：
> - **CDN 版（生产主分发）**：CDN base，包 url 形如
>   `https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/packages/…`。仓库里的
>   `catalog.json` 即此形态，浏览器代取（设备无外网路由）。
> - **设备本地版（回退）**：`gen_catalog.py` 默认 base `/appcenter/apps/`（整合后布局，nginx
>   `alias /userdata/local/appcenter/apps/`），包 url 形如 `/appcenter/apps/<file>.tar.gz`；
>   catalog 本身在设备上 served at `/appcenter/catalog.json`（→ `/userdata/local/catalog/catalog.json`）。
>   仓库里另存一份 `catalog.local.json`（默认 base 产出），与 CDN 版 `catalog.json` 并存、勿互相覆盖。
>
> `models[]` 的 url 前缀默认由包 base 推导（CDN base 把结尾 `packages/`|`pkgs/` 换成 `models/`；
> 设备本地 base `/appcenter/apps/` 落到 `/appcenter/apps/models/<app_id>/…`，仍在 apps alias 下，
> `gen_catalog.py:112-122`），也可用 `--models-base-url` 显式覆盖。

### 怎么把 app 加进 catalog

```sh
python3 market/catalog/gen_catalog.py                       # 扫 ../packaging/dist，base /appcenter/apps/（设备本地版）
python3 market/catalog/gen_catalog.py --out catalog.local.json              # 设备本地版另存，勿覆盖 CDN 版 catalog.json
python3 market/catalog/gen_catalog.py --dist DIR --out FILE --base-url https://cdn.example/packages/   # CDN 版
python3 market/catalog/gen_catalog.py --models-dir DIR --models-base-url https://cdn.example/models/
```

`gen_catalog.py` 是目录的唯一真源：扫 `dist/*.tar.gz`，从**包内 manifest** 取
id/name/version/description，实算 sha256+size，读 `.sig` 边车嵌入 `signature`。
url/checksum 永不手写。缺 `.sig` 会打印 `WARN … UNSIGNED`（默认策略下这类包会被设备拒装）。
`--base-url` 生产环境指向 CDN/OSS。

**共享模型进 catalog**：`gen_catalog.py` 同时读 `market/catalog/models.json`
（`app id → {target_path, files[]}`），把 staged 在 `market/packaging/models/<app_id>/` 的
文件哈希后作为该 app 的 `models[]` 发出（`gen_catalog.py:125-162`）。
**staged 文件缺失是硬错误**（`SystemExit`）——宁可上架时炸掉，也不让 catalog 指向浏览器取不到的模型。
不在 `models.json` 里的 app，`models[]` 恒为 `[]`。

### 装到设备的流程

设备在常见 USB 组网下没有外网路由（`gen_catalog.py:5-10`），所以**由浏览器代下**。
以下两阶段流程只描述 **legacy loopback 迁移链路**，不再是 Web-native 前端行为；
公网 exact `/api/appMgr/putModel` 和 `/api/appMgr/upload` 均返回 410。当前 Web 前端
上传 manifest v2 package 到 `/api/app-center/v1/uploads`，模型/依赖作为经过 BOM
认证的 bundled artifacts 随包进入同一 preflight/install 事务：

```
[阶段 0：共享模型 models-first 循环]  —— 仅当 catalog 该 app 的 models[] 非空
for m in app.models:
    浏览器从 m.url 下载文件
       → 浏览器本地校验 m.sha256
       → POST /api/appMgr/putModel
            raw bytes + 头 X-Filename=m.filename, X-Target-Path=m.target_path, X-Sha256=m.sha256
            → modelstore 白名单校验 + 原子写 + sha256 复核，落到 <target_path>/<filename>

[阶段 1：安装 app 包]
浏览器从 catalog 的 package.url 下载 .tar.gz
   → 浏览器本地校验 sha256
   → POST /api/appMgr/upload （raw bytes + X-Filename 头）
        → appmgr 落盘到 /userdata/appstage/<filename>（server.do_upload）
   → POST /api/appMgr/install { "path": "/userdata/appstage/<filename>", "signature": "<base64>" }
        → installer 验签 + zip-slip 防护 + 原子解包到 /userdata/local/apps/<id>/
```

**`putModel` 端点契约**（`server.py:495-505` / `do_putmodel:180` / `modelstore.write_model:98`）：

- **入参**：raw model 字节为 body；三个头 —— `X-Filename`（裸 basename，正则
  `[A-Za-z0-9][A-Za-z0-9._-]{0,127}`，禁路径分隔符/`..`）、`X-Target-Path`（模型落盘目录的绝对路径）、
  可选 `X-Sha256`（期望摘要）。
- **返回**：`{path, filename, size, sha256}`。
- **安全护栏**（`modelstore.py` 逐条）：目标目录必须落在**根白名单** `MODEL_ROOTS`
  内（默认 `/userdata/local/models`，`APPMGR_MODEL_ROOTS` 可覆盖）——`/etc`、`/oem`、绝对逃逸、
  `..` 上爬、以及"符号链接组件在建目录后逃逸"全部拒绝（**词法预检 + realpath 后检双保险**）；
  拒绝覆盖目标处已存在的符号链接；大小 `1..256 MB`（`APPMGR_MAX_MODEL_BYTES`，与 nginx
  `client_max_body_size 256m` 对齐）；**原子写**（同目录 temp + fsync + `os.replace`）；
  给了 `X-Sha256` 就复核，**不符即删**（绝不在盘上留半截/被篡改的模型）。
- **边界**：仅 loopback 迁移工具可用；公网 nginx exact location 返回 410。
  单元测试见 `market/appmgr/tests/test_modelstore.py`。

设备本地也可直接用 CLI（`__main__.py`）：`python3 -m appmgr install <pkg.tar.gz>`
（包路径须在允许根 `/userdata` 下，`paths.py:60-62`）。

### 启停切换（单活）

- `POST /api/appMgr/switch {id}`：停当前 active（及目标）→ 启目标 → 置 active；
  启动失败会回滚 active 状态（`server.py:243-265`）。
- `POST /api/appMgr/stop {id?}`：停指定 id，或停当前 active。
- 开机自恢复：appmgr 启动时会重启上次的 active app（`server._boot_restore`）。

### 卸载（uninstall）

- `POST /api/appMgr/uninstall {id}`（`server.py:513-517` → `do_uninstall:209`）：卸载一个已装 app。
  时序（与 switch/stop 一样在 busy-gate 内串行）：**①在跑就先停**（干净拆进程组）→ **②若是当前
  active 则清 active 状态**（免得 boot-restore 再去拉一个已删的 app）→ **③`installer.uninstall()`
  删 `/userdata/local/apps/<id>/`，并删 per-app venv `/userdata/local/venvs/<id>`（若存在）**。
  返回 `{id, uninstalled:true, stopped, was_active}`。卸载不存在的 app 是硬 `ValueError`（400），
  停/清 active 均幂等，重复卸载安全。
- **共享模型不动**：`/userdata/local/models` 下的模型是**跨 app 共享**资产（one-gen
  `models[]`+`target_path`），`installer.uninstall()`（`installer.py:180`）按构造只碰 app 自己的目录
  和 venv，**永不删共享模型**——`paths.py` 也把 `VENVS_DIR`/`venv_dir()` 与 models 树刻意分开
  （`paths.py:23-28`）。要清共享模型得手动删。
- CLI 等价：`python3 -m appmgr uninstall <id>`（`__main__.py:37-40`）。

### 发布到 CDN（`publish_oss.sh`）

生产分发照 reCamera 一代的路子：包 + 模型 + catalog 传到 **SenseCraft CDN**，设备离线、
由用户浏览器代取（浏览器有外网路由，设备没有）。脚本 `market/packaging/publish_oss.sh`
（核实自该文件）：

- **OSS 桶 / CDN base**：`oss://sensecraft-statics/solution-app/recamera_pro/`
  ↔ `https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/`（`publish_oss.sh:24-25`）。
- **目录结构**：`packages/<pkg>.tar.gz`、`models/<app_id>/<file>`、`catalog.json`
  （模型 URL 与 catalog 里 `models[]` 的 url 逐字对齐）。
- **传后回校**：每个对象 `ossutil cp` 上传后**下载回来比对 sha256**——`ossutil` 报成功 ≠ 字节真能从 CDN 取到。
- **顺序**：先包、再共享模型、**catalog.json 最后传**（等所有包/模型确认 live，目录才不会指向尚未落地的包）。
- **`.sig` 不上传**：签名嵌在 catalog 里，设备用内置公钥验，边车文件无需上 CDN。
- **闸门**：默认 dry-run 只打印计划；**只有带 `--yes` 才真推**（生产 CDN 难撤回）。前提是
  `ossutil` 已配 + 包已 build+sign+catalog 已用 CDN base-url 重新生成。

典型发布链：`build.py`（确定性打包）→ `sign.py` → `gen_catalog.py --base-url <CDN>/packages/`
→ `publish_oss.sh --yes`。前端 `CAT_DEFAULT` 指向 CDN 的 `catalog.json` url。

**v1.3.0 发布记录（2026-08-13）**：9 个 app 包本轮加入 manifest `output` 块（结果输出
sink 配置，见 [output-sink.md](./output-sink.md)）+ app.py 的 `on_config_reload`（配置热重载），
因此**全部 9 包重打 + 重签 + catalog 用 CDN base 重新生成并发布**。注意 `output` 块随包内
`manifest.json` 分发，**不写进 catalog.json**（catalog 每 app 只含 id/name/version/description/
arch/package/models，`gen_catalog.py:265-275`）；设备安装后从包里读 `output`。本轮 `fall-detection`
升到 **0.2.0**，其余 8 app 仍 0.1.0。catalog live 地址：
`https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/catalog.json`。

---

## 7. 安装/管理 API 参考

Loopback `127.0.0.1:8130`；公网侧用官方 JWT（同源 cookie `token`）把关。官方 React
应用中心只调用 `/api/app-center/v1/`；`/api/appMgr/` 是兼容旧工具的接口。两者都由
appmgr 提供，但 nginx **没有** `/api/v1/` 别名，以免覆盖 SenseCraft/cloud proxy。
浏览器的动态请求还受同源 `Origin` 检查保护（WebSocket 也一样）；跨站 Origin
直接返回 403。设备本机脚本可直接访问 loopback。经 nginx 调用的非浏览器脚本应
显式提供与设备 URL 一致的 `Origin`，并携带有效登录 Cookie；不要依赖浏览器会
自动附带的 Cookie 作为跨站 API 凭据。

Web-native manifest v2 主流程：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/app-center/v1/policy` | 返回运行时 manifest 版本、multipart/解包/暂存限制，以及签名/本地 unsigned 策略 |
| GET | `/api/app-center/v1/trust` | 列出 Vendor/Owner 公钥的规范 SPKI 指纹；不返回 PEM 内容 |
| POST | `/api/app-center/v1/trust/owners` | 显式确认并安装 Owner P-256 PEM 公钥；新建返回 `201`，相同密钥幂等返回 `200` |
| DELETE | `/api/app-center/v1/trust/owners/<64hex>` | 按 SHA-256 SPKI digest 删除 Owner 公钥；Vendor 指纹不可删除 |
| POST | `/api/app-center/v1/uploads` | 受 JWT+同源边界保护的流式 multipart 上传并 preflight；返回服务端 `source/channel`、`upload_id`、manifest、权限、签名、警告、start-time admission 说明和 `release_id` |
| DELETE | `/api/app-center/v1/uploads/<upload_id>` | 幂等取消未进入安装阶段的暂存上传；安装已排队/进行中返回 `409` |
| POST | `/api/app-center/v1/apps` | 原样确认 preflight 权限；unsigned 还必须显式确认 `unsigned_risk_confirmed:true`，提交异步安装 |
| GET | `/api/app-center/v1/apps` | 列出多应用状态 |
| POST | `/api/app-center/v1/apps/<id>/{start,stop,restart}` | 提交异步生命周期操作 |
| GET/PUT | `/api/app-center/v1/apps/<id>/config` | 读取或更新单应用配置 |
| GET/PUT | `/api/app-center/v1/apps/<id>/visualization` | 读取或切换该应用结果是否烧录到设备视频流；能力由受信 manifest 判定 |
| GET | `/api/app-center/v1/apps/<id>/logs` | 读取有界日志尾部 |
| DELETE | `/api/app-center/v1/apps/<id>` | 提交异步卸载 |
| GET | `/api/app-center/v1/operations` | 查询异步操作状态 |
| GET | `/api/app-center/v1/resources` | 查询系统/应用资源视图 |
| GET | `/api/app-center/v1/events` | SSE 状态更新流 |

`policy` 使用 `{manifest, upload, signature}` 嵌套 envelope；其中
`upload.max_package_bytes`、`max_signature_bytes` 和 `filename_pattern` 直接来自设备当前
运行时门禁，前端不应复制编译期常量。`signature.owner_keys` 同样报告 Owner 密钥管理能力、
PEM/P-256 格式、密钥数量/字节上限，以及必须显式确认的约束。
`signature.local_web_unsigned` 明确给出本地例外的服务端 source/channel、确认字段、警告 code
和 `auto_start:false`；它不等于全局 developer mode，也不能用于 direct/cloud 请求。

Unsigned preflight 的关键契约如下（字段由服务端产生）：

```json
{
  "source": "local-web",
  "channel": "app-center-v1-same-origin",
  "signature": {
    "status": "unsigned",
    "unsigned_install_allowed": true
  },
  "warnings": [{"code": "unsigned-root-code", "severity": "critical"}],
  "unsigned_confirmation": {
    "required": true,
    "fields": ["unsigned_risk_confirmed"],
    "confirmation_field": "unsigned_risk_confirmed",
    "expected": true,
    "auto_start": false
  },
  "conflicts": [],
  "start_admission": {
    "enforced_on": "start",
    "live_resources": "deferred",
    "dependencies": "deferred"
  }
}
```

上传/preflight/install 仍静态验证 manifest 的资源声明是否合法，但不会查询或占用当前 live
reservation，也不会探测依赖是否在线。实际资源冲突、内存/存储/温度余量和依赖可用性统一在
`start` 时由 coordinator 重新采样并原子准入；因此别的应用正在占用资源可以让后续 Start
进入 waiting/失败，但不能阻止有效包的上传或安装。`GET /api/app-center/v1/resources` 的
`runtime_admission` 只用于诊断展示，不是资源租约。

Owner 导入 body 为
`{"label":"factory-floor","public_key":"-----BEGIN PUBLIC KEY-----…","confirm_trust":true}`。
`label` 只允许 1–48 个 ASCII 字母、数字、点、下划线或连字符；`public_key` 只接受 P-256
公开 PEM，服务端规范化后以 `0600` 原子写入持久化 Owner store。`confirm_trust` 必须严格为
布尔值 `true`，避免普通上传动作扩大设备信任面。GET 返回 `{"keys":[...]}`；每项包含
`kind`、`fingerprint`、`algorithm` 和 `removable`，Owner 另含 `label`。DELETE 路径使用
`sha256:` 后面的 64 位十六进制 digest。不存在返回 `404`，Vendor 修改或命名冲突返回 `409`，
无效 PEM/字段返回 `400`。POST/DELETE 都经同源 mutation 门禁、全局 busy gate、audit log
和 `trust` SSE 事件；不得把私钥或 PEM 全文写入 audit/event。

删除 Owner key 只改变**后续** package/runtime bundle 的验签结果：appmgr 不会追溯重验、撤销、
停止或卸载已经安装的代码。需要撤销已部署应用时，设备所有者必须另行停止并卸载对应应用。
同样，上传与 preflight 仅用当前 trust store 验证签名，绝不从 package、manifest、边车签名或
其他上传字段自动提取、安装或信任任何公钥材料。

上传取消与 finalize 串行，并在上传状态锁内完成
“读状态→删除”：`install_queued`/`installing` 一律保留并返回 `409`；合法但未知或已经删除的
32 位十六进制 ID 返回 `200 {deleted:false,state:"absent"}`，所以断线重试不会把成功清理误报
成失败；成功删除返回 `200 {deleted:true,state:"deleted",previous_status}`。格式不合法的 ID
不匹配路由并返回 `404`。DELETE 与其他 mutation 一样继续受 JWT 边界和同源 `Origin` 门禁。

以下是 legacy `/api/appMgr/` 兼容参考：

> 安全边界：`/api/appMgr/upload` 与 `/api/appMgr/putModel` 的历史实现会把完整
> raw body 放进 appmgr 内存，因此 nginx 公网边界对这两个 exact path 固定返回
> **410 Gone**。它们只保留给 loopback 迁移工具。Web 应用必须使用流式、有总配额的
> `/api/app-center/v1/uploads`，依赖/模型按 manifest v2 bundled 或 content-addressed
> artifact 声明。

| 方法 | 路径 | 入参 | 返回 | 源码 |
|---|---|---|---|---|
| GET | `/api/appMgr/list` | — | `{active_app, apps:[{id,name,version,type,image,description,scene,author,installed,running,pid,active}]}` | `server.py:444` / `do_list:97` |
| POST | `/api/appMgr/install` | `{path, signature?}` | `{id, version, installed:true, signature:{signed,verified,alg,detail}}` | `server.py:508-512` / `do_install:197` |
| POST | `/api/appMgr/uninstall` | `{id}` | `{id, uninstalled:true, stopped, was_active}`（先停→清 active→删 app 目录 + per-app venv；共享 models 不动） | `server.py:513-517` / `do_uninstall:209` / `installer.uninstall:180` |
| POST | `/api/appMgr/switch` | `{id}` | `{active_app, pid, prev}` | `server.py:518-522` / `do_switch:243` |
| POST | `/api/appMgr/stop` | `{id?}` | `{stopped, detail}`（无 active 时 `{stopped:null, note}`） | `server.py:523-524` / `do_stop:268` |
| POST | `/api/appMgr/upload` | **仅 loopback**；公网 nginx 返回 410 | 历史 raw package staging | `server.py` / `do_upload` |
| POST | `/api/appMgr/putModel` | **仅 loopback**；公网 nginx 返回 410 | 历史 raw model staging | `server.py` / `modelstore.write_model` |
| GET | `/api/appMgr/config` | query `?id=` | `{id, config_schema, values, defaults}` | `server.py:446-453` / `do_get_config:279` |
| POST | `/api/appMgr/config` | `{id, config:{...}}` | `{id, saved, applied:"none"|"live"|"restart", changed_keys, noop, restarted, reloaded, config}`；仅真实 effective delta 参与 apply 分类 | `server.py:do_set_config` |
| GET | `/api/appMgr/mqtt` | — | 全局 MQTT/HA 配置（密码脱敏为 `password_set`） | `server.py:454-455` / `do_get_mqtt:389` |
| POST | `/api/appMgr/mqtt` | `{mqtt:{...}}` 或平铺 | 同上视图 + `restarted`（改后重启 active app 生效） | `server.py:532-536` / `do_set_mqtt:394` |
| GET | `/api/appMgr/metrics` | — | `{npu_load, mem, temp_c, active_app, uptime_s, ts}` | `server.py:456-457` / `do_metrics:371` |

**错误码**（`server.py:538-543`）：
`400` 参数/校验/安装/监督错误（`ValueError`/`InstallError`/`SupervisorError`）；
`409` 忙（`{"error":..., "code":-2}`，有别的 install/uninstall/switch/stop 在跑，busy-gate 串行化）；
`404` 未知路径；`500` 其他异常。

---

## 8. 现状与限制（诚实标注）

- **appmgr 是应用方旁挂实现，非原厂内建**。规格 §1（14-20 行）明确"打包分发/签名/沙箱不在本版固件范围"，
  列为 P1/P2。appmgr 代码+状态放在 `/userdata`（survive OTA），靠 `S94appmgr` 开机重注入
  nginx 边缘 conf，OTA 后的重注入触发链**不完全自动**（需 `appmgr-restore.sh`，见 `S94appmgr:22-30`）。
- **签名/上架是半成品（生态侧）**：机制完整可跑，但只有单密钥自签、无第三方开发者证书体系。
  方案商要上架出厂设备，需 Seeed 侧签发、或自管公钥、或关闭强制。详见 §5。
- **有卸载、仍无版本管理 API**：卸载已接出（HTTP `POST /api/appMgr/uninstall`
  `server.py:513-517` / `do_uninstall:209`，CLI `python3 -m appmgr uninstall <id>`，底层
  `installer.uninstall:180`：停→清 active→删 app 目录 + per-app venv，共享 models 不动）。
  但**仍没有多版本共存、没有回滚面**：安装是"原子换目录"（旧目录移为 `.old` 再删，
  `installer.py:163-173`），`state.json` 只记 active app + version。卸载/`putModel` 的
  **端到端真机验证仍 gated**（机制在位、单元测试覆盖 `modelstore`，但设备侧 E2E 尚未闭环）。
- **单活**：同一时刻只有一个 app 运行（摄像头独占），`switch` 会停掉其它。
- **v1 无沙箱**：app 全部以 root 运行，扩展间无强隔离（规格 §1.1：source_id 防冒充在全 root 下
  "只能防手滑不能防恶意"）。
- **包体上限**：压缩 ≤200 MB、解包 ≤400 MB、成员 ≤4096（`paths.py:63-65`）。
- **当前样本**：`apps/` 有 9 个 app 目录，`catalog.json`（现为 CDN 形态）**9 个应用全部已签名收录**
  （face-analysis / facemesh-reader / fall-detection / fitness-trainer / ppocr-reader /
  qrcode-reader / retail-vision / yolo-detector / voice-transcribe）。manifest v2 的 9 个样本所需
  模型均以 authenticated bundled artifacts 随包安装（catalog `models[]` 为空）；
  voice-transcribe 的 `models: []` 只是不使用 kit 的视频模型加载器，其 ASR/VAD/KWS 资产在
  `artifacts[]` 中并落到 app root 的 `models/asr/`。旧 catalog 的共享模型/`putModel` 链路
  仅供 loopback 迁移，公网已关闭（见 §3、§6）。

# 推理即应用：配置热更、内建一等 app、单活切换

> 事实来源：
> - 热更：`kit/app.py`（`on_config_reload` + SIGHUP，`app.py:63-133`）、`market/appmgr/server.py`（`do_set_config:288` 按 `apply` 分流）、`kit/config.py` / appmgr `config.py`（`write_user_config` merge、`config_schema` 校验）。
> - 内建 driver：`market/appmgr/builtin.py`（封装 entry.cgi `/model/inference` + `/model/info` + `/notify`）。
> - 单活/分派：`market/appmgr/server.py`（`_builtin_entry:130`、`do_activate:309`、config 分派 `server.py:374+`）。
> - 前端：`recamera_web_react/`（wsl2-local）`components/inference/`（推理应用页）+ `SchemaForm` + `AppContext`。
> - 衔接：[kit-design.md](./kit-design.md)（分层）、[app-center-publishing.md](./app-center-publishing.md)（manifest / appMgr API）、[per-app-dependencies.md](./per-app-dependencies.md)（interpreter/依赖）、[adapter-bootstrap.md](./adapter-bootstrap.md)（适配层）。

## 0. 定位

把"固件内建推理"和"自建 kit 应用"收敛成**同一套应用模型**：都在 `/api/appMgr/list` 里以 app 条目出现、都能被 `activate` 单活切换、都有 `config_schema` 驱动的动态配置面板。差异只在 driver 层。本文讲三件事：**① 配置热更（live vs restart）② 内建变一等 app ③ 单活切换契约**。manifest / 打包 / 依赖分发不在本文，见上方衔接文档。

## 1. 配置热更：`apply: "live" | "restart"`

### 1.1 manifest 声明

`config_schema` 的每个可配置项新增 `apply` 字段：

```jsonc
{ "key": "conf", "type": "number", "min": 0, "max": 1, "step": 0.05,
  "apply": "live" },        // 改后不重启，运行中热替换
{ "key": "input_size", "type": "enum", "options": [320, 480],
  "apply": "restart" }      // 改后必须重启进程才生效
```

缺省未标 `apply` 的项，按现有行为（保守走 restart）处理。

### 1.2 appmgr 分流（`do_set_config`）

`POST /api/appMgr/config {id, config:{...}}` 写入时（`server.py:do_set_config:288`）：

1. appmgr 先把已校验的 incoming 与“manifest defaults + 用户 overlay”得到的当前 effective values 做类型感知的深比较；只保留**真实变化**的 key。旧版前端即使误传整张表，也不会因为其中未变化的 restart 字段而重启应用。
2. `write_user_config` 把真实新值 **merge** 进 app 的 `config.json`（覆盖层，不整篇替换——只改传入的 key，其余保留）。传 `null` 删除该 key 的用户 overlay，恢复 manifest default。
3. 按真实变更项的 `apply` 分流：
   - **全部变更项都是 `live`** → 给 app 进程发 `kill -HUP`（SIGHUP），不重启。
   - **任一变更项是 `restart`** → `stop` + `start` 该 app（active 且在跑时）。

响应统一返回 `changed_keys`、`applied`、`restarted`、`reloaded` 与 `noop`。没有 effective change 时 `applied:"none"`、`noop:true`，既不 SIGHUP 也不重启。

### 1.3 kit 侧热重读（SIGHUP → 自动重绑 + `on_params_changed`）

`kit/app.py` 基类（`app.py:63-133`）：

- 主循环启动时装 SIGHUP handler（`app.py:107-111`，best-effort，仅主线程可装）。
- 收到 SIGHUP → 置标志 → 下一帧循环边界重读 `config.json` 的**有效配置**，然后：先把 `apply:"live"` 的 `config_schema` 键**自动重新绑定**到 `self`（`app.py:_bind_params`），对**发生变化**的键调 `on_params_changed(changed)`；随后再调基类的 `on_config_reload(cfg)` 兜底（两者**永不把异常抛进循环**）。
- **应用侧推荐 override `on_params_changed(changed: set)`**：普通标量旋钮（`max_faces`、阈值等）kit 已自动重绑到 `self`，**无需任何代码**；只有需要**重建派生对象**（状态机、缓存几何、`pipeline.pad`）时才 override 它，**不重建 pipeline / 不重载模型**。`on_config_reload` 是更底层的基类钩子（默认重挂 base 管的 `conf`/`iou`），一般不必覆盖。实际迁移后的应用（fitness / fall / facemesh 等）override 的是 `on_params_changed`。
- `restart` 项永远走进程重启，不经这里。

> 收益：调阈值/ROI 这类高频调参不打断摄像头链路（不抢相机、不闪流）；换模型/换输入分辨率这类重操作才重启。

## 2. 内建推理 = 一等 app（builtin driver）

### 2.1 driver（`builtin.py`）

`builtin.py` 把固件内建推理封成一个 id=`"builtin"` 的 app driver，全部经 entry.cgi HTTP（localhost 免 JWT）：

| driver 动作 | entry.cgi 端点 | 说明 |
|---|---|---|
| `start()` / `stop()` | `POST /model/inference {iEnable}` | 开/关内建推理（不碰 rkipc / 视频管线，`builtin.py:150-157`） |
| `is_running()` | `GET /model/inference` → `iEnable` | 运行态 = `iEnable==1`（无 run.pid，`builtin.py:136-138`） |
| `current_model()` | `GET /model/inference` → `sModel` | 当前模型 |
| `get_config()` | `GET /model/inference` + `/model/info` | 反组装成与自建 app **同构**的 config 视图 |
| `set_config()` | `POST /model/info` + `POST /model/inference` | 分派：改模型/帧率/指标 |
| 指标写回 | `/model/info`（read-modify-write，`builtin.py:167`） | — |

> 踩坑（`builtin.py:22-25`，2026-08-13 核实）：单独 `POST /model/info` 改模型**不触发重载**；driver 在内建已启用时把 `/model/info` 变更与一次 `/model/inference` POST 配对，强制 `rc_model_infer_restart`。

### 2.2 注入 list

`server.do_list()` 在应用列表里注入 builtin 条目（`_builtin_entry:130`）：`{id:"builtin", type:"builtin", running, active, ...}`。`running` 从 `/model/inference` 的 `iEnable` 派生，不是 run.pid。

## 3. 单活切换契约（`activate`）

`POST /api/appMgr/activate {id}`（`server.py:do_activate:309`）——**单活互斥**，内建与自建互斥：

| 传入 `id` | 行为 |
|---|---|
| `"builtin"` | 停当前 active 的自建 app → `builtin.start()`（iEnable=1，保留固件持久化的 model/fps） |
| 自建 app id | `builtin.stop()`（先关内建检测）→ 停旧 active → 起目标 → 置 active |
| `"none"` / 空 | 停当前 active 自建 app（内建保持其 iEnable 态） |

- 互斥语义：**同一时刻只有一个推理源在跑**（内建 XOR 一个自建 app），避免双份 NPU/相机争用。builtin 的 active = `/model/inference` 的 iEnable；自建的 active 由 state.json 维护。`_builtin_entry` 里 `active = iEnable AND 无自建 active` 是给 UI 的双保险（`server.py:154-155`）。
- config 分派也认 builtin：`GET/POST /api/appMgr/config?id=builtin` 转 driver 的 `get_config`/`set_config`（`server.py:374+`），返回与自建 app 同构的 `{config_schema, values, defaults}`，前端一套面板通吃。

> 与 `switch` 的关系：`switch` 是旧的自建-only 切换；`activate` 是把 builtin 纳入后的统一单活入口。完整 appMgr 端点表见 [app-center-publishing.md](./app-center-publishing.md) §7。

## 4. 前端：动态配置面板

官方 React `/app-center` 的推理页改造（`components/inference/`）：

- **`SchemaForm`**：读 app 的 `config_schema` 渲染表单；每项按 `apply` 标 chip——`live` 显示"即时生效"、`restart` 显示"需重启"。控件类型对齐 manifest schema（number/boolean/enum/string/zone/line，见 app-center-publishing.md §manifest）。
- **`AppContext`**：维护 `active` 态；切换应用（含 builtin）走 `activate`，list/config 随 active 刷新。
- 原"AI 推理"页替换成**"推理应用"**：单选激活（内建 + 自建同列）+ 动态配置面板，保留模型仓库与输出监控。

## 5. `inferenced` 模型连接的空闲语义

`scheduled` NPU 应用完成 Hello 且通过 appmgr 的 `instance + generation` 授权后，
模型连接允许长期无请求，直到进程关闭 fd（EOF/HUP）或 daemon 停机。级联流水线可能让
某个模型合法空闲数分钟，例如 PPOCR 没检测到文字时 recognizer 不会收到 `infer`；不能把
这种空闲误判为掉线并卸载模型。每次后续操作仍重新校验 appmgr 授权，排队后的 `infer`
进入 RKNN 前还会再次校验，因此长连接不会把一次授权变成永久 bearer credential。

DoS 边界保持有界：Hello 前的静默 socket 最多保留 5 秒，`control_only` 的 status/ping
连接仍受 `client_idle_timeout` 限制；已认证模型连接始终计入 `max_clients`。阻塞接收期间
不持有 service/driver 锁，daemon `close()` 会对全部连接执行 `shutdown()`，立即唤醒阻塞
线程并按已有 EOF/HUP 路径释放该客户端的模型别名。

## 6. 一句话

内建推理与自建应用统一成一套 app 模型：`list` 同列、`activate` 单活互斥、`config_schema`+`SchemaForm` 一套动态面板。配置改动按 `apply` 分流——`live` 走 SIGHUP 热重读（kit 自动重绑参数 + `on_params_changed`，不重启不抢相机），`restart` 才重启进程。内建的 driver（`builtin.py`）把 entry.cgi 的 `/model/inference`+`/model/info` 反组装成同构 config，前端无需为内建单独写页面。

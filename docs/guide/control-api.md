# 扩展控制面 API（M4）

> 方案商用 HTTP 控制 reCamera Pro 的稳定入口。分两部分：
> 1. **版本化扩展域** `/api/v1/ext/*`——查询扩展 API 能力集（本文新增，M4）。
> 2. **存量 API 必需子集**——从现有 18 域里挑出方案商必需的端点，标注哪些**冻结承诺稳定**、哪些 **internal 不承诺**。
>
> 全部端点经 `entry.cgi`（`recamera_web_backend`），走 nginx `/cgi-bin/` 路由。登录/鉴权流程见《rkipc RPC 现状》(`rkipc-rpc-status.md`) §入口与鉴权，本文不重复，仅给要点。

代码基：`project/app/recamera_web/recamera_web_backend/`（C++/cgicc/FastCGI/jwt-cpp）。设备上二进制在 `/oem/usr/www/cgi-bin/entry.cgi`，由 nginx `common_relay.conf` 的 `location /cgi-bin/` 经 `fcgiwrap` 每请求 fork 执行。

> **范围界定（勿与应用中心 API 混淆）**：本文只讲 **entry.cgi 控制面**（`/cgi-bin/entry.cgi/*` 存量域 + `/api/v1/ext/*` 扩展域）。
> 应用中心的**安装/卸载/单活切换/配置/共享模型写盘**是**另一套 API**——appmgr 的 `/api/appMgr/*`
> （loopback `127.0.0.1:8130`，含 `install` / `uninstall` / `switch` / `stop` / `upload` / `putModel` / `config` / `mqtt` / `metrics`），
> 完整端点表见 [app-center-publishing.md](./app-center-publishing.md) §7，本文不覆盖。两者都经 nginx 用同一道官方 JWT 边界把关，但进程与代码基不同（entry.cgi 是 C++ 固件二进制；appmgr 是 `/userdata` 下的 Python 常驻服务）。

---

## 0. 调用约定

- **URL 形态**：`http(s)://<设备>/cgi-bin/entry.cgi/<PATH_INFO>`。nginx 把 `entry.cgi` 之后的部分作为 `PATH_INFO` 传给 CGI（`common_relay.conf` 的 `location /cgi-bin/`：`$fastcgi_script_name ~ "^(.+?\.cgi)(/.+)$"` → `PATH_INFO=$2`）。
  - 例：`GET /cgi-bin/entry.cgi/api/v1/ext/capabilities` → `PATH_INFO=/api/v1/ext/capabilities`。
- **路由**：`rest_api.cpp` 按 `PATH_INFO` 的**第一路径段**分派（`ApiEntry::run()` 的 `Req.Api.compare(1, 20, h.Api, 0, 20)`）。扩展控制面的第一段是 `api`，由 `ExtApiHandler` 独占，内部再校验 `/api/v1/ext/` 前缀。
- **鉴权**（`rest_api.cpp:auth_verify()`，三选一通过）：
  1. **本机直通**：`127.0.0.1` 来源免鉴权（nginx `geo` 注入 `HTTP_X_INTERNAL_FROM_LOCALHOST=1`）。设备上的扩展进程调 API 零门槛。
  2. **Internal API Key**：`VerifyInternalApiKey()`（内部服务用）。
  3. **JWT**：外部访问。登录拿 token 后**放 Cookie**（`Cookie: token=<JWT>`）。获取流程见下节。
- **响应封装**：成功数据类端点 `setApiData(<obj>)` 直接输出该 JSON 对象；写操作成功输出 `{"code":0,"message":"success"}`（`setSuccessResponse`）；错误统一 `{"code":<httpstatus>,"message":"<reason>"}`（`setErrorResponse`）。`Content-Type: application/json`。

### 登录流程（要点，详见 rkipc-rpc-status.md）

```
GET  /cgi-bin/entry.cgi/system/key            → {"sPublicKey":"-----BEGIN RSA PUBLIC KEY-----\n...\n"} （PKCS#1）
POST /cgi-bin/entry.cgi/system/login          body {"sUserName":"admin","sPassword":"<base64(PKCS1v15(明文密码))>"}
     ← 成功 body {"iAuth":1,"iStatus":0,"sWaittime":0} 且 Set-Cookie: token=<JWT>
后续请求带 Cookie: token=<JWT>
```

- token **只在 `Set-Cookie` 里**，不在 body；实测 `Authorization: Bearer`/`?token=` 不如 Cookie 稳。
- 连续失败按 IP 递增锁定等待（`system_api.cpp:3491` 附近）。

---

## 1. 版本化扩展域 `/api/v1/ext/*`（M4 新增）

### 1.1 `GET /api/v1/ext/capabilities`

返回扩展 API 的能力集，与 rkipc 握手 `HelloAck.capabilities`（`../api/spec.md` §1.2）一致。**能力集由 entry.cgi 静态提供**（硬编码于 `ext_api.cpp`）——entry.cgi 与 rkipc 是独立进程，不跨进程查询 rkipc；这些值是 v1 冻结基线契约，rkipc 侧 limits 变更时本表须同步。

鉴权：走 `auth_verify`（本机直通 / JWT Cookie），与其他域一致。

返回体（`Content-Type: application/json`）：

```json
{
  "api_version": 1,
  "auth_mode": "peercred",
  "server_build": 20260811,
  "capabilities": [
    {
      "name": "frame",
      "version": 1,
      "limits": { "pool_depth": 6, "max_subscribers": 4, "max_outstanding": 2 }
    },
    {
      "name": "result",
      "version": 1,
      "limits": { "max_msg_rate": 60, "max_sources": 8, "max_connections": 4, "max_payload_bytes": 65536 }
    },
    {
      "name": "probe",
      "version": 1,
      "limits": {},
      "stages": ["preproc.out", "npu.raw", "postproc.out", "metrics"]
    }
  ]
}
```

字段含义（对应 spec）：

| 字段 | 含义 | spec 出处 |
|---|---|---|
| `api_version` | 服务端支持的最高扩展 API 版本 | §1.2 HelloAck.api_version |
| `auth_mode` | 当前认证模式；v1 = `peercred`（app token 将来作为并行新增模式） | §1.1 |
| `server_build` | 构建标识（yyyymmdd） | §1.2 HelloAck.server_build |
| `frame@1.limits` | 帧代理：池深 / 最大订阅者 / 每连接同时持帧上限 | §2.4 |
| `result@1.limits` | 结果回注：每连接 60 msg/s、≤8 source、≤4 连接、单条 ≤64KB | §3.3 |
| `probe@1.stages` | 观测面 tap 点 | §4.1 |

> **v1 baseline 承诺**：`frame@1`/`result@1`/`probe@1`/
> `inference-control@1` 一经发布不可移除；能力演进 = 新增 Capability 或提升
> version；limits 数值可变，客户端必须按握手/本端点返回值自适应，
> **不得硬编码**（§1.2 / §8.2 扩展五规则）。

示例：

```bash
# 设备本机（免鉴权）
curl http://127.0.0.1/cgi-bin/entry.cgi/api/v1/ext/capabilities

# 外部（先登录拿 Cookie token，再带上）
curl -k -b "token=<JWT>" https://<设备>/cgi-bin/entry.cgi/api/v1/ext/capabilities
```

### 1.2 `GET /api/v1/ext/subscriptions`（诊断视图，本轮未实现）

当前帧/结果/观测的活跃连接（peercred 身份 + 限速计数）诊断视图。

**本轮返回 `501 Not Implemented`**：该视图需要跨进程从 rkipc（`rc_ext_core`）拿运行时状态，而 rkipc 尚未暴露只读查询接口。为不改 rkipc（M4 只碰 entry.cgi），本端点占位返回：

```json
{"code":501,"message":"Not Implemented: subscriptions view awaits an rkipc read-only state interface"}
```

**后续方向**：待 rkipc 暴露订阅状态查询——经现有 `/var/tmp/rkipc` RPC，或新增一条只读 socket——entry.cgi 侧再补充跨进程查询并聚合返回。

### 1.3 kit 控制面客户端 `CgiControl`（已实现）

共享 kit 运行时提供 `CgiControl`——把下列存量端点封装成方案商可直接调用的控制面。`kit.control.select_control()` 现返回 `CgiControl` 实例（不再抛 `NotImplementedError`）。

- **`set_inference(enable, model, fps)`**：调 `POST /cgi-bin/entry.cgi/model/inference?id=<n>`，body `{"iEnable":<0|1>, "sModel":"<name>", "iFPS":<n>}`——开关内建推理、切模型、设帧率。
- **`snapshot()`**：走**帧代理**取帧（`FrameSource` 抓一帧 → `cv2` 编 JPEG 返回字节），**不经 entry.cgi**——entry.cgi 无取帧端点，故用帧代理旁路实现单帧抓取。

**HTTPS + 本机免鉴权（接入要点，踩坑写清）**：

- 请求走 **HTTPS 443**（自签证书，客户端须 `verify=False` / `curl -k`）。**不要打 80**：nginx 从 80 会 **307 跳转**到 443（`/userdata/config/system/ssl/rewrite_enable.conf`），不跟随重定向的客户端（`http.client`、WebSocket 握手）直接拿到 307。直接用 `https://127.0.0.1/...`。
- **固件 HTTPS 开关**：Web UI「连接 → HTTPS」或 `POST /cgi-bin/entry.cgi/system/secure {"sEnable": false}`（entry.cgi `Https_Setting`），关闭后 80 直服、443 反向 `307 http://`。`kit/adapters/cgi_control.py` 与 `market/appmgr/builtin.py` 的 `_request` 对 301/302/307/308 **跟随一跳**（只取 scheme/port/path，host 固定 127.0.0.1），两种状态均已真机验证（2026-08-17）。
- 来源 `127.0.0.1` 命中 nginx localhost 直通（`HTTP_X_INTERNAL_FROM_LOCALHOST=1`），**免 JWT**——设备上的 app 调控制面零门槛，无需先登录拿 Cookie。
- 真机已验证 `set_inference` + `snapshot` 通过。

---

## 2. 存量 API 必需子集（冻结 vs internal）

> 从现有 18 域里挑方案商必需子集。**冻结承诺稳定** = 路径/方法/语义对方案商稳定，可长期依赖；**internal 不承诺** = 设备管理/固件/整机操作，随固件演进可能变，方案商不应依赖。
> 端点核实自 `rest_api.cpp` + 各 `*_api.cpp`（下表标注行号）。

### 2.1 system 域 `/system/*`（`system_api.cpp`）

| 方法 | 路径 | 用途 | 关键参数/返回 | 稳定性 |
|---|---|---|---|---|
| GET | `/system/key` | 登录第 1 步：取 RSA 公钥 | 返回 `{"sPublicKey":"...PKCS#1..."}`（`:2911`） | **冻结** |
| POST | `/system/login` | 登录 | body `{"sUserName","sPassword":base64(PKCS1v15)}`；成功 `Set-Cookie: token=`（`:3477`） | **冻结** |
| POST | `/system/logout` | 注销（作废会话）（`:3587`） | — | **冻结** |
| GET | `/system/device-info` | 设备信息（型号/SN 等）（`:2985`） | JSON | **冻结** |
| GET | `/system/version` | 固件版本（`:2926`） | JSON | **冻结** |
| GET | `/system/resource-info` | CPU/NPU/内存占用（`:2991`；`rk_system_get_cpu/npu/mem`） | JSON | **冻结** |
| GET | `/system/remain-space` | `/userdata` 剩余空间（`:2997`） | JSON | **冻结** |
| GET | `/system/time` / POST `/system/time` | 读/设时间（`:2893`/`:3042`） | JSON | **冻结**（读） |
| POST | `/system/reboot` | 重启设备（`:3413`） | — | internal（慎用，会断相机/推理） |
| POST | `/system/password` | 改密（path `login/modify`）（`:3609`） | — | internal |
| GET/POST | `/system/ssh` `/system/adb` `/system/secure` | 调试口/安全开关（`:2888`/`:2961`/`:2953`） | — | **internal 不承诺** |
| POST | `/system/factory-reset` `/system/recovery` | 恢复出厂/进 recovery（`:3429`/`:3421`） | — | **internal 不承诺** |
| POST/GET | `/system/firmware-*` | OTA 升级/下载/进度（`:3093` 等） | — | **internal 不承诺** |
| GET | `/system/battery` `/system/check` `/system/update-status` | 电量/检查/升级状态（`:2932`/`:2939`/`:2946`） | JSON | internal |

### 2.2 model 域 `/model/*`（`model_api.cpp`）

| 方法 | 路径 | 用途 | 关键参数/返回 | 稳定性 |
|---|---|---|---|---|
| GET | `/model/list` | 列出已装模型（`:339`） | JSON 列表 | **冻结** |
| GET | `/model/info` | 当前模型信息（`:356`） | JSON | **冻结** |
| GET | `/model/algorithm` | 支持的算法/后处理列表（`:390`） | JSON | **冻结** |
| GET | `/model/inference` | 推理配置/状态（`:406`） | JSON | **冻结** |
| GET | `/model/download_status` | 模型下载进度（`:422`） | JSON | internal |
| POST | `/model/upload` | 上传模型（multipart）（`:469`） | FormFile | **冻结** |
| POST | `/model/info` `/model/get_model` | 设置/获取模型（`:784`/`:871`） | JSON | 冻结（写慎用） |
| POST | `/model/inference` `/model/inference-restart` | 配置推理/重启推理（`:1053`/`:1137`） | JSON | 冻结（写慎用） |
| DELETE | `/model/delete` | 删除模型（`:1158`） | — | internal（破坏性） |

### 2.3 video 域 `/video/{stream_id}/*`（`video_api.cpp`）

路径按 `/video/{stream_id}/{specific}` 解析（`:948-985`）。

| 方法 | 路径 | 用途 | 稳定性 |
|---|---|---|---|
| GET | `/video/{id}/encode` | 取编码参数（分辨率/码率/编码器）（`:988`） | **冻结** |
| GET | `/video/{id}/stream` | 取流参数（`:1003`） | **冻结** |
| POST/PUT | `/video/{id}/encode` | 改编码参数（`:1032`） | 冻结（写慎用，影响 RTSP/录像） |
| POST/PUT | `/video/{id}/stream` | 改流参数（`:1070`） | 冻结（写慎用） |

### 2.4 osd 域 `/osd/cfg`（`osd_api.cpp:271-293`）

| 方法 | 路径 | 用途 | 稳定性 |
|---|---|---|---|
| GET | `/osd/cfg` | 读 OSD 配置（`osd_manager_get_cfg`） | **冻结** |
| POST/PUT | `/osd/cfg` | 写 OSD 配置（校验后 `osd_manager_set_cfg`；含 privacyMask 等，坐标归一化 [0,1]） | **冻结** |

> 仅 `cfg` 资源；其余子路径返回 501（`osd_api.cpp:274`）。OSD 叠加是结果上屏/录像的官方通道，方案商需要"结果上 OSD"应优先走 M1 `result-in.sock`（见 result-push.md），`/osd/cfg` 用于配置固定叠加项。

### 2.5 notify 域 `/notify/*`（`notify_api.cpp`）

| 方法 | 路径 | 用途 | 关键参数/返回 | 稳定性 |
|---|---|---|---|---|
| GET | `/notify/cfg` | 读通知配置（去除 `dWebsocket`） | JSON | **冻结** |
| GET | `/notify/status` | 通知运行状态（`/tmp/notify_status.json`） | JSON | **冻结** |
| POST | `/notify/cfg` | 写通知配置（`iMode` 0=off/1=MQTT/2=HTTP/3=UART；合并后校验+重启 notify 服务） | JSON | **冻结** |

> 结果推送落地方式（MQTT/HTTP/UART/WS）见 result-push.md；`/notify/cfg` 是配置面，`/var/tmp/notify` 是数据面。

---

## 3. 冻结承诺与演进纪律

- **冻结**端点（上表标 **冻结**）：路径、方法、语义、响应封装对方案商稳定。字段可**加不可删**；删字段/改语义须升域内约定版本并保留旧行为 ≥2 个固件版本。
- **internal 不承诺**端点：设备管理/固件/整机操作，Seeed 内部与前端用，随固件演进可能变更，**方案商不应依赖**。
- **扩展控制面** `/api/v1/ext/*` 遵循 spec §8.2 扩展五规则：加法优先、数量演进走 `limits`、结构演进走保留位、任务演进走 oneof 追加、只增不减。
- **鉴权过渡**：localhost 直通与 JWT 为 v1 机制；沙箱阶段 app token 作为**新增**认证模式并行提供，老机制保留（§1.1），方案商不要把"本机免鉴权"当长期前提。

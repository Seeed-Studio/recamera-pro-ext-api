# 结果推送接入：向 `/var/tmp/notify` 注入推理结果

> 事实来源：`project/app/recamera_notify/notify-server/`（Python 源码）与
> `project/app/recamera_ipc/common/vigil/protocol/inference.proto`。

## 定位（先读这段）

`/var/tmp/notify` 是 notify-server 的入站 unix socket。写入一条 `InferenceResult`，它会被**分发**到 WebSocket / MQTT / HTTP / UART。

- **此通道只做分发。结果不会画进 OSD 叠加、不会进录像。**
- 要让结果出现在视频叠加中，走 `result-in.sock`；该入口还会推送结果，并保留无 `dSource` 旧录像规则的行为，但不能匹配显式 BUILTIN/APP 来源。
- 托管应用要按 AI 结果启动录像，必须在 manifest v2 声明 `record_trigger`，由 appmgr 过滤并桥接到受保护的 `record-in.sock`；应用不能直接使用该内部入口。
- 该 socket 权限 0666、无鉴权，定位为无特权 legacy 通道；后续固件会对其加全局限速，但格式与路径保持不变。

## 能干什么

- 你的进程算出的检测/分类/分割/跟踪/关键点结果，注入后：
  - **始终**推到 WebSocket（本机 127.0.0.1:8123，外部经 nginx `/ws/inference/results`）；
  - 按配置的模式**再推一路**：MQTT 或 HTTP 或 UART（三选一或关闭）。
- 你的后端/前端可以从 WS:8123 消费全部结果（含官方内建推理的结果）。

## 前置条件

- 本机进程（SSH 或随包部署的服务），任意用户（socket 0666，`core/socket_server.py:90`）。
- 构造 protobuf 需要 `inference.proto` 对应的生成代码。设备上现成的 Python 生成代码在 notify-server 的 `protobufs/inference_pb2.py`；自己编译则 `protoc --python_out=. inference.proto`（proto 文件随本文档发布）。

## 线格式

- socket：`/var/tmp/notify`，`AF_UNIX` + `SOCK_STREAM`（`core/socket_server.py:18,85-90`）。
- 帧格式：`<4 字节小端 uint32 长度><该长度的 InferenceResult protobuf 字节>`（`socket_server.py:172-176`）。
- 一条连接可循环发多帧，长连接即可。

## `InferenceResult` 字段（inference.proto 全量）

```proto
enum TaskType { CLASSIFICATION=0; DETECTION=1; SEGMENTATION=2; TRACKING=3; KEYPOINTS=4; }

message InferenceBox   { float left=1; float top=2; float right=3; float bottom=4; }
message InferencePoint { float x=1; float y=2; float score=3; int32 keypoint_id=4; }

message InferenceDetectionEntry { InferenceBox box=1; float score=2; int32 class_id=3; string class_name=4; }
message InferenceDetectionResult { repeated InferenceDetectionEntry entries=1; }

message InferenceClassificationEntry { float score=1; int32 class_id=2;
  string class_name=3; InferenceBox box=4; } // box 可选
message InferenceClassificationResult { repeated InferenceClassificationEntry entries=1; }

message InferenceSegmentationEntry { InferenceBox box=1; float score=2; int32 class_id=3;
  string class_name=4; bytes mask=5; int32 mask_width=6; int32 mask_height=7; }
message InferenceSegmentationResult { repeated InferenceSegmentationEntry entries=1; }

message InferenceTrackingEntry { InferenceBox box=1; float score=2; int32 class_id=3;
  string class_name=4; int32 track_id=5; }
message InferenceTrackingResult { repeated InferenceTrackingEntry entries=1; }

message InferenceObjectInfo { int32 class_id=1; string class_name=2; float score=3; InferenceBox box=4; }
message InferenceKeypointInstance { oneof object_info { InferenceObjectInfo object=1; }
  repeated InferencePoint points=5; }
message InferenceKeypointsResult { repeated InferenceKeypointInstance instances=1; }

message InferenceResult {
  TaskType task_type = 1;
  int64 timestamp_ms = 2;     // epoch 毫秒
  int32 model_id = 3;
  string source_id = 4;       // 来源；legacy 直写可为空
  uint64 pts_us = 5;          // 帧的 monotonic PTS；0=不关联帧
  oneof data {
    InferenceDetectionResult      detection      = 10;
    InferenceClassificationResult classification = 11;
    InferenceSegmentationResult   segmentation   = 12;
    InferenceTrackingResult       tracking       = 13;
    InferenceKeypointsResult      keypoints      = 14;
  }
}
```

`source_id`/`pts_us` 已是当前协议的 tag 4/5；legacy 直写者可以省略，接收端会看到默认值。box 字段本身只规定 float，不由 notify 做坐标转换：内建来源沿用其原生坐标，`result-in.sock` 转发的外部来源使用归一化 `[0,1]`。

> **注**：此为 **notify 出站**（内建推理原生坐标），与 `result-in.sock` 注入的**归一化 [0,1]** 契约不是同一条路径——notify 不经 result-in.sock、不上 OSD、无 clamp。其坐标单位以内建推理为准（需核实），不要照搬 result-in.sock 的归一化约定。

## 注入示例（Python）

```python
import socket, struct, time
import inference_pb2  # protoc 生成，或复用设备上 notify-server/protobufs/inference_pb2.py

def make_result():
    r = inference_pb2.InferenceResult()
    r.task_type = inference_pb2.TASK_TYPE_DETECTION
    r.timestamp_ms = int(time.time() * 1000)
    r.model_id = 100                      # 自选，避开官方模型在用的 id
    e = r.detection.entries.add()
    e.box.left, e.box.top, e.box.right, e.box.bottom = 100, 100, 300, 400
    e.score, e.class_id, e.class_name = 0.92, 0, "person"
    return r

sk = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sk.connect("/var/tmp/notify")
payload = make_result().SerializeToString()
sk.sendall(struct.pack("<I", len(payload)) + payload)   # <le32 len><protobuf>
# 长连接可继续发；发完 close 即可
sk.close()
```

## 四种出口与配置

配置文件 `/userdata/config/notify.json`（`core/config_manager.py:16`），缺省值见源码 `notify_config_default.json`。`iMode` 选择"额外的一路"：`0=off / 1=mqtt / 2=http / 3=uart`（`core/notifier_manager.py:63`）；**WebSocket 与 iMode 无关，始终开启**（`notifier_manager.py:133-137`）。

```json
{
  "iMode": 0,
  "dMqtt":      { "sURL": "", "iPort": 1883, "sTopic": "results/data",
                  "sUsername": "admin", "sPassword": "admin", "sClientId": "recamera_1126b" },
  "dUart":      { "sPort": "ttyS4", "sPortDev": "/dev/shm/vserial1.sock" },
  "dHttp":      { "sUrl": "", "sToken": "" },
  "dWebsocket": { "iPort": 8123 },
  "dTemplate":  { "sDetection": "", "sClassification": "", "sSegmentation": "",
                  "sTracking": "", "sKeypoint": "" }
}
```

`dTemplate` 支持按任务类型自定义输出文本模板（留空走默认 JSON）。修改配置建议走官方 Web 界面或 entry.cgi 的 `notify` 域 API，而不是手改文件。

## 消费端：从 WS:8123 收结果

WS 服务只监听 `127.0.0.1:8123`（`notifiers/websocket_server.py:107-110`），外部访问必须经 nginx 路由 `/ws/inference/results`（`common_relay.conf:61-72`，带 `auth_request /_jwt_verify` JWT 鉴权）。

```python
# 设备本机消费（token 鉴权开启时需拼 ?token=<JWT>，见 websocket_server.py:146）
import asyncio, websockets, json
async def main():
    async with websockets.connect("ws://127.0.0.1:8123/") as ws:   # 已验证（2026-08-10，固件 V1.0.10 / kernel 6.1.157）：写 /var/tmp/notify → 本机 WS:8123 原样收到，链路成立
        async for msg in ws:
            print(json.loads(msg))
asyncio.run(main())
```

外部消费：`ws://<设备IP>/ws/inference/results`（实测 nginx 将 http 重定向到 https，须用 `wss://<设备IP>/ws/inference/results`），握手需携带有效 JWT。**已验证（2026-08-10，固件 V1.0.10 / kernel 6.1.157）**：设备上 `notify-server` 虽以 `--no-jwt-auth --no-static-token-auth` 运行（本机 8123 免鉴权），但 nginx `/ws/inference/results` 仍走 `auth_request /_jwt_verify` 强制鉴权，无 token 一律 `HTTP 401`。**携带方式实测：Cookie（`Cookie: token=<JWT>`）握手成功并原样收到注入结果；查询参数 `?token=<JWT>` 无效（仍 401）。** 端到端已跑通：设备写 `/var/tmp/notify` → notify-server → nginx WS 代理 → 外部 wsl 客户端（带 JWT Cookie）收到带自定义 label 的检测结果。JWT 获取方式见《rkipc RPC 现状》一文的鉴权节。

收到的消息是 notify-server 解析 protobuf 后的 JSON 文本（`core/inference_parser.py`），不是原始 protobuf。**WS 是单向下行**：服务端对客户端上行消息只做 echo（`websocket_server.py:197-200`），不能经 WS 注入结果。

## 边界与限制

- 不上 OSD、不进录像（见开头定位声明）。
- 无鉴权、无来源标识：当前 proto 没有 `source_id`，消费端无法区分结果来自官方推理还是你的进程——可暂用 `model_id` 约定区分。
- 官方内建推理与你共用同一分发链，高频注入会稀释消费端带宽；后续固件将加全局限速（超限丢弃）。
- MQTT/HTTP/UART 同一时刻只有一路生效（`iMode` 单选）。

## 故障排查

| 现象 | 排查 |
|---|---|
| connect 报 `No such file` | notify-server 未运行：`ls -l /var/tmp/notify`；init 脚本为 S49notify（`notify_server.py:206` 注释） |
| 发送成功但 WS 收不到 | 长度头必须小端 4 字节且与 payload 精确一致（`socket_server.py:172-186`，不完整帧会被丢弃并断连）；看 `/tmp/notify_server.log`（`notify_server.py:165`）。**已验证（2026-08-10）**：发一条长度头大于实际 payload 的畸形帧，notify-server 不崩溃，该连接被丢弃后紧接着的正常帧仍能正常分发到 WS（服务保持存活） |
| protobuf 解析失败日志 | proto 版本不匹配：用随固件发布的 inference.proto 重新生成 |
| 外部 WS 连接 401 | JWT 缺失/过期；先本机 `ws://127.0.0.1:8123/` 验证数据面，再排查鉴权 |

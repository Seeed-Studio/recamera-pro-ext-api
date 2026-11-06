# motion-daemon — reCamera Pro 移动检测守护进程

不进 App Center 的极限精简版：直读 Y 平面、纯 numpy、WebSocket 长连接上报
"是否运动"、HTTP 一次性接口调参。零第三方依赖（recamera_ext / numpy 设备自带）。

## 架构

```
FrameSource(fps_divisor=N)      源级降帧（默认 30fps→5fps）
  → frame.array                 NV12 Y 平面零拷贝（无 RGB 转换）
  → numpy 两段式块均值           720p→160×90，~6ms/帧
  → 基准窗口 vs 当前窗口 平方差   motion_core.py
  → 状态机（沿触发 + 滞回防抖）
      ├─ WebSocket 长连接推送    ws_notify.py（后台线程，断线重连，主线程零阻塞）
      └─ HTTP 参数接口 :8099     http_control.py（GET/POST /config, GET /state）
```

## 文件

| 文件 | 作用 |
|---|---|
| `motion_daemon.py` | 主程序（取帧、状态机、配置管理） |
| `motion_core.py` | 检测算法（窗口化平方差，可离线单测） |
| `ws_notify.py` | 最小 WS 客户端（RFC6455，纯标准库） |
| `http_control.py` | 参数 HTTP 接口（stdlib http.server） |
| `ws_sink.py` | **服务器端参考实现**（收推送并打印） |
| `config.json` | 启动参数（也可用 HTTP 接口在线改） |
| `test_motion_core.py` / `test_net.py` | 离线测试（开发机 `pytest`） |

## 部署

```bash
python3 dev_ssh.py run "mkdir -p /userdata/local/motion-daemon"
python3 dev_ssh.py push motion-daemon/motion_daemon.py motion-daemon/motion_core.py \
    motion-daemon/ws_notify.py motion-daemon/http_control.py motion-daemon/config.json \
    /userdata/local/motion-daemon
# 启动
python3 dev_ssh.py run "cd /userdata/local/motion-daemon && nohup python3 motion_daemon.py > daemon.log 2>&1 &"
```

## 对接你的服务器

1. 起服务（参考 `ws_sink.py`）：`python3 ws_sink.py 9000`
2. 设备上配置推送地址（HTTP 接口热生效，无需重启）：
   ```bash
   curl -X POST http://<设备IP>:8099/config \
        -d '{"ws_url": "ws://<服务器IP>:9000/motion"}'
   ```
3. 服务器将收到：
   - `{"type":"state","motion":true/false,"score":0.23,"changed_ratio":0.012,"changed_pixels":169,"ts":...}` — 仅状态翻转时
   - `{"type":"heartbeat", ...}` — 每 `heartbeat_sec` 一条（保活 + 在线检测）
   - 断线自动重连（退避 1s→30s），重连成功立即补发最新状态

## 参数在线调节（HTTP 一次性通信）

```bash
curl http://<设备IP>:8099/config                       # 查看当前参数
curl http://<设备IP>:8099/state                        # 查看当前运动状态
curl -X POST http://<设备IP>:8099/config \
     -d '{"sq_diff_threshold": 600, "min_changed_ratio": 0.01}'
# 返回 {"applied": {...}, "ignored": {...}, "restart_required": [...]}
# 合法项立即热生效并写回 config.json；sample_fps 需重启进程才作用于源端降帧
```

鉴权：`http_token` 非空时要求请求头 `Authorization: Bearer <token>`。
WS 侧同理用 `ws_token`。

| 参数 | 默认 | 含义 |
|---|---|---|
| `sq_diff_threshold` | 400 | 平方差阈值（≈灰度差 20 的平方） |
| `min_changed_ratio` | 0.005 | 运动判定最小变化像素占比 |
| `motion_hold_ms` | 3000 | 运动消失后的保持时间（防抖） |
| `baseline_frames` | 5 | 基准画面长度（帧） |
| `current_frames` | 2 | 当前画面长度（帧） |
| `baseline_gap_ms` | 1000 | 基准与当前画面的时间间隔 |
| `sample_fps` | 5 | 分析采样率（重启生效） |
| `analysis_width` | 160 | 分析分辨率宽度 |
| `heartbeat_sec` | 30 | 心跳间隔（秒） |
| `ws_url` / `ws_token` | — | WS 推送目标 / 鉴权 |
| `http_host` / `http_port` / `http_token` | 0.0.0.0 / 8099 / 空 | 参数接口 |

## 停止 / 卸载

```bash
python3 dev_ssh.py run "pkill -f motion_daemon"
python3 dev_ssh.py run "rm -rf /userdata/local/motion-daemon"
```

注意：`nohup` 方式不落盘开机自启；需要重启后自动拉起时再加 init 脚本。

#!/usr/bin/env python3
"""motion_daemon -- reCamera Pro 移动检测系统服务（固件内置，不进 App Center）。

数据流（极限省资源路线）：

  recamera_ext.FrameSource(fps_divisor=N)   源级降帧，直接省掉多余帧
    -> frame.array                          NV12 的 Y 平面（零拷贝灰度，
                                            全程无 NV12->RGB 转换）
    -> numpy 两段式块均值降采样到 160 宽小图   （只在采样帧上，~6ms）
    -> 基准窗口 vs 当前窗口 平方差对比         （motion_core.py, 小图运算）
    -> MQTT Discovery 上报 Home Assistant     （ha_mqtt.py, 复用 kit MqttSink）
    -> appmgr motiondetect.forwarder 桥接进平台 Result Hub（前端输出面板）

参数设置：App Center 配置页 / 平台配置 API，或 HTTP 一次性通信
（http_control.py，默认 :8099，供本机调试与健康探活）：
  curl http://<设备>:8099/config                       # 查看当前参数
  curl -X POST http://<设备>:8099/config \\
       -d '{"sq_diff_threshold": 600, "min_changed_ratio": 0.01}'
  # 合法项立即热生效并写回 config.json；sample_fps 需重启进程才作用于源端降帧
也保留 改 config.json + kill -HUP <pid> 的文件热载方式（两者等价）。

运行：python3 motion_daemon.py --config /path/to/config.json
停止：SIGTERM / SIGINT 优雅退出。
依赖（设备自带，无需安装）：recamera_ext、numpy。
"""
import argparse
import json
import os
import signal
import sys
import threading
import time

import numpy as np                      # noqa: F401  (平台自带；显式失败更友好)
from recamera_ext import FrameConfig, FrameSource

from motion_core import MotionCore, retention_seconds
from http_control import ControlServer
from ha_mqtt import HaMqttBridge

# config.json / HTTP POST 中允许出现的键（类型/范围校验，非法值拒绝并返回）
CONFIG_SPEC = {
    # 判定
    "sq_diff_threshold":  ("float", 1.0, 65025.0),
    "min_changed_ratio":  ("float", 0.0, 1.0),
    "motion_hold_ms":     ("int",   0,   600000),
    # 基准/当前窗口
    "baseline_frames":    ("int",   1,   240),
    "current_frames":     ("int",   1,   120),
    "baseline_gap_ms":    ("int",   0,   600000),
    # 采样
    "sample_fps":         ("float", 0.2, 30.0),
    "analysis_width":     ("int",   32,  640),
    # 上报
    "heartbeat_sec":      ("int",   5,   3600),
    # 参数接口
    "http_host":          ("str",   None, None),
    "http_port":          ("int",   0,   65535),   # 0 = 关闭参数接口
    "http_token":         ("str",   None, None),
    # HA / MQTT 上报（kit MqttSink，空 host = 关闭）
    "mqtt_host":          ("str",   None, None),
    "mqtt_port":          ("int",   1,   65535),
    "mqtt_user":          ("str",   None, None),
    "mqtt_pass":          ("str",   None, None),
}

# 改了之后需要重启进程才真正生效的参数（HTTP 返回里会标注 restart_required）
RESTART_KEYS = {"sample_fps"}        # fps_divisor 只能在打开帧源时设置

DEFAULTS = {
    "sq_diff_threshold": 400.0,     # 平方差阈值（≈灰度差 20 的平方）
    "min_changed_ratio": 0.005,     # 运动判定最小变化像素占比
    "motion_hold_ms": 3000,         # 运动消失后的保持时间（防抖）
    "baseline_frames": 5,           # 基准画面长度（帧）
    "current_frames": 2,            # 当前画面长度（帧）
    "baseline_gap_ms": 1000,        # 基准与当前画面的时间间隔
    "sample_fps": 5.0,              # 分析采样率
    "analysis_width": 160,          # 分析分辨率宽度
    "heartbeat_sec": 30,            # 心跳间隔（秒）
    "http_host": "0.0.0.0",
    "http_port": 8099,
    "http_token": "",               # 建议生产环境设置
    "mqtt_host": "",                # 例：192.168.1.10（HA 的 MQTT broker）
    "mqtt_port": 1883,
    "mqtt_user": "",
    "mqtt_pass": "",
}

# 设备主码流帧率（只用于把 sample_fps 换算成 fps_divisor；--camera-fps 可覆盖）
CAMERA_FPS = 30.0


def log(msg):
    print(f"[motion-daemon {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _coerce(spec, key, val):
    """按 CONFIG_SPEC 校验并转换单个值；非法返回 None。"""
    kind, lo, hi = spec
    try:
        val = int(val) if kind == "int" else \
              float(val) if kind == "float" else str(val)
    except (TypeError, ValueError):
        return None
    if kind in ("int", "float") and not (lo <= val <= hi):
        return None
    return val


def load_config(path):
    """读 config.json 并校验；文件问题/非法项只告警，默认值兜底。"""
    cfg = dict(DEFAULTS)
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        log(f"config {path} 不存在，使用内置默认值")
        return cfg
    except (OSError, json.JSONDecodeError) as exc:
        log(f"config 读取失败（沿用默认值）: {exc}")
        return cfg
    if not isinstance(raw, dict):
        log("config 顶层必须是 JSON 对象（沿用默认值）")
        return cfg
    for key, val in raw.items():
        spec = CONFIG_SPEC.get(key)
        if spec is None:
            log(f"config 未知键 {key!r}（忽略）")
            continue
        coerced = _coerce(spec, key, val)
        if coerced is None:
            log(f"config {key}={val!r} 非法或超范围（忽略）")
            continue
        cfg[key] = coerced
    return cfg


class MotionDaemon:
    def __init__(self, config_path, camera_fps):
        self.config_path = config_path
        self.camera_fps = float(camera_fps)
        self.cfg = load_config(config_path)
        self._cfg_lock = threading.Lock()       # 仅保护整体替换，读用原子引用
        self._reload = False
        self._stop = False

        self.core = MotionCore(analysis_width=int(self.cfg["analysis_width"]))
        self.mqtt = HaMqttBridge(self.cfg, log=log)
        self.http = self._make_control(self.cfg)
        self._active = False
        self._last_motion_pts = 0.0
        self._last_sample_pts = float("-inf")
        self._last_notify = 0.0
        self._last_result = {"score": 0.0, "changed_ratio": 0.0,
                             "changed_pixels": 0}

    def _make_control(self, cfg):
        return ControlServer(cfg["http_host"], int(cfg["http_port"]),
                             cfg["http_token"],
                             get_config=lambda: dict(self.cfg),
                             update_config=self.update_config,
                             get_state=self._state_snapshot, log=log)

    def _state_snapshot(self):
        with self._cfg_lock:
            active = self._active
        return {"motion": active, "ts": round(time.time(), 3),
                **{k: self._last_result[k] for k in
                   ("score", "changed_ratio", "changed_pixels")}}

    # -- 参数热更新（HTTP / SIGHUP 共用同一条路径） ----------------------- #
    def update_config(self, partial: dict) -> dict:
        """校验并应用部分配置，持久化到 config.json。返回应用报告。"""
        applied, ignored = {}, {}
        for key, val in partial.items():
            spec = CONFIG_SPEC.get(key)
            coerced = _coerce(spec, key, val) if spec else None
            if spec is None or coerced is None:
                ignored[key] = val
            else:
                applied[key] = coerced
        if not applied:
            return {"applied": applied, "ignored": ignored,
                    "restart_required": []}

        with self._cfg_lock:
            new_cfg = dict(self.cfg)
            new_cfg.update(applied)
            old = self.cfg
            self.cfg = new_cfg                       # 原子替换，主循环下帧生效

        if int(new_cfg["analysis_width"]) != int(old["analysis_width"]):
            self.core = MotionCore(analysis_width=int(new_cfg["analysis_width"]))
            log("analysis_width 变更：检测核心与基准缓冲已重建")
        mqtt_keys = ("mqtt_host", "mqtt_port", "mqtt_user", "mqtt_pass")
        if any(new_cfg[k] != old[k] for k in mqtt_keys):
            self.mqtt.close()
            self.mqtt = HaMqttBridge(new_cfg, log=log)
            log(f"MQTT/HA 目标已更新：{new_cfg['mqtt_host'] or '(关闭)'}")
        restart_required = sorted(k for k in applied if k in RESTART_KEYS
                                  and old.get(k) != new_cfg.get(k))
        if any(k in applied and old.get(k) != new_cfg.get(k)
               for k in ("http_host", "http_port", "http_token")):
            restart_required.append("http_*")        # 监听端口不支持热迁移

        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(new_cfg, f, ensure_ascii=False, indent=2)
        except OSError as exc:
            log(f"config 持久化失败（运行中仍生效）: {exc}")

        log(f"配置热更新：{applied}"
            + (f"（重启生效：{restart_required}）" if restart_required else ""))
        return {"applied": applied, "ignored": ignored,
                "restart_required": restart_required}

    # -- 信号 ------------------------------------------------------------ #
    def install_signals(self):
        def on_hup(_s, _f):
            self._reload = True

        def on_term(_s, _f):
            self._stop = True

        signal.signal(signal.SIGHUP, on_hup)
        signal.signal(signal.SIGTERM, on_term)
        signal.signal(signal.SIGINT, on_term)

    def _maybe_reload(self):
        if not self._reload:
            return
        self._reload = False
        self.update_config(load_config(self.config_path))

    # -- 主循环 ---------------------------------------------------------- #
    def run(self):
        cfg = self.cfg
        divisor = max(1, round(self.camera_fps / float(cfg["sample_fps"])))
        interval_s = 1.0 / max(0.2, float(cfg["sample_fps"]))
        log(f"启动：sample_fps={cfg['sample_fps']} (fps_divisor={divisor}), "
            f"thresh={cfg['sq_diff_threshold']}, "
            f"ratio={cfg['min_changed_ratio']}, "
            f"baseline={cfg['baseline_frames']}f gap={cfg['baseline_gap_ms']}ms")

        self.http.start()
        log(f"MQTT/HA={'-> ' + cfg['mqtt_host'] if self.mqtt.enabled else 'OFF'}")
        try:
            with FrameSource(FrameConfig(fps_divisor=divisor),
                             timeout_ms=5000) as src:
                log(f"取帧成功：{src.width}x{src.height}")
                for frame in src:
                    if self._stop:
                        break
                    self._maybe_reload()
                    cfg = self.cfg

                    now = float(frame.pts_us) / 1e6   # 源级降帧 + pts 节流双保险
                    if now - self._last_sample_pts < interval_s * 0.9:
                        continue
                    self._last_sample_pts = now

                    # Y 平面零拷贝视图 -> 小图副本（新数组，跨帧安全）
                    small = self.core.downscale(frame.array)
                    self.core.push(now, small)
                    self.core.trim(now, retention_seconds(
                        1000.0 / float(cfg["sample_fps"]),
                        cfg["baseline_gap_ms"], cfg["baseline_frames"],
                        cfg["current_frames"]))

                    result = self.core.evaluate(
                        now,
                        baseline_gap_ms=cfg["baseline_gap_ms"],
                        baseline_frames=cfg["baseline_frames"],
                        current_frames=cfg["current_frames"],
                        sq_diff_threshold=cfg["sq_diff_threshold"],
                        min_changed_ratio=cfg["min_changed_ratio"],
                    )
                    if result is None:
                        continue                      # 预热期
                    self._step(result, now)
        finally:
            self.http.stop()
            self.mqtt.close()
        log("帧源关闭，退出")

    # -- 状态机 + 上报 ---------------------------------------------------- #
    def _step(self, result, now):
        cfg = self.cfg
        self._last_result = result
        active = bool(result["active"])
        if active:
            self._last_motion_pts = now
        elif self._active and (now - self._last_motion_pts) * 1000.0 \
                < float(cfg["motion_hold_ms"]):
            active = True                             # 滞回防抖

        if active != self._active:
            with self._cfg_lock:
                self._active = active
            log(f"运动 {'开始' if active else '结束'} "
                f"score={result['score']:.2f} "
                f"ratio={result['changed_ratio']:.4f} "
                f"pixels={result['changed_pixels']}")
            self._report("state", active, result, now, edge=active)
            self._last_notify = now
        elif now - self._last_notify >= float(cfg["heartbeat_sec"]):
            # 心跳（运动中/空闲都有）：保活长连接 + 让服务器确认 daemon 在线
            self._report("heartbeat", active, result, now, edge=False)
            self._last_notify = now

    def _report(self, msg_type, active, result, now, *, edge):
        """同一份状态同时进 WS 长连接（自有服务器）和 MQTT/HA Discovery。"""
        scalars = {"score": result["score"],
                   "changed_ratio": result["changed_ratio"],
                   "changed_pixels": result["changed_pixels"]}
        events = []
        if edge and active:
            events.append({"kind": "motion", **scalars})
        events.append({"kind": "motion_state", "motion": bool(active),
                       **scalars})
        self.mqtt.emit(events, now)


def main():
    ap = argparse.ArgumentParser(description="reCamera Pro motion daemon")
    ap.add_argument("--config", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "config.json"))
    ap.add_argument("--camera-fps", type=float, default=CAMERA_FPS,
                    help="相机主码流帧率（默认 %(default)s），"
                         "用于把 sample_fps 换算为 fps_divisor")
    args = ap.parse_args()

    daemon = MotionDaemon(args.config, args.camera_fps)
    daemon.install_signals()
    daemon.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())

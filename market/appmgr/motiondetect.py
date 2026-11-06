"""motiondetect — motion-detect 系统应用在 AppMgr 内的适配层。

与 builtin（内建 AI 推理）/ acousticslab 同级的"固件系统应用"：
检测本体是 rootfs 常驻服务 motion-detectd（/etc/init.d/S95motion-detectd，
纯 CPU、不碰 NPU、不参与单活互斥），本模块把它以系统应用身份呈现进
App Center：

  - do_v1_apps 列表（server.py 调 motiondetect.snapshot()）
  - POST /api/appMgr/start|stop（平台启停 API）
  - GET/POST /api/appMgr/config（平台配置 API，schema 驱动 UI；写入
    /userdata 配置后 SIGHUP 热载，结构性参数 restart）
  - 开机 reconcile：按持久化的期望状态恢复（"出厂默认加载"由 OEM init
    脚本自启承担；用户在 App Center 停止后重启不再拉起）

本模块不做任何安装/签名事务：它不是可安装包，永不进入包管线。
"""
import json
import os
import signal
import hashlib
import stat
import subprocess
import tempfile
import threading
import time
import urllib.request

from . import paths

MD_ID = "motion-detect"
INIT_SCRIPT = "/etc/init.d/S95motion-detectd"
PIDFILE = "/var/run/motion-detectd.pid"
CONFIG_PATH = "/userdata/local/motion-detect/config.json"
HEALTH_URL = "http://127.0.0.1:8099/state"
_DESIRED_PATH = os.path.join(paths.APPMGR_DIR, "motion-detect.json")

_LIFETIME_TIMEOUT = 20.0
_CONFIRM_TIMEOUT = 10.0
_CONFIRM_POLL = 0.2

_desired_lock = threading.Lock()


class MotionDetectError(Exception):
    """Control-plane failure of the motion-detect system service."""


# --------------------------------------------------------------------------- #
# 配置 schema（UI 渲染 + 服务端校验）：唯一事实来源是 daemon 侧文件
# services/motion-detect/config_schema.json，随固件装进
# /usr/share/motion-detect/。daemon 的 CONFIG_SPEC/DEFAULTS/RESTART_KEYS
# 也从同一份 JSON 派生——改参数只改那个文件。
# --------------------------------------------------------------------------- #
def _load_config_schema() -> dict:
    repo_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             os.pardir, os.pardir,
                             "services", "motion-detect", "config_schema.json")
    candidates = (os.environ.get("RECAMERA_MOTION_DETECT_SCHEMA"),
                  "/usr/share/motion-detect/config_schema.json", repo_file)
    for path in candidates:
        if not path or not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as stream:
                return json.load(stream)
        except (OSError, ValueError) as exc:
            print(f"[appmgr] motion-detect schema {path} unreadable: {exc}",
                  flush=True)
    print("[appmgr] motion-detect config_schema.json not found; "
          "config UI will be empty", flush=True)
    return {"groups": []}


CONFIG_SCHEMA = _load_config_schema()


def _schema_items():
    for group in CONFIG_SCHEMA["groups"]:
        for item in group.get("items") or []:
            yield item


def _defaults():
    return {item["key"]: item.get("default") for item in _schema_items()}


def _validate(incoming: dict):
    """按 schema 校验部分更新。返回 (clean, errors, apply_modes)。"""
    spec = {item["key"]: item for item in _schema_items()}
    clean, errors, modes = {}, {}, set()
    for key, value in (incoming or {}).items():
        item = spec.get(key)
        if item is None:
            errors[key] = "unknown key"
            continue
        kind = item.get("type")
        try:
            if kind == "integer":
                value = int(value)
            elif kind == "number":
                value = float(value)
            elif kind == "boolean":
                value = bool(value)
            else:
                value = str(value)
        except (TypeError, ValueError):
            errors[key] = "invalid type"
            continue
        lo, hi = item.get("min"), item.get("max")
        if kind in ("integer", "number") and lo is not None and hi is not None \
                and not (lo <= value <= hi):
            errors[key] = f"out of range [{lo}, {hi}]"
            continue
        clean[key] = value
        modes.add(item.get("apply") or "live")
    return clean, errors, modes


def _read_config() -> dict:
    try:
        with open(CONFIG_PATH, "rb") as stream:
            data = json.loads(stream.read(256 * 1024).decode("utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def _write_config(cfg: dict) -> None:
    parent = os.path.dirname(CONFIG_PATH)
    os.makedirs(parent, mode=0o755, exist_ok=True)
    payload = (json.dumps(cfg, ensure_ascii=False, indent=2) + "\n").encode()
    fd, temporary = tempfile.mkstemp(prefix=".motion-detect-", dir=parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, CONFIG_PATH)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


# --------------------------------------------------------------------------- #
# 进程状态
# --------------------------------------------------------------------------- #
def process_pid():
    try:
        with open(PIDFILE, "r", encoding="ascii") as stream:
            pid = int(stream.read().strip())
    except (OSError, ValueError):
        return None
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as stream:
            if b"motion_daemon" in stream.read():
                return pid
    except OSError:
        pass
    return None


def is_running() -> bool:
    return process_pid() is not None


def health() -> bool:
    """HTTP 参数接口探活（daemon 正常运行即应答）。"""
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=1.5) as resp:
            return resp.status == 200
    except Exception:
        return False


def snapshot() -> dict:
    """do_v1_apps 的状态视图。available=False 表示该平台未携带本服务。"""
    available = os.path.exists(INIT_SCRIPT)
    pid = process_pid()
    enabled = desired_state() == "running"
    if not available:
        state = "unknown"
    elif pid is not None:
        state = "running"
    else:
        state = "stopped"
    return {"state": state, "available": available, "enabled": enabled,
            "pid": pid}


# --------------------------------------------------------------------------- #
# 期望状态持久化 + 开机恢复（与 acousticslab 同一约定）
# --------------------------------------------------------------------------- #
def desired_state() -> str:
    with _desired_lock:
        try:
            with open(_DESIRED_PATH, "rb") as stream:
                data = json.loads(stream.read(64 * 1024).decode("utf-8"))
        except (OSError, ValueError):
            return "stopped"          # 出厂默认关闭；用户在 App Center 启动后记住
    if isinstance(data, dict) and data.get("desired") in ("running", "stopped"):
        return data["desired"]
    return "stopped"


def _write_desired(desired: str) -> None:
    if desired not in ("running", "stopped"):
        raise ValueError("invalid desired state %r" % desired)
    os.makedirs(paths.APPMGR_DIR, mode=0o755, exist_ok=True)
    payload = (json.dumps({"version": 1, "desired": desired}) + "\n").encode()
    with _desired_lock:
        fd, temporary = tempfile.mkstemp(prefix=".motion-detect-",
                                         dir=paths.APPMGR_DIR)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, _DESIRED_PATH)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def reconcile() -> None:
    """appmgr 启动时调用一次：按持久化意图恢复。绝不抛异常阻塞服务启动。"""
    if not os.path.exists(INIT_SCRIPT):
        return                      # 未携带本服务的平台（如开发机）
    desired = desired_state()
    try:
        if desired == "stopped" and is_running():
            print("[appmgr] motion-detect reconcile: enforcing stopped",
                  flush=True)
            stop()
        elif desired == "running" and not is_running():
            print("[appmgr] motion-detect reconcile: restoring running",
                  flush=True)
            start()
    except MotionDetectError as e:
        print("[appmgr] motion-detect reconcile failed (desired=%s): %s"
              % (desired, e), flush=True)


# --------------------------------------------------------------------------- #
# 生命周期（经由随固件发布的 init 脚本；绝不做第二个 supervisor）
# --------------------------------------------------------------------------- #
def _initctl(action: str) -> str:
    try:
        proc = subprocess.run(
            [INIT_SCRIPT, action], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=_LIFETIME_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise MotionDetectError(f"init script {INIT_SCRIPT} {action} "
                                f"failed to run: {e}")
    output = proc.stdout.decode("utf-8", "replace") if proc.stdout else ""
    if proc.returncode != 0:
        raise MotionDetectError(f"init script {INIT_SCRIPT} {action} exited "
                                f"{proc.returncode}: {output.strip()[:300]}")
    return output


def _wait_for(predicate, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(_CONFIRM_POLL)
    raise MotionDetectError(f"{what} was not confirmed within {timeout:.1f}s")


def start() -> dict:
    print("[appmgr] motion-detect start requested", flush=True)
    _write_desired("running")
    output = _initctl("start")
    try:
        _wait_for(lambda: is_running() and health(), _CONFIRM_TIMEOUT,
                  "motion-detectd start")
    except MotionDetectError:
        if not is_running():
            raise
        print("[appmgr] motion-detectd running but health probe failed",
              flush=True)
    return {"started": True, "output": output.strip()[-300:]}


def stop() -> dict:
    print("[appmgr] motion-detect stop requested", flush=True)
    _write_desired("stopped")
    output = _initctl("stop")
    _wait_for(lambda: not is_running(), _CONFIRM_TIMEOUT,
              "motion-detectd stop")
    return {"stopped": True, "stop_confirmed": True,
            "output": output.strip()[-300:]}


def restart() -> dict:
    stopped = stop()
    started = start()
    return {"restarted": True, "stopped": stopped, "started": started}


# --------------------------------------------------------------------------- #
# 日志读取（App Center 日志页）
# --------------------------------------------------------------------------- #
def read_log_tail(tail: int = 200) -> list:
    """供 do_v1_logs：读 daemon 日志末尾若干行（/userdata 下的滚动日志）。"""
    path = "/userdata/local/motion-detect/daemon.log"
    try:
        with open(path, "rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - 256 * 1024))
            text = stream.read().decode("utf-8", "replace")
    except OSError:
        return []
    return text.splitlines()[-int(tail):]


# --------------------------------------------------------------------------- #
# 平台配置 API（schema 驱动 App Center 配置页；热载 SIGHUP / 重启）
# --------------------------------------------------------------------------- #
def get_config() -> dict:
    values = _defaults()
    values.update({k: v for k, v in _read_config().items() if k in values})
    return {"id": MD_ID, "config_schema": CONFIG_SCHEMA, "values": values,
            "defaults": _defaults()}


def set_config(incoming: dict) -> dict:
    if not isinstance(incoming, dict):
        raise ValueError("config must be an object")
    clean, errors, modes = _validate(incoming)
    if errors:
        raise ValueError("; ".join(f"{k}: {v}" for k, v in errors.items()))
    if not clean:
        return {"id": MD_ID, "saved": False, "applied": "none",
                "restarted": False, "reloaded": False, "noop": True,
                "changed_keys": [], "config": get_config()["values"]}

    current = _read_config()
    changed = sorted(k for k, v in clean.items() if current.get(k) != v)
    if not changed:
        return {"id": MD_ID, "saved": False, "applied": "none",
                "restarted": False, "reloaded": False, "noop": True,
                "changed_keys": [], "config": get_config()["values"]}
    current.update(clean)
    _write_config(current)

    restarted = reloaded = False
    if is_running():
        if "restart" in modes:
            restart()
            restarted = True
        else:
            pid = process_pid()
            if pid is not None:
                os.kill(pid, signal.SIGHUP)     # daemon 热重读 config.json
                reloaded = True
    mode = "restart" if restarted else ("live" if reloaded else "none")
    return {"id": MD_ID, "saved": True, "applied": mode,
            "restarted": restarted, "reloaded": reloaded, "noop": False,
            "changed_keys": changed, "config": get_config()["values"]}


# --------------------------------------------------------------------------- #
# Result Hub 桥接（与 acousticslab.ResultForwarder 同模式）：
# daemon HTTP /state -> appmgr 进程内 -> Hub system envelope
#（source.kind="system", trust="in-process"）
# --------------------------------------------------------------------------- #
class ResultForwarder:
    """轮询 motion-detectd 的 /state，把运动状态发布进 Result Hub。

    运动是状态量不是帧：发布纪律为 变化即发 + 周期心跳（而非每帧）。
    daemon 消失时补发一条 motion=False 终态，防止 Hub 残留“运动中”。
    """

    def __init__(self, *, publish, state_url: str = HEALTH_URL,
                 poll_sec: float = 1.0, heartbeat_sec: float = 10.0):
        self._publish = publish
        self._state_url = state_url
        self._poll_sec = float(poll_sec)
        self._heartbeat_sec = float(heartbeat_sec)
        self._stop = threading.Event()
        self._thread = None
        self._seq = 0
        self._last_key = None          # (motion, daemon_running) 去重
        self._last_publish = 0.0
        self._stats = {"published": 0, "poll_errors": 0, "last_error": ""}

    def start(self) -> "ResultForwarder":
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, daemon=True, name="motiondetect-forwarder")
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    def status(self) -> dict:
        return dict(self._stats)

    def _poll_state(self):
        try:
            with urllib.request.urlopen(self._state_url, timeout=1.5) as resp:
                return json.load(resp)
        except Exception:
            return None

    def _publish_state(self, motion, score, changed_ratio, changed_pixels,
                       ts, daemon_running):
        self._seq += 1
        scalars = {"motion": bool(motion), "score": float(score),
                   "changed_ratio": float(changed_ratio),
                   "changed_pixels": int(changed_pixels)}
        # Hub 的 system 归一化按 task_type_name 取 payload["motion"]["entries"]
        # 进 canonical results[]——这是前端“输出数据”面板实际渲染的字段。
        # 运动中给出一条结果项（无框）；空闲时为空（诚实的“无运动”）。
        entries = ([{"class_id": 0, "class_name": "motion",
                     "score": round(float(score), 4),
                     "changed_ratio": float(changed_ratio),
                     "changed_pixels": int(changed_pixels)}]
                   if motion else [])
        payload = {
            "seq": self._seq,
            "timestamp_ms": int(time.time() * 1000),
            "pts_us": int(float(ts or 0) * 1_000_000),
            "task_type_name": "motion",
            "stream_id": MD_ID,
            "motion": {"entries": entries, "count": len(entries)},
            "summary": {**scalars, "daemon_running": bool(daemon_running)},
            "events": [{"kind": "motion_state", **scalars}],
        }
        self._publish(payload, {"id": MD_ID, "trust": "in-process"})
        self._stats["published"] += 1

    def _run(self) -> None:
        while not self._stop.is_set():
            state = self._poll_state()
            now = time.monotonic()
            if state is None:
                self._stats["poll_errors"] += 1
                key = (None, False)
            else:
                key = (bool(state.get("motion")), True)
            # 变化即发 / 心跳周期发 / daemon 消失补发一次离线终态
            if key != self._last_key or \
                    now - self._last_publish >= self._heartbeat_sec:
                try:
                    if state is None:
                        self._publish_state(False, 0.0, 0.0, 0, 0, False)
                    else:
                        self._publish_state(
                            state.get("motion"), state.get("score"),
                            state.get("changed_ratio"),
                            state.get("changed_pixels"), state.get("ts"),
                            True)
                    self._last_key = key
                    self._last_publish = now
                except Exception as exc:
                    self._stats["last_error"] = f"publish: {exc}"
            self._stop.wait(self._poll_sec)


# --------------------------------------------------------------------------- #
# App Center 呈现
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# 图标（固件内置，经 /api/appMgr/icon 供前端应用卡使用）
# --------------------------------------------------------------------------- #
ICON_PATH = "/usr/share/motion-detect/icon.png"
ICON_MEDIA_TYPE = "image/png"


def icon_info():
    """do_icon/_icon_info_cached 的系统应用分支：固定 rootfs 路径，同样受
    尺寸上限约束；返回与包图标一致的元数据结构。"""
    try:
        st = os.stat(ICON_PATH)
        if not stat.S_ISREG(st.st_mode) or st.st_size > paths.MAX_ICON_BYTES:
            return None
        with open(ICON_PATH, "rb") as stream:
            data = stream.read()
    except OSError:
        return None
    return {"path": ICON_PATH, "relative_path": "icon.png",
            "media_type": ICON_MEDIA_TYPE, "strict_media": True,
            "sha256": hashlib.sha256(data).hexdigest()}


def icon_bytes():
    info = icon_info()
    if info is None:
        raise FileNotFoundError("motion-detect has no bundled icon")
    with open(info["path"], "rb") as stream:
        return stream.read(), info["media_type"]


def manifest() -> dict:
    """系统应用描述。永不进入包/签名管线。"""
    return {
        "id": MD_ID,
        "name": "Motion Detection",
        "name_zh": "移动检测",
        "type": "system",
        "scene": "system",
        "scene_zh": "系统",
        "version": "firmware",
        "author": "reCamera (firmware)",
        "description": "Firmware-resident CPU motion detection "
                       "(motion-detectd): windowed squared-difference "
                       "analysis on the luma plane, motion state pushed over "
                       "WebSocket and reported to Home Assistant via MQTT "
                       "Discovery. Runs alongside the built-in AI.",
        "description_zh": "固件内置纯 CPU 移动检测（motion-detectd）："
                          "Y 平面窗口化平方差分析，运动状态经 WebSocket 推送、"
                          "MQTT Discovery 上报 Home Assistant。"
                          "与内建 AI 推理并行运行，互不干扰。",
        "config_schema": CONFIG_SCHEMA,
    }

#!/usr/bin/env python3
"""inspection-assembly —— reCamera Pro 官方应用包的入口 App。

**走官方那一套，不自建守护进程。** Pro 上一个应用就是 `market/appmgr` 装的一个
tar.gz：`installer.py` 收包验签、`supervisor.py` 托管进程、`activate` 单活切换、
S94 开机恢复上次 active。自写 init 脚本或看门狗的形态，appmgr 的 list 里看不见、
开机不恢复、也不参与单活切换。所以这里只有一个 `App` 子类，由 kit 的 `run_app`
拉起。

**判定链路一行都不在这里写。** 跑的是 `core_inspection.app.InspectionApp`——
Orin(TensorRT) 与 Pi(Hailo) 两个运行镜像的同一个入口类：同一个采集循环、同一个
`VerdictEngine`、同一份 `contracts/mqtt-event.schema.json` 校验、同一张
`contracts/MODBUS.md` 的寄存器表、同一个 `/healthz`。这个文件只做三件事：

    1. 把 appmgr 的 `config.json`（manifest `config_schema`）翻成 core 的配置形状；
    2. 用 kit 的 `App` 生命周期把 `InspectionApp` 的主循环挂进 supervisor；
    3. 把 SIGHUP 热更（`apply:"live"` 的判定阈值）转给正在跑的规则引擎。

    ┌────────────────────────────────────────────────────────────────────┐
    │ kit.run → InspectionAssemblyApp.run() → core_inspection.app        │
    │   sources[kind=rtsp]      ffmpeg 解 rkipc H.265 子码流              │
    │   backends.rknn           YOLOX-tiny INT8 @ RV1126B NPU            │
    │   core_inspection.rules   缺陷计数 → OK/NG 判定                     │
    │   core_inspection.modbus_server  HR 0-11 原子写 → 线圈 0/1          │
    │   core_inspection.publisher      inspection/{stream_id}/results     │
    │   core_inspection.webui          /healthz /metrics                  │
    └────────────────────────────────────────────────────────────────────┘

`needs_frames = False`：帧不由 kit 的相机通道来，而是 core 自己的采集线程经
ffmpeg 拉 RTSP 子码流（`rtsp_source.py`）。

**模型已内嵌（bundled）。** `.rknn` 随应用安装在本应用目录的 `models/` 下，manifest
的 `artifacts[]` 声明安装文件。配置里的 `model_path` 只有在用户明确填写时才覆盖
这个随包路径。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time

from kit.app import App, run_app

import rtsp_source

APP_ID = "inspection-assembly"

#: 应用包内随包模型目录。打包器把 manifest `artifacts[].file` 安装到这里。
DEFAULT_MODEL_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "models")  # bundled, self-contained
DEFAULT_MODEL_PATH = os.path.join(
    DEFAULT_MODEL_DIR, "yolox_tiny_deeppcb6.rv1126b.int8.calib64.normal.rknn")
LEGACY_MODEL_PATH = ("/userdata/local/models/inspection-assembly/"
                     "yolox_tiny_deeppcb6.rv1126b.int8.calib64.normal.rknn")

#: 产出该 rknn 的 ONNX 的 sha256（`models/SHA256SUMS`）。事件里的
#: `model.onnx_sha256` 报的是它——四个平台报同一个值，才能判断两块板跑的是不是
#: 同一个训练产物。rknn 自己的哈希由后端算出来进 `model.artifact_sha256`。
ONNX_SHA256 = "1149b92e6d627f9ed6e50357c84f150c00d3e567263de32f81b135896a739e5e"

DEFAULT_STREAM_URL = "rtsp://127.0.0.1:5554/live/1"

#: DeepPCB6 的六个缺陷类，顺序即模型的类别下标顺序，不能改。
CLASSES = ("open", "short", "mousebite", "spur", "copper", "pin-hole")

LOG = logging.getLogger(APP_ID)


def resolve_model_path(configured_path: str | None, *, bundled_path: str = DEFAULT_MODEL_PATH,
                       legacy_path: str = LEGACY_MODEL_PATH) -> str:
    """Treat cleared config as absent; migrate only the exact old default."""
    if configured_path is None or not str(configured_path).strip():
        return bundled_path
    configured_path = str(configured_path)
    if (configured_path == legacy_path
            and not os.path.isfile(configured_path)
            and os.path.isfile(bundled_path)):
        LOG.warning("legacy model path %s is absent; using bundled model %s",
                    configured_path, bundled_path)
        return bundled_path
    return configured_path


class InspectionAssemblyApp(App):
    """appmgr / supervisor 侧的外壳。业务全在 `core_inspection.app.InspectionApp`。"""

    id = APP_ID
    name = "Assembly Inspection"
    owns_loop = True
    needs_model = False        # 模型不由 kit 加载，见模块 docstring
    needs_frames = False       # 帧由 core 的采集线程经 ffmpeg 拉 RTSP
    postproc = "detect"

    def __init__(self) -> None:
        super().__init__()
        # config_schema 的默认值。`_bind_params` 在 start() 里按 config.json 覆盖，
        # apply:"live" 的项在 SIGHUP 时重新绑定到同名属性上。
        self.model_path = DEFAULT_MODEL_PATH
        self.stream_url = DEFAULT_STREAM_URL
        self.stream_id = "recamera-pro"
        self.assembly_file = ""
        self.max_fps = 10.0
        self.min_score = 0.35
        self.min_defects = 1
        self.consecutive_frames = 1
        self.web_port = 8129
        self.mqtt_host = ""
        self.mqtt_port = 1883
        self.mqtt_topic = ""
        self.mqtt_username = ""
        self.mqtt_password = ""
        self.modbus_enabled = True
        self.modbus_port = 502
        self.modbus_unit_id = 1

        self._service = None                 # core_inspection.app.InspectionApp
        self._stop = threading.Event()

    # -- 配置 ------------------------------------------------------------- #
    def setup(self, config) -> None:
        super().setup(config)
        # core 侧全用 `logging`，而 root logger 默认只放 WARNING 以上——不装这个，
        # 「连上哪个 broker、Modbus 绑没绑上、源出没出帧」这几条 INFO 一条都不会
        # 落进 supervisor 收的 app.log，验收时只能靠猜。
        if not logging.getLogger().handlers:
            logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                                format="%(asctime)s %(levelname)s %(name)s %(message)s")
        logging.getLogger().setLevel(logging.INFO)

    def _load_assembly(self) -> dict:
        """可选的缺件 / 尺寸配置。缺件比对要按工位标 ROI，通用表单填不了。"""
        path = str(self.assembly_file or "").strip()
        if not path:
            return {}
        if not os.path.isfile(path):
            # 填了路径却没有文件，是部署失误：不要静默退化成「只做检测」，
            # 否则面板上写着开了缺件比对而设备上并没有做。
            raise SystemExit("assembly_file points at %s which does not exist"
                             % path)
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise SystemExit("assembly_file must hold a JSON object")
        return {k: data[k] for k in ("assembly", "dimension") if k in data}

    def build_config(self) -> dict:
        """appmgr 的扁平 config.json → `contracts/config.schema.json` 的形状。

        翻译层留在这里而不是塞进 core：面板上一个开关叫什么、分几组，是 Pro 应用
        中心的事；core 的配置形状是四个平台共用的契约，不该被某一块板的 UI 带着走。
        """
        from core_inspection.config import infer_kind

        source = {
            "stream_id": str(self.stream_id),
            "uri": str(self.stream_url),
            # 通常是 rkipc 的 RTSP 子码流；指向本地文件时（回放验收）按文件采集。
            "kind": infer_kind(str(self.stream_url)),
            "fps": float(self.max_fps),
            "width": int(rtsp_source.DEFAULT_WIDTH),
            "height": int(rtsp_source.DEFAULT_HEIGHT),
        }
        if source["kind"] == "file":
            # 常驻应用喂本地文件（回放验收）时必须循环：不循环的话片子放完
            # `all sources exhausted`，进程正常退出，面板上看起来像应用自己崩了。
            source["loop"] = True
        source.update(self._load_assembly())
        config = {
            "sources": [source],
            "model": {
                "path": str(self.model_path),
                "accelerator": "rknn",
                "input_size": [640, 640],
                # 后处理在这个阈值上就把框丢了，判定引擎再也看不到它们：检测阈值
                # 必须不高于判定用的冻结阈值，否则面板上把 min_score 调到 0.25
                # 以下不会有任何效果，还会安静地多判 OK。
                "score_threshold": min(0.25, float(self.min_score)),
                "nms_threshold": 0.45,
                "onnx_sha256": ONNX_SHA256,
            },
            "classes": list(CLASSES),
            "rules": {
                "ng_classes": [],
                "min_score": float(self.min_score),
                "min_defects": max(1, int(self.min_defects)),
                "min_area": 0.0,
                "consecutive_frames": max(1, int(self.consecutive_frames)),
                "ng_on_defect": True,
                "ng_on_missing": True,
                "ng_on_extra": True,
                "ng_on_dimension": True,
            },
            "modbus": {
                "enabled": bool(self.modbus_enabled),
                "bind": "0.0.0.0",
                "port": int(self.modbus_port),
                "unit_id": int(self.modbus_unit_id),
                "heartbeat_interval_s": 1.0,
            },
            # 面板只绑回环：设备上的 nginx 是对外那一层，应用自己的状态口不该
            # 直接暴露在局域网上。
            "web": {"enabled": self.web_port > 0, "bind": "127.0.0.1",
                    "port": int(self.web_port), "preview_fps": 2},
        }
        if self.mqtt_host:
            config["mqtt"] = {
                "host": str(self.mqtt_host),
                "port": int(self.mqtt_port),
                "topic": (str(self.mqtt_topic)
                          or "inspection/{stream_id}/results"),
                "username": str(self.mqtt_username),
                "password": str(self.mqtt_password),
                "qos": 0,
                "retain": False,
            }
        return config

    def on_config_reload(self, config) -> None:
        """SIGHUP。

        manifest 里每一项都是 `apply:"restart"`，所以正常路径下 appmgr 会重启进程
        而不是走这里。保留这个分支是为了「万一被调用到也别把旧规则留着」——但它
        **不能**被当成热更能力来宣传：这个 App 自持主循环（`owns_loop = True`），
        跑在 `InspectionApp.run()` 里，从不调 kit 的 `tick()`，而 kit 的 SIGHUP
        处理器只置一个 `_reload_flag`、由 `tick()` 消费。声明 `apply:"live"` 的
        效果会是「面板上保存成功，设备继续用旧阈值」。
        """
        super().on_config_reload(config)
        service = self._service
        if service is None:
            return
        from core_inspection.rules import Rules

        # `Rules` 是 frozen dataclass：换一个新实例，不是原地改字段。判定器每个
        # stream 一个，都要换到，否则只有一路生效。
        rules = Rules.from_config(self.build_config()["rules"])
        service.rules = rules
        for engine in service.engines.values():
            engine.rules = rules
        LOG.info("rules reloaded: min_score=%s min_defects=%s consecutive=%s",
                 self.min_score, self.min_defects, self.consecutive_frames)

    def _install_stop_handlers(self) -> None:
        import signal

        def _stop(_signum, _frame):
            service = self._service
            if service is not None:
                service.request_stop()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _stop)
            except (ValueError, OSError):
                # 非主线程装不上处理器。装不上就退回默认动作，不是致命的。
                pass

    # -- 主循环 ----------------------------------------------------------- #
    def run(self) -> None:
        from core_inspection.config import apply_defaults, validate

        self.model_path = resolve_model_path(str(self.model_path))

        if not os.path.isfile(str(self.model_path)):
            raise SystemExit(
                "no model at %s. The configured .rknn path does not exist; "
                "the bundled model is at %s. "
                "配置的模型路径不存在；随包模型位于 %s。"
                % (self.model_path, DEFAULT_MODEL_PATH, DEFAULT_MODEL_PATH))

        raw = self.build_config()
        validate(raw)
        config = apply_defaults(raw)

        from core_inspection.app import InspectionApp

        # 取帧口换成 ffmpeg 子进程：设备自带的 OpenCV 打不开 RTSP。采集线程、
        # 丢帧策略、失败重试仍然是 core 的（`rtsp_source.py`）。
        self._service = InspectionApp(config, device_name=APP_ID,
                                      capture_factory=rtsp_source.capture_factory)
        # supervisor 停应用是 TERM→(15 s)→KILL。kit 不装 TERM 处理器，默认动作
        # 直接终止进程，NPU 句柄、Modbus 监听与采集线程都不走释放路径。接上它，
        # `stop` 就走正常收尾。
        self._install_stop_handlers()
        LOG.info("inspection-assembly starting: model=%s source=%s modbus=%s",
                 os.path.basename(str(self.model_path)), self.stream_url,
                 "%s:%s" % (self.modbus_port, self.modbus_unit_id)
                 if self.modbus_enabled else "off")
        try:
            summary = self._service.run()
        finally:
            self._service = None
        LOG.info("inspection-assembly stopped: %s", summary)

    def finish(self) -> None:
        service = self._service
        if service is not None:
            service.request_stop()
        super().finish()


if __name__ == "__main__":
    run_app(InspectionAssemblyApp())

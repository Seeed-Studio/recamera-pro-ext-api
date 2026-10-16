#!/usr/bin/env python3
"""waste-sorting -- reCamera Pro 官方应用包的入口 App。

**走官方那一套，不自建守护进程。** Pro 上一个应用就是 `market/appmgr` 装的一个
tar.gz：`installer.py` 收包、`supervisor.py` 托管进程、`activate` 单活切换、S94
开机恢复上次 active。自写 init 脚本或看门狗的形态，appmgr 的 list 里看不见、
开机不恢复、也不参与单活切换（`gotchas-recamera-pro` #21）。所以这里只有一个
`App` 子类，由 kit 的 `run_app` 拉起。

**推理链路一行都不在这里写。** 跑的是 `core_waste.app.WasteApp`——Pi(Hailo) 与
Orin(TensorRT) 两个运行镜像的同一个入口类：同一个采集/门控/发布循环、同一个
`core_waste.publish_gate.PublishGate`、同一份 `contracts/mqtt-event.schema.json`
校验、同一个 `/healthz`。这个文件只做三件事：

    1. 把 appmgr 的 `config.json`（manifest `config_schema`）翻成 core 的配置形状；
    2. 用 kit 的 `App` 生命周期把 `WasteApp.run()` 挂进 supervisor；
    3. 把 SIGHUP 热更（`apply:"live"` 的门控参数）转给正在跑的门控。

    ┌──────────────────────────────────────────────────────────────┐
    │ kit.run  →  WasteSortingApp.run()  →  core_waste.app.WasteApp │
    │   sources[kind=rtsp]   ffmpeg 解 rkipc H.265 子码流             │
    │   backends.rknn        efficientnet-lite0 INT8 @ RV1126B NPU  │
    │   core_waste.taxonomy  softmax + top-3 + 中国四分类查表        │
    │   core_waste.publish_gate  去抖 / 心跳 / 失败补发              │
    │   core_waste.publisher     waste/{stream_id}/results（契约校验）│
    │   core_waste.webui         /healthz /metrics /trigger          │
    └──────────────────────────────────────────────────────────────┘

`needs_frames = False`：帧不由 kit 的相机通道来，而是 `WasteApp` 自己的采集线程
经 ffmpeg 拉 RTSP 子码流。voice-transcribe 是同一个形态（输入是音频不是相机）——
kit 于是不开相机通道，而 emit / 配置自动绑定 / SIGHUP 全部照常可用。

**取帧不停自带应用。** `S21appinit` 停的是 rkipc，而 rkipc 正是这路 RTSP 的来源；
停了它这个应用就没有帧。单活切换只关别的推理应用，不关供帧链路。

**模型已内嵌（bundled）。** `.rknn` 随应用安装在本应用目录的 `models/` 下，manifest
的 `artifacts[]` 声明安装文件。配置里的 `model_path` 只有在用户明确填写时才覆盖
这个随包路径。
"""
from __future__ import annotations

import logging
import os
import sys
import threading

from kit.app import App, run_app

import rtsp_source

APP_ID = "waste-sorting"

#: 应用包内随包模型目录。打包器把 manifest `artifacts[].file` 安装到这里。
DEFAULT_MODEL_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "models")  # bundled, self-contained
DEFAULT_MODEL_PATH = os.path.join(
    DEFAULT_MODEL_DIR, "efficientnet_lite0_waste8.rv1126b.int8.calib256.mmse.rknn")
LEGACY_MODEL_PATH = ("/userdata/local/models/waste-sorting/"
                     "efficientnet_lite0_waste8.rv1126b.int8.calib256.mmse.rknn")

#: 产出该 rknn 的 ONNX 的 sha256（`models/SHA256SUMS`）。事件里的
#: `model.onnx_sha256` 报的是它——四个平台报同一个值，才能判断两块板跑的是不是
#: 同一个训练产物。rknn 自己的哈希由后端算出来进 `model.artifact_sha256`。
ONNX_SHA256 = "e9f9e847de6899ad4341d8f6084823e7c70307e84ac4d0da4bc4911b5b767391"

#: rkipc 的 H.265 子码流（640x480）。分类只需要 224x224，主码流是 4K，拉它纯属浪费。
#: 取帧细节与"为什么不是 go2rtc 的快照 API"见 `rtsp_source.py`。
DEFAULT_STREAM_URL = "rtsp://127.0.0.1:5554/live/1"

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


class WasteSortingApp(App):
    """appmgr / supervisor 侧的外壳。业务全在 `core_waste.app.WasteApp`。"""

    id = APP_ID
    name = "Waste Sorting"
    owns_loop = True
    needs_model = False        # 模型不由 kit 加载，见模块 docstring
    needs_frames = False       # 帧由 WasteApp 的采集线程经 ffmpeg 拉 RTSP
    postproc = "classify"

    def __init__(self) -> None:
        super().__init__()
        # config_schema 的默认值。`_bind_params` 在 start() 里按 config.json 覆盖，
        # apply:"live" 的项在 SIGHUP 时重新绑定到同名属性上。
        self.model_path = DEFAULT_MODEL_PATH
        self.stream_url = DEFAULT_STREAM_URL
        self.stream_id = "recamera-pro"
        self.max_fps = 5.0
        self.min_confidence = 0.50
        self.consecutive_frames = 3
        self.web_port = 8129
        self.mqtt_host = ""
        self.mqtt_port = 1883
        self.mqtt_topic = ""
        self.mqtt_username = ""
        self.mqtt_password = ""

        self._service = None                 # core_waste.app.WasteApp
        self._stop = threading.Event()

    # -- 配置 ------------------------------------------------------------- #
    def setup(self, config) -> None:
        super().setup(config)
        # core 侧全用 `logging`，而 root logger 默认只放 WARNING 以上——不装这个，
        # 「连上哪个 broker、门控参数是多少、源出没出帧」这几条 INFO 一条都不会
        # 落进 supervisor 收的 app.log，验收时只能靠猜。
        if not logging.getLogger().handlers:
            logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                                format="%(asctime)s %(levelname)s %(name)s %(message)s")
        logging.getLogger().setLevel(logging.INFO)

    def build_config(self) -> dict:
        """appmgr 的扁平 config.json → `contracts/config.schema.json` 的形状。

        翻译层留在这里而不是塞进 core：面板上一个开关叫什么、分几组，是 Pro 应用
        中心的事；core 的配置形状是四个平台共用的契约，不该被某一块板的 UI 带着走。
        """
        from core_waste.taxonomy import MATERIAL_CLASSES

        config = {
            "sources": [{
                "stream_id": str(self.stream_id),
                "uri": str(self.stream_url),
                "kind": "rtsp",
                "width": int(rtsp_source.DEFAULT_WIDTH),
                "height": int(rtsp_source.DEFAULT_HEIGHT),
            }],
            "model": {
                "path": str(self.model_path),
                "accelerator": "rknn",
                "backbone": "efficientnet_lite0",
                "input_size": [224, 224],
                "input_range": "uint8",
                "onnx_sha256": ONNX_SHA256,
                "top_k": 3,
            },
            "classes": list(MATERIAL_CLASSES),
            "rules": {
                "min_confidence": float(self.min_confidence),
                "fallback_category": "residual",
                "consecutive_frames": max(1, int(self.consecutive_frames)),
            },
            "trigger": {
                "mode": "continuous",
                "sources": ["http"],
                "max_fps": float(self.max_fps),
            },
            # 图片不出设备，也不落盘：Pro 的 /userdata 是 2 GB 出头的共享分区，
            # 一条常驻分类链路无人清理地写 JPEG，几天就能把别的应用挤下去。
            "capture_store": {"kind": "none"},
            "web": {"enabled": self.web_port > 0, "bind": "127.0.0.1",
                    "port": int(self.web_port)},
        }
        if self.mqtt_host:
            config["mqtt"] = {
                "host": str(self.mqtt_host),
                "port": int(self.mqtt_port),
                "topic": (str(self.mqtt_topic)
                          or "waste/{stream_id}/results"),
                "username": str(self.mqtt_username),
                "password": str(self.mqtt_password),
            }
        return config

    def on_config_reload(self, config) -> None:
        """SIGHUP。只有门控参数是 apply:"live"，其余项需要 restart。"""
        super().on_config_reload(config)
        service = self._service
        if service is None:
            return
        for gate in service.gates.values():
            gate.debounce_frames = max(1, int(self.consecutive_frames))
            gate.min_confidence = float(self.min_confidence)
        service.rules["min_confidence"] = float(self.min_confidence)
        service.rules["consecutive_frames"] = max(1, int(self.consecutive_frames))
        LOG.info("gate reloaded: consecutive_frames=%s min_confidence=%s",
                 self.consecutive_frames, self.min_confidence)

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
        from core_waste.config import apply_defaults, validate

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

        from core_waste.app import WasteApp

        # 取帧口换成 ffmpeg 子进程：设备自带的 OpenCV 打不开 RTSP。
        # 采集线程、丢帧策略、失败重试仍然是 core 的（`rtsp_source.py`）。
        self._service = WasteApp(config, device_name=APP_ID,
                                 capture_factory=rtsp_source.capture_factory)
        # supervisor 停应用是 TERM→(15 s)→KILL。kit 不装 TERM 处理器，默认动作
        # 直接终止进程，NPU 句柄与采集线程都不走释放路径。接上它，`stop` 就走
        # 正常收尾；15 s 预算对这条循环绰绰有余（一帧 6 ms）。
        self._install_stop_handlers()
        LOG.info("waste-sorting starting: model=%s source=%s topic=%s",
                 os.path.basename(str(self.model_path)), self.stream_url,
                 self._service.publisher.topic_template)
        try:
            summary = self._service.run()
        finally:
            self._service = None
        LOG.info("waste-sorting stopped: frames=%s published=%s",
                 summary.get("frames_processed"), summary.get("events_published"))

    def finish(self) -> None:
        service = self._service
        if service is not None:
            service.request_stop()
        super().finish()


if __name__ == "__main__":
    run_app(WasteSortingApp())

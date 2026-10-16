"""MQTT 发布。一帧一条消息，发布前必过 contracts/validate_payload.py。

契约校验放在发布路径上而不是只放在测试里：任何 backend 改动导致 payload 变形，
在设备上第一帧就会被拒，而不是等消费方解析出错。
"""
from __future__ import annotations

import importlib.util
import json
import logging
import threading
from pathlib import Path
from typing import Any, Callable

LOGGER = logging.getLogger(__name__)


def _load_validator() -> tuple[Callable[[Any], list[str]],
                               Callable[[Any], list[str]]]:
    """contracts/validate_payload.py 是无依赖脚本，按路径加载，避免打包耦合。"""
    try:
        from validate_payload import (validate_event,  # type: ignore
                                      validate_explanation)

        return validate_event, validate_explanation
    except ImportError:
        pass
    root = Path(__file__).resolve().parents[2]
    for candidate in (root / "contracts" / "validate_payload.py",
                      root.parent / "contracts" / "validate_payload.py"):
        if candidate.exists():
            spec = importlib.util.spec_from_file_location(
                "validate_payload", candidate)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)  # type: ignore[union-attr]
            return module.validate_event, module.validate_explanation
    raise ImportError("contracts/validate_payload.py not found")


validate_event, validate_explanation = _load_validator()

DEFAULT_EXPLANATION_TOPIC = "inspection/{stream_id}/explanations"


class PayloadRejected(ValueError):
    """payload 不符合 contracts/mqtt-event.schema.json。"""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def render_topic(template: str, stream_id: str) -> str:
    """topic 是模板，例如 inspection/{stream_id}/results。"""
    return template.replace("{stream_id}", stream_id)


class EventPublisher:
    """校验 + 发布的公共部分。子类只实现 `_deliver`。"""

    def __init__(self, topic_template: str, qos: int = 0,
                 retain: bool = False,
                 explanation_topic_template: str = DEFAULT_EXPLANATION_TOPIC
                 ) -> None:
        self.topic_template = topic_template
        self.explanation_topic_template = explanation_topic_template
        self.qos = qos
        self.retain = retain
        self.published = 0
        self.rejected = 0
        self.explanations_published = 0
        self.explanations_rejected = 0
        self.last_error: str | None = None

    def publish(self, payload: dict) -> str:
        errors = validate_event(payload)
        if errors:
            self.rejected += 1
            self.last_error = "; ".join(errors)
            raise PayloadRejected(errors)
        topic = render_topic(self.topic_template, payload["stream_id"])
        self._deliver(topic, json.dumps(payload, separators=(",", ":")))
        self.published += 1
        return topic

    def publish_explanation(self, payload: dict) -> str:
        """旁路事件：VLM 解释走自己的 topic，主事件的形状与时序不受影响。"""
        errors = validate_explanation(payload)
        if errors:
            self.explanations_rejected += 1
            self.last_error = "; ".join(errors)
            raise PayloadRejected(errors)
        topic = render_topic(self.explanation_topic_template,
                             payload["stream_id"])
        self._deliver(topic, json.dumps(payload, separators=(",", ":")))
        self.explanations_published += 1
        return topic

    def _deliver(self, topic: str, body: str) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass


class NullPublisher(EventPublisher):
    """未配置 mqtt 段时使用。仍然做契约校验，只是不发出去。"""

    def __init__(self, topic_template: str = "inspection/{stream_id}/results",
                 explanation_topic_template: str = DEFAULT_EXPLANATION_TOPIC
                 ) -> None:
        super().__init__(topic_template,
                         explanation_topic_template=explanation_topic_template)
        self.messages: list[tuple[str, str]] = []

    def _deliver(self, topic: str, body: str) -> None:
        self.messages.append((topic, body))
        if len(self.messages) > 100:
            del self.messages[:-100]


class MqttPublisher(EventPublisher):
    """paho-mqtt 1.x / 2.x。连接失败不拖垮推理循环，后台自动重连。

    两个大版本都要支持是因为设备决定了版本，而设备不出网：reCamera Pro 自带的
    是系统包 `paho-mqtt 1.6.1`（`/userdata/rknnenv` 以 `--system-site-packages`
    继承它），装不了 2.x；Pi / Orin 的运行镜像里是 2.x。差别只有两处，都在这个
    类里收敛：构造函数的 `CallbackAPIVersion`（1.x 没有这个枚举，传了就
    `AttributeError`），以及 `on_connect` 的回调签名（1.x 第四个位置参数是 int
    `rc`，2.x 是 `ReasonCode` 且多一个 `properties`）。契约校验、topic 渲染、
    计数一律不受影响。
    """

    def __init__(self, config: dict[str, Any], client_id: str = "") -> None:
        super().__init__(
            config["topic"], int(config.get("qos", 0)),
            bool(config.get("retain", False)),
            explanation_topic_template=explanation_topic(config))
        import paho.mqtt.client as mqtt

        self.host = config["host"]
        self.port = int(config.get("port", 1883))
        api_version = getattr(mqtt, "CallbackAPIVersion", None)
        if api_version is not None:
            self._client = mqtt.Client(api_version.VERSION2,
                                       client_id=client_id or None)
        else:
            self._client = mqtt.Client(client_id=client_id or None)
        if config.get("username"):
            self._client.username_pw_set(config["username"],
                                         config.get("password"))
        if config.get("tls"):
            self._client.tls_set()
        self._connected = threading.Event()
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.connect_async(self.host, self.port, keepalive=30)
        self._client.loop_start()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        # 1.x 传 int rc（0 = 成功），2.x 传 ReasonCode（带 is_failure）。
        failed = (bool(reason_code) if isinstance(reason_code, int)
                  else bool(getattr(reason_code, "is_failure", False)))
        if failed:
            self.last_error = f"connect failed: {reason_code}"
            LOGGER.warning("MQTT %s", self.last_error)
            return
        self._connected.set()
        LOGGER.info("MQTT connected to %s:%s", self.host, self.port)

    def _on_disconnect(self, client, userdata, *args):
        self._connected.clear()
        LOGGER.warning("MQTT disconnected from %s:%s", self.host, self.port)

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def wait_connected(self, timeout: float = 5.0) -> bool:
        return self._connected.wait(timeout)

    def _deliver(self, topic: str, body: str) -> None:
        info = self._client.publish(topic, body, qos=self.qos, retain=self.retain)
        if info.rc != 0:
            self.last_error = f"publish rc={info.rc}"
            LOGGER.warning("MQTT publish failed rc=%s topic=%s", info.rc, topic)

    def close(self) -> None:
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:  # noqa: BLE001 - 关闭失败不影响退出码
            pass


def explanation_topic(mqtt_config: dict[str, Any]) -> str:
    """解释事件的 topic：显式配置优先，否则由 results topic 推出来。"""
    explicit = mqtt_config.get("explanation_topic")
    if explicit:
        return str(explicit)
    topic = str(mqtt_config.get("topic", ""))
    if topic.endswith("/results"):
        return topic[: -len("/results")] + "/explanations"
    return DEFAULT_EXPLANATION_TOPIC


def build_publisher(config: dict[str, Any], client_id: str = "") -> EventPublisher:
    """有 mqtt 段就连 broker，没有就走 NullPublisher（仍做契约校验）。"""
    mqtt_config = config.get("mqtt")
    if not mqtt_config:
        return NullPublisher()
    return MqttPublisher(mqtt_config, client_id=client_id)

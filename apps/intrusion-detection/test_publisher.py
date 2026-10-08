"""Publisher wire behaviour against the kit MQTT test broker (no network)."""
from __future__ import annotations

import json
import os
import socket
import sys
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from kit.adapters import mqtt_sink  # noqa: E402
from kit.adapters.test_mqtt_sink import FakeBroker, broker  # noqa: E402,F401
from esk.publisher import Publisher  # noqa: E402


# -- regression: publisher shutdown vs. a concurrent on_connect ------------- #
def _status_events(broker, topic):
    out = []
    for _n, h, body in list(broker.packets):
        if h >> 4 != 3:
            continue
        topic_out, payload, _qos, _pid = mqtt_sink._parse_publish(h, body)
        if topic_out == topic:
            out.append(json.loads(payload)["online"])
    return out


def test_publisher_goodbye_stays_last_when_on_connect_races_shutdown(broker):
    gate = threading.Event()         # holds the reconnect's online heartbeat
    entered = threading.Event()
    announced = threading.Event()
    n_calls = []

    def health():
        n_calls.append(1)
        if len(n_calls) == 2:                            # reconnect on_connect
            entered.set()
            gate.wait(5.0)
        return {"decode": "sw"}

    pub = Publisher(device_id="dev1", stream_id="cam-0", host="broker",
                    health_fn=health)
    real_on_connect = pub._on_connect

    def on_connect(client):
        try:
            real_on_connect(client)
        finally:
            if len(n_calls) >= 2:
                announced.set()

    pub.client.on_connect = on_connect
    real_disconnect = pub.client.disconnect

    def disconnect_after_racing_announce(*a, **kw):
        gate.set()                   # let the online heartbeat go now...
        announced.wait(3.0)          # ...and land before the DISCONNECT
        real_disconnect(*a, **kw)

    pub.client.disconnect = disconnect_after_racing_announce
    pub.connect()
    broker.sessions[0].shutdown(socket.SHUT_RDWR)        # force a reconnect
    assert entered.wait(5.0)
    threading.Timer(0.3, gate.set).start()   # fixed code: shutdown waits on it
    shutdown = threading.Thread(target=pub.shutdown, daemon=True)
    shutdown.start()
    shutdown.join(5.0)
    assert not shutdown.is_alive()
    # The broker reads asynchronously: wait until it has seen the DISCONNECT,
    # which the client sends after every status write on that socket.
    broker.expect(14, timeout=3.0, session=1)
    events = _status_events(broker, pub.topic_status)
    assert events and events[-1] is False, events        # goodbye is last

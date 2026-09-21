# Application-triggered recording

Read when an app should expose recording conditions or explicitly request a
clip. The authorities are `market/appmgr/manifest.py`, `recording.py`,
`kit/app.py:App.request_recording`, and the firmware Vigil integration.

## Two trigger paths

- **FRAME conditions:** managed detection/classification results can be
  projected through the installed manifest's `record_trigger` authorization.
  The device's recording rules decide which class, score and ROI should trigger.
- **Explicit EVENT request:** call `self.request_recording(event_kind, ts)`
  through the authenticated Kit gateway. AppMgr checks the installed signal
  declaration and the current process/instance/generation. A stopped or replaced
  instance cannot reuse its old authorization.

`self.emit(events=...)` publishes business results; it does not itself submit an
explicit recording request. `request_recording()` returning `True` means the
request was accepted into the local sink queue. It does **not** mean the device
matched a recording rule, started a clip, or wrote a file. Recording enablement,
rules, storage and backend status remain separate device preconditions.

## Minimal event declaration

Merge these fields into a complete manifest, with `result.publish: brokered`
and `instances.endpoint_mode: allocated`. Add an output field describing the
event so the official validator can link the recording signal to its result
contract:

```json
{
  "output": {
    "contract_version": 2,
    "sink": "ws",
    "schema": "Alarm event",
    "default_channel": ["ws"],
    "default_mode": "raw",
    "default_mapping": [],
    "fields": [
      {"name": "alarm_kind", "from": "events[kind=alarm].kind", "type": "string", "description": "Alarm event"}
    ]
  },
  "record_trigger": {
    "version": 1,
    "signals": [
      {"id": "alarm", "type": "event", "event_kind": "alarm", "supports_roi": false}
    ]
  }
}
```

An event kind is a lowercase canonical token and must match the declared output
event kind. Detection signals additionally require an explicit `classes` list
and direct label, score and box fields with declared coordinates. Classification
signals need label and score fields and cannot support ROI. Reuse the official
manifest validator for exact limits and reject runtime attempts to add classes.

Call-site example inside a Kit App (supply `alarm_active` from your app logic):

```python
import time
from kit.app import App


class AlarmApp(App):
    owns_loop = True
    needs_model = False

    def setup(self, config):
        super().setup(config)
        self._alarm_active = False
        self._last_request = float("-inf")

    def request_on_alarm_edge(self, alarm_active, frame):
        now = time.monotonic()
        if alarm_active and not self._alarm_active and now - self._last_request >= 5:
            # Attempt at most once per edge/cooldown, including queue rejection.
            self._last_request = now
            queued = self.request_recording("alarm", ts=frame.pts)
            # queued is local submission status, not recording completion.
        self._alarm_active = alarm_active
```

This is a helper-class fragment; implement/inherit `run(self)` for an installable
app. Choose cooldown and edge/debounce rules for the application; do not loop
on a failed enqueue or submit on every frame. A device may further rate-limit
or reject requests even after local submission.

## Compatibility and authority

Legal FRAME results sent through the direct `result-in.sock` path still enter
Vigil for legacy recording-rule compatibility. Upgrading does not imply that
old YOLO applications stop triggering recordings. Whether a specific clip is
created depends on the enabled device rules and emitted data.

Segmentation masks are result/push data; they do not trigger recording or receive
stream OSD through this path. Publicly exported `recamera_ext.OsdSink` and
`RecordSink` are **AppMgr-only** clients of private endpoints. Regular apps must
not instantiate them. Direct `ResultSink` identity is derived from peer
credentials; caller-supplied `source_id` is not a trusted per-app identity and
cannot claim the reserved `builtin` source.

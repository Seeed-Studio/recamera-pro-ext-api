# AppMgr-managed runtime

Read for App Center launch and result ownership. AppMgr launches every installed
Python application through `kit/run.py`; a plain SDK script can be a standalone
demo, but needs a Kit lifecycle adapter to become an installable application.

## Entry and lifecycle

The entry must export a `kit.app.App` subclass or explicit `APP` (class or
instance). The loader prefers a local leaf class, then a visible/re-exported
one; select `APP` when several candidates remain. Complex mixin MRO is reported
as unverified by the static checker and needs loader validation. Inherited `owns_loop=True`
and `run(self)` are valid. Imports and constructors must not start application
work before Kit's lifecycle. No removed callback hooks are supported.

```python
from kit.app import App, run_app


class MyApp(App):
    owns_loop = True
    needs_model = False

    def run(self):
        for frame in self.frames():
            self.emit(events=[], ts=frame.pts, results=[])


if __name__ == "__main__":
    run_app(MyApp())
```

The static validator resolves local inheritance, aliases and re-exports without
executing app code. Dynamic factories, conditional exports and unknown external
bases receive `managed_entry_unverified`; verify these with the actual loader
and dependencies before claiming launchability. Package validation also checks
the Python sources in the final archive, including payload overlays.

## Result ownership

| Application route | Manifest claim | Consumer |
| --- | --- | --- |
| Kit `App.emit()` / `request_recording()` | `result.publish: brokered`, `instances.endpoint_mode: allocated` | AppMgr authenticated gateway / Result Hub |
| Direct `recamera_ext.ResultSink` | `result.publish: shared` or `exclusive` | Public `result.ingress`; normalized coordinates and microsecond PTS |
| Model calculation without publication | Declare resources actually used | A model alone does not imply a result gateway |

Kit's registry selects `GatewayResultSink` when AppMgr injects its gateway
identity. App code must not construct `WsResultSink` or `GatewayResultSink`,
select a child-owned WebSocket sink, or bind the reserved port `8124`.
`shared` does not inject a gateway. With default adapter settings, Kit opens a development WebSocket sink on
`8124` without a gateway even when the app never calls `emit`. An explicit
verified adapter/lifecycle integration is needed for direct SDK or compute-only
apps; mere socket presence does not select direct ingress. The validator emits
`kit_default_sink_unverified` for this case instead of rewriting a legal claim.

A direct-SDK Kit wrapper must preserve `start/run/finish`, close its SDK handles
on exit, and avoid opening a second camera source or result listener. Use the
intended adapter/lifecycle configuration and verify it against the target; a
legal resource claim alone does not prove correct runtime ownership.

Do not assign `RECAMERA_RESULT_GATEWAY_SOCK`,
`RECAMERA_RESULT_GATEWAY_REQUIRED`, `RECAMERA_APP_ID`,
`RECAMERA_APP_INSTANCE`, or `RECAMERA_APP_GENERATION`; these belong to AppMgr.
For recording authorization and legacy result compatibility see [recording.md](recording.md).

## Manual debugging

A direct `app.py` launch is different from AppMgr launch. Use `--sink stdout`
for a local smoke test. It does not verify gateway identity, scheduled NPU
authorization or App Center lifecycle. A missing/mismatched gateway must be
investigated through AppMgr state and logs, not worked around by binding `8124`.

## Read-only diagnosis

```text
python scripts/probe_target.py --host <user>@<target-host> --timeout 12
python scripts/diagnose_managed_app.py --host <user>@<target-host> --app-id <id> --timeout 12
```

Scripts require target Python 3 and key/agent-based OpenSSH authentication. They
send a read-only Python collector through stdin with a total deadline, bounded
file reads and redacted output. They do not open cameras, sockets, ALSA or GPIO,
import the SDK, install packages, or start/stop services.

Schema 2 reports filesystem types separately from kernel socket registration
and protocol health. Process inventory comes from procfs, never from log names.
Only needed environment fields are retained. The app diagnostic correlates
app ID, PID, instance, generation and process start time with lifecycle state;
current-run errors override optimistic PID/gateway observations. Unattributed
or historical errors remain separate. No errors found is **not** proof of
successful inference. Missing permissions, truncated files, races and unknown
firmware layouts leave the relevant evidence unverified.

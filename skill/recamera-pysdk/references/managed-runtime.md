# AppMgr-managed runtime

Use this reference whenever an App is launched by App Center/AppMgr, especially
for a Kit model App or an App that publishes results.

## Result ownership

The managed result path is:

```text
AppMgr coordinator
  -> supervisor injects RECAMERA_RESULT_GATEWAY_SOCK and App identity
  -> kit registry selects GatewayResultSink
  -> App calls kit.App.emit()
  -> authenticated AppMgr result gateway / result hub
```

The App must not create a listener for the result stream. In particular, it
must not construct `WsResultSink` or `GatewayResultSink`, call
`open_result_sink("ws", ...)`, select a WebSocket/OSD sink, or bind port
`8124`. Port `8124` belongs to the AppMgr result gateway on current firmware;
multiple Apps cannot safely claim it.

For ordinary Kit model/output Apps, the manifest must declare exactly one
`result.publish` resource claim with `mode: "brokered"`. On the current AppMgr,
`mode: "brokered"` is the branch that sets up the authenticated result gateway
and injects `RECAMERA_RESULT_GATEWAY_SOCK`. `mode: "shared"` selects the direct
`result.ingress` resource instead; it does not inject the gateway environment.
Without that environment, Kit falls back to its development `WsResultSink` on
`127.0.0.1:8124`, which is already owned by AppMgr and causes an address-in-use
failure. `result.publish: shared` is therefore not a valid declaration for a
normal Kit App that calls `App.emit()`.

For a Kit App, use only the Kit lifecycle and output surface:

```python
from kit.app import App, run_app


class MyApp(App):
    owns_loop = True

    def run(self):
        for frame in self.frames():
            self.emit(events=[], frame=frame.pts, results=[])


if __name__ == "__main__":
    run_app(MyApp())
```

Do not set or overwrite `RECAMERA_RESULT_GATEWAY_SOCK`,
`RECAMERA_RESULT_GATEWAY_REQUIRED`, `RECAMERA_APP_INSTANCE`, or
`RECAMERA_APP_GENERATION`. AppMgr mints these values for the authenticated
launch. `instances.endpoint_mode` must be `allocated` for managed model/output
Apps.

## Manual debugging

Running `app.py` directly is a different mode. Without AppMgr's gateway
environment, the current Kit falls back to its development WebSocket sink,
whose default is `127.0.0.1:8124`. That port is normally already occupied by
AppMgr, so a direct launch commonly fails with:

```text
OSError: [Errno 98] Address already in use
```

Use `--sink stdout` for a local smoke test, or deliberately choose another
free port for manual debugging. This does not make the process an AppMgr
managed App and cannot validate gateway authentication, scheduled NPU
authorization, or App Center lifecycle behavior.

If a managed launch still reaches `WsResultSink`, inspect the AppMgr operation
and application logs. The likely issue is an old/bypassed launch path or a
device Kit/AppMgr version mismatch, not a Python business-logic fix. The Skill
can prevent an App from taking ownership of `8124`, but it cannot repair the
device firmware's gateway injection path.

## Read-only device diagnosis

When a user reports an App Center startup failure, run the bundled diagnostic
script against the explicitly supplied SSH target:

```text
python scripts/diagnose_managed_app.py --host <user>@<target-host> --app-id <app-id>
```

The script reads the installed manifest, release lock, AppMgr operations and
audit records, App files, App process environment, result sockets, port 8124,
and the recent App log. It emits JSON and does not install, stop, restart, or
modify anything on the device. Treat `historical_failed_operations` as
historical evidence when a current managed process has both `kit/run.py` and
`RECAMERA_RESULT_GATEWAY_SOCK`; do not report an old `WsResultSink` or model
authorization error as the current state in that case. A current process that
continues to emit inference frames is separate evidence of a running release.

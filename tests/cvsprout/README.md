# CVSprout Link validation

CVSprout Link (source: `apps/cvsprout-link`, stable App ID: `cvsprout-capture`)
is the device integration application. It
uploads captured images and deploys compatible trained models into the device's
system model library. Training runs on CVSprout; inference runs through the
device's system model service, without a separate inference application.

Run the host tests from the SDK checkout:

```sh
uv run --frozen pytest tests/cvsprout
```

The portability regression starts a clean Python process with both `sqlite3` and
`_sqlite3` imports blocked before loading real Kit. It verifies capture App setup,
the file queue's restart recovery, owner isolation and matching-receipt deletion.
No system Python packages or firmware are modified.

The diagnostics regressions exercise real Kit `start()` and `frames()` with a
controlled CPU frame source, heartbeat before any frame, stalled frames, finite
capture quotas, corrupt pending records, encoding failures, bounded safe logs,
invalid device token rejection, and v1/v2 heartbeat compatibility.

`test_system_models.py` covers CVSprout Link deployment (introduced in Connector
1.1.0): actual CGI response
shapes and accepted content types, loopback TLS trust plus exact certificate pin,
308 chunk progress, same-origin streamed artifact integrity, durable claim/report
receipts, lost append/finalize/configure/activation replies, restart reconciliation,
model files without JSON sidecars, orphan metadata, project isolation, manual
configuration changes, verified rollback, lease revocation and shared shutdown
deadlines. These tests use controlled HTTP/TLS servers and filesystem journals;
they never contact a device. The certificate test requires the host `openssl` CLI.

For a real HTTP integration with an adjacent CVSprout checkout, pass its root and
the interpreter from its installed environment. This creates a temporary database
and media directory, starts the actual FastAPI application on a loopback ephemeral
port, and removes the isolated instance afterwards:

```sh
uv run --frozen python tests/cvsprout/live_backend.py \
  --cvsprout-root /path/to/CVSprout \
  --backend-python /path/to/CVSprout-venv/bin/python \
  --report dist/cvsprout/cvsprout-capture-1.1.2-live-backend.json
```

It checks the actual upload authentication/body-limit middleware, multipart image
delivery, lost-ack replay after queue restart, content deduplication, pause/drain,
owner isolation, token rotation and the actual v2 heartbeat acceptance/status
round trip with zero frames and after capture progress. No production server or device is contacted.

Use the recamera-pysdk skill's official validator and packager with this checkout
as the explicit `--sdk-root`. For example, with `RECAMERA_SKILL_ROOT` set to the
installed skill directory and `RECAMERA_SDK_ROOT` set to the SDK checkout:

```sh
uv run --frozen python "$RECAMERA_SKILL_ROOT/scripts/validate_app.py" --app-dir apps/cvsprout-link --mode publish --sdk-root "$RECAMERA_SDK_ROOT"
uv run --frozen python "$RECAMERA_SKILL_ROOT/scripts/package_app.py" --app-dir apps/cvsprout-link --out dist/cvsprout --sdk-root "$RECAMERA_SDK_ROOT"
```

Use the archive path printed by the packager, then run
`scripts/smoke_app.py --archive <printed-path> --sdk-root "$RECAMERA_SDK_ROOT"
--smoke-test tests/cvsprout/smoke_capture.py` against that exact archive. The hook
uses the actual Kit loader and the application's capture/upload loop with
controlled frames; it does not connect to a device.

For the CVSprout Link 1.1.2 archive, the full smoke command is:

```sh
uv run --frozen python "$RECAMERA_SKILL_ROOT/scripts/smoke_app.py" \
  --archive dist/cvsprout/cvsprout-capture-1.1.2-arm64.tar.gz \
  --sdk-root "$RECAMERA_SDK_ROOT" \
  --smoke-test tests/cvsprout/smoke_capture.py --timeout 20 \
  --report dist/cvsprout/cvsprout-capture-1.1.2-host-smoke.json
```

The official helper performs its first import before loading external hooks, so
the capture hook explicitly removes the archived App's cached modules and reloads
that exact extracted entry with real Kit while SQLite imports are blocked. It
then runs setup and the real capture/upload loop with a controlled RGB frame,
checks the decoded original JPEG, and checks the acknowledged queue is empty.
Its `capture_portability` log records the missing-module guard and reload evidence.
The original 1.0.0 archive fails this test with `ModuleNotFoundError`; 1.1.2 must pass.
Hardware frame/result sockets and AppMgr device startup still require a device test.

Link does not bundle a model. System model delivery and activation use the
compatible firmware's public model APIs, covered by `test_system_models.py`.

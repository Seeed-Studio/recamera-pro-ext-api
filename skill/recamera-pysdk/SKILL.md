---
name: recamera-pysdk
description: "Assess and build reCamera Pro (RV1126B) Python SDK apps; package by default after offline validation, bundling target-compatible private Python wheels for the device-side per-release environment without modifying the device or shared Python runtime."
---

# reCamera Pro Python Development

Turn a natural-language idea into an installable reCamera Pro App package
(`.tar.gz`) that the device App Center can run. Use this skill only for
reCamera Pro / RV1126B Python extension SDK Apps. It has three outcomes:

1. Decide whether the requested behavior is supported by the published SDK.
2. When supported, write or modify the App's Python source using the real SDK
   API and the current `kit` application shape.
3. By default, once the code passes offline validation, build a deterministic
   App archive containing the App and its non-platform Python dependencies as
   original target-compatible wheels under `wheels/`.

## Authoritative source

The compatible public SDK upstream is
[Seeed-Studio/recamera-pro-ext-api](https://github.com/Seeed-Studio/recamera-pro-ext-api).
It is the source for API docs, bindings, Kit code, official Apps, examples, and
the packaging implementation. For default packaging this skill bundles the
fixed official builder from public commit
`525addec801680f6aabbbb7605d6de3bb390348a` under `scripts/sdk-builder/` and
verifies its three SDK files by SHA-256, so no local SDK checkout is required.
A complete local checkout is only an explicit override via `--sdk-root` or
`RECAMERA_SDK_ROOT`. Never embed a host, account, password, private path, or
environment-specific SDK revision in an App or in this skill, and never
substitute a hand-made archive or an unverified SDK revision.

## Scope and boundaries

This skill may build the App archive described in
[app-packaging.md](references/app-packaging.md). It must not install, sign,
deploy, change firmware or Buildroot, modify AppMgr, or change any system or
shared Python environment. Device-side extraction and launch-time environment
assembly are the device's job. Keep any hardware validation read-only and
narrow. Device access is optional: source generation, static validation, and
packaging must all work with no device connected.

Keep four evidence levels distinct in every report: the public `recamera_ext`
API contract; the Kit/AppMgr packaging and launch contract; this skill's own
conservative publication and safety gates; and single-device evidence (imports,
sockets, logs), which proves presence only, never end-to-end success.

## Workflow

### 1. Assess before coding

Read [capability-routing.md](references/capability-routing.md) for every
request and restate the goal as a concrete SDK path. Conclude one of
**Supported**, **Supported with preconditions**, or **Not supported through this
SDK** before changing source. A route is directly runnable only after its
extension runtime is confirmed present (the binding imports,
`librecamera_ext.so.1` loads, the needed `/run/recamera/*.sock` endpoint is
live); until then report **Supported with preconditions**. Never invent private
RPC calls, direct media-pipeline access, or placeholder code that pretends to
work. Ask a focused question only when a decision materially changes the app.

### 2. Resolve SDK documentation without flooding context

Read [doc-router.md](references/doc-router.md), then load only the documents the
current feature needs. Prefer a user-provided compatible checkout; otherwise
resolve the official public upstream HEAD once to a full commit SHA, record it
as `sdk_source_commit`, and use only content pinned to that SHA. Do not preload
all of `docs/`. If GitHub cannot be read, work only from the bundled stable
contracts and classify the unverified part as **Supported with preconditions**.

### 3. Select the development layer

Use `recamera_ext` directly for a small frame-to-logic-to-result program; use
`kit` for model-backed vision, tracking, zones, OCR, pose, temporal logic,
config hot reload, or the common output pipeline; use `ProbeSource` only to
observe built-in inference. Read
[sdk-contracts.md](references/sdk-contracts.md) before writing direct SDK code,
[kit-app-patterns.md](references/kit-app-patterns.md) for a kit app, and
[managed-runtime.md](references/managed-runtime.md) for App Center launch
behavior. Prefer the nearest official example as a starting point. Speaker
output has **no unified public playback API**; an audible alarm is a conditional
**firmware ALSA path** (`aplay` / `libasound`), and real sound requires an
authorized hardware test. Never invent a `recamera_ext` playback call. Read
[audio-playback.md](references/audio-playback.md) before implementing any audio.

### 4. Preserve the SDK and AppMgr contracts

These are current runtime requirements, not style. The bundled validator
enforces the ones that can be checked offline.

- A `FrameSource`/`ProbeSource` zero-copy array is valid only for the current
  iteration; copy before queues, threads, delayed use, or storage.
- A kit app declares `owns_loop = True` and implements `run(self)` with no
  positional arguments; never use the removed `on_results`, `process_frame`, or
  `run_postproc` callbacks, and never derive the App root with
  `Path(__file__).parents[...]`.
- **NPU inference:** declare the `npu.rknn` resource with `mode: "scheduled"`
  (`brokered` is the scheduled service lane, `exclusive` the legacy direct
  lane). `shared` or an omitted mode is rejected at device preflight with
  `npu.rknn must use scheduled service or exclusive legacy mode`.
- **Bundled RKNN artifact:** in `scheduled`/`brokered` mode `models[]` supplies
  Kit metadata but does not authorize inference. Every `models[].file` must
  also appear in `artifacts[]` as `kind: "rknn"`, `source: "bundled"`, with
  `file` equal to `mount` and an exact `sha256` and positive `size` for the
  real file, or the device reports `inference authorization failed: scheduled
  application <id> has no bundled RKNN artifact`.
- Keep the entry at the package root (for example `app.py`) when models,
  classes, or artifacts are declared; a subdirectory entry with package-root
  resources fails device-side model authorization.
- **Managed result endpoint:** a model/output App uses
  `instances.endpoint_mode: "allocated"`, declares exactly one `result.publish`
  claim with `mode: "brokered"`, and publishes only through `kit.App.emit()`.
  `shared` selects `result.ingress`, does not inject
  `RECAMERA_RESULT_GATEWAY_SOCK`, and makes Kit fall back to its child-owned
  `8124` sink. Never construct a result sink, select `ws`/`osd`, set AppMgr
  identity variables, or bind the reserved `8124` port.
- **Frontend boxes:** a Kit detector is frontend-renderable only when the
  manifest declares `output.contract_version: 2`, `output.sink: "ws"`, a direct
  `output.fields[]` entry named `box` from `results[].box` with one consistent
  `coord` (`pixel_xyxy` or `normalized_xyxy`), and `render.schema_version: 1`
  with `render.boxes`. Burning boxes into the RTSP/recording stream additionally
  needs `render.stream_osd` with `default: false` plus a user opt-in. Calling
  `self.emit(results=...)`, logging `dets=N`, or drawing with `cv2.rectangle()`
  on an app-owned image does not put a box on the official preview. See
  [result-overlay.md](references/result-overlay.md).
- Do not open `/dev/video*`, build a competing VI/VPSS/VENC pipeline, or access
  `/var/tmp/rkipc`. Public Apps must not expose configurable or shell-based
  command execution.

### 5. Verify offline first

Read [manifest-contract.md](references/manifest-contract.md), then run the
bundled standard-library validator. It never connects to a device:

```text
python scripts/validate_app.py --app-dir <app> --mode demo
```

Fix every error before returning the App. Warnings and
`runtime_preconditions_unverified` mark work that still needs SDK, firmware,
model, or hardware confirmation. Also run the narrowest relevant pure-Python
tests. Never fail an otherwise valid Demo just because no device is connected,
and never claim device execution unless it was actually observed. For an
installed App that will not start, the read-only
`scripts/diagnose_managed_app.py --host <user>@<target-host> --app-id <id>`
correlates the manifest, release lock, AppMgr history, gateway socket, and logs.

### 6. Package by default once validation passes

Packaging is a required completion gate, not optional. Read
[app-packaging.md](references/app-packaging.md), then run the bundled helper:

```text
python scripts/package_app.py --app-dir <app> --out <dist>
```

The helper runs `validate_app.py` in `package` mode, fills the fixed platform
contract (`compatibility` and `python.runtime_profile`) into the staged
manifest, resolves an optional flat target wheelhouse into `wheels/`, and
delegates `release.lock.json`, `files.sha256`, and deterministic tar creation
to the official builder. It never runs pip and never copies the developer
machine's `site-packages`. When the App has no private dependencies it creates
an empty wheelhouse automatically; when the App has dependencies the caller must
supply a prepared target wheelhouse, since the helper does not infer, download,
or build wheels. Platform-owned projects (`numpy`, `cv2`, `kit`,
`recamera-ext`, `rknnlite`, and the rest listed in
[app-packaging.md](references/app-packaging.md)) are provided by the device
runtime and must not be bundled.

After the builder returns, the helper reopens the exact final archive and
verifies its embedded manifest, release lock, BOM, payload digests, size and
member caps, and App identity; for scheduled/brokered NPU Apps it requires a
bundled `kind: "rknn"` artifact for every `models[].file`. It deletes a
same-name prior output first, so a stale archive cannot be mistaken for the
current build. Do not report packaging complete unless the final `.tar.gz` path
was printed and the archive contains `manifest.json`, `release.lock.json`, and
`files.sha256`. Do not replace this command with a hand-written `tar`, and do
not stop after running `validate_app.py`. For a public release candidate also
run `python scripts/validate_app.py --app-dir <app> --mode publish`, which adds
a stable-channel requirement and payload-hygiene checks.

The expected archive contains:

```text
manifest.json
app.py
...application files...
wheels/<target-compatible-wheel>.whl
release.lock.json
files.sha256
```

The device AppMgr handles signature verification, safe extraction, per-release
environment construction, activation, and rollback; it does not run pip. This
skill does not install anything on the device, sign the archive, or hold a
private key. A structurally valid unsigned archive is not automatically
accepted by every device or publication channel.

## Output expectations

For an unsupported request, give a concise feasibility conclusion and the
precise blocker; do not create fake source files. For a supported request, name
the selected SDK layer, make the Python edit, call out the important runtime
preconditions and any unverified hardware-dependent path, and deliver the
packaged artifact by default: report the archive and build-report paths, bundled
versus platform-provided dependencies, and the fixed platform contract. If
packaging cannot run because a wheelhouse input is missing, state the missing
input explicitly.

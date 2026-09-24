---
name: recamera-pysdk
description: "Develop and package reCamera Pro (RV1126B) Python SDK apps, including ONNX-to-RKNN conversion, dependency/API checks and controlled runtime tests; optionally upload, install and verify an app on a user-provided device."
---

# reCamera Pro Python Development

Turn a natural-language idea into an installable reCamera Pro App package
(`.tar.gz`) that the device App Center can run. Use this skill only for
reCamera Pro / RV1126B Python extension SDK Apps. It has four outcomes:

1. Decide whether the requested behavior is supported by the published SDK.
2. When supported, write or modify the App's Python source using the real SDK
   API and the current `kit` application shape.
3. For an App Center deliverable, once the code passes offline validation, build a deterministic
   App archive containing the App and its non-platform Python dependencies as
   original target-compatible wheels under `wheels/`.
4. When the user requests device delivery/testing and supplies access, upload
   the verified archive and optionally install/verify it through AppMgr.

## Authoritative source

The compatible public SDK upstream is
[Seeed-Studio/recamera-pro-ext-api](https://github.com/Seeed-Studio/recamera-pro-ext-api).
It is the source for API docs, bindings, Kit code, official Apps, examples, and
the packaging implementation. The skill includes an offline
[public API reference](references/api/index.md), with exact signatures, fields,
defaults, descriptions and [interface characteristics](references/api/features.md).
Its source revision is recorded in `scripts/api-reference-lock.json`;
it is independent of the builder revision. For default packaging this skill bundles the
fixed official builder from public commit
`635ffc3c51d596dd2e8139f297798162e6be62b9` under `scripts/sdk-builder/` and
verifies its three SDK files by SHA-256, so no local SDK checkout is required.
A complete local checkout is only an explicit override via `--sdk-root` or
`RECAMERA_SDK_ROOT`. Never embed a host, account, password, private path, or
environment-specific SDK revision in an App or in this skill, and never
substitute a hand-made archive or an unverified SDK revision.

## Scope and boundaries

This skill may build the App archive described in
[app-packaging.md](references/app-packaging.md) and deliver/test that App within
the user's authorized scope using [runtime-validation.md](references/runtime-validation.md).
Providing device access for upload does not by itself request installation or
starting hardware actions. Honor installation/testing authorization already
given in the session; do not ask again just because a helper has an explicit
action flag. Do not sign, change firmware/Buildroot/AppMgr, overwrite live App
directories, or change the shared Python environment. Extraction, per-release
environment assembly and lifecycle remain AppMgr's job. Device access is
optional; offline development and packaging must work without a device.

Keep four evidence levels distinct in every report: the public `recamera_ext`
API contract; the Kit/AppMgr packaging and launch contract; this skill's own
conservative publication gates; and actual device observations. Read-only
inventory (imports, sockets, logs) alone proves presence, not end-to-end
success; report lifecycle, result delivery and task behavior separately.

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
current feature needs. Start with the bundled [API index](references/api/index.md)
and the relevant module; it includes SDK/Kit, native ABI, HTTP and WebSocket
contracts without requiring a repository checkout or network access.
Prefer a user-provided compatible checkout when comparing versions; otherwise
use the API reference's pinned commit as the documentation baseline. If newer APIs are
needed, resolve and record an explicit revision and compare its contract before
using it; do not mix newer examples with an older packager silently. Do not preload
all of `docs/`. If GitHub cannot be read, work only from the bundled stable
contracts and classify the unverified part as **Supported with preconditions**.

### 3. Select the development layer

Use `recamera_ext` directly for a standalone SDK demo. App Center packages must
expose a `kit.app.App` subclass/instance through the manifest entry; a plain
script is not launchable by `kit.run`. A thin Kit lifecycle wrapper may use
direct SDK primitives without duplicating camera or result ownership. Use
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

When a new model or ONNX conversion is needed, read
[model-conversion.md](references/model-conversion.md). Use the matching pinned
Rockchip Model Zoo example, an isolated host Toolkit2 environment and an
explicit preprocessing contract. `scripts/convert_model.py` covers static
single-image models, FP/INT8 builds and optional ONNX-versus-simulator checks;
model-specific exports/hybrid quantization need their own reviewed recipe.
Keep conversion, numerical validation and device validation separate. A supplied
compatible RKNN does not require reconversion, and converter dependencies must
not enter the App wheelhouse.

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
- **Result route:** Kit `App.emit()` and `App.request_recording()` use exactly
  one `result.publish: brokered` claim and `endpoint_mode: allocated`.
  Direct SDK `ResultSink` uses `result.publish: shared/exclusive` ingress.
  Do not infer gateway requirements merely from the existence of a model.
  Never bind the reserved `8124` port or override AppMgr identity variables.
- **Recording:** read [recording.md](references/recording.md) for manifest
  authorization, explicit requests and legacy FRAME compatibility. `OsdSink`
  and `RecordSink` are AppMgr-only despite being exported by the Python module.
- **Multiple inputs:** read [multi-input models and backend selection](references/kit-app-patterns.md#multi-input-models-and-backend-selection)
  before choosing a runtime. Typed sessions with a complete multi-input contract
  use RKNNLite in `auto`; ctypes/shared DMA require a single input. The ordinary
  App Center manifest-to-`self.models` path does not yet carry that full contract;
  do not promise deployment support from session support alone.
- **DMA:** for supported single-image models with synchronous model-only frame consumption, use `hw-direct`,
  `model_dma_input = True`, and `infer(prepared)`; read the lifetime and RGB/BGR
  rules in [kit-app-patterns.md](references/kit-app-patterns.md). Keep ndarray
  input and CPU/original-pixel paths where the application needs them.
- **Optional frontend boxes:** data-only applications need no renderer. When
  browser overlay is requested, a Kit detector is frontend-renderable only when the
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
model, or hardware confirmation. Test model-specific postprocessing against
known outputs, including input size, class count and original-image coordinates.
The packager checks mandatory source imports against local modules, platform
modules and bundled wheels even when `python.imports` is empty. Dynamic imports
and dispatch still need runtime tests. Never fail an otherwise valid Demo just because no device is connected,
and never claim device execution unless it was actually observed. For an
installed App that will not start, the read-only
`scripts/diagnose_managed_app.py --host <user>@<target-host> --app-id <id>`
correlates the manifest, release lock, AppMgr history, gateway socket, and logs.

### 6. Build the requested App Center package

For an App Center deliverable, create and verify the package. Standalone SDK
demos or a user-requested source-only change do not require a package. Read
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

The device AppMgr applies channel-specific signature policy, safe extraction, per-release
environment construction, activation, and rollback; it does not run pip. This
skill does not sign the archive or hold a private key. A structurally valid unsigned archive is not automatically
accepted by every device or publication channel. Read `/api/app-center/v1/policy`
when assessing installation: current local Web upload permits unsigned packages;
other channels can require a signature.

### 7. Validate the artifact and optionally deliver to the device

Read [runtime-validation.md](references/runtime-validation.md). In an isolated
host environment with the compatible SDK, run `scripts/smoke_app.py` against
the **final archive**, using the real Kit loader and an app-specific bounded
`smoke(app)` hook with controlled frames/model outputs. The packager remains
non-executing. Native target wheels or missing host runtime can prevent this
test; report it as unverified and use the target runtime rather than replacing
dependencies with incompatible host wheels.

With a user-provided IP/user and SSH key or password, `scripts/deploy_app.py`
defaults to **upload only** with SHA-256 verification. For authorized device
installation/testing, choose `--action install` or `--action verify`. It uses
the official preflight/permission/async-operation APIs; verify also checks
start/stop/restart and current-instance results. Never silently replace an
existing App; `--replace` reflects user authorization for that specific App.
Device verification is not complete just because a PID or socket exists.
Use known scene/sample assertions for task accuracy and check requested
overlays, recording, GPIO/audio behavior separately. Without target access,
deliver the archive and clearly mark device acceptance **not run**.

## Output expectations

For an unsupported request, give a concise feasibility conclusion and the
precise blocker; do not create fake source files. For a supported request, name
the selected SDK layer, make the Python edit, call out the important runtime
preconditions and any unverified hardware-dependent path, and deliver the
packaged artifact for App Center requests: report the archive and build-report paths, bundled
versus platform-provided dependencies, and the fixed platform contract. If
packaging cannot run because a wheelhouse input is missing, state the missing
input explicitly. Report separate evidence for static/package validation,
host loader/mock loop, model numerical comparison (when applicable), device
upload/install/lifecycle/result delivery, and task-specific behavior. A failed,
pending or unverified stage must not be described as device acceptance passed.

## Maintaining the fixed contract

Run `python scripts/api_reference.py --sdk-root <checkout> --check` to compare
the complete documented SDK/Kit API surface, AppMgr routes, signatures, fields
and source hashes. Review changed behavior and update `api-notes.json` /
`api-http-notes.json` before `--write --revision <full-commit>`; do not regenerate
against an unreviewed version during ordinary app development. This check is
independent of the builder/manifest contract below and does not certify hardware.

Run `python scripts/sdk_contract.py --sdk-root <checkout>` to detect changes to
manifest/build/dependency admission, Kit APIs, recording and resource routing.
A mismatch requires review, not an automatic update during a user's build.
Both validators use the selected official manifest module. Build reports record
commit, dirty status and file hashes; exported non-Git overrides have unknown
commit/dirty status. API call and entry checks use AST only: dynamic exports are
reported as unverified by the static checks; only the separate smoke helper
executes trusted application code with a bounded lifetime. Run
`uv run --frozen pytest skill/recamera-pysdk/tests` when maintaining this skill.

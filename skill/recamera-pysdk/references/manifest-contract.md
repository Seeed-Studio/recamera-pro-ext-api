# Static App Contract

Use this reference when creating or reviewing an App directory. The bundled
`scripts/validate_app.py` enforces the parts that can be established without a
device. It is intentionally not a firmware, RKNN, camera, or OSD simulator.

## Provenance

This reference combines two evidence levels. Current SDK/Kit-derived rules are
the grouped manifest shape used by current official Apps, the current Kit
lifecycle, model declarations and aliases, Kit emit-envelope behavior, and
referenced resource existence. Additional Skill policy covers validation modes,
strict entry handling, App ID syntax, count-type heuristics, output uniqueness,
`default_mapping` publication gates, and command-execution restrictions. A Skill
policy failure is not automatically a claim about the base `recamera_ext` API.
The fixed platform contract (`compatibility` and `python.runtime_profile`), App
Center v2 package metadata, wheel descriptors, `release.lock.json`, and
`files.sha256` are generated and checked by the SDK's current
`market/packaging/` and `market/appmgr/` code; see `app-packaging.md`.

## Validation modes

| Mode | Purpose | Device required |
| --- | --- | --- |
| `demo` | Source-generation feedback; accepts an implicit `app.py` entry with a warning | No |
| `package` | Packaging gate; adds the fixed platform-contract and wheel-descriptor checks | No |
| `publish` | Public-release gate; adds a stable-channel requirement and payload-hygiene checks | No |

Run it directly with:

```text
python scripts/validate_app.py --app-dir <app> --mode demo
python scripts/validate_app.py --app-dir <app> --mode package
python scripts/validate_app.py --app-dir <app> --mode publish
```

The report always lists `runtime_preconditions_unverified`. Those entries mean
hardware or firmware testing remains; they do not invalidate an otherwise
correct offline Demo or package.

## Manifest identity and entry

- `manifest.json` must contain a JSON object.
- Skill/App Center publication policy requires `id` to match
  `[a-z0-9-]{1,64}` and `version` to be non-empty.
- `entry` must be a safe relative path to an existing Python entry file.
  Package and publish modes require it explicitly.
- For Apps with `models[]`, non-builtin `models[].classes`, or
  `artifacts[]`, keep the entry at the package root (for example `app.py`).
  Kit derives its resource root from the entry module's directory; a
  subdirectory entry makes package-root resource paths resolve to the wrong
  location and fails device-side authorization. The validator reports this as
  `kit_resource_path_mismatch`.
- Do not generate a `pipeline` field. The current public SDK has no consumer
  for it.

## Fixed platform contract (package and publish modes)

In `package` and `publish` modes the validator requires the fixed device
platform contract and rejects any other value, because the App Center compares
it against `APPMGR_PLATFORM_PROFILE` at install time:

- `compatibility.platform_profile = "rv1126b-linux-gnu-cp311-rknn232-v1"`
  (`missing_platform_contract` / `platform_contract_mismatch`)
- `compatibility.arch = "aarch64"` and `compatibility.python = "==3.11.*"`
- `python.runtime_profile` is a non-empty token that must not reuse
  `compatibility.platform_profile`
  (`runtime_profile_reuses_platform_profile`); first-party apps use
  `"recamera-ai-cp311-v1"`
- `python.isolation = "per-release"`

`python.wheels[]` descriptors are checked here too: a platform-owned project
(`numpy`, `cv2`, `kit`, `recamera-ext`, `rknnlite`, ...) is rejected with
`platform_owned_wheel`; the wheel tag must be `py3-none-any`, `cp311-none-any`,
or an `aarch64` platform tag with `py3-none` or `cp311-(cp311|abi3)`
(`unsupported_wheel_tag`); and `tags` must equal exactly the filename tag
(`invalid_wheel_tags`). `package_app.py` fills these fields into the staged
manifest; see [app-packaging.md](app-packaging.md).

## Configuration schema

Current official manifests use `config_schema.groups[].items[]`. Every item
needs a unique non-empty `key`. Types observed in current official Apps are
`number`, `integer`, `boolean`, `string`, `enum`, `zone`, and `line`; observed
apply modes are `live` and `restart`. This is a current Kit/App convention,
not an exhaustive public `recamera_ext` schema.

As an additional Skill consistency policy, use `integer` for count-like values
such as `interval_frames`, `*_count`,
`count_*`, `target_reps`, and `target_sets`. Do not infer type only from an
integer-looking default: seconds, angles, thresholds, and rates may still have
continuous semantics.

## Models and resources

`models` must be an array. Each model needs a unique `id`, a `task`, and a safe
relative `file` that exists in the App. A `classes` string is either a known
kit-provided name such as `coco80` or a non-empty App file.

`artifacts` must be an array when present. Each artifact requires `id`, `kind`,
`source`, `sha256`, `size`, `mount`, `required`, and `share_scope`. `kind` is
one of `data`, `dictionary`, `labels`, `onnx`, or `rknn`; `source` is `bundled`
or `catalog`; `share_scope` is `content` or `private`. For every bundled
artifact, `file` is also required, `file` must equal `mount`, and both must be
safe relative paths. The declared positive integer `size` and 64-character
hexadecimal `sha256` must match the real file. Kit and AppMgr must agree on the
exact package path; do not create a second model path in Python.

A bundled non-model resource (for example an alarm WAV the App plays through the
firmware ALSA path) is declared the same way, with `kind: "data"`. Keep it at the
package root so Kit and AppMgr resolve one identical path; this also avoids the
Windows sub-directory packaging problem described in `app-packaging.md`:

```json
{
  "id": "alarm-audio",
  "kind": "data",
  "source": "bundled",
  "file": "alarm.wav",
  "mount": "alarm.wav",
  "required": true,
  "share_scope": "private",
  "size": 123456,
  "sha256": "<64-hex-digest-of-the-real-file>"
}
```

`size` and `sha256` must match the real bundled file exactly (the validator
recomputes both), and `file` must equal `mount`. Unlike an `rknn` artifact, a
`data` artifact does not authorize inference; it only makes the resource
available to the App at its mount path.

Do not use `Path(__file__).parent` or `Path(__file__).parents[...]` to derive
an App/package root in generated Kit code. Kit resolves manifest-declared
models itself, and the entry module's `__file__` is not a stable package-root
reference under AppMgr.

Generated code may access a model by its manifest ID. A single model with a
known task may also have an unambiguous kit alias such as `det`, `pose`, `rec`,
or `lmk`. Prefer the exact form used by the closest current official App.

### Resource scheduling

If the App declares `resources.claims[]`, every claim must be an object.
For `npu.rknn`, AppMgr accepts only the scheduled service lane
(`scheduled` or `brokered`) or the legacy direct lane (`exclusive`). A
`shared` or omitted mode is rejected during device preflight with:

```text
npu.rknn must use scheduled service or exclusive legacy mode
```

Use `scheduled` for normal kit model Apps.

For `scheduled` and `brokered` NPU inference, AppMgr authorizes only bundled
RKNN artifacts. `models[]` supplies Kit model metadata such as task, input
shape, and classes, but it does not authorize the file. Therefore every
`models[].file` must have an `artifacts[]` entry whose `kind` is `rknn`,
`source` is `bundled`, and `file`/`mount` equals the model file. An empty
`artifacts` array fails on the device with:

```text
inference authorization failed: scheduled application <app-id> has no bundled RKNN artifact
```

### Managed result endpoints

Model Apps and Apps that publish through the App Center result path are
AppMgr-managed. They must declare `instances.endpoint_mode` as `allocated`.
AppMgr injects `RECAMERA_RESULT_GATEWAY_SOCK` and the authenticated instance
identity, and the Kit registry then selects its gateway-backed result sink.
The App itself must only call `kit.App.emit()`; it must not construct
`WsResultSink`/`GatewayResultSink`, select `ws` or `osd`, set the AppMgr
identity variables, or bind the reserved `127.0.0.1:8124` endpoint. See
[`managed-runtime.md`](managed-runtime.md) for the manual-debug distinction.
Such ordinary Kit model/output Apps must also declare exactly one
`resources.claims[]` entry with `name: "result.publish"` and
`mode: "brokered"`. In the current AppMgr, `shared` means `result.ingress` and
does not inject `RECAMERA_RESULT_GATEWAY_SOCK`; Kit then falls back to its
child-owned `127.0.0.1:8124` WebSocket sink. The validator reports this as
`managed_result_claim_not_brokered` before packaging.

## Output declaration

When `capabilities` contains `output`, `output` must declare:

- a non-empty `default_channel` string or string array;
- `default_mode` equal to `raw`, `custom`, or `ha`;
- a non-empty `fields` array;
- unique field `name` and `from` values;
- non-empty `name`, `from`, `type`, and `description` for every field.

As a Skill/App Center publication gate, public distribution includes a
non-empty `default_mapping`. Each mapping
must at least declare non-empty `source`, `target`, and `topic`. Its omission is
a package warning and a publish error. Full formatter or template semantics
must be checked against the pinned SDK documentation rather than reimplemented
by this validator.

For a Kit App, `output.fields[].from` must be present in the envelope produced by
an actual `self.emit()` call. `emit(extra={"alarm": value})` places `alarm` at
the envelope root; `extra.alarm` is not a valid path. Direct
`recamera_ext.ResultSink` output is a different API and is not described by Kit
manifest fields.

### Browser-renderable detection output

Any Kit App that declares a detector model and emits `results` must opt into
the strict browser result contract. It requires:

- `output.contract_version: 2` and `output.sink: "ws"`;
- a direct output field named `box` from `results[].box` with one of
  `coord: "pixel_xyxy"` or `coord: "normalized_xyxy"`;
- `render.schema_version: 1` and a `render.boxes` object.

The current official Kit detector convention uses original-frame pixel
`xyxy` boxes. The browser Result Hub uses this contract for canvas overlays;
publishing an envelope, logging detections, or drawing on an app-owned OpenCV
image alone does not make the official preview draw a box. `render.stream_osd`
is a separate optional declaration for boxes in RTSP/recording streams and is
not required for browser overlays. Direct `recamera_ext.ResultSink` Apps keep
their separate normalized-coordinate contract.

## Kit lifecycle checks

A current `kit.app.App` subclass must:

- declare `owns_loop = True`;
- define `run(self)` without additional positional arguments;
- call `super().setup(config)` when overriding `setup(config)`;
- not define removed callbacks `on_results`, `process_frame`, or
  `run_postproc`.
- avoid `Path(__file__)` for package resources, especially
  `Path(__file__).parents[...]`; the validator fails manual App-root derivation
  and warns on other entry-file path use.

All Python files must parse successfully. Skill safety policy rejects direct
access to `/dev/video*` or `/var/tmp/rkipc` because it bypasses or competes
with RKIPC. It also rejects shell execution and configurable subprocess
executables in public Apps; these are publication safety gates, not base API
syntax rules.

## What static validation cannot prove

Offline validation cannot prove that the target has the matching kit and
`recamera_ext` runtime, that RKNN can load a model, that extension sockets are
live, that ALSA/GPIO permissions are present, or that real events and overlays
appear correctly. Report those as runtime preconditions and perform optional
read-only device testing only when a device is available and the user permits
it.

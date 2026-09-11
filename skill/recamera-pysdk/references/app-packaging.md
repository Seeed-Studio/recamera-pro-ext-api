# App Center v2 Packaging Contract

This reference describes the current SDK/AppMgr v2 package contract. It is a
build contract, not an installation or device-modification procedure.

## Responsibilities

- This skill validates a supplied target wheelhouse, or creates an empty one
  when the App has no private wheel roots; resolves dependency metadata; copies
  accepted wheels into `wheels/`; fills the fixed platform contract into a
  staged manifest; and delegates archive creation to the official builder.
- The SDK's `market/packaging/build.py` is the authority for archive layout,
  deterministic tar creation, `release.lock.json`, and `files.sha256`.
- The skill bundles the fixed official builder files from public commit
  `525addec801680f6aabbbb7605d6de3bb390348a` under `scripts/sdk-builder/`;
  their SHA-256 values are checked before every default build.
- Device AppMgr verifies the detached signature according to device policy,
  safely extracts the archive, and builds a per-App/per-release environment. It
  does not run pip or access a package index.

The skill never installs packages, changes shared Python, signs an archive, or
handles a private signing key.

## Fixed platform contract

There is no configurable target profile. The device platform contract is fixed
in `scripts/package_app.py` and mirrors `market/appmgr/manifest.py`:

- `compatibility.platform_profile = "rv1126b-linux-gnu-cp311-rknn232-v1"`
- `compatibility.arch = "aarch64"`
- `compatibility.python = "==3.11.*"`
- `python.runtime_profile = "recamera-ai-cp311-v1"` (the label used by every
  first-party app; it must not reuse `compatibility.platform_profile`)
- `python.isolation = "per-release"`

`package_app.py` writes these into the staged manifest. If the source manifest
already declares one of them with a different value, the build fails rather than
silently overwriting it, because the App Center compares
`compatibility.platform_profile` against `APPMGR_PLATFORM_PROFILE` and refuses a
mismatch. `validate_app.py` checks the same contract in `package`/`publish`
modes.

## Inputs

Run `scripts/package_app.py` with the following inputs. `--wheelhouse`,
`--requirements`, `--sdk-root`, and `--payload-root` may be omitted:

```text
--app-dir       App source directory containing manifest.json and the entry
--out           Output directory
--wheelhouse    Optional complete target-compatible flat wheel closure; omitted means empty
--requirements  Optional exact root requirements; auto-discovered from the App dir
--sdk-root      Optional complete local SDK checkout override
--payload-root  Optional SDK payload overlay; may be repeated for declared artifacts
--mode          package (default) or publish; publish adds release-hygiene checks
```

The app manifest must be `manifest_version: 2`. The official SDK builder is
mandatory, but the skill already contains the fixed official copy, so no SDK
checkout is required for a default build. A complete local checkout can be
supplied explicitly with `--sdk-root` or `RECAMERA_SDK_ROOT`; a local override
must contain all three required builder files (`market/packaging/build.py`,
`market/appmgr/manifest.py`, `market/appmgr/__init__.py`). The helper never
searches the App tree, working directory, or user home for a checkout. The build
report records `builder.mode` as `bundled` or `local`, records the pinned
`sdk_source_commit` for bundled mode, and records only `caller-supplied-override`
for local mode (never the absolute path).

If `--requirements` is omitted, the helper uses the first exact dependency file
found in the App directory: `requirements.lock`, `requirements.txt`, or
`requirements-py311.txt`. That file is a build-time input only and is excluded
from the payload, because `manifest.python.wheels[]` is the single authoritative
declaration the device reads. If no requirements file exists, every wheel in the
wheelhouse is treated as a root and bundled.

## Minimal v2 manifest

The official builder enforces a closed v2 schema. A minimal no-resource App must
contain all of these fields (the platform-contract fields shown are the exact
values the helper fills and the validator requires):

```json
{
  "manifest_version": 2,
  "id": "demo-app",
  "name": "Demo App",
  "version": "1.0.0",
  "type": "self-hosted",
  "entry": "app.py",
  "release": {"sequence": 1, "channel": "dev"},
  "compatibility": {
    "platform_profile": "rv1126b-linux-gnu-cp311-rknn232-v1",
    "arch": "aarch64",
    "python": "==3.11.*"
  },
  "python": {
    "runtime_profile": "recamera-ai-cp311-v1",
    "isolation": "per-release",
    "imports": [],
    "wheels": []
  },
  "artifacts": [],
  "config_schema": {"groups": []},
  "resources": {"claims": []},
  "permissions": {
    "sdk": [],
    "filesystem": {"read": [], "write": []},
    "network": {"listen": [], "outbound": []}
  },
  "health": {
    "protocol": "kit-health-v1",
    "startup_timeout_sec": 1,
    "stabilization_sec": 0,
    "liveness_interval_sec": 1,
    "liveness_failures": 1,
    "restart": {"policy": "never", "max_attempts": 0, "window_sec": 1, "backoff_sec": [1]}
  },
  "instances": {
    "max": 1,
    "config_scope": "app",
    "data_scope": "app",
    "endpoint_mode": "allocated"
  },
  "capabilities": []
}
```

`version` must be SemVer, `type` must be `self-hosted`, and `release.channel`
must be `stable`, `beta`, or `dev`. Resource claims require the matching SDK
permission. This example is a minimal contract fixture, not a substitute for
app-specific models, output, configuration, and permissions.

## Dependency rules

The wheelhouse is an explicit input. The helper reads `METADATA` and
`Requires-Dist` from each wheel and resolves the complete transitive closure
offline. Missing wheels, unsatisfied version constraints, unsupported markers,
source distributions, duplicate distributions, unsafe wheel members, and
platform-owned wheel copies fail the build. The helper does not infer
dependencies from imports, does not download anything, and does not expand
wheels into `site-packages`. The resolved dependency edges are recorded in the
skill build report only; they are not added to the SDK-defined
`release.lock.json`.

### Wheel tag rule

`validate_wheel_filename_tag` applies `manifest.py:_validate_wheel` exactly:

- Portable wheels must be `py3-none-any` or `cp311-none-any`.
- AArch64 wheels must carry a platform tag ending in `aarch64` combined with
  either `py3-none` or `cp311-(cp311|abi3)`.
- `py3-none-linux_aarch64` is **valid**: the ABI is `none`, only the platform is
  architecture-specific. Do not reject it.
- Each wheel's `WHEEL` `Tag:` entries must equal exactly the filename tag, and
  the bundled descriptor's `tags` must be `[filename_tag]`.

### Platform-owned projects

`appmgr/pythonenv.py` creates the per-release venv with
`--system-site-packages`, so the following projects are already importable on
the device. An app wheel that shadows one is rejected at install time, so the
helper refuses to bundle them. Import them directly instead:

```text
cv2, jinja2, kit, markupsafe, numpy, recamera-ext, recamera-pro-kit,
rknn-toolkit-lite2, rknnlite
```

A dependency on a platform-owned project is recorded in the build report as
`platform_packages` with `source: "platform"` and `version: null`; its version
constraint cannot be verified offline because the SDK does not publish that
version.

### Wheel archive admission

`check_wheel_archive` mirrors `pythonenv.py` admission so install cannot fail
later. It rejects `.data` layouts, symlinks, encrypted members, duplicate
members, split `.dist-info`, non-AArch64 ELF payloads (ELF machine 183 only),
and more than one `METADATA`/`WHEEL`/`RECORD`. Per-release caps: package
`<= 200 MiB`, unpacked `<= 400 MiB`, members `<= 4096`, icon `<= 1 MiB`, wheel
`<= 512 MiB`, wheel members `<= 8192`, environment unpacked `<= 512 MiB`.

The wheelhouse must be a flat directory of regular `.whl` files; nested
directories, symlinks, sdists, and unrelated files are rejected so a dependency
cannot be silently omitted.

## Package contents

The SDK builder receives a temporary App tree containing:

```text
manifest.json
app.py
...application files...
wheels/<distribution-version-python-abi-platform>.whl
```

Every `manifest.artifacts[]` entry with `source: "bundled"` must also have its
declared `file` present in the App tree (or in an explicitly supplied SDK
`--payload-root`), with a positive integer `size` and a `sha256` matching the
real file. The builder fails closed when a declared model or artifact is
unavailable. For scheduled or brokered `npu.rknn`, every `models[].file` must
also be a bundled `kind: "rknn"` artifact; a model file merely copied into the
tree but omitted from `artifacts[]` is not authorized for scheduled inference.

For bundled artifacts, `file` must equal `mount`. Keep the entry at the package
root (for example `entry: "app.py"` with `models/x.rknn` and `labels.txt` beside
it). A subdirectory entry with package-root resources fails the validator with
`kit_resource_path_mismatch` rather than allowing an install-time model
authorization failure. Do not derive the App root in Python with
`Path(__file__).parents[...]`.

**Windows packaging caveat.** The pinned official builder computes archive member
names with `os.path.relpath`, which yields backslash separators for files in
sub-directories when run on Windows. A tar member such as `models\helmet.rknn`
will not extract to the intended path on the device, so the install fails even
though static validation passed. Keep bundled resources at the package root (no
sub-directory) when building on Windows, or build on a POSIX host. The bundled
`package_app.py` reopens the final archive and fails closed if any member name
contains a backslash; do not bypass that check, and confirm the real member names
inside the produced `.tar.gz` rather than trusting validation alone.

The resulting archive additionally contains the SDK-generated:

```text
release.lock.json
files.sha256
```

Each bundled wheel is described in `manifest.json` under `python.wheels[]`,
including `name`, `version`, `filename`, `file`, `sha256`, `size`, `tags`, and
`source: "bundled"`. Catalog artifacts are a separate SDK publication concern;
this skill does not invent catalog descriptors for platform Python packages. Do
not add `python/site-packages/` or `requirements.lock.json` to a v2 package.

## Reproducibility and signing

The official builder creates the deterministic `.tar.gz` and release metadata.
The skill reports the archive SHA-256. Signing is a separate release operation:
current device policy requires an ECDSA P-256/SHA-256 detached signature over
the exact archive bytes, placed outside the archive. The skill must not generate
or store the release private key. An unsigned archive that passes this build
contract is not a promise that every device or publication channel will accept
it; signature admission remains an external AppMgr/release-service decision.

## Offline verification

Run the skill validator before packaging. The helper reopens the exact final
`.tar.gz` and verifies the archive's `manifest.json`, `release.lock.json`, and
`files.sha256` against the payload bytes, enforces the size and member caps, and
checks App identity. For scheduled/brokered NPU Apps it also requires every
declared model file to have a bundled `kind: "rknn"` artifact in that final
archive. The helper reports the archive SHA-256 and deletes a stale same-name
output before building. No device is needed for static validation. A successful
build proves package structure and metadata consistency, not model loading,
socket availability, hardware permissions, signature acceptance, or inference
performance.

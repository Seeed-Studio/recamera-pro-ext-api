# App package manifest v2

Manifest v2 makes an application release reproducible and binds its code,
offline Python dependencies, resource contract, permissions and lifecycle
policy into one authenticated package. The production contract is implemented
by `market/appmgr/manifest.py`; both the host packager and device installer use
that same validator. `market/appmgr/schema/manifest-v2.schema.json` is the
editor/tooling representation of the contract.

## Compatibility policy

- A manifest with no `manifest_version`, or with `manifest_version: 1`, remains
  installable as a legacy v1 package. It uses the historical shared runtime and
  does not get release lock/BOM or a per-release environment.
- A v2 manifest is closed by default. Unknown core fields fail validation;
  vendor extensions must use an `x-*` key.
- `id` is `[a-z0-9-]{1,64}`, `version` is SemVer, and `entry` is a normalized
  app-relative Python path.
- The current platform contract is `aarch64`, CPython `==3.11.*`, profile
  `rv1126b-linux-gnu-cp311-rknn232-v1`, and exactly one instance per app.
- Runtime endpoints are allocated. A fixed `output.port` is rejected.

## Application icon

An installable thumbnail is declared independently of the catalog image:

```json
{
  "image": "/appcenter/apps/example-app.png",
  "icon": {"path": "icon.png", "media_type": "image/png"}
}
```

`image` remains the pre-install catalog URL. The optional v2 `icon` object
binds the installed app to a package member; after installation appmgr exposes
that authenticated content through the app list's `icon_url`. `path` must be a
normalized package-relative regular file and is included in `files.sha256` and
the release identity. The file may be at most 1 MiB. Supported pairs are
`.png`/`image/png`, `.webp`/`image/webp`, and `.jpg` or `.jpeg`/`image/jpeg`;
the extension, media type, and file magic must agree. Active formats such as
SVG, HTML, and JavaScript are intentionally rejected because icons are served
from the device's authenticated origin.

Adding or replacing the icon changes the BOM and therefore the `release_id`.
At minimum, increase `release.sequence`; published examples should also bump
their semantic `version`. Reusing one sequence for different bytes is rejected
as release equivocation. A same-version package with a higher sequence remains
valid, and its installed `icon_url` is isolated by the icon content hash.

The builder packages the complete application tree minus its explicit junk
deny-list and `package.exclude`, so a declared `icon.png` is included without a
special include list. Excluding or omitting a declared icon fails the build.

The required v2 sections are:

```json
{
  "manifest_version": 2,
  "id": "example-app",
  "name": "Example",
  "version": "1.0.0",
  "type": "self-hosted",
  "entry": "src/main.py",
  "release": {"sequence": 1, "channel": "stable"},
  "compatibility": {
    "platform_profile": "rv1126b-linux-gnu-cp311-rknn232-v1",
    "arch": "aarch64",
    "python": "==3.11.*",
    "kit_api": ">=0.2,<0.3"
  },
  "python": {
    "runtime_profile": "system-cp311-rknn232",
    "isolation": "per-release",
    "wheels": [],
    "imports": []
  },
  "artifacts": [],
  "config_schema": {"revision": 1, "groups": []},
  "resources": {"claims": []},
  "permissions": {
    "sdk": [],
    "filesystem": {"read": ["app"], "write": ["appdata", "tmp"]},
    "network": {"listen": [], "outbound": []}
  },
  "health": {
    "protocol": "kit-health-v1",
    "startup_timeout_sec": 30,
    "stabilization_sec": 1,
    "liveness_interval_sec": 10,
    "liveness_failures": 3,
    "restart": {
      "policy": "on-failure",
      "max_attempts": 3,
      "window_sec": 60,
      "backoff_sec": [1, 2, 5]
    }
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

NPU users declare `npu.rknn` with `mode: scheduled`. `brokered` remains
accepted during migration. CPU-only apps omit the NPU claim. Claims currently
cover `camera.frames`, `audio.capture`, `npu.rknn`, `rga`, `codec.decode`,
`probe.read`, and `result.publish`.

## Offline Python dependencies

Every wheel descriptor binds the project name, exact version, filename, one
wheel tag, SHA-256 and byte size. A bundled wheel uses
`wheels/<filename>`; a catalog wheel is resolved only by its digest from the
device wheelhouse. AArch64 platform wheels may use `py3-none` or
`cp311-(cp311|abi3)`; portable wheels must be `py3-none-any`. Apps cannot replace platform-owned packages such as
`numpy`, `cv2`, `rknnlite`, `kit` or `recamera_ext`.

The installer never invokes pip, an index, a build backend, or the network. It
validates all wheel members first, rejects traversal, links, `.data` layouts,
collisions, wrong METADATA name/version and non-AArch64 ELF payloads, and then
expands them into an offline candidate. Import probes run with `-I`,
`PYTHONNOUSERSITE=1`, and no inherited `PYTHONPATH`.

```text
/userdata/local/wheelhouse/sha256/<wheel-sha256>/<filename>
/userdata/local/venvs/<app-id>/releases/<release-id>/
/userdata/local/venvs/<app-id>/current -> releases/<release-id>
/userdata/local/venvs/<app-id>/rollback.json
```

The runtime must select `pythonenv.current_python(app_id)` for v2 and use the
legacy interpreter for v1. A missing or invalid v2 `current` pointer is a hard
startup failure, not permission to fall back to another environment.

## Release lock and BOM

Run the existing builder:

```sh
python3 market/packaging/build.py apps/example-app \
  --payload-root /srv/recamera/models \
  --payload-root /srv/recamera/wheels \
  --out market/packaging/dist
```

`--payload-root` is repeatable and is intended for large, controlled assets
that are not checked into the source repository. Roots are searched in command
line order. The builder only reads a `source: "bundled"` wheel or artifact at
its exact manifest-declared relative path, and only when that path is absent
from the app source tree. It ignores every undeclared file, rejects links and
non-regular files, never shadows source, and still enforces the declared size
and SHA-256 before creating an output. Consequently the same code, manifest,
payload bytes and root order produce the same package bytes.

For v2 it generates two files without modifying the source tree:

- `files.sha256`: canonical, sorted `sha256  size  path` records for every
  payload file;
- `release.lock.json`: the exact app/release identity, compatibility, Python
  lock, artifact lock, manifest digest and BOM digest.

The `release_id` is derived from the manifest and full payload BOM. The device
re-hashes every authenticated tar member and requires an exact lock/BOM match;
missing, extra, reordered or changed payload records are refused before any
installed release is changed. Against an installed v2 app, `release.sequence`
must increase; the exact same release may be reinstalled, but a lower sequence
or different bytes reusing the same sequence is rejected.

Bundled artifacts are included in the BOM and checked against their manifest
descriptor. Catalog artifacts and catalog wheels must already have been
content-addressed and admitted by the upload/catalog finalize layer; package
installation never downloads them.

## Verification and transaction boundary

The vendor public key is immutable firmware content at
`/usr/lib/recamera/appmgr/keys/release_pub.pem`. Device-owner public keys may be
provisioned as direct `*.pem` children of
`/userdata/local/appmgr/keys/owners/`; they extend and cannot replace the vendor
anchor. `APPMGR_RELEASE_PUBKEY` and `APPMGR_OWNER_KEYS_DIR` override those paths
for controlled deployments. Keys are opened read-only without following links,
must be regular, owned by appmgr's effective uid, and not group/world-writable,
and are subject to count/size caps. Every owner `*.pem` must parse as a public
key or the entire store fails closed. Successful verification reports
`signer_kind` and a canonical SPKI
SHA-256 `key_fingerprint`. A supplied-but-invalid signature is always rejected,
and packages containing signing-key/trust-store material are refused before
extraction.

`installer.inspect(path, signature)` follows the global signature policy. The
only normal product flow allowed to pass `allow_unsigned=True` is the
authenticated, same-origin local Web upload route. nginx overwrites a private
route stamp only after its JWT `auth_request` succeeds; appmgr additionally
requires a validated browser `Origin`, an origin-less API/Bearer request cannot
select this channel, and `source`/`channel` are server-minted upload metadata
rather than client JSON fields. The current React client also sends an
`Authorization: Bearer ...` header; its presence does not negate local-Web
provenance once nginx supplied the post-auth stamp and Origin matched. Direct, future cloud and legacy install routes
do not inherit this exception and remain subject to the signed-package policy.

For an unsigned local upload, preflight returns a `critical`
`unsigned-root-code` warning and an `unsigned_confirmation` contract. Finalize
must repeat the exact manifest permissions and send
`unsigned_risk_confirmed: true`. Missing that explicit risk confirmation fails
closed; `developer_mode` is an obsolete compatibility field and has no bearing
on admission. A supplied-but-invalid or forged signature is never treated as unsigned. A
successful unsigned install is committed in `stopped` state and never
automatically starts or restarts an application; starting it is a separate,
explicit lifecycle operation. The returned `preflight` object also includes
the manifest version, release id, compatibility, resources, permissions,
health and instance policy.

Signature verification and tar parsing use one held file descriptor. On Linux
openssl verifies `/proc/self/fd/N`; a proc-less system uses a private snapshot
whose inode is checked after verification. The installer never reopens the
package path to extract it.

Installation orders its mutations as follows:

1. verify signature, schema, compatibility, member limits, hashes and BOM;
2. extract code and build the complete offline environment as candidates;
3. atomically publish the code directory;
4. atomically publish/switch the matching environment generation.

Installation validates that the manifest's resource declarations can be
translated into a static resource plan, but it neither checks nor reserves
current live resources and does not probe runtime dependencies. Those mutable
conditions are sampled atomically by `Coordinator.start()` immediately before
launch. Thus another running application's reservation can delay/reject a
later start, but cannot prevent an otherwise valid package from being
inspected or installed. Upgrade preflight uses the new manifest defaults and
does not read or migrate the installed version's config overlay; installation
revalidates that overlay against the new schema, and start computes the exact
effective profile afterward.

For a managed generation, `resources.limits.memory_mb`, `storage_mb`, and
`cpu_percent` are recorded as soft reservations for diagnostics. Shared memory
does not have a built-in declared-budget aggregate cap: existing processes are
already represented in `MemAvailable`, so summing their manifest maxima as a
second hard gate would reject otherwise viable workloads. Storage retains an
8192 MiB declared-budget reservation cap because current free space cannot
account for data that already-running apps may write later. CPU has no default
aggregate cap. An operator may configure calibrated caps with
`APPMGR_MANAGED_MEMORY_CAP_MB`, `APPMGR_MANAGED_STORAGE_CAP_MB`, and
`APPMGR_MANAGED_CPU_CAP_PERCENT`; a value of `0` leaves that declared-budget
dimension uncapped. The managed camera-frame default is four shared subscribers,
matching the frame proxy's advertised and enforced lower-layer limit. Immediately before reserving, appmgr re-reads
`MemAvailable`, app-data free space, and the highest valid thermal-zone
temperature; it applies only the new generation's declared envelope, keeps
256 MiB system-memory and 128 MiB storage headroom by default, and defers starts
at 100 C or above. While an app is
running, the reconciler applies a separate 110 C hard thermal fence: it stops
and releases the exact generation, leaves the desired state running, and only
retries after the lower start gate is satisfied. All thresholds/capacities are
reported by `GET /api/app-center/v1/resources.runtime_admission`. Deployments
may tune them with `APPMGR_MANAGED_MEMORY_CAP_MB`,
`APPMGR_MANAGED_STORAGE_CAP_MB`, `APPMGR_MANAGED_CPU_CAP_PERCENT`,
`APPMGR_SYSTEM_MEMORY_HEADROOM_MB`, `APPMGR_STORAGE_HEADROOM_MB`,
`APPMGR_START_MAX_TEMP_C`, and `APPMGR_RUNTIME_HARD_TEMP_C`.
The thermal overrides must both be finite and the start threshold must remain
strictly below the runtime threshold. Invalid individual values use their
100/110 C defaults. If a configured start threshold is at/above the hard fence,
appmgr retains the hard fence and lowers the start threshold by 10 C rather
than raising the safety limit.

If an application declares a memory or storage budget but the corresponding
live telemetry cannot be read or parsed, start admission fails closed with a
`memory.telemetry` or `storage.telemetry` reason. Package upload and
installation remain unaffected because they do not sample mutable runtime
capacity.

These are admission reservations and a thermal safety fence, not kernel
cgroup/rlimit isolation. The current target kernel does not provide the
per-application cgroup boundary needed for trustworthy hard CPU/RSS
enforcement; manifests must therefore use conservative limits, and a future
sandbox/cgroup implementation should treat these same fields as hard QoS.

Any ordinary exception or `BaseException` during publication restores the old
code and interpreter together. A later READY/start failure calls
`installer.restore_prev(app_id)`, which performs the same paired rollback.
Orphan `.stage` directories are safe to remove; immutable releases are only
removed when they are not the active `current` target.

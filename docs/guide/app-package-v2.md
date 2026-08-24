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

`installer.inspect(path, signature)` follows the global signature policy.
Authenticated developer-mode callers may explicitly use
`installer.inspect(path, signature, allow_unsigned=True)` and later
`installer.install(..., allow_unsigned=True)`; this exception must never be
inferred from package content. The returned `preflight` object includes the
manifest version, release id, compatibility, resources, permissions, health,
instance policy and whether unsigned developer mode was used.

Signature verification and tar parsing use one held file descriptor. On Linux
openssl verifies `/proc/self/fd/N`; a proc-less system uses a private snapshot
whose inode is checked after verification. The installer never reopens the
package path to extract it.

Installation orders its mutations as follows:

1. verify signature, schema, compatibility, member limits, hashes and BOM;
2. extract code and build the complete offline environment as candidates;
3. atomically publish the code directory;
4. atomically publish/switch the matching environment generation.

Any ordinary exception or `BaseException` during publication restores the old
code and interpreter together. A later READY/start failure calls
`installer.restore_prev(app_id)`, which performs the same paired rollback.
Orphan `.stage` directories are safe to remove; immutable releases are only
removed when they are not the active `current` target.

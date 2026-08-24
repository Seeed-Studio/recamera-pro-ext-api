# Build and test entry points

The repository root is a development-only uv workspace. It locks the two
publishable packages together while retaining their existing import names. The
root test environment does not install the members editable, so subprocess
tests still exercise the repository's explicit `PYTHONPATH`/installation
boundaries:

| Workspace member | Distribution | Import |
|---|---|---|
| `sdk/python` | `recamera-ext` | `recamera_ext` |
| `kit` | `recamera-pro-kit` | `kit` |

`recamera-ext` uses Python distribution version 1.4.0 for the broker-backed
inference lease API. The independently versioned native SDK is 1.3.0; its C
ABI/SONAME remains `librecamera_ext.so.1`, with lease symbols added compatibly.

The supported device interpreter is Python 3.11. The development lock uses
NumPy 1.23.5 to match the RV1126B firmware rather than silently testing against
a newer host ABI. It also installs host-only `opencv-python-headless` for image
and post-processing tests; this wheel is not added to either device package.

## Lock and build

```sh
uv lock
uv lock --check
uv build --all-packages --wheel --offline
```

`uv build --offline` requires the PEP 517 build backend from the uv cache (or a
pre-seeded CI cache); it never needs target hardware or the native shared
library.

## Host tests

```sh
scripts/test-host.sh
```

This runs the fake-native SDK tests, app and kit suites, appmgr lifecycle tests,
the self-contained catalog tests, and packaging tests with the locked Python
3.11 environment. Hardware-only RKNN, socket and shared-library checks do not
belong in this suite. Tests that consume staged packages/models or an adjacent
training repository are reported as skips here and have a separate mandatory
release gate.

## Native SDK reproducibility and ABI gates

The native client has a separate compiled gate because Python fake-library
tests cannot prove that the C source closure builds or that its ELF ABI is
correct:

```sh
scripts/test-native-sdk.sh --host
scripts/test-native-sdk.sh --aarch64
```

Both modes copy only `sdk/` into a temporary directory before configuring, so
an undeclared dependency on a neighbouring `recamera_ipc` checkout fails. They
run the same CTest suite: pinned protobuf-c byte-drift regeneration, source
closure, LP64 structure layout compilation, public-symbol baseline, SONAME,
`libprotobuf-c.so.1` dependency and build-tree symlink checks. Cross mode only
inspects the target ELF on the host; it does not try to execute aarch64 code.

The pinned generator is protobuf-c 1.4.1 with libprotoc 3.21.12. Set
`PROTOC_C`, `PROTOBUF_C_PREFIX`, `AARCH64_CC`, or `AARCH64_SYSROOT` when the
tools are not in the enclosing RV1126B SDK layout. See `sdk/README.md` and
`sdk/SOURCE_PROVENANCE.md` for the source/licensing boundary.

## Release-artifact tests

After staging the app packages and model payloads, and checking out the shared
`sscma-example-sg200x` training repository beside this repository, run:

```sh
scripts/test-release-artifacts.sh
```

This gate deliberately fails when any referenced artifact is absent or its
catalog hash/size does not match. It is separate from host unit tests so a clean
source checkout remains testable without weakening the release checks.

## Package smoke test

```sh
scripts/test-packages.sh
```

The script builds both wheels offline into a temporary directory, checks their
contents (including rejecting test modules from production wheels), installs
only those artifacts into a fresh Python 3.11 virtual environment, and verifies
the `recamera_ext`, `kit`, and `kit.workflow` imports from site-packages rather
than the source checkout. The kit's setuptools build hook also prunes excluded
tests from an incremental `build/lib` tree so stale artifacts cannot leak into
a later wheel.

## Device smoke test

After installing the wheels and native library on a device whose firmware
provides the extension endpoints:

```sh
PYTHON_BIN=/usr/bin/python3 scripts/test-device.sh
```

Use the firmware system interpreter here so the check exercises the packages
installed into the image staging tree, rather than an older `/userdata` venv
that could shadow them.

This is only a presence/load smoke check: it checks that the four paths are
Unix socket inodes, verifies the firmware-compatible NumPy line and both Python
imports, verifies the lease symbols, dynamically loads `librecamera_ext.so.1`,
and imports/constructs the offline-staged `rknn-toolkit-lite2==2.3.2` wrapper.
The RKNN check deliberately does not load a model or call `init_runtime()` or
`inference()`, so it does not allocate an NPU context or contend with rkipc.

It does **not** perform a protocol handshake, validate negotiated protocol
versions, acquire a frame or inference lease, submit a result, initialize RKNN,
or exercise crash recovery. Those are separate device end-to-end tests and
must pass before publishing a firmware/SDK pair.

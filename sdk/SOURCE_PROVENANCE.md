# Native client source provenance

The initial public native-client source snapshot was recovered from the
complete `recamera_ipc` handoff bundle at commit
`5bbf27995e095f3cbd9292dde99eccd3ce0606ce` (`feat/ext-api`). It contains the
client implementation that produced the previously distributed
`librecamera_ext.so.1` ABI:

- `sdk/src/`: result, mask, frame and probe clients plus shared transport code;
- `sdk/proto/`: canonical public wire schemas;
- `sdk/generated/`: committed protobuf-c output used by offline/cross builds.

The public header in `sdk/include/recamera_ext.h` is byte-identical to the
handoff snapshot. Python bindings in this repository intentionally remain the
newer, hardened implementation and are tested against the same ABI.

New client-side protocol work belongs here. The firmware repository owns the
server implementation. When a protocol schema changes, regenerate both sides
with the pinned protobuf toolchain and make the drift/compatibility tests pass
before publishing either artifact.

## Reproducibility boundary

The committed protobuf-c output is generated with `protobuf-c 1.4.1` linked
against `libprotoc 3.21.12`. `sdk/tools/check_generated.py` rejects any other
tool versions and compares all four generated files byte-for-byte. The native
gate builds from an isolated copy containing only `sdk/`, which prevents an
accidental dependency on a neighbouring firmware checkout.

The recovered source, public header, generated code and CMake recipe reproduced
the previously distributed aarch64 `librecamera_ext.so.1.0.0` byte-for-byte
before subsequent additive protocol/client work. This is provenance evidence,
not a promise that a changed source tree must keep matching an older committed
binary, and not a replacement for ABI and device integration tests.

## License

The recovered native client material retains its original BSD-3-Clause
license; it is not relicensed by this repository's Apache-2.0 root license.
The exact path scope is recorded in `sdk/NOTICE`, and the applicable license
text is in `sdk/LICENSE`. Later modifications and additive native client work
inside that path scope use the same BSD-3-Clause terms.

Preserve the recovered copyright notices, `sdk/LICENSE`, `sdk/NOTICE` and this
provenance record when redistributing the native source or binaries.

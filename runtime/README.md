# Device AI runtime inputs

The RV1126B base image already provides Python 3.11, NumPy 1.23.5, OpenCV,
Jinja2 and MarkupSafe. `requirements.lock` pins only the missing device-side
RKNN runtime and its imports. The top-level app Makefile verifies every wheel
by SHA-256 and installs it offline into the firmware staging tree; it never
runs pip on the target or accesses the network.

The currently tracked wheel files remain under `release/pkg/wheels/` for
repository-history compatibility. That does **not** make the archived sideload
package deployable: only these individually locked runtime inputs are consumed
by the source build. The rknn-toolkit-lite2 wheel reports Rockchip commit
`1482e03`; its compiled extensions target AArch64 glibc/CPython 3.11.

Before a public firmware release, confirm the redistribution terms for every
binary wheel and record its upstream URL/license in the release BOM. Updating a
wheel requires updating the hash here and re-running the AArch64 staging/device
smoke gates; never replace a file while retaining its old name/hash.

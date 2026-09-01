#!/bin/sh
# recamera-ext-api install.sh  (persistent sideload into /oem, survives reboot)
# Run ON THE DEVICE as root:  adb push <this dir>/* /userdata/ext-pkg/ ; adb shell "sh /userdata/ext-pkg/install.sh"
# Idempotent: backs up factory files once, md5-verifies every artifact, then overwrites /oem.
set -e

# This directory is an archived sideload snapshot, not a releasable package.
# Its checked-in binaries do not match MANIFEST.txt/install.sh and the rkipc
# hash 9826e9... is classified as both factory and extension in the historic
# lists below.  Installing by merely updating one checksum could destroy the
# only valid rollback target.  A source-built release pipeline must regenerate
# all artifacts, hashes and the mutually-exclusive factory/extension policy,
# then deliberately remove this guard.
echo "FATAL: release/pkg is an archived, internally inconsistent snapshot." >&2
echo "Build rkipc, entry.cgi and librecamera_ext from the pinned source tree; do not sideload this directory." >&2
exit 1

PKG=$(cd "$(dirname "$0")" && pwd)
RKIPC_MD5=f683352a9d062a05a3df1f8df22d7d53
ENTRY_MD5=75a693c87c317a49c37c4dddb6b9ac7a
SO_MD5=5cebfb9e4d9c001c45b58c75daafe934
# Rollback safety -- a valid rollback target MUST be a genuine, unmodified factory rkipc.
#   VERIFIED_FACTORY_MD5S : clean factory rkipc md5s (0 extension sockets). The ONLY md5s
#     accepted as a rollback target. To add a device's factory: confirm
#     `strings rkipc | grep /run/recamera` prints nothing, then append its md5 here.
#   KNOWN_EXT_BUILD_MD5S  : rkipc builds that CARRY the extension endpoints. Listed so we
#     NEVER mistake one for factory -- an ext build (incl. our own shipped rkipc) must not be
#     captured as, or restored as, "factory", or the rollback becomes a silent no-op.
VERIFIED_FACTORY_MD5S="9826e9ecf8ed543a6dc78e3731102e0f"   # V1.0.x clean factory (1.9MB, 0 ext sockets)
KNOWN_EXT_BUILD_MD5S="9826e9ecf8ed543a6dc78e3731102e0f f93ac217c9920bc962771aeed1ac0550 f683352a9d062a05a3df1f8df22d7d53"  # ext builds -- NOT rollback targets

md5of() { md5sum "$1" 2>/dev/null | awk '{print $1}'; }
need() { [ "$(md5of "$1")" = "$2" ] || { echo "FATAL md5 mismatch: $1 (got $(md5of "$1") want $2)"; exit 1; }; }
in_list() { _v=$1; shift; for _m in $*; do [ "$_v" = "$_m" ] && return 0; done; return 1; }
is_factory()   { in_list "$1" $VERIFIED_FACTORY_MD5S; }
is_ext_build() { in_list "$1" $KNOWN_EXT_BUILD_MD5S; }

echo "=== [1/7] verify package artifacts ==="
need "$PKG/rkipc" "$RKIPC_MD5"
need "$PKG/entry.cgi" "$ENTRY_MD5"
need "$PKG/sdk/lib/librecamera_ext.so.1.0.0" "$SO_MD5"
echo "package OK"

echo "=== [2/7] backup factory rkipc (once, verified-factory only) ==="
CUR=$(md5of /oem/usr/bin/rkipc)
if [ -f /userdata/rkipc.factory.bak ]; then
  BAK=$(md5of /userdata/rkipc.factory.bak)
  if is_factory "$BAK"; then
    echo "backup exists, verified factory: $BAK"
  else
    echo "FATAL: existing /userdata/rkipc.factory.bak ($BAK) is NOT a verified factory rkipc."
    echo "       Restoring it would leave a non-factory (likely extension) build on /oem."
    echo "       Replace the backup with a true factory rkipc, then re-run."; exit 1
  fi
else
  # First install: only capture a backup if /oem currently holds a clean factory rkipc.
  # (Capturing an already-installed ext build as "factory" is exactly what breaks rollback.)
  if is_factory "$CUR"; then
    cp /oem/usr/bin/rkipc /userdata/rkipc.factory.bak
    echo "saved /userdata/rkipc.factory.bak (verified factory $CUR)"
  elif is_ext_build "$CUR"; then
    echo "FATAL: /oem rkipc ($CUR) is a known EXTENSION build, not clean factory, and no"
    echo "       factory backup exists -- refusing to capture an ext build as 'factory'."
    echo "       Restore the true factory rkipc first (md5 one of: $VERIFIED_FACTORY_MD5S)."; exit 1
  else
    echo "FATAL: /oem rkipc ($CUR) is neither a verified factory nor a known build."
    echo "       Refusing to guess a rollback target. Verify this device's factory rkipc"
    echo "       (strings rkipc | grep /run/recamera  must be empty), append its md5 to"
    echo "       VERIFIED_FACTORY_MD5S, then re-run."; exit 1
  fi
fi

echo "=== [3/7] backup factory entry.cgi (once) ==="
if [ ! -f /userdata/entry.cgi.factory.bak ]; then
  cp /oem/usr/www/cgi-bin/entry.cgi /userdata/entry.cgi.factory.bak
  echo "saved /userdata/entry.cgi.factory.bak"
else
  echo "entry.cgi backup exists"
fi

echo "=== [4/7] verify /oem writable ==="
touch /oem/.wtest && rm -f /oem/.wtest && echo "OEM writable" || { echo "FATAL /oem not writable"; exit 1; }

echo "=== [5/7] install into /oem (persistent) ==="
cp "$PKG/rkipc"    /oem/usr/bin/rkipc                    && chmod 755 /oem/usr/bin/rkipc
cp "$PKG/entry.cgi" /oem/usr/www/cgi-bin/entry.cgi       && chmod 755 /oem/usr/www/cgi-bin/entry.cgi
cp "$PKG/sdk/lib/librecamera_ext.so.1.0.0" /oem/usr/lib/ && chmod 755 /oem/usr/lib/librecamera_ext.so.1.0.0
ln -sf librecamera_ext.so.1.0.0 /oem/usr/lib/librecamera_ext.so.1
ln -sf librecamera_ext.so.1     /oem/usr/lib/librecamera_ext.so
# SDK python + header for solution vendors (non-/oem, survives OTA)
mkdir -p /userdata/sdk/python
cp -R "$PKG/sdk/python/recamera_ext" /userdata/sdk/python/
cp "$PKG/sdk/recamera_ext.h" /userdata/sdk/ 2>/dev/null || true
sync
echo "installed. post md5:"; md5of /oem/usr/bin/rkipc; md5of /oem/usr/www/cgi-bin/entry.cgi; md5of /oem/usr/lib/librecamera_ext.so.1.0.0

echo "=== [6/7] provision rknnlite python runtime (best-effort) ==="
# Adds the Python inference runtime (rknn-toolkit-lite2 + deps) so vision apps run
# out of the box. Failures here only WARN -- the rkipc install above is already done,
# so we never block the main path.  Idempotent: skips work already present.
RKNNENV=/userdata/rknnenv
WHEELS="$PKG/wheels"
provision_rknnlite() {
  # 1) stock rknnlite hardcodes /usr/lib/librknnrt.so -- point it at the OEM copy.
  if [ ! -e /usr/lib/librknnrt.so ]; then
    if [ -e /oem/usr/lib/librknnrt.so ]; then
      ln -sf /oem/usr/lib/librknnrt.so /usr/lib/librknnrt.so && echo "  linked /usr/lib/librknnrt.so -> /oem/usr/lib/librknnrt.so"
    else
      echo "  WARN: /oem/usr/lib/librknnrt.so missing -- rknnlite will fail to load"
    fi
  else
    echo "  /usr/lib/librknnrt.so already present"
  fi
  # 2) venv with system site-packages (numpy comes from the system, not a wheel).
  if [ ! -d "$RKNNENV" ]; then
    python3 -m venv --system-site-packages "$RKNNENV" && echo "  created venv $RKNNENV" || { echo "  WARN: venv create failed"; return 1; }
  else
    echo "  venv $RKNNENV already exists"
  fi
  # 3) offline install of the aarch64/cp311 wheels (device has no network).
  "$RKNNENV/bin/pip" install --no-index --find-links "$WHEELS" \
      rknn-toolkit-lite2 psutil ruamel.yaml ruamel.yaml.clib jinja2 markupsafe \
    && echo "  wheels installed offline from $WHEELS" || { echo "  WARN: offline pip install failed"; return 1; }
  # 3b) make recamera_ext importable INSIDE the venv. It is copied to
  #     /userdata/sdk/python (step [5/7]), which is not on the venv's sys.path,
  #     so any app using the official FrameSource dies with
  #     "ModuleNotFoundError: No module named 'recamera_ext'". A .pth survives
  #     wipes of /userdata/rknnenv because we rewrite it on every install.
  SITEDIR=$("$RKNNENV/bin/python3" -c 'import site;print(site.getsitepackages()[0])' 2>/dev/null)
  if [ -n "$SITEDIR" ] && [ -d "$SITEDIR" ]; then
    echo "/userdata/sdk/python" > "$SITEDIR/recamera_ext_sdk.pth"
    echo "  wrote $SITEDIR/recamera_ext_sdk.pth -> /userdata/sdk/python"
  else
    echo "  WARN: cannot locate venv site-packages -- recamera_ext .pth not written"
  fi
  # 4) self-check: import RKNNLite with the OEM runtime on LD path.
  if LD_LIBRARY_PATH=/oem/usr/lib "$RKNNENV/bin/python3" \
       -c "from rknnlite.api import RKNNLite; RKNNLite(); import recamera_ext; print('rknnlite + recamera_ext OK')"; then
    echo "  self-check PASSED"
  else
    echo "  WARN: rknnlite self-check FAILED (import/runtime issue) -- inspect on device"
    return 1
  fi
  return 0
}
if provision_rknnlite; then
  echo "rknnlite runtime provisioned."
else
  echo "WARN: rknnlite provision incomplete -- main install unaffected; fix on device before running vision apps."
fi
echo "  vision apps run with:"
echo "    PYTHONPATH=/userdata/local:/userdata/sdk/python \\"
echo "    LD_LIBRARY_PATH=/oem/usr/lib \\"
echo "    $RKNNENV/bin/python3 <app>.py"

echo "=== [7/7] reboot to activate new rkipc ==="
echo "run 'reboot' now (or pass --reboot). After ~1-2min self-check: ls /run/recamera/"
if [ "$1" = "--reboot" ]; then sync; reboot; fi

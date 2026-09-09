#!/bin/sh
# recamera-ext-api install.sh  (persistent sideload into /oem, survives reboot)
# Run ON THE DEVICE as root:  adb push <this dir>/* /userdata/ext-pkg/ ; adb shell "sh /userdata/ext-pkg/install.sh"
# Idempotent: backs up factory files once, md5-verifies every artifact, then overwrites /oem.
#
# Usage: sh install.sh [--reboot] [--strict] [--force]
#   --reboot   reboot immediately after a successful install
#   --strict   abort if the device's firmware baseline is not one we have
#              validated this build against (default: warn and continue)
#   --force    install even when the pre-flight refuses (last resort; see below)
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

DO_REBOOT=0; STRICT=0; FORCE=0
for a in "$@"; do
  case "$a" in
    --reboot) DO_REBOOT=1 ;;
    --strict) STRICT=1 ;;
    --force)  FORCE=1 ;;
    -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done

# ---------------------------------------------------------------------------
# Rollback safety -- what makes a file a valid rollback target?
#
# A rollback target must be a FACTORY build: one that does NOT carry our
# extension. That is a property of the file's CONTENT, so we test the content
# directly instead of matching md5s against a hand-maintained allowlist.
#
# An md5 allowlist tests "have I seen this exact file before", which is not the
# same question: it rejects every official firmware baseline we have not
# personally catalogued (so a legitimate device gets refused in the field) while
# telling us nothing about the one file it does accept. It also rots -- the list
# it replaced had the same md5 in both the "factory" and "extension" columns.
#
# Our rkipc exports the extension endpoints and the hardware-mask symbols; a
# factory rkipc has none of them. Our entry.cgi links rockchip::cgi::ExtApiHandler;
# a factory entry.cgi does not. Both hold for any baseline, past or future.
# ---------------------------------------------------------------------------
RKIPC_EXT_MARKERS='/run/recamera|rc_ext_'
ENTRY_EXT_MARKERS='ExtApiHandler'

# Firmware baselines this rkipc has been validated against, by the md5 of the
# device's FACTORY rkipc. Advisory only: an unlisted baseline warns (or aborts
# under --strict), it never silently blocks a legitimate device.
VALIDATED_BASELINE_MD5S="ce3dfa64a554667028b47f4d9ce84981"   # 2026-08 factory, 2178232 B (192.168.42.1)

md5of() { md5sum "$1" 2>/dev/null | awk '{print $1}'; }
need() { [ "$(md5of "$1")" = "$2" ] || { echo "FATAL md5 mismatch: $1 (got $(md5of "$1") want $2)"; exit 1; }; }
in_list() { _v=$1; shift; for _m in $*; do [ "$_v" = "$_m" ] && return 0; done; return 1; }

# has_ext <file> <marker-regex> -- true if the binary carries our extension.
# busybox strings may be absent; grep -a over the raw binary is the fallback.
has_ext() {
  if command -v strings >/dev/null 2>&1; then
    strings -a "$1" 2>/dev/null | grep -qE "$2"
  else
    grep -aqE "$2" "$1"
  fi
}
is_factory_rkipc() { [ -f "$1" ] && ! has_ext "$1" "$RKIPC_EXT_MARKERS"; }
is_factory_entry() { [ -f "$1" ] && ! has_ext "$1" "$ENTRY_EXT_MARKERS"; }

forced() {  # forced <what> -- honour --force, otherwise abort
  if [ "$FORCE" = 1 ]; then
    echo "  --force: continuing anyway ($1). NO valid rollback point will exist."
    return 0
  fi
  echo "       Re-run with --force to install anyway (you will have no local"
  echo "       rollback point; recovery would need a full OTA / update.img flash,"
  echo "       which rewrites /oem back to factory)."
  exit 1
}

echo "=== [1/8] verify package artifacts ==="
need "$PKG/rkipc" "$RKIPC_MD5"
need "$PKG/entry.cgi" "$ENTRY_MD5"
need "$PKG/sdk/lib/librecamera_ext.so.1.0.0" "$SO_MD5"
# The package must itself be an extension build -- if these markers are missing
# the marker test below is meaningless and every check would silently pass.
has_ext "$PKG/rkipc" "$RKIPC_EXT_MARKERS" || { echo "FATAL: package rkipc carries no extension markers -- wrong or corrupt build"; exit 1; }
has_ext "$PKG/entry.cgi" "$ENTRY_EXT_MARKERS" || { echo "FATAL: package entry.cgi carries no extension markers -- wrong or corrupt build"; exit 1; }
echo "package OK"

echo "=== [2/8] firmware baseline compatibility (advisory) ==="
[ -f /oem/usr/bin/rkipc ] || { echo "FATAL: /oem/usr/bin/rkipc missing -- this is not a reCamera Pro rootfs"; exit 1; }
CUR=$(md5of /oem/usr/bin/rkipc)
CUR_SIZE=$(wc -c < /oem/usr/bin/rkipc 2>/dev/null | tr -d ' ')
OSVER=$(. /etc/os-release 2>/dev/null; echo "${NAME:-?} ${VERSION:-?}")
echo "  device factory rkipc : $CUR  ($CUR_SIZE B)"
echo "  device rootfs        : $OSVER"
if is_factory_rkipc /oem/usr/bin/rkipc && ! in_list "$CUR" $VALIDATED_BASELINE_MD5S; then
  echo "  WARN: this rkipc build has NOT been validated against this firmware baseline."
  echo "        Installing replaces the camera pipeline binary with our build, which was"
  echo "        compiled from a different baseline -- anything that baseline added on top"
  echo "        of ours is lost until you roll back. The install still creates a rollback"
  echo "        point, so this is reversible."
  if [ "$STRICT" = 1 ]; then echo "  --strict: aborting."; exit 1; fi
else
  echo "  baseline OK (or /oem already holds an extension build -- see next step)"
fi

echo "=== [3/8] backup factory rkipc (once, content-verified) ==="
if [ -f /userdata/rkipc.factory.bak ]; then
  if is_factory_rkipc /userdata/rkipc.factory.bak; then
    echo "backup exists and carries no extension markers: $(md5of /userdata/rkipc.factory.bak)"
  else
    echo "REFUSING: existing /userdata/rkipc.factory.bak ($(md5of /userdata/rkipc.factory.bak)) CARRIES"
    echo "       extension markers -- it is one of our builds, not a factory rkipc."
    echo "       Restoring it would be a no-op and would leave the extension on /oem forever."
    forced "bad existing backup"
  fi
else
  # First install: only capture a backup if /oem currently holds a factory rkipc.
  # Capturing an already-installed ext build as "factory" is what breaks rollback.
  if is_factory_rkipc /oem/usr/bin/rkipc; then
    cp /oem/usr/bin/rkipc /userdata/rkipc.factory.bak
    # Provenance sidecar: which baseline this device shipped with. Support needs
    # this when a device turns up later with an unfamiliar rkipc.
    {
      echo "md5=$CUR"
      echo "size=$CUR_SIZE"
      echo "rootfs=$OSVER"
      echo "captured_by=recamera-ext-api install.sh"
      echo "captured_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || echo unknown)"
    } > /userdata/rkipc.factory.bak.info
    echo "saved /userdata/rkipc.factory.bak ($CUR, $CUR_SIZE B) + .info"
  else
    echo "REFUSING: /oem rkipc ($CUR) CARRIES extension markers -- it is already one of our"
    echo "       builds, and no factory backup exists. Refusing to capture an extension"
    echo "       build as 'factory': that would turn every future rollback into a no-op."
    forced "no factory rkipc to capture"
  fi
fi

echo "=== [4/8] backup factory entry.cgi (once, content-verified) ==="
if [ -f /userdata/entry.cgi.factory.bak ]; then
  if is_factory_entry /userdata/entry.cgi.factory.bak; then
    echo "entry.cgi backup exists and carries no extension markers"
  else
    echo "WARN: /userdata/entry.cgi.factory.bak carries extension markers -- not a factory"
    echo "      entry.cgi. Leaving it alone; restoring it would be a no-op. Recover the"
    echo "      factory entry.cgi with an OTA / update.img flash if you need it."
  fi
elif is_factory_entry /oem/usr/www/cgi-bin/entry.cgi; then
  cp /oem/usr/www/cgi-bin/entry.cgi /userdata/entry.cgi.factory.bak
  echo "saved /userdata/entry.cgi.factory.bak ($(md5of /userdata/entry.cgi.factory.bak))"
else
  echo "WARN: /oem entry.cgi already carries extension markers and no backup exists --"
  echo "      skipping capture (an extension entry.cgi must never be stored as factory)."
fi

echo "=== [5/8] verify /oem writable ==="
touch /oem/.wtest && rm -f /oem/.wtest && echo "OEM writable" || { echo "FATAL /oem not writable"; exit 1; }

echo "=== [6/8] install into /oem (persistent) ==="
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

echo "=== [7/8] provision rknnlite python runtime (best-effort) ==="
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

echo "=== [8/8] reboot to activate new rkipc ==="
echo "run 'reboot' now (or pass --reboot). After ~1-2min self-check: ls /run/recamera/"
if [ "$DO_REBOOT" = 1 ]; then sync; reboot; fi

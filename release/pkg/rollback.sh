#!/bin/sh
# recamera-ext-api rollback.sh  -- restore factory rkipc (and entry.cgi) into /oem, then reboot.
# Run ON THE DEVICE as root.
set -e

echo "FATAL: archived rollback policy has overlapping factory/extension hashes; refusing to write /oem." >&2
echo "Restore only from a separately verified factory image using an approved recovery procedure." >&2
exit 1
# A rollback target MUST be a genuine clean factory rkipc (see install.sh for the rationale).
VERIFIED_FACTORY_MD5S="9826e9ecf8ed543a6dc78e3731102e0f"   # V1.0.x clean factory (1.9MB, 0 ext sockets)
KNOWN_EXT_BUILD_MD5S="9826e9ecf8ed543a6dc78e3731102e0f f93ac217c9920bc962771aeed1ac0550 f683352a9d062a05a3df1f8df22d7d53"  # ext builds -- NOT rollback targets
md5of() { md5sum "$1" 2>/dev/null | awk '{print $1}'; }
in_list() { _v=$1; shift; for _m in $*; do [ "$_v" = "$_m" ] && return 0; done; return 1; }
is_factory()   { in_list "$1" $VERIFIED_FACTORY_MD5S; }
is_ext_build() { in_list "$1" $KNOWN_EXT_BUILD_MD5S; }

if [ ! -f /userdata/rkipc.factory.bak ]; then
  echo "FATAL: /userdata/rkipc.factory.bak missing -- cannot roll back"; exit 1
fi
BAK=$(md5of /userdata/rkipc.factory.bak)
if ! is_factory "$BAK"; then
  if is_ext_build "$BAK"; then
    echo "FATAL: backup ($BAK) is a known EXTENSION build, not factory -- refusing to restore."
  else
    echo "FATAL: backup ($BAK) is not a VERIFIED factory rkipc -- refusing to restore."
  fi
  echo "       A rollback target must be clean factory (md5 one of: $VERIFIED_FACTORY_MD5S)."
  echo "       Nothing changed. Obtain the true factory rkipc before rolling back."; exit 1
fi
echo "restoring factory rkipc from backup ($BAK)"
cp /userdata/rkipc.factory.bak /oem/usr/bin/rkipc && chmod 755 /oem/usr/bin/rkipc

if [ -f /userdata/entry.cgi.factory.bak ]; then
  cp /userdata/entry.cgi.factory.bak /oem/usr/www/cgi-bin/entry.cgi && chmod 755 /oem/usr/www/cgi-bin/entry.cgi
  echo "entry.cgi restored"
fi
# ext .so left in place is harmless (nothing loads it unless a solution asks); remove if you want a clean factory:
# rm -f /oem/usr/lib/librecamera_ext.so /oem/usr/lib/librecamera_ext.so.1 /oem/usr/lib/librecamera_ext.so.1.0.0

# --- rknnlite python runtime rollback (idempotent, best-effort) ---------------
# Remove only the /usr/lib/librknnrt.so symlink we created (leave a real file untouched),
# then drop the venv we provisioned. Both are safe to skip if absent.
if [ -L /usr/lib/librknnrt.so ]; then
  rm -f /usr/lib/librknnrt.so && echo "removed our /usr/lib/librknnrt.so symlink"
elif [ -e /usr/lib/librknnrt.so ]; then
  echo "leaving /usr/lib/librknnrt.so (real file, not our symlink)"
fi
if [ -d /userdata/rknnenv ]; then
  rm -rf /userdata/rknnenv && echo "removed /userdata/rknnenv"
fi
sync
echo "rolled back. post md5: $(md5of /oem/usr/bin/rkipc)"
echo "reboot now to run factory rkipc."
if [ "$1" = "--reboot" ]; then reboot; fi

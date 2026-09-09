#!/usr/bin/env bash
#
# deploy-firmware.sh -- reCamera Pro v1.6.2 MASKING FIRMWARE deploy (HIGH RISK).
#
#   #############################################################################
#   ##  DANGER -- READ BEFORE RUNNING                                          ##
#   ##                                                                         ##
#   ##  This replaces the camera pipeline binary (rkipc) + entry.cgi + the     ##
#   ##  extension .so in /oem. The new rkipc MUST be activated by a COLD BOOT  ##
#   ##  (full `reboot`). HOT-swapping rkipc while the old one holds the camera ##
#   ##  triggers a cv181x_vpss / CSIBDG FIFO kernel oops that can hang the box.##
#   ##                                                                         ##
#   ##  => Run this ONLY when you are physically next to the device (or have   ##
#   ##     a power/reset path), on a device you can afford to brick briefly.   ##
#   ##  => Do NOT run this as part of the routine app-layer flow.              ##
#   ##     deploy-app.sh already brings apps/frontend/appmgr up WITHOUT this.  ##
#   ##                                                                         ##
#   ##  The install is idempotent and backs up the factory rkipc/entry.cgi to  ##
#   ##  /userdata/*.factory.bak. Roll back with:  deploy-firmware.sh --rollback##
#   #############################################################################
#
# Usage:
#   ./deploy-firmware.sh --host <ip> [--reboot] [--strict] [--force]   install
#                                                     masking firmware
#   ./deploy-firmware.sh --host <ip> --rollback [--reboot]   restore factory rkipc
#
#   --reboot   let the on-device script reboot immediately after install/rollback.
#              WITHOUT it, the script only stages the change and PROMPTS you to
#              reboot manually (recommended: reboot from the device console).
#   --strict   abort if the device firmware baseline is not one this build was
#              validated against (default: warn and continue).
#   --force    install even if the pre-flight finds no capturable factory rkipc,
#              i.e. accept having NO local rollback point.
#
set -euo pipefail

HOST=100.158.145.29
DO_REBOOT=0
ROLLBACK=0
INSTALL_FLAGS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --host) HOST="$2"; shift 2;;
    --reboot) DO_REBOOT=1; shift;;
    --rollback) ROLLBACK=1; shift;;
    --strict) INSTALL_FLAGS="$INSTALL_FLAGS --strict"; shift;;
    --force)  INSTALL_FLAGS="$INSTALL_FLAGS --force"; shift;;
    -h|--help) grep -E '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

HERE="$(cd "$(dirname "$0")" && pwd)"
SERIAL="${HOST}:5555"
STAGE=/userdata/ext-pkg
VER=1.6.2
PKG="$HERE/recamera-ext-api-v${VER}.tar"

red()  { printf '\033[1;31m%s\033[0m\n' "$*"; }
die()  { printf '\033[1;31mFATAL: %s\033[0m\n' "$*" >&2; exit 1; }
ash()  { adb -s "$SERIAL" shell "echo __B__; { $1; }; echo __E__=\$?" 2>&1 | sed -n '/^__B__$/,/^__E__=/p' | sed '1d;/^__E__=/d'; }

cat <<'BANNER'

  =============================================================================
   deploy-firmware.sh -- HIGH-RISK masking firmware (rkipc) deploy
   Cold boot REQUIRED. Hot-swap => VPSS kernel oops. Local/physical access only.
  =============================================================================
BANNER
red "Target device: $HOST"
[ "$ROLLBACK" = 1 ] && red "Mode: ROLLBACK to factory rkipc" || red "Mode: INSTALL masking firmware v$VER"
printf 'Type EXACTLY "I-HAVE-PHYSICAL-ACCESS" to proceed: '
read -r CONF
[ "$CONF" = "I-HAVE-PHYSICAL-ACCESS" ] || die "confirmation mismatch -- aborted"

command -v adb >/dev/null || die "adb not found on PATH"
adb connect "$SERIAL" >/dev/null 2>&1 || true
sleep 1
[ "$(ash 'id -u')" = "0" ] || die "adb shell not root on $SERIAL"

if [ "$ROLLBACK" = 1 ]; then
  # rollback.sh ships inside the package; use the copy already on the device if
  # a prior install staged it, else push the package and use its rollback.sh.
  if [ "$(ash "[ -f $STAGE/rollback.sh ] && echo yes")" != "yes" ]; then
    [ -f "$PKG" ] || die "no on-device rollback.sh and no local package $PKG"
    adb -s "$SERIAL" push "$PKG" "$STAGE.tar" >/dev/null
    ash "mkdir -p $STAGE && tar -xf $STAGE.tar -C $STAGE" >/dev/null
  fi
  red "Restoring factory rkipc..."
  if [ "$DO_REBOOT" = 1 ]; then ash "sh $STAGE/rollback.sh --reboot"; else ash "sh $STAGE/rollback.sh"; fi
  red "Rollback staged. If you did not pass --reboot, reboot the device now to run factory rkipc."
  exit 0
fi

[ -f "$PKG" ] || die "missing package: $PKG"
red "Pushing + verifying masking firmware package..."
adb -s "$SERIAL" push "$PKG" "$STAGE.tar" >/dev/null
ash "rm -rf $STAGE && mkdir -p $STAGE && tar -xf $STAGE.tar -C $STAGE" >/dev/null
# install.sh md5-verifies every artifact, backs up factory rkipc/entry.cgi once,
# installs into /oem, provisions the rknnlite runtime, then stops BEFORE reboot.
if [ "$DO_REBOOT" = 1 ]; then
  ash "sh $STAGE/install.sh$INSTALL_FLAGS --reboot" | sed 's/^/  /'
  red "Install done; device is rebooting to activate new rkipc."
else
  ash "sh $STAGE/install.sh$INSTALL_FLAGS" | sed 's/^/  /'
  echo
  red "Install STAGED into /oem. New rkipc is NOT active yet."
  red "COLD BOOT the device now (from the device console):   reboot"
  red "After ~1-2 min verify:   ls /run/recamera/   (extension sockets present)"
  red "Roll back any time with:  $0 --host $HOST --rollback"
fi

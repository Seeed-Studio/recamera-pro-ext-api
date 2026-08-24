#!/bin/sh
# provision-runtime.sh -- one-time (idempotent) runtime provisioning that makes
# reCamera Pro App Center apps importable+loadable when launched by appmgr,
# WITHOUT any hand-typed `export` in an ssh session.
#
# APP CENTER PERSISTENCE FIX #3 (recamera_ext importability). The 8 vision apps
# and voice-transcribe launch under the rknn venv interpreter
# (/userdata/rknnenv/bin/python -- see each manifest's "interpreter"). Three
# things must be true for them to actually run there:
#
#   (A) rknnlite / the venv site-packages are reachable
#         -> guaranteed by launching under /userdata/rknnenv/bin/python itself
#            (rknnlite is installed INTO that venv; the system python cannot see
#            it -- that was root-cause #1 of "app won't start").
#
#   (B) `import recamera_ext` works
#         The official extension-API python binding lives in the SDK tree at
#         /userdata/sdk/python/recamera_ext, which is NOT on the venv's default
#         sys.path (root-cause #2). We drop a .pth into the venv site-packages
#         that appends /userdata/sdk/python to sys.path, so every process the
#         venv python starts can `import recamera_ext`. This is the persistent,
#         non-hack replacement for a per-session PYTHONPATH export.
#
#   (C) the native lib `librecamera_ext.so.1` loads
#         recamera_ext dlopen's librecamera_ext.so.1, which ships in
#         /usr/lib, alongside the system OpenCV ABI. appmgr/supervisor.py keeps
#         /usr/lib first, then adds /oem/usr/lib:/oem/lib so RGA/RKNN/MPP
#         libraries that exist only in the OEM image remain loadable without
#         letting old OEM FreeType/PNG copies shadow system dependencies. This
#         script verifies the extension .so and the exact launch-time order.
#
# Idempotent: safe to run any number of times. Prints a PASS/FAIL summary and
# exits non-zero if a hard prerequisite (the SDK python tree, the .so, or the
# venv python) is missing so provisioning failures surface loudly.
#
# BusyBox/POSIX-sh compatible (device shell). Run on the device:
#     sh /userdata/local/appcenter/provision-runtime.sh
set -u

RKNNENV=/userdata/rknnenv
VENV_PY="$RKNNENV/bin/python"
SDK_PY_DIR=/userdata/sdk/python                      # holds recamera_ext/
EXT_SO=/usr/lib/librecamera_ext.so.1                 # dlopen'd by recamera_ext
EXT_LIBDIRS=/usr/lib:/oem/usr/lib:/oem/lib           # must match supervisor.py

rc=0
ok()   { echo "[provision] OK   $*"; }
warn() { echo "[provision] WARN $*"; }
fail() { echo "[provision] FAIL $*"; rc=1; }

# --- (A) venv python present ------------------------------------------------ #
if [ -x "$VENV_PY" ] || [ -L "$VENV_PY" ]; then
    ok "rknn venv interpreter: $VENV_PY ($("$VENV_PY" -V 2>&1))"
else
    fail "rknn venv interpreter missing: $VENV_PY (apps launch under it -- rknnlite lives here)"
fi

# --- (B) recamera_ext on sys.path via .pth ---------------------------------- #
if [ -d "$SDK_PY_DIR/recamera_ext" ]; then
    ok "SDK python binding present: $SDK_PY_DIR/recamera_ext"
else
    fail "SDK python binding missing: $SDK_PY_DIR/recamera_ext (provisioned by extension-API firmware)"
fi

# Locate the venv site-packages (glob the python3.x dir; don't hardcode 3.11).
SITEPKG=""
for d in "$RKNNENV"/lib/python*/site-packages; do
    [ -d "$d" ] && SITEPKG="$d" && break
done
if [ -n "$SITEPKG" ]; then
    PTH="$SITEPKG/recamera_sdk.pth"
    if [ -f "$PTH" ] && [ "$(cat "$PTH" 2>/dev/null)" = "$SDK_PY_DIR" ]; then
        ok ".pth already correct: $PTH -> $SDK_PY_DIR"
    else
        if printf '%s\n' "$SDK_PY_DIR" > "$PTH" 2>/dev/null; then
            ok "wrote .pth: $PTH -> $SDK_PY_DIR"
        else
            fail "could not write .pth at $PTH"
        fi
    fi
else
    fail "no venv site-packages under $RKNNENV/lib/python*/ -- cannot install .pth"
fi

# --- (C) native lib present and safe launch-time lookup order ---------------- #
if [ -e "$EXT_SO" ]; then
    ok "native lib present: $EXT_SO (supervisor injects LD_LIBRARY_PATH=$EXT_LIBDIRS)"
else
    fail "native lib missing: $EXT_SO (recamera_ext dlopen target; from extension-API firmware)"
fi

# --- self-test: can the venv python actually import recamera_ext? ----------- #
# Mirror exactly what supervisor.py does at launch: run under the venv python
# with LD_LIBRARY_PATH set. This proves (A)+(B)+(C) compose correctly.
if [ -x "$VENV_PY" ] || [ -L "$VENV_PY" ]; then
    if LD_LIBRARY_PATH="$EXT_LIBDIRS${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
         "$VENV_PY" -c 'import recamera_ext; print(getattr(recamera_ext, "__file__", "?"))' >/tmp/.prov_ext 2>&1; then
        ok "self-test: import recamera_ext -> $(cat /tmp/.prov_ext)"
    else
        fail "self-test: import recamera_ext FAILED:"
        sed 's/^/[provision]      /' /tmp/.prov_ext
    fi
    rm -f /tmp/.prov_ext
fi

echo "[provision] ---------------------------------------------"
if [ "$rc" -eq 0 ]; then
    echo "[provision] RESULT: PASS -- recamera_ext is importable by app launches"
else
    echo "[provision] RESULT: FAIL -- see FAIL lines above (some prerequisites are"
    echo "[provision]         firmware-provided: SDK python tree + /usr/lib .so)"
fi
exit "$rc"

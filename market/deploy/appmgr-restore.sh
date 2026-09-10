#!/bin/sh
# Compatibility entrypoint for operators of earlier app-center releases.
# Service code and launchers now come from the matching firmware image. Never
# restore launchers or nginx configuration from a writable /userdata master.

set -eu

INIT_DIR=/oem/usr/etc/init.d

for name in S93inferenced S94appmgr; do
    if [ ! -x "$INIT_DIR/$name" ]; then
        echo "[appmgr-restore] missing firmware launcher: $INIT_DIR/$name" >&2
        echo "[appmgr-restore] install matching firmware with platform services" >&2
        exit 1
    fi
done

# Preserve the same order as RkLunch; each service checks its own readiness.
"$INIT_DIR/S93inferenced" start
exec "$INIT_DIR/S94appmgr" start

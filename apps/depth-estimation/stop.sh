#!/bin/sh
# Stop only this application through its managed lifecycle.
set -eu
export PYTHONPATH=/usr/lib/recamera:/usr/lib/python3.11/site-packages
export LD_LIBRARY_PATH=/usr/lib:/oem/usr/lib:/oem/lib
exec /usr/bin/python3 -m appmgr stop depth-estimation

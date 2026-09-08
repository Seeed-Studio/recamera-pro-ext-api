#!/bin/sh
# Use appmgr to preserve resource leases and process lifecycle tracking.
set -eu
export PYTHONPATH=/usr/lib/recamera:/usr/lib/python3.11/site-packages
export LD_LIBRARY_PATH=/usr/lib:/oem/usr/lib:/oem/lib
exec /usr/bin/python3 -m appmgr restart face-recognition

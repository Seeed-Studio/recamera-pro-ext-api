#!/bin/sh
# restart depth-estimation app (dev helper, same shape as apps/face-recognition/restart.sh).
# The camera is single-active, so this also stops whatever other kit app is running.
APP=/userdata/local/apps/depth-estimation
ME=$$
for p in /proc/[0-9]*; do
  pid=${p#/proc/}; [ "$pid" = "$ME" ] && continue
  c=$(tr "\0" " " < $p/cmdline 2>/dev/null)
  case "$c" in *python*kit/run.py*) echo "kill $pid: $c"; kill $pid;; esac
done
sleep 3
for p in /proc/[0-9]*; do
  pid=${p#/proc/}; [ "$pid" = "$ME" ] && continue
  c=$(tr "\0" " " < $p/cmdline 2>/dev/null)
  case "$c" in *python*kit/run.py*) echo "kill -9 $pid"; kill -9 $pid;; esac
done
[ -f $APP/run.log ] && mv $APP/run.log $APP/run.prev.log
cd /userdata/local
LD_LIBRARY_PATH=/oem/usr/lib:/oem/lib PYTHONPATH=/userdata/local KIT_PARENT=/userdata/local \
  setsid /userdata/rknnenv/bin/python /userdata/local/kit/run.py $APP/app.py --sink ws --port 8124 \
  > $APP/run.log 2>&1 < /dev/null &
echo "launched"

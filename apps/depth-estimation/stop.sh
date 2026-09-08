#!/bin/sh
# Stop every running kit app (the camera is single-active). Dev helper.
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
echo "stopped"

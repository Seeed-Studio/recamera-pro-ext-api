#!/bin/sh
APP=/userdata/local/apps/face-recognition
echo "--procs:"
for p in /proc/[0-9]*; do c=$(tr "\0" " " < $p/cmdline 2>/dev/null); case "$c" in *python*kit/run.py*) echo "${p#/proc/} $c";; esac; done
echo "--log:"
grep -n "users=\|source=\|Error\|Traceback" $APP/run.log | head -6
tail -${1:-4} $APP/run.log

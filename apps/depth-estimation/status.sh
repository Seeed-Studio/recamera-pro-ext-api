#!/bin/sh
APP=/userdata/local/apps/depth-estimation
echo "--procs:"
for p in /proc/[0-9]*; do c=$(tr "\0" " " < $p/cmdline 2>/dev/null); case "$c" in *python*kit/run.py*) echo "${p#/proc/} $c";; esac; done
echo "--tracebacks: $(grep -c Traceback $APP/run.log 2>/dev/null)"
echo "--log:"
grep -n "models=\|source=\|Error\|Traceback" $APP/run.log 2>/dev/null | head -6
tail -${1:-4} $APP/run.log 2>/dev/null

#!/usr/bin/env python3
"""Read-only RV1126B target reconnaissance.

The probe deliberately uses only the local OpenSSH client and a POSIX shell
already present on the target. It never writes files, starts services, opens
audio, changes GPIO state, or installs packages. The JSON is evidence for
triage, not a compatibility certificate.
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone


REMOTE_SCRIPT = r"""printf '%s\n' '--- identity ---'
uname -a 2>&1
python3 --version 2>&1
printf '%s\n' '--- python imports ---'
python3 - <<'PY'
try:
    import kit
    print('kit=' + str(kit.__file__))
except Exception as exc:
    print('kit_error=' + repr(exc))
try:
    import recamera_ext
    print('recamera_ext=' + str(recamera_ext.__file__))
except Exception as exc:
    print('recamera_ext_error=' + repr(exc))
PY
printf '%s\n' '--- processes ---'
ps 2>&1 | grep -E '[a]ppmgr|[i]nferenced|[k]it|[r]kipc' || true
printf '%s\n' '--- runtime paths ---'
ls -ld /usr/lib/python3.11/site-packages/kit /usr/lib/python3.11/site-packages/recamera_ext /usr/lib/librecamera_ext.so* 2>&1
printf '%s\n' '--- recamera sockets ---'
ls -l /run/recamera 2>&1
printf '%s\n' '--- appmgr directories ---'
ls -la /userdata/local/apps /userdata/local/appmgr /userdata/appstage 2>&1
printf '%s\n' '--- appmgr environment ---'
if test -r /proc/1/environ; then tr '\000' '\n' < /proc/1/environ | grep -E '^(APPMGR|PYTHON|LD_LIBRARY_PATH|PATH)'; fi
printf '%s\n' '--- local endpoints ---'
if command -v ss >/dev/null 2>&1; then ss -ltn 2>&1; elif command -v netstat >/dev/null 2>&1; then netstat -ltn 2>&1; fi
printf '%s\n' '--- gmgr ---'
ls -l /dev/shm/gmgr.sock 2>&1
printf '%s\n' '--- audio ---'
cat /proc/asound/cards 2>&1
command -v arecord 2>&1
command -v aplay 2>&1
command -v ffmpeg 2>&1
aplay -l 2>&1
aplay -L 2>&1 | head -6
printf '%s\n' '--- audio libs ---'
ls -l /usr/lib/libasound.so* 2>&1
printf '%s\n' '--- audio nodes ---'
ls -l /dev/snd 2>&1
printf '%s\n' '--- audio config ---'
ls -l /etc/asound.conf 2>&1
printf '%s\n' '--- audio identity ---'
id 2>&1
grep '^audio:' /etc/group 2>&1
printf '%s\n' '--- audio sample wav ---'
ls -l /oem/usr/share/speaker_test.wav 2>&1
od -A d -t x1 -N 44 /oem/usr/share/speaker_test.wav 2>&1
printf '%s\n' '--- audio holders ---'
if command -v fuser >/dev/null 2>&1; then for n in /dev/snd/pcmC*D*p; do printf '%s: ' "$n"; fuser "$n" 2>&1; done; else printf '%s\n' 'fuser_absent'; fi
printf '%s\n' '--- gpio inventory ---'
if command -v gpioinfo >/dev/null 2>&1; then
    printf '%s\n' 'gpioinfo_command=present'
    gpioinfo 2>&1
else
    printf '%s\n' 'gpioinfo_command=absent'
fi
ls -l /dev/gpiochip* 2>&1
printf '%s\n' '--- logs ---'
tail -80 /tmp/appmgr.log 2>&1
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only reCamera Pro target probe")
    parser.add_argument("--host", required=True, help="SSH destination, for example user@target-host")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--timeout", type=int, default=12)
    args = parser.parse_args(argv)

    remote_command = f"sh -c {shlex.quote(REMOTE_SCRIPT)}"
    command = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={args.timeout}",
        "-p", str(args.port),
        "--", args.host, remote_command,
    ]
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as error:
        print(json.dumps({"connected": False, "error": str(error)}, indent=2))
        return 2

    output = result.stdout
    sections: dict[str, str] = {}
    current: str | None = None
    lines: list[str] = []
    for line in output.splitlines():
        marker = re.fullmatch(r"--- (.+) ---", line.strip())
        if marker:
            if current is not None:
                sections[current] = "\n".join(lines).strip()
            current = marker.group(1).replace(" ", "_")
            lines = []
        elif current is not None:
            lines.append(line)
    if current is not None:
        sections[current] = "\n".join(lines).strip()

    report = {
        "probe": "recamera-pro-skill",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "target": args.host,
        "connected": result.returncode == 0,
        "ssh_exit_code": result.returncode,
        "ssh_stderr": result.stderr.strip(),
        "facts": {
            "kit_imported": "kit=" in sections.get("python_imports", ""),
            "recamera_ext_imported": "recamera_ext=" in sections.get("python_imports", ""),
            "appmgr_running": (
                "appmgr" in sections.get("processes", "")
                or "appmgr" in sections.get("logs", "")
                or ":8130" in sections.get("local_endpoints", "")
            ),
            "inferenced_running": (
                "inferenced" in sections.get("processes", "")
                or "inferenced.sock" in sections.get("recamera_sockets", "")
            ),
            "gmgr_socket_present": "/dev/shm/gmgr.sock" in sections.get("gmgr", ""),
            "audio_playback_inventory_present": "PLAYBACK Hardware Devices" in sections.get("audio", ""),
            "audio_default_playback_is_loopback": "default:CARD=Loopback" in sections.get("audio", ""),
            "libasound_present": "libasound.so.2" in sections.get("audio_libs", ""),
            "snd_playback_nodes_present": bool(re.search(r"pcmC\d+D\d+p", sections.get("audio_nodes", ""))),
            "asound_conf_present": "/etc/asound.conf" in sections.get("audio_config", ""),
            "audio_identity_root_or_audio": ("uid=0" in sections.get("audio_identity", "")) or ("audio" in sections.get("audio_identity", "")),
            "sample_wav_present": "speaker_test.wav" in sections.get("audio_sample_wav", ""),
            "gpioinfo_available": "gpioinfo_command=present" in sections.get("gpio_inventory", ""),
        },
        "sections": sections,
        "read_only": True,
        "interpretation": [
            "Importing a module or listing a socket does not prove an App can complete its workflow.",
            "Audio playback hardware is not evidence of a public SDK speaker/player API.",
            "The ALSA default playback device may be a silent Loopback card; confirm the real codec card and device string, and verify audible output only with explicit user authorization.",
            "GPIO lines require a verified board mapping, pinmux, gmgr endpoint, and electrical load.",
            "Use the SDK checkout and closest official example to confirm API semantics before coding.",
        ],
    }
    print(json.dumps(report, indent=2, ensure_ascii=True))
    return 0 if result.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

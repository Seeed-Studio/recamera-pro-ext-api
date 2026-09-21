#!/usr/bin/env python3
"""Read-only target inventory. Presence does not establish runtime health."""
import argparse
import json
from target_transport import fetch_inventory
from remote_inventory import redact


def build_report(result):
    inventory = result.get("inventory", {})
    processes = inventory.get("processes", [])
    complete = inventory.get("process_scan_complete", False)
    def service(name):
        found = any(p.get("service") == name and p.get("state") not in {"Z", "X"} for p in processes)
        return True if found else False if complete else None
    paths = inventory.get("paths", {})
    def present(path, kind):
        item = paths.get(path, {})
        if item.get("exists") is None:
            return None
        return item.get("exists") is True and item.get("kind") == kind
    identity = inventory.get("identity", {})
    audio_gid = identity.get("audio_gid")
    permission_identity = (identity["uid"] == 0 or
        audio_gid is not None and audio_gid in identity.get("groups", [])) if "uid" in identity else None
    gmgr = inventory.get("sockets", {}).get("/dev/shm/gmgr.sock", {})
    return redact(dict(result, probe="recamera-pysdk", schema=2, read_only=True,
        facts={
            "appmgr_process_present": service("appmgr"),
            "inferenced_process_present": service("inferenced"),
            "gmgr_socket_present": gmgr.get("kind") == "socket" if gmgr.get("exists") is not None else None,
            "libasound_present": present("/usr/lib/libasound.so.2", "file"),
            "asound_conf_present": present("/etc/asound.conf", "file"),
            "sample_wav_present": present("/oem/usr/share/speaker_test.wav", "file"),
            "audio_identity_root_or_audio": permission_identity,
        },
        interpretation=[
            "A process or kernel-registered socket is inventory, not protocol or health verification.",
            "Missing/unreadable evidence is distinct from an observed absent resource.",
            "Module discoverability does not prove imports, ABI compatibility or a working frame/inference path.",
            "Audio group membership and node permissions do not prove audible playback; no hardware is opened.",
        ]))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--timeout", type=float, default=12, help="Total SSH and collection deadline in seconds")
    args = parser.parse_args(argv)
    try:
        result = fetch_inventory(args.host, args.port, args.timeout)
    except ValueError as exc:
        parser.error(str(exc))
    report = build_report(result)
    print(json.dumps(report, indent=2))
    return 0 if result["connected"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Read-only diagnosis for an AppMgr-managed reCamera Pro application."""
from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
from datetime import datetime, timezone
from typing import Any

APP_ID_PATTERN = re.compile(r"[a-z0-9-]{1,64}")
SECTION_PATTERN = re.compile(r"^--- ([A-Za-z0-9_.-]+) ---$")

REMOTE_TEMPLATE = r"""set +e
APP_ID={app_id}
APP_ROOT=/userdata/local/apps/$APP_ID
printf '%s\n' '--- manifest ---'
cat "$APP_ROOT/manifest.json" 2>&1
printf '%s\n' '--- release_lock ---'
cat "$APP_ROOT/release.lock.json" 2>&1
printf '%s\n' '--- app_files ---'
find "$APP_ROOT" -maxdepth 3 -type f 2>/dev/null | sort
printf '%s\n' '--- app_processes ---'
for proc in /proc/[0-9]*; do
    test -r "$proc/cmdline" || continue
    cmdline=$(tr '\000' ' ' < "$proc/cmdline" 2>/dev/null)
    case "$cmdline" in
        *"/userdata/local/apps/$APP_ID/"*)
            printf '%s\n' 'process_begin'
            printf 'pid=%s\n' "${proc##*/}"
            printf 'cmdline=%s\n' "$cmdline"
            if test -r "$proc/environ"; then
                tr '\000' '\n' < "$proc/environ" 2>/dev/null | grep '^RECAMERA_' | sort
            fi
            printf '%s\n' 'process_end'
            ;;
    esac
done
printf '%s\n' '--- port_8124 ---'
if command -v ss >/dev/null 2>&1; then
    ss -ltnp 2>&1 | grep -E '(:|\])8124[[:space:]]' || ss -ltn 2>&1 | grep -E '(:|\])8124[[:space:]]'
elif command -v netstat >/dev/null 2>&1; then
    netstat -ltnp 2>&1 | grep -E '(:|\])8124[[:space:]]'
fi
printf '%s\n' '--- recamera_sockets ---'
ls -l /run/recamera 2>&1
printf '%s\n' '--- operations ---'
cat /userdata/local/appmgr/operations.json 2>&1
printf '%s\n' '--- audit_matches ---'
if test -r /userdata/local/appmgr/audit.log; then
    grep -n -E "$APP_ID|Address already in use|inference authorization failed|InferenceAuthorizationError|WsResultSink|GatewayResultSink|result.gateway|result_gateway" /userdata/local/appmgr/audit.log 2>/dev/null | tail -200
fi
printf '%s\n' '--- app_log ---'
for log in "$APP_ROOT/logs/app.log" "$APP_ROOT/app.log"; do
    if test -r "$log"; then
        printf 'path=%s\n' "$log"
        tail -200 "$log"
        break
    fi
done
"""

def _parse_sections(output: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    current: str | None = None
    lines: list[str] = []
    for line in output.splitlines():
        marker = SECTION_PATTERN.fullmatch(line.strip())
        if marker:
            if current is not None:
                sections[current] = "\n".join(lines).strip()
            current = marker.group(1)
            lines = []
        elif current is not None:
            lines.append(line)
    if current is not None:
        sections[current] = "\n".join(lines).strip()
    return sections

def _json_object(sections: dict[str, str], name: str) -> dict[str, Any] | None:
    text = sections.get(name, "").strip()
    if not text or text.startswith("cat:"):
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None

def _claims(manifest: dict[str, Any] | None) -> list[dict[str, Any]]:
    resources = manifest.get("resources") if manifest else None
    claims = resources.get("claims") if isinstance(resources, dict) else None
    return [claim for claim in claims if isinstance(claim, dict)] if isinstance(claims, list) else []

def _artifact_summary(manifest: dict[str, Any] | None, app_files: str) -> list[dict[str, Any]]:
    if not manifest or not isinstance(manifest.get("artifacts"), list):
        return []
    files = set(app_files.splitlines())
    result: list[dict[str, Any]] = []
    for artifact in manifest["artifacts"]:
        if not isinstance(artifact, dict):
            continue
        file_name = artifact.get("file")
        result.append({
            "id": artifact.get("id"), "kind": artifact.get("kind"),
            "source": artifact.get("source"), "file": file_name,
            "declared_bundled": artifact.get("source") == "bundled",
            "file_present": isinstance(file_name, str) and any(path.endswith("/" + file_name) for path in files),
        })
    return result

def _process_info(text: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    processes: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in text.splitlines():
        if line == "process_begin":
            current = {"pid": None, "cmdline": "", "environment": {}}
        elif line == "process_end":
            if current is not None:
                processes.append(current)
            current = None
        elif current is not None and line.startswith("pid="):
            current["pid"] = line[4:].strip()
        elif current is not None and line.startswith("cmdline="):
            current["cmdline"] = line[9:].strip()
        elif current is not None and line.startswith("RECAMERA_") and "=" in line:
            key, value = line.split("=", 1)
            current["environment"][key] = value
    if current is not None:
        processes.append(current)
    managed = next(
        (item for item in processes if "/usr/lib/python3.11/site-packages/kit/run.py" in item["cmdline"]),
        processes[0] if processes else {"pid": None, "cmdline": "", "environment": {}},
    )
    return managed, processes

def _operation_records(sections: dict[str, str], app_id: str) -> list[dict[str, Any]]:
    operations = _json_object(sections, "operations")
    values = operations.get("operations") if operations else None
    if not isinstance(values, list):
        return []
    return [item for item in values if isinstance(item, dict) and item.get("app_id") == app_id]

def build_diagnosis(app_id: str, sections: dict[str, str], ssh_exit_code: int) -> dict[str, Any]:
    manifest = _json_object(sections, "manifest")
    claims = _claims(manifest)
    result_claims = [claim for claim in claims if claim.get("name") == "result.publish"]
    npu_claims = [claim for claim in claims if claim.get("name") == "npu.rknn"]
    models = manifest.get("models", []) if manifest else []
    model_files = {item.get("file") for item in models if isinstance(item, dict)}
    artifacts = _artifact_summary(manifest, sections.get("app_files", ""))
    bundled_rknn = [item for item in artifacts if item.get("kind") == "rknn" and item.get("source") == "bundled"]
    process, processes = _process_info(sections.get("app_processes", ""))
    environment = process["environment"]
    operations = _operation_records(sections, app_id)
    failures = [item for item in operations if item.get("status") == "failed"]
    recent_failure_text = "\n".join(str(item.get("error", "")) for item in failures[-5:])
    combined_errors = "\n".join((
        recent_failure_text,
        sections.get("audit_matches", ""),
        sections.get("app_log", ""),
        sections.get("port_8124", ""),
    ))
    managed_launch = any(
        "/usr/lib/python3.11/site-packages/kit/run.py" in item["cmdline"] for item in processes
    )
    gateway_injected = any(
        bool(item["environment"].get("RECAMERA_RESULT_GATEWAY_SOCK")) for item in processes
    )
    npu_mode = npu_claims[0].get("mode") if npu_claims else None
    model_output_app = bool(models) or bool(manifest and "output" in manifest)
    diagnoses: list[str] = []
    if "no bundled RKNN artifact" in combined_errors and npu_mode in {"scheduled", "brokered"}:
        diagnoses.append("The installed release is missing a bundled RKNN artifact authorization for a scheduled/brokered NPU claim.")
    if "authorized bundled artifact" in combined_errors and model_files:
        diagnoses.append("Kit resolved the model to a path outside the manifest-authorized package path; keep entry at app.py and use one package-root model path.")
    current_managed_gateway = managed_launch and gateway_injected and bool(process["pid"])
    if ("WsResultSink" in combined_errors or "Address already in use" in combined_errors) and "8124" in combined_errors and not current_managed_gateway:
        diagnoses.append("The process entered Kit's development WebSocket sink and attempted reserved port 8124; this is a direct/bypassed launch or missing gateway injection, not a model inference error.")
    if model_output_app and result_claims and result_claims[0].get("mode") != "brokered":
        diagnoses.append("The manifest result.publish claim is not brokered, so AppMgr will not provide the authenticated Kit result gateway.")
    if model_output_app and not result_claims:
        diagnoses.append("The manifest has a model/output surface but no result.publish claim; AppMgr cannot establish the managed result path.")
    if managed_launch and model_output_app and not gateway_injected:
        diagnoses.append("The managed process is missing RECAMERA_RESULT_GATEWAY_SOCK; inspect the installed AppMgr/Kit versions and the release manifest before changing application code.")
    if process["pid"] and gateway_injected:
        diagnoses.append("The application currently has a managed Kit process with an injected result gateway; no current startup failure was found in the collected records.")
    if not process["pid"] and failures:
        diagnoses.append("No current application process was found and AppMgr has recorded one or more failed starts; inspect the latest operation error first.")
    if not diagnoses:
        diagnoses.append("Collected evidence is inconclusive; compare the latest AppMgr operation with the application log and confirm the installed release version.")
    return {
        "schema": 1, "app_id": app_id, "connected": ssh_exit_code == 0,
        "managed_launch_detected": managed_launch, "gateway_injected": gateway_injected,
        "process": process, "processes": processes,
        "result_claim": result_claims[0] if result_claims else None,
        "npu_mode": npu_mode,
        "model_files": sorted(file_name for file_name in model_files if isinstance(file_name, str)),
        "bundled_rknn_artifacts": bundled_rknn,
        "port_8124_owner": sections.get("port_8124", "") or None,
        "historical_failed_operations": len(failures),
        "crash_loop": len(failures) >= 3 and not process["pid"],
        "recent_operations": operations[-10:], "diagnosis": diagnoses,
        "sections": sections, "read_only": True,
    }

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Diagnose an AppMgr-managed reCamera Pro App")
    parser.add_argument("--host", required=True, help="SSH destination, for example root@target-host")
    parser.add_argument("--app-id", required=True, help="Installed App ID")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--timeout", type=int, default=12)
    args = parser.parse_args(argv)
    if APP_ID_PATTERN.fullmatch(args.app_id) is None:
        parser.error("--app-id must match [a-z0-9-]{1,64}")
    # Keep remote shell parameter expansions (for example ${proc##*/}) intact.
    remote_script = REMOTE_TEMPLATE.replace("{app_id}", shlex.quote(args.app_id), 1)
    command = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={args.timeout}", "-p", str(args.port), "--", args.host, "sh", "-c", remote_script]
    try:
        result = subprocess.run(command, capture_output=True, check=False)
    except OSError as error:
        print(json.dumps({"schema": 1, "connected": False, "error": str(error), "read_only": True}, indent=2))
        return 2
    stdout = result.stdout.decode("utf-8", errors="replace")
    stderr = result.stderr.decode("utf-8", errors="replace")
    sections = _parse_sections(stdout)
    report = build_diagnosis(args.app_id, sections, result.returncode)
    report.update({"timestamp_utc": datetime.now(timezone.utc).isoformat(), "target": args.host, "ssh_exit_code": result.returncode, "ssh_stderr": stderr.strip()})
    print(json.dumps(report, indent=2, ensure_ascii=True))
    return 0 if result.returncode == 0 else 1

if __name__ == "__main__":
    raise SystemExit(main())

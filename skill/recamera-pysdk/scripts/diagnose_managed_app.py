#!/usr/bin/env python3
"""Read-only managed-app diagnosis with explicit evidence scope."""
import argparse
import json
import re
from remote_inventory import redact
from target_transport import fetch_inventory

ERROR_PATTERN = re.compile(
    r"Traceback|(?:RKNN|rknn|model|inference|initializ|authorization).*?(?:fail|error)|"
    r"(?:fail|error).*?(?:RKNN|rknn|model|inference|initializ|authorization)|"
    r"Address already in use|ModuleNotFoundError|ImportError|RuntimeError|Exception", re.I)


def same_instance(process, lifecycle, pidfile, app_id):
    env = process.get("environment", {})
    if process.get("state") in {"Z", "X"} or process.get("app_id") != app_id:
        return False
    if not process.get("started_at") or not lifecycle.get("started_at"):
        return False
    try:
        return (process.get("pid") == lifecycle.get("pid") == pidfile
                and env.get("RECAMERA_APP_ID") == app_id
                and bool(env.get("RECAMERA_APP_INSTANCE"))
                and env.get("RECAMERA_APP_INSTANCE") == lifecycle.get("instance_id")
                and int(env.get("RECAMERA_APP_GENERATION", -1)) == int(lifecycle.get("generation", -2))
                and abs(process["started_at"] - float(lifecycle["started_at"])) <= 5)
    except (TypeError, ValueError):
        return False


def build_diagnosis(app_id, inventory, ssh_exit_code=0):
    app = inventory.get("app", {})
    lifecycle = app.get("lifecycle", {})
    processes = [p for p in inventory.get("processes", []) if p.get("app_id") == app_id]
    live = [p for p in processes if p.get("state") not in {"Z", "X"}]
    matched = [p for p in live if same_instance(p, lifecycle, app.get("pidfile"), app_id)]
    current = matched[0] if len(matched) == 1 else None
    log = app.get("log", {})
    errors = [line[:1024] for line in log.get("text", "").splitlines() if ERROR_PATTERN.search(line)][-20:]
    scope = "unverified"
    if current and log.get("ok"):
        # Current firmware rotates app.log before spawn. ctime and mtime must
        # belong to this run too; old untagged logs never prove current health.
        start = current["started_at"]
        if log.get("mtime", 0) < start - 2:
            scope = "historical"
        elif log.get("ctime", 0) >= start - 2:
            scope = "current_run"
    observations = []
    health = "unverified"
    if errors and scope == "current_run":
        health = "errors_observed"
        observations.append("Errors occur in the current run log; a live PID or gateway environment does not establish successful startup or inference.")
    elif current and lifecycle.get("observed_state") in {"degraded", "failed", "crash_loop"}:
        health = "errors_observed"
        observations.append("The matching current instance is recorded as " + str(lifecycle["observed_state"]) + ".")
    elif current:
        observations.append("A process matches the AppMgr instance, generation, PID and start time. Successful frame processing is still unverified.")
    elif live:
        observations.append("A process is present but cannot be matched to the committed AppMgr instance; inspect lifecycle/start-time evidence.")
    else:
        observations.append("No live managed app process was observed; incomplete procfs access leaves absence unverified.")
    if errors and scope != "current_run":
        observations.append("Log errors are " + scope + "; they are not attributed to the current generation.")
    operations = app.get("operations", [])
    failed = [o for o in operations if o.get("status") == "failed"]
    # Persisted crash_loop is evidence of a recorded state, not proof of a
    # current repeated crash. Never infer it from counts of historical errors.
    return redact({
        "schema": 2, "app_id": app_id, "connected": ssh_exit_code == 0,
        "read_only": True, "health": health, "current_instance_verified": bool(current),
        "processes": processes, "lifecycle": lifecycle,
        "gateway_injected": any(bool(p.get("environment", {}).get("RECAMERA_RESULT_GATEWAY_SOCK")) for p in live),
        "log_scope": scope, "log_errors": errors, "diagnosis": observations,
        "historical_failed_operations_in_window": len(failed),
        "recorded_crash_loop": lifecycle.get("observed_state") == "crash_loop",
        "crash_loop": None, "inventory": inventory,
    })


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--timeout", type=float, default=12, help="Total SSH and collection deadline in seconds")
    args = parser.parse_args(argv)
    try:
        result = fetch_inventory(args.host, args.port, args.timeout, args.app_id)
    except ValueError as exc:
        parser.error(str(exc))
    if result["connected"]:
        report = build_diagnosis(args.app_id, result.pop("inventory"))
        report.update(result)
    else:
        report = dict(result, schema=2, app_id=args.app_id, read_only=True, health="unverified")
    print(json.dumps(report, indent=2))
    return 0 if result["connected"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

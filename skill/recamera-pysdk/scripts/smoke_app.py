#!/usr/bin/env python3
"""Load a verified App archive with real Kit in a bounded host subprocess.

An optional caller-written smoke(app) hook supplies controlled frames/model
outputs and assertions. This executes app code; it is not a security sandbox
and is deliberately separate from the non-executing packager.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tarfile
import tempfile
import zipfile

from package_app import inspect_archive, sha256_file


def worker(payload, result, sdk_root, hook):
    report = {"loader": "not_run", "mock_loop": "not_run", "device": "not_run"}
    try:
        if sdk_root:
            sys.path[:0] = [sdk_root, str(Path(sdk_root) / "sdk/python")]
        sys.path.insert(0, str(Path(payload) / "_smoke_wheels"))
        from kit.run import load_app_module, find_app, resolve_entry
        _, entry = resolve_entry(payload)
        app = find_app(load_app_module(entry))
        report["loader"] = "passed"
        if hook:
            import importlib.util
            spec = importlib.util.spec_from_file_location("recamera_smoke_hook", hook)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.smoke(app)
            report["mock_loop"] = "passed"
        return 0
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["loader" if report["loader"] != "passed" else "mock_loop"] = "failed"
        return 1
    finally:
        Path(result).write_text(json.dumps(report, indent=2) + "\n")


def smoke(archive, *, sdk_root=None, hook=None, timeout=15):
    archive = Path(archive).resolve(strict=True)
    info = inspect_archive(archive, sdk_root)
    result = {"archive_sha256": sha256_file(archive), "app_id": info["manifest"]["id"],
              "loader": "not_run", "mock_loop": "not_run", "device": "not_run"}
    if not 0 < timeout <= 300:
        raise ValueError("timeout must be in (0,300] seconds")
    wheels = info["manifest"].get("python", {}).get("wheels", [])
    if any(not w["filename"].endswith("-none-any.whl") for w in wheels):
        return dict(result, error="Native target wheels require a matching device/runtime for import validation.")
    with tempfile.TemporaryDirectory(prefix="recamera-smoke-") as tmp:
        temp = Path(tmp)
        payload = temp / "app"
        with tarfile.open(archive, "r:gz") as package:
            package.extractall(payload, filter="data")
        for wheel in wheels:
            with zipfile.ZipFile(payload / wheel["file"]) as zipped:
                # Final archive validation checks wheel provenance. Still refuse
                # unsafe paths before extracting third-party code for the hook.
                for member in zipped.infolist():
                    p = Path(member.filename)
                    if p.is_absolute() or ".." in p.parts or "\\" in member.filename:
                        raise ValueError("unsafe smoke wheel member")
                zipped.extractall(payload / "_smoke_wheels")
        output = temp / "result.json"
        command = [sys.executable, str(Path(__file__).resolve()), "_worker", str(payload),
                   str(output), str(Path(sdk_root).resolve()) if sdk_root else "",
                   str(Path(hook).resolve(strict=True)) if hook else ""]
        with (temp / "log").open("w+b") as log:
            process = subprocess.Popen(command, stdout=log, stderr=log, cwd=temp,
                                       start_new_session=True)
            timed_out = False
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                result.update(loader="failed", error="Host smoke deadline exceeded")
            finally:
                # Reap even children created by a hook, not just its main process.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            if output.exists() and not timed_out:
                result.update(json.loads(output.read_text()))
            elif "error" not in result:
                result.update(loader="failed", error=f"Smoke worker exited {process.returncode} without a report")
            log.seek(max(0, log.seek(0, 2) - 8192))
            result["log_tail"] = log.read().decode(errors="replace")
    return result


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "_worker":
        return worker(*sys.argv[2:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--sdk-root", type=Path)
    parser.add_argument("--smoke-test", type=Path, help="Trusted Python file exporting smoke(app); never copied into the package")
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = smoke(args.archive, sdk_root=args.sdk_root, hook=args.smoke_test, timeout=args.timeout)
    except Exception as exc:
        result = {"loader": "failed", "mock_loop": "not_run", "device": "not_run", "error": str(exc)}
    args.report.write_text(json.dumps(result, indent=2) + "\n")
    print(args.report)
    return 1 if "failed" in (result["loader"], result["mock_loop"]) else 0 if result["loader"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())

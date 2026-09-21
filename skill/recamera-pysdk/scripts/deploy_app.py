#!/usr/bin/env python3
"""Upload a verified archive over SSH; optionally install/verify via AppMgr.

Default: upload only. Passwords use a prompt or a named environment variable,
then an anonymous sshpass file descriptor, never argv, a file or a report.
"""
import argparse
import getpass
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import tempfile

from package_app import inspect_archive, sha256_file


def redact(value, secret):
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]")
    if isinstance(value, list):
        return [redact(v, secret) for v in value]
    if isinstance(value, dict):
        return {k: redact(v, secret) for k, v in value.items()}
    return value


def ssh_command(host, user, port=22, known_hosts=None, password_fd=None):
    # No arbitrary SSH options or remote command interpolation through host/user.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]*", host):
        raise ValueError("Invalid SSH host; supply an IP or DNS name")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", user) or not 1 <= port <= 65535:
        raise ValueError("Invalid SSH user/port")
    command = ["ssh", "-T", "-p", str(port), "-o", "ConnectTimeout=10",
               "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3",
               "-o", "StrictHostKeyChecking=yes", "-o", "LogLevel=ERROR"]
    if known_hosts:
        command += ["-o", "UserKnownHostsFile=" + str(Path(known_hosts).resolve(strict=True))]
    if password_fd is not None:
        if not shutil.which("sshpass"):
            raise ValueError("Password authentication needs sshpass on the host; SSH keys are also supported")
        command = ["sshpass", "-d", str(password_fd)] + command
        command += ["-o", "NumberOfPasswordPrompts=1"]
    else:
        command += ["-o", "BatchMode=yes"]
    source = Path(__file__).with_name("device_app.py").read_text()
    command += ["-l", user, "--", host, "python3 -u -c " + shlex.quote(source)]
    return command


def run_ssh(command, *, stdin, timeout, pass_fds):
    process = subprocess.Popen(command, stdin=stdin, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, pass_fds=pass_fds,
                               start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    finally:
        # sshpass can have a still-running ssh child after its parent exits.
        # Bound the complete local process group; this cannot cancel a remote
        # AppMgr job already accepted, which remains explicitly uncertain.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()


def deploy(archive, *, host, user, port=22, known_hosts=None, password=None,
           action="upload", replace=False, leave_running=False, timeout=180,
           observe_seconds=10, sdk_root=None):
    if action not in {"upload", "install", "verify"}:
        raise ValueError("Invalid action")
    if not 1 <= timeout <= 1800 or not 2 <= observe_seconds <= 120:
        raise ValueError("timeout must be 1..1800; observe-seconds must be 2..120")
    if leave_running and action != "verify" or replace and action == "upload":
        raise ValueError("--leave-running requires verify; --replace requires install/verify")
    if password is not None and (not password or any(c in password for c in "\n\r\0") or len(password.encode()) > 1024):
        raise ValueError("Empty, multiline or oversized SSH password is unsupported")
    archive = Path(archive).resolve(strict=True)
    report = {"action": action, "host": host, "user": user,
              "upload": "not_run", "install": "not_run", "lifecycle": "not_run",
              "results": "not_run", "task_quality": "not_run"}
    # Snapshot before validating so bytes cannot change between validation and
    # transmission when another build replaces the source archive.
    with tempfile.TemporaryDirectory(prefix="recamera-deploy-") as temp:
        snapshot = Path(temp) / "app.tar.gz"
        shutil.copyfile(archive, snapshot)
        info = inspect_archive(snapshot, sdk_root)
        digest = sha256_file(snapshot)
        config = {"action": action, "replace": replace, "leave_running": leave_running,
                  "timeout": timeout, "observe_seconds": observe_seconds,
                  "manifest": info["manifest"], "release_id": info["release_id"],
                  "sha256": digest, "size": snapshot.stat().st_size}
        report.update(app_id=info["manifest"]["id"], archive_sha256=digest,
                      release_id=info["release_id"], permissions=info["manifest"].get("permissions", {}))
        # A private temporary stream avoids loading 200 MiB archives into RAM.
        transfer = Path(temp) / "transfer"
        with transfer.open("wb") as output, snapshot.open("rb") as source:
            output.write(json.dumps(config).encode() + b"\n")
            shutil.copyfileobj(source, output)
        read_fd = None
        try:
            if password is not None:
                read_fd, write_fd = os.pipe()
                try:
                    os.write(write_fd, password.encode() + b"\n")
                finally:
                    os.close(write_fd)
            command = ssh_command(host, user, port, known_hosts, read_fd)
            # Every remote operation has its own bounded polling interval.
            budget = timeout * (10 if action == "verify" else 1) + observe_seconds * 3 + 120
            with transfer.open("rb") as source:
                completed = run_ssh(command, stdin=source, timeout=budget,
                                    pass_fds=(() if read_fd is None else (read_fd,)))
            stdout = completed.stdout.decode(errors="replace")
            lines = [line.removeprefix("RECAMERA_SKILL_RESULT=") for line in stdout.splitlines()
                     if line.startswith("RECAMERA_SKILL_RESULT=")]
            if completed.returncode or len(lines) != 1:
                report.update(upload="unknown", error="SSH transfer/worker failed; inspect remote state before retrying: "
                              + completed.stderr.decode(errors="replace")[-3000:])
                if action != "upload":
                    report["install"] = "unknown"
                if action == "verify":
                    report["lifecycle"] = report["results"] = "unknown"
            else:
                remote = json.loads(lines[0])
                if remote.get("upload") == "passed" and (remote.get("sha256") != digest or remote.get("app_id") != report["app_id"]):
                    raise ValueError("Remote upload receipt identity mismatch")
                report.update(remote)
        except subprocess.TimeoutExpired:
            report.update(upload="unknown", error="SSH deadline exceeded; device operation may still be running. Inspect device state before retrying.")
            if action != "upload":
                report["install"] = "unknown"
            if action == "verify":
                report["lifecycle"] = report["results"] = "unknown"
        finally:
            if read_fd is not None:
                os.close(read_fd)
    # Device/app log tails can contain arbitrary data; never echo our credential.
    if password is not None:
        report = redact(report, password)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", default="root")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--known-hosts", type=Path, help="A trusted known_hosts file; new/changed keys are never auto-accepted")
    auth = parser.add_mutually_exclusive_group()
    auth.add_argument("--password-env", help="Name of an existing environment variable containing the SSH password")
    auth.add_argument("--ask-password", action="store_true")
    parser.add_argument("--action", choices=("upload", "install", "verify"), default="upload")
    parser.add_argument("--replace", action="store_true", help="Authorize replacement/reinstall of this app only")
    parser.add_argument("--leave-running", action="store_true", help="Keep the app running after a successful verify (default: stop)")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--observe-seconds", type=float, default=10)
    parser.add_argument("--sdk-root", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    output, password = args.report, None
    try:
        if args.password_env:
            # Do not propagate the password env var to the child SSH process.
            password = os.environ.pop(args.password_env)
        elif args.ask_password:
            password = getpass.getpass("SSH password: ")
        options = vars(args).copy()
        for name in ("report", "password_env", "ask_password"):
            options.pop(name)
        result = deploy(**options, password=password)
    except Exception as exc:
        error = str(exc)
        result = {"error": error.replace(password, "[REDACTED]") if password else error,
                  "upload": "not_run", "install": "not_run", "lifecycle": "not_run",
                  "results": "not_run", "task_quality": "not_run"}
    output.parent.mkdir(parents=True, exist_ok=True)
    # Reports can contain app manifests/logs. Keep them local and private.
    with output.open("w") as stream:
        os.chmod(output, 0o600)
        json.dump(result, stream, indent=2)
        stream.write("\n")
    print(output)
    if result.get("error"):
        return 1
    return 2 if args.action == "verify" and result.get("results") != "passed" else 0


if __name__ == "__main__":
    raise SystemExit(main())

import json
import os
import socket
import subprocess
import sys
import time

import pytest

from remote_inventory import collect, path_info, read_file, redact
from target_transport import fetch_inventory
from probe_target import build_report
from diagnose_managed_app import build_diagnosis


def test_missing_resources_are_not_present(tmp_path, monkeypatch):
    monkeypatch.setattr("remote_inventory.os.geteuid", lambda: 1234)
    monkeypatch.setattr("remote_inventory.os.getgid", lambda: 1234)
    monkeypatch.setattr("remote_inventory.os.getgroups", lambda: [1234])
    monkeypatch.setattr("remote_inventory.grp.getgrnam", lambda _: type("G", (), {"gr_gid": 29})())
    inventory = collect(root=tmp_path)
    report = build_report(dict(connected=True, inventory=inventory))
    assert report["facts"]["libasound_present"] is False
    assert report["facts"]["sample_wav_present"] is False
    assert report["facts"]["gmgr_socket_present"] is False
    assert report["facts"]["audio_identity_root_or_audio"] is False
    assert not report["facts"]["inferenced_process_present"]


def test_socket_inode_is_not_service_liveness(tmp_path):
    path = tmp_path / "run/recamera/inferenced.sock"
    path.parent.mkdir(parents=True)
    sock = socket.socket(socket.AF_UNIX)
    try:
        sock.bind(str(path))
    finally:
        sock.close()
    net = tmp_path / "proc/net"
    net.mkdir(parents=True)
    (net / "unix").write_text("Num RefCount Protocol Flags Type St Inode Path\n")
    inventory = collect(root=tmp_path)
    evidence = inventory["sockets"]["/run/recamera/inferenced.sock"]
    assert evidence["kind"] == "socket"
    assert evidence["kernel_registered"] is False
    assert evidence["protocol_verified"] is False
    assert build_report(dict(inventory=inventory))["facts"]["inferenced_process_present"] is False


def test_proc_detection_uses_argv_not_log_strings(tmp_path):
    for pid, argv in [(10, "python3\0-m\0inferenced\0"), (11, "tail\0/tmp/appmgr.log\0"), (12, "python3\0-m\0appmgr\0serve\0")]:
        proc = tmp_path / "proc" / str(pid)
        proc.mkdir(parents=True)
        (proc / "cmdline").write_text(argv)
        # fields after comm: state (3) through starttime (22)
        (proc / "stat").write_text(f"{pid} (python3) S " + "0 " * 18 + "100 0\n")
    report = build_report(dict(inventory=collect(root=tmp_path)))
    assert report["facts"]["appmgr_process_present"] is True
    assert report["facts"]["inferenced_process_present"] is True
    assert len(report["inventory"]["processes"]) == 2


def test_permission_errors_are_unknown(monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError()
    monkeypatch.setattr("remote_inventory.os.stat", denied)
    assert path_info("/some/path")["exists"] is None


def test_bounded_reads_and_fifo(tmp_path):
    path = tmp_path / "log"
    path.write_text("old\n" * 1000 + "last-line\n")
    result = read_file(path, limit=30, tail=True)
    assert result["truncated"] and len(result["text"]) <= 30
    assert result["text"].endswith("last-line\n")
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    assert read_file(fifo)["error"] == "not_regular_file"


def instance_inventory():
    return dict(schema=2, processes=[dict(pid=42, state="S", app_id="demo", started_at=1000,
        environment={"RECAMERA_APP_ID": "demo", "RECAMERA_APP_INSTANCE": "inst", "RECAMERA_APP_GENERATION": "3", "RECAMERA_RESULT_GATEWAY_SOCK": "/run/recamera/gateway.sock"})],
        app=dict(pidfile=42, lifecycle=dict(pid=42, instance_id="inst", generation=3, started_at=1000, observed_state="running"),
                 log=dict(ok=True, text="RuntimeError: RKNN initialization failed", ctime=1001, mtime=1002),
                 operations=[dict(status="failed", app_id="demo")] * 3))


def test_current_rknn_error_overrides_live_pid_gateway():
    report = build_diagnosis("demo", instance_inventory())
    assert report["current_instance_verified"]
    assert report["gateway_injected"]
    assert report["health"] == "errors_observed"
    assert report["log_scope"] == "current_run"


def test_old_log_and_restart_are_not_current_failure():
    inventory = instance_inventory()
    inventory["app"]["log"].update(ctime=800, mtime=900)
    report = build_diagnosis("demo", inventory)
    assert report["log_scope"] == "historical"
    assert report["health"] == "unverified"
    assert report["crash_loop"] is None
    assert report["historical_failed_operations_in_window"] == 3


@pytest.mark.parametrize("field,value", [("pid", 99), ("generation", 1), ("instance_id", "old"), ("started_at", 2000)])
def test_lifecycle_identity_mismatch(field, value):
    inventory = instance_inventory()
    inventory["app"]["lifecycle"][field] = value
    report = build_diagnosis("demo", inventory)
    assert not report["current_instance_verified"]
    assert report["log_scope"] == "unverified"


def test_no_error_is_not_health_confirmation():
    inventory = instance_inventory()
    inventory["app"]["log"]["text"] = "ready"
    assert build_diagnosis("demo", inventory)["health"] == "unverified"


@pytest.mark.parametrize("text", [
    "rtsp://user:private-value@192.0.2.1/live", "password=private-value",
    '"token": "private-value"', "Authorization: Bearer private-value",
])
def test_redaction(text):
    assert "private-value" not in json.dumps(redact({"log": text, "access_token": "private-value"}))


def test_ssh_uses_stdin_and_total_timeout(monkeypatch):
    observed = {}
    def fake_run(argv, **kwargs):
        observed.update(argv=argv, **kwargs)
        return subprocess.CompletedProcess(argv, 0, '{"schema": 2}', '')
    monkeypatch.setattr("target_transport.subprocess.run", fake_run)
    assert fetch_inventory("root@192.0.2.1", timeout=3, app_id="demo")["connected"]
    assert observed["argv"][-1] == "python3 -"
    assert "collect('demo')" in observed["input"]
    assert observed["timeout"] == 3
    assert "sh -c" not in " ".join(observed["argv"])


def test_actual_ssh_shell_join_semantics(tmp_path, monkeypatch):
    # A fake ssh uses the real local shell with OpenSSH's joined remote command.
    # The script must arrive on stdin, never be interpreted as `sh -c set +e`.
    executable = tmp_path / "ssh"
    executable.write_text('#!' + sys.executable + '\nimport os,sys\ni=sys.argv.index("--")\nos.execv("/bin/sh",["sh","-c"," ".join(sys.argv[i+2:])])\n')
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    result = fetch_inventory("test-only", timeout=5)
    assert result["connected"], result
    assert result["inventory"]["schema"] == 2


def test_real_total_timeout(tmp_path, monkeypatch):
    executable = tmp_path / "ssh"
    executable.write_text('#!' + sys.executable + '\nimport time\ntime.sleep(5)\n')
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    start = time.monotonic()
    result = fetch_inventory("test-only", timeout=0.2)
    assert result["error"] == "total_timeout"
    assert time.monotonic() - start < 2


def test_invalid_remote_output_not_echoed(monkeypatch):
    monkeypatch.setattr("target_transport.subprocess.run", lambda *a, **k:
        subprocess.CompletedProcess(a, 0, 'PRIVATE_ENV=value\n', 'password=hidden'))
    report = fetch_inventory("root@192.0.2.1")
    assert not report["connected"]
    assert "PRIVATE_ENV" not in json.dumps(report)
    assert "hidden" not in json.dumps(report)

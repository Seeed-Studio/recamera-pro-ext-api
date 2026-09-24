import copy
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from appmgr import memory_guard as mg, memory_control as mc, paths, resources, state


def sample(available=800, memory=100, identity=None):
    return {"system": {"available_mb": available, "total_mb": 1986},
            "apps": {"voice": {"identity": identity or [101, 123, "voice-1", 1],
                               "pid": 101, "rss_mb": memory, "pss_mb": memory, "anon_mb": memory}},
            "inference": {"present": True, "pids": [202], "identity": [202, 456],
                          "rss_mb": 350, "pss_mb": 300, "anon_mb": 300, "models": []}}


@pytest.fixture
def monitor():
    now = [0.0]
    source = [sample()]
    mon = mg.Monitor(sampler=lambda: copy.deepcopy(source[0]), clock=lambda: now[0])
    def advance(seconds=5):
        for _ in range(seconds):
            now[0] += 1
            mon.sample_once()
    return mon, now, source, advance


def test_pressure_requires_continuous_samples_and_recovery_hysteresis(monitor):
    mon, _, source, advance = monitor
    source[0]["system"]["available_mb"] = 300
    mon.sample_once()
    advance(29)
    assert mon.snapshot()["level"] == "normal"
    advance(1)
    assert mon.snapshot()["level"] == "warning"
    source[0]["system"]["available_mb"] = 240
    advance(11)
    assert mon.snapshot()["level"] == "low"
    source[0]["system"]["available_mb"] = 400
    advance(59)
    assert mon.snapshot()["admission_blocked"]
    advance(2)
    assert mon.snapshot()["level"] == "normal"


def test_critical_pressure_is_immediate_and_missing_proc_is_not_zero(monitor):
    mon, now, source, advance = monitor
    source[0]["system"]["available_mb"] = 120
    mon.sample_once()
    assert mon.snapshot()["level"] == "critical"
    source[0]["system"].pop("available_mb")
    now[0] += 16
    mon.sample_once()
    assert mon.snapshot()["stale"]
    source[0]["system"]["available_mb"] = 240
    advance(1)
    assert mon.snapshot()["level"] == "recovering"


def test_rapid_exhaustion_protects_before_critical_threshold_without_calling_it_a_leak(monitor):
    mon, now, source, advance = monitor
    mon.sample_once()
    now[0] += 5
    source[0]["system"]["available_mb"] = 350
    mon.sample_once()
    assert mon.snapshot()["level"] == "critical"
    assert mon.snapshot()["rapid_decline"]
    assert not mon.snapshot()["apps"]["voice"]["growing"]
    advance(16)
    assert not mon.snapshot()["rapid_decline"]


def test_warmup_step_change_and_sustained_growth_are_distinguished(monitor):
    mon, _, source, advance = monitor
    mon.sample_once()
    source[0]["apps"]["voice"].update(pss_mb=700, rss_mb=800)
    advance(425)
    assert not mon.snapshot()["apps"]["voice"]["growing"]
    for _ in range(61):
        source[0]["apps"]["voice"]["pss_mb"] += 2
        advance(5)
    assert mon.snapshot()["apps"]["voice"]["growing"]
    assert mon.snapshot()["level"] == "normal"  # Growth alone never evicts.
    source[0]["apps"]["voice"]["identity"][3] = 2
    advance(1)
    assert not mon.snapshot()["apps"]["voice"]["growing"]


def test_model_set_change_resets_shared_service_baseline(monitor):
    mon, _, source, advance = monitor
    advance(130)
    for _ in range(61):
        source[0]["inference"]["pss_mb"] += 2
        advance(5)
    assert mon.snapshot()["inference"]["growing"]
    source[0]["inference"]["models"] = [{"key": "new-model"}]
    advance(1)
    assert mon.snapshot()["inference"]["warming_up"]
    assert not mon.snapshot()["inference"]["growing"]


def test_history_and_disappeared_generations_are_bounded(monitor):
    mon, _, source, advance = monitor
    advance(2000)
    assert len(mon.snapshot()["apps"]["voice"]["history"]) <= 49
    source[0]["apps"] = {}
    advance(1)
    assert "voice" not in mon._tracks


def test_resource_admission_holds_during_recovery(monitor, monkeypatch, tmp_path):
    mon, _, source, advance = monitor
    monkeypatch.setattr(mg, "active_monitor", mon)
    manager = resources.ResourceManager(str(tmp_path / "resources"), runtime_probe=lambda: {})
    source[0]["system"]["available_mb"] = 100
    mon.sample_once()
    with pytest.raises(mg.MemoryPressureError):
        mg.check_admission()
    with pytest.raises(resources.ResourceBusy, match="memory.pressure"):
        manager.reserve("new", "instance", 1, resources.plan_manifest({"id": "new"}))
    source[0]["system"]["available_mb"] = 500
    advance(61)
    mg.check_admission()


@pytest.mark.parametrize("field,value", [("LOW_MB", "nan"), ("GROWTH_MB", "-1"), ("LOW_MB", "400")])
def test_invalid_policy_rejected(monkeypatch, field, value):
    monkeypatch.setenv("APPMGR_MEMORY_" + field, value)
    with pytest.raises(ValueError):
        mg.Policy.from_env()


@pytest.fixture
def control(tmp_path, monkeypatch, monitor):
    monkeypatch.setattr(paths, "APPS_DIR", str(tmp_path / "apps"))
    monkeypatch.setattr(paths, "APPMGR_DIR", str(tmp_path / "appmgr"))
    monkeypatch.setattr(paths, "STATE_FILE", str(tmp_path / "state.json"))
    enabled = tmp_path / "enabled"
    enabled.touch()
    launcher = tmp_path / "launcher"
    launcher.touch()
    mon, now, source, advance = monitor
    ctrl = mc.Controller(mon, clock=lambda: now[0], wall_clock=lambda: 10000 + now[0],
                         enabled_file=str(enabled), init_script=str(launcher))
    live = {"voice": 101, "cpu": 102}
    for app_id, pid in live.items():
        state.transition(app_id, "running", desired_state="running", pid=pid, pgid=pid,
                         instance_id=app_id + "-1", generation=1,
                         resource_plan={"npu_mode": "scheduled" if app_id == "voice" else "none"})
    monkeypatch.setattr(mc.supervisor, "is_running", lambda app_id: live.get(app_id))
    monkeypatch.setattr(mg, "process_identity", lambda pid: (123 if pid == 101 else 456, pid, 1))
    monkeypatch.setattr(mg, "inference_pids", lambda: source[0]["inference"]["pids"])
    stopped, restarted = [], []
    def stop(app_id):
        stopped.append(app_id)
        live.pop(app_id, None)
        state.transition(app_id, "stopped", desired_state="stopped")
    def run(*args, **kwargs):
        restarted.append(args[0])
        return SimpleNamespace(returncode=0, stdout="ready")
    monkeypatch.setattr(mc.subprocess, "run", run)
    return SimpleNamespace(ctrl=ctrl, mon=mon, now=now, source=source, advance=advance,
                           live=live, coord=SimpleNamespace(stop=stop), stopped=stopped,
                           restarted=restarted, enabled=enabled, tmp=tmp_path)


def test_emergency_stops_one_app_persists_hold_and_never_calls_it_a_leak(control):
    c = control
    c.source[0]["system"]["available_mb"] = 100
    c.mon.sample_once()
    assert c.ctrl.tick(c.coord)
    assert c.stopped == ["voice"]
    rec = state.get_app("voice")
    assert rec["desired_state"] == "stopped"
    assert rec["memory_protection"]["reason"] == "system_memory_pressure"
    assert "voice" not in state.desired_apps()
    assert not c.ctrl.tick(c.coord)
    assert c.live == {"cpu": 102}


@pytest.mark.parametrize("race", ["pid_reused", "generation_changed", "stale"])
def test_never_stop_new_or_stale_identity(control, monkeypatch, race):
    c = control
    c.source[0]["system"]["available_mb"] = 100
    c.mon.sample_once()
    if race == "pid_reused":
        monkeypatch.setattr(mg, "process_identity", lambda pid: (999, pid, 1))
    elif race == "generation_changed":
        state.transition("voice", "running", generation=2)
    else:
        c.now[0] += 16
    c.ctrl.tick(c.coord)
    assert not c.stopped


def test_service_crash_holds_only_dependents_and_budget_survives_controller_restart(control):
    c = control
    c.source[0]["inference"] = {"present": False, "pids": []}
    c.mon.sample_once()
    c.advance(11)
    for _ in range(3):
        assert c.ctrl.tick(c.coord)
        c.advance(130)
        c.ctrl = mc.Controller(c.mon, clock=lambda: c.now[0], wall_clock=lambda: 10000 + c.now[0],
                               enabled_file=str(c.enabled), init_script=str(c.tmp / "launcher"))
    assert not c.ctrl.tick(c.coord)
    assert c.stopped == ["voice"]
    assert len(c.restarted) == 3
    assert state.get_app("cpu")["desired_state"] == "running"
    assert state.get_app("voice")["memory_protection"]["reason"] == "inference_service_lost"


def test_explicit_service_stop_disables_watchdog_and_new_process_wins_race(control, monkeypatch):
    c = control
    c.source[0]["inference"] = {"present": False, "pids": []}
    c.advance(11)
    c.enabled.unlink()
    assert not c.ctrl.tick(c.coord)
    c.enabled.touch()
    monkeypatch.setattr(mg, "inference_pids", lambda: [303])
    assert not c.ctrl.tick(c.coord)
    assert not c.restarted
    assert not c.stopped


def test_proc_sampler_includes_children_without_double_counting_shared_pages(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "meminfo").write_text("MemTotal: 2048000 kB\nMemAvailable: 800000 kB\n")
    for pid, ppid, pgid, pss in [(101, 1, 101, 1200), (102, 101, 101, 600), (103, 102, 103, 300)]:
        entry = proc / str(pid)
        entry.mkdir()
        # Field 22 is starttime; comm can contain spaces and parentheses.
        tail = ["S", str(ppid), str(pgid)] + ["0"] * 16 + ["456"]
        (entry / "stat").write_text(str(pid) + " (test (child)) " + " ".join(tail))
        (entry / "status").write_text("VmRSS: 2048 kB\nRssAnon: 1024 kB\n")
        (entry / "smaps_rollup").write_text("Pss: %d kB\n" % pss)
        (entry / "cmdline").write_bytes(b"test\x00")
    monkeypatch.setattr(state, "app_states", lambda: {"voice": {"instance_id": "voice-1", "generation": 1}})
    monkeypatch.setattr(mg.supervisor, "is_running", lambda _: 101)
    result = mg.ProcSampler(proc)()["apps"]["voice"]
    assert result["processes"] == 3
    assert result["rss_mb"] == 6
    assert result["pss_mb"] == 2100 / 1024


def test_api_never_reuses_measurement_for_new_generation(monitor, monkeypatch):
    mon, _, _, _ = monitor
    mon.sample_once()
    monkeypatch.setattr(mg, "active_monitor", mon)
    assert mg.app_view("voice", {"instance_id": "voice-1", "generation": 1})["process"]
    assert mg.app_view("voice", {"instance_id": "voice-1", "generation": 2})["process"] is None


def test_shared_growth_does_not_blame_cpu_app_and_retained_memory_can_restart_service(control):
    c = control
    c.source[0]["system"]["available_mb"] = 240
    c.advance(130)
    for _ in range(61):
        c.source[0]["inference"]["pss_mb"] += 2
        c.advance(5)
    assert c.mon.snapshot()["inference"]["growing"]
    assert c.ctrl.tick(c.coord)
    assert c.stopped == ["voice"]
    assert state.get_app("voice")["memory_protection"]["reason"] == "shared_inference_pressure"
    c.source[0]["apps"] = {}
    c.source[0]["inference"]["models"] = [{"key": "changed-after-unload"}]
    c.advance(21)
    assert not c.mon.snapshot()["inference"]["growing"]
    assert c.ctrl.tick(c.coord)
    assert len(c.restarted) == 1
    assert c.live == {"cpu": 102}


def test_protection_result_invalidation_never_leaves_mutation_gate(control, monkeypatch):
    from appmgr import server
    c = control
    c.source[0]["system"]["available_mb"] = 100
    c.mon.sample_once()
    monkeypatch.setattr(server, "_memory_controller", c.ctrl)
    monkeypatch.setattr(server, "_coordinator", lambda: c.coord)
    monkeypatch.setattr(server, "_service_stopping", False)
    locked = [False]
    invalidated = []
    @contextmanager
    def gate():
        locked[0] = True
        try:
            yield
        finally:
            locked[0] = False
    def invalidate(app_id):
        assert locked[0], "a concurrent start could lose its result source"
        invalidated.append(app_id)
    monkeypatch.setattr(server, "busy_gate", gate)
    monkeypatch.setattr(server, "_invalidate_result_if_inactive", invalidate)
    assert server._reconcile_once() == []
    assert invalidated == ["voice"]


def test_pressure_rejects_upload_before_reading_and_keeps_stop_available(monitor, monkeypatch):
    from appmgr import server
    mon, _, source, _ = monitor
    source[0]["system"]["available_mb"] = 100
    mon.sample_once()
    monkeypatch.setattr(mg, "active_monitor", mon)
    with pytest.raises(mg.MemoryPressureError):
        server.do_v1_upload(None, 1024, "application/octet-stream")
    with pytest.raises(mg.MemoryPressureError):
        server.do_v1_lifecycle("builtin", "start")
    calls = []
    manager = SimpleNamespace(submit=lambda action, app_id, callback: calls.append(action))
    monkeypatch.setattr(server, "_operation_manager", lambda: manager)
    server.do_v1_lifecycle("builtin", "stop")
    assert calls == ["stop"]

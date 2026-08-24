import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
from appmgr import coordinator, paths, resources, state


class FakeSupervisor:
    def __init__(self, events=None):
        self.running = {}
        self.starts = []
        self.stops = []
        self.exits = {}
        self.next_pid = 4100
        self.events = events if events is not None else []
        self.stop_hook = None
        self.force_stop = False

    def is_running(self, app_id):
        return self.running.get(app_id)

    def start(self, app_id, **kwargs):
        assert app_id not in self.running, "max_instances=1 violated"
        self.next_pid += 1
        pid = self.next_pid
        self.starts.append((app_id, dict(kwargs)))
        self.running[app_id] = pid
        try:
            if kwargs.get("on_spawn"):
                kwargs["on_spawn"](pid)
        except Exception:
            self.running.pop(app_id, None)
            raise
        return pid

    def stop(self, app_id, **kwargs):
        self.events.append(("supervisor.stop", app_id))
        if self.stop_hook is not None:
            self.stop_hook(app_id)
        if self.force_stop:
            callback = kwargs.get("before_force_kill")
            if callback is not None:
                callback()
            self.events.append(("supervisor.kill", app_id))
        self.stops.append(app_id)
        pid = self.running.pop(app_id, None)
        return {"app": app_id, "pid": pid, "signalled": pid is not None,
                "killed": bool(pid is not None and self.force_stop)}

    def last_exit(self, app_id):
        return self.exits.get(app_id)

    def crash(self, app_id, code=1):
        pid = self.running.pop(app_id)
        self.exits[app_id] = {"pid": pid, "code": code, "signal": None}
        return pid


class FakeInferenceRegistry:
    def __init__(self, events=None):
        self.prepared = []
        self.published = []
        self.revoked = []
        self.events = events if events is not None else []

    def prepare(self, app_id, manifest):
        policy = {"app_id": app_id, "manifest": manifest}
        self.prepared.append(policy)
        return policy

    def publish(self, policy, **identity):
        self.events.append(("registry.publish", identity.get("pid")))
        self.published.append((policy, identity))

    def revoke(self, pid, **identity):
        self.events.append(("registry.revoke", pid))
        self.revoked.append((pid, identity))
        return True


def _manifest(app_id, claims=()):
    return {
        "manifest_version": 2,
        "id": app_id,
        "version": "1.0.0",
        "entry": "app.py",
        "instances": {"max": 1},
        "resources": {"claims": list(claims)},
        "config_schema": {"groups": []},
        "health": {
            "stabilization_sec": 2,
            "restart": {"policy": "on-failure", "max_attempts": 3,
                        "window_sec": 300, "backoff_sec": [0, 0, 0]},
        },
    }


@pytest.fixture
def managed(tmp_path, monkeypatch):
    apps = tmp_path / "apps"
    appmgr = tmp_path / "appmgr"
    apps.mkdir()
    appmgr.mkdir()
    monkeypatch.setattr(paths, "APPS_DIR", str(apps))
    monkeypatch.setattr(paths, "APPMGR_DIR", str(appmgr))
    monkeypatch.setattr(paths, "STATE_FILE", str(apps / "state.json"))
    monkeypatch.setattr(paths, "RESULT_GATEWAY_SOCK", str(tmp_path / "results.sock"))
    monkeypatch.setattr(paths, "INFERENCE_SERVICE_SOCK", str(tmp_path / "inferenced.sock"))
    state.save({"active_app": None, "active_version": None})
    events = []
    fake = FakeSupervisor(events)
    manager = resources.ResourceManager(str(appmgr / "resources.json"))
    coord = coordinator.AppCoordinator(
        resource_manager=manager, supervisor_module=fake,
        dependency_probe=lambda _path: {"available": True},
        result_gateway_sock=str(tmp_path / "results.sock"),
        inference_service_sock=str(tmp_path / "inferenced.sock"),
        inference_registry=FakeInferenceRegistry(events))
    return coord, fake, manager


def test_two_cpu_apps_run_concurrently_and_same_app_is_idempotent(managed):
    coord, fake, manager = managed
    result_claim = {"name": "result.publish", "mode": "brokered",
                    "required": True}

    a = coord.start("cpu-a", manifest=_manifest("cpu-a", [result_claim]))
    b = coord.start("cpu-b", manifest=_manifest("cpu-b", [result_claim]))
    again = coord.start("cpu-a", manifest=_manifest("cpu-a", [result_claim]))

    assert a["observed_state"] == b["observed_state"] == "running"
    assert a["pid"] != b["pid"]
    assert again["pid"] == a["pid"] and again["idempotent"] is True
    assert len(fake.starts) == 2
    assert all(call[1]["result_gateway_sock"].endswith("results.sock")
               for call in fake.starts)
    assert all(call[1]["instance_id"] for call in fake.starts)

    stopped = coord.stop("cpu-a")
    assert stopped["observed_state"] == "stopped"
    assert fake.is_running("cpu-b") == b["pid"]
    allocations = manager.snapshot()["allocations"]
    assert {item["app_id"] for item in allocations} == {"cpu-b"}


def test_unbound_starting_observation_is_not_misclassified_as_a_crash(managed):
    """A read racing Popen must not clear the not-yet-bound generation."""
    coord, _fake, manager = managed
    app_id = "startup-window"
    instance = "instance-before-popen"
    rec = state.begin_start(app_id, instance, version="1.0.0")
    generation = int(rec["generation"])
    plan = resources.plan_manifest(_manifest(app_id), {})
    allocations = manager.reserve(app_id, instance, generation, plan)
    state.transition(
        app_id,
        "starting",
        pid=None,
        allocations=[item["allocation_id"] for item in allocations],
    )

    observed = coord.observe(app_id, None)

    assert observed["observed_state"] == "starting"
    assert observed["pid"] is None
    assert manager.snapshot()["allocations"] == allocations
    assert coord.inference_registry.revoked == []


def test_gateway_identity_requires_supervisor_authenticated_peer(managed):
    """A correct hello/state tuple alone is never an authentication proof."""
    coord, fake, _manager = managed
    app_id = "gateway-bound"
    instance = "exact-instance"
    pid = 4777
    rec = state.begin_start(app_id, instance, version="1.0.0")
    generation = int(rec["generation"])
    state.transition(
        app_id,
        "starting",
        pid=pid,
        pgid=pid,
        instance_id=instance,
        generation=generation,
    )

    # Claimed identity and coordinator state match, but no root-owned
    # supervisor run record authenticates the SO_PEERCRED PID yet.
    assert coord.resolve_identity(pid, app_id, instance, generation) is None

    fake.running[app_id] = pid
    accepted = coord.resolve_identity(pid, app_id, instance, generation)
    assert accepted == {
        "app_id": app_id,
        "instance_id": instance,
        "generation": generation,
        "pid": pid,
    }
    # The hello cannot substitute a different kernel peer.
    assert coord.resolve_identity(pid + 1, app_id, instance, generation) is None


def test_live_process_finishes_commit_interrupted_before_on_spawn(managed):
    """Daemon restart adopts run.pid without minting a second generation."""
    coord, fake, manager = managed
    app_id = "boot-adopt"
    claims = [
        {"name": "npu.rknn", "mode": "scheduled", "required": True},
        {"name": "result.publish", "mode": "brokered", "required": True},
    ]
    manifest = _manifest(app_id, claims)
    instance = "interrupted-on-spawn"
    pid = 4888
    rec = state.begin_start(app_id, instance, version="1.0.0")
    generation = int(rec["generation"])
    allocations = manager.reserve(
        app_id, instance, generation, resources.plan_manifest(manifest, {}))
    state.transition(
        app_id,
        "starting",
        pid=None,
        pgid=None,
        allocations=[item["allocation_id"] for item in allocations],
    )
    fake.running[app_id] = pid

    # Simulate appmgr dying after run.pid became authoritative but before the
    # on_spawn callback could bind that PID into state.  Boot restore calls
    # start() directly; it must adopt the live leader and the same generation
    # without requiring a prior read/observe side effect.
    adopted = coord.start(app_id, manifest=manifest, operation="boot_adopt")
    assert adopted["pid"] == pid
    assert adopted["instance_id"] == instance
    assert adopted["generation"] == generation
    assert adopted["observed_state"] == "running"
    assert adopted["idempotent"] is True
    assert fake.starts == []
    assert all(item["state"] == "bound"
               for item in manager.snapshot()["allocations"])
    assert coord.inference_registry.published[-1][1] == {
        "pid": pid,
        "instance_id": instance,
        "generation": generation,
    }
    assert coord.resolve_identity(pid, app_id, instance, generation)["pid"] == pid


def test_scheduled_npu_is_shared_service_and_never_direct_broker(managed):
    coord, fake, manager = managed
    claims = [
        {"name": "npu.rknn", "mode": "scheduled", "required": True},
        {"name": "result.publish", "mode": "brokered", "required": True},
    ]
    first = coord.start("npu-a", manifest=_manifest("npu-a", claims))
    second = coord.start("npu-b", manifest=_manifest("npu-b", claims))

    assert first["observed_state"] == second["observed_state"] == "running"
    assert all(kwargs["npu_mode"] == "scheduled" for _, kwargs in fake.starts)
    assert all(kwargs["inference_service_sock"].endswith("inferenced.sock")
               for _, kwargs in fake.starts)
    assert all("npu_broker_required" not in kwargs for _, kwargs in fake.starts)
    npu = [a for a in manager.snapshot()["allocations"]
           if a["resource"] == "npu.scheduler"]
    assert {a["app_id"] for a in npu} == {"npu-a", "npu-b"}
    published = coord.inference_registry.published
    assert [item[1]["pid"] for item in published] == [first["pid"], second["pid"]]
    assert [item[1]["generation"] for item in published] == [
        first["generation"], second["generation"]
    ]

    start = len(fake.events)
    coord.stop("npu-a")
    assert fake.events[start:start + 2] == [
        ("supervisor.stop", "npu-a"),
        ("registry.revoke", first["pid"]),
    ]


def test_cooperative_stop_keeps_authorization_until_app_teardown(managed):
    coord, fake, _manager = managed
    claims = [{"name": "npu.rknn", "mode": "scheduled", "required": True}]
    started = coord.start("graceful", manifest=_manifest("graceful", claims))
    registry = coord.inference_registry

    def app_finish(_app_id):
        # This hook stands in for App.finish -> RemoteRknnModel.release while
        # supervisor.stop is waiting inside the TERM grace period.
        assert registry.revoked == []
        fake.events.append(("app.release", started["pid"]))

    fake.stop_hook = app_finish
    offset = len(fake.events)
    coord.stop("graceful")

    assert fake.events[offset:] == [
        ("supervisor.stop", "graceful"),
        ("app.release", started["pid"]),
        ("registry.revoke", started["pid"]),
    ]
    assert len(registry.revoked) == 1


def test_forced_stop_revokes_exact_generation_before_sigkill(managed):
    coord, fake, _manager = managed
    claims = [{"name": "npu.rknn", "mode": "scheduled", "required": True}]
    started = coord.start("forced", manifest=_manifest("forced", claims))
    fake.force_stop = True

    offset = len(fake.events)
    coord.stop("forced")

    assert fake.events[offset:] == [
        ("supervisor.stop", "forced"),
        ("registry.revoke", started["pid"]),
        ("supervisor.kill", "forced"),
    ]
    assert coord.inference_registry.revoked == [(
        started["pid"],
        {
            "app_id": "forced",
            "instance_id": started["instance_id"],
            "generation": started["generation"],
        },
    )]


def test_crash_observation_revokes_without_waiting_for_cooperative_stop(managed):
    coord, fake, manager = managed
    claims = [{"name": "npu.rknn", "mode": "scheduled", "required": True}]
    started = coord.start("crashed", manifest=_manifest("crashed", claims))
    fake.crash("crashed", code=137)

    offset = len(fake.events)
    observed = coord.observe("crashed", None, fake.last_exit("crashed"))

    assert fake.events[offset:] == [("registry.revoke", started["pid"])]
    assert observed["observed_state"] == "failed"
    assert not [item for item in manager.snapshot()["allocations"]
                if item["app_id"] == "crashed"]


def test_direct_legacy_owner_cannot_overlap_scheduled_service(managed):
    coord, fake, _manager = managed
    scheduled = _manifest("scheduled", [
        {"name": "npu.rknn", "mode": "scheduled", "required": True},
    ])
    legacy = {"id": "legacy", "version": "1", "entry": "app.py",
              "models": [{"file": "models/model.rknn"}]}

    assert coord.start("scheduled", manifest=scheduled)["pid"] is not None
    blocked = coord.start("legacy", manifest=legacy)

    assert blocked["pid"] is None
    assert blocked["observed_state"] == "waiting_resource"
    assert "npu.direct" in blocked["reason"]
    assert [app for app, _ in fake.starts] == ["scheduled"]


def test_npu_conflict_group_is_symmetric_and_direct_is_single_owner(managed):
    coord, fake, manager = managed
    first_direct = {"id": "direct-a", "version": "1", "entry": "app.py",
                    "models": [{"file": "models/a.rknn"}]}
    second_direct = {"id": "direct-b", "version": "1", "entry": "app.py",
                     "models": [{"file": "models/b.rknn"}]}
    scheduled = _manifest("scheduled-after-direct", [
        {"name": "npu.rknn", "mode": "scheduled", "required": True},
    ])

    assert coord.start("direct-a", manifest=first_direct)["pid"] is not None
    blocked_scheduled = coord.start(
        "scheduled-after-direct", manifest=scheduled)
    blocked_direct = coord.start("direct-b", manifest=second_direct)

    assert blocked_scheduled["observed_state"] == "waiting_resource"
    assert blocked_direct["observed_state"] == "waiting_resource"
    assert [app for app, _ in fake.starts] == ["direct-a"]
    conflicts = manager.conflicts(
        resources.plan_manifest(scheduled))
    assert conflicts and conflicts[0]["owners"] == ["direct-a"]


def test_missing_inferenced_is_waiting_dependency_without_spawn(tmp_path, monkeypatch):
    apps = tmp_path / "apps"
    appmgr = tmp_path / "appmgr"
    apps.mkdir(); appmgr.mkdir()
    monkeypatch.setattr(paths, "APPS_DIR", str(apps))
    monkeypatch.setattr(paths, "APPMGR_DIR", str(appmgr))
    monkeypatch.setattr(paths, "STATE_FILE", str(apps / "state.json"))
    state.save({})
    fake = FakeSupervisor()
    coord = coordinator.AppCoordinator(
        resource_manager=resources.ResourceManager(str(appmgr / "resources.json")),
        supervisor_module=fake,
        dependency_probe=lambda path: {"available": False, "error": "refused"},
        inference_registry=FakeInferenceRegistry())
    manifest = _manifest("npu-wait", [
        {"name": "npu.rknn", "mode": "brokered", "required": True},
    ])

    result = coord.start("npu-wait", manifest=manifest)

    assert result["observed_state"] == "waiting_dependency"
    assert result["pid"] is None
    assert "refused" in result["reason"]
    assert fake.starts == []


def test_resource_profile_uses_effective_config_and_max_instances_is_one():
    manifest = _manifest("voice")
    manifest["config_schema"] = {"groups": [{
        "key": "runtime", "title": "Runtime", "items": [
            {"key": "backend", "type": "enum", "apply": "reschedule",
             "default": "rk"},
        ],
    }]}
    manifest["resources"] = {"profiles": [
        {"when": {"backend": "rk"}, "claims": [
            {"name": "npu.rknn", "mode": "scheduled", "required": True},
        ]},
        {"when": {"backend": "cpu"}, "claims": []},
    ]}

    assert resources.plan_manifest(manifest).npu_mode == "scheduled"
    assert resources.plan_manifest(manifest, {"backend": "cpu"}).npu_mode == "none"
    manifest["instances"]["max"] = 2
    with pytest.raises(resources.ResourceError, match="instances.max=1"):
        resources.plan_manifest(manifest)


def test_stale_generation_cannot_release_new_allocation(managed):
    coord, _fake, manager = managed
    manifest = _manifest("generation")
    first = coord.start("generation", manifest=manifest)
    coord.stop("generation")
    second = coord.start("generation", manifest=manifest)
    assert second["generation"] > first["generation"]

    manager.release(first["instance_id"], first["generation"])
    current = manager.snapshot()["allocations"]
    assert current
    assert {item["instance_id"] for item in current} == {second["instance_id"]}


def test_health_restart_enters_crash_loop_after_bounded_attempts(managed):
    coord, fake, manager = managed
    manifest = _manifest("crashy")
    manifest["health"]["restart"]["max_attempts"] = 2
    first = coord.start("crashy", manifest=manifest)

    fake.crash("crashy")
    restarted_one = coord.reconcile_one(
        "crashy", manifest=manifest, launch=lambda **kw: fake.start("crashy", **kw),
        now=100.0)
    assert restarted_one["action"] == "restarted"
    assert restarted_one["generation"] > first["generation"]

    fake.crash("crashy")
    restarted_two = coord.reconcile_one(
        "crashy", manifest=manifest, launch=lambda **kw: fake.start("crashy", **kw),
        now=101.0)
    assert restarted_two["action"] == "restarted"

    fake.crash("crashy")
    exhausted = coord.reconcile_one(
        "crashy", manifest=manifest, launch=lambda **kw: fake.start("crashy", **kw),
        now=102.0)
    assert exhausted["action"] == "crash_loop"
    assert state.get_app("crashy")["observed_state"] == "crash_loop"
    assert len(fake.starts) == 3
    assert not [a for a in manager.snapshot()["allocations"]
                if a["app_id"] == "crashy"]


def test_explicit_stop_cancels_desired_restart(managed):
    coord, fake, _manager = managed
    manifest = _manifest("stopped-by-user")
    coord.start("stopped-by-user", manifest=manifest)
    fake.crash("stopped-by-user")
    coord.stop("stopped-by-user")

    result = coord.reconcile_one(
        "stopped-by-user", manifest=manifest,
        launch=lambda **kw: fake.start("stopped-by-user", **kw), now=200.0)

    assert result["action"] == "stopped"
    assert state.get_app("stopped-by-user")["desired_state"] == "stopped"
    assert len(fake.starts) == 1


def test_daemon_restart_stale_running_record_is_restored_without_crash_budget(managed):
    coord, fake, _manager = managed
    manifest = _manifest("restore-after-daemon")
    first = coord.start("restore-after-daemon", manifest=manifest)
    fake.running.pop("restore-after-daemon")
    fake.exits.pop("restore-after-daemon", None)

    restored = coord.reconcile_one(
        "restore-after-daemon", manifest=manifest,
        launch=lambda **kw: fake.start("restore-after-daemon", **kw), now=300.0)

    assert restored["pid"] is not None
    assert restored["generation"] > first["generation"]
    assert state.get_app("restore-after-daemon")["restart_history"] == []

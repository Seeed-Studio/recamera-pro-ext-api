import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
from appmgr import coordinator, paths, resources, state, supervisor


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
        self.run_records = set()

    def is_running(self, app_id):
        return self.running.get(app_id)

    def has_run_record(self, app_id):
        return app_id in self.run_records

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

    # Admission diagnostics belong only to waiting_resource/dependency.  A
    # successful stop must clear old persisted values so later API snapshots
    # cannot report owners from an unrelated generation.
    state.transition(
        "cpu-a", "running",
        blocked_resource="camera.frame:camera-0",
        resource_owners=["old-owner"],
        dependency={"available": False},
        runtime_guard={"resource": "thermal.runtime"},
    )
    stopped = coord.stop("cpu-a")
    assert stopped["observed_state"] == "stopped"
    stopped_state = state.get_app("cpu-a")
    assert stopped_state["blocked_resource"] is None
    assert stopped_state["resource_owners"] == []
    assert stopped_state["dependency"] is None
    assert stopped_state["runtime_guard"] is None
    # Assert the raw durable document too: state.get_app() normalises reads and
    # could otherwise hide a regression that leaves stale owners on disk.
    with open(paths.STATE_FILE) as state_file:
        persisted = json.load(state_file)["apps"]["cpu-a"]
    assert persisted["blocked_resource"] is None
    assert persisted["resource_owners"] == []
    assert persisted["dependency"] is None
    assert persisted["runtime_guard"] is None
    assert fake.is_running("cpu-b") == b["pid"]
    allocations = manager.snapshot()["allocations"]
    assert {item["app_id"] for item in allocations} == {"cpu-b"}


def test_runtime_hard_thermal_guard_releases_generation_and_waits_for_cooldown(
        managed, monkeypatch):
    monkeypatch.delenv("APPMGR_START_MAX_TEMP_C", raising=False)
    monkeypatch.delenv("APPMGR_RUNTIME_HARD_TEMP_C", raising=False)
    coord, fake, manager = managed
    sample = {
        "mem_available_mb": 4096,
        "storage_free_mb": 16384,
        "temperature_c": 50.0,
    }
    manager._runtime_probe = lambda: dict(sample)
    manifest = _manifest("thermal-app", [{
        "name": "result.publish", "mode": "brokered", "required": True,
    }])
    manifest["resources"]["limits"] = {
        "memory_mb": 128,
        "storage_mb": 16,
        "cpu_percent": 100,
        "shutdown_grace_sec": 5,
    }
    started = coord.start("thermal-app", manifest=manifest)
    assert started["observed_state"] == "running"

    sample["temperature_c"] = 110.1
    stopped = coord.reconcile_one(
        "thermal-app", manifest=manifest,
        launch=lambda **kwargs: fake.start("thermal-app", **kwargs),
        now=100.0, retry_interval=0.01,
    )
    assert stopped["action"] == "safety_stop"
    assert stopped["observed_state"] == "waiting_resource"
    assert stopped["runtime_guard"]["resource"] == "thermal.runtime"
    assert fake.is_running("thermal-app") is None
    assert state.get_app("thermal-app")["desired_state"] == state.DESIRED_RUNNING
    assert not [item for item in manager.snapshot()["allocations"]
                if item["app_id"] == "thermal-app"]

    sample["temperature_c"] = 100.0
    held = coord.reconcile_one(
        "thermal-app", manifest=manifest,
        launch=lambda **kwargs: fake.start("thermal-app", **kwargs),
        now=time.time() + 2.0, retry_interval=0.01,
    )
    assert held["observed_state"] == "waiting_resource"
    assert fake.is_running("thermal-app") is None
    held_identity = (held["instance_id"], held["generation"])
    assert held_identity[0] != started["instance_id"]
    assert held_identity[1] > started["generation"]

    sample["temperature_c"] = 99.9
    restored = coord.reconcile_one(
        "thermal-app", manifest=manifest,
        launch=lambda **kwargs: fake.start("thermal-app", **kwargs),
        now=time.time() + 4.0, retry_interval=0.01,
    )
    assert restored["observed_state"] == "running"
    assert restored["generation"] > started["generation"]
    assert (restored["instance_id"], restored["generation"]) == held_identity


def test_waiting_resource_retries_reuse_one_unspawned_generation(managed):
    coord, fake, _manager = managed
    claim = {
        "name": "camera.frames", "mode": "exclusive", "required": True,
    }
    owner_manifest = _manifest("camera-owner", [claim])
    waiter_manifest = _manifest("camera-waiter", [claim])
    assert coord.start("camera-owner", manifest=owner_manifest)["pid"] is not None

    first = coord.start("camera-waiter", manifest=waiter_manifest)
    assert first["observed_state"] == "waiting_resource"
    identity = (first["instance_id"], first["generation"])
    waiting_revision = state.load()["revision"]
    assert [app_id for app_id, _kwargs in fake.starts] == ["camera-owner"]

    boot_retry = coord.start(
        "camera-waiter", manifest=waiter_manifest, operation="boot_restore")
    assert boot_retry["observed_state"] == "waiting_resource"
    assert (boot_retry["instance_id"], boot_retry["generation"]) == identity
    assert state.load()["revision"] == waiting_revision

    for offset in (10.0, 20.0, 30.0):
        waiting = coord.reconcile_one(
            "camera-waiter", manifest=waiter_manifest,
            launch=lambda **kwargs: fake.start("camera-waiter", **kwargs),
            now=time.time() + offset, retry_interval=0.01,
        )
        assert waiting["observed_state"] == "waiting_resource"
        assert (waiting["instance_id"], waiting["generation"]) == identity
        assert [app_id for app_id, _kwargs in fake.starts] == ["camera-owner"]
    assert state.load()["revision"] == waiting_revision

    coord.stop("camera-owner")
    restored = coord.reconcile_one(
        "camera-waiter", manifest=waiter_manifest,
        launch=lambda **kwargs: fake.start("camera-waiter", **kwargs),
        now=time.time() + 40.0, retry_interval=0.01,
    )
    assert restored["observed_state"] == "running"
    assert (restored["instance_id"], restored["generation"]) == identity


def test_waiting_dependency_retries_reuse_one_unspawned_generation(managed):
    coord, fake, _manager = managed
    dependency = {"available": False, "error": "starting"}
    coord.dependency_probe = lambda _path: dict(dependency)
    manifest = _manifest("dependency-waiter", [{
        "name": "npu.rknn", "mode": "scheduled", "required": True,
    }])

    first = coord.start("dependency-waiter", manifest=manifest)
    assert first["observed_state"] == "waiting_dependency"
    identity = (first["instance_id"], first["generation"])
    waiting_revision = state.load()["revision"]
    for offset in (10.0, 20.0, 30.0):
        waiting = coord.reconcile_one(
            "dependency-waiter", manifest=manifest,
            launch=lambda **kwargs: fake.start("dependency-waiter", **kwargs),
            now=time.time() + offset, retry_interval=0.01,
        )
        assert waiting["observed_state"] == "waiting_dependency"
        assert (waiting["instance_id"], waiting["generation"]) == identity
        assert fake.starts == []
    assert state.load()["revision"] == waiting_revision

    dependency.update({"available": True, "error": None})
    restored = coord.reconcile_one(
        "dependency-waiter", manifest=manifest,
        launch=lambda **kwargs: fake.start("dependency-waiter", **kwargs),
        now=time.time() + 40.0, retry_interval=0.01,
    )
    assert restored["observed_state"] == "running"
    assert (restored["instance_id"], restored["generation"]) == identity


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


def test_supervisor_stop_error_retains_generation_and_allocations(
        managed, monkeypatch):
    coord, fake, manager = managed
    app_id = "stuck-stop"
    claims = [{"name": "npu.rknn", "mode": "scheduled", "required": True}]
    started = coord.start(app_id, manifest=_manifest(app_id, claims))
    allocation_ids = list(started["allocations"])

    def failed_stop(failed_app_id, **_kwargs):
        fake.events.append(("supervisor.stop", failed_app_id))
        raise RuntimeError("trusted process group remains alive")

    monkeypatch.setattr(fake, "stop", failed_stop)

    with pytest.raises(RuntimeError, match="process group remains alive"):
        coord.stop(app_id)

    retained = state.get_app(app_id)
    assert retained["observed_state"] == "stopping"
    assert retained["teardown_pending"] is True
    assert retained["pid"] == started["pid"]
    assert retained["instance_id"] == started["instance_id"]
    assert retained["generation"] == started["generation"]
    assert retained["allocations"] == allocation_ids
    assert fake.running[app_id] == started["pid"]
    assert [item["allocation_id"] for item in
            manager.allocations_for(started["instance_id"])] == allocation_ids


def test_revoke_failure_keeps_generation_allocations_through_reconcile(
        managed, monkeypatch):
    coord, fake, manager = managed
    app_id = "revoke-fence"
    claims = [{"name": "npu.rknn", "mode": "scheduled", "required": True}]
    started = coord.start(app_id, manifest=_manifest(app_id, claims))
    allocation_ids = list(started["allocations"])
    registry = coord.inference_registry
    real_revoke = registry.revoke

    def failed_revoke(*_args, **_kwargs):
        raise OSError("authorization journal is unavailable")

    monkeypatch.setattr(registry, "revoke", failed_revoke)
    with pytest.raises(coordinator.CoordinatorError,
                       match="authorization revoke failed"):
        coord.stop(app_id)

    retained = state.get_app(app_id)
    assert fake.is_running(app_id) is None
    assert retained["observed_state"] == "stopping"
    assert retained["teardown_pending"] is True
    assert retained["instance_id"] == started["instance_id"]
    assert retained["generation"] == started["generation"]
    assert retained["allocations"] == allocation_ids
    assert coord.reconcile_allocations() == []
    assert [item["allocation_id"] for item in manager.allocations_for(
        started["instance_id"])] == allocation_ids

    monkeypatch.setattr(registry, "revoke", real_revoke)
    coord.stop(app_id)
    assert state.get_app(app_id)["teardown_pending"] is False
    assert manager.allocations_for(started["instance_id"]) == []


def test_start_process_fence_retains_generation_resources_and_reconcile_waits(
        managed):
    coord, fake, manager = managed
    app_id = "startup-fence"
    manifest = _manifest(app_id, [
        {"name": "npu.rknn", "mode": "scheduled", "required": True},
    ])
    failed_pid = 7333

    def launch(**kwargs):
        fake.running[app_id] = failed_pid
        kwargs["on_spawn"](failed_pid)
        # Model a leader exit whose same-boot helper remains in the committed
        # run.pgid.  is_running() is now false, but the identity is still the
        # only safe teardown/retry authority.
        fake.running.pop(app_id)
        fake.run_records.add(app_id)
        raise supervisor.ProcessFenceError(
            app_id, failed_pid, failed_pid,
            leader_alive=False, group_alive=True)

    with pytest.raises(supervisor.ProcessFenceError):
        coord.start(app_id, manifest=manifest, launch=launch)

    retained = state.get_app(app_id)
    allocation_ids = list(retained["allocations"])
    assert retained["observed_state"] == "stopping"
    assert retained["teardown_pending"] is True
    assert retained["pid"] == failed_pid
    assert retained["pgid"] == failed_pid
    assert retained["instance_id"]
    assert retained["generation"] > 0
    assert allocation_ids
    assert [item["allocation_id"] for item in manager.allocations_for(
        retained["instance_id"])] == allocation_ids

    observed = coord.observe(app_id, None)
    assert observed["observed_state"] == "stopping"
    assert observed["teardown_pending"] is True
    assert observed["allocations"] == allocation_ids

    reconciled = coord.reconcile_one(
        app_id, manifest=manifest,
        launch=lambda **_kwargs: pytest.fail("fenced app must not restart"))
    assert reconciled["action"] == "teardown_pending"
    assert state.get_app(app_id)["allocations"] == allocation_ids
    assert manager.allocations_for(retained["instance_id"])


def test_allocation_reconcile_treats_retained_run_record_as_live(managed):
    coord, fake, manager = managed
    app_id = "boot-residual-group"
    started = coord.start(app_id, manifest=_manifest(app_id))
    instance_id = started["instance_id"]
    allocation_ids = [item["allocation_id"] for item in
                      manager.allocations_for(instance_id)]
    assert allocation_ids

    # Model daemon restart after the leader exited but supervisor retained the
    # same-boot identity for a helper that survived containment.
    fake.running.pop(app_id)
    fake.run_records.add(app_id)
    # Reproduce the historical corruption observed on-device: a wait retry
    # replaced lifecycle state with instance B while the run record and bound
    # leases still belonged to instance A.  A live process fence must protect
    # every allocation for this app, not only state.instance_id.
    mismatched = state.begin_start(
        app_id, "incorrect-new-instance", version="1.0.0",
        launch_mode="managed")
    state.transition(app_id, "waiting_resource", allocations=[])
    assert mismatched["instance_id"] != instance_id
    assert coord.reconcile_allocations() == []
    assert [item["allocation_id"] for item in
            manager.allocations_for(instance_id)] == allocation_ids

    # Once a successful teardown retires that identity, ordinary startup
    # reconciliation may reclaim the now-stale reservations.
    fake.run_records.remove(app_id)
    assert set(coord.reconcile_allocations()) == set(allocation_ids)
    assert manager.allocations_for(instance_id) == []


def test_start_never_mints_over_a_retained_process_fence(managed):
    coord, fake, manager = managed
    app_id = "retained-fence"
    manifest = _manifest(app_id)
    started = coord.start(app_id, manifest=manifest)
    fake.running.pop(app_id)
    fake.run_records.add(app_id)
    before = state.get_app(app_id)
    allocations = manager.snapshot()["allocations"]

    with pytest.raises(coordinator.CoordinatorError,
                       match="process identity remains"):
        coord.start(app_id, manifest=manifest)

    after = state.get_app(app_id)
    assert after["instance_id"] == before["instance_id"] == started["instance_id"]
    assert after["generation"] == before["generation"] == started["generation"]
    assert manager.snapshot()["allocations"] == allocations
    assert len(fake.starts) == 1


def test_crash_observation_revokes_without_waiting_for_cooperative_stop(managed):
    coord, fake, manager = managed
    claims = [
        {"name": "npu.rknn", "mode": "scheduled", "required": True},
        {"name": "camera.frames", "mode": "shared", "required": True},
    ]
    started = coord.start("crashed", manifest=_manifest("crashed", claims))
    assert started["frame_stream_contract"]["kind"] == "frame.sock"
    fake.crash("crashed", code=137)

    # The server's global resource pass runs before per-app crash observation.
    # It must not release this generation until observe() has revoked the exact
    # inference authorization.
    assert coord.reconcile_allocations() == []
    assert [item["allocation_id"] for item in manager.allocations_for(
        started["instance_id"])] == started["allocations"]

    offset = len(fake.events)
    observed = coord.observe("crashed", None, fake.last_exit("crashed"))

    assert fake.events[offset:] == [("registry.revoke", started["pid"])]
    assert observed["observed_state"] == "failed"
    assert observed["frame_stream_contract"] == {"id": "", "kind": "none"}
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


def test_resolved_camera_profile_is_persisted_and_passed_to_launch(
        managed, monkeypatch):
    coord, fake, _manager = managed

    def profiled(app_id):
        manifest = _manifest(app_id)
        manifest["resources"] = {"profiles": [
            {"when": {"backend": "native"}, "claims": [
                {"name": "camera.frames", "mode": "shared", "required": True},
                {"name": "result.publish", "mode": "brokered", "required": True},
            ]},
            {"when": {"backend": "cpu"}, "claims": [
                {"name": "result.publish", "mode": "brokered", "required": True},
            ]},
        ]}
        return manifest

    monkeypatch.setattr(
        coordinator.appconfig, "effective_values",
        lambda _manifest_value, app_id: {
            "backend": "native" if app_id == "profile-native" else "cpu",
        })
    native = coord.start("profile-native", manifest=profiled("profile-native"))
    cpu = coord.start("profile-cpu", manifest=profiled("profile-cpu"))

    expected = {"id": "main", "kind": "frame.sock", "path": "/live/0"}
    assert native["frame_stream_contract"] == expected
    assert cpu["frame_stream_contract"] == {"id": "", "kind": "none"}
    assert fake.starts[0][1]["frame_stream_contract"] == expected
    assert fake.starts[1][1]["frame_stream_contract"] == {
        "id": "", "kind": "none"}
    assert state.get_app("profile-native")["frame_stream_contract"] == expected
    assert state.get_app("profile-cpu")["frame_stream_contract"] == {
        "id": "", "kind": "none"}

    stopped = coord.stop("profile-native")
    assert stopped["observed_state"] == "stopped"
    assert state.get_app("profile-native")["frame_stream_contract"] == {
        "id": "", "kind": "none"}


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

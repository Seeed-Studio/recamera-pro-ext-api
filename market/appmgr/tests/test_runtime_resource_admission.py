import json
import os
import sys

import pytest


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from appmgr import resources


def _manifest(app_id="budgeted", *, memory=320, storage=24, cpu=200):
    return {
        "manifest_version": 2,
        "id": app_id,
        "version": "1.0.0",
        "entry": "app.py",
        "instances": {"max": 1},
        "resources": {
            "claims": [
                {"name": "result.publish", "mode": "brokered", "required": True},
            ],
            "limits": {
                "memory_mb": memory,
                "storage_mb": storage,
                "cpu_percent": cpu,
                "shutdown_grace_sec": 5,
            },
        },
    }


def _healthy_probe():
    return {
        "mem_available_mb": 4096,
        "storage_free_mb": 16384,
        "temperature_c": 50.0,
    }


def test_manifest_limits_become_generation_bound_start_reservations(
        tmp_path, monkeypatch):
    monkeypatch.setenv("APPMGR_MANAGED_MEMORY_CAP_MB", "640")
    monkeypatch.setenv("APPMGR_MANAGED_STORAGE_CAP_MB", "100")
    monkeypatch.setenv("APPMGR_MANAGED_CPU_CAP_PERCENT", "0")
    plan = resources.plan_manifest(_manifest())
    requests = {item.resource: item for item in plan.requests}

    assert (requests["memory.managed-mb"].amount,
            requests["memory.managed-mb"].capacity) == (320, 640)
    assert (requests["storage.appdata-mb"].amount,
            requests["storage.appdata-mb"].capacity) == (24, 100)
    assert (requests["cpu.managed-percent"].amount,
            requests["cpu.managed-percent"].capacity) == (200, 0)

    manager = resources.ResourceManager(
        str(tmp_path / "resources.json"), runtime_probe=_healthy_probe)
    made = manager.reserve("budgeted", "instance-a", 1, plan)
    assert {item["resource"] for item in made} >= {
        "memory.managed-mb", "storage.appdata-mb", "cpu.managed-percent",
    }

    with pytest.raises(resources.ResourceBusy, match="memory.managed-mb"):
        manager.reserve(
            "second", "instance-b", 1,
            resources.plan_manifest(_manifest("second", memory=321)),
        )


def test_shared_budgets_default_to_live_capacity_not_manifest_sum(
        tmp_path, monkeypatch):
    monkeypatch.delenv("APPMGR_MANAGED_MEMORY_CAP_MB", raising=False)
    manager = resources.ResourceManager(
        str(tmp_path / "resources.json"), runtime_probe=_healthy_probe)

    manifests = [
        _manifest("face-analysis", memory=512, storage=96),
        _manifest("fall-detection", memory=384, storage=32),
        _manifest("ppocr-reader", memory=384, storage=32),
    ]
    plans = [resources.plan_manifest(manifest) for manifest in manifests]

    # 1,280 MiB exceeds the former built-in 1,024 MiB declared-budget cap,
    # but all three starts are valid while the live probe reports sufficient
    # memory. Existing processes are already reflected in MemAvailable.
    assert all(next(item for item in plan.requests
                    if item.resource == "memory.managed-mb").capacity == 0
               for plan in plans)
    assert all(next(item for item in plan.requests
                    if item.resource == "storage.appdata-mb").capacity == 8192
               for plan in plans)
    for generation, (manifest, plan) in enumerate(zip(manifests, plans), 1):
        manager.reserve(manifest["id"], "instance-%d" % generation,
                        generation, plan)

    memory = [item for item in manager.snapshot()["allocations"]
              if item["resource"] == "memory.managed-mb"]
    assert sum(item["amount"] for item in memory) == 1280


def test_camera_shared_default_matches_four_client_frame_export_capacity(
        tmp_path, monkeypatch):
    monkeypatch.delenv("APPMGR_FRAME_MANAGED_CAP", raising=False)
    manager = resources.ResourceManager(
        str(tmp_path / "resources.json"), runtime_probe=_healthy_probe)

    for generation in range(1, 5):
        manifest = _manifest("camera-%d" % generation, memory=64)
        manifest["resources"]["claims"].insert(0, {
            "name": "camera.frames", "mode": "shared", "required": True,
        })
        plan = resources.plan_manifest(manifest)
        camera = next(item for item in plan.requests
                      if item.resource == "camera.frame:camera-0")
        assert camera.capacity == 4
        manager.reserve(manifest["id"], "instance-%d" % generation,
                        generation, plan)

    fifth = _manifest("camera-5", memory=64)
    fifth["resources"]["claims"].insert(0, {
        "name": "camera.frames", "mode": "shared", "required": True,
    })
    with pytest.raises(resources.ResourceBusy) as caught:
        manager.reserve("camera-5", "instance-5", 5,
                        resources.plan_manifest(fifth))
    assert caught.value.resource == "camera.frame:camera-0"
    assert caught.value.owners == ["camera-1", "camera-2", "camera-3", "camera-4"]
    assert "4 reserved + 1 requested" in str(caught.value)


def test_exclusive_camera_still_conflicts_with_a_shared_owner(
        tmp_path, monkeypatch):
    monkeypatch.delenv("APPMGR_FRAME_MANAGED_CAP", raising=False)
    manager = resources.ResourceManager(
        str(tmp_path / "resources.json"), runtime_probe=_healthy_probe)
    shared = _manifest("shared-camera", memory=64)
    shared["resources"]["claims"].insert(0, {
        "name": "camera.frames", "mode": "shared", "required": True,
    })
    manager.reserve("shared-camera", "instance-shared", 1,
                    resources.plan_manifest(shared))

    exclusive = _manifest("exclusive-camera", memory=64)
    exclusive["resources"]["claims"].insert(0, {
        "name": "camera.frames", "mode": "exclusive", "required": True,
    })
    with pytest.raises(resources.ResourceBusy) as caught:
        manager.reserve("exclusive-camera", "instance-exclusive", 1,
                        resources.plan_manifest(exclusive))

    assert caught.value.resource == "camera.frame:camera-0"
    assert caught.value.owners == ["shared-camera"]


def test_explicit_shared_cap_reports_capacity_math(tmp_path, monkeypatch):
    monkeypatch.setenv("APPMGR_MANAGED_MEMORY_CAP_MB", "640")
    manager = resources.ResourceManager(
        str(tmp_path / "resources.json"), runtime_probe=_healthy_probe)
    manager.reserve("first", "instance-a", 1,
                    resources.plan_manifest(_manifest("first", memory=320)))

    with pytest.raises(resources.ResourceBusy) as caught:
        manager.reserve(
            "second", "instance-b", 1,
            resources.plan_manifest(_manifest("second", memory=321)),
        )

    assert caught.value.resource == "memory.managed-mb"
    assert caught.value.owners == ["first"]
    assert "shared capacity 640 would be exceeded" in str(caught.value)
    assert "320 reserved + 321 requested" in str(caught.value)


@pytest.mark.parametrize(
    ("environment", "manifest_kwargs", "resource_name"),
    [
        ({"APPMGR_MANAGED_MEMORY_CAP_MB": "319"},
         {"memory": 320}, "memory.managed-mb"),
        ({"APPMGR_MANAGED_STORAGE_CAP_MB": "23"},
         {"storage": 24}, "storage.appdata-mb"),
        ({"APPMGR_MANAGED_CPU_CAP_PERCENT": "199"},
         {"cpu": 200}, "cpu.managed-percent"),
    ],
)
def test_first_request_cannot_exceed_aggregate_platform_cap(
        tmp_path, monkeypatch, environment, manifest_kwargs, resource_name):
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    manager = resources.ResourceManager(
        str(tmp_path / "resources.json"), runtime_probe=_healthy_probe)

    with pytest.raises(resources.ResourceBusy) as caught:
        manager.reserve(
            "oversized", "instance-a", 1,
            resources.plan_manifest(_manifest("oversized", **manifest_kwargs)),
        )

    assert caught.value.resource == resource_name
    assert "exceeds capacity" in str(caught.value)
    assert not (tmp_path / "resources.json").exists()


@pytest.mark.parametrize(
    ("sample", "resource_name"),
    [
        ({"mem_available_mb": 575, "storage_free_mb": 9999,
          "temperature_c": 50.0}, "memory.available-mb"),
        ({"mem_available_mb": 4096, "storage_free_mb": 151,
          "temperature_c": 50.0}, "storage.available-mb"),
        ({"mem_available_mb": 4096, "storage_free_mb": 9999,
          "temperature_c": 100.0}, "thermal.start"),
    ],
)
def test_live_capacity_is_checked_only_when_reserving_for_start(
        tmp_path, monkeypatch, sample, resource_name):
    monkeypatch.setenv("APPMGR_SYSTEM_MEMORY_HEADROOM_MB", "256")
    monkeypatch.setenv("APPMGR_STORAGE_HEADROOM_MB", "128")
    monkeypatch.delenv("APPMGR_START_MAX_TEMP_C", raising=False)
    monkeypatch.delenv("APPMGR_RUNTIME_HARD_TEMP_C", raising=False)
    plan = resources.plan_manifest(_manifest())
    manager = resources.ResourceManager(
        str(tmp_path / "resources.json"), runtime_probe=lambda: sample)

    # Planning/inspection is deliberately side-effect free and succeeds even
    # while live capacity is unavailable.  Only start-time reserve is blocked.
    assert plan.requests
    assert not (tmp_path / "resources.json").exists()
    with pytest.raises(resources.ResourceBusy) as caught:
        manager.reserve("budgeted", "instance-a", 1, plan)
    assert caught.value.resource == resource_name
    assert not (tmp_path / "resources.json").exists()


@pytest.mark.parametrize(
    ("sample", "resource_name"),
    [
        ({"storage_free_mb": 9999, "temperature_c": 50.0},
         "memory.telemetry"),
        ({"mem_available_mb": "not-a-number", "storage_free_mb": 9999,
          "temperature_c": 50.0}, "memory.telemetry"),
        ({"mem_available_mb": float("inf"), "storage_free_mb": 9999,
          "temperature_c": 50.0}, "memory.telemetry"),
        ({"mem_available_mb": 4096, "temperature_c": 50.0},
         "storage.telemetry"),
    ],
)
def test_missing_or_invalid_live_capacity_fails_closed_at_start_only(
        tmp_path, sample, resource_name):
    plan = resources.plan_manifest(_manifest())
    manager = resources.ResourceManager(
        str(tmp_path / "resources.json"), runtime_probe=lambda: sample)

    assert plan.requests
    assert not (tmp_path / "resources.json").exists()
    with pytest.raises(resources.ResourceBusy) as caught:
        manager.reserve("budgeted", "instance-a", 1, plan)

    assert caught.value.resource == resource_name
    assert "cannot prove sufficient live" in str(caught.value)
    assert not (tmp_path / "resources.json").exists()


def test_idempotent_exact_generation_does_not_fail_if_runtime_later_heats_up(
        tmp_path):
    sample = {"mem_available_mb": 4096, "storage_free_mb": 9999,
              "temperature_c": 50.0}
    manager = resources.ResourceManager(
        str(tmp_path / "resources.json"), runtime_probe=lambda: dict(sample))
    plan = resources.plan_manifest(_manifest())
    first = manager.reserve("budgeted", "same-instance", 7, plan)

    sample["temperature_c"] = 111.0
    again = manager.reserve("budgeted", "same-instance", 7, plan)
    assert again == first
    with open(tmp_path / "resources.json") as handle:
        assert len(json.load(handle)["allocations"]) == len(first)


def test_runtime_status_reports_both_thermal_thresholds(tmp_path, monkeypatch):
    monkeypatch.setenv("APPMGR_START_MAX_TEMP_C", "76.5")
    monkeypatch.setenv("APPMGR_RUNTIME_HARD_TEMP_C", "84.25")
    manager = resources.ResourceManager(
        str(tmp_path / "resources.json"), runtime_probe=_healthy_probe)

    status = manager.runtime_status()

    assert status["sample"]["temperature_c"] == 50.0
    assert status["policy"]["start_max_temp_c"] == 76.5
    assert status["policy"]["runtime_hard_temp_c"] == 84.25


def test_runtime_status_reports_new_default_thermal_thresholds(
        tmp_path, monkeypatch):
    monkeypatch.delenv("APPMGR_START_MAX_TEMP_C", raising=False)
    monkeypatch.delenv("APPMGR_RUNTIME_HARD_TEMP_C", raising=False)
    manager = resources.ResourceManager(
        str(tmp_path / "resources.json"), runtime_probe=_healthy_probe)

    policy = manager.runtime_status()["policy"]

    assert policy["start_max_temp_c"] == 100.0
    assert policy["runtime_hard_temp_c"] == 110.0


def test_default_start_thermal_boundary_allows_below_100(tmp_path, monkeypatch):
    monkeypatch.delenv("APPMGR_START_MAX_TEMP_C", raising=False)
    monkeypatch.delenv("APPMGR_RUNTIME_HARD_TEMP_C", raising=False)
    sample = {"mem_available_mb": 4096, "storage_free_mb": 9999,
              "temperature_c": 99.9}
    manager = resources.ResourceManager(
        str(tmp_path / "resources.json"), runtime_probe=lambda: sample)

    made = manager.reserve(
        "below-start-limit", "instance-a", 1,
        resources.plan_manifest(_manifest("below-start-limit")),
    )

    assert made


@pytest.mark.parametrize(
    ("temperature", "violated"),
    [(109.9, False), (110.0, True)],
)
def test_default_runtime_hard_thermal_boundary(
        tmp_path, monkeypatch, temperature, violated):
    monkeypatch.delenv("APPMGR_START_MAX_TEMP_C", raising=False)
    monkeypatch.delenv("APPMGR_RUNTIME_HARD_TEMP_C", raising=False)
    sample = {"mem_available_mb": 4096, "storage_free_mb": 9999,
              "temperature_c": temperature}
    manager = resources.ResourceManager(
        str(tmp_path / "resources.json"), runtime_probe=lambda: sample)

    violation = manager.runtime_guard()

    assert (violation is not None) is violated
    if violation is not None:
        assert violation["limit_c"] == 110.0


@pytest.mark.parametrize(
    ("start", "hard", "expected_start", "expected_hard"),
    [
        ("not-a-number", "110", 100.0, 110.0),
        ("nan", "110", 100.0, 110.0),
        ("100", "inf", 100.0, 110.0),
        ("110", "110", 100.0, 110.0),
        ("111", "110", 100.0, 110.0),
        ("105", "100", 90.0, 100.0),
    ],
)
def test_invalid_thermal_policy_preserves_a_safe_order(
        tmp_path, monkeypatch, start, hard, expected_start, expected_hard):
    monkeypatch.setenv("APPMGR_START_MAX_TEMP_C", start)
    monkeypatch.setenv("APPMGR_RUNTIME_HARD_TEMP_C", hard)
    manager = resources.ResourceManager(
        str(tmp_path / "resources.json"), runtime_probe=_healthy_probe)

    policy = manager.runtime_status()["policy"]

    assert policy["start_max_temp_c"] == expected_start
    assert policy["runtime_hard_temp_c"] == expected_hard


def test_invalid_thermal_override_cannot_disable_runtime_hard_stop(
        tmp_path, monkeypatch):
    monkeypatch.setenv("APPMGR_START_MAX_TEMP_C", "100")
    monkeypatch.setenv("APPMGR_RUNTIME_HARD_TEMP_C", "nan")
    sample = {"mem_available_mb": 4096, "storage_free_mb": 9999,
              "temperature_c": 110.0}
    manager = resources.ResourceManager(
        str(tmp_path / "resources.json"), runtime_probe=lambda: sample)

    violation = manager.runtime_guard()

    assert violation["resource"] == "thermal.runtime"
    assert violation["limit_c"] == 110.0


def test_runtime_status_reports_memory_uncapped_but_storage_reserved_by_default(
        tmp_path, monkeypatch):
    monkeypatch.delenv("APPMGR_MANAGED_MEMORY_CAP_MB", raising=False)
    monkeypatch.delenv("APPMGR_MANAGED_STORAGE_CAP_MB", raising=False)
    manager = resources.ResourceManager(
        str(tmp_path / "resources.json"), runtime_probe=_healthy_probe)

    policy = manager.runtime_status()["policy"]

    assert policy["managed_memory_cap_mb"] == 0
    assert policy["managed_storage_cap_mb"] == 8192


def test_explicit_zero_disables_storage_declared_budget_cap(
        tmp_path, monkeypatch):
    monkeypatch.setenv("APPMGR_MANAGED_STORAGE_CAP_MB", "0")
    manager = resources.ResourceManager(
        str(tmp_path / "resources.json"), runtime_probe=_healthy_probe)

    first = resources.plan_manifest(_manifest("storage-a", storage=6000))
    second = resources.plan_manifest(_manifest("storage-b", storage=6000))
    assert next(item for item in first.requests
                if item.resource == "storage.appdata-mb").capacity == 0

    manager.reserve("storage-a", "instance-a", 1, first)
    manager.reserve("storage-b", "instance-b", 1, second)

    assert manager.runtime_status()["policy"]["managed_storage_cap_mb"] == 0

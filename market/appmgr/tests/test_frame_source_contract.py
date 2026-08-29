"""Managed frame-source policy and Result-Hub stream contract tests."""
from __future__ import annotations

import os
import sys


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from appmgr import resources, supervisor  # noqa: E402


def _manifest(*claims):
    return {"resources": {"claims": list(claims)}}


def _plan(manifest, config=None):
    value = dict(manifest)
    value.setdefault("manifest_version", 2)
    value.setdefault("id", "frame-policy-test")
    value.setdefault("instances", {"max": 1})
    return resources.plan_manifest(value, config or {}).as_dict()


def test_admitted_camera_plan_has_one_native_main_stream_contract():
    manifest = _manifest(
        {"name": "npu.rknn", "mode": "scheduled", "required": True},
        {"name": "camera.frames", "mode": "shared", "required": True},
    )
    assert supervisor.managed_frame_stream_contract(_plan(manifest)) == {
        "id": "main", "kind": "frame.sock", "path": "/live/0",
    }


def test_missing_or_malformed_resource_plan_fails_closed_to_none():
    expected = {"id": "", "kind": "none"}
    values = [
        None,
        {},
        {"requests": None},
        {"requests": "camera.frame:camera-0"},
        {"requests": [{"resource": "camera.frame:camera-1"}]},
        {"requests": ["camera.frame:camera-0"]},
    ]
    for plan in values:
        assert supervisor.managed_frame_stream_contract(plan) == expected


def test_contract_returns_fresh_mapping():
    plan = _plan(_manifest({"name": "camera.frames"}))
    first = supervisor.managed_frame_stream_contract(plan)
    first["kind"] = "tampered"
    assert supervisor.managed_frame_stream_contract(plan) == {
        "id": "main", "kind": "frame.sock", "path": "/live/0",
    }


def test_effective_resource_profile_controls_frame_contract():
    manifest = {
        "resources": {"profiles": [
            {"when": {"backend": "native"}, "claims": [
                {"name": "camera.frames", "mode": "shared", "required": True},
            ]},
            {"when": {"backend": "cpu"}, "claims": []},
        ]},
    }
    native = supervisor.managed_frame_stream_contract(
        _plan(manifest, {"backend": "native"}))
    cpu = supervisor.managed_frame_stream_contract(
        _plan(manifest, {"backend": "cpu"}))
    assert native == {"id": "main", "kind": "frame.sock", "path": "/live/0"}
    assert cpu == {"id": "", "kind": "none"}


def test_manifest_claim_alone_never_mints_child_frame_route(monkeypatch):
    monkeypatch.delenv("RECAMERA_FRAME_SOURCE", raising=False)
    env = supervisor._build_env(
        "vision-app", _manifest({"name": "camera.frames"}))
    assert "RECAMERA_FRAME_SOURCE" not in env


def test_build_env_mints_frame_only_opt_in_for_camera_claim(monkeypatch):
    # A stale/manual parent policy must never make a managed app bypass the
    # authenticated Result Gateway when appmgr opts only its frame source in.
    monkeypatch.setenv("RECAMERA_ADAPTER_PREFER", "official")
    monkeypatch.setenv("RECAMERA_RESULT_OSD", "1")
    monkeypatch.setenv("RECAMERA_FRAME_SOURCE", "workaround")
    monkeypatch.setenv("RECAMERA_FRAME_SOCK", "/tmp/untrusted-frame.sock")
    manifest = _manifest({"name": "camera.frames"})
    contract = supervisor.managed_frame_stream_contract(_plan(manifest))

    env = supervisor._build_env(
        "vision-app", manifest,
        instance_id="instance", instance_generation=4,
        result_gateway_sock="/run/recamera/appmgr-results.sock",
        frame_stream_contract=contract)

    assert env["RECAMERA_FRAME_SOURCE"] == "official"
    assert env["RECAMERA_FRAME_SOCK"] == "/run/recamera/frame.sock"
    assert "RECAMERA_ADAPTER_PREFER" not in env
    assert "RECAMERA_RESULT_OSD" not in env
    assert env["RECAMERA_RESULT_GATEWAY_SOCK"] == \
        "/run/recamera/appmgr-results.sock"
    assert env["RECAMERA_RESULT_GATEWAY_REQUIRED"] == "1"


def test_build_env_clears_inherited_frame_policy_without_claim(monkeypatch):
    monkeypatch.setenv("RECAMERA_ADAPTER_PREFER", "official")
    monkeypatch.setenv("RECAMERA_RESULT_OSD", "1")
    monkeypatch.setenv("RECAMERA_FRAME_SOURCE", "official")
    monkeypatch.setenv("RECAMERA_FRAME_SOCK", "/tmp/untrusted-frame.sock")

    env = supervisor._build_env(
        "voice-app", _manifest({"name": "audio.capture"}))

    assert "RECAMERA_FRAME_SOURCE" not in env
    assert "RECAMERA_FRAME_SOCK" not in env
    assert "RECAMERA_ADAPTER_PREFER" not in env
    assert "RECAMERA_RESULT_OSD" not in env

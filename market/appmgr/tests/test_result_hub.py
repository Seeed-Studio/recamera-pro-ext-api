from __future__ import annotations

import base64
import copy
import http.client
import json
import os
import socket
import struct
import sys
import threading
import time
from contextlib import contextmanager, nullcontext
from http.server import ThreadingHTTPServer

import pytest


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from appmgr import (config as appconfig, paths,
                    result_hub as result_hub_module, server,
                    visualization as appvisualization)  # noqa: E402
from appmgr.result_hub import (  # noqa: E402
    DATA_TYPES,
    SCHEMA,
    SCHEMA_VERSION,
    SYSTEM_PROTOCOL,
    ResultHub,
    ResultViewFormatter,
    _HubClient,
    _HubWebSocketServer,
    _Subscription,
    normalize_app_payload,
    normalize_system_payload,
    resolve_builtin_notify_identity,
)


class NoopFormatter:
    def format(self, _raw):
        return []


class EchoFormatter:
    def format(self, raw):
        value = dict(raw)
        value["type"] = "formatted"
        value["id"] = raw["id"] + ":formatted:0"
        value["results"] = []
        value["events"] = []
        value["extensions"] = {
            "raw_id": raw["id"], "raw_type": raw["type"],
            "batch_id": (raw.get("extensions") or {}).get("batch_id"),
            "batch_types": (raw.get("extensions") or {}).get("batch_types"),
            "projection": "authenticated_ingress_batch",
        }
        delivery = (raw.get("extensions") or {}).get("delivery")
        if delivery:
            value["extensions"]["delivery"] = delivery
        value["payload"] = "formatted:" + raw["id"]
        value["content_type"] = "text/plain; charset=utf-8"
        value["profile"] = "test"
        return [value]


class BlockingEchoFormatter(EchoFormatter):
    def __init__(self):
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()

    def format(self, raw):
        self.calls += 1
        self.started.set()
        assert self.release.wait(3)
        return super().format(raw)


def _identity(app="demo", instance="instance-1", generation=1):
    return {"app_id": app, "instance_id": instance,
            "generation": generation, "pid": os.getpid()}


def _manifest(app="demo", *, fields=None, camera=True, render=None):
    claims = ([{"name": "camera.frames", "mode": "shared", "required": True}]
              if camera else [])
    return {
        "manifest_version": 2,
        "id": app,
        "resources": {"claims": claims},
        "output": {"fields": list(fields or [])},
        "render": dict(render or {}),
    }


def _managed_stream(camera=True):
    return ({"id": "main", "kind": "frame.sock", "path": "/live/0"}
            if camera else None)


def _authorize(hub, identity, *, fields=None, camera=True, render=None):
    assert hub.refresh_app_manifest(
        identity, _manifest(identity["app_id"], fields=fields,
                            camera=camera, render=render),
        stream_contract=_managed_stream(camera))
    return identity


def _app_payload(*, seq=1, results=None, events=None, summary=None,
                 width=640, height=480, **extra):
    value = {
        "type": "results", "app": "spoof", "instance": "spoof",
        "generation": 999, "seq": seq, "pts": 1.25,
        "frame": {"width": width, "height": height},
        "results": list(results or []), "events": list(events or []),
    }
    if summary is not None:
        value["summary"] = summary
    value.update(extra)
    return value


def _system_payload(*, task="detection", seq=None):
    value = {
        "task_type": 1,
        "task_type_name": task,
        "timestamp_ms": 1_800_000_000_000,
        "timestamp": "2027-01-15T08:00:00",
        "model_id": 7,
        "source_id": "spoofed-external",
        "pts_us": 123456,
    }
    if seq is not None:
        value["seq"] = seq
    if task == "classification":
        value[task] = {"count": 1, "entries": [{
            "class_id": 2, "class_name": "cat", "score": 0.8,
            "box": {"left": 0.1, "top": 0.2, "right": 0.4, "bottom": 0.5},
        }]}
    else:
        value[task] = {"count": 1, "entries": [{
            "class_id": 0, "class_name": "person", "score": 0.9,
            "box": {"left": 0.1, "top": 0.2, "right": 0.4, "bottom": 0.5},
        }]}
    return value


def test_app_adapter_overwrites_identity_splits_events_and_states_shape_spaces():
    payload = _app_payload(
        seq=9,
        results=[{
            "box": [1, 2, 30, 40],
            "keypoints": [[2, 3, 0.9]],
            "label": "person",
            "space": "normalized_xyxy",
            "spaces": {"box": "normalized_xyxy",
                       "keypoints": "normalized_xyxy"},
        }],
        events=[{"kind": "fall", "event_id": 17}],
        summary={"state": "alarm"},
    )
    envelopes = normalize_app_payload(
        payload, _identity(generation=4),
        trusted_geometry={
            "results": {"box": "pixel_xyxy", "keypoints": "pixel_points"},
            "events": {},
        },
        trusted_stream={"id": "sub", "kind": "go2rtc", "path": "/live/1"})
    by_type = {value["type"]: value for value in envelopes}
    assert set(by_type) == {"frame", "event", "status"}

    frame = by_type["frame"]
    assert frame["schema"] == SCHEMA and frame["schema_version"] == SCHEMA_VERSION
    assert frame["source"] == {
        "kind": "app", "id": "demo", "app_id": "demo",
        "instance": "instance-1", "generation": 4, "trust": "peercred",
    }
    assert frame["stream"] == {
        "id": "sub", "width": 640, "height": 480,
        "coordinate_space": "pixel_xyxy",
    }
    shape = frame["results"][0]
    assert shape["box"] == [1, 2, 30, 40]
    assert shape["spaces"] == {
        "box": "pixel_xyxy", "keypoints": "pixel_points"}
    assert "space" not in shape                 # mixed shapes are never mislabeled

    event = by_type["event"]["events"][0]
    assert event["producer_event_id"] == 17
    assert event["event_id"] == "evt:demo:4:17"
    assert by_type["event"]["id"] == event["event_id"]
    assert by_type["event"]["extensions"]["delivery"] == "edge"
    assert by_type["event"]["extensions"]["event_kind"] == "fall"
    assert by_type["status"]["summary"] == {"state": "alarm"}


def test_app_geometry_is_unknown_without_reference_dimensions():
    envelopes = normalize_app_payload(
        _app_payload(results=[{"quad": [[1, 2], [3, 4], [5, 6], [7, 8]]}],
                     width=None, height=None),
        _identity(), trusted_geometry={
            "results": {"quad": "pixel_quad"}, "events": {}},
        trusted_stream={"id": "sub"})
    frame = next(item for item in envelopes if item["type"] == "frame")
    assert frame["stream"]["coordinate_space"] == "unknown"
    assert frame["results"][0]["space"] == "unknown"
    assert frame["results"][0]["spaces"]["quad"] == "unknown"


def test_canonical_geometry_uses_manifest_space_and_bounded_render_policy():
    payload = _app_payload(
        results=[], events=[],
        geometry=[
            {"type": "point", "points": [[0.25, 0.75]],
             "space": "pixel_points", "id": "nose",
             "style": {"color": "#00FF00", "point_radius": 5,
                       "filter": "url(javascript:bad)"}},
            {"type": "line", "points": [[0.1, 0.1], [0.9, 0.9]],
             "style": {"opacity": 2}},
            {"type": "polygon", "points": [[0, 0], [1, 0], [1, 1]]},
            {"type": "point", "points": [[2, 2]]},
        ],
        render={"geometry": {"types": ["polygon"]}},
        stream_id="spoofed-substream",
    )
    envelopes = normalize_app_payload(
        payload, _identity(generation=4),
        trusted_render={
            "schema_version": 1,
            "geometry": {"types": ["point", "line"], "max_items": 8,
                         "max_points": 8,
                         "style": {"line_width": 2, "opacity": 0.8}},
        },
        trusted_geometry={
            "results": {}, "events": {},
            "primitives": {
                "space": "normalized_points", "types": ["point", "line"],
                "max_items": 8, "max_points": 8,
                "style": {"line_width": 2, "opacity": 0.8},
            },
        },
        trusted_stream={"id": "main", "kind": "frame.sock", "path": "/live/0"},
    )
    assert len(envelopes) == 1
    frame = envelopes[0]
    assert frame["type"] == "frame"
    assert frame["source"]["generation"] == 4
    assert frame["stream"]["id"] == "main"
    assert frame["stream"]["coordinate_space"] == "normalized_xyxy"
    assert frame["render"]["geometry"]["types"] == ["point", "line"]
    assert frame["geometry"] == [
        {"type": "point", "points": [[0.25, 0.75]],
         "space": "normalized_points", "id": "nose",
         "style": {"line_width": 2.0, "opacity": 0.8,
                   "color": "#00ff00", "point_radius": 5.0}},
        {"type": "line", "points": [[0.1, 0.1], [0.9, 0.9]],
         "space": "normalized_points",
         "style": {"line_width": 2.0, "opacity": 0.8}},
    ]
    assert "geometry" not in (frame["extensions"].get("payload") or {})


def test_shared_geometry_fixture_is_exact_hub_sanitizer_output(monkeypatch):
    """The React overlay suite reads this same file; keep one wire oracle."""
    monkeypatch.setattr(result_hub_module.time, "time", lambda: 1_780_000_000.0)
    render = {
        "schema_version": 1,
        "geometry": {
            "types": ["point", "line", "polyline", "polygon"],
            "max_items": 64, "max_points": 128,
            "style": {"line_width": 2, "opacity": 0.8},
        },
    }
    trusted_geometry = {
        "results": {}, "events": {},
        "primitives": {
            "space": "normalized_points",
            "types": ["point", "line", "polyline", "polygon"],
            "max_items": 64, "max_points": 128,
            "style": {"line_width": 2, "opacity": 0.8},
        },
    }
    payload = _app_payload(
        seq=7, pts=1.234567, gateway_ts=1_780_000_000.0,
        stream_id="spoofed", render={"geometry": {"types": ["polygon"]}},
        geometry=[
            {"type": "point", "points": [[0.25, 0.75]],
             "space": "pixel_points", "id": "nose", "label": "nose",
             "score": 0.96,
             "style": {"color": "#00FF00", "point_radius": 5,
                       "filter": "url(javascript:bad)"}},
            {"type": "line", "points": [[0.1, 0.1], [0.9, 0.9]]},
            {"type": "polyline",
             "points": [[0.1, 0.2], [0.3, 0.4], [0.6, 0.2]]},
            {"type": "polygon",
             "points": [[0.1, 0.1], [0.3, 0.1], [0.3, 0.3]],
             "style": {"opacity": 0.5, "fill": True,
                       "fill_color": "#0088FF80"}},
        ],
    )
    actual = normalize_app_payload(
        payload, _identity("demo", "i-4", 4), trusted_render=render,
        trusted_geometry=trusted_geometry,
        trusted_stream={"id": "main", "kind": "frame.sock", "path": "/live/0"},
    )
    fixture_path = os.path.join(
        os.path.dirname(__file__), "fixtures", "geometry-v2-canonical.json")
    with open(fixture_path, encoding="utf-8") as source:
        expected = json.load(source)
    assert actual == [expected]


def test_geometry_fails_closed_without_installed_manifest_policy():
    envelopes = normalize_app_payload(
        _app_payload(results=[], geometry=[
            {"type": "point", "points": [[1, 2]], "space": "pixel_points"}]),
        _identity(), trusted_geometry={"results": {}, "events": {}},
        trusted_stream={"id": "main", "kind": "frame.sock", "path": "/live/0"})
    frame = next(value for value in envelopes if value["type"] == "frame")
    assert frame["geometry"] == []


def test_geometry_compiler_requires_strict_output_and_render_versions():
    field = {
        "name": "geometry", "from": "geometry[]", "type": "geometry[]",
        "coord": "normalized_points",
    }
    policy = {"types": ["point"], "max_items": 4, "max_points": 2}
    loose_output = {
        "output": {"fields": [field]},
        "render": {"schema_version": 1, "geometry": policy},
    }
    loose_render = {
        "output": {"contract_version": 2, "fields": [field]},
        "render": {"geometry": policy},
    }
    boolean_render = {
        "output": {"contract_version": 2, "fields": [field]},
        "render": {"schema_version": True, "geometry": policy},
    }
    strict = {
        "output": {"contract_version": 2, "fields": [field]},
        "render": {"schema_version": 1, "geometry": policy},
    }
    derived = copy.deepcopy(strict)
    derived["output"]["fields"][0]["derived"] = True

    assert result_hub_module._compile_geometry_contract(loose_output)["primitives"] == {}
    assert result_hub_module._compile_geometry_contract(loose_render)["primitives"] == {}
    assert result_hub_module._compile_geometry_contract(boolean_render)["primitives"] == {}
    assert result_hub_module._compile_geometry_contract(derived)["primitives"] == {}
    assert result_hub_module._compile_geometry_contract(strict)["primitives"] == {
        "space": "normalized_points", "types": ["point"],
        "max_items": 4, "max_points": 2, "style": {},
    }


def test_manifest_geometry_and_controlled_launch_stream_are_trusted(tmp_path):
    hub = ResultHub(ws_port=0, system_uds_path=str(tmp_path / "system.sock"),
                    formatter=NoopFormatter())
    identity = _authorize(hub, _identity("normalized-app"), fields=[
        {"from": "results[].box", "coord": "normalized_xyxy"},
        {"from": "events[kind=detection].box",
         "coord": "normalized_xyxy", "event_kind": "detection"},
    ])
    published = hub.publish_app(_app_payload(
        results=[{"box": [0.1, 0.2, 0.4, 0.5],
                  "spaces": {"box": "pixel_xyxy"}}],
        events=[{"kind": "detection", "box": [0.1, 0.2, 0.4, 0.5]}],
        stream_id="camera-0"),
        identity)
    frame = next(value for value in published if value["type"] == "frame")
    event = next(value for value in published if value["type"] == "event")
    assert frame["stream"]["id"] == "main"
    assert frame["stream"]["coordinate_space"] == "normalized_xyxy"
    assert frame["extensions"]["reported_stream_id"] == "camera-0"
    assert frame["extensions"]["stream_source"] == {
        "id": "main", "kind": "frame.sock", "path": "/live/0"}
    assert frame["results"][0]["spaces"]["box"] == "normalized_xyxy"
    assert event["events"][0]["spaces"]["box"] == "normalized_xyxy"


def test_camera_permission_alone_cannot_claim_main_preview(tmp_path):
    hub = ResultHub(ws_port=0, system_uds_path=str(tmp_path / "system.sock"),
                    formatter=NoopFormatter())
    identity = _identity("permission-only")
    # camera.frames is an authorization declaration, not evidence that this
    # exact process generation opened the managed official frame source.
    assert hub.refresh_app_manifest(identity, _manifest("permission-only"))
    frame = hub.publish_app(_app_payload(
        results=[{"box": [1, 2, 3, 4]}], stream_id="main"), identity)[0]
    assert frame["stream"]["id"] == ""
    assert frame["extensions"]["reported_stream_id"] == "main"
    assert frame["extensions"]["stream_source"] == {
        "id": "", "kind": "none"}


def test_launch_stream_contract_is_strictly_whitelisted_and_generation_bound(
        tmp_path):
    hub = ResultHub(ws_port=0, system_uds_path=str(tmp_path / "system.sock"),
                    formatter=NoopFormatter())
    manifest = _manifest("stream-app", fields=[
        {"from": "results[].box", "coord": "pixel_xyxy"}])
    first = _identity("stream-app", "official", 1)
    contract = _managed_stream()
    assert hub.refresh_app_manifest(first, manifest, stream_contract=contract)
    contract["id"] = "mutated-after-refresh"
    frame = hub.publish_app(_app_payload(
        results=[{"box": [1, 2, 3, 4]}]), first)[0]
    assert frame["stream"]["id"] == "main"
    assert frame["extensions"]["stream_source"] == _managed_stream()

    invalid = [
        {"id": "main", "kind": "frame.sock"},
        {"id": "main", "kind": "frame.sock", "path": "/live/1"},
        {"id": "sub", "kind": "frame.sock", "path": "/live/0"},
        {"id": "main", "kind": "ffmpeg", "path": "/live/0"},
        {"id": "main", "kind": "frame.sock", "path": "/live/0",
         "extra": True},
        "main",
    ]
    for offset, candidate in enumerate(invalid, start=2):
        current = _identity("stream-app", "generation-%s" % offset, offset)
        assert hub.refresh_app_manifest(
            current, manifest, stream_contract=candidate)
        frame = hub.publish_app(_app_payload(
            seq=offset, results=[{"box": [1, 2, 3, 4]}]), current)[0]
        assert frame["stream"]["id"] == ""
        assert frame["extensions"]["stream_source"] == {
            "id": "", "kind": "none"}

    # The replacement generation cannot inherit the preceding generation's
    # main association when its launch supplied no contract.
    replacement = _identity("stream-app", "no-stream", 20)
    assert hub.refresh_app_manifest(replacement, manifest)
    assert hub.publish_app(_app_payload(seq=20), first) == []
    frame = hub.publish_app(_app_payload(
        seq=20, results=[{"box": [1, 2, 3, 4]}]), replacement)[0]
    assert frame["stream"]["id"] == ""


def test_untrusted_geometry_aliases_and_conflicting_manifest_fail_closed(tmp_path):
    direct = normalize_app_payload(_app_payload(results=[{
        "normalized_xyxy": [0.1, 0.2, 0.4, 0.5],
        "space": "normalized_xyxy",
    }]), _identity())[0]
    assert direct["results"][0]["spaces"]["normalized_xyxy"] == "unknown"
    assert direct["results"][0]["space"] == "unknown"
    assert direct["stream"]["coordinate_space"] == "unknown"

    hub = ResultHub(ws_port=0, system_uds_path=str(tmp_path / "system.sock"),
                    formatter=NoopFormatter())
    identity = _authorize(hub, _identity("conflict-app"), fields=[
        {"from": "results[].box", "coord": "pixel_xyxy"},
        {"from": "results[].box", "coord": "normalized_xyxy"},
    ])
    frame = hub.publish_app(
        _app_payload(results=[{"box": [1, 2, 3, 4]}]), identity)[0]
    assert frame["results"][0]["spaces"]["box"] == "unknown"
    assert frame["stream"]["coordinate_space"] == "unknown"


def test_app_render_is_only_generation_bound_installed_manifest_data(tmp_path):
    identity = _identity("render-app", "instance-a", 7)
    malicious = _app_payload(
        results=[{"box": [1, 2, 3, 4]}],
        render={"boxes": {"label": "payload-owned"}, "script": "evil"})

    # The standalone adapter has no trusted control-plane declaration, so the
    # application-supplied rendering contract is discarded completely.
    direct = normalize_app_payload(malicious, identity)[0]
    assert direct["render"] == {}

    hub = ResultHub(ws_port=0, system_uds_path=str(tmp_path / "system.sock"),
                    formatter=EchoFormatter())
    trusted = {
        "schema_version": 1,
        "boxes": {"label": "label", "color_by": "cls"},
    }
    assert hub.refresh_app_manifest(identity, {
        "manifest_version": 2,
        "id": "render-app",
        "render": trusted,
    }) is True
    published = hub.publish_app(malicious, identity)
    frame = next(value for value in published if value["type"] == "frame")
    assert frame["render"] == trusted
    assert "script" not in frame["render"]

    record = next(value for value in hub.snapshot_records()
                  if value.raw["type"] == "frame")
    assert record.raw["render"] == trusted
    hub.ensure_formatted(record)
    assert record.formatted[0]["render"] == trusted

    # Cache keys include the exact instance and generation.  A replacement
    # process cannot inherit an earlier manifest contract before its hello path
    # refreshes the trusted entry.
    newer = _identity("render-app", "instance-b", 8)
    assert hub.publish_app(malicious, newer) == []


def test_result_hub_projects_strict_legacy_box_osd_capability(tmp_path):
    hub = ResultHub(ws_port=0, system_uds_path=str(tmp_path / "system.sock"),
                    formatter=NoopFormatter())
    identity = _identity("legacy-box", "instance-1", 1)
    manifest = {
        "manifest_version": 2,
        "id": "legacy-box",
        "render": {"schema_version": 1, "boxes": {"line_width": 2}},
        "output": {"contract_version": 2, "fields": [{
            "name": "box", "from": "results[].box", "coord": "pixel_xyxy",
        }]},
    }
    assert hub.refresh_app_manifest(identity, manifest) is True
    published = hub.publish_app(
        _app_payload(results=[{"box": [1, 2, 30, 40]}]), identity)
    frame = next(value for value in published if value["type"] == "frame")
    assert frame["render"] == {
        "schema_version": 1,
        "boxes": {"line_width": 2},
        "stream_osd": {"supported": ["boxes"], "default": False},
    }
    assert "stream_osd" not in manifest["render"]

    denied = _identity("legacy-box", "instance-2", 2)
    manifest["render"]["stream_osd"] = {
        "supported": [], "default": False,
    }
    assert hub.refresh_app_manifest(denied, manifest) is True
    denied_frame = next(value for value in hub.publish_app(
        _app_payload(results=[{"box": [1, 2, 30, 40]}]), denied)
        if value["type"] == "frame")
    assert denied_frame["render"]["stream_osd"]["supported"] == []

    explicit = _identity("legacy-box", "instance-3", 3)
    manifest["render"] = {
        "schema_version": 1,
        "stream_osd": {"supported": ["boxes"], "default": False},
    }
    manifest["output"]["fields"].append({
        "name": "box_copy", "from": "results[].box",
        "coord": "pixel_xyxy",
    })
    assert hub.refresh_app_manifest(explicit, manifest) is True
    explicit_frame = next(value for value in hub.publish_app(
        _app_payload(results=[{"box": [1, 2, 30, 40]}]), explicit)
        if value["type"] == "frame")
    assert explicit_frame["render"] == manifest["render"]
    assert explicit_frame["results"][0]["spaces"]["box"] == "pixel_xyxy"


def test_manifest_render_cache_rejects_wrong_id_or_manifest_version(tmp_path):
    hub = ResultHub(ws_port=0, system_uds_path=str(tmp_path / "system.sock"),
                    formatter=NoopFormatter())
    identity = _identity("demo", "instance-1", 1)
    assert hub.refresh_app_manifest(identity, {
        "manifest_version": 2, "id": "other", "render": {"boxes": {}}
    }) is False
    assert hub.refresh_app_manifest(identity, {
        "manifest_version": 1, "id": "demo", "render": {"boxes": {}}
    }) is False
    assert hub.status()["trusted_app_renders"] == 0


def test_gateway_hello_primes_render_once_off_result_hot_path(tmp_path, monkeypatch):
    hub = ResultHub(ws_port=0, system_uds_path=str(tmp_path / "system.sock"),
                    formatter=NoopFormatter())
    identity = _identity("demo", "instance-1", 3)

    class Coordinator:
        @staticmethod
        def resolve_identity(*_args):
            return dict(identity)

    reads = []

    def read_manifest(app_id):
        reads.append(app_id)
        return {"manifest_version": 2, "id": app_id,
                "resources": {"claims": [{
                    "name": "camera.frames", "mode": "shared",
                    "required": True,
                }]},
                    "render": {"boxes": {"label": "label"}}}

    monkeypatch.setattr(server, "_read_manifest", read_manifest)
    monkeypatch.setattr(server.state, "get_app", lambda _app_id: {
        "instance_id": "instance-1",
        "generation": 3,
        "pid": os.getpid(),
        "observed_state": "running",
        "frame_stream_contract": {
            "id": "main", "kind": "frame.sock", "path": "/live/0",
        },
    })
    resolved = server._resolve_result_identity(
        Coordinator(), hub, os.getpid(), "demo", "instance-1", 3)
    assert resolved == identity
    for seq in range(1, 6):
        frame = hub.publish_app(
            _app_payload(seq=seq, results=[{"box": [0, 0, 1, 1]}]),
            identity)[0]
        assert frame["render"]["boxes"]["label"] == "label"
        assert frame["stream"]["id"] == "main"
        assert frame["extensions"]["stream_source"] == _managed_stream()
    assert reads == ["demo"]


def test_gateway_hello_losing_stop_race_cannot_revive_retired_generation(
        tmp_path, monkeypatch):
    hub = ResultHub(ws_port=0, system_uds_path=str(tmp_path / "system.sock"),
                    formatter=NoopFormatter())
    identity = _identity("race-stop", "old-run", 2)
    live_state = {
        "instance_id": "old-run", "generation": 2, "pid": os.getpid(),
        "observed_state": "running",
        "frame_stream_contract": _managed_stream(),
    }

    class Coordinator:
        @staticmethod
        def resolve_identity(*_args):
            return dict(identity)

    monkeypatch.setattr(server.state, "get_app", lambda _app_id: dict(live_state))
    monkeypatch.setattr(server, "_read_manifest", lambda app_id: _manifest(app_id))
    assert hub.refresh_app_manifest(
        identity, _manifest("race-stop"), stream_contract=_managed_stream())
    original_refresh = hub.refresh_app_manifest
    published_inside_revoked_window = []

    def stop_between_second_read_and_refresh(*args, **kwargs):
        live_state["observed_state"] = "stopping"
        hub.invalidate_app_manifest("race-stop")
        refreshed = original_refresh(*args, **kwargs)
        published_inside_revoked_window.extend(
            hub.publish_app(_app_payload(seq=8), identity))
        return refreshed

    monkeypatch.setattr(hub, "refresh_app_manifest", stop_between_second_read_and_refresh)
    assert server._resolve_result_identity(
        Coordinator(), hub, os.getpid(), "race-stop", "old-run", 2) is None
    assert hub.status()["active_app_generations"] == 0
    assert hub.status()["generation_fence"]["retired_refresh_ignored"] == 1
    assert published_inside_revoked_window == []
    assert hub.publish_app(_app_payload(seq=9), identity) == []


def test_nonlive_new_generation_clears_older_hub_snapshot(tmp_path, monkeypatch):
    hub = ResultHub(ws_port=0, system_uds_path=str(tmp_path / "system.sock"),
                    formatter=NoopFormatter())
    old = _authorize(hub, _identity("waiting-app", "old-run", 1))
    hub.publish_app(_app_payload(seq=1), old)
    waiting = {
        "app_id": "waiting-app", "instance_id": "new-run", "generation": 2,
        "pid": None, "observed_state": "waiting_resource",
        "frame_stream_contract": _managed_stream(),
    }
    monkeypatch.setattr(server, "_result_hub_instance", hub)
    monkeypatch.setattr(server.state, "get_app", lambda _app_id: dict(waiting))

    assert server._refresh_result_manifest(
        "waiting-app", _manifest("waiting-app"), waiting) is False
    assert hub.status()["active_app_generations"] == 0
    assert not hub.snapshot_records()


def test_failed_stop_still_revokes_hub_generation_and_latest_frame(
        tmp_path, monkeypatch):
    hub = ResultHub(ws_port=0, system_uds_path=str(tmp_path / "system.sock"),
                    formatter=NoopFormatter())
    identity = _authorize(hub, _identity("stop-error", "old-run", 3))
    hub.publish_app(_app_payload(seq=1), identity)
    current = {
        "instance_id": "old-run", "generation": 3, "pid": os.getpid(),
        "observed_state": "running", "launch_mode": "managed",
        "frame_stream_contract": _managed_stream(),
    }

    class Coordinator:
        @staticmethod
        def stop(_app_id):
            current["observed_state"] = "stopping"
            raise RuntimeError("teardown uncertain")

    monkeypatch.setattr(server, "_result_hub_instance", hub)
    monkeypatch.setattr(server, "_coordinator", lambda: Coordinator())
    monkeypatch.setattr(server.state, "get_app", lambda _app_id: dict(current))
    monkeypatch.setattr(server, "busy_gate", lambda **_kwargs: nullcontext())

    with pytest.raises(RuntimeError, match="teardown uncertain"):
        server.do_stop("stop-error")
    assert hub.status()["active_app_generations"] == 0
    assert not hub.snapshot_records()


def test_managed_upgrade_rollback_fences_failed_generation_before_restore_and_refreshes_old(
        tmp_path, monkeypatch):
    hub = ResultHub(ws_port=0, system_uds_path=str(tmp_path / "system.sock"),
                    formatter=NoopFormatter())
    failed = _authorize(
        hub, _identity("rollback-app", "failed-upgrade", 2), fields=[
            {"from": "results[].box", "coord": "pixel_xyxy"},
        ])
    hub.publish_app(_app_payload(
        seq=1, results=[{"box": [0, 0, 10, 10]}]), failed)
    current = {
        "instance_id": "failed-upgrade", "generation": 2, "pid": None,
        "observed_state": "failed",
        "frame_stream_contract": {"id": "", "kind": "none"},
    }
    restored = {
        "app_id": "rollback-app", "instance_id": "restored-old-version",
        "generation": 3, "pid": os.getpid(), "observed_state": "running",
        "frame_stream_contract": _managed_stream(),
    }
    old_manifest = _manifest("rollback-app", fields=[
        {"from": "results[].box", "coord": "pixel_xyxy"},
    ])
    order = []

    class Coordinator:
        @staticmethod
        def start(*_args, **_kwargs):
            # The failed upgrade's presentation generation must be gone before
            # the retained version is allowed to launch.
            assert hub.status()["active_app_generations"] == 0
            assert not hub.snapshot_records()
            order.append("restore-start")
            current.clear()
            current.update(restored)
            return dict(restored)

    original_invalidate = hub.invalidate_app_manifest

    def invalidate(*args, **kwargs):
        order.append("invalidate")
        return original_invalidate(*args, **kwargs)

    monkeypatch.setattr(hub, "invalidate_app_manifest", invalidate)
    monkeypatch.setattr(server, "_result_hub_instance", hub)
    monkeypatch.setattr(server, "_coordinator", lambda: Coordinator())
    monkeypatch.setattr(server.state, "get_app", lambda _app_id: dict(current))
    monkeypatch.setattr(server.state, "clear_active_if", lambda _app_id: None)
    monkeypatch.setattr(server.supervisor, "stop", lambda _app_id: {})
    monkeypatch.setattr(server.installer, "restore_prev", lambda _app_id: True)
    monkeypatch.setattr(server, "_read_manifest", lambda _app_id: old_manifest)
    monkeypatch.setattr(server, "_managed_launch", lambda *_args: None)

    assert server._rollback_upgrade(
        "rollback-app", restore_active=False, restore_managed=True,
    ) == "rollback-app"
    assert order[:2] == ["invalidate", "restore-start"]
    assert hub.status()["active_app_generations"] == 1
    assert hub.publish_app(_app_payload(seq=2), failed) == []
    frame = hub.publish_app(_app_payload(
        seq=2, width=1280, height=720,
        results=[{"box": [1, 2, 3, 4]}]), restored)[0]
    assert frame["source"]["instance"] == "restored-old-version"
    assert frame["stream"]["id"] == "main"
    assert frame["extensions"]["stream_source"] == _managed_stream()


def test_failed_upgrade_rollback_start_leaves_result_hub_empty(
        tmp_path, monkeypatch):
    hub = ResultHub(ws_port=0, system_uds_path=str(tmp_path / "system.sock"),
                    formatter=NoopFormatter())
    failed = _authorize(hub, _identity("rollback-fails", "upgrade", 4))
    hub.publish_app(_app_payload(seq=1), failed)
    attempted = _identity("rollback-fails", "old-version-attempt", 5)
    current = {
        **attempted, "observed_state": "running",
        "frame_stream_contract": _managed_stream(),
    }

    class Coordinator:
        @staticmethod
        def start(*_args, **_kwargs):
            assert hub.status()["active_app_generations"] == 0
            assert hub.refresh_app_manifest(
                attempted, _manifest("rollback-fails"),
                stream_contract=_managed_stream())
            hub.publish_app(_app_payload(seq=2), attempted)
            current["observed_state"] = "failed"
            current["pid"] = None
            current["frame_stream_contract"] = {"id": "", "kind": "none"}
            raise server.supervisor.SupervisorError("old version also failed")

    monkeypatch.setattr(server, "_result_hub_instance", hub)
    monkeypatch.setattr(server, "_coordinator", lambda: Coordinator())
    monkeypatch.setattr(server.state, "get_app", lambda _app_id: dict(current))
    monkeypatch.setattr(server.state, "clear_active_if", lambda _app_id: None)
    monkeypatch.setattr(server.supervisor, "stop", lambda _app_id: {})
    monkeypatch.setattr(server.installer, "restore_prev", lambda _app_id: True)
    monkeypatch.setattr(
        server, "_read_manifest", lambda app_id: _manifest(app_id))
    monkeypatch.setattr(server, "_managed_launch", lambda *_args: None)

    assert server._rollback_upgrade(
        "rollback-fails", restore_active=False, restore_managed=True,
    ) is None
    assert hub.status()["active_app_generations"] == 0
    assert not hub.snapshot_records()
    assert hub.publish_app(_app_payload(seq=3), attempted) == []


def test_reconciler_observes_legacy_crash_revokes_and_tombstones_without_restart(
        tmp_path, monkeypatch):
    apps = tmp_path / "apps"
    appmgr = tmp_path / "appmgr"
    apps.mkdir()
    appmgr.mkdir()
    monkeypatch.setattr(paths, "APPS_DIR", str(apps))
    monkeypatch.setattr(paths, "APPMGR_DIR", str(appmgr))
    monkeypatch.setattr(paths, "STATE_FILE", str(apps / "state.json"))
    monkeypatch.setattr(server, "_coordinator_instance", None)
    monkeypatch.setattr(server, "_coordinator_layout", None)

    app_id = "legacy-crash"
    app_dir = paths.app_dir(app_id)
    os.makedirs(app_dir)
    manifest = _manifest(app_id)
    manifest.update({"version": "1.0.0", "entry": "app.py"})
    with open(os.path.join(app_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f)
    server.cache_clear()
    server.state.save({"active_app": None, "active_version": None})
    started = server.state.begin_start(
        app_id, "legacy-instance", version="1.0.0", launch_mode="legacy")
    generation = int(started["generation"])
    server.state.transition(
        app_id, "running", pid=4321, pgid=4321,
        frame_stream_contract=_managed_stream(), started_at=time.time())

    revocations = []

    class Registry:
        @staticmethod
        def revoke(pid, **identity):
            revocations.append((pid, identity))

    coord = server._coordinator()
    coord.inference_registry = Registry()
    monkeypatch.setattr(server.supervisor, "reap_children", lambda: 0)
    monkeypatch.setattr(server.supervisor, "drain_exits", lambda: [])
    monkeypatch.setattr(server.supervisor, "sweep_stale", lambda: [])
    monkeypatch.setattr(server.supervisor, "is_running", lambda _app_id: None)
    monkeypatch.setattr(
        server.supervisor, "last_exit",
        lambda _app_id: {"pid": 4321, "exit_code": 137})

    class Events:
        published = []

        @classmethod
        def publish(cls, *args, **kwargs):
            cls.published.append((args, kwargs))

    class Operations:
        events = Events()

    monkeypatch.setattr(server, "_operation_manager", lambda: Operations())

    hub = ResultHub(ws_port=0, system_uds_path=str(tmp_path / "system.sock"),
                    formatter=NoopFormatter())
    identity = _authorize(
        hub, {"app_id": app_id, "instance_id": "legacy-instance",
              "generation": generation, "pid": 4321})
    hub.publish_app(_app_payload(seq=1), identity)
    controls = []

    class WebSockets:
        @staticmethod
        def purge_source(_source_id):
            return 0

        @staticmethod
        def broadcast_control(envelope):
            controls.append(envelope)

    hub._ws = WebSockets()
    monkeypatch.setattr(server, "_result_hub_instance", hub)

    result = server._reconcile_once()

    assert result == [{
        "id": app_id, "action": "legacy-unmanaged",
        "observed_state": "failed",
    }]
    assert revocations == [(4321, {
        "app_id": app_id, "instance_id": "legacy-instance",
        "generation": generation,
    })]
    record = server.state.get_app(app_id)
    assert record["observed_state"] == "failed"
    assert record["pid"] is None
    assert record["frame_stream_contract"] == {"id": "", "kind": "none"}
    hub._ws = None
    assert hub.status()["active_app_generations"] == 0
    assert not hub.snapshot_records()
    assert hub.publish_app(_app_payload(seq=2), identity) == []
    assert [item["type"] for item in controls] == ["source_invalidated"]
    assert controls[0]["source"] == {
        "kind": "app", "id": app_id, "app_id": app_id,
        "instance": "legacy-instance", "generation": generation,
        "trust": "local",
    }


def test_reconciler_missing_install_rechecks_under_gate_and_preserves_reinstalled_generation(
        tmp_path, monkeypatch):
    apps = tmp_path / "apps"
    appmgr = tmp_path / "appmgr"
    apps.mkdir()
    appmgr.mkdir()
    monkeypatch.setattr(paths, "APPS_DIR", str(apps))
    monkeypatch.setattr(paths, "APPMGR_DIR", str(appmgr))
    monkeypatch.setattr(paths, "STATE_FILE", str(apps / "state.json"))

    app_id = "reinstall-race"
    server.state.save({"active_app": None, "active_version": None})
    started = server.state.begin_start(
        app_id, "reinstalled-instance", version="2.0.0",
        launch_mode="managed")
    generation = int(started["generation"])
    server.state.transition(
        app_id, "running", pid=5432, pgid=5432,
        frame_stream_contract=_managed_stream(), started_at=time.time())
    identity = {
        "app_id": app_id, "instance_id": "reinstalled-instance",
        "generation": generation, "pid": 5432,
    }
    hub = ResultHub(ws_port=0, system_uds_path=str(tmp_path / "system.sock"),
                    formatter=NoopFormatter())
    _authorize(hub, identity)
    hub.publish_app(_app_payload(seq=1), identity)
    monkeypatch.setattr(server, "_result_hub_instance", hub)

    class Coordinator:
        @staticmethod
        def reconcile_one(_app_id, **_kwargs):
            assert os.path.isdir(paths.app_dir(app_id))
            return {
                "id": app_id, "action": "healthy", "pid": 5432,
                "observed_state": "running",
            }

    monkeypatch.setattr(server, "_coordinator", lambda: Coordinator())
    monkeypatch.setattr(server, "_managed_launch", lambda *_args: None)
    monkeypatch.setattr(server.supervisor, "reap_children", lambda: 0)
    monkeypatch.setattr(server.supervisor, "drain_exits", lambda: [])
    monkeypatch.setattr(server.supervisor, "sweep_stale", lambda: [])

    gate_calls = []

    @contextmanager
    def raced_gate(**_kwargs):
        gate_calls.append(len(gate_calls) + 1)
        if len(gate_calls) == 2:
            # The first gate is the stale-process sweep.  Reinstallation wins
            # while this app's reconcile tick is waiting for the mutation gate.
            directory = paths.app_dir(app_id)
            os.makedirs(directory)
            manifest = _manifest(app_id)
            manifest.update({"version": "2.0.0", "entry": "app.py"})
            with open(os.path.join(directory, "manifest.json"), "w") as f:
                json.dump(manifest, f)
            server.cache_clear()
        yield

    monkeypatch.setattr(server, "busy_gate", raced_gate)

    assert server._reconcile_once() == [{
        "id": app_id, "action": "healthy", "pid": 5432,
        "observed_state": "running",
    }]
    assert gate_calls == [1, 2]
    record = server.state.get_app(app_id)
    assert record["observed_state"] == "running"
    assert record["instance_id"] == "reinstalled-instance"
    assert record["frame_stream_contract"] == _managed_stream()
    assert hub.status()["active_app_generations"] == 1
    assert hub.publish_app(_app_payload(seq=2), identity)


def test_ws_generation_change_sends_exact_tombstone_and_cas_preserves_winner(
        tmp_path):
    hub = ResultHub(ws_port=0, system_uds_path=str(tmp_path / "system.sock"),
                    formatter=NoopFormatter()).start()
    old = _authorize(hub, _identity("demo", "old-run", 4), fields=[
        {"from": "results[].box", "coord": "pixel_xyxy"}])
    ws = _WsReader(hub.ws_port)
    ws.sock.settimeout(2)
    try:
        assert ws.json()["type"] == "hello"
        assert ws.json()["type"] == "snapshot"
        hub.publish_app(_app_payload(
            seq=1, results=[{"box": [0, 0, 10, 10]}]), old)
        assert ws.json()["type"] == "frame"

        current = _identity("demo", "new-run", 5)
        assert hub.refresh_app_manifest(
            current, _manifest("demo"), stream_contract=_managed_stream())
        tombstone = ws.json()
        assert tombstone["type"] == "source_invalidated"
        assert tombstone["source"] == {
            "kind": "app", "id": "demo", "app_id": "demo",
            "instance": "old-run", "generation": 4, "trust": "local",
        }
        assert not [record for record in hub.snapshot_records()
                    if record.source_id == "demo"]

        # A delayed old control path may revoke only the tuple it authenticated;
        # the winning generation and its trusted stream remain intact.
        assert hub.invalidate_app_manifest("demo", identity=old) is False
        frame = hub.publish_app(_app_payload(
            seq=2, width=1280, height=720,
            results=[{"box": [1, 2, 3, 4]}]), current)[0]
        assert frame["source"]["instance"] == "new-run"
        assert frame["stream"]["id"] == "main"
        assert ws.json()["source"]["instance"] == "new-run"
    finally:
        ws.close()
        hub.stop()


def test_system_adapter_preserves_box_object_and_marks_normalized_space():
    envelope = normalize_system_payload(
        _system_payload(task="classification", seq=3),
        {"kind": "builtin", "id": "builtin", "trust": "test"})[0]
    assert envelope["source"]["kind"] == "builtin"
    assert envelope["source"]["id"] == "builtin"
    assert envelope["source"]["model_id"] == 7
    assert envelope["stream"]["coordinate_space"] == "normalized_xyxy"
    result = envelope["results"][0]
    assert result["box"] == {
        "left": 0.1, "top": 0.2, "right": 0.4, "bottom": 0.5}
    assert result["space"] == "normalized_xyxy"
    assert result["spaces"]["box"] == "normalized_xyxy"
    assert result["kind"] == "classification"
    # The untrusted payload claim is diagnostic only.
    assert envelope["extensions"]["reported_source_id"] == "spoofed-external"


def test_system_keypoints_alias_keeps_trusted_geometry_space():
    payload = {
        "task_type": 4,
        "task_type_name": "keypoints",
        "timestamp_ms": 1_800_000_000_000,
        "model_id": 7,
        "source_id": "builtin",
        "pts_us": 123456,
        "keypoints": {"count": 1, "instances": [{
            "class_id": 0,
            "class_name": "person",
            "score": 0.9,
            "points": [
                {"keypoint_id": 0, "x": 0.1, "y": 0.2, "score": 0.8},
                {"keypoint_id": 1, "x": 0.3, "y": 0.4, "score": 0.7},
            ],
        }]},
    }
    envelope = normalize_system_payload(
        payload, {"kind": "builtin", "id": "builtin", "trust": "test"})[0]
    result = envelope["results"][0]
    assert result["keypoints"] == result["points"]
    assert result["spaces"]["points"] == "normalized_points"
    assert result["spaces"]["keypoints"] == "normalized_points"


def test_event_replay_is_bounded_namespaced_and_deduplicated(tmp_path):
    hub = ResultHub(
        ws_port=0, system_uds_path=str(tmp_path / "system.sock"),
        formatter=NoopFormatter(), event_replay=2)
    app_a = _authorize(hub, _identity("app-a", "a", 1), camera=False)
    app_b = _authorize(hub, _identity("app-b", "b", 8), camera=False)
    first = _app_payload(events=[{"kind": "fall", "event_id": "same"}], seq=1)
    assert len(hub.publish_app(first, app_a)) == 1
    assert hub.publish_app(first, app_a) == []
    # The same producer id in another app/generation cannot collide globally.
    assert len(hub.publish_app(first, app_b)) == 1
    third = _app_payload(events=[{"kind": "wake", "event_id": "third"}], seq=2)
    assert len(hub.publish_app(third, app_a)) == 1

    events = [record.raw for record in hub.snapshot_records()
              if record.raw["type"] == "event"]
    assert len(events) == 2
    assert {value["id"] for value in events} == {
        "evt:app-b:8:same", "evt:app-a:1:third"}
    status = hub.status()
    assert status["event_replay"]["duplicates"] == 1
    assert status["event_replay"]["size"] == 2


def test_event_delivery_classification_and_state_producer_id_collision():
    envelopes = normalize_app_payload(_app_payload(events=[
        {"kind": "pose_state", "event_id": 7, "state": "fallen"},
        {"kind": "fall", "event_id": 7, "state": "fallen"},
        {"kind": "workout", "rep_completed": False, "reps": 2},
        {"kind": "workout", "rep_completed": True, "reps": 3},
        {"kind": "metrics", "value": 1},
        {"kind": "listen_timeout"},
        {"kind": "qrcode", "text": "continuous"},
        {"kind": "text", "text": "continuous"},
        {"kind": "drowsiness", "state": "danger"},
    ]), _identity(generation=5))
    assert [value["extensions"]["delivery"] for value in envelopes] == [
        "state", "edge", "state", "edge", "state", "edge",
        "state", "state", "state"]
    pose, fall = envelopes[:2]
    assert pose["events"][0]["producer_event_id"] == 7
    assert pose["id"].startswith("evt:demo:5:h-")
    assert fall["id"] == "evt:demo:5:7"
    assert pose["id"] != fall["id"]


class CountingFormatter:
    def __init__(self):
        self.calls = 0

    def format(self, _raw):
        self.calls += 1
        return []


def test_edge_priority_ring_state_latest_wins_and_preflight_skips_format(tmp_path):
    formatter = CountingFormatter()
    hub = ResultHub(
        ws_port=0, system_uds_path=str(tmp_path / "system.sock"),
        formatter=formatter, event_replay=3)
    identity = _identity("priority", "p", 2)
    _authorize(hub, identity, camera=False)

    def publish(seq, event):
        return hub.publish_app(_app_payload(seq=seq, events=[event]), identity)

    assert publish(1, {"kind": "metrics", "value": 1})
    assert publish(2, {"kind": "fall", "event_id": "fall-1"})
    assert publish(3, {"kind": "line_cross", "event_id": "line-1"})
    assert publish(4, {"kind": "metrics", "value": 2})
    records = [value.raw for value in hub.snapshot_records()
               if value.raw["type"] == "event"]
    assert len(records) == 3
    metrics = next(value for value in records
                   if value["extensions"]["event_kind"] == "metrics")
    assert metrics["events"][0]["value"] == 2

    # A new state kind may evict only an older state; an edge reclaims that
    # state slot.  Once the ring is all-edge, incoming state is rejected before
    # formatter work and cannot erase any critical transition.
    assert publish(5, {"kind": "track", "track_id": 9})
    assert publish(6, {"kind": "blink", "blink_count": 1})
    before_reject_calls = formatter.calls
    assert publish(7, {"kind": "drowsiness", "state": "danger"}) == []
    assert formatter.calls == before_reject_calls
    kinds = [value.raw["extensions"]["event_kind"]
             for value in hub.snapshot_records() if value.raw["type"] == "event"]
    assert kinds == ["fall", "line_cross", "blink"]

    assert publish(8, {"kind": "yawn", "yawn_count_5min": 1})
    kinds = [value.raw["extensions"]["event_kind"]
             for value in hub.snapshot_records() if value.raw["type"] == "event"]
    assert kinds == ["line_cross", "blink", "yawn"]
    replay = hub.status()["event_replay"]
    assert replay["edge"] == 3 and replay["state"] == 0
    assert replay["state_dropped"] == 1
    assert replay["state_replaced"] == 1
    assert replay["state_evicted"] == 2
    assert replay["edge_evicted"] == 1


def test_stable_qrcode_state_is_deduplicated_then_changed_state_replaces(tmp_path):
    formatter = CountingFormatter()
    hub = ResultHub(ws_port=0, system_uds_path=str(tmp_path / "system.sock"),
                    formatter=formatter)
    identity = _authorize(hub, _identity(), camera=False)
    event = {"kind": "qrcode", "text": "same-code", "event_id": 1,
             "quad": [[0, 0], [1, 0], [1, 1], [0, 1]]}
    assert hub.publish_app(_app_payload(seq=1, events=[event]), identity)
    assert hub.publish_app(_app_payload(
        seq=2, events=[dict(event, event_id=2)]), identity) == []
    moved = dict(event, event_id=3,
                 quad=[[2, 2], [3, 2], [3, 3], [2, 3]])
    assert hub.publish_app(_app_payload(seq=3, events=[moved]), identity)
    records = [value.raw for value in hub.snapshot_records()
               if value.raw["type"] == "event"]
    assert len(records) == 1
    assert records[0]["extensions"]["delivery"] == "state"
    assert records[0]["events"][0]["quad"] == moved["quad"]
    assert formatter.calls == 0  # raw-only path never invokes templates
    record = next(value for value in hub.snapshot_records()
                  if value.raw["type"] == "event")
    hub.ensure_formatted(record)
    hub.ensure_formatted(record)
    assert formatter.calls == 1  # lazy snapshot projection is cached per batch
    assert hub.status()["event_replay"]["duplicates"] == 1


def test_multiple_qrcodes_are_one_latest_state_snapshot(tmp_path):
    hub = ResultHub(ws_port=0, system_uds_path=str(tmp_path / "system.sock"),
                    formatter=NoopFormatter())
    identity = _authorize(hub, _identity("qr-app"), fields=[
        {"from": "events[kind=qrcode].quad", "coord": "pixel_quad",
         "event_kind": "qrcode"}], camera=True)
    code_a = {"kind": "qrcode", "text": "A",
              "quad": [[0, 0], [4, 0], [4, 4], [0, 4]]}
    code_b = {"kind": "qrcode", "text": "B",
              "quad": [[10, 10], [14, 10], [14, 14], [10, 14]]}
    first = hub.publish_app(_app_payload(seq=1, events=[code_a, code_b]), identity)
    group = next(value for value in first if value["type"] == "event")
    assert [value["text"] for value in group["events"]] == ["A", "B"]
    assert all(value["spaces"]["quad"] == "pixel_quad"
               for value in group["events"])
    replay = [record.raw for record in hub.snapshot_records()
              if record.raw["type"] == "event"]
    assert len(replay) == 1 and len(replay[0]["events"]) == 2

    hub.publish_app(_app_payload(seq=2, events=[code_a]), identity)
    replay = [record.raw for record in hub.snapshot_records()
              if record.raw["type"] == "event"]
    assert len(replay) == 1
    assert [value["text"] for value in replay[0]["events"]] == ["A"]


def test_identical_producerless_transcripts_are_distinct_edges(tmp_path):
    formatter = CountingFormatter()
    hub = ResultHub(ws_port=0, system_uds_path=str(tmp_path / "system.sock"),
                    formatter=formatter)
    identity = _authorize(hub, _identity(), camera=False)
    event = {"kind": "transcript", "text": "same words"}
    first = hub.publish_app(_app_payload(seq=10, events=[event]), identity)
    second = hub.publish_app(_app_payload(seq=11, events=[event]), identity)
    assert first and second
    assert first[0]["id"] != second[0]["id"]
    assert first[0]["id"].startswith("evt:demo:1:e-10-")
    assert second[0]["id"].startswith("evt:demo:1:e-11-")
    assert formatter.calls == 0


def test_frame_latest_wins_and_summary_status_is_retained(tmp_path):
    hub = ResultHub(ws_port=0, system_uds_path=str(tmp_path / "system.sock"),
                    formatter=NoopFormatter())
    identity = _authorize(hub, _identity(), fields=[
        {"from": "results[].box", "coord": "pixel_xyxy"}])
    hub.publish_app(_app_payload(seq=1, results=[{"box": [0, 0, 1, 1]}],
                                 summary={"state": "one"}), identity)
    hub.publish_app(_app_payload(seq=2, results=[{"box": [1, 1, 2, 2]}],
                                 summary={"state": "two"}), identity)
    records = [record.raw for record in hub.snapshot_records()]
    frames = [value for value in records if value["type"] == "frame"]
    statuses = [value for value in records if value["type"] == "status"]
    assert len(frames) == 1 and frames[0]["seq"] == 2
    assert len(statuses) == 1 and statuses[0]["summary"]["state"] == "two"


def test_generation_refresh_purges_only_that_apps_old_snapshot_records(tmp_path):
    hub = ResultHub(ws_port=0, system_uds_path=str(tmp_path / "system.sock"),
                    formatter=NoopFormatter())
    first = _identity("demo", "old-instance", 1)
    other = _identity("other", "other-instance", 4)
    manifest = {"manifest_version": 2, "id": "demo", "render": {}}
    other_manifest = {"manifest_version": 2, "id": "other", "render": {}}
    assert hub.refresh_app_manifest(first, manifest)
    assert hub.refresh_app_manifest(other, other_manifest)
    hub.publish_app(_app_payload(
        seq=1, results=[{"box": [0, 0, 1, 1]}],
        events=[{"kind": "fall", "event_id": 1}],
        summary={"state": "old"}), first)
    hub.publish_app(_app_payload(
        seq=2, results=[{"box": [1, 1, 2, 2]}],
        events=[{"kind": "wake", "keyword": "other"}],
        summary={"state": "other"}), other)

    current = _identity("demo", "new-instance", 2)
    assert hub.refresh_app_manifest(current, manifest)
    after_refresh = [record.raw for record in hub.snapshot_records()]
    assert not [value for value in after_refresh
                if value["source"]["id"] == "demo"]
    assert {value["source"]["id"] for value in after_refresh} == {"other"}
    hub._ingress_thread = threading.current_thread()
    assert hub.submit_app(_app_payload(seq=99), first) is False
    hub._ingress_thread = None

    hub.publish_app(_app_payload(
        seq=3, results=[{"box": [2, 2, 3, 3]}],
        summary={"state": "new"}), current)
    final = [record.raw for record in hub.snapshot_records()]
    demo = [value for value in final if value["source"]["id"] == "demo"]
    assert demo and {value["source"]["generation"] for value in demo} == {2}
    assert {value["summary"].get("state") for value in demo} == {"new"}
    fence = hub.status()["generation_fence"]
    assert fence["records_purged"] == 3
    assert fence["stale_rejected"] == 1


def test_manifest_refresh_cannot_downgrade_generation_or_replace_same_generation(
        tmp_path):
    hub = ResultHub(ws_port=0, system_uds_path=str(tmp_path / "system.sock"),
                    formatter=NoopFormatter())
    current = _identity("monotonic-app", "current-instance", 2)
    current_manifest = _manifest(
        "monotonic-app",
                fields=[{
                    "name": "box", "from": "results[].box",
                    "coord": "pixel_xyxy",
                }],
        render={"boxes": {"color_by": "label", "line_width": 2}},
    )
    assert hub.refresh_app_manifest(
        current, current_manifest, stream_contract=_managed_stream())
    assert hub.publish_app(_app_payload(
        seq=20, width=1280, height=720,
        results=[{"box": [10, 20, 30, 40], "label": "current"}],
        events=[{"kind": "fall", "event_id": 20}],
        summary={"state": "current"}), current)

    before_records = json.dumps(
        [record.raw for record in hub.snapshot_records()], sort_keys=True)
    before_contract = hub._trusted_app_contract(current)
    before_replay = dict(hub.status()["event_replay"])
    before_purged = hub.status()["generation_fence"]["records_purged"]

    # A delayed old hello may arrive after its manifest disappeared or was
    # replaced.  It is still a successful no-op: returning False would make
    # server._resolve_result_identity invalidate the current generation.
    old = _identity("monotonic-app", "old-instance", 1)
    assert hub.refresh_app_manifest(old, None, stream_contract={
        "id": "sub", "kind": "ffmpeg", "path": "/live/1"})

    # A generation number uniquely names one instance.  Another instance with
    # the same number must not replace the current contract either.
    collision = _identity("monotonic-app", "collision-instance", 2)
    replacement_manifest = _manifest(
        "monotonic-app",
        fields=[{"from": "results[].box", "coord": "normalized_xyxy"}],
        render={"boxes": {"color_by": "score", "line_width": 9}},
    )
    assert hub.refresh_app_manifest(
        collision, replacement_manifest, stream_contract=None)

    assert hub._is_current_app_identity(current)
    assert not hub._is_current_app_identity(old)
    assert not hub._is_current_app_identity(collision)
    assert hub._trusted_app_contract(current) == before_contract
    assert json.dumps(
        [record.raw for record in hub.snapshot_records()], sort_keys=True
    ) == before_records
    assert hub.status()["event_replay"] == before_replay
    fence = hub.status()["generation_fence"]
    assert fence["records_purged"] == before_purged
    assert fence["stale_refresh_ignored"] == 2

    assert hub.publish_app(_app_payload(seq=21), old) == []
    assert hub.publish_app(_app_payload(seq=22), collision) == []
    frame = hub.publish_app(_app_payload(
        seq=23, width=1280, height=720,
        results=[{"box": [20, 30, 40, 50], "label": "still-current"}]),
        current)[0]
    assert frame["source"]["instance"] == "current-instance"
    assert frame["source"]["generation"] == 2
    assert frame["stream"]["id"] == "main"


def test_generation_final_cas_blocks_inflight_old_publish_and_future_wall(
        tmp_path, monkeypatch):
    hub = ResultHub(ws_port=0, system_uds_path=str(tmp_path / "system.sock"),
                    formatter=NoopFormatter())
    old = _authorize(hub, _identity("race-app", "old", 1), fields=[
        {"from": "results[].box", "coord": "pixel_xyxy"}])
    entered = threading.Event()
    release = threading.Event()
    original = result_hub_module.normalize_app_payload

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(result_hub_module, "normalize_app_payload", blocked)
    returned = []
    thread = threading.Thread(target=lambda: returned.append(hub.publish_app(
        _app_payload(results=[{"box": [0, 0, 1, 1]}],
                     timestamp_ms=9_000_000_000_000_000), old)))
    thread.start()
    assert entered.wait(2)
    new = _identity("race-app", "new", 2)
    assert hub.refresh_app_manifest(new, _manifest("race-app", fields=[
        {"from": "results[].box", "coord": "pixel_xyxy"}]))
    release.set()
    thread.join(timeout=3)
    assert returned == [[]]
    assert not [record for record in hub.snapshot_records()
                if record.source_id == "race-app"]

    monkeypatch.setattr(result_hub_module, "normalize_app_payload", original)
    before = int(time.time() * 1000)
    frame = hub.publish_app(_app_payload(
        seq=2, results=[{"box": [1, 1, 2, 2]}],
        timestamp_ms=9_000_000_000_000_000), new)[0]
    after = int(time.time() * 1000)
    assert before <= frame["time"]["wall_ms"] <= after
    assert frame["extensions"]["reported_timestamp_ms"] == \
        9_000_000_000_000_000
    assert frame["source"]["generation"] == 2


class _DummySocket:
    def shutdown(self, *_args):
        pass

    def close(self):
        pass


def test_full_ws_queue_never_evicts_event_for_incoming_frame_or_status():
    client = _HubClient(_DummySocket(), "local", None, max_queue=4,
                        send_timeout=1, lag_limit=100)
    try:
        for index in range(4):
            assert client._append("event", "app", b"event", data=True)
        assert client._append("frame", "app", b"frame", data=True) is False
        assert client._append("status", "app", b"status", data=True) is False
        assert [item[0] for item in client._items] == ["event"] * 4
    finally:
        client.close()


def test_ws_queue_state_latest_wins_and_never_evicts_edge():
    client = _HubClient(_DummySocket(), "local", None, max_queue=4,
                        send_timeout=1, lag_limit=100)
    try:
        assert client._append(
            "event", "edge-1", b"fall", data=True, delivery="edge")
        assert client._append(
            "event", "app:metrics", b"metrics-1", data=True,
            delivery="state", replace=True)
        assert client._append(
            "event", "app:metrics", b"metrics-2", data=True,
            delivery="state", replace=True)
        assert len(client._items) == 2
        assert [item[2] for item in client._items] == [b"fall", b"metrics-2"]
        assert client._append(
            "event", "edge-2", b"line", data=True, delivery="edge")
        assert client._append(
            "event", "edge-3", b"blink", data=True, delivery="edge")
        # Incoming edge reclaims the only state slot.
        assert client._append(
            "event", "edge-4", b"wake", data=True, delivery="edge")
        assert [item[4] for item in client._items] == ["edge"] * 4
        # With an all-edge queue, state is rejected and no edge is removed.
        assert client._append(
            "event", "app:track", b"track", data=True,
            delivery="state", replace=True) is False
        assert [item[2] for item in client._items] == [
            b"fall", b"line", b"blink", b"wake"]
        status = client.status()
        assert status["state_replaced"] == 2
        assert status["state_dropped"] == 1
        assert status["edge_dropped"] == 0
    finally:
        client.close()


def test_all_edge_ws_saturation_counts_lag_and_disconnects_for_replay():
    client = _HubClient(_DummySocket(), "local", None, max_queue=4,
                        send_timeout=1, lag_limit=1)
    try:
        for index in range(4):
            assert client._append(
                "event", "edge-%d" % index, b"edge", data=True,
                delivery="edge")
        assert client._append(
            "event", "edge-4", b"edge", data=True, delivery="edge") is False
        assert client.alive() is True
        assert client._append(
            "event", "edge-5", b"edge", data=True, delivery="edge") is False
        status = client.status()
        assert status["alive"] is False
        assert status["dropped"] == 2
        assert status["edge_dropped"] == 2
        assert status["lag_strikes"] == 2
    finally:
        client.close()


def test_formatted_edge_batch_has_edge_priority_and_seen_only_after_enqueue(tmp_path):
    hub = ResultHub(ws_port=0, system_uds_path=str(tmp_path / "system.sock"),
                    formatter=EchoFormatter())
    identity = _authorize(hub, _identity("formatted-edge"), fields=[
        {"from": "results[].box", "coord": "pixel_xyxy"}])
    hub.publish_app(_app_payload(
        results=[{"box": [0, 0, 1, 1]}],
        events=[{"kind": "fall", "event_id": 1}]), identity)
    record = next(value for value in hub.snapshot_records()
                  if value.raw["type"] == "frame")
    assert record.format_batch.delivery == "edge"
    hub.ensure_formatted(record)
    ws_server = _HubWebSocketServer(hub, host="127.0.0.1", port=0)
    client = _HubClient(_DummySocket(), "local", ws_server, max_queue=4,
                        send_timeout=1, lag_limit=100)
    client.subscription = _Subscription(
        view="formatted", sources=frozenset(("formatted-edge",)),
        types=frozenset(("frame", "event")))
    try:
        for index in range(4):
            assert client._append(
                "frame", "old-%d" % index, b"old", data=True)
        client._formatted_pending.add(record.batch_id)
        assert client.offer_formatted_ready(record)
        assert record.batch_id in client._formatted_seen
        assert any(item[0] == "event" and item[4] == "edge"
                   for item in client._items)

        client._items.clear()
        client._formatted_seen.clear()
        for index in range(4):
            assert client._append(
                "event", "edge-%d" % index, b"edge", data=True,
                delivery="edge")
        client._formatted_pending.add(record.batch_id)
        assert client.offer_formatted_ready(record) is False
        assert record.batch_id not in client._formatted_seen
        assert record.batch_id not in client._formatted_pending
    finally:
        client.close()


def test_ws_generation_fence_purges_queued_source_data_only():
    client = _HubClient(_DummySocket(), "local", None, max_queue=8,
                        send_timeout=1, lag_limit=100)
    try:
        client.offer_control({"type": "snapshot"})
        assert client._append(
            "frame", "demo:camera-0", b"old-demo", data=True)
        assert client._append(
            "event", "evt:demo:1:edge", b"old-edge", data=True,
            delivery="edge")
        assert client._append(
            "frame", "other:camera-0", b"other", data=True)
        assert client.purge_source("demo") == 2
        assert [item[2] for item in client._items if item[3]] == [b"other"]
        assert any(not item[3] for item in client._items)  # control retained
    finally:
        client.close()


def test_ws_generation_fence_purges_queued_formatted_edge_batch():
    client = _HubClient(_DummySocket(), "local", None, max_queue=8,
                        send_timeout=1, lag_limit=100)
    try:
        assert client._append(
            "event", "batch:demo:1:17", b"old-formatted-edge", data=True,
            delivery="edge")
        assert client._append(
            "event", "batch:other:1:9", b"other-formatted-edge", data=True,
            delivery="edge")
        client._formatted_seen.update(("batch:demo:1:17", "batch:other:1:9"))
        client._formatted_order.extend(("batch:demo:1:17", "batch:other:1:9"))
        client._formatted_pending.update(
            ("batch:demo:1:18", "batch:other:1:10"))

        assert client.purge_source("demo") == 1
        assert [item[2] for item in client._items if item[3]] == [
            b"other-formatted-edge"]
        assert list(client._formatted_order) == ["batch:other:1:9"]
        assert client._formatted_seen == {"batch:other:1:9"}
        assert client._formatted_pending == {"batch:other:1:10"}
    finally:
        client.close()


def test_replay_and_live_publish_have_total_latest_wins_order(tmp_path, monkeypatch):
    hub = ResultHub(ws_port=0, system_uds_path=str(tmp_path / "system.sock"),
                    formatter=NoopFormatter())
    identity = _authorize(hub, _identity("ordered-app"), fields=[
        {"from": "results[].box", "coord": "pixel_xyxy"}])
    hub.publish_app(_app_payload(
        seq=1, results=[{"box": [0, 0, 1, 1]}]), identity)
    ws_server = _HubWebSocketServer(hub, host="127.0.0.1", port=0)
    hub._ws = ws_server
    client = _HubClient(_DummySocket(), "local", ws_server, max_queue=8,
                        send_timeout=1, lag_limit=100)
    ws_server._clients.append(client)
    captured = threading.Event()
    release = threading.Event()
    original = hub.snapshot_records

    def blocked_snapshot():
        records = original()
        captured.set()
        assert release.wait(3)
        return records

    monkeypatch.setattr(hub, "snapshot_records", blocked_snapshot)
    replay = threading.Thread(
        target=lambda: client.replace_subscription(_Subscription()))
    replay.start()
    assert captured.wait(2)
    published = threading.Thread(target=lambda: hub.publish_app(
        _app_payload(seq=2, results=[{"box": [2, 2, 3, 3]}]), identity))
    published.start()
    time.sleep(0.05)
    assert published.is_alive()  # waiting behind replay's publication fence
    release.set()
    replay.join(timeout=3)
    published.join(timeout=3)
    data = [item[2] for item in client._items if item[3]]
    assert len(data) == 1
    assert b'"seq":2' in data[0] and b'"seq":1' not in data[0]
    client.close()
    hub._ws = None


def test_ingress_queue_classifies_and_prioritizes_edges(tmp_path):
    hub = ResultHub(ws_port=0, system_uds_path=str(tmp_path / "system.sock"),
                    formatter=NoopFormatter(), ingress_queue=8)
    # Enable submit() without starting the consumer so the bounded queue can be
    # inspected deterministically.
    hub._ingress_thread = threading.current_thread()
    identity = _identity("queue-app", "q", 1)
    manifest = {"manifest_version": 2, "id": "queue-app", "render": {}}
    assert hub.refresh_app_manifest(identity, manifest)
    try:
        for index in range(8):
            assert hub.submit_app(
                _app_payload(seq=index, events=[{
                    "kind": "fall", "event_id": "edge-%s" % index}]),
                identity)
        assert hub.submit_app(
            _app_payload(seq=9, events=[{"kind": "metrics", "value": 9}]),
            identity) is False
        assert hub.submit_app(
            _app_payload(seq=10, events=[{
                "kind": "wake", "event_id": "edge-10"}]),
            identity) is False
        assert {item[3] for item in hub._ingress} == {"edge"}
        status = hub.status()["ingress"]
        assert status["state_dropped"] == 1
        assert status["edge_dropped"] == 1
    finally:
        hub.stop()

    hub = ResultHub(ws_port=0, system_uds_path=str(tmp_path / "system-2.sock"),
                    formatter=NoopFormatter(), ingress_queue=8)
    hub._ingress_thread = threading.current_thread()
    assert hub.refresh_app_manifest(identity, manifest)
    try:
        assert hub.submit_app(
            _app_payload(events=[{"kind": "metrics", "value": 1}]), identity)
        for index in range(7):
            assert hub.submit_app(
                _app_payload(seq=index + 20, events=[{
                    "kind": "fall", "event_id": "other-%s" % index}]),
                identity)
        assert hub.submit_app(
            _app_payload(seq=99, events=[{"kind": "transcript", "text": "hi"}]),
            identity)
        assert len(hub._ingress) == 8
        assert {item[3] for item in hub._ingress} == {"edge"}
    finally:
        hub.stop()


def test_observer_is_bounded_async_mutation_safe_and_failure_isolated(tmp_path):
    hub = ResultHub(ws_port=0, system_uds_path=str(tmp_path / "system.sock"),
                    formatter=NoopFormatter(), observer_queue=2)
    identity = _authorize(hub, _identity(), fields=[
        {"from": "results[].box", "coord": "pixel_xyxy"}])
    called = threading.Event()

    def observer(value):
        time.sleep(0.2)
        value["source"]["id"] = "mutated"
        called.set()
        raise RuntimeError("bridge failure")

    token = hub.add_observer(observer)
    started = time.monotonic()
    hub.publish_app(_app_payload(results=[{"box": [0, 0, 1, 1]}]), identity)
    assert time.monotonic() - started < 0.1
    assert called.wait(2)
    end = time.time() + 2
    while time.time() < end and not hub.status()["observers"]["errors"]:
        time.sleep(0.01)
    assert hub.status()["observers"]["errors"] == 1
    frame = next(record.raw for record in hub.snapshot_records()
                 if record.raw["type"] == "frame")
    assert frame["source"]["id"] == "demo"
    assert hub.remove_observer(token) is True


def test_observer_revoke_orders_inflight_old_generation_before_new(tmp_path):
    hub = ResultHub(ws_port=0, system_uds_path=str(tmp_path / "system.sock"),
                    formatter=NoopFormatter(), observer_queue=4)
    def osd_manifest():
        value = _manifest(
            "demo",
            fields=[{
                "name": "box", "from": "results[].box",
                "coord": "pixel_xyxy",
            }],
            render={
                "schema_version": 1,
                "boxes": {},
                "stream_osd": {"supported": ["boxes"], "default": False},
            })
        value["output"]["contract_version"] = 2
        return value

    old_identity = _identity(instance="old", generation=1)
    assert hub.refresh_app_manifest(
        old_identity, osd_manifest(), stream_contract=_managed_stream())

    class BlockingBridge:
        def __init__(self):
            self.started = threading.Event()
            self.release = threading.Event()
            self.lock = threading.Lock()
            self.visible = []
            self.revokes = []
            self.source_epoch = 0

        def observe(self, envelope):
            generation = envelope["source"]["generation"]
            with self.lock:
                captured_epoch = self.source_epoch
            if generation == 1:
                self.started.set()
                assert self.release.wait(3)
            with self.lock:
                if captured_epoch == self.source_epoch:
                    self.visible.append(generation)

        def invalidate_source(self, source_id, **details):
            with self.lock:
                self.source_epoch += 1
                self.visible.clear()
                self.revokes.append((source_id, details))

    bridge = BlockingBridge()
    token = hub.add_observer(bridge.observe)
    try:
        assert hub.publish_app(
            _app_payload(seq=1, results=[{"box": [1, 2, 3, 4]}]),
            old_identity)
        assert bridge.started.wait(2)
        # This second old-generation record remains queued behind the blocked
        # callback and must be purged by the refresh, not delivered later.
        assert hub.publish_app(
            _app_payload(seq=2, results=[{"box": [2, 3, 4, 5]}]),
            old_identity)

        new_identity = _identity(instance="new", generation=2)
        assert hub.refresh_app_manifest(
            new_identity,
            osd_manifest())
        # Revocation reaches the bridge before refresh returns.  The blocked
        # old callback will therefore fail its epoch check before side effects.
        with bridge.lock:
            assert len(bridge.revokes) == 1
            assert bridge.visible == []
        assert hub.publish_app(
            _app_payload(seq=3, results=[{"box": [3, 4, 5, 6]}]),
            new_identity)
        bridge.release.set()

        end = time.time() + 3
        while time.time() < end:
            with bridge.lock:
                if bridge.visible == [2] and bridge.revokes:
                    break
            time.sleep(0.01)
        with bridge.lock:
            assert bridge.visible == [2]
            assert len(bridge.revokes) == 1
            source_id, details = bridge.revokes[0]
            assert source_id == "demo"
            assert details["identity"]["instance"] == "new"
            assert details["identity"]["generation"] == 2
            assert details["capability"]["valid"] is True
            assert details["capability"]["render"]["stream_osd"] == {
                "supported": ["boxes"], "default": False}
            assert details["capability"]["stream"] == {
                "id": "", "kind": "none"}
        observer_status = hub.status()["observers"]
        assert observer_status["invalidations"] == 1
        assert observer_status["invalidated_queued"] >= 1
    finally:
        bridge.release.set()
        hub.remove_observer(token)


class _WsReader:
    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=3)
        self.buffer = bytearray()
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((
            "GET /ws/ai/results/v2 HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        response = bytearray()
        while b"\r\n\r\n" not in response:
            response.extend(self.sock.recv(2048))
        headers, remainder = bytes(response).split(b"\r\n\r\n", 1)
        assert headers.startswith(b"HTTP/1.1 101")
        self.buffer.extend(remainder)

    def _exact(self, size):
        while len(self.buffer) < size:
            self.buffer.extend(self.sock.recv(max(2048, size - len(self.buffer))))
        value = bytes(self.buffer[:size])
        del self.buffer[:size]
        return value

    def json(self):
        first = self._exact(2)
        assert first[0] & 0x0F == 0x1
        size = first[1] & 0x7F
        if size == 126:
            size = struct.unpack(">H", self._exact(2))[0]
        elif size == 127:
            size = struct.unpack(">Q", self._exact(8))[0]
        return json.loads(self._exact(size).decode("utf-8"))

    def send(self, value):
        payload = json.dumps(value, separators=(",", ":")).encode()
        mask = os.urandom(4)
        header = bytearray([0x81])
        if len(payload) < 126:
            header.append(0x80 | len(payload))
        else:
            header.append(0x80 | 126)
            header.extend(struct.pack(">H", len(payload)))
        encoded = bytes(byte ^ mask[index & 3]
                        for index, byte in enumerate(payload))
        self.sock.sendall(bytes(header) + mask + encoded)

    def close(self):
        self.sock.close()


def test_ws_receive_only_client_stays_connected_past_send_timeout(tmp_path):
    send_timeout = 0.1
    hub = ResultHub(
        ws_port=0, system_uds_path=str(tmp_path / "system.sock"),
        formatter=NoopFormatter()).start()
    # No client has connected yet, so the accept loop will copy this value into
    # the new _HubClient.  Keep the production default out of this regression's
    # wall-clock duration.
    hub._ws.send_timeout = send_timeout
    identity = _authorize(hub, _identity("receive-only"), fields=[
        {"from": "results[].box", "coord": "pixel_xyxy"}])
    ws = _WsReader(hub.ws_port)
    ws.sock.settimeout(2)
    try:
        assert ws.json()["type"] == "hello"
        assert ws.json()["type"] == "snapshot"
        started = time.monotonic()
        seq = 0
        while time.monotonic() - started <= 3 * send_timeout:
            seq += 1
            hub.publish_app(_app_payload(
                seq=seq, results=[{"box": [0, 0, 20, 20]}]), identity)
            frame = ws.json()
            assert frame["type"] == "frame" and frame["seq"] == seq
            time.sleep(send_timeout / 5)

        # The browser sent only its HTTP upgrade; it did not need to send a
        # subscribe/ping frame to keep the server-side blocking recv alive.
        assert time.monotonic() - started > 2 * send_timeout
        assert hub._ws.status()["subscribers"] == 1
        with hub._ws._lock:
            server_client = hub._ws._clients[0]
        assert server_client.conn.gettimeout() is None
    finally:
        ws.close()
        hub.stop()


def test_ws_send_timeout_closes_nonreading_socket_and_unblocks_reader():
    writer_sock, nonreader_sock = socket.socketpair()
    writer_sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    client = _HubClient(
        writer_sock, "local", None, max_queue=4,
        send_timeout=0.05, lag_limit=100)
    # Much larger than the deliberately tiny kernel send buffer.  With no peer
    # reader, the strict send deadline must close both Hub threads.
    assert client._append(
        "event", "slow-peer", b"x" * (4 * 1024 * 1024), data=True,
        delivery="edge")
    started = time.monotonic()
    try:
        client.start()
        client._writer.join(timeout=2)
        client._reader.join(timeout=2)
        assert not client._writer.is_alive()
        assert not client._reader.is_alive()
        assert client.alive() is False
        assert time.monotonic() - started < 2
    finally:
        client.close()
        nonreader_sock.close()


def test_ws_send_timeout_is_total_deadline_against_trickle_reader():
    writer_sock, trickle_sock = socket.socketpair()
    writer_sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    send_timeout = 0.05
    client = _HubClient(
        writer_sock, "local", None, max_queue=4,
        send_timeout=send_timeout, lag_limit=100)
    assert client._append(
        "event", "trickle-peer", b"x" * (1024 * 1024), data=True,
        delivery="edge")
    received = bytearray()

    def read_slowly():
        try:
            while True:
                chunk = trickle_sock.recv(8192)
                if not chunk:
                    return
                received.extend(chunk)
                time.sleep(0.02)
        except OSError:
            return

    reader = threading.Thread(target=read_slowly, daemon=True)
    reader.start()
    started = time.monotonic()
    try:
        client.start()
        client._writer.join(timeout=1)
        client._reader.join(timeout=1)
        elapsed = time.monotonic() - started
        assert not client._writer.is_alive()
        assert not client._reader.is_alive()
        assert client.alive() is False
        assert received
        assert len(received) < 1024 * 1024
        assert send_timeout <= elapsed < 0.5
    finally:
        client.close()
        trickle_sock.close()
        reader.join(timeout=1)


def test_ws_first_message_hello_then_raw_and_formatted_subscription(tmp_path):
    hub = ResultHub(
        ws_port=0, system_uds_path=str(tmp_path / "system.sock"),
        system_identity_resolver=lambda *_args: {"id": "builtin"},
        formatter=EchoFormatter()).start()
    visible = _authorize(hub, _identity("visible"), fields=[
        {"from": "results[].box", "coord": "pixel_xyxy"}])
    ws = _WsReader(hub.ws_port)
    ws.sock.settimeout(3)
    try:
        hello = ws.json()
        assert hello["type"] == "hello"
        assert hello["schema"] == SCHEMA and hello["schema_version"] == 2
        assert hello["extensions"]["views"] == ["formatted", "raw"]
        assert ws.json()["type"] == "snapshot"

        hub.publish_app(_app_payload(
            results=[{"box": [0, 0, 20, 20]}]), visible)
        raw = ws.json()
        assert raw["type"] == "frame" and raw["source"]["id"] == "visible"
        assert raw["results"]

        ws.send({"type": "subscribe", "view": "formatted",
                 "sources": ["visible"], "types": ["frame"]})
        snapshot = ws.json()
        assert snapshot["type"] == "snapshot"
        assert snapshot["extensions"]["view"] == "formatted"
        formatted = ws.json()
        assert formatted["type"] == "formatted"
        assert formatted["extensions"]["raw_type"] == "frame"
        assert formatted["profile"] == "test"
        assert formatted["payload"].startswith("formatted:batch:visible:")
        assert formatted["extensions"]["projection"] == \
            "authenticated_ingress_batch"
    finally:
        ws.close()
        hub.stop()


def test_ws_initial_replay_delivers_default_full_edge_ring(tmp_path):
    hub = ResultHub(
        ws_port=0, system_uds_path=str(tmp_path / "system.sock"),
        formatter=NoopFormatter(), event_ttl_s=120).start()
    identity = _authorize(hub, _identity("edge-replay"), camera=False)
    ws = None
    try:
        for index in range(128):
            assert hub.publish_app(_app_payload(
                seq=index + 1,
                events=[{"kind": "fall", "event_id": "fall-%s" % index}],
            ), identity)
        assert hub.status()["event_replay"]["edge"] == 128

        ws = _WsReader(hub.ws_port)
        ws.sock.settimeout(5)
        assert ws.json()["type"] == "hello"
        snapshot = ws.json()
        assert snapshot["type"] == "snapshot"
        assert snapshot["extensions"]["records"] == 128
        replay = [ws.json() for _index in range(128)]
        assert all(item["type"] == "event" for item in replay)
        assert {item["events"][0]["producer_event_id"] for item in replay} == {
            "fall-%s" % index for index in range(128)}
        assert hub.status()["client_edge_dropped"] == 0
    finally:
        if ws is not None:
            ws.close()
        hub.stop()


def test_formatted_worker_never_blocks_ingress_and_renders_edge_batch_once(tmp_path):
    formatter = BlockingEchoFormatter()
    hub = ResultHub(
        ws_port=0, system_uds_path=str(tmp_path / "system.sock"),
        system_identity_resolver=lambda *_args: {"id": "builtin"},
        formatter=formatter).start()
    identity = _authorize(hub, _identity("async-app"), fields=[
        {"from": "results[].box", "coord": "pixel_xyxy"}])
    ws = _WsReader(hub.ws_port)
    ws.sock.settimeout(3)
    try:
        assert ws.json()["type"] == "hello"
        assert ws.json()["type"] == "snapshot"
        ws.send({"type": "subscribe", "view": "formatted",
                 "sources": ["async-app"],
                 "types": ["frame", "event", "status"]})
        assert ws.json()["type"] == "snapshot"

        started = time.monotonic()
        published = hub.publish_app(_app_payload(
            results=[{"box": [0, 0, 20, 20]}],
            events=[{"kind": "fall", "event_id": 1}]), identity)
        elapsed = time.monotonic() - started
        assert {value["type"] for value in published} == {"frame", "event"}
        assert elapsed < 0.1
        assert formatter.started.wait(2)
        # The template worker is intentionally still blocked, yet raw ingress
        # and the generation fence have already returned to the publisher.
        assert formatter.calls == 1
        formatter.release.set()
        formatted = ws.json()
        assert formatted["type"] == "formatted"
        assert formatted["extensions"]["batch_types"] == ["frame", "event"]
        assert formatted["extensions"]["delivery"] == "edge"
        assert formatter.calls == 1
    finally:
        formatter.release.set()
        ws.close()
        hub.stop()


def _recv_line(sock):
    data = bytearray()
    while not data.endswith(b"\n"):
        data.extend(sock.recv(1))
    return json.loads(bytes(data).decode("utf-8"))


def test_system_uds_requires_exact_hello_and_resolver_then_normalizes(tmp_path):
    resolver_calls = []

    def resolve(peer_pid, hello):
        resolver_calls.append((peer_pid, hello))
        return {"kind": "builtin", "id": "builtin", "trust": "test"}

    hub = ResultHub(ws_port=0, system_uds_path=str(tmp_path / "system.sock"),
                    system_identity_resolver=resolve,
                    formatter=EchoFormatter()).start()
    bad = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    good = None
    ws = None
    try:
        bad.connect(hub.system_uds_path)
        bad.sendall(json.dumps({
            "type": "hello", "protocol": SYSTEM_PROTOCOL, "source": "builtin",
            "extra": True,
        }).encode() + b"\n")
        assert _recv_line(bad)["ok"] is False
        bad.close()

        ws = _WsReader(hub.ws_port)
        ws.sock.settimeout(3)
        assert ws.json()["type"] == "hello"
        assert ws.json()["type"] == "snapshot"
        good = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        good.connect(hub.system_uds_path)
        good.sendall(json.dumps({
            "type": "hello", "protocol": SYSTEM_PROTOCOL, "source": "builtin",
        }).encode() + b"\n")
        ack = _recv_line(good)
        assert ack == {"type": "hello_ack", "protocol": SYSTEM_PROTOCOL, "ok": True}
        good.sendall(json.dumps(_system_payload()).encode() + b"\n")
        frame = ws.json()
        assert frame["type"] == "frame"
        assert frame["source"]["kind"] == "builtin"
        assert frame["source"]["id"] == "builtin"
        assert frame["results"][0]["space"] == "normalized_xyxy"
        assert resolver_calls and resolver_calls[-1][0] == os.getpid()
    finally:
        bad.close()
        if good is not None:
            good.close()
        if ws is not None:
            ws.close()
        hub.stop()
    assert not os.path.exists(hub.system_uds_path)


def test_production_system_identity_resolver_checks_pidfile_proc_and_exact_module(
        tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    pid_dir = proc / "42"
    pid_dir.mkdir(parents=True)
    owner = os.getuid()
    (pid_dir / "status").write_text(
        "Name:\tpython3\nUid:\t%d\t%d\t%d\t%d\n" % ((owner,) * 4))
    (pid_dir / "cmdline").write_bytes(
        b"/usr/bin/python3\0-m\0recamera_notify.notify_server\0")
    (pid_dir / "stat").write_text(
        "42 (python3) " + " ".join(["S"] + ["0"] * 18 + ["12345"]) + "\n")
    (pid_dir / "exe").symlink_to("/usr/bin/python3.11")
    pidfile = tmp_path / "notify_server.pid"
    pidfile.write_text("42\n")
    pidfile.chmod(0o600)
    hello = {"type": "hello", "protocol": SYSTEM_PROTOCOL, "source": "builtin"}
    kwargs = {"proc_root": str(proc), "pidfile": str(pidfile),
              "owner_uid": owner}
    identity = resolve_builtin_notify_identity(42, hello, **kwargs)
    assert identity["id"] == "builtin" and identity["starttime"] == 12345

    # A second same-UID process with byte-identical argv cannot authenticate:
    # its SO_PEERCRED PID is not the supervisor-owned pidfile PID.
    impostor = proc / "43"
    impostor.mkdir()
    (impostor / "status").write_text((pid_dir / "status").read_text())
    (impostor / "cmdline").write_bytes((pid_dir / "cmdline").read_bytes())
    (impostor / "stat").write_text(
        "43 (python3) " + " ".join(["S"] + ["0"] * 18 + ["54321"]) + "\n")
    (impostor / "exe").symlink_to("/usr/bin/python3.11")
    assert resolve_builtin_notify_identity(43, hello, **kwargs) is None
    pidfile.chmod(0o622)
    assert resolve_builtin_notify_identity(42, hello, **kwargs) is None
    pidfile.chmod(0o600)
    (pid_dir / "cmdline").write_bytes(
        b"/usr/bin/python3\0/tmp/notify_server.py\0")
    assert resolve_builtin_notify_identity(42, hello, **kwargs) is None
    (pid_dir / "cmdline").write_bytes(
        b"/usr/bin/python3\0-m\0recamera_notify.notify_server\0")

    starts = iter((12345, 99999))
    monkeypatch.setattr(result_hub_module, "_proc_starttime",
                        lambda _path: next(starts))
    assert resolve_builtin_notify_identity(42, hello, **kwargs) is None

    link = tmp_path / "notify-link.pid"
    link.symlink_to(pidfile)
    assert resolve_builtin_notify_identity(
        42, hello, proc_root=str(proc), pidfile=str(link), owner_uid=owner) is None


def test_formatted_view_reads_app_effective_config_and_restricted_builtin_template(
        tmp_path, monkeypatch):
    apps = tmp_path / "apps"
    appdata = tmp_path / "appdata"
    app_dir = apps / "fmt-app"
    app_dir.mkdir(parents=True)
    monkeypatch.setattr(paths, "APPS_DIR", str(apps))
    monkeypatch.setattr(paths, "APPDATA_DIR", str(appdata))
    manifest = {
        "id": "fmt-app", "name": "Formatter", "capabilities": ["output"],
        "config_schema": {"groups": []},
        "output": {
            "default_channel": ["ws"], "default_mode": "custom",
            "templates": {"detection": "{{ detection.count | tojson }}"},
        },
    }
    (app_dir / "manifest.json").write_text(json.dumps(manifest))
    notify = tmp_path / "notify.json"
    notify.write_text(json.dumps({
        "dTemplate": {"sDetection": "objects={{ detection.count }}"}}))
    formatter = ResultViewFormatter(notify_config=str(notify))

    raw = normalize_app_payload(
        _app_payload(results=[{"box": [0, 0, 1, 1]}]),
        _identity("fmt-app"))[0]
    assert formatter.format(raw)[0]["payload"] == "1"
    appconfig.write_user_config("fmt-app", {
        "iMode": "custom",
        "dTemplate": {"sDetection": "{{ (detection.count + 10) | tojson }}"},
    })
    raw2 = normalize_app_payload(
        _app_payload(seq=2, results=[{"box": [0, 0, 1, 1]}]),
        _identity("fmt-app"))[0]
    assert formatter.format(raw2)[0]["payload"] == "11"

    builtin = normalize_system_payload(
        _system_payload(seq=1), {"id": "builtin", "trust": "test"})[0]
    built = formatter.format(builtin)[0]
    assert built["payload"] == "objects=1"
    assert built["content_type"].startswith("text/plain")
    # The same restricted sandbox used by Kit rejects access to object globals
    # and falls back to compact JSON; it can never replace the raw envelope.
    notify.write_text(json.dumps({"dTemplate": {
        "sDetection": "{{ cycler.__init__.__globals__.os.system('id') }}"}}))
    os.utime(notify, None)
    fallback = formatter.format(builtin)[0]
    assert fallback["profile"] == "builtin:raw-fallback"
    assert json.loads(fallback["payload"])["task_type_name"] == "detection"
    assert builtin["type"] == "frame" and builtin["results"]

    # Builtin uses the same arithmetic/resource policy; the raw canonical
    # frame remains available even when formatted projection is rejected.
    notify.write_text(json.dumps({"dTemplate": {
        "sDetection": "{{ 'x' * 20000000 }}"}}))
    os.utime(notify, None)
    started = time.monotonic()
    repeat_fallback = formatter.format(builtin)[0]
    assert time.monotonic() - started < 1.0
    assert repeat_fallback["profile"] == "builtin:raw-fallback"
    assert builtin["type"] == "frame" and builtin["results"]


def test_formatted_projection_renders_complete_legacy_batch_once(tmp_path, monkeypatch):
    apps = tmp_path / "apps"
    appdata = tmp_path / "appdata"
    app_dir = apps / "batch-app"
    app_dir.mkdir(parents=True)
    monkeypatch.setattr(paths, "APPS_DIR", str(apps))
    monkeypatch.setattr(paths, "APPDATA_DIR", str(appdata))
    manifest = _manifest("batch-app", fields=[
        {"from": "results[].box", "coord": "pixel_xyxy"}], render={
            "schema_version": 1, "boxes": {"label": "label"}})
    manifest.update({
        "name": "Batch formatter", "capabilities": ["output"],
        "config_schema": {"groups": []},
    })
    manifest["output"].update({
        "default_channel": ["ws"], "default_mode": "custom",
        "templates": {"detection": (
            "{{ results|length }}|{{ events|length }}|{{ summary.state }}|"
            "{{ inference_time_ms }}|{{ pipeline_ms }}|{{ stream_id }}")},
    })
    (app_dir / "manifest.json").write_text(json.dumps(manifest))
    hub = ResultHub(ws_port=0, system_uds_path=str(tmp_path / "system.sock"),
                    formatter=ResultViewFormatter())
    identity = _identity("batch-app")
    assert hub.refresh_app_manifest(
        identity, manifest, stream_contract=_managed_stream())
    published = hub.publish_app(_app_payload(
        results=[{"box": [1, 2, 30, 40], "label": "person"}],
        events=[{"kind": "fall", "event_id": 7}],
        summary={"state": "alarm"}, inference_time_ms=12.5,
        pipeline_ms=20.0), identity)
    assert {value["type"] for value in published} == {"frame", "event", "status"}
    records = hub.snapshot_records()
    assert len({record.batch_id for record in records}) == 1
    for record in records:
        hub.ensure_formatted(record)
    assert hub.status()["formatted_batches"] == 1
    formatted = records[0].formatted[0]
    assert formatted["payload"] == "1|1|alarm|12.5|20.0|main"
    assert formatted["render"] == appvisualization.effective_render(manifest)
    assert formatted["extensions"]["batch_types"] == [
        "frame", "status", "event"]


def test_result_hub_status_api(tmp_path, monkeypatch):
    class FakeHub:
        @staticmethod
        def status():
            return {"running": True, "schema": SCHEMA,
                    "schema_version": SCHEMA_VERSION, "ws_port": 8125}

    monkeypatch.setattr(server, "_result_hub_instance", FakeHub())
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server._Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", httpd.server_port,
                                                timeout=3)
        connection.request("GET", "/api/app-center/v1/results/status")
        response = connection.getresponse()
        value = json.loads(response.read())
        assert response.status == 200
        assert value["running"] is True and value["ws_port"] == 8125
        connection.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)

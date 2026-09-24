"""Device-level browser display override (render-override) coverage."""
from __future__ import annotations

import json
import os
import sys
import tarfile
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
from appmgr import (installer, manifest as appmanifest, paths,  # noqa: E402
                    render_override, server, state, supervisor, visualization)
from appmgr.result_hub import (ResultHub, _HubWebSocketServer,  # noqa: E402
                               _Subscription)


APP_ID = "overlay-app"


@pytest.fixture
def layout(tmp_path, monkeypatch):
    apps = tmp_path / "apps"
    appmgr = tmp_path / "appmgr"
    appdata = tmp_path / "appdata"
    venvs = tmp_path / "venvs"
    for directory in (apps, appmgr, appdata, venvs):
        directory.mkdir()
    monkeypatch.setattr(paths, "APPS_DIR", str(apps))
    monkeypatch.setattr(paths, "APPMGR_DIR", str(appmgr))
    monkeypatch.setattr(paths, "APPDATA_DIR", str(appdata))
    monkeypatch.setattr(paths, "VENVS_DIR", str(venvs))
    monkeypatch.setattr(paths, "STATE_FILE", str(apps / "state.json"))
    monkeypatch.setattr(paths, "ALLOWED_PKG_ROOTS", (str(tmp_path),))
    monkeypatch.setattr(paths, "REQUIRE_SIGNATURE", False)
    monkeypatch.delenv("APPMGR_RENDER_OVERRIDES_DIR", raising=False)
    monkeypatch.setattr(server, "_result_hub_instance", None)
    monkeypatch.setattr(server, "_coordinator_instance", None)
    monkeypatch.setattr(server, "_coordinator_layout", None)
    # Real _audit: it writes under the tmp APPMGR_DIR, and a stub here once
    # hid a TypeError from duplicate keyword arguments.
    if server._operation_manager_instance is not None:
        server._operation_manager_instance.close()
    monkeypatch.setattr(server, "_operation_manager_instance", None)
    monkeypatch.setattr(server, "_operation_manager_layout", None)
    server.cache_clear()
    yield tmp_path
    if server._operation_manager_instance is not None:
        server._operation_manager_instance.close()
        server._operation_manager_instance = None


def _render():
    return {
        "schema_version": 1,
        "boxes": {"color_by": "label", "label": "label", "line_width": 2},
        "events": {"transcript": {"as": "subtitle", "position": "bottom",
                                  "duration_sec": 4}},
    }


def _manifest(app_id=APP_ID, render=None):
    return {"manifest_version": 2, "id": app_id, "version": "1.0.0",
            "render": render if render is not None else _render()}


def _install_dir(app_id=APP_ID, manifest=None):
    directory = paths.app_dir(app_id)
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "manifest.json"), "w") as stream:
        json.dump(manifest or _manifest(app_id), stream)
    server.cache_clear()
    return directory


VALID = {
    "boxes": {"color_by": "attributes.class_name", "label": "label",
              "colors": {"person": "#FF0000", "0": "#00ff0080"},
              "line_width": 4},
    "subtitles": {"font_size": 24, "color": "#FFFFFF",
                  "background_color": "#000000", "background_opacity": 0.5,
                  "position": "bottom-center", "max_lines": 2,
                  "duration_sec": 3.5},
    "events": {"transcript": {"as": "subtitle", "text": "{{ text }}",
                              "position": "top-left", "duration_sec": 0.1,
                              "max_lines": 10}},
}


# --------------------------------------------------------------------------
# validation


def test_valid_override_round_trips_as_deep_copy():
    clean = render_override.validate(VALID)
    assert clean == VALID
    clean["boxes"]["colors"]["person"] = "#000000"
    assert VALID["boxes"]["colors"]["person"] == "#FF0000"


@pytest.mark.parametrize("override,field", [
    ([], "override"),
    ({"stream_osd": {}}, "stream_osd"),
    ({"schema_version": 1}, "schema_version"),
    ({"geometry": {"max_items": 1}}, "geometry"),
    ({"x-extra": 1}, "x-extra"),
    ({"boxes": {"coordinate_space": "pixel"}}, "boxes.coordinate_space"),
    ({"boxes": {"x-a": 1}}, "boxes.x-a"),
    ({"subtitles": {"shadow": True}}, "subtitles.shadow"),
    ({"subtitles": {"x-a": 1}}, "subtitles.x-a"),
    ({"events": {"t": {"fields": ["a"]}}}, "events.t.fields"),
    ({"boxes": []}, "boxes"),
    ({"events": []}, "events"),
    ({"events": {"t": "subtitle"}}, "events.t"),
])
def test_unknown_or_forbidden_fields_are_rejected(override, field):
    with pytest.raises(render_override.RenderOverrideError) as exc:
        render_override.validate(override)
    assert exc.value.field == field


@pytest.mark.parametrize("override,field", [
    # boolean is never a number
    ({"boxes": {"line_width": True}}, "boxes.line_width"),
    ({"subtitles": {"font_size": True}}, "subtitles.font_size"),
    ({"subtitles": {"background_opacity": False}}, "subtitles.background_opacity"),
    ({"subtitles": {"max_lines": True}}, "subtitles.max_lines"),
    ({"events": {"t": {"duration_sec": True}}}, "events.t.duration_sec"),
    # ranges / integer-ness
    ({"boxes": {"line_width": 0}}, "boxes.line_width"),
    ({"boxes": {"line_width": 17}}, "boxes.line_width"),
    ({"boxes": {"line_width": 2.5}}, "boxes.line_width"),
    ({"subtitles": {"font_size": 7}}, "subtitles.font_size"),
    ({"subtitles": {"font_size": 73}}, "subtitles.font_size"),
    ({"subtitles": {"font_size": 12.0}}, "subtitles.font_size"),
    ({"subtitles": {"background_opacity": 1.01}}, "subtitles.background_opacity"),
    ({"subtitles": {"background_opacity": float("nan")}},
     "subtitles.background_opacity"),
    ({"subtitles": {"max_lines": 11}}, "subtitles.max_lines"),
    ({"subtitles": {"duration_sec": 0.05}}, "subtitles.duration_sec"),
    ({"subtitles": {"duration_sec": 61}}, "subtitles.duration_sec"),
    ({"subtitles": {"duration_sec": "3"}}, "subtitles.duration_sec"),
    ({"events": {"t": {"max_lines": 0}}}, "events.t.max_lines"),
    # enums
    ({"subtitles": {"position": "bottom"}}, "subtitles.position"),
    ({"events": {"t": {"position": "middle"}}}, "events.t.position"),
    ({"events": {"t": {"as": "popup"}}}, "events.t.as"),
    # strings
    ({"events": {"t": {"text": "x" * 1025}}}, "events.t.text"),
    ({"events": {"t": {"text": 5}}}, "events.t.text"),
    ({"boxes": {"color_by": ""}}, "boxes.color_by"),
    ({"boxes": {"color_by": "a" * 65}}, "boxes.color_by"),
    ({"boxes": {"label": "a b"}}, "boxes.label"),
    ({"boxes": {"label": 3}}, "boxes.label"),
])
def test_invalid_values_are_rejected(override, field):
    with pytest.raises(render_override.RenderOverrideError) as exc:
        render_override.validate(override)
    assert exc.value.field == field


@pytest.mark.parametrize("color", [
    "red", "#FFF", "#GGGGGG", "#FFFFFFF", "#FFFFFFFFF", "FFFFFF", "#FFFFFF\n",
    None, 16777215,
])
def test_color_format_is_strict(color):
    with pytest.raises(render_override.RenderOverrideError):
        render_override.validate({"subtitles": {"color": color}})
    with pytest.raises(render_override.RenderOverrideError):
        render_override.validate({"boxes": {"colors": {"a": color}}})


@pytest.mark.parametrize("override", [
    {"boxes": {"color_by": "__proto__"}},
    {"boxes": {"label": "constructor"}},
    {"boxes": {"color_by": "attributes.__proto__"}},
    {"boxes": {"label": "a.prototype"}},
    {"boxes": {"colors": {"__proto__": "#FFFFFF"}}},
    {"boxes": {"colors": {"constructor": "#FFFFFF"}}},
    {"events": {"__proto__": {"as": "none"}}},
    {"events": {"constructor": {"as": "none"}}},
])
def test_prototype_property_names_are_rejected(override):
    with pytest.raises(render_override.RenderOverrideError):
        render_override.validate(override)


def test_color_table_limits():
    table = {"k%d" % index: "#000000" for index in range(64)}
    assert render_override.validate({"boxes": {"colors": table}})
    table["k64"] = "#000000"
    with pytest.raises(render_override.RenderOverrideError):
        render_override.validate({"boxes": {"colors": table}})
    with pytest.raises(render_override.RenderOverrideError):
        render_override.validate({"boxes": {"colors": {"k" * 129: "#000000"}}})
    assert render_override.validate({"boxes": {"colors": {"k" * 128: "#000000"}}})


def test_merge_is_leafwise_colors_replace_and_events_merge_by_field():
    render = {
        "schema_version": 1,
        "boxes": {"color_by": "label", "line_width": 2,
                  "colors": {"a": "#111111", "b": "#222222"}},
        "stream_osd": {"supported": ["boxes"], "default": False},
        "events": {"t": {"as": "toast", "position": "bottom", "text": "x"},
                   "other": {"as": "badge"}},
        "subtitles": {"font_size": 12, "color": "#FFFFFF"},
    }
    merged = render_override.merge(render, {
        "boxes": {"line_width": 5, "colors": {"c": "#333333"}},
        "events": {"t": {"as": "subtitle"}, "new": {"as": "panel"}},
        "subtitles": {"font_size": 30},
    })
    assert merged["boxes"] == {"color_by": "label", "line_width": 5,
                               "colors": {"c": "#333333"}}
    assert merged["events"] == {
        "t": {"as": "subtitle", "position": "bottom", "text": "x"},
        "other": {"as": "badge"}, "new": {"as": "panel"}}
    assert merged["subtitles"] == {"font_size": 30, "color": "#FFFFFF"}
    assert merged["stream_osd"] == render["stream_osd"]
    assert render["boxes"]["line_width"] == 2


def test_effective_render_ignores_forbidden_override_keys():
    manifest = _manifest()
    effective = visualization.effective_render(manifest, override={
        "stream_osd": {"supported": ["boxes"], "default": True},
        "schema_version": 2,
        "boxes": {"line_width": 9, "coordinate_space": "x"},
    })
    assert effective["schema_version"] == 1
    assert "stream_osd" not in effective
    assert effective["boxes"]["line_width"] == 9
    assert "coordinate_space" not in effective["boxes"]


def test_sanitize_keeps_valid_leaves_and_reports_dropped():
    clean, dropped = render_override.sanitize({
        "boxes": {"line_width": 99, "label": "name"},
        "subtitles": {"color": "#FFFFFF", "font_size": True},
        "events": {"t": {"as": "subtitle", "text": 5}, "bad kind": {"as": "x"}},
        "stream_osd": {},
    })
    assert clean == {"boxes": {"label": "name"},
                     "subtitles": {"color": "#FFFFFF"},
                     "events": {"t": {"as": "subtitle"}}}
    assert sorted(dropped) == sorted([
        "stream_osd", "boxes.line_width", "subtitles.font_size",
        "events.t.text", "events.bad kind.as"])


# --------------------------------------------------------------------------
# manifest v2 schema sync


def test_manifest_v2_accepts_colors_and_subtitles_with_same_rules():
    render = {"schema_version": 1,
              "boxes": {"colors": {"person": "#FF0000"}},
              "subtitles": {"font_size": 20, "position": "top-right",
                            "background_opacity": 0.25, "x-note": "ok"}}
    appmanifest._validate_render_contract(render)
    for bad in (
        {"schema_version": 1, "boxes": {"colors": {"a": "red"}}},
        {"schema_version": 1, "boxes": {"colors": {"__proto__": "#FFFFFF"}}},
        {"schema_version": 1, "subtitles": {"font_size": True}},
        {"schema_version": 1, "subtitles": {"position": "bottom"}},
        {"schema_version": 1, "subtitles": {"unknown": 1}},
    ):
        with pytest.raises(appmanifest.ManifestValidationError):
            appmanifest._validate_render_contract(bad)


def test_published_manifest_schema_matches_runtime_for_new_render_fields():
    jsonschema = pytest.importorskip("jsonschema")
    with open(os.path.join(os.path.dirname(appmanifest.__file__), "schema",
                           "manifest-v2.schema.json")) as stream:
        schema = json.load(stream)
    render_schema = dict(schema["$defs"]["renderV1"])
    render_schema["$defs"] = schema["$defs"]
    validator = jsonschema.Draft202012Validator(render_schema)
    good = {"schema_version": 1,
            "boxes": {"colors": {"person": "#FF0000"}},
            "subtitles": {"font_size": 20, "position": "top-right"}}
    assert not list(validator.iter_errors(good))
    for bad in (
        {"schema_version": 1, "boxes": {"colors": {"a": "red"}}},
        {"schema_version": 1, "boxes": {"colors": {"__proto__": "#FFFFFF"}}},
        {"schema_version": 1, "subtitles": {"font_size": 100}},
        {"schema_version": 1, "subtitles": {"position": "bottom"}},
        {"schema_version": 1, "subtitles": {"unknown": 1}},
    ):
        assert list(validator.iter_errors(bad)), bad


# --------------------------------------------------------------------------
# storage + HTTP API


def _serve():
    httpd = server._AppHTTPServer(("127.0.0.1", 0), server._Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def _raw_request(httpd, method, path, body=None):
    import http.client
    connection = http.client.HTTPConnection(
        "127.0.0.1", httpd.server_port, timeout=5)
    try:
        headers = {"Content-Type": "application/json"}
        payload = None if body is None else json.dumps(body)
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def test_http_get_put_delete_shapes_revision_and_errors(layout, capsys):
    _install_dir()
    route = "/api/app-center/v1/apps/%s/render-override" % APP_ID
    httpd = _serve()
    try:
        status, body = _raw_request(httpd, "GET", route)
        assert status == 200
        assert body == {
            "app_id": APP_ID, "schema_version": 1, "revision": 0,
            "override": {}, "defaults": _render(), "effective": _render(),
        }

        status, body = _raw_request(httpd, "PUT", route, {
            "revision": 0, "override": VALID})
        assert status == 200
        assert body["revision"] == 1
        assert body["override"] == VALID
        assert body["defaults"] == _render()
        assert body["effective"]["boxes"] == {
            "color_by": "attributes.class_name", "label": "label",
            "line_width": 4, "colors": VALID["boxes"]["colors"]}
        assert body["effective"]["events"]["transcript"] == {
            "as": "subtitle", "position": "top-left", "duration_sec": 0.1,
            "text": "{{ text }}", "max_lines": 10}
        assert body["effective"]["subtitles"] == VALID["subtitles"]
        with open(render_override.override_path(APP_ID)) as stream:
            assert json.load(stream) == {
                "schema_version": 1, "revision": 1, "override": VALID}
        status, fetched = _raw_request(httpd, "GET", route)
        assert fetched == body
        with capsys.disabled():
            print("\nGET_RESPONSE_SAMPLE=" + json.dumps(fetched, sort_keys=True))

        status, body = _raw_request(httpd, "PUT", route, {
            "revision": 0, "override": {}})
        assert (status, body) == (409, {"error": "revision_conflict",
                                        "current_revision": 1})

        status, body = _raw_request(httpd, "PUT", route, {
            "revision": 1, "override": {"boxes": {"line_width": True}}})
        assert status == 400
        assert body["error"] == "invalid_override"
        assert body["field"] == "boxes.line_width"
        assert isinstance(body["detail"], str)

        for payload, field in (
            ({"revision": 1, "override": {}, "extra": 1}, "extra"),
            ({"override": {}}, "revision"),
            ({"revision": True, "override": {}}, "revision"),
            ({"revision": "1", "override": {}}, "revision"),
            ({"revision": 1}, "override"),
        ):
            status, body = _raw_request(httpd, "PUT", route, payload)
            assert status == 400 and body["field"] == field, payload
        assert render_override.load(APP_ID)["revision"] == 1

        status, body = _raw_request(httpd, "DELETE", route + "?revision=0")
        assert (status, body) == (409, {"error": "revision_conflict",
                                        "current_revision": 1})
        for query in ("", "?revision=", "?revision=-1", "?revision=a",
                      "?revision=1&revision=1"):
            status, body = _raw_request(httpd, "DELETE", route + query)
            assert status == 400 and body["field"] == "revision", query

        status, body = _raw_request(httpd, "DELETE", route + "?revision=1")
        assert status == 200
        assert body == {
            "app_id": APP_ID, "schema_version": 1, "revision": 2,
            "override": {}, "defaults": _render(), "effective": _render(),
        }

        missing = "/api/app-center/v1/apps/not-installed/render-override"
        status, body = _raw_request(httpd, "GET", missing)
        assert status == 404 and "error" in body
        status, body = _raw_request(httpd, "PUT", missing,
                                    {"revision": 0, "override": {}})
        assert status == 404
        status, body = _raw_request(httpd, "DELETE", missing + "?revision=0")
        assert status == 404
        assert not os.path.exists(render_override.override_path("not-installed"))
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_tampered_store_never_reaches_effective_render(layout):
    _install_dir()
    os.makedirs(paths.render_overrides_dir())
    with open(render_override.override_path(APP_ID), "w") as stream:
        json.dump({"schema_version": 1, "revision": 3, "override": {
            "boxes": {"line_width": 3, "coordinate_space": "x"},
            "stream_osd": {"supported": ["boxes"], "default": True},
        }}, stream)
    view = server.do_get_render_override(APP_ID)
    assert view["revision"] == 3
    assert view["override"] == {"boxes": {"line_width": 3}}
    assert "stream_osd" not in view["effective"]


def test_env_override_relocates_store(layout, monkeypatch, tmp_path):
    target = tmp_path / "elsewhere"
    monkeypatch.setenv("APPMGR_RENDER_OVERRIDES_DIR", str(target))
    _install_dir()
    server.do_set_render_override(APP_ID, {"revision": 0, "override": {}})
    assert os.path.exists(target / (APP_ID + ".json"))


# --------------------------------------------------------------------------
# Result Hub injection + render_updated


class _NoopFormatter:
    def format(self, _raw):
        return []


class _CaptureWs:
    def __init__(self):
        self.controls = []

    @staticmethod
    def purge_source(_source_id):
        return 0

    def broadcast_control(self, envelope):
        self.controls.append(json.loads(json.dumps(envelope)))

    def broadcast(self, _record):
        return None


def _identity(instance="run-1", generation=3):
    return {"app_id": APP_ID, "instance_id": instance,
            "generation": generation, "pid": os.getpid()}


def _payload(seq, **extra):
    value = {"type": "results", "seq": seq, "pts": 1.0,
             "frame": {"width": 640, "height": 480},
             "results": [{"label": "person"}], "events": []}
    value.update(extra)
    return value


def test_hub_injects_effective_render_and_broadcasts_render_updated(
        layout, monkeypatch, tmp_path, capsys):
    _install_dir()
    server.do_set_render_override(APP_ID, {
        "revision": 0, "override": {"boxes": {"line_width": 7}}})
    hub = ResultHub(ws_port=0, system_uds_path=str(tmp_path / "system.sock"),
                    formatter=_NoopFormatter())
    identity = _identity()
    manifest = _manifest()
    assert hub.refresh_app_manifest(identity, manifest,
                                    stream_contract=None)

    forged = {"schema_version": 1, "boxes": {"line_width": 16},
              "stream_osd": {"supported": ["boxes"], "default": True}}
    frame = hub.publish_app(_payload(1, render=forged), identity)[0]
    expected = visualization.effective_render(
        manifest, override={"boxes": {"line_width": 7}})
    assert frame["render"] == expected
    assert frame["render"]["boxes"]["line_width"] == 7

    # An application cannot mint the control message through its payload.
    ws = _CaptureWs()
    hub._ws = ws
    envelopes = hub.publish_app(_payload(2, type="render_updated",
                                         render=forged), identity)
    assert all(item["type"] != "render_updated" for item in envelopes)
    assert all(item["render"] == expected for item in envelopes)
    assert ws.controls == []

    monkeypatch.setattr(server, "_result_hub_instance", hub)
    view = server.do_set_render_override(APP_ID, {
        "revision": 1,
        "override": {"boxes": {"colors": {"person": "#00FF00"}},
                     "subtitles": {"font_size": 30}}})
    assert [item["type"] for item in ws.controls] == ["render_updated"]
    control = ws.controls[0]
    assert control["schema"] == frame["schema"]
    assert control["schema_version"] == frame["schema_version"]
    assert control["source"] == {
        "kind": "app", "id": APP_ID, "app_id": APP_ID,
        "instance": "run-1", "generation": 3, "trust": "local"}
    assert control["render"] == view["effective"]
    assert control["render"]["boxes"]["colors"] == {"person": "#00FF00"}
    assert "line_width" not in control["render"]["boxes"] or \
        control["render"]["boxes"]["line_width"] == 2
    with capsys.disabled():
        print("\nRENDER_UPDATED_SAMPLE=" + json.dumps(control, sort_keys=True))

    # Cache of the same generation is refreshed without a new hello.
    frame = hub.publish_app(_payload(3), identity)[0]
    assert frame["render"] == view["effective"]
    assert frame["source"]["generation"] == 3

    # Delete restores defaults and broadcasts again.
    server.do_delete_render_override(APP_ID, 2)
    assert [item["type"] for item in ws.controls] == [
        "render_updated", "render_updated"]
    assert ws.controls[1]["render"] == visualization.effective_render(manifest)
    assert hub.publish_app(_payload(4), identity)[0]["render"] == \
        visualization.effective_render(manifest)
    hub._ws = None


class _ReplayClient:
    """Minimal stand-in for the WS client used by the initial replay path."""

    def __init__(self):
        self.subscription = _Subscription()
        self.controls = []
        self.records = []
        self.reserved = None

    def reserve_replay(self, record_count):
        self.reserved = record_count

    def offer_control(self, envelope):
        self.controls.append(envelope)
        return True

    def offer_record(self, record):
        self.records.append(record)


class _ReplayServer:
    """Only ``hub`` is touched by :meth:`_HubWebSocketServer.replay`."""

    def __init__(self, hub):
        self.hub = hub

    def replay(self, client):
        return _HubWebSocketServer.replay(self, client)


def _live_hub(tmp_path, monkeypatch, identity, manifest):
    hub = ResultHub(ws_port=0, system_uds_path=str(tmp_path / "system.sock"),
                    formatter=_NoopFormatter())
    assert hub.refresh_app_manifest(identity, manifest, stream_contract=None)
    monkeypatch.setattr(server, "_result_hub_instance", hub)
    return hub


def test_saved_override_reaches_replay_without_a_new_frame(
        layout, monkeypatch, tmp_path):
    """Records published before the override still replay the new style."""
    _install_dir()
    identity = _identity()
    manifest = _manifest()
    hub = _live_hub(tmp_path, monkeypatch, identity, manifest)
    hub._ws = _CaptureWs()

    published = hub.publish_app(_payload(
        1, events=[{"kind": "fall", "event_id": 1}],
        summary={"state": "idle"}), identity)
    kinds = {item["type"] for item in published}
    assert {"frame", "event", "status"} <= kinds
    before = visualization.effective_render(manifest)
    assert all(item["render"] == before for item in published)

    # The application publishes nothing after this point.
    view = server.do_set_render_override(APP_ID, {
        "revision": 0, "override": {"boxes": {"line_width": 9}}})
    assert view["effective"] != before

    replayed = hub.snapshot_records()
    assert {record.raw["type"] for record in replayed} == {
        "frame", "event", "status"}
    for record in replayed:
        assert record.raw["render"] == view["effective"]
        assert json.loads(record.raw_bytes.decode())["render"] == \
            view["effective"]

    # The stored records themselves keep every other field untouched.
    stored = list(hub._latest_frames.values()) + list(
        hub._latest_status.values()) + [
            item for _timestamp, item in hub._events]
    assert len(stored) == len(replayed)
    for old, new in zip(stored, replayed):
        assert old.raw["id"] == new.raw["id"]
        assert old.batch_id == new.batch_id
        assert {key: value for key, value in old.raw.items()
                if key != "render"} == {
            key: value for key, value in new.raw.items() if key != "render"}

    # The real initial-replay path returns the same refreshed records.
    client = _ReplayClient()
    _ReplayServer(hub).replay(client)
    assert client.reserved == len(replayed)
    assert [record.raw["type"] for record in client.records] == [
        record.raw["type"] for record in replayed]
    assert all(record.raw["render"] == view["effective"]
               for record in client.records)
    hub._ws = None


def test_replay_never_injects_render_into_another_generation(
        layout, monkeypatch, tmp_path):
    _install_dir()
    identity = _identity()
    manifest = _manifest()
    hub = _live_hub(tmp_path, monkeypatch, identity, manifest)

    live = hub.publish_app(_payload(1), identity)[0]
    stale_raw = json.loads(json.dumps(live))
    stale_raw["id"] = "stale:frame"
    stale_raw["source"]["generation"] = identity["generation"] - 1
    stale = next(record for record in hub._latest_frames.values())
    hub._latest_frames[("stale", "")] = type(stale)(
        raw=stale_raw, raw_bytes=json.dumps(stale_raw).encode(),
        format_batch=stale.format_batch)
    before = visualization.effective_render(manifest)

    view = server.do_set_render_override(APP_ID, {
        "revision": 0, "override": {"boxes": {"line_width": 9}}})
    replayed = {record.raw["id"]: record.raw["render"]
                for record in hub.snapshot_records()}
    assert replayed["stale:frame"] == before
    assert replayed[live["id"]] == view["effective"]


def test_in_flight_frame_is_rebound_to_the_render_saved_mid_publish(
        layout, monkeypatch, tmp_path):
    """A frame that read the old render must not repaint the new style away."""
    _install_dir()
    identity = _identity()
    manifest = _manifest()
    hub = _live_hub(tmp_path, monkeypatch, identity, manifest)
    hub._ws = _CaptureWs()

    assert hub.publish_app(_payload(1), identity)[0]["render"] == \
        visualization.effective_render(manifest)
    assert hub._render_rebound == 0

    saved = {}
    original = hub._trusted_app_contract

    def racing_contract(current):
        contract = original(current)
        if not saved:
            # The override lands after the render was read but before the
            # envelope reaches the publication fence.
            saved["view"] = server.do_set_render_override(APP_ID, {
                "revision": 0,
                "override": {"boxes": {"line_width": 9},
                             "subtitles": {"font_size": 28}}})
        return contract

    monkeypatch.setattr(hub, "_trusted_app_contract", racing_contract)
    published = hub.publish_app(_payload(
        2, events=[{"kind": "fall", "event_id": 2}],
        summary={"state": "idle"}), identity)
    monkeypatch.setattr(hub, "_trusted_app_contract", original)

    effective = saved["view"]["effective"]
    assert effective["boxes"]["line_width"] == 9
    assert published
    for envelope in published:
        assert envelope["render"] == effective
    assert hub._render_rebound == len(published)

    # The next frame reads the new revision and is not copied again.
    rebound = hub._render_rebound
    assert hub.publish_app(_payload(3), identity)[0]["render"] == effective
    assert hub._render_rebound == rebound
    hub._ws = None


def test_in_flight_rebind_still_discards_a_payload_supplied_render(
        layout, monkeypatch, tmp_path):
    _install_dir()
    identity = _identity()
    manifest = _manifest()
    hub = _live_hub(tmp_path, monkeypatch, identity, manifest)

    forged = {"schema_version": 1, "boxes": {"line_width": 16},
              "stream_osd": {"supported": ["boxes"], "default": True}}
    saved = {}
    original = hub._trusted_app_contract

    def racing_contract(current):
        contract = original(current)
        if not saved:
            saved["view"] = server.do_set_render_override(APP_ID, {
                "revision": 0, "override": {"boxes": {"line_width": 9}}})
        return contract

    monkeypatch.setattr(hub, "_trusted_app_contract", racing_contract)
    frame = hub.publish_app(_payload(1, render=forged), identity)[0]
    monkeypatch.setattr(hub, "_trusted_app_contract", original)

    assert frame["render"] == saved["view"]["effective"]
    assert frame["render"]["boxes"]["line_width"] == 9
    assert "stream_osd" not in frame["render"]

    # Replay agrees with the publication, and a forged payload render never
    # survives into the stored record either.
    replayed = next(record for record in hub.snapshot_records()
                    if record.raw["type"] == "frame")
    assert replayed.raw["render"] == saved["view"]["effective"]


def test_render_refresh_without_live_generation_is_noop(layout, tmp_path):
    hub = ResultHub(ws_port=0, system_uds_path=str(tmp_path / "system.sock"),
                    formatter=_NoopFormatter())
    ws = _CaptureWs()
    hub._ws = ws
    assert hub.refresh_app_render(APP_ID, _manifest()) is False
    assert hub.refresh_app_render(APP_ID, _manifest("other")) is False
    assert ws.controls == []
    hub._ws = None


# --------------------------------------------------------------------------
# lifecycle


def _package(root, version, *, v2=False):
    package = root / ("%s-%s.tar.gz" % (APP_ID, version))
    manifest = {"id": APP_ID, "name": "Overlay", "version": version,
                "entry": "app.py", "config_schema": {}}
    source = root / ("source-" + version)
    source.mkdir()
    (source / "manifest.json").write_text(json.dumps(manifest))
    (source / "app.py").write_text("# %s\n" % version)
    with tarfile.open(package, "w:gz") as archive:
        archive.add(source / "manifest.json", arcname="manifest.json")
        archive.add(source / "app.py", arcname="app.py")
    return str(package)


def _write_override(override, revision=1):
    os.makedirs(paths.render_overrides_dir(), exist_ok=True)
    with open(render_override.override_path(APP_ID), "w") as stream:
        json.dump({"schema_version": 1, "revision": revision,
                   "override": override}, stream)


def test_uninstall_deletes_override(layout, monkeypatch):
    _install_dir()
    server.do_set_render_override(APP_ID, {"revision": 0, "override": VALID})
    with open(render_override.pending_path(APP_ID), "w") as stream:
        stream.write("{}")
    monkeypatch.setattr(server.supervisor, "is_running", lambda _app: None)
    server.do_uninstall(APP_ID)
    assert not os.path.exists(render_override.override_path(APP_ID))
    assert not os.path.exists(render_override.pending_path(APP_ID))


def test_fresh_install_clears_historical_override(layout):
    _write_override(VALID, revision=5)
    published_with = []
    real_commit = installer.commit_prepared

    def observe(candidate):
        published_with.append(
            os.path.exists(render_override.override_path(APP_ID)))
        return real_commit(candidate)

    installer_commit = installer.commit_prepared
    installer.commit_prepared = observe
    try:
        server.do_install(_package(layout, "1.0.0"))
    finally:
        installer.commit_prepared = installer_commit
    assert published_with == [False]
    assert render_override.load(APP_ID) == {
        "schema_version": 1, "revision": 0, "override": {}}


def test_upgrade_pending_is_staged_before_publish_and_committed(
        layout, monkeypatch):
    server.do_install(_package(layout, "1.0.0"))
    _write_override({"boxes": {"line_width": 3}}, revision=4)
    # Package fixtures are unsigned v1 manifests; treat the new release as a
    # render-capable manifest so the carried override is kept.
    monkeypatch.setattr(render_override, "sanitize_for_manifest",
                        lambda value, _manifest: render_override.sanitize(value))
    seen = []
    real_commit = installer.commit_prepared

    def observe(candidate):
        with open(render_override.pending_path(APP_ID)) as stream:
            seen.append(json.load(stream))
        return real_commit(candidate)

    monkeypatch.setattr(installer, "commit_prepared", observe)
    server.do_install(_package(layout, "2.0.0"))
    assert seen == [{"schema_version": 1, "revision": 4,
                     "override": {"boxes": {"line_width": 3}}}]
    assert not os.path.exists(render_override.pending_path(APP_ID))
    assert render_override.load(APP_ID) == {
        "schema_version": 1, "revision": 4,
        "override": {"boxes": {"line_width": 3}}}


def test_upgrade_to_non_v2_manifest_drops_override(layout):
    server.do_install(_package(layout, "1.0.0"))
    _write_override({"boxes": {"line_width": 3}}, revision=4)
    server.do_install(_package(layout, "2.0.0"))
    assert render_override.load(APP_ID) == {
        "schema_version": 1, "revision": 5, "override": {}}


def test_upgrade_publish_failure_discards_pending_keeps_live(
        layout, monkeypatch):
    server.do_install(_package(layout, "1.0.0"))
    _write_override({"boxes": {"line_width": 3}}, revision=4)
    before = open(render_override.override_path(APP_ID), "rb").read()

    def fail_commit(_candidate):
        assert os.path.exists(render_override.pending_path(APP_ID))
        raise installer.InstallError("injected publish failure")

    monkeypatch.setattr(installer, "commit_prepared", fail_commit)
    with pytest.raises(installer.InstallError, match="publish failure"):
        server.do_install(_package(layout, "2.0.0"))
    assert not os.path.exists(render_override.pending_path(APP_ID))
    assert open(render_override.override_path(APP_ID), "rb").read() == before


def test_upgrade_ready_failure_rolls_back_override(layout, monkeypatch):
    server.do_install(_package(layout, "1.0.0"))
    state.set_active(APP_ID, "1.0.0")
    _write_override({"boxes": {"line_width": 3}}, revision=4)
    before = open(render_override.override_path(APP_ID), "rb").read()
    running = {APP_ID: 4242}
    monkeypatch.setattr(server.supervisor, "is_running",
                        lambda app: running.get(app))
    monkeypatch.setattr(server.supervisor, "has_run_record", lambda _app: False)
    monkeypatch.setattr(server.supervisor, "owned_pid_is_running",
                        lambda *_args: False)

    def stop(app_id):
        running.pop(app_id, None)
        return {"stopped": app_id}

    def start(app_id, operation, proof=None, **_kwargs):
        if operation == "upgrade_restart":
            assert os.path.exists(render_override.pending_path(APP_ID))
            raise supervisor.SupervisorError("injected READY failure")
        running[app_id] = 5252
        return 5252

    monkeypatch.setattr(server.supervisor, "stop", stop)
    monkeypatch.setattr(server, "_prepare_external_start", lambda *_args: {})
    monkeypatch.setattr(server, "_coordinated_legacy_start", start)
    with pytest.raises(supervisor.SupervisorError, match="READY failure"):
        server.do_install(_package(layout, "2.0.0"))
    assert not os.path.exists(render_override.pending_path(APP_ID))
    assert open(render_override.override_path(APP_ID), "rb").read() == before


def test_startup_recovery_removes_pending_orphans_and_revalidates(layout):
    _install_dir()
    _install_dir("legacy-app", manifest={"id": "legacy-app", "version": "1"})
    directory = paths.render_overrides_dir()
    os.makedirs(directory)
    _write_override({"boxes": {"line_width": 99, "label": "name"},
                     "stream_osd": {}}, revision=2)
    with open(render_override.pending_path(APP_ID), "w") as stream:
        json.dump({"schema_version": 1, "revision": 9,
                   "override": {"boxes": {"line_width": 1}}}, stream)
    with open(os.path.join(directory, "gone-app.json"), "w") as stream:
        json.dump({"schema_version": 1, "revision": 1, "override": {}}, stream)
    with open(os.path.join(directory, "legacy-app.json"), "w") as stream:
        json.dump({"schema_version": 1, "revision": 1,
                   "override": {"boxes": {"line_width": 2}}}, stream)

    report = server._recover_render_overrides()

    assert report["pending_removed"] == [APP_ID + ".json.pending"]
    assert report["orphans_removed"] == ["gone-app"]
    assert sorted(report["revalidated"]) == ["legacy-app", APP_ID]
    assert sorted(os.listdir(directory)) == sorted(
        [APP_ID + ".json", "legacy-app.json"])
    assert render_override.load(APP_ID) == {
        "schema_version": 1, "revision": 3,
        "override": {"boxes": {"label": "name"}}}
    assert render_override.load("legacy-app")["override"] == {}
    # A second pass is a no-op.
    assert server._recover_render_overrides() == {
        "pending_removed": [], "orphans_removed": [], "revalidated": {}}


def test_render_override_mutations_write_audit_records(layout):
    _install_dir()
    server.do_set_render_override(APP_ID, {"revision": 0, "override": VALID})
    server.do_delete_render_override(APP_ID, 1)
    records = [json.loads(line) for line in open(paths.audit_log())]
    ops = [(r["action"], r.get("op"), r.get("revision"))
           for r in records if r["action"] == "v1_render_override"]
    assert ops == [("v1_render_override", "replace", 1),
                   ("v1_render_override", "reset", 2)]

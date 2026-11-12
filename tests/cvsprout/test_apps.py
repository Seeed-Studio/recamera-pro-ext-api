"""Host tests for CVSprout Link capture and delivery (no hardware)."""
import errno
import importlib.util
import os
import json
from pathlib import Path
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
import urllib.request

import cv2
import numpy as np
import pytest


ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


transport = load("transport", "apps/cvsprout-link/transport.py")
system_models = load("system_models", "apps/cvsprout-link/system_models.py")
capture = load("cvsprout_link_app", "apps/cvsprout-link/app.py")


def test_spool_survives_restart_and_only_matching_ack_deletes(tmp_path):
    queue = transport.Spool(tmp_path / "capture-spool")
    cid = queue.enqueue(b"jpeg", "owner1", "2026-11-01T12:00:00Z", "batch1")
    restarted = transport.Spool(tmp_path / "capture-spool")
    entry = restarted.oldest("owner1")
    assert entry["capture_id"] == cid
    assert entry["jpeg"] == b"jpeg"
    assert restarted.oldest("owner2") is None
    for ack in [{"received": True, "capture_id": "wrong", "asset_id": 1},
                {"received": False, "capture_id": cid, "asset_id": 1},
                {"received": True, "capture_id": cid, "asset_id": 0},
                {"received": True, "capture_id": cid, "asset_id": True}]:
        with pytest.raises(transport.DeliveryError):
            restarted.acknowledge(entry, ack)
        assert restarted.stats()["pending_images"] == 1
    restarted.acknowledge(entry, {"received": True, "capture_id": cid, "asset_id": 4})
    assert restarted.stats() == {"pending_images": 0, "pending_bytes": 0}


def test_capacity_preserves_pending_and_private_permissions(tmp_path):
    queue = transport.Spool(tmp_path / "capture-spool", max_bytes=5, max_images=2)
    cid = queue.enqueue(b"1234", "owner", "2026-11-01T00:00:00Z")
    assert queue.enqueue(b"12", "owner", "2026-11-01T00:00:01Z") is None
    assert queue.oldest("owner")["capture_id"] == cid
    assert queue.path.stat().st_mode & 0o777 == 0o700
    assert all(entry.stat().st_mode & 0o777 == 0o600 for entry in queue.path.iterdir())
    queue.acknowledge(queue.oldest("owner"), {"received": True, "capture_id": cid, "asset_id": 4})
    assert not list(queue.path.iterdir())  # storage is released after the ACK


def test_failed_record_write_preserves_pending_and_cleans_temporary(tmp_path, monkeypatch):
    queue = transport.Spool(tmp_path / "capture-spool", max_bytes=1024 * 1024)
    first_id = queue.enqueue(b"original", "owner", "2026-11-01T00:00:00Z")
    original_fsync = transport.os.fsync

    def full_disk(fd):
        raise OSError(errno.ENOSPC, "controlled storage exhaustion")

    monkeypatch.setattr(transport.os, "fsync", full_disk)
    assert queue.enqueue(b"new-image", "owner", "2026-11-01T00:00:01Z") is None
    assert queue.oldest("owner")["capture_id"] == first_id
    assert queue.stats() == {"pending_images": 1, "pending_bytes": 8}
    assert len(list(queue.path.iterdir())) == 1
    monkeypatch.setattr(transport.os, "fsync", original_fsync)
    restarted = transport.Spool(queue.path)
    assert restarted.oldest("owner")["jpeg"] == b"original"


def test_publish_directory_sync_failure_keeps_capture_tracked(tmp_path, monkeypatch):
    queue = transport.Spool(tmp_path / "capture-spool", max_bytes=5)

    def full_disk():
        raise OSError(errno.ENOSPC, "controlled directory sync exhaustion")

    monkeypatch.setattr(queue, "_sync_directory", full_disk)
    with pytest.raises(transport.DeliveryError, match="spool_storage_error"):
        queue.enqueue(b"image", "owner", "2026-11-01T00:00:00Z")
    assert queue.stats() == {"pending_images": 1, "pending_bytes": 5}
    assert queue.enqueue(b"x", "owner", "2026-11-01T00:00:01Z") is None
    entry = queue.oldest("owner")
    restarted = transport.Spool(queue.path)
    assert restarted.oldest("owner")["capture_id"] == entry["capture_id"]


def test_disk_full_during_ack_is_retryable_and_retains_capture(tmp_path, monkeypatch):
    queue = transport.Spool(tmp_path / "capture-spool")
    cid = queue.enqueue(b"image", "owner", "2026-11-01T00:00:00Z")
    entry = queue.oldest("owner")
    original_unlink = Path.unlink

    def full_disk(path, *args, **kwargs):
        if path.suffix == ".capture":
            raise OSError(errno.ENOSPC, "controlled unlink exhaustion")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", full_disk)
    ack = {"received": True, "capture_id": cid, "asset_id": 1}
    with pytest.raises(transport.DeliveryError, match="spool_storage_full"):
        queue.acknowledge(entry, ack)
    assert queue.oldest("owner")["capture_id"] == cid
    monkeypatch.setattr(Path, "unlink", original_unlink)
    queue.acknowledge(entry, ack)
    assert queue.stats()["pending_images"] == 0


def test_atomic_write_fsync_order_and_restart_discards_only_unpublished(tmp_path, monkeypatch):
    queue = transport.Spool(tmp_path / "capture-spool")
    events = []
    original_fsync, original_replace = transport.os.fsync, transport.os.replace

    def sync(fd):
        events.append("sync-directory" if os.path.isdir("/proc/self/fd/%d" % fd) else "sync-file")
        return original_fsync(fd)

    def replace(source, destination):
        events.append("publish")
        return original_replace(source, destination)

    monkeypatch.setattr(transport.os, "fsync", sync)
    monkeypatch.setattr(transport.os, "replace", replace)
    cid = queue.enqueue(b"image", "owner", "2026-11-01T00:00:00Z")
    assert events == ["sync-file", "publish", "sync-directory"]
    temporary = queue.path / (".pending-" + "a" * 32 + ".tmp")
    temporary.write_bytes(b"interrupted uncommitted write")
    restarted = transport.Spool(queue.path)
    assert restarted.oldest("owner")["capture_id"] == cid
    assert not temporary.exists()
    assert len(list(queue.path.iterdir())) == 1


def test_corrupt_metadata_and_payload_fail_closed_without_deletion(tmp_path):
    queue = transport.Spool(tmp_path / "capture-spool")
    queue.enqueue(b"image", "owner", "2026-11-01T00:00:00Z")
    record = next(queue.path.iterdir())
    original = record.read_bytes()
    record.write_bytes(original[:-1] + b"!")
    with pytest.raises(transport.DeliveryError, match="capture_spool_corrupt"):
        queue.oldest("owner")
    assert record.exists() and queue.stats()["pending_images"] == 1
    record.write_bytes(b"invalid-header\nimage")
    temporary = queue.path / (".pending-" + "a" * 32 + ".tmp")
    temporary.write_bytes(b"keep for inspection when corruption blocks startup")
    with pytest.raises(transport.DeliveryError, match="capture_spool_corrupt"):
        transport.Spool(queue.path)
    assert record.exists() and temporary.exists()


def test_concurrent_sampling_obeys_image_count_and_fifo(tmp_path):
    queue = transport.Spool(tmp_path / "capture-spool", max_images=10)
    captures = []

    def sample():
        for _ in range(4):
            captures.append(queue.enqueue(b"image", "owner", "2026-11-01T00:00:00Z"))

    threads = [threading.Thread(target=sample) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    accepted = [capture_id for capture_id in captures if capture_id is not None]
    assert len(accepted) == len(set(accepted)) == 10
    assert queue.stats() == {"pending_images": 10, "pending_bytes": 50}
    restarted = transport.Spool(queue.path)
    sequences = []
    while entry := restarted.oldest("owner"):
        sequences.append(entry["sequence"])
        restarted.acknowledge(entry, {"received": True, "capture_id": entry["capture_id"], "asset_id": 1})
    assert sequences == sorted(sequences)
    assert not list(queue.path.iterdir())


def test_first_launch_syncs_all_new_directory_entries(tmp_path, monkeypatch):
    events = []
    original_fsync = transport.os.fsync

    def sync(fd):
        events.append(Path(os.readlink("/proc/self/fd/%d" % fd)))
        return original_fsync(fd)

    monkeypatch.setattr(transport.os, "fsync", sync)
    directory = tmp_path / "cvsprout-capture" / "capture-spool"
    transport.Spool(directory)
    assert events == [directory, directory.parent, tmp_path]
    assert directory.parent.stat().st_mode & 0o777 == 0o700


def test_concurrent_capture_and_ack_preserve_all_accepted_images(tmp_path):
    queue = transport.Spool(tmp_path / "capture-spool", max_images=100)
    accepted, acknowledged, errors = [], [], []
    complete = threading.Event()
    barrier = threading.Barrier(3)

    def sample():
        try:
            barrier.wait(timeout=5)
            for _ in range(20):
                capture_id = queue.enqueue(b"image", "owner", "2026-11-01T00:00:00Z")
                assert capture_id
                accepted.append(capture_id)
        except BaseException as error:
            errors.append(error)

    def drain():
        try:
            barrier.wait(timeout=5)
            deadline = time.monotonic() + 10
            while not complete.is_set() or queue.stats()["pending_images"]:
                assert time.monotonic() < deadline
                entry = queue.oldest("owner")
                if entry is None:
                    time.sleep(0.001)
                    continue
                queue.acknowledge(entry, {"received": True, "capture_id": entry["capture_id"], "asset_id": 1})
                acknowledged.append(entry["capture_id"])
        except BaseException as error:
            errors.append(error)

    producers = [threading.Thread(target=sample) for _ in range(2)]
    consumer = threading.Thread(target=drain)
    consumer.start()
    for producer in producers:
        producer.start()
    for producer in producers:
        producer.join(timeout=10)
    complete.set()
    consumer.join(timeout=10)
    assert not errors
    assert not any(thread.is_alive() for thread in [*producers, consumer])
    assert len(accepted) == len(acknowledged) == len(set(accepted)) == 40
    assert set(accepted) == set(acknowledged)
    assert queue.stats() == {"pending_images": 0, "pending_bytes": 0}


def test_metadata_bounds_and_unknown_temp_names_preserve_files(tmp_path):
    queue = transport.Spool(tmp_path / "capture-spool")
    with pytest.raises(ValueError, match="invalid capture metadata"):
        queue.enqueue(b"image", "owner", "2026-11-01T00:00:00Z", batch_id="x" * 129)
    unknown = queue.path / ".pending-unrecognized.tmp"
    unknown.write_bytes(b"must remain untouched")
    with pytest.raises(transport.DeliveryError, match="capture_spool_corrupt"):
        transport.Spool(queue.path)
    assert unknown.read_bytes() == b"must remain untouched"
    unknown.unlink()
    huge = queue.path / "corrupted.capture"
    # A corrupt multi-megabyte header is rejected after at most 2049 bytes.
    huge.write_bytes(b"x" * (2 * 1024 * 1024))
    with pytest.raises(transport.DeliveryError, match="capture_spool_corrupt"):
        transport.Spool(queue.path)
    assert huge.stat().st_size == 2 * 1024 * 1024


@pytest.mark.parametrize("legacy_name", ["captures.sqlite3", "captures.sqlite3-journal", "captures.sqlite3-wal", "captures.sqlite3-shm"])
def test_legacy_queue_is_preserved_and_upgrade_explains_recovery(tmp_path, monkeypatch, legacy_name):
    monkeypatch.setenv("APPMGR_APPDATA_DIR", str(tmp_path))
    data_dir = tmp_path / "cvsprout-capture"
    data_dir.mkdir()
    legacy = data_dir / legacy_name
    legacy.write_bytes(b"legacy data must not be discarded")
    app = capture.CVSproutLinkApp()
    try:
        with pytest.raises(transport.DeliveryError, match="legacy_spool_migration_required"):
            app.setup({"server_url": "https://controlled.example", "device_token": "cvsprout_device_controlled"})
        assert legacy.read_bytes() == b"legacy data must not be discarded"
        assert not (data_dir / "capture-spool").exists()
    finally:
        app.finish()


class FakeClient:
    server_url = "https://cvsprout.example"

    def __init__(self):
        self.settings = {"device_id": "camera-1", "project_id": 2, "capture_enabled": True,
                         "capture_interval_seconds": 10, "max_image_bytes": 10000}
        self.receipts = []
        self.uploads = []
        self.fail = False

    def configuration(self):
        if self.fail:
            raise transport.DeliveryError("server_unreachable_or_invalid_response")
        return dict(self.settings)

    def heartbeat(self, values):
        return {"received": True}

    def upload(self, entry):
        self.uploads.append(entry["capture_id"])
        if self.fail:
            raise transport.DeliveryError("server_unreachable_or_invalid_response")
        return {"received": True, "capture_id": entry["capture_id"], "asset_id": 3}


def test_retry_reuses_capture_id_and_pause_drains_spool(tmp_path):
    client = FakeClient()
    now = [0.0]
    queue = transport.Spool(tmp_path / "capture-spool")
    worker = transport.Uploader(client, queue, clock=lambda: now[0])
    worker.step()
    cid = queue.enqueue(b"jpeg", worker.owner, "2026-11-01T00:00:00Z")
    client.fail = True
    worker.step()
    assert queue.stats()["pending_images"] == 1
    worker.step()
    assert client.uploads == [cid]  # exponential backoff avoids a busy loop
    client.fail = False
    client.settings["capture_enabled"] = False
    now[0] = 31
    worker.step()
    assert not worker.capture_state()["enabled"]
    assert client.uploads == [cid, cid]
    assert queue.stats()["pending_images"] == 0


def test_offline_configuration_expires_and_other_identity_cannot_drain(tmp_path):
    client = FakeClient()
    now = [0.0]
    queue = transport.Spool(tmp_path / "capture-spool")
    worker = transport.Uploader(client, queue, clock=lambda: now[0])
    worker.step()
    owner = worker.owner
    queue.enqueue(b"jpeg", owner, "2026-11-01T00:00:00Z")
    client.settings["project_id"] = 3
    now[0] = 31
    worker.step()
    assert worker.owner != owner
    assert queue.stats()["pending_images"] == 1
    client.fail = True
    now[0] = 400
    worker.step()
    assert not worker.capture_state()["enabled"]
    assert queue.stats()["pending_images"] == 1


@pytest.mark.parametrize("address", ["ftp://host", "http://user:pass@host", "https://host?token=a", "https://host/#fragment"])
def test_server_url_rejects_embedded_credentials_and_query(address):
    with pytest.raises(ValueError):
        transport.EdgeClient(address, "secret")


def test_http_redirect_does_not_forward_credentials():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization")))
            self.send_response(302)
            self.send_header("Location", "/stolen")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = transport.EdgeClient("http://127.0.0.1:%s" % server.server_port, "cvsprout_device_private_token")
        with pytest.raises(transport.DeliveryError, match="server_redirect_refused") as error:
            client.configuration()
        assert "cvsprout_device_private_token" not in str(error.value)
        assert requests == [("/api/edge/v1/config", "Bearer cvsprout_device_private_token")]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_multipart_has_fixed_capture_id_original_bytes_and_bearer():
    client = transport.EdgeClient("https://host/base", "cvsprout_device_private_token")
    seen = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, limit):
            return b'{"received": true, "capture_id": "fixed-id", "asset_id": 7}'

    client._opener = SimpleNamespace(open=lambda request, timeout: seen.append(request) or Response())
    client.upload({"capture_id": "fixed-id", "captured_at": "2026-11-01T00:00:00Z", "jpeg": b"original-image"})
    request = seen[0]
    assert request.full_url == "https://host/base/api/edge/v1/captures"
    assert request.get_header("Authorization") == "Bearer cvsprout_device_private_token"
    assert b'filename="capture.jpg"' in request.data
    assert b"original-image" in request.data and b"fixed-id" in request.data
    assert b"cvsprout_device_private_token" not in request.data


def test_capture_preserves_original_resolution_and_rgb_color():
    pixels = np.zeros((40, 80, 3), dtype=np.uint8)
    pixels[..., 0] = 255
    frame = SimpleNamespace(data=pixels, w=80, h=40, fmt="RGB")
    jpeg = capture.encode_original(frame, 95)
    pixels[:] = 0  # the queued JPEG is independent of the borrowed input
    decoded = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    assert decoded.shape == (40, 80, 3)
    assert decoded[..., 2].mean() > 250 and decoded[..., :2].mean() < 5


def test_frame_failure_stops_upload_thread_and_repeated_finish_is_safe(tmp_path, monkeypatch):
    monkeypatch.setenv("APPMGR_APPDATA_DIR", str(tmp_path))
    monkeypatch.setattr(capture, "EdgeClient", lambda *args: FakeClient())
    app = capture.CVSproutLinkApp()
    app.setup({"server_url": "https://controlled.example", "device_token": "cvsprout_device_controlled"})

    def failed_frames():
        raise RuntimeError("controlled frame-source failure")
        yield  # keep a frame iterator without opening any device

    app.frames = failed_frames
    with pytest.raises(RuntimeError, match="controlled frame-source failure"):
        app.run()
    assert app.upload_thread is None
    assert app.uploader.stop.is_set()
    app.finish()
    app.finish()


def test_server_capacity_limit_and_revocation_stop_new_captures(tmp_path):
    client = FakeClient()
    client.settings.update(max_pending_images=1, max_pending_bytes=5)
    spool = transport.Spool(tmp_path / "capture-spool")
    clock = [0.0]
    uploader = transport.Uploader(client, spool, clock=lambda: clock[0])
    uploader.step()
    assert spool.max_images == 1 and spool.max_bytes == 5
    assert spool.enqueue(b"jpeg", uploader.owner, "2026-11-01T00:00:00Z")
    assert spool.enqueue(b"jpeg", uploader.owner, "2026-11-01T00:00:01Z") is None

    def revoked():
        raise transport.DeliveryError("http_401")

    client.configuration = revoked
    client.fail = True
    clock[0] = 31
    uploader.step()
    assert not uploader.capture_state()["enabled"]
    assert spool.stats()["pending_images"] == 1


class DiagnosticClient(FakeClient):
    def __init__(self, schema=2):
        super().__init__()
        self.settings['heartbeat_schema_version'] = schema
        self.heartbeats = []

    def heartbeat(self, values):
        self.heartbeats.append(dict(values))
        return {'received': True}


def test_diagnostics_report_frames_sampling_stall_and_session_quota(tmp_path):
    now = [0.0]
    client = DiagnosticClient()
    worker = transport.Uploader(client, transport.Spool(tmp_path / 'queue'), clock=lambda: now[0])
    worker.step()
    initial = client.heartbeats[-1]
    assert initial['state'] == 'waiting_frames'
    assert initial['frames_received'] == initial['captured_images'] == 0
    assert initial['last_frame_age_seconds'] is None
    worker.frame_received()
    worker.capture_succeeded()
    now[0] = 4
    worker.step()
    assert client.heartbeats[-1]['state'] == 'capturing'
    assert client.heartbeats[-1]['frames_received'] == client.heartbeats[-1]['captured_images'] == 1
    now[0] = 20
    worker.step()
    assert client.heartbeats[-1]['state'] == 'waiting_frames'
    assert client.heartbeats[-1]['last_frame_age_seconds'] == 20
    worker.frame_received()
    worker.capture_succeeded(session_complete=True)
    now[0] = 24
    worker.step()
    assert client.heartbeats[-1]['state'] == 'session_limit'
    assert client.heartbeats[-1]['session_complete'] is True
    assert client.heartbeats[-1]['captured_images'] == 2


def test_diagnostics_remain_compatible_with_old_servers(tmp_path):
    client = DiagnosticClient(schema=1)
    worker = transport.Uploader(client, transport.Spool(tmp_path / 'queue'))
    worker.step()
    assert set(client.heartbeats[-1]) == {
        'app_version', 'state', 'pending_images', 'pending_bytes', 'uploaded_images', 'last_error'}
    assert client.heartbeats[-1]['state'] == 'waiting_frames'


def test_capture_errors_are_not_cleared_by_unrelated_upload_success(tmp_path):
    client = DiagnosticClient()
    worker = transport.Uploader(client, transport.Spool(tmp_path / 'queue'))
    worker.step()
    worker.frame_received()
    worker.capture_failed('jpeg_encode_failed')
    worker.spool.enqueue(b'old-image', worker.owner, '2026-11-01T00:00:00Z')
    worker.step()
    assert worker.uploaded == 1 and worker.last_error == 'jpeg_encode_failed'
    assert worker.capture_state()['state'] == 'capture_error'
    worker.capture_failed('spool_full_capture_paused')
    assert worker.capture_state()['state'] == 'queue_full'
    worker.capture_succeeded()
    assert worker.last_error == '' and worker.capture_state()['state'] == 'capturing'


def test_configuration_failures_are_logged_without_secrets_and_recover(tmp_path, capsys):
    client = DiagnosticClient()
    client.fail = True
    now = [0.0]
    worker = transport.Uploader(client, transport.Spool(tmp_path / 'queue'), clock=lambda: now[0])
    worker.step()
    assert worker.capture_state()['state'] == 'waiting_config'
    assert not client.heartbeats
    worker.step()
    output = capsys.readouterr().out
    assert output.count('configuration: server_unreachable_or_invalid_response') == 1
    assert client.server_url not in output
    client.fail = False
    now[0] = 16
    worker.step()
    assert worker.last_error == ''
    output = capsys.readouterr().out
    assert 'configuration: recovered' in output
    assert 'connection: server_configuration_received' in output
    assert 'heartbeat: server_confirmed' in output
    worker._error('upload', 'secret_token_even_without_spaces')
    worker._error('upload', 'http_401')
    output = capsys.readouterr().out
    assert 'secret_token' not in output and 'unexpected_capture_error' in output and 'http_401' in output


def test_corrupt_spool_keeps_heartbeat_alive_and_reports_upload_failure(tmp_path):
    client = DiagnosticClient()
    now = [0.0]
    worker = transport.Uploader(client, transport.Spool(tmp_path / 'queue'), clock=lambda: now[0])
    worker.step()
    worker.spool.enqueue(b'image', worker.owner, '2026-11-01T00:00:00Z')
    path = next(worker.spool.path.iterdir())
    data = path.read_bytes()
    path.write_bytes(data[:-1] + b'!')
    worker.step()  # Old implementation raised before its upload try block.
    assert worker.last_error == 'capture_spool_corrupt'
    now[0] = 4
    worker.step()
    assert client.heartbeats[-1]['upload_error'] == 'capture_spool_corrupt'
    assert worker.spool.stats()['pending_images'] == 1 and path.exists()


def test_upload_thread_connects_and_heartbeats_while_no_frames_arrive(tmp_path, monkeypatch):
    monkeypatch.setenv('APPMGR_APPDATA_DIR', str(tmp_path))
    client = DiagnosticClient()
    monkeypatch.setattr(capture, 'EdgeClient', lambda *args: client)
    app = capture.CVSproutLinkApp()
    app.setup({'server_url': 'https://controlled.example', 'device_token': 'cvsprout_device_controlled'})

    def no_frames():
        deadline = time.monotonic() + 3
        while not client.heartbeats:
            assert time.monotonic() < deadline
            time.sleep(0.005)
        assert client.heartbeats[-1]['state'] == 'waiting_frames'
        assert client.heartbeats[-1]['frames_received'] == 0
        return
        yield

    app.frames = no_frames
    try:
        app.run()
        assert client.heartbeats[-1]['state'] == 'capture_stopped'
        assert app.upload_thread is None
    finally:
        app.finish()


def test_real_kit_start_and_frame_iteration_capture_cpu_originals(tmp_path, monkeypatch):
    import kit.app as kit_app
    from kit import Frame
    from kit.adapters.result_sink import StdoutSink

    monkeypatch.setenv('APPMGR_APPDATA_DIR', str(tmp_path))
    client = DiagnosticClient()
    monkeypatch.setattr(capture, 'EdgeClient', lambda *args: client)
    app = capture.CVSproutLinkApp()
    opened = []
    pixels = np.zeros((40, 80, 3), dtype=np.uint8)
    pixels[:, :, 0] = 255

    class Source:
        closed = False

        def frames(self):
            deadline = time.monotonic() + 3
            while not app.uploader.capture_state()['enabled']:
                assert time.monotonic() < deadline
                time.sleep(0.005)
            for index in range(3):
                yield Frame(data=pixels, w=80, h=40, fmt='RGB', pts=index + 1.0)

        def close(self):
            self.closed = True

    source = Source()
    monkeypatch.setattr(kit_app, 'open_frame_source', lambda **kw: opened.append(kw) or source)
    manifest = json.loads((ROOT / 'apps/cvsprout-link/manifest.json').read_text())
    try:
        app.start(sink=StdoutSink(), manifest=manifest,
                  app_dir=str(ROOT / 'apps/cvsprout-link'), verbose=False,
                  config={'server_url': 'https://controlled.example', 'device_token': 'cvsprout_device_controlled',
                          'max_session_images': 1})
        app.run()  # real Kit warm-up, iteration and emit, only camera backend controlled
        assert opened[0]['input_size'] == 0 and not opened[0]['hw_letterbox']
        assert app.capture_count == 1 and app.uploader.frames_received == 2
        assert app.uploader.session_complete is True
        app.uploader.stop.clear()
        app.uploader.step()
        assert app.uploader.uploaded == 1
    finally:
        app.finish()
    assert source.closed


@pytest.mark.parametrize('error, expected', [
    (urllib.error.URLError(ConnectionRefusedError('private-host')), 'server_connection_refused'),
    (urllib.error.URLError(TimeoutError('private-host')), 'server_timeout'),
    (TimeoutError('private-host'), 'server_timeout'),
])
def test_network_errors_are_actionable_but_sanitized(error, expected):
    client = transport.EdgeClient('https://private-host', 'cvsprout_device_private_token')

    def fail(*args, **kwargs):
        raise error

    client._opener.open = fail
    with pytest.raises(transport.DeliveryError) as result:
        client.configuration()
    assert str(result.value) == expected


def test_encoding_failure_is_visible_and_does_not_kill_uploader(tmp_path, monkeypatch):
    monkeypatch.setenv('APPMGR_APPDATA_DIR', str(tmp_path))
    client = DiagnosticClient()
    monkeypatch.setattr(capture, 'EdgeClient', lambda *args: client)
    app = capture.CVSproutLinkApp()
    app.setup({'server_url': 'https://controlled.example', 'device_token': 'cvsprout_device_controlled'})

    def bad_encode(*args):
        raise ValueError('private-host / cvsprout_device_private_token must not leak')

    monkeypatch.setattr(capture, 'encode_original', bad_encode)

    def frames():
        deadline = time.monotonic() + 3
        while not app.uploader.capture_state()['enabled']:
            assert time.monotonic() < deadline
            time.sleep(0.005)
        yield SimpleNamespace(pts=1.0)

    app.frames = frames
    app.emit = lambda *args, **kwargs: None
    try:
        app.run()
        final = client.heartbeats[-1]
        assert final['state'] == 'capture_error'
        assert final['capture_error'] == 'jpeg_encode_failed'
        assert final['frames_received'] == 1 and final['captured_images'] == 0
        assert app.upload_thread is None
    finally:
        app.finish()


@pytest.mark.parametrize('token', ['', 'device-id-or-admin-token', 'cvsprout_device_', 'cvsprout_device_a\r\nprivate-token'])
def test_wrong_device_token_fails_early_with_safe_actionable_message(token):
    with pytest.raises(ValueError, match='device_token_invalid') as result:
        transport.EdgeClient('https://controlled.example', token)
    message = str(result.value)
    assert 'complete cvsprout_device_... token' in message
    assert 'device ID or administrator token' in message
    assert 'device-id-or-admin-token' not in message and '\r\n' not in message


@pytest.mark.parametrize('error, enabled', [
    ('http_401', False), ('http_403', False), ('http_404', False), ('http_410', False),
    ('server_timeout', True),
])
def test_heartbeat_auth_failure_immediately_expires_capture_lease(tmp_path, error, enabled):
    client = DiagnosticClient()
    worker = transport.Uploader(client, transport.Spool(tmp_path / 'queue'))
    worker.step()
    assert worker.capture_state()['enabled']

    def rejected(values):
        raise transport.DeliveryError(error)

    client.heartbeat = rejected
    worker.send_status(force=True)
    assert worker.capture_state()['enabled'] is enabled
    if not enabled:
        assert worker.valid_until == 0
    assert worker.last_error == error


def test_hung_uploader_has_one_shutdown_budget_across_repeated_finish(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(capture, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    app = capture.CVSproutLinkApp()
    waits = []

    class HungWorker:
        ident = 1

        def join(self, timeout):
            waits.append(timeout)
            now[0] += timeout

        def is_alive(self):
            return True

    class Worker:
        stop = threading.Event()

        def send_status(self, **kwargs):
            raise AssertionError('shutdown must not perform HTTP in the main thread')

    app.uploader = Worker()
    app.upload_thread = HungWorker()
    with pytest.raises(RuntimeError, match='did not stop'):
        app._stop_uploader()
    with pytest.raises(RuntimeError, match='did not stop'):
        app.finish()
    with pytest.raises(RuntimeError, match='did not stop'):
        app.finish()
    assert waits == [10, 0, 0]
    assert sum(waits) == 10 and app.uploader.stop.is_set()


def test_terminal_heartbeat_runs_only_in_upload_worker(tmp_path, monkeypatch):
    monkeypatch.setenv('APPMGR_APPDATA_DIR', str(tmp_path))
    client = DiagnosticClient()
    threads = []
    original = client.heartbeat

    def heartbeat(payload):
        threads.append((threading.current_thread().name, payload['state']))
        return original(payload)

    client.heartbeat = heartbeat
    monkeypatch.setattr(capture, 'EdgeClient', lambda *args: client)
    app = capture.CVSproutLinkApp()
    app.setup({'server_url': 'https://controlled.example', 'device_token': 'cvsprout_device_controlled'})

    def no_frames():
        deadline = time.monotonic() + 3
        while not client.heartbeats:
            assert time.monotonic() < deadline
            time.sleep(0.005)
        return
        yield

    app.frames = no_frames
    try:
        app.run()
        assert threads[-1] == ('cvsprout-upload', 'capture_stopped')
        assert {name for name, state in threads} == {'cvsprout-upload'}
        assert app.upload_thread is None
    finally:
        app.finish()

"""Cross-repository HTTP acceptance against an isolated actual CVSprout server.

The backend starts with a temporary database/media directory and no annotation
or training workers. It never connects to the production instance or a camera.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import cv2
import numpy as np


def exercise(backend_root, backend_python):
    source = Path(__file__).resolve().parents[2] / "apps/cvsprout-link/transport.py"
    spec = importlib.util.spec_from_file_location("cvsprout_acceptance_transport", source)
    transport = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(transport)
    with tempfile.TemporaryDirectory(prefix="cvsprout-device-http-") as temporary:
        temporary = Path(temporary)
        env = os.environ.copy()
        env.update(MDP_DATA_DIR=str(temporary / "data"), MDP_MODELS_DIR=str(temporary / "models"),
                   MDP_AUTH_MODE="disabled", MDP_BOOTSTRAP_API_KEY="", MDP_PUBLIC_ORIGIN="",
                   MDP_EMBEDDED_WORKER="false", MDP_EMBEDDED_PIPELINE_WORKER="false",
                   MDP_TRAINING_ENABLED="false", MDP_ENABLE_DEMO_MODEL="false", MDP_MODEL_DEVICE="cpu",
                   MDP_RECAMERA_SSH_KEY="", MDP_RECAMERA_KNOWN_HOSTS="", CUDA_VISIBLE_DEVICES="")
        listening = socket.socket()
        listening.bind(("127.0.0.1", 0))
        listening.listen(128)
        origin = "http://127.0.0.1:%s" % listening.getsockname()[1]
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def request(path, data=None, method=None, token=None):
            headers = {"Content-Type": "application/json"}
            if token:
                headers["Authorization"] = "Bearer " + token
            req = urllib.request.Request(origin + path, data=json.dumps(data).encode() if data is not None else None,
                                         headers=headers, method=method)
            with opener.open(req, timeout=5) as response:
                result = json.load(response)
                assert response.headers.get("X-Content-Type-Options") == "nosniff"
                return result

        with (temporary / "server.log").open("w+") as log:
            process = subprocess.Popen([str(backend_python), "-m", "uvicorn", "app.main:app", "--fd",
                                        str(listening.fileno()), "--no-access-log", "--log-level", "warning"],
                                       cwd=backend_root, env=env, pass_fds=(listening.fileno(),),
                                       stdout=log, stderr=subprocess.STDOUT)
            listening.close()
            try:
                deadline = time.monotonic() + 30
                while True:
                    try:
                        request("/api/health")
                        break
                    except (urllib.error.URLError, TimeoutError, OSError):
                        if process.poll() is not None or time.monotonic() >= deadline:
                            log.seek(0)
                            raise AssertionError("isolated backend failed to start: " + log.read()[-3000:])
                        time.sleep(0.05)

                project = request("/api/projects", {"name": "Controlled capture test", "task_type": "detect", "labels": ["person"]})
                device = request("/api/edge/devices", {"name": "Controlled reCamera", "project_id": project["id"]})
                client = transport.EdgeClient(origin, device["device_token"])
                assert client.configuration()["project_id"] == project["id"]
                pixels = np.zeros((40, 80, 3), dtype=np.uint8)
                pixels[..., 2] = 255
                ok, encoded = cv2.imencode(".jpg", pixels)
                assert ok
                jpeg = encoded.tobytes()
                spool_path = temporary / "device-data" / "capture-spool"
                spool = transport.Spool(spool_path)
                clock = [0.0]
                uploader = transport.Uploader(client, spool, clock=lambda: clock[0])
                uploader.step()
                assert uploader.schema_version >= 2
                initial = next(item for item in request("/api/edge/devices")["devices"]
                               if item["id"] == device["id"])
                assert initial["heartbeat"]["state"] == "waiting_frames"
                assert initial["heartbeat"]["frames_received"] == 0
                assert initial["heartbeat"]["last_frame_age_seconds"] is None
                assert initial["last_seen_at"]
                uploader.frame_received()
                uploader.capture_succeeded()
                uploader.send_status(force=True)
                active = next(item for item in request("/api/edge/devices")["devices"]
                              if item["id"] == device["id"])
                assert active["heartbeat"]["state"] == "capturing"
                assert active["heartbeat"]["captured_images"] == active["heartbeat"]["frames_received"] == 1
                owner = uploader.owner
                cid = spool.enqueue(jpeg, owner, "2026-11-01T12:00:00+00:00", "controlled-batch")
                entry = spool.oldest(owner)
                actual_upload = client.upload
                acknowledged = []

                def lost_ack(capture):
                    acknowledged.append(actual_upload(capture))
                    raise transport.DeliveryError("simulated_lost_ack_after_server_commit")

                client.upload = lost_ack
                uploader.step()
                assert spool.stats()["pending_images"] == 1
                assert acknowledged[0]["capture_id"] == cid and acknowledged[0]["received"] is True

                # A process restart must replay the original ID, not create a
                # fresh event or remove a record merely because upload returned.
                client.upload = actual_upload
                restarted_spool = transport.Spool(spool_path)
                replay = actual_upload(restarted_spool.oldest(owner))
                assert replay == acknowledged[0]
                restarted = transport.Uploader(client, restarted_spool, clock=lambda: clock[0])
                restarted.step()
                assert restarted_spool.stats()["pending_images"] == 0

                def counts():
                    with sqlite3.connect(temporary / "data/mdp.sqlite3") as conn:
                        return tuple(conn.execute("SELECT (SELECT COUNT(*) FROM assets),(SELECT COUNT(*) FROM edge_capture_events)").fetchone())

                assert counts() == (1, 1)
                queued_id = restarted_spool.enqueue(jpeg, owner, "2026-11-01T12:00:01+00:00")
                request("/api/edge/devices/" + device["id"], {"capture_enabled": False}, method="PATCH")
                clock[0] = 31
                restarted.step()
                assert not restarted.capture_state()["enabled"]
                assert restarted_spool.stats()["pending_images"] == 0
                assert counts() == (1, 2)  # identical image, distinct capture event

                pending_id = restarted_spool.enqueue(jpeg, owner, "2026-11-01T12:00:02+00:00")
                second_project = request("/api/projects", {"name": "Different owner", "task_type": "detect", "labels": ["person"]})
                second_device = request("/api/edge/devices", {"name": "Other camera", "project_id": second_project["id"]})
                second_client = transport.EdgeClient(origin, second_device["device_token"])
                other = transport.Uploader(second_client, transport.Spool(spool_path), clock=lambda: clock[0])
                other.step()
                assert other.owner != owner
                assert other.spool.stats()["pending_images"] == 1
                assert other.spool.oldest(owner)["capture_id"] == pending_id
                assert counts() == (1, 2)

                rotated = request("/api/edge/devices/" + device["id"] + "/rotate-token", method="POST")
                try:
                    actual_upload(entry)
                except transport.DeliveryError as exc:
                    assert str(exc) == "http_401"  # actual body-limit preauth middleware
                else:
                    raise AssertionError("revoked token uploaded through request middleware")
                new_client = transport.EdgeClient(origin, rotated["device_token"])
                resumed = transport.Uploader(new_client, transport.Spool(spool_path), clock=lambda: clock[0])
                resumed.step()
                assert resumed.owner == owner and resumed.spool.stats()["pending_images"] == 0
                assert counts() == (1, 3)

                listed = request("/api/edge/devices")["devices"]
                status = next(item for item in listed if item["id"] == device["id"])
                manifest = json.loads(source.with_name("manifest.json").read_text())
                assert status["heartbeat"]["app_version"] == manifest["version"]
                assert status["capture_count"] == 3 and status["asset_count"] == 1
                assert device["device_token"] not in json.dumps(listed)
                return {"passed": True, "actual_main_middlewares": True,
                        "v2_heartbeat_no_frames_and_capture_progress": True,
                        "http_multipart_upload": True, "ack_loss_and_restart_replay": True,
                        "content_dedup": True, "pause_drains_queue": True,
                        "owner_change_isolated": True, "token_rotation_recovers_pending": True,
                        "assets": 1, "capture_events": 3, "device": "not_run"}
            finally:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cvsprout-root", type=Path, required=True)
    parser.add_argument("--backend-python", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    # Keep a venv's interpreter symlink path: resolving it to /usr/bin/python
    # would silently discard that environment's installed backend dependencies.
    result = exercise(args.cvsprout_root.resolve(), args.backend_python.absolute())
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered)
    print(rendered)


if __name__ == "__main__":
    main()

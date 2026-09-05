"""Enrollment HTTP endpoint: dispatch rules, and one real socket round-trip.

`ops` is faked, so nothing here touches a model, the NPU, or the frame loop.
"""
import json
import urllib.error
import urllib.request
from concurrent.futures import Future

import pytest

from cmd_server import CmdServer

TAG = "rv1126b:scrfd500m+mbf512@fp16"


class FakeOps:
    def __init__(self, resolve=True):
        self.model_tag = TAG
        self.users = {"alice": {"n": 5}}
        self.jobs = []
        self.reloads = 0
        self.resolve = resolve          # False -> the future never completes

    def user_count(self):
        return len(self.users)

    def list_users(self):
        return [{"name": n, "n": r["n"]} for n, r in sorted(self.users.items())]

    def remove_user(self, name):
        return self.users.pop(name, None) is not None

    def reload(self):
        self.reloads += 1
        return len(self.users)

    def submit_enroll(self, job):
        self.jobs.append(job)
        fut = Future()
        if self.resolve:
            self.users[job["name"]] = {"n": job.get("frames") or 1}
            fut.set_result({"source": job["source"], "frames": 3})
        return fut


@pytest.fixture()
def srv():
    return CmdServer(0, FakeOps(), timeout=0.3)


class TestDispatch:
    def test_list(self, srv):
        status, body = srv.handle({"op": "list"})
        assert status == 200
        assert body["ok"] is True
        assert body["op"] == "list"
        assert body["model_tag"] == TAG
        assert body["users"] == 1
        assert body["names"] == [{"name": "alice", "n": 5}]

    def test_reload(self, srv):
        status, body = srv.handle({"op": "reload"})
        assert (status, body["ok"], srv.ops.reloads) == (200, True, 1)

    def test_remove(self, srv):
        status, body = srv.handle({"op": "remove", "name": "alice"})
        assert (status, body["ok"], body["users"]) == (200, True, 0)

    def test_remove_unknown_is_404(self, srv):
        status, body = srv.handle({"op": "remove", "name": "nobody"})
        assert status == 404 and body["ok"] is False and "nobody" in body["err"]

    def test_remove_without_a_name_is_400(self, srv):
        status, body = srv.handle({"op": "remove"})
        assert status == 400 and "name" in body["err"]

    def test_unknown_op_is_400(self, srv):
        status, body = srv.handle({"op": "drop_table"})
        assert status == 400 and body["ok"] is False

    def test_enroll_from_camera_goes_through_the_queue(self, srv):
        status, body = srv.handle({"op": "enroll", "name": "bob",
                                   "source": "camera", "frames": 4})
        assert status == 200 and body["ok"] is True
        assert srv.ops.jobs == [{"name": "bob", "source": "camera",
                                 "frames": 4, "image_b64": None}]
        assert body["frames"] == 3          # what the loop actually collected

    def test_enroll_defaults_to_the_camera_source(self, srv):
        srv.handle({"op": "enroll", "name": "bob"})
        assert srv.ops.jobs[0]["source"] == "camera"

    def test_enroll_from_image_requires_the_payload(self, srv):
        status, body = srv.handle({"op": "enroll", "name": "bob",
                                   "source": "image"})
        assert status == 400 and "image_b64" in body["err"]
        assert srv.ops.jobs == []

    def test_enroll_from_image_is_queued_with_its_payload(self, srv):
        status, _ = srv.handle({"op": "enroll", "name": "bob",
                                "source": "image", "image_b64": "Zm9v"})
        assert status == 200
        assert srv.ops.jobs[0]["image_b64"] == "Zm9v"

    def test_enroll_rejects_an_unknown_source(self, srv):
        status, body = srv.handle({"op": "enroll", "name": "bob",
                                   "source": "telepathy"})
        assert status == 400 and "source" in body["err"]

    def test_enroll_without_a_name_is_400(self, srv):
        status, body = srv.handle({"op": "enroll", "source": "camera"})
        assert status == 400 and "name" in body["err"]

    def test_enroll_times_out_instead_of_hanging_the_client(self):
        """★The loop may never see a usable face★ -- the request must return."""
        srv = CmdServer(0, FakeOps(resolve=False), timeout=0.2)
        status, body = srv.handle({"op": "enroll", "name": "bob"})
        assert status == 504 and body["ok"] is False and "timed out" in body["err"]

    def test_an_enroll_failure_surfaces_as_the_error_text(self, srv):
        def failing(job):
            fut = Future()
            fut.set_exception(ValueError("needs exactly 1 face, found 3"))
            return fut
        srv.ops.submit_enroll = failing
        status, body = srv.handle({"op": "enroll", "name": "bob"})
        assert status == 400 and "found 3" in body["err"]


class TestOverTheWire:
    @pytest.fixture()
    def live(self):
        s = CmdServer(0, FakeOps(), host="127.0.0.1", timeout=1.0).start()
        yield s
        s.stop()

    def _post(self, srv, obj):
        req = urllib.request.Request(
            f"http://127.0.0.1:{srv.port}/cmd",
            data=json.dumps(obj).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_bound_to_a_real_port(self, live):
        assert live.port > 0

    def test_post_cmd_list(self, live):
        status, body = self._post(live, {"op": "list"})
        assert status == 200 and body["names"][0]["name"] == "alice"

    def test_post_cmd_enroll(self, live):
        status, body = self._post(live, {"op": "enroll", "name": "bob",
                                         "source": "camera", "frames": 2})
        assert status == 200 and body["ok"] is True and body["users"] == 2

    def test_get_gallery_is_list(self, live):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{live.port}/gallery", timeout=5) as r:
            body = json.loads(r.read())
        assert body["op"] == "list" and body["model_tag"] == TAG

    def test_unknown_path_is_404(self, live):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{live.port}/nope",
                                   timeout=5)
            assert False, "expected 404"
        except urllib.error.HTTPError as e:
            assert e.code == 404

    def test_malformed_json_is_400(self, live):
        req = urllib.request.Request(f"http://127.0.0.1:{live.port}/cmd",
                                     data=b"{not json", method="POST")
        try:
            urllib.request.urlopen(req, timeout=5)
            assert False, "expected 400"
        except urllib.error.HTTPError as e:
            assert e.code == 400
            assert "bad JSON" in json.loads(e.read())["err"]


class TestDisabled:
    def test_port_zero_or_below_binds_nothing(self):
        s = CmdServer(-1, FakeOps()).start()
        assert s._httpd is None
        s.stop()

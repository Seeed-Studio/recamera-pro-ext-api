"""Large-package boundaries without allocating/transferring multi-GiB bodies."""
import http.client
import io
import json
import os
import sys
import tarfile
import threading
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from appmgr import installer, paths, server, uploads


def test_ten_gib_package_and_multipart_boundary():
    cap = 10 * 1024 ** 3
    assert paths.PACKAGE_SIZE_LIMIT_BYTES == cap
    assert paths.MAX_PKG_BYTES == cap
    assert paths.MAX_UNPACKED_BYTES >= cap
    assert paths.MAX_UPLOAD_STAGING_BYTES > cap + uploads.MAX_MULTIPART_OVERHEAD
    content_type = "multipart/form-data; boundary=boundary"
    request_cap = cap + uploads.MAX_MULTIPART_OVERHEAD
    assert uploads._request_shape(request_cap, content_type) == (request_cap, b"boundary")
    with pytest.raises(uploads.UploadSizeError):
        uploads._request_shape(request_cap + 1, content_type)


def test_sparse_package_path_accepts_exact_limit_and_rejects_one_extra_byte(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "ALLOWED_PKG_ROOTS", (str(tmp_path),))
    package = tmp_path / "large.tar.gz"
    with package.open("wb") as output:
        output.truncate(paths.MAX_PKG_BYTES)
    assert installer.validate_pkg_path(str(package)) == str(package)
    with package.open("r+b") as output:
        output.truncate(paths.MAX_PKG_BYTES + 1)
    with pytest.raises(installer.InstallError, match="package too large"):
        installer.validate_pkg_path(str(package))


@pytest.mark.parametrize("route,length,status", [
    ("/api/app-center/v1/uploads", 10 * 1024 ** 3 + 256 * 1024 + 1, 413),
    ("/api/appMgr/upload", 200 * 1024 ** 2 + 1, 400),
])
def test_oversized_headers_rejected_without_waiting_for_body(tmp_path, monkeypatch, route, length, status):
    monkeypatch.setattr(paths, "APPSTAGE_DIR", str(tmp_path / "stage"))
    httpd = server._AppHTTPServer(("127.0.0.1", 0), server._Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=2)
    try:
        conn.putrequest("POST", route)
        conn.putheader("Content-Type", "multipart/form-data; boundary=boundary")
        conn.putheader("Content-Length", str(length))
        conn.putheader("Connection", "close")
        conn.endheaders()  # Deliberately send no body.
        response = conn.getresponse()
        assert response.status == status
        payload = json.loads(response.read())
        if status == 413:
            assert payload["code"] == "upload_too_large"
            assert payload["max_package_bytes"] == 10 * 1024 ** 3
        assert not (tmp_path / "stage").exists()
    finally:
        conn.close()
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def test_part_limit_is_checked_independently_of_multipart_allowance(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "APPSTAGE_DIR", str(tmp_path / "stage"))
    monkeypatch.setattr(paths, "MAX_PKG_BYTES", 8)
    body = (b'--boundary\r\nContent-Disposition: form-data; name="package"; '
            b'filename="demo.tar.gz"\r\n\r\n123456789\r\n--boundary--\r\n')
    with pytest.raises(uploads.UploadSizeError, match="field exceeds"):
        uploads.receive(io.BytesIO(body), len(body), "multipart/form-data; boundary=boundary")
    assert os.listdir(paths.uploads_dir()) == []


def test_insufficient_upload_space_rejected_before_reading(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "APPSTAGE_DIR", str(tmp_path / "stage"))
    monkeypatch.setattr(uploads.shutil, "disk_usage", lambda _: SimpleNamespace(free=1024))

    class Unreadable:
        def read(self, *_):
            raise AssertionError("must reject before receiving the large body")

    with pytest.raises(uploads.StagingQuotaError, match="insufficient free space"):
        uploads.receive(Unreadable(), paths.MAX_PKG_BYTES,
                        "multipart/form-data; boundary=boundary")
    assert os.listdir(paths.uploads_dir()) == []


def test_tar_member_count_is_bounded_during_iteration(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "MAX_MEMBERS", 2)

    class Archive:
        def getmembers(self):
            raise AssertionError("must not materialize every header first")

        def __iter__(self):
            for i in range(3):
                yield tarfile.TarInfo(f"file-{i}")
            raise AssertionError("member scan must stop at the cap")

    with pytest.raises(installer.InstallError, match="too many members"):
        installer._vetted_tar_members(Archive(), str(tmp_path))


def test_extraction_preserves_disk_reserve_before_writing(tmp_path, monkeypatch):
    archive = tmp_path / "small.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        member = tarfile.TarInfo("payload")
        member.size = 32
        tar.addfile(member, io.BytesIO(b"x" * 32))
    monkeypatch.setattr(paths, "MIN_UPLOAD_FREE_BYTES", 16)
    monkeypatch.setattr(installer.shutil, "disk_usage", lambda _: SimpleNamespace(free=47))
    target = tmp_path / "unpacked"
    with tarfile.open(archive, "r:gz") as tar:
        with pytest.raises(installer.InsufficientStorageError, match="insufficient free space"):
            installer.extract_vetted_tar(tar, str(target))
    assert not target.exists()

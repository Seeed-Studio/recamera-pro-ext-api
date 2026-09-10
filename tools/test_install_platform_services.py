from __future__ import annotations

import re
import stat
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "tools/install_platform_services.py"


def _location_block(config: str, prefix: str) -> str:
    pattern = re.compile(
        rf"^\s*location\s+\^~\s+{re.escape(prefix)}\s*\{{",
        re.MULTILINE,
    )
    match = pattern.search(config)
    assert match is not None
    depth = 0
    for index in range(match.start(), len(config)):
        if config[index] == "{":
            depth += 1
        elif config[index] == "}":
            depth -= 1
            if depth == 0:
                return config[match.start() : index + 1]
    raise AssertionError(f"unterminated nginx location {prefix}")


def test_real_platform_sources_stage_without_test_or_cache_payload(tmp_path):
    rootfs = tmp_path / "root"
    oem = tmp_path / "oem"
    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo-root",
            str(REPO),
            "--rootfs",
            str(rootfs),
            "--oem",
            str(oem),
        ],
        check=True,
    )

    assert (rootfs / "usr/lib/recamera/appmgr/server.py").is_file()
    assert (rootfs / "usr/lib/recamera/appmgr/result_hub.py").is_file()
    assert (rootfs / "usr/lib/recamera/appmgr/visualization.py").is_file()
    assert (rootfs / "usr/lib/recamera/appmgr/inference_auth.py").is_file()
    assert (rootfs / "usr/lib/recamera/appmgr/trust.py").is_file()
    assert (rootfs / "usr/lib/recamera/inferenced/server.py").is_file()
    assert (rootfs / "usr/lib/recamera/inferenced/authorization.py").is_file()
    vendor_key = rootfs / "usr/lib/recamera/appmgr/keys/release_pub.pem"
    assert stat.S_IMODE(vendor_key.stat().st_mode) == 0o644
    assert (rootfs / "usr/lib/recamera/appmgr/service_health.py").is_file()
    assert (oem / "etc/init.d/S93inferenced").stat().st_mode & 0o100
    assert (oem / "etc/init.d/S94appmgr").stat().st_mode & 0o100
    assert not (rootfs / "etc/init.d/S93inferenced").exists()
    assert not (rootfs / "etc/init.d/S94appmgr").exists()
    assert (oem / "etc/nginx/ext_appmgr.conf").is_file()
    edge = (oem / "etc/nginx/ext_appmgr.conf").read_text()
    locations = re.findall(
        r"^\s*location\s+(?:=\s+)?([^\s{]+)", edge, re.MULTILINE)
    assert "/api/app-center/v1/" in locations
    assert "/ws/ai/results/v2" in locations
    assert "/api/v1/" not in locations
    assert len(locations) == len(set(locations))
    assert edge.count("$recamera_appcenter_origin_ok = 0") == 4
    assert edge.count("$recamera_ai_results_origin_ok = 0") == 1
    assert "proxy_pass http://127.0.0.1:8125;" in edge
    assert "proxy_set_header X-Forwarded-Proto $scheme;" in edge
    assert re.search(
        r"^\s*location\s+\^~\s+/appcenter/go2rtc/\s*\{", edge, re.MULTILINE
    )
    go2rtc = _location_block(edge, "/appcenter/go2rtc/")
    assert "set $recamera_go2rtc_connection close;" in go2rtc
    assert 'if ($http_upgrade ~* "^websocket$")' in go2rtc
    assert ("proxy_set_header Connection "
            "$recamera_go2rtc_connection;") in go2rtc
    assert not re.search(
        r"proxy_set_header\s+Connection\s+[\"']?Upgrade[\"']?\s*;",
        go2rtc,
        re.IGNORECASE,
    )
    assert ("proxy_set_header X-ReCamera-App-Center-Route "
            "authenticated-local-web-v1;") in edge
    for retired in ("upload", "putModel"):
        assert re.search(
            r"location\s+=\s+/api/appMgr/%s\s*\{\s*return\s+410;\s*\}" %
            retired,
            edge,
            re.MULTILINE,
        )
    launcher = (oem / "etc/init.d/S94appmgr").read_text()
    assert "APPMGR_PARENT=/usr/lib/recamera" in launcher
    assert "PYTHONPATH_VALUE=/usr/lib/recamera:/usr/lib/python3.11/site-packages" in launcher
    assert "APPMGR_KIT_PARENT_VALUE=/usr/lib/python3.11/site-packages" in launcher
    assert "LD_LIBRARY_PATH_VALUE=/usr/lib:/oem/usr/lib:/oem/lib" in launcher
    assert "reinject_nginx" not in launcher
    inference_launcher = (oem / "etc/init.d/S93inferenced").read_text()
    assert "AUTH_DIR=/run/recamera/inference-authorizations" in inference_launcher
    assert '--authorization-dir "$AUTH_DIR"' in inference_launcher
    platform = rootfs / "usr/lib/recamera"
    assert not list(platform.rglob("test_*.py"))
    assert not list(platform.rglob("__pycache__"))
    assert not list(platform.rglob("*.pyc"))


def test_incremental_stage_removes_old_launchers_and_ota_masters(tmp_path):
    rootfs, oem = tmp_path / "root", tmp_path / "oem"
    retired = [rootfs / directory / name
               for directory in ("etc/init.d", "userdata/config/system/etc/init.d")
               for name in ("S93inferenced", "S94appmgr")]
    for path in retired:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n# old userdata code launcher\n")
    sibling = rootfs / "etc/init.d/S95other"
    sibling.write_text("preserve this unrelated service")
    cache = rootfs / "usr/lib/recamera/appmgr/__pycache__/old.pyc"
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"old bytecode")

    for _ in range(2):
        subprocess.run([
            sys.executable, str(SCRIPT), "--repo-root", str(REPO),
            "--rootfs", str(rootfs), "--oem", str(oem),
        ], check=True)
        assert all(not path.exists() for path in retired)
        assert not cache.exists()
        assert sibling.read_text() == "preserve this unrelated service"
        for name in ("S93inferenced", "S94appmgr"):
            assert (oem / "etc/init.d" / name).read_bytes() == (
                REPO / "market/deploy" / name).read_bytes()

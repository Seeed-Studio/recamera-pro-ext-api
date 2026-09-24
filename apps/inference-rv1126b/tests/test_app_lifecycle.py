"""Real Kit launcher regression checks; no models or camera requests."""

import errno
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("INFERENCE_ENGINE_ROOT"),
    reason="Set INFERENCE_ENGINE_ROOT to the pinned engine checkout",
)

APP_DIR = Path(__file__).resolve().parents[1]
SDK_ROOT = APP_DIR.parents[1]
ENGINE_ROOT = Path(os.environ.get("INFERENCE_ENGINE_ROOT", "/__unconfigured_engine__"))


@pytest.mark.parametrize(
    "http_occupied,cached_platform_typing,fresh_lan",
    [(False, False, False), (True, False, False), (False, True, False),
     (False, False, True)],
)
def test_real_kit_launcher_ignores_occupied_overlay_port(
    tmp_path, http_occupied, cached_platform_typing, fresh_lan
):
    launcher = SDK_ROOT / "kit/run.py"
    if not launcher.is_file():
        pytest.skip("Set RECAMERA_SDK_TEST_ROOT to test against the real Kit launcher")

    with socket.socket() as overlay, socket.socket() as http_reservation:
        try:
            overlay.bind(("127.0.0.1", 8124))
            overlay.listen()
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
            # An existing host listener is also an occupied-port regression.

        http_reservation.bind(("127.0.0.1", 0))
        port = http_reservation.getsockname()[1]
        if http_occupied:
            http_reservation.listen()
        else:
            http_reservation.close()

        ready = tmp_path / "ready"
        app_directory = tmp_path / "installed-app"
        app_directory.mkdir()
        shutil.copyfile(APP_DIR / "app.py", app_directory / "app.py")
        shutil.copyfile(
            APP_DIR / "manifest.json",
            app_directory / "manifest.json",
        )
        appdata = tmp_path / "appdata"
        user_data = appdata / "inference-rv1126b"
        user_data.mkdir(parents=True)
        initial = {"port": port} if fresh_lan else {
            "host": "127.0.0.1", "port": port, "api_token": ""}
        (user_data / "config.json").write_text(json.dumps(initial))
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("RECAMERA_", "APPMGR_"))
        }
        env.update(
            PYTHONPATH=os.pathsep.join((str(SDK_ROOT / "market"), str(SDK_ROOT), str(ENGINE_ROOT))),
            INFERENCE_RUNTIME_PROFILE="rv1126b",
            INFERENCE_EDGE_HOST="127.0.0.1",
            INFERENCE_EDGE_PORT="invalid-superseded-development-setting",
            APPMGR_READY_FILE=str(ready),
            APPMGR_APPDATA_DIR=str(appdata),
            OPENBLAS_NUM_THREADS="1",
            OMP_NUM_THREADS="1",
        )
        command = [
            sys.executable,
            str(launcher),
            str(app_directory / "app.py"),
            "--quiet",
        ]
        if cached_platform_typing:
            platform = tmp_path / "site-packages"
            platform.mkdir()
            (platform / "typing_extensions.py").write_text(
                "OLD_FIRMWARE_MODULE = True\n"
            )
            env["PYTHONPATH"] = str(platform) + os.pathsep + env["PYTHONPATH"]
            driver = tmp_path / "cached_platform_launcher.py"
            driver.write_text(
                "import os, runpy, sys, typing_extensions\n"
                "print('cached_old_typing=' + str(getattr(typing_extensions, "
                "'OLD_FIRMWARE_MODULE', False)), flush=True)\n"
                "sys.argv = sys.argv[1:]\n"
                "runpy.run_path(sys.argv[0], run_name='__main__')\n"
            )
            command.insert(1, str(driver))
        log_path = tmp_path / "app.log"
        with log_path.open("w+") as log:
            process = subprocess.Popen(
                command,
                env=env,
                cwd=tmp_path,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                deadline = time.monotonic() + 30
                while process.poll() is None and not ready.exists():
                    if time.monotonic() > deadline:
                        pytest.fail("Kit startup timed out: " + log_path.read_text())
                    time.sleep(0.05)
                if http_occupied:
                    process.wait(timeout=10)
                    assert process.returncode != 0
                    assert not ready.exists(), log_path.read_text()
                    assert "HTTP server failed to start" in log_path.read_text()
                else:
                    assert process.poll() is None, log_path.read_text()
                    assert ready.read_text() == str(process.pid)
                    if cached_platform_typing:
                        # The inherited log descriptor and same-PID READY
                        # survive exec; the second interpreter has no stale
                        # platform module in sys.modules.
                        text = log_path.read_text()
                        assert text.count("cached_old_typing=True") == 1
                        assert text.count("cached_old_typing=False") == 1
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/healthz", timeout=3
                    ) as response:
                        assert json.load(response)["profile"] == "rv1126b"
                    if fresh_lan:
                        saved = json.loads((user_data / "config.json").read_text())
                        assert len(saved["api_token"]) >= 32
                        assert saved["port"] == port
                        assert saved["api_token"] not in log_path.read_text()
                        with pytest.raises(urllib.error.HTTPError) as refused:
                            urllib.request.urlopen(
                                f"http://127.0.0.1:{port}/app-center/workflow-runtime",
                                timeout=3,
                            )
                        assert refused.value.code == 401
                        # Use the actual AppMgr exchange, with the generated
                        # credential read from managed config, to open Builder.
                        check = subprocess.run(
                            [sys.executable, "-c",
                             "import json,sys; from appmgr import workflow_ui; "
                             "m=json.load(open(sys.argv[1])); "
                             "r=workflow_ui.editor_session(m,m['id']); "
                             "assert r['runtime_path'].startswith('/ui/runtime/')" ,
                             str(app_directory / "manifest.json")],
                            env=env, capture_output=True, text=True, timeout=20,
                        )
                        assert check.returncode == 0, check.stdout + check.stderr
                    children = Path(f"/proc/{process.pid}/task/{process.pid}/children")
                    if children.exists():
                        assert children.read_text().strip() == ""
                    process.send_signal(signal.SIGTERM)
                    process.wait(timeout=15)
                    assert process.returncode == 0, log_path.read_text()
                    with pytest.raises(urllib.error.URLError):
                        urllib.request.urlopen(
                            f"http://127.0.0.1:{port}/healthz", timeout=0.5
                        )
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)

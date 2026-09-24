"""App Center entry: HTTP, camera and model sessions share the authorized PID."""

import os
import secrets
import sys
import sysconfig
import threading
import time
from pathlib import Path

os.environ["INFERENCE_RUNTIME_PROFILE"] = "rv1126b"
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")


def _bootstrap_release_dependencies():
    """Keep firmware Kit imports from shadowing this release's wheel packages.

    Kit 0.1/appmgr puts the firmware site-packages on PYTHONPATH before loading
    the app, and Kit may already have imported firmware PIL/cffi. Changing paths
    or deleting sys.modules entries here would mix old and new Python/C objects.
    Re-exec the same interpreter and original launcher argv once instead. PID,
    process start time, stdout/stderr and the appmgr READY-file contract survive;
    no model session, camera or application-owned resource has opened yet.
    """
    prefix = Path(sys.prefix).resolve()
    if prefix == Path(sys.base_prefix).resolve():
        raise RuntimeError(
            "The App Center entry requires its private virtual environment"
        )
    private = []
    for key in ("purelib", "platlib"):
        path = Path(sysconfig.get_path(key)).resolve()
        if not path.is_relative_to(prefix) or not path.is_dir():
            raise RuntimeError(
                "Release dependency path is outside the active virtual environment"
            )
        if str(path) not in private:
            private.append(str(path))
    ordered = [str(Path(path or os.getcwd()).resolve()) for path in sys.path]
    first_private = min(
        (ordered.index(path) for path in private if path in ordered),
        default=len(ordered),
    )
    shadowed = any(
        Path(path).name in ("site-packages", "dist-packages") and path not in private
        for path in ordered[:first_private]
    )
    owned = {
        member.name.split(".")[0]
        for directory in private
        for member in Path(directory).iterdir()
        if member.is_dir() or member.suffix in (".py", ".pyc", ".so")
    }
    for name, module in tuple(sys.modules.items()):
        origin = getattr(module, "__file__", None)
        if "." not in name and name in owned and origin:
            loaded = Path(origin).resolve()
            if "site-packages" in loaded.parts and not any(
                loaded.is_relative_to(path) for path in map(Path, private)
            ):
                shadowed = True
    if not shadowed:
        # Put the selected release first for later lazy Workflow imports too.
        sys.path[:] = private + [
            path
            for path in sys.path
            if str(Path(path or os.getcwd()).resolve()) not in private
        ]
        return
    marker = "INFERENCE_EDGE_BOOTSTRAPPED_PREFIX"
    if os.environ.get(marker) == str(prefix):
        raise RuntimeError(
            "Firmware packages still shadow release dependencies after bootstrap"
        )
    inherited = [
        path for path in os.environ.get("PYTHONPATH", "").split(os.pathsep) if path
    ]
    kit_module = sys.modules.get("kit")
    if getattr(kit_module, "__file__", None):
        # Keep KIT_PARENT present so kit.run does not insert it at index zero.
        inherited.append(str(Path(kit_module.__file__).resolve().parent.parent))
    os.environ["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(private + inherited))
    os.environ[marker] = str(prefix)
    argv = list(sys.orig_argv)
    argv[0] = sys.executable
    os.execv(sys.executable, argv)


_bootstrap_release_dependencies()

from kit.app import App, run_app


def managed_access_config(config):
    """Persist a per-device credential before opening the LAN listener.

    AppMgr's editor/results bridge reads this same managed configuration. Use
    its atomic merge writer so the generated credential survives upgrades and
    is visible in the existing password field, without a firmware change.
    """
    values = dict(config)
    values.setdefault("host", "0.0.0.0")
    if values["host"] != "0.0.0.0" or values.get("api_token"):
        return values
    from appmgr import config as appconfig

    token = appconfig.load_user_config(InferenceEdgeApp.id).get("api_token")
    if not token:
        token = secrets.token_urlsafe(32)
        appconfig.write_user_config(InferenceEdgeApp.id, {"api_token": token})
    values["api_token"] = token
    return values


class InferenceEdgeApp(App):
    id = "inference-rv1126b"
    name = "Inference RV1126B"
    owns_loop = True
    needs_model = False
    needs_frames = False

    def __init__(self):
        super().__init__()
        kit_app = sys.modules["kit.app"]
        original_factory = kit_app.open_result_sink

        def restore_factory():
            if kit_app.open_result_sink is select_sink:
                kit_app.open_result_sink = original_factory

        def select_sink(kind="ws", **kwargs):
            # Kit 0.1's launcher opens its default WS sink before App.start(),
            # and appmgr cannot pass --sink stdout through manifest-v2. This
            # HTTP-only app has no result.publish permission or overlay port.
            # Adapt only this app's initial selection in its own process, then
            # restore the SDK factory before opening the official stdout sink.
            if kind == "ws" and kwargs.get("app_id") == self.id:
                restore_factory()
                return original_factory("stdout")
            return original_factory(kind, **kwargs)

        self._restore_sink_factory = restore_factory
        kit_app.open_result_sink = select_sink

    def setup(self, config):
        config = managed_access_config(config)
        super().setup(config)
        from kit.config import appdata_root, app_dir_of

        from inference.edge.settings import EdgeSettings

        self._edge_settings = EdgeSettings.from_app_config(
            config,
            model_root=Path(app_dir_of(self)) / "models",
            storage_root=Path(appdata_root()).resolve() / self.id,
        )
        self._server = None
        self._http_thread = None
        self._http_error = None

    def prepare_runtime(self):
        import cv2
        import uvicorn

        from inference.edge.api import create_app
        from inference.edge.models import EdgeModelManager, RegisteredModelStore
        from appmgr.workflow_model_contract import bindings

        cv2.setNumThreads(1)
        settings = self._edge_settings
        store = RegisteredModelStore(settings.model_root, bindings(self.id, verify=True))
        manager = EdgeModelManager(store)
        store.validate(manager)
        application = create_app(settings, manager=manager)
        self._application = application
        self._server = uvicorn.Server(
            uvicorn.Config(
                application,
                host=settings.host,
                port=settings.port,
                workers=1,
                access_log=False,
                timeout_graceful_shutdown=5,
            )
        )

        def serve():
            try:
                self._server.run()
            except BaseException as exc:
                self._http_error = exc

        self._http_thread = threading.Thread(
            target=serve, name="inference-http", daemon=True
        )
        self._http_thread.start()
        deadline = time.monotonic() + 20
        while not self._server.started:
            if not self._http_thread.is_alive():
                raise RuntimeError(
                    "Inference HTTP server failed to start"
                ) from self._http_error
            if time.monotonic() >= deadline:
                raise RuntimeError("Inference HTTP server startup exceeded 20 seconds")
            time.sleep(0.05)
        # A selected Workflow is the app's deployed workload. READY requires
        # its first successful frame, rather than just an open HTTP listener.
        if settings.workflow_autostart:
            deadline = time.monotonic() + 30
            while application.state.video.status()["frames"] == 0:
                state = application.state.deployment.status()
                if state.get("error"):
                    raise RuntimeError(state["error"]["message"])
                if time.monotonic() >= deadline:
                    raise RuntimeError("Workflow did not produce its first result within 30 seconds")
                time.sleep(0.05)

    def run(self):
        while not self._stop_flag:
            if not self._http_thread.is_alive():
                raise RuntimeError("Inference HTTP server exited") from self._http_error
            state = self._application.state.deployment.status()
            if self._edge_settings.workflow_autostart and state.get("error"):
                raise RuntimeError(state["error"]["message"])
            self.tick()
            time.sleep(0.1)

    def finish(self):
        try:
            self._restore_sink_factory()
            server = getattr(self, "_server", None)
            thread = getattr(self, "_http_thread", None)
            if server:
                server.should_exit = True
            if thread:
                thread.join(timeout=10)
                if thread.is_alive():
                    raise RuntimeError(
                        "Inference worker failed to stop; process must exit"
                    )
        finally:
            super().finish()


if __name__ == "__main__":
    run_app(InferenceEdgeApp())

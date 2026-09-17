"""Durable, bounded Workflow model preparation, independent of browser lifetime."""
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from . import config, paths, state, workflow_ui, workflow_cloud as cloud
from . import workflow_model_contract as contract
from . import workflow_roboflow as roboflow, workflow_onnx

ACTIVE = {"queued", "fetching", "uploading", "converting", "downloading", "validating"}
TERMINAL = {"registered", "failed", "cancelled", "submission_uncertain", "removed"}


class ModelConflict(ValueError):
    code = "model_task_conflict"


def bundled(app_id):
    result = []
    directory = Path(paths.app_dir(app_id)) / "models"
    for path in sorted(directory.glob("**/model.json"))[:64]:
        if not path.resolve().is_relative_to(directory.resolve()):
            continue
        doc = contract.read_json(path)
        contract.metadata(doc)
        result.append(doc)
    return result


def requirements(manifest, app_id, available):
    """Keep original Workflow JSON; resolve configured dynamic model inputs."""
    values = config.effective_values(manifest, app_id)
    result = []
    for item in workflow_ui.workflows(manifest, app_id, include_documents=True)["workflows"]:
        doc = item.pop("document")
        try:
            spec = doc.get("config", doc)
            if isinstance(spec, str):
                spec = json.loads(spec)
            spec = spec.get("specification", spec)
            params = {i["name"]: i["default_value"] for i in spec.get("inputs", [])
                      if isinstance(i, dict) and "name" in i and "default_value" in i}
            if item["id"] == values.get("workflow_id"):
                overrides = values.get("workflow_parameters", {})
                params.update(json.loads(overrides) if isinstance(overrides, str) else overrides)
            required, unresolved = set(), set()
            for step in spec.get("steps", []):
                if not isinstance(step, dict) or "model_id" not in step:
                    continue
                identifier = step["model_id"]
                if isinstance(identifier, str) and identifier.startswith("$inputs."):
                    name = identifier[8:]
                    identifier = params.get(name)
                    if not isinstance(identifier, str) or identifier.startswith("$"):
                        unresolved.add(name)
                        continue
                required.add(contract.model_id(identifier))
            result.append({**item, "models": sorted(required), "missing": sorted(required - available),
                           "unresolved": sorted(unresolved), "selected": item["id"] == values.get("workflow_id")})
        except (ValueError, TypeError, AttributeError):
            result.append({**item, "error": "workflow_model_inputs_invalid"})
    return result


class Manager:
    def __init__(self, activate=None):
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.active = set()
        self.thread = None
        self.activate = activate
        self.last_discovery = 0
        self.login_checks = {}

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name="workflow-models", daemon=True)
            self.thread.start()

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=50)

    def _path(self, app_id, identifier):
        if not paths.valid_app_id(app_id) or not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise ValueError("Invalid model task identity")
        return contract.root() / app_id / "jobs" / identifier

    def _jobs(self, app_id):
        directory = contract.root() / app_id / "jobs"
        return [contract.read_json(p) for p in sorted(directory.glob("*/task.json"))][:32]

    def _save(self, task, **updates):
        with self.lock:
            task.update(updates, updated_at=time.time())
            contract.atomic_json(self._path(task["app_id"], task["id"]) / "task.json", task)

    @staticmethod
    def public(task):
        return {k: v for k, v in task.items() if k not in {"metadata", "user_id"}}

    def snapshot(self, manifest, app_id):
        workflow_ui.require(manifest)
        local = bundled(app_id)
        registered = contract.bindings(app_id)
        validation = {}
        if (state.get_app(app_id) or {}).get("pid"):
            try:
                response, _ = workflow_ui._request(config.effective_values(manifest, app_id), "/app-center/models")
                validation = response.get("validation", {})
            except RuntimeError:
                pass
        models = [{"model_id": d["model_id"], "sha256": d["sha256"], "status": "bundled", "metadata": d} for d in local]
        for item in registered:
            report = validation.get(item["model_id"], {})
            status = report.get("status") if report.get("sha256") == item["sha256"] else "restart_required"
            models.append({"model_id": item["model_id"], "sha256": item["sha256"], "status": status,
                           "error": report.get("error"), "metadata": item["metadata"]})
        available = {m["model_id"] for m in models if m["status"] in {"bundled", "ready"}}
        with self.lock:
            jobs = [self.public(t) for t in self._jobs(app_id)]
        return {"models": models, "tasks": sorted(jobs, key=lambda t: -t["created_at"]),
                "workflows": requirements(manifest, app_id, available),
                "automatic": True, "supported_architectures": list(workflow_onnx.SUPPORTED_ARCHITECTURES)}

    def create(self, manifest, app_id, body):
        workflow_ui.require(manifest)
        if not isinstance(body, dict) or set(body) - {"mode", "metadata", "cloud_id", "dataset", "model_id", "filename"}:
            raise ValueError("Invalid model preparation request")
        mode = body.get("mode")
        if mode not in {"onnx", "rknn", "cloud", "roboflow", "auto"}:
            raise ValueError("Choose ONNX conversion, RKNN import or a SenseCraft task")
        automatic = mode in {"roboflow", "auto"}
        if automatic:
            identifier = body.get("model_id")
            if not identifier and mode == "auto":
                filename = str(body.get("filename", ""))
                if not filename.lower().endswith(".onnx") or len(filename) > 256:
                    raise ValueError("Choose an ONNX model")
                identifier = (re.sub(r"[^A-Za-z0-9_-]", "-", Path(filename).stem)[:80].strip("-") or "uploaded") + "/1"
            doc = None
            identifier = contract.model_id(identifier)
        else:
            doc = contract.metadata(body.get("metadata"))
            identifier = doc["model_id"]
        cloud_id = body.get("cloud_id", "")
        if mode == "cloud" and not re.fullmatch(r"[\w-]{1,160}", str(cloud_id)):
            raise ValueError("Invalid SenseCraft task ID")
        if type(body.get("dataset", False)) is not bool:
            raise ValueError("Invalid calibration dataset selection")
        with self.lock:
            jobs = self._jobs(app_id)
            existing = contract.bindings(app_id)
            ids = {m["model_id"] for m in bundled(app_id) + existing}
            if identifier in ids or any(t["model_id"] == identifier and t["state"] not in TERMINAL for t in jobs):
                raise ModelConflict("Model ID already exists; choose a new version ID")
            if len(existing) + sum(t["state"] not in TERMINAL for t in jobs) >= contract.MAX_MODELS:
                raise ModelConflict("At most four additional Workflow models may be prepared")
            # Bound task history as well as files. Never discard an active task.
            for old in sorted(jobs, key=lambda t: t["created_at"]):
                if len(jobs) < 24:
                    break
                if old["state"] in TERMINAL:
                    shutil.rmtree(self._path(app_id, old["id"]))
                    jobs.remove(old)
            if len(jobs) >= 24:
                raise ModelConflict("Too many model preparation tasks")
            task = {"id": uuid.uuid4().hex, "app_id": app_id, "model_id": identifier, "mode": mode,
                    "metadata": doc, "cloud_id": str(cloud_id), "dataset": mode == "onnx" and body.get("dataset", False),
                    "state": "queued" if mode in {"cloud", "roboflow"} else "awaiting_upload", "created_at": time.time(),
                    "files": {}, "progress": 0, "error": None, "auto_activate": automatic}
            self._save(task)
        self.wake.set()
        return self.public(task)

    def remove(self, manifest, app_id, identifier):
        workflow_ui.require(manifest)
        contract.model_id(identifier)
        record = state.get_app(app_id) or {}
        if record.get("pid") or record.get("desired_state") == "running":
            raise ModelConflict("Stop the application before removing a model")
        with self.lock:
            if any(identifier in item.get("models", []) or item.get("unresolved")
                   for item in requirements(manifest, app_id, set())):
                raise ModelConflict("Model may be referenced by a saved Workflow; update that Workflow first")
            items = contract.bindings(app_id)
            found = next((m for m in items if m["model_id"] == identifier), None)
            if not found:
                raise ValueError("Only additional registered models can be removed")
            contract.atomic_json(contract.root() / app_id / "bindings.json", [
                {k: m[k] for k in ("model_id", "sha256")} for m in items if m["model_id"] != identifier])
            shutil.rmtree(found["directory"])
            for task in self._jobs(app_id):
                if task["model_id"] == identifier and task["state"] == "registered":
                    self._save(task, state="removed")
        return {"removed": identifier}

    def upload(self, app_id, identifier, slot, stream, length):
        if slot not in {"source", "dataset"} or not 0 < length <= contract.MAX_MODEL:
            raise ValueError("Files must be between 1 byte and 256 MiB")
        directory = self._path(app_id, identifier)
        key = (app_id, identifier)
        with self.lock:
            task = contract.read_json(directory / "task.json")
            if key in self.active or task["state"] != "awaiting_upload" or (slot == "dataset" and not task["dataset"]):
                raise ModelConflict("Task is not accepting this upload")
            if self.active and any(k[0] == "upload" for k in self.active):
                raise ModelConflict("Another model file is uploading")
            staged = sum(p.stat().st_size for p in contract.root().glob("*/jobs/*/*") if p.is_file())
            if staged + length > 768 * 1024 * 1024 or shutil.disk_usage(directory).free < length + 384 * 1024 * 1024:
                raise ModelConflict("Not enough model staging space; cancel unused tasks")
            self.active.update({key, ("upload", identifier)})
        temporary = directory / (slot + ".part")
        try:
            digest = hashlib.sha256()
            remaining = length
            with open(temporary, "wb") as target:
                while remaining:
                    chunk = stream.read(min(65536, remaining))
                    if not chunk:
                        raise ValueError("Incomplete model upload; upload the file again")
                    target.write(chunk)
                    digest.update(chunk)
                    remaining -= len(chunk)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, directory / slot)
            task["files"][slot] = {"sha256": digest.hexdigest(), "size": length}
            self._save(task)
            return self.public(task)
        finally:
            temporary.unlink(missing_ok=True)
            with self.lock:
                self.active.difference_update({key, ("upload", identifier)})

    def action(self, app_id, identifier, action, body):
        with self.lock:
            task = contract.read_json(self._path(app_id, identifier) / "task.json")
            if (app_id, identifier) in self.active:
                raise ModelConflict("Task is busy; try again shortly")
            if action == "cancel":
                if task["state"] == "registered":
                    raise ModelConflict("An installed model cannot be cancelled")
                self._save(task, state="cancelled", error=None)
                self._cleanup(task)
            elif action == "resume":
                if task["state"] not in {"awaiting_upload", "awaiting_login", "failed", "submission_uncertain"} and not (
                        task["state"] == "cancelled" and task["mode"] == "roboflow"):
                    raise ModelConflict("Task cannot be resumed in its current state")
                if task["state"] == "cancelled" and not task["cloud_id"]:
                    task.update(metadata=None, files={})
                if task["state"] == "submission_uncertain":
                    candidate = body.get("cloud_id", "")
                    if not re.fullmatch(r"[\w-]{1,160}", str(candidate)):
                        raise ValueError("Select the existing SenseCraft task to avoid duplicate conversion")
                    task["cloud_id"] = str(candidate)
                if task["mode"] != "roboflow" and not task["cloud_id"] and ("source" not in task["files"] or task["dataset"] and "dataset" not in task["files"]):
                    raise ValueError("Upload the model and selected calibration dataset first")
                self._save(task, state="queued", error=None, attempts=0, next_poll=0)
            else:
                raise ValueError("Unknown model task action")
        self.wake.set()
        return self.public(task)

    def _cleanup(self, task):
        directory = self._path(task["app_id"], task["id"])
        for filename in ("source", "dataset", "model.part", "source.part", "dataset.part", "class_names.txt", "inference_config.json"):
            (directory / filename).unlink(missing_ok=True)

    def _register(self, task, asset):
        if task["mode"] in {"roboflow", "auto"}:
            self._save(task, metadata=workflow_onnx.converted_metadata(Path(asset), task["metadata"]))
        with open(asset, "rb") as stream:
            if stream.read(4) != b"RKNN":
                raise ValueError("invalid_rknn_file")
            stream.seek(0)
            digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(65536), b""):
                digest.update(chunk)
        digest = digest.hexdigest()
        size = os.stat(asset).st_size
        if not 8 < size <= contract.MAX_MODEL:
            raise ValueError("model_too_large")
        destination = contract.root() / task["app_id"] / "assets" / digest / task["model_id"]
        with self.lock:
            items = contract.bindings(task["app_id"], verify=True)
            existing = next((m for m in items if m["model_id"] == task["model_id"]), None)
            if existing and existing["sha256"] != digest:
                raise ModelConflict("model_id_conflict")
            if not existing:
                if len(items) >= contract.MAX_MODELS:
                    raise ModelConflict("model_limit_reached")
                if shutil.disk_usage(asset).free < size + 128 * 1024 * 1024:
                    raise ModelConflict("insufficient_storage")
                from .inference_auth import estimate_model_memory_mb
                installed = contract.read_json(Path(paths.app_dir(task["app_id"])) / "manifest.json")
                reserved = sum(estimate_model_memory_mb(a["size"]) for a in installed.get("artifacts", []) if a.get("kind") == "rknn")
                reserved += sum(estimate_model_memory_mb(m["size"]) for m in items) + estimate_model_memory_mb(size)
                limit = min(768, installed.get("resources", {}).get("limits", {}).get("memory_mb", 768))
                if reserved > limit:
                    raise ModelConflict("model_memory_limit")
                contract.private_directory(destination)
                shutil.copyfile(asset, destination / "model.rknn")
                with open(destination / "model.rknn", "rb") as stream:
                    os.fsync(stream.fileno())
                doc = {**task["metadata"], "sha256": digest, "memory_mb": max(64, estimate_model_memory_mb(size))}
                contract.atomic_json(destination / "model.json", doc)
                bindings = [{k: m[k] for k in ("model_id", "sha256")} for m in items]
                bindings.append({"model_id": task["model_id"], "sha256": digest})
                contract.atomic_json(contract.root() / task["app_id"] / "bindings.json", bindings)
            self._save(task, state="registered", progress=100, sha256=digest, error=None)
            self._cleanup(task)

    def _step(self, task):
        directory = self._path(task["app_id"], task["id"])
        try:
            installed = contract.read_json(Path(paths.app_dir(task["app_id"])) / "manifest.json")
            if not workflow_ui.supported(installed):
                raise ModelConflict("application_unavailable")
            if task["mode"] == "roboflow" and not task.get("metadata"):
                if shutil.disk_usage(directory).free < contract.MAX_MODEL + 384 * 1024 * 1024:
                    raise ModelConflict("insufficient_storage")
                self._save(task, state="fetching", error=None)
                values = config.effective_values(installed, task["app_id"])
                prepared = roboflow.prepare(task["model_id"], directory, values.get("roboflow_api_key") or None)
                self._save(task, metadata=prepared["metadata"], files={"source": prepared["source"]},
                           resolved_model_id=prepared["resolved_model_id"], package_id=prepared["package_id"],
                           architecture=prepared["architecture"], state="queued")
            if task["mode"] == "auto" and not task.get("metadata"):
                self._save(task, metadata=workflow_onnx.metadata(directory / "source", task["model_id"]))
            if task["mode"] == "rknn":
                self._save(task, state="validating")
                self._register(task, directory / "source")
                return
            session = cloud.credentials()
            if task.get("user_id") and task["user_id"] != session["user_id"]:
                self._save(task, state="awaiting_login", error="account_changed")
                return
            self._save(task, user_id=session["user_id"])
            if task["mode"] in {"roboflow", "auto"} and not task["cloud_id"]:
                for previous in self._jobs(task["app_id"]):
                    # Reuse a completed conversion of identical weights and preprocessing.
                    # Credentials and signed URLs never enter this cache key.
                    if (previous["id"] != task["id"] and previous["state"] in {"registered", "removed"}
                            and previous["mode"] in {"roboflow", "auto"} and not previous.get("dataset")
                            and previous.get("cloud_id") and previous.get("user_id") == session["user_id"]
                            and previous.get("files", {}).get("source", {}).get("sha256") == task["files"].get("source", {}).get("sha256")
                            and {k: v for k, v in previous.get("metadata", {}).get("input", {}).items() if k != "padding_value"}
                            == {k: v for k, v in task["metadata"]["input"].items() if k != "padding_value"}
                            and previous.get("metadata", {}).get("labels") == task["metadata"]["labels"]):
                        self._save(task, cloud_id=previous["cloud_id"], reused_conversion=True)
                        break
            if not task["cloud_id"]:
                self._save(task, state="uploading")
                identifier = cloud.submit(session, directory / "source", directory / "dataset" if task["dataset"] else None, task["model_id"])
                self._save(task, cloud_id=identifier, state="converting")
            response = cloud.query("train_status", session, model_id=task["cloud_id"])
            status = response.get("status") if isinstance(response, dict) else None
            if status == "error":
                raise cloud.CloudError("conversion_failed")
            if status != "done":
                if status not in ("init", "queued", "running"):
                    try:
                        progress = min(99, max(0, int(float(status))))
                    except (ValueError, TypeError, OverflowError):
                        raise cloud.CloudError("cloud_response_invalid") from None
                else:
                    progress = task.get("progress", 0)
                if time.time() - task["created_at"] > 24 * 3600:
                    raise cloud.CloudError("conversion_timeout")
                self._save(task, state="converting", progress=progress, next_poll=time.time()+8, attempts=0, error=None)
                return
            if shutil.disk_usage(directory).free < contract.MAX_MODEL + 128 * 1024 * 1024:
                raise ModelConflict("insufficient_storage")
            self._save(task, state="downloading", progress=100)
            cloud.download(session, task["cloud_id"], directory / "model.part")
            self._save(task, state="validating")
            self._register(task, directory / "model.part")
        except cloud.LoginRequired:
            self._save(task, state="awaiting_login", error="sensecraft_login_required", next_poll=time.time()+15)
        except (roboflow.DownloadError, workflow_onnx.ModelFormatError) as exc:
            attempts = task.get("attempts", 0) + 1
            retry = str(exc) == "roboflow_unreachable" and attempts <= 3
            self._save(task, state="queued" if retry else "failed", error=str(exc), attempts=attempts,
                       architecture=getattr(exc, "architecture", None) or task.get("architecture"),
                       next_poll=time.time()+min(60, attempts*10))
        except cloud.SubmissionUncertain:
            self._save(task, state="submission_uncertain", error="submission_uncertain")
        except (OSError, TimeoutError, cloud.CloudError) as exc:
            attempts = task.get("attempts", 0) + 1
            code = str(exc) if isinstance(exc, cloud.CloudError) else "cloud_unreachable"
            retry = task.get("cloud_id") and attempts <= 6 and code in {"cloud_unreachable", "cloud_request_failed", "download_incomplete"}
            self._save(task, state="converting" if retry else "failed", attempts=attempts,
                       next_poll=time.time()+min(60, attempts*10), error=code)
        except ModelConflict as exc:
            self._save(task, state="failed", error=str(exc))
        except Exception:
            self._save(task, state="failed", error="model_validation_failed")

    def discover(self, manifest, app_id):
        """A saved Workflow is the durable request; never depend on a browser tab."""
        with self.lock:
            available = {m["model_id"] for m in bundled(app_id) + contract.bindings(app_id)}
            attempted = {t["model_id"] for t in self._jobs(app_id) if t["state"] != "removed"}
            for workflow in requirements(manifest, app_id, available):
                for identifier in workflow.get("missing", []):
                    if identifier not in attempted:
                        try:
                            self.create(manifest, app_id, {"mode": "roboflow", "model_id": identifier})
                            attempted.add(identifier)
                        except ModelConflict:
                            return  # Surface remaining requirements without exceeding the model budget.

    def dependency(self, app_id, manifest):
        if not workflow_ui.supported(manifest):
            return None
        available = {m["model_id"] for m in bundled(app_id) + contract.bindings(app_id)}
        for workflow in requirements(manifest, app_id, available):
            if workflow.get("selected") and (workflow.get("missing") or workflow.get("unresolved") or workflow.get("error")):
                self.wake.set()
                return {"available": False, "socket": "workflow-models", "error": "workflow_models_preparing", "retryable": True}
        return None

    def _discover_installed(self):
        for path in Path(paths.APPS_DIR).glob("*/manifest.json"):
            try:
                manifest = contract.read_json(path)
                if workflow_ui.supported(manifest):
                    self.discover(manifest, path.parent.name)
            except (OSError, ValueError, TypeError):
                continue

    def _activate_registered(self):
        if not self.activate:
            return
        for directory in contract.root().glob("*/jobs"):
            try:
                tasks = self._jobs(directory.parent.name)
                pending = [t for t in tasks if t["state"] == "registered" and t.get("auto_activate") and not t.get("activation_done")]
                if not pending or any(t["state"] in ACTIVE for t in tasks):
                    continue
                # Callback owns the lifecycle mutation gate and rechecks user intent.
                if self.activate(directory.parent.name):
                    for task in pending:
                        self._save(task, activation_done=True)
            except Exception as exc:
                logging.getLogger(__name__).debug("Model activation deferred: %s", type(exc).__name__)
                continue

    def _run(self):
        # Never replay a POST whose response may have been lost across restart.
        for path in contract.root().glob("*/jobs/*/task.json"):
            try:
                task = contract.read_json(path)
            except (OSError, ValueError):
                continue
            if task["state"] == "uploading" and not task.get("cloud_id"):
                self._save(task, state="submission_uncertain", error="submission_uncertain")
        while not self.stop.is_set():
            if time.monotonic() - self.last_discovery >= 5:
                self._discover_installed()
                self.last_discovery = time.monotonic()
            for path in contract.root().glob("*/jobs/*/task.json"):
                if self.stop.is_set():
                    break
                with self.lock:
                    try:
                        task = contract.read_json(path)
                    except (ValueError, OSError):
                        continue
                    key = (task["app_id"], task["id"])
                    if task["state"] not in ACTIVE | {"awaiting_login"} or task.get("next_poll", 0) > time.time() or key in self.active:
                        continue
                    if task["state"] == "awaiting_login" and self.login_checks.get(key, 0) > time.monotonic():
                        continue
                    self.active.add(key)
                try:
                    if task["state"] == "awaiting_login":
                        self.login_checks[key] = time.monotonic() + 15
                        try:
                            session = cloud.credentials()
                            if task.get("user_id") and session["user_id"] != task["user_id"]:
                                continue
                        except (cloud.LoginRequired, cloud.CloudError, OSError):
                            continue
                    self._step(task)
                finally:
                    with self.lock:
                        self.active.discard(key)
            self._activate_registered()
            self.wake.wait(2)
            self.wake.clear()


manager = Manager()

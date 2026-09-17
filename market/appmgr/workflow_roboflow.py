"""Small streaming adapter for Roboflow's Inference model package provider."""
import base64
import hashlib
import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request

from . import workflow_model_contract as contract, workflow_onnx

API = "https://api.roboflow.com/models/v1/external/weights"


class DownloadError(Exception):
    def __init__(self, code, architecture=None):
        super().__init__(code)
        # Keep credentials, package URLs and remote response bodies out of tasks.
        self.architecture = str(architecture)[:64] if architecture is not None else None


def _allowed(url):
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443)
            or not (host == "roboflow.com" or host.endswith(".roboflow.com") or host == "storage.googleapis.com")):
        raise DownloadError("roboflow_download_invalid")
    return url


class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _allowed(newurl)
        if req.has_header("Authorization"):
            raise DownloadError("roboflow_download_invalid")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url, api_key=None):
    context = ssl.create_default_context()
    if os.path.isfile("/etc/ssl/certs/ca-certificates.crt"):
        context.load_verify_locations("/etc/ssl/certs/ca-certificates.crt")
    opener = urllib.request.build_opener(_Redirect(), urllib.request.HTTPSHandler(context=context))
    headers = {"User-Agent": "RV1126B-Workflow/0.3", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    try:
        return opener.open(urllib.request.Request(_allowed(url), headers=headers), timeout=30)
    except urllib.error.HTTPError as exc:
        code = "roboflow_authorization_required" if exc.code in (401, 403) else (
            "roboflow_model_not_found" if exc.code == 404 else "roboflow_unreachable")
        raise DownloadError(code) from None
    except (OSError, urllib.error.URLError):
        raise DownloadError("roboflow_unreachable") from None


def resolve(model_id, api_key=None):
    contract.model_id(model_id)
    query = {"modelId": model_id}
    for _ in range(10):
        with _open(API + "?" + urllib.parse.urlencode(query), api_key) as response:
            data = response.read(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024:
            raise DownloadError("roboflow_download_invalid")
        try:
            model = json.loads(data)["modelMetadata"]
            architecture = workflow_onnx.normalize_architecture(model.get("modelArchitecture"))
            if model.get("taskType") != "object-detection":
                raise DownloadError("unsupported_model_task", architecture)
            if architecture not in workflow_onnx.SUPPORTED_ARCHITECTURES:
                raise DownloadError("unsupported_model_architecture", architecture)
            for package in model["modelPackages"]:
                manifest = package.get("packageManifest", {})
                if (manifest.get("backendType") == "onnx" and manifest.get("staticBatchSize") == 1
                        and manifest.get("quantization") == "fp32"):
                    files = {item["fileHandle"]: item for item in package["packageFiles"]}
                    if {"weights.onnx", "class_names.txt", "inference_config.json"}.issubset(files):
                        return model, package, files
            cursor = model.get("nextPage")
            if not isinstance(cursor, str) or not cursor:
                break
            query["startAfter"] = cursor
        except (KeyError, TypeError, ValueError):
            raise DownloadError("roboflow_download_invalid") from None
    raise DownloadError("roboflow_onnx_unavailable")


def _download(item, target, limit):
    digest = hashlib.md5()
    sha = hashlib.sha256()
    temporary = target.with_suffix(".part")
    size = 0
    try:
        with _open(item["downloadUrl"]) as response, temporary.open("wb") as output:
            length = response.headers.get("Content-Length")
            if length and int(length) > limit:
                raise DownloadError("model_too_large")
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise DownloadError("model_too_large")
                output.write(chunk)
                digest.update(chunk)
                sha.update(chunk)
            output.flush()
            os.fsync(output.fileno())
        if not size or (length and size != int(length)) or item.get("md5Hash") not in (
                digest.hexdigest(), base64.b64encode(digest.digest()).decode()):
            raise DownloadError("roboflow_checksum_failed")
        os.replace(temporary, target)
    except (OSError, urllib.error.URLError):
        raise DownloadError("roboflow_unreachable") from None
    finally:
        temporary.unlink(missing_ok=True)
    return {"size": size, "sha256": sha.hexdigest()}


def prepare(model_id, directory, api_key=None):
    model, package, files = resolve(model_id, api_key)
    source = _download(files["weights.onnx"], directory / "source", contract.MAX_MODEL)
    _download(files["class_names.txt"], directory / "class_names.txt", 128 * 1024)
    _download(files["inference_config.json"], directory / "inference_config.json", 128 * 1024)
    try:
        labels = (directory / "class_names.txt").read_text().splitlines()
        configuration = contract.read_json(directory / "inference_config.json")
        metadata = workflow_onnx.metadata(directory / "source", model_id,
                                          architecture=model["modelArchitecture"], labels=labels,
                                          configuration=configuration)
    except (UnicodeError, ValueError) as exc:
        raise DownloadError(str(exc) if isinstance(exc, workflow_onnx.ModelFormatError) else "roboflow_download_invalid",
                            model.get("modelArchitecture")) from None
    return {"metadata": metadata, "source": source, "resolved_model_id": model["modelId"],
            "package_id": package["packageId"], "architecture": model["modelArchitecture"]}

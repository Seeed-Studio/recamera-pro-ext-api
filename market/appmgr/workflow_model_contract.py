"""System-owned, immutable Workflow model bindings (never manifest overrides)."""
import hashlib
import json
import math
import os
import re
import stat
from pathlib import Path

from . import paths

MAX_MODEL = 256 * 1024 * 1024
MAX_MODELS = 4


def root():
    return Path(paths.APPMGR_DIR) / "workflow-models"


def private_directory(path):
    path = Path(path)
    base = root()
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    current = base
    for part in ("", *path.relative_to(base).parts):
        if part:
            current = current / part
            current.mkdir(exist_ok=True, mode=0o700)
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError("Untrusted model asset directory")


def read_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 256 * 1024:
            raise ValueError("Invalid model registry document")
        return json.loads(stream.read(256 * 1024 + 1))


def atomic_json(path, value):
    path = Path(path)
    if path.is_relative_to(root()):
        private_directory(path.parent)
    else:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, ensure_ascii=True, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def model_id(value):
    if (not isinstance(value, str) or len(value) > 160
            or not re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_.-]+)*", value)
            or any(p in {".", ".."} for p in value.split("/"))):
        raise ValueError("Invalid Workflow model ID")
    return value


def metadata(value):
    """Validate the supported edge decoder contract without loading ML libraries.

    RKNN target/runtime and actual output shapes still require the supervised
    application's first inference. A registered package is pending until then.
    """
    value = json.loads(json.dumps(value, allow_nan=False))
    if not isinstance(value, dict):
        raise ValueError("Model configuration must be a JSON object")
    model_id(value.get("model_id"))
    if any(value.get(k) != v for k, v in {
        "schema_version": 1, "platform": "rv1126b", "format": "rknn", "task": "object-detection"
    }.items()) or type(value.get("schema_version")) is not int:
        raise ValueError("Require schema 1 RV1126B RKNN object-detection configuration")
    labels = value.get("labels")
    if not isinstance(labels, list) or not 1 <= len(labels) <= 1000 or any(
        not isinstance(x, str) or not x or len(x) > 256 for x in labels
    ):
        raise ValueError("Provide the model's class names in their original order")
    inp, outputs, post = value.get("input"), value.get("outputs"), value.get("postprocess")
    if not isinstance(inp, dict) or not isinstance(outputs, list) or not 1 <= len(outputs) <= 12:
        raise ValueError("Provide input and output tensor configuration")
    for tensor in [inp] + outputs:
        if not isinstance(tensor, dict) or not isinstance(tensor.get("name"), str) or not tensor["name"]:
            raise ValueError("Invalid tensor name")
        shape = tensor.get("shape")
        if not isinstance(shape, list) or not shape or any(type(x) is not int or x <= 0 for x in shape):
            raise ValueError("Tensor shapes must be fixed positive integers")
    shape = inp["shape"]
    padding = inp.get("padding_value", 114)
    if type(padding) is not int or not 0 <= padding <= 255:
        raise ValueError("Invalid letterbox padding value")
    if (len(shape) != 4 or shape[0] != 1 or shape[1] != shape[2] or shape[3] != 3
            or shape[1] > 1280 or any(inp.get(k) != v for k, v in {
                "dtype": "uint8", "layout": "NHWC", "color_format": "RGB", "normalization": "baked"
            }.items())):
        raise ValueError("Require square batch-1 NHWC RGB uint8 input, normalization baked into RKNN")
    if len({o["name"] for o in outputs}) != len(outputs) or any(o.get("dtype") != "float32" for o in outputs):
        raise ValueError("Require unique float32 output tensors")
    if sum(math.prod(o["shape"]) * 4 for o in outputs) > 64 * 1024 * 1024:
        raise ValueError("Output tensors exceed 64 MiB")
    if not isinstance(post, dict) or post.get("scores") not in {"logits", "probabilities"}:
        raise ValueError("Specify whether output scores are logits or probabilities")
    nms = post.get("nms", post.get("kind") != "yolo-end2end")
    if type(nms) is not bool or (post.get("kind") == "yolo-end2end" and nms):
        raise ValueError("NMS must be boolean and disabled for end-to-end outputs")
    topk = post.get("topk", 300)
    if type(topk) is not int or not 1 <= topk <= 1000:
        raise ValueError("End-to-end topk must be an integer in 1..1000")
    if "topk" in post and (nms or post.get("kind") not in {"yolo-distance", "yolo-dfl"}):
        raise ValueError("topk only applies to raw end-to-end detection heads")
    if post.get("kind") == "yolo-decoded":
        o = outputs[0]
        axis = 1 if o.get("layout") == "BCN" else 2
        objectness = post.get("objectness", False)
        channels = len(labels) + 4 + int(objectness is True)
        if (len(outputs) != 1 or post.get("box_format") != "xywh" or o.get("layout") not in {"BCN", "BNC"}
                or type(objectness) is not bool
                or len(o["shape"]) != 3 or o["shape"][0] != 1
                or o["shape"][axis] != channels or o["shape"][3-axis] <= channels
                or o.get("role") not in {None, "detections"}):
            raise ValueError("Decoded YOLO requires one BCN/BNC pixel-xywh output matching class count")
    elif post.get("kind") == "yolo-end2end":
        o = outputs[0]
        if (len(outputs) != 1 or post.get("box_format") != "xyxy"
                or post.get("scores") != "probabilities" or o.get("layout") != "BNC"
                or len(o["shape"]) != 3 or o["shape"][0] != 1 or o["shape"][2] != 6
                or o.get("role") not in {None, "detections"}):
            raise ValueError("End-to-end YOLO requires BNC [1,N,6] pixel xyxy, score and class ID")
    elif post.get("kind") == "yolo-distance":
        sizes = {}
        for o in outputs:
            s, role = o["shape"], o.get("role")
            channels = {"boxes": 4, "scores": len(labels)}.get(role)
            if (channels is None or o.get("layout") != "NCHW" or len(s) != 4
                    or s[0] != 1 or s[1] != channels or s[2] != s[3] or shape[1] % s[2]):
                raise ValueError("Distance YOLO requires explicit NCHW box/class branches")
            sizes.setdefault(s[2], []).append(role)
        if any(sorted(roles) != ["boxes", "scores"] for roles in sizes.values()):
            raise ValueError("Each distance scale requires one box and one class branch")
    elif post.get("kind") == "yolo-dfl":
        if post.get("reg_max") != 16 or len(labels) == 64:
            raise ValueError("DFL requires reg_max 16 and a class count other than 64")
        sizes = {}
        for o in outputs:
            s = o["shape"]
            role = o.get("role") or ("boxes" if len(s) > 1 and s[1] == 64 else "scores")
            channels = {"boxes": 64, "scores": len(labels), "score_sum": 1}.get(role)
            if (o.get("layout") != "NCHW" or len(s) != 4 or s[0] != 1 or s[1] != channels
                    or s[2] != s[3] or shape[1] % s[2]):
                raise ValueError("Invalid DFL box/class tensor branches")
            sizes.setdefault(s[2], []).append(role)
        if any(sorted(v) not in (["boxes", "scores"], ["boxes", "score_sum", "scores"]) for v in sizes.values()):
            raise ValueError("Each DFL scale requires one box and one class branch")
    else:
        raise ValueError("Unsupported object-detection output decoder")
    value["model_file"] = "model.rknn"
    value.pop("sha256", None)  # Always compute from the actual downloaded/uploaded artifact.
    value["memory_mb"] = 64
    return value


def bindings(app_id, verify=False):
    if not paths.valid_app_id(app_id):
        raise ValueError("Invalid application ID")
    registry = root() / app_id / "bindings.json"
    try:
        items = read_json(registry)
    except FileNotFoundError:
        return []
    if not isinstance(items, list) or len(items) > MAX_MODELS:
        raise ValueError("Invalid model bindings")
    result = []
    for item in items:
        identifier = model_id(item.get("model_id"))
        digest = item.get("sha256", "")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Invalid model binding digest")
        directory = root() / app_id / "assets" / digest / identifier
        # No client-provided absolute paths, symlink traversal, or mutable appdata.
        current = directory
        while current != root().parent:
            info = current.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise ValueError("Untrusted model asset directory")
            current = current.parent
        doc = read_json(directory / "model.json")
        checked = metadata(doc)
        if doc.get("sha256") != digest or checked["model_id"] != identifier or doc.get("model_file") != "model.rknn":
            raise ValueError("Model binding does not match its package")
        asset = directory / "model.rknn"
        fd = os.open(asset, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or not 8 < info.st_size <= MAX_MODEL:
                raise ValueError("Invalid RKNN asset")
            if verify:
                sha = hashlib.sha256()
                for chunk in iter(lambda: stream.read(65536), b""):
                    sha.update(chunk)
                if sha.hexdigest() != digest:
                    raise ValueError("Registered RKNN digest mismatch")
        result.append({**item, "directory": str(directory), "path": str(asset), "size": info.st_size, "metadata": doc})
    return result

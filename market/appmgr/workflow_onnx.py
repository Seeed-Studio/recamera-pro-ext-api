"""Read bounded ONNX descriptors without importing an ML runtime or its weights.

Only known Ultralytics detection exports are inferred for single-file uploads.
Roboflow packages supply their own verified architecture and preprocessing.
"""
import ast
import json
import mmap
import re
import struct

from . import workflow_model_contract as contract

# These names select an export contract, not permission to run arbitrary tensors.
# Input, output, preprocessing and converted RKNN descriptors are checked below.
SUPPORTED_ARCHITECTURES = ("yolov5", "yolov5u", "yolov7", "yolov8", "yolov9", "yolov10", "yolov11", "yolov12", "yolo26")


def normalize_architecture(value):
    value = str(value or "").lower()
    return {"yolo5": "yolov5", "yolo5u": "yolov5u", "yolo7": "yolov7", "yolo8": "yolov8",
            "yolo9": "yolov9", "yolo10": "yolov10", "yolo11": "yolov11",
            "yolo12": "yolov12", "yolov26": "yolo26"}.get(value, value)


class ModelFormatError(ValueError):
    pass


def _varint(data, offset, end):
    value = 0
    for shift in range(0, 70, 7):
        if offset >= end:
            break
        byte = data[offset]
        offset += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, offset
    raise ModelFormatError("unsupported_onnx")


def _fields(data, start=0, end=None):
    end = len(data) if end is None else end
    offset = start
    while offset < end:
        tag, offset = _varint(data, offset, end)
        number, wire = tag >> 3, tag & 7
        if not number:
            raise ModelFormatError("unsupported_onnx")
        if wire == 2:
            size, offset = _varint(data, offset, end)
            stop = offset + size
        elif wire in (1, 5):
            stop = offset + (8 if wire == 1 else 4)
        elif wire == 0:
            _, stop = _varint(data, offset, end)
        else:
            raise ModelFormatError("unsupported_onnx")
        if stop > end:
            raise ModelFormatError("unsupported_onnx")
        yield number, wire, offset, stop
        offset = stop


def _text(data, start, end):
    if end - start > 65536:
        raise ModelFormatError("unsupported_onnx")
    return data[start:end].decode("utf-8")


def _tensor(data, start, end):
    name, dtype, shape = "", None, []
    for number, wire, a, b in _fields(data, start, end):
        if number == 1 and wire == 2:
            name = _text(data, a, b)
        if number != 2 or wire != 2:
            continue
        for field, kind, c, d in _fields(data, a, b):
            if field != 1 or kind != 2:
                continue
            for sub, subwire, e, f in _fields(data, c, d):
                if sub == 1 and subwire == 0:
                    dtype, _ = _varint(data, e, f)
                elif sub == 2 and subwire == 2:
                    for dim, dimwire, g, h in _fields(data, e, f):
                        if dim != 1 or dimwire != 2:
                            continue
                        size = None
                        for key, keywire, i, j in _fields(data, g, h):
                            if key == 1 and keywire == 0:
                                size, _ = _varint(data, i, j)
                        if not size or len(shape) >= 8:
                            raise ModelFormatError("dynamic_onnx_unsupported")
                        shape.append(size)
    if not name or dtype != 1 or not shape:
        raise ModelFormatError("unsupported_onnx")
    return {"name": name, "shape": shape, "dtype": "float32"}


def describe(path):
    inputs, outputs, properties = [], [], {}
    if not 0 < path.stat().st_size <= contract.MAX_MODEL:
        raise ModelFormatError("model_too_large")
    try:
        with path.open("rb") as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
            for number, wire, start, end in _fields(data):
                if number == 7 and wire == 2:
                    for field, kind, a, b in _fields(data, start, end):
                        if field in (11, 12) and kind == 2:
                            target = inputs if field == 11 else outputs
                            if len(target) >= 12:
                                raise ModelFormatError("unsupported_onnx")
                            target.append(_tensor(data, a, b))
                elif number == 14 and wire == 2:
                    item = {field: _text(data, a, b) for field, kind, a, b in _fields(data, start, end)
                            if field in (1, 2) and kind == 2}
                    if 1 in item and 2 in item:
                        if len(properties) >= 64:
                            raise ModelFormatError("unsupported_onnx")
                        properties[item[1]] = item[2]
    except (UnicodeError, IndexError, OverflowError) as exc:
        raise ModelFormatError("unsupported_onnx") from exc
    return inputs, outputs, properties


def metadata(path, model_id, *, architecture=None, labels=None, configuration=None):
    inputs, outputs, properties = describe(path)
    if architecture is None:
        description = properties.get("description", "").lower()
        match = re.search(r"\byolo(v?(?:5|7|8|9|10|11|12|26))(?:[nslmx])?(u)?\b", description)
        if (properties.get("author") != "Ultralytics" or properties.get("task") != "detect"
                or not match):
            raise ModelFormatError("unsupported_onnx")
        architecture = normalize_architecture("yolo" + match[1] + (match[2] or ""))
        try:
            names = ast.literal_eval(properties.get("names", ""))
            labels = [names[i] for i in range(len(names))] if isinstance(names, dict) else names
        except (ValueError, SyntaxError, KeyError, TypeError, RecursionError) as exc:
            raise ModelFormatError("model_labels_missing") from exc
    architecture = normalize_architecture(architecture)
    if architecture not in SUPPORTED_ARCHITECTURES:
        raise ModelFormatError("unsupported_model_architecture")
    if len(inputs) != 1 or len(outputs) != 1 or not isinstance(labels, list) or not labels:
        raise ModelFormatError("unsupported_onnx")
    shape = inputs[0]["shape"]
    if len(shape) != 4 or shape[:2] != [1, 3] or shape[2] != shape[3] or shape[2] % 32:
        raise ModelFormatError("unsupported_onnx")
    size = shape[2]
    output = outputs[0]
    out_shape = output["shape"]
    objectness = architecture in {"yolov5", "yolov7"}
    channels = len(labels) + (5 if objectness else 4)
    # YOLO10/26 use xyxy, confidence, class ID in their end-to-end export.
    # Some Roboflow packages still declare post_processing.fused=false, so
    # require the known architecture as well as the actual BNC output layout.
    post_config = (configuration or {}).get("post_processing", {})
    args = {}
    try:
        args = ast.literal_eval(properties.get("args", "{}"))
    except (ValueError, SyntaxError, RecursionError):
        pass
    fused = post_config.get("fused") is True or (isinstance(args, dict) and args.get("nms") is True)
    if (len(out_shape) == 3 and out_shape[0] == 1 and out_shape[2] == 6
            and (architecture in {"yolov10", "yolo26"} or fused)):
        layout = "BNC"
        post = {"kind": "yolo-end2end", "scores": "probabilities", "box_format": "xyxy"}
    else:
        candidates = [sum((size // stride) ** 2 for stride in strides) * (3 if objectness else 1)
                      for strides in ((8, 16, 32), (8, 16, 32, 64))]
        if len(out_shape) != 3 or out_shape[0] != 1 or fused:
            raise ModelFormatError("unsupported_model_output")
        if out_shape[1] == channels and out_shape[2] in candidates:
            layout = "BCN"
        elif out_shape[2] == channels and out_shape[1] in candidates:
            layout = "BNC"
        else:
            raise ModelFormatError("unsupported_model_output")
        post = {"kind": "yolo-decoded", "scores": "probabilities", "box_format": "xywh"}
        if objectness:
            post["objectness"] = True
    padding = 114
    if configuration is not None:
        network = configuration.get("network_input", {})
        preprocessing = configuration.get("image_pre_processing", {})
        if (any(key != "auto-orient" and value.get("enabled", True)
                for key, value in preprocessing.items() if isinstance(value, dict))
                or network.get("training_input_size") != {"height": size, "width": size}
                or network.get("color_mode") != "rgb" or network.get("resize_mode") != "letterbox"
                or network.get("input_channels") != 3 or network.get("scaling_factor") != 255
                or network.get("normalization") is not None
                or post_config.get("type") != "nms"
                or type(post_config.get("fused")) is not bool):
            raise ModelFormatError("unsupported_model_preprocessing")
        padding = network.get("padding_value", 0)
    return contract.metadata({
        "schema_version": 1, "model_id": model_id, "platform": "rv1126b", "format": "rknn",
        "task": "object-detection", "labels": labels,
        "source_architecture": architecture,
        "input": {"name": inputs[0]["name"], "shape": [1, size, size, 3], "dtype": "uint8",
                  "layout": "NHWC", "color_format": "RGB", "normalization": "baked", "padding_value": padding},
        "outputs": [{**output, "layout": layout, "role": "detections"}],
        "postprocess": post,
    })


def converted_metadata(path, prepared):
    """SenseCraft may cut YOLO's decoder off during conversion.

    RKNN v6 carries length-prefixed compiler attributes in its header. Read only
    this bounded descriptive record, then require a known YOLO output contract.
    A real broker-authorized inference still verifies the generated contract.
    """
    with path.open("rb") as stream:
        header = stream.read(256 * 1024)
    if header[:8] != b"RKNN\x00\x00\x00\x00" or len(header) < 24 or struct.unpack_from("<Q", header, 8)[0] != 6:
        raise ModelFormatError("unsupported_rknn_metadata")
    start = header.find(b"{'attrs': {")
    if start < 4:
        raise ModelFormatError("unsupported_rknn_metadata")
    length = struct.unpack_from("<I", header, start-4)[0]
    if not 0 < length <= 128 * 1024 or start + length >= len(header) or header[start+length] != 0:
        raise ModelFormatError("unsupported_rknn_metadata")
    try:
        attrs = ast.literal_eval(header[start:start+length].decode())["attrs"]
        inputs = [(name, value) for name, value in attrs.items() if value.get("is_output") is False]
        outputs = sorted(((name, value) for name, value in attrs.items() if value.get("is_output") is True), key=lambda item: item[1]["idx"])
        size = prepared["input"]["shape"][1]
        if len(inputs) != 1:
            raise ValueError()
        name, inp = inputs[0]
        if (name != prepared["input"]["name"] or inp.get("shape") != [1, 3, size, size]
                or inp.get("mean") != [0, 0, 0] or inp.get("std") != [255, 255, 255]
                or inp.get("rgb2bgr") is not False):
            raise ValueError()
        if (len(outputs) == 1 and outputs[0][1].get("shape") == prepared["outputs"][0]["shape"]
                and outputs[0][1].get("dtype") == "float32"):
            return contract.metadata({**prepared, "outputs": [{**prepared["outputs"][0], "name": outputs[0][0]}]})
        if len(outputs) != 6 or [out["idx"] for _, out in outputs] != list(range(6)):
            raise ValueError()
        tensors, heads = [], set()
        # A converter may expose the detection head before its decoder. YOLO26
        # regresses distances directly; other supported raw heads use DFL.
        distance = prepared.get("source_architecture") == "yolo26"
        if prepared.get("postprocess", {}).get("objectness"):
            raise ValueError()  # Anchor-based raw heads need their actual anchors.
        for name, value in outputs:
            match = re.fullmatch(r"/model\.\d+/(?:(one2one|one2many)_)?cv([23])\.([012])/.*/Conv_output_0", name)
            if not match or value.get("dtype") != "float32" or value.get("layout") != "nchw":
                raise ValueError()
            heads.add(match[1])
            role = "boxes" if match[2] == "2" else "scores"
            grid = size // (8 * 2 ** int(match[3]))
            if value.get("shape") != [1, (4 if distance else 64) if role == "boxes" else len(prepared["labels"]), grid, grid]:
                raise ValueError()
            tensors.append({"name": name, "shape": value["shape"], "dtype": "float32", "layout": "NCHW", "role": role})
        if len(heads) != 1:
            raise ValueError()
        post = {"kind": "yolo-distance", "scores": "logits"} if distance else {
            "kind": "yolo-dfl", "scores": "logits", "reg_max": 16}
        end2end = prepared["postprocess"]["kind"] == "yolo-end2end"
        if end2end and prepared.get("source_architecture") not in {"yolov10", "yolo26"}:
            # A cut fused-NMS graph is not a native one2one TopK graph. Its
            # embedded IoU threshold/selection limits must not be guessed.
            raise ValueError()
        if "one2many" in heads and end2end:
            raise ValueError()  # Conversion changed the source detection branch.
        if "one2one" in heads or (heads == {None} and end2end):
            if not end2end:
                raise ValueError()  # No source selection limit to reproduce.
            post.update(nms=False, topk=prepared["outputs"][0]["shape"][1])
        elif distance:
            post["nms"] = True
        return contract.metadata({**prepared, "outputs": tensors, "postprocess": post})
    except (ValueError, TypeError, KeyError, AttributeError, SyntaxError, RecursionError, UnicodeError) as exc:
        raise ModelFormatError("unsupported_rknn_metadata") from exc

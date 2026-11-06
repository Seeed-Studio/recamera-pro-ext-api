"""Strict detector output contract around the public Kit YOLO postprocessor."""
import math

import numpy as np
from kit.runtime.postprocess.detect import postprocess


def validate_contract(contract, model):
    if (contract.get("schema_version") != 1 or contract.get("task") != "detect"
            or contract.get("target_platform") != "rv1126b"):
        raise ValueError("unsupported model task or target contract")
    size, classes = contract.get("input_size"), contract.get("classes")
    if type(size) is not int or not 32 <= size <= 2048 or size % 32:
        raise ValueError("model input_size must be a square multiple of 32")
    if (not isinstance(classes, list) or not classes
            or any(not isinstance(name, str) or not name.strip() for name in classes)
            or len(set(classes)) != len(classes)):
        raise ValueError("model classes must be a nonempty ordered unique label list")
    if model.get("input") != [1, size, size, 3] or model.get("classes") != classes:
        raise ValueError("model contract geometry/classes differ from manifest")
    layout = contract.get("output_layout")
    if layout not in ("yolo_decoded_xywh", "yolo_dfl"):
        raise ValueError("unsupported detector output layout")
    if contract.get("architecture") not in ("yolov8", "yolo11", "yolo26"):
        raise ValueError("unsupported detector architecture")
    if contract.get("architecture") == "yolo26" and layout != "yolo_decoded_xywh":
        raise ValueError("YOLO26 requires an explicit non-end-to-end decoded export")
    if contract.get("nms") is not False or contract.get("end2end") is not False:
        raise ValueError("device decoder requires nms=false and end2end=false export")
    return contract


def decode(outputs, info, contract, conf, iou):
    arrays = [np.asarray(value) for value in outputs]
    classes = contract["classes"]
    # The scheduled ctypes backend requests dequantized FLOAT32. Refuse an
    # unexpected integer output from another backend instead of interpreting
    # quantized bytes as pixel coordinates and class probabilities.
    if any(not np.issubdtype(array.dtype, np.floating) for array in arrays):
        raise ValueError("model outputs must be dequantized floating-point tensors")
    if any(not np.isfinite(array).all() for array in arrays):
        raise ValueError("model output contains non-finite values")
    if contract["output_layout"] == "yolo_decoded_xywh":
        if len(arrays) != 1 or arrays[0].ndim not in (2, 3):
            raise ValueError("expected one decoded YOLO output tensor")
        arr = arrays[0]
        if arr.ndim == 3:
            if arr.shape[0] != 1:
                raise ValueError("only batch=1 inference is supported")
            arr = arr[0]
        channels = 4 + len(classes)
        if arr.shape[0] == channels:
            arr = arr.T
        elif arr.shape[1] != channels:
            raise ValueError("decoded YOLO output class dimension does not match labels")
        if not len(arr):
            return []
        # Kit chooses orientation by the shorter dimension. Pad tiny controlled
        # outputs with zero-score anchors so 1-class/single-anchor outputs keep
        # their unambiguous [anchors, 4+classes] shape as well.
        if len(arr) <= channels:
            arr = np.pad(arr, ((0, channels + 1 - len(arr)), (0, 0)))
        arrays = [arr[None, ...]]
    elif not arrays or any(array.ndim != 4 or array.shape[0] != 1 for array in arrays):
        raise ValueError("expected batch=1 raw DFL branches")
    if not math.isfinite(float(conf)) or not 0 < conf <= 1:
        raise ValueError("invalid confidence threshold")
    return postprocess(arrays, info, conf_thres=float(conf), iou_thres=float(iou),
                       input_size=contract["input_size"], class_names=classes)

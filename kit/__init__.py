"""High-level Python API for reCamera AI applications.

The package keeps imports lazy so an audio-only application does not
accidentally import NumPy/OpenCV.  Existing ``kit.adapters`` imports remain
valid; these names form the new, documented common contract.
"""
from __future__ import annotations

import importlib


_EXPORTS = {
    "AdapterError": "errors",
    "BufferReleasedError": "errors",
    "CapabilityError": "errors",
    "ConfigurationError": "errors",
    "DeviceControlError": "errors",
    "InferenceError": "errors",
    "ImageOperationError": "errors",
    "InputValidationError": "errors",
    "KitError": "errors",
    "ModelLoadError": "errors",
    "ResourceBusyError": "errors",
    "ResourceTimeoutError": "errors",
    "TransportError": "errors",
    "Capabilities": "capabilities",
    "Capability": "capabilities",
    "CapabilityStatus": "capabilities",
    "get_capabilities": "capabilities",
    "probe_capabilities": "capabilities",
    "ImageBuffer": "buffer",
    "MemoryKind": "buffer",
    "Ownership": "buffer",
    "PixelFormat": "buffer",
    "PlaneLayout": "buffer",
    "Frame": "frame",
    "DeviceCloseReport": "device",
    "Device": "device",
    "ImageOps": "media",
    "Rect": "media",
    "RgaContext": "media",
    "Size": "media",
    "TransformMapping": "media",
    "TransformResult": "media",
    "ExternalNpuLease": "resources",
    "ResourceKind": "resources",
    "InferenceStats": "runtime.engine",
    "ModelSpec": "runtime.engine",
    "RknnModel": "runtime.engine",
    "RknnSession": "runtime.engine",
    "TensorSpec": "runtime.engine",
    "RemoteRknnModel": "runtime.remote",
    "RemoteRknnSession": "runtime.remote",
    "CancellationToken": "workflow",
    "DropPolicy": "workflow",
    "InputQueue": "workflow",
    "Pipeline": "workflow",
    "PutResult": "workflow",
    "PutStatus": "workflow",
    "QueueStats": "workflow",
    "Stage": "workflow",
    "WorkflowContext": "workflow",
    "WorkflowCloseReport": "workflow",
    "WorkflowCleanupError": "workflow",
    "AIResult": "ai",
    "Box": "ai",
    "Classification": "ai",
    "CoordinateSpace": "ai",
    "Detection": "ai",
    "Keypoint": "ai",
    "Pose": "ai",
    "PublishReport": "ai",
    "ResultBatch": "ai",
    "ResultBatchPublisher": "ai",
    "Segmentation": "ai",
    "Track": "ai",
    "publish_result_batch": "ai",
    "to_legacy_payload": "ai",
    "GeometryBuilder": "geometry",
    "GeometryError": "geometry",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(f".{module_name}", __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))

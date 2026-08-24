"""Inference runtime interfaces."""

from .engine import (
    InferenceStats,
    ModelSpec,
    RknnModel,
    RknnSession,
    TensorSpec,
)
from .remote import (
    DEFAULT_INFERENCE_SOCKET,
    INFERENCE_SOCKET_ENV,
    RemoteRknnModel,
    RemoteRknnSession,
    configured_inference_socket,
)

__all__ = [
    "InferenceStats",
    "DEFAULT_INFERENCE_SOCKET",
    "INFERENCE_SOCKET_ENV",
    "ModelSpec",
    "RemoteRknnModel",
    "RemoteRknnSession",
    "configured_inference_socket",
    "RknnModel",
    "RknnSession",
    "TensorSpec",
]

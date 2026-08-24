"""Typed, validated AI result objects for reCamera application workflows.

The public names are defined in :mod:`kit.ai.results` and re-exported here so
applications can use the compact ``from kit.ai import Detection`` form without
expanding the historical top-level :mod:`kit` namespace.
"""

from .results import (
    AIResult,
    Box,
    Classification,
    CoordinateSpace,
    Detection,
    Keypoint,
    Pose,
    ResultBatch,
    Segmentation,
    Track,
)
from .publisher import (
    LegacyResultSink,
    ModelToPixel,
    PublishReport,
    ResultBatchPublisher,
    publish_result_batch,
    to_legacy_payload,
)

__all__ = [
    "AIResult",
    "Box",
    "Classification",
    "CoordinateSpace",
    "Detection",
    "Keypoint",
    "LegacyResultSink",
    "ModelToPixel",
    "Pose",
    "PublishReport",
    "ResultBatch",
    "ResultBatchPublisher",
    "Segmentation",
    "Track",
    "publish_result_batch",
    "to_legacy_payload",
]

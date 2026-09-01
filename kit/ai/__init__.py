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
from ..geometry import (
    GeometryBuilder,
    GeometryError,
    box as geometry_box,
    keypoints as geometry_keypoints,
    line as geometry_line,
    point as geometry_point,
    polygon as geometry_polygon,
    polyline as geometry_polyline,
    pose as geometry_pose,
    quad as geometry_quad,
)

__all__ = [
    "AIResult",
    "Box",
    "Classification",
    "CoordinateSpace",
    "Detection",
    "Keypoint",
    "GeometryBuilder",
    "GeometryError",
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
    "geometry_box",
    "geometry_keypoints",
    "geometry_line",
    "geometry_point",
    "geometry_polygon",
    "geometry_polyline",
    "geometry_pose",
    "geometry_quad",
]

"""Publish typed AI results through the existing legacy result-sink contract.

The current kit sinks accept ``emit(payload: dict, pts: float)`` and treat all
coordinates in that payload as original-frame pixels.  In particular,
``OfficialResultSink`` calls ``set_frame_size`` and performs the final
pixel-to-normalized conversion required by the rkipc/OSD ABI.  This module is a
strict compatibility boundary between that contract and :mod:`kit.ai.results`:

* pixel coordinates pass through unchanged;
* normalized coordinates are multiplied by the batch frame width/height;
* model-input coordinates require an explicit caller-supplied mapping and are
  never guessed from frame/model dimensions or a presumed letterbox policy;
* tracking objects are placed in ``events`` because that is the route consumed
  by the existing OSD sink, while other result types remain in ``results``.

Batch and result metadata are copied into the legacy payload.  Structural
fields such as ``box`` and ``keypoints`` are rebuilt from validated typed data,
so an old normalized alias can never coexist with the pixel value the sink
will consume.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from numbers import Real
from typing import Any, Protocol, TypeAlias

from ..errors import (
    AdapterError,
    ConfigurationError,
    TransportError,
    wrap_error,
)
from ..diagnostics import get_logger
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


_LOG = get_logger("ai.publisher")
_PREPARE_OPERATION = "ai.result.prepare"
_PUBLISH_OPERATION = "ai.result.publish"
_FRAME_SIZE_OPERATION = "ai.result.set_frame_size"


ModelToPixel: TypeAlias = Callable[[float, float], tuple[float, float]]
"""Explicit model-input to original-frame pixel point mapping.

The callback receives one ``(x, y)`` point in :class:`CoordinateSpace.MODEL`
and must return the corresponding original-frame pixel point.  A closure may
capture the exact crop/resize/letterbox transform produced by preprocessing.
Scaling from model and frame dimensions alone is intentionally not provided:
it would silently mishandle padding, crops and non-square inputs.
"""


class LegacyResultSink(Protocol):
    """Structural type implemented by the existing result/OSD sinks."""

    def emit(self, payload: dict[str, Any], pts: float) -> None:
        """Publish one legacy result payload at a timestamp in seconds."""

    def set_frame_size(self, w: int, h: int) -> None:
        """Set the original-frame pixel extent before ``emit``."""

    def emit_checked(self, payload: dict[str, Any], pts: float) -> None:
        """Optional strict counterpart that surfaces local delivery failures."""

    def set_frame_size_checked(self, w: int, h: int) -> None:
        """Optional strict counterpart for frame geometry."""


@dataclass(frozen=True, slots=True)
class PublishReport:
    """Summary returned only after the underlying sink accepts ``emit`` locally.

    ``input_results`` is the number of typed objects supplied.  Tracks become
    legacy events, hence ``payload_results`` and ``payload_events`` describe the
    actual lists sent to the sink.  Coordinate counters count x/y *pairs* that
    were transformed; a normalized box contributes two, and one normalized
    keypoint contributes one.  Pixel pairs are not counted because they pass
    through unchanged.
    """

    source_id: str
    pts_us: int
    frame_width: int | None
    frame_height: int | None
    input_results: int
    payload_results: int
    payload_events: int
    track_events: int
    normalized_pairs: int
    model_pairs: int
    sink_type: str
    elapsed_ms: float
    locally_accepted: bool = True
    server_acknowledged: bool = False

    @property
    def converted_pairs(self) -> int:
        """Total normalized/model coordinate pairs converted to pixels."""

        return self.normalized_pairs + self.model_pairs

    def as_dict(self) -> dict[str, Any]:
        """Return JSON-compatible structured telemetry for health endpoints."""

        return {
            "source_id": self.source_id,
            "pts_us": self.pts_us,
            "frame_width": self.frame_width,
            "frame_height": self.frame_height,
            "input_results": self.input_results,
            "payload_results": self.payload_results,
            "payload_events": self.payload_events,
            "track_events": self.track_events,
            "normalized_pairs": self.normalized_pairs,
            "model_pairs": self.model_pairs,
            "converted_pairs": self.converted_pairs,
            "sink_type": self.sink_type,
            "elapsed_ms": self.elapsed_ms,
            "locally_accepted": self.locally_accepted,
            "server_acknowledged": self.server_acknowledged,
        }


@dataclass(slots=True)
class _ConversionCounts:
    normalized_pairs: int = 0
    model_pairs: int = 0


def _configuration_error(
    message: str,
    field_name: str,
    *,
    details: Mapping[str, Any] | None = None,
) -> ConfigurationError:
    context = {"field": field_name}
    context.update(details or {})
    return ConfigurationError(
        message,
        operation=_PREPARE_OPERATION,
        details=context,
    )


def _finite_coordinate(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise _configuration_error(
            "coordinate mapping must return real numbers", field_name
        )
    result = float(value)
    if not math.isfinite(result):
        raise _configuration_error(
            "coordinate mapping must return finite numbers", field_name
        )
    if result < 0.0:
        raise _configuration_error(
            "coordinate mapping must return non-negative pixels", field_name
        )
    return result


def _frame_size(batch: ResultBatch, *, required_for: str) -> tuple[int, int]:
    if batch.frame_size is None:
        raise _configuration_error(
            f"batch.frame_size is required for {required_for}",
            "frame_size",
            details={"coordinate_space": required_for},
        )
    return batch.frame_size


class _LegacyConverter:
    def __init__(
        self,
        batch: ResultBatch,
        model_to_pixel: ModelToPixel | None,
    ) -> None:
        self.batch = batch
        self.model_to_pixel = model_to_pixel
        self.counts = _ConversionCounts()

    def point(self, x: float, y: float, space: CoordinateSpace) -> tuple[float, float]:
        if space is CoordinateSpace.PIXEL:
            return float(x), float(y)

        if space is CoordinateSpace.NORMALIZED:
            width, height = _frame_size(self.batch, required_for="normalized")
            self.counts.normalized_pairs += 1
            return float(x) * width, float(y) * height

        if space is not CoordinateSpace.MODEL:
            raise _configuration_error(
                f"unsupported coordinate space: {space!r}", "coordinate_space"
            )
        if self.model_to_pixel is None:
            raise _configuration_error(
                "model coordinates require an explicit model_to_pixel mapping",
                "model_to_pixel",
                details={"coordinate_space": "model"},
            )
        try:
            mapped = self.model_to_pixel(float(x), float(y))
        except Exception as exc:
            error = _configuration_error(
                "model_to_pixel mapping failed",
                "model_to_pixel",
                details={"mapper_type": type(self.model_to_pixel).__name__},
            )
            raise error from exc
        if isinstance(mapped, (str, bytes, bytearray)) or not isinstance(
            mapped, Sequence
        ) or len(mapped) != 2:
            raise _configuration_error(
                "model_to_pixel must return exactly two pixel coordinates",
                "model_to_pixel",
            )
        px = _finite_coordinate(mapped[0], "model_to_pixel.x")
        py = _finite_coordinate(mapped[1], "model_to_pixel.y")
        if self.batch.frame_size is not None:
            width, height = self.batch.frame_size
            if px > width or py > height:
                raise _configuration_error(
                    "model_to_pixel output exceeds batch.frame_size",
                    "model_to_pixel",
                    details={"frame_width": width, "frame_height": height},
                )
        self.counts.model_pairs += 1
        return px, py

    def box(self, box: Box) -> list[float]:
        x1, y1 = self.point(box.x1, box.y1, box.space)
        x2, y2 = self.point(box.x2, box.y2, box.space)
        if x2 < x1 or y2 < y1:
            raise _configuration_error(
                "coordinate mapping inverted a box", "model_to_pixel"
            )
        return [x1, y1, x2, y2]

    @staticmethod
    def _base(result: AIResult, structural: set[str]) -> dict[str, Any]:
        legacy = result.to_legacy_dict()
        if not isinstance(legacy, dict):
            raise _configuration_error(
                "typed result produced a non-mapping legacy value", "result"
            )
        for key in structural:
            legacy.pop(key, None)
        return legacy

    def detection(self, result: Detection) -> dict[str, Any]:
        item = self._base(
            result,
            {
                "box", "bbox", "score", "cls", "class_id", "cls_name",
                "label", "coordinate_space", "space",
            },
        )
        item.update(
            {
                "box": self.box(result.box),
                "score": result.score,
                "cls": result.class_id,
                "cls_name": result.label,
                "coordinate_space": "pixel",
            }
        )
        return item

    def classification(self, result: Classification) -> dict[str, Any]:
        item = self._base(
            result,
            {
                "kind", "box", "bbox", "score", "cls", "class_id",
                "cls_name", "label", "coordinate_space", "space",
            },
        )
        item.update(
            {
                "kind": "classification",
                "score": result.score,
                "cls": result.class_id,
                "label": result.label,
            }
        )
        if result.box is not None:
            # OfficialResultSink's current legacy discriminator recognizes an
            # ROI classification only through the face-analysis attributes it
            # already serves.  Merely adding kind="classification" is not
            # enough: it would route a generic ROI label as a detection.  Do
            # not invent a fake gender/emotion field just to trigger routing.
            if not any(item.get(key) for key in ("gender", "age", "emotion")):
                raise _configuration_error(
                    "the legacy OSD sink cannot represent a generic ROI classification",
                    "classification.box",
                    details={"required_route": "face_attributes"},
                )
            item["box"] = self.box(result.box)
            item["coordinate_space"] = "pixel"
        return item

    def pose(self, result: Pose) -> dict[str, Any]:
        ordered = sorted(result.keypoints, key=lambda point: point.id)
        if [point.id for point in ordered] != list(range(len(ordered))):
            raise _configuration_error(
                "the legacy sink requires dense zero-based keypoint IDs",
                "keypoints",
                details={"keypoint_ids": [point.id for point in ordered]},
            )
        item = self._base(
            result,
            {
                "kind", "keypoints", "points", "keypoint_ids", "box", "bbox",
                "score", "cls", "class_id", "cls_name", "label",
                "coordinate_space", "space",
            },
        )
        points = []
        for point in ordered:
            x, y = self.point(point.x, point.y, point.space)
            points.append([x, y, point.score])
        item.update(
            {
                "keypoints": points,
                "score": result.score,
                "cls": result.class_id,
                "cls_name": result.label,
                "coordinate_space": "pixel",
            }
        )
        if result.box is not None:
            item["box"] = self.box(result.box)
        return item

    def track(self, result: Track) -> dict[str, Any]:
        item = self._base(
            result,
            {
                "kind", "track_id", "box", "bbox", "score", "cls",
                "class_id", "cls_name", "label", "coordinate_space", "space",
            },
        )
        item.update(
            {
                "kind": "track",
                "track_id": result.track_id,
                "box": self.box(result.box),
                "score": result.score,
                "cls": result.class_id,
                "label": result.label,
                "coordinate_space": "pixel",
            }
        )
        return item

    def segmentation(self, result: Segmentation) -> dict[str, Any]:
        # Although the native ABI can encode an empty segmentation, the current
        # high-level sink selects its task branch by truthiness of mask bytes.
        # Publishing b"" would therefore be silently misrouted as a different
        # task.  Refuse that unrepresentable legacy case explicitly.
        if not result.mask:
            raise _configuration_error(
                "the legacy OSD sink cannot route an empty segmentation mask",
                "segmentation.mask",
            )
        item = self._base(
            result,
            {
                "kind", "mask", "mask_bytes", "mask_w", "mask_h",
                "mask_width", "mask_height", "width", "height", "box", "bbox",
                "score", "cls", "class_id", "cls_name", "label",
                "coordinate_space", "space",
            },
        )
        item.update(
            {
                "kind": "segmentation",
                "mask_bytes": result.mask,
                "mask_w": result.width,
                "mask_h": result.height,
                "score": result.score,
                "cls": result.class_id,
                "cls_name": result.label,
            }
        )
        if result.box is not None:
            item["box"] = self.box(result.box)
            item["coordinate_space"] = "pixel"
        return item

    def result(self, result: AIResult) -> tuple[str, dict[str, Any]]:
        if isinstance(result, Detection):
            return "result", self.detection(result)
        if isinstance(result, Classification):
            return "result", self.classification(result)
        if isinstance(result, Pose):
            return "result", self.pose(result)
        if isinstance(result, Track):
            return "event", self.track(result)
        if isinstance(result, Segmentation):
            return "result", self.segmentation(result)
        raise _configuration_error(
            f"unsupported typed result: {type(result).__name__}", "result"
        )

    def payload(self) -> tuple[dict[str, Any], int]:
        # ResultBatch owns the lossless copy of any original payload.  Starting
        # from it retains app/business metadata while the lists below are always
        # rebuilt from validated typed objects.
        payload = self.batch.to_legacy_dict()
        if not isinstance(payload, dict):
            raise _configuration_error("batch legacy payload must be a mapping", "batch")

        existing_events = payload.get("events", [])
        if existing_events is None:
            existing_events = []
        if isinstance(existing_events, (str, bytes, bytearray)) or not isinstance(
            existing_events, Sequence
        ):
            raise _configuration_error("payload events must be a sequence", "events")
        events = list(existing_events)
        results: list[dict[str, Any]] = []
        track_events = 0
        for result in self.batch.results:
            destination, item = self.result(result)
            if destination == "event":
                events.append(item)
                track_events += 1
            else:
                results.append(item)

        payload["results"] = results
        payload["events"] = events
        payload["pts_us"] = self.batch.pts_us
        payload["source_id"] = self.batch.source_id
        if self.batch.source_id:
            payload.setdefault("app", self.batch.source_id)
        payload["coordinate_space"] = "pixel"

        if self.batch.frame_size is not None:
            width, height = self.batch.frame_size
            frame = payload.get("frame")
            if frame is None:
                frame = {}
            if not isinstance(frame, Mapping):
                raise _configuration_error("payload frame metadata must be a mapping", "frame")
            frame = dict(frame)
            frame.update({"width": width, "height": height})
            payload["frame"] = frame
        return payload, track_events


def _convert_batch(
    batch: ResultBatch,
    model_to_pixel: ModelToPixel | None,
) -> tuple[dict[str, Any], _ConversionCounts, int]:
    if not isinstance(batch, ResultBatch):
        raise _configuration_error("publish requires a ResultBatch", "batch")
    if model_to_pixel is not None and not callable(model_to_pixel):
        raise _configuration_error("model_to_pixel must be callable", "model_to_pixel")
    converter = _LegacyConverter(batch, model_to_pixel)
    payload, track_events = converter.payload()
    return payload, converter.counts, track_events


def to_legacy_payload(
    batch: ResultBatch,
    *,
    model_to_pixel: ModelToPixel | None = None,
) -> dict[str, Any]:
    """Convert a typed batch to the pixel-coordinate payload existing sinks use.

    This function is side-effect free and is useful for inspection or custom
    transport integration.  It applies exactly the same strict coordinate and
    metadata rules as :class:`ResultBatchPublisher` but does not call a sink.
    """

    payload, _counts, _tracks = _convert_batch(batch, model_to_pixel)
    return payload


class ResultBatchPublisher:
    """Compatibility adapter that publishes :class:`ResultBatch` to a sink.

    Parameters:
        sink: Existing duck-typed result sink.  It must provide ``emit`` and may
            provide ``set_frame_size``; all built-in kit sinks provide both.
        model_to_pixel: Exact preprocess inverse for model-space coordinates.
            Omitting it is valid until a model-space result is encountered, at
            which point publication raises :class:`ConfigurationError`.
        logger: Optional logger override, primarily for host integrations.  The
            default is ``recamera.ai.publisher`` and import has no logging side
            effects.

    Conversion/configuration failures happen before the sink is called.
    Exceptions from ``set_frame_size`` or ``emit`` are logged with structured
    context and re-raised as :class:`AdapterError` or :class:`TransportError`,
    retaining the original exception as ``__cause__``.  No publish failure is
    converted into a successful report.
    """

    def __init__(
        self,
        sink: LegacyResultSink,
        *,
        model_to_pixel: ModelToPixel | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        emit = getattr(sink, "emit", None)
        if not callable(emit):
            raise _configuration_error("sink must provide callable emit", "sink")
        if model_to_pixel is not None and not callable(model_to_pixel):
            raise _configuration_error("model_to_pixel must be callable", "model_to_pixel")
        self.sink = sink
        self.model_to_pixel = model_to_pixel
        self.logger = logger or _LOG

    @property
    def sink_type(self) -> str:
        """Concrete sink class name used in reports, logs and error context."""

        return type(self.sink).__name__

    def _context(self, batch: ResultBatch) -> dict[str, Any]:
        width = batch.frame_size[0] if batch.frame_size is not None else None
        height = batch.frame_size[1] if batch.frame_size is not None else None
        return {
            "sink_type": self.sink_type,
            "source_id": batch.source_id,
            "pts_us": batch.pts_us,
            "input_results": len(batch.results),
            "frame_width": width,
            "frame_height": height,
        }

    def _raise_sink_error(
        self,
        exc: Exception,
        batch: ResultBatch,
        *,
        operation: str,
    ) -> None:
        details = self._context(batch)
        if isinstance(exc, AdapterError):
            # A checked sink already supplied the stable kit error family,
            # retryability and native cause.  Rewrapping it would downgrade a
            # TransportError to a generic AdapterError and lose that signal.
            self.logger.error(
                "typed AI result publication failed",
                extra={
                    "event": "ai_result_publish_failed",
                    "operation": operation,
                    "error_type": type(exc).__name__,
                    **details,
                },
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            raise exc
        error_type: type[AdapterError]
        retryable = isinstance(exc, (ConnectionError, TimeoutError, OSError))
        if retryable:
            error_type = TransportError
        else:
            error_type = AdapterError
        self.logger.error(
            "typed AI result publication failed",
            extra={
                "event": "ai_result_publish_failed",
                "operation": operation,
                "error_type": type(exc).__name__,
                **details,
            },
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        error = wrap_error(
            exc,
            error_type,
            "result sink rejected a typed AI result batch",
            operation=operation,
            retryable=retryable,
            details=details,
        )
        raise error from exc

    def publish(self, batch: ResultBatch) -> PublishReport:
        """Convert and synchronously publish one batch, returning its report."""

        started = time.perf_counter()
        payload, counts, track_events = _convert_batch(batch, self.model_to_pixel)

        if batch.frame_size is not None:
            setter = getattr(self.sink, "set_frame_size_checked", None)
            if not callable(setter):
                setter = getattr(self.sink, "set_frame_size", None)
            if setter is not None and not callable(setter):
                raise _configuration_error(
                    "sink.set_frame_size must be callable", "sink.set_frame_size"
                )
            if callable(setter):
                try:
                    setter(*batch.frame_size)
                except Exception as exc:
                    self._raise_sink_error(
                        exc, batch, operation=_FRAME_SIZE_OPERATION
                    )

        try:
            emitter = getattr(self.sink, "emit_checked", None)
            if not callable(emitter):
                emitter = self.sink.emit
            emitter(payload, batch.pts_us / 1_000_000.0)
        except Exception as exc:
            self._raise_sink_error(exc, batch, operation=_PUBLISH_OPERATION)

        width = batch.frame_size[0] if batch.frame_size is not None else None
        height = batch.frame_size[1] if batch.frame_size is not None else None
        report = PublishReport(
            source_id=batch.source_id,
            pts_us=batch.pts_us,
            frame_width=width,
            frame_height=height,
            input_results=len(batch.results),
            payload_results=len(payload["results"]),
            payload_events=len(payload["events"]),
            track_events=track_events,
            normalized_pairs=counts.normalized_pairs,
            model_pairs=counts.model_pairs,
            sink_type=self.sink_type,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )
        # Successful publication is a per-frame hot path.  Keep it available
        # for diagnostics without flooding normal INFO logs; failures remain
        # ERROR records above.
        self.logger.debug(
            "published typed AI result batch",
            extra={
                "event": "ai_result_published",
                "operation": _PUBLISH_OPERATION,
                **report.as_dict(),
            },
        )
        return report


def publish_result_batch(
    sink: LegacyResultSink,
    batch: ResultBatch,
    *,
    model_to_pixel: ModelToPixel | None = None,
    logger: logging.Logger | None = None,
) -> PublishReport:
    """One-shot convenience wrapper around :class:`ResultBatchPublisher`."""

    return ResultBatchPublisher(
        sink,
        model_to_pixel=model_to_pixel,
        logger=logger,
    ).publish(batch)


__all__ = [
    "LegacyResultSink",
    "ModelToPixel",
    "PublishReport",
    "ResultBatchPublisher",
    "publish_result_batch",
    "to_legacy_payload",
]

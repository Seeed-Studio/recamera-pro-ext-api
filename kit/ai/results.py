"""Typed AI results with explicit coordinates and lossless legacy adapters.

The first application generation exchanged loosely structured dictionaries.
Those dictionaries remain accepted at the migration boundary, but new code can
now state whether coordinates are source-image pixels, normalized fractions or
model-input pixels.  Every public model validates itself at construction time;
invalid inference output therefore fails close to the decoder instead of much
later in a result transport.

``to_dict``/``from_dict`` implement a JSON-safe canonical representation.
``to_legacy_dict``/``from_legacy_dict`` bridge the existing nine applications.
When an object originates from a legacy mapping, the original mapping is kept
privately and returned by ``to_legacy_dict`` so unknown business fields and the
original aliases (``cls`` versus ``class_id``, for example) are not lost.
"""

from __future__ import annotations

import base64
import binascii
import copy
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from numbers import Integral, Real
from types import MappingProxyType
from typing import Any, TypeAlias

from ..errors import ConfigurationError, InputValidationError


_VALIDATE_OPERATION = "ai.result.validate"
_DECODE_OPERATION = "ai.result.deserialize"


def _input_error(message: str, field_name: str) -> InputValidationError:
    return InputValidationError(
        message,
        operation=_VALIDATE_OPERATION,
        details={"field": field_name},
    )


def _configuration_error(message: str, field_name: str) -> ConfigurationError:
    return ConfigurationError(
        message,
        operation=_DECODE_OPERATION,
        details={"field": field_name},
    )


def _number(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise _input_error(f"{field_name} must be a real number", field_name)
    result = float(value)
    if not math.isfinite(result):
        raise _input_error(f"{field_name} must be finite", field_name)
    return result


def _score(value: Any, field_name: str = "score") -> float:
    result = _number(value, field_name)
    if not 0.0 <= result <= 1.0:
        raise _input_error(f"{field_name} must be between 0 and 1", field_name)
    return result


def _identifier(value: Any, field_name: str, *, allow_zero: bool = True) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise _input_error(f"{field_name} must be an integer", field_name)
    result = int(value)
    minimum = 0 if allow_zero else 1
    if result < minimum:
        qualifier = "non-negative" if allow_zero else "positive"
        raise _input_error(f"{field_name} must be {qualifier}", field_name)
    return result


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise _input_error(f"{field_name} must be a string", field_name)
    return value


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _configuration_error(f"{field_name} must be a mapping", field_name)
    if any(not isinstance(key, str) for key in value):
        raise _configuration_error(
            f"{field_name} keys must be strings", field_name
        )
    return value


def _sequence(value: Any, field_name: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray, memoryview)) or not isinstance(
        value, Sequence
    ):
        raise _configuration_error(f"{field_name} must be a sequence", field_name)
    return value


def _normalize_json(value: Any, field_name: str) -> Any:
    """Copy JSON-compatible metadata into immutable containers."""

    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, Integral) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, Real) and not isinstance(value, bool):
        return _number(value, field_name)
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise _input_error(f"{field_name} keys must be strings", field_name)
        return MappingProxyType(
            {
                key: _normalize_json(item, f"{field_name}.{key}")
                for key, item in value.items()
            }
        )
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray, memoryview)
    ):
        return tuple(
            _normalize_json(item, f"{field_name}[{index}]")
            for index, item in enumerate(value)
        )
    raise _input_error(f"{field_name} must be JSON-compatible", field_name)


def _attributes(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _input_error("attributes must be a mapping", "attributes")
    normalized = _normalize_json(value, "attributes")
    return normalized


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _unknown_fields(data: Mapping[str, Any], known: set[str]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if key not in known}


def _remember_legacy(instance: Any, data: Mapping[str, Any]) -> None:
    object.__setattr__(instance, "_legacy", copy.deepcopy(dict(data)))


def _legacy_or(instance: Any, canonical: dict[str, Any]) -> dict[str, Any]:
    original = getattr(instance, "_legacy", None)
    return copy.deepcopy(original if original is not None else canonical)


def _required(data: Mapping[str, Any], key: str) -> Any:
    if key not in data:
        raise _configuration_error(f"missing required field: {key}", key)
    return data[key]


class CoordinateSpace(str, Enum):
    """Meaning of every x/y coordinate carried by a result.

    ``PIXEL``
        Coordinates in the original camera frame.  Values are non-negative;
        :class:`ResultBatch` can additionally check them against ``frame_size``.
    ``NORMALIZED``
        Fractions of an image extent.  Both axes are strictly constrained to
        the closed interval ``[0, 1]``.
    ``MODEL``
        Pixels in the tensor/model input before inverse letterbox or crop
        mapping.  These must never be sent as source pixels accidentally;
        :class:`ResultBatch` can check them against ``model_size``.
    """

    PIXEL = "pixel"
    NORMALIZED = "normalized"
    MODEL = "model"

    @classmethod
    def parse(cls, value: "CoordinateSpace | str") -> "CoordinateSpace":
        """Return a coordinate-space enum or raise ``ConfigurationError``."""

        if isinstance(value, cls):
            return value
        try:
            return cls(value)
        except (TypeError, ValueError) as exc:
            error = _configuration_error(
                "coordinate_space must be pixel, normalized, or model",
                "coordinate_space",
            )
            raise error from exc


@dataclass(frozen=True, slots=True)
class Box:
    """An axis-aligned ``xyxy`` rectangle in an explicit coordinate space.

    Coordinates must be finite, non-negative and ordered.  Normalized boxes
    are additionally bounded by one.  A zero-area box is representable because
    the native segmentation ABI uses it for an absent ROI; object-bearing
    models such as :class:`Detection` and :class:`Track` reject zero area.
    """

    x1: float
    y1: float
    x2: float
    y2: float
    space: CoordinateSpace = CoordinateSpace.PIXEL

    def __post_init__(self) -> None:
        object.__setattr__(self, "space", CoordinateSpace.parse(self.space))
        for name in ("x1", "y1", "x2", "y2"):
            value = _number(getattr(self, name), name)
            if value < 0.0:
                raise _input_error(f"{name} must be non-negative", name)
            if self.space is CoordinateSpace.NORMALIZED and value > 1.0:
                raise _input_error(f"{name} must be at most 1", name)
            object.__setattr__(self, name, value)
        if self.x2 < self.x1:
            raise _input_error("x2 must be greater than or equal to x1", "x2")
        if self.y2 < self.y1:
            raise _input_error("y2 must be greater than or equal to y1", "y2")

    @property
    def width(self) -> float:
        """Rectangle width in this box's declared space."""

        return self.x2 - self.x1

    @property
    def height(self) -> float:
        """Rectangle height in this box's declared space."""

        return self.y2 - self.y1

    @property
    def is_empty(self) -> bool:
        """Whether either rectangle dimension is zero."""

        return self.width == 0.0 or self.height == 0.0

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical JSON-compatible box representation."""

        return {
            "x1": self.x1,
            "y1": self.y1,
            "x2": self.x2,
            "y2": self.y2,
            "space": self.space.value,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Box":
        """Decode a canonical box mapping."""

        data = _mapping(data, "box")
        return cls(
            _required(data, "x1"),
            _required(data, "y1"),
            _required(data, "x2"),
            _required(data, "y2"),
            data.get("space", CoordinateSpace.PIXEL),
        )

    @classmethod
    def from_legacy(
        cls,
        coordinates: Sequence[Any],
        *,
        space: CoordinateSpace | str = CoordinateSpace.PIXEL,
    ) -> "Box":
        """Decode a historical ``[x1, y1, x2, y2]`` sequence."""

        coordinates = _sequence(coordinates, "box")
        if len(coordinates) != 4:
            raise _configuration_error("box must contain exactly four values", "box")
        return cls(*coordinates, space=space)

    def to_legacy(self) -> list[float]:
        """Return the historical four-element ``xyxy`` list."""

        return [self.x1, self.y1, self.x2, self.y2]


def _require_object_box(box: Box, owner: str) -> Box:
    if not isinstance(box, Box):
        raise _input_error(f"{owner}.box must be a Box", "box")
    if box.is_empty:
        raise _input_error(f"{owner}.box must have positive area", "box")
    return box


@dataclass(frozen=True, slots=True)
class Detection:
    """One localized class prediction.

    ``box`` identifies the object and owns its coordinate-space declaration.
    ``score`` is a finite probability in ``[0, 1]``; ``class_id`` is a
    non-negative integer.  ``attributes`` carries JSON-compatible app-specific
    annotations without weakening the core schema.
    """

    box: Box
    score: float
    class_id: int = 0
    label: str = ""
    attributes: Mapping[str, Any] = field(default_factory=dict, hash=False)
    _legacy: dict[str, Any] | None = field(
        default=None, init=False, repr=False, compare=False, hash=False
    )

    def __post_init__(self) -> None:
        _require_object_box(self.box, "detection")
        object.__setattr__(self, "score", _score(self.score))
        object.__setattr__(self, "class_id", _identifier(self.class_id, "class_id"))
        object.__setattr__(self, "label", _text(self.label, "label"))
        object.__setattr__(self, "attributes", _attributes(self.attributes))

    def to_dict(self) -> dict[str, Any]:
        """Return a canonical JSON-compatible detection mapping."""

        return {
            "type": "detection",
            "box": self.box.to_dict(),
            "score": self.score,
            "class_id": self.class_id,
            "label": self.label,
            "attributes": _thaw_json(self.attributes),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Detection":
        """Decode the canonical detection representation."""

        data = _mapping(data, "detection")
        return cls(
            box=Box.from_dict(_required(data, "box")),
            score=_required(data, "score"),
            class_id=data.get("class_id", 0),
            label=data.get("label", ""),
            attributes=data.get("attributes", {}),
        )

    @classmethod
    def from_legacy_dict(
        cls,
        data: Mapping[str, Any],
        *,
        space: CoordinateSpace | str = CoordinateSpace.PIXEL,
    ) -> "Detection":
        """Decode and retain a legacy detection dictionary losslessly."""

        data = _mapping(data, "detection")
        box_value = data.get("box", data.get("bbox"))
        if box_value is None:
            raise _configuration_error("legacy detection requires box or bbox", "box")
        actual_space = data.get("coordinate_space", data.get("space", space))
        known = {
            "box", "bbox", "score", "cls", "class_id", "cls_name", "label",
            "coordinate_space", "space",
        }
        result = cls(
            box=Box.from_legacy(box_value, space=actual_space),
            score=_required(data, "score"),
            class_id=data.get("cls", data.get("class_id", 0)),
            label=data.get("cls_name", data.get("label", "")),
            attributes=_unknown_fields(data, known),
        )
        _remember_legacy(result, data)
        return result

    def to_legacy_dict(self) -> dict[str, Any]:
        """Return the original legacy mapping or an equivalent flat mapping."""

        canonical = {
            **_thaw_json(self.attributes),
            "box": self.box.to_legacy(),
            "score": self.score,
            "cls": self.class_id,
            "cls_name": self.label,
            "coordinate_space": self.box.space.value,
        }
        return _legacy_or(self, canonical)


@dataclass(frozen=True, slots=True)
class Classification:
    """One image-level or optional ROI-localized class prediction.

    A classification always has a bounded ``score`` and non-negative
    ``class_id``.  ``box`` is optional for whole-image classifiers; when
    present it is non-empty and carries the ROI coordinate space explicitly.
    """

    score: float
    class_id: int = 0
    label: str = ""
    box: Box | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict, hash=False)
    _legacy: dict[str, Any] | None = field(
        default=None, init=False, repr=False, compare=False, hash=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "score", _score(self.score))
        object.__setattr__(self, "class_id", _identifier(self.class_id, "class_id"))
        object.__setattr__(self, "label", _text(self.label, "label"))
        if self.box is not None:
            _require_object_box(self.box, "classification")
        object.__setattr__(self, "attributes", _attributes(self.attributes))

    def to_dict(self) -> dict[str, Any]:
        """Return a canonical JSON-compatible classification mapping."""

        return {
            "type": "classification",
            "score": self.score,
            "class_id": self.class_id,
            "label": self.label,
            "box": None if self.box is None else self.box.to_dict(),
            "attributes": _thaw_json(self.attributes),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Classification":
        """Decode the canonical classification representation."""

        data = _mapping(data, "classification")
        box = data.get("box")
        return cls(
            score=_required(data, "score"),
            class_id=data.get("class_id", 0),
            label=data.get("label", ""),
            box=None if box is None else Box.from_dict(box),
            attributes=data.get("attributes", {}),
        )

    @classmethod
    def from_legacy_dict(
        cls,
        data: Mapping[str, Any],
        *,
        space: CoordinateSpace | str = CoordinateSpace.PIXEL,
    ) -> "Classification":
        """Decode and retain a legacy classification dictionary losslessly."""

        data = _mapping(data, "classification")
        box_value = data.get("box", data.get("bbox"))
        actual_space = data.get("coordinate_space", data.get("space", space))
        known = {
            "kind", "box", "bbox", "score", "cls", "class_id", "cls_name",
            "label", "coordinate_space", "space",
        }
        result = cls(
            score=_required(data, "score"),
            class_id=data.get("cls", data.get("class_id", 0)),
            label=data.get("label", data.get("cls_name", "")),
            box=(None if box_value is None
                 else Box.from_legacy(box_value, space=actual_space)),
            attributes=_unknown_fields(data, known),
        )
        _remember_legacy(result, data)
        return result

    def to_legacy_dict(self) -> dict[str, Any]:
        """Return the original legacy mapping or an equivalent flat mapping."""

        canonical: dict[str, Any] = {
            **_thaw_json(self.attributes),
            "kind": "classification",
            "score": self.score,
            "cls": self.class_id,
            "label": self.label,
        }
        if self.box is not None:
            canonical["box"] = self.box.to_legacy()
            canonical["coordinate_space"] = self.box.space.value
        return _legacy_or(self, canonical)


@dataclass(frozen=True, slots=True)
class Keypoint:
    """One named/indexed landmark with confidence and coordinate space.

    ``id`` is a non-negative stable index (for example a COCO-17 joint index).
    ``score`` is the visibility/confidence probability in ``[0, 1]``.  Pixel
    and model coordinates are non-negative; normalized coordinates are also
    bounded by one.
    """

    x: float
    y: float
    score: float = 1.0
    id: int = 0
    space: CoordinateSpace = CoordinateSpace.PIXEL

    def __post_init__(self) -> None:
        object.__setattr__(self, "space", CoordinateSpace.parse(self.space))
        for name in ("x", "y"):
            value = _number(getattr(self, name), name)
            if value < 0.0:
                raise _input_error(f"{name} must be non-negative", name)
            if self.space is CoordinateSpace.NORMALIZED and value > 1.0:
                raise _input_error(f"{name} must be at most 1", name)
            object.__setattr__(self, name, value)
        object.__setattr__(self, "score", _score(self.score))
        object.__setattr__(self, "id", _identifier(self.id, "keypoint_id"))

    def to_dict(self) -> dict[str, Any]:
        """Return a canonical JSON-compatible landmark mapping."""

        return {
            "x": self.x,
            "y": self.y,
            "score": self.score,
            "id": self.id,
            "space": self.space.value,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Keypoint":
        """Decode a canonical landmark mapping."""

        data = _mapping(data, "keypoint")
        return cls(
            x=_required(data, "x"),
            y=_required(data, "y"),
            score=data.get("score", 1.0),
            id=data.get("id", 0),
            space=data.get("space", CoordinateSpace.PIXEL),
        )

    def to_legacy(self) -> list[float]:
        """Return the historical ``[x, y, confidence]`` representation."""

        return [self.x, self.y, self.score]


@dataclass(frozen=True, slots=True)
class Pose:
    """A scored keypoint instance, optionally localized by an object box.

    The pose must contain at least one :class:`Keypoint`; point IDs must be
    unique and every point (and optional box) must use the same coordinate
    space.  This catches the common error of mixing model-input joints with an
    already un-letterboxed source-image box.
    """

    keypoints: tuple[Keypoint, ...]
    score: float = 1.0
    class_id: int = 0
    label: str = "person"
    box: Box | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict, hash=False)
    _legacy: dict[str, Any] | None = field(
        default=None, init=False, repr=False, compare=False, hash=False
    )

    def __post_init__(self) -> None:
        points = tuple(self.keypoints)
        if not points:
            raise _input_error("pose must contain at least one keypoint", "keypoints")
        if any(not isinstance(point, Keypoint) for point in points):
            raise _input_error("pose keypoints must be Keypoint objects", "keypoints")
        ids = [point.id for point in points]
        if len(ids) != len(set(ids)):
            raise _input_error("pose keypoint IDs must be unique", "keypoints")
        spaces = {point.space for point in points}
        if len(spaces) != 1:
            raise _input_error("pose keypoints must share one coordinate space", "keypoints")
        if self.box is not None:
            _require_object_box(self.box, "pose")
            if self.box.space not in spaces:
                raise _input_error(
                    "pose box and keypoints must share one coordinate space", "box"
                )
        object.__setattr__(self, "keypoints", points)
        object.__setattr__(self, "score", _score(self.score))
        object.__setattr__(self, "class_id", _identifier(self.class_id, "class_id"))
        object.__setattr__(self, "label", _text(self.label, "label"))
        object.__setattr__(self, "attributes", _attributes(self.attributes))

    @property
    def space(self) -> CoordinateSpace:
        """The common coordinate space of the instance's points and box."""

        return self.keypoints[0].space

    def to_dict(self) -> dict[str, Any]:
        """Return a canonical JSON-compatible pose mapping."""

        return {
            "type": "pose",
            "keypoints": [point.to_dict() for point in self.keypoints],
            "score": self.score,
            "class_id": self.class_id,
            "label": self.label,
            "box": None if self.box is None else self.box.to_dict(),
            "attributes": _thaw_json(self.attributes),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Pose":
        """Decode the canonical pose representation."""

        data = _mapping(data, "pose")
        points = _sequence(_required(data, "keypoints"), "keypoints")
        box = data.get("box")
        return cls(
            keypoints=tuple(Keypoint.from_dict(point) for point in points),
            score=data.get("score", 1.0),
            class_id=data.get("class_id", 0),
            label=data.get("label", "person"),
            box=None if box is None else Box.from_dict(box),
            attributes=data.get("attributes", {}),
        )

    @classmethod
    def from_legacy_dict(
        cls,
        data: Mapping[str, Any],
        *,
        space: CoordinateSpace | str = CoordinateSpace.PIXEL,
    ) -> "Pose":
        """Decode legacy ``keypoints`` arrays and retain all original fields."""

        data = _mapping(data, "pose")
        raw_points = data.get("keypoints", data.get("points"))
        if raw_points is None:
            raise _configuration_error("legacy pose requires keypoints", "keypoints")
        raw_points = _sequence(raw_points, "keypoints")
        actual_space = CoordinateSpace.parse(
            data.get("coordinate_space", data.get("space", space))
        )
        explicit_ids = data.get("keypoint_ids")
        if explicit_ids is not None:
            explicit_ids = _sequence(explicit_ids, "keypoint_ids")
            if len(explicit_ids) != len(raw_points):
                raise _configuration_error(
                    "keypoint_ids length must match keypoints", "keypoint_ids"
                )
        points: list[Keypoint] = []
        for index, raw_point in enumerate(raw_points):
            point_id = explicit_ids[index] if explicit_ids is not None else index
            if isinstance(raw_point, Mapping):
                point_data = dict(raw_point)
                point_data.setdefault("id", point_id)
                point_data.setdefault("space", actual_space.value)
                points.append(Keypoint.from_dict(point_data))
                continue
            raw_point = _sequence(raw_point, f"keypoints[{index}]")
            if not 2 <= len(raw_point) <= 4:
                raise _configuration_error(
                    "legacy keypoint must contain x, y, optional score and id",
                    f"keypoints[{index}]",
                )
            points.append(
                Keypoint(
                    x=raw_point[0],
                    y=raw_point[1],
                    score=raw_point[2] if len(raw_point) >= 3 else 1.0,
                    id=raw_point[3] if len(raw_point) == 4 else point_id,
                    space=actual_space,
                )
            )
        box_value = data.get("box", data.get("bbox"))
        known = {
            "kind", "keypoints", "points", "keypoint_ids", "box", "bbox",
            "score", "cls", "class_id", "cls_name", "label",
            "coordinate_space", "space",
        }
        result = cls(
            keypoints=tuple(points),
            score=data.get("score", 1.0),
            class_id=data.get("cls", data.get("class_id", 0)),
            label=data.get("cls_name", data.get("label", "person")),
            box=(None if box_value is None
                 else Box.from_legacy(box_value, space=actual_space)),
            attributes=_unknown_fields(data, known),
        )
        _remember_legacy(result, data)
        return result

    def to_legacy_dict(self) -> dict[str, Any]:
        """Return the original mapping or the app-compatible keypoint shape."""

        canonical: dict[str, Any] = {
            **_thaw_json(self.attributes),
            "keypoints": [point.to_legacy() for point in self.keypoints],
            "score": self.score,
            "cls": self.class_id,
            "cls_name": self.label,
            "coordinate_space": self.space.value,
        }
        ids = [point.id for point in self.keypoints]
        if ids != list(range(len(self.keypoints))):
            canonical["keypoint_ids"] = ids
        if self.box is not None:
            canonical["box"] = self.box.to_legacy()
        return _legacy_or(self, canonical)


@dataclass(frozen=True, slots=True)
class Track:
    """One tracked object with a stable non-negative track ID.

    ``box`` must have positive area and declares the coordinate space.  Scores
    and class IDs follow :class:`Detection`; ``attributes`` is the place for
    legacy state such as ``state``, ``in_zone`` or ``speed_px_s``.
    """

    track_id: int
    box: Box
    score: float
    class_id: int = 0
    label: str = ""
    attributes: Mapping[str, Any] = field(default_factory=dict, hash=False)
    _legacy: dict[str, Any] | None = field(
        default=None, init=False, repr=False, compare=False, hash=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "track_id", _identifier(self.track_id, "track_id"))
        _require_object_box(self.box, "track")
        object.__setattr__(self, "score", _score(self.score))
        object.__setattr__(self, "class_id", _identifier(self.class_id, "class_id"))
        object.__setattr__(self, "label", _text(self.label, "label"))
        object.__setattr__(self, "attributes", _attributes(self.attributes))

    def to_dict(self) -> dict[str, Any]:
        """Return a canonical JSON-compatible tracking mapping."""

        return {
            "type": "track",
            "track_id": self.track_id,
            "box": self.box.to_dict(),
            "score": self.score,
            "class_id": self.class_id,
            "label": self.label,
            "attributes": _thaw_json(self.attributes),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Track":
        """Decode the canonical tracking representation."""

        data = _mapping(data, "track")
        return cls(
            track_id=_required(data, "track_id"),
            box=Box.from_dict(_required(data, "box")),
            score=_required(data, "score"),
            class_id=data.get("class_id", 0),
            label=data.get("label", ""),
            attributes=data.get("attributes", {}),
        )

    @classmethod
    def from_legacy_dict(
        cls,
        data: Mapping[str, Any],
        *,
        space: CoordinateSpace | str = CoordinateSpace.PIXEL,
    ) -> "Track":
        """Decode and retain a legacy tracking event losslessly."""

        data = _mapping(data, "track")
        box_value = data.get("box", data.get("bbox"))
        if box_value is None:
            raise _configuration_error("legacy track requires box or bbox", "box")
        actual_space = data.get("coordinate_space", data.get("space", space))
        known = {
            "kind", "track_id", "box", "bbox", "score", "cls", "class_id",
            "cls_name", "label", "coordinate_space", "space",
        }
        result = cls(
            track_id=_required(data, "track_id"),
            box=Box.from_legacy(box_value, space=actual_space),
            score=_required(data, "score"),
            class_id=data.get("cls", data.get("class_id", 0)),
            label=data.get(
                "label", data.get("cls_name", data.get("state", "object"))
            ),
            attributes=_unknown_fields(data, known),
        )
        _remember_legacy(result, data)
        return result

    def to_legacy_dict(self) -> dict[str, Any]:
        """Return the original tracking event or an equivalent flat mapping."""

        canonical = {
            **_thaw_json(self.attributes),
            "kind": "track",
            "track_id": self.track_id,
            "box": self.box.to_legacy(),
            "score": self.score,
            "cls": self.class_id,
            "label": self.label,
            "coordinate_space": self.box.space.value,
        }
        return _legacy_or(self, canonical)


def _mask_bytes(mask: Any, width: int, height: int) -> tuple[bytes, int, int]:
    if mask is None:
        raw = b""
    elif isinstance(mask, (bytes, bytearray)):
        raw = bytes(mask)
    elif isinstance(mask, memoryview):
        if mask.itemsize != 1 or not mask.contiguous:
            raise _input_error("mask buffer must be contiguous one-byte values", "mask")
        if mask.ndim == 2:
            inferred_h, inferred_w = mask.shape
            if width and width != inferred_w:
                raise _input_error("mask width does not match its buffer shape", "mask")
            if height and height != inferred_h:
                raise _input_error("mask height does not match its buffer shape", "mask")
            width = width or int(inferred_w)
            height = height or int(inferred_h)
        raw = mask.tobytes()
    elif isinstance(mask, Sequence) and not isinstance(mask, str):
        rows = list(mask)
        if rows and isinstance(rows[0], Sequence) and not isinstance(
            rows[0], (str, bytes, bytearray, memoryview)
        ):
            nested = [list(row) for row in rows]
            row_width = len(nested[0])
            if any(len(row) != row_width for row in nested):
                raise _input_error("mask rows must be rectangular", "mask")
            if width and width != row_width:
                raise _input_error("mask width does not match its rows", "mask")
            if height and height != len(nested):
                raise _input_error("mask height does not match its rows", "mask")
            width = width or row_width
            height = height or len(nested)
            values = [item for row in nested for item in row]
        else:
            values = rows
        packed = bytearray()
        for index, value in enumerate(values):
            if isinstance(value, bool) or not isinstance(value, Integral):
                raise _input_error(
                    f"mask[{index}] must be an integer byte", "mask"
                )
            value = int(value)
            if not 0 <= value <= 255:
                raise _input_error(f"mask[{index}] must be in [0, 255]", "mask")
            packed.append(value)
        raw = bytes(packed)
    else:
        try:
            view = memoryview(mask)
        except TypeError as exc:
            error = _input_error("mask must be bytes or a rectangular byte grid", "mask")
            raise error from exc
        return _mask_bytes(view, width, height)

    if not raw:
        if width != 0 or height != 0:
            raise _input_error("an empty mask requires width=height=0", "mask")
        return raw, 0, 0
    if width <= 0 or height <= 0:
        raise _input_error("a non-empty mask requires positive width and height", "mask")
    if len(raw) != width * height:
        raise _input_error("mask length must equal width * height", "mask")
    return raw, width, height


@dataclass(frozen=True, slots=True)
class Segmentation:
    """A row-major one-byte-per-pixel segmentation mask and optional ROI.

    Non-empty masks require positive ``width`` and ``height`` and exactly
    ``width * height`` bytes.  Empty masks are represented only as ``b""`` with
    both dimensions zero, matching the native extension ABI.  ``box`` is an
    optional ROI whose coordinate space is explicit; mask pixels themselves
    are indexed in the mask grid, not in an implicit image coordinate space.
    """

    mask: bytes
    width: int
    height: int
    score: float = 1.0
    class_id: int = 0
    label: str = ""
    box: Box | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict, hash=False)
    _legacy: dict[str, Any] | None = field(
        default=None, init=False, repr=False, compare=False, hash=False
    )

    def __post_init__(self) -> None:
        width = _identifier(self.width, "mask_width")
        height = _identifier(self.height, "mask_height")
        mask, width, height = _mask_bytes(self.mask, width, height)
        object.__setattr__(self, "mask", mask)
        object.__setattr__(self, "width", width)
        object.__setattr__(self, "height", height)
        object.__setattr__(self, "score", _score(self.score))
        object.__setattr__(self, "class_id", _identifier(self.class_id, "class_id"))
        object.__setattr__(self, "label", _text(self.label, "label"))
        if self.box is not None and not isinstance(self.box, Box):
            raise _input_error("segmentation.box must be a Box", "box")
        object.__setattr__(self, "attributes", _attributes(self.attributes))

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe mapping with the mask encoded as base64."""

        return {
            "type": "segmentation",
            "mask": base64.b64encode(self.mask).decode("ascii"),
            "mask_encoding": "base64",
            "width": self.width,
            "height": self.height,
            "score": self.score,
            "class_id": self.class_id,
            "label": self.label,
            "box": None if self.box is None else self.box.to_dict(),
            "attributes": _thaw_json(self.attributes),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Segmentation":
        """Decode a canonical base64 segmentation representation."""

        data = _mapping(data, "segmentation")
        if data.get("mask_encoding", "base64") != "base64":
            raise _configuration_error("unsupported mask_encoding", "mask_encoding")
        encoded = _required(data, "mask")
        if not isinstance(encoded, str):
            raise _configuration_error("canonical mask must be base64 text", "mask")
        try:
            mask = base64.b64decode(encoded.encode("ascii"), validate=True)
        except (UnicodeEncodeError, binascii.Error) as exc:
            error = _configuration_error("mask is not valid base64", "mask")
            raise error from exc
        box = data.get("box")
        return cls(
            mask=mask,
            width=_required(data, "width"),
            height=_required(data, "height"),
            score=data.get("score", 1.0),
            class_id=data.get("class_id", 0),
            label=data.get("label", ""),
            box=None if box is None else Box.from_dict(box),
            attributes=data.get("attributes", {}),
        )

    @classmethod
    def from_legacy_dict(
        cls,
        data: Mapping[str, Any],
        *,
        space: CoordinateSpace | str = CoordinateSpace.PIXEL,
    ) -> "Segmentation":
        """Decode bytes or nested legacy masks and retain the mapping exactly."""

        data = _mapping(data, "segmentation")
        if "mask" in data:
            mask = data["mask"]
        elif "mask_bytes" in data:
            mask = data["mask_bytes"]
        else:
            raise _configuration_error("legacy segmentation requires mask", "mask")
        width = data.get("mask_w", data.get("mask_width", data.get("width", 0)))
        height = data.get("mask_h", data.get("mask_height", data.get("height", 0)))
        box_value = data.get("box", data.get("bbox"))
        actual_space = data.get("coordinate_space", data.get("space", space))
        known = {
            "kind", "mask", "mask_bytes", "mask_w", "mask_h", "mask_width",
            "mask_height", "width", "height", "box", "bbox", "score", "cls",
            "class_id", "cls_name", "label", "coordinate_space", "space",
        }
        result = cls(
            mask=mask,
            width=width,
            height=height,
            score=data.get("score", 1.0),
            class_id=data.get("cls", data.get("class_id", 0)),
            label=data.get("cls_name", data.get("label", "")),
            box=(None if box_value is None
                 else Box.from_legacy(box_value, space=actual_space)),
            attributes=_unknown_fields(data, known),
        )
        _remember_legacy(result, data)
        return result

    def to_legacy_dict(self) -> dict[str, Any]:
        """Return the original mapping or the SDK-compatible flat mask shape."""

        canonical: dict[str, Any] = {
            **_thaw_json(self.attributes),
            "mask_bytes": self.mask,
            "mask_w": self.width,
            "mask_h": self.height,
            "score": self.score,
            "cls": self.class_id,
            "cls_name": self.label,
        }
        if self.box is not None:
            canonical["box"] = self.box.to_legacy()
            canonical["coordinate_space"] = self.box.space.value
        return _legacy_or(self, canonical)


AIResult: TypeAlias = Detection | Classification | Pose | Track | Segmentation


_CANONICAL_TYPES = {
    "detection": Detection,
    "classification": Classification,
    "pose": Pose,
    "track": Track,
    "segmentation": Segmentation,
}


def _result_from_dict(data: Mapping[str, Any]) -> AIResult:
    data = _mapping(data, "result")
    result_type = data.get("type")
    model = _CANONICAL_TYPES.get(result_type)
    if model is None:
        raise _configuration_error(f"unsupported result type: {result_type!r}", "type")
    return model.from_dict(data)


def _result_from_legacy(
    data: Mapping[str, Any], space: CoordinateSpace
) -> AIResult:
    data = _mapping(data, "result")
    kind = str(data.get("kind", "")).lower()
    if data.get("track_id") is not None:
        return Track.from_legacy_dict(data, space=space)
    if "mask" in data or "mask_bytes" in data:
        return Segmentation.from_legacy_dict(data, space=space)
    if "keypoints" in data or "points" in data:
        return Pose.from_legacy_dict(data, space=space)
    class_attributes = any(
        key in data for key in ("gender", "age", "race", "emotion")
    )
    if kind == "classification" or class_attributes:
        return Classification.from_legacy_dict(data, space=space)
    if data.get("box") is not None or data.get("bbox") is not None:
        return Detection.from_legacy_dict(data, space=space)
    if data.get("label") is not None or data.get("cls_name") is not None:
        return Classification.from_legacy_dict(data, space=space)
    raise _configuration_error("cannot infer legacy AI result type", "result")


def _size(value: Any, field_name: str) -> tuple[int, int] | None:
    if value is None:
        return None
    value = _sequence(value, field_name)
    if len(value) != 2:
        raise _configuration_error(f"{field_name} must contain width and height", field_name)
    return (
        _identifier(value[0], f"{field_name}.width", allow_zero=False),
        _identifier(value[1], f"{field_name}.height", allow_zero=False),
    )


def _coordinate_items(result: AIResult) -> tuple[list[Box], list[Keypoint]]:
    boxes: list[Box] = []
    points: list[Keypoint] = []
    if isinstance(result, (Detection, Track)):
        boxes.append(result.box)
    elif isinstance(result, Classification):
        if result.box is not None:
            boxes.append(result.box)
    elif isinstance(result, Pose):
        if result.box is not None:
            boxes.append(result.box)
        points.extend(result.keypoints)
    elif isinstance(result, Segmentation) and result.box is not None:
        boxes.append(result.box)
    return boxes, points


@dataclass(frozen=True, slots=True)
class ResultBatch:
    """Validated results associated with one frame or inference invocation.

    ``pts_us`` is a non-negative monotonic frame timestamp (zero means
    unassociated, matching the extension API).  ``frame_size`` bounds pixel
    coordinates and ``model_size`` bounds model coordinates when supplied;
    normalized coordinates are always checked at their own construction.
    Mixed task types and coordinate spaces are allowed because multi-head
    workflows may publish them together, but every individual value remains
    explicit and validated.

    The canonical ``to_dict`` representation is versioned and JSON-safe,
    including base64 segmentation masks.  Legacy payloads can be read with
    :meth:`from_legacy_dict` and returned without dropping unknown fields.
    """

    results: tuple[AIResult, ...] = ()
    pts_us: int = 0
    source_id: str = ""
    frame_size: tuple[int, int] | None = None
    model_size: tuple[int, int] | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict, hash=False)
    _legacy: dict[str, Any] | None = field(
        default=None, init=False, repr=False, compare=False, hash=False
    )

    SCHEMA_VERSION = 1

    def __post_init__(self) -> None:
        results = tuple(self.results)
        allowed = (Detection, Classification, Pose, Track, Segmentation)
        if any(not isinstance(result, allowed) for result in results):
            raise _input_error("results must contain typed AI result objects", "results")
        object.__setattr__(self, "results", results)
        object.__setattr__(self, "pts_us", _identifier(self.pts_us, "pts_us"))
        object.__setattr__(self, "source_id", _text(self.source_id, "source_id"))
        object.__setattr__(self, "frame_size", _size(self.frame_size, "frame_size"))
        object.__setattr__(self, "model_size", _size(self.model_size, "model_size"))
        object.__setattr__(self, "attributes", _attributes(self.attributes))

        track_ids = [
            result.track_id for result in results if isinstance(result, Track)
        ]
        if len(track_ids) != len(set(track_ids)):
            raise _input_error("track IDs must be unique within a batch", "results")

        for result in results:
            boxes, points = _coordinate_items(result)
            for box in boxes:
                self._validate_extent(box.space, box.x2, box.y2)
            for point in points:
                self._validate_extent(point.space, point.x, point.y)

    def _validate_extent(
        self, space: CoordinateSpace, x: float, y: float
    ) -> None:
        extent = None
        field_name = "coordinates"
        if space is CoordinateSpace.PIXEL:
            extent = self.frame_size
            field_name = "frame_size"
        elif space is CoordinateSpace.MODEL:
            extent = self.model_size
            field_name = "model_size"
        if extent is not None and (x > extent[0] or y > extent[1]):
            raise _input_error(
                f"coordinates exceed declared {field_name}", field_name
            )

    def to_dict(self) -> dict[str, Any]:
        """Return the versioned, JSON-compatible canonical batch mapping."""

        return {
            "schema_version": self.SCHEMA_VERSION,
            "pts_us": self.pts_us,
            "source_id": self.source_id,
            "frame_size": None if self.frame_size is None else list(self.frame_size),
            "model_size": None if self.model_size is None else list(self.model_size),
            "results": [result.to_dict() for result in self.results],
            "attributes": _thaw_json(self.attributes),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ResultBatch":
        """Decode a canonical batch and reject unknown schema versions."""

        data = _mapping(data, "batch")
        version = data.get("schema_version", cls.SCHEMA_VERSION)
        if version != cls.SCHEMA_VERSION:
            raise _configuration_error(
                f"unsupported result schema version: {version!r}", "schema_version"
            )
        raw_results = _sequence(data.get("results", ()), "results")
        return cls(
            results=tuple(_result_from_dict(result) for result in raw_results),
            pts_us=data.get("pts_us", 0),
            source_id=data.get("source_id", ""),
            frame_size=data.get("frame_size"),
            model_size=data.get("model_size"),
            attributes=data.get("attributes", {}),
        )

    def to_json(self, **json_kwargs: Any) -> str:
        """Serialize the canonical representation with :func:`json.dumps`."""

        return json.dumps(self.to_dict(), **json_kwargs)

    @classmethod
    def from_json(cls, payload: str | bytes | bytearray) -> "ResultBatch":
        """Deserialize JSON text into a validated batch."""

        try:
            data = json.loads(payload)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            error = _configuration_error("invalid result batch JSON", "payload")
            raise error from exc
        return cls.from_dict(data)

    @classmethod
    def from_legacy_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        space: CoordinateSpace | str = CoordinateSpace.PIXEL,
    ) -> "ResultBatch":
        """Decode a historical payload while preserving it for exact output.

        The historical ``results`` list is typed.  Business ``events`` remain
        in batch attributes because many are temporal state transitions rather
        than inference results; callers may convert track events explicitly
        with :meth:`Track.from_legacy_dict`.
        """

        payload = _mapping(payload, "payload")
        actual_space = CoordinateSpace.parse(space)
        raw_results = _sequence(payload.get("results", ()), "results")
        frame_size = payload.get("frame_size")
        frame = payload.get("frame")
        if frame_size is None and isinstance(frame, Mapping):
            width = frame.get("width", frame.get("w"))
            height = frame.get("height", frame.get("h"))
            if width is not None and height is not None:
                frame_size = (width, height)
        known = {
            "results", "pts_us", "source_id", "app", "frame_size",
            "model_size", "frame",
        }
        result = cls(
            results=tuple(
                _result_from_legacy(item, actual_space) for item in raw_results
            ),
            pts_us=payload.get("pts_us", 0),
            source_id=payload.get("source_id", payload.get("app", "")),
            frame_size=frame_size,
            model_size=payload.get("model_size"),
            attributes=_unknown_fields(payload, known),
        )
        _remember_legacy(result, payload)
        return result

    def to_legacy_dict(self) -> dict[str, Any]:
        """Return the original payload or an explicit app-compatible payload."""

        canonical: dict[str, Any] = {
            **_thaw_json(self.attributes),
            "results": [result.to_legacy_dict() for result in self.results],
            "pts_us": self.pts_us,
        }
        if self.source_id:
            canonical["source_id"] = self.source_id
        if self.frame_size is not None:
            canonical["frame"] = {
                "width": self.frame_size[0],
                "height": self.frame_size[1],
            }
        if self.model_size is not None:
            canonical["model_size"] = list(self.model_size)
        return _legacy_or(self, canonical)


__all__ = [
    "AIResult",
    "Box",
    "Classification",
    "CoordinateSpace",
    "Detection",
    "Keypoint",
    "Pose",
    "ResultBatch",
    "Segmentation",
    "Track",
]

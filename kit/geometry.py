"""Validated drawing primitives for the canonical Result Hub overlay.

Applications describe *what* to draw; the installed manifest remains the
authority for the coordinate space and render policy.  Consequently these
builders deliberately do not accept a ``space`` argument.  Managed Result Hub
ingress discards any payload-supplied space and injects the generation-bound
``output.fields[from=\"geometry[]\"].coord`` declaration instead.

The wire representation is intentionally small and uniform::

    {"type": "polygon", "points": [[10.0, 20.0], ...],
     "style": {"color": "#00ff00", "line_width": 2}}

``box``/``quad`` are compatibility helpers that produce polygons;
``keypoints``/``pose`` produce point and line primitives.  Existing
``results[].box/quad/keypoints`` remain unchanged and can coexist with this
top-level ``geometry`` array.
"""
from __future__ import annotations

import math
import numbers
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Optional


PRIMITIVE_TYPES = frozenset(("point", "line", "polyline", "polygon"))
STYLE_FIELDS = frozenset((
    "color", "line_width", "point_radius", "fill", "fill_color", "opacity",
))

MAX_GEOMETRY_ITEMS = 256
MAX_POINTS_PER_PRIMITIVE = 256
MAX_TOTAL_POINTS = 4096
MAX_LABEL_CHARS = 128
MAX_ID_CHARS = 64

_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_POINT_COUNTS = {
    "point": (1, 1),
    "line": (2, 2),
    "polyline": (2, MAX_POINTS_PER_PRIMITIVE),
    "polygon": (3, MAX_POINTS_PER_PRIMITIVE),
}


class GeometryError(ValueError):
    """A primitive cannot be represented by the bounded wire contract."""


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise GeometryError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise GeometryError(f"{name} must be finite")
    if result < 0:
        raise GeometryError(f"{name} must be non-negative")
    return result


def _is_sequence(value: Any) -> bool:
    """Accept ordinary sequences and NumPy-style one-dimensional arrays."""
    return (not isinstance(value, (str, bytes, bytearray, Mapping))
            and (isinstance(value, Sequence)
                 or (hasattr(value, "__len__") and hasattr(value, "__getitem__"))))


def _points(value: Any, kind: str, *, max_points: int = MAX_POINTS_PER_PRIMITIVE
            ) -> list[list[float]]:
    if not _is_sequence(value):
        raise GeometryError("points must be a sequence")
    minimum, inherent_max = _POINT_COUNTS[kind]
    limit = min(inherent_max, max(1, int(max_points)))
    if not minimum <= len(value) <= limit:
        expected = str(minimum) if minimum == limit else f"{minimum}..{limit}"
        raise GeometryError(f"{kind} requires {expected} points")
    result = []
    for index, raw in enumerate(value):
        if not _is_sequence(raw) or len(raw) < 2:
            raise GeometryError(f"points[{index}] must be [x,y]")
        result.append([
            _number(raw[0], f"points[{index}].x"),
            _number(raw[1], f"points[{index}].y"),
        ])
    return result


def sanitize_style(value: Any, *, strict: bool = False) -> dict[str, Any]:
    """Return the safe style subset.

    Hub ingress uses ``strict=False`` so one malformed optional style cannot
    erase otherwise valid geometry; application builders use ``strict=True``
    and fail early.  Unknown keys are never copied.
    """
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        if strict:
            raise GeometryError("style must be an object")
        return {}
    result: dict[str, Any] = {}
    for key in STYLE_FIELDS:
        if key not in value:
            continue
        raw = value[key]
        valid = True
        normalized: Any = raw
        if key in ("color", "fill_color"):
            valid = isinstance(raw, str) and bool(_COLOR_RE.fullmatch(raw))
            normalized = raw.lower() if valid else raw
        elif key == "fill":
            valid = isinstance(raw, bool)
        else:
            valid = (not isinstance(raw, bool)
                     and isinstance(raw, numbers.Real)
                     and math.isfinite(float(raw)))
            if valid:
                normalized = float(raw)
                if key == "opacity":
                    valid = 0.0 <= normalized <= 1.0
                elif key == "line_width":
                    valid = 0.25 <= normalized <= 16.0
                elif key == "point_radius":
                    valid = 0.5 <= normalized <= 32.0
        if not valid:
            if strict:
                raise GeometryError(f"invalid style.{key}")
            continue
        result[key] = normalized
    if strict:
        unknown = set(value) - STYLE_FIELDS
        if unknown:
            raise GeometryError("unknown style field(s): " + ", ".join(sorted(unknown)))
    return result


def primitive(kind: str, points: Sequence[Sequence[float]], *,
              style: Optional[Mapping[str, Any]] = None,
              id: Optional[str] = None, label: Optional[str] = None,
              score: Optional[float] = None, **style_values: Any) -> dict[str, Any]:
    """Build one validated canonical primitive."""
    kind = str(kind).strip().lower()
    if kind not in PRIMITIVE_TYPES:
        raise GeometryError(f"unsupported primitive type {kind!r}")
    item: dict[str, Any] = {"type": kind, "points": _points(points, kind)}
    if id is not None:
        if not isinstance(id, str) or not _ID_RE.fullmatch(id):
            raise GeometryError("id must be a 1..64 character token")
        item["id"] = id
    if label is not None:
        if not isinstance(label, str) or len(label) > MAX_LABEL_CHARS:
            raise GeometryError(f"label must be at most {MAX_LABEL_CHARS} characters")
        item["label"] = label
    if score is not None:
        score_value = _number(score, "score")
        if score_value > 1:
            raise GeometryError("score must be in [0,1]")
        item["score"] = score_value
    merged = dict(style or {})
    merged.update(style_values)
    safe_style = sanitize_style(merged, strict=True)
    if safe_style:
        item["style"] = safe_style
    return item


def point(x: float, y: float, **kwargs: Any) -> dict[str, Any]:
    return primitive("point", ((x, y),), **kwargs)


def line(a: Sequence[float], b: Sequence[float], **kwargs: Any) -> dict[str, Any]:
    return primitive("line", (a, b), **kwargs)


def polyline(points: Sequence[Sequence[float]], **kwargs: Any) -> dict[str, Any]:
    return primitive("polyline", points, **kwargs)


def polygon(points: Sequence[Sequence[float]], **kwargs: Any) -> dict[str, Any]:
    return primitive("polygon", points, **kwargs)


def box(xyxy: Sequence[float], **kwargs: Any) -> dict[str, Any]:
    """Compatibility adapter: ``[x1,y1,x2,y2]`` -> polygon."""
    if not _is_sequence(xyxy) or len(xyxy) != 4:
        raise GeometryError("box must be [x1,y1,x2,y2]")
    x1, y1, x2, y2 = (_number(value, f"box[{index}]")
                      for index, value in enumerate(xyxy))
    if x2 < x1 or y2 < y1:
        raise GeometryError("box coordinates must be ordered")
    return polygon(((x1, y1), (x2, y1), (x2, y2), (x1, y2)), **kwargs)


def quad(points: Sequence[Sequence[float]], **kwargs: Any) -> dict[str, Any]:
    """Compatibility adapter: four OCR/OBB points -> polygon."""
    if not _is_sequence(points) or len(points) != 4:
        raise GeometryError("quad must contain exactly four points")
    return polygon(points, **kwargs)


def keypoints(points: Sequence[Sequence[float]], *, labels: Optional[Sequence[str]] = None,
              conf_min: float = 0.0, id_prefix: str = "keypoint",
              **kwargs: Any) -> list[dict[str, Any]]:
    """Compatibility adapter: keypoint tuples -> point primitives.

    Input points may be ``[x,y]`` or ``[x,y,score]``.  Scores below
    ``conf_min`` are omitted.  IDs remain stable by input index.
    """
    threshold = _number(conf_min, "conf_min")
    if threshold > 1:
        raise GeometryError("conf_min must be in [0,1]")
    if not isinstance(id_prefix, str) or not _ID_RE.fullmatch(id_prefix):
        raise GeometryError("id_prefix must be a token")
    output = []
    for index, raw in enumerate(points):
        if not _is_sequence(raw) or len(raw) < 2:
            raise GeometryError(f"keypoints[{index}] must be [x,y,(score)]")
        score = None
        if len(raw) >= 3 and raw[2] is not None:
            score = _number(raw[2], f"keypoints[{index}].score")
            if score > 1:
                raise GeometryError("keypoint score must be in [0,1]")
            if score < threshold:
                continue
        label = labels[index] if labels is not None and index < len(labels) else None
        output.append(point(raw[0], raw[1], id=f"{id_prefix}-{index}",
                            label=label, score=score, **kwargs))
    return output


def pose(points: Sequence[Sequence[float]], skeleton: Iterable[Sequence[int]], *,
         conf_min: float = 0.0, point_style: Optional[Mapping[str, Any]] = None,
         line_style: Optional[Mapping[str, Any]] = None,
         labels: Optional[Sequence[str]] = None,
         id_prefix: str = "pose") -> list[dict[str, Any]]:
    """Compatibility adapter: pose keypoints+skeleton -> lines then points."""
    threshold = _number(conf_min, "conf_min")
    if threshold > 1:
        raise GeometryError("conf_min must be in [0,1]")
    visible: dict[int, Sequence[float]] = {}
    for index, raw in enumerate(points):
        if not _is_sequence(raw) or len(raw) < 2:
            raise GeometryError(f"pose point {index} must be [x,y,(score)]")
        score = 1.0 if len(raw) < 3 or raw[2] is None else _number(
            raw[2], f"pose[{index}].score")
        if score > 1:
            raise GeometryError("pose score must be in [0,1]")
        if score >= threshold:
            visible[index] = raw
    output: list[dict[str, Any]] = []
    for edge_index, edge in enumerate(skeleton):
        if (not _is_sequence(edge) or len(edge) != 2
                or isinstance(edge[0], bool) or isinstance(edge[1], bool)
                or not isinstance(edge[0], numbers.Integral)
                or not isinstance(edge[1], numbers.Integral)):
            raise GeometryError("skeleton edges must be [from_index,to_index]")
        left, right = int(edge[0]), int(edge[1])
        if left < 0 or right < 0 or left >= len(points) or right >= len(points):
            raise GeometryError("skeleton index is outside pose points")
        if left in visible and right in visible:
            output.append(line(visible[left], visible[right],
                               id=f"{id_prefix}-edge-{edge_index}",
                               style=line_style))
    for index, raw in visible.items():
        label = labels[index] if labels is not None and index < len(labels) else None
        score = raw[2] if len(raw) >= 3 and raw[2] is not None else None
        output.append(point(
            raw[0], raw[1], id=f"{id_prefix}-point-{index}", label=label,
            score=score, style=point_style,
        ))
    return output


def sanitize_geometry(values: Any, *, space: str,
                      allowed_types: Iterable[str] = PRIMITIVE_TYPES,
                      max_items: int = 64, max_points: int = 128,
                      default_style: Optional[Mapping[str, Any]] = None,
                      frame_size: Optional[Sequence[int]] = None) -> list[dict[str, Any]]:
    """Validate untrusted wire primitives and inject a trusted coordinate space.

    Invalid primitives are omitted independently.  Unknown keys/styles are
    discarded.  Pixel coordinates are bounded by ``frame_size`` when available;
    without reference dimensions they remain diagnostic with ``space=unknown``.
    """
    if not isinstance(values, list):
        return []
    allowed = PRIMITIVE_TYPES.intersection(str(value) for value in allowed_types)
    item_limit = min(MAX_GEOMETRY_ITEMS, max(1, int(max_items)))
    point_limit = min(MAX_POINTS_PER_PRIMITIVE, max(1, int(max_points)))
    trusted_space = space if space in ("pixel_points", "normalized_points") else "unknown"
    width = height = None
    if (isinstance(frame_size, Sequence) and len(frame_size) == 2
            and all(isinstance(v, int) and not isinstance(v, bool) and v > 0
                    for v in frame_size)):
        width, height = int(frame_size[0]), int(frame_size[1])
    if trusted_space == "pixel_points" and (width is None or height is None):
        output_space = "unknown"
    else:
        output_space = trusted_space
    defaults = sanitize_style(default_style, strict=False)
    output: list[dict[str, Any]] = []
    total_points = 0
    for raw in values[:item_limit]:
        if not isinstance(raw, Mapping):
            continue
        kind = str(raw.get("type") or "").strip().lower()
        if kind not in allowed:
            continue
        try:
            points_value = _points(raw.get("points"), kind, max_points=point_limit)
        except (GeometryError, TypeError, ValueError):
            continue
        if total_points + len(points_value) > MAX_TOTAL_POINTS:
            break
        valid = True
        for x, y in points_value:
            if trusted_space == "normalized_points" and (x > 1 or y > 1):
                valid = False
                break
            if trusted_space == "pixel_points" and width is not None \
                    and (x > width or y > height):
                valid = False
                break
        if not valid:
            continue
        item: dict[str, Any] = {
            "type": kind, "points": points_value, "space": output_space,
        }
        raw_id = raw.get("id")
        if isinstance(raw_id, str) and _ID_RE.fullmatch(raw_id):
            item["id"] = raw_id
        raw_label = raw.get("label")
        if isinstance(raw_label, str) and len(raw_label) <= MAX_LABEL_CHARS:
            item["label"] = raw_label
        raw_score = raw.get("score")
        if (not isinstance(raw_score, bool) and isinstance(raw_score, numbers.Real)
                and math.isfinite(float(raw_score)) and 0 <= float(raw_score) <= 1):
            item["score"] = float(raw_score)
        merged = dict(defaults)
        merged.update(sanitize_style(raw.get("style"), strict=False))
        if merged:
            item["style"] = merged
        output.append(item)
        total_points += len(points_value)
    return output


class GeometryBuilder:
    """Small fluent accumulator accepted directly by :meth:`kit.app.App.emit`."""

    def __init__(self) -> None:
        self._items: list[dict[str, Any]] = []
        self._total_points = 0

    def add(self, item: Mapping[str, Any]) -> "GeometryBuilder":
        if len(self._items) >= MAX_GEOMETRY_ITEMS:
            raise GeometryError(f"geometry exceeds {MAX_GEOMETRY_ITEMS} items")
        if not isinstance(item, Mapping):
            raise GeometryError("geometry item must be an object")
        unknown = set(item) - {"type", "points", "id", "label", "score", "style"}
        if unknown:
            raise GeometryError(
                "unknown geometry field(s): " + ", ".join(sorted(unknown)))
        # Re-run through the strict builder to avoid storing a mutable/unchecked
        # caller dictionary.  The Hub will still perform independent sanitizing.
        value = primitive(
            item.get("type", ""), item.get("points"), style=item.get("style"),
            id=item.get("id"), label=item.get("label"), score=item.get("score"),
        )
        if self._total_points + len(value["points"]) > MAX_TOTAL_POINTS:
            raise GeometryError(
                f"geometry exceeds {MAX_TOTAL_POINTS} total points")
        self._items.append(value)
        self._total_points += len(value["points"])
        return self

    def primitive(self, kind: str, points: Sequence[Sequence[float]],
                  **kwargs: Any) -> "GeometryBuilder":
        return self.add(primitive(kind, points, **kwargs))

    def point(self, x: float, y: float, **kwargs: Any) -> "GeometryBuilder":
        return self.add(point(x, y, **kwargs))

    def line(self, a: Sequence[float], b: Sequence[float], **kwargs: Any) -> "GeometryBuilder":
        return self.add(line(a, b, **kwargs))

    def polyline(self, points: Sequence[Sequence[float]], **kwargs: Any) -> "GeometryBuilder":
        return self.add(polyline(points, **kwargs))

    def polygon(self, points: Sequence[Sequence[float]], **kwargs: Any) -> "GeometryBuilder":
        return self.add(polygon(points, **kwargs))

    def box(self, xyxy: Sequence[float], **kwargs: Any) -> "GeometryBuilder":
        return self.add(box(xyxy, **kwargs))

    def quad(self, points: Sequence[Sequence[float]], **kwargs: Any) -> "GeometryBuilder":
        return self.add(quad(points, **kwargs))

    def keypoints(self, points: Sequence[Sequence[float]], **kwargs: Any
                  ) -> "GeometryBuilder":
        return self.extend(keypoints(points, **kwargs))

    def pose(self, points: Sequence[Sequence[float]],
             skeleton: Iterable[Sequence[int]], **kwargs: Any
             ) -> "GeometryBuilder":
        return self.extend(pose(points, skeleton, **kwargs))

    def extend(self, items: Iterable[Mapping[str, Any]]) -> "GeometryBuilder":
        for item in items:
            self.add(item)
        return self

    def build(self) -> list[dict[str, Any]]:
        result = []
        for item in self._items:
            copied = dict(item)
            copied["points"] = [list(point) for point in item["points"]]
            if "style" in item:
                copied["style"] = dict(item["style"])
            result.append(copied)
        return result

    def __iter__(self):
        return iter(self.build())

    def __len__(self) -> int:
        return len(self._items)


__all__ = [
    "GeometryBuilder", "GeometryError", "MAX_GEOMETRY_ITEMS",
    "MAX_POINTS_PER_PRIMITIVE", "MAX_TOTAL_POINTS", "PRIMITIVE_TYPES",
    "STYLE_FIELDS", "box", "keypoints", "line", "point", "polygon",
    "polyline", "pose", "primitive", "quad", "sanitize_geometry",
    "sanitize_style",
]

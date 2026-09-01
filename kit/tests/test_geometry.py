from __future__ import annotations

import pytest

from kit.geometry import (
    GeometryBuilder,
    GeometryError,
    box,
    keypoints,
    line,
    point,
    polygon,
    pose,
    quad,
    sanitize_geometry,
)


def test_primitive_builders_share_one_bounded_wire_shape():
    values = GeometryBuilder() \
        .point(10, 20, id="origin", color="#00FF00", point_radius=4) \
        .line((0, 0), (10, 10), opacity=0.5) \
        .polygon(((0, 0), (10, 0), (10, 10)), fill=True,
                 fill_color="#11223380") \
        .build()

    assert [value["type"] for value in values] == ["point", "line", "polygon"]
    assert values[0] == {
        "type": "point", "points": [[10.0, 20.0]], "id": "origin",
        "style": {"color": "#00ff00", "point_radius": 4.0},
    }
    assert all("space" not in value for value in values)


def test_box_quad_keypoint_and_pose_compatibility_helpers():
    assert box((1, 2, 5, 7))["points"] == [
        [1.0, 2.0], [5.0, 2.0], [5.0, 7.0], [1.0, 7.0]]
    assert quad(((0, 0), (2, 0), (2, 1), (0, 1)))["type"] == "polygon"
    assert len(keypoints(((1, 2, 0.9), (3, 4, 0.1)), conf_min=0.5)) == 1

    primitives = pose(
        ((1, 2, 0.9), (3, 4, 0.8), (5, 6, 0.1)),
        ((0, 1), (1, 2)), conf_min=0.5,
        point_style={"point_radius": 3}, line_style={"line_width": 2})
    assert [value["type"] for value in primitives] == ["line", "point", "point"]

    built = GeometryBuilder().keypoints(((1, 2), (3, 4))).pose(
        ((1, 2), (3, 4)), ((0, 1),)).build()
    assert [value["type"] for value in built] == [
        "point", "point", "line", "point", "point"]


def test_pose_point_ids_keep_original_indices_when_middle_point_is_filtered():
    primitives = pose(
        ((1, 2, 0.9), (3, 4, 0.1), (5, 6, 0.8)),
        ((0, 1), (1, 2), (0, 2)), conf_min=0.5,
    )

    assert [value["id"] for value in primitives] == [
        "pose-edge-2", "pose-point-0", "pose-point-2"]


@pytest.mark.parametrize("callback", [
    lambda: point(float("nan"), 0),
    lambda: line((0, 0), (1, 1), color="red"),
    lambda: polygon(((0, 0), (1, 1))),
    lambda: point(0, 0, style={"filter": "url(javascript:bad)"}),
])
def test_builders_reject_non_finite_or_unbounded_input(callback):
    with pytest.raises(GeometryError):
        callback()


def test_builder_rejects_payload_authority_fields():
    with pytest.raises(GeometryError, match="unknown geometry field"):
        GeometryBuilder().add({
            "type": "point", "points": [[0, 0]],
            "space": "normalized_points",
        })


def test_hub_sanitizer_injects_space_bounds_coords_and_discards_unknown_style():
    values = sanitize_geometry([
        {"type": "point", "points": [[0.25, 0.75]], "space": "pixel_points",
         "style": {"color": "#abcdef", "line_width": 4, "shadow": "bad"}},
        {"type": "point", "points": [[1.1, 0.5]]},
        {"type": "polygon", "points": [[0, 0], [1, 0], [1, 1]]},
    ], space="normalized_points", allowed_types=("point",),
       default_style={"opacity": 0.75})

    assert values == [{
        "type": "point", "points": [[0.25, 0.75]],
        "space": "normalized_points",
        "style": {"opacity": 0.75, "color": "#abcdef", "line_width": 4.0},
    }]

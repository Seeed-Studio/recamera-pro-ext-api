from __future__ import annotations

import json

import pytest

from kit.ai import (
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
from kit.errors import ConfigurationError, InputValidationError


def test_coordinate_spaces_and_normalized_boundaries_are_explicit():
    full = Box(0, 0, 1, 1, CoordinateSpace.NORMALIZED)
    pixel = Box(0, 0, 640, 480, CoordinateSpace.PIXEL)
    model = Box(12, 16, 320, 320, "model")

    assert full.space is CoordinateSpace.NORMALIZED
    assert pixel.to_legacy() == [0.0, 0.0, 640.0, 480.0]
    assert model.to_dict()["space"] == "model"


@pytest.mark.parametrize(
    "args",
    [
        (-1, 0, 1, 1, "pixel"),
        (0, 0, 1.01, 1, "normalized"),
        (2, 0, 1, 1, "pixel"),
        (0, 2, 1, 1, "pixel"),
        (0, 0, float("nan"), 1, "pixel"),
        (0, 0, 1, float("inf"), "model"),
    ],
)
def test_box_rejects_invalid_coordinates(args):
    with pytest.raises(InputValidationError):
        Box(*args)


def test_unknown_coordinate_space_is_configuration_error():
    with pytest.raises(ConfigurationError):
        Box(0, 0, 1, 1, "camera-ish")


@pytest.mark.parametrize("score", [-0.001, 1.001, float("nan"), True, "0.5"])
def test_scores_are_finite_probabilities(score):
    with pytest.raises(InputValidationError):
        Detection(Box(0, 0, 10, 10), score=score)


@pytest.mark.parametrize("class_id", [-1, 1.5, True, "1"])
def test_class_ids_are_non_negative_integers(class_id):
    with pytest.raises(InputValidationError):
        Classification(score=0.5, class_id=class_id)


def test_object_results_reject_empty_boxes_but_box_can_represent_empty_roi():
    empty = Box(0, 0, 0, 0, CoordinateSpace.NORMALIZED)
    assert empty.is_empty
    with pytest.raises(InputValidationError):
        Track(track_id=1, box=empty, score=0.8)


def test_pose_requires_unique_ids_and_one_coordinate_space():
    with pytest.raises(InputValidationError):
        Pose((Keypoint(1, 2, id=0), Keypoint(3, 4, id=0)))

    with pytest.raises(InputValidationError):
        Pose(
            (
                Keypoint(0.1, 0.2, id=0, space="normalized"),
                Keypoint(10, 20, id=1, space="pixel"),
            )
        )


def test_valid_pose_keeps_ids_and_legacy_shape():
    pose = Pose(
        (
            Keypoint(10, 20, 0.9, 4, "pixel"),
            Keypoint(30, 40, 0.8, 9, "pixel"),
        ),
        score=0.95,
        box=Box(1, 2, 50, 60),
    )
    legacy = pose.to_legacy_dict()
    assert legacy["keypoint_ids"] == [4, 9]
    assert legacy["coordinate_space"] == "pixel"
    decoded = Pose.from_legacy_dict(legacy)
    assert decoded.keypoints == pose.keypoints


def test_segmentation_validates_shape_values_and_empty_abi_case():
    grid = Segmentation(mask=[[0, 1], [2, 255]], width=0, height=0)
    assert (grid.mask, grid.width, grid.height) == (b"\x00\x01\x02\xff", 2, 2)
    assert Segmentation(mask=b"", width=0, height=0).mask == b""

    with pytest.raises(InputValidationError):
        Segmentation(mask=b"\x00", width=2, height=2)
    with pytest.raises(InputValidationError):
        Segmentation(mask=[[0], [1, 2]], width=0, height=0)
    with pytest.raises(InputValidationError):
        Segmentation(mask=[[0, 1], [2, 3]], width=1, height=4)
    with pytest.raises(InputValidationError):
        Segmentation(mask=[[256]], width=0, height=0)


def test_legacy_detection_roundtrip_is_exact_and_keeps_unknown_fields():
    legacy = {
        "box": [1, 2, 30, 40],
        "score": 0.875,
        "cls": 7,
        "cls_name": "truck",
        "kind": "detection",
        "app_payload": {"lane": 2, "flags": ["moving"]},
    }
    result = Detection.from_legacy_dict(legacy)

    assert result.attributes["kind"] == "detection"
    assert result.attributes["app_payload"]["lane"] == 2
    assert result.to_legacy_dict() == legacy
    assert result.to_legacy_dict() is not legacy


def test_legacy_pose_track_classification_and_mask_roundtrip_exactly():
    values = [
        (
            Pose,
            {"box": [0, 0, 10, 20], "score": 0.8,
             "keypoints": [[1, 2], [3, 4, 0.6]], "state": "standing"},
        ),
        (
            Track,
            {"kind": "track", "track_id": 3, "box": [1, 2, 3, 4],
             "score": 1.0, "state": "engaged", "in_zone": True},
        ),
        (
            Classification,
            {"kind": "classification", "label": "cat", "score": 0.7,
             "class_id": 4},
        ),
        (
            Segmentation,
            {"mask": [[0, 1]], "mask_w": 2, "mask_h": 1, "score": 0.5,
             "label": "floor"},
        ),
    ]
    for model, legacy in values:
        assert model.from_legacy_dict(legacy).to_legacy_dict() == legacy


def test_batch_json_roundtrip_preserves_every_typed_result():
    results = (
        Detection(Box(1, 2, 30, 40), 0.9, 1, "person"),
        Classification(0.8, 2, "adult"),
        Pose((Keypoint(5, 6, 0.7, 0),), 0.75, box=Box(1, 2, 30, 40)),
        Track(12, Box(2, 3, 31, 41), 0.88, label="person"),
        Segmentation(b"\x00\x01\x01\x00", 2, 2, 0.6, label="person"),
    )
    batch = ResultBatch(
        results,
        pts_us=123456,
        source_id="test-app",
        frame_size=(640, 480),
        model_size=(320, 320),
        attributes={"sequence": 9},
    )

    payload = batch.to_json(sort_keys=True)
    assert json.loads(payload)["results"][-1]["mask_encoding"] == "base64"
    assert ResultBatch.from_json(payload) == batch


def test_batch_checks_declared_extents_and_duplicate_track_ids():
    with pytest.raises(InputValidationError):
        ResultBatch(
            (Detection(Box(0, 0, 641, 10), 0.5),),
            frame_size=(640, 480),
        )

    track = Track(1, Box(0, 0, 10, 10), 0.5)
    with pytest.raises(InputValidationError):
        ResultBatch((track, track), frame_size=(20, 20))


def test_batch_legacy_dispatch_and_lossless_payload_roundtrip():
    payload = {
        "app": "legacy-app",
        "frame": {"w": 100, "h": 80},
        "results": [
            {"box": [1, 2, 3, 4], "score": 0.9, "cls": 0},
            {"kind": "classification", "label": "ok", "score": 1.0},
            {"box": [1, 2, 3, 4], "score": 0.8,
             "keypoints": [[1, 2, 0.7]]},
            {"track_id": 5, "box": [1, 2, 3, 4], "score": 0.6},
            {"mask_bytes": b"\x01", "mask_w": 1, "mask_h": 1},
        ],
        "events": [{"kind": "line_cross", "track_id": 5}],
    }
    batch = ResultBatch.from_legacy_dict(payload)

    assert [type(result) for result in batch.results] == [
        Detection,
        Classification,
        Pose,
        Track,
        Segmentation,
    ]
    assert batch.frame_size == (100, 80)
    assert batch.to_legacy_dict() == payload


def test_invalid_canonical_schema_and_json_use_configuration_error():
    with pytest.raises(ConfigurationError):
        ResultBatch.from_dict({"schema_version": 99, "results": []})
    with pytest.raises(ConfigurationError):
        ResultBatch.from_json("not json")
    with pytest.raises(ConfigurationError):
        ResultBatch.from_dict(
            {"schema_version": 1, "results": [{"type": "mystery"}]}
        )

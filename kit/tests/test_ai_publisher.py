from __future__ import annotations

import logging

import pytest

from kit.ai import (
    Box,
    Classification,
    CoordinateSpace,
    Detection,
    Keypoint,
    Pose,
    PublishReport,
    ResultBatch,
    ResultBatchPublisher,
    Segmentation,
    Track,
    publish_result_batch,
    to_legacy_payload,
)
from kit.errors import AdapterError, ConfigurationError, TransportError


class FakeSink:
    def __init__(self):
        self.frame_sizes = []
        self.calls = []

    def set_frame_size(self, width, height):
        self.frame_sizes.append((width, height))

    def emit(self, payload, pts):
        self.calls.append((payload, pts))


class FailingSink(FakeSink):
    def __init__(self, error, *, fail_set_frame_size=False):
        super().__init__()
        self.error = error
        self.fail_set_frame_size = fail_set_frame_size

    def set_frame_size(self, width, height):
        if self.fail_set_frame_size:
            raise self.error
        super().set_frame_size(width, height)

    def emit(self, payload, pts):
        raise self.error


def test_pixel_detection_publish_calls_existing_sink_contract_and_reports():
    sink = FakeSink()
    batch = ResultBatch(
        (
            Detection(
                Box(10, 20, 110, 220, CoordinateSpace.PIXEL),
                score=0.9,
                class_id=2,
                label="car",
                attributes={"app_tag": "moving"},
            ),
        ),
        pts_us=1_250_000,
        source_id="traffic",
        frame_size=(640, 480),
        attributes={"request_id": "abc"},
    )

    report = ResultBatchPublisher(sink).publish(batch)
    payload, pts = sink.calls[0]

    assert isinstance(report, PublishReport)
    assert sink.frame_sizes == [(640, 480)]
    assert pts == 1.25
    assert payload["results"] == [
        {
            "app_tag": "moving",
            "box": [10.0, 20.0, 110.0, 220.0],
            "score": 0.9,
            "cls": 2,
            "cls_name": "car",
            "coordinate_space": "pixel",
        }
    ]
    assert payload["request_id"] == "abc"
    assert payload["source_id"] == payload["app"] == "traffic"
    assert payload["frame"] == {"width": 640, "height": 480}
    assert report.input_results == report.payload_results == 1
    assert report.payload_events == 0
    assert report.converted_pairs == 0
    assert report.locally_accepted is True
    assert report.server_acknowledged is False


def test_normalized_boxes_points_and_tracks_become_original_pixels():
    space = CoordinateSpace.NORMALIZED
    batch = ResultBatch(
        (
            Detection(Box(0.1, 0.2, 0.5, 0.75, space), 0.8, label="person"),
            Pose(
                (
                    Keypoint(0.25, 0.5, 0.9, 0, space),
                    Keypoint(1.0, 0.0, 0.7, 1, space),
                ),
                score=0.85,
                box=Box(0.1, 0.2, 0.5, 0.75, space),
            ),
            Track(
                7,
                Box(0.2, 0.25, 0.4, 0.5, space),
                0.75,
                label="person",
                attributes={"state": "engaged"},
            ),
        ),
        frame_size=(200, 100),
    )
    sink = FakeSink()

    report = publish_result_batch(sink, batch)
    payload = sink.calls[0][0]

    assert payload["results"][0]["box"] == [20.0, 20.0, 100.0, 75.0]
    assert payload["results"][1]["keypoints"] == [
        [50.0, 50.0, 0.9],
        [200.0, 0.0, 0.7],
    ]
    assert payload["results"][1]["box"] == [20.0, 20.0, 100.0, 75.0]
    assert payload["events"] == [
        {
            "state": "engaged",
            "kind": "track",
            "track_id": 7,
            "box": [40.0, 25.0, 80.0, 50.0],
            "score": 0.75,
            "cls": 0,
            "label": "person",
            "coordinate_space": "pixel",
        }
    ]
    # detection box 2 + pose points 2 + pose box 2 + track box 2
    assert report.normalized_pairs == 8
    assert report.track_events == 1
    assert report.payload_results == 2


def test_bridge_matches_official_osd_sink_pixel_boundary():
    from kit.adapters.official import OfficialResultSink

    class FakeNativeSink:
        def __init__(self):
            self.detections = []

        def send_detections(self, pts_us, items):
            self.detections.append((pts_us, items))

    native = FakeNativeSink()
    osd = OfficialResultSink(verbose=False)
    osd._sink = native
    batch = ResultBatch(
        (Detection(Box(0.1, 0.2, 0.5, 0.75, "normalized"), 0.8,
                   class_id=2, label="person"),),
        pts_us=250_000,
        frame_size=(200, 100),
    )

    ResultBatchPublisher(osd).publish(batch)

    # The bridge first emits original-frame pixels; OfficialResultSink then
    # performs the one intended pixel->ABI-normalized conversion.
    assert native.detections == [
        (250_000, [(0.1, 0.2, 0.5, 0.75, 0.8, "person", 2)])
    ]


def test_official_native_send_failure_cannot_be_reported_as_success():
    from kit.adapters.official import OfficialResultSink

    backend = ConnectionResetError("result socket closed")

    class FailingNativeSink:
        def send_detections(self, _pts_us, _items):
            raise backend

    osd = OfficialResultSink(verbose=False)
    osd._sink = FailingNativeSink()
    batch = ResultBatch(
        (Detection(Box(1, 2, 3, 4), 0.8, label="person"),),
        frame_size=(10, 10),
    )

    # Historical app loops keep their best-effort behavior.
    osd.set_frame_size(10, 10)
    osd.emit(to_legacy_payload(batch), 0.0)
    assert osd.stats()["send_fail"] == 1

    # The typed publisher uses emit_checked and must never return success.
    with pytest.raises(TransportError) as caught:
        ResultBatchPublisher(osd).publish(batch)
    assert caught.value.__cause__ is backend
    assert osd.stats()["send_fail"] == 2


def test_publisher_prefers_checked_sink_methods():
    class CheckedSink(FakeSink):
        def set_frame_size(self, _width, _height):
            raise AssertionError("legacy setter must not be used")

        def set_frame_size_checked(self, width, height):
            self.frame_sizes.append((width, height))

        def emit(self, _payload, _pts):
            raise AssertionError("legacy emit must not be used")

        def emit_checked(self, payload, pts):
            self.calls.append((payload, pts))

    sink = CheckedSink()
    ResultBatchPublisher(sink).publish(ResultBatch((), frame_size=(2, 2)))
    assert sink.frame_sizes == [(2, 2)]
    assert len(sink.calls) == 1


def test_normalized_coordinates_require_batch_frame_size_before_sink_call():
    sink = FakeSink()
    batch = ResultBatch(
        (Detection(Box(0.1, 0.2, 0.3, 0.4, "normalized"), 0.5),)
    )

    with pytest.raises(ConfigurationError) as caught:
        ResultBatchPublisher(sink).publish(batch)

    assert caught.value.operation == "ai.result.prepare"
    assert caught.value.details["field"] == "frame_size"
    assert sink.calls == []


def test_model_coordinates_are_never_guessed_from_declared_sizes():
    sink = FakeSink()
    batch = ResultBatch(
        (Detection(Box(1, 2, 10, 20, "model"), 0.5),),
        frame_size=(640, 480),
        model_size=(320, 320),
    )

    with pytest.raises(ConfigurationError) as caught:
        ResultBatchPublisher(sink).publish(batch)

    assert caught.value.details["coordinate_space"] == "model"
    assert sink.frame_sizes == []
    assert sink.calls == []


def test_explicit_model_mapping_is_applied_and_validated():
    sink = FakeSink()
    batch = ResultBatch(
        (Detection(Box(1, 2, 10, 20, "model"), 0.5),),
        frame_size=(200, 300),
        model_size=(20, 20),
    )
    publisher = ResultBatchPublisher(
        sink,
        model_to_pixel=lambda x, y: (x * 10, y * 10),
    )

    report = publisher.publish(batch)

    assert sink.calls[0][0]["results"][0]["box"] == [10.0, 20.0, 100.0, 200.0]
    assert report.model_pairs == 2

    with pytest.raises(ConfigurationError):
        to_legacy_payload(batch, model_to_pixel=lambda _x, _y: (201, 1))


def test_classification_segmentation_and_metadata_keep_legacy_shape():
    sink = FakeSink()
    batch = ResultBatch(
        (
            Classification(
                0.88,
                class_id=3,
                label="adult",
                attributes={"model": "fairface"},
            ),
            Segmentation(
                b"\x00\x01\x02\x03",
                2,
                2,
                score=0.7,
                class_id=4,
                label="floor",
                box=Box(0.25, 0.25, 0.75, 0.75, "normalized"),
                attributes={"palette": "gray"},
            ),
        ),
        frame_size=(80, 40),
        attributes={
            "tenant": "lab",
            "events": [{"kind": "alarm", "severity": 2}],
        },
    )

    ResultBatchPublisher(sink).publish(batch)
    payload = sink.calls[0][0]

    assert payload["tenant"] == "lab"
    assert payload["events"] == [{"kind": "alarm", "severity": 2}]
    assert payload["results"][0] == {
        "model": "fairface",
        "kind": "classification",
        "score": 0.88,
        "cls": 3,
        "label": "adult",
    }
    assert payload["results"][1] == {
        "palette": "gray",
        "kind": "segmentation",
        "mask_bytes": b"\x00\x01\x02\x03",
        "mask_w": 2,
        "mask_h": 2,
        "score": 0.7,
        "cls": 4,
        "cls_name": "floor",
        "box": [20.0, 10.0, 60.0, 30.0],
        "coordinate_space": "pixel",
    }


def test_unrepresentable_legacy_osd_cases_fail_instead_of_misrouting():
    roi_classification = ResultBatch(
        (Classification(0.9, label="generic", box=Box(1, 2, 3, 4)),),
        frame_size=(10, 10),
    )
    empty_segmentation = ResultBatch(
        (Segmentation(b"", 0, 0, label="empty"),),
        frame_size=(10, 10),
    )

    with pytest.raises(ConfigurationError) as classification_error:
        to_legacy_payload(roi_classification)
    assert classification_error.value.details["required_route"] == "face_attributes"

    with pytest.raises(ConfigurationError) as segmentation_error:
        to_legacy_payload(empty_segmentation)
    assert segmentation_error.value.details["field"] == "segmentation.mask"


def test_legacy_batch_metadata_and_business_events_survive_republication():
    original = {
        "app": "old-app",
        "pts_us": 9,
        "frame": {"w": 20, "h": 10, "rotation": 0},
        "results": [
            {"box": [1, 2, 3, 4], "score": 0.9, "cls": 1, "opaque": "keep"}
        ],
        "events": [{"kind": "line_cross", "track_id": 8}],
        "correlation_id": "xyz",
    }
    batch = ResultBatch.from_legacy_dict(original)

    payload = to_legacy_payload(batch)

    assert payload["app"] == payload["source_id"] == "old-app"
    assert payload["correlation_id"] == "xyz"
    assert payload["events"] == original["events"]
    assert payload["frame"] == {
        "w": 20,
        "h": 10,
        "rotation": 0,
        "width": 20,
        "height": 10,
    }
    assert payload["results"][0]["opaque"] == "keep"


def test_sparse_keypoint_ids_are_rejected_instead_of_silently_renumbered():
    batch = ResultBatch(
        (Pose((Keypoint(1, 2, id=4), Keypoint(3, 4, id=9))),),
        frame_size=(20, 20),
    )

    with pytest.raises(ConfigurationError) as caught:
        to_legacy_payload(batch)

    assert caught.value.details["keypoint_ids"] == [4, 9]


def test_emit_failure_is_adapter_error_with_context_cause_and_structured_log(caplog):
    backend = RuntimeError("serializer exploded")
    sink = FailingSink(backend)
    batch = ResultBatch((), pts_us=42, source_id="app-a", frame_size=(4, 3))

    with caplog.at_level(logging.ERROR, logger="recamera.ai.publisher"):
        with pytest.raises(AdapterError) as caught:
            ResultBatchPublisher(sink).publish(batch)

    assert type(caught.value) is AdapterError
    assert caught.value.operation == "ai.result.publish"
    assert caught.value.details == {
        "sink_type": "FailingSink",
        "source_id": "app-a",
        "pts_us": 42,
        "input_results": 0,
        "frame_width": 4,
        "frame_height": 3,
    }
    assert caught.value.__cause__ is backend
    record = next(record for record in caplog.records
                  if getattr(record, "event", None) == "ai_result_publish_failed")
    assert record.operation == "ai.result.publish"
    assert record.source_id == "app-a"
    assert record.sink_type == "FailingSink"


def test_transport_emit_failure_is_retryable_transport_error():
    backend = ConnectionResetError("peer reset")
    batch = ResultBatch((), source_id="app-b")

    with pytest.raises(TransportError) as caught:
        ResultBatchPublisher(FailingSink(backend)).publish(batch)

    assert caught.value.retryable is True
    assert caught.value.__cause__ is backend


def test_frame_size_failure_is_not_swallowed_or_reported_as_success():
    backend = RuntimeError("bad dimensions")
    batch = ResultBatch((), frame_size=(10, 10))

    with pytest.raises(AdapterError) as caught:
        ResultBatchPublisher(
            FailingSink(backend, fail_set_frame_size=True)
        ).publish(batch)

    assert caught.value.operation == "ai.result.set_frame_size"
    assert caught.value.__cause__ is backend


def test_publisher_requires_emit_and_model_mapping_must_be_callable():
    with pytest.raises(ConfigurationError):
        ResultBatchPublisher(object())
    with pytest.raises(ConfigurationError):
        ResultBatchPublisher(FakeSink(), model_to_pixel="scale")

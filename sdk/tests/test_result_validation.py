"""ResultSink input validation tests backed by a fake native result API."""

from __future__ import annotations

import ctypes

import pytest


class FakeResultLib:
    def __init__(self):
        self.calls = []
        self.close_calls = 0

    @staticmethod
    def _value(value):
        return int(getattr(value, "value", value))

    def rc_ext_result_open(self, _source_id, err_ptr):
        ctypes.cast(err_ptr, ctypes.POINTER(ctypes.c_int))[0] = 0
        return 0xCAFE

    def _record(self, name, pts_us, _items, count):
        self.calls.append((name, self._value(pts_us), self._value(count)))
        return 0

    def rc_ext_result_send_detections(self, _handle, pts_us, items, count):
        return self._record("detections", pts_us, items, count)

    def rc_ext_result_send_classification(self, _handle, pts_us, items, count):
        return self._record("classification", pts_us, items, count)

    def rc_ext_result_send_segmentation(self, _handle, pts_us, items, count):
        return self._record("segmentation", pts_us, items, count)

    def rc_ext_result_send_tracking(self, _handle, pts_us, items, count):
        return self._record("tracking", pts_us, items, count)

    def rc_ext_result_send_keypoints(self, _handle, pts_us, items, count):
        return self._record("keypoints", pts_us, items, count)

    def rc_ext_result_close(self, _handle):
        self.close_calls += 1


def _sink(sdk_module, monkeypatch):
    fake = FakeResultLib()
    monkeypatch.setattr(sdk_module, "_load", lambda _path=None: fake)
    return sdk_module.ResultSink("validation-test"), fake


@pytest.mark.parametrize(
    ("mask_w", "mask_h"),
    [
        (True, 1),
        (1, False),
        (-1, 1),
        (1, -1),
        (1.0, 1),
        (1, "1"),
        (2**31, 1),
    ],
)
def test_segmentation_rejects_invalid_dimensions_before_native_call(
    sdk_module, monkeypatch, mask_w, mask_h
):
    sink, fake = _sink(sdk_module, monkeypatch)
    item = (0.1, 0.1, 0.9, 0.9, 0.8, 0, "person", b"\x01", mask_w, mask_h)

    with pytest.raises(sdk_module.FormatError) as raised:
        sink.send_segmentation(0, [item])

    assert raised.value.operation == "send_segmentation"
    assert fake.calls == []
    sink.close()


@pytest.mark.parametrize(
    ("mask", "mask_w", "mask_h", "message"),
    [
        (b"\x00\x01\x02", 2, 2, "must equal"),
        (None, 1, 1, "must equal"),
        (b"", 0, 5, "both be zero"),
        (b"\x00", 0, 0, "must equal"),
    ],
)
def test_segmentation_rejects_mask_shape_mismatch_before_native_call(
    sdk_module, monkeypatch, mask, mask_w, mask_h, message
):
    sink, fake = _sink(sdk_module, monkeypatch)
    item = (0.1, 0.1, 0.9, 0.9, 0.8, 0, "person", mask, mask_w, mask_h)

    with pytest.raises(sdk_module.FormatError, match=message):
        sink.send_segmentation(0, [item])

    assert fake.calls == []
    sink.close()


def test_segmentation_mask_budget_is_checked_before_copy_or_native_call(
    sdk_module, monkeypatch
):
    sink, fake = _sink(sdk_module, monkeypatch)
    sink.MAX_MESSAGE_BYTES = 160
    mask = memoryview(bytes(range(64)))
    item = (0.1, 0.1, 0.9, 0.9, 0.8, 0, "person", mask, 8, 8)

    with pytest.raises(sdk_module.ResultTooLarge):
        sink.send_segmentation(0, [item])

    assert fake.calls == []
    assert sink.stats()["oversize_rejected"] == 1
    sink.close()


@pytest.mark.parametrize(
    "invoke",
    [
        pytest.param(lambda sink: sink.send_detections(-1, []), id="negative-pts"),
        pytest.param(lambda sink: sink.send_detections(True, []), id="bool-pts"),
        pytest.param(lambda sink: sink.send_detections(1.5, []), id="float-pts"),
        pytest.param(
            lambda sink: sink.send_detections(2**64, []), id="overflow-pts"
        ),
        pytest.param(
            lambda sink: sink.send_detections(
                0, [(float("nan"), 0.1, 0.9, 0.9, 0.8, "person")]
            ),
            id="nonfinite-coordinate",
        ),
        pytest.param(
            lambda sink: sink.send_detections(
                0, [(0.1, 0.1, 1.01, 0.9, 0.8, "person")]
            ),
            id="coordinate-outside-unit-range",
        ),
        pytest.param(
            lambda sink: sink.send_detections(
                0, [(0.8, 0.1, 0.2, 0.9, 0.8, "person")]
            ),
            id="reversed-box",
        ),
        pytest.param(
            lambda sink: sink.send_detections(
                0, [(0.1, 0.1, 0.9, 0.9, float("inf"), "person")]
            ),
            id="nonfinite-score",
        ),
        pytest.param(
            lambda sink: sink.send_detections(0, [(0.1, 0.1, 0.9)]),
            id="detection-shape",
        ),
        pytest.param(
            lambda sink: sink.send_classification(0, [(0.8, 1, "cat", (0, 1))]),
            id="classification-box-shape",
        ),
        pytest.param(
            lambda sink: sink.send_tracking(
                0, [(0.1, 0.1, 0.9, 0.9, 0.8, 1, "person")]
            ),
            id="tracking-shape",
        ),
        pytest.param(
            lambda sink: sink.send_keypoints(
                0, [{"points": [(1.1, 0.2, 0.8, 0)]}]
            ),
            id="keypoint-coordinate",
        ),
        pytest.param(
            lambda sink: sink.send_keypoints(0, [{"points": [(0.1, 0.2)]}]),
            id="keypoint-shape",
        ),
        pytest.param(
            lambda sink: sink.send_keypoints(0, [(0.1, 0.2, 0.8)]),
            id="keypoint-instance-type",
        ),
    ],
)
def test_other_result_inputs_fail_with_typed_format_error_before_native(
    sdk_module, monkeypatch, invoke
):
    sink, fake = _sink(sdk_module, monkeypatch)

    with pytest.raises(sdk_module.FormatError) as raised:
        invoke(sink)

    assert raised.value.operation is not None
    assert fake.calls == []
    assert sink.stats() == {
        "sent": 0,
        "oversize_rejected": 0,
        "send_error": 0,
    }
    sink.close()


def test_all_documented_result_shapes_still_reach_native(
    sdk_module, monkeypatch
):
    sink, fake = _sink(sdk_module, monkeypatch)

    sink.send_detections(
        2**64 - 1,
        [(0.1, 0.2, 0.8, 0.9, 0.75, "person"),
         (0.2, 0.3, 0.7, 0.8, 1.0, b"car", -1)],
    )
    sink.send_classification(
        0,
        [(0.7, 2, "cat"), (0.6, 3, "dog", (0.1, 0.1, 0.9, 0.9))],
    )
    sink.send_segmentation(0, [(0.1, 0.1, 0.9, 0.9, 0.8, 0, "empty")])
    sink.send_segmentation(
        0,
        [(0.1, 0.1, 0.9, 0.9, 0.8, 0, "mask", bytearray(range(4)), 2, 2)],
    )
    assert sink._masks == [bytes(range(4))]
    sink.send_tracking(
        0, [(0.1, 0.1, 0.9, 0.9, 0.8, 0, "person", -1)]
    )
    sink.send_keypoints(
        0,
        [{
            "points": [(0.2, 0.3, 0.9), (0.4, 0.5, 1.0, 7)],
            "box": (0.1, 0.1, 0.9, 0.9),
            "score": 0.8,
            "class_id": 0,
            "label": "person",
        }],
    )

    assert fake.calls == [
        ("detections", 2**64 - 1, 2),
        ("classification", 0, 2),
        ("segmentation", 0, 1),
        ("segmentation", 0, 1),
        ("tracking", 0, 1),
        ("keypoints", 0, 1),
    ]
    assert sink.stats()["sent"] == 6
    sink.close()

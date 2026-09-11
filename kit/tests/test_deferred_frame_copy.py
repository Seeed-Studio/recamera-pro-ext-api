"""Copies of hw frames must retain camera RGB and its distinct model image."""
import numpy as np
import pytest

from kit.adapters._model_frame import DeferredModelImage
from kit.app import App
from kit.buffer import ImageBuffer
from kit.errors import BufferReleasedError
from kit.frame import Frame
from kit.runtime.preprocess import LetterboxInfo


def make_frame():
    rgb = np.arange(12 * 24 * 3, dtype=np.uint8).reshape(12, 24, 3)
    model_rgb = np.full((8, 8, 3), 114, dtype=np.uint8)
    model_rgb[2:6] = (13, 31, 51)
    calls = []

    def materialize():
        calls.append("model")
        return model_rgb.copy()

    image = DeferredModelImage(materialize, lambda _target: True)
    frame = Frame(data=rgb, pts_us=123,
                  model_info=LetterboxInfo(scale=1 / 3, pad_w=0, pad_h=2,
                                           orig_w=24, orig_h=12))
    frame._deferred_model_image = image
    return frame, image, calls, model_rgb


def test_copy_retains_distinct_model_pixels_and_transform_after_lease_ends():
    frame, image, calls, expected_model = make_frame()
    expected_rgb = frame.data.copy()
    assert calls == []  # reading camera pixels must not materialize detector input
    copied = frame.copy()
    assert calls == ["model"]
    assert copied.owned and copied.roi_cropper is None
    assert not hasattr(copied, "_deferred_model_image")
    image.expire()
    frame.data[:] = 0
    frame.release()

    np.testing.assert_array_equal(copied.data, expected_rgb)
    value = App().pre(copied)
    np.testing.assert_array_equal(value.data, expected_model)
    assert value.info is frame.model_info
    assert value.data.shape == (8, 8, 3)
    assert copied.data.shape == (12, 24, 3)


def test_copy_preserves_edited_model_data_without_aliasing_either_image():
    frame, image, calls, _ = make_frame()
    value = App().pre(frame)
    value.data[0, 0] = (7, 8, 9)
    copied = frame.copy()
    assert calls == ["model"]
    image.map()[:] = 0
    frame.data[:] = 0
    np.testing.assert_array_equal(App().pre(copied).data[0, 0], [7, 8, 9])
    assert np.any(copied.data)


@pytest.mark.parametrize("materialized", [False, True])
def test_release_invalidates_model_even_when_camera_rgb_has_its_own_buffer(materialized):
    frame, image, _, _ = make_frame()
    value = App().pre(frame)
    if materialized:
        _ = value.data
    frame.release()
    frame.release()
    assert frame.released and image.released
    with pytest.raises(BufferReleasedError):
        _ = value.data
    with pytest.raises(BufferReleasedError):
        image.prepare({})
    with pytest.raises(BufferReleasedError):
        frame.copy()


def test_copy_cannot_recover_unmaterialized_model_after_lease_expiration():
    frame, image, calls, _ = make_frame()
    image.expire()
    assert frame.data.shape == (12, 24, 3)  # owned source pixels still survive
    with pytest.raises(BufferReleasedError):
        frame.copy()
    assert calls == []


def test_direct_copy_remains_one_editable_model_image():
    _, image, calls, expected_model = make_frame()
    frame = Frame(buffer=ImageBuffer.from_backend(
        image, width=8, height=8, format="RGB", planes=[(0, 24, 8)], memory="backend"),
        w=24, h=12, model_info=LetterboxInfo(
            scale=1 / 3, pad_w=0, pad_h=2, orig_w=24, orig_h=12))
    frame._deferred_model_image = image
    copied = frame.copy()
    assert calls == ["model"]
    image.expire()
    frame.release()
    np.testing.assert_array_equal(copied.data, expected_model)
    copied.data[0, 0] = (17, 18, 19)
    value = App().pre(copied)
    np.testing.assert_array_equal(value.data[0, 0], [17, 18, 19])

from __future__ import annotations

import numpy as np
import pytest

import kit
from kit.adapters import Frame as AdapterFrame
from kit.adapters.frame_source import Frame as SourceFrame
from kit.errors import BufferReleasedError, ConfigurationError
from kit.frame import Frame


def test_all_public_frame_imports_are_the_same_class():
    assert kit.Frame is Frame
    assert AdapterFrame is Frame
    assert SourceFrame is Frame


def test_legacy_positional_constructor_and_fields_are_preserved():
    pixels = np.zeros((10, 20, 3), dtype=np.uint8)
    marker = object()
    frame = Frame(pixels, 1920, 1080, "RGB", 1.25,
                  marker, pixels, marker)
    assert frame.data is pixels
    assert (frame.w, frame.h, frame.fmt, frame.pts) == (1920, 1080, "RGB", 1.25)
    assert frame.pts_us == 1_250_000
    assert frame.model_info is marker
    assert frame.model_data is pixels
    assert frame.roi_cropper is marker


def test_copy_outlives_release_and_drops_borrowed_cropper():
    pixels = np.arange(18, dtype=np.uint8).reshape(2, 3, 3)
    frame = Frame(pixels, 3, 2, "RGB", pts_us=123, roi_cropper=object())
    copied = frame.copy()
    frame.release()
    with pytest.raises(BufferReleasedError):
        _ = frame.data
    assert copied.data.tolist() == pixels.tolist()
    assert copied.pts_us == 123
    assert copied.roi_cropper is None


def test_invalid_or_conflicting_frame_metadata_is_typed():
    pixels = np.zeros((2, 3, 3), dtype=np.uint8)
    with pytest.raises(ConfigurationError):
        Frame(pixels, -1, 2, "RGB", 1.0)
    with pytest.raises(ConfigurationError):
        Frame(pixels, 3, 2, "RGB", float("nan"))
    with pytest.raises(ConfigurationError) as caught:
        Frame(pixels, 3, 2, "RGB", 1.0, pts_us=999_999)
    assert caught.value.operation == "frame.create"

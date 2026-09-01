from __future__ import annotations

import threading

import numpy as np
import pytest

from kit.buffer import ImageBuffer, MemoryKind, Ownership, PixelFormat, PlaneLayout
from kit.errors import AdapterError, BufferReleasedError, ConfigurationError


class FakeBackend:
    def __init__(self, array):
        self.array = array
        self.maps = 0
        self.releases = 0
        self.released = False

    def map(self):
        self.maps += 1
        return self.array

    def release(self):
        self.releases += 1
        self.released = True


def test_cpu_buffer_copy_is_independent_and_release_is_checked():
    original = np.arange(18, dtype=np.uint8).reshape(2, 3, 3)
    buf = ImageBuffer.from_numpy(original, format="RGB")
    copied = buf.copy()
    assert buf.memory is MemoryKind.CPU and buf.ownership is Ownership.OWNED
    assert copied.numpy().tolist() == original.tolist()

    buf.release()
    buf.release()
    with pytest.raises(BufferReleasedError):
        buf.numpy()
    assert copied.numpy()[0, 0, 0] == 0


def test_borrowed_backend_maps_once_and_releases_once():
    backend = FakeBackend(np.zeros((4, 6), dtype=np.uint8))
    buf = ImageBuffer.from_backend(
        backend,
        width=6,
        height=4,
        format=PixelFormat.NV12,
        planes=(PlaneLayout(0, 8, 4), PlaneLayout(32, 8, 2)),
    )
    assert buf.numpy().shape == (4, 6)
    assert buf.numpy().shape == (4, 6)
    assert backend.maps == 1
    assert tuple(buf.planes[0]) == (0, 8, 4)
    buf.release()
    buf.release()
    assert backend.releases == 1
    with pytest.raises(BufferReleasedError):
        buf.numpy()


def test_borrowed_flat_mapping_copy_preserves_geometry_and_planes():
    raw = np.arange(48, dtype=np.uint8)
    backend = FakeBackend(raw)
    planes = (PlaneLayout(0, 8, 4), PlaneLayout(32, 8, 2))
    buf = ImageBuffer.from_backend(
        backend,
        width=6,
        height=4,
        format=PixelFormat.NV12,
        planes=planes,
    )

    copied = buf.copy()
    buf.release()

    assert copied.owned is True
    assert copied.memory is MemoryKind.CPU
    assert (copied.width, copied.height) == (6, 4)
    assert copied.planes == planes
    assert copied.numpy().shape == (48,)
    assert copied.numpy().tolist() == raw.tolist()
    assert backend.releases == 1


def test_borrowed_layout_is_never_inferred():
    backend = FakeBackend(np.zeros((4, 6), dtype=np.uint8))
    with pytest.raises(ConfigurationError):
        ImageBuffer.from_backend(
            backend,
            width=6,
            height=4,
            format="NV12",
            planes=(),
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"memory": "somewhere"},
        {"ownership": "shared"},
    ],
)
def test_invalid_buffer_enums_are_typed(kwargs):
    base = dict(
        width=2,
        height=2,
        format="RGB",
        memory=MemoryKind.CPU,
        ownership=Ownership.OWNED,
        array=np.zeros((2, 2, 3), dtype=np.uint8),
    )
    base.update(kwargs)
    with pytest.raises(ConfigurationError) as caught:
        ImageBuffer(**base)
    assert caught.value.operation == "buffer.create"


def test_invalid_plane_layout_is_typed():
    with pytest.raises(ConfigurationError):
        PlaneLayout(-1, 8, 4)


def test_external_backend_invalidation_rejects_cached_view():
    backend = FakeBackend(np.zeros((4, 6), dtype=np.uint8))
    buf = ImageBuffer.from_backend(
        backend,
        width=6,
        height=4,
        format="NV12",
        planes=(PlaneLayout(0, 8, 4), PlaneLayout(32, 8, 2)),
    )
    assert buf.numpy().shape == (4, 6)  # populate the local cache
    backend.released = True
    assert buf.released is True
    with pytest.raises(BufferReleasedError):
        buf.numpy()


def test_release_attempts_callback_and_backend_and_reports_both():
    class FailingBackend(FakeBackend):
        def release(self):
            super().release()
            raise RuntimeError("backend failed")

    calls = []

    def callback():
        calls.append("callback")
        raise ValueError("callback failed")

    backend = FailingBackend(np.zeros((4, 6), dtype=np.uint8))
    buf = ImageBuffer.from_backend(
        backend,
        width=6,
        height=4,
        format="NV12",
        planes=(PlaneLayout(0, 8, 4), PlaneLayout(32, 8, 2)),
        release_callback=callback,
    )
    with pytest.raises(AdapterError) as caught:
        buf.release()
    assert calls == ["callback"]
    assert backend.releases == 1
    assert [item["stage"] for item in caught.value.details["failures"]] == [
        "callback", "backend",
    ]


def test_backend_control_flow_release_error_is_not_hidden_by_callback_error():
    class InterruptedBackend(FakeBackend):
        def release(self):
            super().release()
            raise SystemExit(19)

    backend = InterruptedBackend(np.zeros((4, 6), dtype=np.uint8))
    buf = ImageBuffer.from_backend(
        backend,
        width=6,
        height=4,
        format="NV12",
        planes=(PlaneLayout(0, 8, 4), PlaneLayout(32, 8, 2)),
        release_callback=lambda: (_ for _ in ()).throw(RuntimeError("first")),
    )
    with pytest.raises(SystemExit) as caught:
        buf.release()
    assert caught.value.code == 19


def test_release_waits_for_first_backend_map_to_finish():
    events = []
    map_started = threading.Event()
    allow_map = threading.Event()

    class BlockingBackend(FakeBackend):
        def map(self):
            events.append("map-start")
            map_started.set()
            assert allow_map.wait(2.0)
            events.append("map-end")
            return super().map()

        def release(self):
            events.append("release")
            super().release()

    backend = BlockingBackend(np.zeros((4, 6), dtype=np.uint8))
    buf = ImageBuffer.from_backend(
        backend,
        width=6,
        height=4,
        format="NV12",
        planes=(PlaneLayout(0, 8, 4), PlaneLayout(32, 8, 2)),
    )
    mapped = []
    reader = threading.Thread(target=lambda: mapped.append(buf.numpy()))
    releaser = threading.Thread(target=buf.release)
    reader.start()
    assert map_started.wait(1.0)
    releaser.start()
    assert "release" not in events
    allow_map.set()
    reader.join(2.0)
    releaser.join(2.0)
    assert events == ["map-start", "map-end", "release"]
    assert len(mapped) == 1
    with pytest.raises(BufferReleasedError):
        buf.numpy()

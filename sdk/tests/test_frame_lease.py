"""FrameLease and BorrowedBuffer tests backed by a fake C ABI surface."""

from __future__ import annotations

import ctypes
import logging

import pytest


class FakeFrameLib:
    """Small stateful implementation of the ``rc_ext_frame_*`` C functions."""

    def __init__(self, sdk, events=(), *, open_error=0, map_fails=False):
        self.sdk = sdk
        self.events = list(events)
        self.open_error = int(open_error)
        self.map_fails = bool(map_fails)
        self.next_timeouts = []
        self.release_seqs = []
        self.map_calls = 0
        self.close_calls = 0
        self._backings = {}

    @staticmethod
    def _write(ptr, ctype, value):
        ctypes.cast(ptr, ctypes.POINTER(ctype))[0] = value

    def rc_ext_frame_open(self, _cfg, err_ptr):
        self._write(err_ptr, ctypes.c_int, self.open_error)
        return 0 if self.open_error else 0xF00D

    def rc_ext_frame_geometry(self, _handle, width, height, fourcc, depth, maximum):
        for ptr, value in zip(
            (width, height, fourcc, depth, maximum),
            (4, 2, self.sdk.FOURCC_NV12, 6, 2),
        ):
            self._write(ptr, ctypes.c_uint32, value)
        return 0

    def rc_ext_frame_next(self, _handle, out_ptr, timeout_ms):
        self.next_timeouts.append(int(timeout_ms))
        if not self.events:
            return -int(self.sdk.ErrorCode.EINTERNAL)
        event = self.events.pop(0)
        if isinstance(event, int):
            return event

        spec = {
            "seq": 1,
            "pts_us": 1_000_001,
            "width": 4,
            "height": 2,
            "fourcc": self.sdk.FOURCC_NV12,
            "buf_size": 48,
            "flags": 0,
            "chn_id": 4,
            "n_planes": 2,
            "planes": [(0, 8, 4), (32, 8, 2)],
            "fd": 51,
        }
        spec.update(event)
        frame = ctypes.cast(out_ptr, ctypes.POINTER(self.sdk._FrameBuf)).contents
        frame.seq = spec["seq"]
        frame.pts_us = spec["pts_us"]
        frame.width = spec["width"]
        frame.height = spec["height"]
        frame.fourcc = spec["fourcc"]
        frame.buf_size = spec["buf_size"]
        frame.flags = spec["flags"]
        frame.chn_id = spec["chn_id"]
        frame.n_planes = spec["n_planes"]
        for index, (offset, stride, vstride) in enumerate(spec["planes"][:3]):
            frame.plane[index].offset = offset
            frame.plane[index].stride = stride
            frame.plane[index].vstride = vstride
        frame.fd = spec["fd"]
        frame._base = None
        frame._map_len = 0

        size = max(1, int(spec["buf_size"]))
        backing = (ctypes.c_ubyte * size)(
            *((int(spec["seq"]) * 10 + i) % 256 for i in range(size))
        )
        self._backings[int(spec["seq"])] = backing
        return 0

    def rc_ext_frame_map(self, _handle, frame_ptr):
        self.map_calls += 1
        if self.map_fails:
            return 0
        frame = ctypes.cast(frame_ptr, ctypes.POINTER(self.sdk._FrameBuf)).contents
        backing = self._backings[int(frame.seq)]
        address = ctypes.addressof(backing)
        frame._base = address
        frame._map_len = frame.buf_size
        return address + int(frame.plane[0].offset)

    def rc_ext_frame_release(self, _handle, frame_ptr):
        frame = ctypes.cast(frame_ptr, ctypes.POINTER(self.sdk._FrameBuf)).contents
        self.release_seqs.append(int(frame.seq))
        frame.fd = -1
        frame._base = None
        frame._map_len = 0

    def rc_ext_frame_close(self, _handle):
        self.close_calls += 1


class FakeProbeLib:
    """Small stateful implementation of the ``rc_ext_probe_*`` functions."""

    def __init__(self, sdk, events=()):
        self.sdk = sdk
        self.events = list(events)
        self.next_timeouts = []
        self.release_seqs = []
        self.close_calls = 0
        self._stage = b"metrics"
        self._payloads = []

    @staticmethod
    def _write(ptr, ctype, value):
        ctypes.cast(ptr, ctypes.POINTER(ctype))[0] = value

    def rc_ext_probe_open(self, _stages, _count, _sample_every, err_ptr):
        self._write(err_ptr, ctypes.c_int, 0)
        return 0xBEEF

    def rc_ext_probe_info(self, _handle, sample_every, mask):
        self._write(sample_every, ctypes.c_uint32, 1)
        self._write(mask, ctypes.c_uint32, 8)
        return 0

    def rc_ext_probe_next(self, _handle, out_ptr, timeout_ms):
        self.next_timeouts.append(int(timeout_ms))
        if not self.events:
            return -int(self.sdk.ErrorCode.EINTERNAL)
        event = self.events.pop(0)
        if isinstance(event, int):
            return event
        seq = int(event.get("seq", 1))
        payload = ctypes.create_string_buffer(event.get("payload", b"{}"))
        self._payloads.append(payload)
        sample = ctypes.cast(
            out_ptr, ctypes.POINTER(self.sdk._ProbeSample)
        ).contents
        sample.stage_id = self._stage
        sample.seq = seq
        sample.pts_us = int(event.get("pts_us", 1_000_000))
        sample.payload = ctypes.addressof(payload)
        sample.payload_len = len(payload.raw) - 1
        sample.flags = 0
        sample.has_meta = 0
        sample._fd = -1
        sample._base = None
        sample._map_len = 0
        sample._pb = None
        return 0

    def rc_ext_probe_release(self, _handle, sample_ptr):
        sample = ctypes.cast(
            sample_ptr, ctypes.POINTER(self.sdk._ProbeSample)
        ).contents
        self.release_seqs.append(int(sample.seq))

    def rc_ext_probe_close(self, _handle):
        self.close_calls += 1


def _source(sdk_module, monkeypatch, events, **fake_options):
    fake = FakeFrameLib(sdk_module, events, **fake_options)
    monkeypatch.setattr(sdk_module, "_load", lambda _path=None: fake)
    return sdk_module.FrameSource(timeout_ms=100), fake


def _probe_source(sdk_module, monkeypatch, events, *, timeout_ms=100):
    fake = FakeProbeLib(sdk_module, events)
    monkeypatch.setattr(sdk_module, "_load", lambda _path=None: fake)
    return sdk_module.ProbeSource(["metrics"], timeout_ms=timeout_ms), fake


def test_frame_is_compatible_explicit_lease_with_named_plane_layout(
    sdk_module, monkeypatch
):
    source, fake = _source(sdk_module, monkeypatch, [{"seq": 7, "fd": 70}])
    frame = source.acquire()

    assert type(frame) is sdk_module.Frame
    assert isinstance(frame, sdk_module.FrameLease)
    assert frame.seq == 7 and frame.fd == 70
    assert isinstance(frame.planes, list)  # historical public container shape
    assert frame.planes == [(0, 8, 4), (32, 8, 2)]
    assert isinstance(frame.planes[0], sdk_module.PlaneLayout)
    offset, stride, vstride = frame.planes[0]
    assert (offset, stride, vstride) == (0, 8, 4)
    assert frame.buffer.planes == tuple(frame.planes)
    assert "borrowed" in repr(frame) and "borrowed" in repr(frame.buffer)
    with pytest.raises(AttributeError):
        frame.buffer.size = 1_000_000
    with pytest.raises(AttributeError):
        frame.buffer.planes = ((0, 1_000_000, 1_000_000),)

    source.close()
    assert fake.release_seqs == [7]


def test_checked_mapping_planes_and_owned_copy(sdk_module, monkeypatch):
    source, fake = _source(sdk_module, monkeypatch, [{"seq": 2}])
    frame = source.acquire()

    complete = frame.buffer.map()
    assert complete.shape == (48,)
    assert complete.flags.writeable is False
    assert frame.buffer.map() is complete
    assert fake.map_calls == 1

    y_plane = frame.plane_array(0)
    uv_plane = frame.plane_array(1)
    assert y_plane.shape == (4, 8)
    assert uv_plane.shape == (2, 8)
    assert y_plane.flags.writeable is False
    assert y_plane[1, 0] == 28  # seq*10 + second padded row offset

    valid_y = frame.array
    assert valid_y.shape == (2, 4)
    assert valid_y.tolist() == [[20, 21, 22, 23], [28, 29, 30, 31]]
    owned = frame.copy()

    assert frame.release() is True
    assert frame.release() is False
    assert fake.release_seqs == [2]
    assert owned.tolist() == [[20, 21, 22, 23], [28, 29, 30, 31]]
    source.close()


def test_every_new_buffer_access_is_rejected_after_release(sdk_module, monkeypatch):
    source, _fake = _source(sdk_module, monkeypatch, [{}])
    frame = source.acquire()
    # Exercise the cached-array path too: it must still check the live lease.
    _ = frame.array
    assert frame.buffer.release() is True
    assert frame.released and frame.buffer.released
    assert frame.release_reason == "explicit"
    assert "released" in repr(frame)

    accessors = [
        lambda: frame.fd,
        lambda: frame.array,
        lambda: frame.plane_array(0),
        lambda: frame.buffer.fd,
        lambda: frame.buffer.map(),
        lambda: frame.buffer.copy(),
    ]
    for access in accessors:
        with pytest.raises(sdk_module.BufferReleasedError):
            access()
    with pytest.raises(sdk_module.BufferReleasedError):
        frame.buffer.__enter__()

    source.close()


def test_acquiring_next_frame_auto_releases_previous_and_close_releases_current(
    sdk_module, monkeypatch, caplog
):
    source, fake = _source(
        sdk_module,
        monkeypatch,
        [{"seq": 11, "fd": 61}, {"seq": 12, "fd": 62}],
    )
    caplog.set_level(logging.DEBUG, logger=sdk_module.__name__)

    first = source.acquire()
    second = source.acquire(timeout_ms=17)
    assert first.released and first.release_reason == "next_acquire"
    assert not second.released
    assert fake.release_seqs == [11]
    assert fake.next_timeouts == [100, 17]

    assert source.close() is True
    assert source.close() is False
    assert second.released and second.release_reason == "source_close"
    assert fake.release_seqs == [11, 12]
    assert fake.close_calls == 1
    assert "released FrameSource borrow" in caplog.text


def test_frame_context_releases_once_when_body_raises(sdk_module, monkeypatch):
    source, fake = _source(sdk_module, monkeypatch, [{"seq": 3}])
    with pytest.raises(LookupError):
        with source.acquire() as frame:
            assert not frame.released
            raise LookupError("application failure")

    assert frame.released
    assert fake.release_seqs == [3]
    source.close()
    assert fake.release_seqs == [3]


@pytest.mark.parametrize("target_name", ["frame", "buffer"])
def test_borrow_context_preserves_body_error_when_cleanup_fails(
    sdk_module, monkeypatch, caplog, target_name
):
    source, fake = _source(sdk_module, monkeypatch, [{"seq": 4}])
    frame = source.acquire()
    target = frame if target_name == "frame" else frame.buffer

    class BodyError(Exception):
        pass

    class CleanupAbort(BaseException):
        pass

    def fail_cleanup():
        raise CleanupAbort(f"{target_name} cleanup")

    monkeypatch.setattr(target, "release", fail_cleanup)
    caplog.set_level(logging.ERROR)
    original = BodyError("body failure")
    with pytest.raises(BodyError) as raised:
        with target:
            raise original

    assert raised.value is original
    assert any("CleanupAbort" in note for note in raised.value.__notes__)
    assert "cleanup failed" in caplog.text
    assert fake.release_seqs == []
    source.close()
    assert fake.release_seqs == [4]


@pytest.mark.parametrize("target_name", ["frame", "buffer"])
def test_borrow_context_propagates_cleanup_error_without_body_error(
    sdk_module, monkeypatch, target_name
):
    source, _fake = _source(sdk_module, monkeypatch, [{"seq": 5}])
    frame = source.acquire()
    target = frame if target_name == "frame" else frame.buffer

    class CleanupError(Exception):
        pass

    def fail_cleanup():
        raise CleanupError(f"{target_name} cleanup")

    monkeypatch.setattr(target, "release", fail_cleanup)
    with pytest.raises(CleanupError):
        with target:
            pass
    source.close()


def test_strict_acquire_distinguishes_timeout_and_native_error(
    sdk_module, monkeypatch
):
    timeout_source, timeout_fake = _source(sdk_module, monkeypatch, [1])
    with pytest.raises(sdk_module.AcquireTimeoutError) as timeout:
        timeout_source.acquire(timeout_ms=25)
    assert isinstance(timeout.value, TimeoutError)
    assert timeout.value.operation == "rc_ext_frame_next"
    assert timeout.value.retryable
    assert timeout_fake.next_timeouts == [25]
    timeout_source.close()

    error_source, _error_fake = _source(sdk_module, monkeypatch, [-5])
    with pytest.raises(sdk_module.BackpressureError) as error:
        error_source.acquire()
    assert error.value.code is sdk_module.ErrorCode.EBACKPRESSURE
    assert error.value.rc == -5 and error.value.retryable
    error_source.close()


@pytest.mark.parametrize(
    "timeout_ms",
    [True, False, -1, 1.5, "10", 2**31],
)
def test_source_constructors_reject_invalid_timeout_before_loading_native(
    sdk_module, monkeypatch, timeout_ms
):
    load_calls = []

    def record_load(_path=None):
        load_calls.append(_path)
        raise AssertionError("native loader must not run")

    monkeypatch.setattr(sdk_module, "_load", record_load)
    with pytest.raises(sdk_module.FormatError):
        sdk_module.FrameSource(timeout_ms=timeout_ms)
    with pytest.raises(sdk_module.FormatError):
        sdk_module.ProbeSource(["metrics"], timeout_ms=timeout_ms)
    assert load_calls == []


@pytest.mark.parametrize("timeout_ms", [0, 2**31 - 1])
def test_frame_timeout_c_int_boundaries_reach_native_without_truncation(
    sdk_module, monkeypatch, timeout_ms
):
    source, fake = _source(sdk_module, monkeypatch, [{"seq": 31}])
    frame = source.acquire(timeout_ms=timeout_ms)
    assert fake.next_timeouts == [timeout_ms]
    frame.release()
    source.close()


@pytest.mark.parametrize("timeout_ms", [True, -1, 1.5, "10", 2**31])
def test_bad_frame_acquire_override_does_not_release_current_borrow(
    sdk_module, monkeypatch, timeout_ms
):
    source, fake = _source(sdk_module, monkeypatch, [{"seq": 32}])
    frame = source.acquire()

    with pytest.raises(sdk_module.FormatError):
        source.acquire(timeout_ms=timeout_ms)

    assert not frame.released
    assert fake.release_seqs == []
    assert fake.next_timeouts == [100]
    source.close()
    assert fake.release_seqs == [32]


def test_bad_probe_acquire_override_does_not_release_current_borrow(
    sdk_module, monkeypatch
):
    source, fake = _probe_source(sdk_module, monkeypatch, [{"seq": 41}])
    sample = source.acquire()

    with pytest.raises(sdk_module.FormatError):
        source.acquire(timeout_ms=False)

    assert not sample.released
    assert fake.release_seqs == []
    assert fake.next_timeouts == [100]
    source.close()
    assert fake.release_seqs == [41]


def test_compatibility_iterator_retries_timeout_and_records_terminal_error(
    sdk_module, monkeypatch
):
    source, fake = _source(sdk_module, monkeypatch, [1, {"seq": 8}, -4])

    frame = next(source)
    assert frame.seq == 8
    assert fake.next_timeouts == [100, 100]
    with pytest.raises(StopIteration):
        next(source)
    assert frame.released and frame.release_reason == "iterator_advance"
    assert isinstance(source.last_error, sdk_module.FormatError)
    assert source.last_error.rc == -4
    source.close()


def test_acquire_after_close_raises_handle_closed(sdk_module, monkeypatch):
    source, fake = _source(sdk_module, monkeypatch, [])
    source.close()
    with pytest.raises(sdk_module.HandleClosedError):
        source.acquire()
    assert fake.next_timeouts == []


def test_malformed_native_plane_metadata_fails_closed(sdk_module, monkeypatch):
    too_many, fake_count = _source(
        sdk_module,
        monkeypatch,
        [{"seq": 21, "n_planes": 4}],
    )
    with pytest.raises(sdk_module.FormatError, match="plane count"):
        too_many.acquire()
    assert fake_count.release_seqs == [21]
    too_many.close()

    out_of_bounds, fake_bounds = _source(
        sdk_module,
        monkeypatch,
        [{"seq": 22, "planes": [(40, 8, 4), (32, 8, 2)]}],
    )
    with pytest.raises(sdk_module.FormatError, match="plane layout"):
        out_of_bounds.acquire()
    assert fake_bounds.release_seqs == [22]
    out_of_bounds.close()


def test_wrapper_baseexception_releases_native_borrow_immediately(
    sdk_module, monkeypatch
):
    source, fake = _source(sdk_module, monkeypatch, [{"seq": 23}])

    class WrapperAbort(BaseException):
        pass

    def abort_wrap(_cbuf):
        raise WrapperAbort("abort wrapper construction")

    monkeypatch.setattr(source, "_wrap", abort_wrap)
    with pytest.raises(WrapperAbort):
        source.acquire()

    assert fake.release_seqs == [23]
    assert source._cur is None
    source.close()
    assert fake.release_seqs == [23]


def test_plane_array_rejects_non_integer_index(sdk_module, monkeypatch):
    source, _fake = _source(sdk_module, monkeypatch, [{}])
    frame = source.acquire()
    with pytest.raises(IndexError, match="integer"):
        frame.plane_array(slice(None))
    source.close()


def test_native_map_failure_is_typed_internal_error(sdk_module, monkeypatch):
    source, _fake = _source(sdk_module, monkeypatch, [{}], map_fails=True)
    frame = source.acquire()
    with pytest.raises(sdk_module.InternalError) as raised:
        _ = frame.array
    assert raised.value.operation == "rc_ext_frame_map"
    source.close()


def test_frame_open_error_is_typed(sdk_module, monkeypatch):
    fake = FakeFrameLib(sdk_module, open_error=2)
    monkeypatch.setattr(sdk_module, "_load", lambda _path=None: fake)
    with pytest.raises(sdk_module.AuthenticationError) as raised:
        sdk_module.FrameSource()
    assert raised.value.rc == 2

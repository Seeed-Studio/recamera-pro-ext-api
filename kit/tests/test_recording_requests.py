"""Explicit recording controls are independent from presentation and ML output."""
import importlib
import json
import queue
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kit.app import App
from kit.adapters.result_sink import GatewayResultSink, MultiSink, ResultSink
from kit.logic.recording import request_configured_recording


def gateway_without_thread(size=2):
    sink = GatewayResultSink.__new__(GatewayResultSink)
    sink._q = queue.Queue(maxsize=size)
    sink._stop = threading.Event()
    sink._errors = sink._dropped = sink._seq = 0
    sink._frame_w = sink._frame_h = 0
    sink.app_id = 'demo-app'
    sink.preserve_envelope = False
    return sink


def test_display_frames_cannot_replace_explicit_request_and_queue_is_bounded():
    sink = gateway_without_thread()
    assert sink.request_recording('fall', 1)
    sink.emit({'results': []}, 2)
    sink.emit({'events': [{'kind': 'fall'}]}, 3)
    assert json.loads(sink._q.queue[0])['type'] == 'recording_request'
    assert sink.request_recording('fall', 4)
    assert not sink.request_recording('fall', 5)
    assert len(sink._q.queue) == 2
    assert [json.loads(v)['pts'] for v in sink._q.queue] == [1, 4]


def test_app_api_warmup_and_unmanaged_sinks_do_not_request_or_publish():
    sink = gateway_without_thread(8)
    app = App()
    app._rt = {'sink': MultiSink([sink])}
    app._cur_frame = SimpleNamespace(pts=1, w=200, h=100)
    assert not app.request_recording('fall')
    app._warmed = True
    app.emit([{'kind': 'fall'}], results=[])
    assert json.loads(sink._q.queue[-1])['type'] == 'results'
    assert app.request_recording('fall')
    assert json.loads(sink._q.queue[-1]) == {
        'type': 'recording_request', 'event_kind': 'fall', 'pts': 1.0, 'seq': 2}
    app._rt['sink'] = SimpleNamespace(emit=lambda *_: pytest.fail('display'))
    assert not app.request_recording('fall')
    for invalid in ['', 'FALL', '../fall', None, 'fall\n']:
        with pytest.raises(ValueError):
            app.request_recording(invalid)


def test_recording_policy_is_opt_in_live_configured_and_only_queued_requests_cool_down():
    calls = []
    app = SimpleNamespace(config={}, request_recording=lambda *a: calls.append(a) or True)
    assert not request_configured_recording(app, 'fall', 1)
    app.config = {'recording_enabled': True, 'recording_cooldown_sec': 10}
    assert request_configured_recording(app, 'fall', 1)
    assert not request_configured_recording(app, 'fall', 2)
    assert request_configured_recording(app, 'fall', 11)
    assert not request_configured_recording(app, 'blink', 11)
    app.config['recording_blink'] = True
    app.request_recording = lambda *_: False
    assert not request_configured_recording(app, 'blink', 12)
    app.request_recording = lambda *a: calls.append(a) or True
    assert not request_configured_recording(app, 'blink', 12.5)
    assert request_configured_recording(app, 'blink', 13)
    app.config['recording_enabled'] = False
    assert not request_configured_recording(app, 'fall', 100)
    assert calls == [('fall', 1.0), ('fall', 11.0), ('blink', 13.0)]


def test_actual_qr_loop_records_new_codes_not_each_frame_and_keeps_display():
    fixture = importlib.import_module('kit.tests.test_qrcode_shape_equivalence')
    cls = fixture._load_new_app_module().QrcodeReaderApp
    app = cls()
    app.config = {'recording_enabled': True, 'recording_cooldown_sec': 5}
    # A persists, disappears and returns within cooldown, then returns later.
    script = [('A',), ('A',), (), ('A',), (), ('A',), ('B',)]
    times = [1, 2, 3, 4, 5, 7, 8]
    app._decoder = SimpleNamespace(decode=lambda data: [
        {'text': text, 'quad': [[0, 0]] * 4} for text in data])
    app.frames = lambda: iter(SimpleNamespace(data=data, pts=t)
                             for data, t in zip(script, times))
    displays, requests = [], []
    app.emit = lambda events, pts, **_: displays.append((events, pts))
    app.request_recording = lambda kind, pts: requests.append((kind, pts)) or True
    app.run()
    assert requests == [('qrcode', 1.0), ('qrcode', 7.0)]
    assert len(displays) == 7
    assert [x['text'] for x in displays[1][0]] == ['A']


@pytest.mark.parametrize('name', ['fall', 'facemesh', 'retail'])
def test_real_business_loops_explicit_requests_follow_edges_and_keep_outputs(name):
    fixture = importlib.import_module(f'kit.tests.test_{name}_shape_equivalence')
    harness = fixture._Base(methodName='runTest')
    harness.setUp()
    requests = []
    try:
        config = dict(fixture.EFF, recording_enabled=True, recording_cooldown_sec=0,
                      recording_blink=True, recording_yawn=True,
                      recording_drowsiness=True, recording_direction='in')
        with patch.object(App, 'request_recording',
                          lambda app, kind, pts: requests.append((kind, pts)) or True):
            sink, _app = harness._run_new(config)
        expected = []
        previous_drowsy = False
        for payload, pts in sink.payloads:
            events = payload['events']
            if name == 'fall':
                expected.extend(('fall', pts) for ev in events if ev['kind'] == 'fall')
            elif name == 'retail':
                if any(ev['kind'] == 'line_cross' and ev['dir'] == 'in' for ev in events):
                    expected.append(('line_cross', pts))
            else:
                expected.extend((ev['kind'], pts) for ev in events
                                if ev['kind'] in ('blink', 'yawn'))
                drowsy = any(ev['kind'] == 'drowsiness' for ev in events)
                if drowsy and not previous_drowsy:
                    expected.append(('drowsiness', pts))
                previous_drowsy = drowsy
        assert expected, 'fixture must exercise at least one real business trigger'
        assert requests == expected
    finally:
        harness.tearDown()


def test_recording_cooldown_recovers_when_frame_pts_resets():
    app = SimpleNamespace(config={'recording_enabled': True}, request_recording=lambda *_: True)
    assert request_configured_recording(app, 'fall', 500)
    assert not request_configured_recording(app, 'fall', 501)
    assert request_configured_recording(app, 'fall', 1)
    assert not request_configured_recording(app, 'fall', 2)


def test_qr_retries_a_failed_request_while_code_remains_visible_at_most_once_per_second():
    fixture = importlib.import_module('kit.tests.test_qrcode_shape_equivalence')
    app = fixture._load_new_app_module().QrcodeReaderApp()
    app.config = {'recording_enabled': True, 'recording_cooldown_sec': 0}
    app._decoder = SimpleNamespace(decode=lambda _: [{'text': 'A', 'quad': [[0, 0]] * 4}])
    app.frames = lambda: iter(SimpleNamespace(data=None, pts=t) for t in [1, 1.2, 2, 2.1, 3])
    app.emit = lambda *_args, **_kwargs: None
    attempts = []
    app.request_recording = lambda kind, pts: attempts.append(pts) or len(attempts) > 1
    app.run()
    assert attempts == [1.0, 2.0]


def test_gateway_saturation_uses_cached_priority_without_parsing_large_payloads(monkeypatch):
    sink = gateway_without_thread()
    assert sink.request_recording('fall', 1)
    sink.emit({'results': ['x' * 100000]}, 2)
    with monkeypatch.context() as patcher:
        patcher.setattr(json, 'loads', lambda *_: pytest.fail('queue reparsed wire JSON'))
        sink.emit({'results': []}, 3)
    assert sink._q.unfinished_tasks == 2
    assert json.loads(sink._q.queue[-1])['pts'] == 3


def test_gateway_worker_accounts_for_sent_and_shutdown_items():
    sink = gateway_without_thread(3)
    sent = []
    sink._conn = SimpleNamespace(settimeout=lambda _: None, sendall=sent.append)
    sink._conn_lock = threading.Lock()
    sink._send_timeout = .1
    sink._sent = 0
    sink.emit({'results': []}, 1)
    assert sink.request_recording('fall', 2)
    sink._q.put_nowait(None)
    sink._run()
    assert len(sent) == 2
    assert sink._q.unfinished_tasks == 0


def test_drowsiness_loop_retries_failed_edge_while_active_without_repeating_success():
    fixture = importlib.import_module('kit.tests.test_facemesh_shape_equivalence')
    harness = fixture._Base(methodName='runTest')
    harness.setUp()
    attempts = []
    try:
        config = dict(fixture.EFF, recording_enabled=True, recording_cooldown_sec=0,
                      recording_blink=False, recording_yawn=False, recording_drowsiness=True)
        def request(_app, kind, pts):
            assert kind == 'drowsiness'
            attempts.append(pts)
            return len(attempts) > 1
        with patch.object(App, 'request_recording', request):
            sink, _ = harness._run_new(config)
        assert len(attempts) >= 2
        assert attempts[1] - attempts[0] >= 1
        active_frames = sum(any(ev['kind'] == 'drowsiness' for ev in payload['events'])
                            for payload, _ in sink.payloads)
        assert active_frames > len(attempts)
    finally:
        harness.tearDown()


def test_close_racing_serialization_cannot_enqueue_after_writer_stops(monkeypatch):
    from kit.adapters import result_sink
    sink = gateway_without_thread()
    original = result_sink._GatewayLine
    def closing_line(value, priority=False):
        sink._stop.set()
        return original(value, priority)
    monkeypatch.setattr(result_sink, '_GatewayLine', closing_line)
    assert not sink.request_recording('fall', 1)
    assert sink._q.empty()
    assert sink._q.unfinished_tasks == 0

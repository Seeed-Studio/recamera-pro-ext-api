"""Connector deployment state machine and real HTTP protocol boundary tests."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import urllib.parse

import pytest
from tests.cvsprout.test_apps import system_models as models, transport


def fixture_task(data=b'RKNN' * 150000, activate=False):
    sha = hashlib.sha256(data).hexdigest()
    md5 = hashlib.md5(data).hexdigest()
    return {'id': 1, 'release_id': 'test-release', 'filename': 'cvsprout-' + sha[:24] + '.rknn',
            'artifact_url': '/api/edge/system-model-releases/test/artifact', 'sha256': sha,
            'md5': md5, 'size_bytes': len(data), 'model_info': {
                'category': 'Detection', 'algorithm': 'yolov8', 'classes': ['object'],
                'metrics': {'confidence': .25, 'iou': .45, 'max_obj': 128}, 'md5sum': md5, 'size': len(data)},
            'contract': {'algorithm': 'yolov8', 'target_platform': 'rv1126b', 'task': 'detect'},
            'activate': activate, 'fps': 10, 'status': 'running', 'stage': 'queued', 'last_sequence': 0}


class Backend:
    server_url = 'https://controlled.example'

    def __init__(self, data=b'RKNN' * 150000, activate=False):
        self.data = data
        self.task = fixture_task(data, activate)
        self.requests = []
        self.reports = {}
        self.fail_claim_ack = self.fail_report_ack = False
        self.renew_rejected = False
        self.config = {'device_id': 'test-device', 'project_id': 1, 'system_model_schema_version': 1}

    def configuration(self):
        return dict(self.config)

    def json_request(self, path, payload):
        self.requests.append((path, deepcopy(payload)))
        if path == 'system/status':
            return {'received': True}
        if path == 'model-tasks/claim':
            if self.renew_rejected:
                raise transport.DeliveryError('http_409')
            previous = self.task.get('lease_token')
            if previous:
                assert payload['lease_token'] == previous
            self.task['lease_token'] = payload['lease_token']
            if self.fail_claim_ack:
                self.fail_claim_ack = False
                raise transport.DeliveryError('server_timeout')
            now = datetime.now(timezone.utc)
            result = deepcopy(self.task)
            result['lease_expires_at'] = (now + timedelta(seconds=120)).isoformat()
            return {'task': result, 'server_time': now.isoformat()}
        if path.endswith('/report'):
            assert payload['lease_token'] == self.task['lease_token']
            sequence = payload['sequence']
            if sequence in self.reports:
                assert self.reports[sequence] == payload
            elif payload['status'] == 'running':
                # Enforce the actual backend's monotonic report state machine.
                if models.PROGRESS_STAGES[payload['stage']] < models.PROGRESS_STAGES.get(self.task['stage'], 0):
                    raise transport.DeliveryError('http_409')
            self.reports[sequence] = deepcopy(payload)
            self.task.update(status=payload['status'], stage=payload['stage'], last_sequence=sequence)
            if self.fail_report_ack:
                self.fail_report_ack = False
                raise transport.DeliveryError('server_timeout')
            return {'received': True}
        raise AssertionError('unexpected backend route')

    def download(self, task, agent_id, target, guard):
        guard()
        target.write_bytes(self.data)


class System:
    def __init__(self):
        self.state = {'iEnable': 1, 'sModel': 'original.rknn', 'sStatus': 'running',
                      'iFPS': 10, 'iActualFPS': 10, 'bExternalHold': False}
        self.files = {}
        self.sessions = {}
        self.calls = []
        self.lose_chunk_ack = self.lose_finalize_ack = self.lose_configure_ack = self.lose_activate_ack = False
        self.chunk_callback = None

    def current(self):
        return dict(self.state)

    def probe(self):
        return {'capabilities': {'schema_version': 1, 'target_platform': 'rv1126b', 'model_api': True,
                                'algorithms': ['yolov8', 'yolo11']}, 'current': self.current(),
                'firmware_version': 'controlled', 'available_bytes': 2**30}

    def info(self, filename):
        return deepcopy(self.files.get(filename))

    def models(self):
        return [{'model': name, 'modelInfo': deepcopy(info)} for name, info in self.files.items()]

    def start_upload(self):
        self.calls.append(('start',))
        identifier = str(len(self.sessions) + 1)
        self.sessions[identifier] = b''
        return identifier

    def chunk(self, session, data=b''):
        self.calls.append(('chunk', len(data)))
        self.sessions[session] += data
        if self.chunk_callback:
            self.chunk_callback()
        if data and self.lose_chunk_ack:
            self.lose_chunk_ack = False
            raise models.ModelError('system_api_unavailable', uncertain=True)
        return max(1, len(self.sessions[session]))

    def finalize(self, session, task):
        self.calls.append(('finalize',))
        data = self.sessions[session]
        assert hashlib.md5(data).hexdigest() == task['md5'], 'Duplicated append corrupted bytes'
        assert task['filename'] not in self.files, 'Must never overwrite an existing model'
        self.files[task['filename']] = {'md5sum': task['md5'], 'size': task['size_bytes']}
        if self.lose_finalize_ack:
            self.lose_finalize_ack = False
            raise models.ModelError('system_api_unavailable', uncertain=True)

    def configure(self, task):
        self.calls.append(('configure',))
        self.files[task['filename']] = deepcopy(task['model_info'])
        if self.lose_configure_ack:
            self.lose_configure_ack = False
            raise models.ModelError('system_api_unavailable', uncertain=True)

    def activate(self, values):
        self.calls.append(('activate', values['sModel']))
        self.state.update({key: values[key] for key in ('sModel', 'iEnable', 'iFPS')})
        if self.lose_activate_ack:
            self.lose_activate_ack = False
            raise models.ModelError('system_api_unavailable', uncertain=True)


def worker(tmp_path, backend=None, system=None):
    backend, system = backend or Backend(), system or System()
    return models.ModelDeploymentWorker(backend, tmp_path / 'deployments', system=system), backend, system


def test_import_only_streams_chunks_and_registers_complete_metadata_without_activation(tmp_path):
    agent, backend, system = worker(tmp_path)
    agent.step()
    assert backend.task['status'] == 'succeeded' and backend.task['stage'] == 'uploaded'
    assert [call[1] for call in system.calls if call[0] == 'chunk'] == [512 * 1024, len(backend.data) - 512 * 1024]
    assert not any(call[0] == 'activate' for call in system.calls)
    assert system.files[backend.task['filename']] == backend.task['model_info']
    assert agent.active is None and not list((tmp_path / 'deployments').glob('*/model.rknn'))


def test_activate_uses_system_engine_and_verifies_running_fps(tmp_path):
    agent, backend, system = worker(tmp_path, Backend(activate=True))
    agent.step()
    assert backend.task['status'] == 'succeeded' and backend.task['stage'] == 'verified'
    assert system.state['sModel'] == backend.task['filename']
    assert len([call for call in system.calls if call[0] == 'activate']) == 1


def test_old_server_does_not_touch_system_api_or_create_journal(tmp_path):
    backend = Backend()
    backend.config.pop('system_model_schema_version')
    agent, _, system = worker(tmp_path, backend)
    agent.step()
    assert not backend.requests and not system.calls and not (tmp_path / 'deployments').exists()


def test_unsupported_firmware_is_reported_without_claiming_or_crashing_capture(tmp_path):
    agent, backend, system = worker(tmp_path)
    system.probe = lambda: (_ for _ in ()).throw(models.ModelError('system_api_unsupported'))
    agent.step()
    assert backend.requests[-1][0] == 'system/status'
    assert backend.requests[-1][1]['capabilities']['model_api'] is False
    assert not any(path == 'model-tasks/claim' for path, body in backend.requests)


def test_first_claim_ack_loss_recovers_persisted_client_lease_after_restart(tmp_path):
    agent, backend, system = worker(tmp_path)
    backend.fail_claim_ack = True
    with pytest.raises(transport.DeliveryError):
        agent.step()
    saved = backend.task['lease_token']
    restarted, _, _ = worker(tmp_path, backend, system)
    restarted.step()
    assert backend.task['lease_token'] == saved and backend.task['status'] == 'succeeded'
    assert len([call for call in system.calls if call[0] == 'start']) == 1


@pytest.mark.parametrize('lost', ['lose_chunk_ack', 'lose_finalize_ack', 'lose_configure_ack', 'lose_activate_ack'])
def test_lost_mutation_receipts_reconcile_without_duplicate_side_effects(tmp_path, lost):
    agent, backend, system = worker(tmp_path, Backend(activate=True))
    setattr(system, lost, True)
    agent.step()
    assert backend.task['status'] == 'running'
    restarted, _, _ = worker(tmp_path, backend, system)
    restarted.step()
    assert backend.task['status'] == 'succeeded'
    assert len([call for call in system.calls if call[0] == 'start']) == 1
    assert len([call for call in system.calls if call[0] == 'finalize']) == 1
    assert len([call for call in system.calls if call[0] == 'configure']) == 1
    assert len([call for call in system.calls if call[0] == 'activate']) == 1
    if lost == 'lose_chunk_ack':
        assert ('chunk', 0) in system.calls
    assert next(iter(system.sessions.values())) == backend.data


def test_lost_final_report_ack_reconciles_terminal_without_redeployment(tmp_path):
    agent, backend, system = worker(tmp_path)
    original = backend.json_request

    def lose_terminal(path, payload):
        if path.endswith('/report') and payload['status'] == 'succeeded':
            backend.fail_report_ack = True
        return original(path, payload)

    backend.json_request = lose_terminal
    with pytest.raises(transport.DeliveryError):
        agent.step()
    calls = list(system.calls)
    backend.json_request = original
    restarted, _, _ = worker(tmp_path, backend, system)
    restarted.step()
    assert restarted.active is None and system.calls == calls


def test_existing_foreign_model_is_never_replaced(tmp_path):
    agent, backend, system = worker(tmp_path)
    system.files[backend.task['filename']] = {'md5sum': 'f' * 32, 'size': 9}
    agent.step()
    assert backend.task['status'] == 'unknown' and not system.calls
    assert system.files[backend.task['filename']]['md5sum'] == 'f' * 32


def test_lease_rejection_stops_firmware_mutations(tmp_path):
    agent, backend, system = worker(tmp_path)
    original = backend.json_request
    claims = [0]

    def refuse_renew(path, payload):
        if path == 'model-tasks/claim':
            claims[0] += 1
            if claims[0] >= 2:
                raise transport.DeliveryError('http_409')
        return original(path, payload)

    backend.json_request = refuse_renew
    agent.step()
    assert not system.calls and agent.active is not None
    assert agent.lease_until == 0


def test_restart_does_not_overwrite_a_manual_model_switch(tmp_path):
    agent, backend, system = worker(tmp_path, Backend(activate=True))
    system.lose_activate_ack = True
    agent.step()
    system.state['sModel'] = 'manually-selected.rknn'
    calls = list(system.calls)
    restarted, _, _ = worker(tmp_path, backend, system)
    restarted.step()
    assert backend.task['status'] == 'unknown'
    assert system.calls == calls and system.state['sModel'] == 'manually-selected.rknn'


def test_binding_change_has_distinct_agent_journal_and_preserves_old_pending(tmp_path):
    agent, backend, system = worker(tmp_path)
    system.lose_chunk_ack = True
    agent.step()
    first_path = agent.journal.path
    first_agent = agent.journal.data['agent_id']
    backend.config['project_id'] = 2
    backend.task = fixture_task(b'new-model-data')
    backend.reports = {}
    backend.data = b'new-model-data'
    agent.step()
    assert agent.journal.path != first_path and agent.journal.data['agent_id'] != first_agent
    assert (first_path / 'model.rknn').exists() and json.loads((first_path / 'journal.json').read_text())['active']


def test_journal_permissions_and_no_sqlite_dependency(tmp_path):
    journal = models.Journal(tmp_path / 'root', 'a' * 64)
    assert journal.file.stat().st_mode & 0o777 == 0o600
    assert journal.path.stat().st_mode & 0o777 == 0o700
    assert models.Journal(tmp_path / 'root', 'a' * 64).data['agent_id'] == journal.data['agent_id']


def test_real_cgi_308_chunk_progress_is_not_a_redirect():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            data = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            requests.append((self.path, data))
            self.send_response(308)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"range":"bytes 0-3"}')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        system = models.SystemModelClient(tls=False, port=server.server_port)
        assert system.chunk('7', b'data') == 4
        assert system.chunk('7') == 4
        assert requests == [('/cgi-bin/entry.cgi/model/upload?id=7', b'data'),
                            ('/cgi-bin/entry.cgi/model/upload?id=7', b'')]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_artifact_streaming_checks_origin_length_hash_and_lease_headers(tmp_path):
    data = b'RKNN' * 30000
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append((self.path, self.headers.get('X-Model-Lease'), self.headers.get('X-Agent-Id')))
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = models.DeploymentClient('http://127.0.0.1:%d/base' % server.server_port,
                                        'cvsprout_device_controlled')
        task = fixture_task(data)
        task['lease_token'] = 'a' * 32
        guards = []
        path = tmp_path / 'model.rknn'
        client.download(task, 'controlled-agent', path, lambda: guards.append(True))
        assert path.read_bytes() == data and len(guards) >= 3
        assert received == [('/base/api/edge/system-model-releases/test/artifact', 'a' * 32, 'controlled-agent')]
        task['sha256'] = 'f' * 64
        with pytest.raises(models.ModelError, match='artifact_integrity_failed'):
            client.download(task, 'controlled-agent', path, lambda: None)
        assert path.read_bytes() == data and not path.with_suffix('.part').exists()
        task['artifact_url'] = 'http://other.invalid/private'
        with pytest.raises(models.ModelError, match='artifact_download_failed'):
            client.download(task, 'controlled-agent', path, lambda: None)
        assert len(received) == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_actual_firmware_detection_capabilities_and_empty_version_fallback():
    system = models.SystemModelClient()
    seen = []
    actual = {
        'lDetection': ['yolov5', 'yolov5u', 'yolov8', 'yolov10', 'yolo11', 'yolo12', 'nanodet', 'detr', 'yolox'],
        'lClassification': ['classify'], 'lSegmentation': [], 'lKeypoint': ['yolov8'],
        'lTracking': ['yolov8'],
    }
    responses = {'/model/algorithm': actual, '/model/list': [], '/model/inference?id=0': System().current(),
                 '/system/version': {}, '/system/update-status': {'sStatus': 'success', 'sCurrentVersion': 'V1.2.4'},
                 '/system/remain-space': {'availableDisk': 4 * 1024**3}}

    def request(path):
        seen.append(path)
        return responses[path]

    system.request = request
    result = system.probe()
    assert result['capabilities']['algorithms'] == sorted(actual['lDetection'])
    assert 'classify' not in result['capabilities']['algorithms']
    assert result['firmware_version'] == 'V1.2.4'
    assert '/system/update-status' in seen


def test_real_cgi_body_type_and_success_envelope():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            data = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            seen.append((self.path, self.headers.get('Content-Type'), data))
            if self.path.endswith('id=7'):
                # The actual CGI rejects application/octet-stream.
                assert self.headers.get('Content-Type') == 'text/plain'
                status, body = 308, {'range': 'bytes 0-3'}
            else:
                assert self.headers.get('Content-Type') == 'application/json'
                status, body = 200, {'code': 200, 'message': 'success'}
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        system = models.SystemModelClient(tls=False, port=server.server_port)
        assert system.chunk('7', b'data') == 4
        system.configure(fixture_task())
        system.activate(System().current())
        assert len(seen) == 3
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize('destination', [
    'https://external.example/cgi-bin/entry.cgi/model/algorithm',
    'http://127.0.0.1/cgi-bin/entry.cgi/model/algorithm',
    'https://127.0.0.1/cgi-bin/entry.cgi/model/inference?id=0',
    'https://user:password@127.0.0.1/cgi-bin/entry.cgi/model/algorithm',
])
def test_cgi_redirect_never_changes_origin_operation_or_downgrades(destination):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(301)
            self.send_header('Location', destination)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = models.SystemModelClient(tls=False, port=server.server_port)
        with pytest.raises(models.ModelError, match='system_api_unsupported'):
            client.request('/model/algorithm')
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_loopback_tls_uses_public_certificate_trust_and_exact_leaf_pin(tmp_path):
    import ssl
    import subprocess
    cert = tmp_path / 'certificate.pem'
    key = tmp_path / 'key.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                    '-keyout', str(key), '-out', str(cert), '-days', '1',
                    '-subj', '/CN=controlled-device'], check=True, capture_output=True)
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"lDetection":["yolov8"]}')

        def log_message(self, *args):
            pass

    class QuietTLSServer(ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            # The pin-mismatch branch deliberately closes TLS before HTTP.
            pass

    server = QuietTLSServer(('127.0.0.1', 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = models.SystemModelClient(tls=True, port=server.server_port, certificate=str(cert))
        assert client.request('/model/algorithm') == {'lDetection': ['yolov8']}
        original = client._tls_context
        trusted_context, fingerprint = original()
        assert trusted_context.verify_mode == ssl.CERT_REQUIRED
        client._tls_context = lambda: (trusted_context, b'wrong-exact-pin')
        with pytest.raises(models.ModelError, match='system_api_unavailable'):
            client.request('/model/algorithm')
        # A pin mismatch is rejected BEFORE any HTTP request or mutation.
        assert received == ['/cgi-bin/entry.cgi/model/algorithm']
        client.certificate = str(tmp_path / 'missing-certificate')
        client._tls_context = original
        with pytest.raises(models.ModelError, match='system_api_unavailable'):
            client.request('/model/algorithm')
        assert len(received) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_resume_after_download_failure_does_not_query_a_fresh_empty_upload(tmp_path):
    agent, backend, system = worker(tmp_path)
    download = backend.download
    backend.download = lambda *a: (_ for _ in ()).throw(transport.DeliveryError('server_timeout'))
    with pytest.raises(transport.DeliveryError):
        agent.step()
    assert agent.active['phase'] == 'downloading'
    backend.download = download
    restarted, _, _ = worker(tmp_path, backend, system)
    restarted.step()
    assert backend.task['status'] == 'succeeded'
    assert ('chunk', 0) not in system.calls


def test_initial_terminal_claim_receipt_clears_token_and_accepts_next_task(tmp_path):
    agent, backend, system = worker(tmp_path)
    backend.fail_claim_ack = True
    with pytest.raises(transport.DeliveryError):
        agent.step()
    old_token = backend.task['lease_token']
    backend.task.update(status='failed', stage='failed', lease_expires_at=None)
    agent.step()
    assert agent.active is None and 'claim_token' not in agent.journal.data
    assert not system.calls
    backend.task = fixture_task(b'another-model')
    backend.data = b'another-model'
    backend.reports = {}
    agent.step()
    assert backend.task['status'] == 'succeeded' and backend.task['lease_token'] != old_token


def test_admin_resolved_unknown_receipt_never_reactivates_or_rolls_back(tmp_path):
    agent, backend, system = worker(tmp_path, Backend(activate=True))
    system.lose_activate_ack = True
    agent.step()
    old = deepcopy(agent.active)
    backend.task.update(status='failed', stage='failed', lease_expires_at=None)
    calls = list(system.calls)
    agent.step()
    assert agent.active is None and system.calls == calls
    backend.task = fixture_task(b'new-task-after-resolution')
    backend.data = b'new-task-after-resolution'
    backend.reports = {}
    agent.step()
    assert backend.task['status'] == 'succeeded'
    assert backend.task['lease_token'] != old['task']['lease_token']


def test_existing_incomplete_metadata_owned_by_someone_else_is_not_overwritten(tmp_path):
    agent, backend, system = worker(tmp_path)
    system.files[backend.task['filename']] = {'md5sum': backend.task['md5'], 'size': backend.task['size_bytes']}
    agent.step()
    assert backend.task['status'] == 'unknown' and not system.calls


class FakeClock:
    def __init__(self):
        self.now = 100.0
        self.stopped = False

    def __call__(self):
        return self.now

    def is_set(self):
        return self.stopped

    def wait(self, seconds):
        self.now += seconds
        return self.stopped


def prepare_activation_failure(tmp_path):
    agent, backend, system = worker(tmp_path, Backend(activate=True))
    clock = FakeClock()
    agent.clock = agent.stop = clock
    original = system.activate

    def activate(values):
        original(values)
        system.state.update(sStatus='error' if values['sModel'] == backend.task['filename'] else 'running',
                            iActualFPS=0 if values['sModel'] == backend.task['filename'] else 10)

    system.activate = activate
    return agent, backend, system, clock


def test_failed_activation_rolls_back_and_verifies_previous_running_configuration(tmp_path):
    agent, backend, system, clock = prepare_activation_failure(tmp_path)
    previous = system.current()
    agent.step()
    assert system.current() == previous
    assert backend.task['status'] == 'failed'
    assert backend.reports[max(backend.reports)]['rollback_status'] == 'restored'
    assert [call[1] for call in system.calls if call[0] == 'activate'] == [backend.task['filename'], 'original.rknn']


def test_rollback_to_disabled_configuration_requires_stopped_status(tmp_path):
    agent, backend, system, clock = prepare_activation_failure(tmp_path)
    system.state.update(iEnable=0, sStatus='stopped', iActualFPS=0)
    original = system.activate

    def activate(values):
        original(values)
        if not values['iEnable']:
            system.state.update(sStatus='stopped', iActualFPS=0)

    system.activate = activate
    agent.step()
    assert backend.task['status'] == 'failed'
    assert backend.reports[max(backend.reports)]['rollback_status'] == 'restored'
    assert system.state['sStatus'] == 'stopped' and system.state['iEnable'] == 0


def test_lease_revoked_before_rollback_prevents_all_further_device_mutations(tmp_path):
    agent, backend, system, clock = prepare_activation_failure(tmp_path)
    original = backend.json_request

    def revoke(path, payload):
        if path == 'model-tasks/claim' and clock.now >= 130:
            raise transport.DeliveryError('http_409')
        return original(path, payload)

    backend.json_request = revoke
    agent.step()
    assert [call[1] for call in system.calls if call[0] == 'activate'] == [backend.task['filename']]
    assert agent.active and agent.lease_until == 0


def test_manual_fps_change_during_verification_is_not_overwritten_or_reported_success(tmp_path):
    agent, backend, system, clock = prepare_activation_failure(tmp_path)
    activate = system.activate

    def concurrent_change(values):
        activate(values)
        system.state.update(iFPS=27, sStatus='running', iActualFPS=27)

    system.activate = concurrent_change
    agent.step()
    assert backend.task['status'] == 'unknown' and system.state['iFPS'] == 27
    assert len([call for call in system.calls if call[0] == 'activate']) == 1


def test_failed_rollback_running_state_is_not_reported_as_restored(tmp_path):
    agent, backend, system, clock = prepare_activation_failure(tmp_path)
    original = system.activate

    def failure(values):
        original(values)
        system.state.update(sStatus='error', iActualFPS=0)

    system.activate = failure
    agent.step()
    assert backend.task['status'] == 'unknown'
    assert backend.reports[max(backend.reports)]['rollback_status'] == 'unknown'


def test_capture_and_deployment_threads_share_total_shutdown_deadline(monkeypatch):
    from tests.cvsprout.test_apps import capture
    clock = [100.0]
    monkeypatch.setattr(capture, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    waits = []

    class Hung:
        ident = 1

        def join(self, timeout):
            waits.append(timeout)
            clock[0] += timeout

        def is_alive(self):
            return True

    app = capture.CVSproutLinkApp()
    app.uploader = SimpleNamespace(stop=threading.Event())
    app.model_worker = SimpleNamespace(stop=threading.Event())
    app.upload_thread, app.model_thread = Hung(), Hung()
    for _ in range(2):
        with pytest.raises(RuntimeError):
            app._stop_uploader()
    assert waits == [10, 0, 0, 0]
    assert sum(waits) == 10 and app.uploader.stop.is_set() and app.model_worker.stop.is_set()


def test_existing_model_file_with_missing_sidecar_is_never_overwritten(tmp_path):
    agent, backend, system = worker(tmp_path)
    system.models = lambda: [{'model': backend.task['filename'], 'modelInfo': {}}]
    # Real CGI returns info 404 even though /list still contains the .rknn.
    agent.step()
    assert backend.task['status'] == 'unknown' and not system.calls


def test_orphan_metadata_is_not_reported_as_a_successful_import(tmp_path):
    agent, backend, system = worker(tmp_path)
    system.files[backend.task['filename']] = deepcopy(backend.task['model_info'])
    system.models = lambda: []
    agent.step()
    assert backend.task['status'] == 'unknown' and not system.calls


def test_concurrent_metadata_edit_during_activation_is_not_reported_success(tmp_path):
    agent, backend, system = worker(tmp_path, Backend(activate=True))
    activate = system.activate

    def concurrent(values):
        activate(values)
        system.files[backend.task['filename']]['classes'] = ['manually-edited']

    system.activate = concurrent
    agent.step()
    assert backend.task['status'] == 'unknown'
    assert system.files[backend.task['filename']]['classes'] == ['manually-edited']
    assert len([call for call in system.calls if call[0] == 'activate']) == 1


def test_deployment_large_json_response_has_own_bounded_limit():
    body = {'task': {'model_info': {'classes': ['class-%d' % index for index in range(10000)]}}}
    encoded = json.dumps(body).encode()
    assert len(encoded) > 65536

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length', 0)))
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = models.DeploymentClient('http://127.0.0.1:%d' % server.server_port, 'cvsprout_device_controlled')
        assert client.json_request('model-tasks/claim', {}) == body
        assert transport.EdgeClient.max_response_bytes == 65536
        encoded = json.dumps({'padding': 'x' * models.MAX_JSON_BYTES}).encode()
        with pytest.raises(transport.DeliveryError, match='server_response_too_large'):
            client.json_request('model-tasks/claim', {})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_unknown_task_polling_observes_without_renewing_until_manual_resolution(tmp_path):
    agent, backend, system = worker(tmp_path, Backend(activate=True))
    system.lose_activate_ack = True
    agent.step()
    system.state['sModel'] = 'manual-choice.rknn'
    agent.step()
    assert backend.task['status'] == 'unknown'
    before = len(backend.requests)
    calls = list(system.calls)
    agent.step()
    fresh_claims = [body for path, body in backend.requests[before:] if path == 'model-tasks/claim']
    assert len(fresh_claims) == 1 and fresh_claims[0]['observe_only'] is True
    assert agent.lease_until == 0 and system.calls == calls
    backend.task['status'] = 'failed'  # explicit administrator resolution receipt
    agent.step()
    assert agent.active is None and system.calls == calls


def test_unknown_positive_evidence_renews_only_for_verified_terminal_report(tmp_path):
    agent, backend, system = worker(tmp_path, Backend(activate=True))
    system.lose_activate_ack = True
    agent.step()
    system.state['sModel'] = 'manual-choice.rknn'
    agent.step()
    assert backend.task['status'] == 'unknown'
    system.state['sModel'] = backend.task['filename']
    before = len(backend.requests)
    calls = list(system.calls)
    agent.step()
    claims = [body for path, body in backend.requests[before:] if path == 'model-tasks/claim']
    assert claims[0]['observe_only'] is True
    assert len(claims) == 2 and not claims[1].get('observe_only')
    assert backend.task['status'] == 'succeeded' and system.calls == calls


@pytest.mark.parametrize('version_status,update_status,expected', [
    (200, 200, 'V1.2.4'),  # Actual V1.2.4 /version response is JSON null.
    (404, 200, 'V1.2.4'),
    (200, 503, ''),       # Both version sources are optional metadata.
])
def test_real_cgi_null_or_unavailable_version_does_not_hide_model_capability(version_status, update_status, expected):
    seen = []
    responses = {
        '/model/algorithm': (200, {'lDetection': ['yolov8', 'yolo11']}),
        '/model/list': (200, []),
        '/model/inference?id=0': (200, System().current()),
        '/system/version': (version_status, None),
        '/system/update-status': (update_status, {'sStatus': 'success', 'sCurrentVersion': 'V1.2.4'}),
        '/system/remain-space': (200, {'availableDisk': 3268423680}),
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.removeprefix('/cgi-bin/entry.cgi')
            seen.append(path)
            status, body = responses[path]
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = models.SystemModelClient(tls=False, port=server.server_port)
        probe = client.probe()
        assert probe['capabilities']['model_api'] is True
        assert probe['capabilities']['algorithms'] == ['yolo11', 'yolov8']
        assert probe['firmware_version'] == expected
        assert probe['available_bytes'] == 3268423680
        assert '/system/update-status' in seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_200_embedded_not_found_only_means_absent_for_optional_info_read():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"code":404,"message":"Model info not found"}')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = models.SystemModelClient(tls=False, port=server.server_port)
        assert client.info('not-present.rknn') is None
        with pytest.raises(models.ModelError, match='system_api_unavailable'):
            client.request('/model/list')
        with pytest.raises(models.ModelError, match='system_api_unavailable'):
            client.request('/model/info?File-name=not-present.rknn')
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_retry_after_info_read_failure_never_regresses_server_stage(tmp_path):
    agent, backend, system = worker(tmp_path)
    original = system.info
    failures = [True]

    def unavailable_once(filename):
        if failures[0]:
            failures[0] = False
            raise models.ModelError('system_api_unavailable')
        return original(filename)

    system.info = unavailable_once
    agent.step()
    assert backend.task['stage'] == 'uploading'
    assert agent.active['phase'] == 'upload_preparing'
    before = len(backend.requests)
    restarted, _, _ = worker(tmp_path, backend, system)
    restarted.step()
    fresh = [body for path, body in backend.requests[before:] if path.endswith('/report')]
    assert not any(body['status'] == 'running' and body['stage'] in ('downloading', 'uploading') for body in fresh)
    assert backend.task['status'] == 'succeeded'
    assert len([call for call in system.calls if call[0] == 'start']) == 1


def test_old_journal_regressive_pending_report_is_reconciled_against_claim(tmp_path):
    agent, backend, system = worker(tmp_path)
    original = system.info
    system.info = lambda filename: (_ for _ in ()).throw(models.ModelError('system_api_unavailable'))
    agent.step()
    assert backend.task['stage'] == 'uploading'
    # Reproduce the exact older journal: the later uploading ACK exists, but
    # local phase still downloading and a regressive report is pending after409.
    sequence = agent.active['sequence'] + 1
    agent.save(phase='downloading', sequence=sequence, pending_report={
        'agent_id': agent.journal.data['agent_id'], 'lease_token': backend.task['lease_token'],
        'sequence': sequence, 'status': 'running', 'stage': 'downloading',
        'error_code': '', 'rollback_status': 'not_needed'})
    agent.active.pop('reported_stage')
    agent.journal.save()
    system.info = original
    before = len(backend.requests)
    restarted, _, _ = worker(tmp_path, backend, system)
    restarted.step()
    fresh = [body for path, body in backend.requests[before:] if path.endswith('/report')]
    assert fresh and all(body['stage'] != 'downloading' for body in fresh)
    assert backend.task['status'] == 'succeeded' and restarted.active is None


@pytest.mark.parametrize('lost_stage', ['downloading', 'uploading'])
def test_progress_report_ack_loss_replays_exact_sequence_before_new_stage(tmp_path, lost_stage):
    agent, backend, system = worker(tmp_path)
    original = backend.json_request
    once = [True]

    def lose_ack(path, payload):
        if path.endswith('/report') and payload['stage'] == lost_stage and once[0]:
            once[0] = False
            backend.fail_report_ack = True
        return original(path, payload)

    backend.json_request = lose_ack
    with pytest.raises(transport.DeliveryError):
        agent.step()
    pending = deepcopy(agent.active['pending_report'])
    assert pending['stage'] == lost_stage
    restarted, _, _ = worker(tmp_path, backend, system)
    restarted.step()
    attempts = [body for path, body in backend.requests if path.endswith('/report') and body['sequence'] == pending['sequence']]
    assert attempts == [pending, pending]
    assert backend.task['status'] == 'succeeded'


@pytest.mark.parametrize('remote_stage', ['uploaded', 'activating', 'activated', 'verified'])
def test_later_server_progress_without_local_intent_never_replays_mutations(tmp_path, remote_stage):
    agent, backend, system = worker(tmp_path, Backend(activate=True))
    # Persist a real first-claim journal, then model the incompatible older
    # local checkpoint: server moved beyond uploading but the write intent and
    # prior inference configuration are absent from local durable storage.
    backend.fail_report_ack = True
    with pytest.raises(transport.DeliveryError):
        agent.step()
    backend.task.update(stage=remote_stage, last_sequence=2)
    agent.active.pop('reported_stage')
    agent.journal.save()
    assert agent.active['phase'] == 'downloading'
    system.files[backend.task['filename']] = deepcopy(backend.task['model_info'])
    system.state['sModel'] = 'manually-selected.rknn'
    restarted, _, _ = worker(tmp_path, backend, system)
    restarted.step()
    assert backend.task['status'] == 'unknown'
    assert restarted.active['phase'] == 'progress_conflict'
    assert not system.calls and system.state['sModel'] == 'manually-selected.rknn'
    # Further reconciliation remains read-only and cannot silently activate.
    before = len(backend.requests)
    restarted.step()
    claims = [body for path, body in backend.requests[before:] if path == 'model-tasks/claim']
    assert claims and all(body.get('observe_only') for body in claims)
    assert not system.calls and backend.task['status'] == 'unknown'

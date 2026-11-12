"""System-model deployment via public CGI APIs; no NPU, decoder or SQLite.

Every device mutation belongs to a renewable CVSprout lease. Durable intents
precede side effects; an uncertain response is reconciled rather than replayed.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from transport import APP_VERSION, DeliveryError, EdgeClient

CHUNK_BYTES = 512 * 1024
MAX_ARTIFACT_BYTES = 500 * 1024 * 1024
MAX_JSON_BYTES = 2 * 1024 * 1024
SYSTEM_CERTIFICATE = '/userdata/config/system/ssl/server.crt'
TERMINAL = {'succeeded', 'failed', 'cancelled'}
PROGRESS_STAGES = {'queued': 0, 'downloading': 1, 'uploading': 2, 'uploaded': 3,
                   'activating': 4, 'activated': 5, 'verified': 6}
ERROR_CODES = frozenset({
    'system_api_unavailable', 'system_api_unauthorized', 'system_api_unsupported',
    'system_model_incompatible', 'system_model_busy', 'artifact_download_failed',
    'artifact_integrity_failed', 'insufficient_storage', 'upload_failed',
    'activation_failed', 'verification_failed', 'rollback_failed', 'lease_expired',
    'credential_changed', 'deployment_interrupted', 'device_error',
})


class ModelError(RuntimeError):
    def __init__(self, code, uncertain=False):
        super().__init__(code if code in ERROR_CODES else 'device_error')
        self.uncertain = uncertain


def safe_current(value):
    if not isinstance(value, dict):
        raise ModelError('system_api_unsupported')
    required = {'iEnable', 'sModel', 'sStatus', 'iFPS', 'iActualFPS', 'bExternalHold'}
    if not required <= value.keys():
        raise ModelError('system_api_unsupported')
    result = {key: value[key] for key in required}
    if (type(result['iEnable']) is not int or result['iEnable'] not in (0, 1)
            or type(result['iFPS']) is not int or not 0 <= result['iFPS'] <= 1000
            or type(result['iActualFPS']) not in (int, float)
            or not math.isfinite(result['iActualFPS']) or not 0 <= result['iActualFPS'] <= 1000
            or type(result['bExternalHold']) is not bool
            or not isinstance(result['sModel'], str) or len(result['sModel']) > 255
            or result['sStatus'] not in ('running', 'starting', 'stopped', 'stopping', 'error', 'unknown')):
        raise ModelError('system_api_unsupported')
    return result


def algorithm_names(payload):
    """rc_infer_get_support_alog exposes task-specific lDetection names."""
    values = payload.get('lDetection') if isinstance(payload, dict) else None
    if not isinstance(values, list):
        return []
    return sorted({name for name in values[:256] if isinstance(name, str)
                   and re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', name)})


class SystemModelClient:
    """Only the device loopback nginx control plane; never private RKIPC RPC."""
    def __init__(self, *, tls=False, port=None, timeout=3, certificate=SYSTEM_CERTIFICATE):
        self.tls, self.port, self.timeout = tls, port or (443 if tls else 80), timeout
        self.certificate = certificate

    def _tls_context(self):
        try:
            pem = Path(self.certificate).read_text(encoding='ascii')
            if len(pem) > 65536:
                raise ValueError()
            expected = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).digest()
            context = ssl.create_default_context(cafile=self.certificate)
            # The official self-signed device certificate need not name
            # 127.0.0.1. Check validity/chain AND pin the exact local leaf.
            context.check_hostname = False
            return context, expected
        except (OSError, ValueError, ssl.SSLError):
            raise ModelError('system_api_unavailable') from None

    def request(self, path, method='GET', payload=None, raw=False, allow_missing=False):
        if not path.startswith(('/model/', '/system/')) or '\\' in path or '\r' in path or '\n' in path:
            raise ModelError('system_api_unsupported')
        body = payload if raw else (json.dumps(payload, separators=(',', ':')).encode() if payload is not None else None)
        target = '/cgi-bin/entry.cgi' + path
        tls, port = self.tls, self.port
        for redirect in range(2):
            context, expected = self._tls_context() if tls else (None, None)
            connection = (http.client.HTTPSConnection('127.0.0.1', port, timeout=self.timeout, context=context)
                          if tls else http.client.HTTPConnection('127.0.0.1', port, timeout=self.timeout))
            try:
                if tls:
                    connection.connect()
                    if hashlib.sha256(connection.sock.getpeercert(binary_form=True)).digest() != expected:
                        raise ModelError('system_api_unavailable')
                connection.request(method, target, body=body, headers={
                    # Official CGI accepts binary resumable bodies via
                    # text/plain; application/octet-stream is rejected.
                    'Content-Type': 'text/plain' if raw else 'application/json',
                    'Accept': 'application/json', 'User-Agent': 'CVSprout-Connector/' + APP_VERSION,
                })
                response = connection.getresponse()
                status, location = response.status, response.getheader('Location')
                data = response.read(MAX_JSON_BYTES + 1)
            except (OSError, http.client.HTTPException, ValueError):
                raise ModelError('system_api_unavailable', uncertain=method != 'GET') from None
            finally:
                connection.close()
            # Firmware uses 308 without Location for upload progress. It is not
            # an HTTP redirect and must never cause chunk retransmission.
            if status in (301, 302, 307, 308) and location:
                parsed = urllib.parse.urlsplit(location)
                if (redirect or parsed.scheme != 'https'
                        or parsed.hostname not in ('127.0.0.1', 'localhost', '::1')
                        or parsed.username or parsed.password or parsed.fragment
                        or parsed.port not in (None, 443)
                        or parsed.path + ('?' + parsed.query if parsed.query else '') != target):
                    raise ModelError('system_api_unsupported', uncertain=method != 'GET')
                tls = parsed.scheme == 'https'
                port = parsed.port or (443 if tls else 80)
                target = parsed.path + ('?' + parsed.query if parsed.query else '')
                continue
            if status == 404 and allow_missing:
                return None
            if status in (401, 403):
                raise ModelError('system_api_unauthorized', uncertain=method != 'GET')
            if status in (404, 405, 501):
                raise ModelError('system_api_unsupported', uncertain=method != 'GET')
            if status not in (200, 201, 308) or len(data) > MAX_JSON_BYTES:
                raise ModelError('system_api_unavailable', uncertain=method != 'GET')
            try:
                result = json.loads(data)
            except (ValueError, TypeError):
                raise ModelError('system_api_unavailable', uncertain=method != 'GET') from None
            # Some firmware versions encode API failures inside HTTP 200.
            # Only the explicit optional model-info read can treat 404 as absent.
            if allow_missing and isinstance(result, dict) and result.get('code') == 404:
                return None
            if not isinstance(result, (dict, list)) or isinstance(result, dict) and result.get('code', 0) not in (0, 200, 201):
                raise ModelError('system_api_unavailable', uncertain=method != 'GET')
            self.tls, self.port = tls, port
            return result
        raise ModelError('system_api_unavailable')

    def current(self):
        return safe_current(self.request('/model/inference?id=0'))

    def info(self, filename):
        return self.request('/model/info?' + urllib.parse.urlencode({'File-name': filename}), allow_missing=True)

    def models(self):
        result = self.request('/model/list')
        if (not isinstance(result, list) or len(result) > 10000
                or any(not isinstance(item, dict) or not isinstance(item.get('model'), str)
                       or not isinstance(item.get('modelInfo'), dict) for item in result)):
            raise ModelError('system_api_unsupported')
        return result

    def probe(self):
        algorithms = algorithm_names(self.request('/model/algorithm'))
        if not algorithms:
            raise ModelError('system_api_unsupported')
        current = self.current()
        self.models()
        # Firmware versions may expose /version as JSON null or leave the
        # route unavailable. Version metadata must not hide working model APIs.
        try:
            version = self.request('/system/version')
        except ModelError:
            version = {}
        if not isinstance(version, dict):
            version = {}
        space = self.request('/system/remain-space')
        if not isinstance(space, dict):
            raise ModelError('system_api_unsupported')
        available = space.get('availableDisk')
        if type(available) is not int or available < 0:
            raise ModelError('system_api_unsupported')
        firmware = next((version[key] for key in ('sVersion', 'sFirmwareVersion', 'version', 'sCurrentVersion')
                         if isinstance(version.get(key), str)), '')
        if not firmware:
            # /update-status is the public read-only installed version.
            try:
                update = self.request('/system/update-status')
                firmware = update.get('sCurrentVersion', '') if isinstance(update, dict) else ''
            except ModelError:
                firmware = ''
        if not isinstance(firmware, str):
            firmware = ''
        firmware = ''.join(c for c in firmware if 32 <= ord(c) < 127)[:200]
        return {'capabilities': {'schema_version': 1, 'target_platform': 'rv1126b',
                                'model_api': True, 'algorithms': algorithms},
                'current': current, 'firmware_version': firmware, 'available_bytes': available}

    def start_upload(self):
        result = self.request('/model/upload?upload-type=resumable', 'POST', b'', raw=True)
        identifier = str(result.get('id', ''))
        if not re.fullmatch(r'[0-9]{1,12}', identifier):
            raise ModelError('upload_failed', uncertain=True)
        return identifier

    def chunk(self, session, data=b''):
        result = self.request('/model/upload?id=' + session, 'POST', data, raw=True)
        match = re.fullmatch(r'bytes 0-([0-9]+)', str(result.get('range', '')))
        if not match:
            raise ModelError('upload_failed', uncertain=True)
        return int(match.group(1)) + 1

    def finalize(self, session, task):
        query = urllib.parse.urlencode({'start': session, 'File-name': task['filename'], 'md5sum': task['md5']})
        self.request('/model/upload?' + query, 'POST', b'', raw=True)

    def configure(self, task):
        self.request('/model/info?' + urllib.parse.urlencode({'File-name': task['filename']}),
                     'POST', task['model_info'])

    def activate(self, values):
        self.request('/model/inference?id=0', 'POST', {key: values[key] for key in ('iEnable', 'sModel', 'iFPS')})


class Journal:
    """One fsync/atomic-rename journal per server + device + project identity."""
    def __init__(self, root, owner):
        self.root, self.path = Path(root), Path(root) / owner
        if self.root.is_symlink() or self.path.is_symlink():
            raise ModelError('device_error')
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        os.chmod(self.path, 0o700)
        self.file = self.path / 'journal.json'
        if self.file.is_symlink():
            raise ModelError('device_error')
        if self.file.exists():
            if self.file.stat().st_size > MAX_JSON_BYTES:
                raise ModelError('device_error')
            try:
                self.data = json.loads(self.file.read_text())
                uuid.UUID(self.data['agent_id'])
                if self.data.get('version') != 1 or self.data.get('owner') != owner:
                    raise ValueError()
            except (ValueError, KeyError, TypeError):
                raise ModelError('device_error') from None
        else:
            self.data = {'version': 1, 'owner': owner, 'agent_id': str(uuid.uuid4()), 'active': None}
            self.save()

    def save(self):
        raw = json.dumps(self.data, ensure_ascii=True, separators=(',', ':')).encode()
        if len(raw) > MAX_JSON_BYTES:
            raise ModelError('device_error')
        temporary = self.path / ('.journal-' + uuid.uuid4().hex + '.tmp')
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.file)
        finally:
            temporary.unlink(missing_ok=True)
        descriptor = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class DeploymentClient(EdgeClient):
    # Model metadata includes class names more than once; capture receipts
    # retain their separate 64 KiB limit.
    max_response_bytes = MAX_JSON_BYTES

    def json_request(self, path, payload):
        return self.request(path, json.dumps(payload, separators=(',', ':')).encode())

    def download(self, task, agent_id, target, guard):
        artifact = task['artifact_url']
        url = (self.server_url + artifact if artifact.startswith('/api/edge/') else
               urllib.parse.urljoin(self.server_url + '/', artifact))
        base, parsed = urllib.parse.urlsplit(self.server_url), urllib.parse.urlsplit(url)
        if (parsed.scheme, parsed.hostname, parsed.port) != (base.scheme, base.hostname, base.port) or parsed.username or parsed.password or parsed.fragment:
            raise ModelError('artifact_download_failed')
        if not parsed.path.startswith(base.path.rstrip('/') + '/api/edge/'):
            raise ModelError('artifact_download_failed')
        request = urllib.request.Request(url, headers={
            'Authorization': 'Bearer ' + self._token, 'X-Model-Lease': task['lease_token'],
            'X-Agent-Id': agent_id, 'User-Agent': 'CVSprout-Connector/' + APP_VERSION,
        })
        sha, md5, size = hashlib.sha256(), hashlib.md5(), 0
        temporary = target.with_suffix('.part')
        if temporary.is_symlink() or target.is_symlink():
            raise ModelError('artifact_download_failed')
        try:
            guard()
            with self._opener.open(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise ModelError('artifact_download_failed')
                length = response.headers.get('Content-Length')
                if length is not None and int(length) != task['size_bytes']:
                    raise ModelError('artifact_integrity_failed')
                fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
                with os.fdopen(fd, 'wb') as stream:
                    while True:
                        guard()
                        chunk = response.read(65536)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > task['size_bytes'] or size > MAX_ARTIFACT_BYTES:
                            raise ModelError('artifact_integrity_failed')
                        sha.update(chunk)
                        md5.update(chunk)
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
            if size != task['size_bytes'] or sha.hexdigest() != task['sha256'] or md5.hexdigest() != task['md5']:
                raise ModelError('artifact_integrity_failed')
            os.replace(temporary, target)
        except ModelError:
            raise
        except (OSError, ValueError, urllib.error.URLError, DeliveryError):
            raise ModelError('artifact_download_failed') from None
        finally:
            temporary.unlink(missing_ok=True)


def validate_task(task):
    try:
        valid = (type(task['id']) is int and task['id'] > 0
                 and re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,120}\.rknn', task['filename'])
                 and '..' not in task['filename']
                 and re.fullmatch('[a-f0-9]{64}', task['sha256'])
                 and re.fullmatch('[a-f0-9]{32}', task['md5'])
                 and type(task['size_bytes']) is int and 1 < task['size_bytes'] <= MAX_ARTIFACT_BYTES
                 and isinstance(task['model_info'], dict) and isinstance(task['contract'], dict)
                 and task['contract']['target_platform'] == 'rv1126b'
                 and isinstance(task['contract']['algorithm'], str)
                 and type(task['activate']) is bool
                 and type(task['fps']) is int and 1 <= task['fps'] <= 120
                 and isinstance(task['lease_token'], str) and 32 <= len(task['lease_token']) <= 256)
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ModelError('system_model_incompatible')
    if task['model_info'].get('md5sum') != task['md5'] or task['model_info'].get('size') != task['size_bytes']:
        raise ModelError('artifact_integrity_failed')
    return task


def matches_file(info, task):
    return isinstance(info, dict) and info.get('md5sum') == task['md5'] and info.get('size') == task['size_bytes']


def matches_metadata(info, task):
    return matches_file(info, task) and all(info.get(key) == value for key, value in task['model_info'].items())


def running(current, filename, fps):
    return (current['sModel'] == filename and current['iEnable'] == 1 and current['sStatus'] == 'running'
            and current['iFPS'] == fps and current['iActualFPS'] > 0 and current['bExternalHold'] is False)


def configuration_matches(current, expected):
    return all(current[k] == expected[k] for k in ('sModel', 'iEnable', 'iFPS')) and not current['bExternalHold']


def restored(current, previous):
    return configuration_matches(current, previous) and (
        (current['sStatus'] == 'running' and current['iActualFPS'] > 0)
        if previous['iEnable'] else current['sStatus'] == 'stopped')


class ModelDeploymentWorker:
    def __init__(self, client, root, system=None, clock=time.monotonic, configuration=None):
        self.client, self.root, self.system, self.clock = client, Path(root), system or SystemModelClient(), clock
        self.stop = threading.Event()
        self.journal = None
        self.lease_until = 0
        self.next_renew = 0
        self.last_error = ''
        self._last_log = None
        self.configuration = configuration or client.configuration

    def log(self, code):
        code = code if code in ERROR_CODES else 'device_error'
        if code != self._last_log:
            print('[cvsprout-connector] system_model: ' + code, flush=True)
            self._last_log = code
        self.last_error = code

    @property
    def active(self):
        return self.journal.data['active']

    def save(self, **changes):
        self.active.update(changes)
        self.journal.save()

    def model_info(self, filename):
        # /info reads only the JSON sidecar: a 404 DOES NOT establish that
        # the .rknn file is absent. /list enumerates the actual model files.
        entries = [entry for entry in self.system.models() if entry['model'] == filename]
        info = self.system.info(filename)
        if not entries and info is None:
            return None
        if len(entries) != 1 or info is None or entries[0]['modelInfo'] != info:
            raise ModelError('artifact_integrity_failed', uncertain=True)
        return info

    def target_ready(self, current, task):
        if not running(current, task['filename'], task['fps']):
            return False
        if not matches_metadata(self.model_info(task['filename']), task):
            raise ModelError('artifact_integrity_failed', uncertain=True)
        return True

    def claim(self, resume=False, observe_only=False):
        payload = {'agent_id': self.journal.data['agent_id']}
        if resume:
            payload.update(task_id=self.active['task']['id'], lease_token=self.active['task']['lease_token'])
            if observe_only:
                payload['observe_only'] = True
        else:
            if not self.journal.data.get('claim_token'):
                self.journal.data['claim_token'] = uuid.uuid4().hex
                self.journal.save()
            payload['lease_token'] = self.journal.data['claim_token']
        result = self.client.json_request('model-tasks/claim', payload)
        task = result.get('task')
        if task is None:
            if resume:
                raise ModelError('lease_expired')
            return None
        task = validate_task(task)
        if resume and (task['id'] != self.active['task']['id'] or task['lease_token'] != self.active['task']['lease_token']
                       or any(task[key] != self.active['task'][key] for key in
                              ('sha256', 'md5', 'size_bytes', 'filename', 'model_info', 'activate', 'fps', 'contract'))):
            raise ModelError('credential_changed')
        if task['status'] in TERMINAL:
            return task
        if resume and task['status'] == 'running':
            remote_rank = PROGRESS_STAGES.get(task.get('stage'), -1)
            changes = {}
            if remote_rank > PROGRESS_STAGES.get(self.active.get('reported_stage'), -1):
                changes['reported_stage'] = task['stage']
            pending = self.active.get('pending_report')
            if (pending and pending['status'] == 'running'
                    and PROGRESS_STAGES.get(pending['stage'], -1) < remote_rank):
                # Compatibility with an older journal that persisted an already
                # completed earlier stage before the server rejected regression.
                # Remote progress proves this status-only report is obsolete;
                # never discard an uncertain mutation or terminal receipt.
                changes['pending_report'] = None
                changes['sequence'] = max(self.active.get('sequence', 0), task.get('last_sequence', 0))
            if self.active['phase'] == 'downloading':
                if task.get('stage') == 'uploading':
                    changes['phase'] = 'upload_preparing'
                elif remote_rank > PROGRESS_STAGES['uploading']:
                    # Only the known pre-upload checkpoint bug is migratable.
                    # Later server progress without the matching durable intent
                    # cannot authorize another upload, configuration or switch.
                    changes['phase'] = 'progress_conflict'
            if changes:
                self.save(**changes)
        if observe_only:
            # Unresolved tasks must become eligible for explicit administrator
            # resolution; polling evidence must not renew their mutation lease.
            self.lease_until = self.next_renew = 0
            return task
        try:
            seconds = (datetime.fromisoformat(task['lease_expires_at'].replace('Z', '+00:00')) -
                       datetime.fromisoformat(result['server_time'].replace('Z', '+00:00'))).total_seconds()
        except (ValueError, KeyError, TypeError):
            raise ModelError('lease_expired') from None
        if not 5 <= seconds <= 600:
            raise ModelError('lease_expired')
        self.lease_until = self.clock() + min(seconds, 120) - 5
        self.next_renew = self.clock() + 15
        return task

    def guard(self, force=False):
        if self.stop.is_set():
            raise ModelError('deployment_interrupted')
        if force or self.clock() >= self.next_renew:
            try:
                task = self.claim(resume=True)
            except DeliveryError:
                self.lease_until = 0
                raise ModelError('lease_expired') from None
            if task['status'] in TERMINAL:
                raise ModelError('lease_expired')
        if self.clock() >= self.lease_until:
            raise ModelError('lease_expired')

    def report(self, status, stage, error='', current=None, rollback='not_needed'):
        self.guard()
        if self.active.get('pending_report'):
            self.flush_report()
        if (status == 'running' and PROGRESS_STAGES.get(stage, -1)
                <= PROGRESS_STAGES.get(self.active.get('reported_stage'), -1)):
            return
        sequence = self.active.get('sequence', 0) + 1
        payload = {'agent_id': self.journal.data['agent_id'], 'lease_token': self.active['task']['lease_token'],
                   'sequence': sequence, 'status': status, 'stage': stage,
                   'error_code': error, 'rollback_status': rollback}
        if current is not None:
            payload['current'] = current
        self.save(sequence=sequence, pending_report=payload)
        self.flush_report()

    def flush_report(self):
        pending = self.active.get('pending_report')
        if not pending:
            return
        self.guard()
        pending = self.active.get('pending_report')
        if not pending:
            return
        self.client.json_request('model-tasks/%d/report' % self.active['task']['id'], pending)
        self.save(pending_report=None, reported_status=pending['status'], reported_stage=pending['stage'])

    def finish_task(self):
        cache = self.journal.path / 'model.rknn'
        cache.unlink(missing_ok=True)
        cache.with_suffix('.part').unlink(missing_ok=True)
        self.journal.data['active'] = None
        self.journal.data.pop('claim_token', None)
        self.journal.save()

    def cache(self, task):
        path = self.journal.path / 'model.rknn'
        if path.exists() and not path.is_symlink():
            sha, md5, size = hashlib.sha256(), hashlib.md5(), 0
            with path.open('rb') as stream:
                for block in iter(lambda: stream.read(65536), b''):
                    size += len(block)
                    if size > task['size_bytes']:
                        break
                    sha.update(block)
                    md5.update(block)
            if size == task['size_bytes'] and sha.hexdigest() == task['sha256'] and md5.hexdigest() == task['md5']:
                return path
            path.unlink()
        path.with_suffix('.part').unlink(missing_ok=True)
        # Old binding caches remain isolated; bound aggregate storage instead of
        # deleting another identity's possibly unacknowledged deployment state.
        used = sum(p.stat().st_size for p in self.root.glob('*/*') if p.name in ('model.rknn', 'model.part'))
        if used + task['size_bytes'] > MAX_ARTIFACT_BYTES:
            raise ModelError('insufficient_storage')
        statv = os.statvfs(self.journal.path)
        if statv.f_bavail * statv.f_frsize < task['size_bytes'] + 16 * 1024 * 1024:
            raise ModelError('insufficient_storage')
        self.client.download(task, self.journal.data['agent_id'], path, self.guard)
        return path

    def _upload(self, task, cache, resumed):
        phase = self.active['phase']
        info = self.model_info(task['filename'])
        if info is not None:
            if not matches_file(info, task):
                raise ModelError('artifact_integrity_failed', uncertain=True)
            self.save(phase='uploaded')
            return
        if phase in ('finalizing', 'configuring', 'uploaded', 'ready', 'activating', 'verifying', 'rolling_back'):
            raise ModelError('deployment_interrupted', uncertain=True)
        if phase == 'upload_starting':
            raise ModelError('deployment_interrupted', uncertain=True)
        created_session = not self.active.get('session')
        if created_session:
            self.guard(force=True)
            self.save(phase='upload_starting')
            session = self.system.start_upload()
            self.save(phase='uploading', session=session, offset=0, append_pending=False)
        session = self.active['session']
        offset = self.active.get('offset', 0)
        if self.active.get('append_pending') or resumed and not created_session:
            self.guard(force=True)
            observed = self.system.chunk(session)
            # The firmware reports bytes 0-0 for both zero and one byte. Never
            # guess after an uncertain first append; a checksum cannot fix an
            # already duplicated prefix without starting another write.
            maximum = min(task['size_bytes'], offset + CHUNK_BYTES) if self.active.get('append_pending') else offset
            if observed == 1 or not offset <= observed <= maximum:
                self.save(phase='upload_conflict')
                raise ModelError('upload_failed', uncertain=True)
            offset = observed
            self.save(offset=offset, append_pending=False)
        with cache.open('rb') as stream:
            stream.seek(offset)
            while offset < task['size_bytes']:
                self.guard(force=True)
                block = stream.read(min(CHUNK_BYTES, task['size_bytes'] - offset))
                self.save(append_pending=True, offset=offset)
                acknowledged = self.system.chunk(session, block)
                if acknowledged != offset + len(block):
                    raise ModelError('upload_failed', uncertain=True)
                offset = acknowledged
                self.save(offset=offset, append_pending=False)
        # A fresh read protects existing immutable names. Never overwrite a
        # different actor's artifact or repair its model information silently.
        self.guard(force=True)
        if self.model_info(task['filename']) is not None:
            raise ModelError('artifact_integrity_failed', uncertain=True)
        self.save(phase='finalizing', owned_finalization=True)
        self.system.finalize(session, task)
        if not matches_file(self.model_info(task['filename']), task):
            raise ModelError('artifact_integrity_failed', uncertain=True)
        self.save(phase='uploaded')

    def _activate(self, task, resumed):
        current = self.system.current()
        if self.active['phase'] in ('activating', 'verifying', 'rolling_back'):
            if self.target_ready(current, task):
                self.report('succeeded', 'verified', current=current)
                return
            old = self.active.get('previous')
            if self.active['phase'] == 'rolling_back' and old and restored(current, old):
                self.report('failed', 'failed', 'activation_failed', current, 'restored')
                return
            # Restart/uncertain reply: only observe. A user may have deliberately
            # changed the system model while this connector was disconnected.
            self.report('unknown', 'unknown', 'deployment_interrupted', current)
            return
        if current['bExternalHold']:
            raise ModelError('system_model_busy')
        desired = {'sModel': task['filename'], 'iEnable': 1, 'iFPS': task['fps']}
        self.guard(force=True)
        self.save(previous=current, desired=desired, phase='activating')
        self.report('running', 'activating', current=current)
        self.guard(force=True)
        observed = self.system.current()
        if (any(observed[k] != current[k] for k in ('sModel', 'iEnable', 'iFPS', 'bExternalHold'))
                or not matches_metadata(self.model_info(task['filename']), task)):
            raise ModelError('system_model_busy', uncertain=True)
        self.system.activate(desired)
        self.save(phase='verifying')
        deadline = self.clock() + 30
        while self.clock() < deadline:
            self.guard()
            current = self.system.current()
            if self.target_ready(current, task):
                self.report('succeeded', 'verified', current=current)
                return
            if any(current[k] != desired[k] for k in ('sModel', 'iEnable', 'iFPS')) or current['bExternalHold']:
                raise ModelError('system_model_busy', uncertain=True)
            if self.stop.wait(1):
                raise ModelError('deployment_interrupted')
        previous = self.active['previous']
        self.guard(force=True)
        current = self.system.current()
        if (any(current[k] != desired[k] for k in ('sModel', 'iEnable', 'iFPS')) or current['bExternalHold']
                or not matches_metadata(self.model_info(task['filename']), task)):
            raise ModelError('rollback_failed', uncertain=True)
        self.save(phase='rolling_back')
        self.system.activate(previous)
        deadline = self.clock() + 30
        observed = self.system.current()
        while self.clock() < deadline and not restored(observed, previous):
            self.guard()
            if not configuration_matches(observed, previous):
                break
            if self.stop.wait(1):
                raise ModelError('deployment_interrupted')
            observed = self.system.current()
        ok = restored(observed, previous)
        self.report('failed' if ok else 'unknown', 'failed' if ok else 'unknown',
                    'activation_failed' if ok else 'rollback_failed', observed, 'restored' if ok else 'unknown')

    def execute(self, probe, resumed):
        task = self.active['task']
        if self.active['phase'] == 'progress_conflict' and self.active.get('reported_status') != 'unknown':
            raise ModelError('deployment_interrupted', uncertain=True)
        if self.active.get('pending_report'):
            self.flush_report()
        if self.active.get('reported_status') in TERMINAL:
            self.finish_task()
            return
        if self.active.get('reported_status') == 'unknown':
            # Explicit reconciliation can confirm a running model, but unknown
            # never authorizes a second upload/configuration/activation attempt.
            current = self.system.current()
            if task['activate'] and self.target_ready(current, task):
                self.report('succeeded', 'verified', current=current)
                self.finish_task()
            elif self.active.get('phase') == 'rolling_back' and restored(current, self.active['previous']):
                self.report('failed', 'failed', 'activation_failed', current, 'restored')
                self.finish_task()
            elif not task['activate'] and matches_metadata(self.model_info(task['filename']), task):
                self.report('succeeded', 'uploaded', current=current)
                self.finish_task()
            return
        if task['contract']['algorithm'] not in probe['capabilities']['algorithms']:
            raise ModelError('system_model_incompatible')
        if self.active['phase'] in ('activating', 'verifying', 'rolling_back'):
            self._activate(task, resumed)
        else:
            if self.active['phase'] == 'downloading':
                if probe['available_bytes'] < task['size_bytes'] * 2 + 16 * 1024 * 1024:
                    raise ModelError('insufficient_storage')
                self.report('running', 'downloading')
            cache = self.cache(task)
            if self.active['phase'] == 'downloading':
                self.save(phase='upload_preparing')
            self.report('running', 'uploading')
            self._upload(task, cache, resumed)
            info = self.model_info(task['filename'])
            if not matches_metadata(info, task):
                # Existing finalized artifacts with foreign metadata are never
                # overwritten. A newly finalized upload contains only checksum
                # metadata, so configure exactly once with a durable intent.
                if (not self.active.get('owned_finalization') or self.active.get('metadata_intent')
                        or not isinstance(info, dict) or set(info) - {'md5sum', 'size'}):
                    raise ModelError('artifact_integrity_failed', uncertain=True)
                self.guard(force=True)
                if self.model_info(task['filename']) != info:
                    raise ModelError('artifact_integrity_failed', uncertain=True)
                self.save(phase='configuring', metadata_intent=True)
                self.system.configure(task)
                if not matches_metadata(self.model_info(task['filename']), task):
                    raise ModelError('verification_failed', uncertain=True)
            self.save(phase='ready')
            if task['activate']:
                self._activate(task, resumed)
            else:
                self.report('succeeded', 'uploaded', current=self.system.current())
        if self.active and self.active.get('reported_status') in TERMINAL:
            self.finish_task()

    def step(self):
        config = self.configuration()
        if config.get('system_model_schema_version') != 1:
            return
        device, project = config.get('device_id'), config.get('project_id')
        if not isinstance(device, str) or type(project) is not int or project <= 0:
            raise ModelError('credential_changed')
        owner = hashlib.sha256(json.dumps([self.client.server_url, device, project]).encode()).hexdigest()
        if self.journal is None or self.journal.data['owner'] != owner:
            self.journal = Journal(self.root, owner)
            self.lease_until = 0
        try:
            probe = self.system.probe()
            payload = {**probe, 'agent_id': self.journal.data['agent_id'], 'error_code': ''}
        except ModelError as exc:
            payload = {'agent_id': self.journal.data['agent_id'], 'capabilities': {
                'schema_version': 1, 'target_platform': 'rv1126b', 'model_api': False, 'algorithms': []},
                'current': None, 'error_code': str(exc)}
            self.client.json_request('system/status', payload)
            self.log(str(exc))
            return
        self.client.json_request('system/status', payload)
        resumed = self.active is not None
        observe_only = resumed and (self.active.get('reported_status') == 'unknown'
                                    or (self.active.get('pending_report') or {}).get('status') == 'unknown')
        claimed = self.claim(resume=resumed, observe_only=observe_only)
        if not claimed:
            return
        if claimed['status'] in TERMINAL:
            self.finish_task()
            return
        if observe_only and claimed['status'] == 'unknown':
            pending = self.active.get('pending_report')
            if pending and pending['sequence'] == claimed.get('last_sequence'):
                self.save(pending_report=None, reported_status='unknown')
        elif observe_only:
            # The initial unknown report itself lost delivery, not its ACK.
            # Renew once to deliver that exact durable report; future reads
            # then remain observe-only until positive reconciliation evidence.
            claimed = self.claim(resume=True)
            if claimed['status'] in TERMINAL:
                self.finish_task()
                return
        if not resumed:
            self.journal.data['active'] = {'task': claimed, 'phase': 'downloading',
                'sequence': claimed.get('last_sequence', 0), 'reported_status': 'running',
                'reported_stage': claimed.get('stage', 'queued')}
            self.journal.save()
        try:
            self.execute(probe, resumed)
            self.last_error = ''
        except ModelError as exc:
            self.log(str(exc))
            if observe_only:
                return
            if str(exc) in ('lease_expired', 'credential_changed', 'deployment_interrupted') and not exc.uncertain:
                return
            # Preserve the write-ahead intent across transport loss. The next
            # iteration first reconnects/renews, then reads device state. It
            # never blindly repeats a possibly committed side effect.
            if str(exc) == 'system_api_unavailable':
                return
            if self.active and self.active['phase'] == 'uploading' and self.active.get('append_pending'):
                return
            if self.active:
                uncertain = exc.uncertain or self.active['phase'] in (
                    'upload_starting', 'finalizing', 'configuring', 'activating', 'verifying', 'rolling_back')
                self.report('unknown' if uncertain else 'failed', 'unknown' if uncertain else 'failed', str(exc))
                if not uncertain:
                    self.finish_task()

    def run(self):
        while not self.stop.is_set():
            try:
                self.step()
            except DeliveryError:
                self.lease_until = 0
                self.log('lease_expired')
            except (ModelError, OSError, ValueError, KeyError, TypeError):
                self.log('device_error')
            except Exception:
                self.log('device_error')
            self.stop.wait(15)

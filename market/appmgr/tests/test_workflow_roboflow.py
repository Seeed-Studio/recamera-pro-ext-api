"""Download integrity, bounded ONNX inspection and unattended preparation."""
import hashlib
import io
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from .test_workflow_models import service, MANIFEST, staged
from appmgr import workflow_models as models, workflow_roboflow as rf, workflow_onnx as onnx
from appmgr import workflow_model_contract as contract, paths


def varint(value):
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    return bytes(result) + bytes([value])


def field(number, value):
    if isinstance(value, int):
        return varint(number << 3) + varint(value)
    if isinstance(value, str):
        value = value.encode()
    return varint(number << 3 | 2) + varint(len(value)) + value


def tensor(name, shape):
    dims = b''.join(field(1, field(1, size)) for size in shape)
    return field(1, name) + field(2, field(1, field(1, 1) + field(2, dims)))


def model_bytes(output=None, description='Ultralytics YOLOv8n model'):
    graph = field(11, tensor('images', [1, 3, 32, 32])) + field(12, tensor('output0', output or [1, 5, 21]))
    # Include a large unknown protobuf field: it must be skipped, not parsed as a tensor.
    graph += field(5, b'weights' * 10000)
    props = {'author': 'Ultralytics', 'description': description, 'task': 'detect', 'names': "{0: 'person'}"}
    return field(1, 8) + field(7, graph) + b''.join(field(14, field(1, key)+field(2, value)) for key, value in props.items())


def test_single_onnx_upload_extracts_labels_and_rejects_truncation(tmp_path):
    path = tmp_path / 'model.onnx'
    path.write_bytes(model_bytes())
    doc = onnx.metadata(path, 'uploaded/1')
    assert doc['labels'] == ['person']
    assert doc['outputs'][0]['shape'] == [1, 5, 21]
    assert doc['input']['padding_value'] == 114
    path.write_bytes(model_bytes()[:-1])
    with pytest.raises(onnx.ModelFormatError):
        onnx.metadata(path, 'uploaded/1')


@pytest.mark.parametrize('architecture,shape,decoder,objectness', [
    ('yolov5', [1, 63, 6], 'yolo-decoded', True),
    ('yolov7', [1, 6, 63], 'yolo-decoded', True),
    ('yolov5u', [1, 5, 21], 'yolo-decoded', False),
    ('yolov8', [1, 5, 21], 'yolo-decoded', False),
    ('yolov9', [1, 21, 5], 'yolo-decoded', False),
    ('yolov10', [1, 300, 6], 'yolo-end2end', False),
    ('yolo11', [1, 5, 21], 'yolo-decoded', False),
    ('yolo12', [1, 5, 21], 'yolo-decoded', False),
    ('yolo26', [1, 300, 6], 'yolo-end2end', False),
    ('yolov26', [1, 5, 21], 'yolo-decoded', False),
])
def test_detection_export_profiles_require_matching_actual_outputs(tmp_path, architecture, shape, decoder, objectness):
    path = tmp_path / 'model.onnx'; path.write_bytes(model_bytes(shape))
    doc = onnx.metadata(path, 'model/1', architecture=architecture, labels=['person'])
    assert doc['postprocess']['kind'] == decoder
    assert doc['postprocess'].get('objectness', False) is objectness
    assert doc['outputs'][0]['shape'] == shape
    path.write_bytes(model_bytes([1, 25, 30]))
    with pytest.raises(onnx.ModelFormatError, match='unsupported_model_output'):
        onnx.metadata(path, 'model/1', architecture=architecture, labels=['person'])


def test_yolo26_single_upload_reads_identity_and_end2end_output(tmp_path):
    path = tmp_path / 'model.onnx'
    path.write_bytes(model_bytes([1, 300, 6], 'Ultralytics YOLO26n model'))
    doc = onnx.metadata(path, 'model/1')
    assert doc['source_architecture'] == 'yolo26'
    assert doc['postprocess']['kind'] == 'yolo-end2end'
    assert doc['postprocess']['box_format'] == 'xyxy'
    # A one-class v5 output also has six columns but is not end-to-end.
    path.write_bytes(model_bytes([1, 63, 6], 'Ultralytics YOLOv5n model'))
    doc = onnx.metadata(path, 'model/1')
    assert doc['postprocess']['kind'] == 'yolo-decoded'
    assert doc['postprocess']['objectness'] is True


@pytest.mark.parametrize('architecture,task,error', [
    ('rfdetr', 'object-detection', 'unsupported_model_architecture'),
    ('yolo26', 'instance-segmentation', 'unsupported_model_task'),
])
def test_provider_rejects_incompatible_tasks_before_downloading(monkeypatch, architecture, task, error):
    def opened(*args):
        return io.BytesIO(json.dumps({'modelMetadata': {'modelArchitecture': architecture, 'taskType': task}}).encode())
    monkeypatch.setattr(rf, '_open', opened)
    with pytest.raises(rf.DownloadError) as exc:
        rf.resolve('model/1')
    assert str(exc.value) == error
    assert exc.value.architecture == architecture


def test_package_preprocessing_is_preserved_or_rejected(tmp_path):
    path = tmp_path / 'model.onnx'; path.write_bytes(model_bytes())
    config = {'network_input': {'training_input_size': {'height': 32, 'width': 32}, 'color_mode': 'rgb',
                               'resize_mode': 'letterbox', 'padding_value': 0, 'input_channels': 3,
                               'scaling_factor': 255, 'normalization': None},
              'post_processing': {'type': 'nms', 'fused': False}}
    assert onnx.metadata(path, 'public', architecture='yolov8', labels=['person'], configuration=config)['input']['padding_value'] == 0
    config['network_input']['resize_mode'] = 'stretch'
    with pytest.raises(onnx.ModelFormatError, match='preprocessing'):
        onnx.metadata(path, 'public', architecture='yolov8', labels=['person'], configuration=config)


@pytest.mark.parametrize('url', ['http://repo.roboflow.com/file', 'https://127.0.0.1/file',
                                 'https://roboflow.com.evil.test/file', 'https://user:pw@repo.roboflow.com/file',
                                 'https://repo.roboflow.com:8080/file'])
def test_download_rejects_untrusted_destination(url):
    with pytest.raises(rf.DownloadError):
        rf._allowed(url)


def test_checksum_failure_never_publishes_partial_model(tmp_path, monkeypatch):
    response = io.BytesIO(b'invalid'); response.headers = {'Content-Length': '7'}
    monkeypatch.setattr(rf, '_open', lambda *a: response)
    target = tmp_path / 'source'
    with pytest.raises(rf.DownloadError, match='checksum'):
        rf._download({'downloadUrl': 'https://repo.roboflow.com/model', 'md5Hash': '0'*32}, target, 1024)
    assert not target.exists() and not target.with_suffix('.part').exists()


def test_provider_selects_static_onnx_and_never_passes_api_key_to_download(monkeypatch, tmp_path):
    blobs = {'weights.onnx': model_bytes(), 'class_names.txt': b'person\n', 'inference_config.json': json.dumps({
        'network_input': {'training_input_size': {'height': 32, 'width': 32}, 'color_mode': 'rgb',
                          'resize_mode': 'letterbox', 'padding_value': 0, 'input_channels': 3,
                          'scaling_factor': 255, 'normalization': None}, 'post_processing': {'type': 'nms', 'fused': False}}).encode()}
    package = {'packageId': 'static', 'packageManifest': {'backendType': 'onnx', 'staticBatchSize': 1, 'quantization': 'fp32'},
               'packageFiles': [{'fileHandle': name, 'downloadUrl': 'https://repo.roboflow.com/'+name, 'md5Hash': hashlib.md5(blob).hexdigest()} for name, blob in blobs.items()]}
    calls = []
    def opened(url, api_key=None):
        calls.append((url, api_key))
        if url.startswith(rf.API):
            blob = json.dumps({'modelMetadata': {'modelId': 'coco/3', 'modelArchitecture': 'yolov8', 'taskType': 'object-detection',
                                                'modelPackages': [{'packageManifest': {'backendType': 'trt'}}, package]}}).encode()
        else:
            assert api_key is None
            blob = blobs[url.rsplit('/', 1)[-1]]
        response = io.BytesIO(blob); response.headers = {'Content-Length': str(len(blob))}
        return response
    monkeypatch.setattr(rf, '_open', opened)
    prepared = rf.prepare('yolov8n-640', tmp_path, 'private-key')
    assert prepared['package_id'] == 'static'
    assert prepared['metadata']['model_id'] == 'yolov8n-640'
    assert calls[0][1] == 'private-key'
    assert prepared['source']['sha256'] == hashlib.sha256(model_bytes()).hexdigest()


def save_workflow(app_id='demo', model_id='yolov8n-640'):
    directory = Path(paths.APPDATA_DIR) / app_id / 'workflows'
    contract.atomic_json(directory / (hashlib.sha256(b'saved').hexdigest()+'.json'), {
        'id': 'saved', 'name': 'Saved workflow', 'config': json.dumps({'specification': {'steps': [{'model_id': model_id}]}})})


def test_discovery_is_idempotent_and_cancel_does_not_create_new_cloud_job(service):
    manager, _ = service
    save_workflow()
    manager.discover(MANIFEST, 'demo'); manager.discover(MANIFEST, 'demo')
    task, = manager._jobs('demo')
    assert task['mode'] == 'roboflow' and task['model_id'] == 'yolov8n-640'
    assert manager.dependency('demo', MANIFEST)['available'] is False
    manager.action('demo', task['id'], 'cancel', {})
    manager.discover(MANIFEST, 'demo')
    assert len(manager._jobs('demo')) == 1
    assert manager._jobs('demo')[0]['state'] == 'cancelled'
    manager.action('demo', task['id'], 'resume', {})
    assert manager._jobs('demo')[0]['state'] == 'queued'


def test_automatic_download_waits_for_login_without_losing_source(service, monkeypatch):
    manager, doc = service
    task = manager.create(MANIFEST, 'demo', {'mode': 'roboflow', 'model_id': 'detector/2'})
    def prepared(identifier, directory, key):
        (directory/'source').write_bytes(b'onnx')
        return {'metadata': doc, 'source': {'sha256': 'digest', 'size': 4},
                'resolved_model_id': identifier, 'package_id': 'static', 'architecture': 'yolov8'}
    monkeypatch.setattr(models.roboflow, 'prepare', Mock(side_effect=prepared))
    monkeypatch.setattr(models.cloud, 'credentials', Mock(side_effect=models.cloud.LoginRequired))
    task = manager._jobs('demo')[0]; manager._step(task)
    assert task['state'] == 'awaiting_login'
    assert (manager._path('demo', task['id'])/'source').read_bytes() == b'onnx'
    manager._step(task)
    assert models.roboflow.prepare.call_count == 1
    assert task['cloud_id'] == ''


def test_auto_upload_needs_only_a_file(service, monkeypatch):
    manager, _ = service
    task = manager.create(MANIFEST, 'demo', {'mode': 'auto', 'filename': 'my-model.onnx'})
    assert task['model_id'] == 'my-model/1'
    blob = model_bytes()
    manager.upload('demo', task['id'], 'source', io.BytesIO(blob), len(blob))
    manager.action('demo', task['id'], 'resume', {})
    monkeypatch.setattr(models.cloud, 'credentials', Mock(side_effect=models.cloud.LoginRequired))
    task = manager._jobs('demo')[0]; manager._step(task)
    assert task['state'] == 'awaiting_login' and task['metadata']['labels'] == ['person']


def test_registered_automatic_model_activation_is_once_and_retries_busy(service):
    manager, _ = service
    task, _ = staged(service)
    task['auto_activate'] = True
    manager._step(task)
    manager.activate = Mock(side_effect=[False, True])
    manager._activate_registered(); manager._activate_registered(); manager._activate_registered()
    assert manager.activate.call_count == 2
    assert manager._jobs('demo')[0]['activation_done'] is True


def rknn_header(attrs):
    import struct
    doc = repr({'attrs': attrs}).encode()
    return b'RKNN\0\0\0\0' + struct.pack('<Q', 6) + b'\0' * 64 + struct.pack('<I', len(doc)) + doc + b'\0'


def test_conversion_metadata_handles_sensecraft_yolo_head_rewrite(tmp_path):
    path = tmp_path/'source'; path.write_bytes(model_bytes())
    prepared = onnx.metadata(path, 'detector/1')
    attrs = {'images': {'is_output': False, 'shape': [1, 3, 32, 32], 'mean': [0, 0, 0], 'std': [255, 255, 255], 'rgb2bgr': False}}
    for scale, grid in enumerate((4, 2, 1)):
        for branch, channels in ((2, 64), (3, 1)):
            name = f'/model.22/cv{branch}.{scale}/cv{branch}.{scale}.2/Conv_output_0'
            attrs[name] = {'is_output': True, 'idx': scale*2 + branch-2, 'shape': [1, channels, grid, grid], 'dtype': 'float32', 'layout': 'nchw'}
    asset = tmp_path/'model.rknn'; asset.write_bytes(rknn_header(attrs))
    actual = onnx.converted_metadata(asset, prepared)
    assert actual['postprocess'] == {'kind': 'yolo-dfl', 'scores': 'logits', 'reg_max': 16}
    assert [output['role'] for output in actual['outputs']] == ['boxes', 'scores'] * 3
    attrs['images']['std'] = [1, 1, 1]
    asset.write_bytes(rknn_header(attrs))
    with pytest.raises(onnx.ModelFormatError, match='unsupported_rknn_metadata'):
        onnx.converted_metadata(asset, prepared)


@pytest.mark.parametrize('head,nms', [('one2one_', False), ('one2many_', True), ('', False)])
@pytest.mark.parametrize('architecture', ['yolo26', 'yolov10'])
def test_converted_raw_heads_preserve_head_semantics(tmp_path, head, nms, architecture):
    path = tmp_path/'source'; path.write_bytes(model_bytes([1, 300, 6], f'Ultralytics {architecture}n model'))
    prepared = onnx.metadata(path, 'detector/1')
    if nms:
        prepared['postprocess'] = {'kind': 'yolo-decoded', 'scores': 'probabilities', 'box_format': 'xywh'}
        prepared['outputs'] = [{'name': 'output0', 'shape': [1, 5, 21], 'layout': 'BCN', 'dtype': 'float32'}]
    attrs = {'images': {'is_output': False, 'shape': [1, 3, 32, 32], 'mean': [0, 0, 0], 'std': [255, 255, 255], 'rgb2bgr': False}}
    for scale, grid in enumerate((4, 2, 1)):
        for branch, channels in ((2, 4 if architecture == 'yolo26' else 64), (3, 1)):
            name = f'/model.23/{head}cv{branch}.{scale}/cv{branch}.{scale}.2/Conv_output_0'
            attrs[name] = {'is_output': True, 'idx': scale*2 + branch-2, 'shape': [1, channels, grid, grid], 'dtype': 'float32', 'layout': 'nchw'}
    asset = tmp_path/'model.rknn'; asset.write_bytes(rknn_header(attrs))
    actual = onnx.converted_metadata(asset, prepared)
    expected = {'kind': 'yolo-distance', 'scores': 'logits', 'nms': nms} if architecture == 'yolo26' else {'kind': 'yolo-dfl', 'scores': 'logits', 'reg_max': 16}
    if not nms:
        expected.update(nms=False, topk=300)
    assert actual['postprocess'] == expected
    if not nms:
        with pytest.raises(onnx.ModelFormatError, match='unsupported_rknn_metadata'):
            onnx.converted_metadata(asset, {**prepared, 'source_architecture': 'yolov8'})
    if head == 'one2many_':
        prepared['postprocess'] = {'kind': 'yolo-end2end', 'scores': 'probabilities', 'box_format': 'xyxy'}
        with pytest.raises(onnx.ModelFormatError, match='unsupported_rknn_metadata'):
            onnx.converted_metadata(asset, prepared)
    # A raw class branch cannot silently be interpreted as a box branch.
    attrs[next(k for k in attrs if 'cv2.0/' in k)]['shape'][1] = 1
    asset.write_bytes(rknn_header(attrs))
    with pytest.raises(onnx.ModelFormatError, match='unsupported_rknn_metadata'):
        onnx.converted_metadata(asset, prepared)


def test_converted_end2end_preserves_explicit_xyxy_contract(tmp_path):
    path = tmp_path/'source'; path.write_bytes(model_bytes([1, 300, 6], 'Ultralytics YOLO26n model'))
    prepared = onnx.metadata(path, 'detector/1')
    attrs = {'images': {'is_output': False, 'shape': [1, 3, 32, 32], 'mean': [0, 0, 0], 'std': [255, 255, 255], 'rgb2bgr': False},
             'detections': {'is_output': True, 'idx': 0, 'shape': [1, 300, 6], 'dtype': 'float32', 'layout': 'nchw'}}
    asset = tmp_path/'model.rknn'; asset.write_bytes(rknn_header(attrs))
    actual = onnx.converted_metadata(asset, prepared)
    assert actual['postprocess']['kind'] == 'yolo-end2end'
    assert actual['outputs'][0]['name'] == 'detections'
    asset.write_bytes(b'RKNN' + b'bad metadata')
    with pytest.raises(onnx.ModelFormatError):
        onnx.converted_metadata(asset, prepared)

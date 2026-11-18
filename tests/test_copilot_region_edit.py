"""Offline restoration checks. Solid-color model doubles are NOT AI results."""
import base64
import io
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'backend')]
import presentation_copilot as runner
from services.ai_providers import copilot_limits as limits



@pytest.fixture(autouse=True)
def isolated_budget(monkeypatch, tmp_path):
    monkeypatch.setattr(limits, '_root', lambda: tmp_path)
    monkeypatch.setenv('PC_IMAGE_OPERATIONS', 'text_generate')


def png(color='white'):
    buffer = io.BytesIO()
    Image.new('RGB', (400, 300), color).save(buffer, format='PNG')
    buffer.seek(0)
    return buffer


def payload(**overrides):
    values = dict(operation='region_edit', prompt='离线测试：把选区改成蓝色',
                  apply_mode='overlay_selection', source_image=(png(), '目标.png'),
                  selection=json.dumps(dict(x=100, y=80, width=200, height=140,
                                            image_width=400, image_height=300)))
    values.update(overrides)
    return values


def test_reference_images_use_existing_gemini_request_and_budget(monkeypatch, tmp_path):
    from google import genai
    from google.genai import types
    from services.ai_providers.image.genai_provider import GenAIImageProvider
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={'candidates': [{'content': {'role': 'model', 'parts': [
            {'inlineData': {'mimeType': 'image/png', 'data': base64.b64encode(png('blue').getvalue()).decode()}}]}}]})
    for key, value in runner.PRESET.items(): monkeypatch.setenv(key, value)
    monkeypatch.setenv('IMAGE_API_KEY', 'offline-test-only')
    monkeypatch.setenv('PC_BUDGET_CREDITS', '20')
    monkeypatch.setenv('PC_MODE', 'live')
    monkeypatch.setenv('PC_BUDGET_CREDITS', '20')
    monkeypatch.setenv('PC_REGION_EDIT_ENABLED', '1')
    monkeypatch.setattr(limits, '_root', lambda: tmp_path)
    provider = GenAIImageProvider(model=limits.MODELS['image'], api_key='offline', api_base='https://tokenflux.dev')
    provider.client.close()
    provider.client = genai.Client(api_key='offline', http_options=types.HttpOptions(
        base_url='https://tokenflux.dev', retry_options=types.HttpRetryOptions(attempts=1),
        client_args={'transport': httpx.MockTransport(respond)}))
    refs = [Image.new('RGB', (40, 30), 'white'), Image.new('RGB', (40, 30), 'red')]
    with pytest.raises(RuntimeError, match='参考图|authorization_required'):
        provider.generate_image('offline', ref_images=refs, enable_thinking=False)
    with limits.image_operation_scope('material_region_edit', ['validated-source', 'validated-marked']):
        monkeypatch.setenv('PC_REGION_EDIT_ENABLED', '0')
        with pytest.raises(RuntimeError, match='参考图|authorization_required'):
            provider.generate_image('offline', ref_images=refs, enable_thinking=False)
        monkeypatch.setenv('PC_REGION_EDIT_ENABLED', '1')
        assert provider.generate_image('offline', ref_images=refs, enable_thinking=False).size == (400, 300)
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    assert requests[0].url.path == '/v1beta/models/nano-banana-2:generateContent'
    assert sum('inlineData' in part for part in body['contents'][0]['parts']) == 2
    assert body['generationConfig']['imageConfig']['imageSize'] == '2K'
    state = json.loads((tmp_path / 'call-budget.json').read_text())
    assert state['image'] == 1 and state['reserved_credits'] == 4
    monkeypatch.setenv('PC_BUDGET_CREDITS', '4')
    with limits.image_operation_scope('material_region_edit', ['validated-source', 'validated-marked']), pytest.raises(RuntimeError, match='上限'):
        provider.generate_image('offline', ref_images=refs, enable_thinking=False)
    assert len(requests) == 1
    monkeypatch.setenv('PC_BUDGET_CREDITS', '20')
    with pytest.raises(RuntimeError, match='参考图|authorization_required'):
        provider.generate_image('offline', ref_images=refs, enable_thinking=False)
    provider.client.close()


def test_region_cli_option_cannot_enable_models_on_its_own():
    with pytest.raises(RuntimeError, match='预算'):
        runner.prepare_environment(False, 0, region_edit=True)


@pytest.fixture(scope='module')
def app(tmp_path_factory):
    folder = tmp_path_factory.mktemp('region')
    os.environ['DATABASE_PATH'] = str(folder / 'database.db')
    os.environ['UPLOAD_FOLDER'] = str(folder / 'uploads')
    return runner.create_local_app(False, 5011)


@pytest.mark.parametrize('mode,ready,enabled', [('preview', '1', '1'), ('live', '1', '0'), ('live', '0', '1')])
def test_gate_rejects_before_task_file_or_model(app, monkeypatch, mode, ready, enabled):
    from controllers import material_controller as controller
    from models import Task
    monkeypatch.setenv('PC_MODE', mode)
    monkeypatch.setenv('PC_CONFIG_READY', ready)
    monkeypatch.setenv('IMAGE_API_KEY', 'offline-test-only' if ready == '1' else '')
    monkeypatch.setenv('PC_REGION_EDIT_ENABLED', enabled)
    monkeypatch.setattr(controller, 'get_ai_service', lambda: pytest.fail('Must reject before model setup'))
    with app.app_context():
        before = Task.query.count()
    upload = Path(app.config['UPLOAD_FOLDER'])
    files_before = set(upload.rglob('*'))
    client = app.test_client()
    assert client.get('/api/product/region-edit/status').json['data'] == {
        'available': False, 'message': '生成服务暂不可用，请稍后重试。'}
    assert client.post('/api/projects/none/materials/process', data=payload()).status_code == 503
    assert set(upload.rglob('*')) == files_before
    with app.app_context():
        assert Task.query.count() == before


@pytest.fixture
def offline_authorized(monkeypatch):
    for key, value in runner.PRESET.items(): monkeypatch.setenv(key, value)
    monkeypatch.setenv('IMAGE_API_KEY', 'offline-test-only')
    monkeypatch.setenv('PC_BUDGET_CREDITS', '20')
    monkeypatch.setenv('PC_MODE', 'live')
    monkeypatch.setenv('PC_CONFIG_READY', '1')
    monkeypatch.setenv('PC_REGION_EDIT_ENABLED', '1')
    import psutil
    monkeypatch.setattr(psutil, 'virtual_memory', lambda: SimpleNamespace(available=8 * 2**30))


@pytest.mark.parametrize('bad', [
    {'operation': 'edit_full'}, {'operation': 'erase_region'}, {'model': 'not-allowed'},
    {'selection': '{"x":-1}'},
    {'selection': json.dumps(dict(x=300, y=0, width=200, height=30, image_width=400, image_height=300))},
    {'source_image': (io.BytesIO(b'invalid image'), 'broken.png')},
])
def test_invalid_requests_never_enqueue(app, monkeypatch, offline_authorized, bad):
    from controllers import material_controller as controller
    monkeypatch.setattr(controller, 'get_ai_service', lambda: pytest.fail('Invalid request reached model setup'))
    assert app.test_client().post('/api/projects/none/materials/process', data=payload(**bad)).status_code == 400


@pytest.mark.parametrize('apply_mode', ['overlay_selection', 'replace_full'])
def test_original_task_saves_material_and_pollable_result(app, monkeypatch, offline_authorized, apply_mode):
    from controllers import material_controller as controller
    from models import Task, Material
    calls = []
    def fake_edit(**kwargs):
        calls.append(kwargs)
        assert Path(kwargs['current_image_path']).is_file()
        assert len(kwargs['additional_ref_images']) == 1  # source + generated marked image in AIService
        assert Path(kwargs['additional_ref_images'][0]).is_file()
        return Image.new('RGB', (400, 300), 'blue')
    monkeypatch.setattr(controller, 'get_ai_service', lambda: SimpleNamespace(edit_image=fake_edit))
    monkeypatch.setattr(controller.task_manager, 'submit_task', lambda task_id, fn, *args: fn(task_id, *args))
    client = app.test_client()
    response = client.post('/api/projects/none/materials/process', data=payload(apply_mode=apply_mode))
    assert response.status_code == 202
    task_id = response.json['data']['task_id']
    with app.app_context():
        task = Task.query.get(task_id)
        assert task.status == 'COMPLETED'
        progress = task.get_progress()
        material = Material.query.get(progress['material_id'])
        result = Image.open(Path(app.config['UPLOAD_FOLDER']) / material.relative_path)
        assert result.size == (400, 300) and result.getpixel((200, 150)) == (0, 0, 255)
        assert result.getpixel((0, 0)) == ((255, 255, 255) if apply_mode == 'overlay_selection' else (0, 0, 255))
        assert client.get(progress['image_url']).status_code == 200
    assert client.get(f'/api/projects/global/tasks/{task_id}').json['data']['status'] == 'COMPLETED'
    assert len(calls) == 1

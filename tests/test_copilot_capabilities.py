"""PC-004: real routes/workers, isolated DB/files/budget, synthetic Provider only."""
import io
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import httpx
ORIGINAL_HTTP_SEND = httpx.Client.send


@pytest.mark.parametrize('fail', [False, True])
def test_outline_route_progress_and_preserved_saved_page(app, page_data, monkeypatch, fail):
    from test_copilot_stream_timeout import provider, Stream, chunk, wait_worker
    from controllers import project_controller
    from services.ai_service import AIService
    from models import db, Project, Page
    import threading
    release = threading.Event()
    project_id, page_id, old_image = page_data
    with app.app_context():
        project = db.session.get(Project, project_id)
        project.idea_prompt = 'PC-005 offline test'
        db.session.commit()
    monkeypatch.setenv('PC_TEXT_TOTAL_TIMEOUT_SECONDS', '0.1')
    def create(**kwargs):
        if fail: release.wait(2)
        return Stream([chunk('## 离线模拟页\n- 测试\n<!-- PAGE_END -->\n<!-- END -->\n')])
    service = AIService(text_provider=provider(create), image_provider=SimpleNamespace(), caption_provider=SimpleNamespace())
    monkeypatch.setattr(project_controller, 'get_ai_service', lambda: service)
    try:
        response = app.test_client().post(f'/api/projects/{project_id}/generate/outline/stream', json={'language': 'zh'})
        body = response.get_data(as_text=True)
        assert 'event: progress' in body
        if fail:
            assert 'event: error' in body and 'event: done' not in body
            with app.app_context():
                page = db.session.get(Page, page_id)
                assert page.get_outline_content()['title'] == 'PC-004 mock'
                assert page.generated_image_path == old_image
            assert limits.budget_state()['paused']
        else:
            assert 'event: done' in body and 'event: error' not in body
            with app.app_context():
                page = db.session.get(Page, page_id)
                assert page.get_outline_content()['title'] == '离线模拟页'
    finally:
        release.set()
        wait_worker()
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'backend')]
import presentation_copilot as runner
from services.ai_providers import copilot_limits as limits


def png(color='white', size=(400, 300)):
    data = io.BytesIO()
    Image.new('RGB', size, color).save(data, format='PNG')
    data.seek(0)
    return data


@pytest.fixture(scope='module')
def app(tmp_path_factory):
    folder = tmp_path_factory.mktemp('pc004-app')
    # Module app creation runs before the per-test isolated fixture. Bootstrap
    # only synthetic settings here, without discovering any local .env file.
    import dotenv
    with pytest.MonkeyPatch.context() as bootstrap:
        bootstrap.setattr(dotenv, 'load_dotenv', lambda *args, **kwargs: False)
        values = {**runner.PRESET, 'DATABASE_PATH': str(folder / 'database.db'),
                  'UPLOAD_FOLDER': str(folder / 'uploads'), 'PC_MODE': 'preview',
                  'OPENAI_API_KEY': 'offline-placeholder', 'GOOGLE_API_KEY': 'offline-placeholder',
                  'IMAGE_CAPTION_MODEL_SOURCE': 'openai', 'IMAGE_CAPTION_MODEL': 'disabled',
                  'MAX_IMAGE_WORKERS': '1', 'MAX_DESCRIPTION_WORKERS': '1'}
        for key, value in values.items():
            bootstrap.setenv(key, value)
        application = runner.create_local_app(False, 5011)
    # These existing tests exercise the inner capability chain. PC-008 separately
    # exercises that same chain with real independent visitor cookies and CSRF.
    application.config['PC_VISITOR_ENABLED'] = False
    return application


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(limits, '_root', lambda: tmp_path / 'budget')
    for key, value in runner.PRESET.items():
        monkeypatch.setenv(key, value)
    for key, value in {'PC_MODE': 'live', 'PC_MODEL_ROLES': 'text,image', 'PC_CONFIG_READY': '1', 'PC_BUDGET_CREDITS': '20',
                       'TEXT_API_KEY': 'offline-test-only', 'IMAGE_API_KEY': 'offline-test-only',
                       'PC_IMAGE_OPERATIONS': ','.join(limits.IMAGE_OPERATIONS)}.items():
        monkeypatch.setenv(key, value)
    import psutil
    monkeypatch.setattr(psutil, 'virtual_memory', lambda: SimpleNamespace(available=8 * 2**30))


@pytest.fixture
def models(app, monkeypatch):
    from services.ai_service import AIService
    from controllers import page_controller, project_controller, material_controller
    from services.task_manager import task_manager
    calls = []
    class Provider:
        model = limits.MODELS['image']
        fail = False
        @limits.model_call('image')
        def generate_image(self, prompt, ref_images=None, aspect_ratio='16:9', resolution='2K', **kwargs):
            calls.append({'prompt': prompt, 'pixels': [image.copy() for image in ref_images or []],
                          'operation': limits._image_operation.get()[0], 'resolution': resolution,
                          'aspect_ratio': aspect_ratio})
            if self.fail:
                raise TimeoutError('synthetic timeout; no request was sent')
            return Image.new('RGB', (400, 300), 'blue')
    provider = Provider()
    service = AIService(text_provider=SimpleNamespace(), image_provider=provider, caption_provider=SimpleNamespace())
    for controller in (page_controller, project_controller, material_controller):
        monkeypatch.setattr(controller, 'get_ai_service', lambda: service)
    monkeypatch.setattr(task_manager, 'submit_task', lambda task_id, fn, *args, **kwargs: fn(task_id, *args, **kwargs))
    return provider, calls


@pytest.fixture
def page_data(app):
    from models import db, Project, Page
    from services import FileService
    from services.task_manager import save_image_with_version
    with app.app_context():
        project = Project(creation_type='blank', template_style='PC-004 synthetic test only')
        db.session.add(project); db.session.flush()
        page = Page(project_id=project.id, order_index=0)
        page.set_outline_content({'title': 'PC-004 mock', 'points': ['42']})
        page.set_description_content({'text': 'PC-004 synthetic; no AI output'})
        db.session.add(page); db.session.commit()
        fs = FileService(app.config['UPLOAD_FOLDER'])
        old, _ = save_image_with_version(Image.new('RGB', (400, 300), 'white'), project.id, page.id, fs, page)
        return project.id, page.id, old


@pytest.mark.parametrize('change,reason', [
    ({'PC_MODE': 'preview'}, 'preview'), ({'IMAGE_API_KEY': ''}, 'configuration_missing'),
    ({'PC_BUDGET_CREDITS': '0'}, 'authorization_required'),
    ({'PC_IMAGE_OPERATIONS': 'text_generate'}, 'authorization_required'),
    ({'PC_BUDGET_CREDITS': '3'}, 'budget_exhausted'),
    ({'IMAGE_MODEL_SOURCE': 'openai'}, 'provider_unsupported'),
])
def test_gate_never_calls_provider(app, models, page_data, monkeypatch, change, reason):
    project, page, old = page_data
    for key, value in change.items(): monkeypatch.setenv(key, value)
    client = app.test_client()
    status = client.get('/api/product/status').json['data']['capabilities']['slide_edit']
    assert status['reason'] == reason and not status['available']
    response = client.post(f'/api/projects/{project}/pages/{page}/edit/image', json={'edit_instruction': 'edit'})
    assert response.status_code == 503 and response.json['error']['code'] == reason
    assert not models[1]
    assert not (limits._root() / 'call-budget.json').exists()


@pytest.mark.parametrize('template_mode', ['single', 'multi'])
def test_template_uses_actual_route_worker_and_exact_reference(app, models, page_data, template_mode):
    from models import db, Project, Page, ProjectTemplateAsset, Task, PageImageVersion
    project, page, old = page_data
    path = Path(app.config['UPLOAD_FOLDER']) / project / 'templates/test.png'
    path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(png('red').getvalue())
    with app.app_context():
        obj = Project.query.get(project)
        if template_mode == 'single':
            obj.template_image_path = str(path.relative_to(app.config['UPLOAD_FOLDER']))
        else:
            asset = ProjectTemplateAsset(project_id=project, image_path=str(path.relative_to(app.config['UPLOAD_FOLDER'])))
            db.session.add(asset); db.session.flush()
            Page.query.get(page).template_asset_id = asset.id
        db.session.commit()
    response = app.test_client().post(f'/api/projects/{project}/pages/{page}/generate/image',
                                     json={'force_regenerate': True})
    assert response.status_code == 202
    with app.app_context():
        assert Task.query.get(response.json['data']['task_id']).status == 'COMPLETED'
        assert PageImageVersion.query.filter_by(page_id=page).count() == 2
    call = models[1][0]
    assert call['operation'] == 'template_generate' and call['resolution'] == '2K'
    assert len(call['pixels']) == 1 and call['pixels'][0].getpixel((0, 0)) == (255, 0, 0)
    assert Path(app.config['UPLOAD_FOLDER'], old).is_file()


@pytest.mark.parametrize('with_reference', [False, True])
def test_slide_edit_versions_and_material_semantics(app, models, page_data, with_reference):
    from models import Task, Page, PageImageVersion, Material
    project, page, old = page_data
    client = app.test_client()
    with app.app_context(): before_materials = Material.query.count()
    url = f'/api/projects/{project}/pages/{page}/edit/image'
    if with_reference:
        response = client.post(url, data={'edit_instruction': 'PC004 crop reference',
                                          'context_images': (png('red'), '选区.png')})
    else:
        response = client.post(url, json={'edit_instruction': 'PC004 number 42 -> 43'})
    assert response.status_code == 202
    with app.app_context():
        task = Task.query.get(response.json['data']['task_id'])
        assert task.status == 'COMPLETED', task.error_message
        assert Material.query.count() == before_materials
        assert PageImageVersion.query.filter_by(page_id=page).count() == 2
        assert PageImageVersion.query.filter_by(page_id=page, is_current=True).count() == 1
        result_path = Page.query.get(page).generated_image_path
        assert result_path != old
        assert Image.open(Path(app.config['UPLOAD_FOLDER'], result_path)).getpixel((200, 150)) == (0, 0, 255)
    call = models[1][0]
    assert call['operation'] == 'slide_edit' and 'PC004' in call['prompt']
    assert len(call['pixels']) == (2 if with_reference else 1)
    assert call['pixels'][0].getpixel((0, 0)) == (255, 255, 255)
    if with_reference: assert call['pixels'][1].getpixel((0, 0)) == (255, 0, 0)
    assert Image.open(Path(app.config['UPLOAD_FOLDER'], old)).getpixel((0, 0)) == (255, 255, 255)


@pytest.mark.parametrize('apply_mode', ['overlay_selection', 'replace_full'])
def test_material_region_saves_new_material_without_changing_page(app, models, page_data, apply_mode):
    from models import Task, Material, Page, PageImageVersion
    project, page, old = page_data
    response = app.test_client().post(f'/api/projects/{project}/materials/process', data={
        'operation': 'region_edit', 'prompt': 'PC004 mock region', 'apply_mode': apply_mode,
        'source_image': (png(), 'source.png'),
        'selection': json.dumps(dict(x=100, y=80, width=200, height=140, image_width=400, image_height=300))})
    assert response.status_code == 202
    with app.app_context():
        task = Task.query.get(response.json['data']['task_id'])
        assert task.status == 'COMPLETED', task.error_message
        material = Material.query.get(task.get_progress()['material_id'])
        result = Image.open(Path(app.config['UPLOAD_FOLDER'], material.relative_path))
        assert result.getpixel((200, 150)) == (0, 0, 255)
        assert result.getpixel((0, 0)) == ((255, 255, 255) if apply_mode == 'overlay_selection' else (0, 0, 255))
        assert Page.query.get(page).generated_image_path == old
        assert PageImageVersion.query.filter_by(page_id=page).count() == 1
    call = models[1][0]
    assert call['operation'] == 'material_region_edit' and len(call['pixels']) == 2
    assert call['pixels'][0].tobytes() != call['pixels'][1].tobytes()


@pytest.mark.parametrize('kind', ['external', 'traversal', 'wrong-project', 'no-image', 'too-many', 'broken', 'oversized'])
def test_invalid_page_inputs_rejected_before_provider(app, models, page_data, kind):
    from models import db, Page
    project, page, old = page_data
    url = f'/api/projects/{project}/pages/{page}/edit/image'
    body = {'edit_instruction': 'mock'}
    multipart = False
    if kind == 'external': body['context_images'] = {'desc_image_urls': ['https://invalid.example/image.png']}
    if kind == 'traversal': body['context_images'] = {'desc_image_urls': ['../../secret.png']}
    if kind == 'wrong-project': url = f'/api/projects/other-project/pages/{page}/edit/image'
    if kind == 'no-image':
        with app.app_context():
            Page.query.get(page).generated_image_path = None; db.session.commit()
    if kind == 'too-many':
        multipart = True; body['context_images'] = [(png(), f'{i}.png') for i in range(3)]
    if kind == 'broken':
        multipart = True; body['context_images'] = (io.BytesIO(b'not image'), 'bad.png')
    if kind == 'oversized':
        multipart = True; body['context_images'] = (png(size=(5001, 4000)), 'huge.png')
    response = app.test_client().post(url, **({'data': body} if multipart else {'json': body}))
    assert response.status_code in (400, 404, 413)
    assert not models[1]


def test_failure_preserves_image_version_and_pauses_budget(app, models, page_data):
    from models import Task, Page, PageImageVersion
    project, page, old = page_data
    models[0].fail = True
    client = app.test_client()
    response = client.post(f'/api/projects/{project}/pages/{page}/edit/image', json={'edit_instruction': 'mock fail'})
    with app.app_context():
        task = Task.query.get(response.json['data']['task_id'])
        assert task.status == 'FAILED' and task.get_progress()['completed'] == 0
        assert Page.query.get(page).generated_image_path == old
        assert PageImageVersion.query.filter_by(page_id=page).count() == 1
    status = client.get('/api/product/status').json['data']['capabilities']['slide_edit']
    assert status['reason'] == 'previous_failure' and not status['available']
    assert len(models[1]) == 1


def test_empty_model_result_pauses_budget():
    class EmptyImage:
        model = limits.MODELS['image']
        @limits.model_call('image')
        def generate(self, prompt):
            return None
    with pytest.raises(RuntimeError, match='响应为空'):
        EmptyImage().generate('offline')
    assert limits.budget_state()['paused'] is True
    assert limits.budget_state()['image'] == 1


def test_missing_bound_template_never_falls_back_to_text_style(app, models, page_data):
    from models import db, Project
    project, page, _ = page_data
    with app.app_context():
        Project.query.get(project).template_image_path = f'{project}/missing.png'
        db.session.commit()
    response = app.test_client().post(f'/api/projects/{project}/pages/{page}/generate/image', json={'force_regenerate': True})
    assert response.status_code == 503
    assert not models[1]


def test_status_language_and_management_are_separate(app, models):
    client = app.test_client()
    body = client.get('/api/product/status').json
    assert body['data']['capabilities']['slide_edit']['available']
    assert body['data']['capabilities']['slide_edit']['verified'] is False
    serialized = json.dumps(body)
    for secret in ('offline-test-only', 'tokenflux', 'api_key', 'api_base', 'Bearer'):
        assert secret not in serialized
    assert client.put('/api/product/preferences', json={'output_language': 'ja'}).status_code == 200
    assert client.get('/api/output-language').json['data']['language'] == 'ja'
    assert client.put('/api/product/preferences', json={'output_language': 'invalid'}).status_code == 400
    for method, path in [('GET', '/api/settings'), ('PUT', '/api/settings'), ('POST', '/api/settings/reset'),
                         ('POST', '/api/settings/tests/image-model'), ('GET', '/api/settings/openai-oauth/authorize')]:
        assert client.open(path, method=method).status_code == 403
    assert client.put('/api/product/preferences', json={'api_key': 'invalid'}).status_code == 400
    assert not models[1]


@pytest.mark.parametrize('fail', [False, True])
def test_batch_worker_keeps_context_and_failure_status(app, models, page_data, fail):
    from models import db, Project, Task, Page
    project, page, old = page_data
    path = Path(app.config['UPLOAD_FOLDER']) / project / 'templates/batch.png'
    path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(png('red').getvalue())
    with app.app_context():
        Project.query.get(project).template_image_path = str(path.relative_to(app.config['UPLOAD_FOLDER']))
        db.session.commit()
    models[0].fail = fail
    response = app.test_client().post(f'/api/projects/{project}/generate/images', json={'page_ids': [page]})
    assert response.status_code == 202
    with app.app_context():
        task = Task.query.get(response.json['data']['task_id'])
        assert task.status == ('FAILED' if fail else 'COMPLETED')
        assert task.get_progress()['failed'] == int(fail)
        assert task.get_progress()['completed'] == int(not fail)
        if fail: assert Page.query.get(page).generated_image_path == old
    assert models[1][0]['operation'] == 'template_generate'


def test_bound_template_cannot_read_another_project(app, models, page_data):
    from models import db, ProjectTemplateAsset, Page
    project, page, _ = page_data
    with app.app_context():
        asset = ProjectTemplateAsset(project_id='foreign-project', image_path='../not-allowed.png')
        db.session.add(asset); db.session.flush()
        Page.query.get(page).template_asset_id = asset.id; db.session.commit()
    response = app.test_client().post(f'/api/projects/{project}/pages/{page}/generate/image', json={'force_regenerate': True})
    assert response.status_code == 503
    assert not models[1]


def test_sdk_serializes_template_and_page_edit_references_without_network(monkeypatch, tmp_path):
    import base64
    import httpx
    from google import genai
    from google.genai import types
    from services.ai_providers.image.genai_provider import GenAIImageProvider
    sent = []
    def respond(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={'candidates': [{'content': {'role': 'model', 'parts': [
            {'inlineData': {'mimeType': 'image/png', 'data': base64.b64encode(png('blue').getvalue()).decode()}}
        ]}}]})
    # Remove only the preview HTTP wrapper for an in-memory MockTransport. pytest-socket remains disabled.
    provider = GenAIImageProvider(model=limits.MODELS['image'], api_key='offline-test-only', api_base='https://tokenflux.dev')
    provider.client.close()
    provider.client = genai.Client(api_key='offline-test-only', http_options=types.HttpOptions(
        base_url='https://tokenflux.dev', retry_options=types.HttpRetryOptions(attempts=1),
        client_args={'transport': httpx.MockTransport(respond)}))
    # create_local_app has installed a HTTP guard; use the mock transport's send only in this test.
    monkeypatch.setattr(httpx.Client, 'send', ORIGINAL_HTTP_SEND)
    for operation, count in [('template_generate', 1), ('slide_edit', 2)]:
        with limits.image_operation_scope(operation, [f'test-image-{i}' for i in range(count)]):
            result = provider.generate_image('synthetic request', ref_images=[Image.new('RGB', (40, 30), 'red') for _ in range(count)], enable_thinking=False)
        assert result.size == (400, 300)
        parts = sent[-1]['contents'][0]['parts']
        assert sum('inlineData' in part for part in parts) == count
        assert sent[-1]['generationConfig']['imageConfig']['imageSize'] == '2K'
    assert len(sent) == 2
    provider.client.close()

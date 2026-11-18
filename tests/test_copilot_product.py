"""PC-002 config boundaries and ordinary draft editing; no paid requests."""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'backend')]
import presentation_copilot as runner


@pytest.fixture(scope='module')
def app(tmp_path_factory):
    folder = tmp_path_factory.mktemp('product')
    os.environ['DATABASE_PATH'] = str(folder / 'database.db')
    os.environ['UPLOAD_FOLDER'] = str(folder / 'uploads')
    result = runner.create_local_app(False, 5011)
    yield result


@pytest.mark.parametrize('method,path', [
    ('GET', '/api/settings'), ('PUT', '/api/settings'), ('POST', '/api/settings/reset'),
    ('GET', '/api/settings/active-config'), ('POST', '/api/settings/verify'),
    ('POST', '/api/settings/tests/text-model'), ('POST', '/api/settings/tests/image-model'),
    ('GET', '/api/settings/openai-oauth/authorize'), ('POST', '/api/settings/openai-oauth/manual-callback'),
])
def test_no_public_settings_or_connection_probes(app, method, path):
    response = app.test_client().open(path, method=method, json={'text_api_key': 'fake-unusable-test-value'})
    assert response.status_code == 403
    assert set(response.json) == {'error'}


def test_status_is_minimal_and_preferences_stay_usable(app):
    client = app.test_client()
    status = client.get('/api/product/status').json['data']
    assert status['available'] is False
    assert status['capabilities']['text']['reason'] == 'preview'
    assert 'api_key' not in str(status) and 'api_base' not in str(status)
    response = client.put('/api/product/preferences', json={'description_extra_fields': ['页面重点']})
    assert response.status_code == 200
    assert set(response.json['data']) == {'description_generation_mode', 'description_extra_fields', 'image_prompt_extra_fields', 'output_language'}
    assert response.json['data']['description_extra_fields'] == ['页面重点']
    assert client.put('/api/product/preferences', json={'text_model': 'unselected'}).status_code == 400
    assert client.put('/api/product/preferences', json={'api_key': 'unselected'}).status_code == 400
    assert client.get('/api/output-language').status_code == 200


def test_database_connection_values_never_override_managed_file(app, monkeypatch):
    from app import _load_settings_to_config
    from models import db, Settings
    from services import ai_providers as factory
    with app.app_context():
        settings = Settings.get_settings()
        settings.text_model = 'old-model'
        settings.text_model_source = 'codex'
        settings.image_model_source = 'openai'
        settings.api_key = 'old-fake-test-value'
        settings.text_api_base_url = 'https://old.invalid'
        settings.enable_image_quality_control = True
        db.session.commit()
        _load_settings_to_config(app)
        from services.task_manager import get_image_quality_control_enabled
        assert get_image_quality_control_enabled() is False
        assert settings.enable_image_quality_control is True
        assert app.config['TEXT_MODEL'] == runner.PRESET['TEXT_MODEL']
        monkeypatch.setenv('PC_MODE', 'live')
        for key, value in runner.PRESET.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv('TEXT_API_KEY', 'text-fake-test-value')
        monkeypatch.setenv('IMAGE_API_KEY', 'image-fake-test-value')
        monkeypatch.setitem(app.config, 'TEXT_MODEL_SOURCE', 'codex')
        monkeypatch.setitem(app.config, 'IMAGE_API_BASE', 'https://unselected.invalid')
        monkeypatch.setattr(factory, 'OpenAITextProvider', lambda **kw: SimpleNamespace(**kw))
        monkeypatch.setattr(factory, 'GenAIImageProvider', lambda **kw: SimpleNamespace(**kw))
        text = factory.get_text_provider(model='request-supplied-model')
        image = factory.get_image_provider(model='request-supplied-image')
        assert text.model == 'deepseek-flash' and text.api_base == 'https://tokenflux.dev/v1'
        assert image.model == 'nano-banana-2' and image.api_base == 'https://tokenflux.dev'
        assert text.api_key == 'text-fake-test-value' and image.api_key == 'image-fake-test-value'
        assert settings.text_model == 'old-model'  # No destructive reset of old settings.


@pytest.mark.parametrize('role,missing', [('text', 'TEXT_API_KEY'), ('text', 'TEXT_API_BASE'),
                                       ('image', 'IMAGE_API_KEY'), ('image', 'IMAGE_API_BASE')])
def test_missing_file_field_cannot_fall_back(app, monkeypatch, role, missing):
    from services.ai_providers import _get_model_type_provider_config
    monkeypatch.setenv('PC_MODE', 'live')
    for key, value in runner.PRESET.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv('TEXT_API_KEY', 'text-fake-test-value')
    monkeypatch.setenv('IMAGE_API_KEY', 'image-fake-test-value')
    monkeypatch.setenv('OPENAI_API_KEY', 'must-not-fall-back')
    monkeypatch.setenv('GOOGLE_API_KEY', 'must-not-fall-back')
    monkeypatch.delenv(missing, raising=False)
    with app.app_context(), pytest.raises(ValueError, match='生成服务暂不可用'):
        _get_model_type_provider_config(role)


def test_manual_draft_edit_and_preview_routes_remain(app):
    client = app.test_client()
    created = client.post('/api/projects', json={'creation_type': 'idea', 'idea_prompt': 'PC-002 手工界面验证，无模型产物'})
    assert created.status_code == 201
    project = created.json['data']['project_id']
    assert client.get(f'/api/reference-files/project/{project}').status_code == 200
    assert client.post('/api/reference-files/upload', data={}).status_code == 403
    page = client.post(f'/api/projects/{project}/pages', json={'order_index': 0,
        'outline_content': {'title': '手工测试页', 'points': ['预算 100 元']}})
    assert page.status_code == 201
    page_id = page.json['data']['page_id']
    response = client.put(f'/api/projects/{project}/pages/{page_id}/description',
                          json={'description_content': {'text': '手工修改为预算 120 元'}})
    assert response.status_code == 200
    for route in ('/', '/settings', '/about', f'/project/{project}/outline',
                  f'/project/{project}/detail', f'/project/{project}/preview'):
        result = client.get(route)
        assert result.status_code == 200 and b'Presentation Copilot' in result.data
    assert client.post(f'/api/projects/{project}/generate/outline', json={}).status_code == 503
    assert client.post(f'/api/projects/{project}/pages/{page_id}/generate/image', json={}).status_code == 503
    assert client.get(f'/api/projects/{project}/export/pptx').status_code == 400
    assert client.get(f'/api/projects/{project}/export/pdf').status_code == 400
    assert b'GNU AFFERO GENERAL PUBLIC LICENSE' in client.get('/open-source/LICENSE').data


def _template_image():
    import io
    # Use the actual bundled preset, not an AI output.
    return io.BytesIO((ROOT / 'frontend/public/templates/template_y.png').read_bytes())


def _two_page_pdf():
    import io
    import fitz
    with fitz.open() as document:
        for n in (1, 2):
            page = document.new_page(width=480, height=270)
            page.insert_text((30, 70), f'Local import test - original page {n}, budget 100. No AI.')
        return io.BytesIO(document.tobytes())


def test_templates_upload_apply_and_resolve_without_models(app, monkeypatch):
    from models import db, Project, Page, ProjectTemplateAsset, Task
    from services.file_service import FileService
    from services import task_manager as tm
    calls = []
    monkeypatch.setattr(tm.task_manager, 'submit_task', lambda *args: calls.append(args))
    client = app.test_client()
    project_id = client.post('/api/projects', json={'creation_type': 'blank'}).json['data']['project_id']
    user = client.post('/api/user-templates', data={'template_image': (_template_image(), 'preset.png'), 'name': 'Local test'})
    assert user.status_code == 200, user.json
    saved = client.post(f'/api/projects/{project_id}/template', data={'template_image': (_template_image(), 'preset.png')})
    assert saved.status_code == 200, saved.json
    page_id = client.post(f'/api/projects/{project_id}/pages', json={'order_index': 0,
        'outline_content': {'title': 'Template binding test', 'points': []}}).json['data']['page_id']
    asset_response = client.post(f'/api/projects/{project_id}/template-assets',
        data={'image': (_template_image(), 'per-page.png')})
    assert asset_response.status_code == 201, asset_response.json
    asset = asset_response.json['data']['asset']
    assert asset['analysis_status'] == 'deferred'
    assert asset_response.json['data']['analyze_task_id'] is None
    assert client.patch(f'/api/projects/{project_id}/template-mode', json={'mode':'multi'}).status_code == 200
    bind = client.patch(f'/api/projects/{project_id}/pages/{page_id}/template',
        json={'template_asset_id': asset['id'], 'selection_source':'manual'})
    assert bind.status_code == 200, bind.json
    with app.app_context():
        project, page = Project.query.get(project_id), Page.query.get(page_id)
        path, style = tm.resolve_page_template(page, project, FileService(app.config['UPLOAD_FOLDER']))
        assert Path(path).is_file()
        assert path.endswith(ProjectTemplateAsset.query.get(asset['id']).image_path.replace('/', os.sep)) or Path(path).name == 'original.png'
        assert Task.query.filter_by(project_id=project_id).count() == 0
        # Removing the page override resolves the saved project-wide image instead.
        page.template_asset_id = None
        project.template_mode = 'single'
        db.session.commit()
        path, _ = tm.resolve_page_template(page, project, FileService(app.config['UPLOAD_FOLDER']))
        assert Path(path).is_file() and Path(path).name == 'template.png'
    assert calls == []
    assert client.get(asset['image_url']).status_code == 200
    assert client.post(f"/api/projects/{project_id}/template-assets/{asset['id']}/reanalyze").status_code == 503
    other = client.post('/api/projects', json={'creation_type':'blank'}).json['data']['project_id']
    assert client.patch(f'/api/projects/{other}/pages/{page_id}/template', json={'template_asset_id': asset['id']}).status_code in (400,404)


def test_pdf_import_previews_originals_without_parsing_or_model_calls(app, monkeypatch):
    import fitz
    from models import Project, Task
    from services import task_manager as tm
    calls = []
    monkeypatch.setattr(tm.task_manager, 'submit_task', lambda *args: calls.append(args))
    client = app.test_client()
    response = client.post('/api/projects/renovation', data={'file': (_two_page_pdf(), '原稿.pdf'), 'keep_layout':'true'})
    assert response.status_code == 201, response.json
    result = response.json['data']
    assert result['page_count'] == 2 and result['task_id'] is None and result['processing_deferred']
    project_id = result['project_id']
    with app.app_context():
        project = Project.query.get(project_id)
        assert project.creation_type == 'ppt_renovation' and project.status == 'DRAFT'
        assert len(project.pages) == 2
        assert all(p.generated_image_path for p in project.pages)
        assert all(not p.description_content for p in project.pages)
        assert Task.query.filter_by(project_id=project_id).count() == 0
    assert calls == []
    exported = client.get(f'/api/projects/{project_id}/export/pdf')
    assert exported.status_code == 200
    # Export controller returns a file URL; verify the real exported original pages.
    data = client.get(exported.json['data']['download_url']).data
    with fitz.open(stream=data, filetype='pdf') as document:
        assert len(document) == 2
    assert client.post(f'/api/projects/{project_id}/renovation/process').status_code == 503


def test_pdf_template_split_does_not_enqueue_hidden_analysis(app, monkeypatch):
    from models import Task, ProjectTemplateAsset
    from services import task_manager as tm
    calls = []
    monkeypatch.setattr(tm.task_manager, 'submit_task', lambda *args: calls.append(args))
    client = app.test_client()
    project_id = client.post('/api/projects', json={'creation_type':'blank'}).json['data']['project_id']
    response = client.post(f'/api/projects/{project_id}/template-assets/upload-pdf', data={'pdf': (_two_page_pdf(), 'template.pdf')})
    assert response.status_code == 202, response.json
    assert len(calls) == 1
    task_id, func, *args = calls[0]
    assert func.__name__ == 'process_template_pdf_split_task'
    func(task_id, *args)  # Real PDF rendering, synchronously for deterministic local verification.
    with app.app_context():
        task = Task.query.get(task_id)
        assert task.status == 'COMPLETED', task.error_message
        assets = ProjectTemplateAsset.query.filter_by(project_id=project_id).all()
        assert len(assets) == 2 and all(a.analysis_status == 'deferred' for a in assets)
        assert Task.query.filter_by(project_id=project_id).count() == 1
    assert len(calls) == 1


def test_import_validation_blocks_unavailable_or_invalid_inputs_before_writing(app):
    import io
    from models import Project
    client = app.test_client()
    with app.app_context():
        before = Project.query.count()
    for name, content in [('original.pptx', b'not-converted'), ('broken.pdf', b'not-pdf')]:
        result = client.post('/api/projects/renovation', data={'file': (io.BytesIO(content), name)})
        assert result.status_code == 400
    result = client.post('/api/user-templates', data={'template_image': (io.BytesIO(b'not-an-image'), 'fake.png')})
    assert result.status_code == 400
    with app.app_context():
        assert Project.query.count() == before

"""PC-007: isolated DB/ledger, socket disabled, synthetic images and providers."""
import json
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
import pytest
from PIL import Image
from test_copilot_capabilities import app, isolated, page_data, png
from services.ai_providers import copilot_limits as limits


@pytest.fixture
def queued(app, monkeypatch):
    from services import task_manager as tm
    from services.ai_service import AIService
    from services import task_tracking as trace
    from controllers import project_controller, page_controller
    requests, jobs = [], []
    class ImageProvider:
        model = limits.MODELS['image']
        max_output_tokens = 4096
        fail = False
        @limits.model_call('image')
        def generate_image(self, prompt, ref_images=None, aspect_ratio='16:9', resolution='2K', **kwargs):
            trace.sent()  # simulated transport boundary; no network exists in these tests
            requests.append({'prompt': prompt, 'model': self.model, 'limit': self.max_output_tokens,
                'refs': [im.getpixel((0, 0)) for im in ref_images or []], 'resolution': resolution})
            if self.fail:
                raise TimeoutError('synthetic timeout with secret sk-not-for-frontend-12345')
            limits.record_usage('image', SimpleNamespace(model_dump=lambda **kw: {'prompt_token_count': 12}), 'mock', 'mock-request-1')
            return Image.new('RGB', (400, 300), 'blue')
    class TextProvider:
        model = limits.MODELS['text']
        max_output_tokens = 4096
        @limits.model_call('text')
        def generate_text(self, prompt, **kwargs):
            trace.sent()
            requests.append({'prompt': prompt, 'model': self.model})
            return '离线描述'
    ai = AIService(text_provider=TextProvider(), image_provider=ImageProvider(), caption_provider=SimpleNamespace())
    for controller in (project_controller, page_controller):
        monkeypatch.setattr(controller, 'get_ai_service', lambda: ai)
    def submit(fn, *args, **kwargs):
        future = Future()
        jobs.append((fn, args, kwargs, future))
        return future
    monkeypatch.setattr(tm.task_manager, 'executor', SimpleNamespace(submit=submit))
    def run():
        fn, args, kwargs, future = jobs.pop(0)
        result = fn(*args, **kwargs)
        future.set_result(result)
    return ai, requests, jobs, run


def enqueue(app, page_data, editing=False):
    project, page, _ = page_data
    route = 'edit/image' if editing else 'generate/image'
    body = {'edit_instruction': '将每周检查15处用水点中的15改为18，其余文字和整体版式尽量保持不变。'} if editing else {'force_regenerate': True}
    response = app.test_client().post(f'/api/projects/{project}/pages/{page}/{route}', json=body)
    assert response.status_code == 202, response.json
    return response.json['data']['task_id']


def details(app, project, **params):
    response = app.test_client().get(f'/api/projects/{project}/task-details', query_string=params)
    assert response.status_code == 200, response.json
    return response.json['data']


def test_snapshot_executes_frozen_prompt_model_template_and_config(app, page_data, queued):
    from models import db, Project, Page, Task, TaskTrace, PageImageVersion
    project, page, old = page_data
    template = Path(app.config['UPLOAD_FOLDER']) / project / 'template.png'
    template.write_bytes(png('red').getvalue())
    with app.app_context():
        db.session.get(Project, project).template_image_path = str(template.relative_to(app.config['UPLOAD_FOLDER']))
        db.session.commit()
    task_id = enqueue(app, page_data)
    ai, calls, jobs, run = queued
    with app.app_context():
        snapshot = json.loads(db.session.get(TaskTrace, task_id).snapshot)
        frozen_prompt = snapshot['pages'][0]['prompt']
        page_obj = db.session.get(Page, page)
        page_obj.set_description_content({'text': 'new content must not be sent'})
        db.session.commit()
    template.write_bytes(png('green').getvalue())
    ai.image_provider.model = 'changed-after-enqueue'
    ai.image_provider.max_output_tokens = 99
    run()
    assert len(calls) == 1
    assert calls[0]['prompt'] == frozen_prompt and 'new content must not be sent' not in calls[0]['prompt']
    assert calls[0]['model'] == limits.MODELS['image'] and calls[0]['limit'] == 4096
    assert calls[0]['refs'] == [(255, 0, 0)]
    with app.app_context():
        assert db.session.get(Page, page).generated_image_path == old
        assert PageImageVersion.query.filter_by(page_id=page).count() == 1
        assert db.session.get(Task, task_id).status == 'FAILED'
    record = details(app, project, task_id=task_id)
    assert record['calls'][0]['status'] == 'response_received'
    assert record['calls'][0]['failure_stage'] == 'input_or_save'


def test_one_call_one_budget_record_and_version_specific_details(app, page_data, queued):
    from models import db, PageImageVersion, Page
    project, page, old = page_data
    task_id = enqueue(app, page_data)
    queued[3]()
    with app.app_context():
        versions = PageImageVersion.query.filter_by(page_id=page).order_by(PageImageVersion.version_number).all()
        old_id, new_id = [v.id for v in versions]
    record = details(app, project, page_id=page, version_id=new_id)
    assert record['task_id'] == task_id and len(record['calls']) == 1
    call = record['calls'][0]
    assert call['status'] == 'response_saved' and call['sent_at']
    assert call['usage']['prompt_token_count'] == 12
    assert call['actual_credits'] is None and call['actual_credits_source'] is None
    assert call['reserved_credits'] == 4 and call['estimated_credits'] == 4
    assert record['outputs'][0]['call_id'] == call['call_id']
    assert limits.budget_state()['image'] == 1 and limits.budget_state()['reserved_credits'] == 4
    assert len((limits._root() / 'calls.jsonl').read_text().splitlines()) == 1
    historical = details(app, project, page_id=page, version_id=old_id)
    assert not historical['tracked'] and historical['snapshot'] is None
    assert app.test_client().get(f'/api/projects/{project}/task-details', query_string={
        'page_id': page, 'version_id': old_id, 'task_id': task_id}).status_code == 404
    # Reading details neither reserves budget nor calls the provider.
    assert len(queued[1]) == 1


@pytest.mark.parametrize('setting,value', [('PC_MODE','preview'), ('IMAGE_API_KEY',''),
    ('PC_BUDGET_CREDITS','0'), ('PC_BUDGET_CREDITS','3'), ('PC_IMAGE_OPERATIONS','slide_edit')])
def test_worker_rechecks_gate_without_provider_or_budget(app, page_data, queued, monkeypatch, setting, value):
    from models import db, Page
    project, page, old = page_data
    task_id = enqueue(app, page_data)
    monkeypatch.setenv(setting, value)
    queued[3]()
    assert not queued[1]
    assert not (limits._root() / 'call-budget.json').exists()
    call = details(app, project, task_id=task_id)['calls'][0]
    assert call['status'] == 'local_rejected' and call['sent_at'] is None
    assert call['actual_credits'] == 0 and call['reserved_credits'] == 0
    with app.app_context():
        assert db.session.get(Page, page).status == 'COMPLETED'


def test_timeout_keeps_unknown_cost_old_image_and_never_retries(app, page_data, queued):
    from models import db, Page, PageImageVersion
    project, page, old = page_data
    queued[0].image_provider.fail = True
    task_id = enqueue(app, page_data, editing=True)
    queued[3]()
    record = details(app, project, task_id=task_id)
    call = record['calls'][0]
    assert call['status'] == 'failed_needs_review'
    assert call['failure_stage'] == 'provider_timeout'
    assert call['actual_credits'] is None and call['usage'] is None
    assert len(queued[1]) == 1 and limits.budget_state()['paused']
    assert limits.budget_state()['reserved_credits'] == 4
    assert 'sk-not-for' not in json.dumps(record)
    with app.app_context():
        assert db.session.get(Page, page).generated_image_path == old
        assert PageImageVersion.query.filter_by(page_id=page).count() == 1


def test_edit_links_source_and_synced_output_and_preserves_other_page(app, page_data, queued):
    from models import db, Page, PageImageVersion
    project, page, old = page_data
    with app.app_context():
        obj = db.session.get(Page, page)
        obj.set_description_content({'text': '每周检查15处用水点'})
        obj.set_outline_content({'title': '行动', 'points': ['每周检查15处用水点']})
        other = Page(project_id=project, order_index=1)
        other.set_description_content({'text':'其他页面15保持'})
        db.session.add(other); db.session.commit(); other_id = other.id
    task_id = enqueue(app, page_data, editing=True)
    queued[3]()
    with app.app_context():
        obj = db.session.get(Page, page)
        assert '18处' in obj.get_description_content()['text']
        assert '18处' in obj.get_outline_content()['points'][0]
        assert db.session.get(Page, other_id).get_description_content()['text'] == '其他页面15保持'
        assert PageImageVersion.query.filter_by(page_id=page).count() == 2
    record = details(app, project, task_id=task_id)
    assert record['snapshot']['pages'][0]['source_version'] == 1
    assert record['outputs'][0]['version_number'] == 2
    assert record['calls'][0]['status'] == 'response_saved'


def test_migration_idempotent_cross_project_and_interrupted(app, page_data, queued):
    from models import db, TaskTrace, Project, Page, PageImageVersion, Task
    project, page, old = page_data
    task_id = enqueue(app, page_data)
    with app.app_context():
        before = (Task.query.count(), Page.query.count(), PageImageVersion.query.count())
        TaskTrace.__table__.create(db.engine, checkfirst=True)
        TaskTrace.__table__.create(db.engine, checkfirst=True)
        assert before == (Task.query.count(), Page.query.count(), PageImageVersion.query.count())
        trace = db.session.get(TaskTrace, task_id); trace.process_id = 'previous-process'
        other = Project(creation_type='blank'); db.session.add(other); db.session.commit(); other_id = other.id
    assert details(app, project, task_id=task_id)['status'] == 'INTERRUPTED'
    client = app.test_client()
    assert client.get(f'/api/projects/{other_id}/task-details?task_id={task_id}').status_code == 404
    assert client.get(f'/api/projects/{other_id}/task-details?page_id={page}').status_code == 404
    assert not queued[1]  # no automatic recovery


def test_prompt_secrets_never_persist_or_leak(app, page_data, queued, monkeypatch):
    from models import db, Page, TaskTrace
    project, page, _ = page_data
    monkeypatch.setenv('IMAGE_API_KEY', 'offline-secret-value-123')
    with app.app_context():
        db.session.get(Page, page).set_description_content({'text': 'offline-secret-value-123 Authorization: Bearer fake-secret'})
        db.session.commit()
    task_id = enqueue(app, page_data)
    with app.app_context():
        assert 'offline-secret-value-123' not in db.session.get(TaskTrace, task_id).snapshot
        assert 'fake-secret' not in db.session.get(TaskTrace, task_id).snapshot
    public = json.dumps(details(app, project, task_id=task_id))
    assert 'Authorization' not in public and 'input_text' not in public and 'prompt"' not in public


def test_async_description_captures_queue_input_and_tracks_text(app, page_data, queued):
    from models import db, Task, Page
    from services import task_manager as tm
    from services.ai_service import ProjectContext
    from models import Project
    project, page, _ = page_data
    with app.app_context():
        task = Task(project_id=project, task_type='GENERATE_DESCRIPTIONS')
        db.session.add(task); db.session.commit(); task_id = task.id
        tm.task_manager.submit_task(task_id, tm.generate_descriptions_task, project, queued[0],
            ProjectContext(db.session.get(Project, project)), [{'title':'固定标题','points':['固定内容']}], app=app, language='zh')
    queued[3]()
    assert '固定标题' in queued[1][0]['prompt']
    record = details(app, project, task_id=task_id)
    assert record['calls'][0]['status'] == 'response_saved'
    assert record['calls'][0]['usage'] is None
    with app.app_context():
        assert db.session.get(Page, page).get_description_content()['text'] == '离线描述'


def test_save_failure_rolls_back_text_and_version(app, page_data, queued, monkeypatch):
    from services import task_manager as tm
    from models import db, Page, PageImageVersion
    project, page, old = page_data
    task_id = enqueue(app, page_data, editing=True)
    def fail(*args, **kwargs):
        raise OSError('synthetic storage failure')
    monkeypatch.setattr(tm, 'save_image_with_version', fail)
    queued[3]()
    call = details(app, project, task_id=task_id)['calls'][0]
    assert call['status'] == 'response_received' and call['failure_stage'] == 'input_or_save'
    with app.app_context():
        assert db.session.get(Page, page).generated_image_path == old
        assert PageImageVersion.query.filter_by(page_id=page).count() == 1
    assert len(queued[1]) == 1


def test_tampered_frozen_reference_rejected_before_call(app, page_data, queued):
    from models import db, Project
    project, page, _ = page_data
    template = Path(app.config['UPLOAD_FOLDER']) / project / 'template.png'
    template.write_bytes(png('red').getvalue())
    with app.app_context():
        db.session.get(Project, project).template_image_path = str(template.relative_to(app.config['UPLOAD_FOLDER']))
        db.session.commit()
    task_id = enqueue(app, page_data)
    frozen = Path(app.config['UPLOAD_FOLDER']) / project / 'task-inputs' / task_id / page / '0.png'
    frozen.write_bytes(png('green').getvalue())
    queued[3]()
    assert not queued[1]
    assert details(app, project, task_id=task_id)['calls'][0]['status'] == 'local_rejected'


def test_real_sse_route_tracks_simulated_text_without_invoice(app, page_data, monkeypatch):
    from test_copilot_stream_timeout import provider, Stream, chunk, wait_worker
    from controllers import project_controller
    from services.ai_service import AIService
    from models import db, Project
    from test_visitor_isolation import client, project as create_project
    from test_release_runtime import policy
    from services.visitor_access import DEFAULTS
    monkeypatch.setitem(app.config, 'PC_VISITOR_ENABLED', True)
    cap = {**DEFAULTS, 'pages': 1, 'default_auto_pages': 1, 'text': 6, 'image': 5, 'credits': 30}
    monkeypatch.setitem(app.config, 'PC_VISITOR_LIMITS', cap)
    p = policy(); p['authorization'].update(text=6, credits=30, roles=['text', 'image']); p['visitor'] = cap
    monkeypatch.setenv('PC_RUN_POLICY', json.dumps(p))
    limits.initialize_runtime(app)
    browser = client(app); project = create_project(browser)
    service = AIService(text_provider=provider(lambda **kw: Stream([
        chunk('## 离线模拟页\n- 模拟内容\n<!-- PAGE_END -->\n<!-- END -->\n')])),
        image_provider=SimpleNamespace(), caption_provider=SimpleNamespace())
    monkeypatch.setattr(project_controller, 'get_ai_service', lambda: service)
    revision = browser.get(f'/api/projects/{project}/input-revision').json['data']['revision']
    response = browser.post(f'/api/projects/{project}/generate/outline/stream', json={'language':'zh','idea_prompt':'生成1页PPT'},
        headers={'Idempotency-Key':'pc0102-tracked-stream','X-PC-Input-Version':revision})
    assert 'event: done' in response.get_data(as_text=True)
    wait_worker()
    summary = browser.get(f'/api/projects/{project}/task-details').json['data'][0]
    record = browser.get(f'/api/projects/{project}/task-details',query_string={'task_id':summary['task_id']}).json['data']
    assert record['tracked'] and record['snapshot']['model'] == 'deepseek-flash'
    assert record['calls'][0]['status'] == 'response_saved'
    assert record['calls'][0]['actual_credits'] is None


def test_table_creation_on_legacy_database_keeps_rows_and_is_repeatable(tmp_path):
    from sqlalchemy import create_engine, text
    from models import db, TaskTrace
    engine = create_engine('sqlite:///' + str(tmp_path / 'legacy.db'))
    db.metadata.create_all(engine, tables=[t for t in db.metadata.sorted_tables if t.name != 'task_traces'])
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO projects(id,creation_type,status,created_at,updated_at) VALUES ('old','blank','DRAFT',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        conn.execute(text("INSERT INTO tasks(id,project_id,task_type,status,created_at) VALUES ('oldtask','old','OLD','COMPLETED',CURRENT_TIMESTAMP)"))
    TaskTrace.__table__.create(engine, checkfirst=True)
    TaskTrace.__table__.create(engine, checkfirst=True)
    with engine.connect() as conn:
        assert conn.execute(text('SELECT COUNT(*) FROM tasks')).scalar() == 1
        assert conn.execute(text('SELECT COUNT(*) FROM task_traces')).scalar() == 0
    engine.dispose()

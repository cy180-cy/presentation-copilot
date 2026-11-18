"""Explicit retry through real admission/worker/ledger paths; mock providers only."""
import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_admin_console import app, admin, isolated, admin_client, login, live_draft, apply
from test_task_tracking import queued
from test_settlement_guards import setup
from test_cycle_guards import transition
from test_visitor_isolation import call, client, owner, project, page
from test_copilot_capabilities import png
from services import image_retry as retry, visitor_access as va
from services.ai_providers import copilot_limits as budget
from services.ledger_settlement import summary
from image_retry_maintenance import review, run


def failed(app, admin, queued):
    from models import db, Project
    manager, visitor, pid, pg, _ = setup(app, admin, queued, visitor_credits=12)
    transition()
    folder = Path(app.config['UPLOAD_FOLDER']) / pid
    template = folder / 'template.png'
    template.write_bytes(png('red').getvalue())
    with app.app_context(), va.scope(owner(app, pid)):
        db.session.get(Project, pid).template_image_path = str(template.relative_to(app.config['UPLOAD_FOLDER']))
        db.session.commit()
    queued[0].image_provider.fail = True
    response = call(visitor, pid, pg, key='failed-current-cycle')
    assert response.status_code == 202
    queued[3]()
    state = budget.budget_state()
    assert state['paused'] and summary(state)['pending_reserved'] == 4
    queued[0].image_provider.fail = False
    return manager, visitor, pid, pg, state['last_call_id'], response.json['data']['task_id']


def permit(app, cid):
    root = Path(app.config['SQLALCHEMY_DATABASE_URI'].removeprefix('sqlite:///')).parent.parent
    proposal, _ = review(root, budget.budget_state(), cid, budget.MODELS['image'])
    updated, changed = retry.grant(budget.budget_state(), proposal)
    assert changed
    budget._save(budget._root()/'call-budget.json', updated)
    return proposal


def test_seven_attempts_two_confirmed_free_and_one_output_occupy_twenty_once(app, admin, queued):
    """Reproduce the observed accounting through HTTP/worker/admin, offline."""
    from models import db, Project, TaskTrace, Task, VisitorCall, PageImageVersion
    from services import budget_cycles as cycles, ledger_settlement as settlement, generation_budget as gb

    manager = login(admin_client(app, admin))
    draft = live_draft()
    draft['authorization'].update(credits=50, image=10)
    draft['visitor'].update(credits=27.71, image=10)
    apply(manager, draft)
    visitor = client(app)
    pid = project(visitor)
    pg, _ = page(app, visitor, pid)
    template = Path(app.config['UPLOAD_FOLDER']) / pid / 'template.png'
    template.write_bytes(png('red').getvalue())
    with app.app_context():
        db.session.get(Project, pid).template_image_path = str(template.relative_to(app.config['UPLOAD_FOLDER']))
        db.session.commit()
    state, _ = cycles.create(budget.budget_state(), cycle_id='offline-seven-attempts', credit_cap=27.71,
        source_sha256='a'*64, archive_sha256='b'*64, created_at='2026-10-24T00:00:00Z')
    budget._save(budget._root()/'call-budget.json', state)
    call_ids, task_ids = [], []
    for index in range(7):
        if index == 6:
            # Only the first two failures have explicit no-charge evidence.
            state = budget.budget_state()
            proposals = [{'target': cid, 'kind': 'confirmed_no_charge', 'confirmation_source': 'user',
                          'evidence_sha256': 'c'*64} for cid in call_ids[:2]]
            state, _ = settlement.prepare(state, proposals, settlement.catalog(state, []),
                                           no_charge=set(call_ids[:2]))
            budget._save(budget._root()/'call-budget.json', state)
            assert summary(state)['total_occupied'] == 16
        if index:
            permit(app, call_ids[-1])
        queued[0].image_provider.fail = index < 6
        response = call(visitor, pid, pg, key=f'offline-attempt-{index}')
        assert response.status_code == 202, response.json
        task_ids.append(response.json['data']['task_id'])
        queued[3]()
        state = budget.budget_state()
        call_ids.append(state['last_call_id'])
        assert cycles.counts(state) == {'text': 0, 'image': index + 1}
        assert state['reserved_credits'] == 4 * (index + 1)

    assert len(set(call_ids)) == 7 and len(state['reservation_calls']) == 7
    assert summary(state)['original_reserved'] == 28
    assert summary(state)['settled_reserved'] == summary(state)['released_credits'] == 8
    assert summary(state)['pending_reserved'] == summary(state)['total_occupied'] == 20
    assert summary(state)['settled_actual'] == 0  # latest successful bill still unknown
    assert gb.held(state) == {'text': 0, 'image': 0, 'credits': 0}
    overview = manager.get('/api/admin/overview').json['data']
    assert overview['remaining_reservation'] == 7.71
    assert overview['settlement']['effective_credits'] == 20
    with app.app_context(), va.scope(owner(app, pid)):
        assert va.usage_summary(va.identity()) == {'text': 0, 'image': 7, 'reserved': 20}
        for cid, tid in zip(call_ids, task_ids):
            record = db.session.get(TaskTrace, tid)
            assert 'budget_plan_id' not in json.loads(record.snapshot)
            assert len(json.loads(record.calls)) == 1
            assert json.loads(record.calls)[0]['call_id'] == cid
            assert state['reservation_calls'][cid]['task_id'] == tid
            assert db.session.get(VisitorCall, cid).task_id == tid
        last = db.session.get(TaskTrace, task_ids[-1])
        output, = json.loads(last.outputs)
        assert output['call_id'] == call_ids[-1]
        assert db.session.get(PageImageVersion, output['version_id']).page_id == pg
        assert db.session.get(Task, task_ids[-1]).status == 'COMPLETED'
        assert json.loads(last.calls)[0]['actual_credits'] is None
    # The consumed permit closes generation before replay admission. A repeat
    # is rejected by the existing pause guard and cannot reserve again.
    replay = call(visitor, pid, pg, key='offline-attempt-6')
    assert replay.status_code == 503, replay.json
    assert not queued[2] and budget.budget_state() == state
    assert not retry.ready(state)


def test_confirmed_free_failure_keeps_other_pending_costs_and_only_one_retry(app, admin, queued):
    from models import db, TaskTrace
    from services import ledger_settlement as settlement

    manager, visitor, pid, pg, first, first_task = failed(app, admin, queued)
    permit(app, first)
    queued[0].image_provider.fail = True
    second_request = call(visitor, pid, pg, key='second-synthetic-failure')
    assert second_request.status_code == 202
    queued[3]()
    queued[0].image_provider.fail = False
    before = budget.budget_state()
    second = before['last_call_id']
    assert second != first and summary(before)['pending_reserved'] == 8
    with app.app_context():
        old_trace = db.session.get(TaskTrace, first_task).calls
    proposal = {'target': first, 'kind': 'confirmed_no_charge',
                'confirmation_source': 'user', 'evidence_sha256': 'a'*64}
    updated, _ = settlement.prepare(before, [proposal], settlement.catalog(before, []), no_charge={first})
    budget._save(budget._root()/'call-budget.json', updated)
    assert updated['image'] == before['image'] and updated['reserved_credits'] == before['reserved_credits']
    assert updated['paused'] and second not in updated['settlements']
    overview = manager.get('/api/admin/overview').json['data']
    assert overview['settlement']['pending_reserved'] == 4
    assert overview['settlement']['released_credits'] == 4
    assert overview['remaining_reservation'] == 4
    detail = manager.get('/api/admin/tasks/'+first_task).json['data']['calls'][0]
    assert detail['actual_credits'] == 0 and detail['cost_status'] == 'confirmed_no_charge'
    assert detail['actual_credits_source'] == 'user_confirmed_no_charge'
    with app.app_context(), va.scope(owner(app, pid)):
        assert db.session.get(TaskTrace, first_task).calls == old_trace
        assert va.usage_summary(va.identity())['reserved'] == 4
    # Releasing a fee does not itself reopen sending or reset attempt counters.
    assert not visitor.get('/api/product/status').json['data']['capabilities']['template_generate']['available']
    permit(app, second)
    with app.app_context():
        snapshot = budget.budget_state()
        budget.initialize_runtime(app)
        assert budget.budget_state() == snapshot
    response = call(visitor, pid, pg, key='one-after-explicit-free-confirmation')
    assert response.status_code == 202
    queued[3]()
    after = budget.budget_state()
    assert after['image'] == before['image'] + 1
    assert summary(after)['pending_reserved'] == 8
    assert not retry.ready(after)


def test_single_retry_preserves_old_fee_and_cannot_repeat_or_restart(app, admin, queued):
    from models import db, TaskTrace, Task
    manager, visitor, pid, pg, cid, tid = failed(app, admin, queued)
    with app.app_context():
        old_trace = db.session.get(TaskTrace, tid).calls
    before = budget.budget_state()
    permit(app, cid)
    with app.app_context():
        budget.initialize_runtime(app)
    assert budget.budget_state()['paused'] is True
    assert summary(budget.budget_state())['total_occupied'] == 4
    assert visitor.get('/api/product/status').json['data']['capabilities']['template_generate']['available']
    # Reusing the old idempotency key returns the old task, never queues it again.
    replay = call(visitor, pid, pg, key='failed-current-cycle')
    assert replay.status_code == 202 and replay.json['data']['task_id'] == tid
    assert len(queued[2]) == 0
    response = call(visitor, pid, pg, key='explicit-new-retry')
    assert response.status_code == 202, response.json
    queued[3]()
    after = budget.budget_state()
    assert after['image'] == before['image'] + 1
    assert after['reserved_credits'] == before['reserved_credits'] + 4
    assert after['paused'] is True and summary(after)['pending_reserved'] == 8
    grant = after['image_retry_permits'][after['active_image_retry']]
    assert grant['state'] == 'consumed' and grant['used_call_id'] == after['last_call_id']
    assert grant['used_task_id'] == response.json['data']['task_id']
    with app.app_context():
        assert db.session.get(Task, grant['used_task_id']).status == 'COMPLETED'
        assert db.session.get(TaskTrace, tid).calls == old_trace
        with pytest.raises(RuntimeError):
            budget.initialize_runtime(app)
    count = len(queued[1])
    assert call(visitor, pid, pg).status_code >= 400
    assert len(queued[1]) == count and not queued[2]
    assert not visitor.get('/api/product/status').json['data']['capabilities']['template_generate']['available']
    assert after['quota_cycle'] == before['quota_cycle']
    assert after['runtime_calls'][cid] == before['runtime_calls'][cid]


def test_capability_display_opens_only_granted_visitors_template_action(app, admin, queued):
    _, visitor, _, _, cid, _ = failed(app, admin, queued)
    permit(app, cid)
    own = visitor.get('/api/product/status').json['data']['capabilities']
    assert own['template_generate']['available'] is True
    assert all(not own[k]['available'] for k in ('text', 'text_generate', 'slide_edit', 'material_region_edit'))
    other = client(app).get('/api/product/status').json['data']['capabilities']
    assert not other['template_generate']['available']
    assert retry.ready(budget.budget_state()) is not None


@pytest.mark.parametrize('mismatch', ['owner', 'page', 'input', 'budget', 'model', 'template', 'text'])
def test_retry_rejects_unapproved_scope_before_provider(app, admin, queued, monkeypatch, mismatch):
    from models import db, Page
    _, visitor, pid, pg, cid, _ = failed(app, admin, queued)
    permit(app, cid)
    count = len(queued[1])
    if mismatch == 'owner':
        other = client(app)
        revision = visitor.get(f'/api/projects/{pid}/input-revision').json['data']['revision']
        response = other.post(f'/api/projects/{pid}/pages/{pg}/generate/image',
            json={'force_regenerate': True},
            headers={'Idempotency-Key': 'foreign-retry', 'X-PC-Input-Version': revision})
    elif mismatch == 'page':
        added = visitor.post(f'/api/projects/{pid}/pages', json={'order_index': 1,
            'outline_content': {'title': 'other', 'points': ['other']}})
        assert added.status_code == 201, added.json
        response = call(visitor, pid, added.json['data']['page_id'])
    elif mismatch == 'input':
        with app.app_context(), va.scope(owner(app, pid)):
            db.session.get(Page, pg).set_description_content({'text': 'changed'})
            db.session.commit()
        response = call(visitor, pid, pg)
    elif mismatch == 'budget':
        state = budget.budget_state(); state['external_unknown'] = 1
        budget._save(budget._root()/'call-budget.json', state)
        response = call(visitor, pid, pg)
    elif mismatch == 'model':
        monkeypatch.setenv('IMAGE_MODEL', 'not-approved')
        response = call(visitor, pid, pg)
    elif mismatch == 'text':
        response = visitor.post(f'/api/projects/{pid}/generate/descriptions', json={})
    else:
        template = Path(app.config['UPLOAD_FOLDER']) / pid / 'template.png'
        template.write_bytes(png('green').getvalue())
        response = call(visitor, pid, pg)
        if response.status_code == 202:
            queued[3]()
    assert response.status_code >= 400 or mismatch == 'template'
    assert len(queued[1]) == count
    assert summary(budget.budget_state())['pending_reserved'] == 4


def test_failed_retry_also_consumes_permission_and_stays_paused(app, admin, queued):
    _, visitor, pid, pg, cid, _ = failed(app, admin, queued)
    permit(app, cid)
    queued[0].image_provider.fail = True
    response = call(visitor, pid, pg)
    assert response.status_code == 202
    queued[3]()
    state = budget.budget_state()
    assert state['image_retry_permits'][state['active_image_retry']]['state'] == 'consumed'
    assert summary(state)['pending_reserved'] == 8
    count = len(queued[1])
    assert call(visitor, pid, pg).status_code >= 400 and len(queued[1]) == count


def test_concurrent_workers_cannot_consume_the_same_permit_twice(app, admin, queued):
    from models import db, Task, TaskTrace
    from services import task_tracking as trace
    from services.ai_providers.copilot_limits import image_operation_scope
    from PIL import Image
    _, visitor, pid, pg, cid, _ = failed(app, admin, queued)
    permit(app, cid)
    response = call(visitor, pid, pg)
    assert response.status_code == 202
    first = response.json['data']['task_id']
    with app.app_context():
        snapshot = json.loads(db.session.get(TaskTrace, first).snapshot)
        second = Task(project_id=pid, task_type='GENERATE_PAGE_IMAGE', status='PENDING')
        db.session.add(second); db.session.commit()
        trace.create_trace(second.id, snapshot)
        ids = [first, second.id]
    who = owner(app, pid)
    count = len(queued[1])
    def invoke(task_id):
        with app.app_context(), va.scope(who), trace.scope(task_id, pg, app), image_operation_scope('template_generate', ['offline-reference']):
            try:
                queued[0].image_provider.generate_image(snapshot['pages'][0]['prompt'],
                    ref_images=[Image.new('RGB', (2, 2), 'red')])
                return True
            except RuntimeError:
                return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(invoke, ids))
    assert sorted(outcomes) == [False, True]
    assert len(queued[1]) == count + 1
    assert summary(budget.budget_state())['pending_reserved'] == 8


@pytest.mark.parametrize('quality', [False, True])
def test_offline_grant_backup_persistence_and_idempotence(app, admin, queued, tmp_path, monkeypatch, quality):
    if quality:
        _, _, _, cid, _ = quality_source(app, admin, queued, monkeypatch)
    else:
        _, _, _, _, cid, _ = failed(app, admin, queued)
    # Keep synthetic backup paths below Windows MAX_PATH, including UUID images.
    root = tmp_path.parent/'qm' if quality else tmp_path/'maintenance'
    (root/'data').mkdir(parents=True); (root/'run').mkdir()
    source = Path(app.config['SQLALCHEMY_DATABASE_URI'].removeprefix('sqlite:///'))
    with sqlite3.connect(source.as_uri()+'?mode=ro', uri=True) as src, sqlite3.connect(root/'data/database.db') as dst:
        src.backup(dst)
    if quality:
        import shutil
        shutil.copytree(app.config['UPLOAD_FOLDER'], root/'uploads')
    before = budget.budget_state()
    budget._save(root/'run/call-budget.json', before)
    first = run(root, cid, budget.MODELS['image'], quality=quality)
    assert not first['applied'] and first['additional_reservation'] == 4
    assert json.loads((root/'run/call-budget.json').read_bytes()) == before
    with pytest.raises(ValueError, match='预览'):
        run(root, cid, budget.MODELS['image'], 'wrong-confirmation', quality=quality)
    from presentation_copilot import PRESET
    from services.runtime_config import DEFAULT
    (root/'.env.copilot').write_text('\n'.join(f'{k}={v}' for k,v in
        {**PRESET, 'TEXT_API_KEY': 'offline-only', 'IMAGE_API_KEY': 'offline-only'}.items()), encoding='utf-8')
    cfg = {**DEFAULT, 'data_dir': str(root), 'secret_file': str(root/'.env.copilot')}
    config_path = root/'runtime.json'; config_path.write_text(json.dumps(cfg), encoding='utf-8')
    entry = Path(__file__).resolve().parents[1]/'scripts/presentation_copilot.py'
    child = "import runpy,socket,sys; socket.socket.connect=lambda *a,**k: (_ for _ in ()).throw(AssertionError('No network')); sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name='__main__')"
    env = {k:v for k,v in os.environ.items() if k in ('SYSTEMROOT','WINDIR','SYSTEMDRIVE','COMSPEC','PATHEXT')}
    def cli(*extra):
        result = subprocess.run([sys.executable, '-I', '-B', '-X', 'utf8', '-c', child,
            str(entry), '--runtime-config', str(config_path), '--quality-source-call' if quality else '--retry-failed-call', cid,
            *extra, 'image-retry'], env=env, capture_output=True, text=True, encoding='utf-8', timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout)
    assert cli()['confirmation'] == first['confirmation']
    actual = cli('--apply-image-retry', first['confirmation'])
    assert actual['applied'] and Path(actual['backup']).is_dir()
    after = json.loads((root/'run/call-budget.json').read_bytes())
    assert all(after[k] == v for k, v in before.items())
    assert retry.ready(after)['source_call_id' if quality else 'failed_call_id'] == cid
    data = (root/'run/call-budget.json').read_bytes()
    repeat = run(root, cid, budget.MODELS['image'], first['confirmation'], quality=quality)
    assert repeat['unchanged'] and repeat['applied']
    assert (root/'run/call-budget.json').read_bytes() == data
    assert len(after['image_retry_permits']) == 1


def quality_source(app, admin, queued, monkeypatch):
    """One successful image in a cycle; old/new prompt versions are synthetic."""
    from models import db, Project
    from services import prompts, budget_cycles as cycles
    manager = login(admin_client(app, admin))
    draft = live_draft()
    draft['authorization'].update(credits=12, image=3)
    draft['visitor'].update(credits=12, image=3)
    apply(manager, draft)
    visitor = client(app); pid = project(visitor); pg, _ = page(app, visitor, pid)
    template = Path(app.config['UPLOAD_FOLDER']) / pid / 'template.png'
    template.write_bytes(png('red').getvalue())
    with app.app_context():
        db.session.get(Project, pid).template_image_path = str(template.relative_to(app.config['UPLOAD_FOLDER']))
        db.session.commit()
    state, _ = cycles.create(budget.budget_state(), cycle_id='offline-quality-cycle', credit_cap=12,
        source_sha256='a'*64, archive_sha256='b'*64, created_at='2026-10-25T00:00:00Z')
    budget._save(budget._root()/'call-budget.json', state)
    original = prompts.get_image_generation_prompt
    monkeypatch.setattr(prompts, 'IMAGE_GENERATION_PROMPT_VERSION', 'old-offline-prompt')
    from services import ai_service
    monkeypatch.setattr(ai_service, 'get_image_generation_prompt', lambda **kw: original(**kw)+' old offline rule')
    response = call(visitor, pid, pg, key='quality-source-offline-001')
    assert response.status_code == 202, response.json
    queued[3]()
    monkeypatch.setattr(ai_service, 'get_image_generation_prompt', original)
    monkeypatch.setattr(prompts, 'IMAGE_GENERATION_PROMPT_VERSION', 'pc0153h-single-number-v1')
    state = budget.budget_state()
    state['paused'] = True  # isolated equivalent of the retained failure pause
    budget._save(budget._root()/'call-budget.json', state)
    return visitor, pid, pg, state['last_call_id'], response.json['data']['task_id']


def quality_permit(app, cid):
    root = Path(app.config['SQLALCHEMY_DATABASE_URI'].removeprefix('sqlite:///')).parent.parent
    proposal, _ = review(root, budget.budget_state(), cid, budget.MODELS['image'], quality=True)
    updated, changed = retry.grant(budget.budget_state(), proposal)
    assert changed
    budget._save(budget._root()/'call-budget.json', updated)
    return proposal


def test_quality_success_new_prompt_one_version_and_idempotent_grant(app, admin, queued, monkeypatch):
    from models import db, TaskTrace, PageImageVersion, Task
    visitor, pid, pg, cid, old_task = quality_source(app, admin, queued, monkeypatch)
    before = budget.budget_state()
    p = quality_permit(app, cid)
    assert p['purpose'] == 'quality_recheck' and 'failed_call_id' not in p
    with app.app_context():
        old_snapshot = db.session.get(TaskTrace, old_task).snapshot
        assert p['prompt_hash'] != json.loads(old_snapshot)['pages'][0]['prompt_hash']
        versions = PageImageVersion.query.filter_by(page_id=pg).count()
        budget.initialize_runtime(app)
    again, changed = retry.grant(budget.budget_state(), p)
    assert not changed and again == budget.budget_state()
    assert call(visitor, pid, pg, key='quality-source-offline-001').json['data']['task_id'] == old_task
    assert not queued[2]
    response = call(visitor, pid, pg, key='quality-recheck-offline-001')
    assert response.status_code == 202, response.json
    queued[3]()
    after = budget.budget_state()
    assert after['reserved_credits'] == before['reserved_credits'] + 4
    assert after['image'] == before['image'] + 1 and after['text'] == before['text']
    assert after['paused'] and not retry.ready(after)
    with app.app_context():
        assert db.session.get(Task, response.json['data']['task_id']).status == 'COMPLETED'
        record = db.session.get(TaskTrace, response.json['data']['task_id'])
        assert json.loads(record.snapshot)['pages'][0]['prompt_hash'] == p['prompt_hash']
        assert json.loads(record.calls)[0]['actual_credits'] is None
        assert PageImageVersion.query.filter_by(page_id=pg).count() == versions + 1
        assert db.session.get(TaskTrace, old_task).snapshot == old_snapshot
    assert call(visitor, pid, pg, key='quality-recheck-offline-001').status_code == 503
    assert not queued[2] and retry.grant(after, p) == (after, False)


@pytest.mark.parametrize('change', ['prompt', 'input', 'version', 'template', 'budget', 'owner', 'queued_input'])
def test_quality_changed_binding_or_budget_never_sends(app, admin, queued, monkeypatch, change):
    from models import db, Page, PageImageVersion
    from services import ai_service
    visitor, pid, pg, cid, _ = quality_source(app, admin, queued, monkeypatch)
    p = quality_permit(app, cid)
    count = len(queued[1]); before = budget.budget_state()
    if change == 'queued_input':
        response = call(visitor, pid, pg)
        assert response.status_code == 202
    if change in ('input', 'queued_input'):
        with app.app_context():
            db.session.get(Page, pg).set_description_content({'text': 'changed input'})
            db.session.commit()
    elif change == 'version':
        with app.app_context():
            db.session.get(PageImageVersion, p['source_version_id']).is_current = False
            db.session.commit()
    elif change == 'prompt':
        original = ai_service.get_image_generation_prompt
        monkeypatch.setattr(ai_service, 'get_image_generation_prompt', lambda **kw: original(**kw)+' changed rule')
    elif change == 'template':
        (Path(app.config['UPLOAD_FOLDER'])/pid/'template.png').write_bytes(png('green').getvalue())
    elif change == 'budget':
        state = budget.budget_state(); state['external_unknown'] = 5
        budget._save(budget._root()/'call-budget.json', state)
    elif change == 'owner':
        revision = visitor.get(f'/api/projects/{pid}/input-revision').json['data']['revision']
        response = client(app).post(f'/api/projects/{pid}/pages/{pg}/generate/image',
            json={'force_regenerate': True}, headers={'Idempotency-Key': 'foreign-quality-request',
                                                      'X-PC-Input-Version': revision})
        assert response.status_code >= 400
    if change not in ('queued_input', 'owner'):
        response = call(visitor, pid, pg)
    if response.status_code == 202:
        queued[3]()
    assert len(queued[1]) == count
    assert budget.budget_state()['reserved_credits'] == before['reserved_credits']


def test_quality_concurrent_workers_consume_once(app, admin, queued, monkeypatch):
    from models import db, Task, TaskTrace
    from services import task_tracking as trace
    from services.ai_providers.copilot_limits import image_operation_scope
    from PIL import Image
    visitor, pid, pg, cid, _ = quality_source(app, admin, queued, monkeypatch)
    quality_permit(app, cid)
    response = call(visitor, pid, pg)
    assert response.status_code == 202
    first = response.json['data']['task_id']
    with app.app_context():
        snapshot = json.loads(db.session.get(TaskTrace, first).snapshot)
        second = Task(project_id=pid, task_type='GENERATE_PAGE_IMAGE', status='PENDING')
        db.session.add(second); db.session.commit()
        trace.create_trace(second.id, snapshot)
        ids = [first, second.id]
    who = owner(app, pid); count = len(queued[1])
    def invoke(tid):
        with app.app_context(), va.scope(who), trace.scope(tid, pg, app), image_operation_scope('template_generate', ['offline-reference']):
            try:
                queued[0].image_provider.generate_image(snapshot['pages'][0]['prompt'], ref_images=[Image.new('RGB', (2, 2), 'red')])
                return True
            except RuntimeError:
                return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(invoke, ids)) == [False, True]
    assert len(queued[1]) == count + 1
    assert summary(budget.budget_state())['pending_reserved'] == 8

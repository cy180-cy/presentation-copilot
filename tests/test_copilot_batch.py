"""PC-006.2: no sockets, temporary ledger and synthetic Providers only."""
import copy
import json
import pytest
from test_copilot_capabilities import isolated, app, models, page_data
from services.ai_providers import copilot_limits as limits
from services.slide_consistency import plan_edit_sync


INSTRUCTION = '将每周检查15处用水点中的15改为18，\n其余文字和整体版式尽量保持不变。'


@pytest.fixture
def batch(monkeypatch, isolated):
    historical = {'text': 4, 'image': 4, 'reserved_credits': 19.2, 'paused': False,
                  'last_error_type': 'OldError', 'external_unknown': 3}
    limits._save(limits._root() / 'call-budget.json', historical)
    monkeypatch.setenv('PC_BATCH_ID', limits.BATCH_ID)
    monkeypatch.setenv('PC_MODEL_ROLES', 'image')
    monkeypatch.setenv('PC_IMAGE_OPERATIONS', 'template_generate,slide_edit')
    limits.initialize_batch(limits.BATCH_ID)
    return historical


def invoke(operation, calls, fail=False):
    class Provider:
        model = limits.MODELS['image']
        @limits.model_call('image')
        def generate(self, prompt, ref_images=None, aspect_ratio='16:9', resolution='2K'):
            calls.append(operation)
            if fail:
                raise TimeoutError('offline synthetic failure')
            return 'offline-result'
    with limits.image_operation_scope(operation, ['local-test-only.png']):
        return Provider().generate('offline', ['synthetic-pixels'])


def test_two_operations_third_refused_and_restart_retains_start(batch):
    calls = []
    assert limits.execution_reason('image') is None
    start = copy.deepcopy(limits.initialize_batch(limits.BATCH_ID)['start'])
    invoke('template_generate', calls)
    before = (limits._root() / 'call-budget.json').read_bytes()
    assert limits.initialize_batch(limits.BATCH_ID)['start'] == start
    assert (limits._root() / 'call-budget.json').read_bytes() == before
    with pytest.raises(RuntimeError):
        invoke('template_generate', calls)
    invoke('slide_edit', calls)
    with pytest.raises(RuntimeError):
        invoke('slide_edit', calls)
    assert calls == ['template_generate', 'slide_edit']
    state = limits.budget_state()
    assert state['image'] == 6 and state['text'] == 4 and state['reserved_credits'] == 27.2
    assert state['external_unknown'] == 3 and state['last_error_type'] == 'OldError'
    assert state['batches'][limits.BATCH_ID]['start'] == start
    assert limits.execution_reason('image') == 'budget_exhausted'


def test_unauthorized_and_budget_exhausted_never_reach_provider(batch, monkeypatch):
    calls = []
    for op in ('text_generate', 'material_region_edit'):
        with pytest.raises(RuntimeError):
            invoke(op, calls)
    class Text:
        model = limits.MODELS['text']
        @limits.model_call('text')
        def generate(self, prompt):
            calls.append('text')
    with pytest.raises(RuntimeError):
        Text().generate('offline')
    monkeypatch.setitem(limits.RESERVATIONS, 'image', 16)
    with pytest.raises(RuntimeError):
        invoke('template_generate', calls)
    assert not calls and limits.budget_state()['image'] == 4


def test_failure_keeps_attempt_and_pause_on_restart(batch):
    calls = []
    with pytest.raises(TimeoutError):
        invoke('template_generate', calls, fail=True)
    state = limits.budget_state()
    assert state['image'] == 5 and state['reserved_credits'] == 23.2 and state['paused']
    with pytest.raises(RuntimeError):
        limits.initialize_batch(limits.BATCH_ID)
    with pytest.raises(RuntimeError):
        invoke('slide_edit', calls)
    assert calls == ['template_generate']


def test_batch_missing_or_corrupt_cannot_fall_back(batch):
    state = limits.budget_state()
    del state['batches']
    limits._save(limits._root() / 'call-budget.json', state)
    assert limits.execution_reason('image', 'template_generate') == 'authorization_required'


@pytest.mark.parametrize('retry_fails', [False, True])
def test_explicit_retry_reassigns_only_remaining_attempt(batch, retry_fails):
    calls = []
    with pytest.raises(TimeoutError):
        invoke('template_generate', calls, fail=True)
    before = copy.deepcopy(limits.budget_state())
    failed_id = before['last_call_id']
    limits.authorize_template_retry(failed_id)
    approved = limits.budget_state()
    for key in ('image', 'text', 'reserved_credits', 'external_unknown', 'last_call_id', 'last_error_type'):
        assert approved[key] == before[key]
    assert approved['batches'][limits.BATCH_ID]['limits'] == limits.BATCH_LIMITS
    assert limits.execution_reason('image', 'slide_edit') == 'budget_exhausted'
    if retry_fails:
        with pytest.raises(TimeoutError):
            invoke('template_generate', calls, fail=True)
    else:
        invoke('template_generate', calls)
    saved = (limits._root() / 'call-budget.json').read_bytes()
    limits.authorize_template_retry(failed_id)
    assert (limits._root() / 'call-budget.json').read_bytes() == saved
    with pytest.raises(RuntimeError):
        invoke('template_generate', calls)
    with pytest.raises(RuntimeError):
        invoke('slide_edit', calls)
    assert calls == ['template_generate', 'template_generate']
    assert limits.budget_state()['image'] == 6
    assert limits.budget_state()['reserved_credits'] == 27.2
    assert limits.budget_state()['paused'] == retry_fails


def test_retry_rejects_unmatched_failure_and_no_failure(batch):
    before = (limits._root() / 'call-budget.json').read_bytes()
    with pytest.raises(RuntimeError):
        limits.authorize_template_retry('a' * 32)
    assert (limits._root() / 'call-budget.json').read_bytes() == before
    with pytest.raises(TimeoutError):
        invoke('template_generate', [], fail=True)
    before = (limits._root() / 'call-budget.json').read_bytes()
    with pytest.raises(RuntimeError):
        limits.authorize_template_retry('b' * 32)
    assert (limits._root() / 'call-budget.json').read_bytes() == before


@pytest.mark.parametrize('final_fails', [False, True])
def test_final_retry_preserves_costs_and_rejects_fourth(batch, final_fails):
    calls = []
    with pytest.raises(TimeoutError):
        invoke('template_generate', calls, fail=True)
    limits.authorize_template_retry(limits.budget_state()['last_call_id'])
    with pytest.raises(TimeoutError):
        invoke('template_generate', calls, fail=True)
    before = limits.budget_state()
    failed_id = before['last_call_id']
    limits.authorize_final_template_retry(failed_id)
    approved = limits.budget_state()
    assert approved['reserved_credits'] == before['reserved_credits'] == 27.2
    assert approved['batches'][limits.BATCH_ID]['limits'] == limits.BATCH_LIMITS
    with pytest.raises(RuntimeError):
        invoke('slide_edit', calls)
    if final_fails:
        with pytest.raises(TimeoutError):
            invoke('template_generate', calls, fail=True)
    else:
        invoke('template_generate', calls)
    saved = (limits._root() / 'call-budget.json').read_bytes()
    limits.authorize_final_template_retry(failed_id)
    assert (limits._root() / 'call-budget.json').read_bytes() == saved
    with pytest.raises(RuntimeError):
        invoke('template_generate', calls)
    assert len(calls) == 3
    state = limits.budget_state()
    assert state['image'] == 7 and state['text'] == 4
    assert state['reserved_credits'] == 31.2 and state['external_unknown'] == 3
    assert state['paused'] == final_fails


def test_final_retry_requires_exact_failure_and_keeps_credit_ceiling(batch):
    with pytest.raises(RuntimeError):
        limits.authorize_final_template_retry('a' * 32)
    for n in range(2):
        with pytest.raises(TimeoutError):
            invoke('template_generate', [], fail=True)
        if n == 0:
            limits.authorize_template_retry(limits.budget_state()['last_call_id'])
    state = limits.budget_state()
    with pytest.raises(RuntimeError):
        limits.authorize_final_template_retry('b' * 32)
    state['reserved_credits'] = state['batches'][limits.BATCH_ID]['start']['reserved_credits'] + 12
    limits._save(limits._root() / 'call-budget.json', state)
    with pytest.raises(RuntimeError):
        limits.authorize_final_template_retry(state['last_call_id'])
    assert limits.budget_state() == state


@pytest.fixture
def copy_batch(batch, monkeypatch):
    state = limits.budget_state()
    state.update(image=7, reserved_credits=31.2)
    limits._save(limits._root() / 'call-budget.json', state)
    old = copy.deepcopy(state)
    monkeypatch.setenv('PC_BATCH_ID', limits.COPY_BATCH_ID)
    monkeypatch.setenv('PC_IMAGE_OPERATIONS', 'template_generate')
    limits.initialize_batch(limits.COPY_BATCH_ID)
    return old


@pytest.mark.parametrize('fail', [False, True])
def test_copy_batch_single_attempt_retains_history_and_never_refills(copy_batch, fail):
    calls = []
    start = limits.initialize_batch(limits.COPY_BATCH_ID)['start']
    assert start == {'text': 4, 'image': 7, 'reserved_credits': 31.2}
    if fail:
        with pytest.raises(TimeoutError):
            invoke('template_generate', calls, fail=True)
    else:
        invoke('template_generate', calls)
    saved = (limits._root() / 'call-budget.json').read_bytes()
    if fail:
        with pytest.raises(RuntimeError):
            limits.initialize_batch(limits.COPY_BATCH_ID)
    else:
        limits.initialize_batch(limits.COPY_BATCH_ID)
    assert (limits._root() / 'call-budget.json').read_bytes() == saved
    with pytest.raises(RuntimeError):
        invoke('template_generate', calls)
    state = limits.budget_state()
    assert calls == ['template_generate']
    assert (state['image'], state['text'], state['reserved_credits']) == (8, 4, 35.2)
    assert state['batches'][limits.BATCH_ID] == copy_batch['batches'][limits.BATCH_ID]
    assert state['external_unknown'] == 3 and state['paused'] == fail


def test_copy_batch_rejects_other_operations_text_and_excess_cost(copy_batch, monkeypatch):
    calls = []
    for operation in ('slide_edit', 'material_region_edit', 'text_generate'):
        with pytest.raises(RuntimeError):
            invoke(operation, calls)
    class Text:
        model = limits.MODELS['text']
        @limits.model_call('text')
        def generate(self, prompt):
            calls.append('text')
    with pytest.raises(RuntimeError):
        Text().generate('offline')
    monkeypatch.setitem(limits.RESERVATIONS, 'image', 5.01)
    with pytest.raises(RuntimeError):
        invoke('template_generate', calls)
    assert not calls and limits.budget_state()['image'] == 7


def test_copy_batch_rejects_retry_metadata_and_tampered_limit(copy_batch):
    state = limits.budget_state()
    batch = state['batches'][limits.COPY_BATCH_ID]
    batch['template_retry_approval'] = {}
    assert limits.budget_reason(state, 'image', 'template_generate') == 'authorization_required'
    del batch['template_retry_approval']
    batch['limits']['credits'] = 15
    assert limits.budget_reason(state, 'image', 'template_generate') == 'authorization_required'


@pytest.mark.parametrize('fail', [False, True])
def test_region_batch_single_edit_only_preserves_old_batches(copy_batch, monkeypatch, fail):
    invoke('template_generate', [])
    old = copy.deepcopy(limits.budget_state())
    monkeypatch.setenv('PC_BATCH_ID', limits.REGION_BATCH_ID)
    monkeypatch.setenv('PC_IMAGE_OPERATIONS', 'slide_edit')
    limits.initialize_batch(limits.REGION_BATCH_ID)
    calls = []
    for op in ('template_generate', 'text_generate', 'material_region_edit'):
        with pytest.raises(RuntimeError):
            invoke(op, calls)
    assert limits.execution_reason('text') == 'authorization_required'
    monkeypatch.setitem(limits.RESERVATIONS, 'image', 5.01)
    with pytest.raises(RuntimeError):
        invoke('slide_edit', calls)
    monkeypatch.setitem(limits.RESERVATIONS, 'image', 4.0)
    if fail:
        with pytest.raises(TimeoutError):
            invoke('slide_edit', calls, fail=True)
    else:
        invoke('slide_edit', calls)
    saved = (limits._root() / 'call-budget.json').read_bytes()
    if fail:
        with pytest.raises(RuntimeError):
            limits.initialize_batch(limits.REGION_BATCH_ID)
    else:
        limits.initialize_batch(limits.REGION_BATCH_ID)
    with pytest.raises(RuntimeError):
        invoke('slide_edit', calls)
    assert (limits._root() / 'call-budget.json').read_bytes() == saved
    state = limits.budget_state()
    assert calls == ['slide_edit']
    assert (state['image'], state['text'], state['reserved_credits']) == (9, 4, 39.2)
    assert state['paused'] == fail
    for batch_id, data in old['batches'].items():
        assert state['batches'][batch_id] == data


def test_template_business_route_with_batch(app, models, page_data, batch):
    from test_copilot_capabilities import test_template_uses_actual_route_worker_and_exact_reference
    test_template_uses_actual_route_worker_and_exact_reference(app, models, page_data, 'single')
    assert limits.budget_state()['image'] == 5
    assert limits.execution_reason('image', 'template_generate') == 'budget_exhausted'
    assert limits.execution_reason('image', 'slide_edit') is None


def desc():
    return {'text': '--- 页面文字 ---\n每周检查15处用水点\n建立漏水反馈记录\n--- 页面文字结束 ---',
            'extra_fields': {'版式与重点': '放大“15”处用水点', '备注': '2015年，15%和编号15不变'},
            'generated_at': '2015-01-15'}


def test_scoped_numbers_preserve_unrelated_numbers_and_metadata():
    outline = {'points': ['每周检查15处用水点', '另有15个班级', '2015年，15%']}
    d, o, status = plan_edit_sync(desc(), outline, INSTRUCTION)
    assert status == 'synced_by_instruction'
    assert '18处' in d['text'] and d['extra_fields']['版式与重点'] == '放大“18”处用水点'
    assert d['extra_fields']['备注'] == desc()['extra_fields']['备注']
    assert d['generated_at'] == desc()['generated_at']
    assert o['points'] == ['每周检查18处用水点', '另有15个班级', '2015年，15%']


@pytest.mark.parametrize('text', ['没有目标原文', '每周检查15处用水点\n每周检查15处用水点', '每周检查2015处用水点'])
def test_missing_ambiguous_and_boundary_no_change(text):
    d = {'text': text}; o = {'points': ['每周检查15处用水点']}
    new_d, new_o, status = plan_edit_sync(d, o, INSTRUCTION)
    assert (new_d, new_o, status) == (d, o, 'needs_review')


def test_missing_outline_is_not_claimed_fully_synchronized():
    d, o, status = plan_edit_sync(desc(), {'points': ['另有15个班级']}, INSTRUCTION)
    assert '18处' in d['text'] and o['points'] == ['另有15个班级'] and status == 'needs_review'
    d, o, status = plan_edit_sync(desc(), {}, '将背景改为蓝色')
    assert d == desc() and status == 'visual_only'


@pytest.mark.parametrize('fail', [False, True])
def test_actual_edit_route_scoped_sync_and_failure_preserves_version(app, models, page_data, batch, fail):
    from models import db, Page, Task, PageImageVersion
    project_id, page_id, old_path = page_data
    with app.app_context():
        page = db.session.get(Page, page_id)
        page.set_description_content(desc())
        page.set_outline_content({'points': ['每周检查15处用水点', '2015年']})
        db.session.commit()
    models[0].fail = fail
    response = app.test_client().post(f'/api/projects/{project_id}/pages/{page_id}/edit/image', json={'edit_instruction': INSTRUCTION})
    assert response.status_code == 202
    with app.app_context():
        page = db.session.get(Page, page_id)
        task = db.session.get(Task, response.json['data']['task_id'])
        assert task.status == ('FAILED' if fail else 'COMPLETED')
        assert PageImageVersion.query.filter_by(page_id=page_id).count() == (1 if fail else 2)
        if fail:
            assert page.get_description_content() == desc() and page.generated_image_path == old_path
            assert page.get_outline_content()['points'][0] == '每周检查15处用水点'
        else:
            assert '18处' in page.get_description_content()['text']
            assert page.get_outline_content()['points'] == ['每周检查18处用水点', '2015年']
            assert task.get_progress()['description_sync'] == 'synced_by_instruction'

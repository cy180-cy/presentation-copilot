"""PC-010.1: sockets disabled, synthetic inputs, isolated DB and budget only."""
import copy
import json
import re
import secrets
from types import SimpleNamespace
import pytest
from test_admin_console import app, admin, isolated, admin_client, login, live_draft, save, apply
from test_visitor_isolation import client, owner
from services import page_planning as planning, admin_runtime as ar, visitor_access as va
from services.ai_providers import copilot_limits as budget
from services.runtime_config import DEFAULT, validate


@pytest.mark.parametrize('text,expected', [
    ('帮我生成一份AI Agent发展趋势的PPT', None), ('介绍6个产品', None), ('2026年趋势', None), ('销售增长20%', None),
    ('6页', 6), ('6 页', 6), ('做成6页', 6), ('生成6页PPT', 6), ('做一个6页的PPT', 6), ('PPT共6页', 6),
    ('生成 7 页 PPT', 7), ('6 slides', 6), ('6 pages', 6), ('做5页', 5), ('做8页', 8),
    ('第6页介绍产品', None), ('6到8页', None), ('6-8页', None), ('6页左右', None), ('最多6页', None),
    ('6页或8页', None), ('6.5页', None), ('about 6 pages', None), ('-6页', None),
    ('帮我生成一份ppt 内容是agent的记忆 三页', 3), ('生成三页PPT', 3),
    ('PPT三页', 3), ('三 页', 3), ('做两页', 2), ('共十页', 10),
    ('第三页介绍记忆', None), ('第 三页介绍记忆', None), ('第 3页介绍记忆', None),
    ('三到五页', None), ('3到五页', None), ('三到5页', None), ('三-五页', None),
    ('三页左右', None), ('最多三页', None), ('大约三页', None), ('三页或五页', None),
    ('二点五页', None), ('两三页', None), ('负三页', None), ('-三页', None),
    ('三页，3页', 3), ('三页，5页', None), ('介绍三个产品', None),
])
def test_conservative_count(text, expected):
    assert planning.requested_pages(text) == expected
    result = planning.resolve(text, {'default_auto_pages': 6, 'pages': 8})
    assert result['requested_pages'] == expected
    assert result['planned_page_count'] == min(expected if expected is not None else 6, 8)
    assert result['page_count_source'] == ('user_explicit' if expected is not None else 'default_auto')


@pytest.mark.parametrize('text,count', [('十一页', 11), ('二十三页', 23), ('五十页', 50),
                                      ('一百零五页', 105), ('一千零二十页', 1020)])
def test_chinese_count_respects_existing_limits(text, count):
    plan = planning.resolve(text, {'default_auto_pages': 5, 'pages': 10}, 3)
    assert plan['requested_pages'] == plan['target_pages'] == count
    assert plan['resolved_pages'] == 3
    assert plan['page_count_source'] == 'user_explicit'
    assert plan['page_limit_reason'] == 'max_pages_and_budget'


def test_old_config_read_defaults_without_mutating_max_or_policy(admin):
    raw = copy.deepcopy(DEFAULT)
    raw['visitor'].pop('default_auto_pages'); raw['visitor']['pages'] = 5
    cfg = validate(raw, admin.root, source=admin.root/'legacy.json')
    assert cfg['visitor']['default_auto_pages'] == 5 and cfg['visitor']['pages'] == 5
    assert 'default_auto_pages' not in raw['visitor']
    legacy = live_draft(); legacy['visitor'].pop('default_auto_pages')
    with admin.store.transaction() as conn:
        conn.execute('UPDATE pc_admin_control SET active=?,draft=?,revision=1', (json.dumps(legacy),json.dumps(legacy)))
    before = admin.store.control()
    reloaded = ar.effective(admin.base, admin.root)
    assert reloaded['visitor']['default_auto_pages'] == 5 and reloaded['visitor']['pages'] == 2
    assert admin.store.control() == before  # no migration/write-back
    assert planning.resolve('产品介绍', cfg['visitor'])['planned_page_count'] == 5
    assert planning.resolve('做5页', cfg['visitor'])['planned_page_count'] == 5


@pytest.mark.parametrize('invalid',[None, '', 0, -1, 1.5, True, 51])
def test_invalid_default_rejected_on_save_and_apply(app, admin, invalid):
    c = login(admin_client(app,admin)); draft = live_draft()
    draft['visitor'].update(pages=8, default_auto_pages=invalid)
    assert c.put('/api/admin/draft',json={'draft':draft,'revision':0,'draft_revision':0}).status_code == 400
    # Corrupted/stale drafts still cannot pass apply; only the isolated test DB is touched.
    with admin.store.transaction() as conn:
        conn.execute('UPDATE pc_admin_control SET draft=?', (json.dumps(draft),))
    review = c.post('/api/admin/validate',json={}).json['data']
    assert not review['can_apply']
    assert c.post('/api/admin/apply',json={'confirmation':review['confirmation'],'phrase':'确认'}).status_code == 400
    assert not (budget._root()/'call-budget.json').exists()


def test_admin_diff_audit_reload_and_legacy_policy_compatibility(app, admin, monkeypatch):
    c = login(admin_client(app,admin)); draft = live_draft()
    draft['visitor'].update(pages=8, default_auto_pages=5)
    apply(c,draft)
    old_policy = copy.deepcopy(budget.runtime_policy()); old_policy['visitor'].pop('default_auto_pages')
    state = budget.budget_state(); state['runtime_authorizations'][draft['authorization']['id']]['policy'] = old_policy
    budget._save(budget._root()/'call-budget.json',state)
    with app.app_context():
        budget.initialize_runtime(app)  # restart-compatible, not a new authorization
    assert budget.budget_state()['runtime_authorizations'][draft['authorization']['id']]['policy'] == old_policy
    before = (budget._root()/'call-budget.json').read_bytes()
    assert budget.execution_reason('text') is None
    assert (budget._root()/'call-budget.json').read_bytes() == before
    draft['visitor']['default_auto_pages'] = 6
    save(c,draft)
    review = c.post('/api/admin/validate',json={}).json['data']
    assert review['changes']['visitor']['before']['default_auto_pages'] == 5
    assert review['changes']['visitor']['after']['default_auto_pages'] == 6
    response = c.post('/api/admin/apply',json={'confirmation':review['confirmation'],'phrase':'确认'})
    assert response.status_code == 200
    assert ar.effective(admin.base,admin.root)['visitor']['default_auto_pages'] == 6
    assert 'default_auto_pages' in json.dumps(c.get('/api/admin/audit').json)
    assert budget.budget_state()['text'] == state['text'] == 0
    altered = copy.deepcopy(old_policy); altered['authorization']['credits'] += 1
    assert not budget.same_runtime_policy(old_policy,altered)


@pytest.fixture
def planning_client(app, admin, monkeypatch):
    c=login(admin_client(app,admin)); draft=live_draft()
    draft['authorization'].update(credits=1000,text=100,image=100)
    draft['visitor'].update(credits=1000,text=100,image=100,pages=8,default_auto_pages=6)
    apply(c,draft)
    a = client(app)
    return a


def new_project(a, kind='idea', text='AI Agent发展趋势'):
    field = {'idea':'idea_prompt','outline':'outline_text','descriptions':'description_text'}[kind]
    r = a.post('/api/projects',json={'creation_type':kind, field:text})
    assert r.status_code == 201, r.json
    return r.json['data']['project_id']


def submit(a, pid, route, body=None):
    revision = a.get(f'/api/projects/{pid}/input-revision').json['data']['revision']
    return a.post(f'/api/projects/{pid}/generate/{route}',json=body or {},headers={
        'Idempotency-Key':secrets.token_hex(16),'X-PC-Input-Version':revision})


@pytest.fixture
def text_provider(monkeypatch):
    from services.ai_service import AIService
    from services import task_tracking
    from controllers import project_controller
    calls = []
    class Mock:
        model = budget.MODELS['text']
        forced_count = None
        def count(self,prompt):
            match = re.search(r'本次请规划为(\d+)页',prompt)
            self.last_count = self.forced_count or (int(match.group(1)) if match else self.last_count)
            return self.last_count
        @budget.model_call('text')
        def generate_text(self,prompt,**kwargs):
            task_tracking.sent(); calls.append(prompt)
            return json.dumps([{'title':f'模拟页{i+1}','points':['原内容']} for i in range(self.count(prompt))])
        @budget.model_call('text')
        def generate_text_stream(self,prompt,**kwargs):
            task_tracking.sent(); calls.append(prompt)
            yield ''.join(f'## 模拟页{i+1}\n- 原内容\n<!-- PAGE_END -->\n' for i in range(self.count(prompt)))+'<!-- END -->\n'
    provider = Mock()
    service = AIService(text_provider=provider,image_provider=SimpleNamespace(),caption_provider=SimpleNamespace())
    monkeypatch.setattr(project_controller,'get_ai_service',lambda:service)
    return provider,calls


@pytest.mark.parametrize('kind,route,text,target', [
    ('idea','outline','AI Agent发展趋势',6), ('idea','outline/stream','生成5页PPT',5),
    ('idea','outline/stream','生成8页PPT',8), ('outline','outline','介绍6个产品',6),
    ('idea','outline/stream','帮我生成一份ppt 内容是agent的记忆 三页',3),
    ('idea','outline','生成三页PPT',3), ('outline','outline/stream','三页PPT大纲',3),
    ('outline','outline/stream','2026年趋势',6), ('descriptions','outline','销售增长20%',6),
    ('descriptions','outline/stream','生成 7 页 PPT',7),
    ('descriptions','from-description','生成5页PPT',5),
    ('descriptions','outline','三页演示说明',3), ('descriptions','from-description','生成三页PPT',3),
])
def test_real_route_mock_provider_snapshot_and_owner(app, planning_client, text_provider, kind, route, text, target):
    from models import db, Task, TaskTrace, Page, VisitorCall
    a=planning_client; pid=new_project(a,kind,text)
    r=submit(a,pid,route); body=r.get_data(as_text=True)
    assert r.status_code == 200,body
    if route.endswith('stream'): assert 'event: done' in body and 'event: error' not in body,body
    expected_calls = 2 if route == 'from-description' else 1
    assert len(text_provider[1]) == expected_calls
    prompt=text_provider[1][0]; assert f'本次请规划为{target}页' in prompt
    assert prompt.count('页数约束：') == 1
    with app.app_context(),va.scope(owner(app,pid)):
        task=Task.query.filter_by(project_id=pid).one(); snap=json.loads(db.session.get(TaskTrace,task.id).snapshot)
        assert task.status == 'COMPLETED' and Page.query.filter_by(project_id=pid).count() == target
        assert snap['planned_page_count'] == target and snap['default_auto_pages'] == 6 and snap['max_pages'] == 8
        assert snap['prompt_version'] == planning.PROMPT_VERSION
        assert snap['requested_pages'] == planning.requested_pages(text)
        assert snap['pages'][0]['input_text'] == prompt
        usage=VisitorCall.query.all(); assert len(usage)==expected_calls and all(u.task_id==task.id for u in usage)
        detail=a.get(f'/api/projects/{pid}/task-details',query_string={'task_id':task.id}).json['data']
        assert detail['snapshot']['planned_page_count'] == target
        assert {call['call_id'] for call in detail['calls']} == {u.call_id for u in usage}
    assert budget.budget_state()['text'] == expected_calls
    b=client(app); assert b.get(f'/api/projects/{pid}').status_code == 404


@pytest.mark.parametrize('route',['outline','outline/stream','from-description'])
def test_over_max_softens_target_in_same_route(app,planning_client,text_provider,route):
    from models import Task, VisitorCall, VisitorRequest
    a=planning_client; pid=new_project(a,'descriptions' if route=='from-description' else 'idea','生成9页PPT')
    r=submit(a,pid,route)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert len(text_provider[1]) == (2 if route=='from-description' else 1)
    with app.app_context():
        from models import TaskTrace
        snap=json.loads(TaskTrace.query.one().snapshot)
        assert snap['resolved_pages']==8 and snap['page_limit_reason']=='max_pages'



@pytest.mark.parametrize('mode',['preview','paused','budget'])
def test_existing_guards_precede_planning(app,planning_client,text_provider,monkeypatch,mode):
    a=planning_client; pid=new_project(a,text='生成9页PPT')
    if mode=='preview':monkeypatch.setenv('PC_MODE','preview')
    elif mode=='paused':monkeypatch.setenv('PC_ADMIN_PAUSED','1')
    else:monkeypatch.setenv('PC_RUNTIME_INCONSISTENT','1')
    r=submit(a,pid,'outline/stream')
    assert r.status_code >= 400 and r.json['error']['code'] != 'PAGE_LIMIT'
    assert not text_provider[1] and budget.budget_state()['text']==0


@pytest.mark.parametrize('route',['outline','outline/stream','from-description'])
def test_model_overflow_keeps_old_pages_without_retry(app,planning_client,text_provider,route):
    from models import db,Page,Task
    a=planning_client; pid=new_project(a,'descriptions' if route=='from-description' else 'idea')
    original=a.post(f'/api/projects/{pid}/pages',json={'order_index':0,'outline_content':{'title':'保留的原页'}}).json['data']['page_id']
    text_provider[0].forced_count=9
    r=submit(a,pid,route); body=r.get_data(as_text=True)
    assert ('event: error' in body if route.endswith('stream') else r.status_code==400),body
    assert len(text_provider[1])==1 and budget.budget_state()['text']==1
    with app.app_context(),va.scope(owner(app,pid)):
        assert Page.query.filter_by(project_id=pid).count()==1
        assert db.session.get(Page,original).get_outline_content()['title']=='保留的原页'
        assert Task.query.filter_by(project_id=pid).one().status=='FAILED'

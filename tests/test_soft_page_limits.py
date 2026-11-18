"""PC-010.2 isolated workflow holds; socket-disabled, synthetic images only."""
import json
import copy
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
import pytest
from test_admin_console import app,admin,isolated,admin_client,login,live_draft,apply
from test_page_planning import text_provider,new_project,submit
from test_task_tracking import queued
from test_visitor_isolation import client,owner
from services import generation_budget as gb,page_planning as pp,visitor_access as va
from services.ai_providers import copilot_limits as budget


def setup(app,admin,credits=1000,text=100,image=100,visitor=None,parallel=False):
    c=login(admin_client(app,admin));d=live_draft()
    d['authorization'].update(credits=credits,text=text,image=image)
    d['visitor'].update(credits=credits,text=text,image=image,pages=10,default_auto_pages=5)
    if visitor:d['visitor'].update(visitor)
    apply(c,d);a=client(app)
    if parallel:assert a.put('/api/product/preferences',json={'description_generation_mode':'parallel'}).status_code==200
    return a


@pytest.mark.parametrize('prompt,images,credits,target,actual,reason',[
    ('节水',10,1000,5,5,'none'),('节水',3,1000,5,3,'budget'),
    ('生成8页PPT',10,1000,8,8,'none'),('生成12页PPT',10,1000,12,10,'max_pages'),
    ('8 slides',6,1000,8,6,'budget'),('12 pages',7,1000,12,7,'max_pages_and_budget'),
    ('介绍6个产品',10,1000,5,5,'none'),('生成8页PPT',10,25.6,8,6,'budget')])
def test_real_entry_target_and_snapshot(app,admin,text_provider,prompt,images,credits,target,actual,reason):
    a=setup(app,admin,credits=credits,image=images);pid=new_project(a,text=prompt)
    r=submit(a,pid,'outline');assert r.status_code==200,r.json
    assert len(r.json['data']['pages'])==actual
    from models import TaskTrace,db,Task
    with app.app_context(),va.scope(owner(app,pid)):
        snap=json.loads(TaskTrace.query.one().snapshot)
        assert (snap['target_pages'],snap['resolved_pages'],snap['page_limit_reason'])==(target,actual,reason)
    assert len(text_provider[1])==1
    state=budget.budget_state();held=gb.held(state)
    assert state['reserved_credits']==budget.RESERVATIONS['text']
    assert held['image']==actual and held['text']==1
    assert ('本次已完成' in (pp.completion_message(snap) or '')) == (reason!='none')
    assert a.get(f'/api/projects/{pid}').json['data']['page_limit_notice'] is None


@pytest.mark.parametrize('gate',['no_image_space','no_text_space','preview','paused','expired','unknown'])
def test_zero_send(app,admin,text_provider,monkeypatch,gate):
    a=setup(app,admin);pid=new_project(a)
    if gate=='preview':monkeypatch.setenv('PC_MODE','preview')
    elif gate=='paused':monkeypatch.setenv('PC_ADMIN_PAUSED','1')
    elif gate=='expired':
        policy=budget.runtime_policy();policy['authorization']['expires_at']=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
        monkeypatch.setenv('PC_RUN_POLICY',json.dumps(policy))
    else:
        cap=app.config['PC_VISITOR_LIMITS']
        if gate=='no_image_space':monkeypatch.setitem(cap,'image',0)
        elif gate=='no_text_space':monkeypatch.setitem(cap,'text',1)
        else:
            s=budget.budget_state();s['external_unknown']=999;budget._save(budget._root()/'call-budget.json',s)
    before=budget.budget_state();r=submit(a,pid,'outline')
    assert r.status_code>=400 and not text_provider[1]
    assert budget.budget_state()==before


def test_full_parallel_workflow_converts_holds_and_saves_notice(app,admin,text_provider,queued,monkeypatch):
    from controllers import project_controller
    from models import db,Project,Task,TaskTrace,VisitorCall,PageImageVersion
    a=setup(app,admin,image=3,parallel=True);pid=new_project(a)
    # queued fixture installs its description service; restore outline fake first.
    from services.ai_service import AIService
    from types import SimpleNamespace
    outline=AIService(text_provider=text_provider[0],image_provider=SimpleNamespace(),caption_provider=SimpleNamespace())
    monkeypatch.setattr(project_controller,'get_ai_service',lambda:outline)
    r=submit(a,pid,'outline');assert r.status_code==200,r.json
    with app.app_context(),va.scope(owner(app,pid)):
        db.session.get(Project,pid).template_style='仅离线测试';db.session.commit()
    ai,calls,jobs,run=queued
    monkeypatch.setattr(project_controller,'get_ai_service',lambda:ai)
    r=submit(a,pid,'descriptions');assert r.status_code==202,r.json
    run();assert not jobs
    r=submit(a,pid,'images');assert r.status_code==202,r.json
    run()
    with app.app_context(),va.scope(owner(app,pid)):
        assert all(t.status=='COMPLETED' for t in Task.query.all())
        assert PageImageVersion.query.count()==3
        actual=VisitorCall.query.all();assert len(actual)==7
        assert len({x.call_id for x in actual})==7
        assert all(json.loads(t.snapshot).get('budget_plan_id') for t in TaskTrace.query.all())
    s=budget.budget_state();assert s['text']==4 and s['image']==3
    assert s['reserved_credits']==round(4*budget.RESERVATIONS['text']+3*budget.RESERVATIONS['image'],4)
    assert gb.held(s)=={'text':0,'image':0,'credits':0}
    result=a.get(f'/api/projects/{pid}').json['data']
    assert result['page_limit_notice']=='默认计划生成5页，受当前可用额度限制，本次已完成3页。'


def test_idempotency_restart_and_last_allowance(app,admin,text_provider):
    from models import Task,TaskTrace
    a=setup(app,admin,credits=21.6,image=5);pid=new_project(a)
    revision=a.get(f'/api/projects/{pid}/input-revision').json['data']['revision'];key=secrets.token_hex(16)
    headers={'Idempotency-Key':key,'X-PC-Input-Version':revision}
    def post():
        return a.post(f'/api/projects/{pid}/generate/outline',json={},headers=headers)
    # Independent clients, same normal browser cookie; simultaneous request replay.
    b=app.test_client();b.set_cookie(va.COOKIE,a.get_cookie(va.COOKIE).value);b.environ_base.update(a.environ_base)
    with ThreadPoolExecutor(2) as pool:
        futures=[pool.submit(post),pool.submit(lambda:b.post(f'/api/projects/{pid}/generate/outline',json={},headers=headers))]
        results=[f.result() for f in futures]
    assert all(r.status_code in (200,202) for r in results),[r.json for r in results]
    assert len(text_provider[1])==1
    before=copy.deepcopy(budget.budget_state())
    with app.app_context():budget.initialize_runtime(app)
    assert budget.budget_state()==before
    assert post().status_code==200 and len(text_provider[1])==1
    other=client(app);otherpid=new_project(other)
    assert submit(other,otherpid,'outline').status_code>=400
    with app.app_context():assert Task.query.count()==TaskTrace.query.count()==1


def test_failure_keeps_attempt_and_releases_only_unsent(app,admin,text_provider):
    a=setup(app,admin);pid=new_project(a);text_provider[0].forced_count=11
    assert submit(a,pid,'outline').status_code==400
    s=budget.budget_state();assert s['text']==1 and s['reserved_credits']==budget.RESERVATIONS['text']
    p=next(iter(s['generation_plans'].values()));assert p['state']=='aborted'
    assert p['released_unsent']=={'text':1,'image':5} and gb.held(s)['credits']==0


def test_visitor_and_global_minimum(app,admin,text_provider):
    a=setup(app,admin,image=7,visitor={'image':3});pid=new_project(a,text='8 pages')
    r=submit(a,pid,'outline');assert len(r.json['data']['pages'])==3


def test_defaults_soft_max_and_business_numbers():
    for text in ['介绍8个产品','2026年趋势','增长20%']:
        p=pp.resolve(text,{'pages':10});assert p['requested_pages'] is None and p['resolved_pages']==5
    assert pp.resolve('12 pages',{'pages':10},7)['page_limit_reason']=='max_pages_and_budget'
    assert pp.resolve('',{'pages':3,'default_auto_pages':5},3)['resolved_pages']==3


@pytest.mark.parametrize('fail,paused',[(False,False),(True,False),(False,True)])
def test_stream_workflow_failure_pause_and_unknown_cost(app,admin,text_provider,queued,monkeypatch,fail,paused):
    from models import db,Project,Task,VisitorCall
    from controllers import project_controller
    from services.ai_service import AIService
    from services import task_tracking as trace
    from types import SimpleNamespace
    a=setup(app,admin,image=3);pid=new_project(a)
    outline=AIService(text_provider=text_provider[0],image_provider=SimpleNamespace(),caption_provider=SimpleNamespace())
    monkeypatch.setattr(project_controller,'get_ai_service',lambda:outline)
    r=submit(a,pid,'outline/stream');assert 'event: done' in r.get_data(as_text=True)
    ai,calls,jobs,run=queued
    class Stream:
        model=budget.MODELS['text']
        @budget.model_call('text')
        def generate_text_stream(self,prompt,**kw):
            trace.sent();calls.append({'model':self.model})
            yield '<!-- BEGIN -->\n'+''.join('离线描述\n<!-- PAGE_END -->\n' for _ in range(3))+'<!-- END -->\n'
    ai.text_provider=Stream()
    monkeypatch.setattr(project_controller,'get_ai_service',lambda:ai)
    r=submit(a,pid,'descriptions/stream');assert 'event: done' in r.get_data(as_text=True),r.get_data(as_text=True)
    assert budget.budget_state()['text']==2 and gb.held(budget.budget_state())['image']==3
    with app.app_context(),va.scope(owner(app,pid)):
        db.session.get(Project,pid).template_style='离线';db.session.commit()
    ai.image_provider.fail=fail
    r=submit(a,pid,'images');assert r.status_code==202,r.json
    if paused:monkeypatch.setenv('PC_ADMIN_PAUSED','1')
    run();s=budget.budget_state()
    assert s['image']==(0 if paused else 1 if fail else 3)
    assert gb.held(s)['credits']==0
    if fail:assert s['paused'] and s['reserved_credits']==5.6
    with app.app_context(),va.scope(owner(app,pid)):
        assert VisitorCall.query.count()==s['text']+s['image']
        assert Task.query.order_by(Task.created_at.desc()).first().status==('FAILED' if fail or paused else 'COMPLETED')


def test_two_visitors_concurrent_last_full_plan(app,admin,text_provider):
    a=setup(app,admin,credits=21.6,image=5);b=client(app)
    pa,pb=new_project(a),new_project(b)
    with ThreadPoolExecutor(2) as pool:
        responses=list(pool.map(lambda args:submit(*args,'outline'),[(a,pa),(b,pb)]))
    assert sorted(r.status_code for r in responses)==[200,400]
    assert len(text_provider[1])==1 and gb.held(budget.budget_state())['image']==5


def test_pause_resume_keeps_fully_funded_plan(app,admin,text_provider):
    a=setup(app,admin,credits=21.6,text=2,image=5);pid=new_project(a)
    assert submit(a,pid,'outline').status_code==200
    before=copy.deepcopy(budget.budget_state())
    c=login(admin_client(app,admin,False))
    assert c.post('/api/admin/pause',json={'phrase':'暂停新生成'}).status_code==200
    v=c.get('/api/admin/overview').json['data']
    assert v['remaining_reservation']==0
    r=c.post('/api/admin/resume',json={'confirmation':v['resume_confirmation'],'phrase':'恢复已应用授权'})
    assert r.status_code==200,r.json
    assert budget.budget_state()==before and len(text_provider[1])==1


def test_image_subsets_share_plan_without_repeat(app,admin,text_provider,queued,monkeypatch):
    from models import db,Project,Page
    from controllers import project_controller
    from services.ai_service import AIService
    from types import SimpleNamespace
    a=setup(app,admin,image=3);pid=new_project(a)
    outline=AIService(text_provider=text_provider[0],image_provider=SimpleNamespace(),caption_provider=SimpleNamespace())
    monkeypatch.setattr(project_controller,'get_ai_service',lambda:outline)
    assert submit(a,pid,'outline').status_code==200
    with app.app_context(),va.scope(owner(app,pid)):
        db.session.get(Project,pid).template_style='离线样式'
        pages=Page.query.filter_by(project_id=pid).order_by(Page.order_index).all()
        ids=[p.id for p in pages]
        for p in pages:p.set_description_content({'text':'人工准备离线说明'})
        db.session.commit()
    ai,calls,jobs,run=queued
    monkeypatch.setattr(project_controller,'get_ai_service',lambda:ai)
    assert submit(a,pid,'images',{'page_ids':ids[:1]}).status_code==202
    run()
    assert budget.budget_state()['image']==1
    assert submit(a,pid,'images',{'page_ids':ids[:1]}).status_code==409
    assert not jobs and budget.budget_state()['image']==1
    assert submit(a,pid,'images',{'page_ids':ids[1:]}).status_code==202
    run()
    assert budget.budget_state()['image']==3 and gb.held(budget.budget_state())['credits']==0

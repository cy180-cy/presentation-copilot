"""PC-010: synthetic administrator/visitors/SQLite/ledger; run with --disable-socket."""
import copy
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from contextlib import contextmanager
import pytest
from test_copilot_capabilities import isolated, runner
from test_task_tracking import queued
from test_visitor_isolation import client, project, page, call, owner
from services import admin_runtime as ar, visitor_access as va
from services.admin_store import Store, COOKIE
from services.ai_providers import copilot_limits as budget
from services.runtime_config import DEFAULT, validate

PASSWORD='isolated-test-password-only'


@pytest.fixture(scope='module')
def app(tmp_path_factory):
    root=tmp_path_factory.mktemp('pc010-admin')
    (root/'data').mkdir(); (root/'run').mkdir()
    import dotenv
    dotenv.load_dotenv=lambda *args,**kwargs:False
    os.environ.update(runner.PRESET)
    os.environ.update(OPENAI_API_KEY='offline-placeholder',GOOGLE_API_KEY='offline-placeholder',
        IMAGE_CAPTION_MODEL_SOURCE='openai',IMAGE_CAPTION_MODEL='disabled',PC_CONFIG_READY='1',PC_TEXT_CONFIG_READY='1',PC_IMAGE_CONFIG_READY='1',
        MAX_IMAGE_WORKERS='1',MAX_DESCRIPTION_WORKERS='1',CUDA_VISIBLE_DEVICES='')
    os.environ.update(PC_MODE='preview',DATABASE_PATH=str(root/'data/database.db'),UPLOAD_FOLDER=str(root/'uploads'))
    app=runner.create_local_app(False,5011)
    cfg=validate({**copy.deepcopy(DEFAULT),'data_dir':str(root),'secret_file':str(root/'.env.copilot')},root,source=root/'runtime.json')
    from controllers.admin_controller import install
    install(app,cfg,root,lambda:{'ready':True,'source':'.env.copilot','models':[]},lambda _cfg:None)
    return app


@pytest.fixture(autouse=True)
def admin(app,isolated,monkeypatch,tmp_path):
    from models import db
    with app.app_context():db.drop_all();db.create_all()
    ctrl=app.extensions['pc_admin'];ctrl.store.migrate()
    with ctrl.store.transaction() as c:
        for table in ('pc_admin_identity','pc_admin_sessions','pc_admin_audit','pc_admin_control'):c.execute('DELETE FROM '+table)
        c.execute('INSERT INTO pc_admin_control(id) VALUES(1)')
    ctrl.current=copy.deepcopy(ctrl.base);ctrl.pending=tmp_path/'budget/admin-apply.pending.json'
    for k in ('PC_ADMIN_PAUSED','PC_ADMIN_APPLYING','PC_RUNTIME_INCONSISTENT','PC_RUN_POLICY','PC_BATCH_ID','PC_DRAINING'):
        monkeypatch.delenv(k,raising=False)
    monkeypatch.setenv('PC_MODE','preview')
    monkeypatch.setitem(app.config,'PC_VISITOR_LIMITS',copy.deepcopy(DEFAULT['visitor']))
    monkeypatch.setattr(ctrl,'resource_ok',lambda:True)
    yield ctrl


def admin_client(app,admin,initialize=True):
    if initialize:admin.store.initialize(PASSWORD)
    c=app.test_client();r=c.get('/api/admin/session');assert r.status_code==200
    c.environ_base['HTTP_ORIGIN']='http://127.0.0.1:5011'
    c.environ_base['HTTP_X_PC_ADMIN_CSRF']=r.json['data']['csrf']
    return c


def login(c):
    r=c.post('/api/admin/login',json={'password':PASSWORD});assert r.status_code==200,r.json
    c.environ_base['HTTP_X_PC_ADMIN_CSRF']=r.json['data']['csrf']
    return c


def live_draft():
    now=datetime.now(timezone.utc)
    return {'name':'OFFLINE ONLY','mode':'live','authorization':{'id':'offline-admin-001',
        'not_before':(now-timedelta(minutes=5)).isoformat(),'expires_at':(now+timedelta(hours=2)).isoformat(),
        'credits':12,'text':1,'image':3,'roles':['text','image'],'operations':['text_generate','template_generate','slide_edit']},
        'visitor':{'window_seconds':86400,'text':1,'image':2,'credits':8,'pages':2,'default_auto_pages':2}}


def save(c,draft):
    state=c.get('/api/admin/overview').json['data']
    r=c.put('/api/admin/draft',json={'draft':draft,'revision':state['revision'],'draft_revision':state['draft_revision']})
    assert r.status_code==200,r.json
    return r.json['data']


def apply(c,draft=None):
    draft=draft or live_draft();save(c,draft)
    v=c.post('/api/admin/validate',json={});assert v.status_code==200,v.json
    assert v.json['data']['can_apply'],v.json
    r=c.post('/api/admin/apply',json={'confirmation':v.json['data']['confirmation'],'phrase':'确认'})
    assert r.status_code==200,r.json
    return r.json['data']


def test_uninitialized_and_no_default_password(app,admin):
    c=admin_client(app,admin,False)
    assert c.get('/api/admin/session').json['data']['initialized'] is False
    assert c.post('/api/admin/login',json={'password':PASSWORD}).status_code==401
    assert c.get('/api/admin/overview').status_code==401
    assert not (budget._root()/'call-budget.json').exists()


def test_auth_rotation_logout_visitor_separation_and_cookie_flags(app,admin):
    c=client(app);p=project(c);old_visitor=c.get_cookie(va.COOKIE).value
    admin.store.initialize(PASSWORD)
    session=c.get('/api/admin/session');assert 'HttpOnly' in session.headers['Set-Cookie'] and 'SameSite=Strict' in session.headers['Set-Cookie']
    c.environ_base.update(HTTP_ORIGIN='http://127.0.0.1:5011',HTTP_X_PC_ADMIN_CSRF=session.json['data']['csrf'])
    old=c.get_cookie(COOKIE,path='/api/admin').value
    assert c.get('/api/admin/overview').status_code==401
    login(c);assert c.get_cookie(COOKIE,path='/api/admin').value!=old
    assert c.get('/api/admin/overview').status_code==200
    assert c.get('/api/projects/'+p).status_code==200
    assert c.get_cookie(va.COOKIE).value==old_visitor
    b=client(app);private_b=project(b)
    assert c.get('/api/projects/'+private_b).status_code==404
    assert c.get('/api/settings').status_code==403
    attacker=app.test_client();attacker.set_cookie(COOKIE,old,path='/api/admin');assert attacker.get('/api/admin/overview').status_code==401
    attacker.set_cookie(COOKIE,old_visitor,path='/api/admin');assert attacker.get('/api/admin/tasks').status_code==401
    assert c.post('/api/admin/logout',json={}).status_code==200
    assert c.get('/api/admin/overview').status_code==401 and c.get('/api/projects/'+p).status_code==200


def test_login_cooldown_reset_expiry_and_csrf(app,admin):
    c=admin_client(app,admin)
    for _ in range(5):assert c.post('/api/admin/login',json={'password':'wrong'}).status_code==401
    assert c.post('/api/admin/login',json={'password':PASSWORD}).status_code==401
    with admin.store.transaction() as db:db.execute('UPDATE pc_admin_identity SET blocked_until=?,failures=0',(time.time()-1,))
    login(c)
    assert c.post('/api/admin/pause',json={'phrase':'暂停新生成'},headers={'Origin':'https://other.invalid'}).status_code==403
    assert c.post('/api/admin/pause',json={'phrase':'暂停新生成'},headers={'X-PC-Admin-CSRF':'wrong'}).status_code==403
    token=c.get_cookie(COOKIE,path='/api/admin').value
    with admin.store.transaction() as db:db.execute('UPDATE pc_admin_sessions SET expires=0')
    assert c.get('/api/admin/overview').status_code==401
    c=admin_client(app,admin,False);login(c)
    admin.store.initialize(PASSWORD,reset=True)
    assert c.get('/api/admin/overview').status_code==401


def test_draft_preview_whitelist_conflicts_and_no_side_effect(app,admin,queued):
    c=login(admin_client(app,admin));draft=live_draft()
    first=save(c,draft)
    assert os.getenv('PC_MODE')=='preview' and not queued[1]
    assert not (budget._root()/'call-budget.json').exists()
    assert c.put('/api/admin/draft',json={'draft':draft,'revision':0,'draft_revision':0}).status_code==400
    for key in ('secret_file','data_dir','port','resources','owner_id','api_key'):
        bad={**draft,key:'not-allowed'}
        assert c.put('/api/admin/draft',json={'draft':bad,'revision':0,'draft_revision':first['draft_revision']}).status_code==400
    assert c.get('/api/settings').status_code==403
    assert 'no-store' in c.get('/api/admin/overview').headers['Cache-Control']


@pytest.mark.parametrize('kind',['missing_budget','expired','exhausted','bad_number','missing_key','resource','paused'])
def test_invalid_apply_does_not_call_or_modify_ledger(app,admin,queued,monkeypatch,kind):
    c=login(admin_client(app,admin));draft=live_draft()
    state={'text':0,'image':0,'reserved_credits':0,'paused':False,'external_unknown':0}
    if kind=='missing_budget':draft['authorization']['credits']=0
    elif kind=='expired':draft['authorization']['expires_at']=(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()
    elif kind=='exhausted':state.update(image=3,reserved_credits=12)
    elif kind=='bad_number':draft['visitor']['image']=-1
    elif kind=='missing_key':monkeypatch.setattr(admin,'credentials',lambda:{'ready':False})
    elif kind=='resource':monkeypatch.setattr(admin,'resource_ok',lambda:False)
    elif kind=='paused':state['paused']=True
    budget._save(budget._root()/'call-budget.json',state);before=(budget._root()/'call-budget.json').read_bytes()
    if kind=='bad_number':
        assert c.put('/api/admin/draft',json={'draft':draft,'revision':0,'draft_revision':0}).status_code==400
    else:
        save(c,draft);check=c.post('/api/admin/validate',json={}).json['data'];assert not check['can_apply']
        assert c.post('/api/admin/apply',json={'confirmation':check['confirmation'],'phrase':'确认'}).status_code==400
    assert os.getenv('PC_MODE')=='preview' and not queued[1]
    assert (budget._root()/'call-budget.json').read_bytes()==before


def test_live_call_tracking_pause_queue_and_free_access(app,admin,queued):
    from models import db,TaskTrace,PageImageVersion,VisitorCall
    c=login(admin_client(app,admin));a=client(app);p=project(a);pg,old=page(app,a,p)
    apply(c)
    assert a.get('/api/product/status').json['data']['mode']=='live'
    result=call(a,p,pg);assert result.status_code==202,result.json
    task_id=result.json['data']['task_id']
    with app.app_context():original=db.session.get(TaskTrace,task_id).snapshot
    queued[3]();assert len(queued[1])==1 and os.getenv('PC_MODE')=='live'
    state=budget.budget_state();assert state['image']==1 and state['reserved_credits']==4
    with app.app_context():
        assert db.session.get(VisitorCall,state['last_call_id']).task_id==task_id
        assert db.session.get(TaskTrace,task_id).snapshot==original
        assert PageImageVersion.query.filter_by(page_id=pg).count()==2
    detail=c.get('/api/admin/tasks/'+task_id).json['data'];assert detail['calls'][0]['call_id']==state['last_call_id']
    assert detail['calls'][0]['actual_credits'] is None
    assert 'edit_instruction' not in json.dumps(detail)
    assert app.test_client().get('/api/admin/tasks/'+task_id).status_code==401
    next_task=call(a,p,pg);assert next_task.status_code==202
    assert c.post('/api/admin/pause',json={'phrase':'暂停新生成'}).status_code==200
    queued[3]();assert len(queued[1])==1 and budget.budget_state()['image']==1
    assert a.get('/api/projects/'+p).status_code==200
    assert a.get('/api/product/status').json['data']['capabilities']['text']['reason']=='admin_paused'


def test_resume_reload_rename_no_credit_reset(app,admin,queued):
    c=login(admin_client(app,admin));a=client(app);p=project(a);pg,_=page(app,a,p)
    apply(c);assert call(a,p,pg).status_code==202;queued[3]()
    state=copy.deepcopy(budget.budget_state())
    before=admin.store.control();assert c.post('/api/admin/logout',json={}).status_code==200
    assert os.getenv('PC_MODE')=='live'
    c=login(admin_client(app,admin,False))
    saved=ar.effective(admin.base,admin.root)
    assert saved['mode']=='live' and saved['authorization']['id']=='offline-admin-001'
    budget.initialize_runtime(app);assert budget.budget_state()==state
    d=live_draft();d['authorization']=copy.deepcopy(saved['authorization']);d['name']='renamed offline'
    apply(c,d);assert budget.budget_state()==state
    c.post('/api/admin/pause',json={'phrase':'暂停新生成'})
    v=c.get('/api/admin/overview').json['data']
    assert c.post('/api/admin/resume',json={'confirmation':v['resume_confirmation'],'phrase':'恢复已应用授权'}).status_code==200
    assert budget.budget_state()==state
    d['authorization']['credits']=16;d['authorization']['image']=4
    apply(c,d);current=budget.budget_state()
    assert current['image']==state['image'] and current['reserved_credits']==state['reserved_credits']
    assert len(current['runtime_authorizations']['offline-admin-001']['history'])==1


@pytest.mark.parametrize('fault',['ledger','database','provider'])
def test_apply_failure_preserves_effective_config_and_budget(app,admin,queued,monkeypatch,fault):
    c=login(admin_client(app,admin));save(c,live_draft());check=c.post('/api/admin/validate',json={}).json['data']
    original_save=budget._save;original_tx=admin.store.transaction
    if fault=='ledger':
        def save_fail(path,value):
            if path.name=='call-budget.json':raise OSError('synthetic')
            return original_save(path,value)
        monkeypatch.setattr(budget,'_save',save_fail)
    elif fault=='provider':
        monkeypatch.setattr(admin,'prepare_models',lambda cfg:(_ for _ in ()).throw(RuntimeError('synthetic')))
    else:
        original_audit=admin.store.audit
        def audit_fail(conn,action,summary,actor='developer'):
            if action=='apply':raise OSError('synthetic write failure')
            return original_audit(conn,action,summary,actor)
        monkeypatch.setattr(admin.store,'audit',audit_fail)
    response=c.post('/api/admin/apply',json={'confirmation':check['confirmation'],'phrase':'确认'})
    assert response.status_code>=400
    assert os.getenv('PC_MODE')=='preview' and admin.store.control()['active'] is None
    assert not (budget._root()/'call-budget.json').exists() and not queued[1]


def test_migration_repeat_and_reset_no_visitor_or_history_change(app,admin):
    a=client(app);p=project(a);cookie=a.get_cookie(va.COOKIE).value
    admin.store.initialize(PASSWORD);admin.store.migrate();admin.store.migrate()
    with pytest.raises(ValueError):admin.store.initialize(PASSWORD)
    assert a.get('/api/projects/'+p).status_code==200 and a.get_cookie(va.COOKIE).value==cookie


def test_preview_to_live_prepares_real_factory_without_network(app,admin,monkeypatch):
    # Real reload callback + synthetic credentials/factories, no .env access.
    calls=[]
    monkeypatch.setattr(runner,'config_values',lambda:{**runner.PRESET,'TEXT_API_KEY':'synthetic-only','IMAGE_API_KEY':'synthetic-only'})
    from services import ai_providers, ai_service_manager
    for role in ('text','image'):
        monkeypatch.setattr(ai_providers,'get_'+role+'_provider',lambda r=role:calls.append((r,os.getenv('PC_MODE'))))
    monkeypatch.setattr(ai_service_manager,'clear_ai_service_cache',lambda:calls.append(('cache','cleared')))
    monkeypatch.setattr(admin,'prepare_models',lambda cfg:runner.prepare_runtime_models(app,cfg))
    c=login(admin_client(app,admin));apply(c)
    assert calls==[('text','live'),('image','live'),('cache','cleared')]
    assert os.getenv('IMAGE_API_KEY')=='synthetic-only'


def test_expiry_pause_and_old_snapshot_stop_without_provider(app,admin,queued,monkeypatch):
    c=login(admin_client(app,admin));a=client(app);p=project(a);pg,_=page(app,a,p)
    apply(c);assert call(a,p,pg).status_code==202
    monkeypatch.setattr('services.runtime_config.authorized_now',lambda _:False)
    queued[3]();assert not queued[1] and budget.budget_state()['image']==0
    assert budget.execution_reason('image')=='authorization_expired'

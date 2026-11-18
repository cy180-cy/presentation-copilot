"""PC-009: synthetic policies, isolated ledgers/SQLite, simulated providers; no sockets."""
import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from test_visitor_isolation import app, isolated, trial, queued, client, project, page, owner, call
from services import visitor_access as va
from services.ai_providers import copilot_limits as budget
from services.runtime_config import DEFAULT, load
from managed_server import DataLock, recover_interrupted, boundaries
from runtime_maintenance import backup, restore
from release_files import source_allowed


def policy():
    now=datetime.now(timezone.utc)
    return {'authorization':{'id':'offline-runtime-001','not_before':(now-timedelta(hours=1)).isoformat(),
        'expires_at':(now+timedelta(days=1)).isoformat(),'credits':16,'text':0,'image':5,
        'roles':['image'],'operations':['text_generate','template_generate','slide_edit']},
        'visitor':{**DEFAULT['visitor'],'image':2,'credits':8}}


@pytest.fixture
def runtime(app,monkeypatch):
    test_root=budget._root().parent/'run'
    monkeypatch.setattr(budget,'_root',lambda:test_root)
    p=policy();monkeypatch.setenv('PC_RUN_POLICY',json.dumps(p))
    monkeypatch.delenv('PC_BATCH_ID',raising=False)
    monkeypatch.delenv('PC_RUNTIME_INCONSISTENT',raising=False)
    monkeypatch.setitem(app.config,'PC_VISITOR_LIMITS',p['visitor'])
    history={'text':3,'image':3,'reserved_credits':8,'external_unknown':0,'paused':False,
             'batches':{'old':{'unchanged':True}}}
    budget._save(budget._root()/'call-budget.json',history)
    budget.initialize_runtime(app)
    a,b=client(app),client(app);pa,pb=project(a),project(b);pg,old=page(app,a,pa)
    return p,a,b,pa,pb,pg,old,history


def test_persistent_absolute_caps_call_id_trace_and_private_result(app,runtime,queued,monkeypatch):
    from models import db,VisitorCall,TaskTrace
    p,a,b,pa,pb,pg,old,history=runtime
    first=call(a,pa,pg,key='same-runtime-request');assert first.status_code==202
    assert call(a,pa,pg,key='same-runtime-request').json==first.json
    queued[3]();assert len(queued[1])==1
    state=budget.budget_state();assert state['image']==4 and state['reserved_credits']==12
    with app.app_context():
        record=db.session.get(VisitorCall,state['last_call_id'])
        assert record.task_id==first.json['data']['task_id'] and record.visitor_id==owner(app,pa)
        trace=db.session.get(TaskTrace,record.task_id)
        assert state['last_call_id'] in trace.calls
    assert b.get(f'/api/projects/{pa}/task-details?task_id={first.json["data"]["task_id"]}').status_code==404
    before=copy.deepcopy(state);budget.initialize_runtime(app);assert budget.budget_state()==before
    second=call(a,pa,pg);assert second.status_code==202;queued[3]()
    assert len(queued[1])==2 and budget.budget_state()['image']==5
    budget.initialize_runtime(app)
    assert call(a,pa,pg).status_code>=400 and len(queued[1])==2
    # A different authorization id does not turn an absolute stop-line into a fresh grant.
    p['authorization']['id']='offline-renamed-002';monkeypatch.setenv('PC_RUN_POLICY',json.dumps(p))
    budget.initialize_runtime(app)
    assert budget.execution_reason('image')=='budget_exhausted'
    assert budget.budget_state()['batches']==history['batches']


@pytest.mark.parametrize('block',['preview','expired','text','operation','budget','paused','draining'])
def test_refusals_send_nothing(app,runtime,queued,monkeypatch,block):
    p,a,b,pa,pb,pg,old,_=runtime
    if block=='preview':monkeypatch.setenv('PC_MODE','preview')
    elif block=='expired':
        p['authorization']['expires_at']=(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()
        monkeypatch.setenv('PC_RUN_POLICY',json.dumps(p))
    elif block=='text':monkeypatch.setenv('PC_MODEL_ROLES','text')
    elif block=='operation':monkeypatch.setenv('PC_IMAGE_OPERATIONS','slide_edit')
    elif block=='draining':monkeypatch.setenv('PC_DRAINING','1')
    else:
        state=budget.budget_state()
        if block=='paused':state['paused']=True
        else:state['external_unknown']=10
        budget._save(budget._root()/'call-budget.json',state)
    response=call(a,pa,pg)
    if response.status_code==202:queued[3]()
    assert not queued[1] and budget.budget_state()['image']==3


def test_failed_provider_preserves_unknown_version_and_refuses_restart(app,runtime,queued):
    from models import db,Page,PageImageVersion
    p,a,b,pa,pb,pg,old,_=runtime
    queued[0].image_provider.fail=True
    response=call(a,pa,pg);assert response.status_code==202;queued[3]()
    state=budget.budget_state();assert state['image']==4 and state['reserved_credits']==12 and state['paused']
    with pytest.raises(RuntimeError):budget.initialize_runtime(app)
    with app.app_context(),va.scope(owner(app,pa)):
        assert PageImageVersion.query.filter_by(page_id=pg).count()==1
        assert db.session.get(PageImageVersion,old).is_current
    assert call(a,pa,pg).status_code>=400 and len(queued[1])==1


def test_half_written_reservation_blocks_all_visitors_and_restart(app,runtime,queued,monkeypatch):
    p,a,b,pa,pb,pg,old,_=runtime
    original=budget._save
    def fail(path,value):
        if path.name=='call-budget.json' and value.get('runtime_calls'):raise OSError('synthetic write failure')
        return original(path,value)
    monkeypatch.setattr(budget,'_save',fail)
    r=call(a,pa,pg);assert r.status_code==202;queued[3]()
    assert not queued[1] and budget.execution_reason('image')=='previous_failure'
    monkeypatch.setattr(budget,'_save',original)
    with pytest.raises(RuntimeError,match='全局登记'):budget.initialize_runtime(app)


def test_changed_terms_and_deleted_ledger_do_not_regrant(app,runtime,monkeypatch):
    p,*_=runtime
    p['authorization']['credits']=17;monkeypatch.setenv('PC_RUN_POLICY',json.dumps(p))
    with pytest.raises(RuntimeError,match='条款'):budget.initialize_runtime(app)
    (budget._root()/'call-budget.json').unlink() # isolated ledger only
    with pytest.raises(RuntimeError,match='账本缺失'):budget.initialize_runtime(app)


def test_unknown_occupancy_and_inconsistent_totals_fail_closed(app,runtime,queued):
    p,a,b,pa,pb,pg,old,_=runtime
    state=budget.budget_state();state['external_unknown']=2
    budget._save(budget._root()/'call-budget.json',state)
    assert budget.execution_reason('text')=='authorization_required'
    r=call(a,pa,pg);assert r.status_code==202;queued[3]()
    budget.initialize_runtime(app)
    state=budget.budget_state();assert state['external_unknown']==2
    assert budget.execution_reason('image')=='budget_exhausted'
    state['image']-=1;budget._save(budget._root()/'call-budget.json',state)
    assert budget.execution_reason('image')=='authorization_required'
    with pytest.raises(RuntimeError,match='累计账目'):budget.initialize_runtime(app)


def test_crash_recovery_never_requeues_or_refunds(app,runtime):
    import time
    from models import db,Task,VisitorRequest
    p,a,b,pa,pb,pg,old,_=runtime
    with app.app_context(),va.scope(owner(app,pa)):
        task=Task(project_id=pa,task_type='GENERATE_PAGE_IMAGE',status='PROCESSING')
        db.session.add(task);db.session.commit();tid=task.id
        request=VisitorRequest(visitor_id=owner(app,pa),key='interrupted-no-task',fingerprint='unchanged',created=time.time())
        db.session.add(request);db.session.commit()
    before=budget.budget_state();recover_interrupted(app,budget._root().parent)
    with app.app_context():
        assert db.session.get(Task,tid).status=='INTERRUPTED'
        request=db.session.get(VisitorRequest,(owner(app,pa),'interrupted-no-task'))
        assert request.state=='interrupted' and request.fingerprint=='unchanged'
    after=budget.budget_state();assert after['paused']
    assert all(after[k]==before[k] for k in ('text','image','reserved_credits','batches'))


def test_config_defaults_and_local_server_resource_boundaries(tmp_path):
    path=tmp_path/'runtime.json'
    path.write_text('{}');cfg=load(path,tmp_path)
    assert cfg['mode']=='preview' and cfg['authorization'] is None and not cfg['visitor']['credits']
    for change in ({'mode':'live'},{'resources':{**DEFAULT['resources'],'start_memory_gib':1}},
                   {'resources':{**DEFAULT['resources'],'rss_mib':4096}},
                   {'visitor':{**DEFAULT['visitor'],'credits':-1}}, {'http_threads':1}):
        path.write_text(json.dumps(change))
        with pytest.raises(ValueError):load(path,tmp_path)
    path.write_text(json.dumps({'profile':'server','resources':{**DEFAULT['resources'],'start_memory_gib':2,'stop_memory_gib':1}}))
    assert load(path,tmp_path)['mode']=='preview'
    p=policy();p['authorization']['expires_at']=(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()
    path.write_text(json.dumps({'mode':'live',**p}))
    with pytest.raises(ValueError,match='过期'):load(path,tmp_path)
    assert load(path,tmp_path,enforce_authorization=False)['mode']=='live' # read-only check/stop, never serve


def test_data_lock_and_private_backup_restore(tmp_path):
    import sqlite3
    root=tmp_path/'state';(root/'data').mkdir(parents=True)
    with sqlite3.connect(root/'data/database.db') as con:
        con.executescript("create table tasks(id text,status text);insert into tasks values ('old','COMPLETED');")
    (root/'run').mkdir();(root/'run/call-budget.json').write_text('{"image": 7, "paused": true}')
    (root/'run/visitor-signing.key').write_text('synthetic-signing-key')
    (root/'uploads').mkdir();(root/'uploads/synthetic.txt').write_text('offline-only')
    with DataLock(root/'run'):
        with pytest.raises(RuntimeError):
            with DataLock(root/'run'):pass
        with pytest.raises(RuntimeError):backup(root,tmp_path/'blocked-backup')
    count=backup(root,tmp_path/'backup');assert count==4
    assert restore(tmp_path/'backup',tmp_path/'restored')==count
    assert (tmp_path/'restored/run/call-budget.json').read_bytes()==(root/'run/call-budget.json').read_bytes()
    with pytest.raises(ValueError):restore(tmp_path/'backup',root)


@pytest.mark.parametrize('name',['.env.copilot','.copilot/run/call-budget.json','backend/uploads/private.png',
    'frontend/node_modules/a.js','backend/instance/database.db','logs/server.log','frontend/dist/index.html','../secret.py'])
def test_release_rejects_runtime_paths(name):assert not source_allowed(name)


def test_proxy_host_and_https_boundary():
    from flask import Flask,request
    proxyapp=Flask('boundary-test')
    proxyapp.get('/')(lambda:{'secure':request.is_secure,'remote':request.remote_addr})
    boundaries(proxyapp,{**DEFAULT,'proxy':True,'public_origin':'https://slides.example.test'})
    c=proxyapp.test_client()
    assert c.get('/',base_url='https://wrong.example.test').status_code==400
    assert c.get('/',base_url='http://slides.example.test',headers={'X-Forwarded-Proto':'https'}).status_code==400
    assert c.get('/',base_url='https://slides.example.test').status_code==200

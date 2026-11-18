"""Real admin/prepare/send/save paths with isolated files and socket disabled."""
import copy
import json
import os
import pytest
from test_admin_console import app, admin, admin_client, login, live_draft, apply, save
from test_copilot_capabilities import isolated
from test_task_tracking import queued
from test_visitor_isolation import client, project, page, call
from services.ai_providers import copilot_limits as b
from services import manual_balance as mb


def daily_fixture(app,admin,queued):
    from services import image_retry as retry
    from models import db,Task
    manager=login(admin_client(app,admin));apply(manager,draft(18.71))
    visitor=client(app);pid=project(visitor);pg,_=page(app,visitor,pid)
    request=call(visitor,pid,pg);assert request.status_code==202;queued[3]()
    state=b.budget_state();cid=state['last_call_id'];tid=request.json['data']['task_id']
    permits={'old-consumed':{'id':'old-consumed','state':'consumed','used_call_id':cid,'used_task_id':tid},
             'old-ready':{'id':'old-ready','state':'ready','used_call_id':None,'used_task_id':None}}
    for p in permits.values():p['digest']=retry.checksum(p)
    state.update(paused=True,active_image_retry='old-consumed',image_retry_permits=permits)
    b._save(b._root()/'call-budget.json',state)
    manifest={'id':'offline-daily-001','reason':'Explicit offline daily-use transition',
              'balance_id':state['active_balance_checkpoint'],'retire_permit_ids':['old-ready']}
    return manager,visitor,pid,pg,tid,state,manifest


def test_daily_closeout_preserves_balance_and_blocks_old_tasks(app,admin,queued,monkeypatch):
    from services import image_retry as retry
    from services.budget_cycles import assert_task
    manager,visitor,pid,pg,tid,before,manifest=daily_fixture(app,admin,queued)
    updated,changed=retry.close_acceptance(before,manifest,[tid]);assert changed
    for key in ('reservation_calls','runtime_calls','balance_checkpoints','active_balance_checkpoint','reserved_credits','text','image'):
        assert updated[key]==before[key]
    assert updated['image_retry_permits']['old-consumed']==before['image_retry_permits']['old-consumed']
    assert updated['image_retry_permits']['old-ready']['state']=='revoked'
    assert retry.ready(updated) is None and updated['paused'] is False
    b._save(b._root()/'call-budget.json',updated)
    assert overview(manager)['available']==15.71
    with pytest.raises(RuntimeError,match='不允许重放'):assert_task(updated,tid)
    b.initialize_runtime(app)
    assert b.budget_state()==updated
    assert call(visitor,pid,pg).status_code==202;queued[3]()
    assert overview(manager)['available']==12.71
    assert manager.post('/api/admin/pause',json={'phrase':'暂停新生成'}).status_code==200
    assert call(visitor,pid,pg).status_code!=202
    after=b.budget_state();after['paused']=True
    repeat,changed=retry.close_acceptance(after,manifest,[tid]);assert not changed and repeat==after
    with pytest.raises(ValueError):retry.close_acceptance(before,{**manifest,'retire_permit_ids':[]},[tid])
    with pytest.raises(ValueError):retry.close_acceptance(before,{**manifest,'balance_id':'different'},[tid])


def test_daily_maintenance_backup_atomic_write_and_idempotence(app,admin,queued,tmp_path,monkeypatch):
    import sqlite3
    from pathlib import Path
    from image_retry_maintenance import run
    manager,visitor,pid,pg,tid,before,manifest=daily_fixture(app,admin,queued)
    root=tmp_path/'daily';(root/'data').mkdir(parents=True);(root/'run').mkdir()
    source=Path(app.config['SQLALCHEMY_DATABASE_URI'].removeprefix('sqlite:///'))
    with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True) as src,sqlite3.connect(root/'data/database.db') as dst:src.backup(dst)
    path=root/'run/call-budget.json';b._save(path,before)
    mf=root/'daily.json';mf.write_text(json.dumps(manifest),encoding='utf-8')
    preview=run(root,None,None,daily_file=mf)
    assert not preview['applied'] and preview['balance_credits']==18.71
    with pytest.raises(ValueError):run(root,None,None,'wrong',daily_file=mf)
    raw=path.read_bytes();original=b._save
    def fail(path_,value):
        if path_==path:raise OSError('simulated atomic write failure')
        return original(path_,value)
    with monkeypatch.context() as m:
        m.setattr(b,'_save',fail)
        with pytest.raises(OSError):run(root,None,None,preview['confirmation'],daily_file=mf)
    assert path.read_bytes()==raw
    # A retry reuses only the fully verified backup of the unchanged source.
    preview=run(root,None,None,daily_file=mf)
    done=run(root,None,None,preview['confirmation'],daily_file=mf)
    assert done['applied'] and Path(done['backup']).exists()
    after=path.read_bytes()
    repeated=run(root,None,None,preview['confirmation'],daily_file=mf)
    assert repeated['unchanged'] and repeated['applied'] and path.read_bytes()==after
    assert json.loads(after)['balance_checkpoints']==before['balance_checkpoints']


def test_managed_restart_pause_allows_explicit_balance_calibration(app,admin,queued):
    from managed_server import initialize_live_runtime
    manager=login(admin_client(app,admin));apply(manager,live_draft())
    state=b.budget_state();state['paused']=True
    b._save(b._root()/'call-budget.json',state)
    original=(b._root()/'call-budget.json').read_bytes()
    initialize_live_runtime(app)
    assert os.getenv('PC_RUNTIME_INCONSISTENT')!='1'
    assert (b._root()/'call-budget.json').read_bytes()==original
    assert b.execution_reason('image','template_generate') is not None
    # Real admin validation/apply, not a helper-only assertion; no provider send.
    apply(manager,draft(18.71,'balance-restart-001'))
    result=overview(manager)
    assert result['reported_balance']==18.71
    assert result['estimated_spent']==0 and result['available']==18.71
    assert b.budget_state()['paused'] is True
    assert not queued[1]


@pytest.mark.parametrize('damage',['partial_reservation','pending_cycle'])
def test_managed_restart_keeps_real_inconsistency_blocked(app,admin,queued,damage):
    from managed_server import initialize_live_runtime
    manager=login(admin_client(app,admin));apply(manager,live_draft())
    state=b.budget_state();state['paused']=True
    if damage=='partial_reservation':
        state['runtime_calls']['unmatched-call']={'role':'image','reserved':4}
    else:
        b._save(b._root()/'cycle-apply.pending.json',{'offline':True})
    b._save(b._root()/'call-budget.json',state)
    original=(b._root()/'call-budget.json').read_bytes()
    initialize_live_runtime(app)
    assert os.getenv('PC_RUNTIME_INCONSISTENT')=='1'
    assert (b._root()/'call-budget.json').read_bytes()==original
    save(manager,draft(18.71,'balance-blocked-001'))
    preview=manager.post('/api/admin/validate',json={}).json['data']
    assert not preview['can_apply']
    assert '配置或账目存在未完成写入，需本地核对。' in preview['blockers']
    assert not queued[1]


def draft(credits=9, identity='balance-offline-001'):
    d=live_draft()
    d['authorization'].update(text=20,image=20,balance={
        'id':identity,'credits':credits,'image_2k':3,'text_fallback':0.8})
    d['visitor'].update(text=20,image=20,credits=0,pages=5)
    return d


def overview(manager):
    result=manager.get('/api/admin/overview')
    assert result.status_code==200,result.json
    return result.json['data']['manual_balance']


def test_daily_validity_keeps_history_balance_and_guards(app,admin,queued,monkeypatch):
    from datetime import datetime, timezone
    from services import runtime_config as rc
    manager=login(admin_client(app,admin));fixed=draft(6);apply(manager,fixed)
    visitor=client(app);pid=project(visitor);pg,_=page(app,visitor,pid)
    assert call(visitor,pid,pg).status_code==202;queued[3]()
    before=copy.deepcopy(b.budget_state())
    daily=copy.deepcopy(fixed);daily['authorization'].update(validity='daily',expires_at=None)
    apply(manager,daily)
    for key in ('reservation_calls','runtime_calls','balance_checkpoints','active_balance_checkpoint','reserved_credits','text','image'):
        assert b.budget_state()[key]==before[key]
    assert overview(manager)['available']==3
    entry=b.budget_state()['runtime_authorizations'][fixed['authorization']['id']]
    assert entry['history'][-1]['policy']['authorization']==fixed['authorization']
    class Future(datetime):
        @classmethod
        def now(cls,tz=None):return datetime(2050,1,1,tzinfo=timezone.utc)
    monkeypatch.setattr(rc,'datetime',Future)
    # Expiry is tested through the actual overview and send guard, with no network.
    assert not rc.authorized_now(fixed['authorization'])
    assert manager.get('/api/admin/overview').json['data']['authorization_valid']
    b.initialize_runtime(app)
    assert manager.get('/api/admin/overview').json['data']['reasons']['image'] is None
    stranger=client(app)
    revision=visitor.get(f'/api/projects/{pid}/input-revision').json['data']['revision']
    denied=stranger.post(f'/api/projects/{pid}/pages/{pg}/generate/image',json={'force_regenerate':True},
        headers={'Idempotency-Key':'f'*32,'X-PC-Input-Version':revision})
    assert denied.status_code in (403,404)
    assert manager.post('/api/admin/pause',json={'phrase':'暂停新生成'}).status_code==200
    assert manager.get('/api/admin/overview').json['data']['reasons']['image']=='admin_paused'
    apply(manager,daily)
    assert overview(manager)['available']==3  # same config must not reset the checkpoint
    assert call(visitor,pid,pg).status_code==202;queued[3]()
    assert overview(manager)['available']==0
    assert call(visitor,pid,pg).status_code!=202
    assert len(queued[1])==2


def test_daily_validity_requires_explicit_mode_and_balance(app,admin,queued):
    from services import runtime_config as rc
    valid=draft()['authorization'];valid.update(validity='daily',expires_at=None)
    assert rc.authorized_now(rc.authorization(valid))
    invalid=[{**valid,'validity':'forever'},{**valid,'validity':'fixed'},
             {**valid,'balance':None},{**valid,'expires_at':'2030-01-01T00:00:00Z'},
             {**valid,'not_before':'2030-01-01T00:00:00'}]
    for value in invalid:
        with pytest.raises(ValueError):rc.authorization(value)
    future={**valid,'not_before':'2099-01-01T00:00:00Z'}
    assert not rc.authorized_now(future)
    manager=login(admin_client(app,admin));d=draft();d['authorization']=future;save(manager,d)
    assert not manager.post('/api/admin/validate',json={}).json['data']['can_apply']
    assert not queued[1]


def test_daily_validity_still_enforces_call_count(app,admin,queued):
    d=draft(18.71);d['authorization'].update(validity='daily',expires_at=None,image=1)
    manager=login(admin_client(app,admin));apply(manager,d)
    visitor=client(app);pid=project(visitor);pg,_=page(app,visitor,pid)
    assert call(visitor,pid,pg).status_code==202;queued[3]()
    assert overview(manager)['available']==15.71
    assert call(visitor,pid,pg).status_code!=202
    assert len(queued[1])==1


def test_success_failure_budget_and_replay_through_real_routes(app,admin,queued):
    manager=login(admin_client(app,admin));apply(manager,draft(6))
    visitor=client(app);pid=project(visitor);pg,_=page(app,visitor,pid)
    queued[0].image_provider.fail=True
    first=call(visitor,pid,pg,key='11111111111111111111111111111111');assert first.status_code==202,first.json
    queued[3]()
    assert overview(manager)['estimated_spent']==0
    assert overview(manager)['available']==6
    assert overview(manager)['failed_calls']==1
    # No automatic retry; the failed idempotency key cannot send another call.
    call(visitor,pid,pg,key='11111111111111111111111111111111');assert len(queued[1])==1 and not queued[2]
    queued[0].image_provider.fail=False
    second=call(visitor,pid,pg,key='22222222222222222222222222222222');assert second.status_code==202,second.json
    queued[3]()
    assert overview(manager)['estimated_spent']==3
    assert overview(manager)['success_counts']=={'image':1,'text':0}
    assert overview(manager)['available']==3
    third=call(visitor,pid,pg,key='33333333333333333333333333333333');assert third.status_code==202,third.json
    queued[3]()
    assert overview(manager)['available']==0
    rejected=call(visitor,pid,pg,key='44444444444444444444444444444444');assert rejected.status_code!=202
    assert len(queued[1])==3 and not queued[2]
    state=b.budget_state()
    assert state['image']==3 and state['reserved_credits']==12
    assert len(state['reservation_calls'])==3  # historical failures were not deleted


def test_checkpoint_excludes_history_and_restart_does_not_reset(app,admin,queued):
    manager=login(admin_client(app,admin));apply(manager,draft(9))
    visitor=client(app);pid=project(visitor);pg,_=page(app,visitor,pid)
    assert call(visitor,pid,pg).status_code==202;queued[3]()
    original=copy.deepcopy(b.budget_state()['reservation_calls'])
    assert overview(manager)['estimated_spent']==3
    new=draft(21.71,'balance-offline-002');apply(manager,new)
    assert overview(manager)['estimated_spent']==0 and overview(manager)['available']==21.71
    assert b.budget_state()['reservation_calls']==original
    assert call(visitor,pid,pg).status_code==202;queued[3]()
    before=b.budget_state();b.initialize_runtime(app,approve_change=False)
    assert b.budget_state()==before
    assert overview(manager)['estimated_spent']==3 and overview(manager)['available']==18.71
    same=copy.deepcopy(before);mb.initialize(same,new['authorization']['balance']);assert same==before
    with pytest.raises(ValueError):mb.initialize(same,draft(22,'balance-offline-002')['authorization']['balance'])
    with pytest.raises(ValueError):mb.initialize(same,draft()['authorization']['balance'])


def test_pending_multi_image_estimate_and_finite_permit_still_protected(app,admin,queued):
    from models import db,TaskTrace
    manager=login(admin_client(app,admin));apply(manager,draft(9))
    visitor=client(app);pid=project(visitor);pg,_=page(app,visitor,pid)
    request=call(visitor,pid,pg);assert request.status_code==202;queued[3]()
    task=request.json['data']['task_id']
    with app.app_context():
        trace=db.session.get(TaskTrace,task);rows=json.loads(trace.calls)
        rows[0]['returned_image_count']=2;trace.calls=json.dumps(rows);db.session.commit()
    assert overview(manager)['estimated_spent']==6
    with app.app_context():
        trace=db.session.get(TaskTrace,task);rows=json.loads(trace.calls)
        rows[0]['status']='sent';trace.calls=json.dumps(rows);db.session.commit()
    assert overview(manager)['estimated_spent']==0
    assert overview(manager)['in_flight_reserved']==3
    assert overview(manager)['available']==6
    state=b.budget_state();state.update(paused=True,pause_reason='provider_error',active_image_retry='finite-old-permit')
    assert mb.previous_failure_blocks(state) is True


def test_apply_failure_restores_checkpoint_and_original_ledger(app,admin,monkeypatch):
    manager=login(admin_client(app,admin));apply(manager,draft())
    before=(b._root()/'call-budget.json').read_bytes()
    save(manager,draft(20,'balance-offline-rollback'))
    preview=manager.post('/api/admin/validate',json={}).json['data'];assert preview['can_apply']
    original=admin.store.audit
    def fail(c,action,fields):
        if action=='apply':raise RuntimeError('synthetic audit failure')
        return original(c,action,fields)
    monkeypatch.setattr(admin.store,'audit',fail)
    response=manager.post('/api/admin/apply',json={'confirmation':preview['confirmation'],'phrase':'确认'})
    assert response.status_code!=200
    assert (b._root()/'call-budget.json').read_bytes()==before
    assert overview(manager)['reported_balance']==9


def test_manual_balance_keeps_apply_and_send_budget_guards(app,admin,queued):
    manager=login(admin_client(app,admin))
    small=draft(2);small['authorization']['credits']=0
    save(manager,small)
    check=manager.post('/api/admin/validate',json={})
    assert check.status_code==200 and not check.json['data']['can_apply']
    assert not (b._root()/'call-budget.json').exists() and not queued[1]
    sufficient=draft(3);sufficient['authorization']['credits']=0
    apply(manager,sufficient)
    a=client(app);pid=project(a);pg,_=page(app,a,pid)
    other=client(app)
    assert other.get(f'/api/projects/{pid}/input-revision').status_code in (403,404)
    assert call(a,pid,pg).status_code==202;queued[3]()
    assert overview(manager)['available']==0
    assert call(a,pid,pg).status_code!=202 and len(queued[1])==1

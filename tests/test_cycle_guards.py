"""Cycle changes through actual HTTP admission, worker, model guard and admin."""
import copy
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_admin_console import app,admin,isolated,admin_client,login,live_draft,apply,save
from test_task_tracking import queued
from test_visitor_isolation import client,project,page,call,owner
from test_settlement_guards import setup,adjustment
from services import budget_cycles as cycles,visitor_access as va,generation_budget as gb
from services.ai_providers import copilot_limits as b


def transition():
    # Simulate the already tested offline transition while deliberately keeping
    # this isolated app live to exercise the mock-provider send boundary.
    state=b.budget_state()
    new,_=cycles.create(state,cycle_id='offline-http-cycle',credit_cap=8,
        source_sha256='a'*64,archive_sha256='b'*64,created_at='2026-10-23T00:00:00Z')
    b._save(b._root()/'call-budget.json',new)
    return new


def test_cycle_overview_visitor_send_trace_and_restart_keep_old_history(app,admin,queued):
    from models import db,TaskTrace,Task
    manager,visitor,pid,pg,tid=setup(app,admin,queued,visitor_credits=8)
    old=b.budget_state(); transition()
    with app.app_context(),va.scope(owner(app,pid)):
        old_trace=db.session.get(TaskTrace,tid).snapshot
        assert va.usage_summary(va.identity())=={'text':0,'image':0,'reserved':0}
        assert gb.calculate_affordable_pages(b.budget_state(),va.limits(),va.usage_summary(va.identity()),0,0)==2
    overview=manager.get('/api/admin/overview').json['data']
    assert overview['ledger']['image']==0 and overview['ledger']['reserved_credits']==0
    assert overview['lifetime_ledger']['image']==1 and overview['remaining_reservation']==8
    assert overview['quota_cycle']['id']=='offline-http-cycle'
    # Reusing an old HTTP idempotency key returns its original task, no replay.
    replay=call(visitor,pid,pg,key='first-synthetic-image')
    assert replay.json['data']['task_id']==tid and len(queued[1])==1 and not queued[2]
    snapshot=b.budget_state(); b.initialize_runtime(app); assert b.budget_state()==snapshot
    response=call(visitor,pid,pg);assert response.status_code==202,response.json
    queued[3]();new=b.budget_state();cid=new['last_call_id']
    assert cycles.counts(new)['image']==1 and new['image']==2
    assert new['reservation_calls'][cid]['quota_cycle_id']=='offline-http-cycle'
    with app.app_context(),va.scope(owner(app,pid)):
        trace=db.session.get(TaskTrace,response.json['data']['task_id'])
        assert db.session.get(Task,response.json['data']['task_id']).status=='COMPLETED'
        assert json.loads(trace.outputs)
        assert json.loads(trace.snapshot)['quota_cycle_id']=='offline-http-cycle'
        assert json.loads(trace.calls)[0]['quota_cycle_id']=='offline-http-cycle'
        assert db.session.get(TaskTrace,tid).snapshot==old_trace
        assert va.usage_summary(va.identity())['reserved']==4
    assert old['reserved_credits']==4 and new['reserved_credits']==8
    terminal=[json.loads(line) for line in (b._root()/'calls.jsonl').read_text().splitlines()]
    assert terminal[-1]['quota_cycle_id']=='offline-http-cycle'
    b.initialize_runtime(app);assert cycles.counts(b.budget_state())['image']==1


def test_old_prepared_task_rejected_before_mock_provider(app,admin,queued):
    manager=login(admin_client(app,admin));apply(manager)
    visitor=client(app);pid=project(visitor);pg,_=page(app,visitor,pid)
    response=call(visitor,pid,pg);assert response.status_code==202
    transition();queued[3]()
    assert not queued[1] and cycles.counts(b.budget_state())['image']==0


def test_cycle_actual_over_reservation_remains_occupied_and_global_guard_blocks(app,admin,queued):
    manager,visitor,pid,pg,_=setup(app,admin,queued,visitor_credits=8)
    transition()
    response=call(visitor,pid,pg);assert response.status_code==202;queued[3]()
    adjustment(6)
    overview=manager.get('/api/admin/overview').json['data']
    assert overview['settlement']['settled_actual']==6 and overview['remaining_reservation']==2
    assert b.execution_reason('image')=='budget_exhausted'
    with app.app_context(),va.scope(owner(app,pid)):
        assert va.usage_summary(va.identity())['reserved']==6
    count=len(queued[1]);response=call(visitor,pid,pg)
    if response.status_code==202:queued[3]()
    assert len(queued[1])==count


def test_admin_reapply_cannot_raise_frozen_cycle_budget_or_clear_counter(app,admin,queued):
    manager,visitor,pid,pg,_=setup(app,admin,queued,visitor_credits=8)
    transition();before=b.budget_state()
    draft=live_draft();draft['authorization'].update(credits=97.2,id='different-authorization')
    save(manager,draft)
    preview=manager.post('/api/admin/validate',json={}).json['data']
    assert not preview['can_apply'] and any('周期预算' in p for p in preview['blockers'])
    assert b.budget_state()==before


def test_concurrent_mock_workers_cannot_exceed_cycle_cap(app,admin,queued):
    manager=login(admin_client(app,admin));draft=live_draft();draft['authorization'].update(credits=8,image=10)
    apply(manager,draft)
    # No old call is necessary; both independent users can enqueue before send.
    new=transition();new['quota_cycle']['credit_limit']=4
    new['quota_cycle']['digest']=cycles.checksum({k:v for k,v in new['quota_cycle'].items() if k!='digest'})
    b._save(b._root()/'call-budget.json',new)
    for _ in range(2):
        visitor=client(app);pid=project(visitor);pg,_=page(app,visitor,pid)
        response=call(visitor,pid,pg);assert response.status_code==202,response.json
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _:queued[3](),range(2)))
    assert len(queued[1])==1 and cycles.counts(b.budget_state())['image']==1


def test_preview_and_pending_transition_cannot_send(app,admin,queued,monkeypatch):
    _,visitor,pid,pg,_=setup(app,admin,queued,visitor_credits=8);transition()
    monkeypatch.setenv('PC_MODE','preview')
    before=len(queued[1]);response=call(visitor,pid,pg)
    if response.status_code==202:queued[3]()
    assert len(queued[1])==before
    monkeypatch.setenv('PC_MODE','live')
    b._save(b._root()/'cycle-apply.pending.json',{'incomplete':True})
    assert b.execution_reason('image')=='previous_failure'
    with pytest.raises(RuntimeError,match='迁移'):b.initialize_runtime(app)


def test_new_synchronous_text_task_gets_server_cycle_and_cannot_use_legacy_batch(app,admin,queued,monkeypatch):
    from services import task_tracking as tracking
    from models import db,TaskTrace,Task
    manager=login(admin_client(app,admin));apply(manager)
    transition()
    visitor=client(app);pid=project(visitor)
    with app.test_request_context('/synthetic-text',method='POST'),va.scope(owner(app,pid)):
        with tracking.scope(None) as scope:
            scope['project_id']=pid;scope['operation']='SYNTHETIC_TEXT_ONLY'
            assert queued[0].text_provider.generate_text('offline synthetic text')=='离线描述'
            row=db.session.get(TaskTrace,scope['task_id'])
            assert json.loads(row.snapshot)['quota_cycle_id']=='offline-http-cycle'
            assert json.loads(row.calls)[0]['quota_cycle_id']=='offline-http-cycle'
        assert cycles.counts(b.budget_state())=={'text':1,'image':0}
    monkeypatch.delenv('PC_RUN_POLICY')
    monkeypatch.setenv('PC_BATCH_ID','pc0083-content-count-regression')
    assert b.budget_reason(b.budget_state(),'image')=='authorization_required'

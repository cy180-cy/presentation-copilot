"""Synthetic cycles/receipts only; no credentials, network or real ledger writes."""
import copy
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from services import budget_cycles as cycles, ledger_settlement as s
from services.ai_providers import copilot_limits as b
from services.admin_store import Store
from cycle_maintenance import run, database_digest


def old_state():
    return {'text':8,'image':22,'reserved_credits':94.4,'paused':True,'external_unknown':3,
        'reservation_calls':{'old-success':{'role':'image','reserved':4,'attempt':1},
                             'old-timeout':{'role':'image','reserved':4,'attempt':2}}}


def start(state=None, cap=27.71):
    return cycles.create(state or old_state(),cycle_id='offline-cycle-001',credit_cap=cap,
                         source_sha256='a'*64,archive_sha256='b'*64,created_at='2026-10-23T00:00:00Z')[0]


def test_cycle_preserves_all_lifetime_evidence_and_excludes_old_settled_unknown_and_timeouts():
    old=old_state()
    old,_=s.prepare(old,[{'target':'old-success','kind':'invoice','actual_credits':6,
        'invoice_id':'old-invoice','evidence_sha256':'c'*64}],s.catalog(old,[]))
    original=copy.deepcopy(old); new=start(old)
    assert old==original
    for k in ('text','image','reserved_credits','settlements','reservation_calls','external_unknown'):
        assert new[k]==old[k]
    assert cycles.counts(new)==dict(text=0,image=0)
    assert s.occupied(new)==0 and s.summary(new)['settled_actual']==0
    assert s.summary(new,lifetime=True)['settled_actual']==6
    assert cycles.credit_limit(new,{'credits':97.2})==27.71
    assert not cycles.owns_call(new,'old-timeout') and not cycles.owns_call(new,'unknown')


def add_call(state, key='new-image'):
    result=copy.deepcopy(state)
    result['image']+=1; result['reserved_credits']=round(result['reserved_credits']+4,6)
    result['reservation_calls'][key]={'role':'image','reserved':4,'attempt':result['image'],
                                    'quota_cycle_id':cycles.identity(state)}
    return result


@pytest.mark.parametrize('actual,pending,occupied',[(3,0,3),(6,0,6)])
def test_current_cycle_invoice_difference_and_idempotency(actual,pending,occupied):
    state=add_call(start())
    proposal=[{'target':'new-image','kind':'invoice','actual_credits':actual,
        'invoice_id':'new-invoice','evidence_sha256':'d'*64}]
    settled,_=s.prepare(state,proposal,s.catalog(state,[]))
    assert s.summary(settled)['pending_reserved']==pending and s.occupied(settled)==occupied
    replay,_=s.prepare(settled,proposal,s.catalog(settled,[])); assert replay==settled
    assert cycles.counts(settled)['image']==1
    assert settled['reserved_credits']==98.4


def test_unsent_release_and_unknown_failure_remain_distinct_in_current_cycle():
    state=add_call(add_call(start()),'new-unsent')
    proposal=[{'target':'new-unsent','kind':'confirmed_not_sent',
               'evidence_sha256':'d'*64}]
    settled,_=s.prepare(state,proposal,s.catalog(state,[]),not_sent={'new-unsent'})
    assert s.occupied(settled)==4  # sent/unknown new-image is still occupied
    assert s.summary(settled)['released_credits']==4


def test_confirmed_no_charge_release_is_owned_by_cycle_and_does_not_reset_counts():
    state=add_call(add_call(start()),'second-unknown')
    proposal=[{'target':'new-image','kind':'confirmed_no_charge','confirmation_source':'user',
               'evidence_sha256':'d'*64}]
    settled,_=s.prepare(state,proposal,s.catalog(state,[]),no_charge={'new-image'})
    assert s.summary(settled)['pending_reserved']==4
    assert s.summary(settled)['released_credits']==4 and s.summary(settled)['settled_actual']==0
    assert cycles.counts(settled)==dict(text=0,image=2)
    assert settled['quota_cycle']==state['quota_cycle']
    assert settled['reserved_credits']==state['reserved_credits']
    assert cycles.credit_limit(settled,{'credits':97.2})==27.71
    replay,result=s.prepare(settled,proposal,s.catalog(settled,[]),no_charge={'new-image'})
    assert replay==settled and result['changed']==[]


def test_repeat_cycle_or_changed_id_never_clears_new_usage():
    state=add_call(start())
    again,changed=cycles.create(state,cycle_id='offline-cycle-001',credit_cap=27.71,
                               source_sha256='',archive_sha256='',created_at='')
    assert not changed and again==state and s.occupied(again)==4
    for key,cap in [('different-cycle',27.71),('offline-cycle-001',50)]:
        with pytest.raises(ValueError):
            cycles.create(state,cycle_id=key,credit_cap=cap,source_sha256='',archive_sha256='',created_at='')


@pytest.fixture
def instance(tmp_path):
    root=tmp_path/'instance'; (root/'run').mkdir(parents=True); (root/'data').mkdir()
    store=Store(root/'data/database.db'); store.migrate()
    from test_admin_console import live_draft
    active=live_draft()
    active['authorization'].update(credits=97.2,id='old-authorization')
    active['visitor'].update(pages=10,default_auto_pages=5,credits=50)
    with store.transaction() as c:
        c.execute('CREATE TABLE tasks(id TEXT PRIMARY KEY,status TEXT)')
        c.execute('CREATE TABLE pc_visitor_requests(task_id TEXT,state TEXT)')
        c.execute('CREATE TABLE pc_visitor_calls(call_id TEXT,role TEXT,reserved REAL,created REAL)')
        c.execute('CREATE TABLE works(id TEXT,body TEXT)')
        c.execute("INSERT INTO works VALUES('preserved-work','original version')")
        c.execute("UPDATE pc_admin_control SET active=?,draft=?,revision=6,draft_revision=7",
                  (json.dumps(active),json.dumps({'original_unsaved_form':'keep'})))
    b._save(root/'run/call-budget.json',old_state())
    (root/'run/calls.jsonl').write_text('original call evidence\n')
    (root/'run/server.json').write_text(json.dumps({'stopped':True}))
    return root


def apply_cycle(root):
    preview=run(root,'offline-cycle-001',27.71)
    return run(root,'offline-cycle-001',27.71,preview['confirmation'])


def test_real_maintenance_transaction_backup_restore_readback_and_repeat(instance,tmp_path):
    from runtime_maintenance import restore
    root=instance; ledger=(root/'run/call-budget.json').read_bytes()
    preview=run(root,'offline-cycle-001',27.71)
    assert (root/'run/call-budget.json').read_bytes()==ledger
    assert not (root/'backups').exists()
    done=run(root,'offline-cycle-001',27.71,preview['confirmation'])
    assert done['applied'] and done['settlement']['total_occupied']==0
    saved=json.loads((root/'run/call-budget.json').read_bytes())
    assert saved['text']==8 and saved['image']==22 and saved['reserved_credits']==94.4
    with sqlite3.connect(root/'data/database.db') as c:
        row=c.execute('SELECT active,draft,draft_revision,revision,paused FROM pc_admin_control').fetchone()
        assert json.loads(row[0])['mode']=='live' and json.loads(row[0])['authorization']['credits']==27.71
        assert json.loads(row[1])=={'original_unsaved_form':'keep'} and row[2:]==(7,7,1)
        assert c.execute('SELECT * FROM works').fetchall()==[('preserved-work','original version')]
        assert c.execute("SELECT count(*) FROM pc_admin_audit WHERE action='quota_cycle_started'").fetchone()[0]==1
    assert run(root,'offline-cycle-001',27.71)['unchanged']
    assert json.loads((root/'run/call-budget.json').read_bytes())==saved
    recovered=tmp_path/'recovered'
    assert restore(Path(done['backup']),recovered)>0
    assert (recovered/'run/call-budget.json').read_bytes()==ledger
    assert (root/'run/calls.jsonl').read_text()=='original call evidence\n'


def test_concurrent_cycle_apply_occurs_once(instance):
    from managed_server import DataLock
    preview=run(instance,'offline-cycle-001',27.71)
    with DataLock(instance/'run'),pytest.raises(Exception):
        run(instance,'offline-cycle-001',27.71,preview['confirmation'])
    assert not (instance/'backups').exists()
    apply_cycle(instance)
    with pytest.raises(ValueError):run(instance,'other-cycle-002',27.71)


@pytest.mark.parametrize('gate',['active','pending','running','changed','hold','pending_write'])
def test_migration_refuses_conflicting_or_changed_state(instance,gate):
    preview=run(instance,'offline-cycle-001',27.71)
    if gate in ('active','pending','changed'):
        with sqlite3.connect(instance/'data/database.db') as c:
            if gate=='active':c.execute("INSERT INTO tasks VALUES('t','PROCESSING')")
            elif gate=='pending':c.execute("INSERT INTO pc_visitor_requests VALUES(NULL,'unknown')")
            else:c.execute("UPDATE works SET body='user changed'")
    elif gate=='running':(instance/'run/server.json').write_text(json.dumps({'stopped':False}))
    elif gate=='pending_write':b._save(instance/'run/cycle-apply.pending.json',{'incomplete':True})
    else:
        state=old_state();state['generation_plans']={'p':{'state':'held','remaining':{'image':1}}}
        b._save(instance/'run/call-budget.json',state)
    before=(instance/'run/call-budget.json').read_bytes()
    with pytest.raises((ValueError,RuntimeError)):run(instance,'offline-cycle-001',27.71,preview['confirmation'])
    assert (instance/'run/call-budget.json').read_bytes()==before
    assert not (instance/'backups').exists()


def test_transaction_failure_restores_original_ledger_and_admin_state(instance,monkeypatch):
    preview=run(instance,'offline-cycle-001',27.71)
    original=(instance/'run/call-budget.json').read_bytes()
    def fail(*args,**kw):raise RuntimeError('synthetic audit disk failure')
    monkeypatch.setattr(Store,'audit',fail)
    with pytest.raises(RuntimeError,match='audit disk'):run(instance,'offline-cycle-001',27.71,preview['confirmation'])
    assert (instance/'run/call-budget.json').read_bytes()==original
    assert not (instance/'run/cycle-apply.pending.json').exists()
    with sqlite3.connect(instance/'data/database.db') as c:
        assert c.execute('SELECT revision,paused FROM pc_admin_control').fetchone()==(6,0)


def test_unverified_backup_blocks_any_real_write(instance,monkeypatch):
    import cycle_maintenance as maintenance
    preview=run(instance,'offline-cycle-001',27.71)
    original=(instance/'run/call-budget.json').read_bytes()
    def fail(*args,**kw):raise ValueError('synthetic corrupt backup')
    monkeypatch.setattr(maintenance,'verify_backup',fail)
    with pytest.raises(ValueError,match='backup'):run(instance,'offline-cycle-001',27.71,preview['confirmation'])
    assert (instance/'run/call-budget.json').read_bytes()==original
    assert not (instance/'run/cycle-apply.pending.json').exists()


def test_lost_commit_ack_retains_cycle_and_blocks_unreviewed_retry(instance,monkeypatch):
    import cycle_maintenance as maintenance
    original_connect=sqlite3.connect
    class LostAck(sqlite3.Connection):
        def commit(self):
            super().commit()
            raise RuntimeError('synthetic lost commit acknowledgement')
    def connect(*args,**kw):
        if Path(str(args[0]))==instance/'data/database.db' and not kw.get('uri'):
            kw['factory']=LostAck
        return original_connect(*args,**kw)
    monkeypatch.setattr(maintenance.sqlite3,'connect',connect)
    preview=run(instance,'offline-cycle-001',27.71)
    with pytest.raises(RuntimeError,match='acknowledgement'):run(instance,'offline-cycle-001',27.71,preview['confirmation'])
    assert cycles.identity(json.loads((instance/'run/call-budget.json').read_bytes()))=='offline-cycle-001'
    assert (instance/'run/cycle-apply.pending.json').exists()
    with original_connect(instance/'data/database.db') as c:
        assert c.execute('SELECT revision,paused FROM pc_admin_control').fetchone()==(7,1)
    with pytest.raises(RuntimeError,match='未完成'):run(instance,'offline-cycle-001',27.71)


def test_cli_migration_never_constructs_app_or_providers(instance,monkeypatch,capsys):
    import presentation_copilot as runner
    config=instance/'runtime.json'
    config.write_text(json.dumps({'data_dir':str(instance),'secret_file':str(instance/'.env.copilot')}))
    for name in ('RUNTIME','CONFIG','PID_FILE'):monkeypatch.setattr(runner,name,getattr(runner,name))
    def forbidden(*args,**kw):raise AssertionError('no credentials/providers')
    monkeypatch.setattr(runner,'config_values',forbidden);monkeypatch.setattr(runner,'create_local_app',forbidden)
    args=['presentation_copilot.py','--runtime-config',str(config),'ledger-cycle','--cycle-id','offline-cycle-001','--cycle-credits','27.71']
    monkeypatch.setattr('sys.argv',args);runner.main();preview=json.loads(capsys.readouterr().out)
    monkeypatch.setattr('sys.argv',args+['--apply-cycle',preview['confirmation']]);runner.main()
    assert json.loads(capsys.readouterr().out)['applied']
    # The persisted active configuration must still load on the actual CLI,
    # rather than only being internally consistent as an isolated JSON record.
    monkeypatch.setattr('sys.argv',args);runner.main()
    assert json.loads(capsys.readouterr().out)['unchanged']
    from services.admin_runtime import effective
    from services.runtime_config import load
    cfg=effective(load(config,instance,enforce_authorization=False),instance)
    assert cfg['_admin_paused'] and cfg['mode']=='live' and cfg['authorization']['credits']==27.71


def test_migration_registers_new_policy_and_keeps_original_policy_history(instance,monkeypatch):
    import time
    from services.admin_runtime import effective
    from services.runtime_config import load
    config=instance/'runtime.json'
    config.write_text(json.dumps({'data_dir':str(instance),'secret_file':str(instance/'.env.copilot')}))
    cfg=effective(load(config,instance,enforce_authorization=False),instance)
    policy={'authorization':cfg['authorization'],'visitor':cfg['visitor']}
    state=old_state();state.update(runtime_started_at=time.time(),runtime_calls={},
        runtime_start_totals={k:state[k] for k in ('text','image','reserved_credits')})
    b.register_runtime_policy(state,copy.deepcopy(policy))
    b._save(instance/'run/call-budget.json',state)
    apply_cycle(instance)
    new=json.loads((instance/'run/call-budget.json').read_bytes())
    cfg2=effective(load(config,instance,enforce_authorization=False),instance)
    policy2={'authorization':cfg2['authorization'],'visitor':cfg2['visitor']}
    assert b.same_runtime_policy(new['runtime_authorizations'][cfg['authorization']['id']]['policy'],policy2)
    history=new['runtime_authorizations'][cfg['authorization']['id']]['history']
    assert len(history)==1 and history[0]['policy']==policy
    same=copy.deepcopy(new);b.register_runtime_policy(same,policy2);assert same==new
    assert b.runtime_totals_match(new) and cycles.counts(new)=={'text':0,'image':0}

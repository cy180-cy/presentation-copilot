"""Same-cycle absolute cap amendment through locked backup/transaction entry."""
import json
import sqlite3
from pathlib import Path
import pytest
import sys
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/n) for n in ('scripts','backend')]
from test_budget_cycles import instance, apply_cycle
from cycle_maintenance import run, database_digest
from services import budget_cycles as cycles
from services.ai_providers import copilot_limits as b


def amend(root,confirmation=None,credits=38,reason='Explicit user cap amendment, preserve all usage'):
    return run(root,'offline-cycle-001',credits,confirmation,revision_id='offline-cap-revision',reason=reason)


def test_revision_preserves_baseline_history_and_replay(instance):
    root=instance;apply_cycle(root)
    path=root/'run/call-budget.json';before=json.loads(path.read_bytes())
    preview=amend(root);assert json.loads(path.read_bytes())==before
    done=amend(root,preview['confirmation']);assert done['applied']
    after=json.loads(path.read_bytes())
    for k,v in before.items():
        if k!='quota_cycle':assert after[k]==v
    assert cycles.current(after)['credit_limit']==38
    for k,v in before['quota_cycle'].items():
        if k not in ('credit_limit','digest'):assert after['quota_cycle'][k]==v
    archive=Path(done['backup']);assert json.loads((archive/'run/call-budget.json').read_bytes())==before
    with sqlite3.connect(root/'data/database.db') as c:
        active,revision,draft,draftrev=c.execute('select active,revision,draft,draft_revision from pc_admin_control').fetchone()
        assert json.loads(active)['authorization']['credits']==38
        assert json.loads(draft)=={'original_unsaved_form':'keep'} and draftrev==7
        assert c.execute("select count(*) from pc_admin_audit where action='quota_cycle_revised'").fetchone()[0]==1
    assert amend(root,preview['confirmation'])['unchanged']
    assert json.loads(path.read_bytes())==after
    with pytest.raises(ValueError):amend(root,credits=39)
    with pytest.raises(ValueError):amend(root,reason='different authorization')


def test_revision_failure_rolls_back_both_stores(instance,monkeypatch):
    from services.admin_store import Store
    root=instance;apply_cycle(root);path=root/'run/call-budget.json';before=path.read_bytes()
    with sqlite3.connect(root/'data/database.db') as c:dbhash=database_digest(c)
    preview=amend(root)
    def fail(*a,**kw):raise RuntimeError('offline injected audit failure')
    monkeypatch.setattr(Store,'audit',fail)
    with pytest.raises(RuntimeError):amend(root,preview['confirmation'])
    assert path.read_bytes()==before and not (root/'run/cycle-apply.pending.json').exists()
    with sqlite3.connect(root/'data/database.db') as c:assert database_digest(c)==dbhash


def test_revision_rejects_stale_preview_and_active_permit(instance):
    root=instance;apply_cycle(root);path=root/'run/call-budget.json'
    preview=amend(root);state=json.loads(path.read_bytes());state['paused']=True;b._save(path,state)
    with pytest.raises(ValueError):amend(root,preview['confirmation'])
    state['image_retry_permits']={'offline':{'state':'ready'}};b._save(path,state)
    with pytest.raises(ValueError):amend(root)


def test_revision_updates_effective_policy_without_restarting_counts(instance,monkeypatch,capsys):
    import time,copy
    import presentation_copilot as runner
    from services.admin_runtime import effective
    from services.runtime_config import load
    root=instance;config=root/'runtime.json'
    config.write_text(json.dumps({'data_dir':str(root),'secret_file':str(root/'.env.copilot')}))
    cfg=effective(load(config,root,enforce_authorization=False),root)
    path=root/'run/call-budget.json';state=json.loads(path.read_bytes())
    state.update(runtime_started_at=time.time(),runtime_calls={},runtime_start_totals={k:state[k] for k in ('text','image','reserved_credits')})
    b.register_runtime_policy(state,{'authorization':cfg['authorization'],'visitor':cfg['visitor']})
    b._save(path,state);apply_cycle(root)
    before=json.loads(path.read_bytes())
    for name in ('RUNTIME','CONFIG','PID_FILE'):monkeypatch.setattr(runner,name,getattr(runner,name))
    def forbidden(*a,**kw):raise AssertionError('no model or credential loading')
    monkeypatch.setattr(runner,'config_values',forbidden);monkeypatch.setattr(runner,'create_local_app',forbidden)
    args=['presentation_copilot.py','--runtime-config',str(config),'ledger-cycle','--cycle-id','offline-cycle-001',
        '--cycle-credits','38','--cycle-revision-id','offline-cli-amendment','--authorization-reason','Explicit offline user amendment']
    monkeypatch.setattr('sys.argv',args);runner.main();preview=json.loads(capsys.readouterr().out)
    monkeypatch.setattr('sys.argv',args+['--apply-cycle',preview['confirmation']]);runner.main()
    assert json.loads(capsys.readouterr().out)['applied']
    cfg=effective(load(config,root,enforce_authorization=False),root)
    new=json.loads(path.read_bytes());assert cfg['authorization']['credits']==cycles.current(new)['credit_limit']==38
    original=before['runtime_authorizations'][cfg['authorization']['id']]
    entry=new['runtime_authorizations'][cfg['authorization']['id']]
    assert entry['history'][:-1]==original['history'] and entry['history'][-1]['policy']==original['policy']
    same=copy.deepcopy(new);b.register_runtime_policy(same,{'authorization':cfg['authorization'],'visitor':cfg['visitor']})
    assert same==new and b.runtime_totals_match(new)
    monkeypatch.setattr('sys.argv',args);runner.main();assert json.loads(capsys.readouterr().out)['unchanged']

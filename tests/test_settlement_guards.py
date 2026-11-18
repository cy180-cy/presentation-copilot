"""Settlement through existing visitor routes, Provider guard, planning and admin.

All data/providers are synthetic. No running service or model connection.
"""
import copy
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from test_admin_console import app, admin, isolated, admin_client, login, live_draft, apply
from test_task_tracking import queued
from test_visitor_isolation import client, project, page, call, owner
from services import visitor_access as va, generation_budget as gb, ledger_settlement as s
from services.ai_providers import copilot_limits as budget


def setup(app, admin, queued, *, credits=8, visitor_credits=5, image_limit=3):
    manager = login(admin_client(app, admin))
    draft = live_draft()
    draft['authorization'].update(credits=credits, image=image_limit)
    draft['visitor'].update(credits=visitor_credits, image=3)
    apply(manager, draft)
    visitor = client(app); pid = project(visitor); pg, _ = page(app, visitor, pid)
    response = call(visitor, pid, pg, key='first-synthetic-image')
    assert response.status_code == 202, response.json
    queued[3]()
    assert len(queued[1]) == 1
    return manager, visitor, pid, pg, response.json['data']['task_id']


def adjustment(cost):
    state = budget.budget_state(); call_id = state['last_call_id']
    updated, report = s.prepare(state, [{'target': call_id, 'kind': 'invoice', 'actual_credits': cost,
        'invoice_id': 'offline-invoice', 'evidence_sha256': 'a'*64}], s.catalog(state, []))
    budget._save(budget._root()/'call-budget.json', updated)
    return call_id, report


def test_credit_release_consistent_in_visitor_global_admin_planning_and_restart(app, admin, queued):
    from models import db, TaskTrace, VisitorCall
    manager, visitor, pid, pg, tid = setup(app, admin, queued)
    before = budget.budget_state()
    assert call(visitor, pid, pg).status_code == 429
    call_id, _ = adjustment(1)
    with app.app_context(), va.scope(owner(app, pid)):
        assert va.usage_summary(va.identity()) == {'text': 0, 'image': 1, 'reserved': 1}
        assert gb.calculate_affordable_pages(budget.budget_state(), va.limits(), va.usage_summary(va.identity()), 0, 0) == 1
        trace = db.session.get(TaskTrace, tid)
        assert json.loads(trace.calls)[0]['actual_credits'] is None
        record = db.session.get(VisitorCall, call_id)
        assert record.reserved == 4 and record.state == 'reserved_unknown'
    overview = manager.get('/api/admin/overview').json['data']
    assert overview['remaining_reservation'] == 7 and overview['settlement']['settled_actual'] == 1
    assert overview['ledger']['reserved_credits'] == 4
    detail = manager.get('/api/admin/tasks/'+tid).json['data']
    assert detail['calls'][0]['actual_credits'] == 1 and detail['calls'][0]['cost_status'] == 'settled'
    assert '人工核查' in detail['quality_note']
    # Restart validation keeps original counters plus the adjustment; no regrant.
    snapshot = budget.budget_state(); budget.initialize_runtime(app)
    assert budget.budget_state() == snapshot and budget.runtime_totals_match(snapshot)
    assert call(visitor, pid, pg, key='first-synthetic-image').status_code == 202
    assert not queued[2] and len(queued[1]) == 1
    response = call(visitor, pid, pg); assert response.status_code == 202, response.json
    queued[3](); after = budget.budget_state()
    assert after['image'] == 2 and after['reserved_credits'] == 8 and s.occupied(after) == 5
    assert after['runtime_start_totals'] == before['runtime_start_totals']
    stranger = client(app)
    assert stranger.get(f'/api/projects/{pid}/task-details?task_id={tid}').status_code == 404
    assert stranger.get('/api/admin/tasks/'+tid).status_code == 401


def test_invoice_overrun_blocks_next_global_and_visitor_request(app, admin, queued):
    manager, visitor, pid, pg, _ = setup(app, admin, queued, visitor_credits=8)
    adjustment(6)
    assert budget.execution_reason('image') == 'budget_exhausted'
    assert manager.get('/api/admin/overview').json['data']['remaining_reservation'] == 2
    with app.app_context(), va.scope(owner(app, pid)):
        assert va.usage_summary(va.identity())['reserved'] == 6
        assert gb.calculate_affordable_pages(budget.budget_state(), va.limits(), va.usage_summary(va.identity()), 0, 0) == 0
    response = call(visitor, pid, pg)
    if response.status_code == 202: queued[3]()
    assert len(queued[1]) == 1 and budget.budget_state()['image'] == 1


@pytest.mark.parametrize('cost,can_send_next', [(3, True), (6, False)])
def test_real_cli_settlement_flows_into_http_guards_and_admin(app, admin, queued, monkeypatch, capsys, cost, can_send_next):
    import presentation_copilot as runner
    from models import db, TaskTrace
    # Reuse the normal app/worker. Only its synthetic SQLite snapshot is copied
    # for the maintenance command, which never writes database rows.
    root = budget._root().parent
    monkeypatch.setattr(budget, '_root', lambda: root/'run')
    manager, visitor, pid, pg, tid = setup(app, admin, queued, visitor_credits=8)
    call_id = budget.budget_state()['last_call_id']
    (root/'data').mkdir()
    with app.app_context():
        source = Path(db.engine.url.database)
        assert source.is_relative_to(Path(__file__).resolve().parents[1]/'.copilot/test-data')
        with sqlite3.connect(source.resolve().as_uri()+'?mode=ro', uri=True) as src, sqlite3.connect(root/'data/database.db') as dst:
            src.backup(dst)
        original_calls = db.session.get(TaskTrace, tid).calls
    proof = json.dumps({'kind': 'invoice', 'target': call_id, 'invoice_id': 'offline-cli-receipt',
                        'actual_credits': cost, 'billed_images': 2 if cost == 6 else 1,
                        'reference': 'synthetic verified invoice fixture only'}).encode()
    (root/'receipt.json').write_bytes(proof)
    manifest = root/'settle.json'
    manifest.write_text(json.dumps({'entries': [{'kind': 'invoice', 'target': call_id, 'actual_credits': cost,
        'invoice_id': 'offline-cli-receipt', 'evidence': {'path': 'receipt.json', 'sha256': hashlib.sha256(proof).hexdigest()}}]}))
    config = root/'runtime.json'
    config.write_text(json.dumps({'data_dir': str(root), 'secret_file': str(root/'.env.copilot')}))
    for name in ('RUNTIME', 'CONFIG', 'PID_FILE'):
        monkeypatch.setattr(runner, name, getattr(runner, name))
    def forbidden(*args, **kwargs): raise AssertionError('maintenance must not initialize providers or read credentials')
    monkeypatch.setattr(runner, 'config_values', forbidden)
    monkeypatch.setattr(runner, 'create_local_app', forbidden)
    args = ['presentation_copilot.py', '--runtime-config', str(config), 'ledger-settle', '--settlement-file', str(manifest)]
    monkeypatch.setattr('sys.argv', args); runner.main()
    preview = json.loads(capsys.readouterr().out); assert not preview['applied']
    database = (root/'data/database.db').read_bytes()
    monkeypatch.setattr('sys.argv', args+['--apply-settlement', preview['confirmation']]); runner.main()
    applied = json.loads(capsys.readouterr().out); assert applied['applied']
    assert (root/'data/database.db').read_bytes() == database
    with app.app_context(): assert db.session.get(TaskTrace, tid).calls == original_calls
    row = budget.budget_state()['settlements'][call_id]
    assert row['invoice_id'] == 'offline-cli-receipt' and row['actual_credits'] == cost
    assert budget.budget_state()['image'] == 1  # billed images are not request attempts
    archived = root/'run/settlement-evidence'/(hashlib.sha256(proof).hexdigest()+'.json')
    assert archived.read_bytes() == proof
    overview = manager.get('/api/admin/overview').json['data']
    assert overview['settlement']['total_occupied'] == cost and overview['remaining_reservation'] == 8-cost
    assert manager.get('/api/admin/tasks/'+tid).json['data']['calls'][0]['actual_credits'] == cost
    with app.app_context(), va.scope(owner(app, pid)):
        assert va.usage_summary(va.identity())['reserved'] == cost
    response = call(visitor, pid, pg)
    if response.status_code == 202: queued[3]()
    assert len(queued[1]) == (2 if can_send_next else 1)


@pytest.mark.parametrize('gate', ['count', 'preview', 'paused', 'unknown', 'hold'])
def test_release_cannot_bypass_other_guards(app, admin, queued, monkeypatch, gate):
    _, visitor, pid, pg, _ = setup(app, admin, queued, image_limit=1 if gate == 'count' else 3)
    adjustment(0)
    if gate == 'preview': monkeypatch.setenv('PC_MODE', 'preview')
    if gate in ('paused', 'unknown', 'hold'):
        state = budget.budget_state()
        if gate == 'paused': state['paused'] = True
        elif gate == 'unknown': state['external_unknown'] = 8
        else:
            state['generation_plans'] = {'offline-hold': {'state': 'held', 'owner': owner(app, pid),
                'project_id': 'other-project', 'remaining': {'text': 0, 'image': 1}, 'costs': budget.RESERVATIONS}}
        budget._save(budget._root()/'call-budget.json', state)
    response = call(visitor, pid, pg)
    if response.status_code == 202: queued[3]()
    assert len(queued[1]) == 1 and budget.budget_state()['image'] == 1


def test_visitor_windows_use_original_time_and_original_owner(app, admin, queued, monkeypatch):
    from models import db, VisitorCall
    _, visitor, pid, _, _ = setup(app, admin, queued)
    call_id, _ = adjustment(6)
    original_time = 1000
    with app.app_context():
        row = db.session.get(VisitorCall, call_id); row.created = original_time; db.session.commit()
    monkeypatch.setattr(va.time, 'time', lambda: original_time + va.limits()['window_seconds'] + 1)
    with app.app_context(), va.scope(owner(app, pid)):
        assert va.usage_summary(va.identity()) == {'text': 0, 'image': 0, 'reserved': 0}
    assert s.occupied(budget.budget_state()) == 6  # global lifetime occupancy never expires


def test_fixed_batch_cannot_be_refreshed_by_settling_prebatch_requests(monkeypatch):
    monkeypatch.delenv('PC_RUN_POLICY', raising=False)
    monkeypatch.setenv('PC_BATCH_ID', budget.COPY_BATCH_ID)
    key = 'a'*32
    state = {'text': 0, 'image': 2, 'reserved_credits': 8, 'paused': False,
        'reservation_calls': {key: {'role': 'image', 'attempt': 1, 'reserved': 4}},
        'batches': {budget.COPY_BATCH_ID: {'start': {'text': 0, 'image': 1, 'reserved_credits': 4},
            'limits': budget.COPY_BATCH_LIMITS, 'operations': {'template_generate': 1}}}}
    state, _ = s.prepare(state, [{'target': key, 'kind': 'invoice', 'actual_credits': 0,
        'invoice_id': 'offline-old-invoice', 'evidence_sha256': 'a'*64}], s.catalog(state, []))
    assert budget.budget_reason(state, 'image', 'template_generate') == 'budget_exhausted'
    assert state['image'] == 2 and state['batches'][budget.COPY_BATCH_ID]['operations']['template_generate'] == 1

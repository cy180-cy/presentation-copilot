"""Synthetic invoices and isolated files/SQLite only; no provider or sockets."""
import copy
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from services import ledger_settlement as s
from services.ai_providers import copilot_limits as budget
from ledger_maintenance import run
from managed_server import DataLock

CALL = 'a' * 32
SECOND = 'b' * 32
SHA = '1' * 64


def original(total=8):
    return {'text': 0, 'image': 2, 'reserved_credits': total, 'paused': False,
            'runtime_calls': {CALL: {'role': 'image', 'reserved': 4}, SECOND: {'role': 'image', 'reserved': 4}}}


def invoice(target=CALL, cost='3.000000', invoice_id='offline-invoice-1', proof=SHA):
    return {'target': target, 'kind': 'invoice', 'actual_credits': cost,
            'invoice_id': invoice_id, 'evidence_sha256': proof}


def settle(state, proposals=None, not_sent=()):
    return s.prepare(state, proposals or [invoice()], s.catalog(state, []), not_sent)


def test_invoice_releases_only_difference_preserving_original_and_counts():
    before = original(); state, result = settle(before)
    assert before == original()  # pure preview
    assert state['reserved_credits'] == 8 and state['image'] == 2
    assert s.summary(state) == dict(original_reserved=8., settled_actual=3., settled_reserved=4.,
        pending_reserved=4., released_credits=1., additional_credits=0., effective_credits=7., external_unknown=0., total_occupied=7.)
    assert result['changed'] == [CALL]


def test_overrun_is_charged_unknown_external_stays_and_zero_invoice_is_explicit():
    state = {**original(), 'external_unknown': 3}
    state, _ = settle(state, [invoice(cost=6)])
    assert s.summary(state)['additional_credits'] == 2 and s.occupied(state) == 13
    state, _ = settle(state, [invoice(SECOND, 0, 'offline-zero-receipt')])
    assert s.occupied(state) == 9 and state['external_unknown'] == 3


def test_repeat_is_exactly_idempotent_and_conflicting_amount_or_invoice_is_refused():
    state, _ = settle(original()); frozen = copy.deepcopy(state)
    again, result = settle(state)
    assert again == frozen and result['changed'] == [] and result['unchanged'] == 1
    for proposal in [invoice(cost=2), invoice(invoice_id='changed-receipt'), invoice(proof='2'*64), invoice(SECOND)]:
        with pytest.raises(ValueError):
            settle(state, [proposal])
    assert state == frozen


def test_confirmed_unsent_releases_cost_but_never_attempts():
    proposal = {'target': CALL, 'kind': 'confirmed_not_sent', 'evidence_sha256': SHA}
    with pytest.raises(ValueError, match='未知费用'):
        settle(original(), [proposal])
    state, _ = settle(original(), [proposal], {CALL})
    assert s.occupied(state) == 4 and state['image'] == 2 and state['reserved_credits'] == 8
    assert s.summary(state)['settled_actual'] == 0


def test_confirmed_no_charge_needs_explicit_evidence_and_preserves_unknowns():
    before = original()
    proposal = {'target': CALL, 'kind': 'confirmed_no_charge',
                'confirmation_source': 'user', 'evidence_sha256': SHA}
    with pytest.raises(ValueError, match='明确未扣费'):
        s.prepare(before, [proposal], s.catalog(before, []))
    state, _ = s.prepare(before, [proposal], s.catalog(before, []), no_charge={CALL})
    assert before == original()
    assert s.summary(state)['pending_reserved'] == 4
    assert s.summary(state)['released_credits'] == 4
    assert s.summary(state)['settled_actual'] == 0
    assert state['reserved_credits'] == 8 and state['image'] == 2
    record = state['settlements'][CALL]
    assert record['kind'] == 'confirmed_no_charge' and record['invoice_id'] is None
    assert record['confirmation_source'] == 'user'
    again, result = s.prepare(state, [proposal], s.catalog(state, []), no_charge={CALL})
    assert again == state and result['changed'] == []
    forged = {**proposal, 'actual_credits': 0, 'invoice_id': 'not-a-real-invoice'}
    with pytest.raises(ValueError):
        s.prepare(before, [forged], s.catalog(before, []), no_charge={CALL})


@pytest.mark.parametrize('cost', [-1, True, None, 'NaN', 'Infinity', '0.0000001', 'abc', 1000000001])
def test_bad_money_never_changes_ledger(cost):
    state = original(); before = copy.deepcopy(state)
    with pytest.raises(ValueError):
        settle(state, [invoice(cost=cost)])
    assert state == before


def test_no_invoice_unknown_and_duplicate_targets_are_not_settlements():
    for proposals in [[{'target': CALL, 'kind': 'unknown', 'evidence_sha256': SHA}],
                      [invoice(), invoice()], [invoice('missing')], [invoice(), invoice(SECOND, 2)]]:
        with pytest.raises(ValueError):
            settle(original(), proposals)


def test_corrupt_adjustments_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(budget, '_root', lambda: tmp_path)
    state, _ = settle(original()); state['settlements'][CALL]['actual_credits'] = 0
    budget._save(tmp_path/'call-budget.json', state)
    assert budget.budget_state()['paused']
    with pytest.raises(ValueError):
        s.summary(state)


def test_legacy_and_current_evidence_merge_without_double_count_and_missing_is_not_inferred():
    state = original(); state['reservation_calls'] = {CALL: {'role': 'image', 'attempt': 1, 'reserved': 4}}
    rows = [{'call_id': CALL, 'role': 'image', 'attempt': 1, 'reserved_credits': 4}]
    assert len(s.catalog(state, rows)) == 2
    with pytest.raises(ValueError):
        s.catalog(state, rows + [{**rows[0], 'call_id': SECOND}])
    # A missing historical finish row does not imply an unsent or free request.
    legacy = {'text': 2, 'image': 0, 'reserved_credits': 1.6, 'paused': False}
    catalog = s.catalog(legacy, [{'role': 'text', 'attempt': 1, 'reserved_credits': .8}])
    assert 'legacy:text:2' not in catalog
    with pytest.raises(ValueError):
        s.prepare(legacy, [invoice('legacy:text:2', '.084825')], catalog)
    result, _ = s.prepare(legacy, [invoice('legacy:text:1', '.1')], catalog)
    assert s.summary(result)['pending_reserved'] == .8


def test_six_decimal_reference_math_and_partial_settlement_keeps_unknown():
    # Same totals as the reviewed example; synthetic request IDs/receipts only.
    state = {'text': 8, 'image': 22, 'reserved_credits': 94.4, 'external_unknown': 3, 'paused': False,
             'reservation_calls': {}}
    proposals = []
    costs = [.084825, .117739, .155145, .206295, .097170, .288865, .340425] + [4]*8 + [3]*5 + [6] + [3]*6
    assert len(costs) == 27
    for index, cost in enumerate(costs):
        key = f'offline-{index}'
        state['reservation_calls'][key] = {'role': 'text' if index < 7 else 'image', 'reserved': .8 if index < 7 else 4}
        proposals.append(invoice(key, cost, f'offline-bill-{index}'))
    state, _ = settle(state, proposals)
    result = s.summary(state)
    assert result['settled_actual'] == 72.290464 and result['pending_reserved'] == 8.8
    assert result['effective_credits'] == 81.090464 and result['total_occupied'] == 84.090464
    assert result['released_credits'] == 15.309536 and result['additional_credits'] == 2


@pytest.fixture
def private_instance(tmp_path):
    root = tmp_path/'private'; (root/'run').mkdir(parents=True); (root/'data').mkdir()
    state = original(); state['generation_plans'] = {'old-aborted': {'state': 'aborted', 'released_unsent': {'text': 2, 'image': 5}}}
    budget._save(root/'run/call-budget.json', state)
    with sqlite3.connect(root/'data/database.db') as c:
        c.executescript('''CREATE TABLE tasks(id TEXT,status TEXT);
            CREATE TABLE pc_visitor_requests(task_id TEXT,state TEXT);
            CREATE TABLE pc_visitor_calls(call_id TEXT,role TEXT,reserved REAL,task_id TEXT);
            CREATE TABLE task_traces(calls TEXT);''')
        c.execute('INSERT INTO pc_visitor_calls VALUES(?,?,?,?)', (CALL, 'image', 4, 'task-one'))
        c.execute('INSERT INTO pc_visitor_calls VALUES(?,?,?,?)', (SECOND, 'image', 4, 'task-two'))
        c.execute('INSERT INTO task_traces VALUES(?)', (json.dumps([{'call_id': CALL, 'status': 'response_saved', 'sent_at': 'offline-time'}]),))
    proof = json.dumps({'kind': 'invoice', 'target': CALL, 'invoice_id': 'offline-invoice-1', 'actual_credits': 3,
                        'reference': 'synthetic invoice fixture, never a real bill'}).encode()
    (root/'receipt.json').write_bytes(proof)
    proposal = invoice(proof=hashlib.sha256(proof).hexdigest())
    proposal['evidence'] = {'path': 'receipt.json', 'sha256': proposal.pop('evidence_sha256')}
    manifest = root/'settle.json'; manifest.write_text(json.dumps({'entries': [proposal]}), encoding='utf8')
    return root, manifest, state


def test_maintenance_dry_run_apply_replay_original_archives_and_db_immutable(private_instance):
    root, manifest, before = private_instance
    old = (root/'run/call-budget.json').read_bytes(); database = (root/'data/database.db').read_bytes()
    preview = run(root, manifest)
    assert preview['after']['total_occupied'] == 7 and not preview['applied']
    assert (root/'run/call-budget.json').read_bytes() == old
    assert not (root/'run/settlement-evidence').exists()
    result = run(root, manifest, preview['confirmation']); assert result['applied']
    new = (root/'run/call-budget.json').read_bytes()
    assert run(root, manifest, preview['confirmation'])['changed'] == []
    assert (root/'run/call-budget.json').read_bytes() == new
    assert (root/'data/database.db').read_bytes() == database
    assert (root/'run/settlement-evidence'/(hashlib.sha256(old).hexdigest()+'.json')).read_bytes() == old
    after = json.loads(new)
    assert {k: v for k, v in after.items() if k != 'settlements'} == before


def test_concurrent_maintenance_releases_once(private_instance):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    root, manifest, before = private_instance
    token = run(root, manifest)['confirmation']; barrier = Barrier(2)
    def apply_once():
        barrier.wait(timeout=5)
        try:
            return run(root, manifest, token)
        except RuntimeError as exc:
            assert '锁定' in str(exc)
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(apply_once) for _ in range(2)]
        results = [future.result(timeout=15) for future in futures]
    assert sum(len(result['changed']) for result in results if result) == 1
    after = json.loads((root/'run/call-budget.json').read_bytes())
    assert len(after['settlements']) == 1 and s.occupied(after) == 7
    assert {k: v for k, v in after.items() if k != 'settlements'} == before


def test_data_lock_excludes_another_process(private_instance):
    import subprocess
    import sys
    root, _, _ = private_instance
    scripts = Path(__file__).resolve().parents[1]/'scripts'
    code = '''import sys,socket
sys.path.insert(0,sys.argv[1])
def blocked(*args,**kwargs): raise AssertionError('offline sockets blocked')
socket.socket.connect=blocked
socket.socket.connect_ex=blocked
from managed_server import DataLock
try:
    with DataLock(sys.argv[2]): pass
except RuntimeError:
    print('locked')
else:
    raise AssertionError('another process acquired the active lock')
'''
    with DataLock(root/'run'):
        result = subprocess.run([sys.executable, '-I', '-B', '-X', 'utf8', '-c', code,
                                 str(scripts), str(root/'run')], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'locked'


def test_incomplete_temporary_write_keeps_original_parseable(private_instance, monkeypatch):
    root, manifest, _ = private_instance
    token = run(root, manifest)['confirmation']; old = (root/'run/call-budget.json').read_bytes()
    def partial_write(path, value):
        path.with_suffix('.tmp').write_text('{"partial":', encoding='utf8')
        raise OSError('synthetic failure before replace')
    monkeypatch.setattr(budget, '_save', partial_write)
    with pytest.raises(OSError): run(root, manifest, token)
    assert (root/'run/call-budget.json').read_bytes() == old
    assert s.occupied(json.loads(old)) == 8


def test_failure_after_atomic_commit_replays_without_second_release(private_instance, monkeypatch):
    root, manifest, before = private_instance
    token = run(root, manifest)['confirmation']; save = budget._save
    def committed_then_failed(path, value):
        save(path, value)
        raise OSError('synthetic lost acknowledgement after replace')
    monkeypatch.setattr(budget, '_save', committed_then_failed)
    with pytest.raises(OSError): run(root, manifest, token)
    committed = (root/'run/call-budget.json').read_bytes()
    monkeypatch.setattr(budget, '_save', save)
    assert run(root, manifest, token)['changed'] == []
    assert (root/'run/call-budget.json').read_bytes() == committed
    after = json.loads(committed)
    assert len(after['settlements']) == 1 and s.occupied(after) == 7
    assert {k: v for k, v in after.items() if k != 'settlements'} == before


@pytest.mark.parametrize('fault', ['busy', 'active', 'pending', 'proof', 'stale', 'visitor', 'visitor_missing', 'save'])
def test_failed_maintenance_preserves_original(private_instance, monkeypatch, fault):
    root, manifest, _ = private_instance
    preview = run(root, manifest); token = preview['confirmation']
    if fault in ('active', 'pending', 'visitor', 'visitor_missing'):
        with sqlite3.connect(root/'data/database.db') as c:
            if fault == 'active': c.execute("INSERT INTO tasks VALUES('busy','PROCESSING')")
            elif fault == 'pending': c.execute("INSERT INTO pc_visitor_requests VALUES(NULL,'unknown')")
            elif fault == 'visitor': c.execute('UPDATE pc_visitor_calls SET reserved=5')
            else: c.execute('DELETE FROM pc_visitor_calls')
    if fault == 'proof': (root/'receipt.json').write_text('{}')
    if fault == 'stale': token = '0'*64
    if fault == 'save':
        monkeypatch.setattr(budget, '_save', lambda *args: (_ for _ in ()).throw(OSError('offline write failure')))
    before = (root/'run/call-budget.json').read_bytes()
    if fault == 'busy':
        with DataLock(root/'run'), pytest.raises(RuntimeError): run(root, manifest, token)
    else:
        with pytest.raises((ValueError, RuntimeError, OSError)): run(root, manifest, token)
    assert (root/'run/call-budget.json').read_bytes() == before


@pytest.mark.parametrize('field,value', [('target', SECOND), ('invoice_id', 'wrong'), ('actual_credits', 2)])
def test_proof_fields_must_match_the_exact_call_and_bill(private_instance, field, value):
    root, manifest, _ = private_instance
    proof = json.loads((root/'receipt.json').read_bytes()); proof[field] = value
    raw = json.dumps(proof).encode(); (root/'receipt.json').write_bytes(raw)
    request = json.loads(manifest.read_text()); request['entries'][0]['evidence']['sha256'] = hashlib.sha256(raw).hexdigest()
    manifest.write_text(json.dumps(request))
    before = (root/'run/call-budget.json').read_bytes()
    with pytest.raises(ValueError): run(root, manifest)
    assert (root/'run/call-budget.json').read_bytes() == before


def test_actual_cli_entry_is_preview_only_without_confirmation(private_instance, monkeypatch, capsys):
    import presentation_copilot as runner
    root, manifest, _ = private_instance
    cfg = root/'runtime.json'; cfg.write_text(json.dumps({'data_dir': str(root), 'secret_file': str(root/'.env.copilot')}))
    before = (root/'run/call-budget.json').read_bytes()
    # Restore module path globals after the CLI's normal config selection.
    for name in ('RUNTIME', 'CONFIG', 'PID_FILE'):
        monkeypatch.setattr(runner, name, getattr(runner, name))
    def forbidden(*args, **kwargs): raise AssertionError('settlement must not load credentials or initialize providers')
    monkeypatch.setattr(runner, 'config_values', forbidden)
    monkeypatch.setattr(runner, 'create_local_app', forbidden)
    monkeypatch.setattr('sys.argv', ['presentation_copilot.py', '--runtime-config', str(cfg), 'ledger-settle', '--settlement-file', str(manifest)])
    runner.main()
    result = json.loads(capsys.readouterr().out)
    assert not result['applied'] and result['model_requests'] == 0 and result['after']['total_occupied'] == 7
    assert (root/'run/call-budget.json').read_bytes() == before
    assert not (root/'.env.copilot').exists()


@pytest.mark.parametrize('status,sent,usage,expected', [
    ('local_rejected', None, {}, True), ('failed_needs_review', None, {}, False),
    ('local_rejected', 'sent', {}, False), ('response_saved', None, {}, False),
    ('local_rejected', None, {'total_tokens': 3}, False)])
def test_not_sent_needs_positive_evidence_not_missing_log(private_instance, status, sent, usage, expected):
    root, manifest, _ = private_instance
    trace = {'call_id': CALL, 'status': status, 'sent_at': sent, 'usage': usage, 'ended_at': 'offline',
             'failure_stage': 'configuration_or_budget', 'actual_credits': 0,
             'actual_credits_source': 'confirmed_not_sent', 'reserved_credits': 4}
    with sqlite3.connect(root/'data/database.db') as c:
        c.execute('UPDATE task_traces SET calls=?', (json.dumps([trace]),))
    proposal = json.loads(manifest.read_text())['entries'][0]
    proposal.pop('actual_credits'); proposal.pop('invoice_id'); proposal['kind'] = 'confirmed_not_sent'
    proof = json.dumps({'target': CALL, 'kind': 'confirmed_not_sent', 'reference': 'synthetic local rejection trace'}).encode()
    (root/'receipt.json').write_bytes(proof)
    proposal['evidence']['sha256'] = hashlib.sha256(proof).hexdigest()
    manifest.write_text(json.dumps({'entries': [proposal]}))
    if expected:
        result = run(root, manifest); assert result['after']['total_occupied'] == 4
        assert run(root, manifest, result['confirmation'])['applied']
        after = json.loads((root/'run/call-budget.json').read_bytes())
        assert after['reserved_credits'] == 8 and after['image'] == 2 and s.occupied(after) == 4
    else:
        with pytest.raises(ValueError, match='未知费用'): run(root, manifest)


def no_charge_fixture(private_instance, *, source='user', trace_change=None, proof_change=None):
    root, manifest, state = private_instance
    trace = {'call_id': CALL, 'status': 'failed_needs_review', 'sent_at': 'offline-time',
             'ended_at': 'offline-end', 'usage': None, 'actual_credits': None}
    trace.update(trace_change or {})
    with sqlite3.connect(root/'data/database.db') as c:
        c.execute('UPDATE task_traces SET calls=?', (json.dumps([trace]),))
    proof = {'target': CALL, 'kind': 'confirmed_no_charge', 'confirmed_no_charge': True,
             'confirmation_source': source, 'confirmation': 'Synthetic explicit zero-charge confirmation.',
             'reference': 'isolated user/provider confirmation fixture; not a platform invoice'}
    proof.update(proof_change or {})
    raw = json.dumps(proof).encode()
    (root/'receipt.json').write_bytes(raw)
    proposal = {'target': CALL, 'kind': 'confirmed_no_charge', 'confirmation_source': source,
                'evidence': {'path': 'receipt.json', 'sha256': hashlib.sha256(raw).hexdigest()}}
    manifest.write_text(json.dumps({'entries': [proposal]}))
    return root, manifest, state


@pytest.mark.parametrize('source', ['user', 'provider'])
def test_no_charge_maintenance_archives_replays_and_never_fakes_invoice(private_instance, source):
    root, manifest, before = no_charge_fixture(private_instance, source=source)
    old = (root/'run/call-budget.json').read_bytes()
    database = (root/'data/database.db').read_bytes()
    preview = run(root, manifest)
    assert preview['after']['total_occupied'] == 4 and not preview['applied']
    assert (root/'run/call-budget.json').read_bytes() == old
    assert run(root, manifest, preview['confirmation'])['changed'] == [CALL]
    committed = (root/'run/call-budget.json').read_bytes()
    assert run(root, manifest, preview['confirmation'])['changed'] == []
    assert (root/'run/call-budget.json').read_bytes() == committed
    assert (root/'data/database.db').read_bytes() == database
    assert (root/'run/settlement-evidence'/(hashlib.sha256(old).hexdigest()+'.json')).read_bytes() == old
    state = json.loads(committed)
    assert {k: v for k, v in state.items() if k != 'settlements'} == before
    assert state['settlements'][CALL]['kind'] == 'confirmed_no_charge'
    assert state['settlements'][CALL]['confirmation_source'] == source
    assert state['settlements'][CALL]['invoice_id'] is None


@pytest.mark.parametrize('change', [
    {'confirmed_no_charge': False}, {'confirmed_no_charge': 'true'},
    {'confirmation_source': 'estimate'}, {'confirmation_source': 'provider'},
    {'confirmation': ''}, {'confirmation': None}, {'target': SECOND},
])
def test_no_charge_needs_matching_explicit_confirmation(private_instance, change):
    root, manifest, _ = no_charge_fixture(private_instance, proof_change=change)
    before = (root/'run/call-budget.json').read_bytes()
    with pytest.raises(ValueError):
        run(root, manifest)
    assert (root/'run/call-budget.json').read_bytes() == before


@pytest.mark.parametrize('change', [
    {'status': 'response_saved'}, {'status': 'local_rejected'},
    {'sent_at': None}, {'ended_at': None}, {'actual_credits': 6},
    {'usage': {'total_tokens': 3}},
])
def test_no_charge_conflicting_trace_cannot_release(private_instance, change):
    root, manifest, _ = no_charge_fixture(private_instance, trace_change=change)
    before = (root/'run/call-budget.json').read_bytes()
    with pytest.raises(ValueError, match='未扣费确认'):
        run(root, manifest)
    assert (root/'run/call-budget.json').read_bytes() == before


def test_concurrent_zero_charge_confirmation_releases_once(private_instance):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    root, manifest, _ = no_charge_fixture(private_instance)
    token = run(root, manifest)['confirmation']
    barrier = Barrier(2)

    def apply_once():
        barrier.wait(timeout=5)
        try:
            return run(root, manifest, token)
        except RuntimeError as exc:
            assert '锁定' in str(exc)
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(apply_once) for _ in range(2)]
        results = [future.result(timeout=15) for future in futures]
    assert sum(len(x['changed']) for x in results if x) == 1
    state = json.loads((root/'run/call-budget.json').read_bytes())
    assert s.summary(state)['released_credits'] == 4 and s.occupied(state) == 4

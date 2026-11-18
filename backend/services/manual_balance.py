"""Manual balance checkpoint and success-only estimates in the existing ledger.

Not supplier billing. Never settles, deletes or rewrites historical attempts.
The existing admin transaction creates checkpoints; all other operations read.
"""
import copy
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from decimal import Decimal
from services.ledger_settlement import amount, digest


def validate(value):
    if not isinstance(value, dict) or set(value) != {'id', 'credits', 'image_2k', 'text_fallback'}:
        raise ValueError('人工余额基准字段不完整。')
    if not isinstance(value['id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}', value['id']):
        raise ValueError('余额校准需要唯一标识。')
    for key in ('credits', 'image_2k', 'text_fallback'):
        if not 0 <= amount(value[key]) <= 100000:
            raise ValueError('余额或估算单价无效。')
    if not value['image_2k'] or not value['text_fallback']:
        raise ValueError('成功调用估算单价必须大于0。')
    return value


def configured(auth=None):
    if auth is None:
        from services.ai_providers import copilot_limits as b
        import os
        if not os.getenv('PC_RUN_POLICY'):
            return None
        auth = b.runtime_policy()['authorization']
    value = (auth or {}).get('balance')
    return validate(value) if value is not None else None


def initialize(state, value):
    validate(value)
    checkpoints = state.setdefault('balance_checkpoints', {})
    old = checkpoints.get(value['id'])
    if old:
        if old['settings'] != value or old['digest'] != digest({k:v for k,v in old.items() if k!='digest'}):
            raise ValueError('同一余额基准不可改值；请明确建立新校准。')
        if state.get('active_balance_checkpoint') != value['id']:
            raise ValueError('不能重新启用旧余额基准。')
        return
    row = {'settings': copy.deepcopy(value), 'created_at': datetime.now(timezone.utc).isoformat(),
           'previous_id': state.get('active_balance_checkpoint'),
           'prior_call_ids': sorted(state.get('reservation_calls', {})),
           'source': 'user_reported_balance_not_live_provider_balance'}
    row['digest'] = digest(row)
    checkpoints[value['id']] = row
    state['active_balance_checkpoint'] = value['id']


def summary(state, auth=None):
    value = configured(auth)
    if value is None:
        return None
    row = state.get('balance_checkpoints', {}).get(value['id'])
    if (not row or row['settings'] != value or state.get('active_balance_checkpoint') != value['id']
            or row['digest'] != digest({k:v for k,v in row.items() if k!='digest'})):
        raise ValueError('人工余额尚未通过正式配置应用。')
    from services.ai_providers import copilot_limits as b
    prior = set(row['prior_call_ids'])
    calls = {k:v for k,v in state.get('reservation_calls', {}).items() if k not in prior}
    spent = Decimal(0); pending = Decimal(0)
    successful = {'text':0, 'image':0}; pending_counts = {'text':0, 'image':0}; failed = 0; unknown = 0
    outcomes = {}
    database = Path(b._root()).parent/'data/database.db'
    # Tests and managed runtime share the configured application's database.
    from flask import has_app_context, current_app
    if has_app_context():
        database = Path(current_app.config['SQLALCHEMY_DATABASE_URI'].removeprefix('sqlite:///'))
    with sqlite3.connect(database.resolve().as_uri()+'?mode=ro', uri=True) as conn:
        traces = {}
        for call_id, original in calls.items():
            task = original.get('task_id')
            if task not in traces:
                found = conn.execute('SELECT calls FROM task_traces WHERE task_id=?', (task,)).fetchone()
                traces[task] = {c['call_id']:c for c in json.loads(found[0])} if found else {}
            call = traces[task].get(call_id, {})
            status = call.get('status')
            role = original['role']
            unit = amount(value['image_2k'] if role=='image' else value['text_fallback'])
            if status == 'response_saved':
                outcomes[call_id] = 'success'
                successful[role] += 1
                # Multiple returned images can be billed even when only the last is displayed.
                n = call.get('returned_image_count', 1) if role=='image' else 1
                if type(n) is not int or not 1 <= n <= 100:
                    raise ValueError('返回图片数量异常，需核对。')
                spent += unit*n
            elif status in ('failed_needs_review','local_rejected') or call.get('failure_stage') == 'input_or_save':
                outcomes[call_id] = 'failed'
                failed += 1  # zero local estimate, NOT confirmed zero supplier fee
            else:
                outcomes[call_id] = 'pending'
                pending += unit; unknown += 1; pending_counts[role] += 1
    balance = amount(value['credits']) - spent
    return {'id':value['id'], 'created_at':row['created_at'], 'reported_balance':value['credits'],
            'estimated_spent':float(spent), 'estimated_remaining':float(balance),
            'in_flight_reserved':float(pending), 'available':float(max(Decimal(0),balance-pending)),
            'success_counts':successful, 'failed_calls':failed, 'unresolved_calls':unknown,
            'pending_counts':pending_counts, '_outcomes':outcomes,
            'image_2k':value['image_2k'], 'text_fallback':value['text_fallback'],
            'source':row['source']}


def limits(state, auth=None):
    from services.ledger_settlement import occupied
    from services.budget_cycles import credit_limit
    from services.ai_providers import copilot_limits as b
    auth = auth or b.runtime_policy()['authorization']
    manual = summary(state, auth)
    if manual:
        return manual['reported_balance'], manual['estimated_spent'] + manual['in_flight_reserved']
    return credit_limit(state, auth), occupied(state)


def unit(role, auth=None):
    from services.ai_providers import copilot_limits as b
    value = configured(auth)
    return value['image_2k' if role=='image' else 'text_fallback'] if value else b.RESERVATIONS[role]


def task_hold(hold, auth=None):
    return sum(hold[r]*unit(r, auth) for r in ('text','image'))


def previous_failure_blocks(state):
    """Finite acceptance permits remain restrictive; ordinary failures don't block new tasks."""
    if not state.get('paused'):
        return False
    if not configured() or not state.get('runtime_calls'):
        return True
    if state.get('active_image_retry') or any(p.get('state')=='ready' for p in state.get('image_retry_permits', {}).values()):
        return True
    return not (state.get('pause_reason') == 'provider_error' or
                state.get('last_result')=='error' and state.get('last_call_id') in state['runtime_calls'])


def counts(state, auth=None):
    """Quota counts exclude failed attempts; attempt evidence remains immutable."""
    current = summary(state, auth)
    if current:
        return {r: current['success_counts'][r] + current['pending_counts'][r] for r in ('text','image')}
    from services.budget_cycles import counts as original_counts
    return original_counts(state)


def quota_rows(state, rows):
    current = summary(state)
    if current is None:
        return rows
    return [row for row in rows if current['_outcomes'].get(row.call_id) in ('success','pending')]

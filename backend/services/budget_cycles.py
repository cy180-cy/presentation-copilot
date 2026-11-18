"""A one-time, explicit cycle baseline over immutable lifetime counters.

Authorization names, process restarts and admin reapplication never create cycles.
No HTTP request can select a cycle or reset its baseline.
"""
import hashlib
import json
import re
from decimal import Decimal


def checksum(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def current(state):
    value = state.get('quota_cycle')
    if value is None:
        return None
    if (not isinstance(value, dict) or value.get('version') != 1
            or value.get('migration_id') != 'pc0153f-initial-cycle-v1'
            or value.get('digest') != checksum({k: v for k, v in value.items() if k != 'digest'})):
        raise ValueError('额度周期记录损坏，禁止执行。')
    from services.ledger_settlement import amount
    if (not re.fullmatch(r'[A-Za-z0-9_-]{8,80}',value['id'])
            or any(not re.fullmatch(r'[0-9a-f]{64}',value[k]) for k in ('source_ledger_sha256','archive_manifest_sha256'))
            or not isinstance(value['prior_runtime_call_ids'],list)
            or len(set(value['prior_runtime_call_ids']))!=len(value['prior_runtime_call_ids'])):
        raise ValueError('额度周期身份或证据无效。')
    start = value['baseline']
    for role in ('text', 'image'):
        if type(start[role]) is not int or type(state[role]) is not int or start[role] < 0 or state[role] < start[role]:
            raise ValueError('额度周期累计次数不一致。')
    if amount(state['reserved_credits']) < amount(start['reserved_credits']):
        raise ValueError('额度周期原预留不能倒退。')
    amount(value['external_unknown_baseline'])
    limit = amount(value['credit_limit'])
    if not limit.is_finite() or not 0 < limit <= 100000:
        raise ValueError('额度周期停止线无效。')
    return value


def identity(state):
    value = current(state)
    return value['id'] if value else None


def counts(state):
    value = current(state)
    return {role: state[role] - (value['baseline'][role] if value else 0) for role in ('text', 'image')}


def credit_limit(state, authorization):
    value = current(state)
    return min(authorization['credits'], value['credit_limit']) if value else authorization['credits']


def owns_call(state, call_id):
    value = current(state)
    if not value:
        return True
    row = state.get('reservation_calls', {}).get(call_id) or state.get('runtime_calls', {}).get(call_id)
    return bool(row and row.get('quota_cycle_id') == value['id'])


def assert_task(state, task_id):
    for record in state.get('acceptance_closeouts',{}).values():
        if record.get('digest') != checksum({k:v for k,v in record.items() if k!='digest'}):
            raise RuntimeError('验收收尾证据无效，未发送请求。')
        if task_id and task_id in record['closed_task_ids']:
            raise RuntimeError('此任务属于已归档验收，不允许重放；请创建新任务。')
    value = current(state)
    if not value:
        return
    from models import db, TaskTrace
    row = db.session.get(TaskTrace, task_id) if task_id else None
    if row is None or json.loads(row.snapshot).get('quota_cycle_id') != value['id']:
        raise RuntimeError('此任务不属于当前额度周期，未发送请求；请查看原任务。')


def create(state, *, cycle_id, credit_cap, source_sha256, archive_sha256, created_at):
    """Pure one-time transition. The maintenance command supplies locked evidence."""
    import copy
    import re
    existing = current(state)
    if existing:
        if existing['id'] != cycle_id or existing['credit_limit'] != credit_cap:
            raise ValueError('已建立本轮额度周期，禁止用其他ID/预算再次清零。')
        return copy.deepcopy(state), False
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,80}', cycle_id):
        raise ValueError('周期ID需8–80位字母、数字、横线或下划线。')
    cap = Decimal(str(credit_cap))
    if not cap.is_finite() or not 0 < cap <= 100000 or cap != cap.quantize(Decimal('.000001')):
        raise ValueError('周期预算无效。')
    result = copy.deepcopy(state)
    value = {'version': 1, 'migration_id': 'pc0153f-initial-cycle-v1', 'id': cycle_id,
        'previous_id': 'legacy-' + source_sha256[:16], 'created_at': created_at,
        'credit_limit': float(cap), 'baseline': {k: state[k] for k in ('text', 'image', 'reserved_credits')},
        'external_unknown_baseline': state.get('external_unknown', 0),
        'source_ledger_sha256': source_sha256, 'archive_manifest_sha256': archive_sha256,
        'previous_paused': state['paused'],
        'history_status': 'preserved_not_reconciled',
        'prior_runtime_call_ids': sorted(state.get('runtime_calls', {}))}
    value['digest'] = checksum(value)
    result['quota_cycle'] = value
    # Only the active failure latch changes; its former value is archived above.
    # Models stay closed by the separate preview/admin pause transition.
    result['paused'] = False
    current(result)
    return result, True


def revise(state, *, cycle_id, credit_cap, revision_id, reason, source_sha256,
           archive_sha256, created_at, retire_permit_ids=()):
    """Explicit absolute cap amendment; never a new baseline or an increment."""
    import copy
    from services.ledger_settlement import amount, summary
    cycle = current(state)
    if not cycle or cycle['id'] != cycle_id:
        raise ValueError('只能修订当前周期，不能创建或切换周期。')
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,80}', revision_id or '') or not reason or len(reason) > 2000:
        raise ValueError('需要唯一修订ID及明确授权依据。')
    cap = amount(credit_cap)
    if not 0 < cap <= 100000:
        raise ValueError('周期停止线无效。')
    retired = sorted(retire_permit_ids)
    if len(retired) != len(set(retired)) or len(retired) > 4:
        raise ValueError('撤销许可范围重复或超限。')
    records = state.get('quota_cycle_revisions', {})
    if revision_id in records:
        old = records[revision_id]
        if old['cycle_id'] != cycle_id or old['after'] != float(cap) or old['reason'] != reason or sorted(old.get('retired_permits', {})) != retired:
            raise ValueError('同一修订ID不能更换授权内容。')
        if cycle['credit_limit'] != float(cap):
            raise ValueError('周期已有后续修订，不能重放旧修订。')
        return copy.deepcopy(state), False
    if cap < amount(summary(state)['total_occupied']):
        raise ValueError('停止线不能低于当前有效占用。')
    permits = state.get('image_retry_permits', {})
    pending_ids = sorted(k for k,p in permits.items() if p.get('state') == 'ready')
    if pending_ids != retired:
        raise ValueError('须明确列出全部待撤销的旧许可，不能隐式改变绑定预算。')
    if retired:
        from services.image_retry import ready
        if ready(state) or not state.get('paused'):
            raise ValueError('只能衔接已失效且暂停中的旧许可。')
        groups = []
        for key in retired:
            p = permits[key]
            if (p.get('purpose') != 'historical_quality' or p.get('cycle_id') != cycle_id
                    or p.get('used_call_id') is not None or p.get('used_task_id') is not None
                    or p.get('digest') != checksum({k:v for k,v in p.items() if k != 'digest'})):
                raise ValueError('旧许可已消费、损坏或不属于当前历史修正范围。')
            groups.append(p.get('group_ids'))
        if not groups[0] or any(g != groups[0] for g in groups) or not set(retired).issubset(groups[0]):
            raise ValueError('只能衔接同一历史修正批次。')
    result = copy.deepcopy(state)
    for key in retired:
        p = result['image_retry_permits'][key]
        p.update(state='revoked', revoked_by_revision=revision_id, revoked_at=created_at)
        p['digest'] = checksum({k:v for k,v in p.items() if k != 'digest'})
    result.setdefault('quota_cycle_revisions', {})[revision_id] = {
        'cycle_id': cycle_id, 'before': cycle['credit_limit'], 'after': float(cap),
        'reason': reason, 'created_at': created_at, 'source_ledger_sha256': source_sha256,
        'archive_manifest_sha256': archive_sha256, 'previous_cycle': copy.deepcopy(cycle),
        'retired_permits': {k: copy.deepcopy(permits[k]) for k in retired}}
    value = result['quota_cycle']
    value['credit_limit'] = float(cap)
    value['digest'] = checksum({k: v for k, v in value.items() if k != 'digest'})
    current(result)
    return result, True

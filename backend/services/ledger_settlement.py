"""Evidence-backed adjustments; original reservations and attempt counts never change.

No provider client or billing estimate belongs here. The only write entry is the
stopped-instance maintenance command, under the existing data-directory lock.
"""
import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

UNIT = Decimal('0.000001')
ID = re.compile(r'[A-Za-z0-9_.:-]{1,160}')


def amount(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError('积分必须是非负有限数字，最多六位小数。')
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or result > Decimal('1000000000') or result != result.quantize(UNIT):
            raise ValueError()
        return result
    except (InvalidOperation, ValueError):
        raise ValueError('积分必须是非负有限数字，最多六位小数。') from None


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def entries(state):
    """Validate stored adjustments before any guard can use released capacity."""
    if any(not isinstance(state.get(name, {}), dict) for name in ('runtime_calls', 'reservation_calls')):
        raise ValueError('原预留明细损坏。')
    rows = state.get('settlements', {})
    if not isinstance(rows, dict):
        raise ValueError('结算记录损坏。')
    invoices = set()
    for target, row in rows.items():
        if not isinstance(row, dict) or row.get('target') != target or row.get('role') not in ('text', 'image'):
            raise ValueError('结算归属损坏。')
        if not isinstance(target, str) or not ID.fullmatch(target):
            raise ValueError('结算标识无效。')
        if row.get('digest') != digest({k: v for k, v in row.items() if k != 'digest'}):
            raise ValueError('结算记录校验失败。')
        original, actual = amount(row['reserved_credits']), amount(row['actual_credits'])
        if original <= 0:
            raise ValueError('没有可结算的原预留。')
        if row.get('kind') == 'invoice':
            invoice = row.get('invoice_id', '')
            if not isinstance(invoice, str) or not ID.fullmatch(invoice) or invoice.casefold() in invoices:
                raise ValueError('账单标识无效或重复。')
            invoices.add(invoice.casefold())
        elif row.get('kind') in ('confirmed_not_sent', 'confirmed_no_charge'):
            if actual != 0 or row.get('invoice_id') is not None:
                raise ValueError('未发送或确认未扣费结算不能伪造实扣或账单。')
            if row['kind'] == 'confirmed_no_charge' and row.get('confirmation_source') not in ('user', 'provider'):
                raise ValueError('确认未扣费结算缺少明确确认来源。')
        else:
            raise ValueError('结算类型无效。')
        if not re.fullmatch(r'[0-9a-f]{64}', row.get('evidence_sha256', '')):
            raise ValueError('结算缺少证据摘要。')
        recorded = state.get('reservation_calls', {}).get(target) or state.get('runtime_calls', {}).get(target)
        if recorded and (recorded['role'] != row['role'] or amount(recorded['reserved']) != original
                         or recorded.get('quota_cycle_id') != row.get('quota_cycle_id')):
            raise ValueError('结算与原预留不符。')
    return rows


def summary(state, *, lifetime=False):
    from services.budget_cycles import current, owns_call
    cycle = None if lifetime else current(state)
    original = amount(state['reserved_credits'])
    rows = list(entries(state).values())
    if cycle:
        original -= amount(cycle['baseline']['reserved_credits'])
        rows = [r for r in rows if owns_call(state, r['target'])]
    settled = sum((amount(r['reserved_credits']) for r in rows), Decimal(0))
    actual = sum((amount(r['actual_credits']) for r in rows if r['kind'] == 'invoice'), Decimal(0))
    if settled > original:
        raise ValueError('结算预留超过原始累计。')
    pending = original - settled
    unknown = amount(state.get('external_unknown', 0))
    if cycle:
        unknown -= amount(cycle['external_unknown_baseline'])
        if unknown < 0:
            raise ValueError('周期外部未知占位归属需核对。')
    released = sum((max(Decimal(0), amount(r['reserved_credits']) - amount(r['actual_credits'])) for r in rows), Decimal(0))
    additional = sum((max(Decimal(0), amount(r['actual_credits']) - amount(r['reserved_credits'])) for r in rows), Decimal(0))
    return {k: float(v) for k, v in dict(original_reserved=original,
        settled_actual=actual, settled_reserved=settled, pending_reserved=pending,
        released_credits=released, additional_credits=additional,
        effective_credits=pending + actual, external_unknown=unknown,
        total_occupied=pending + actual + unknown).items()}


def occupied(state):
    return summary(state)['total_occupied']


def call_cost(state, call_id, reserved):
    row = entries(state).get(call_id)
    if row and amount(row['reserved_credits']) != amount(reserved):
        raise ValueError('访客预留与结算不一致。')
    return amount(row['actual_credits'] if row else reserved)


def catalog(state, call_rows):
    """Merge original evidence, not totals divided by an assumed historical price.

    Missing terminal rows are recoverable from the pre-send registry. Older
    unidentifiable attempts stay occupied until their original evidence is found.
    """
    result = {}
    attempts = {}

    def add(target, role, reserved, attempt=None, **metadata):
        if role not in ('text', 'image') or not isinstance(target, str) or not ID.fullmatch(target):
            raise ValueError('原请求标识无效。')
        reserved = amount(reserved)
        if not reserved:
            raise ValueError('原请求预留无效。')
        row = result.setdefault(target, {'role': role, 'reserved': float(reserved)})
        if row['role'] != role or amount(row['reserved']) != reserved:
            raise ValueError('原请求的多份证据不一致。')
        if attempt is not None:
            if type(attempt) is not int or not 1 <= attempt <= state[role]:
                raise ValueError('原请求次数不符。')
            previous = attempts.setdefault((role, attempt), target)
            if previous != target or row.get('attempt', attempt) != attempt:
                raise ValueError('原请求存在重复或不明确的次数归属。')
            row['attempt'] = attempt
        for key, value in metadata.items():
            if value is not None:
                if key in row and row[key] != value:
                    raise ValueError('原请求归属不一致。')
                row[key] = value

    for name in ('runtime_calls', 'reservation_calls'):
        for target, row in state.get(name, {}).items():
            add(target, row['role'], row['reserved'], row.get('attempt'), task_id=row.get('task_id'),
                quota_cycle_id=row.get('quota_cycle_id'))
    for row in call_rows:
        target = row.get('call_id') or f"legacy:{row['role']}:{row['attempt']}"
        add(target, row['role'], row['reserved_credits'], row['attempt'], task_id=row.get('task_id'))
    if sum((amount(r['reserved']) for r in result.values()), Decimal(0)) > amount(state['reserved_credits']):
        raise ValueError('原请求明细超过累计预留。')
    for role in ('text', 'image'):
        if sum(r['role'] == role for r in result.values()) > state[role]:
            raise ValueError('原请求明细超过累计次数。')
    return result


def prepare(state, proposals, originals, not_sent=(), *, no_charge=()):
    """Pure, all-or-nothing preview. Actual use requires the original evidence hash.

    Invoice amounts may exceed their reservation; this increases occupancy.
    Failures, absent sent_at, absent log rows and token estimates are not proof
    of a zero charge or an unsent request.
    """
    summary(state)
    if not isinstance(proposals, list) or not 1 <= len(proposals) <= 200:
        raise ValueError('每次结算需1–200条已核对记录。')
    updated = copy.deepcopy(state)
    settlements = updated.setdefault('settlements', {})
    changed = []
    seen = set()
    at = datetime.now(timezone.utc).isoformat()
    for proposal in proposals:
        common = {'target', 'kind', 'evidence_sha256'}
        if not isinstance(proposal, dict):
            raise ValueError('结算输入无效。')
        kind = proposal.get('kind')
        required = common | ({'actual_credits', 'invoice_id'} if kind == 'invoice' else
                             {'confirmation_source'} if kind == 'confirmed_no_charge' else set())
        if set(proposal) != required or kind not in ('invoice', 'confirmed_not_sent', 'confirmed_no_charge'):
            raise ValueError('结算只接受账单、已确认未发送或明确未扣费证据。')
        target = proposal['target']
        if not isinstance(target, str) or target in seen or target not in originals:
            raise ValueError('请求缺少明确原预留或重复。')
        seen.add(target)
        original = originals[target]
        if kind == 'confirmed_not_sent' and target not in not_sent:
            raise ValueError('缺少发送前拒绝的明确证据；未知费用继续占位。')
        if kind == 'confirmed_no_charge' and (target not in no_charge
                or proposal['confirmation_source'] not in ('user', 'provider')):
            raise ValueError('缺少明确未扣费确认；失败状态不能作为零费用证据。')
        record = {**proposal, 'role': original['role'], 'reserved_credits': float(amount(original['reserved'])),
                  'actual_credits': float(amount(proposal['actual_credits'])) if kind == 'invoice' else 0.0,
                  'invoice_id': proposal.get('invoice_id')}
        if original.get('attempt') is not None:
            record['attempt'] = original['attempt']
        if original.get('quota_cycle_id') is not None:
            record['quota_cycle_id'] = original['quota_cycle_id']
        old = settlements.get(target)
        if old:
            if record != {k: v for k, v in old.items() if k not in ('at', 'digest')}:
                raise ValueError('同一请求已有不同结算，禁止覆盖历史证据。')
            continue
        record['at'] = at
        record['digest'] = digest(record)
        settlements[target] = record
        changed.append(target)
    after = summary(updated)
    return updated, {'before': summary(state), 'after': after, 'changed': changed,
                     'unchanged': len(proposals) - len(changed)}

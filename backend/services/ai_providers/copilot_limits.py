"""Opt-in limits for the local PC-001 baseline, inactive in upstream mode."""
import functools
import inspect
import json
import math
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

_lock = threading.RLock()
_diagnostic_lock = threading.Lock()
_network_worker_running = threading.Event()
_call_context = ContextVar('copilot_call_context', default=None)
_image_operation = ContextVar('copilot_image_operation', default=None)
# Assigned by trusted workers, never taken from a request's operation tag.
IMAGE_OPERATIONS = {'text_generate': (0, 2), 'template_generate': (1, 3),
                    'slide_edit': (1, 3), 'material_region_edit': (2, 2)}


@contextmanager
def image_operation_scope(operation, paths):
    if operation not in IMAGE_OPERATIONS:
        raise ValueError('不支持的图片操作。')
    token = _image_operation.set((operation, tuple(paths)))
    try:
        yield
    finally:
        _image_operation.reset(token)


def configured(role):
    prefix = role.upper()
    if os.getenv('PC_MODE') == 'preview':
        return os.getenv(f'PC_{prefix}_CONFIG_READY', os.getenv('PC_CONFIG_READY')) == '1'
    return all(os.getenv(f'{prefix}_{field}', '').strip()
               for field in ('API_KEY', 'API_BASE', 'MODEL_SOURCE', 'MODEL'))


def provider_supported(role):
    return (os.getenv(f'{role.upper()}_MODEL_SOURCE') == {'text': 'openai', 'image': 'gemini'}[role]
            and os.getenv(f'{role.upper()}_MODEL') == MODELS[role])


def budget_state():
    path = _root() / 'call-budget.json'
    try:
        state = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {
            'text': 0, 'image': 0, 'reserved_credits': 0.0, 'paused': False}
        if not isinstance(state.get('paused'), bool) or any(
                type(state.get(k)) not in (int, float) or not math.isfinite(state[k]) or state[k] < 0
                for k in ('text', 'image', 'reserved_credits')):
            raise ValueError()
        from services.ledger_settlement import summary
        summary(state)  # Invalid adjustments must fail closed, never free capacity.
        summary(state, lifetime=True)
        return state
    except (OSError, ValueError, TypeError, KeyError):
        return {'text': 0, 'image': 0, 'reserved_credits': 0, 'paused': True}


# Local CLI authorization only. Web requests cannot create or enlarge a batch.
BATCH_ID = 'pc0061-quality-region'
BATCH_LIMITS = {'credits': 15.0, 'text': 0, 'image': 2,
                'operations': {'template_generate': 1, 'slide_edit': 1}}
COPY_BATCH_ID = 'pc006-copy-allocation-validation'
COPY_BATCH_LIMITS = {'credits': 5.0, 'text': 0, 'image': 1,
                     'operations': {'template_generate': 1}}
REGION_BATCH_ID = 'pc006-region-15-to-18-validation'
REGION_BATCH_LIMITS = {'credits': 5.0, 'text': 0, 'image': 1,
                       'operations': {'slide_edit': 1}}
VISITOR_BATCH_ID = 'pc0081-visitor-image-trace'
CONTENT_BATCH_ID = 'pc0083-content-count-regression'
VISITOR_BATCH_IDS = frozenset((VISITOR_BATCH_ID, CONTENT_BATCH_ID))
BATCH_SPECS = {BATCH_ID: BATCH_LIMITS, COPY_BATCH_ID: COPY_BATCH_LIMITS,
               REGION_BATCH_ID: REGION_BATCH_LIMITS,
               VISITOR_BATCH_ID: {'credits': 5.0, 'text': 0, 'image': 1,
                                  'operations': {'template_generate': 1}},
               CONTENT_BATCH_ID: {'credits': 5.0, 'text': 0, 'image': 1,
                                  'operations': {'template_generate': 1}}}


def batch_operations(batch, batch_id=BATCH_ID):
    """An explicit local approval can reassign the remaining attempt, not add one."""
    if batch_id != BATCH_ID:
        if batch_id not in BATCH_SPECS or any(
                key in batch for key in ('template_retry_approval', 'final_template_retry_approval')):
            raise ValueError('本批没有重试授权。')
        return BATCH_SPECS[batch_id]['operations']
    approval = batch.get('template_retry_approval')
    if approval is None:
        if batch.get('final_template_retry_approval'):
            raise ValueError('缺少前次授权记录。')
        return BATCH_LIMITS['operations']
    if (not isinstance(approval, dict) or
            not re.fullmatch(r'[0-9a-f]{32}', approval.get('failed_call_id', '')) or
            approval.get('at_image_delta') != 1 or
            approval.get('operations') != {'template_generate': 2, 'slide_edit': 0}):
        raise ValueError('无效重试授权记录。')
    final = batch.get('final_template_retry_approval')
    if final is not None:
        if (not isinstance(final, dict) or
                not re.fullmatch(r'[0-9a-f]{32}', final.get('failed_call_id', '')) or
                final['failed_call_id'] == approval['failed_call_id'] or
                final.get('at_image_delta') != 2 or
                final.get('operations') != {'template_generate': 3, 'slide_edit': 0}):
            raise ValueError('无效最后一次重试授权。')
        return final['operations']
    return approval['operations']


def authorize_final_template_retry(failed_call_id):
    """One further human-approved attempt; original 15-credit ceiling is unchanged."""
    with _lock:
        state = budget_state()
        batch = state.get('batches', {}).get(BATCH_ID)
        if not batch or batch.get('limits') != BATCH_LIMITS:
            raise RuntimeError('缺少原批次。')
        operations = batch_operations(batch)
        previous = batch.get('final_template_retry_approval')
        if previous:
            if previous['failed_call_id'] != failed_call_id:
                raise RuntimeError('最后一次重试授权不符。')
            return
        if (operations != {'template_generate': 2, 'slide_edit': 0} or
                not re.fullmatch(r'[0-9a-f]{32}', failed_call_id or '') or
                not state['paused'] or state.get('last_call_id') != failed_call_id or
                state.get('last_result') != 'error' or
                state.get('last_operation') != 'template_generate' or
                state['image'] - batch['start']['image'] != 2 or
                state['text'] != batch['start']['text'] or
                batch['operations'] != {'template_generate': 2} or
                not 0 <= state['reserved_credits'] - batch['start']['reserved_credits'] <=
                BATCH_LIMITS['credits'] - RESERVATIONS['image']):
            raise RuntimeError('当前账目与最后一次模板重试授权不符。')
        batch['final_template_retry_approval'] = {
            'failed_call_id': failed_call_id, 'at_image_delta': 2,
            'operations': {'template_generate': 3, 'slide_edit': 0},
            'approved_at': datetime.now(timezone.utc).isoformat(),
            'previous_error_type': state.get('last_error_type'),
        }
        state['paused'] = False
        _save(_root() / 'call-budget.json', state)


def authorize_template_retry(failed_call_id):
    """CLI only, after explicit human approval; preserve counters and unknown costs."""
    with _lock:
        state = budget_state()
        batch = state.get('batches', {}).get(BATCH_ID)
        if not batch or batch.get('limits') != BATCH_LIMITS:
            raise RuntimeError('缺少原批次，禁止重建额度。')
        if batch.get('template_retry_approval'):
            batch_operations(batch)
            if batch['template_retry_approval']['failed_call_id'] != failed_call_id:
                raise RuntimeError('重试授权与原失败不符。')
            return  # Never clear a subsequent pause or refresh the allowance.
        if (not re.fullmatch(r'[0-9a-f]{32}', failed_call_id or '') or
                not state['paused'] or state.get('last_call_id') != failed_call_id or
                state.get('last_result') != 'error' or
                state.get('last_operation') != 'template_generate' or
                state['image'] - batch['start']['image'] != 1 or
                state['text'] != batch['start']['text'] or
                batch['operations'] != {'template_generate': 1} or
                not 0 <= state['reserved_credits'] - batch['start']['reserved_credits'] <=
                BATCH_LIMITS['credits'] - RESERVATIONS['image']):
            raise RuntimeError('失败状态与本次单次模板重试授权不符。')
        batch['template_retry_approval'] = {
            'failed_call_id': failed_call_id, 'at_image_delta': 1,
            'operations': {'template_generate': 2, 'slide_edit': 0},
            'approved_at': datetime.now(timezone.utc).isoformat(),
            'previous_error_type': state.get('last_error_type'),
        }
        state['paused'] = False
        _save(_root() / 'call-budget.json', state)


def initialize_batch(batch_id, visitor_id=None):
    if batch_id not in BATCH_SPECS:
        raise ValueError('未知验收批次。')
    limits = BATCH_SPECS[batch_id]
    if batch_id in VISITOR_BATCH_IDS and not visitor_id:
        raise RuntimeError('本批必须绑定明确访客。')
    with _lock:
        state = budget_state()
        if state['paused']:
            raise RuntimeError('账本无效或尚未确认失败，不能开始批次。')
        batches = state.setdefault('batches', {})
        if batch_id not in batches:
            batches[batch_id] = {'start': {k: state[k] for k in ('text', 'image', 'reserved_credits')},
                                 'limits': limits, 'operations': {},
                                 'created_at': datetime.now(timezone.utc).isoformat()}
            if batch_id in VISITOR_BATCH_IDS:
                batches[batch_id]['visitor_id'] = visitor_id
            _save(_root() / 'call-budget.json', state)
        if batch_id in VISITOR_BATCH_IDS and batches[batch_id].get('visitor_id') != visitor_id:
            raise RuntimeError('本批访客归属不能更换。')
        if batches[batch_id]['limits'] != limits:
            raise RuntimeError('批次额度与原授权不符，禁止覆盖。')
        batch_operations(batches[batch_id], batch_id)
        return batches[batch_id]


def budget_reason(state, role, operation=None):
    from services.ledger_settlement import occupied, entries, amount
    from services.budget_cycles import current
    if os.getenv('PC_RUN_POLICY'):
        return runtime_reason(state, role, operation)
    if current(state):
        return 'authorization_required'  # Old fixed batches cannot enter a new cycle.
    batch_id = os.getenv('PC_BATCH_ID')
    if batch_id:
        try:
            if batch_id not in BATCH_SPECS:
                raise ValueError()
            limits = BATCH_SPECS[batch_id]
            batch = state['batches'][batch_id]
            if batch_id in VISITOR_BATCH_IDS:
                from services.visitor_access import enabled, identity
                if not enabled() or not identity() or identity() != batch.get('visitor_id'):
                    return 'authorization_required'
            if batch['limits'] != limits:
                raise ValueError()
            operations = batch_operations(batch, batch_id)
            delta = {k: state[k] - batch['start'][k] for k in ('text', 'image', 'reserved_credits')}
            if any(not math.isfinite(v) or v < 0 for v in delta.values()):
                raise ValueError()
            # Only adjustments to attempts AFTER this fixed batch's start count.
            # Settling older requests cannot refresh a batch's allowance.
            batch_credits = amount(state['reserved_credits']) - amount(batch['start']['reserved_credits']) + sum(
                (amount(r['actual_credits']) - amount(r['reserved_credits'])
                 for r in entries(state).values()
                 if r.get('attempt') is not None and r['attempt'] > batch['start'][r['role']]), amount(0))
            counts = batch['operations']
            if (not isinstance(counts, dict) or set(counts) - set(limits['operations']) or
                    any(type(v) is not int or v < 0 for v in counts.values()) or
                    sum(counts.values()) != delta['image']):
                raise ValueError()
            if role == 'image' and operation is None:
                # Generic HTTP preflight asks whether any image operation remains.
                # The trusted worker still checks its concrete operation before send.
                reasons = [budget_reason(state, role, op) for op in operations]
                return None if None in reasons else 'budget_exhausted'
            if role != 'image' or operation not in limits['operations']:
                return 'authorization_required'
            if (delta['text'] != 0 or delta['image'] >= sum(operations.values()) or
                    batch_credits + amount(RESERVATIONS[role]) > amount(limits['credits']) or
                    batch['operations'].get(operation, 0) >= operations[operation]):
                return 'budget_exhausted'
            return None
        except (KeyError, TypeError, ValueError):
            return 'authorization_required'
    try:
        cap = float(os.getenv('PC_BUDGET_CREDITS', '0'))
    except ValueError:
        cap = 0
    if not 0 < cap <= 30:
        return 'authorization_required'
    if state[role] >= LIMITS[role] or occupied(state) + RESERVATIONS[role] > cap + 1e-8:
        return 'budget_exhausted'
    return None


def execution_reason(role, operation=None):
    if (_root()/'cycle-apply.pending.json').exists():
        return 'previous_failure'
    if os.getenv('PC_ADMIN_PAUSED') == '1':
        return 'admin_paused'
    if os.getenv('PC_ADMIN_APPLYING') == '1':
        return 'configuration_applying'
    if os.getenv('PC_RUNTIME_INCONSISTENT') == '1':
        return 'previous_failure'
    if os.getenv('PC_DRAINING') == '1':
        return 'authorization_required'
    if os.getenv('PC_MODE') != 'live':
        return 'preview'
    if role not in os.getenv('PC_MODEL_ROLES', 'text,image').split(','):
        return 'authorization_required'
    if _network_worker_running.is_set():
        return 'request_in_progress'
    if not configured(role):
        return 'configuration_missing'
    if not provider_supported(role):
        return 'provider_unsupported'
    allowed = set(os.getenv('PC_IMAGE_OPERATIONS', 'text_generate').split(','))
    if os.getenv('PC_REGION_EDIT_ENABLED') == '1':
        allowed.add('material_region_edit')
    if operation and operation not in allowed:
        return 'authorization_required'
    state = budget_state()
    from services.manual_balance import previous_failure_blocks
    if previous_failure_blocks(state):
        from services.image_retry import allowed as retry_allowed
        if not retry_allowed(state, role, operation):
            return 'previous_failure'
    from services import manual_balance as mb
    if mb.configured():
        from services.task_tracking import association
        task_id=association().get('task_id')
        if task_id:
            from models import db, TaskTrace
            trace=db.session.get(TaskTrace,task_id)
            if trace and any(c.get('status')=='failed_needs_review' for c in json.loads(trace.calls)):
                return 'task_failed'
    return budget_reason(state, role, operation)


def require_execution(role, operation=None):
    reason = execution_reason(role, operation)
    if reason:
        raise RuntimeError(f'模型执行受限（{reason}）；请核对配置、预算上限或未确认失败。')


def check_image_context(primary, extras):
    if not os.getenv('PC_MODE'):
        return
    paths = ([primary] if primary else []) + list(extras or [])
    context = _image_operation.get()
    require_execution('image', context[0] if context else 'text_generate')
    if paths and (not context or tuple(paths) != context[1]):
        raise ValueError('参考图未通过当前任务的归属与输入校验。')

MODELS = {"text": "deepseek-flash", "image": "nano-banana-2"}
# PC-005 continuation: user explicitly raised cumulative image attempts to five.
LIMITS = {"text": 6, "image": 5}
# PC-005, 2026-10-15: peak text pricing is 15/60 credits per 1M tokens.
# 32768 input bytes + 1024 tokens of framing, and 4096 output tokens
# conservatively fit within 0.8 credits. 2K image pricing is 4 credits/image.
# These are local estimates, not a provider billing guarantee.
RESERVATIONS = {"text": 0.8, "image": 4.0}


def _root():
    return Path(os.getenv('PC_RUNTIME_ROOT') or Path(__file__).resolve().parents[3] / '.copilot') / 'run'


def runtime_policy():
    from services.runtime_config import authorization
    policy = json.loads(os.environ['PC_RUN_POLICY'])
    authorization(policy['authorization'])
    return policy


def same_runtime_policy(left, right):
    """Compare older records using the defaults applied when loading their config."""
    from services.managed_resources import DEFAULTS as protection_defaults

    def normalized(policy):
        visitor = {'default_auto_pages': 5, **protection_defaults, **policy.get('visitor', {})}
        return {**policy, 'visitor': visitor}

    return normalized(left) == normalized(right)


def runtime_totals_match(state):
    try:
        start=state['runtime_start_totals']; calls=list(state['runtime_calls'].values())
        for role in ('text','image'):
            if state[role] != start[role]+sum(c['role']==role for c in calls):return False
        return abs(state['reserved_credits']-start['reserved_credits']-sum(c['reserved'] for c in calls)) < .00001
    except (KeyError,TypeError,ValueError):
        return False


def register_runtime_policy(state, policy, *, approve_change=False):
    """Shared by stopped-cycle migration and normal initialization; no sends.

    Policy history is appended; lifetime counters and existing calls are untouched.
    """
    from services.runtime_config import authorization
    authorization(policy['authorization'])
    auth=policy['authorization']; entries=state.setdefault('runtime_authorizations',{})
    if auth['id'] in entries and not same_runtime_policy(entries[auth['id']]['policy'],policy):
        if not approve_change:
            raise RuntimeError('运行授权条款已变化，必须经管理确认；计数不能刷新。')
        entry=entries[auth['id']]
        entry.setdefault('history',[]).append({'policy':entry['policy'],'replaced_at':datetime.now(timezone.utc).isoformat()})
        entry['policy']=policy
    if auth['id'] not in entries:
        entries[auth['id']]={'policy':policy,'registered_at':datetime.now(timezone.utc).isoformat()}


class RuntimeGenerationPaused(RuntimeError):
    """Reconciled history intentionally blocks sends, not an incomplete write."""


def initialize_runtime(app, *, approve_change=False):
    """CLI only, under the same budget lock; counters are absolute instance totals."""
    from models import db, VisitorCall
    with _lock, app.app_context():
        path = _root() / 'call-budget.json'
        marker = _root() / 'runtime-initialized.json'
        if not path.exists() and (marker.exists() or db.session.query(VisitorCall).count() or
                any((_root()/n).exists() for n in ('calls.jsonl','usage.jsonl'))):
            raise RuntimeError('账本缺失但存在历史证据，禁止重建额度。')
        state = budget_state()
        from services.manual_balance import configured, initialize as initialize_balance
        manual = configured()
        if (_root()/'cycle-apply.pending.json').exists():
            raise RuntimeError('额度周期迁移尚未核对完成，禁止模型调用。')
        # Detect a half-written visitor/global reservation before any future send.
        known = state.get('runtime_calls', {})
        for call_id, row in known.items():
            record = db.session.get(VisitorCall, call_id)
            if not record or record.role != row['role'] or record.reserved != row['reserved']:
                raise RuntimeError('访客与全局用量记录不一致。')
        if state.get('runtime_started_at'):
            if not runtime_totals_match(state):raise RuntimeError('常驻调用和累计账目不一致。')
            for record in db.session.query(VisitorCall).filter(VisitorCall.created >= state['runtime_started_at']):
                if record.call_id not in known: raise RuntimeError('存在未完成全局登记的访客占位。')
        from services.budget_cycles import current, counts
        cycle = current(state)
        if cycle:
            new = [row for call_id,row in known.items() if call_id not in cycle['prior_runtime_call_ids']]
            if any(row.get('quota_cycle_id') != cycle['id'] for row in new) or any(
                    counts(state)[role] != sum(row['role']==role for row in new) for role in ('text','image')):
                raise RuntimeError('当前额度周期调用归属不一致。')
        register_runtime_policy(state,runtime_policy(),approve_change=approve_change)
        # First reconcile all stores and policy. A deliberate pause must never
        # hide a real partial write, nor be classified as one at managed startup.
        if state['paused'] and not manual:
            from services.image_retry import allowed as retry_allowed
            if not retry_allowed(state, 'image', startup=True):
                raise RuntimeGenerationPaused('历史调用或单次许可保持暂停。')
        if manual: initialize_balance(state,manual)
        state.setdefault('runtime_started_at', time.time())
        state.setdefault('runtime_calls', {})
        state.setdefault('runtime_start_totals', {k:state[k] for k in ('text','image','reserved_credits')})
        _save(path, state)
        if not marker.exists(): _save(marker, {'initialized_at': state['runtime_started_at']})


def runtime_reason(state, role, operation=None):
    from services.runtime_config import authorized_now
    from services.ledger_settlement import occupied
    from services.budget_cycles import counts, credit_limit
    try:
        if not (_root()/'call-budget.json').exists(): return 'authorization_required'
        policy=runtime_policy(); auth=policy['authorization']
        if not authorized_now(auth):
            return 'authorization_expired'
        if not runtime_totals_match(state) or not same_runtime_policy(state['runtime_authorizations'][auth['id']]['policy'], policy):
            return 'authorization_required'
        if role not in auth['roles'] or (role=='image' and operation and operation not in auth['operations']):
            return 'authorization_required'
        unknown=state.get('external_unknown',0)
        if type(unknown) not in (int,float) or not math.isfinite(unknown) or unknown<0: return 'authorization_required'
        from services import manual_balance as mb
        cap, used = mb.limits(state,auth)
        if mb.counts(state,auth)[role] >= auth[role] or used+mb.unit(role,auth)>cap+1e-8:
            return 'budget_exhausted'
        return None
    except (KeyError, TypeError, ValueError):
        return 'authorization_required'


def _save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def record_usage(role, usage, actual_model=None, request_id=None):
    if os.getenv("PC_MODE") != "live":
        return
    data = usage.model_dump(mode="json", exclude_none=True) if usage is not None else {}
    from services import task_tracking as tracking
    tracking.usage(data, request_id)
    fields = {key: value for key, value in data.items() if "token" in key or key == "traffic_type"}
    row = {"time": datetime.now(timezone.utc).isoformat(), "role": role,
           "batch_id": os.getenv('PC_BATCH_ID'),
           "model": MODELS[role], "response_model": actual_model, "usage": fields, **tracking.association(),
           "request_id": request_id, "attempt": budget_state()[role], "actual_credits": None}
    with (_root() / "usage.jsonl").open("a", encoding="utf-8") as target:
        target.write(json.dumps(row, ensure_ascii=False) + "\n")


def record_diagnostic(event, **fields):
    """Append only explicit metadata; never prompts, headers or raw exceptions."""
    context = _call_context.get()
    if not context or os.getenv('PC_MODE') != 'live':
        return
    row = {key: context[key] for key in ('call_id', 'role', 'attempt', 'model')}
    row.update(time=datetime.now(timezone.utc).isoformat(), event=event,
               elapsed_seconds=round(time.perf_counter() - context['started'], 3))
    for key in ('http_status', 'content_characters', 'chunks', 'total_timeout_seconds'):
        value = fields.get(key)
        if type(value) in (int, float) and math.isfinite(value):
            row[key] = value
    for key in ('phase', 'error_type', 'finish_reason', 'provider_request_id'):
        value = fields.get(key)
        if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', value):
            if not value.lower().startswith(('sk-', 'tf-', 'bearer')) and value not in (
                    os.getenv('TEXT_API_KEY'), os.getenv('IMAGE_API_KEY')):
                row[key] = value
    if row.get('provider_request_id'):
        from services.task_tracking import update_call
        update_call(provider_request_id=row['provider_request_id'])
    with _diagnostic_lock:
        with (_root() / 'diagnostics.jsonl').open('a', encoding='utf-8') as target:
            target.write(json.dumps(row, ensure_ascii=False) + '\n')


def model_call(role):
    def decorate(function):
        def invoke(instance, args, kwargs):
            from services import task_tracking as tracking
            call_id = uuid.uuid4().hex
            from services.budget_cycles import assert_task, identity as cycle_identity
            if tracking.association().get('task_id'):
                assert_task(budget_state(), tracking.association()['task_id'])
            prompt = args[0] if args else kwargs.get("prompt", "")
            tracking.begin_call(role, instance, prompt, call_id)
            assert_task(budget_state(), tracking.association().get('task_id'))
            operation = None
            if role == 'image':
                context = _image_operation.get()
                operation = context[0] if context else 'text_generate'
            require_execution(role, operation)
            if instance.model != MODELS[role]:
                raise RuntimeError("模型与本轮固定配置不一致，已停止调用。")
            prompt = args[0] if args else kwargs.get("prompt", "")
            if len(prompt.encode("utf-8")) > 32768:
                raise RuntimeError("提示词超过本轮 32768 字节输入上限。")
            if role == "image":
                refs = args[1] if len(args) > 1 else kwargs.get("ref_images")
                resolution = args[3] if len(args) > 3 else kwargs.get("resolution", "2K")
                minimum, maximum = IMAGE_OPERATIONS[operation]
                count = len(refs or [])
                if (not minimum <= count <= maximum or resolution != '2K'
                        or (count and (not context or count != len(context[1])))):
                    raise RuntimeError('参考图须来自已校验的任务上下文，数量须符合操作限制且输出为 2K。')
            path = _root() / "call-budget.json"
            state = budget_state()
            from services.image_retry import allowed as retry_allowed, consume as consume_retry
            from services.manual_balance import previous_failure_blocks
            if (previous_failure_blocks(state) and not retry_allowed(state, role, operation)) or budget_reason(state, role, operation):
                raise RuntimeError("本轮调用上限已到或存在未确认失败；请先核对用量，不自动追加调用。")
            from services.generation_budget import claim, assert_capacity
            claim(state, role, operation, call_id, tracking.association().get('task_id'))
            assert_capacity(state, role)
            if previous_failure_blocks(state):
                consume_retry(state, role, operation, call_id, tracking.association().get('task_id'))
            elif state.get('paused'):
                state['pause_reason'] = 'provider_error'
            batch_id = os.getenv('PC_BATCH_ID')
            if batch_id:
                counts = state['batches'][batch_id]['operations']
                counts[operation] = counts.get(operation, 0) + 1
            state[role] += 1
            state['last_operation'] = operation or 'text'
            state["reserved_credits"] = round(state["reserved_credits"] + RESERVATIONS[role], 4)
            state['last_call_id'] = call_id
            if os.getenv('PC_RUN_POLICY'):
                state.setdefault('runtime_calls', {})[call_id] = {
                    'role': role, 'reserved': RESERVATIONS[role],
                    'authorization_id': runtime_policy()['authorization']['id']}
            # Independent of terminal logs: a crash can omit a finish row.
            # This only records the original reservation, not an invoice.
            state.setdefault('reservation_calls', {})[call_id] = {
                'role': role, 'reserved': RESERVATIONS[role], 'attempt': state[role],
                'task_id': tracking.association().get('task_id')}
            cycle_id = cycle_identity(state)
            if cycle_id:
                state['reservation_calls'][call_id]['quota_cycle_id'] = cycle_id
                state['runtime_calls'][call_id]['quota_cycle_id'] = cycle_id
            from services.visitor_access import reserve_call
            if os.getenv('PC_RUN_POLICY'):
                from services.visitor_access import enabled
                if not enabled():
                    raise RuntimeError('常驻执行缺少访客隔离，拒绝发送。')
            reserve_call(call_id, role, RESERVATIONS[role], tracking.association().get('task_id'), state)
            try:
                _save(path, state)  # Reserve before sending; a timeout may still cost money.
            except Exception:
                # SQLite may already contain the reservation. Stop the entire run;
                # initialize_runtime reconciles both stores on the next startup.
                os.environ['PC_RUNTIME_INCONSISTENT'] = '1'
                raise
            token = _call_context.set({'call_id': state['last_call_id'], 'role': role,
                'attempt': state[role], 'model': instance.model, 'started': time.perf_counter()})
            tracking.update_call(reserved_credits=RESERVATIONS[role],
                                 estimated_credits=RESERVATIONS[role],
                                 estimate_source='local_reservation_rule_not_invoice',
                                 operation=operation or 'text', cost_status='estimated')
            record_diagnostic('started')
            return path, state, token

        def finish(path, state, started, error=None):
            from services import task_tracking as tracking
            from services.budget_cycles import identity as cycle_identity
            state["last_elapsed_seconds"] = round(time.perf_counter() - started, 3)
            tracking.call_end(error, state["last_elapsed_seconds"])
            state['last_result'] = 'error' if error else 'response'
            if error:
                state["paused"] = True
                state['pause_reason'] = 'provider_error'
                state["last_error_type"] = type(error).__name__  # Never persist raw exceptions/keys.
            try:
                _save(path, state)
            except Exception:
                os.environ['PC_RUNTIME_INCONSISTENT'] = '1'
                raise
            record_diagnostic('error' if error else 'response_finished',
                              error_type=type(error).__name__ if error else None)
            row = {"time": datetime.now(timezone.utc).isoformat(), "role": role,
                   "batch_id": os.getenv('PC_BATCH_ID'),
                   "attempt": state[role], "model": MODELS[role],
                   "call_id": state['last_call_id'], **tracking.association(),
                   "operation": state['last_operation'],
                   "elapsed_seconds": state['last_elapsed_seconds'],
                   "reserved_credits": RESERVATIONS[role], "actual_credits": None,
                   "result": 'error' if error else 'response',
                   "error_type": type(error).__name__ if error else None}
            cycle_id = cycle_identity(state)
            if cycle_id:
                row['quota_cycle_id'] = cycle_id
            with (path.parent / 'calls.jsonl').open('a', encoding='utf8') as target:
                target.write(json.dumps(row, ensure_ascii=False) + '\n')

        @functools.wraps(function)
        def regular(instance, *args, **kwargs):
            if not os.getenv("PC_MODE"):
                return function(instance, *args, **kwargs)
            with _lock:
                try:
                    path, state, token = invoke(instance, args, kwargs)
                except Exception:
                    from services import task_tracking as tracking
                    tracking.update_call(status='local_rejected', failure_stage='configuration_or_budget',
                        ended_at=tracking.now(),
                        reserved_credits=RESERVATIONS[role] if budget_state().get('last_call_id') == tracking.association().get('call_id') else 0,
                        actual_credits=0,
                        actual_credits_source='confirmed_not_sent', cost_status='not_sent')
                    raise
                started = time.perf_counter()
                try:
                    result = function(instance, *args, **kwargs)
                    if role == 'image' and result is None:
                        raise RuntimeError('图片响应为空，费用尚未确认；暂停后续模型请求。')
                except BaseException as error:
                    finish(path, state, started, error)
                    raise
                else:
                    finish(path, state, started)
                    return result
                finally:
                    _call_context.reset(token)

        @functools.wraps(function)
        def streaming(instance, *args, **kwargs):
            if not os.getenv("PC_MODE"):
                yield from function(instance, *args, **kwargs)
                return
            with _lock:
                try:
                    path, state, token = invoke(instance, args, kwargs)
                except Exception:
                    from services import task_tracking as tracking
                    tracking.update_call(status='local_rejected', failure_stage='configuration_or_budget',
                        ended_at=tracking.now(),
                        reserved_credits=RESERVATIONS[role] if budget_state().get('last_call_id') == tracking.association().get('call_id') else 0,
                        actual_credits=0,
                        actual_credits_source='confirmed_not_sent', cost_status='not_sent')
                    raise
                started = time.perf_counter()
                try:
                    yield from function(instance, *args, **kwargs)
                except BaseException as error:
                    finish(path, state, started, error)
                    raise
                else:
                    finish(path, state, started)
                finally:
                    _call_context.reset(token)

        return streaming if inspect.isgeneratorfunction(function) else regular
    return decorate

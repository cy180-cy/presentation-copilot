"""Whole-workflow holds in the existing ledger; actual calls still use VisitorCall.

Single process only. Admission and conversion share copilot_limits._lock. A hold
is NOT a call or a charge. On send it is converted, never added a second time.
Interrupted/unknown work is not replayed or refunded on startup.
"""
import json
import os
import time
import uuid
from flask import request
from models import db, Page, TaskTrace
from services import visitor_access as va, task_tracking as tracking
from services.ai_providers import copilot_limits as b
from services.page_planning import PagePlanningError, PLAN_FIELDS, for_project
from services.ledger_settlement import occupied
from services import manual_balance as mb
from services.budget_cycles import identity as cycle_id, counts, credit_limit


def in_cycle(state, plan):
    current = cycle_id(state)
    return not current or plan.get('quota_cycle_id') == current


def held(state, owner=None):
    total = dict(text=0, image=0, credits=0.0)
    for plan in state.get('generation_plans', {}).values():
        if not in_cycle(state, plan):
            continue
        if plan['state'] != 'held' or owner is not None and plan['owner'] != owner:
            continue
        for role in ('text', 'image'):
            total[role] += plan['remaining'][role]
            total['credits'] += plan['remaining'][role] * plan['costs'][role]
    total['credits'] = round(total['credits'], 4)
    return total


def calculate_affordable_pages(state, cap, used, fixed_text, text_per_page):
    """Costs come exclusively from the Provider's reservation rules."""
    policy = b.runtime_policy(); auth = policy['authorization']
    global_hold, visitor_hold = held(state), held(state, va.identity())
    limit, spent = mb.limits(state, auth)
    credits = limit-spent-mb.task_hold(global_hold,auth)
    if not mb.configured(auth):
        credits = min(credits,cap['credits']-used['reserved']-visitor_hold['credits'])
    used_counts = mb.counts(state,auth)
    text = min(auth['text'] - used_counts['text'] - global_hold['text'], cap['text'] - used['text'] - visitor_hold['text'])
    image = min(auth['image'] - used_counts['image'] - global_hold['image'], cap['image'] - used['image'] - visitor_hold['image'])
    return max((n for n in range(1, cap['pages'] + 1)
                if fixed_text + text_per_page*n <= text and n <= image
                and (fixed_text + text_per_page*n)*mb.unit('text',auth) + n*mb.unit('image',auth) <= credits + 1e-8), default=0)


def _save(state):
    b._save(b._root() / 'call-budget.json', state)


def plan_generation(project, body):
    # Batch-only image approvals cannot fund a text-to-deck workflow.
    if not os.getenv('PC_RUN_POLICY') or not va.enabled() or not va.identity():
        raise PagePlanningError('当前入口未配置完整生成额度，未开始生成。')
    operations = [op for op in ('template_generate', 'text_generate') if not b.execution_reason('image', op)]
    if b.execution_reason('text') or not operations:
        raise PagePlanningError('当前额度或运行授权不足以完成1页，未开始生成。')
    # Freeze the server's visitor preference, never a client-supplied cost.
    from models import Settings
    split = request.path.endswith('/generate/from-description')
    description_mode = Settings.get_settings().description_generation_mode or 'streaming'
    if description_mode not in ('streaming', 'parallel'):
        raise PagePlanningError('当前描述入口无法可靠估算，未开始生成。')
    fixed, per_page = (2, 0) if split or description_mode == 'streaming' else (1, 1)
    with b._lock:
        state = b.budget_state()
        previous = active_plan(state, project.id)
        if previous:
            _, old = previous
            if not old['stages'] or any(s['status'] != 'completed' for s in old['stages'].values()):
                raise PagePlanningError('该作品已有未结束任务，请查看原任务；未再次预留额度。')
            # Explicit new idempotency key starts a new plan, never refunds sent calls.
            old['state'] = 'superseded'
            old['released_unsent'] = dict(old['remaining'])
            old['remaining'] = dict(text=0, image=0)
        affordable = calculate_affordable_pages(state, va.limits(), va.usage_summary(va.identity()), fixed, per_page)
        plan = for_project(project, body, va.limits(), affordable)
        plan_id = uuid.uuid4().hex
        n = plan['resolved_pages']
        state.setdefault('generation_plans', {})[plan_id] = {
            'quota_cycle_id': cycle_id(state),
            'owner': va.identity(), 'project_id': project.id, 'request_key': request.headers.get('Idempotency-Key'),
            'created': time.time(), 'state': 'held', 'plan': plan,
            'costs': dict(b.RESERVATIONS), 'remaining': {'text': fixed + per_page*n, 'image': n},
            'split': split, 'description_mode': description_mode,
            'operations': operations, 'stages': {}, 'page_ids': [], 'calls': {}}
        _save(state)
        return {**plan, 'budget_plan_id': plan_id, 'budget_stage': 'outline'}


def active_plan(state, project_id):
    return next(((key, p) for key, p in state.get('generation_plans', {}).items()
                 if in_cycle(state, p) and p['project_id'] == project_id and p['owner'] == va.identity() and p['state'] == 'held'), None)


def continuation(project_id, stage, page_id=None):
    with b._lock:
        found = active_plan(b.budget_state(), project_id)
        if not found:
            return None
        key, p = found
        if p['stages'].get('outline', {}).get('status') != 'completed':
            raise PagePlanningError('原规划任务尚未完整结束，请查看原任务；不会重复执行。')
        ids = [x.id for x in Page.query.filter_by(project_id=project_id).order_by(Page.order_index).all()]
        if ids != p['page_ids'] or page_id and page_id not in ids:
            raise PagePlanningError('页面数量或归属已变化，原预算计划不能用于这次操作。')
        if stage == 'description' and p['split']:
            return None  # A new explicit rewrite is a separate, normally charged operation.
        if stage == 'description' and (page_id or request.path.endswith('/stream') != (p['description_mode'] == 'streaming')):
            raise PagePlanningError('描述生成方式与已预留计划不同，请沿原流程完成；未发送请求。')
        selected = []
        if stage == 'image':
            selected = [page_id] if page_id else (request.get_json(silent=True) or {}).get('page_ids') or ids
            if not isinstance(selected, list) or any(not isinstance(x, str) or x not in ids for x in selected):
                raise PagePlanningError('选中的页面不属于本次生成计划。')
            selected = sorted(set(selected))
            if any(c['role'] == 'image' and c.get('page_id') in selected for c in p['calls'].values()):
                raise PagePlanningError('所选页面已使用本次计划，请查看原结果；不会重复请求。')
        stage_key = 'image:' + ','.join(selected) if selected else stage
        if stage_key in p['stages']:
            raise PagePlanningError('该生成阶段已经提交，请查看原任务，不会重复请求。')
        return {**p['plan'], 'budget_plan_id': key, 'budget_stage': stage_key, 'budget_page_ids': selected}


def attach_task(task_id, context):
    """Called after Task exists, before its first Provider call/worker enqueue."""
    if not context or not context.get('budget_plan_id'):
        return
    with b._lock:
        state = b.budget_state(); p = state['generation_plans'][context['budget_plan_id']]
        if not in_cycle(state, p) or p['state'] != 'held' or p['owner'] != va.identity():
            raise PagePlanningError('生成计划已结束或归属不符。')
        stage = context['budget_stage']
        old = p['stages'].get(stage)
        if old and old['task_id'] != task_id:
            raise PagePlanningError('原任务已提交，不能重复占位。')
        p['stages'].setdefault(stage, {'task_id': task_id, 'status': 'running'})
        _save(state)


def context_for_task(task_id=None):
    context = tracking.current_page_plan()
    if context and context.get('budget_plan_id'):
        return context
    task_id = task_id or tracking.association().get('task_id')
    row = db.session.get(TaskTrace, task_id) if task_id and va.enabled() else None
    return json.loads(row.snapshot) if row else {}


def claim(state, role, operation, call_id, task_id):
    """Modify the caller's locked ledger transaction, not a separate wallet."""
    context = context_for_task(task_id)
    key = context.get('budget_plan_id')
    if not key:
        return
    p = state['generation_plans'][key]; stage = context['budget_stage']
    entry = p['stages'][stage]
    if (not in_cycle(state, p) or p['state'] != 'held' or p['owner'] != va.identity() or entry['task_id'] != task_id
            or entry['status'] != 'running' or p['costs'] != b.RESERVATIONS):
        raise PagePlanningError('任务预算计划已变化，未发送请求。')
    if role == 'image' and operation not in p['operations']:
        raise PagePlanningError('模板操作已变化，不能使用原预算计划。')
    expected = 'image' if stage.startswith('image') else 'text'
    if role != expected or p['remaining'][role] < 1 or call_id in p['calls']:
        raise PagePlanningError('任务计划没有剩余调用，未发送请求。')
    maximum = (2 if p['split'] else 1) if stage == 'outline' else (1 if p['description_mode'] == 'streaming' else p['plan']['resolved_pages'])
    if role == 'text' and sum(c['task_id'] == task_id for c in p['calls'].values()) >= maximum:
        raise PagePlanningError('本阶段的文本调用已使用，未追加请求。')
    if role == 'image':
        page_id = tracking.association().get('page_id')
        if page_id not in context.get('budget_page_ids', []) or page_id not in p['page_ids'] or any(c.get('page_id') == page_id and c['role'] == 'image' for c in p['calls'].values()):
            raise PagePlanningError('该页已经使用本次计划，未重复调用。')
    p['remaining'][role] -= 1
    p['calls'][call_id] = {'role': role, 'task_id': task_id, 'page_id': tracking.association().get('page_id')}


def assert_capacity(state, role):
    if not os.getenv('PC_RUN_POLICY'):
        return
    a = b.runtime_policy()['authorization']; total = held(state)
    cap, spent = mb.limits(state,a)
    if (mb.counts(state,a)[role] + total[role] + 1 > a[role] or
            spent + mb.task_hold(total,a) + mb.unit(role,a) > cap + 1e-8):
        raise PagePlanningError('当前额度已用于其他生成任务，未发送请求。')


def finish_task(task_id, ok):
    with b._lock:
        state = b.budget_state()
        found = next(((p, name) for p in state.get('generation_plans', {}).values()
                      for name, stage in p['stages'].items() if stage['task_id'] == task_id), None)
        if not found:
            return
        p, stage = found
        if not in_cycle(state, p) or p['state'] != 'held':
            return
        p['stages'][stage]['status'] = 'completed' if ok else 'failed'
        if not ok:
            p['state'] = 'aborted'
            p['released_unsent'] = dict(p['remaining'])
            p['remaining'] = dict(text=0, image=0)
        elif stage == 'outline':
            p['page_ids'] = [x.id for x in Page.query.filter_by(project_id=p['project_id']).order_by(Page.order_index).all()]
            if p['split']:
                p['remaining']['text'] = 0
        elif stage == 'description':
            p['released_description_calls'] = p['remaining']['text']
            p['remaining']['text'] = 0  # streamed description used one, not N
        elif stage.startswith('image') and p['remaining']['image'] == 0:
            p['released_unsent_text'] = p['remaining']['text']
            p['remaining']['text'] = 0
        if not any(p['remaining'].values()):
            p['state'] = 'completed' if ok else 'aborted'
        _save(state)


def abandon_unsent(context):
    if not context or not context.get('budget_plan_id'):
        return
    with b._lock:
        state = b.budget_state(); p = state['generation_plans'][context['budget_plan_id']]
        if in_cycle(state, p) and p['state'] == 'held' and not p['stages'] and not p['calls']:
            p['released_unsent'] = dict(p['remaining'])
            p.update(state='aborted', remaining=dict(text=0, image=0))
            _save(state)


def public_plan(project_id, notice=False):
    """Only saved snapshots; never rebuild historical task values from settings."""
    from models import Task
    rows = db.session.query(TaskTrace).join(Task, Task.id == TaskTrace.task_id).filter(Task.project_id == project_id).order_by(Task.created_at.desc()).all()
    for row in rows:
        s = json.loads(row.snapshot)
        if 'resolved_pages' not in s:
            continue
        if notice:
            from services.page_planning import completion_message
            p = b.budget_state().get('generation_plans', {}).get(s.get('budget_plan_id'), {})
            return completion_message(s) if p.get('state') == 'completed' else None
        return {k: s.get(k) for k in PLAN_FIELDS}
    return None

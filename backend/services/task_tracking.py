"""Local task evidence. Never reserves budget, retries, or probes a provider."""
import copy
import functools
import hashlib
import json
import os
import re
import threading
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from flask import current_app, make_response, g, request
from sqlalchemy.orm import Session
from models import db, Task, Page, PageImageVersion, TaskTrace

PRESET_ID = 'copilot-personal-verified-pair'
CONFIG_VERSION = 'pc007-v1'
PROCESS_ID = str(uuid.uuid4())
_scope = ContextVar('task_trace_scope', default=None)
_write_lock = threading.RLock()


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def digest(value):
    return hashlib.sha256((value if isinstance(value, bytes) else dump(value).encode('utf8'))).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def redact(value):
    """Known secrets and credential-shaped text never enter persisted evidence."""
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if not isinstance(value, str):
        return value
    for name in ('TEXT_API_KEY', 'IMAGE_API_KEY'):
        secret = os.getenv(name)
        if secret:
            value = value.replace(secret, '[已隐藏]')
    value = re.sub(r'(?i)\b(?:sk-|tf-)[a-z0-9_-]{8,}|Bearer\s+\S+', '[已隐藏]', value)
    value = re.sub(r'(?i)(?:https?://)[^\s<>]+', '[链接已隐藏]', value)
    value = re.sub(r'(?im)(authorization|cookie|api[_ -]?key)\s*[:=]\s*[^\n]+', r'\1: [已隐藏]', value)
    return value


def freeze_service(service):
    """Freeze nonsecret settings and provider objects in memory, never env swaps."""
    frozen = copy.copy(service)
    for name in ('text_provider', 'image_provider'):
        provider = getattr(service, name, None)
        if provider is not None:
            setattr(frozen, name, copy.copy(provider))
    if getattr(frozen, 'text_provider', None) is not None:
        frozen.text_provider.total_timeout_seconds = float(os.getenv('PC_TEXT_TOTAL_TIMEOUT_SECONDS', '720'))
    return frozen


def preset(service, role):
    provider = getattr(service, role + '_provider')
    runtime = json.loads(os.getenv('PC_RUN_POLICY') or '{}')
    return {'preset_id': PRESET_ID, 'config_version': CONFIG_VERSION,
            'runtime_authorization_id':runtime.get('authorization',{}).get('id'),
            'runtime_config_revision':os.getenv('PC_ADMIN_CONFIG_REVISION') or None,
            'model': provider.model, 'provider': type(provider).__name__,
            'model_identity': 'submitted_alias_not_verified_weights',
            'max_output_tokens': getattr(provider, 'max_output_tokens', None),
            'timeout_seconds': getattr(provider, 'request_timeout_seconds', None)}


def create_trace(task_id, snapshot):
    if db.session.get(TaskTrace, task_id):
        raise ValueError('任务快照已经固定，不能重新入队覆盖。')
    from services.budget_cycles import identity
    from services.ai_providers.copilot_limits import budget_state
    snapshot = copy.deepcopy(snapshot)
    cycle_id = identity(budget_state())
    if cycle_id:
        snapshot['quota_cycle_id'] = cycle_id
    db.session.add(TaskTrace(task_id=task_id, snapshot=dump(redact(snapshot)),
                             calls='[]', outputs='[]', process_id=PROCESS_ID))
    db.session.commit()


@contextmanager
def scope(task_id, page_id=None, app=None):
    state = {'task_id': task_id, 'page_id': page_id, 'call_id': None,
             'app': app or current_app._get_current_object()}
    token = _scope.set(state)
    try:
        yield state
    finally:
        _scope.reset(token)


def association():
    state = _scope.get()
    return {k: state.get(k) for k in ('task_id', 'page_id', 'call_id')} if state else {}


def current_page_plan():
    return copy.deepcopy((_scope.get() or {}).get('page_plan'))


def update_call(**fields):
    state = _scope.get()
    if not state or not state.get('call_id'):
        return
    with _write_lock, state['app'].app_context(), Session(db.engine) as session:
        trace = session.get(TaskTrace, state['task_id'])
        if trace is None:
            raise RuntimeError('任务追踪记录不存在，停止发送。')
        calls = json.loads(trace.calls)
        call = next((c for c in calls if c['call_id'] == state['call_id']), None)
        if call is None:
            call = {'call_id': state['call_id'], 'page_id': state.get('page_id'),
                    'started_at': now(), 'ended_at': None, 'sent_at': None,
                    'status': 'not_sent', 'usage': None, 'provider_request_id': None,
                    'elapsed_seconds': None, 'reserved_credits': None,
                    'estimated_credits': None, 'actual_credits': None,
                    'actual_credits_source': None, 'cost_status': 'unknown'}
            cycle_id = json.loads(trace.snapshot).get('quota_cycle_id')
            if cycle_id:
                call['quota_cycle_id'] = cycle_id
            calls.append(call)
        call.update(redact(fields))
        trace.calls = dump(calls)
        session.commit()


def mark_save_failure():
    """Do not replace a more specific provider/guard failure with a save error."""
    state = _scope.get()
    if not state or not state.get('call_id'):
        return
    with state['app'].app_context():
        row = db.session.get(TaskTrace, state['task_id'])
        call = next(c for c in json.loads(row.calls) if c['call_id'] == state['call_id'])
        if call['status'] == 'response_received':
            update_call(failure_stage='input_or_save', result_saved=False)


def begin_call(role, instance, prompt, call_id):
    state = _scope.get()
    if not state:
        return
    if not state.get('task_id'):
        # Synchronous/SSE text work has no queue: create immediately before call.
        task = Task(project_id=state['project_id'], task_type=state['operation'], status='PROCESSING')
        db.session.add(task)
        db.session.commit()
        state['task_id'] = task.id
        from services.page_planning import PROMPT_VERSION
        create_trace(task.id, {'operation': state['operation'], 'preset_id': PRESET_ID,
            'config_version': CONFIG_VERSION, 'model': instance.model,
            'provider': type(instance).__name__, 'model_identity': 'submitted_alias_not_verified_weights',
            'max_output_tokens': getattr(instance, 'max_output_tokens', None),
            'pages': [{'page_id': state.get('page_id'), 'prompt_hash': digest(prompt),
                       'input_text': prompt}], 'prompt_version': PROMPT_VERSION if state.get('page_plan') else 'pc007-v1',
            **(state.get('page_plan') or {})})
        from services.generation_budget import attach_task
        attach_task(task.id, state.get('page_plan'))
    state['call_id'] = call_id
    update_call(role=role, model=instance.model)


def sent():
    update_call(sent_at=now(), status='sent', cost_status='pending_verification')


def usage(data, request_id=None):
    safe = {k: v for k, v in data.items() if 'token' in k and type(v) in (int, float)}
    rid = request_id if isinstance(request_id, str) and re.fullmatch(r'[\w.:-]{1,160}', request_id) else None
    update_call(usage=safe or None, provider_request_id=rid)


def call_end(error=None, elapsed=None):
    update_call(cost_status='pending_verification', status='failed_needs_review' if error else 'response_received',
                ended_at=now(), elapsed_seconds=elapsed,
                failure_stage=('provider_timeout' if error and 'timeout' in type(error).__name__.lower()
                               else 'provider_or_response' if error else None))


def link_output(page, version=None):
    """Same transaction as Page/ImageVersion save. No second fee record."""
    state = _scope.get()
    if not state:
        return
    trace = db.session.get(TaskTrace, state['task_id'])
    db.session.refresh(trace)
    outputs = json.loads(trace.outputs)
    outputs.append({'page_id': page.id, 'version_id': version.id if version else None,
                    'version_number': version.version_number if version else None,
                    'call_id': state['call_id']})
    trace.outputs = dump(outputs)
    calls = json.loads(trace.calls)
    for call in calls:
        if call['call_id'] == state['call_id']:
            call.update(status='response_saved', failure_stage=None)
    trace.calls = dump(calls)


def route(operation):
    """Track existing sync and SSE text endpoints without changing their payloads."""
    def decorate(fn):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            if not os.getenv('PC_MODE'):
                return fn(*args, **kwargs)
            state = {'task_id': None, 'call_id': None, 'project_id': kwargs.get('project_id'),
                     'page_id': kwargs.get('page_id'), 'operation': operation,
                     'app': current_app._get_current_object()}
            from services.page_planning import PLANNING_OPERATIONS, preflight
            if operation in PLANNING_OPERATIONS:
                plan = getattr(g, 'pc_page_plan', None)
                if plan is None:
                    plan, rejection = preflight(state['project_id'], request.get_json(silent=True) or {})
                    if rejection is not None:
                        return rejection
                state['page_plan'] = plan
            elif getattr(g, 'pc_page_plan', None):
                state['page_plan'] = g.pc_page_plan
            def finish(ok):
                if not state['task_id']:
                    from services.generation_budget import abandon_unsent
                    abandon_unsent(state.get('page_plan'))
                    return
                with state['app'].app_context():
                    task = db.session.get(Task, state['task_id'])
                    task.status = 'COMPLETED' if ok else 'FAILED'
                    task.completed_at = datetime.utcnow()
                    task.error_message = None if ok else '文本结果未完整保存，请核对已保存内容。'
                    db.session.commit()
                    if ok:
                        update_call(status='response_saved', failure_stage=None)
                    else:
                        mark_save_failure()
                    from services.generation_budget import finish_task
                    finish_task(task.id, ok)
            token = _scope.set(state)
            try:
                response = make_response(fn(*args, **kwargs))
                if response.mimetype == 'text/event-stream':
                    original = response.response
                    def stream():
                        inner = _scope.set(state)
                        done = False
                        failed = False
                        try:
                            for chunk in original:
                                text = chunk.decode() if isinstance(chunk, bytes) else chunk
                                done |= 'event: done' in text
                                failed |= 'event: error' in text
                                yield chunk
                        finally:
                            finish(done and not failed)
                            _scope.reset(inner)
                    response.response = stream()
                else:
                    finish(response.status_code < 400)
                return response
            finally:
                _scope.reset(token)
        return wrapped
    return decorate

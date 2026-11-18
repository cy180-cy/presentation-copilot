"""Single-process visitor boundary for managed mode, independent of model authorization.

ORM ownership applies equally to route lists, references and worker reads. New records
receive grants in the same transaction; historical rows never receive implicit grants.
The provider lock serializes billing; the worker condition also excludes heavy
foreground file processing. Neither counter is a visitor's monetary balance.
"""
from services import manual_balance as mb
import functools
import hashlib
import json
import os
import re
import secrets
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import unquote, urlsplit
from flask import abort, current_app, g, has_app_context, has_request_context, request
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from sqlalchemy import event, select
from sqlalchemy.orm import Session, with_loader_criteria
from models import (db, Project, Page, Task, PageImageVersion, Material, ReferenceFile,
                    UserTemplate, UserStyleTemplate, ProjectTemplateAsset, TaskTrace)
from models.visitor import Visitor, ResourceOwner, VisitorRequest, VisitorCall

IDENTITY = ContextVar('pc_visitor', default=None)
ADMISSION = ContextVar('pc_admission', default=None)
LOCK = threading.RLock()
ADMISSION_LOCK = threading.RLock()
RESOURCE_LOCK = threading.RLock()
WORKERS = threading.Condition()
WORKERS_IN_USE = 0
FOREGROUND_IN_USE = False
HEAVY_CONTEXT = ContextVar('pc_heavy_work', default=False)
MODELS = (Project, Page, Task, PageImageVersion, Material, ReferenceFile,
          UserTemplate, UserStyleTemplate, ProjectTemplateAsset, TaskTrace)
COOKIE = 'pc_visitor'
SAFE = {'GET', 'HEAD', 'OPTIONS'}
DEFAULTS = dict(window_seconds=86400, text=0, image=0, credits=0,
                active_tasks=1, service_concurrency=1, queue=2, pages=10, default_auto_pages=5,
                input_bytes=131072, upload_bytes=20971520, storage_bytes=209715200,
                projects=20, new_visitors_per_hour=60)
from services.managed_resources import DEFAULTS as PROTECTION_DEFAULTS
DEFAULTS.update(PROTECTION_DEFAULTS)


@contextmanager
def heavy_work():
    """Use the existing worker condition for atomic, fail-fast foreground work."""
    global FOREGROUND_IN_USE
    if not enabled() or HEAVY_CONTEXT.get():
        yield
        return
    from services.managed_resources import ResourceLimit
    with WORKERS:
        if WORKERS_IN_USE or FOREGROUND_IN_USE:
            raise ResourceLimit('当前正在生成或处理文件，请稍后再试。')
        FOREGROUND_IN_USE = True
    mark = HEAVY_CONTEXT.set(True)
    try:
        yield
    finally:
        HEAVY_CONTEXT.reset(mark)
        with WORKERS:
            FOREGROUND_IN_USE = False
            WORKERS.notify_all()


def heavy_export(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        from services.managed_resources import ResourceLimit
        try:
            with heavy_work():
                return view(*args, **kwargs)
        except ResourceLimit as exc:
            return error('RESOURCE_BUSY', str(exc), 429)
    return wrapped


def enabled():
    return has_app_context() and current_app.config.get('PC_VISITOR_ENABLED', False)


def identity():
    return IDENTITY.get() or (getattr(g, 'visitor_id', None) if has_request_context() else None)


def limits():
    result = {**DEFAULTS, **current_app.config.get('PC_VISITOR_LIMITS', {})}
    trial_owner = current_app.config.get('PC_TRIAL_VISITOR')
    if trial_owner:
        # Temporary local CLI grant, never a client-controlled identity or public quota.
        result.update(text=0, image=1 if identity() == trial_owner else 0,
                      credits=5 if identity() == trial_owner else 0)
    return result


def bind_trial(app, visitor_id):
    """Validate the normal session identity and durably bind the fixed batch once."""
    from services.ai_providers.copilot_limits import initialize_batch, VISITOR_BATCH_IDS
    batch_id = os.getenv('PC_BATCH_ID')
    if batch_id not in VISITOR_BATCH_IDS:
        raise RuntimeError('本次未配置固定访客验收批次。')
    with app.app_context():
        visitor = db.session.get(Visitor, visitor_id)
        if not visitor or visitor.expires <= time.time():
            raise RuntimeError('验收访客不存在或已过期。')
        initialize_batch(batch_id, visitor_id)
    app.config['PC_TRIAL_VISITOR'] = visitor_id


@contextmanager
def scope(visitor_id, admission=None):
    token = IDENTITY.set(visitor_id)
    mark = ADMISSION.set(admission)
    try:
        yield
    finally:
        ADMISSION.reset(mark)
        IDENTITY.reset(token)


def capture_worker(function):
    visitor_id, admission = identity(), ADMISSION.get()
    capacity = limits()['service_concurrency'] if enabled() else None
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        global WORKERS_IN_USE
        if capacity is not None:
            with WORKERS:
                while WORKERS_IN_USE >= capacity or FOREGROUND_IN_USE:
                    WORKERS.wait()
                WORKERS_IN_USE += 1
        mark = HEAVY_CONTEXT.set(capacity is not None)
        try:
            with scope(visitor_id, admission):
                return function(*args, **kwargs)
        finally:
            HEAVY_CONTEXT.reset(mark)
            if capacity is not None:
                with WORKERS:
                    WORKERS_IN_USE -= 1
                    WORKERS.notify_all()
    return wrapped


def check_task_admission(task_id):
    """Includes free PDF/import tasks, not only model request admissions.

    The new Task is already persisted. Conservatively reject if simultaneous
    admissions filled the bound; no worker starts until this check completes.
    """
    if not enabled():
        return
    if not identity() or db.session.get(Task, task_id) is None:
        raise ValueError('访客任务归属缺失。')
    cap = limits()
    with LOCK, db.engine.connect() as conn:
        rows = conn.execute(select(Task.id, ResourceOwner.visitor_id).outerjoin(
            ResourceOwner, (ResourceOwner.kind == Task.__tablename__) &
            (ResourceOwner.resource_id == Task.id)).where(
            Task.status.in_(['PENDING', 'PROCESSING', 'RUNNING', 'QUEUED']))).all()
    if len(rows) > cap['service_concurrency'] + cap['queue'] or sum(
            row.visitor_id == identity() for row in rows) > cap['active_tasks']:
        raise ValueError('当前任务较多，请等待已提交任务完成。')


def primary(model):
    return model.task_id if model is TaskTrace else model.id


@event.listens_for(Session, 'do_orm_execute')
def filter_owned(state):
    if not enabled() or not identity():
        return
    for model in MODELS:
        ids = select(ResourceOwner.resource_id).where(
            ResourceOwner.kind == model.__tablename__, ResourceOwner.visitor_id == identity())
        state.statement = state.statement.options(
            with_loader_criteria(model, primary(model).in_(ids), include_aliases=True))


@event.listens_for(Session, 'before_flush')
def validate_parents(session, *_):
    if not enabled() or not identity():
        return
    for obj in session.new | session.dirty | session.deleted:
        if not isinstance(obj, MODELS):
            continue
        for attr, model in [('project_id', Project), ('page_id', Page), ('task_id', Task)]:
            value = getattr(obj, attr, None)
            if value and not any(isinstance(p, model) and p.id == value for p in session.new):
                if session.get(model, value) is None:
                    raise ValueError('资源归属不匹配。')
        if isinstance(obj, Page) and obj in session.new:
            existing = session.query(Page).filter_by(project_id=obj.project_id).count()
            pending = sum(isinstance(p, Page) and p.project_id == obj.project_id for p in session.new)
            if existing + pending > limits()['pages']:
                raise ValueError('作品页数超过限制。')


@event.listens_for(Session, 'after_flush')
def assign_owned(session, *_):
    if not enabled() or not identity():
        return
    connection = session.connection()
    for obj in session.new:
        if isinstance(obj, MODELS):
            rid = obj.task_id if isinstance(obj, TaskTrace) else obj.id
            connection.execute(ResourceOwner.__table__.insert().values(
                kind=obj.__tablename__, resource_id=rid, visitor_id=identity()))
            if isinstance(obj, Task) and ADMISSION.get():
                connection.execute(VisitorRequest.__table__.update().where(
                    VisitorRequest.visitor_id == identity(), VisitorRequest.key == ADMISSION.get()
                ).values(task_id=obj.id))


def preferences(settings):
    """Detached settings view; never mutate shared Settings for visitor preferences."""
    if not enabled() or not identity():
        return settings
    from models import Settings
    result = Settings(**{c.name: getattr(settings, c.name) for c in Settings.__table__.columns})
    visitor = db.session.get(Visitor, identity())
    for key, value in json.loads(visitor.preferences).items():
        setattr(result, key, value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
    return result


def save_preferences(values):
    visitor = db.session.get(Visitor, identity())
    visitor.preferences = json.dumps({**json.loads(visitor.preferences), **values}, ensure_ascii=False)
    db.session.commit()


def error(code, message, status):
    return {'success': False, 'error': {'code': code, 'message': message}}, status


def allowed_file(url):
    """Authorize logical file owner, then reject traversal and symlinks at every level."""
    parsed = urlsplit(url)
    if parsed.netloc or parsed.scheme:
        abort(403)
    parts = unquote(parsed.path).strip('/').split('/')
    if len(parts) < 3 or parts[0] != 'files' or any(p in ('', '.', '..') or '\\' in p for p in parts):
        abort(404)
    root = Path(current_app.config['UPLOAD_FOLDER']).resolve()
    tail = parts[1:]
    if tail[0] == 'user-templates':
        if db.session.get(UserTemplate, tail[1]) is None:
            abort(404)
    elif tail[0] == 'materials':
        if not Material.query.filter_by(filename=tail[-1]).first():
            abort(404)
    elif tail[0] == 'mineru':
        if not ReferenceFile.query.filter_by(mineru_batch_id=tail[1]).first():
            abort(404)
        tail[0] = 'mineru_files'
    else:
        if db.session.get(Project, tail[0]) is None:
            abort(404)
        if len(tail) > 2 and tail[1] == 'template-assets':
            asset = db.session.get(ProjectTemplateAsset, tail[2])
            if not asset or asset.project_id != tail[0]:
                abort(404)
    candidate = root.joinpath(*tail)
    if not candidate.resolve().is_relative_to(root):
        abort(404)
    while candidate != root:
        if candidate.is_symlink() or (candidate.exists() and candidate.resolve() != candidate.absolute()):
            abort(404)
        candidate = candidate.parent
    return root.joinpath(*tail)


def inspect_inputs(data, project_id=None, page_id=None):
    mapping = {'project_id': Project, 'page_id': Page, 'page_ids': Page, 'pages_order': Page,
               'task_id': Task, 'version_id': PageImageVersion, 'material_id': Material,
               'material_ids': Material, 'file_id': ReferenceFile, 'reference_file_ids': ReferenceFile,
               'asset_id': ProjectTemplateAsset, 'template_asset_id': ProjectTemplateAsset,
               'template_id': UserTemplate, 'style_template_id': UserStyleTemplate}
    if isinstance(data, dict):
        for key, value in data.items():
            if key in {'owner_id', 'visitor_id', 'role', 'api_key', 'api_base', 'base_url', 'model'}:
                abort(400)
            if key in {'text_model', 'image_model', 'model_id', 'api_base_url', 'provider',
                       'max_tokens', 'max_output_tokens', 'temperature', 'thinking_budget',
                       'budget_credits', 'trial_limits'}:
                abort(400)
            if key == 'preset_id' and value != 'copilot-personal-verified-pair':
                abort(400)
            if key in mapping and value:
                if key == 'project_id' and value in ('all', 'none'):
                    continue  # Existing list filter sentinels; ORM ownership still applies.
                model = mapping[key]
                if key in ('page_ids', 'pages_order', 'material_ids', 'reference_file_ids') and isinstance(value, str):
                    try:
                        value = json.loads(value) if value.startswith('[') else value.split(',')
                    except ValueError:
                        abort(400)
                for rid in value if isinstance(value, list) else [value]:
                    obj = db.session.get(model, str(rid))
                    if not obj or (project_id and hasattr(obj, 'project_id') and obj.project_id not in (None, project_id)):
                        abort(404)
                    if page_id and hasattr(obj, 'page_id') and obj.page_id != page_id:
                        abort(404)
            inspect_inputs(value, project_id, page_id)
    elif isinstance(data, list):
        for value in data:
            inspect_inputs(value, project_id, page_id)
    elif isinstance(data, str):
        # Covers reference arrays and embedded Markdown, not just dedicated URL fields.
        for url in re.findall(r'(?:https?://[^\s"\)<>]+)?/files/[^\s"\)<>]+', data):
            allowed_file(url)


def usage_summary(visitor_id):
    start = time.time() - limits()['window_seconds']
    with db.engine.connect() as conn:
        rows = conn.execute(select(VisitorCall.call_id, VisitorCall.role, VisitorCall.reserved).where(
            VisitorCall.visitor_id == visitor_id, VisitorCall.created >= start)).all()
    from services.ai_providers.copilot_limits import budget_state
    from services.ledger_settlement import call_cost
    state = budget_state()
    from services.budget_cycles import owns_call
    rows = [r for r in rows if owns_call(state, r.call_id)]
    rows = mb.quota_rows(state, rows)
    return dict(text=sum(r.role == 'text' for r in rows), image=sum(r.role == 'image' for r in rows),
                reserved=float(sum(call_cost(state, r.call_id, r.reserved) for r in rows)))


def reserve_call(call_id, role, amount, task_id, budget_state=None):
    """Called under the original global budget lock, BEFORE its file reservation/send.

    SQLite first, global ledger second. Any uncertain second write leaves an occupied
    visitor reservation, never sends and never automatically refunds or replays.
    """
    if not enabled():
        return
    owner = identity()
    if not owner or not task_id:
        raise RuntimeError('访客执行上下文缺失，未发送请求。')
    cap = limits()
    with LOCK, db.engine.begin() as conn:
        conn.exec_driver_sql('BEGIN IMMEDIATE')
        existing = conn.execute(select(VisitorCall.call_id).where(VisitorCall.call_id == call_id)).first()
        if existing:
            raise RuntimeError('此调用已登记，禁止重复发送。')
        rows = conn.execute(select(VisitorCall.call_id, VisitorCall.role, VisitorCall.reserved).where(
            VisitorCall.visitor_id == owner, VisitorCall.created >= time.time()-cap['window_seconds'])).all()
        from services.generation_budget import held
        from services.ledger_settlement import call_cost
        from services.budget_cycles import owns_call
        rows = [r for r in rows if owns_call(budget_state or {}, r.call_id)]
        rows = mb.quota_rows(budget_state or {}, rows)
        occupied = held(budget_state or {}, owner)
        used = float(sum(call_cost(budget_state or {}, r.call_id, r.reserved) for r in rows))
        if role not in ('text', 'image') or sum(r.role == role for r in rows)+occupied[role] >= cap.get(role, 0) or (not mb.configured() and used+amount+occupied['credits'] > cap['credits']+1e-8):
            raise RuntimeError('试用额度未配置或已用完，未发送请求。')
        conn.execute(VisitorCall.__table__.insert().values(call_id=call_id, visitor_id=owner,
            task_id=task_id, role=role, created=time.time(), reserved=amount, state='reserved_unknown'))


def model_route():
    path = request.path
    return request.method == 'POST' and any(x in path for x in ('/generate/', '/edit/image', '/refine/', '/materials/process'))


def input_revision(project_id):
    project = db.session.get(Project, project_id)
    if project is None:
        abort(404)
    # Only business input and selected source image, not progress/status timestamps.
    fields = ['creation_type', 'idea_prompt', 'outline_text', 'description_text', 'extra_requirements',
              'outline_requirements', 'description_requirements', 'template_style', 'template_image_path',
              'template_mode', 'image_aspect_ratio']
    data = [{k:getattr(project,k,None) for k in fields}]
    data.append(json.loads(db.session.get(Visitor, identity()).preferences))
    data += [[p.id,p.order_index,p.outline_content,p.description_content,p.generated_image_path,
              p.template_asset_id,p.template_style_text] for p in Page.query.filter_by(project_id=project_id).order_by(Page.id).all()]
    data += [[a.id,a.image_path,str(a.updated_at)] for a in ProjectTemplateAsset.query.filter_by(project_id=project_id).order_by(ProjectTemplateAsset.id).all()]
    return hashlib.sha256(json.dumps(data,sort_keys=True,default=str).encode()).hexdigest()


def idempotent(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not enabled() or not model_route():
            return view(*args, **kwargs)
        key = request.headers.get('Idempotency-Key', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', key):
            return error('IDEMPOTENCY_REQUIRED', '请刷新页面后重新提交。', 400)
        files = []
        for name, upload in request.files.items(multi=True):
            files.append([name, hashlib.sha256(upload.read()).hexdigest()]); upload.seek(0)
        body = request.get_json(silent=True) if request.is_json else request.form.to_dict(flat=False)
        fingerprint = hashlib.sha256(json.dumps([request.path, body, files,
            request.headers.get('X-PC-Input-Version'), 'copilot-personal-verified-pair'], sort_keys=True).encode()).hexdigest()
        from services.ai_providers import copilot_limits as budget
        # Always global budget -> admission -> visitor lock, matching admin apply.
        with budget._lock, ADMISSION_LOCK:
            if os.getenv('PC_ADMIN_APPLYING') == '1' or os.getenv('PC_ADMIN_PAUSED') == '1':
                return error('GENERATION_PAUSED', '生成已暂停或配置正在更新，免费操作仍可用。', 503)
            old = db.session.get(VisitorRequest, (identity(), key))
            if old:
                if old.fingerprint != fingerprint:
                    return error('IDEMPOTENCY_CONFLICT', '同一请求标识对应了不同输入，请确认操作。', 409)
                if old.response:
                    return json.loads(old.response), old.status_code
                return {'success': True, 'data': {'task_id': old.task_id, 'status': old.state,
                    'message': '该请求已登记，请查看原任务；不会重复执行。'}}, 202
            from services.managed_resources import request_allowed
            if not request_allowed():
                return error('REQUEST_RATE_LIMIT', '请求较频繁，请稍后再试。', 429)
            with WORKERS:
                if FOREGROUND_IN_USE and not HEAVY_CONTEXT.get():
                    return error('RESOURCE_BUSY', '当前正在生成或处理文件，请稍后再试。', 429)
            project_id = (request.view_args or {}).get('project_id')
            if request.headers.get('X-PC-Input-Version') != input_revision(project_id):
                return error('INPUT_CHANGED', '页面输入已变化，请刷新后确认再提交。', 409)
            cap = limits()
            # Core SQL deliberately counts all visitors; no resource payload is exposed.
            with db.engine.connect() as conn:
                active = conn.execute(select(VisitorRequest.visitor_id).outerjoin(
                    Task, Task.id == VisitorRequest.task_id).where(
                    (Task.status.in_(['PENDING', 'PROCESSING', 'RUNNING', 'QUEUED'])) |
                    ((VisitorRequest.task_id.is_(None)) & (VisitorRequest.state.in_(['pending', 'accepted', 'unknown']))))).all()
            if len(active) >= cap['service_concurrency']+cap['queue'] or sum(r.visitor_id == identity() for r in active) >= cap['active_tasks']:
                return error('QUEUE_FULL', '当前任务较多，请等待已提交任务完成。', 429)
            project_id = (request.view_args or {}).get('project_id')
            count = Page.query.filter_by(project_id=project_id).count() if project_id else 1
            count = max(1, count if request.path.endswith(('/images', '/descriptions', '/descriptions/stream')) else 1)
            role = 'image' if request.path.endswith(('/image', '/images', '/process')) else 'text'
            from services.ai_providers.copilot_limits import RESERVATIONS
            used = usage_summary(identity())
            if request.path.endswith(('/generate/outline', '/generate/outline/stream', '/generate/from-description')):
                from services.page_planning import preflight
                g.pc_page_plan, rejection = preflight(project_id, body or {})
                if rejection is not None:
                    return rejection
            else:
                from services.generation_budget import continuation, held
                from services.page_planning import PagePlanningError
                try:
                    stage = 'description' if '/generate/description' in request.path else 'image' if '/generate/image' in request.path else None
                    g.pc_page_plan = continuation(project_id, stage, (request.view_args or {}).get('page_id')) if stage else None
                except PagePlanningError as exc:
                    return error('PAGE_PLAN', str(exc), 409)
                occupied = held(budget.budget_state(), identity())
                if not g.pc_page_plan and (count > cap['pages'] or used[role]+occupied[role]+count > cap[role] or (not mb.configured() and used['reserved']+occupied['credits']+count*RESERVATIONS[role] > cap['credits'])):
                    return error('TRIAL_EXHAUSTED', '当前额度不足，已有作品仍可使用。', 429)
            db.session.add(VisitorRequest(visitor_id=identity(), key=key, fingerprint=fingerprint, created=time.time()))
            db.session.commit()
        token = ADMISSION.set(key)
        try:
            response = current_app.make_response(view(*args, **kwargs))
            record = db.session.get(VisitorRequest, (identity(), key))
            if response.is_json:
                payload = response.get_json()
                record.response = json.dumps(payload, ensure_ascii=False)
                record.status_code = response.status_code
            record.state = 'accepted' if response.status_code < 400 else 'rejected'
            db.session.commit()
            if response.is_streamed and not response.is_json:
                original = response.response
                app, who = current_app._get_current_object(), identity()
                def stream():
                    completed = False
                    try:
                        with scope(who, key):
                            yield from original
                        completed = True
                    finally:
                        with app.app_context():
                            row = db.session.get(VisitorRequest, (who, key))
                            row.state = 'finished' if completed else 'unknown'
                            db.session.commit()
                response.response = stream()
            return response
        except Exception:
            db.session.rollback()
            # Keep pending/unknown: replay cannot create another charged attempt.
            raise
        finally:
            ADMISSION.reset(token)
    return wrapped


def install(app, root):
    root = Path(root)
    secret_file = root / 'visitor-signing.key'
    root.mkdir(parents=True, exist_ok=True)
    if not secret_file.exists():
        try:
            with secret_file.open('x', encoding='ascii') as file:
                file.write(secrets.token_hex(32))
        except FileExistsError:
            pass
    secret = secret_file.read_text(encoding='ascii').strip()
    if len(secret) < 64:
        raise RuntimeError('访客签名配置无效。')
    serializer = URLSafeTimedSerializer(secret, salt='pc-visitor-v1')
    app.config.update(PC_VISITOR_ENABLED=True, MAX_CONTENT_LENGTH=DEFAULTS['upload_bytes'])
    config = root / 'visitor-limits.json'
    if os.getenv('PC_RUN_VISITOR_LIMITS'):
        app.config['PC_VISITOR_LIMITS'] = json.loads(os.environ['PC_RUN_VISITOR_LIMITS'])
    elif config.exists():
        values = json.loads(config.read_text(encoding='utf8'))
        if set(values)-set(DEFAULTS) or any(type(v) is not int or v < 0 for v in values.values()):
            raise RuntimeError('访客限额配置无效。')
        if values.get('service_concurrency', 1) != 1 or values.get('window_seconds', 1) < 1:
            raise RuntimeError('当前仅支持单进程、串行模型执行。')
        app.config['PC_VISITOR_LIMITS'] = values
    cap = {**DEFAULTS, **app.config.get('PC_VISITOR_LIMITS', {})}
    if (any(type(cap[k]) is not int or cap[k] < 1 for k in PROTECTION_DEFAULTS) or
            cap['service_storage_bytes'] < cap['storage_bytes'] or cap['storage_free_bytes'] < 2**30):
        raise RuntimeError('请求频率或存储保护参数无效。')
    app.config['MAX_CONTENT_LENGTH'] = min(DEFAULTS['upload_bytes'],
        app.config.get('PC_VISITOR_LIMITS', {}).get('upload_bytes', DEFAULTS['upload_bytes']))
    ttl = 30 * 86400

    def auth():
        if not enabled():
            return  # Isolated pre-visitor regression fixtures; launcher always enables.
        if not request.path.startswith(('/api/', '/files/', '/uploads/')):
            return
        if request.path.startswith('/api/admin/'):
            return  # Separate server-side admin sessions and CSRF; never promote a visitor.
        if request.path.startswith(('/uploads/', '/api/openai-oauth', '/api/settings')):
            return error('PRIVATE', '此入口不对访客开放。', 403)
        if request.path == '/api/visitor/restart' and request.method == 'POST':
            if request.headers.get('Origin') != request.host_url.rstrip('/') or request.headers.get('X-PC-Restart') != '1':
                return error('CSRF', '请从首页重新开始。', 403)
            return
        if request.method not in SAFE:
            if request.headers.get('Origin') != request.host_url.rstrip('/') or request.headers.get('Sec-Fetch-Site') == 'cross-site':
                return error('CSRF', '请从当前页面提交操作。', 403)
        raw = request.cookies.get(COOKIE)
        if raw:
            try:
                payload = serializer.loads(raw, max_age=ttl)
                visitor = db.session.get(Visitor, payload['id'])
                if not visitor or visitor.expires < time.time():
                    raise SignatureExpired('expired')
            except (BadSignature, KeyError, TypeError):
                return error('SESSION_EXPIRED', '访客会话已失效，请返回首页重新开始。旧作品不会自动转给新会话。', 401)
        else:
            if request.path != '/api/visitor/session' or request.method != 'GET':
                return error('SESSION_REQUIRED', '请返回首页建立访客会话。', 401)
            with LOCK:
                count = Visitor.query.filter(Visitor.created > time.time()-3600).count()
                if count >= limits()['new_visitors_per_hour']:
                    return error('SESSION_RATE_LIMIT', '当前新访客较多，请稍后再试。', 429)
                visitor = Visitor(id=secrets.token_hex(24), created=time.time(), expires=time.time()+ttl)
                db.session.add(visitor); db.session.commit()
                payload = {'id': visitor.id, 'csrf': secrets.token_urlsafe(32)}
                g.visitor_cookie = serializer.dumps(payload)
        g.visitor_id, g.csrf = visitor.id, payload['csrf']
        g.visitor_context_token = IDENTITY.set(visitor.id)
        if (request.method not in SAFE or '/export/' in request.path) and not secrets.compare_digest(request.headers.get('X-PC-CSRF', ''), g.csrf):
            return error('CSRF', '页面凭据已失效，请刷新后重试。', 403)
        if request.files:
            from services.managed_resources import ResourceLimit, new_files_transaction
            slot = heavy_work()
            try:
                slot.__enter__()
            except ResourceLimit as exc:
                return error('RESOURCE_BUSY', str(exc), 429)
            g.pc_upload_slot = slot
            transaction = new_files_transaction()
            transaction.__enter__()
            g.pc_upload_transaction = transaction
        if request.method not in SAFE and not model_route():
            RESOURCE_LOCK.acquire()
            g.visitor_resource_lock = True
        args = request.view_args or {}
        if request.blueprint == 'user_style_templates' and args.get('template_id'):
            if not db.session.get(UserStyleTemplate, args['template_id']):
                abort(404)
            args = {k:v for k,v in args.items() if k != 'template_id'}
        inspect_inputs(args, args.get('project_id'), args.get('page_id'))
        inspect_inputs(request.args.to_dict(), args.get('project_id'), args.get('page_id'))
        if request.path.startswith('/files/'):
            allowed_file(request.path)
        if request.method not in SAFE:
            content = request.get_json(silent=True) if request.is_json else request.form.to_dict()
            inspect_inputs(content, args.get('project_id'), args.get('page_id'))
            if request.is_json and len(request.get_data()) > limits()['input_bytes']:
                return error('INPUT_LIMIT', '输入内容过大。', 413)
            if not request.is_json and sum(len(k.encode()) + len(v.encode()) for k, v in request.form.items(multi=True)) > limits()['input_bytes']:
                return error('INPUT_LIMIT', '输入内容过大。', 413)
            if request.path == '/api/projects' and request.method == 'POST' and Project.query.count() >= limits()['projects']:
                return error('PROJECT_LIMIT', '作品数量已达上限，已有作品仍可查看和下载。', 429)
            pages = (content or {}).get('pages', []) if isinstance(content, dict) else []
            if len(pages) > limits()['pages']:
                return error('PAGE_LIMIT', '单次页面数量超过限制。', 400)
        if model_route():
            if os.getenv('PC_MODE') != 'live':
                return error('PREVIEW', '当前为预览模式，可手动编辑和查看作品；模型执行关闭。', 403)
            core = r'/api/projects/[\w-]+/(?:generate/(?:outline(?:/stream)?|from-description|descriptions(?:/stream)?|images)|pages/[\w-]+/(?:generate/(?:description|image)|edit/image))'
            if not re.fullmatch(core, request.path):
                return error('INTEGRATION_REQUIRED', '这项模型操作尚未接入访客任务记录，原功能保留，当前暂不执行。', 403)
            if not limits()['credits'] and not mb.configured():
                return error('TRIAL_UNCONFIGURED', '试用额度尚未配置，已有作品仍可查看和下载。', 403)
        # Caption endpoints can perform hidden model work; return only existing caption.
        if request.path.startswith('/api/materials/') and request.path.endswith(('/caption', '/by-url')):
            material = Material.query.filter_by(url=request.args.get('url')).first() if request.path.endswith('/by-url') else db.session.get(Material, args.get('material_id'))
            if material and material.caption is None:
                return error('CAPTION_UNAVAILABLE', '该素材尚无说明，当前不启用自动识图。', 403)

    app.before_request_funcs.setdefault(None, []).insert(0, auth)
    @app.teardown_request
    def clear_identity(_error):
        transaction = getattr(g, 'pc_upload_transaction', None)
        g.pc_upload_transaction = None
        try:
            if transaction is not None:
                failure = _error or RuntimeError('Upload response did not finish')
                transaction.__exit__(type(failure), failure, failure.__traceback__)
        finally:
            slot = getattr(g, 'pc_upload_slot', None)
            if slot is not None:
                slot.__exit__(None, None, None)
                g.pc_upload_slot = None
            if getattr(g, 'visitor_resource_lock', False):
                RESOURCE_LOCK.release()
                g.visitor_resource_lock = False
            token = getattr(g, 'visitor_context_token', None)
            if token is not None:
                IDENTITY.reset(token)
                g.visitor_context_token = None
    for endpoint, view in list(app.view_functions.items()):
        app.view_functions[endpoint] = idempotent(view)

    @app.after_request
    def response_boundary(response):
        if getattr(g, 'pc_resource_error', None):
            response = app.make_response(error('STORAGE_LIMIT', g.pc_resource_error, 413))
        transaction = getattr(g, 'pc_upload_transaction', None)
        if transaction is not None:
            failure = RuntimeError('Upload was rejected') if response.status_code >= 400 else None
            g.pc_upload_transaction = None
            transaction.__exit__(type(failure) if failure else None, failure, None)
        if getattr(g, 'visitor_cookie', None):
            response.set_cookie(COOKIE, g.visitor_cookie, max_age=ttl, httponly=True,
                secure=request.is_secure or bool(app.config.get('PC_PUBLIC_HTTPS', False)), samesite='Lax')
        if request.path.startswith(('/api/', '/files/')):
            response.headers['Cache-Control'] = 'private, no-store'
            response.headers['X-Content-Type-Options'] = 'nosniff'
            if request.path.startswith('/files/') or '/example/files/' in request.path or request.path.endswith('/download'):
                response.headers['Content-Security-Policy'] = "sandbox; default-src 'none'; style-src 'unsafe-inline'"
            response.headers.pop('Access-Control-Allow-Origin', None)
            response.headers.pop('Access-Control-Allow-Credentials', None)
            if response.status_code >= 500:
                response = app.make_response(error('TASK_ERROR', '操作未完成，请查看任务状态；不要重复提交未知结果的请求。', response.status_code))
                response.headers['Cache-Control'] = 'private, no-store'
                response.headers['X-Content-Type-Options'] = 'nosniff'
            elif response.is_json:
                def sanitize(value, key=''):
                    if isinstance(value, dict):
                        return {k: sanitize(v, k) for k, v in value.items()}
                    if isinstance(value, list):
                        return [sanitize(v) for v in value]
                    if isinstance(value, str):
                        if value and key in ('error_message', 'analysis_error', 'traceback') and value != '存储空间不足，请减少文件或联系管理员；已有作品仍可下载。':
                            return '操作未完成，请查看任务状态。'
                        if re.search(r'[A-Za-z]:[\\/]|Traceback|Bearer\s|sk-[A-Za-z0-9]', value):
                            return '内部诊断已保留，请联系开发者核对。'
                    return value
                response.set_data(app.json.dumps(sanitize(response.get_json())))
        return response

    @app.get('/api/visitor/session')
    def visitor_session():
        return {'success': True, 'data': {'csrf': g.csrf, 'expires_at': db.session.get(Visitor, identity()).expires,
            'mode': os.getenv('PC_MODE'), 'trial_configured': bool(limits()['text'] or limits()['image']) if mb.configured() else bool(limits()['credits']),
            'usage': usage_summary(identity()), 'message': '预览模式：模型执行关闭。试用额度尚未开放。' if os.getenv('PC_MODE') == 'preview' else '试用限制与服务总预算同时生效。'}}

    @app.get('/api/projects/<project_id>/input-revision')
    def revision(project_id):
        return {'success': True, 'data': {'revision': input_revision(project_id)}}

    @app.post('/api/visitor/restart')
    def restart_session():
        response = app.json.response({'success': True})
        response.delete_cookie(COOKIE, httponly=True, samesite='Lax')
        return response

    @app.get('/api/visitor/example')
    def example():
        folder = root.parent / 'examples' / 'water-saving'
        names = ['page1.png', 'page2.png', 'example.pptx', 'example.pdf']
        if not all((folder / name).is_file() for name in names):
            return {'success': True, 'data': {'unavailable': True}}
        base = '/api/visitor/example/files/'
        return {'success': True, 'data': {'images': [base+n for n in names[:2]],
            'pptx': base+names[2], 'pdf': base+names[3], 'source': 'pregenerated_demo'}}

    @app.get('/api/visitor/example/files/<filename>')
    def example_file(filename):
        from flask import send_file
        folder = root.parent / 'examples' / 'water-saving'
        if filename not in ('page1.png', 'page2.png', 'example.pptx', 'example.pdf'):
            abort(404)
        path = folder / filename
        if path.is_symlink() or not path.resolve().is_relative_to(folder.resolve()) or not path.is_file():
            abort(404)
        return send_file(path, as_attachment=path.suffix in ('.pdf', '.pptx'))

    @app.get('/api/reference-files/<file_id>/download')
    def reference_download(file_id):
        from flask import send_file
        item = db.session.get(ReferenceFile, file_id)
        if item is None:
            abort(404)
        base = Path(app.config['UPLOAD_FOLDER']).resolve()
        path = base / item.file_path
        if not path.resolve().is_relative_to(base) or path.is_symlink() or not path.is_file():
            abort(404)
        for parent in path.parents:
            if parent == base:
                break
            if parent.is_symlink():
                abort(404)
        return send_file(path, as_attachment=True, download_name=item.filename)

"""Read-only, project-scoped evidence. Never constructs a provider."""
import json
from flask import Blueprint, request
from models import db, Project, Page, Task, PageImageVersion, TaskTrace
from services.task_tracking import PROCESS_ID, redact
from services.page_planning import PLAN_FIELDS

task_trace_bp = Blueprint('task_trace', __name__)


def detail(task, trace, page_id=None, version_id=None):
    base = {'task_id': task.id, 'operation': task.task_type, 'status': task.status,
            'created_at': task.created_at.isoformat(), 'tracked': trace is not None,
            'snapshot': None, 'calls': [], 'outputs': [],
            'message': '历史任务未记录；未接入追踪的任务也不会反填当前配置。'}
    if not trace:
        return base
    snap = json.loads(trace.snapshot)
    # Public projection deliberately excludes prompts, raw inputs and file paths.
    base['snapshot'] = {k: snap.get(k) for k in ('preset_id', 'config_version', 'model',
        'provider', 'model_identity', 'max_output_tokens', 'timeout_seconds', 'language', 'prompt_version',
        'runtime_authorization_id', 'runtime_config_revision', *PLAN_FIELDS)}
    base['snapshot']['pages'] = [{k: p.get(k) for k in ('page_id', 'source_version_id',
        'source_version', 'has_source_image', 'input_hash', 'prompt_hash', 'template_id', 'references',
        'aspect_ratio', 'resolution', 'edit_instruction')} for p in snap.get('pages', [])
        if not page_id or p.get('page_id') == page_id]
    outputs = json.loads(trace.outputs)
    base['outputs'] = [o for o in outputs if (not page_id or o['page_id'] == page_id)
                       and (not version_id or o.get('version_id') == version_id)]
    call_ids = {o['call_id'] for o in base['outputs']}
    base['calls'] = [c for c in json.loads(trace.calls) if
                     (not page_id or c.get('page_id') in (None, page_id)) and
                     (not version_id or c['call_id'] in call_ids)]
    if task.status in ('PENDING', 'PROCESSING', 'RUNNING') and trace.process_id != PROCESS_ID:
        base['status'] = 'INTERRUPTED'
        base['message'] = '任务所属进程已结束，结果与费用需核对；不会自动重发。'
    else:
        base['message'] = '费用预留和估算不是平台实扣。历史图片切换不代表描述与大纲完整回滚。'
    return redact(base)


@task_trace_bp.get('/api/projects/<project_id>/task-details')
def task_details(project_id):
    if not db.session.get(Project, project_id):
        return {'error': {'message': '作品不存在。'}}, 404
    page_id, version_id = request.args.get('page_id'), request.args.get('version_id')
    task_id = request.args.get('task_id')
    requested_task = db.session.get(Task, task_id) if task_id else None
    if task_id and (not requested_task or requested_task.project_id != project_id):
        return {'error': {'message': '任务不存在。'}}, 404
    page = db.session.get(Page, page_id) if page_id else None
    if page_id and (not page or page.project_id != project_id):
        return {'error': {'message': '页面不存在。'}}, 404
    if version_id:
        version = db.session.get(PageImageVersion, version_id)
        if not page or not version or version.page_id != page.id:
            return {'error': {'message': '图片版本不存在。'}}, 404
        candidates = db.session.query(Task, TaskTrace).join(TaskTrace, Task.id == TaskTrace.task_id).filter(Task.project_id == project_id).all()
        for task, trace in candidates:
            if any(o.get('version_id') == version_id and o['page_id'] == page_id for o in json.loads(trace.outputs)):
                if task_id and task_id != task.id:
                    return {'error': {'message': '版本与任务不匹配。'}}, 404
                return {'success': True, 'data': detail(task, trace, page_id, version_id)}
        if task_id:
            return {'error': {'message': '没有可核实的版本与任务关联。'}}, 404
        return {'success': True, 'data': {'tracked': False, 'message': '历史任务未记录',
            'snapshot': None, 'calls': [], 'outputs': [], 'version_number': version.version_number}}
    if task_id:
        task = db.session.get(Task, task_id)
        if not task or task.project_id != project_id:
            return {'error': {'message': '任务不存在。'}}, 404
        trace = db.session.get(TaskTrace, task_id)
        if page_id and (not trace or not any(p.get('page_id') == page_id for p in json.loads(trace.snapshot).get('pages', []))):
            return {'error': {'message': '任务与页面不匹配。'}}, 404
        return {'success': True, 'data': detail(task, trace, page_id)}
    tasks = Task.query.filter_by(project_id=project_id).order_by(Task.created_at.desc()).limit(30).all()
    return {'success': True, 'data': [{'task_id': t.id, 'operation': t.task_type,
        'status': t.status, 'created_at': t.created_at.isoformat(),
        'tracked': db.session.get(TaskTrace, t.id) is not None} for t in tasks]}

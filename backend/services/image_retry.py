"""One explicitly approved image retry; existing unknown fees stay occupied."""
import copy
import hashlib
import json
from datetime import datetime, timezone

from services.budget_cycles import checksum, current


def input_hash(description, outline):
    return hashlib.sha256(json.dumps([description, outline], ensure_ascii=False,
                                    sort_keys=True).encode('utf-8')).hexdigest()


def close_acceptance(state, manifest, task_ids):
    """Explicit offline closeout; no budget, history or administrator changes."""
    import re
    from services.manual_balance import initialize
    if (set(manifest) != {'id','reason','balance_id','retire_permit_ids'} or
            not re.fullmatch(r'[A-Za-z0-9_-]{8,80}', manifest.get('id','')) or
            not isinstance(manifest.get('reason'),str) or not manifest['reason'].strip()):
        raise ValueError('日常使用收尾清单无效。')
    old=state.get('acceptance_closeouts',{}).get(manifest['id'])
    if old:
        if old['manifest']!=manifest or old['digest']!=checksum({k:v for k,v in old.items() if k!='digest'}):
            raise ValueError('同一收尾记录不能覆盖。')
        return copy.deepcopy(state), False
    if state.get('acceptance_closeouts'):
        raise ValueError('已有验收收尾记录，不能重复解除后续暂停。')
    balance_id=manifest['balance_id']
    if balance_id!=state.get('active_balance_checkpoint'):
        raise ValueError('余额基准已变化。')
    updated=copy.deepcopy(state)
    initialize(updated, state['balance_checkpoints'][balance_id]['settings'])
    active=state.get('active_image_retry');permits=state.get('image_retry_permits',{})
    if not state.get('paused') or active not in permits or permits[active].get('state')!='consumed':
        raise ValueError('不是已消费许可遗留的验收暂停。')
    ids=manifest['retire_permit_ids']
    if not isinstance(ids,list) or len(ids)!=len(set(ids)) or sorted(ids)!=sorted(k for k,p in permits.items() if p.get('state')=='ready'):
        raise ValueError('必须明确列出全部未使用的旧许可。')
    archived={k:copy.deepcopy(permits[k]) for k in set(ids+[active])}
    for p in archived.values():
        if p.get('digest')!=checksum({k:v for k,v in p.items() if k!='digest'}):
            raise ValueError('旧许可证据校验失败。')
    at=datetime.now(timezone.utc).isoformat()
    for key in ids:
        p=updated['image_retry_permits'][key]
        p.update(state='revoked',revoked_by_closeout=manifest['id'],revoked_at=at)
        p['digest']=checksum({k:v for k,v in p.items() if k!='digest'})
    record={'manifest':copy.deepcopy(manifest),'created_at':at,'previous_active':active,
            'previous_paused':state['paused'],'previous_pause_reason':state.get('pause_reason'),
            'archived_permits':archived,'closed_task_ids':sorted(set(task_ids))}
    record['digest']=checksum(record)
    updated.setdefault('acceptance_closeouts',{})[manifest['id']]=record
    updated.pop('active_image_retry',None)
    updated['paused']=False
    updated['pause_reason']='acceptance_closed_for_daily_use'
    return updated, True


def ready(state):
    """Malformed, stale, consumed or replayed grants never open execution."""
    try:
        key = state.get('active_image_retry')
        p = state.get('image_retry_permits', {}).get(key)
        if p and p.get('purpose') == 'historical_quality':
            group = p['group_ids']
            members = [state['image_retry_permits'][k] for k in group]
            p = next((row for row in members if row['state'] == 'ready'), None)
            if not p:
                return None
        if not p or p['digest'] != checksum({k: v for k, v in p.items() if k != 'digest'}):
            return None
        if (p['version'] != 1 or p['state'] != 'ready' or p['max_calls'] != 1
                or p['operation'] != 'template_generate' or p['resolution'] != '2K'
                or p['used_call_id'] is not None or p['used_task_id'] is not None):
            return None
        cycle = current(state)
        if not cycle or p['cycle_id'] != cycle['id'] or p['credit_limit'] != cycle['credit_limit']:
            return None
        if p.get('purpose') == 'historical_quality':
            index = group.index(p['id'])
            if (len(group) > 4 or len(set(group)) != len(group) or not state['paused']
                    or not p.get('source_version_id') or not p.get('source_image_sha256')
                    or not p.get('prompt_version') or not p.get('approval_sha256')
                    or {k: state[k] for k in p['baseline']} != p['baseline']):
                return None
            for prior in members[:index]:
                if (prior['state'] != 'consumed' or not prior.get('used_call_id')
                        or prior['digest'] != checksum({k:v for k,v in prior.items() if k != 'digest'})):
                    return None
            anchor = members[index-1]['used_call_id'] if index else p['anchor_call_id']
            if state.get('last_call_id') != anchor or state.get('last_result') != 'response':
                return None
            return p
        quality = p.get('purpose') == 'quality_recheck'
        if p.get('purpose') not in (None, 'quality_recheck'):
            return None
        origin = p['source_call_id'] if quality else p['failed_call_id']
        origin_task = p['source_task_id'] if quality else p['failed_task_id']
        if quality and (not p.get('source_version_id') or not p.get('source_image_sha256')
                        or not p.get('prompt_version')):
            return None
        if ({k: state[k] for k in p['baseline']} != p['baseline']
                or state.get('last_call_id') != origin
                or state.get('last_result') != ('response' if quality else 'error') or not state['paused']
                or (not quality and origin in state.get('settlements', {}))):
            return None
        original = state['reservation_calls'][origin]
        if (original['task_id'] != origin_task or original['role'] != 'image'
                or original['quota_cycle_id'] != cycle['id']
                or original['reserved'] != p['reserved_credits']):
            return None
        return p
    except (ValueError, TypeError, KeyError):
        return None


def allowed(state, role, operation=None, *, startup=False):
    p = ready(state)
    if not p or role != 'image' or operation not in (None, p['operation']):
        return False
    from services.ai_providers.copilot_limits import MODELS, RESERVATIONS
    if p['model'] != MODELS['image'] or p['reserved_credits'] != RESERVATIONS['image']:
        return False
    if startup:
        return True  # Startup still reconciles every visitor, count and budget.
    from flask import has_request_context, request
    from models import db, Task, TaskTrace, Page, PageImageVersion
    from services import task_tracking as trace
    from services.visitor_access import identity
    if p.get('purpose') == 'historical_quality':
        index = p['group_ids'].index(p['id'])
        if index:
            previous = state['image_retry_permits'][p['group_ids'][index-1]]
            task = db.session.get(Task, previous['used_task_id'])
            if not task or task.status != 'COMPLETED':
                return False
    if p.get('purpose') in ('quality_recheck', 'historical_quality'):
        # Check live input/version even after a task was queued. Frozen snapshots
        # alone cannot detect edits between admission and the actual send.
        from pathlib import Path
        from flask import current_app
        from services.prompts import IMAGE_GENERATION_PROMPT_VERSION
        live = db.session.get(Page, p['page_id'])
        version = db.session.get(PageImageVersion, p['source_version_id'])
        if (not live or not version or version.page_id != live.id or not version.is_current
                or live.project_id != p['project_id'] or live.generated_image_path != version.image_path
                or input_hash(live.get_description_content(), live.get_outline_content()) != p['input_hash']
                or IMAGE_GENERATION_PROMPT_VERSION != p['prompt_version']):
            return False
        from services.copilot_product import validate_local_image
        root = Path(current_app.config['UPLOAD_FOLDER']).resolve()
        path = validate_local_image(root / version.image_path, root / live.project_id)
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != p['source_image_sha256']:
            return False
        if p.get('purpose') == 'historical_quality':
            from models import Project
            from services.prepared_tasks import current_single_page_snapshot
            try:
                fresh = current_single_page_snapshot(db.session.get(Project, p['project_id']), live,
                                                     p['model'], current_app._get_current_object())
                item = fresh['pages'][0]
                if (item['prompt_hash'] != p['prompt_hash'] or item['input_hash'] != p['input_hash']
                        or item['source_version_id'] != p['source_version_id']
                        or item['resolution'] != p['resolution'] or item['aspect_ratio'] != p['aspect_ratio']
                        or [r['sha256'] for r in item['references']] != p['reference_hashes']):
                    return False
            except (ValueError, KeyError, TypeError, OSError):
                return False
    context = trace.association()
    if context.get('task_id'):
        task = db.session.get(Task, context['task_id'])
        record = db.session.get(TaskTrace, context['task_id'])
        if (not task or not record or task.project_id != p['project_id']
                or context.get('page_id') != p['page_id'] or identity() != p['visitor_id']):
            return False
        snapshot = json.loads(record.snapshot)
        pages = snapshot.get('pages', [])
        if (snapshot.get('operation') != 'generate_single_page_image_task'
                or snapshot.get('quota_cycle_id') != p['cycle_id']
                or snapshot.get('model') != p['model'] or len(pages) != 1):
            return False
        if p.get('purpose') in ('quality_recheck', 'historical_quality') and snapshot.get('prompt_version') != p['prompt_version']:
            return False
        page = pages[0]
        return all(page.get(k) == p[k] for k in (
            'page_id', 'input_hash', 'prompt_hash', 'source_version_id', 'aspect_ratio', 'resolution'
        )) and [r['sha256'] for r in page.get('references', [])] == p['reference_hashes']
    if not has_request_context():
        return False
    if request.method in ('GET', 'HEAD'):
        if operation is None:
            return True  # Administrator diagnostics; never a transport send.
        if request.path == '/api/product/status' and identity() == p['visitor_id']:
            return True  # Only this visitor's template action; other operations failed above.
        return False
    wanted = f"/api/projects/{p['project_id']}/pages/{p['page_id']}/generate/image"
    if request.method != 'POST' or request.path != wanted or identity() != p['visitor_id']:
        return False
    page = db.session.get(Page, p['page_id'])
    return bool(page and page.project_id == p['project_id'] and
                input_hash(page.get_description_content(), page.get_outline_content()) == p['input_hash'])


def consume(state, role, operation, call_id, task_id):
    if not allowed(state, role, operation) or not task_id:
        raise RuntimeError('本次图片重试不在已批准的页面、输入或周期范围内。')
    p = state['image_retry_permits'][ready(state)['id']]
    p.update(state='consumed', used_call_id=call_id, used_task_id=task_id,
             consumed_at=datetime.now(timezone.utc).isoformat())
    if p.get('purpose') == 'historical_quality':
        state['last_result'] = 'pending'
    p['digest'] = checksum({k: v for k, v in p.items() if k != 'digest'})
    # Keep paused=True. Success, failure and restart cannot grant a second send.


def grant(state, proposal):
    updated = copy.deepcopy(state)
    permits = updated.setdefault('image_retry_permits', {})
    key = proposal['id']
    if key in permits:
        old = permits[key]
        if any(old.get(k) != v for k, v in proposal.items()):
            raise ValueError('同一失败调用已有不同重试授权，禁止覆盖。')
        return updated, False
    if ready(state):
        raise ValueError('已有未使用的图片重试授权。')
    p = {**copy.deepcopy(proposal), 'state': 'ready', 'used_call_id': None,
         'used_task_id': None, 'approved_at': datetime.now(timezone.utc).isoformat()}
    p['digest'] = checksum(p)
    permits[key] = p
    updated['active_image_retry'] = key
    if not ready(updated):
        raise ValueError('重试授权与当前失败、原预留或周期不一致。')
    return updated, True


def grant_historical(state, proposals):
    """One ordered, finite group within the existing one-use permit store."""
    if not 1 <= len(proposals) <= 4:
        raise ValueError('历史修正许可仅支持明确的一至四页。')
    ids = [p['id'] for p in proposals]
    if len(set(ids)) != len(ids) or len({p['page_id'] for p in proposals}) != len(ids):
        raise ValueError('重复页面或许可。')
    existing = state.get('image_retry_permits', {})
    if any(k in existing for k in ids):
        if not all(k in existing and all(existing[k].get(f) == v for f,v in p.items())
                   for k,p in zip(ids, proposals)):
            raise ValueError('既有历史修正授权不能覆盖或扩充。')
        return copy.deepcopy(state), False
    if any(p.get('state') == 'ready' for p in existing.values()):
        raise ValueError('已有未消费许可，禁止叠加授权。')
    result = copy.deepcopy(state)
    for p in proposals:
        if p.get('purpose') != 'historical_quality' or p.get('group_ids') != ids:
            raise ValueError('历史修正范围不一致。')
        row = dict(copy.deepcopy(p), state='ready', used_call_id=None, used_task_id=None,
                   approved_at=datetime.now(timezone.utc).isoformat())
        row['digest'] = checksum(row)
        result.setdefault('image_retry_permits', {})[row['id']] = row
    result['active_image_retry'] = ids[0]
    if not ready(result):
        raise ValueError('历史修正许可与当前周期和计数不符。')
    return result, True

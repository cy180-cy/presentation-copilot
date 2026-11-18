"""Local explicit single-image retry grant. Never sends or settles a request."""
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3

from services import image_retry as retry
from services.ai_providers import copilot_limits as budget
from services.budget_cycles import current
from services.ledger_settlement import summary
from services.runtime_config import no_links, authorized_now


def quality_snapshot(root, project_id, page_id, model, visitor_id):
    """Read-only ORM + the same prepare path; no application startup/providers."""
    from flask import Flask
    from models import db, Project, Page
    from services.prepared_tasks import current_single_page_snapshot
    from services.visitor_access import scope
    app = Flask('quality-permit-preview')
    database = no_links(root/'data/database.db')
    app.config.update(SQLALCHEMY_DATABASE_URI='sqlite:///'+database.as_posix(), SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLALCHEMY_ENGINE_OPTIONS={'creator': lambda: sqlite3.connect(database.as_uri()+'?mode=ro', uri=True)},
        UPLOAD_FOLDER=str(root/'uploads'), DEFAULT_RESOLUTION='2K', OUTPUT_LANGUAGE='zh', PC_VISITOR_ENABLED=True)
    db.init_app(app)
    with app.app_context(), scope(visitor_id):
        try:
            project, page = db.session.get(Project, project_id), db.session.get(Page, page_id)
            if not project or not page or page.project_id != project.id:
                raise ValueError('复验页面不存在。')
            # All methods called by prepare are local prompt assembly. Deliberately
            # do not initialize SDKs or supply credentials to this preview.
            snap = current_single_page_snapshot(project, page, model, app)
            if not snap or len(snap['pages']) != 1:
                raise ValueError('未取得正式单页准备快照。')
            return {k: snap[k] for k in ('prompt_version', 'model', 'operation', 'pages')}
        finally:
            db.session.remove()
            db.engine.dispose()


def review(root, state, failed_call_id, model, *, quality=False):
    if not re.fullmatch(r'[0-9a-f]{32}', failed_call_id):
        raise ValueError('需要明确的原失败call_id。')
    key = ('single-image-quality-' if quality else 'single-image-retry-') + failed_call_id
    existing = state.get('image_retry_permits', {}).get(key)
    if existing:
        if (existing.get('model') != model or existing.get('source_call_id' if quality else 'failed_call_id') != failed_call_id
                or existing.get('digest') != retry.checksum({k: v for k, v in existing.items() if k != 'digest'})):
            raise ValueError('既有重试许可与本次模型或失败调用不符。')
        return None, {'id': key, 'state': existing['state'], 'unchanged': True}
    cycle = current(state)
    if (not cycle or not state['paused'] or state.get('last_call_id') != failed_call_id
            or state.get('last_result') != ('response' if quality else 'error') or not budget.runtime_totals_match(state)
            or (not quality and failed_call_id in state.get('settlements', {}))):
        raise ValueError('不是当前周期最近一次未结算失败，不能创建重试许可。')
    with sqlite3.connect((root/'data/database.db').as_uri()+'?mode=ro', uri=True) as c:
        c.row_factory = sqlite3.Row
        c.execute('BEGIN')
        if c.execute("SELECT count(*) FROM tasks WHERE status IN ('PENDING','PROCESSING','RUNNING','QUEUED')").fetchone()[0]:
            raise ValueError('有活动任务，不能准备重试。')
        if c.execute("SELECT count(*) FROM pc_visitor_requests WHERE task_id IS NULL AND state IN ('pending','accepted','unknown')").fetchone()[0]:
            raise ValueError('有未决请求，不能准备重试。')
        visitor = c.execute('SELECT * FROM pc_visitor_calls WHERE call_id=?', (failed_call_id,)).fetchone()
        original = state['reservation_calls'].get(failed_call_id)
        if (not visitor or not original or visitor['role'] != 'image'
                or visitor['reserved'] != budget.RESERVATIONS['image']
                or original['reserved'] != visitor['reserved']
                or original.get('task_id') != visitor['task_id']
                or original.get('quota_cycle_id') != cycle['id']):
            raise ValueError('原调用、访客和预留不一致。')
        task = c.execute('SELECT * FROM tasks WHERE id=?', (visitor['task_id'],)).fetchone()
        trace = c.execute('SELECT * FROM task_traces WHERE task_id=?', (visitor['task_id'],)).fetchone()
        if not task or not trace or task['status'] != ('COMPLETED' if quality else 'FAILED'):
            raise ValueError('原任务终态与本次许可用途不符。')
        snapshot, calls = json.loads(trace['snapshot']), json.loads(trace['calls'])
        call = next((r for r in calls if r['call_id'] == failed_call_id), None)
        pages = snapshot.get('pages', [])
        if (snapshot.get('operation') != 'generate_single_page_image_task'
                or snapshot.get('quota_cycle_id') != cycle['id'] or len(pages) != 1
                or not call or call['status'] != ('response_saved' if quality else 'failed_needs_review') or not call.get('sent_at')
                or (not quality and (call.get('actual_credits') is not None or json.loads(trace['outputs'])))):
            raise ValueError('仅允许有完整发送/失败证据、未产出且费用未知的单页图片调用。')
        item = pages[0]
        if item.get('operation') != 'template_generate' or item.get('resolution') != '2K':
            raise ValueError('仅允许原模板页面的2K图片重试。')
        page = c.execute('SELECT * FROM pages WHERE id=? AND project_id=?', (item['page_id'], task['project_id'])).fetchone()
        owner = c.execute("SELECT visitor_id FROM pc_resource_owners WHERE kind='projects' AND resource_id=?", (task['project_id'],)).fetchone()
        if not page or not owner or owner['visitor_id'] != visitor['visitor_id']:
            raise ValueError('页面或访客归属不一致。')
        current_input_hash = retry.input_hash(json.loads(page['description_content']), json.loads(page['outline_content']))
        if quality:
            from services.slide_consistency import content_fingerprint
            if content_fingerprint(json.loads(page['description_content']), json.loads(page['outline_content'])) != content_fingerprint(item['description'], item['outline']):
                raise ValueError('复验业务文案已变化。')
        elif current_input_hash != item['input_hash']:
            raise ValueError('页面文案已变化，不能重放此前输入的重试许可。')
        prepared = None
        image_sha = None
        if quality:
            outputs = json.loads(trace['outputs'])
            if snapshot.get('model') != model or len(outputs) != 1 or outputs[0].get('call_id') != failed_call_id:
                raise ValueError('成功调用与唯一图片产物不能对应。')
            version = c.execute('SELECT * FROM page_image_versions WHERE id=? AND page_id=?',
                                (outputs[0].get('version_id'), page['id'])).fetchone()
            if not version or not version['is_current'] or version['image_path'] != page['generated_image_path']:
                raise ValueError('复验必须绑定该成功调用的当前图片版本。')
            from services.copilot_product import validate_local_image
            image_path = validate_local_image(root/'uploads'/version['image_path'], root/'uploads'/task['project_id'])
            image_sha = hashlib.sha256(Path(image_path).read_bytes()).hexdigest()
            prepared = quality_snapshot(root, task['project_id'], page['id'], model, visitor['visitor_id'])
            new_item = prepared['pages'][0]
            if (new_item['source_version_id'] != version['id'] or new_item['input_hash'] != current_input_hash
                    or new_item['operation'] != 'template_generate' or new_item['resolution'] != '2K'
                    or new_item['aspect_ratio'] != '16:9' or new_item['references'] != item['references']
                    or new_item['prompt_hash'] == item['prompt_hash']):
                raise ValueError('复验须保持原输入/模板/规格，并使用修复后的新提示词。')
            item = new_item
        elif item.get('source_version_id'):
            version = c.execute('SELECT image_path FROM page_image_versions WHERE id=? AND page_id=?', (item['source_version_id'], page['id'])).fetchone()
            if not version or version['image_path'] != page['generated_image_path']:
                raise ValueError('原图片版本已变化。')
        elif page['generated_image_path']:
            raise ValueError('页面已有新图，不能使用首次生成的重试许可。')
        control = c.execute('SELECT active,revision FROM pc_admin_control WHERE id=1').fetchone()
        active = json.loads(control['active']) if control and control['active'] else None
        auth = active['authorization'] if active and active['mode'] == 'live' else None
        if (not auth or not authorized_now(auth) or 'image' not in auth['roles']
                or 'template_generate' not in auth['operations']):
            raise ValueError('已应用的图片授权当前无效。')
        from services.budget_cycles import counts, credit_limit
        costs = summary(state)
        from services.generation_budget import held
        if held(state)['credits']:
            raise ValueError('存在尚未完成的整套生成占位，不能启用单页重试。')
        if (counts(state)['image'] + 1 > auth['image'] or
                costs['total_occupied'] + budget.RESERVATIONS['image'] > credit_limit(state, auth) + 1e-8):
            raise ValueError('现有停止线或次数不足以保留旧占位并新增一次请求。')
        proposal = {'version': 1, 'id': key, 'failed_call_id': failed_call_id,
            'failed_task_id': task['id'], 'project_id': task['project_id'],
            'page_id': page['id'], 'visitor_id': visitor['visitor_id'],
            'cycle_id': cycle['id'], 'credit_limit': cycle['credit_limit'],
            'model': model, 'operation': 'template_generate', 'max_calls': 1,
            'reserved_credits': budget.RESERVATIONS['image'],
            'baseline': {k: state[k] for k in ('text','image','reserved_credits')},
            'reference_hashes': [r['sha256'] for r in item['references']],
            **{k: item[k] for k in ('input_hash','prompt_hash','source_version_id','aspect_ratio','resolution')}}
        if quality:
            proposal.update(purpose='quality_recheck', source_call_id=proposal.pop('failed_call_id'),
                source_task_id=proposal.pop('failed_task_id'), source_image_sha256=image_sha,
                prompt_version=prepared['prompt_version'], prepared_snapshot=prepared)
        report = {'id': key, 'project_id': proposal['project_id'], 'page_id': proposal['page_id'],
            'cycle_id': cycle['id'], 'model': model, 'max_calls': 1,
            'existing_pending_reserved': costs['pending_reserved'],
            'additional_reservation': proposal['reserved_credits'],
            'credit_limit': credit_limit(state, auth), 'unchanged': False,
            'database_evidence': retry.checksum({'snapshot': snapshot, 'call': call,
                'visitor': dict(visitor), 'page': dict(page), 'revision': control['revision']})}
        return proposal, report


def historical_review(root, state, manifest, model):
    """Explicit current approval, independent of old task/cycle/model authority."""
    from services.budget_cycles import counts, credit_limit
    from services.generation_budget import held
    from services.copilot_product import validate_local_image
    if (set(manifest) != {'id','cycle_id','credit_limit','project_id','model','resolution',
                          'aspect_ratio','template_sha256','reason','pages'}
            or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}', manifest['id'])
            or not manifest['reason'] or len(manifest['reason']) > 2000
            or not re.fullmatch(r'[0-9a-f]{64}', manifest['template_sha256'])
            or manifest['model'] != model or manifest['resolution'] != '2K'
            or manifest['aspect_ratio'] != '16:9' or not 1 <= len(manifest['pages']) <= 4):
        raise ValueError('历史修正必须明确限定周期、作品、页面、新模板、模型与规格。')
    pages = manifest['pages']
    if any(set(p) != {'page_id','source_version_id','source_call_id'} for p in pages):
        raise ValueError('每页需要当前源版本及对应原调用。')
    if len({p['page_id'] for p in pages}) != len(pages):
        raise ValueError('历史修正不能重复页面。')
    approval = retry.checksum(manifest)
    ids = [manifest['id']+'-'+p['page_id'] for p in pages]
    existing = state.get('image_retry_permits', {})
    if any(k in existing for k in ids):
        if not all(k in existing and existing[k].get('approval_sha256') == approval
                   and existing[k]['digest'] == retry.checksum({f:v for f,v in existing[k].items() if f!='digest'})
                   for k in ids):
            raise ValueError('已有同名许可，不能变更或扩充。')
        return None, {'ids':ids,'unchanged':True,'states':[existing[k]['state'] for k in ids]}
    cycle = current(state)
    if (not cycle or cycle['id'] != manifest['cycle_id'] or cycle['credit_limit'] != manifest['credit_limit']
            or not state['paused'] or state.get('last_result') != 'response'
            or not budget.runtime_totals_match(state) or held(state)['credits']):
        raise ValueError('周期、占用或暂停现场与新授权不一致。')
    with sqlite3.connect((root/'data/database.db').as_uri()+'?mode=ro',uri=True) as c:
        c.row_factory = sqlite3.Row
        c.execute('BEGIN')
        from cycle_maintenance import check_database
        control = check_database(c)
        active = json.loads(control['active'])
        auth = active.get('authorization')
        if (active['mode'] != 'live' or not auth or not authorized_now(auth)
                or 'image' not in auth['roles'] or 'template_generate' not in auth['operations']):
            raise ValueError('当前已应用图片授权无效。')
        costs = summary(state)
        if (counts(state)['image'] + len(pages) > auth['image']
                or costs['total_occupied'] + len(pages)*budget.RESERVATIONS['image'] > credit_limit(state,auth)+1e-8):
            raise ValueError('预算或次数不足，不能准备历史修正许可。')
        owner = c.execute("SELECT visitor_id FROM pc_resource_owners WHERE kind='projects' AND resource_id=?",
                          (manifest['project_id'],)).fetchone()
        if not owner:
            raise ValueError('作品访客归属缺失。')
        proposals=[]
        for index, target in enumerate(pages):
            page=c.execute('SELECT * FROM pages WHERE id=? AND project_id=?',
                           (target['page_id'],manifest['project_id'])).fetchone()
            version=c.execute('SELECT * FROM page_image_versions WHERE id=? AND page_id=?',
                              (target['source_version_id'],target['page_id'])).fetchone()
            visitor=c.execute('SELECT * FROM pc_visitor_calls WHERE call_id=?',(target['source_call_id'],)).fetchone()
            if (not page or not version or not visitor or visitor['visitor_id'] != owner['visitor_id']
                    or visitor['role']!='image' or not version['is_current']
                    or page['generated_image_path'] != version['image_path']):
                raise ValueError('历史页面、访客或当前版本不一致。')
            task=c.execute('SELECT * FROM tasks WHERE id=?',(visitor['task_id'],)).fetchone()
            trace=c.execute('SELECT * FROM task_traces WHERE task_id=?',(visitor['task_id'],)).fetchone()
            if not task or not trace or task['project_id']!=manifest['project_id'] or task['status']!='COMPLETED':
                raise ValueError('原任务不属于该作品或未完成。')
            snapshot=json.loads(trace['snapshot'])
            call=next((v for v in json.loads(trace['calls']) if v['call_id']==target['source_call_id']),None)
            outputs=[v for v in json.loads(trace['outputs']) if v.get('version_id')==version['id']]
            if (snapshot.get('operation') not in ('generate_images_task','generate_single_page_image_task','edit_page_image_task')
                    or not call or call['status']!='response_saved' or not call.get('sent_at')
                    or len(outputs)!=1 or outputs[0].get('call_id')!=target['source_call_id']):
                raise ValueError('当前图片不能可靠对应历史调用产物。')
            image=validate_local_image(root/'uploads'/version['image_path'],root/'uploads'/manifest['project_id'])
            prepared=quality_snapshot(root,manifest['project_id'],page['id'],model,owner['visitor_id'])
            item=prepared['pages'][0]
            if (prepared['model']!=model or item['source_version_id']!=version['id']
                    or item['operation']!='template_generate'
                    or item['resolution']!=manifest['resolution'] or item['aspect_ratio']!=manifest['aspect_ratio']
                    or [r['sha256'] for r in item['references']] != [manifest['template_sha256']]
                    or item['input_hash'] != retry.input_hash(json.loads(page['description_content']),json.loads(page['outline_content']))):
                raise ValueError('正式prepare结果与明确批准的新输入、模板或规格不符。')
            baseline={k:state[k] for k in ('text','image','reserved_credits')}
            baseline['image']+=index
            baseline['reserved_credits']=round(baseline['reserved_credits']+index*budget.RESERVATIONS['image'],4)
            proposals.append(dict(version=1,id=ids[index],purpose='historical_quality',group_ids=ids,
                approval_sha256=approval,approval_reason=manifest['reason'],anchor_call_id=state['last_call_id'],
                source_call_id=target['source_call_id'],source_task_id=task['id'],
                source_evidence_sha256=retry.checksum({'snapshot':snapshot,'call':call,'output':outputs[0]}),
                project_id=manifest['project_id'],page_id=page['id'],visitor_id=owner['visitor_id'],
                cycle_id=cycle['id'],credit_limit=cycle['credit_limit'],model=model,operation='template_generate',
                max_calls=1,reserved_credits=budget.RESERVATIONS['image'],baseline=baseline,
                reference_hashes=[manifest['template_sha256']],prompt_version=prepared['prompt_version'],
                prepared_snapshot=prepared,source_image_sha256=hashlib.sha256(Path(image).read_bytes()).hexdigest(),
                **{k:item[k] for k in ('input_hash','prompt_hash','source_version_id','aspect_ratio','resolution')}))
        retry.grant_historical(state,proposals)
        return proposals, {'ids':ids,'unchanged':False,'project_id':manifest['project_id'],
            'additional_reservation':len(pages)*budget.RESERVATIONS['image'],
            'approval_sha256':approval,'existing_occupied':costs['total_occupied'],
            'database_revision':control['revision'], 'prompt_hashes':[p['prompt_hash'] for p in proposals]}


def daily_review(root, state, manifest):
    """Read-only reconciliation before closing obsolete acceptance grants."""
    from cycle_maintenance import check_database, database_digest
    from services import manual_balance as mb
    from services.generation_budget import held
    with sqlite3.connect((root/'data/database.db').as_uri()+'?mode=ro',uri=True) as c:
        c.row_factory=sqlite3.Row
        control=check_database(c);active=json.loads(control['active'])
        auth=active['authorization'];value=mb.configured(auth)
        if not value or value['id']!=manifest.get('balance_id'):
            raise ValueError('已应用配置与余额基准不一致。')
        entry=state.get('runtime_authorizations',{}).get(auth['id'],{})
        if entry.get('policy',{}).get('authorization')!=auth or not budget.runtime_totals_match(state):
            raise ValueError('运行授权或历史累计记录不一致。')
        known=state.get('runtime_calls',{})
        rows={r['call_id']:dict(r) for r in c.execute('SELECT call_id,role,reserved,created FROM pc_visitor_calls')}
        if any(k not in rows or rows[k]['role']!=v['role'] or rows[k]['reserved']!=v['reserved'] for k,v in known.items()) or any(
                r['created']>=state['runtime_started_at'] and k not in known for k,r in rows.items()):
            raise ValueError('访客和全局请求证据不一致。')
        cycle=current(state)
        if cycle:
            from services.budget_cycles import counts
            new=[v for k,v in known.items() if k not in cycle['prior_runtime_call_ids']]
            if any(v.get('quota_cycle_id')!=cycle['id'] for v in new) or any(counts(state)[r]!=sum(v['role']==r for v in new) for r in ('text','image')):
                raise ValueError('周期调用归属不一致。')
        if any(held(state).values()):
            raise ValueError('仍有任务级预算占位，不能收尾。')
        task_ids=[r[0] for r in c.execute('SELECT id FROM tasks')]
        _,changed=retry.close_acceptance(state,manifest,task_ids)
        db_digest=database_digest(c)
    report={'unchanged':not changed,'id':manifest['id'],'balance_id':value['id'],
            'balance_credits':value['credits'],'retired_permits':manifest['retire_permit_ids'],
            'database_digest':db_digest,'additional_reservation':0}
    return ({'manifest':manifest,'task_ids':task_ids} if changed else None),report


def run(root, failed_call_id, model, confirmation=None, *, quality=False, historical_file=None, daily_file=None):
    root = no_links(Path(root).resolve())
    from contextlib import nullcontext
    from managed_server import DataLock
    from runtime_maintenance import stopped, backup_locked
    with (DataLock(root/'run') if confirmation is not None else nullcontext()), budget._lock:
        path = no_links(root/'run/call-budget.json')
        raw = path.read_bytes()
        state = json.loads(raw)
        if (root/'run/cycle-apply.pending.json').exists() or (root/'run/admin-apply.pending.json').exists():
            raise RuntimeError('存在未完成维护事务。')
        if historical_file or daily_file:
            source = no_links(Path(historical_file or daily_file).resolve())
            if not source.is_relative_to(root):
                raise ValueError('授权清单必须位于本项目私人数据目录。')
            manifest = json.loads(source.read_text('utf-8'))
            proposal, report = daily_review(root,state,manifest) if daily_file else historical_review(root,state,manifest,model)
        else:
            proposal, report = review(root, state, failed_call_id, model, quality=quality)
        expected = retry.checksum({'ledger': hashlib.sha256(raw).hexdigest(),
                                   'proposal': proposal, 'evidence': report})
        if raw != path.read_bytes():
            raise RuntimeError('核对期间账本变化，请重新预览。')
        report.update(confirmation=expected, applied=False, model_requests=0)
        if confirmation is not None:
            stopped(root)
            if proposal is not None:
                if not secrets.compare_digest(confirmation, expected):
                    raise ValueError('重试许可预览已变化，未应用。')
                if daily_file:
                    updated,changed=retry.close_acceptance(state,proposal['manifest'],proposal['task_ids'])
                else:
                    updated, changed = retry.grant_historical(state, proposal) if historical_file else retry.grant(state, proposal)
                backup = root/'backups'/f"image-retry-{manifest['id'] if historical_file or daily_file else failed_call_id}-{hashlib.sha256(raw).hexdigest()[:12]}"
                if not (daily_file and backup.exists()):
                    backup_locked(root, backup)
                if daily_file:
                    from cycle_maintenance import verify_backup, database_digest
                    verify_backup(backup,raw,report['database_digest'])
                    with sqlite3.connect((root/'data/database.db').as_uri()+'?mode=ro',uri=True) as c:
                        if database_digest(c)!=report['database_digest'] or path.read_bytes()!=raw:
                            raise RuntimeError('备份期间现场变化，未应用。')
                manifest = json.loads((backup/'backup-manifest.json').read_text('utf-8'))
                for entry in manifest['files']:
                    if hashlib.sha256((backup/entry['path']).read_bytes()).hexdigest() != entry['sha256']:
                        raise RuntimeError('重试许可备份校验失败，未应用。')
                with sqlite3.connect((backup/'data/database.db').as_uri()+'?mode=ro', uri=True) as c:
                    if c.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise RuntimeError('备份数据库不可读，未应用。')
                budget._save(path, updated)
                if json.loads(path.read_bytes()) != updated:
                    raise RuntimeError('重试许可持久化核对失败；不要重复发送。')
                report['backup'] = str(backup)
            report['applied'] = True
        return report

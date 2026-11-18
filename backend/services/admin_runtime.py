"""Single-process runtime controls. SQLite is the sole authority for editable fields.

Bootstrap JSON owns paths/resources; .env.copilot owns secrets. The existing ledger
owns all costs. No task input or historical charge is rewritten here.
"""
import copy
import hashlib
import json
import os
import threading
import time
from pathlib import Path
from services import runtime_config as rc
from services.admin_store import persisted_control

LOCK = threading.RLock()
VISITOR_FIELDS = {'window_seconds', 'text', 'image', 'credits', 'pages', 'default_auto_pages'}
PATCH_FIELDS = {'name', 'mode', 'authorization', 'visitor'}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def projection(cfg, name=''):
    return {'name': name, 'mode': cfg['mode'], 'authorization': copy.deepcopy(cfg['authorization']),
            'visitor': {k: cfg['visitor'][k] for k in sorted(VISITOR_FIELDS)}}


def readable_patch(value):
    value = copy.deepcopy(value)
    value['visitor'].setdefault('default_auto_pages', 5)
    return value


def validate_draft(value, *, legacy_page_default=False):
    if not isinstance(value, dict) or set(value) != PATCH_FIELDS:
        raise ValueError('草稿包含不允许的字段。')
    if not isinstance(value['name'], str) or len(value['name']) > 80 or any(ord(c)<32 for c in value['name']):
        raise ValueError('授权名称最多80字，不含控制字符。')
    if value['mode'] not in ('preview', 'live'):
        raise ValueError('模式无效。')
    if not isinstance(value['visitor'], dict) or set(value['visitor']) != VISITOR_FIELDS:
        raise ValueError('访客可编辑字段无效。')
    for k, v in value['visitor'].items():
        rc.number(v, 0, 366*86400 if k=='window_seconds' else 100000, k != 'credits')
    rc.number(value['visitor']['window_seconds'], 60, 366*86400, True)
    rc.number(value['visitor']['pages'], 1, 50, True)
    rc.number(value['visitor']['default_auto_pages'], 1, 50, True)
    a = value['authorization']
    if a is not None:
        if not isinstance(a, dict) or set(a) - {'balance', 'validity'} != {'id','not_before','expires_at','credits','text','image','roles','operations'}:
            raise ValueError('授权字段不完整或含未知字段。')
        if a.get('validity', 'fixed') not in ('fixed', 'daily'):
            raise ValueError('授权期限类型无效。')
        if a.get('balance') is not None:
            from services.manual_balance import validate as validate_balance
            validate_balance(a['balance'])
        for k in ('id','not_before','expires_at'):
            if k == 'expires_at' and a.get('validity') == 'daily' and a[k] is None: continue
            if not isinstance(a[k], str) or len(a[k]) > 80: raise ValueError('授权文字字段无效。')
        for k in ('credits','text','image'): rc.number(a[k], 0, 100000, k != 'credits')
        for k, permitted in [('roles', {'text','image'}), ('operations', rc.OPERATIONS)]:
            if not isinstance(a[k], list) or any(not isinstance(x,str) for x in a[k]) or set(a[k])-permitted:
                raise ValueError('只允许已接入的角色和核心操作。')
    return copy.deepcopy(value)


def combine(base, patch, root, enforce=True, *, legacy=False):
    legacy_page_default = legacy and 'default_auto_pages' not in patch['visitor']
    patch = readable_patch(patch)
    validate_draft(patch, legacy_page_default=legacy_page_default)
    value = {k: copy.deepcopy(base[k]) for k in rc.DEFAULT}
    value.update(mode=patch['mode'], authorization=copy.deepcopy(patch['authorization']))
    value['visitor'].update(patch['visitor'])
    return rc.validate(value, root, source=base['_source'], enforce_authorization=enforce,
                       legacy_page_default=legacy_page_default)


def effective(base, root, enforce=False):
    state = persisted_control(base['data_dir'])
    cfg = combine(base, state['active'], root, enforce, legacy=True) if state and state['active'] else base
    cfg['_admin_revision'] = state['revision'] if state else 0
    cfg['_admin_paused'] = bool(state and state['paused'])
    return cfg


class RuntimeControl:
    def __init__(self, app, store, base, root, credentials, prepare_models):
        self.app, self.store, self.base, self.root = app, store, copy.deepcopy(base), root
        self.credentials, self.prepare_models = credentials, prepare_models
        self.current = effective(base, root)
        os.environ['PC_ADMIN_CONFIG_REVISION']=str(self.current.get('_admin_revision',0))
        self.pending = Path(base['data_dir'])/'run/admin-apply.pending.json'
        if self.pending.exists() or self.pending.with_name('cycle-apply.pending.json').exists():
            os.environ['PC_RUNTIME_INCONSISTENT'] = '1'
            os.environ['PC_ADMIN_PAUSED'] = '1'
        else:
            os.environ['PC_ADMIN_PAUSED'] = '1' if self.current.get('_admin_paused') else '0'

    def environment(self, cfg):
        a = cfg['authorization']; live = cfg['mode']=='live'
        return dict(PC_MODE=cfg['mode'], PC_BUDGET_CREDITS=str(a['credits'] if a else 0),
            PC_BATCH_ID='', PC_TRIAL_VISITOR='', PC_MODEL_ROLES=','.join(a['roles']) if live else '',
            PC_IMAGE_OPERATIONS=','.join(a['operations']) if live else '', PC_REGION_EDIT_ENABLED='0',
            PC_RUN_POLICY=json.dumps({'authorization':a,'visitor':cfg['visitor']}) if live else '',
            PC_RUN_VISITOR_LIMITS=json.dumps(cfg['visitor']))

    def active_count(self):
        from models import db, Task, VisitorRequest
        from services.ai_providers import copilot_limits as b
        with self.app.app_context():
            tasks = db.session.query(Task).filter(Task.status.in_(['PENDING','PROCESSING','RUNNING','QUEUED'])).count()
            pending = db.session.query(VisitorRequest).filter(VisitorRequest.task_id.is_(None), VisitorRequest.state.in_(['pending','accepted','unknown'])).count()
        return tasks + pending + int(b._network_worker_running.is_set())

    def resource_ok(self):
        import psutil, shutil
        r = self.base['resources']
        return (psutil.virtual_memory().available/2**30 >= r['start_memory_gib'] and
                psutil.Process().memory_info().rss/2**20 <= r['rss_mib'] and
                psutil.cpu_percent(interval=.1) <= r['start_cpu_percent'] and
                shutil.disk_usage(self.base['data_dir']).free/2**30 >= r['disk_gib'])

    def check(self, patch):
        from services.ai_providers import copilot_limits as b
        from services.ledger_settlement import occupied as settled_occupancy
        from services.budget_cycles import counts, credit_limit, current as current_cycle
        problems=[]; cfg=None
        try: cfg=combine(self.base, patch, self.root)
        except (ValueError, TypeError, KeyError):
            problems.append('配置字段、额度或带时区的有效期不完整/无效。')
        if cfg and cfg['mode']=='live':
            status = self.credentials()
            if not status['ready']: problems.append('受管模型配置未齐全或与已有预设不符。')
            if self.pending.exists() or self.pending.with_name('cycle-apply.pending.json').exists() or os.getenv('PC_RUNTIME_INCONSISTENT')=='1': problems.append('配置或账目存在未完成写入，需本地核对。')
            state=b.budget_state(); a=cfg['authorization']
            from services import manual_balance as mb
            manual = mb.configured(a)
            if manual:
                proposed = copy.deepcopy(state)
                mb.initialize(proposed, manual)
                balance = mb.summary(proposed, a)
            if not manual and current_cycle(state) and a['credits'] > credit_limit(state, a):
                problems.append('授权停止线超过本轮已批准的周期预算，不能通过重新应用配置提额。')
            from services.generation_budget import held
            occupied=held(state)
            if state['paused'] and not manual: problems.append('历史失败/未知费用尚未核对，不能清除暂停后恢复。')
            for role in a['roles']:
                # A fully funded waiting workflow can resume without reserving
                # a second copy of its next call. New sends still use the guard.
                extra = 0 if occupied['credits'] else 1
                credit_short = (balance['available'] < mb.task_hold(occupied,a)+extra*mb.unit(role,a)) if manual else (settled_occupancy(state)+occupied['credits']+extra*b.RESERVATIONS[role]>credit_limit(state,a)+1e-8)
                used_counts = mb.counts(proposed,a) if manual else counts(state)
                if used_counts[role]+occupied[role]+extra>a[role] or credit_short:
                    problems.append(f'{role}累计停止线不足以容纳下一次预留。')
                if not cfg['visitor'][role] or (not manual and cfg['visitor']['credits']<b.RESERVATIONS[role]):
                    problems.append(f'{role}访客额度未开放或不足。')
            if not self.resource_ok(): problems.append('当前个人项目资源保护条件未满足。')
        if self.active_count(): problems.append('仍有活动或未决任务；配置应用需等待其结束。')
        return cfg, problems

    def draft(self, patch, revision, draft_revision):
        patch=validate_draft(patch)
        with LOCK, self.store.transaction() as c:
            state=c.execute('SELECT * FROM pc_admin_control WHERE id=1').fetchone()
            if state['revision']!=revision or state['draft_revision']!=draft_revision:
                raise ValueError('配置版本已变化，请刷新后重新编辑。')
            c.execute('UPDATE pc_admin_control SET draft=?,draft_revision=draft_revision+1 WHERE id=1', (json.dumps(patch,ensure_ascii=False),))
            self.store.audit(c, 'save_draft', {'draft_revision':draft_revision+1,'changed_fields':sorted(PATCH_FIELDS)})
        return self.view()

    def preview_change(self):
        state=self.store.control(); patch=state['draft']
        if patch is None: raise ValueError('请先保存草稿。')
        confirmation=fingerprint([state['revision'],state['draft_revision'],patch])
        _, problems=self.check(patch)
        before=readable_patch(state['active']) if state['active'] else projection(self.base)
        patch=readable_patch(patch)
        changes={k:{'before':before.get(k),'after':patch.get(k)} for k in PATCH_FIELDS if before.get(k)!=patch.get(k)}
        return {'revision':state['revision'],'draft_revision':state['draft_revision'],
                'confirmation':confirmation,
                'changes':changes,'blockers':problems,'can_apply':not problems}

    def apply(self, confirmation, phrase, resume=False):
        from services.ai_providers import copilot_limits as b
        # Applying excludes admissions and provider reservations; pause itself never
        # waits for a long provider call. No HTTP endpoint manages a process/PID.
        with LOCK:
            state=self.store.control(); patch=state['active'] if resume else state['draft']
            if patch is None: raise ValueError('没有可应用的配置。')
            expected=fingerprint([state['revision'],state['draft_revision'],patch])
            if confirmation!=expected: raise ValueError('确认对应的配置已变化，请重新校验。')
            wanted='恢复已应用授权' if resume else '确认'
            if phrase!=wanted: raise ValueError('需要明确确认本次应用范围。')
            os.environ['PC_ADMIN_APPLYING']='1'
            try:
                cfg, problems=self.check(patch)
                if problems: raise ValueError('；'.join(problems))
                from services.visitor_access import ADMISSION_LOCK as visitor_lock
                with b._lock, visitor_lock:
                    if self.active_count(): raise ValueError('有任务正在进入执行，请稍后应用。')
                    ledger=b._root()/'call-budget.json'; marker=b._root()/'runtime-initialized.json'
                    old_files={p:p.read_bytes() if p.exists() else None for p in (ledger,marker)}
                    previous_env=dict(os.environ); old_config=dict(self.app.config)
                    try:
                        b._save(self.pending, {'revision':state['revision']+1,'action':'apply_runtime'})
                        if cfg['mode']=='live':
                            # Reads the one .env and constructs providers without requests.
                            os.environ.update(self.environment(cfg))
                            self.prepare_models(cfg)
                            b.initialize_runtime(self.app, approve_change=True)
                        else:
                            os.environ.update(self.environment(cfg))
                            for k in ('TEXT_API_KEY','IMAGE_API_KEY'): os.environ[k]=''; self.app.config[k]=''
                        with self.store.transaction() as c:
                            row=c.execute('SELECT * FROM pc_admin_control WHERE id=1').fetchone()
                            if row['revision']!=state['revision'] or row['draft_revision']!=state['draft_revision']:
                                raise ValueError('版本冲突，请重新校验。')
                            c.execute('UPDATE pc_admin_control SET active=?,revision=revision+1,paused=0 WHERE id=1', (json.dumps(patch,ensure_ascii=False),))
                            self.store.audit(c, 'resume' if resume else 'apply', {'revision':state['revision']+1,'mode':cfg['mode'],
                                'authorization_id':cfg['authorization']['id'] if cfg['authorization'] else None,
                                'fields':patch})
                        self.pending.unlink()
                    except BaseException:
                        # Restore only this attempted transaction; never refund a model call.
                        os.environ.clear(); os.environ.update(previous_env)
                        self.app.config.update(old_config)
                        try:
                            with self.store.transaction() as c:
                                c.execute('UPDATE pc_admin_control SET active=?,revision=?,paused=? WHERE id=1',
                                    (json.dumps(state['active']) if state['active'] else None,state['revision'],state['paused']))
                                self.store.audit(c,'apply_failed',{'restored_revision':state['revision']})
                            for path, content in old_files.items():
                                if content is None: path.unlink(missing_ok=True)
                                else:
                                    tmp=path.with_suffix('.rollback');tmp.write_bytes(content);tmp.replace(path)
                            self.pending.unlink(missing_ok=True)
                        except Exception:
                            os.environ['PC_RUNTIME_INCONSISTENT']='1'
                            os.environ['PC_ADMIN_PAUSED']='1'
                        raise
                    self.current=cfg
                    self.current['_admin_revision']=state['revision']+1
                    os.environ['PC_ADMIN_CONFIG_REVISION']=str(state['revision']+1)
                    self.app.config['PC_VISITOR_LIMITS']=copy.deepcopy(cfg['visitor'])
                    os.environ['PC_ADMIN_PAUSED']='0'
            finally:
                os.environ.pop('PC_ADMIN_APPLYING',None)
        return self.view()

    def pause(self):
        with LOCK, self.store.transaction() as c:
            c.execute('UPDATE pc_admin_control SET paused=1,revision=revision+1 WHERE id=1')
            self.store.audit(c,'pause',{'meaning':'blocks new sends; in-flight results and reservations retained'})
        os.environ['PC_ADMIN_PAUSED']='1'
        self.current['_admin_revision']=self.store.control()['revision']
        return self.view()

    def view(self):
        from services.ai_providers import copilot_limits as b
        from services.generation_budget import held
        from services.ledger_settlement import summary
        from services.budget_cycles import current as current_cycle, counts, credit_limit
        state=self.store.control(); ledger=b.budget_state(); a=self.current['authorization']
        settlement=summary(ledger)
        cycle=current_cycle(ledger)
        from services import manual_balance as mb
        manual = mb.summary(ledger,a) if a else None
        if manual: manual.pop('_outcomes',None)
        current_ledger={**counts(ledger),'reserved_credits':settlement['original_reserved'], 'paused':ledger['paused']}
        return {'revision':state['revision'],'draft_revision':state['draft_revision'],
            'draft':readable_patch(state['draft']) if state['draft'] else projection(self.current),
            'active':readable_patch(state['active']) if state['active'] else projection(self.base),
            'has_saved_draft':state['draft'] is not None, 'paused':bool(state['paused']),
            'mode':os.getenv('PC_MODE'),'active_tasks':self.active_count(), 'queue_limit':self.base['queue'],
            'authorization_valid':bool(a and rc.authorized_now(a)),
            'resume_confirmation':fingerprint([state['revision'],state['draft_revision'],state['active']]),
            'config_source':'本地启动JSON（路径/资源）＋SQLite已应用管理配置（运行/额度）',
            'credentials':self.credentials(), 'ledger':current_ledger,
            'quota_cycle':{'id':cycle['id'],'credit_limit':cycle['credit_limit'],'created_at':cycle['created_at']} if cycle else None,
            'lifetime_ledger':{k:ledger[k] for k in ('text','image','reserved_credits')},
            'external_unknown':settlement['external_unknown'],
            'settlement':settlement, 'manual_balance':manual,
            'remaining_reservation':max(0,round(manual['available']-mb.task_hold(held(ledger),a),6)) if manual else max(0,round(credit_limit(ledger,a)-settlement['total_occupied']-held(ledger)['credits'],6)) if a else None,
            'platform_balance':None,'actual_credits':settlement['settled_actual'] if ledger.get('settlements') else None,
            'reasons':{r:b.execution_reason(r) for r in ('text','image')},
            'resources_ready':self.resource_ok(),
            'applied_version_in_process':self.current.get('_admin_revision',0),
            'requires_restart':False, 'model_probe_performed':False}

"""One explicitly approved, offline cycle transition. No provider/credential access.

Lifetime counters are immutable. A durable pending marker blocks model sends if
the SQLite/JSON transition is interrupted; it is never cleared speculatively.
"""
import copy
import hashlib
import json
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from managed_server import DataLock
from runtime_maintenance import stopped, backup_locked
from services.runtime_config import no_links
from services.admin_store import Store
from services import budget_cycles as cycles, ledger_settlement as settlement
from services.ai_providers import copilot_limits as budget


def database_digest(conn):
    result = {}
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        safe = '"' + table.replace('"', '""') + '"'
        rows = sorted(json.dumps(list(row), default=str, ensure_ascii=False) for row in conn.execute('SELECT * FROM '+safe))
        result[table] = cycles.checksum(rows)
    return cycles.checksum(result)


def check_database(conn):
    if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        raise ValueError('数据库完整性检查失败。')
    if conn.execute("SELECT count(*) FROM tasks WHERE status IN ('PENDING','PROCESSING','RUNNING','QUEUED')").fetchone()[0]:
        raise RuntimeError('存在活动任务，禁止周期迁移。')
    if conn.execute("SELECT count(*) FROM pc_visitor_requests WHERE task_id IS NULL AND state IN ('pending','accepted','unknown')").fetchone()[0]:
        raise RuntimeError('存在未决请求，禁止周期迁移。')
    row = conn.execute('SELECT * FROM pc_admin_control WHERE id=1').fetchone()
    if row is None or not row['active']:
        raise ValueError('没有已应用管理配置，禁止猜测迁移。')
    return dict(row)


def verify_backup(destination, raw_ledger, db_digest):
    manifest = no_links(destination/'backup-manifest.json')
    raw = manifest.read_bytes()
    for row in json.loads(raw)['files']:
        relative = Path(row['path'])
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('备份文件路径无效。')
        path = no_links(destination/relative)
        if not path.is_relative_to(destination) or hashlib.sha256(path.read_bytes()).hexdigest() != row['sha256']:
            raise ValueError('备份文件校验失败。')
    if (destination/'run/call-budget.json').read_bytes() != raw_ledger:
        raise ValueError('备份账本与当前原始记录不同。')
    with sqlite3.connect((destination/'data/database.db').as_uri()+'?mode=ro', uri=True) as conn:
        if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or database_digest(conn) != db_digest:
            raise ValueError('备份数据库不能恢复当前现场。')
    return hashlib.sha256(raw).hexdigest()


def report(state, *, applied, backup=None, confirmation=None, unchanged=False):
    cycle = cycles.current(state)
    return {'applied':applied, 'unchanged':unchanged, 'confirmation':confirmation,
        'cycle_id':cycle['id'] if cycle else None, 'credit_limit':cycle['credit_limit'] if cycle else None,
        'counts':cycles.counts(state), 'settlement':settlement.summary(state),
        'lifetime_counts':{k:state[k] for k in ('text','image','reserved_credits')},
        'backup':str(backup) if backup else None, 'models_enabled':False}


def register_cycle_policy(state, active, previous_active):
    """Approve only this cap change using the original initializer's mechanism."""
    if not state.get('runtime_started_at'):
        return
    entry=state.get('runtime_authorizations',{}).get(active['authorization']['id'])
    if not entry:raise ValueError('当前周期缺少原常驻授权记录。')
    old=entry['policy']
    wanted=copy.deepcopy(old);wanted['authorization']=copy.deepcopy(active['authorization'])
    if budget.same_runtime_policy(old,wanted):return
    if old['authorization']!=previous_active['authorization'] or any(
            {'default_auto_pages':5,**old['visitor']}.get(k)!=v for k,v in previous_active['visitor'].items()):
        raise ValueError('原常驻策略与迁移前管理配置不符，禁止猜测覆盖。')
    budget.register_runtime_policy(state,wanted,approve_change=True)


def run(root, cycle_id, credits, confirmation=None, *, revision_id=None, reason=None, retire_permit_ids=()):
    if retire_permit_ids and not revision_id:
        raise ValueError('撤销旧许可仅限显式同周期修订。')
    root = no_links(Path(root).resolve())
    ledger = no_links(root/'run/call-budget.json')
    pending = no_links(root/'run/cycle-apply.pending.json')
    path = no_links(root/'data/database.db')
    with DataLock(root/'run'), budget._lock:
        stopped(root)
        if pending.exists() or (root/'run/admin-apply.pending.json').exists():
            raise RuntimeError('存在未完成写入，保留标记，需先核对恢复；未再次清零。')
        raw = ledger.read_bytes(); state = json.loads(raw)
        settlement.summary(state, lifetime=True)
        settlement.summary(state)
        if state.get('runtime_started_at') and not budget.runtime_totals_match(state):
            raise ValueError('原累计账目不一致，禁止迁移。')
        with sqlite3.connect(path.as_uri()+'?mode=ro', uri=True) as read:
            read.row_factory=sqlite3.Row
            control=check_database(read); db_hash=database_digest(read)
            known=state.get('runtime_calls', {})
            for call_id, row in known.items():
                original=read.execute('SELECT role,reserved FROM pc_visitor_calls WHERE call_id=?',(call_id,)).fetchone()
                if not original or original['role']!=row['role'] or original['reserved']!=row['reserved']:
                    raise ValueError('访客与原预留不一致，禁止迁移。')
            if state.get('runtime_started_at'):
                for (call_id,) in read.execute('SELECT call_id FROM pc_visitor_calls WHERE created>=?',(state['runtime_started_at'],)):
                    if call_id not in known:raise ValueError('存在半写入访客占位，禁止迁移。')
        existing=cycles.current(state)
        if existing and not revision_id:
            cycles.create(state,cycle_id=cycle_id,credit_cap=credits,source_sha256='',archive_sha256='',created_at='')
            active=json.loads(control['active'])
            if active['authorization']['credits'] > existing['credit_limit']:
                raise ValueError('当前授权超过周期预算，需核对；不重新清零。')
            return report(state,applied=False,unchanged=True)
        if revision_id:
            _, changed = cycles.revise(state, cycle_id=cycle_id, credit_cap=credits,
                revision_id=revision_id, reason=reason, retire_permit_ids=retire_permit_ids, source_sha256='', archive_sha256='', created_at='')
            if not changed:
                if json.loads(control['active'])['authorization']['credits'] != credits:
                    raise ValueError('修订与已应用策略不一致，保留现场。')
                return report(state, applied=False, unchanged=True)
        def transition(**evidence):
            if revision_id:
                return cycles.revise(state, cycle_id=cycle_id, credit_cap=credits,
                                     revision_id=revision_id, reason=reason, retire_permit_ids=retire_permit_ids, **evidence)
            return cycles.create(state, cycle_id=cycle_id, credit_cap=credits, **evidence)
        if any(p.get('state')=='held' and any(p['remaining'].values()) for p in state.get('generation_plans',{}).values()):
            raise ValueError('存在未完成任务预算占位，禁止迁移。')
        source_hash=hashlib.sha256(raw).hexdigest()
        pin=cycles.checksum({'ledger':source_hash,'database':db_hash,'cycle_id':cycle_id,'credits':credits,'revision_id':revision_id,'reason':reason,'retire_permit_ids':sorted(retire_permit_ids)})
        # Validate even during dry-run; no write is necessary to preview the cycle.
        proposed,_=transition(source_sha256=source_hash,
                                archive_sha256='0'*64,created_at=datetime.now(timezone.utc).isoformat())
        if confirmation is None:
            return report(proposed,applied=False,confirmation=pin)
        if not secrets.compare_digest(confirmation,pin):
            raise ValueError('现场或迁移范围已变化，必须重新只读核对。')
        destination=no_links(root/'backups'/('quota-cycle-'+(revision_id or cycle_id)+'-'+pin[:12]))
        # This receipt contradicts the broad historical no-charge assertion.
        # Preserve it without retrospectively inventing a settlement or refund.
        receipt='logs/pc005-two-page/billing-final.json'
        extra=[receipt] if (root/receipt).is_file() else []
        backup_locked(root,destination,extra_files=extra)
        archive_hash=verify_backup(destination,raw,db_hash)
        updated,_=transition(source_sha256=source_hash,
                               archive_sha256=archive_hash,created_at=datetime.now(timezone.utc).isoformat())
        active=copy.deepcopy(json.loads(control['active']))
        if not active.get('authorization'):raise ValueError('原运行授权缺失。')
        # Preserve the validated execution mode. Preview cannot carry a live
        # authorization in the existing config contract; paused=1 below closes
        # every send without discarding the existing authorization fields.
        active['authorization']['credits']=credits
        register_cycle_policy(updated,active,json.loads(control['active']))
        evidence={'cycle_id':cycle_id,'history_ledger_sha256':source_hash,
            'backup_manifest_sha256':archive_hash,'historical_costs':'preserved_not_reconciled',
            'user_assertion':'Historical failed/timeout requests reported unbilled; do not carry their holds into the new cycle.',
            'contradictory_receipt':extra,'manual_failure_protection':'Retained in original budget-events; not imported into new cycle.',
            'new_credit_limit':credits,'models_enabled':False}
        if revision_id:
            evidence = dict(updated['quota_cycle_revisions'][revision_id], revision_id=revision_id)
        target_paused = control['paused'] if revision_id else 1
        conn=sqlite3.connect(path,timeout=5); conn.row_factory=sqlite3.Row
        committed=False
        try:
            conn.execute('BEGIN IMMEDIATE')
            if database_digest(conn)!=db_hash or ledger.read_bytes()!=raw:
                raise ValueError('备份后现场发生变化，未迁移。')
            budget._save(pending,{'cycle_id':cycle_id,'backup':str(destination),
                'before_ledger_sha256':source_hash,'after_ledger_sha256':cycles.checksum(updated),
                'next_revision':control['revision']+1})
            budget._save(ledger,updated)
            conn.execute('UPDATE pc_admin_control SET active=?,revision=revision+1,paused=? WHERE id=1',
                         (json.dumps(active,ensure_ascii=False),target_paused))
            Store.audit(conn,'quota_cycle_revised' if revision_id else 'quota_cycle_started',evidence,actor='local-cli')
            conn.commit(); committed=True
        except BaseException:
            conn.rollback()
            # A commit may have succeeded even if its acknowledgement was lost.
            actual=conn.execute('SELECT active,revision,paused FROM pc_admin_control WHERE id=1').fetchone()
            committed=(actual['revision']==control['revision']+1 and actual['active']==json.dumps(active,ensure_ascii=False) and actual['paused']==target_paused)
            if not committed:
                tmp=ledger.with_suffix('.cycle-rollback'); tmp.write_bytes(raw); tmp.replace(ledger)
                pending.unlink(missing_ok=True)
            raise
        finally:
            conn.close()
        # Read back both stores before removing the fail-closed marker.
        reread=json.loads(ledger.read_bytes())
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as read:
            read.row_factory=sqlite3.Row
            final=check_database(read)
        if (reread!=updated or final['revision']!=control['revision']+1
                or json.loads(final['active'])!=active or final['paused']!=target_paused
                or any(final[k]!=control[k] for k in ('draft','draft_revision'))):
            raise RuntimeError('迁移持久化核对失败；已保留pending标记，禁止模型调用。')
        pending.unlink()
        return report(reread,applied=committed,backup=destination)

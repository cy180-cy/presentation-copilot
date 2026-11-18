"""Offline settlement, using the managed instance's exclusive lock and read-only DB.

No dotenv, providers, migrations, task execution or authorization changes.
"""
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3

from managed_server import DataLock
from services.runtime_config import no_links
from services import ledger_settlement as settlement
from services.ai_providers import copilot_limits as budget


def read_json(path, limit=1048576):
    path = no_links(path)
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError('结算输入或证据缺失/过大。')
    raw = path.read_bytes()
    return json.loads(raw), raw


def local_file(root, relative):
    if not isinstance(relative, str) or Path(relative).is_absolute() or '..' in Path(relative).parts:
        raise ValueError('结算文件必须在当前私有数据目录内。')
    path = no_links(root / relative)
    if not path.is_relative_to(root):
        raise ValueError('结算文件越界。')
    return path


def review_database(root, originals, runtime_ids=(), no_charge_targets=()):
    """A missing sent_at alone is never a proof of not sending."""
    path = no_links(root / 'data/database.db')
    if not path.is_file():
        raise ValueError('原数据库缺失，不能结算。')
    confirmed = set()
    traces = {}
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        if conn.execute("SELECT count(*) FROM tasks WHERE status IN ('PENDING','PROCESSING','RUNNING','QUEUED')").fetchone()[0]:
            raise RuntimeError('存在活动任务，结算需等待其结束。')
        if conn.execute("SELECT count(*) FROM pc_visitor_requests WHERE task_id IS NULL AND state IN ('pending','accepted','unknown')").fetchone()[0]:
            raise RuntimeError('存在未决请求，需先核对，不能结算。')
        for row in conn.execute('SELECT calls FROM task_traces'):
            for call in json.loads(row['calls']):
                call_id = call['call_id']
                if call_id in traces and traces[call_id] != call:
                    raise ValueError('调用追踪重复且不一致。')
                traces[call_id] = call
        for call_id, original in originals.items():
            visitor = conn.execute('SELECT role,reserved,task_id FROM pc_visitor_calls WHERE call_id=?', (call_id,)).fetchone()
            if call_id in runtime_ids and visitor is None:
                raise ValueError('常驻原预留缺少访客记录，不能结算。')
            if visitor and (visitor['role'] != original['role'] or settlement.amount(visitor['reserved']) != settlement.amount(original['reserved'])
                            or original.get('task_id') and visitor['task_id'] != original['task_id']):
                raise ValueError('访客与原预留不一致，不能结算。')
            trace = traces.get(call_id, {})
            if call_id in no_charge_targets and not (
                    trace.get('status') == 'failed_needs_review' and trace.get('sent_at')
                    and trace.get('ended_at') and not trace.get('usage')
                    and trace.get('actual_credits') is None):
                raise ValueError('未扣费确认缺少已结束失败的发送证据，或存在费用/用量矛盾。')
            if (trace.get('status') == 'local_rejected' and not trace.get('sent_at') and trace.get('ended_at')
                    and trace.get('actual_credits_source') == 'confirmed_not_sent'
                    and trace.get('actual_credits') == 0
                    and settlement.amount(trace.get('reserved_credits', 0)) == settlement.amount(original['reserved'])
                    and trace.get('failure_stage') == 'configuration_or_budget'):
                confirmed.add(call_id)
            if trace.get('sent_at') or trace.get('status') in ('sent', 'response_received', 'response_saved') or trace.get('usage'):
                confirmed.discard(call_id)
    return confirmed, settlement.digest(traces)


def archive(root, raw):
    checksum = hashlib.sha256(raw).hexdigest()
    directory = no_links(root / 'run/settlement-evidence')
    directory.mkdir(exist_ok=True)
    path = no_links(directory / (checksum + '.json'))
    if path.exists():
        if path.read_bytes() != raw:
            raise RuntimeError('证据归档冲突，停止写入。')
    else:
        with path.open('xb') as file:
            file.write(raw)
            file.flush()
    return checksum


def run(root, input_path, confirmation=None):
    root = no_links(Path(root).resolve())
    input_path = no_links(Path(input_path).resolve())
    if not input_path.is_relative_to(root):
        raise ValueError('结算输入必须位于当前私有数据目录。')
    with DataLock(root / 'run'), budget._lock:
        ledger_path = no_links(root / 'run/call-budget.json')
        state, raw_ledger = read_json(ledger_path, limit=16777216)
        if state.get('runtime_started_at') and not budget.runtime_totals_match(state):
            raise ValueError('原累计账目不一致，不能结算。')
        request, raw_input = read_json(input_path)
        if not isinstance(request, dict) or set(request) != {'entries'} or not isinstance(request['entries'], list):
            raise ValueError('结算文件只接受entries清单。')
        terminal = root / 'run/calls.jsonl'
        rows = [json.loads(line) for line in no_links(terminal).read_text(encoding='utf8').splitlines() if line.strip()] if terminal.exists() else []
        originals = settlement.catalog(state, rows)
        for target, record in settlement.entries(state).items():
            original = originals.get(target)
            if not original or original['role'] != record['role'] or settlement.amount(original['reserved']) != settlement.amount(record['reserved_credits']):
                raise ValueError('历史结算的原预留证据缺失或不符。')
        no_charge_targets = {row.get('target') for row in request['entries']
                             if isinstance(row, dict) and row.get('kind') == 'confirmed_no_charge'}
        not_sent, trace_digest = review_database(root, originals, state.get('runtime_calls', {}), no_charge_targets)
        for row in rows:
            if row.get('result') == 'response':
                not_sent.discard(row.get('call_id'))
        usage_path = root / 'run/usage.jsonl'
        if usage_path.exists():
            for line in no_links(usage_path).read_text(encoding='utf8').splitlines():
                if line.strip():
                    row = json.loads(line)
                    not_sent.discard(row.get('call_id') or f"legacy:{row['role']}:{row['attempt']}")
        proposals, proofs, no_charge = [], [], set()
        for row in request['entries']:
            if not isinstance(row, dict) or 'evidence' not in row:
                raise ValueError('每笔结算需要本地已核对证据。')
            evidence = row['evidence']
            if not isinstance(evidence, dict) or set(evidence) != {'path', 'sha256'}:
                raise ValueError('证据需提供私有相对路径和SHA256。')
            proof, raw_proof = read_json(local_file(root, evidence['path']))
            checksum = hashlib.sha256(raw_proof).hexdigest()
            if checksum != evidence['sha256']:
                raise ValueError('证据摘要变化，请重新核对。')
            if not isinstance(proof, dict) or proof.get('target') != row.get('target') or proof.get('kind') != row.get('kind'):
                raise ValueError('证据与本次请求/结算类型不符。')
            if not isinstance(proof.get('reference'), str) or not 1 <= len(proof['reference'].strip()) <= 500:
                raise ValueError('证据需要原始账单/未发送核对依据的位置。')
            if row.get('kind') == 'invoice' and (proof.get('invoice_id') != row.get('invoice_id')
                    or settlement.amount(proof.get('actual_credits')) != settlement.amount(row.get('actual_credits'))):
                raise ValueError('账单证据与请求标识或实扣不符。')
            if row.get('kind') == 'confirmed_no_charge':
                if (proof.get('confirmed_no_charge') is not True
                        or proof.get('confirmation_source') not in ('user', 'provider')
                        or proof.get('confirmation_source') != row.get('confirmation_source')
                        or not isinstance(proof.get('confirmation'), str)
                        or not proof['confirmation'].strip()):
                    raise ValueError('未扣费证据需要明确来源、原始确认内容及逐笔归属。')
                no_charge.add(row['target'])
            proposals.append({**{k: v for k, v in row.items() if k != 'evidence'}, 'evidence_sha256': checksum})
            proofs.append(raw_proof)
        updated, report = settlement.prepare(state, proposals, originals, not_sent, no_charge=no_charge)
        expected = settlement.digest({'ledger': hashlib.sha256(raw_ledger).hexdigest(),
            'input': hashlib.sha256(raw_input).hexdigest(), 'originals': originals, 'traces': trace_digest,
            'not_sent': sorted(not_sent)})
        report.update(confirmation=expected, applied=False, model_requests=0)
        if confirmation is not None:
            if report['changed']:
                if not secrets.compare_digest(confirmation, expected):
                    raise ValueError('预览已变化，请重新dry-run确认；未应用。')
                if state.get('runtime_started_at') and not budget.runtime_totals_match(state):
                    raise ValueError('原累计账目不一致，不能结算。')
                # Archive originals/proofs BEFORE one atomic ledger replacement.
                # A failed archive/save leaves either unused evidence or the full
                # committed adjustment; it never partially settles individual rows.
                for raw in [raw_ledger, raw_input, *proofs]:
                    archive(root, raw)
                budget._save(ledger_path, updated)
            report['applied'] = True  # Exact replay: no write and no extra release.
        return report

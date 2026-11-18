"""Offline-only legacy ownership plan/apply. No server or provider imports.

Run without --apply first. Applying requires the individual service to be stopped.
This is deliberately not reachable through HTTP and never grants administrator roles.
"""
import argparse
import json
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def plan(connection, project, visitor):
    if not connection.execute('select 1 from projects where id=?', (project,)).fetchone():
        raise ValueError('作品不存在。')
    if not connection.execute('select 1 from pc_visitors where id=?', (visitor,)).fetchone():
        raise ValueError('目标访客不存在；请先用自己的浏览器建立会话。')
    pairs = [('projects', project)]
    for table in ('pages', 'tasks', 'materials', 'reference_files', 'project_template_assets'):
        pairs += [(table, r[0]) for r in connection.execute(f'select id from {table} where project_id=?', (project,))]
    pairs += [('page_image_versions', r[0]) for r in connection.execute(
        'select id from page_image_versions where page_id in (select id from pages where project_id=?)', (project,))]
    pairs += [('task_traces', r[0]) for r in connection.execute(
        'select task_id from task_traces where task_id in (select id from tasks where project_id=?)', (project,))]
    for kind, rid in pairs:
        existing = connection.execute('select visitor_id from pc_resource_owners where kind=? and resource_id=?', (kind,rid)).fetchone()
        if existing and existing[0] != visitor:
            raise ValueError('资源已有其他归属，禁止覆盖。')
    return pairs


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True);parser.add_argument('--visitor', required=True)
    parser.add_argument('--apply', action='store_true');args=parser.parse_args()
    path=ROOT/'.copilot/data/database.db'
    connection=sqlite3.connect(f'file:{path.as_posix()}?mode=ro',uri=True)
    pairs=plan(connection,args.project,args.visitor)
    print(json.dumps({'project':args.project,'resource_count':len(pairs),'applied':False}))
    if not args.apply:return
    import psutil
    record=json.loads((ROOT/'.copilot/run/server.json').read_text(encoding='utf8'))
    if psutil.pid_exists(record['pid']) and not record.get('stopped'):
        raise RuntimeError('先通过个人启动脚本停止服务，再执行已确认的映射。')
    if connection.execute("select 1 from tasks where status in ('PENDING','PROCESSING','QUEUED','RUNNING') limit 1").fetchone():
        raise RuntimeError('存在未结束任务，不能映射。')
    backup=ROOT/f'.copilot/backups/visitor-mapping-{time.time_ns()}.db'
    backup.parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(backup) as destination:connection.backup(destination)
    connection.close()
    with sqlite3.connect(path) as connection:
        connection.execute('BEGIN IMMEDIATE')
        pairs=plan(connection,args.project,args.visitor)
        connection.executemany('insert or ignore into pc_resource_owners(kind,resource_id,visitor_id) values(?,?,?)',
            [(kind,rid,args.visitor) for kind,rid in pairs])
    print('已完成明确指定作品的本地映射；未修改作品内容。')


if __name__=='__main__':main()

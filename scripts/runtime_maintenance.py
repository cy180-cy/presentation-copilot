"""Offline maintenance only. Backups include PRIVATE data; never publish them."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'backend'),str(ROOT/'scripts')]
from services.runtime_config import load, no_links
from managed_server import DataLock
from release_files import EXAMPLE_FILES


def stopped(root):
    record=root/'run/server.json'
    if record.exists() and not json.loads(record.read_text(encoding='utf8')).get('stopped'):
        raise RuntimeError('先通过受管入口停止已核实服务；不能在线复制私有数据。')
    path=root/'data/database.db'
    if path.exists():
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as con:
            if con.execute("select count(*) from tasks where status in ('PENDING','PROCESSING','RUNNING','QUEUED')").fetchone()[0]:
                raise RuntimeError('有未核对任务，拒绝备份/迁移。')


def backup(root, destination):
    with DataLock(root/'run'):
        return backup_locked(root, destination)


def backup_locked(root, destination, extra_files=()):
    """Caller holds DataLock throughout backup and any following migration."""
    root=no_links(root);destination=no_links(destination)
    if destination.exists() or (destination.is_relative_to(root) and not destination.is_relative_to(root/'backups')):
        raise ValueError('备份必须是新目录；运行目录内仅允许backups子目录。')
    stopped(root);destination.mkdir(parents=True)
    dbpath=root/'data/database.db'
    if not dbpath.exists():raise ValueError('数据库不存在。')
    (destination/'data').mkdir()
    with sqlite3.connect(dbpath.as_uri()+'?mode=ro',uri=True) as src, sqlite3.connect(destination/'data/database.db') as dst:
        src.backup(dst)
    paths = [p for folder in ('uploads','run','examples') for p in (root/folder).rglob('*')]
    for relative in extra_files:
        p=no_links(root/relative)
        if not p.is_relative_to(root):raise ValueError('额外证据路径越界。')
        paths.append(p)
    for path in paths:
        no_links(path)
        if not path.is_file() or path.name in ('service.lock','server.json','stop.request'):continue
        target=destination/path.relative_to(root);target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,target)
    files=[{'path':p.relative_to(destination).as_posix(),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
           for p in destination.rglob('*') if p.is_file()]
    (destination/'backup-manifest.json').write_text(json.dumps({'private':True,'files':files}),encoding='utf8')
    return len(files)


def restore(source, destination):
    source=no_links(source);destination=no_links(destination)
    if destination.exists() or destination==source or destination.is_relative_to(source):raise ValueError('只恢复到独立新目录，不覆盖原运行库。')
    manifest=json.loads((source/'backup-manifest.json').read_text())
    validated=[]
    for row in manifest['files']:
        relative=Path(row['path'])
        if relative.is_absolute() or '..' in relative.parts:raise ValueError('备份路径越界。')
        p=no_links(source/relative)
        if not p.is_relative_to(source) or hashlib.sha256(p.read_bytes()).hexdigest()!=row['sha256']:raise ValueError('备份校验失败。')
        validated.append((p,relative))
    destination.mkdir(parents=True)
    for p,relative in validated:
        target=destination/relative;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,target)
    with sqlite3.connect(destination/'data/database.db') as con:
        if con.execute('pragma integrity_check').fetchone()[0]!='ok':raise ValueError('恢复数据库完整性检查失败。')
    return len(validated)


def retention(root):
    # Candidates only: no mutation, no budget/idempotency/works expiry rules.
    result=[]
    for folder,days in [('tmp',7),('logs',30),('cache',30)]:
        for p in (root/folder).rglob('*'):
            no_links(p)
            if p.is_file() and time.time()-p.stat().st_mtime>days*86400:
                result.append({'path':str(p.relative_to(root)),'bytes':p.stat().st_size,'older_than_days':days})
    return result


def import_examples(root, source):
    source=no_links(source)
    with DataLock(root/'run'):
        stopped(root)
        destination=root/'examples/water-saving'
        for name in EXAMPLE_FILES:
            p=no_links(source/name)
            if not p.is_file():raise ValueError('公共示例不完整：'+name)
        if destination.exists():raise ValueError('示例已存在；不覆盖此前审核的成品。')
        destination.mkdir(parents=True)
        for name in EXAMPLE_FILES:shutil.copy2(source/name,destination/name)
    return 4


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['backup','restore','retention-dry-run','import-examples'])
    parser.add_argument('--runtime-config',required=True)
    parser.add_argument('--source');parser.add_argument('--destination')
    args=parser.parse_args();cfg=load(args.runtime_config,ROOT,enforce_authorization=False);root=Path(cfg['data_dir'])
    if args.action=='backup':result=backup(root,Path(args.destination))
    elif args.action=='restore':
        target=no_links(args.destination)
        if target==root or target.is_relative_to(root):raise ValueError('禁止恢复到当前真实运行目录。')
        result=restore(Path(args.source),target)
    elif args.action=='import-examples':result=import_examples(root,Path(args.source))
    else:result=retention(root)
    print(json.dumps({'action':args.action,'result':result},ensure_ascii=False))


if __name__=='__main__':main()

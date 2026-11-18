"""Explicit release allowlist; dry-run by default. Never archive the runtime tree."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'backend'))
from services.runtime_config import no_links

EXAMPLE_FILES = ('page1.png', 'page2.png', 'example.pptx', 'example.pdf')
ROOT_FILES = {'README.md','LICENSE','AGENTS.md','.gitignore','.env.copilot.example',
              'pyproject.toml','uv.lock','CLA.md','CONTRIBUTING.md'}
SOURCE_DIRS = {'backend','frontend','scripts','deploy','tests','validation'}
SUFFIXES = {'.py','.ts','.tsx','.js','.mjs','.cjs','.json','.toml','.txt','.md','.html',
            '.css','.scss','.sql','.ini','.mako','.yaml','.yml','.svg','.png','.jpg','.webp','.ico','.example'}
DENIED_PARTS = {'.git','node_modules','.venv','__pycache__','uploads','instance',
                'runtime','artifacts','dist','build','.cache'}
# Reviewed additions: include working-tree bytes without staging or committing.
EXTRA_SOURCE_FILES = {
    'scripts/source_offer.py', 'tests/test_source_offer.py', 'docs/SOURCE_OFFER.md',
    'frontend/src/components/shared/SourceOffer.tsx',
    'frontend/public/open-source/source-offer.json',
    'frontend/public/open-source/THIRD_PARTY_NOTICES.txt',
    'backend/services/managed_resources.py', 'tests/test_public_resources.py',
    'backend/services/budget_cycles.py', 'backend/services/image_retry.py',
    'backend/services/ledger_settlement.py', 'backend/services/manual_balance.py',
    'scripts/cycle_maintenance.py', 'scripts/image_retry_maintenance.py',
    'scripts/ledger_maintenance.py', 'tests/test_budget_cycles.py',
    'tests/test_cycle_guards.py', 'tests/test_cycle_revision.py',
    'tests/test_genai_base64_compatibility.py', 'tests/test_historical_quality.py',
    'tests/test_ledger_settlement.py', 'tests/test_managed_bootstrap_no_retry.py',
    'tests/test_manual_balance.py', 'tests/test_settlement_guards.py',
    'tests/test_single_image_retry.py', 'tests/test_release_package.py',
}


def source_allowed(name):
    p=Path(name)
    if not name or '\\' in name or ':' in name or p.is_absolute() or '..' in p.parts or any(x in DENIED_PARTS for x in p.parts): return False
    # Local/private runtime configuration must never be a public source artifact,
    # even if it was accidentally tracked. Only the reviewed server example ships.
    if name.startswith('deploy/runtime.') and name != 'deploy/runtime.server.example.json': return False
    if name in ROOT_FILES: return True
    if name in ('docs/PROJECT_PLAN.md','docs/DEPLOYMENT.md','docs/ADMIN.md','docs/SOURCE_OFFER.md'): return True
    return (p.parts[0] in SOURCE_DIRS and p.suffix in SUFFIXES and
            not any(x.startswith('.env') for x in p.parts) and
            not re.search(r'(?i)(credential|cookie|private.key|\.pem$|\.log$)',name))


def git(root, *args):
    return subprocess.check_output(['git', '-c', 'safe.directory='+root.as_posix(), *args], cwd=root).decode().strip('\n')


def source_names(root):
    tracked=set(filter(None,git(root,'ls-files','--cached','-z').split('\0')))
    untracked=set(filter(None,git(root,'ls-files','--others','--exclude-standard','-z').split('\0')))
    unknown=sorted(n for n in untracked if source_allowed(n) and n not in EXTRA_SOURCE_FILES)
    if unknown: raise ValueError('需先审查并明确纳入的新文件：'+', '.join(unknown))
    return sorted(n for n in tracked | EXTRA_SOURCE_FILES if source_allowed(n))


def frontend_evidence(root, names):
    """Reuse the already validated build; reject visibly newer inputs or stale public copies.

    Timestamps are a freshness check, not a reproducible-build attestation. Source
    and output hashes in the release manifest identify this exact snapshot.
    """
    build=root/'.copilot/frontend-dist'
    built=min((build/'index.html').stat().st_mtime_ns,
              (build/'.vite/manifest.json').stat().st_mtime_ns)
    inputs=[n for n in names if n.startswith('frontend/')]
    newer=[n for n in inputs if (root/n).stat().st_mtime_ns>built]
    if newer: raise ValueError('前端输入晚于构建，请用受管配置重新构建：'+', '.join(newer))
    for name in inputs:
        if name.startswith('frontend/public/'):
            target=no_links(build/name.removeprefix('frontend/public/'))
            if not target.is_file() or target.read_bytes()!=(root/name).read_bytes():
                raise ValueError('前端public构建副本不匹配：'+name)
    digest=hashlib.sha256()
    for name in inputs:
        digest.update(name.encode()+b'\0'+hashlib.sha256((root/name).read_bytes()).digest())
    return {'method':'existing validated build + input timestamps + exact public byte comparison',
            'source_sha256':digest.hexdigest(), 'input_files':len(inputs),
            'build_manifest_sha256':hashlib.sha256((build/'.vite/manifest.json').read_bytes()).hexdigest()}


def collect(root=ROOT):
    root=no_links(root)
    names=source_names(root)
    entries={name:root/name for name in names if name and source_allowed(name)}
    build=root/'.copilot/frontend-dist'
    if not (build/'index.html').is_file(): raise ValueError('缺少个人版前端构建。')
    manifest_path=build/'.vite/manifest.json'
    if not manifest_path.is_file():raise ValueError('缺少本次Vite manifest，不能将旧构建混入发布。')
    frontend_evidence(root,names)
    build_manifest=json.loads(manifest_path.read_text())
    index=(build/'index.html').read_text(encoding='utf8')
    entry=build_manifest.get('index.html',{})
    if not entry.get('file') or any(asset not in index for asset in [entry['file'],*entry.get('css',[])]):
        raise ValueError('前端index与构建manifest不匹配。')
    build_names={'index.html','.vite/manifest.json'}
    for item in build_manifest.values():
        build_names.add(item['file']);build_names.update(item.get('css',[]));build_names.update(item.get('assets',[]))
    for name in names:
        if name.startswith('frontend/public/') and source_allowed(name):build_names.add(name.removeprefix('frontend/public/'))
    for name in build_names:
        p=no_links(build/name)
        if not p.is_relative_to(build) or not p.is_file():raise ValueError('构建资源缺失或越界：'+name)
        entries[p.relative_to(root).as_posix()]=p
    for name in EXAMPLE_FILES:
        p=root/'.copilot/examples/water-saving'/name
        if not p.is_file(): raise ValueError('缺少审核后的公共示例：'+name)
        entries['public-examples/water-saving/'+name]=p
    if not all(x in entries for x in ('LICENSE','scripts/managed_server.py','deploy/requirements.txt')):
        raise ValueError('发布所需源码或依赖清单缺失。')
    # Compare exact local secret values, without printing values or lengths.
    from dotenv import dotenv_values
    secrets=[v.encode() for k,v in dotenv_values(root/'.env.copilot',interpolate=False).items()
             if v and any(t in k.upper() for t in ('KEY','TOKEN','PASSWORD','SECRET'))]
    result=[]
    for name,p in sorted(entries.items()):
        no_links(p)
        if not p.is_relative_to(root): raise ValueError('发布源越界：'+name)
        data=p.read_bytes()
        if len(data)>30*1024*1024: raise ValueError('发布文件异常大：'+name)
        if any(secret in data for secret in secrets) or re.search(rb'(?m)^-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----\s*$',data):
            raise ValueError('疑似凭据，仅报告路径：'+name)
        result.append({'path':name,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()})
    return entries,result


def write_archive(archive, entries, manifest, provenance):
    with zipfile.ZipFile(archive,'x',compression=zipfile.ZIP_DEFLATED) as z:
        for row in manifest:
            data=entries[row['path']].read_bytes()
            if len(data)!=row['bytes'] or hashlib.sha256(data).hexdigest()!=row['sha256']:
                raise ValueError('打包期间文件改变，不能发布：'+row['path'])
            z.writestr(row['path'],data)
        z.writestr('release-manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2))
        z.writestr('release-provenance.json',json.dumps(provenance,ensure_ascii=False,indent=2))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',default='.copilot/releases/manifest.json')
    parser.add_argument('--archive',help='Optional new local zip; default is manifest only')
    args=parser.parse_args()
    entries,manifest=collect()
    provenance={'head':git(ROOT,'rev-parse','HEAD'), 'branch':git(ROOT,'branch','--show-current'),
                'source':'current working-tree bytes, including reviewed untracked additions; not a clean commit',
                'frontend':frontend_evidence(ROOT,source_names(ROOT))}
    target=no_links(ROOT/args.output)
    if not target.is_relative_to(ROOT/'.copilot/releases') or target.exists(): raise ValueError('清单须为忽略的发布目录内新文件。')
    target.parent.mkdir(parents=True,exist_ok=True)
    if args.archive:
        archive=no_links(ROOT/args.archive)
        if not archive.is_relative_to(ROOT/'.copilot/releases') or archive.exists(): raise ValueError('压缩包须为发布目录内新文件。')
        write_archive(archive,entries,manifest,provenance)
        archive_digest=hashlib.sha256(archive.read_bytes()).hexdigest()
        archive.with_suffix('.zip.sha256').write_text(archive_digest+'  '+archive.name+'\n',encoding='ascii')
    target.write_text(json.dumps({'files':manifest,'dry_run':not bool(args.archive),
                                 'provenance':provenance},ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({'files':len(manifest),'bytes':sum(x['bytes'] for x in manifest),
                      'manifest':str(target),'archive_created':bool(args.archive)},ensure_ascii=False))


if __name__=='__main__':main()

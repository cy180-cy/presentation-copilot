"""Prepare a separate public-source candidate. Offline, allowlisted, dry-run by default.

Never imports the app, reads runtime trees/credentials, or reuses a deployment ZIP.
Automated checks assist review; they do not certify that all private data is absent.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import zipfile
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = {'README.md', 'LICENSE', 'CLA.md', 'CONTRIBUTING.md',
              'pyproject.toml', 'uv.lock', '.gitignore', '.env.copilot.example'}
DOCS = {'docs/SOURCE_OFFER.md', 'validation/offline_guard.py'}
DEPLOY = {'deploy/requirements.txt', 'deploy/runtime.server.example.json',
          'deploy/presentation-copilot.service.example', 'deploy/nginx.conf.example'}
EXTRA = {'scripts/source_offer.py', 'tests/test_source_offer.py', 'docs/SOURCE_OFFER.md',
         'frontend/src/components/shared/SourceOffer.tsx',
         'frontend/public/open-source/source-offer.json',
         'frontend/public/open-source/THIRD_PARTY_NOTICES.txt'}
DENIED = {'.git', '.copilot', '.venv', 'node_modules', '__pycache__', 'instance',
          'uploads', 'exports', 'logs', 'backups', 'runtime', 'artifacts',
          'dist', 'build', '.cache', '.pytest_cache', 'test-results',
          'data', 'sessions', 'accounts', 'cache'}
TEXT = {'.py', '.ts', '.tsx', '.js', '.mjs', '.cjs', '.json', '.toml', '.txt',
        '.md', '.html', '.css', '.scss', '.sql', '.ini', '.mako', '.yaml', '.yml', '.sh'}
ASSETS = {'.svg', '.png', '.jpg', '.jpeg', '.webp', '.ico', '.ttf', '.woff', '.woff2'}
# This exact isolated fixture uses a fake key to test redaction. Any byte change
# requires fresh review; no runtime file or general key pattern is exempt.
SYNTHETIC_FIXTURES = {'tests/test_task_tracking.py': '9e87566d95fb6304ce0e88bd11b048aaac7f6a538aa1545134839ad1e1679d6d'}
SENSITIVE = re.compile(rb'(?m)^-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|'
                       rb'\bsk-[A-Za-z0-9_-]{20,}|\bAKIA[0-9A-Z]{16}\b|'
                       rb'(?i:Bearer)[ \t]+[A-Za-z0-9_./+=-]{24,}')


def allowed(name):
    p = Path(name)
    if not name or '\\' in name or ':' in name or p.is_absolute() or '..' in p.parts:
        return False
    if any(part in DENIED for part in p.parts):
        return False
    # The frontend history components are source code; other history directories may store runtime data.
    if 'history' in p.parts and p.parts[:4] != ('frontend', 'src', 'components', 'history'):
        return False
    # Upstream process-restarting E2E fixtures carry credential-shaped mocks;
    # they are not needed to build/run the offered application or isolated tests.
    if name.startswith('frontend/e2e/'):
        return False
    if name in ROOT_FILES | DOCS | DEPLOY:
        return True
    if any(part.startswith('.env') for part in p.parts):
        return False
    if any(word in p.name.lower() for word in ('credential', 'cookie', 'private', 'ledger.json', 'call-budget')):
        return False
    if p.suffix.lower() == '.json':
        return (name in {'frontend/package.json', 'frontend/package-lock.json',
                         'frontend/tsconfig.json', 'frontend/tsconfig.node.json',
                         'frontend/public/open-source/source-offer.json',
                         'scripts/visitor-limits.example.json'} or
                name.startswith('frontend/src/locales/'))
    if p.suffix.lower() in TEXT:
        return p.parts[0] in {'backend', 'frontend', 'scripts', 'tests'}
    return (p.suffix.lower() in ASSETS and
            name.startswith(('frontend/src/assets/', 'frontend/public/', 'backend/fonts/')))


def git(root, *args):
    return subprocess.check_output(['git', '-c', 'safe.directory='+root.as_posix(), *args],
                                   cwd=root).decode('utf8').strip('\n')


def check_template(name, data):
    if name == '.env.copilot.example':
        for line in data.decode('utf8').splitlines():
            if '=' not in line or line.lstrip().startswith('#'):
                continue
            key, value = line.split('=', 1)
            if key.strip().endswith(('_KEY', '_PASSWORD', '_TOKEN', '_SECRET')) and value.strip() not in ('', "''", '""'):
                raise ValueError('Example credential fields must be empty')
    if name == 'deploy/runtime.server.example.json':
        cfg = json.loads(data)
        if cfg.get('authorization') is not None or cfg.get('mode') != 'preview' or not (urlsplit(cfg.get('public_origin', '')).hostname or '').endswith('.invalid'):
            raise ValueError('Server example must remain a placeholder with no authorization')


def linked(path):
    return path.is_symlink() or (path.exists() and bool(getattr(path.lstat(), 'st_file_attributes', 0) & 0x400))


def collect(root=ROOT):
    root = root.resolve()
    tracked = set(filter(None, git(root, 'ls-files', '-z').split('\0')))
    untracked = set(filter(None, git(root, 'ls-files', '--others', '--exclude-standard', '-z').split('\0')))
    unknown = sorted(n for n in untracked if allowed(n) and n not in EXTRA)
    if unknown:
        raise ValueError('Review new source paths before inclusion: '+', '.join(unknown))
    names = sorted(n for n in tracked | EXTRA if allowed(n))
    rows = []
    for name in names:
        path = root/name
        if any(linked(p) for p in (path, *path.parents)) or not path.resolve().is_relative_to(root):
            raise ValueError('Linked or escaped source path: '+name)
        data = path.read_bytes()
        if len(data) > 30*2**20 or (SENSITIVE.search(data) and
                SYNTHETIC_FIXTURES.get(name) != hashlib.sha256(data.replace(b'\r\n', b'\n')).hexdigest()):
            raise ValueError('Review potentially sensitive/oversized source: '+name)
        check_template(name, data)
        rows.append({'path': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
    required = {'LICENSE', 'frontend/package-lock.json', 'frontend/vite.copilot.config.ts',
                'scripts/presentation_copilot.py', 'scripts/runtime_maintenance.py',
                'backend/services/managed_resources.py', 'deploy/requirements.txt',
                'frontend/public/open-source/THIRD_PARTY_NOTICES.txt'}
    if not required.issubset(names):
        raise ValueError('Necessary public source files are missing')
    return rows


def output_path(root, value):
    path = Path(value).absolute()
    parent = (root/'.copilot/source-offer').resolve()
    if (any(linked(p) for p in (path, *path.parents)) or not parent.is_relative_to(root.resolve()) or
            not path.resolve().is_relative_to(parent)):
        raise ValueError('Output must be a new file under .copilot/source-offer')
    if path.exists():
        raise ValueError('Existing output is never overwritten')
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def archive(root, target, rows, version):
    """Explicit opt-in only. Every byte must still match the reviewed manifest."""
    created = False
    try:
        with zipfile.ZipFile(target, 'x', compression=zipfile.ZIP_DEFLATED) as z:
            created = True
            for row in rows:
                data = (root/row['path']).read_bytes()
                if hashlib.sha256(data).hexdigest() != row['sha256']:
                    raise ValueError('Source changed after review: '+row['path'])
                z.writestr(row['path'], data)
            z.writestr('source-manifest.json', json.dumps({'version': version, 'files': rows}, indent=2))
    except BaseException:
        if created:
            target.unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', help='Write a local review manifest; no archive by default')
    parser.add_argument('--archive', help='Explicit local-only public-source candidate ZIP')
    parser.add_argument('--version', help='Version/commit label of the source offered')
    args = parser.parse_args()
    if args.archive and (not args.version or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}', args.version)):
        parser.error('--archive requires an explicit --version')
    rows = collect()
    if args.plan:
        dest = output_path(ROOT, args.plan)
        with dest.open('x', encoding='utf8') as f:
            json.dump({'baseline': git(ROOT, 'rev-parse', 'HEAD'),
                       'working_tree_snapshot': True, 'public_release_approved': False,
                       'files': rows}, f, ensure_ascii=False, indent=2)
    if args.archive:
        archive(ROOT, output_path(ROOT, args.archive), rows, args.version)
    print(json.dumps({'files': len(rows), 'bytes': sum(r['bytes'] for r in rows),
                      'archive_written': bool(args.archive), 'published': False}))


if __name__ == '__main__':
    main()

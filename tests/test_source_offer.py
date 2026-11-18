"""Public source privacy boundaries, using synthetic files only; no app or network."""
import hashlib
import json
from pathlib import Path
import sys
import zipfile
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from source_offer import allowed, archive, output_path, SENSITIVE, check_template


@pytest.mark.parametrize('name', [
    '.env', '.env.copilot', 'deploy/runtime.local-preview.json', 'deploy/runtime.real.json',
    '.copilot/run/visitor-signing.key', 'backend/instance/database.db',
    'backend/uploads/old-version.png', 'frontend/node_modules/a/index.js',
    'docs/PROJECT_PLAN.md', 'docs/DEPLOYMENT.md', 'docs/ADMIN.md',
    'logs/calls.json', 'backups/data.zip', 'backend/private-key.txt', '../README.md',
    'backend/secrets.pem', 'backend/exports/test.pdf', 'frontend/public/user.db',
    'backend/data/calls.json', 'frontend/public/invoice.json', 'scripts/ledger.json',
    'backend/history/visitor-records.txt', 'frontend/public/history/visitor-records.txt',
])
def test_private_paths_are_excluded(name):
    assert not allowed(name)


@pytest.mark.parametrize('name', ['backend/services/managed_resources.py',
    'frontend/src/components/shared/SourceOffer.tsx', 'frontend/package-lock.json',
    'frontend/src/components/history/ProjectCard.tsx',
    'deploy/runtime.server.example.json', '.env.copilot.example', 'docs/SOURCE_OFFER.md',
    'frontend/public/open-source/THIRD_PARTY_NOTICES.txt'])
def test_needed_source_and_only_explicit_examples_are_kept(name):
    assert allowed(name)


def test_known_secret_shapes_are_refused():
    assert SENSITIVE.search(b'sk-'+b'X'*32)
    assert SENSITIVE.search(b'-----BEGIN '+b'PRIVATE KEY-----')
    assert not SENSITIVE.search(b'TEXT_API_KEY=\n')


def test_examples_must_not_contain_real_credentials_or_authorizations():
    check_template('.env.copilot.example', b'TEXT_API_KEY=\n')
    with pytest.raises(ValueError): check_template('.env.copilot.example', b'TEXT_API_KEY=not-empty')
    cfg = {'mode': 'preview', 'public_origin': 'https://REPLACE_DOMAIN.invalid', 'authorization': None}
    check_template('deploy/runtime.server.example.json', json.dumps(cfg).encode())
    cfg['authorization'] = {'credits': 100}
    with pytest.raises(ValueError): check_template('deploy/runtime.server.example.json', json.dumps(cfg).encode())


def test_archive_keeps_reviewed_bytes_and_refuses_changed_files(tmp_path):
    source = tmp_path/'LICENSE'; source.write_bytes(b'original notice')
    rows = [{'path': 'LICENSE', 'bytes': source.stat().st_size,
             'sha256': hashlib.sha256(source.read_bytes()).hexdigest()}]
    target = tmp_path/'source.zip'
    archive(tmp_path, target, rows, 'test-1')
    with zipfile.ZipFile(target) as z:
        assert z.read('LICENSE') == b'original notice'
        assert json.loads(z.read('source-manifest.json'))['version'] == 'test-1'
    before = target.read_bytes()
    with pytest.raises(FileExistsError): archive(tmp_path, target, rows, 'test-1')
    assert target.read_bytes() == before
    source.write_bytes(b'changed')
    with pytest.raises(ValueError): archive(tmp_path, tmp_path/'bad.zip', rows, 'test-1')
    assert not (tmp_path/'bad.zip').exists()


def test_output_is_new_and_stays_under_local_candidate_directory(tmp_path):
    with pytest.raises(ValueError): output_path(tmp_path, tmp_path/'public.zip')
    target = output_path(tmp_path, tmp_path/'.copilot/source-offer/candidate.zip')
    target.write_bytes(b'keep')
    with pytest.raises(ValueError): output_path(tmp_path, target)
    assert target.read_bytes() == b'keep'

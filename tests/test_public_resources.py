"""Public-ready guards: real Flask routes, isolated DB/files, sockets disabled."""
import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from test_admin_console import app, admin, admin_client, login, apply
from test_copilot_capabilities import isolated, png
from test_manual_balance import draft
from test_task_tracking import queued
from test_visitor_isolation import client, project, page, call, owner
from services import visitor_access as va, managed_resources as mr, manual_balance as mb
from services.ai_providers import copilot_limits as budget


@pytest.fixture(autouse=True)
def ready(app, admin, monkeypatch, tmp_path):
    upload = tmp_path / 'uploads'
    upload.mkdir()
    monkeypatch.setitem(app.config, 'UPLOAD_FOLDER', str(upload))
    app.extensions.pop('pc_request_frequency', None)
    manager = login(admin_client(app, admin))
    config = draft(200)
    config['authorization'].update(validity='daily', expires_at=None)
    apply(manager, config)
    # Test the disk floor deterministically, independent of workstation capacity.
    monkeypatch.setattr(mr.shutil, 'disk_usage', lambda _: SimpleNamespace(free=100 * 2**30))
    yield manager
    assert not mr._pending
    assert va.WORKERS_IN_USE == 0 and not va.FOREGROUND_IN_USE


def test_failed_new_requests_rate_limited_without_success_charge(app, queued):
    cap = app.config['PC_VISITOR_LIMITS']
    cap.update(visitor_requests=2, global_requests=10)
    c = client(app); pid = project(c); pg, _ = page(app, c, pid)
    queued[0].image_provider.fail = True
    with app.app_context(): before = mb.counts(budget.budget_state())
    key = 'same-failure-request-001'
    assert call(c, pid, pg, key).status_code == 202
    queued[3]()
    assert call(c, pid, pg, key).status_code == 202
    assert not queued[2] and len(queued[1]) == 1
    assert call(c, pid, pg).status_code == 202
    queued[3]()
    response = call(c, pid, pg)
    assert response.status_code == 429
    assert response.json['error']['code'] == 'REQUEST_RATE_LIMIT'
    with app.app_context(): assert mb.counts(budget.budget_state()) == before
    assert len(queued[1]) == 2
    assert c.get('/api/projects').status_code == 200
    assert c.get('/api/visitor/example').status_code != 429


def test_global_frequency_survives_new_identity_and_apply(app, queued, ready):
    app.config['PC_VISITOR_LIMITS'].update(visitor_requests=10, global_requests=1)
    a = client(app); pa = project(a); pg, _ = page(app, a, pa)
    queued[0].image_provider.fail = True
    assert call(a, pa, pg).status_code == 202
    queued[3]()
    config = draft(200)
    config['authorization'].update(validity='daily', expires_at=None)
    apply(ready, config)
    assert len(app.extensions['pc_request_frequency']) == 1
    app.config['PC_VISITOR_LIMITS'].update(visitor_requests=10, global_requests=1)
    b = client(app); pb = project(b); pgb, _ = page(app, b, pb)
    assert call(b, pb, pgb).json['error']['code'] == 'REQUEST_RATE_LIMIT'
    assert len(queued[1]) == 1


def test_export_exclusion_and_light_reads_real_http(app, monkeypatch):
    from controllers.export_controller import ExportService
    c = client(app); pid = project(c); page(app, c, pid)
    entered, release = threading.Event(), threading.Event()
    def slow(paths, output_file, **kwargs):
        entered.set()
        assert release.wait(5)
        Path(output_file).write_bytes(b'offline-pptx')
    monkeypatch.setattr(ExportService, 'create_pptx_from_images', slow)
    def export():
        return c.get(f'/api/projects/{pid}/export/pptx')
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(export)
        assert entered.wait(5)
        try:
            other = client(app); po = project(other); page(app, other, po)
            assert other.get(f'/api/projects/{po}/export/pdf').status_code == 429
            assert other.get('/api/projects').status_code == 200
            assert c.get(f'/files/{pid}/pages/synthetic.png').status_code == 200
        finally:
            release.set()
        assert future.result().status_code == 200
    assert c.get(f'/files/{pid}/exports/presentation_{pid}.pptx').status_code == 200


def test_worker_export_race_and_exception_release(app):
    c = client(app); pid = project(c); page(app, c, pid)
    entered, release = threading.Event(), threading.Event()
    def work():
        entered.set()
        assert release.wait(5)
        raise RuntimeError('offline worker failure')
    with app.app_context(), va.scope(owner(app, pid)):
        wrapped = va.capture_worker(work)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(wrapped)
        assert entered.wait(5)
        try:
            assert c.get(f'/api/projects/{pid}/export/pdf').status_code == 429
            assert c.get('/api/projects').status_code == 200
        finally:
            release.set()
        with pytest.raises(RuntimeError): future.result()
    # Reverse order: worker cannot enter while a foreground export holds the slot.
    entered.clear(); release.set()
    with ThreadPoolExecutor(1) as pool:
        with app.app_context(), va.heavy_work():
            future = pool.submit(wrapped)
            assert not entered.wait(.05)
        assert entered.wait(5)
        with pytest.raises(RuntimeError): future.result()
    assert not va.FOREGROUND_IN_USE


def test_export_failure_and_actual_size_limit_keep_old_output(app, monkeypatch):
    from controllers.export_controller import ExportService
    c = client(app); pid = project(c); page(app, c, pid)
    dest = Path(app.config['UPLOAD_FOLDER']) / pid / 'exports' / f'presentation_{pid}.pdf'
    dest.parent.mkdir(); dest.write_bytes(b'old-valid-export')
    def failure(paths, output_file, **kwargs):
        Path(output_file).write_bytes(b'partial')
        raise RuntimeError('offline write failure')
    monkeypatch.setattr(ExportService, 'create_pdf_from_images', failure)
    assert c.get(f'/api/projects/{pid}/export/pdf').status_code == 500
    assert dest.read_bytes() == b'old-valid-export'
    def oversized(paths, output_file, **kwargs):
        Path(output_file).write_bytes(b'x' * 3 * 2**20)
    monkeypatch.setattr(ExportService, 'create_pdf_from_images', oversized)
    app.config['PC_VISITOR_LIMITS']['storage_bytes'] = 2 * 2**20
    response = c.get(f'/api/projects/{pid}/export/pdf')
    assert response.status_code == 413 and '存储空间不足' in response.json['error']['message']
    assert dest.read_bytes() == b'old-valid-export'
    assert not list(dest.parent.glob('.pc-*'))


def test_storage_counts_upload_image_versions_exports_and_restart(app):
    from services.file_service import FileService
    from werkzeug.datastructures import FileStorage
    c = client(app); pid = project(c); pg, _ = page(app, c, pid)
    with app.app_context(), va.scope(owner(app, pid)):
        fs = FileService(app.config['UPLOAD_FOLDER'])
        fs.save_template_image(FileStorage(stream=png(), filename='template.png'), pid)
        for version in (2, 3):
            fs.save_generated_image(Image.new('RGB', (10, 10)), pid, pg, version_number=version)
        export = fs._get_exports_dir(pid) / 'saved.pdf'
        with mr.output(export, 100) as temp: temp.write_bytes(b'x' * 100)
        expected = sum(p.stat().st_size for p in (Path(app.config['UPLOAD_FOLDER']) / pid).rglob('*') if p.is_file())
        assert mr.usage() == (expected, expected)
        # No cached byte counters to reset: a fresh disk scan produces same result.
        assert mr.usage() == (expected, expected)
        old = export.read_bytes()
        app.config['PC_VISITOR_LIMITS']['storage_bytes'] = expected
        with pytest.raises(mr.ResourceLimit):
            with mr.output(export, 1): pytest.fail('must reject before writing')
        assert export.read_bytes() == old


def test_global_storage_disk_floor_and_reservation_release(app):
    c = client(app); pid = project(c)
    root = Path(app.config['UPLOAD_FOLDER']); folder = root / pid; folder.mkdir(exist_ok=True)
    with app.app_context(), va.scope(owner(app, pid)):
        cap = app.config['PC_VISITOR_LIMITS']
        cap.update(storage_bytes=1000, service_storage_bytes=100)
        with mr.output(folder / 'first.bin', 80) as temp:
            with pytest.raises(mr.ResourceLimit):
                with mr.output(folder / 'second.bin', 30): pytest.fail('over reserved total')
            temp.write_bytes(b'one')
        assert mr.usage() == (3, 3)
        cap['storage_free_bytes'] = 101 * 2**30
        with pytest.raises(mr.ResourceLimit):
            with mr.output(folder / 'floor.bin', 1): pytest.fail('disk floor')
        cap['storage_free_bytes'] = 5 * 2**30
        with pytest.raises(KeyboardInterrupt):
            with mr.output(folder / 'interrupt.bin', 10) as temp:
                temp.write_bytes(b'partial'); raise KeyboardInterrupt
        assert not list(folder.glob('.pc-*'))


def test_image_storage_failure_before_provider_and_history_preserved(app, queued):
    c = client(app); pid = project(c); pg, _ = page(app, c, pid)
    app.config['PC_VISITOR_LIMITS']['storage_bytes'] = 2**20
    response = call(c, pid, pg)
    assert response.status_code == 202
    queued[3]()
    assert queued[1] == []
    from models import db, Task, PageImageVersion
    with app.app_context(), va.scope(owner(app, pid)):
        task = db.session.get(Task, response.json['data']['task_id'])
        assert task.status == 'FAILED' and '存储空间不足' in task.error_message
        assert PageImageVersion.query.count() == 1


def test_new_file_rollback_and_standalone_ownership(app):
    c = client(app); pid = project(c); who = owner(app, pid)
    root = Path(app.config['UPLOAD_FOLDER']); (root / 'materials').mkdir()
    dest = root / 'materials' / 'uncommitted.png'
    with app.app_context(), va.scope(who):
        with pytest.raises(RuntimeError):
            with mr.new_files_transaction():
                with mr.output(dest, 10) as temp: temp.write_bytes(b'new')
                assert mr.usage() == (3, 3)
                raise RuntimeError('offline DB commit failure')
        assert not dest.exists() and mr.usage() == (0, 0)


def test_upload_http_quota_and_image_success_share_storage(app, queued):
    c = client(app); pid = project(c); pg, _ = page(app, c, pid)
    response = c.post(f'/api/projects/{pid}/materials/upload',
                      data={'file': (png('green'), 'material.png')})
    assert response.status_code in (200, 201), response.json
    with app.app_context(), va.scope(owner(app, pid)):
        before = mr.usage()[0]
    assert call(c, pid, pg).status_code == 202
    queued[3]()
    from models import db, PageImageVersion
    with app.app_context(), va.scope(owner(app, pid)):
        assert PageImageVersion.query.count() == 2
        assert mr.usage()[0] > before
        before = mr.usage()[0]
    app.config['PC_VISITOR_LIMITS']['storage_bytes'] = before
    response = c.post(f'/api/projects/{pid}/materials/upload',
                      data={'file': (png('red'), 'over.png')})
    assert response.status_code == 413, response.json
    with app.app_context(), va.scope(owner(app, pid)):
        assert mr.usage()[0] == before


def test_upload_thumbnail_failure_removes_only_new_files(app, monkeypatch):
    from services.file_service import FileService
    c = client(app); pid = project(c); page(app, c, pid)
    root = Path(app.config['UPLOAD_FOLDER'])
    before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
    def failure(*args, **kwargs):
        raise mr.ResourceLimit('存储空间不足，请减少文件或联系管理员；已有作品仍可下载。')
    monkeypatch.setattr(FileService, 'save_user_template_thumbnail', failure)
    response = c.post('/api/user-templates', data={'template_image': (png(), 'template.png')})
    assert response.status_code >= 400
    assert {p: p.read_bytes() for p in root.rglob('*') if p.is_file()} == before


def test_generated_thumbnail_failure_preserves_old_version(app, queued, monkeypatch):
    from services.file_service import FileService
    from models import db, Task, PageImageVersion
    c = client(app); pid = project(c); pg, _ = page(app, c, pid)
    root = Path(app.config['UPLOAD_FOLDER']) / pid / 'pages'
    before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
    def failure(*args, **kwargs):
        raise mr.ResourceLimit('存储空间不足，请减少文件或联系管理员；已有作品仍可下载。')
    monkeypatch.setattr(FileService, 'save_cached_image', failure)
    response = call(c, pid, pg)
    assert response.status_code == 202
    queued[3]()
    with app.app_context(), va.scope(owner(app, pid)):
        assert db.session.get(Task, response.json['data']['task_id']).status == 'FAILED'
        assert PageImageVersion.query.count() == 1
    assert {p: p.read_bytes() for p in root.rglob('*') if p.is_file()} == before


@pytest.mark.parametrize('route', ['generate/outline', 'generate/outline/stream',
    'generate/from-description', 'generate/descriptions', 'generate/descriptions/stream',
    'generate/images', 'pages/{page}/generate/image', 'pages/{page}/generate/description',
    'pages/{page}/edit/image'])
def test_all_core_model_http_routes_share_frequency_guard(app, route):
    c = client(app); pid = project(c); pg, _ = page(app, c, pid)
    app.config['PC_VISITOR_LIMITS']['global_requests'] = 1
    with app.app_context(): assert mr.request_allowed()
    response = c.post(f'/api/projects/{pid}/' + route.format(page=pg), json={},
                      headers={'Idempotency-Key': 'fresh-request-key-001'})
    assert response.status_code == 429, response.json
    assert response.json['error']['code'] == 'REQUEST_RATE_LIMIT'


def test_storage_is_global_across_visitors_and_cannot_escape_root(app):
    a = client(app); pa = project(a)
    b = client(app); pb = project(b)
    root = Path(app.config['UPLOAD_FOLDER'])
    (root / pa).mkdir(); (root / pb).mkdir()
    with app.app_context(), va.scope(owner(app, pa)):
        with mr.output(root / pa / 'owned.bin', 10) as temp: temp.write_bytes(b'x' * 10)
    app.config['PC_VISITOR_LIMITS']['service_storage_bytes'] = 10
    with app.app_context(), va.scope(owner(app, pb)):
        assert mr.usage() == (0, 10)
        with pytest.raises(mr.ResourceLimit):
            with mr.output(root / pb / 'new.bin', 1): pytest.fail('global storage bypass')
        with pytest.raises(mr.ResourceLimit):
            with mr.output(root / '..' / 'escape.bin', 1): pytest.fail('path escape')


def test_release_collector_includes_guard_and_tests():
    from release_files import ROOT, source_names
    names = source_names(ROOT)
    assert 'backend/services/managed_resources.py' in names
    assert 'tests/test_public_resources.py' in names


def test_legacy_configuration_receives_protected_defaults(tmp_path):
    from services.runtime_config import DEFAULT, validate
    cfg = copy.deepcopy(DEFAULT)
    for field in mr.DEFAULTS: cfg['visitor'].pop(field)
    cfg.update(data_dir=str(tmp_path), secret_file=str(tmp_path / '.env.copilot'))
    result = validate(cfg, tmp_path, source=tmp_path / 'runtime.json')
    for field, value in mr.DEFAULTS.items(): assert result['visitor'][field] == value


def test_existing_budget_policy_accepts_new_protection_defaults(ready):
    current = budget.runtime_policy()
    legacy = copy.deepcopy(current)
    for field in mr.DEFAULTS:
        legacy['visitor'].pop(field)
    auth_id = current['authorization']['id']
    state = {'runtime_authorizations': {auth_id: {'policy': legacy}}}
    before = copy.deepcopy(state)

    budget.register_runtime_policy(state, current)
    assert state == before

    current['visitor']['global_requests'] += 1
    with pytest.raises(RuntimeError, match='授权条款已变化'):
        budget.register_runtime_policy(state, current)
    assert state == before

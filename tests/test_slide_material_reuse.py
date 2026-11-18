"""Existing file + material upload flow; isolated visitors/data, sockets disabled."""
import io
import json
from pathlib import Path
from test_visitor_isolation import app, isolated, trial, queued, client, project, page, owner
from services import visitor_access as va
from services.ai_providers import copilot_limits as budget


def test_selected_version_becomes_private_material_without_any_model(app, queued, monkeypatch):
    from models import db, Page, PageImageVersion, Material
    monkeypatch.setenv('PC_MODE', 'preview')
    a, b = client(app), client(app)
    pid = project(a)
    pg, version_id = page(app, a, pid)
    before = budget.budget_state()
    with app.app_context(), va.scope(owner(app, pid)):
        original_page = db.session.get(Page, pg).to_dict()
        versions = [v.to_dict() for v in PageImageVersion.query.filter_by(page_id=pg).all()]
    filename = versions[0]['image_path'].replace('\\', '/').rsplit('/', 1)[-1]
    source = f'/files/{pid}/pages/{filename}'
    original = a.get(source)
    assert original.status_code == 200 and b.get(source).status_code == 404
    r = a.post('/api/materials/upload', data={
        'file': (io.BytesIO(original.data), '第1页_离线模拟_v1.png')})
    assert r.status_code == 201, r.json
    material = r.json['data']
    assert material['project_id'] is None
    assert a.get(material['url']).data == original.data
    assert b.get(material['url']).status_code == 404
    assert material['id'] in json.dumps(a.get('/api/materials?project_id=all').json)
    assert material['id'] not in json.dumps(b.get('/api/materials?project_id=all').json)
    with app.app_context(), va.scope(owner(app, pid)):
        assert db.session.get(Page, pg).to_dict() == original_page
        assert [v.to_dict() for v in PageImageVersion.query.filter_by(page_id=pg).all()] == versions
        assert Material.query.one().caption is None
    assert queued[1] == [] and queued[2] == []
    assert budget.budget_state() == before
    original.close()  # Close the Flask download handle before Windows removes the source folder.
    assert a.delete(f'/api/projects/{pid}').status_code == 200
    assert a.get(material['url']).data == original.data
    assert b.get(material['url']).status_code == 404


def test_reuse_still_requires_owner_csrf_and_available_storage(app, queued, monkeypatch):
    from models import Material
    a, b = client(app), client(app)
    pid = project(a)
    pg, _ = page(app, a, pid)
    version = a.get(f'/api/projects/{pid}/pages/{pg}/image-versions').json['data']['versions'][0]
    filename = version['image_path'].replace('\\', '/').rsplit('/', 1)[-1]
    source = f'/files/{pid}/pages/{filename}'
    image = a.get(source).data
    def upload(c):
        return c.post(f'/api/projects/{pid}/materials/upload', data={'file': (io.BytesIO(image), 'slide.png')})
    assert upload(b).status_code == 404
    csrf = a.environ_base.pop('HTTP_X_PC_CSRF')
    assert upload(a).status_code == 403
    a.environ_base['HTTP_X_PC_CSRF'] = csrf
    monkeypatch.setitem(app.config, 'PC_VISITOR_LIMITS', dict(storage_bytes=1))
    assert upload(a).status_code == 413
    with app.app_context(), va.scope(owner(app, pid)):
        assert Material.query.count() == 0
    assert queued[1] == [] and queued[2] == []

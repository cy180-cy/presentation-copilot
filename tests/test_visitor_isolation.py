"""PC-008: independent cookie jars, isolated SQLite/budget, sockets disabled.
All images/providers/limits in this module are synthetic test data.
"""
import json
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest
from test_copilot_capabilities import app, isolated, png
from test_task_tracking import queued
from services import visitor_access as va
from services.ai_providers import copilot_limits as budget


@pytest.fixture(autouse=True)
def trial(app, monkeypatch):
    monkeypatch.setitem(app.config, 'PC_VISITOR_ENABLED', True)
    monkeypatch.setitem(app.config, 'PC_VISITOR_LIMITS', dict(text=2, image=2, credits=8,
        active_tasks=1, queue=2, new_visitors_per_hour=1000))


def client(app):
    c = app.test_client()
    response = c.get('/api/visitor/session')
    assert response.status_code == 200, response.json
    c.environ_base['HTTP_ORIGIN'] = 'http://localhost'
    c.environ_base['HTTP_X_PC_CSRF'] = response.json['data']['csrf']
    return c


def project(c):
    response = c.post('/api/projects', json={'creation_type': 'blank'})
    assert response.status_code == 201, response.json
    return response.json['data']['project_id']


def owner(app, project_id):
    from models import db, ResourceOwner
    with app.app_context():
        return db.session.get(ResourceOwner, ('projects', project_id)).visitor_id


def page(app, c, pid):
    from models import db, Page, PageImageVersion, Project
    response = c.post(f'/api/projects/{pid}/pages', json={'order_index': 0,
        'outline_content': {'title': '离线模拟', 'points': ['每周检查15处用水点']}})
    assert response.status_code == 201, response.json
    page_id = response.json['data']['page_id']
    with app.app_context(), va.scope(owner(app, pid)):
        p = db.session.get(Page, page_id)
        p.set_description_content({'text': '每周检查15处用水点'})
        db.session.get(Project, pid).template_style = '离线测试'
        folder = Path(app.config['UPLOAD_FOLDER']) / pid / 'pages'
        folder.mkdir(parents=True, exist_ok=True)
        image = folder / 'synthetic.png'; image.write_bytes(png().getvalue())
        p.generated_image_path = str(image.relative_to(app.config['UPLOAD_FOLDER']))
        p.status = 'COMPLETED'
        version = PageImageVersion(page_id=p.id, image_path=p.generated_image_path, version_number=1, is_current=True)
        db.session.add(version); db.session.commit()
        return page_id, version.id


def call(c, pid, page_id, key=None, body=None):
    key = key or secrets.token_hex(16)
    cache = getattr(c, '_revisions', {})
    if key not in cache:cache[key]=c.get(f'/api/projects/{pid}/input-revision').json['data']['revision']
    c._revisions=cache
    return c.post(f'/api/projects/{pid}/pages/{page_id}/generate/image',
        json=body or {'force_regenerate': True}, headers={'Idempotency-Key': key,'X-PC-Input-Version':cache[key]})


def test_two_cookie_jars_projects_and_private_history(app):
    from models import db, Project
    a, b = client(app), client(app)
    pa, pb = project(a), project(b)
    assert a.get_cookie(va.COOKIE).value != b.get_cookie(va.COOKIE).value
    for c, own, other in [(a, pa, pb), (b, pb, pa)]:
        body = c.get('/api/projects').get_data(as_text=True)
        assert own in body and other not in body
        assert c.get(f'/api/projects/{other}').status_code == 404
        assert c.put(f'/api/projects/{other}', json={'idea_prompt': 'attack'}).status_code == 404
        assert c.delete(f'/api/projects/{other}').status_code == 404
    with app.app_context():
        legacy = Project(creation_type='blank'); db.session.add(legacy); db.session.commit(); legacy_id=legacy.id
    assert a.get(f'/api/projects/{legacy_id}').status_code == 404
    with app.app_context():
        assert db.session.get(Project, legacy_id) is not None


def test_free_background_tasks_share_queue_and_worker_bound(app, monkeypatch):
    import threading
    from models import db, Task
    from services.task_manager import TaskManager
    a = client(app); p = project(a); who = owner(app, p)
    with app.app_context(), va.scope(who):
        first = Task(project_id=p, task_type='LOCAL_PDF', status='PROCESSING')
        second = Task(project_id=p, task_type='LOCAL_PDF', status='PENDING')
        db.session.add_all([first, second]); db.session.commit()
        manager = TaskManager(max_workers=1)
        called = []
        with pytest.raises(ValueError, match='任务较多'):
            manager.submit_task(second.id, lambda *_: called.append(True))
        assert called == [] and db.session.get(Task, second.id).status == 'FAILED'
        assert db.session.get(Task, first.id).status == 'PROCESSING'
        manager.shutdown()
        lock = threading.Lock(); running = 0; peak = 0
        def work():
            nonlocal running, peak
            with lock:
                running += 1; peak = max(peak, running)
            assert va.identity() == who
            time.sleep(.02)
            with lock: running -= 1
        wrapped = va.capture_worker(work)
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(lambda _: wrapped(), range(3)))
        assert peak == 1 and va.WORKERS_IN_USE == 0


def test_pages_versions_files_parent_mixing(app):
    a,b=client(app),client(app); pa,pb=project(a),project(b)
    pg, ver=page(app,a,pa)
    for url in [f'/api/projects/{pa}/pages/{pg}/image-versions', f'/files/{pa}/pages/synthetic.png',
                f'/api/projects/{pa}/task-details?page_id={pg}&version_id={ver}']:
        assert a.get(url).status_code == 200
        assert b.get(url).status_code == 404
    pc=project(a)
    assert a.put(f'/api/projects/{pc}/pages/{pg}/description',json={'text':'attack'}).status_code == 404
    assert b.post(f'/api/projects/{pb}/pages/{pg}/image-versions/{ver}/set-current',json={}).status_code == 404
    assert b.get(f'/uploads/{pa}/pages/synthetic.png').status_code == 403


def test_cookie_csrf_owner_and_admin(app):
    a=client(app)
    assert a.post('/api/projects',json={'creation_type':'blank','owner_id':'forged'}).status_code == 400
    assert a.post('/api/projects',json={'creation_type':'blank'},headers={'Origin':'https://other.example'}).status_code == 403
    assert a.post('/api/projects',json={'creation_type':'blank'},headers={'X-PC-CSRF':'forged'}).status_code == 403
    for route in ['/api/settings', '/api/settings/active-config', '/api/openai-oauth/status']:
        assert a.get(route).status_code == 403
    a.set_cookie(va.COOKIE,'tampered')
    assert a.get('/api/projects').status_code == 401


def test_preferences_are_per_visitor_and_global_unchanged(app):
    from models import Settings
    a,b=client(app),client(app)
    before=b.get('/api/product/preferences').json['data']
    assert a.put('/api/product/preferences',json={'output_language':'ja'}).status_code == 200
    assert a.get('/api/product/preferences').json['data']['output_language']=='ja'
    assert b.get('/api/product/preferences').json['data']==before
    assert a.put('/api/product/preferences',json={'api_key':'forged'}).status_code == 400


def test_template_material_reference_acl(app):
    from models import db, Material, ReferenceFile, Task, UserStyleTemplate
    a,b=client(app),client(app); pa=project(a); pb=project(b)
    template=a.post('/api/user-templates',data={'template_image':(png(),'test.png')},content_type='multipart/form-data')
    assert template.status_code==200,template.json
    info=template.json['data']; tid=info['template_id']
    assert b.get(info['template_image_url']).status_code==404
    assert tid not in b.get('/api/user-templates').get_data(as_text=True)
    assert b.delete(f'/api/user-templates/{tid}').status_code==404
    with app.app_context(),va.scope(owner(app,pa)):
        material=Material(filename='private.png',relative_path='materials/private.png',url='/files/materials/private.png')
        ref=ReferenceFile(filename='test.pdf',file_path='private/test.pdf',file_size=1,file_type='pdf')
        task=Task(project_id=pa,task_type='OFFLINE_TEST',status='FAILED',error_message='C:\\private\\secret')
        db.session.add_all([material,ref,task]);db.session.commit(); mid,fid,taskid=material.id,ref.id,task.id
    assert b.get('/files/materials/private.png').status_code==404
    assert b.post('/api/materials/download',json={'material_ids':[mid]}).status_code==404
    assert b.get(f'/api/reference-files/{fid}').status_code==404
    assert b.post(f'/api/reference-files/{fid}/associate',json={'project_id':pb}).status_code==404
    assert b.get(f'/api/projects/{pb}/tasks/{taskid}').status_code==404
    assert 'private' not in a.get(f'/api/projects/{pa}/tasks/{taskid}').get_data(as_text=True)


def test_preview_and_unconfigured_quota_no_task(app,queued,monkeypatch):
    a=client(app);p=project(a);pg,_=page(app,a,p)
    monkeypatch.setenv('PC_MODE','preview')
    assert call(a,p,pg).json['error']['code']=='PREVIEW'
    monkeypatch.setenv('PC_MODE','live');monkeypatch.setitem(app.config,'PC_VISITOR_LIMITS',{'credits':0})
    assert call(a,p,pg).json['error']['code']=='TRIAL_UNCONFIGURED'
    assert queued[1]==[] and queued[2]==[]


def test_idempotency_snapshot_call_version_and_replay(app,queued):
    from models import db, PageImageVersion, VisitorCall, TaskTrace
    a,b=client(app),client(app);p=project(a);pg,old=page(app,a,p);key=secrets.token_hex(16)
    first=call(a,p,pg,key);assert first.status_code==202,first.json
    tid=first.json['data']['task_id']
    assert call(a,p,pg,key).json==first.json
    assert call(a,p,pg,key,{'force_regenerate':False}).status_code==409
    assert len(queued[2])==1
    queued[3]()
    assert len(queued[1])==1
    detail=a.get(f'/api/projects/{p}/task-details?task_id={tid}').json['data']
    assert detail['calls'][0]['status']=='response_saved',detail
    cid=detail['calls'][0]['call_id']
    with app.app_context():
        stored=db.session.get(VisitorCall,cid)
        assert stored.visitor_id==owner(app,p) and stored.reserved==4
        assert PageImageVersion.query.filter_by(page_id=pg).count()==2
        assert db.session.get(PageImageVersion,old) is not None
    assert budget.budget_state()['last_call_id']==cid
    assert call(a,p,pg,key).json==first.json
    assert len(queued[1])==1
    assert b.get(f'/api/projects/{p}/task-details?task_id={tid}').status_code==404


def test_timeout_keeps_old_version_and_occupied_quota(app,queued):
    from models import db, PageImageVersion
    a=client(app);p=project(a);pg,_=page(app,a,p);queued[0].image_provider.fail=True
    response=call(a,p,pg);assert response.status_code==202,response.json
    queued[3]()
    with app.app_context():
        assert PageImageVersion.query.filter_by(page_id=pg).count()==1
    assert budget.budget_state()['paused'] and budget.budget_state()['image']==1
    assert a.get('/api/visitor/session').json['data']['usage']['reserved']==4


def test_last_quota_concurrency_restart_replay(app,monkeypatch):
    a=client(app);p=project(a);who=owner(app,p)
    monkeypatch.setitem(app.config,'PC_VISITOR_LIMITS',dict(image=1,credits=4))
    def attempt(i):
        with app.app_context(),va.scope(who):
            try:
                va.reserve_call(f'test-{secrets.token_hex(16)}','image',4,'synthetic-task')
                return True
            except RuntimeError:return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(attempt,[1,2]))==1
    # New HTTP client with the SAME signed cookie simulates page/service reconnect.
    again=app.test_client();again.set_cookie(va.COOKIE,a.get_cookie(va.COOKIE).value)
    assert again.get('/api/visitor/session').json['data']['usage']['reserved']==4
    assert not attempt(3)


def test_global_guard_and_text_limit_no_provider(app,queued,monkeypatch):
    a=client(app);p=project(a);pg,_=page(app,a,p)
    monkeypatch.setenv('PC_BUDGET_CREDITS','0')
    response=call(a,p,pg)
    assert response.status_code>=400
    assert queued[1]==[]
    monkeypatch.setenv('PC_BUDGET_CREDITS','20')
    monkeypatch.setitem(app.config,'PC_VISITOR_LIMITS',dict(text=0,image=2,credits=8))
    r=a.post(f'/api/projects/{p}/generate/outline',json={},headers={'Idempotency-Key':secrets.token_hex(16)})
    assert r.status_code==429 and queued[1]==[]


def test_additive_schema_repeat_and_sample_readonly(app):
    from models import db, Project, ResourceOwner
    a,b=client(app),client(app)
    with app.app_context():
        before=Project.query.count(); grants=ResourceOwner.query.count()
        db.create_all();db.create_all()
        assert Project.query.count()==before and ResourceOwner.query.count()==grants
    assert a.get('/api/visitor/example').status_code==200
    assert b.get('/api/visitor/example').status_code==200
    assert a.post('/api/visitor/example',json={}).status_code==405


def test_parallel_same_key_one_task_and_queue_limit(app,queued):
    a=client(app);p=project(a);pg,_=page(app,a,p);key=secrets.token_hex(16)
    cookie=a.get_cookie(va.COOKIE).value; csrf=a.environ_base['HTTP_X_PC_CSRF']
    def submit(_):
        c=app.test_client();c.set_cookie(va.COOKIE,cookie)
        c.environ_base.update(HTTP_ORIGIN='http://localhost',HTTP_X_PC_CSRF=csrf)
        return call(c,p,pg,key)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(submit,[1,2]))
    assert all(r.status_code==202 for r in responses)
    assert len(queued[2])==1
    assert call(a,p,pg).json['error']['code']=='QUEUE_FULL'
    queued[3]()
    assert len(queued[1])==1


def test_sse_admission_keeps_identity_and_task_after_response(app,monkeypatch):
    from test_copilot_stream_timeout import provider, Stream, chunk, wait_worker
    from services.ai_service import AIService
    from controllers import project_controller
    from types import SimpleNamespace
    from models import db, VisitorRequest, ResourceOwner
    a=client(app);p=project(a);key=secrets.token_hex(16);calls=[]
    def create(**kwargs):
        calls.append(kwargs)
        return Stream([chunk('## 离线模拟页\n- 测试\n<!-- PAGE_END -->\n<!-- END -->\n')])
    service=AIService(text_provider=provider(create),image_provider=SimpleNamespace(),caption_provider=SimpleNamespace())
    monkeypatch.setattr(project_controller,'get_ai_service',lambda:service)
    headers={'Idempotency-Key':key,'X-PC-Input-Version':a.get(f'/api/projects/{p}/input-revision').json['data']['revision']}
    response=a.post(f'/api/projects/{p}/generate/outline/stream',json={'idea_prompt':'离线演示'},headers=headers)
    body=response.get_data(as_text=True)
    assert 'event: done' in body,body
    with app.app_context():
        row=db.session.get(VisitorRequest,(owner(app,p),key))
        assert row.task_id and db.session.get(ResourceOwner,('tasks',row.task_id)).visitor_id==owner(app,p)
    repeat=a.post(f'/api/projects/{p}/generate/outline/stream',json={'idea_prompt':'离线演示'},headers=headers)
    assert repeat.status_code==202 and len(calls)==1
    wait_worker()


def test_upload_storage_page_project_limits_and_file_traversal(app,monkeypatch):
    a=client(app);p=project(a);pg,_=page(app,a,p)
    monkeypatch.setitem(app.config,'PC_VISITOR_LIMITS',dict(projects=1,pages=1,storage_bytes=1))
    assert a.post('/api/projects',json={'creation_type':'blank'}).status_code==429
    assert a.post('/api/user-templates',data={'template_image':(png(),'test.png')}).status_code==413
    assert a.post(f'/api/projects/{p}/pages',json={'order_index':1}).status_code>=400
    for url in [f'/files/{p}/pages/%2e%2e%5csecret',f'/files/{p}/pages/../../secret',f'/files/{p}/task-inputs/secret']:
        assert a.get(url).status_code>=400
    assert a.get(f'/api/projects/{p}/export/pdf',headers={'X-PC-CSRF':''}).status_code==403


def test_expired_session_rate_limit_and_secure_cookie(app,monkeypatch):
    from models import db, Visitor
    a=client(app);p=project(a);who=owner(app,p)
    with app.app_context():
        db.session.get(Visitor,who).expires=time.time()-1;db.session.commit()
    assert a.get('/api/projects').status_code==401
    monkeypatch.setitem(app.config,'PC_VISITOR_LIMITS',dict(new_visitors_per_hour=0))
    assert app.test_client().get('/api/visitor/session').status_code==429
    monkeypatch.setitem(app.config,'PC_VISITOR_LIMITS',dict(new_visitors_per_hour=1000))
    secure=app.test_client().get('/api/visitor/session',base_url='https://localhost')
    assert 'Secure' in secure.headers['Set-Cookie'] and 'HttpOnly' in secure.headers['Set-Cookie']


def test_free_upload_download_and_no_automatic_caption(app,queued):
    import io
    a,b=client(app),client(app);p=project(a)
    response=a.post('/api/materials/upload',data={'file':(png(),'local.png')})
    assert response.status_code==201,response.json
    url=response.json['data']['url']
    assert a.get(url).status_code==200 and b.get(url).status_code==404
    assert 'sandbox' in a.get(url).headers['Content-Security-Policy']
    assert a.post('/api/materials/upload?generate_caption=true',data={'file':(png(),'local.png')}).status_code==403
    ref=a.post('/api/reference-files/upload',data={'file':(io.BytesIO(b'local offline test'),'sample.txt'),'project_id':p})
    assert ref.status_code==200,ref.json
    fid=ref.json['data']['file']['id']
    assert a.get(f'/api/reference-files/{fid}/download').data==b'local offline test'
    assert b.get(f'/api/reference-files/{fid}/download').status_code==404
    assert a.post(f'/api/reference-files/{fid}/parse',json={}).status_code==403
    assert queued[1]==[] and queued[2]==[]


def test_free_exports_authorize_selected_pages_and_downloads(app,queued,monkeypatch):
    a,b=client(app),client(app);p=project(a);pg,_=page(app,a,p)
    monkeypatch.setenv('PC_MODE','preview')
    for kind,magic in [('pptx',b'PK'),('pdf',b'%PDF')]:
        response=a.get(f'/api/projects/{p}/export/{kind}?page_ids={pg}&filename=offline-export')
        assert response.status_code==200,response.json
        url=response.json['data']['download_url']
        assert a.get(url).data.startswith(magic)
        assert b.get(url).status_code==404
    assert queued[1]==[] and queued[2]==[]


def test_changed_input_version_rejected_before_task(app,queued):
    a=client(app);p=project(a);pg,_=page(app,a,p)
    revision=a.get(f'/api/projects/{p}/input-revision').json['data']['revision']
    assert a.put(f'/api/projects/{p}',json={'extra_requirements':'new input'}).status_code==200
    response=a.post(f'/api/projects/{p}/pages/{pg}/generate/image',json={'force_regenerate':True},
        headers={'Idempotency-Key':secrets.token_hex(16),'X-PC-Input-Version':revision})
    assert response.status_code==409 and response.json['error']['code']=='INPUT_CHANGED'
    assert queued[2]==[] and queued[1]==[]


def test_second_storage_write_failure_never_sends_or_refunds(app,queued,monkeypatch):
    a=client(app);p=project(a);pg,_=page(app,a,p)
    def unavailable(*args,**kwargs):raise OSError('isolated ledger write failure')
    monkeypatch.setattr(budget,'_save',unavailable)
    response=call(a,p,pg);assert response.status_code==202,response.json
    queued[3]()
    assert queued[1]==[]
    assert a.get('/api/visitor/session').json['data']['usage']['reserved']==4
    assert budget.budget_state()['reserved_credits']==0


def test_upgrade_from_legacy_sqlite_and_restore(tmp_path):
    import sqlite3
    from sqlalchemy import create_engine
    from models import Project, Visitor, ResourceOwner, VisitorRequest, VisitorCall
    path=tmp_path/'legacy.db';backup=tmp_path/'backup.db';restored=tmp_path/'restored.db'
    engine=create_engine('sqlite:///'+path.as_posix())
    Project.__table__.create(engine)
    with engine.begin() as connection:
        connection.execute(Project.__table__.insert().values(id='legacy-private',creation_type='blank',status='DRAFT'))
    source=sqlite3.connect(path)
    with sqlite3.connect(backup) as destination:source.backup(destination)
    original=source.execute('select * from projects').fetchall();source.close()
    for _ in range(2):
        for model in (Visitor,ResourceOwner,VisitorRequest,VisitorCall):model.__table__.create(engine,checkfirst=True)
    with sqlite3.connect(path) as connection:
        assert connection.execute('select * from projects').fetchall()==original
        assert connection.execute('select count(*) from pc_resource_owners').fetchone()[0]==0
    with sqlite3.connect(backup) as source,sqlite3.connect(restored) as destination:source.backup(destination)
    with sqlite3.connect(restored) as connection:
        assert connection.execute('select * from projects').fetchall()==original
        assert not connection.execute("select 1 from sqlite_master where name='pc_resource_owners'").fetchone()
    engine.dispose()

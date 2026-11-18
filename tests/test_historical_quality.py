"""Finite historical repair through real HTTP, prepare, budget and worker; sockets off."""
import copy
import hashlib
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import pytest
from test_admin_console import app, admin, isolated, admin_client, login, live_draft, apply
from test_task_tracking import queued
from test_visitor_isolation import client, project, page, call, owner
from test_copilot_capabilities import png
from services import image_retry as retry, budget_cycles as cycles, visitor_access as va
from services.ai_providers import copilot_limits as b
from services.ledger_settlement import summary
from image_retry_maintenance import historical_review


def sources(app, admin, queued):
    from models import db, Project, PageImageVersion, TaskTrace
    manager=login(admin_client(app,admin)); draft=live_draft()
    draft['authorization'].update(credits=50,image=20)
    draft['visitor'].update(credits=50,image=20,pages=10)
    apply(manager,draft)
    visitor=client(app); pid=project(visitor); targets=[]
    for i in range(4):
        pg,_=page(app,visitor,pid)
        response=call(visitor,pid,pg,key=f'old-history-source-{i}')
        assert response.status_code==202,response.json
        queued[3]()
        with app.app_context():
            tr=db.session.get(TaskTrace,response.json['data']['task_id'])
            snap=json.loads(tr.snapshot)
            # Old batch/edit operation and old model are historical evidence,
            # never authority for the new model/inputs.
            snap['operation']='edit_page_image_task' if i==1 else 'generate_images_task'
            snap['model']='old-historical-model'
            tr.snapshot=json.dumps(snap)
            version=PageImageVersion.query.filter_by(page_id=pg,is_current=True).one()
            targets.append(dict(page_id=pg,source_version_id=version.id,source_call_id=b.budget_state()['last_call_id']))
            db.session.commit()
    new,_=cycles.create(b.budget_state(),cycle_id='offline-historical-cycle',credit_cap=16,
        source_sha256='a'*64,archive_sha256='b'*64,created_at='offline')
    new['paused']=True; b._save(b._root()/'call-budget.json',new)
    # Existing official template upload/assignment, no model analysis in product mode.
    payload=png('blue').getvalue()
    response=visitor.post(f'/api/projects/{pid}/template-assets',data={'image':(png('blue'),'clean.png')})
    assert response.status_code==201,response.json
    assert response.json['data']['analyze_task_id'] is None
    asset=response.json['data']['asset']['id']
    for t in targets:
        r=visitor.patch(f"/api/projects/{pid}/pages/{t['page_id']}/template",json={'template_asset_id':asset})
        assert r.status_code==200,r.json
    manifest=dict(id='offline-historical-four',cycle_id=cycles.identity(new),credit_limit=16,
        project_id=pid,model=b.MODELS['image'],resolution='2K',aspect_ratio='16:9',
        template_sha256=hashlib.sha256(payload).hexdigest(),reason='Explicit offline four-page approval',pages=targets)
    root=Path(app.config['SQLALCHEMY_DATABASE_URI'].removeprefix('sqlite:///')).parent.parent
    proposals,report=historical_review(root,new,manifest,b.MODELS['image'])
    updated,changed=retry.grant_historical(new,proposals);assert changed
    b._save(b._root()/'call-budget.json',updated)
    return visitor,pid,targets,proposals,manifest,root


def test_four_serial_new_versions_history_restart_replay(app,admin,queued):
    from models import db,Task,TaskTrace,PageImageVersion
    visitor,pid,targets,proposals,manifest,root=sources(app,admin,queued)
    before=b.budget_state(); sends=len(queued[1])
    with app.app_context(): b.initialize_runtime(app)
    assert b.budget_state()==before
    assert call(visitor,pid,targets[1]['page_id']).status_code>=400
    for i,t in enumerate(targets):
        r=call(visitor,pid,t['page_id'],key=f'new-historical-only-{i}')
        assert r.status_code==202,r.json
        queued[3]()
        with app.app_context():
            assert db.session.get(Task,r.json['data']['task_id']).status=='COMPLETED'
            tr=db.session.get(TaskTrace,r.json['data']['task_id'])
            assert json.loads(tr.snapshot)['pages'][0]['prompt_hash']==proposals[i]['prompt_hash']
            assert db.session.get(PageImageVersion,t['source_version_id']) is not None
        assert call(visitor,pid,t['page_id']).status_code>=400
        repeat,report=historical_review(root,b.budget_state(),manifest,b.MODELS['image'])
        assert repeat is None and report['unchanged']
    after=b.budget_state()
    assert len(queued[1])-sends==4 and cycles.counts(after)=={'text':0,'image':4}
    assert summary(after)['total_occupied']==16 and not retry.ready(after) and after['paused']
    assert retry.grant_historical(after,proposals)==(after,False)
    assert after['quota_cycle']==before['quota_cycle']
    for key,value in before['reservation_calls'].items():assert after['reservation_calls'][key]==value


@pytest.mark.parametrize('change',['input','version','template','owner','budget','prompt','failure','queued_template','queued_project'])
def test_changed_scope_or_failure_blocks_sending(app,admin,queued,monkeypatch,change):
    from models import db,Page,PageImageVersion
    from services import ai_service
    visitor,pid,targets,proposals,manifest,root=sources(app,admin,queued)
    target=targets[0]; before=b.budget_state(); sends=len(queued[1])
    if change.startswith('queued_'):
        response=call(visitor,pid,target['page_id']);assert response.status_code==202
    if change in ('input','version','template','queued_template','queued_project'):
        with app.app_context():
            pg=db.session.get(Page,target['page_id'])
            if change=='input':pg.set_description_content({'text':'changed'})
            if change=='version':db.session.get(PageImageVersion,target['source_version_id']).is_current=False
            if change in ('template','queued_template'):pg.template_asset_id=None
            if change=='queued_project':
                from models import Project
                db.session.get(Project,pid).extra_requirements='unexpected new requirements'
            db.session.commit()
    elif change=='budget':
        state=b.budget_state();state['external_unknown']=15;b._save(b._root()/'call-budget.json',state)
    elif change=='prompt':
        original=ai_service.get_image_generation_prompt
        monkeypatch.setattr(ai_service,'get_image_generation_prompt',lambda **kw:original(**kw)+'changed')
    elif change=='failure':queued[0].image_provider.fail=True
    if change.startswith('queued_'):r=response
    elif change=='owner':
        other=client(app)
        r=other.post(f"/api/projects/{pid}/pages/{target['page_id']}/generate/image",json={'force_regenerate':True})
    else:r=call(visitor,pid,target['page_id'])
    if r.status_code==202:queued[3]()
    assert len(queued[1])-sends==(1 if change=='failure' else 0)
    if change=='failure':
        assert call(visitor,pid,targets[1]['page_id']).status_code>=400
    else:assert b.budget_state()['reserved_credits']==before['reserved_credits']


def test_concurrent_same_page_consumes_once(app,admin,queued):
    from models import db,Task,TaskTrace
    from services import task_tracking as trace
    from services.ai_providers.copilot_limits import image_operation_scope
    from PIL import Image
    visitor,pid,targets,_,_,_=sources(app,admin,queued)
    pg=targets[0]['page_id'];r=call(visitor,pid,pg);assert r.status_code==202
    with app.app_context():
        first=r.json['data']['task_id'];snap=json.loads(db.session.get(TaskTrace,first).snapshot)
        second=Task(project_id=pid,task_type='GENERATE_PAGE_IMAGE',status='PENDING')
        db.session.add(second);db.session.commit();trace.create_trace(second.id,snap)
        ids=[first,second.id]
    who=owner(app,pid);count=len(queued[1])
    def invoke(tid):
        with app.app_context(),va.scope(who),trace.scope(tid,pg,app),image_operation_scope('template_generate',['offline']):
            try:
                queued[0].image_provider.generate_image(snap['pages'][0]['prompt'],ref_images=[Image.new('RGB',(2,2),'blue')])
                return True
            except RuntimeError:return False
    with ThreadPoolExecutor(max_workers=2) as pool:assert sorted(pool.map(invoke,ids))==[False,True]
    assert len(queued[1])==count+1
    assert summary(b.budget_state())['total_occupied']==4


def test_formal_historical_maintenance_backup_persistence_idempotence(app,admin,queued,tmp_path):
    import sqlite3,shutil
    from image_retry_maintenance import run
    visitor,pid,targets,proposals,manifest,source=sources(app,admin,queued)
    root=tmp_path.parent/'hm';(root/'run').mkdir(parents=True);(root/'data').mkdir()
    with sqlite3.connect((source/'data/database.db').as_uri()+'?mode=ro',uri=True) as src,sqlite3.connect(root/'data/database.db') as dst:src.backup(dst)
    shutil.copytree(app.config['UPLOAD_FOLDER'],root/'uploads')
    # Isolated pre-grant snapshot; no real history modified.
    state=b.budget_state();state.pop('image_retry_permits');state.pop('active_image_retry')
    b._save(root/'run/call-budget.json',state)
    (root/'run/server.json').write_text(json.dumps({'stopped':True}))
    approval=root/'approval.json';approval.write_text(json.dumps(manifest))
    before=(root/'run/call-budget.json').read_bytes()
    preview=run(root,None,b.MODELS['image'],historical_file=approval)
    assert (root/'run/call-budget.json').read_bytes()==before
    with pytest.raises(ValueError):run(root,None,b.MODELS['image'],'wrong',historical_file=approval)
    done=run(root,None,b.MODELS['image'],preview['confirmation'],historical_file=approval)
    assert done['applied'] and (Path(done['backup'])/'run/call-budget.json').read_bytes()==before
    after=(root/'run/call-budget.json').read_bytes()
    assert len(json.loads(after)['image_retry_permits'])==4
    assert run(root,None,b.MODELS['image'],preview['confirmation'],historical_file=approval)['unchanged']
    assert (root/'run/call-budget.json').read_bytes()==after

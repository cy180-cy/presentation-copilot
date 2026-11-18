"""PC-008.1: fixed visitor grant, isolated database/ledger and simulated provider."""
import copy
from pathlib import Path
import pytest
from test_visitor_isolation import app, isolated, trial, queued, client, project, page, owner, call
from test_copilot_capabilities import png
from services import visitor_access as va
from services.ai_providers import copilot_limits as budget


@pytest.fixture(params=[budget.VISITOR_BATCH_ID, budget.CONTENT_BATCH_ID])
def setup_batch(app, monkeypatch, request):
    from models import db, Project
    a, b = client(app), client(app)
    pa, pb = project(a), project(b)
    pg, old = page(app, a, pa)
    who = owner(app, pa)
    with app.app_context(), va.scope(who):
        template = Path(app.config['UPLOAD_FOLDER']) / pa / 'template.png'
        template.write_bytes(png('red').getvalue())
        db.session.get(Project, pa).template_image_path = str(template.relative_to(app.config['UPLOAD_FOLDER']))
        db.session.commit()
    historical = dict(text=4, image=9, reserved_credits=39.2, paused=False, external_unknown=3,
                      batches={'old': {'preserved': True}})
    budget._save(budget._root() / 'call-budget.json', historical)
    monkeypatch.setenv('PC_BATCH_ID', request.param)
    monkeypatch.setenv('PC_MODEL_ROLES', 'image')
    monkeypatch.setenv('PC_IMAGE_OPERATIONS', 'template_generate')
    monkeypatch.setitem(app.config, 'PC_TRIAL_VISITOR', None)
    va.bind_trial(app, who)
    return a, b, pa, pb, pg, who, historical


def test_only_owner_one_call_persisted_binding_and_trace(app, setup_batch, queued):
    from models import db, VisitorCall
    a,b,pa,pb,pg,who,historical = setup_batch
    with app.app_context(), va.scope(owner(app,pb)):
        assert va.limits()['image'] == 0
        assert budget.execution_reason('image','template_generate') == 'authorization_required'
    r=call(a,pa,pg); assert r.status_code==202, r.json
    queued[3]()
    assert len(queued[1])==1 and queued[1][0]['refs']==[(255,0,0)]
    state=budget.budget_state()
    assert state['image']==10 and state['text']==4 and state['reserved_credits']==43.2
    assert state['external_unknown']==3 and state['batches']['old']==historical['batches']['old']
    with app.app_context():
        record=db.session.get(VisitorCall,state['last_call_id'])
        assert record.visitor_id==who and record.task_id==r.json['data']['task_id']
    before=copy.deepcopy(state)
    va.bind_trial(app,who)
    assert budget.budget_state()==before
    assert call(a,pa,pg).status_code>=400
    assert len(queued[1])==1 and not queued[2]
    with pytest.raises(RuntimeError): va.bind_trial(app,owner(app,pb))
    assert budget.budget_state()==before


@pytest.mark.parametrize('block',['preview','credits','wrong_operation','text'])
def test_block_before_send(app, setup_batch, queued, monkeypatch, block):
    a,b,pa,pb,pg,who,_=setup_batch
    if block=='preview': monkeypatch.setenv('PC_MODE','preview')
    elif block=='credits': monkeypatch.setitem(budget.RESERVATIONS,'image',6)
    elif block=='wrong_operation': monkeypatch.setenv('PC_IMAGE_OPERATIONS','slide_edit')
    else: monkeypatch.setenv('PC_MODEL_ROLES','text')
    response = call(a,pa,pg)
    if block == 'wrong_operation':
        assert response.status_code == 202
        queued[3]()  # Trusted worker checks the concrete operation before send.
        from models import db, Task
        with app.app_context(), va.scope(who):
            assert db.session.get(Task, response.json['data']['task_id']).status == 'FAILED'
    else:
        assert response.status_code>=400
    assert not queued[1] and not queued[2]
    assert budget.budget_state()['image']==9


def test_timeout_keeps_unknown_and_attempt(app,setup_batch,queued):
    a,b,pa,pb,pg,who,_=setup_batch
    queued[0].image_provider.fail=True
    r=call(a,pa,pg); assert r.status_code==202,r.json
    queued[3]()
    assert len(queued[1])==1
    state=budget.budget_state()
    assert state['image']==10 and state['reserved_credits']==43.2 and state['paused']
    with pytest.raises(RuntimeError): va.bind_trial(app,who)
    assert budget.budget_state()==state

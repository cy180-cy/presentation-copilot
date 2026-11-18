"""Real Waitress WSGI response validation, in memory, sockets disabled.

Only the HTTP channel is replaced. Routes, visitor sessions, admission, tracking,
budget conversion and Waitress.start_response run with isolated data/fake models.
"""
import json
from types import SimpleNamespace
import pytest
from waitress.task import WSGITask
from test_admin_console import app, admin, isolated
from test_soft_page_limits import setup
from test_page_planning import new_project, submit, text_provider
from services.ai_providers import copilot_limits as budget


def waitress_response(application, environ):
    task = WSGITask(SimpleNamespace(server=SimpleNamespace(application=application)),
                    SimpleNamespace(version='1.1', command=environ['REQUEST_METHOD']))
    task.environ = environ
    chunks = []
    def write(chunk):
        task.content_bytes_written += len(chunk)
        chunks.append(chunk)
    task.write = write
    task.execute()
    return task, chunks


@pytest.fixture
def waitress_app(app, monkeypatch):
    original = app.wsgi_app
    def adapter(environ, start_response):
        task, chunks = waitress_response(original, environ)
        start_response(task.status, task.response_headers)
        return chunks
    monkeypatch.setattr(app, 'wsgi_app', adapter)


def test_old_connection_header_fails_before_body_iteration():
    sent = []
    def old_app(environ, start_response):
        start_response('200 OK', [('Content-Type', 'text/event-stream'), ('Connection', 'keep-alive')])
        def body():
            sent.append(True)
            yield b'data: mock\n\n'
        return body()
    with pytest.raises(AssertionError, match='hop-by-hop'):
        waitress_response(old_app, {'REQUEST_METHOD': 'POST'})
    assert not sent


def test_outline_and_description_through_waitress(app, admin, text_provider, waitress_app, monkeypatch):
    from models import Task, TaskTrace, VisitorRequest, VisitorCall
    from services import task_tracking
    a = setup(app, admin)
    pid = new_project(a)
    original_revision = a.get(f'/api/projects/{pid}/input-revision').json['data']['revision']
    r = submit(a, pid, 'outline/stream')
    assert r.status_code == 200 and 'event: done' in r.get_data(as_text=True)
    assert 'event: error' not in r.get_data(as_text=True)
    assert 'Connection' not in r.headers
    assert len(text_provider[1]) == 1
    with app.app_context():
        task = Task.query.one()
        admission = VisitorRequest.query.one()
        assert task.status == 'COMPLETED' and admission.task_id == task.id and admission.state == 'finished'
        assert len(json.loads(TaskTrace.query.one().calls)) == 1
        assert VisitorCall.query.one().task_id == task.id
    # Same admitted request returns its record, never another model attempt.
    with app.app_context():
        request_key = VisitorRequest.query.one().key
    replay = a.post(f'/api/projects/{pid}/generate/outline/stream', json={}, headers={
        'Idempotency-Key': request_key, 'X-PC-Input-Version': original_revision})
    assert replay.status_code == 202 and replay.json['data']['task_id']
    assert len(text_provider[1]) == budget.budget_state()['text'] == 1
    from test_visitor_isolation import client
    b = client(app)
    assert b.get(f'/api/projects/{pid}').status_code == 404
    class DescriptionProvider:
        model = budget.MODELS['text']
        @budget.model_call('text')
        def generate_text_stream(self, prompt, **kwargs):
            task_tracking.sent()
            yield '<!-- BEGIN -->\n' + '离线描述\n<!-- PAGE_END -->\n' * 5 + '<!-- END -->\n'
    from controllers import project_controller
    from services.ai_service import AIService
    service = AIService(text_provider=DescriptionProvider(), image_provider=SimpleNamespace(), caption_provider=SimpleNamespace())
    monkeypatch.setattr(project_controller, 'get_ai_service', lambda: service)
    r = submit(a, pid, 'descriptions/stream')
    assert r.status_code == 200 and 'event: done' in r.get_data(as_text=True)
    assert 'event: error' not in r.get_data(as_text=True) and 'Connection' not in r.headers
    assert budget.budget_state()['text'] == 2
    with app.app_context():
        assert Task.query.count() == 2
        assert all(r.state == 'finished' and r.task_id for r in VisitorRequest.query.all())
    # Admission was freed: another intentional project request is not a queue error.
    monkeypatch.setattr(project_controller, 'get_ai_service', lambda: AIService(
        text_provider=text_provider[0], image_provider=SimpleNamespace(), caption_provider=SimpleNamespace()))
    assert submit(a, new_project(a), 'outline/stream').status_code == 200


@pytest.mark.parametrize('failure', ['preview', 'quota', 'invalid_output'])
def test_stream_failure_guards_and_unknown_reservation(app, admin, text_provider, waitress_app, monkeypatch, failure):
    from models import Task, VisitorRequest
    a = setup(app, admin)
    pid = new_project(a)
    if failure == 'preview':
        monkeypatch.setenv('PC_MODE', 'preview')
    elif failure == 'quota':
        monkeypatch.setitem(app.config['PC_VISITOR_LIMITS'], 'image', 0)
    else:
        text_provider[0].forced_count = 11
    r = submit(a, pid, 'outline/stream')
    if failure != 'invalid_output':
        assert r.status_code >= 400 and not text_provider[1]
        assert budget.budget_state()['text'] == 0
    else:
        assert 'event: error' in r.get_data(as_text=True)
        assert len(text_provider[1]) == budget.budget_state()['text'] == 1
        assert budget.budget_state()['reserved_credits'] == budget.RESERVATIONS['text']
        with app.app_context():
            assert Task.query.one().status == 'FAILED'
            assert VisitorRequest.query.one().task_id

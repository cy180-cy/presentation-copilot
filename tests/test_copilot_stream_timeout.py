"""Offline SDK doubles: deadlines, partial output, diagnostics and shared budget."""
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'backend'), str(ROOT / 'scripts')]
import presentation_copilot as runner
from services.ai_providers import copilot_limits as limits
from services.ai_providers.text.openai_provider import OpenAITextProvider
from services.ai_providers.text.copilot_stream import LocalTextDeadline


@pytest.fixture(autouse=True)
def gate(monkeypatch, tmp_path):
    monkeypatch.setattr(limits, '_root', lambda: tmp_path)
    for key, value in runner.PRESET.items():
        monkeypatch.setenv(key, value)
    for key, value in {'PC_MODE': 'live', 'PC_MODEL_ROLES': 'text,image',
                       'PC_BUDGET_CREDITS': '20', 'PC_TEXT_TOTAL_TIMEOUT_SECONDS': '0.15',
                       'TEXT_API_KEY': 'offline-secret-not-a-key', 'IMAGE_API_KEY': 'offline-image-not-a-key'}.items():
        monkeypatch.setenv(key, value)
    limits._save(tmp_path / 'call-budget.json',
                 {'text': 2, 'image': 0, 'reserved_credits': 1.6, 'paused': False})
    yield tmp_path
    assert not limits._network_worker_running.is_set(), 'Test SDK worker must exit before teardown'


def provider(create):
    value = OpenAITextProvider.__new__(OpenAITextProvider)
    value.model = limits.MODELS['text']
    value.client = NS(chat=NS(completions=NS(create=create)))
    value._output_limit = lambda: {'max_tokens': 4096}
    return value


def wait_worker():
    deadline = time.monotonic() + 2
    while limits._network_worker_running.is_set() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert not limits._network_worker_running.is_set()


def chunk(content='', usage=None, reason=None):
    return NS(usage=usage, model=limits.MODELS['text'], id='mock-response-1',
              choices=[NS(delta=NS(content=content), finish_reason=reason)])


class Stream:
    response = NS(status_code=200, headers={'x-request-id': 'mock-request-1'})
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = threading.Event()
    def __iter__(self):
        yield from self.chunks
    def close(self):
        self.closed.set()


def test_header_wait_has_deadline_and_retains_prior_budget(gate):
    release = threading.Event()
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        release.wait(2)
        return Stream([chunk('late output must be discarded')])
    model = provider(create)
    started = time.monotonic()
    try:
        with pytest.raises(LocalTextDeadline):
            list(model.generate_text_stream('private prompt'))
        assert time.monotonic() - started < 1
        state = limits.budget_state()
        assert (state['text'], state['image'], state['reserved_credits'], state['paused']) == (3, 0, 2.4, True)
        assert limits.execution_reason('text') == 'request_in_progress'
        with pytest.raises(RuntimeError):
            list(model.generate_text_stream('must not retry'))
        assert len(calls) == 1
    finally:
        release.set()
        wait_worker()
    assert limits.budget_state()['paused']


def test_empty_heartbeats_cannot_extend_deadline(gate):
    stopped = threading.Event()
    def chunks():
        while not stopped.wait(0.01):
            yield chunk()
    stream = Stream(chunks())
    stream.close = stopped.set
    values = []
    try:
        with pytest.raises(LocalTextDeadline):
            for value in provider(lambda **kwargs: stream).generate_text_stream('private prompt'):
                values.append(value)
    finally:
        stopped.set()
        wait_worker()
    assert ''.join(values) == ''
    assert any(hasattr(value, 'progress') for value in values)
    rows = [json.loads(line) for line in (gate / 'diagnostics.jsonl').read_text().splitlines()]
    assert {'response_headers', 'first_sdk_chunk', 'local_deadline'} <= {r['event'] for r in rows}
    assert 'first_text' not in {r['event'] for r in rows}


def test_success_records_safe_progress_usage_and_no_prompt(gate):
    usage = NS(model_dump=lambda **kwargs: {'prompt_tokens': 12, 'completion_tokens': 20, 'total_tokens': 32})
    stream = Stream([chunk('## 第一页\n- 节水\n'), chunk('<!-- PAGE_END -->\n<!-- END -->\n', usage, 'stop')])
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        return stream
    output = list(provider(create).generate_text_stream('PRIVATE-PROMPT-CANARY'))
    wait_worker()
    assert ''.join(output).startswith('## 第一页') and stream.closed.is_set()
    assert len(calls) == 1 and calls[0]['max_tokens'] == 4096 and calls[0]['stream_options']['include_usage']
    rows = [json.loads(line) for line in (gate / 'diagnostics.jsonl').read_text().splitlines()]
    assert {'started', 'response_headers', 'first_text', 'sdk_finished', 'response_finished'} <= {r['event'] for r in rows}
    assert len({r['call_id'] for r in rows}) == 1
    assert limits.budget_state()['text'] == 3 and not limits.budget_state()['paused']
    for path in gate.glob('*.json*'):
        assert 'PRIVATE-PROMPT-CANARY' not in path.read_text()
        assert 'offline-secret-not-a-key' not in path.read_text()
    assert json.loads((gate / 'usage.jsonl').read_text())['usage']['total_tokens'] == 32


def test_sdk_error_is_sanitized_and_never_retried(gate):
    class SDKError(Exception):
        status_code = 502
        request_id = 'safe-request-id'
    calls = []
    def create(**kwargs):
        calls.append(1)
        raise SDKError('PRIVATE-BODY offline-secret-not-a-key')
    with pytest.raises(RuntimeError) as caught:
        list(provider(create).generate_text_stream('private'))
    wait_worker()
    assert len(calls) == 1 and limits.budget_state()['paused']
    assert 'PRIVATE-BODY' not in str(caught.value)
    rows = (gate / 'diagnostics.jsonl').read_text()
    assert '502' in rows and 'safe-request-id' in rows and 'PRIVATE-BODY' not in rows


@pytest.mark.parametrize('change', [
    {'PC_MODE': 'preview'}, {'TEXT_API_KEY': ''}, {'PC_MODEL_ROLES': 'image'},
    {'PC_BUDGET_CREDITS': '0'}, {'PC_BUDGET_CREDITS': '1.6'},
])
def test_blocked_requests_never_enter_sdk(gate, monkeypatch, change):
    for key, value in change.items(): monkeypatch.setenv(key, value)
    model = provider(lambda **kwargs: pytest.fail('SDK must not be called'))
    with pytest.raises(RuntimeError): list(model.generate_text_stream('private'))
    assert limits.budget_state()['text'] == 2 and limits.budget_state()['reserved_credits'] == 1.6


def test_outline_parser_forwards_progress_without_inventing_pages(gate):
    from services.ai_service import AIService
    from services import ProjectContext
    model = provider(lambda **kwargs: Stream([chunk('## 第一页\n- 节水\n<!-- PAGE_END -->\n<!-- END -->\n')]))
    service = AIService(text_provider=model, image_provider=NS(), caption_provider=NS())
    context = ProjectContext({'creation_type': 'idea', 'idea_prompt': 'offline only'})
    result = list(service.generate_outline_stream(context, 'zh'))
    wait_worker()
    assert any('__progress__' in item for item in result)
    pages = [item for item in result if 'title' in item]
    assert len(pages) == 1 and pages[0]['title'] == '第一页'

"""Bound local waiting without claiming that the upstream request was cancelled."""
import os
import queue
import threading
import time
from contextvars import copy_context

from ..copilot_limits import _network_worker_running, record_diagnostic, record_usage


class TextStreamProgress(str):
    """Empty text for existing consumers; outline UI can expose safe progress."""
    def __new__(cls, phase, elapsed, limit):
        value = super().__new__(cls, '')
        value.progress = {'phase': phase, 'elapsed_seconds': round(elapsed, 1),
                          'total_timeout_seconds': limit}
        return value


class LocalTextDeadline(TimeoutError):
    pass


def text_response(provider, prompt, *, stream):
    """One SDK call, no retry. A bounded consumer waits independently of reads.

    Closing an HTTP stream is best effort. If a blocking SDK read remains alive,
    the process-wide marker keeps subsequent model calls blocked until it exits.
    Late output is discarded, never saved as a successful page or refunded.
    """
    limit = getattr(provider, 'total_timeout_seconds', None)
    if limit is None:
        limit = float(os.getenv('PC_TEXT_TOTAL_TIMEOUT_SECONDS', '720'))
    if not 0 < limit <= 1800:
        raise ValueError('文本总时限无效。')
    started = time.perf_counter()
    messages = queue.Queue()
    cancelled = threading.Event()
    response_holder = []
    state = {'phase': 'waiting_response', 'characters': 0, 'chunks': 0}

    def close_response():
        if response_holder:
            try:
                response_holder[0].close()
            except Exception:
                record_diagnostic('close_failed')

    def work():
        try:
            response = provider.client.chat.completions.create(
                model=provider.model, messages=[{'role': 'user', 'content': prompt}],
                stream=stream, **provider._output_limit(),
                **({'stream_options': {'include_usage': True}} if stream else {}))
            if stream:
                response_holder.append(response)
                http_response = getattr(response, 'response', None)
                record_diagnostic('response_headers',
                    http_status=getattr(http_response, 'status_code', None),
                    provider_request_id=http_response.headers.get('x-request-id') if http_response is not None else None)
                state['phase'] = 'waiting_first_text'
                for chunk in response:
                    if cancelled.is_set():
                        break
                    state['chunks'] += 1
                    if state['chunks'] == 1:
                        record_diagnostic('first_sdk_chunk')
                    if chunk.usage:
                        record_usage('text', chunk.usage, chunk.model, chunk.id)
                    choice = chunk.choices[0] if chunk.choices else None
                    if choice and choice.finish_reason:
                        record_diagnostic('finish_reason', finish_reason=choice.finish_reason)
                    content = choice.delta.content if choice and choice.delta else None
                    if content:
                        if state['characters'] == 0:
                            state['phase'] = 'receiving_text'
                            record_diagnostic('first_text')
                        state['characters'] += len(content)
                        if state['characters'] > 1_000_000:
                            raise ValueError('文本响应超出本地大小上限。')
                        messages.put(('text', content))
            else:
                if cancelled.is_set():
                    return
                record_usage('text', response.usage, response.model,
                             getattr(response, '_request_id', None) or response.id)
                content = response.choices[0].message.content
                state['characters'] = len(content or '')
                record_diagnostic('first_text', content_characters=state['characters'])
                messages.put(('text', content or ''))
            if not cancelled.is_set():
                if not state['characters']:
                    raise ValueError('模型未返回可用正文。')
                record_diagnostic('sdk_finished', chunks=state['chunks'],
                                  content_characters=state['characters'])
                messages.put(('done', None))
        except BaseException as error:
            record_diagnostic('sdk_error', error_type=type(error).__name__,
                              http_status=getattr(error, 'status_code', None),
                              provider_request_id=getattr(error, 'request_id', None))
            # Do not pass a supplier response body, URL or auth data to the UI/log.
            messages.put(('error', RuntimeError('模型请求未完成，费用待核对；已暂停后续调用。')))
        finally:
            close_response()
            _network_worker_running.clear()
            if cancelled.is_set():
                record_diagnostic('worker_exited_after_local_stop')

    context = copy_context()
    _network_worker_running.set()
    worker = threading.Thread(target=lambda: context.run(work), daemon=True,
                              name='copilot-text-request')
    completed = False
    try:
        record_diagnostic('wait_started', total_timeout_seconds=limit)
        worker.start()
        yield TextStreamProgress(state['phase'], 0, limit)
        last_phase = state['phase']
        while True:
            elapsed = time.perf_counter() - started
            if elapsed >= limit:
                record_diagnostic('local_deadline', phase=state['phase'])
                raise LocalTextDeadline('已达到本次等待时限，上游可能仍在处理或计费；已暂停，未自动重试。')
            try:
                kind, value = messages.get(timeout=min(1.0, limit - elapsed))
            except queue.Empty:
                yield TextStreamProgress(state['phase'], time.perf_counter() - started, limit)
                continue
            if time.perf_counter() - started >= limit:
                record_diagnostic('local_deadline', phase=state['phase'])
                raise LocalTextDeadline('已达到本次等待时限，上游可能仍在处理或计费；已暂停，未自动重试。')
            if kind == 'error':
                raise value
            if kind == 'done':
                completed = True
                return
            if state['phase'] != last_phase:
                last_phase = state['phase']
                yield TextStreamProgress(last_phase, time.perf_counter() - started, limit)
            yield value
    finally:
        if not completed and worker.is_alive():
            cancelled.set()
            record_diagnostic('local_wait_stopped', phase=state['phase'])
            # A blocking close must not defeat the consumer's deadline either.
            closing_context = copy_context()
            threading.Thread(target=lambda: closing_context.run(close_response), daemon=True,
                             name='copilot-text-close').start()
        elif not worker.ident:
            _network_worker_running.clear()

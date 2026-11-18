import { describe, it, expect, vi, afterEach } from 'vitest';
import { generateOutlineStream } from '@/api/endpoints';

// These tests isolate SSE transport; backend tests cover real visitor admission.
vi.mock('@/api/client', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/client')>(),
  visitorHeaders: vi.fn(async () => ({ 'X-CSRF-Token': 'offline-only' })),
}));

afterEach(() => vi.unstubAllGlobals());
function mockStream(text: string) {
  const bytes = new TextEncoder().encode(text);
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, body: new ReadableStream({
    start(controller) { controller.enqueue(bytes); controller.close(); },
  }) });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}
const callbacks = () => ({ onPage: vi.fn(), onDone: vi.fn(), onError: vi.fn(), onProgress: vi.fn() });
describe('outline stream completion and waiting', () => {
  it.each([
    ['QUEUE_FULL', '当前任务较多，请等待已提交任务完成。', '生成服务暂不可用，请稍后重试。'],
    ['TRIAL_EXHAUSTED', '当前额度不足，已有作品仍可使用。', '当前可用额度不足，请联系管理员。'],
  ])('keeps the meaning of a 429 %s response and never retries', async (code, message, expected) => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: false, status: 429, json: async () => ({ error: { code, message } }) });
    vi.stubGlobal('fetch', fetchMock);
    const cb = callbacks();
    await generateOutlineStream('offline-project', cb, 'zh');
    expect(cb.onError).toHaveBeenCalledTimes(1);
    expect(cb.onError).toHaveBeenCalledWith(expected);
    expect(cb.onDone).not.toHaveBeenCalled();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
  it('does not render an HTML server failure or retry it', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: false, status: 500, json: async () => { throw new Error('HTML'); } });
    vi.stubGlobal('fetch', fetchMock);
    const cb = callbacks();
    await generateOutlineStream('offline-project', cb, 'zh');
    expect(cb.onError).toHaveBeenCalledTimes(1);
    expect(cb.onError).toHaveBeenCalledWith('生成服务暂不可用，请稍后重试。');
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
  it('shows a safe page-limit refusal before streaming without retry', async () => {
    const message='当前单任务最多支持8页，你要求9页，请调整页数后再生成。';
    const fetchMock=vi.fn().mockResolvedValue({ok:false,status:400,json:async()=>({error:{code:'PAGE_LIMIT',message}})});
    vi.stubGlobal('fetch',fetchMock);const cb=callbacks();
    await generateOutlineStream('offline-project',cb,'zh');
    expect(cb.onError).toHaveBeenCalledWith(message);expect(cb.onPage).not.toHaveBeenCalled();
    expect(cb.onDone).not.toHaveBeenCalled();expect(fetchMock).toHaveBeenCalledTimes(1);
  });
  it('exposes progress separately from saved pages and performs one request', async () => {
    const fetchMock = mockStream('event: progress\ndata: {"phase":"waiting_first_text","elapsed_seconds":120,"total_timeout_seconds":720}\n\nevent: page\ndata: {"index":0,"title":"节水","points":[]}\n\nevent: done\ndata: {"total":1,"pages":[{"id":"saved-page"}]}\n\n');
    const cb = callbacks();
    await generateOutlineStream('offline-project', cb, 'zh');
    expect(cb.onProgress).toHaveBeenCalledWith(expect.objectContaining({ elapsed_seconds: 120 }));
    expect(cb.onPage).toHaveBeenCalledTimes(1);
    expect(cb.onDone).toHaveBeenCalledTimes(1);
    expect(cb.onError).not.toHaveBeenCalled();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
  it('treats EOF without done as unconfirmed, never successful', async () => {
    const fetchMock = mockStream('event: page\ndata: {"index":0,"title":"partial","points":[]}\n\n');
    const cb = callbacks();
    await generateOutlineStream('offline-project', cb, 'zh');
    expect(cb.onDone).not.toHaveBeenCalled();
    expect(cb.onError).toHaveBeenCalledWith(expect.stringContaining('尚未确认'));
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
  it('does not swallow error callback exceptions or retry', async () => {
    const fetchMock = mockStream('event: error\ndata: {"message":"local deadline"}\n\n');
    const cb = callbacks();
    cb.onError.mockImplementation(() => { throw new Error('stop'); });
    await expect(generateOutlineStream('offline-project', cb, 'zh')).rejects.toThrow('stop');
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

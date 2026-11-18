import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, act } from '@testing-library/react';

describe('visitor UI with simulated session only', () => {
  it('bootstraps once and attaches CSRF and a stable pending intent', async () => {
    const fetch = vi.fn().mockResolvedValue({ok:true,json:async()=>({data:{revision:'offline-input-v1',csrf:'offline-csrf',message:'预览模式：模型执行关闭。'}})});
    vi.stubGlobal('fetch',fetch);
    const { apiClient } = await import('@/api/client');
    const headers:any[]=[];
    const adapter=async(config:any)=>{headers.push(config.headers);return {status:202,statusText:'Accepted',headers:{},config,data:{}};};
    await Promise.all([apiClient.post('/api/projects/test/generate/images',{}, {adapter}),apiClient.post('/api/projects/test/generate/images',{}, {adapter})]);
    expect(fetch.mock.calls.filter(c=>c[0]==='/api/visitor/session')).toHaveLength(1);
    expect(headers[0]['X-PC-CSRF']).toBe('offline-csrf');
    expect(headers[0]['Idempotency-Key']).toBe(headers[1]['Idempotency-Key']);
    vi.unstubAllGlobals();
  });
  it('keeps a valid session invisible and only prompts when expired', async () => {
    const { VisitorNotice } = await import('@/components/shared/VisitorNotice');
    const view=render(<VisitorNotice/>);
    await act(async()=>{});
    expect(view.container).toBeEmptyDOMElement();
    act(()=>window.dispatchEvent(new Event('pc-session-expired')));
    expect(screen.getByRole('alert')).toHaveTextContent('会话已失效，请返回首页重新开始。');
    expect(screen.getByRole('button',{name:'返回首页'})).toBeInTheDocument();
    expect(view.container).not.toHaveTextContent(/preview|预览模式|模型执行/);
  });
});

describe('visitor bootstrap recovery without model requests', () => {
  beforeEach(() => { vi.resetModules(); });
  afterEach(() => { vi.unstubAllGlobals(); });

  it('shares a failed bootstrap and sends a new request on explicit retry', async () => {
    const request = vi.fn().mockRejectedValueOnce(new TypeError('offline'))
      .mockResolvedValueOnce({ ok: true, json: async () => ({ data: { csrf: 'new-session' } }) });
    vi.stubGlobal('fetch', request);
    const { ensureVisitorSession } = await import('@/api/client');
    const first = ensureVisitorSession();
    const second = ensureVisitorSession();
    expect(first).toBe(second);
    await expect(first).rejects.toThrow('offline');
    await expect(second).rejects.toThrow('offline');
    await expect(ensureVisitorSession()).resolves.toEqual({ csrf: 'new-session' });
    expect(request).toHaveBeenCalledTimes(2);
    expect(request.mock.calls.every(call => call[0] === '/api/visitor/session')).toBe(true);
  });

  it('preserves an expired-session code without automatically replacing the identity', async () => {
    const request = vi.fn().mockResolvedValue({ ok: false, status: 401,
      json: async () => ({ error: { code: 'SESSION_EXPIRED', message: 'expired' } }) });
    vi.stubGlobal('fetch', request);
    const { ensureVisitorSession } = await import('@/api/client');
    await expect(ensureVisitorSession()).rejects.toMatchObject({ code: 'SESSION_EXPIRED', status: 401 });
    expect(request).toHaveBeenCalledTimes(1);
    expect(request.mock.calls[0][0]).toBe('/api/visitor/session');
  });

  it('only resets the cached session after the explicit restart succeeds', async () => {
    const request = vi.fn()
      .mockResolvedValueOnce({ ok: true, json: async () => ({ data: { csrf: 'old-session' } }) })
      .mockResolvedValueOnce({ ok: false, status: 403 })
      .mockResolvedValueOnce({ ok: true })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ data: { csrf: 'new-session' } }) });
    vi.stubGlobal('fetch', request);
    const { ensureVisitorSession, restartVisitorSession } = await import('@/api/client');
    const original = ensureVisitorSession();
    await original;
    await expect(restartVisitorSession()).rejects.toThrow();
    expect(ensureVisitorSession()).toBe(original);
    await restartVisitorSession();
    expect(request).toHaveBeenCalledTimes(3);
    expect(request.mock.calls[2]).toEqual(['/api/visitor/restart', {
      method: 'POST', credentials: 'same-origin', headers: { 'X-PC-Restart': '1' },
    }]);
    await expect(ensureVisitorSession()).resolves.toEqual({ csrf: 'new-session' });
    expect(request).toHaveBeenCalledTimes(4);
  });
});

import axios from 'axios';
import { isDesktop } from '@/utils';
import { publicFailure } from '@/utils/publicMessages';

const DESKTOP_BACKEND_PORT_STORAGE_KEY = '__desktop_backend_port__';

function isValidPort(port: number): boolean {
  return Number.isInteger(port) && port > 0 && port <= 65535;
}

function parsePort(value: unknown): number {
  return Number.parseInt(String(value ?? ''), 10);
}

function resolveDesktopBackendPort(): number {
  const rawPort = (window as any).electronAPI?.getBackendPort?.();
  const port = parsePort(rawPort);
  if (isValidPort(port)) {
    sessionStorage.setItem(DESKTOP_BACKEND_PORT_STORAGE_KEY, String(port));
    return port;
  }

  const cachedPort = parsePort(sessionStorage.getItem(DESKTOP_BACKEND_PORT_STORAGE_KEY));
  if (isValidPort(cachedPort)) {
    return cachedPort;
  }

  console.warn('Desktop backend port is unavailable');
  return 0;
}

const isClientSideUrl = (url: string) => /^\/?(data|blob):/i.test(url);
const normalizeClientSideUrl = (url: string) => url.replace(/^\/(?=(data|blob):)/i, '');

// 桌面模式：从 URL query param 同步读取后端端口，避免异步竞态
if (isDesktop) {
  (window as any).__BACKEND_PORT__ = resolveDesktopBackendPort();
}

export function getBaseURL(): string {
  if (!isDesktop) return '';
  const port = (window as any).__BACKEND_PORT__;
  if (!isValidPort(port)) {
    console.warn('Desktop backend port is unavailable');
    return 'http://127.0.0.1:0';
  }
  return `http://127.0.0.1:${port}`;
}

// 统一下载入口：desktop 走原生保存对话框，web 走 window.open
export function triggerDownload(relativeOrAbsoluteUrl: string, filename?: string): void {
  if (!relativeOrAbsoluteUrl || typeof relativeOrAbsoluteUrl !== 'string') {
    console.warn('Invalid download URL:', relativeOrAbsoluteUrl);
    return;
  }

  if (isDesktop) {
    if (isClientSideUrl(relativeOrAbsoluteUrl)) {
      const url = normalizeClientSideUrl(relativeOrAbsoluteUrl);
      (window as any).electronAPI.downloadFile(url, filename || 'download');
      return;
    }

    const normalizedPath = relativeOrAbsoluteUrl.startsWith('/')
      ? relativeOrAbsoluteUrl
      : `/${relativeOrAbsoluteUrl}`;
    const url = /^https?:\/\//i.test(relativeOrAbsoluteUrl)
      ? relativeOrAbsoluteUrl
      : `${getBaseURL()}${normalizedPath}`;
    let fallbackFilename = 'download';
    try {
      fallbackFilename = new URL(url).pathname.split('/').pop() || 'download';
    } catch {
      fallbackFilename = relativeOrAbsoluteUrl.split('?')[0].split('/').filter(Boolean).pop() || 'download';
    }
    (window as any).electronAPI.downloadFile(url, filename || fallbackFilename);
  } else {
    window.open(relativeOrAbsoluteUrl, '_blank');
  }
}

// 创建 axios 实例
export const apiClient = axios.create({
  timeout: 300000, // 5分钟超时（AI生成可能很慢）
});

let visitorSession: Promise<any> | undefined;
const pendingIntents = new Map<string, string>();
export function ensureVisitorSession(): Promise<any> {
  if (!visitorSession) {
    const pending = fetch('/api/visitor/session', { credentials: 'same-origin' })
      .then(async response => {
        const body = await response.json();
        if (!response.ok) throw Object.assign(
          new Error(body.error?.message || '访客会话不可用，请返回首页。'),
          { code: body.error?.code, status: response.status },
        );
        return body.data;
      });
    visitorSession = pending;
    // Share an in-flight bootstrap, but let an explicit retry make a new request.
    void pending.catch(() => { if (visitorSession === pending) visitorSession = undefined; });
  }
  return visitorSession;
}

export async function restartVisitorSession(): Promise<void> {
  const response = await fetch('/api/visitor/restart', {
    method: 'POST', credentials: 'same-origin', headers: { 'X-PC-Restart': '1' },
  });
  if (!response.ok) throw new Error('暂时无法重新开始，请稍后重试。');
  visitorSession = undefined;
}
export async function visitorHeaders(projectId?: string): Promise<Record<string, string>> {
  const session = await ensureVisitorSession();
  const headers:Record<string,string> = { 'X-PC-CSRF': session.csrf, 'Idempotency-Key': crypto.randomUUID() };
  if (projectId) {
    const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/input-revision`, {credentials:'same-origin'});
    const body = await response.json();
    if (!response.ok) throw new Error('作品输入暂不可读取，请刷新后再试。');
    headers['X-PC-Input-Version'] = body.data.revision;
  }
  return headers;
}

// 请求拦截器
apiClient.interceptors.request.use(
  async (config) => {
    await ensureVisitorSession();
    if (!['get', 'head', 'options'].includes(config.method || 'get') || config.url?.includes('/export/')) {
      const modelOperation = /\/(generate|edit|refine)\//.test(config.url || '');
      const projectId = modelOperation ? config.url?.match(/\/api\/projects\/([^/]+)/)?.[1] : undefined;
      const headers = await visitorHeaders(projectId);
      for (const [key, value] of Object.entries(headers)) {
        if (!config.headers[key]) config.headers[key] = value;
      }
      if (/\/(generate|edit|refine)\//.test(config.url || '')) {
        const input = config.data instanceof FormData ? Array.from(config.data.entries()).map(([k,v]) => [k, typeof v === 'string' ? v : [v.name,v.size,v.lastModified]]) : config.data;
        const intent = JSON.stringify([config.url, input, config.headers['X-PC-Input-Version']]);
        if (!pendingIntents.has(intent)) pendingIntents.set(intent, String(config.headers['Idempotency-Key']));
        config.headers['Idempotency-Key'] = pendingIntents.get(intent)!;
        (config as any).__visitorIntent = intent;
      }
    }
    const hasAbsoluteBaseURL = typeof config.baseURL === 'string' && /^https?:\/\//i.test(config.baseURL);
    const hasAbsoluteRequestURL = typeof config.url === 'string' && /^https?:\/\//i.test(config.url);
    if (!hasAbsoluteBaseURL && !hasAbsoluteRequestURL) {
      config.baseURL = getBaseURL();
    }

    // Attach access code header for backend enforcement
    const accessCode = localStorage.getItem('banana-access-code');
    if (accessCode && config.headers) {
      config.headers['X-Access-Code'] = accessCode;
    }

    // 如果请求体是 FormData，删除 Content-Type 让浏览器自动设置
    // 浏览器会自动添加正确的 Content-Type 和 boundary
    if (config.data instanceof FormData) {
      // 不设置 Content-Type，让浏览器自动处理
      if (config.headers) {
        delete config.headers['Content-Type'];
      }
    } else if (config.headers && !config.headers['Content-Type']) {
      // 对于非 FormData 请求，默认设置为 JSON
      config.headers['Content-Type'] = 'application/json';
    }

    return config;
  },
  (error) => {
    return Promise.reject(error);
  }
);

// 响应拦截器
apiClient.interceptors.response.use(
  (response) => {
    const intent = (response.config as any).__visitorIntent;
    if (intent) setTimeout(() => pendingIntents.delete(intent), 2000);
    return response;
  },
  (error) => {
    // 统一错误处理
    if (error.response) {
      // Admin uses its own client; preserve full diagnostics there.
      const detail = error.response.data?.error;
      if (detail && typeof detail.message === 'string') {
        detail.message = publicFailure(detail.message, detail.code);
        error.message = detail.message;
      }
      // 服务器返回错误状态码
      if (error.response.status === 401) window.dispatchEvent(new Event('pc-session-expired'));
      console.error('API Error:', error.response.status, error.response.data?.error?.code || 'request_failed');
    } else if (error.request) {
      // 请求已发送但没有收到响应
      console.error('Network Error: result unknown; do not automatically retry.');
    } else {
      // 其他错误
      console.error('Error:', error.message);
    }
    return Promise.reject(error);
  }
);

// 图片URL处理工具
// 使用相对路径，通过代理转发到后端
export const getImageUrl = (path?: string, timestamp?: string | number): string => {
  if (!path) return '';
  if (isClientSideUrl(path)) {
    return normalizeClientSideUrl(path);
  }

  // 如果已经是完整URL，直接返回
  if (path.startsWith('http://') || path.startsWith('https://')) {
    return path;
  }
  // 使用相对路径（确保以 / 开头）
  let url = path.startsWith('/') ? path : '/' + path;

  if (isDesktop) {
    url = `${getBaseURL()}${url}`;
  }
  
  // 添加时间戳参数避免浏览器缓存（仅在提供时间戳时添加）
  if (timestamp) {
    const ts = typeof timestamp === 'string' 
      ? new Date(timestamp).getTime() 
      : timestamp;
    url += `?v=${ts}`;
  }
  
  return url;
};

export default apiClient;

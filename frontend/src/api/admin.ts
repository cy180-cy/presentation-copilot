// Separate admin CSRF and HttpOnly session. Never reuses visitor credentials.
let csrf = '';
export async function adminRequest<T = any>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const response = await fetch(`/api/admin/${path}`, {
    method, credentials: 'same-origin', cache: 'no-store',
    headers: method === 'GET' ? {} : {'Content-Type': 'application/json', 'X-PC-Admin-CSRF': csrf},
    body: method === 'GET' ? undefined : JSON.stringify(body ?? {}),
  });
  const result = await response.json();
  if (!response.ok) {
    const error = new Error(result.error?.message || '管理操作未完成，请刷新核对。');
    Object.assign(error, {status: response.status});
    throw error;
  }
  if (result.data?.csrf) csrf = result.data.csrf;
  return result.data as T;
}

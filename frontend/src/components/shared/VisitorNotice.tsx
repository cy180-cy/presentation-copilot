import { useEffect, useState } from 'react';
import { ensureVisitorSession } from '@/api/client';

export function VisitorNotice() {
  const [expired, setExpired] = useState(false);
  useEffect(() => { ensureVisitorSession().catch(() => setExpired(true)); }, []);
  useEffect(() => {
    const listener = () => setExpired(true);
    window.addEventListener('pc-session-expired', listener);
    return () => window.removeEventListener('pc-session-expired', listener);
  }, []);
  if (!expired) return null;
  return <div className="border-b bg-amber-50 px-4 py-2 text-sm text-slate-700 flex flex-wrap gap-3" role="alert">
    <span>会话已失效，请返回首页重新开始。</span>
    <button className="underline" onClick={async () => {
      await fetch('/api/visitor/restart', { method: 'POST', headers: { 'X-PC-Restart': '1' } });
      localStorage.removeItem('currentProjectId'); window.location.href = '/';
    }}>返回首页</button>
  </div>;
}

import { useEffect, useState } from 'react';

type Offer = { url: string; version: string };

// Public metadata only: never read runtime configuration or credentials here.
export function parseSourceOffer(value: unknown): Offer | null {
  if (!value || typeof value !== 'object') return null;
  const { url, version } = value as Record<string, unknown>;
  if (typeof url !== 'string' || typeof version !== 'string' ||
      !/^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/.test(version)) return null;
  try {
    if (url !== url.trim() || /[\\\s]/.test(url)) return null;
    const target = new URL(url, 'https://source-offer.invalid');
    const local = url.startsWith('/open-source/') && !decodeURIComponent(url).includes('..');
    if ((!local && (!url.startsWith('https://') || target.hostname === 'source-offer.invalid')) ||
        target.username || target.password || target.search) return null;
    return { url, version };
  } catch { return null; }
}

export function SourceOffer() {
  const [offer, setOffer] = useState<Offer | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    fetch('/open-source/source-offer.json', {
      signal: controller.signal, cache: 'no-store', credentials: 'omit', redirect: 'error',
    }).then(response => response.ok ? response.json() : null)
      .then(value => { if (!controller.signal.aborted) setOffer(parseSourceOffer(value)); })
      .catch(() => { /* A missing or invalid public URL keeps the honest fallback. */ });
    return () => controller.abort();
  }, []);
  return <section id="source-code" className="rounded-2xl border border-slate-200 bg-white p-6 space-y-4 dark:bg-background-secondary dark:border-border-primary">
    <h2 className="text-xl font-semibold">获取本版本源码</h2>
    <p>本项目在 AGPL 开源代码基础上修改，涉及产品界面、匿名访客归属、任务追踪、预算管理、导出、资源保护及部署工具。</p>
    <p>远程用户可免费获取本版本对应的修改版源码。上游项目地址用于说明来源，本版本源码另行提供。</p>
    {offer ? <div className="space-y-2">
      <a className="underline underline-offset-4" href={offer.url} target="_blank" rel="noopener noreferrer">获取本版本对应的修改版源码</a>
      <p className="text-sm text-slate-500">源码版本：{offer.version}</p>
    </div> : <p role="status" className="text-sm leading-7 text-slate-600 dark:text-slate-400">本版本源码获取地址待正式公开服务时补齐，当前尚未提供公开下载链接。</p>}
    <p><a className="underline underline-offset-4" href="/open-source/THIRD_PARTY_NOTICES.txt" target="_blank" rel="noreferrer">开源依赖许可证与版权声明</a></p>
  </section>;
}

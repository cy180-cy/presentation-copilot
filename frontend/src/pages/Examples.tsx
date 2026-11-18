import { useEffect, useState } from 'react';
import { apiClient } from '@/api/client';

export function Examples() {
  const [example, setExample] = useState<any>(null);
  useEffect(() => { apiClient.get('/api/visitor/example').then(r => setExample(r.data.data)).catch(() => setExample({ unavailable: true })); }, []);
  return <main className="max-w-5xl mx-auto p-6 space-y-5">
    <a href="/" className="underline">返回创作首页</a>
    <h1 className="text-3xl font-bold">校园节水行动 · 只读示例</h1>
    <p>这是预生成的演示材料，可直接查看和下载。示例与我的作品分开保存。</p>
    {!example ? <p>正在加载…</p> : example.unavailable ? <p>暂时无法加载示例，请稍后重试。</p> : <>
      {example.images.map((url: string, i: number) => <img key={url} src={url} alt={`预生成示例第 ${i+1} 页`} className="w-full rounded-xl border" />)}
      <div className="flex gap-6"><a className="underline" href={example.pptx}>下载图片式 PPTX</a><a className="underline" href={example.pdf}>下载 PDF</a></div>
    </>}
  </main>;
}

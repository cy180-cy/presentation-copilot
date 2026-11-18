import { Link } from 'react-router-dom';
import { useEffect, useState } from 'react';
import { getSettings, updateSettings, OUTPUT_LANGUAGE_OPTIONS, type OutputLanguage } from '@/api/endpoints';
import { ProductHeader } from '@/components/shared/ProductHeader';
import { Footer } from '@/components/shared/Footer';
import { PRODUCT_NAME } from '@/config/product';
import { ASPECT_RATIO_OPTIONS } from '@/config/aspectRatio';
import { SourceOffer } from '@/components/shared/SourceOffer';

export function About() {
  return <div className="min-h-screen bg-slate-50 dark:bg-background-primary text-slate-800 dark:text-foreground-primary">
    <ProductHeader />
    <main className="mx-auto max-w-3xl space-y-7 px-6 py-14">
      <p className="text-sm font-medium text-slate-500">{PRODUCT_NAME}</p>
      <h1 className="text-3xl font-bold">关于与开源说明</h1>
      <p className="leading-8">多模态演示文稿生成与交付平台。输入主题或大纲，整理逐页描述，生成页面，再修改、预览并下载。</p>
      <section className="rounded-2xl border border-slate-200 bg-white p-6 space-y-4 dark:bg-background-secondary dark:border-border-primary">
        <h2 className="text-xl font-semibold">来源与许可</h2>
        <p>基于开源项目 <a className="underline" href="https://github.com/Anionex/banana-slides" target="_blank" rel="noreferrer">Anionex/banana-slides</a>，保留原作者及贡献者的版权通知。</p>
        <p>许可：<a className="underline" href="/open-source/LICENSE" target="_blank" rel="noreferrer">GNU AGPL-3.0 许可证原文</a>。上游项目链接用于标明来源。</p>
      </section>
      <SourceOffer />
      <section className="space-y-3"><h2 className="text-xl font-semibold">作品与导出</h2>
        <p>示例页提供预生成的演示材料。普通 PPTX 为图片式页面，图片中的文字不能逐字编辑。</p>
        <p>作品与当前浏览器关联；清理浏览器数据或更换浏览器后，可能无法找回原作品。</p>
      </section>
      <Link to="/" className="inline-block rounded-xl bg-slate-900 px-5 py-3 text-white">返回创建演示文稿</Link>
    </main><Footer />
  </div>;
}

export function ManagedSettingsNotice() {
  const [language, setLanguage] = useState<OutputLanguage>('zh');
  const [languageReady, setLanguageReady] = useState(false);
  const [languageMessage, setLanguageMessage] = useState('正在读取输出语言…');
  useEffect(() => {
    getSettings().then(result => {
      setLanguage((result.data?.output_language || 'zh') as OutputLanguage);
      setLanguageReady(true); setLanguageMessage('');
    }).catch(() => setLanguageMessage('暂时无法读取创作偏好，请刷新后重试。'));
  }, []);
  const [ratio, setRatio] = useState(() => {
    const value = localStorage.getItem('copilot-default-aspect-ratio');
    return ASPECT_RATIO_OPTIONS.some(option => option.value === value) ? value! : '16:9';
  });
  return <div className="min-h-screen bg-slate-50 dark:bg-background-primary text-slate-700 dark:text-foreground-primary"><ProductHeader />
    <main className="mx-auto max-w-2xl px-6 py-16 space-y-6">
      <h1 className="text-3xl font-semibold">创作设置</h1>
      <p className="text-sm text-slate-500">选择新作品的默认画面比例和内容语言，已有内容保持不变。</p>
      <section className="rounded-2xl border border-slate-200 bg-white p-6 space-y-5 dark:bg-background-secondary dark:border-border-primary">
        <label className="flex items-center justify-between gap-4">默认画面比例
          <select aria-label="默认画面比例" className="rounded-lg border border-slate-200 bg-transparent px-4 py-2 dark:border-border-primary" value={ratio} onChange={event => {
            setRatio(event.target.value); localStorage.setItem('copilot-default-aspect-ratio', event.target.value);
          }}>{ASPECT_RATIO_OPTIONS.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}</select>
        </label>
        <label className="flex items-center justify-between gap-4">内容输出语言
          <select aria-label="内容输出语言" disabled={!languageReady} value={language}
            className="rounded-lg border border-slate-200 bg-transparent px-4 py-2 dark:border-border-primary"
            onChange={async event => {
              const next = event.target.value as OutputLanguage;
              setLanguageReady(false);
              try {
                await updateSettings({ output_language: next });
                setLanguage(next); setLanguageMessage('已保存');
              } catch { setLanguageMessage('保存失败，仍保留原来的输出语言。'); }
              finally { setLanguageReady(true); }
            }}>
            {OUTPUT_LANGUAGE_OPTIONS.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
          </select>
        </label>
        <p role="status" className="text-sm text-slate-500">{languageMessage}</p>
        <p className="text-sm text-slate-500">内容输出语言与右上角的界面语言分别设置。</p>
        <p className="text-sm leading-7 text-slate-500">模板与文字风格可在创建页选择，每份作品可单独调整。界面语言和主题模式可在右上角切换。</p>
      </section>
      <Link to="/#create-presentation" className="inline-block rounded-xl bg-amber-400 px-5 py-3 text-slate-900">返回创作</Link>
    </main><Footer /></div>;
}

export function ProductHelp() {
  return <div className="min-h-screen bg-slate-50 dark:bg-background-primary text-slate-700 dark:text-foreground-primary"><ProductHeader />
    <main className="mx-auto max-w-3xl px-6 py-16 space-y-7">
      <h1 className="text-3xl font-semibold">创作帮助</h1>
      <ol className="list-decimal pl-6 space-y-5 leading-8">
        <li><strong>输入内容：</strong>选择一句话、大纲或逐页描述，填写内容；也可以从空白项目开始手动编辑。</li>
        <li><strong>选择风格：</strong>浏览图片模板，或打开“使用文字描述风格”，选择预设并补充配色、布局要求。</li>
        <li><strong>生成与修改：</strong>整理大纲与页面描述，再逐页生成；修改描述后可以重新生成对应页面。</li>
        <li><strong>预览与交付：</strong>检查文字、数字与页数后，导出 PPTX 或 PDF。普通 PPTX 是图片式页面，图片中的文字不是独立文本框。</li>
      </ol>
      <section className="rounded-2xl border border-amber-200 bg-amber-50 p-6 text-sm leading-7 text-slate-600 dark:bg-background-secondary dark:border-border-primary dark:text-foreground-secondary">
        <h2 className="mb-2 font-semibold">创作与文件格式</h2>
        <p>图片模板支持上传、应用与逐页手动分配。PPT 翻新可先导入 PDF 并查看原稿。PPT/PPTX 需先转为 PDF。预览页可对已有图片准备自然语言或选区修改；素材工具箱支持本地选图与框选，执行后保存新素材。文字示例只填入内容，不会自动生成。</p>
        <p>看到“生成服务暂不可用”时，可以继续手工编辑已有作品，或选择图片模板、文字风格创建空白作品。</p>
      </section>
      <Link to="/about" className="underline underline-offset-4">关于与开源说明</Link>
    </main><Footer /></div>;
}

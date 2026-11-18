import { Link } from 'react-router-dom';
import { FolderOpen, Globe, ImagePlus, Monitor, Settings } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useTheme, type Theme } from '@/hooks/useTheme';
import { PRODUCT_NAME } from '@/config/product';
import { ProductMark } from './ProductMark';

export function ProductHeader() {
  const { i18n } = useTranslation();
  const { theme, setTheme } = useTheme();
  const zh = !i18n.language || i18n.language.startsWith('zh');
  const navClass = 'inline-flex items-center gap-2 whitespace-nowrap transition-colors hover:text-orange-600 dark:hover:text-banana';
  return <header className="relative z-20 bg-white/35 dark:bg-background-primary">
    <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-4 px-5 py-5">
      <Link to="/" aria-label={`${PRODUCT_NAME}首页`} title={PRODUCT_NAME}>
        <ProductMark />
      </Link>
      <nav aria-label="产品导航" className="flex flex-wrap items-center justify-center gap-x-6 gap-y-3 text-sm md:text-base text-slate-600 dark:text-foreground-secondary">
        <Link className={navClass} to="/?action=material-generate"><ImagePlus size={20} />{zh ? '素材生成' : 'Generate assets'}</Link>
        <Link className={navClass} to="/?action=material-center"><FolderOpen size={20} />{zh ? '素材中心' : 'Asset library'}</Link>
        <Link className={navClass} to="/history">{zh ? '我的作品' : 'Projects'}</Link>
        <Link className={navClass} to="/examples">{zh ? '示例' : 'Examples'}</Link>
        <Link className={navClass} to="/settings"><Settings size={19} />{zh ? '设置' : 'Preferences'}</Link>
        <Link className={`${navClass} font-semibold`} to="/help">{zh ? '帮助' : 'Help'}</Link>
      </nav>
      <div className="flex items-center gap-4 border-l border-slate-200 pl-4 dark:border-border-primary text-slate-600 dark:text-foreground-secondary">
        <button type="button" title={zh ? '界面语言' : 'Language'} className={navClass} onClick={() => i18n.changeLanguage(zh ? 'en' : 'zh')}><Globe size={18} /><span>{zh ? 'EN' : '中文'}</span></button>
        <label className="flex items-center gap-1" title={zh ? '主题模式' : 'Theme'}><Monitor size={19} />
          <select aria-label={zh ? '主题模式' : 'Theme'} value={theme} onChange={event => setTheme(event.target.value as Theme)} className="max-w-24 bg-transparent text-xs dark:bg-background-primary">
            <option value="system">{zh ? '跟随系统' : 'System'}</option><option value="light">{zh ? '浅色' : 'Light'}</option><option value="dark">{zh ? '深色' : 'Dark'}</option>
          </select>
        </label>
      </div>
    </div>
  </header>;
}

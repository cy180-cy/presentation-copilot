import { Link } from 'react-router-dom';
import { PRODUCT_NAME } from '@/config/product';

export function Footer({ showIcp = false }: { showIcp?: boolean }) {
  return <footer className="relative px-5 py-7 text-center text-xs text-slate-500 dark:text-foreground-tertiary">
    <span>{PRODUCT_NAME}</span><span className="mx-3">·</span>
    <Link to="/about" className="underline underline-offset-4">关于与开源说明</Link>
    <span className="mx-3">·</span>
    <Link to="/about#source-code" className="underline underline-offset-4">获取本版本源码</Link>
    {showIcp && <div className="mt-2">
      <a href="https://beian.miit.gov.cn/#/Integrated/recordQuery" className="underline underline-offset-4">豫ICP备2026058238号-1</a>
    </div>}
  </footer>;
}

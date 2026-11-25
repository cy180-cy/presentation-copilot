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
      <div className="mt-2">
        <a
          href="https://beian.mps.gov.cn/#/query/webSearch?code=41032902000430"
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center justify-center gap-1.5 whitespace-nowrap underline underline-offset-4"
        >
          <img src="/filing/police-filing.png" width={18} height={20} alt="" aria-hidden="true" />
          <span>豫公网安备41032902000430号</span>
        </a>
      </div>
    </div>}
  </footer>;
}

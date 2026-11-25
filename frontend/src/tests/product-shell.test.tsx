import { Footer } from '@/components/shared/Footer';
import { Loading } from '@/components/shared/Loading';
import { apiClient } from '@/api/client';
import { afterEach, beforeEach, describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { About, ManagedSettingsNotice } from '../pages/About';
import { ProductHeader } from '../components/shared/ProductHeader';
import { ProjectSettingsModal } from '../components/shared/ProjectSettingsModal';
import { SourceOffer, parseSourceOffer } from '../components/shared/SourceOffer';

beforeEach(() => vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ url: '', version: '' }) })));
afterEach(() => vi.unstubAllGlobals());

vi.mock('@/api/client', () => ({ apiClient: { get: vi.fn().mockResolvedValue({ data: { data: { output_language: 'zh' } } }), put: vi.fn().mockResolvedValue({ data: { data: {} } }) } }));
vi.mock('@/components/shared/TextStyleSelector', () => ({ TextStyleSelector: () => <div>文字风格选择</div> }));
vi.mock('react-i18next', async importOriginal => ({
  ...await importOriginal<typeof import('react-i18next')>(),
  useTranslation: () => ({ t: (key: string) => key, i18n: { language: 'zh', changeLanguage: vi.fn() } }),
}));
vi.mock('@/hooks/useT', () => ({ useT: (bundle: { zh: Record<string, unknown> }) => (key: string) =>
  key.split('.').reduce<unknown>((value, part) => (value as Record<string, unknown>)?.[part], bundle.zh) || key }));

describe('personal product shell', () => {
  it('keeps an icon-only brand and restores ordinary navigation', () => {
    render(<MemoryRouter><ProductHeader /></MemoryRouter>);
    const brand = screen.getByRole('link', { name: 'Presentation Copilot首页' });
    expect(brand).toHaveAttribute('href', '/');
    expect(brand.textContent).toBe('');
    expect(screen.getByRole('link', { name: '素材生成' })).toHaveAttribute('href', '/?action=material-generate');
    expect(screen.getByRole('link', { name: '素材中心' })).toHaveAttribute('href', '/?action=material-center');
    expect(screen.getByRole('link', { name: '我的作品' })).toHaveAttribute('href', '/history');
    expect(screen.getByRole('link', { name: '设置' })).toHaveAttribute('href', '/settings');
  });
  it('direct settings page contains no credential form', () => {
    const { container } = render(<MemoryRouter><ManagedSettingsNotice /></MemoryRouter>);
    expect(screen.getByText('创作设置')).toBeInTheDocument();
    expect(container.querySelector('input')).toBeNull();
    expect(container.textContent).not.toMatch(/API Key|Base URL|充值|购买/);
    fireEvent.change(screen.getByLabelText('默认画面比例'), { target: { value: '4:3' } });
    expect(localStorage.getItem('copilot-default-aspect-ratio')).toBe('4:3');
  });
  it('keeps editable project options without global/export-admin panels', () => {
    render(<ProjectSettingsModal isOpen onClose={vi.fn()} extraRequirements="" onExtraRequirementsChange={vi.fn()}
      templateStyle="" onTemplateStyleChange={vi.fn()} onSaveExtraRequirements={vi.fn()} onSaveTemplateStyle={vi.fn()}
      isSavingRequirements={false} isSavingTemplateStyle={false} />);
    expect(screen.queryByText('全局设置')).not.toBeInTheDocument();
    expect(screen.queryByText('导出设置')).not.toBeInTheDocument();
    expect(screen.getByText('文字风格选择')).toBeInTheDocument();
  });
  it('preserves source attribution and license without release preparation notes', () => {
    render(<MemoryRouter><About /></MemoryRouter>);
    expect(screen.getByRole('link', { name: /许可证原文/ })).toHaveAttribute('href', '/open-source/LICENSE');
    expect(screen.getByRole('link', { name: 'Anionex/banana-slides' })).toHaveAttribute('href', 'https://github.com/Anionex/banana-slides');
    expect(screen.queryByText(/源码发布地址尚未确认|上游基线|远程仓库|9277bfcb/)).not.toBeInTheDocument();
    expect(screen.getByText(/示例页提供预生成的演示材料/)).toBeInTheDocument();
    expect(screen.queryByText(/验收|真实验证|预览模式|PC-00/)).not.toBeInTheDocument();
  });
});

it('shows neither a footer mode badge nor internal progress logs',()=>{
 const view=render(<MemoryRouter><Footer/><Loading message="模型输入检查中" progress={{total:2,completed:1,messages:['live 配置就绪','PC-010 预算预留'],current_step:'Provider'}}/></MemoryRouter>);
 expect(screen.getByText('50%')).toBeInTheDocument();
 expect(view.container).not.toHaveTextContent(/live|preview|预览|PC-010|配置|预算|模型|Provider/);
 expect(screen.getByRole('link',{name:'关于与开源说明'})).toBeInTheDocument();
 expect(screen.queryByRole('link',{name:'豫公网安备41032902000430号'})).not.toBeInTheDocument();
});

it('links the homepage police filing badge to its official record and preserves the ICP link', () => {
 render(<MemoryRouter><Footer showIcp /></MemoryRouter>);
 const policeLink = screen.getByRole('link', { name: '豫公网安备41032902000430号' });
 expect(policeLink).toHaveAttribute('href', 'https://beian.mps.gov.cn/#/query/webSearch?code=41032902000430');
 expect(policeLink).toHaveAttribute('target', '_blank');
 expect(policeLink).toHaveAttribute('rel', 'noopener noreferrer');
 expect(policeLink.querySelector('img')).toHaveAttribute('src', '/filing/police-filing.png');
 expect(screen.getByRole('link', { name: '豫ICP备2026058238号-1' })).toHaveAttribute('href', 'https://beian.miit.gov.cn/#/Integrated/recordQuery');
});

it('offers an honest fallback without a fabricated source URL', async () => {
 render(<SourceOffer />);
 await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
 expect(screen.getByRole('status')).toHaveTextContent('待正式公开服务时补齐');
 expect(screen.queryByRole('link', { name: '获取本版本对应的修改版源码' })).toBeNull();
 expect(screen.getByRole('link', { name: '开源依赖许可证与版权声明' })).toHaveAttribute('href', '/open-source/THIRD_PARTY_NOTICES.txt');
});

it('uses a configured URL and version without contacting the target', async () => {
 vi.mocked(fetch).mockResolvedValue({ ok: true, json: async () => ({ url: 'https://example.org/source/v1.zip', version: 'release-1' }) } as Response);
 render(<SourceOffer />);
 expect(await screen.findByRole('link', { name: '获取本版本对应的修改版源码' })).toHaveAttribute('href', 'https://example.org/source/v1.zip');
 expect(screen.getByText('源码版本：release-1')).toBeInTheDocument();
 expect(fetch).toHaveBeenCalledTimes(1);
});

it('keeps source information readable when configuration cannot load', async () => {
 vi.mocked(fetch).mockRejectedValue(new Error('offline'));
 render(<SourceOffer />);
 await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
 expect(screen.getByRole('status')).toHaveTextContent('尚未提供公开下载链接');
});

it('rejects unsafe or incomplete source addresses', () => {
 for (const url of ['javascript:alert(1)', '//example.org/source.zip', 'http://example.org/source.zip', 'https://user:secret@example.org/a', 'https://example.org/a?token=secret', '/open-source/../admin']) {
   expect(parseSourceOffer({ url, version: 'release-1' })).toBeNull();
 }
 expect(parseSourceOffer({ url: 'https://example.org/source.zip', version: '' })).toBeNull();
 expect(parseSourceOffer({ url: '/open-source/source-v1.zip', version: 'release-1' })).not.toBeNull();
});

import { beforeEach, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { TemplateSetupPage } from '@/pages/TemplateSetupPage';
import { TemplatePickerModal } from '@/components/template/TemplatePickerModal';
import { TemplateAnalysisEditor } from '@/components/template/TemplateAnalysisEditor';
import { AccessCodeGuard } from '@/components/shared/AccessCodeGuard';
import { apiClient, restartVisitorSession } from '@/api/client';
import type { TemplateAsset } from '@/types';

const state = vi.hoisted(() => ({
  currentProject: { id: 'draft', template_mode: 'multi', pages: [{ id: 'page', order_index: 0,
    status: 'COMPLETED', outline_content: { title: '校园节水行动', points: [] },
    description_content: { text: '检查18处用水点' }, template_asset_id: 'template' }] },
  templateAssets: [{ id: 'template', project_id: 'draft', user_label: '手绘风格',
    image_url: '/files/draft/template.png', analysis_status: 'deferred' }],
  syncProject: vi.fn(), loadTemplateAssets: vi.fn(), uploadTemplateAsset: vi.fn(),
  uploadTemplatePdf: vi.fn(), updateTemplateAsset: vi.fn(), deleteTemplateAsset: vi.fn(),
  reanalyzeTemplateAsset: vi.fn(), updatePageTemplate: vi.fn(), switchTemplateMode: vi.fn(),
  switchTemplateModeWithUpload: vi.fn(), autoMatchAll: vi.fn(), autoMatchPage: vi.fn(), pollTemplateTask: vi.fn(),
}));
vi.mock('@/store/useProjectStore', () => ({ useProjectStore: Object.assign(
  (select?: any) => select ? select(state) : state, { getState: () => state }) }));
vi.mock('@/api/client', () => ({ apiClient: { get: vi.fn(), post: vi.fn(), put: vi.fn() }, getImageUrl: (path: string) => path,
  restartVisitorSession: vi.fn() }));
vi.mock('@/hooks/useT', () => ({ useT: (bundle: any) => (key: string) =>
  key.split('.').reduce((value: any, part: string) => value?.[part], bundle.zh) || key }));

beforeEach(() => { vi.clearAllMocks(); });

it('keeps uploads, style and manual page assignment without analysis readiness controls', () => {
  const view = render(<MemoryRouter initialEntries={['/project/draft/template-setup']}><Routes>
    <Route path="/project/:projectId/template-setup" element={<TemplateSetupPage />} />
  </Routes></MemoryRouter>);
  expect(screen.getByRole('button', { name: '上传图片' })).toBeEnabled();
  expect(screen.getByRole('button', { name: '上传 PDF' })).toBeEnabled();
  expect(screen.getByRole('button', { name: '前往预览' })).toBeEnabled();
  expect(screen.getByRole('button', { name: '选择模板' })).toBeEnabled();
  expect(view.container).not.toHaveTextContent(/等待服务开放|未接入|未分析|自动匹配|重新分析|当前执行状态|PC-\d|preview|live/);
  expect(screen.queryByTestId('auto-match-readiness')).not.toBeInTheDocument();
  expect(state.autoMatchAll).not.toHaveBeenCalled();
  expect(state.autoMatchPage).not.toHaveBeenCalled();
  expect(state.reanalyzeTemplateAsset).not.toHaveBeenCalled();
});

it('lets users select an uploaded template without displaying analysis status', () => {
  const select = vi.fn();
  const view = render(<TemplatePickerModal isOpen onClose={vi.fn()} assets={state.templateAssets as TemplateAsset[]}
    onSelect={select} />);
  fireEvent.click(screen.getByRole('button', { name: /手绘风格/ }));
  expect(select).toHaveBeenCalledWith('template');
  expect(view.container).not.toHaveTextContent(/未分析|解析中|解析失败|已解析|deferred/);
});

it('keeps manual template style saving while hiding unavailable reanalysis and its diagnostics', async () => {
  const save = vi.fn(); const analyze = vi.fn();
  const view = render(<TemplateAnalysisEditor asset={{ ...state.templateAssets[0], analysis_status: 'failed' } as TemplateAsset}
    onSave={save} onReanalyze={analyze} />);
  expect(screen.queryByRole('button', { name: '重新解析' })).not.toBeInTheDocument();
  expect(view.container).not.toHaveTextContent(/解析失败|重新解析/);
  fireEvent.click(screen.getByRole('button', { name: '保存' }));
  await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
  expect(analyze).not.toHaveBeenCalled();
});

it('keeps access checks closed on failure without asking users to inspect the backend', async () => {
  vi.mocked(apiClient.get).mockRejectedValueOnce(new Error('runtime path /api/internal'));
  const view = render(<AccessCodeGuard><div>受保护的创作内容</div></AccessCodeGuard>);
  expect(await screen.findByText('服务暂不可用，请稍后重试。')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '重试' })).toBeEnabled();
  expect(screen.queryByText('受保护的创作内容')).not.toBeInTheDocument();
  expect(view.container).not.toHaveTextContent(/后端|runtime|\/api|path/);
  expect(apiClient.post).not.toHaveBeenCalled();
});

it('shows explicit recovery for an expired visitor before protected content mounts', async () => {
  vi.mocked(apiClient.get).mockRejectedValueOnce(Object.assign(new Error('expired'), { code: 'SESSION_EXPIRED' }));
  vi.mocked(restartVisitorSession).mockRejectedValueOnce(new Error('restart unavailable'));
  render(<AccessCodeGuard><div>受保护的创作内容</div></AccessCodeGuard>);
  expect(await screen.findByText(/会话已失效，请重新开始/)).toBeInTheDocument();
  expect(screen.queryByText('受保护的创作内容')).not.toBeInTheDocument();
  expect(restartVisitorSession).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '重新开始' }));
  await waitFor(() => expect(restartVisitorSession).toHaveBeenCalledTimes(1));
  expect(await screen.findByRole('alert')).toHaveTextContent('网络错误，请稍后重试');
  expect(screen.queryByText('受保护的创作内容')).not.toBeInTheDocument();
  expect(apiClient.post).not.toHaveBeenCalled();
});

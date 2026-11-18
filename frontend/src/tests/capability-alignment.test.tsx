import { beforeEach, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { apiClient } from '@/api/client';
import { refreshCapabilities } from '@/hooks/useProductCapabilities';
import { DescriptionCard } from '@/components/preview/DescriptionCard';
import { ManagedSettingsNotice } from '@/pages/About';
import { DetailEditor } from '@/pages/DetailEditor';
import { getStoredOutputLanguage, generatePageImage } from '@/api/endpoints';

vi.mock('@/api/client', () => ({ apiClient: { get: vi.fn(), put: vi.fn(), post: vi.fn() }, getBaseURL: () => '' }));
const state = vi.hoisted(() => ({ currentProject: {
  id: 'pc004', creation_type: 'blank', template_mode: 'single', image_aspect_ratio: '16:9',
  pages: [{ id: 'page1', order_index: 0, status: 'DESCRIPTION_GENERATED',
    outline_content: { title: 'PC004 synthetic draft', points: [] }, description_content: { text: '手工保存' } }],
}, syncProject: vi.fn(), updatePageLocal: vi.fn(), generateDescriptions: vi.fn(),
generatePageDescription: vi.fn(), regenerateRenovationPage: vi.fn(), switchTemplateMode: vi.fn(),
pageGeneratingTasks: {}, outlineStreamingProjectIds: [] }));
vi.mock('@/store/useProjectStore', () => ({ useProjectStore: Object.assign((select?: any) => select ? select(state) : state, { getState: () => state }) }));
vi.mock('react-i18next', async original => ({ ...await original<any>(), useTranslation: () => ({ i18n: { language: 'zh', changeLanguage: vi.fn() } }) }));
vi.mock('@/hooks/useT', () => ({ useT: (bundle?: any) => (key: string) =>
  key.split('.').reduce((value: any, part: string) => value?.[part], bundle?.zh) || key }));

let language = 'zh';
let available = false;
beforeEach(async () => {
  vi.clearAllMocks();
  language = 'zh'; available = false;
  vi.mocked(apiClient.get).mockImplementation(async (url: string) => ({ data: { data:
    url === '/api/product/status' ? { mode: 'preview', capabilities: { text: {
      available, configured: true, verified: false, reason: available ? null : 'preview', message: '当前为预览模式'
    } } } : url === '/api/output-language' ? { language } : url === '/api/product/preferences'
      ? { output_language: language, description_generation_mode: 'streaming', description_extra_fields: [], image_prompt_extra_fields: [] }
      : { files: [], materials: [], tasks: [] }
  } }) as any);
  vi.mocked(apiClient.put).mockImplementation(async (_url: string, value: any) => {
    if (value.output_language) language = value.output_language;
    return { data: { data: value } } as any;
  });
  vi.mocked(apiClient.post).mockResolvedValue({ data: {} });
  await refreshCapabilities();
});

it('description navigation and manual editing remain usable while all text execution buttons are disabled', async () => {
  render(<MemoryRouter initialEntries={['/project/pc004/detail']}><Routes>
    <Route path="/project/:projectId/detail" element={<DetailEditor />} />
    <Route path="/project/:projectId/preview" element={<div>FREE_PREVIEW_ROUTE</div>} />
  </Routes></MemoryRouter>);
  await waitFor(() => expect(screen.getByRole('button', { name: '重新生成' })).toBeDisabled());
  expect(screen.queryByText(/当前为预览模式|当前执行状态|验证范围/)).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: /批量生成/ })).toBeDisabled();
  expect(screen.getByRole('button', { name: '重新生成' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'common.edit' })).toBeEnabled();
  expect(screen.getByText('已保存')).toBeInTheDocument();
  const navigation = screen.getByRole('button', { name: '生成图片' });
  expect(navigation).toBeEnabled();
  fireEvent.click(navigation);
  expect(screen.getByText('FREE_PREVIEW_ROUTE')).toBeInTheDocument();
  expect(state.generateDescriptions).not.toHaveBeenCalled();
  expect(state.generatePageDescription).not.toHaveBeenCalled();
  expect(apiClient.post).not.toHaveBeenCalled();
});

it('capability changes reach memoized cards; unverified but authorized service can start first validation', async () => {
  render(<MemoryRouter initialEntries={['/project/pc004/detail']}><Routes>
    <Route path="/project/:projectId/detail" element={<DetailEditor />} />
  </Routes></MemoryRouter>);
  await waitFor(() => expect(screen.getByRole('button', { name: '重新生成' })).toBeDisabled());
  available = true;
  await act(async () => { await refreshCapabilities(); });
  expect(screen.getByRole('button', { name: '重新生成' })).toBeEnabled();
  expect(screen.getByRole('button', { name: /批量生成/ })).toBeEnabled();
  expect(apiClient.post).not.toHaveBeenCalled();
});

it('persists output language through business preferences and carries it on generation requests', async () => {
  render(<MemoryRouter><ManagedSettingsNotice /></MemoryRouter>);
  const select = screen.getByLabelText('内容输出语言');
  await waitFor(() => expect(select).toBeEnabled());
  fireEvent.change(select, { target: { value: 'ja' } });
  await waitFor(() => expect(screen.getByText('已保存')).toBeInTheDocument());
  expect(apiClient.put).toHaveBeenCalledWith('/api/product/preferences', { output_language: 'ja' });
  expect(await getStoredOutputLanguage()).toBe('ja');
  // HTTP client is a double: asserts payload only; no network/model is involved.
  await generatePageImage('pc004', 'page1');
  expect(apiClient.post).toHaveBeenCalledWith('/api/projects/pc004/pages/page1/generate/image', expect.objectContaining({ language: 'ja' }));
  expect(apiClient.get).not.toHaveBeenCalledWith('/api/settings');
});

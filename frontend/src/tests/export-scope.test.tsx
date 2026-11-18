import { beforeEach, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { ProjectSettingsModal } from '@/components/shared/ProjectSettingsModal';
import { useExportTasksStore } from '@/store/useExportTasksStore';
import * as api from '@/api/endpoints';

vi.mock('@/api/endpoints', () => ({ getTaskStatus: vi.fn() }));
vi.mock('@/components/shared', () => ({
  Button: ({ children, ...props }: any) => <button {...props}>{children}</button>,
  Textarea: (props: any) => <textarea {...props} />,
  TextStyleSelector: () => <div>文字风格选择</div>,
}));
vi.mock('@/hooks/useT', () => ({ useT: (bundle: any) => (key: string) =>
  key.split('.').reduce((v: any, k: string) => v?.[k], bundle.zh) || key }));

beforeEach(() => { vi.clearAllMocks(); localStorage.clear(); useExportTasksStore.setState({ tasks: [] }); });

it('keeps project editing controls while export reconstruction has no settings tab', () => {
  const save = vi.fn();
  const view = render(<ProjectSettingsModal isOpen onClose={vi.fn()} extraRequirements="" templateStyle=""
    onExtraRequirementsChange={vi.fn()} onTemplateStyleChange={vi.fn()} onSaveExtraRequirements={save}
    onSaveTemplateStyle={vi.fn()} isSavingRequirements={false} isSavingTemplateStyle={false} />);
  expect(screen.getByText('保存额外要求')).toBeEnabled();
  fireEvent.click(screen.getByText('保存额外要求'));
  expect(save).toHaveBeenCalledTimes(1);
  expect(screen.getByText('文字风格选择')).toBeInTheDocument();
  expect(view.container.textContent).not.toMatch(/Editable|可编辑\s*PPTX|组件提取|图标透明背景|导出设置|背景图获取/);
});

it('does not restore or poll cached retired tasks while retaining regular export history', async () => {
  localStorage.setItem('export-tasks-storage', JSON.stringify({ state: { tasks: [
    { id: 'old-retired', type: 'editable-pptx', projectId: 'p', taskId: 'old', status: 'PROCESSING' },
    { id: 'ordinary', type: 'pptx', projectId: 'p', taskId: '', status: 'COMPLETED', downloadUrl: '/files/p/exports/a.pptx' },
  ] }, version: 0 }));
  await useExportTasksStore.persist.rehydrate();
  expect(useExportTasksStore.getState().tasks.map(t => t.id)).toEqual(['ordinary']);
  useExportTasksStore.getState().restoreActiveTasks();
  expect(api.getTaskStatus).not.toHaveBeenCalled();
});

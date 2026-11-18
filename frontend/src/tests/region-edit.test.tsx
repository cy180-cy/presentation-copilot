import { apiClient } from '@/api/client';
import { refreshCapabilities } from '@/hooks/useProductCapabilities';
import { beforeEach, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MaterialGeneratorModal } from '@/components/shared/MaterialGeneratorModal';
import { processMaterialImage } from '@/api/endpoints';

vi.mock('@/api/client', () => ({ apiClient: { get: vi.fn() }, getImageUrl: (url: string) => url }));
vi.mock('@/hooks/useT', () => ({ useT: () => (key: string) => key }));
vi.mock('@/store/useProjectStore', () => ({ useProjectStore: (select: any) => select({ currentProject: null }) }));
vi.mock('@/components/shared/Toast', () => ({ useToast: () => ({ show: vi.fn() }) }));
vi.mock('@/components/shared/MaterialSelector', () => ({ MaterialSelector: () => null, materialUrlToFile: vi.fn() }));
vi.mock('@/api/endpoints', () => ({
  getRegionEditStatus: vi.fn(),
  processMaterialImage: vi.fn().mockResolvedValue({ data: { task_id: 'offline-task' } }),
  getTaskStatus: vi.fn().mockResolvedValue({ data: { status: 'COMPLETED', progress: { image_url: '/files/offline.png' } } }),
}));

beforeEach(async () => {
  vi.mocked(apiClient.get).mockResolvedValue({ data: { data: { capabilities: {} } } } as any);
  await refreshCapabilities();
  vi.clearAllMocks();
  sessionStorage.clear();
  URL.createObjectURL = vi.fn(() => 'blob:offline');
  URL.revokeObjectURL = vi.fn();
});

function chooseRegion() {
  fireEvent.change(screen.getByTestId('material-source-input'), {
    target: { files: [new File(['offline-test'], 'source.png', { type: 'image/png' })] },
  });
  const image = screen.getByAltText('material.sourceImage');
  Object.defineProperties(image, { naturalWidth: { value: 400 }, naturalHeight: { value: 300 },
    clientWidth: { value: 200 }, clientHeight: { value: 150 } });
  image.getBoundingClientRect = () => ({ left: 10, top: 20, width: 200, height: 150 } as DOMRect);
  fireEvent.click(screen.getByTestId('material-selection-toggle'));
  const canvas = screen.getByTestId('material-source-canvas');
  fireEvent.mouseDown(canvas, { clientX: 60, clientY: 50 });
  fireEvent.mouseMove(canvas, { clientX: 160, clientY: 110 });
  fireEvent.mouseUp(canvas);
  fireEvent.change(screen.getByPlaceholderText('material.promptPlaceholderRegion'), { target: { value: '只修改选区' } });
}

it('allows local source selection and drawing while denying paid submission', async () => {
  vi.mocked(apiClient.get).mockResolvedValue({ data: { data: { capabilities: { material_region_edit: { available: false, message: '预览模式' } } } } } as any);
  render(<MaterialGeneratorModal isOpen onClose={vi.fn()} />);
  await waitFor(() => expect(apiClient.get).toHaveBeenCalledWith('/api/product/status'));
  chooseRegion();
  expect(screen.getByText('material.selectionReady')).toBeInTheDocument();
  const toggle = screen.getByTestId('material-selection-toggle');
  // A real click starts with mousedown; it must not begin a zero-size canvas selection.
  fireEvent.mouseDown(toggle, { clientX: 30, clientY: 30 });
  fireEvent.mouseUp(toggle);
  fireEvent.click(toggle);
  expect(screen.getByText('material.selectionReady')).toBeInTheDocument();
  expect(toggle).toHaveTextContent('material.startSelection');
  expect(screen.getByRole('button', { name: 'material.runTool' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: 'material.runTool' }));
  expect(processMaterialImage).not.toHaveBeenCalled();
  expect(screen.queryByTestId('material-ref-input')).not.toBeInTheDocument();
});

it('uses upstream coordinates, request and polling only after server opt-in', async () => {
  vi.mocked(apiClient.get).mockResolvedValue({ data: { data: { capabilities: { material_region_edit: { available: true, message: '' } } } } } as any);
  render(<MaterialGeneratorModal isOpen onClose={vi.fn()} projectId="project-test" />);
  chooseRegion();
  await waitFor(() => expect(screen.getByRole('button', { name: 'material.runTool' })).toBeEnabled());
  expect(screen.queryByText(/执行后会调用图片模型|本地仅接入/)).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'material.runTool' }));
  await waitFor(() => expect(processMaterialImage).toHaveBeenCalledWith('project-test', expect.objectContaining({
    operation: 'region_edit', applyMode: 'overlay_selection', prompt: '只修改选区',
    selection: { x: 100, y: 60, width: 200, height: 120, image_width: 400, image_height: 300 },
  })));
  await waitFor(() => expect(screen.getByAltText('material.generatedResult')).toBeInTheDocument());
  fireEvent.click(screen.getByText('material.toolEditFull'));
  expect(screen.queryByText('当前本地仅接入框选编辑。')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'material.runTool' })).toBeDisabled();
});

it('fails closed when readiness cannot be read', async () => {
  vi.mocked(apiClient.get).mockRejectedValue(new Error('offline'));
  render(<MaterialGeneratorModal isOpen onClose={vi.fn()} />);
  await waitFor(() => expect(apiClient.get).toHaveBeenCalledWith('/api/product/status'));
  chooseRegion();
  expect(screen.getByRole('button', { name: 'material.runTool' })).toBeDisabled();
  expect(processMaterialImage).not.toHaveBeenCalled();
});

import { beforeEach, afterEach, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, act } from '@testing-library/react';
import { apiClient } from '@/api/client';
import { saveSlideImageToMaterials } from '@/api/endpoints';
import { SaveSlideToMaterialsButton } from '@/components/preview/SaveSlideToMaterialsButton';

vi.mock('@/hooks/useT', () => ({ useT: (bundle: any) => (key: string) => bundle.zh[key] }));
const source = '/files/owner-project/pages/page_v2.png';
const props = { projectId: 'owner-project', pageId: 'page', imageUrl: source, pageNumber: 2, title: '节水行动', versionNumber: 2 };
let get: ReturnType<typeof vi.spyOn>;
let post: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  get = vi.spyOn(apiClient, 'get').mockResolvedValue({ data: new Blob(['existing image bytes'], { type: 'image/png' }) });
  post = vi.spyOn(apiClient, 'post').mockResolvedValue({ data: { success: true, data: { id: 'private-material' } } });
});
afterEach(() => vi.restoreAllMocks());

it('only copies the selected image on demand, with no caption or model request', async () => {
  render(<SaveSlideToMaterialsButton {...props} />);
  expect(get).not.toHaveBeenCalled(); expect(post).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '将此页保存到素材库' }));
  await waitFor(() => expect(screen.getByRole('button', { name: '已保存到素材库' })).toBeDisabled());
  expect(get).toHaveBeenCalledWith(source, { responseType: 'blob' });
  expect(post).toHaveBeenCalledTimes(1);
  expect(post.mock.calls[0][0]).toBe('/api/materials/upload');
  const form = post.mock.calls[0][1] as FormData;
  const file = form.get('file') as File;
  expect(file.name).toBe('第2页_节水行动_v2.png');
  expect(file.type).toBe('image/png');
  expect(file.size).toBe('existing image bytes'.length);
  expect(Array.from(form.keys())).toEqual(['file']);
  expect(screen.getByText('已保存到我的素材库，可在选择模板时复用。')).toBeInTheDocument();
});

it('disables empty and busy-generation pages without requesting anything', () => {
  const view = render(<SaveSlideToMaterialsButton {...props} imageUrl={undefined} />);
  expect(screen.getByRole('button')).toBeDisabled();
  view.rerender(<SaveSlideToMaterialsButton {...props} disabled />);
  expect(screen.getByRole('button')).toBeDisabled();
  expect(get).not.toHaveBeenCalled(); expect(post).not.toHaveBeenCalled();
});

it('ignores rapid duplicate clicks and keeps page/version selection fixed during save', async () => {
  let complete!: (value: any) => void;
  post.mockReturnValue(new Promise(resolve => { complete = resolve; }));
  const view = render(<SaveSlideToMaterialsButton {...props} />);
  const button = screen.getByRole('button', { name: '将此页保存到素材库' });
  fireEvent.click(button); fireEvent.click(button);
  await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
  view.rerender(<SaveSlideToMaterialsButton {...props} pageId="other-page" imageUrl="/files/owner-project/pages/other.png" pageNumber={3} />);
  await act(async () => { complete({ data: { success: true, data: { id: 'private-material' } } }); });
  expect(screen.getByRole('button', { name: '将此页保存到素材库' })).toBeEnabled();
  expect((post.mock.calls[0][1] as FormData).get('file')).toHaveProperty('name', '第2页_节水行动_v2.png');
  view.rerender(<SaveSlideToMaterialsButton {...props} />);
  expect(screen.getByRole('button', { name: '已保存到素材库' })).toBeDisabled();
  view.rerender(<SaveSlideToMaterialsButton {...props} imageUrl="/files/owner-project/pages/page_v3.png" versionNumber={3} />);
  expect(screen.getByRole('button', { name: '将此页保存到素材库' })).toBeEnabled();
  expect(get).toHaveBeenCalledTimes(1);
});

it.each(['https://outside.example/image.png', '/files/other-project/pages/secret.png'])(
  'refuses an external or mismatched source before any request: %s', async url => {
    await expect(saveSlideImageToMaterials('owner-project', url, 'page')).rejects.toThrow('无法读取当前页图片');
    expect(get).not.toHaveBeenCalled(); expect(post).not.toHaveBeenCalled();
  });

it('does not upload a forbidden or missing image and does not retry', async () => {
  get.mockRejectedValue(new Error('not found'));
  render(<SaveSlideToMaterialsButton {...props} />);
  fireEvent.click(screen.getByRole('button', { name: '将此页保存到素材库' }));
  await screen.findByText('保存未完成，请查看素材库后再试。');
  expect(get).toHaveBeenCalledTimes(1); expect(post).not.toHaveBeenCalled();
  expect(screen.getByRole('button', { name: '将此页保存到素材库' })).toBeEnabled();
});

it('does not save HTML or empty image responses', async () => {
  for (const blob of [new Blob(['error page'], { type: 'text/html' }), new Blob([], { type: 'image/png' })]) {
    get.mockResolvedValueOnce({ data: blob });
    await expect(saveSlideImageToMaterials('owner-project', source, 'page')).rejects.toThrow('图片不可用');
  }
  expect(post).not.toHaveBeenCalled();
});

it('keeps a storage failure visible and never marks the image saved', async () => {
  post.mockRejectedValue({ response: { data: { error: { message: '私人存储空间不足，已有文件仍可下载。' } } } });
  render(<SaveSlideToMaterialsButton {...props} />);
  fireEvent.click(screen.getByRole('button', { name: '将此页保存到素材库' }));
  await screen.findByText('私人存储空间不足，已有文件仍可下载。');
  expect(screen.queryByRole('button', { name: '已保存到素材库' })).not.toBeInTheDocument();
  expect(post).toHaveBeenCalledTimes(1);
});

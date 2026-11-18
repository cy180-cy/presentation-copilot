import { describe, expect, it, vi, afterEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { resolve } from 'node:path';
import { getTemplateFile, PRESET_TEMPLATES } from '@/components/shared/TemplateSelector';

describe('getTemplateFile', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('returns a File when the preset template response is an image', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(new Blob(['image-bytes'], { type: 'image/png' }), {
        status: 200,
        headers: { 'content-type': 'image/png' },
      })
    );

    const file = await getTemplateFile('1', []);

    expect(file).toBeInstanceOf(File);
    expect(file?.name).toBe('template_y.png');
    expect(file?.type).toBe('image/png');
  });

  it('rejects a preset template response that is html', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response('<html>not found</html>', {
        status: 200,
        headers: { 'content-type': 'text/html' },
      })
    );

    const file = await getTemplateFile('1', []);

    expect(file).toBeNull();
  });

  it('rejects a failed user template response', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response('missing', {
        status: 404,
        headers: { 'content-type': 'text/plain' },
      })
    );

    const file = await getTemplateFile('template-001', [
      {
        template_id: 'template-001',
        template_image_url: '/files/user-templates/template-001/template.png',
        created_at: '2026-05-29T00:00:00Z',
      },
    ]);

    expect(file).toBeNull();
  });

  it('displays and sends the same versioned clean glass reference bytes', async () => {
    const preset = PRESET_TEMPLATES.find(item => item.id === '3')!;
    expect(preset.preview).toBe('/templates/template_glass_clean_v1.png');
    expect(preset.thumb).toBe('/templates/template_glass_clean_v1-thumb.webp');
    const bytes = readFileSync(resolve('public/templates/template_glass_clean_v1.png'));
    expect(createHash('sha256').update(bytes).digest('hex'))
      .toBe('ce500cf9966ad04500d3829dbcfb38473be3fa18b589b81f1d64244ca324ea8e');
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue({ ok: true,
      headers: new Headers({ 'content-type': 'image/png' }),
      blob: async () => new Blob([bytes], { type: 'image/png' }),
    } as Response);
    const file = await getTemplateFile('3', []);
    expect(fetch).toHaveBeenCalledOnce();
    expect(fetch).toHaveBeenCalledWith(preset.preview);
    expect(file?.name).toBe('template_glass_clean_v1.png');
    const content = await new Promise<ArrayBuffer>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result as ArrayBuffer);
      reader.onerror = reject;
      reader.readAsArrayBuffer(file!);
    });
    expect(createHash('sha256').update(new Uint8Array(content)).digest('hex'))
      .toBe('ce500cf9966ad04500d3829dbcfb38473be3fa18b589b81f1d64244ca324ea8e');
    const old = readFileSync(resolve('public/templates/template_glass.png'));
    expect(createHash('sha256').update(old).digest('hex'))
      .toBe('ef8c912a79e78ae879d3cb9c6e98c5a9d17841900394f215e184ebdd5619d6c4');
  });
});

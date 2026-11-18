import { beforeEach, describe, expect, test } from 'vitest';
import { normalizeErrorMessage, normalizeRenovationErrorMessage } from '@/utils';
import { SERVICE_UNAVAILABLE } from '@/config/product';

describe('public error messages', () => {
  beforeEach(() => {
    localStorage.setItem('banana-slides-language', 'zh-CN');
    localStorage.setItem('i18nextLng', 'zh-CN');
  });

  test.each([
    '401 OpenAI OAuth is not connected for codex export',
    "HTTPSConnectionPool(host='chatgpt.com'): SSLEOFError Max retries exceeded",
    "HTTPSConnectionPool(host='api.openai.com'): Max retries exceeded",
    'Failed to get upload URL: MinerU API returned 401 Unauthorized: token expired',
    'MinerU parsing failed: Failed to get upload URL: user authenticate failed',
    'OpenAI API returned 401 Unauthorized',
    'MinerU parsing failed: File upload failed: 403 Forbidden',
    '401 Unauthorized',
  ])('uses a business message for technical failure %s', raw => {
    expect(normalizeErrorMessage(raw)).toBe(SERVICE_UNAVAILABLE);
    expect(normalizeRenovationErrorMessage(raw)).toBe(SERVICE_UNAVAILABLE);
  });

  test('handles non-string errors without crashing and preserves business input checks', () => {
    expect(typeof normalizeErrorMessage({ error: 'boom', status: 500 } as any)).toBe('string');
    expect(normalizeErrorMessage('请先填写页面描述')).toBe('请先填写页面描述');
  });

  test('does not restore provider recovery instructions after a language switch', () => {
    localStorage.setItem('banana-slides-language', 'en');
    expect(normalizeRenovationErrorMessage('MinerU parsing failed: token expired')).toBe(SERVICE_UNAVAILABLE);
  });
});

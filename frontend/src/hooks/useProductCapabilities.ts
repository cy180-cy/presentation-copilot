import { useCallback, useEffect } from 'react';
import { create } from 'zustand';
import { apiClient } from '@/api/client';
import { PRODUCT_MODE } from '@/config/product';
import { capabilityFailure } from '@/utils/publicMessages';

export type Capability = 'text' | 'text_generate' | 'template_generate' | 'slide_edit' | 'material_region_edit' | 'renovation_parse';
export interface CapabilityState {
  implemented: boolean; provider_supported: boolean; configured: boolean;
  available: boolean; verified: boolean; reason: string | null; message: string;
}
interface ProductStatus { mode: 'preview' | 'live'; capabilities: Partial<Record<Capability, CapabilityState>> }
const useStatus = create<{ status: ProductStatus | null }>(() => ({ status: null }));
let pending: Promise<void> | null = null;
export function refreshCapabilities(): Promise<void> {
  if (!pending) pending = apiClient.get('/api/product/status').then(response => {
    useStatus.setState({ status: response.data?.data || null });
  }).catch(() => { useStatus.setState({ status: null }); }).finally(() => { pending = null; });
  return pending;
}

/** Read-only status: no connection tests. Fail closed and refresh after focus/usage. */
export function useProductCapabilities(active = true) {
  const status = useStatus(s => s.status);
  useEffect(() => {
    if (!PRODUCT_MODE || !active) return;
    void refreshCapabilities();
    const refresh = () => { void refreshCapabilities(); };
    window.addEventListener('focus', refresh);
    const timer = window.setInterval(refresh, 15000);
    return () => { window.removeEventListener('focus', refresh); window.clearInterval(timer); };
  }, [active]);
  const can = useCallback((name: Capability) => !PRODUCT_MODE || status?.capabilities[name]?.available === true, [status]);
  const message = useCallback((name: Capability) => capabilityFailure(status?.capabilities[name]?.reason), [status]);
  return { status, can, message };
}

import { useRef, useState } from 'react';
import { Check, ImagePlus } from 'lucide-react';
import { Button } from '@/components/shared/Button';
import { useToast } from '@/components/shared/Toast';
import { saveSlideImageToMaterials } from '@/api/endpoints';
import { useT } from '@/hooks/useT';

const messages = {
  zh: { save: '将此页保存到素材库', saving: '正在保存…', saved: '已保存到素材库',
    hint: '保存当前页图片，之后可从素材库选作风格模板',
    success: '已保存到我的素材库，可在选择模板时复用。', failed: '保存未完成，请查看素材库后再试。' },
  en: { save: 'Save this slide to materials', saving: 'Saving…', saved: 'Saved to materials',
    hint: 'Save this slide image for reuse as a style reference',
    success: 'Saved to My Materials. You can reuse it when choosing a template.', failed: 'Save did not complete. Check your materials before trying again.' },
};

interface Props {
  projectId: string;
  pageId: string;
  imageUrl?: string;
  pageNumber: number;
  title?: string;
  versionNumber?: number;
  disabled?: boolean;
}

export function SaveSlideToMaterialsButton({ projectId, pageId, imageUrl, pageNumber, title, versionNumber, disabled }: Props) {
  const t = useT(messages);
  const { show, ToastContainer } = useToast();
  const busy = useRef(false);
  const [saving, setSaving] = useState(false);
  const [savedImages, setSavedImages] = useState<Set<string>>(() => new Set());
  const key = JSON.stringify([projectId, pageId, imageUrl]);
  const saved = savedImages.has(key);

  const save = async () => {
    if (!imageUrl || disabled || saved || busy.current) return;
    busy.current = true;
    setSaving(true);
    // Freeze the selected page/version at click time, including its readable name.
    const name = [`第${pageNumber}页`, title, versionNumber ? `v${versionNumber}` : undefined]
      .filter(Boolean).join('_').replace(/[<>:"/\\|?*\u0000-\u001f]/g, '_').slice(0, 100);
    try {
      const response = await saveSlideImageToMaterials(projectId, imageUrl, name);
      if (!response.data?.id) throw new Error(t('failed'));
      setSavedImages(previous => new Set(previous).add(key));
      show({ message: t('success'), type: 'success' });
    } catch (error: any) {
      show({ message: error.response?.data?.error?.message || t('failed'), type: 'error' });
    } finally {
      busy.current = false;
      setSaving(false);
    }
  };

  return <>
    <Button variant="secondary" size="sm" icon={saved ? <Check size={16} /> : <ImagePlus size={16} />}
      onClick={save} disabled={!imageUrl || disabled || saving || saved} loading={saving}
      title={t('hint')} className="whitespace-nowrap shrink-0">
      {saving ? t('saving') : saved ? t('saved') : t('save')}
    </Button>
    <ToastContainer />
  </>;
}

import type { Page } from '@/types';

/** Surface a concrete content mismatch only; routine verification state stays internal. */
export function DescriptionConsistencyNotice({ state, hasImage }: {
  state: Page['description_image_state']; hasImage: boolean;
}) {
  if (!hasImage || state !== 'needs_review') return null;
  return <p role="alert" className="mx-4 my-2 text-xs text-gray-500 dark:text-foreground-tertiary">
    描述可能与当前图片不同，重新生成前请确认文字与数字。
  </p>;
}

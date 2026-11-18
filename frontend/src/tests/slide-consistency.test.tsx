import { render, screen } from '@testing-library/react';
import { it, expect } from 'vitest';
import { DescriptionConsistencyNotice } from '@/components/preview/DescriptionConsistencyNotice';
it.each([undefined,'synced_by_instruction','visual_only','generated_from_description','no_image'] as const)(
 'does not expose routine or unknown verification state %s',state=>{
  const view=render(<DescriptionConsistencyNotice state={state} hasImage/>);
  expect(view.container).toBeEmptyDOMElement();
});
it('preserves only a concrete content mismatch warning without internal diagnostics',()=>{
 const view=render(<DescriptionConsistencyNotice state="needs_review" hasImage/>);
 expect(screen.getByRole('alert')).toHaveTextContent('描述可能与当前图片不同，重新生成前请确认文字与数字。');
 expect(screen.queryByRole('status')).not.toBeInTheDocument();
 view.rerender(<DescriptionConsistencyNotice state="needs_review" hasImage={false}/>);
 expect(view.container).toBeEmptyDOMElement();
});

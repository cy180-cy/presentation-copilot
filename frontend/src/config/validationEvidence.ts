import type { Capability } from '@/hooks/useProductCapabilities';

/** Manually reviewed historical evidence, NOT runtime permission or a health probe.
 * Source: docs/PROJECT_PLAN.md, PC-005 through PC-008.3; reviewed in PC-009.
 * Scope is the preset and sample used at that time, not arbitrary future models.
 * Update only after reviewing actual artifacts; never infer success from configuration.
 */
export const VALIDATION_REVIEW_DATE = '2026-10-18';
export const VALIDATION_EVIDENCE = [
  { id: 'text', label: '文本规划', result: '两页样例通过',
    detail: 'PC-005 已真实保存两页大纲与描述；上游曾发生长延迟及故障。访客文本生成未单独实测，不代表当前服务健康。' },
  { id: 'text_generate', label: '文字风格生图', result: '待真实验证',
    detail: '尚未完成无图片模板的真实生图验收，不能用带模板生图结果替代。' },
  { id: 'template_generate', label: '带模板图片生成', result: '单次链路通过',
    detail: '两页个人样例及访客单页链路已有真实记录。PC-008.3 三条行动和18处正确、没有第四项，但多出含错字的标题；执行完成不等于内容完全正确。' },
  { id: 'slide_edit', label: '页内自然语言修改', result: '单案例通过',
    detail: 'PC-005 第二页12处改15处，旧图片版本保留、第一页不变。该旧流程当时未同步描述；后续规则已补齐，不回填旧作品。' },
  { id: 'slide_region_edit', label: '页内选区／参考图修改', result: '单案例通过',
    detail: 'PC-006 后续框选15改18，图片、描述和大纲一致，旧版本保留。不是像素级锁定，也不表示历史文字可随图片完整回滚。' },
  { id: 'material_region_edit', label: '素材框选编辑', result: '待真实验证',
    detail: '选图、框选交互与模拟链路已有验证，真实生成新素材的效果尚未验收。' },
  { id: 'pptx_export', label: '普通图片式 PPTX 导出', result: '单页及两页结构检查通过',
    detail: '真实图片已嵌入并与选中版本核对，文字不可逐字编辑。桌面 PowerPoint 打开效果待人工检查。' },
  { id: 'pdf_export', label: 'PDF 导出', result: '单页及两页渲染检查通过',
    detail: '实际样例已核对页数、选中图片与渲染；保留图片自身内容缺陷，不代表其他输入或高级导出通过。' },
  { id: 'visitor_tracking', label: '访客与任务详情', result: '限定链路通过',
    detail: '访客A浏览器单页生成、追踪、私有访问及普通导出通过；B为同站独立HTTP会话。双独立浏览器及公网安全尚未验收。' },
] as const;

export function evidenceFor(capability: Capability) {
  return VALIDATION_EVIDENCE.find(item => item.id === capability);
}

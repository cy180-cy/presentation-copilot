export type PagePlan = {requested_pages?: unknown; default_auto_pages?: unknown;
  planned_page_count?: unknown; max_pages?: unknown; page_count_source?: unknown;
  target_pages?: unknown; resolved_pages?: unknown; affordable_pages_at_start?: unknown; page_limit_reason?: unknown};

export function PagePlanSummary({snapshot}: {snapshot: PagePlan}) {
  if (snapshot.planned_page_count == null) return <p>页数规划：历史任务未记录。</p>;
  const count = (n: unknown) => typeof n === 'number' ? `${n}页` : '未记录';
  if (snapshot.resolved_pages != null) return <p>页数计划：目标{count(snapshot.target_pages)}，本次{count(snapshot.resolved_pages)}；单任务最多{count(snapshot.max_pages)}，开始时额度支持{count(snapshot.affordable_pages_at_start)}。{snapshot.page_limit_reason === 'none' ? '无需缩减。' : '按单任务上限与可用额度缩减。'}计划不代表内容已核查。</p>;
  const source = snapshot.page_count_source === 'user_explicit' ? '用户指定' : snapshot.page_count_source === 'default_auto' ? '默认自动规划' : '未记录';
  return <p>页数规划：{count(snapshot.planned_page_count)}（{source}）；用户要求：{snapshot.requested_pages === null ? '未指定' : count(snapshot.requested_pages)}；默认：{count(snapshot.default_auto_pages)}；硬上限：{count(snapshot.max_pages)}。</p>;
}

import { useEffect, useState } from 'react';
import { apiClient } from '@/api/client';
import { Modal } from '@/components/shared/Modal';
import { SERVICE_UNAVAILABLE } from '@/config/product';

type Input = { page_id: string; source_version: number | null; has_source_image?: boolean | null;
  aspect_ratio: string | null; resolution: string | null; edit_instruction: string | null };
type Detail = { tracked: boolean; status?: string; operation?: string;
  outputs: {version_number: number | null}[]; snapshot: {pages: Input[]} | null };
type Summary = {task_id: string; operation: string; status: string; created_at: string};
const labels: Record<string, string> = {
  COMPLETED: '已完成', FAILED: '未完成', PENDING: '等待中', PROCESSING: '处理中', INTERRUPTED: '已中断',
};
const operations: Record<string, string> = {
  GENERATE_OUTLINE: '生成大纲', GENERATE_DESCRIPTIONS: '生成描述', GENERATE_PAGE_DESCRIPTION: '生成页面描述',
  GENERATE_IMAGES: '生成图片', GENERATE_PAGE_IMAGE: '生成页面图片', EDIT_PAGE_IMAGE: '修改页面图片',
};
export function TaskDetails({projectId, pageId, versionId}: {projectId: string; pageId?: string; versionId?: string}) {
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState('version');
  const [tasks, setTasks] = useState<Summary[]>([]);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [error, setError] = useState('');
  useEffect(() => { setSelected('version'); setDetail(null); }, [projectId, pageId, versionId]);
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    apiClient.get(`/api/projects/${projectId}/task-details`).then(r => {
      if (!cancelled) setTasks(r.data.data);
    }).catch(() => { if (!cancelled) setTasks([]); });
    return () => { cancelled = true; };
  }, [open, projectId]);
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setDetail(null); setError('');
    if (selected === 'version' && (!pageId || !versionId)) {
      setError('当前页面还没有图片，可查看近期任务。');
      return;
    }
    const params = selected === 'version' ? {page_id: pageId, version_id: versionId} : {task_id: selected};
    apiClient.get(`/api/projects/${projectId}/task-details`, {params}).then(r => {
      if (!cancelled) setDetail(r.data.data);
    }).catch(() => { if (!cancelled) setError('暂时无法读取任务记录，请稍后重新打开。'); });
    return () => { cancelled = true; };
  }, [open, selected, projectId, pageId, versionId]);
  return <>
    <button type="button" onClick={() => setOpen(true)} className="px-3 py-2 rounded-lg border border-gray-200 text-sm hover:bg-amber-50">任务详情</button>
    <Modal isOpen={open} onClose={() => setOpen(false)} title="任务详情" size="xl">
      <div className="space-y-4 text-sm text-gray-700 break-words">
        <label className="block">查看范围
          <select aria-label="任务详情查看范围" value={selected} onChange={e => setSelected(e.target.value)} className="mt-1 w-full p-2 border rounded-lg">
            <option value="version">当前选中的图片版本</option>
            {tasks.map(t => <option key={t.task_id} value={t.task_id}>{t.created_at.slice(0, 19).replace('T', ' ')} · {operations[t.operation] || '任务'} · {labels[t.status] || '处理中'}</option>)}
          </select>
        </label>
        {error && <p role="alert">{error}</p>}
        {!error && !detail && <p>正在读取记录…</p>}
        {detail && <>
          {detail.status && <p>状态：{labels[detail.status] || '处理中'}</p>}
          {detail.operation && <p>操作：{operations[detail.operation] || '任务'}</p>}
          {!detail.tracked && <p>暂无任务详情，仍可查看和导出当前图片。</p>}
          {['FAILED', 'INTERRUPTED'].includes(detail.status || '') && <p role="alert">{SERVICE_UNAVAILABLE}</p>}
          {detail.snapshot?.pages?.map((p, i) => <div key={p.page_id || i} className="rounded-lg border p-3 space-y-1">
            {p.source_version != null && <p>源图片：第 {p.source_version} 版</p>}
            {p.has_source_image === false && <p>首次生成</p>}
            {(p.aspect_ratio || p.resolution) && <p>画面：{[p.aspect_ratio, p.resolution].filter(Boolean).join(' · ')}</p>}
            {p.edit_instruction && <p>修改指令：{p.edit_instruction}</p>}
          </div>)}
          {!!detail.outputs.length && <p>已保存结果：{detail.outputs.map(o => o.version_number == null ? '页面文字' : `图片第 ${o.version_number} 版`).join('、')}。</p>}

        </>}
      </div>
    </Modal>
  </>;
}

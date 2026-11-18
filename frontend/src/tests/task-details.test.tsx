import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { vi, it, expect, beforeEach } from 'vitest';
import { TaskDetails } from '@/components/preview/TaskDetails';
import { apiClient } from '@/api/client';
import { PageLimitNotice } from '@/components/shared/PageLimitNotice';
import { PagePlanSummary } from '@/components/shared/PagePlanSummary';
vi.mock('@/api/client', () => ({ apiClient: { get: vi.fn() } }));
vi.mock('@/components/shared/Modal', () => ({ Modal: ({isOpen, children}: any) => isOpen ? <div>{children}</div> : null }));
beforeEach(() => vi.clearAllMocks());

it('keeps saved planning and missing history available to the admin summary', () => {
  const view=render(<PagePlanSummary snapshot={{requested_pages:5,default_auto_pages:6,planned_page_count:5,max_pages:8,page_count_source:'user_explicit'}}/>);
  expect(screen.getByText(/5页（用户指定）/)).toHaveTextContent('默认：6页；硬上限：8页');
  view.rerender(<PagePlanSummary snapshot={{}}/>);
  expect(screen.getByText('页数规划：历史任务未记录。')).toBeInTheDocument();
});

it('reads the selected version and never displays untracked technical explanations', async () => {
  vi.mocked(apiClient.get).mockImplementation(async (_url, config) => ({data: {data: config?.params ? {
    tracked:false, message:'PC-007 历史任务未记录，preview', snapshot:null, calls:[], outputs:[]
  } : []}}));
  const view=render(<TaskDetails projectId="p" pageId="page" versionId="v1"/>);
  fireEvent.click(screen.getByText('任务详情'));
  await screen.findByText('暂无任务详情，仍可查看和导出当前图片。');
  expect(apiClient.get).toHaveBeenCalledWith('/api/projects/p/task-details', {params:{page_id:'page',version_id:'v1'}});
  expect(view.container).not.toHaveTextContent(/PC-007|preview|历史任务未记录/);
  view.rerender(<TaskDetails projectId="p" pageId="page" versionId="v2"/>);
  await waitFor(()=>expect(apiClient.get).toHaveBeenCalledWith('/api/projects/p/task-details', {params:{page_id:'page',version_id:'v2'}}));
});

it('retains business details and task selection, hiding runtime, cost and validation data', async () => {
  const detail={task_id:'internal-task',status:'COMPLETED',operation:'EDIT_PAGE_IMAGE',tracked:true,
    message:'PC-008 preview 验收通过', snapshot:{model:'private-model', provider:'private-provider',
      config_version:'secret-revision', runtime_authorization_id:'authorization-id', affordable_pages:3,
      pages:[{page_id:'page',source_version:1,aspect_ratio:'16:9',resolution:'2K',
        edit_instruction:'将15改为18',prompt_hash:'prompt-hash',input_hash:'input-hash',references:[]}]},
    outputs:[{version_number:2,call_id:'private-call'}],
    calls:[{call_id:'private-call',status:'response_saved',elapsed_seconds:39,usage:{input_tokens:100},
      reserved_credits:4,estimated_credits:4,actual_credits:null,provider_request_id:'private-request',failure_stage:'configuration_or_budget'}]};
  vi.mocked(apiClient.get).mockImplementation(async (_url, config)=>({data:{data:config?.params?detail:[{task_id:'t2',operation:'EDIT_PAGE_IMAGE',status:'COMPLETED',created_at:'2026-10-20T10:00:00'}]}}));
  const view=render(<TaskDetails projectId="p" pageId="page" versionId="v2"/>);
  fireEvent.click(screen.getByText('任务详情'));
  await screen.findByText('已保存结果：图片第 2 版。');
  expect(screen.getByText('状态：已完成')).toBeInTheDocument();
  expect(screen.getByText('源图片：第 1 版')).toBeInTheDocument();
  expect(screen.getByText('修改指令：将15改为18')).toBeInTheDocument();
  expect(screen.getByText('画面：16:9 · 2K')).toBeInTheDocument();
  expect(view.container).not.toHaveTextContent(/private-|internal-|secret-|authorization-id|prompt-hash|input-hash|preview|live|PC-008|积分|预留|估算|输入检查|验收|配置|预算|Provider|调用/);
  fireEvent.change(screen.getByLabelText('任务详情查看范围'),{target:{value:'t2'}});
  await waitFor(()=>expect(apiClient.get).toHaveBeenCalledWith('/api/projects/p/task-details',{params:{task_id:'t2'}}));
});

it('does not turn a failed task into success or expose its blocker',async()=>{
 vi.mocked(apiClient.get).mockImplementation(async(_url,config)=>({data:{data:config?.params?{
  tracked:true,status:'FAILED',message:'预算停止线',snapshot:null,outputs:[],calls:[]}:[]}}));
 render(<TaskDetails projectId="p" pageId="page" versionId="v1"/>);fireEvent.click(screen.getByText('任务详情'));
 expect(await screen.findByText('状态：未完成')).toBeInTheDocument();
 expect(screen.getByRole('alert')).toHaveTextContent('生成服务暂不可用，请稍后重试。');
 expect(screen.queryByText(/停止线/)).not.toBeInTheDocument();
});

it('shows completion counts only, without reduction causes or invented results',()=>{
 const view=render(<PageLimitNotice message={null}/>);
 expect(screen.queryByRole('status')).not.toBeInTheDocument();
 const cases=[['默认计划生成5页，受当前可用额度限制，本次已完成3页。','默认计划生成5页，本次已完成3页。'],
 ['你请求生成12页，当前单任务最多支持10页，本次已完成10页。','你请求生成12页，本次已完成10页。'],
 ['你请求生成8页，结合单任务上限和当前可用额度，本次已完成6页。','你请求生成8页，本次已完成6页。']];
 for(const [input,output] of cases){view.rerender(<PageLimitNotice message={input}/>);expect(screen.getByRole('status')).toHaveTextContent(output);expect(view.container).not.toHaveTextContent(/预算|额度|上限/);}
 view.rerender(<PageLimitNotice message="预算预留完成，计划3页"/>);
 expect(screen.queryByRole('status')).not.toBeInTheDocument();
});

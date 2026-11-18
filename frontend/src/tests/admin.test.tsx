import {beforeEach,expect,it,vi} from 'vitest';
import {render,screen,fireEvent,waitFor,cleanup,act} from '@testing-library/react';
import {MemoryRouter,Routes,Route} from 'react-router-dom';
import userEvent from '@testing-library/user-event';
import {AdminLogin,AdminConsole,chinaLocal} from '../pages/Admin';
import {adminRequest} from '../api/admin';
vi.mock('../api/admin',()=>({adminRequest:vi.fn()}));
const base={revision:0,draft_revision:0,mode:'preview',paused:false,applied_version_in_process:0,active_tasks:0,queue_limit:2,
 credentials:{ready:true,preset:'verified-pair',source:'.env.copilot',models:[]},ledger:{text:4,image:11,reserved_credits:47.2,paused:false},
 reasons:{text:'preview',image:'preview'},resources_ready:true,remaining_reservation:null,external_unknown:0,
 draft:{name:'',mode:'preview',authorization:null,visitor:{window_seconds:86400,text:0,image:0,credits:0,pages:8,default_auto_pages:6}},
 active:{mode:'preview'},has_saved_draft:false};
function show(path='/admin'){render(<MemoryRouter initialEntries={[path]}><Routes><Route path="/admin" element={<AdminConsole/>}/><Route path="/admin/login" element={<AdminLogin/>}/></Routes></MemoryRouter>);}
beforeEach(()=>{cleanup();vi.clearAllMocks();});
it('daily use removes only expiry and retains balance and limits in the draft',async()=>{
 let state:any={...structuredClone(base),mode:'live',authorization_valid:true};
 const auth={id:'offline-daily-auth',not_before:'2026-10-19T08:31:00Z',expires_at:'2026-10-26T08:31:00Z',credits:18.71,text:24,image:23,roles:['text','image'],operations:['template_generate'],balance:{id:'balance-existing',credits:18.71,image_2k:3,text_fallback:0.8}};
 state.active={...state.draft,mode:'live',authorization:structuredClone(auth)};state.draft=structuredClone(state.active);
 vi.mocked(adminRequest).mockImplementation(async(p,_method,body:any)=>{
  if(p==='session')return {authenticated:true};
  if(p==='draft')state={...state,draft:body.draft,draft_revision:1,has_saved_draft:true};
  return structuredClone(state);
 });
 show();await screen.findByLabelText('运行概览指标');fireEvent.click(screen.getByRole('button',{name:'运行授权与额度'}));
 expect(screen.getByLabelText(/^结束时间/)).toBeInTheDocument();
 fireEvent.change(screen.getByLabelText('使用期限'),{target:{value:'daily'}});
 expect(screen.queryByLabelText(/^结束时间/)).not.toBeInTheDocument();
 expect(screen.getByText(/无需反复续期/)).toBeInTheDocument();
 fireEvent.click(screen.getByText('1. 保存草稿'));await screen.findByText('草稿已保存，执行模式未改变。');
 expect(state.draft.authorization).toEqual({...auth,validity:'daily',expires_at:null});
 expect(state.active.authorization).toEqual(auth);
 expect(vi.mocked(adminRequest).mock.calls.some(([p])=>p==='apply')).toBe(false);
 fireEvent.change(screen.getByLabelText('使用期限'),{target:{value:'fixed'}});
 expect(screen.getByLabelText(/^结束时间/)).toHaveValue('');
});
it('uninitialized account has no default login and cannot read console',async()=>{
 vi.mocked(adminRequest).mockResolvedValue({authenticated:false,initialized:false});show();
 expect(await screen.findByText('开发者登录')).toBeInTheDocument();expect(screen.getByRole('button',{name:'登录'})).toBeDisabled();
 expect(vi.mocked(adminRequest).mock.calls.every(([p])=>p==='session')).toBe(true);
});
it('overview contains only five metrics and does not probe or apply',async()=>{
 vi.mocked(adminRequest).mockImplementation(async p=>p==='session'?{authenticated:true}:{...structuredClone(base),remaining_reservation:18.71,
  manual_balance:{id:'balance-test',created_at:'2026-10-26T00:00:00Z',reported_balance:21.71,estimated_spent:3,available:18.71,in_flight_reserved:0,success_counts:{image:1,text:0}}});
 show();const metrics=await screen.findByLabelText('运行概览指标');
 expect(metrics).toBeInTheDocument();
 expect(screen.getByLabelText('运行概览指标').querySelectorAll('dt')).toHaveLength(5);
 for(const label of ['执行模式','在执行任务','累计用量','停止线','剩余可用'])expect(screen.getByText(label)).toBeInTheDocument();
 expect(screen.getByText('3 积分')).toBeInTheDocument();expect(screen.getByText('21.71')).toBeInTheDocument();expect(screen.getByText('18.71')).toBeInTheDocument();
 expect(screen.queryByText('管理员暂停')).not.toBeInTheDocument();expect(screen.queryByText('队列上限')).not.toBeInTheDocument();
 expect(screen.queryByText(/本地累计预留/)).not.toBeInTheDocument();expect(screen.queryByRole('button',{name:'暂停生成'})).not.toBeInTheDocument();
 expect(vi.mocked(adminRequest).mock.calls.map(x=>x[0])).toEqual(['session','overview']);
});
it('keeps historical ledger details and task records under the renamed tab',async()=>{
 vi.mocked(adminRequest).mockImplementation(async p=>p==='session'?{authenticated:true}:p==='overview'?{...structuredClone(base),remaining_reservation:13.109536,
  quota_cycle:{id:'synthetic-cycle-001',credit_limit:27.71,created_at:'2026-10-23T00:00:00Z'},
  lifetime_ledger:{text:8,image:22,reserved_credits:94.4},
  settlement:{settled_actual:72.290464,pending_reserved:8.8,released_credits:15.309536,additional_credits:2,total_occupied:84.090464}}:{items:[],has_more:false});
 show();await screen.findByLabelText('运行概览指标');fireEvent.click(screen.getByRole('button',{name:'运行记录'}));
 fireEvent.click(screen.getByText('历史额度与结算记录'));
 expect(screen.getByText(/保留的全历史原始记录/)).toHaveTextContent('94.4');
 expect(screen.getByText(/已结算实扣：/)).toHaveTextContent('72.290464');expect(screen.getByText(/待核对预留：/)).toHaveTextContent('8.8');
 expect(screen.getByLabelText('任务状态筛选')).toBeInTheDocument();expect(screen.getByText('管理操作记录')).toBeInTheDocument();
});
it('saving a draft never invokes apply and confirmation is required separately',async()=>{
 vi.mocked(adminRequest).mockImplementation(async(p,_m,body:any)=>{
  if(p==='session')return {authenticated:true};if(p==='overview')return structuredClone(base);
  if(p==='draft')return {...structuredClone(base),draft:body.draft,has_saved_draft:true,draft_revision:1};
  if(p==='validate')return {confirmation:'review-token',changes:{mode:'preview'},blockers:[],can_apply:true};
  return structuredClone(base);
 });show();await screen.findByLabelText('运行概览指标');fireEvent.click(screen.getByRole('button',{name:'运行授权与额度'}));
 fireEvent.change(screen.getByLabelText('授权显示名称'),{target:{value:'隔离草稿'}});
 fireEvent.click(screen.getByText('1. 保存草稿'));await screen.findByText('草稿已保存，执行模式未改变。');
 expect(vi.mocked(adminRequest).mock.calls.some(([p])=>p==='apply')).toBe(false);
 fireEvent.click(screen.getByText('2. 校验并查看变更'));await screen.findByText('应用前确认');
 expect(screen.getByText('3. 确认应用')).toBeDisabled();fireEvent.change(screen.getByLabelText('应用确认文字'),{target:{value:'确认'}});
 fireEvent.click(screen.getByText('3. 确认应用'));await waitFor(()=>expect(adminRequest).toHaveBeenCalledWith('apply','POST',{confirmation:'review-token',phrase:'确认'}));
});
it('shows task unknown cost as unknown, not zero or inferred actual',async()=>{
 vi.mocked(adminRequest).mockImplementation(async p=>{
  if(p==='session')return {authenticated:true};if(p==='overview')return structuredClone(base);
  if(p.startsWith('tasks?'))return {items:[{task_id:'mock-1',status:'COMPLETED',operation:'GENERATE_PAGE_IMAGE',created_at:'2026-10-19T08:00:00',visitor:'synthetic-visitor',model:'mock',tracked:true}],has_more:false};
  if(p==='tasks/mock-1')return {task_id:'mock-1',status:'COMPLETED',snapshot:null,calls:[{call_id:'synthetic-call',reserved_credits:4,estimated_credits:4,actual_credits:null}],outputs:[],quality_note:'内容仍需人工核查',message:'模拟测试'};
  return {items:[],has_more:false};
 });show();await screen.findByLabelText('运行概览指标');fireEvent.click(screen.getByRole('button',{name:'运行记录'}));fireEvent.click(await screen.findByRole('button',{name:'详情'}));
 expect(await screen.findByText(/平台实扣：未取得/)).toBeInTheDocument();expect(screen.getByText('内容仍需人工核查')).toBeInTheDocument();
});
it.each([{blockers:[]},{blockers:['运行授权已到期']}])('explains unchanged configuration without offering reapply, retaining blockers $blockers',async({blockers})=>{
 vi.mocked(adminRequest).mockImplementation(async p=>{
  if(p==='session')return {authenticated:true};
  if(p==='validate')return {confirmation:'review-token',changes:{},blockers,can_apply:!blockers.length};
  return {...structuredClone(base),has_saved_draft:true,draft_revision:4};
 });show();await screen.findByLabelText('运行概览指标');
 fireEvent.click(screen.getByRole('button',{name:'运行授权与额度'}));
 fireEvent.click(screen.getByText('2. 校验并查看变更'));
 expect(await screen.findByText('当前草稿与生效配置一致，无需再次应用。')).toBeInTheDocument();
 expect(screen.queryByText('{}')).not.toBeInTheDocument();
 expect(screen.queryByLabelText('应用确认文字')).not.toBeInTheDocument();
 expect(screen.queryByRole('button',{name:'3. 确认应用'})).not.toBeInTheDocument();
 blockers.forEach(blocker=>expect(screen.getByText(blocker)).toBeInTheDocument());
 expect(vi.mocked(adminRequest).mock.calls.map(([path])=>path)).toEqual(['session','overview','validate']);
});
it('time inputs explicitly preserve Shanghai offset',()=>{
 expect(chinaLocal('2026-10-19T01:30:00Z')).toBe('2026-10-19T09:30');expect(chinaLocal('')).toBe('');
 expect(chinaLocal('unfinished-date')).toBe('');
});

async function openQuotaForm(){
 vi.mocked(adminRequest).mockImplementation(async(p,_method,body:any)=>{
  if(p==='session')return {authenticated:true};
  if(p==='draft')return {...structuredClone(base),draft:body.draft,has_saved_draft:true,draft_revision:1};
  return structuredClone(base);
 });
 show();await screen.findByLabelText('运行概览指标');
 fireEvent.click(screen.getByRole('button',{name:'运行授权与额度'}));
}
it('lets all numeric fields be cleared and typed directly, saving numbers only as a draft',async()=>{
 await openQuotaForm();const user=userEvent.setup();
 fireEvent.change(screen.getByLabelText('拟应用模式'),{target:{value:'live'}});
 const edits:[RegExp,string][]=[[/^累计预留停止线/,'97.2'],[/^文本累计请求/,'24'],[/^图片累计请求/,'23'],
  [/^窗口长度/,'86400'],[/^每访客窗口预留积分/,'50.25'],[/^每访客窗口文本/,'20'],[/^每访客窗口图片/,'12'],[/^默认页数/,'4'],[/^单任务最多页数/,'5']];
 for(const [label,text] of edits){
  const entry=screen.getByLabelText(label);expect(entry).toHaveAttribute('type','text');
  await act(async()=>{await user.clear(entry);});expect(entry).toHaveValue('');
  await act(async()=>{await user.type(entry,text);});expect(entry).toHaveValue(text);
 }
 expect(screen.queryAllByRole('spinbutton')).toHaveLength(0);
 expect(vi.mocked(adminRequest).mock.calls.every(([,method])=>!method||method==='GET')).toBe(true);
 fireEvent.click(screen.getByText('1. 保存草稿'));await screen.findByText('草稿已保存，执行模式未改变。');
 const saved=vi.mocked(adminRequest).mock.calls.find(([path])=>path==='draft')![2] as any;
 expect(saved.draft.authorization).toMatchObject({credits:97.2,text:24,image:23,roles:['text','image']});
 expect(saved.draft.visitor).toEqual({window_seconds:86400,credits:50.25,text:20,image:12,pages:5,default_auto_pages:4});
 expect(vi.mocked(adminRequest).mock.calls.some(([path])=>['apply','validate'].includes(path))).toBe(false);
});
it('keeps a cleared field empty and rejects saving it rather than silently using zero',async()=>{
 await openQuotaForm();const entry=screen.getByLabelText(/^每访客窗口图片/);
 await act(async()=>{await userEvent.setup().clear(entry);});fireEvent.click(screen.getByText('1. 保存草稿'));
 expect(await screen.findByRole('alert')).toHaveTextContent('请填写每访客窗口图片尝试上限，不能留空');
 expect(entry).toHaveValue('');expect(vi.mocked(adminRequest).mock.calls.some(([path])=>path==='draft')).toBe(false);
});
it('rejects invalid counts before a request and still accepts an explicit zero',async()=>{
 await openQuotaForm();const entry=screen.getByLabelText(/^每访客窗口图片/);
 for(const text of ['abc','-1','1.5','1e3','Infinity']){
  fireEvent.change(entry,{target:{value:text}});fireEvent.click(screen.getByText('1. 保存草稿'));
  expect(await screen.findByRole('alert')).toHaveTextContent('每访客窗口图片尝试上限请填写不小于0的整数');
  await waitFor(()=>expect(screen.getByText('1. 保存草稿')).toBeEnabled());
 }
 expect(vi.mocked(adminRequest).mock.calls.some(([path])=>path==='draft')).toBe(false);
 fireEvent.change(entry,{target:{value:'0'}});fireEvent.click(screen.getByText('1. 保存草稿'));
 await screen.findByText('草稿已保存，执行模式未改变。');
 const saved=vi.mocked(adminRequest).mock.calls.find(([path])=>path==='draft')![2] as any;
 expect(saved.draft.visitor.image).toBe(0);
});

it('keeps a higher default as a soft target without applying any configuration',async()=>{
 await openQuotaForm();
 const defaultPages=screen.getByLabelText('默认页数'),maxPages=screen.getByLabelText('单任务最多页数');
 fireEvent.change(defaultPages,{target:{value:'9'}});fireEvent.click(screen.getByText('1. 保存草稿'));
 await screen.findByText('草稿已保存，执行模式未改变。');
 const saved=vi.mocked(adminRequest).mock.calls.find(([p])=>p==='draft')![2] as any;
 expect(saved.draft.visitor).toMatchObject({default_auto_pages:9,pages:8});
 expect(maxPages).toHaveValue('8');
 expect(vi.mocked(adminRequest).mock.calls.some(([p])=>p==='apply')).toBe(false);
 expect(screen.getByText(/系统会自动减少页数/)).toBeInTheDocument();
});

it.each(['','1.5','0','-1'])('rejects invalid planning default %j without saving',async(raw)=>{
 await openQuotaForm();fireEvent.change(screen.getByLabelText('默认页数'),{target:{value:raw}});
 fireEvent.click(screen.getByText('1. 保存草稿'));expect(await screen.findByRole('alert')).toHaveTextContent('默认页数');
 expect(vi.mocked(adminRequest).mock.calls.some(([p])=>p==='draft')).toBe(false);
});

it('keeps historical verification details only in the authenticated admin view',async()=>{
 vi.mocked(adminRequest).mockImplementation(async p=>p==='session'?{authenticated:true}:structuredClone(base));show();
 await screen.findByLabelText('运行概览指标');fireEvent.click(screen.getByRole('button',{name:'模型与功能'}));
 fireEvent.click(screen.getByText('历史验证记录'));
 expect(screen.getByText(/PC-008.3 三条行动/)).toBeVisible();
 expect(vi.mocked(adminRequest).mock.calls.every(([,method])=>!method||method==='GET')).toBe(true);
});

it('keeps retired exports absent from admin without enabling or applying',async()=>{
 vi.mocked(adminRequest).mockImplementation(async p=>p==='session'?{authenticated:true}:structuredClone(base));
 show();await screen.findByLabelText('运行概览指标');fireEvent.click(screen.getByRole('button',{name:'模型与功能'}));
 expect(screen.queryByRole('article',{name:'Editable PPTX审计状态'})).not.toBeInTheDocument();
 expect(screen.queryByText(/可编辑\s*PPTX|Editable PPTX/)).not.toBeInTheDocument();
 expect(vi.mocked(adminRequest).mock.calls.map(x=>x[0])).toEqual(['session','overview']);
});

it('manual balance is staged explicitly and a normal save keeps the checkpoint id',async()=>{
 await openQuotaForm();fireEvent.change(screen.getByLabelText('拟应用模式'),{target:{value:'live'}});
 fireEvent.change(screen.getByLabelText('当前平台剩余余额（积分）'),{target:{value:'21.71'}});
 fireEvent.click(screen.getByRole('button',{name:'将此金额设为新的余额基准'}));
 fireEvent.click(screen.getByText('1. 保存草稿'));await screen.findByText('草稿已保存，执行模式未改变。');
 const first=(vi.mocked(adminRequest).mock.calls.find(([p])=>p==='draft')![2] as any).draft;
 expect(first.authorization.balance).toMatchObject({credits:21.71,image_2k:3,text_fallback:0.8});
 expect(first.authorization.balance.id).toMatch(/^balance-/);
 fireEvent.click(screen.getByText('1. 保存草稿'));await waitFor(()=>expect(vi.mocked(adminRequest).mock.calls.filter(([p])=>p==='draft')).toHaveLength(2));
 const second=(vi.mocked(adminRequest).mock.calls.filter(([p])=>p==='draft')[1][2] as any).draft;
 expect(second.authorization.balance).toEqual(first.authorization.balance);
 expect(vi.mocked(adminRequest).mock.calls.some(([p])=>p==='apply')).toBe(false);
});

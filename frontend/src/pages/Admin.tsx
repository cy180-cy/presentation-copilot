import { VALIDATION_EVIDENCE, VALIDATION_REVIEW_DATE } from '@/config/validationEvidence';
import {useEffect, useState, type FormEvent, type ReactNode} from 'react';
import {Link, useNavigate} from 'react-router-dom';
import {adminRequest} from '../api/admin';
import {PagePlanSummary} from '../components/shared/PagePlanSummary';

type Balance = {id:string;credits:number;image_2k:number;text_fallback:number};
type Auth = {id:string;not_before:string;expires_at:string|null;validity?:'fixed'|'daily';credits:number;text:number;image:number;roles:string[];operations:string[];balance?:Balance};
type Draft = {name:string;mode:'preview'|'live';authorization:Auth|null;visitor:{window_seconds:number;text:number;image:number;credits:number;pages:number;default_auto_pages:number}};
type NumericForm<T> = {[K in keyof T]:T[K] extends number?string:T[K]};
type DraftForm = Omit<Draft,'authorization'|'visitor'>&{authorization:NumericForm<Auth>|null;visitor:NumericForm<Draft['visitor']>};
type Overview = {revision:number;draft_revision:number;draft:Draft;active:Draft;has_saved_draft:boolean;paused:boolean;mode:string;
 active_tasks:number;queue_limit:number;resume_confirmation:string;remaining_reservation:number|null;external_unknown:number;authorization_valid:boolean;
 applied_version_in_process:number;resources_ready:boolean;credentials:{ready:boolean;source:string;preset:string;models:{role:string;model:string;provider:string;protocol:string;configured:boolean;validation:string}[]};
 ledger:{text:number;image:number;reserved_credits:number;paused:boolean};reasons:Record<string,string|null>;
 quota_cycle?:{id:string;credit_limit:number;created_at:string}|null;lifetime_ledger?:{text:number;image:number;reserved_credits:number};
 manual_balance?:{id:string;created_at:string;reported_balance:number;estimated_spent:number;available:number;in_flight_reserved:number;success_counts:{image:number;text:number}}|null;
 settlement?:{settled_actual:number;pending_reserved:number;released_credits:number;additional_credits:number;total_occupied:number}};
type Change = {confirmation:string;blockers:string[];can_apply:boolean;changes:Record<string,unknown>};
type Task = {task_id:string;operation:string;status:string;created_at:string;visitor:string|null;model:string|null;tracked:boolean};
type TaskDetail = {task_id:string;status:string;tracked:boolean;quality_note:string;snapshot:Record<string,unknown>|null;calls:Record<string,unknown>[];outputs:Record<string,unknown>[];message:string};
const input='w-full rounded-xl border border-slate-200 px-3 py-2 bg-white text-slate-800';
const button='rounded-xl px-4 py-2 border border-amber-300 bg-amber-50 text-slate-800 disabled:opacity-40';
const reasons:Record<string,string>={preview:'预览模式，生成关闭',admin_paused:'管理员已暂停生成',configuration_applying:'配置正在应用',
 configuration_missing:'模型配置缺失',provider_unsupported:'Provider尚未接入',authorization_required:'没有有效执行范围',
 authorization_expired:'授权未生效或已到期',budget_exhausted:'累计额度不足',previous_failure:'历史失败或账目待核对',request_in_progress:'模型请求正在执行'};
const sections=['运行概览','模型与功能','运行授权与额度','运行记录'];
const value=(v:unknown)=>v==null?'未取得':typeof v==='object'?JSON.stringify(v):String(v);
const field=(label:string,child:ReactNode)=><label className="block space-y-1 text-sm"><span className="font-medium">{label}</span>{child}</label>;
function message(e:unknown){return e instanceof Error?e.message:'操作未完成';}
export function chinaLocal(iso:string|null){const time=iso?Date.parse(iso):NaN;return Number.isFinite(time)?new Date(time+8*3600000).toISOString().slice(0,16):'';}
const isoLocal=(s:string)=>s?`${s}:00+08:00`:'';
function editableDraft(d:Draft):DraftForm{
 return {...d,authorization:d.authorization?{...d.authorization,credits:String(d.authorization.credits),text:String(d.authorization.text),image:String(d.authorization.image)}:null,
  visitor:{window_seconds:String(d.visitor.window_seconds),credits:String(d.visitor.credits),text:String(d.visitor.text),image:String(d.visitor.image),pages:String(d.visitor.pages),default_auto_pages:String(d.visitor.default_auto_pages ?? 5)}};
}
function numericValue(raw:string,label:string,integer=false,min=0){
 const text=raw.trim();
 if(!text)throw new Error(`请填写${label}，不能留空。`);
 const number=Number(text);
 if(!(integer?/^\d+$/:/^(?:\d+\.?\d*|\.\d+)$/).test(text)||!Number.isFinite(number)||(integer&&!Number.isSafeInteger(number))||number<min)
  throw new Error(`${label}请填写不小于${min}的${integer?'整数':'数字'}。`);
 return number;
}
function savedDraft(d:DraftForm):Draft{
 const defaultPages=numericValue(d.visitor.default_auto_pages,'默认页数',true,1),maxPages=numericValue(d.visitor.pages,'单任务最多页数',true,1);
 if(maxPages>50)throw new Error('单任务最多页数不能超过50页。');
 return {...d,authorization:d.authorization?{...d.authorization,credits:numericValue(d.authorization.credits,'累计预留停止线'),
  text:numericValue(d.authorization.text,'文本累计请求尝试停止线',true),image:numericValue(d.authorization.image,'图片累计请求尝试停止线',true)}:null,
  visitor:{window_seconds:numericValue(d.visitor.window_seconds,'窗口长度',true,60),credits:numericValue(d.visitor.credits,'每访客窗口预留积分上限'),
   text:numericValue(d.visitor.text,'每访客窗口文本尝试上限',true),image:numericValue(d.visitor.image,'每访客窗口图片尝试上限',true),pages:maxPages,default_auto_pages:defaultPages}};
}

export function AdminLogin(){
 const [session,setSession]=useState<{authenticated:boolean;initialized:boolean}|null>(null);
 const [password,setPassword]=useState('');const [error,setError]=useState('');const [busy,setBusy]=useState(false);const navigate=useNavigate();
 useEffect(()=>{adminRequest('session').then(s=>{setSession(s);if(s.authenticated)navigate('/admin',{replace:true});}).catch(e=>setError(message(e)));},[navigate]);
 async function login(e:FormEvent){e.preventDefault();setBusy(true);setError('');try{await adminRequest('login','POST',{password});setPassword('');navigate('/admin');}catch(e){setError(message(e));}finally{setBusy(false);}}
 return <main className="min-h-screen bg-[#fffdf5] flex items-center justify-center p-6"><section className="bg-white border rounded-3xl shadow-sm p-8 w-full max-w-md space-y-5">
  <Link to="/" className="text-sm text-slate-500">← 返回 Presentation Copilot</Link><h1 className="text-2xl font-bold">开发者登录</h1>
  <p className="text-slate-600 text-sm">管理运行与额度。此身份独立于创作访客，登录不会改变作品归属。</p>
  {session&&!session.initialized&&<p className="p-3 rounded-xl bg-amber-50">管理员尚未初始化。请先在本机PowerShell使用 admin-init 命令设置密码，无默认密码。</p>}
  <form onSubmit={login} className="space-y-4">{field('管理员密码',<input className={input} aria-label="管理员密码" type="password" autoComplete="current-password" maxLength={128} value={password} onChange={e=>setPassword(e.target.value)} />)}
   <button className={`${button} w-full`} disabled={busy||!session?.initialized||!password}>{busy?'正在登录…':'登录'}</button></form>
  {error&&<p role="alert" className="text-red-700">{error}</p>}<p className="text-xs text-slate-500">页面读取和登录不会调用模型。会话有效期8小时。</p>
 </section></main>;
}

export function AdminConsole(){
 const navigate=useNavigate();const [ready,setReady]=useState(false);const [section,setSection]=useState(0);
 const [data,setData]=useState<Overview|null>(null);const [draft,setDraft]=useState<DraftForm|null>(null);const [review,setReview]=useState<Change|null>(null);
 const [phrase,setPhrase]=useState('');const [busy,setBusy]=useState(false);const [notice,setNotice]=useState('');const [error,setError]=useState('');
 const [tasks,setTasks]=useState<Task[]>([]);const [page,setPage]=useState(1);const [more,setMore]=useState(false);const [status,setStatus]=useState('');
 const [detail,setDetail]=useState<TaskDetail|null>(null);const [audit,setAudit]=useState<{id:number;at:number;action:string;summary:string}[]>([]);const [auditPage,setAuditPage]=useState(1);const [auditMore,setAuditMore]=useState(false);
 const [balanceAmount,setBalanceAmount]=useState('');const [imagePrice,setImagePrice]=useState('3');const [textPrice,setTextPrice]=useState('0.8');
 const accept=(d:Overview)=>{setData(d);setDraft(editableDraft(structuredClone(d.draft)));setReview(null);setPhrase('');};
 async function run(action:()=>Promise<void>){setBusy(true);setError('');setNotice('');try{await action();}catch(e){setError(message(e));if((e as {status?:number}).status===401)navigate('/admin/login');}finally{setBusy(false);}}
 useEffect(()=>{adminRequest('session').then(async s=>{if(!s.authenticated){navigate('/admin/login',{replace:true});return;}accept(await adminRequest<Overview>('overview'));setReady(true);}).catch(e=>setError(message(e)));},[navigate]);
 useEffect(()=>{if(!ready||section!==3)return;adminRequest<{items:Task[];has_more:boolean}>(`tasks?page=${page}&status=${encodeURIComponent(status)}`).then(r=>{setTasks(r.items);setMore(r.has_more);}).catch(e=>setError(message(e)));},[ready,section,page,status]);
 useEffect(()=>{if(!ready||section!==3)return;adminRequest<{items:typeof audit;has_more:boolean}>(`audit?page=${auditPage}`).then(r=>{setAudit(r.items);setAuditMore(r.has_more);}).catch(e=>setError(message(e)));},[ready,section,auditPage]);
 function edit(next:DraftForm){setDraft(next);setReview(null);setPhrase('');setNotice('表单已修改，尚未保存或应用。');}
 function authField(key:keyof Auth,v:unknown){if(draft?.authorization)edit({...draft,authorization:{...draft.authorization,[key]:v}});}
 if(!ready||!data||!draft)return <main className="p-8">{error||'正在检查管理会话…'}</main>;
 const required='确认';
 const hasChanges=!!review&&Object.keys(review.changes).length>0;
 return <main className="min-h-screen bg-[#fffdf5] text-slate-700 px-4 py-6"><div className="max-w-6xl mx-auto space-y-5">
  <header className="flex flex-wrap gap-3 items-center justify-between"><div><h1 className="text-2xl font-bold text-slate-900">Presentation Copilot · 开发者后台</h1><p className="text-sm">本地管理 · 当前执行模式 {data.mode} · 生效版本 {data.applied_version_in_process}</p></div>
   <div className="flex gap-3"><Link to="/" className={button}>返回创作端</Link><button className={button} onClick={()=>run(async()=>{await adminRequest('logout','POST');navigate('/admin/login');})}>退出管理</button></div></header>
  <nav className="flex flex-wrap gap-2" aria-label="管理分区">{sections.map((s,i)=><button key={s} className={`${button} ${section===i?'bg-yellow-300 font-bold':''}`} onClick={()=>setSection(i)}>{s}</button>)}</nav>
  {error&&<p role="alert" className="bg-red-50 rounded-xl p-3 text-red-700">{error}</p>}{notice&&<p role="status" className="bg-green-50 rounded-xl p-3">{notice}</p>}
  <section className="bg-white border rounded-2xl p-5 md:p-7 space-y-5">
  {section===0&&<><h2 className="text-xl font-bold">运行概览</h2>
   <dl className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-4" aria-label="运行概览指标">
    {[
     ['执行模式',`${data.mode}${data.paused?'（已暂停）':''}`,''],
     ['在执行任务',String(data.active_tasks),''],
     ['累计用量',data.manual_balance?`${value(data.manual_balance.estimated_spent)} 积分`:data.settlement?`${value(data.settlement.total_occupied)} 积分`:'未取得',data.manual_balance?'成功调用估算':'本地有效占用'],
     ['停止线',value(data.manual_balance?.reported_balance??data.quota_cycle?.credit_limit??data.active.authorization?.credits),'积分'],
     ['剩余可用',value(data.remaining_reservation),'积分 · 本地估算'],
    ].map(([label,amount,note])=><div key={label} className="rounded-xl bg-amber-50 p-4 min-w-0"><dt className="text-sm">{label}</dt><dd className="mt-2 text-2xl font-semibold break-words">{amount}</dd>{note&&<p className="mt-2 text-xs text-slate-500">{note}</p>}</div>)}
   </dl>
  </>}
  {section===1&&<><h2 className="text-xl font-bold">模型与功能</h2><p>唯一现有预设：{data.credentials.preset}。连接字段由 {data.credentials.source} 管理，本页只读。</p>
   {data.credentials.models.map(m=><article key={m.role} className="rounded-xl border p-4 space-y-2"><h3 className="font-bold">{m.role==='image'?'图片模型':'文本模型'} · {m.model}</h3><p>{m.provider} · {m.protocol} · Key{m.configured?'已配置':'未配置'}</p><p>{m.validation}</p><p>{data.reasons[m.role]?reasons[data.reasons[m.role]!]||data.reasons[m.role]:'当前允许执行'}</p></article>)}
   <p>核心操作：文字规划、模板/文字风格图片生成、页内修改；仍受具体授权及Provider能力限制。</p><p>视觉QC、素材模型任务、自动翻新、模板分析和视频保留原实现，尚未开放或仍需额外接入/验证。</p><p className="text-sm text-slate-500">生成图片仍可能出现错字或额外内容；普通PPTX为图片式，不能逐字编辑。这里没有连接测试按钮。</p>

   <details className="rounded-xl border p-4"><summary className="cursor-pointer font-semibold">历史验证记录</summary>
    <p className="mt-3 text-sm">记录日期 {VALIDATION_REVIEW_DATE}，仅对应当时样例，不代表当前服务状态或生成授权。</p>
    <dl className="mt-3 space-y-3">{VALIDATION_EVIDENCE.map(item=><div key={item.id}><dt className="font-medium">{item.label}：{item.result}</dt><dd className="text-sm">{item.detail}</dd></div>)}</dl>
   </details>
  </>}
  {section===2&&<><h2 className="text-xl font-bold">运行授权与额度</h2><p className="rounded-xl bg-amber-50 p-3">余额校准填写此刻的平台剩余余额。从应用时刻起，成功调用计入估算费用，失败不计成功次数或估算支出；执行中的请求临时占位。历史记录完整保留，平台实际扣费仍以账单为准。</p>
   <p>运行授权：{data.authorization_valid?(data.active.authorization?.validity==='daily'?'日常使用（不设到期时间）':'当前在有效时间内'):data.active.authorization?'尚未生效或已经到期':'未设置'}；当前允许角色：{data.active.authorization?.roles.join('、')||'无'}；图片操作：{data.active.authorization?.operations.join('、')||'无'}。</p>
   <div className="space-y-2">{Object.entries(data.reasons).map(([r,v])=><p key={r}>{r==='image'?'图片':'文本'}：{v?reasons[v]||v:'当前守卫允许，发送前仍会再次检查'}</p>)}</div>
   <div className="flex flex-wrap gap-2"><button className={button} disabled={busy} onClick={()=>run(async()=>{accept(await adminRequest('overview'));})}>刷新本地状态</button>
    <button className={button} disabled={busy} onClick={()=>{if(window.confirm('确认暂停新生成？已有网页、作品和下载保持可用。'))run(async()=>{accept(await adminRequest('pause','POST',{phrase:'暂停新生成'}));setNotice('已暂停新的模型发送。');});}}>暂停生成</button>
    <button className={button} disabled={busy||!data.paused||data.active.mode!=='live'} onClick={()=>{if(window.confirm('确认按已应用授权恢复？将重新检查有效期、原账目和资源，不会清除失败暂停。'))run(async()=>{accept(await adminRequest('resume','POST',{confirmation:data.resume_confirmation,phrase:'恢复已应用授权'}));setNotice('已按有效授权恢复。');});}}>按已应用授权恢复</button></div>
   {draft.authorization&&<div className="rounded-xl border p-4 space-y-3">
    <h3 className="font-semibold">校准当前余额</h3>
    <p className="text-sm">手动填写，不会实时同步平台。校准前的消费留在历史中，不重复扣除。刷新、重启或重复应用同一配置都不会重新计算起点。</p>
    <div className="grid md:grid-cols-3 gap-3">
     {field('当前平台剩余余额（积分）',<input className={input} type="text" inputMode="decimal" value={balanceAmount} onChange={e=>setBalanceAmount(e.target.value)}/>)}
     {field('每张2K图片估算费用（积分）',<input className={input} type="text" inputMode="decimal" value={imagePrice} onChange={e=>setImagePrice(e.target.value)}/>)}
     {field('每次文本调用估算费用（积分）',<input className={input} type="text" inputMode="decimal" value={textPrice} onChange={e=>setTextPrice(e.target.value)}/>)}
    </div>
    <p className="text-sm">文本暂按每次估算，实际费用会随字数变化。返回多张图片按数量估算；失败记录仍可查看，不代表平台已确认免费。</p>
    <button className={button} disabled={busy} onClick={()=>{try{const balance={id:`balance-${crypto.randomUUID()}`,credits:numericValue(balanceAmount,'当前平台剩余余额'),image_2k:numericValue(imagePrice,'每张2K图片估算费用',false,0.000001),text_fallback:numericValue(textPrice,'每次文本调用估算费用',false,0.000001)};authField('balance',balance);setNotice('新的余额基准已填入草稿，请保存、校验并确认应用。');}catch(e){setError(message(e));}}}>将此金额设为新的余额基准</button>
    {draft.authorization.balance&&<p className="text-sm">草稿余额基准：{draft.authorization.balance.credits} 积分；应用后开始统计。</p>}
    {data.manual_balance&&<p className="text-sm">当前基准：{data.manual_balance.reported_balance} 积分；校准时间：{new Date(data.manual_balance.created_at).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'})}（上海）。</p>}
   </div>}
   <div className="grid md:grid-cols-2 gap-4">{field('授权显示名称',<input className={input} value={draft.name} maxLength={80} onChange={e=>edit({...draft,name:e.target.value})}/>)}
    {field('拟应用模式',<select className={input} aria-label="拟应用模式" value={draft.mode} onChange={e=>edit({...draft,mode:e.target.value as Draft['mode'],authorization:e.target.value==='preview'?null:draft.authorization||{id:'',not_before:'',expires_at:'',credits:'0',text:'0',image:'0',roles:[],operations:[]},visitor:e.target.value==='preview'?{...draft.visitor,credits:'0',text:'0',image:'0'}:draft.visitor})}><option value="preview">preview：生成关闭</option><option value="live">live：需完整授权才能应用</option></select>)}
   </div>
   {draft.authorization&&<div className="grid md:grid-cols-2 gap-4">{field('稳定授权ID（8–80位字母、数字、横线）',<input className={input} value={draft.authorization.id} onChange={e=>authField('id',e.target.value)}/>)}
    {!draft.authorization.balance&&field('累计预留停止线（平台积分计价单位）',<input type="text" inputMode="decimal" className={input} value={draft.authorization.credits} onChange={e=>authField('credits',e.target.value)}/>)}
    {field('使用期限',<select className={input} value={draft.authorization.validity||'fixed'} onChange={e=>edit({...draft,authorization:{...draft.authorization!,validity:e.target.value as 'fixed'|'daily',expires_at:e.target.value==='daily'?null:''}})}><option value="fixed">定时授权</option><option value="daily" disabled={!draft.authorization.balance}>日常使用（不设到期时间）</option></select>)}
    {draft.authorization.validity==='daily'&&<p className="text-sm">无需反复续期；余额、调用次数、访客限制和手动暂停继续生效。余额不会自动同步平台。</p>}
    {(['not_before','expires_at'] as const).filter(k=>k==='not_before'||draft.authorization!.validity!=='daily').map((k,i)=><div key={k}>{field(`${i?'结束':'开始'}时间（Asia/Shanghai，UTC+08:00）`,<input type="datetime-local" className={input} value={chinaLocal(draft.authorization![k])} onChange={e=>authField(k,isoLocal(e.target.value))}/>)}</div>)}
    {(['text','image'] as const).map(r=><div key={r}>{field(`${r==='text'?'文本':'图片'}${draft.authorization?.balance?'成功调用上限（执行中临时占用）':'累计请求尝试停止线（含失败/超时）'}`,<input type="text" inputMode="numeric" className={input} value={draft.authorization![r]} onChange={e=>{const raw=e.target.value;edit({...draft,authorization:{...draft.authorization!,[r]:raw,roles:[...draft.authorization!.roles.filter(x=>x!==r),...(Number(raw)>0?[r]:[])]}});}}/>)}</div>)}
    <fieldset className="md:col-span-2"><legend className="font-medium">允许的图片操作（文本由文本角色及次数控制）</legend><div className="flex flex-wrap gap-4 mt-2">{[['text_generate','文字风格生成'],['template_generate','模板生成'],['slide_edit','页内修改/区域修改']].map(([id,label])=><label key={id}><input type="checkbox" checked={draft.authorization!.operations.includes(id)} onChange={e=>authField('operations',e.target.checked?[...draft.authorization!.operations,id]:draft.authorization!.operations.filter(x=>x!==id))}/> {label}</label>)}</div></fieldset>
   </div>}
   <h3 className="font-semibold">访客窗口限制</h3><div className="grid md:grid-cols-3 gap-4">{([['window_seconds','窗口长度（秒，不按零点重置）'],['credits','每访客窗口预留积分上限'],['text','每访客窗口文本尝试上限'],['image','每访客窗口图片尝试上限']] as const).map(([k,label])=><div key={k}>{field(label,<input type="text" inputMode={k==='credits'?'decimal':'numeric'} className={input} value={draft.visitor[k]} onChange={e=>edit({...draft,visitor:{...draft.visitor,[k]:e.target.value}})}/>)}</div>)}</div>
   <h3 className="font-semibold">页数规划</h3><div className="grid md:grid-cols-2 gap-4">{([['default_auto_pages','默认页数','用户未指定页数时使用。建议5页。'],['pages','单任务最多页数','实际生成不会超过此值。建议10页。']] as const).map(([k,label,help])=><div key={k}>{field(label,<input type="text" inputMode="numeric" className={input} value={draft.visitor[k]} onChange={e=>edit({...draft,visitor:{...draft.visitor,[k]:e.target.value}})}/>)}<p className="text-sm text-slate-500 mt-1">{help}</p></div>)}</div>
   <p className="text-sm text-slate-600">用户未指定页数时按默认值生成。若超过单任务上限或当前额度不足，系统会自动减少页数，并在生成后提示用户。</p>
   <p className="text-sm">当前生效版本 {data.revision}；草稿版本 {data.draft_revision}。路径、端口、资源阈值、并发和连接信息不在网页编辑范围内。仅保存草稿不会开放生成。</p>
   <div className="flex gap-3"><button className={button} disabled={busy} onClick={()=>run(async()=>{accept(await adminRequest('draft','PUT',{draft:savedDraft(draft),revision:data.revision,draft_revision:data.draft_revision}));setNotice('草稿已保存，执行模式未改变。');})}>1. 保存草稿</button>
    <button className={button} disabled={busy||!data.has_saved_draft||JSON.stringify(draft)!==JSON.stringify(editableDraft(data.draft))} onClick={()=>run(async()=>{setReview(await adminRequest('validate','POST'));})}>2. 校验并查看变更</button></div>
   {review&&<div className="border rounded-xl p-4 space-y-3"><h3 className="font-semibold">{hasChanges?'应用前确认':'配置检查结果'}</h3>
    {hasChanges?<pre className="text-xs bg-slate-50 p-3 whitespace-pre-wrap break-all max-h-64 overflow-auto">{JSON.stringify(review.changes,null,2)}</pre>:<p role="status" className="rounded-xl bg-slate-50 p-3">当前草稿与生效配置一致，无需再次应用。</p>}
    {review.blockers.map(b=><p key={b} className="text-red-700">{b}</p>)}
    {hasChanges&&<>{field(`输入“${required}”确认本次范围`,<input className={input} aria-label="应用确认文字" value={phrase} onChange={e=>setPhrase(e.target.value)}/>)}
    <button className={button} disabled={busy||!review.can_apply||phrase!==required} onClick={()=>run(async()=>{accept(await adminRequest('apply','POST',{confirmation:review.confirmation,phrase}));setNotice('配置已应用到当前进程；刷新、退出或任务完成不会自动重置额度。');})}>3. 确认应用</button></>}</div>}
  </>}
  {section===3&&<><h2 className="text-xl font-bold">运行记录</h2><p className="text-sm">仅显示本服务的摘要，不提供原始提示词、私人输入批量下载或重生成。未知费用保持未取得。</p>
   <details className="rounded-xl border p-3 space-y-3"><summary className="cursor-pointer">历史额度与结算记录</summary>
   <p>累计文本尝试 {data.ledger.text} 次 · 图片尝试 {data.ledger.image} 次 · 本地累计预留 {data.ledger.reserved_credits} 积分</p><p>当前累计停止线内剩余可预留：{value(data.remaining_reservation)}；额外未知费用占位：{data.external_unknown}。</p>
   {data.quota_cycle&&<div className="rounded-xl border p-3 text-sm space-y-1"><p>当前额度周期：{data.quota_cycle.id} · 周期预算上限：{data.quota_cycle.credit_limit}</p><p>上方计数与结算仅计当前周期。周期不会因重启、改授权ID或重新应用配置而清零；平台余额需人工核对。</p>{data.lifetime_ledger&&<p>保留的全历史原始记录：文本 {data.lifetime_ledger.text} 次 · 图片 {data.lifetime_ledger.image} 次 · 原预留 {data.lifetime_ledger.reserved_credits} 积分。旧周期保留证据不代表已完成费用对账。</p>}</div>}
   {data.settlement&&<div className="rounded-xl bg-slate-50 p-3 text-sm space-y-1"><p>已结算实扣：{data.settlement.settled_actual} · 待核对预留：{data.settlement.pending_reserved}</p><p>已释放差额：{data.settlement.released_credits} · 超预留补计：{data.settlement.additional_credits} · 当前有效占用（含额外未知）：{data.settlement.total_occupied}</p><p>累计预留为原始历史记录；停止线按结算后有效占用检查。结算不刷新请求次数，不自动调整授权，不读取平台余额。</p></div>}
   </details>
   {field('任务状态筛选',<select className={input} value={status} onChange={e=>{setStatus(e.target.value);setPage(1);}}><option value="">全部状态</option>{['PENDING','PROCESSING','COMPLETED','FAILED','INTERRUPTED'].map(s=><option key={s}>{s}</option>)}</select>)}
   <div className="overflow-x-auto"><table className="w-full text-left text-sm"><thead><tr>{['时间','任务/模型','访客','状态','详情'].map(s=><th className="p-2" key={s}>{s}</th>)}</tr></thead><tbody>{tasks.map(t=><tr key={t.task_id} className="border-t"><td className="p-2">{t.created_at.slice(0,19)}</td><td className="p-2">{t.operation}<br/>{t.model||'未记录'}</td><td className="p-2">{t.visitor?`${t.visitor.slice(0,12)}…`:'历史未映射'}</td><td>{t.status}</td><td><button className={button} onClick={()=>run(async()=>{setDetail(await adminRequest(`tasks/${t.task_id}`));})}>详情</button></td></tr>)}</tbody></table>{!tasks.length&&<p className="py-6">此筛选下没有任务记录。</p>}</div>
   <div className="flex gap-3"><button className={button} disabled={page===1} onClick={()=>setPage(page-1)}>上一页</button><span>第{page}页</span><button className={button} disabled={!more} onClick={()=>setPage(page+1)}>下一页</button></div>
   {detail&&<article className="border rounded-xl p-4 space-y-3" aria-label="管理任务详情"><h3 className="font-bold">任务详情</h3><p className="break-all">{detail.task_id} · {detail.status}</p><p>{detail.quality_note}</p><p>{detail.message}</p>
    {detail.snapshot?<div className="grid sm:grid-cols-2 gap-2 text-sm">{['preset_id','model','provider','config_version','prompt_version','timeout_seconds','language','runtime_authorization_id','runtime_config_revision'].map(k=><p key={k}>{k}：{value(detail.snapshot![k])}</p>)}</div>:<p>历史任务未记录配置快照。</p>}
    {detail.snapshot&&<PagePlanSummary snapshot={detail.snapshot}/>}
    {!detail.calls.length&&<p>没有已记录的模型调用。</p>}{detail.calls.map((c,i)=><div key={i} className="rounded-xl bg-slate-50 p-3 space-y-1 text-sm break-all"><p>call_id：{value(c.call_id)} · {value(c.status)}</p><p>耗时：{value(c.elapsed_seconds)} 秒 · 用量：{value(c.usage)}</p><p>预留：{value(c.reserved_credits)} · 估算：{value(c.estimated_credits)}</p><p>平台实扣：{value(c.actual_credits)}</p><p>费用口径：{value(c.cost_status)} · 来源：{value(c.actual_credits_source)}</p><p>脱敏错误类别：{value(c.error_type)} · 阶段：{value(c.failure_stage)}</p></div>)}
    <p className="text-sm break-all">输出版本：{detail.outputs.length?JSON.stringify(detail.outputs):'未记录'}</p></article>}
   <h3 className="font-bold">管理操作记录</h3>{audit.map(a=><div key={a.id} className="border-t py-2 text-sm break-all"><p>{new Date(a.at*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'})}（上海） · {a.action}</p><p>{a.summary}</p></div>)}{!audit.length&&<p>暂无操作记录。</p>}
   <div className="flex gap-3"><button className={button} disabled={auditPage===1} onClick={()=>setAuditPage(auditPage-1)}>记录上一页</button><span>第{auditPage}页</span><button className={button} disabled={!auditMore} onClick={()=>setAuditPage(auditPage+1)}>记录下一页</button></div>
  </>}
  </section><p className="text-xs text-slate-500">管理员返回创作端后仍使用自己的访客身份，受原任务、归属及费用检查约束。</p>
 </div></main>;
}

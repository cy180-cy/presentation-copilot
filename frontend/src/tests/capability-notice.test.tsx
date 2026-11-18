import { beforeEach, expect, it, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { apiClient } from '@/api/client';
import { refreshCapabilities, useProductCapabilities } from '@/hooks/useProductCapabilities';
import { publicFailure, publicProgress, QUOTA_UNAVAILABLE } from '@/utils/publicMessages';
import { SERVICE_UNAVAILABLE } from '@/config/product';
import { useState } from 'react';
vi.mock('@/api/client',()=>({apiClient:{get:vi.fn(),post:vi.fn()}}));
function Action(){const {can,message}=useProductCapabilities();const [notice,setNotice]=useState('');return <><button disabled={!can('template_generate')}>生成</button><button onClick={()=>setNotice(message('template_generate'))}>提交检查</button>{notice&&<p role="alert">{notice}</p>}</>;}
beforeEach(()=>vi.clearAllMocks());
it.each([
 ['preview',false,'preview',SERVICE_UNAVAILABLE],['live',false,'configuration_missing',SERVICE_UNAVAILABLE],
 ['live',false,'budget_exhausted',QUOTA_UNAVAILABLE],['live',false,'trial_exhausted',QUOTA_UNAVAILABLE],
 ['live',false,'admin_paused',SERVICE_UNAVAILABLE],['live',true,null,SERVICE_UNAVAILABLE],
])('keeps %s availability %s without a runtime banner',async(mode,available,reason,expected)=>{
 vi.mocked(apiClient.get).mockResolvedValue({data:{data:{mode,capabilities:{template_generate:{available,reason,configured:true,verified:false,message:'内部输入检查 PC-010'}}}}});
 await refreshCapabilities();const view=render(<Action/>);
 expect(screen.getByRole('button',{name:'生成'}).hasAttribute('disabled')).toBe(!available);
 expect(screen.queryByRole('status')).not.toBeInTheDocument();
 expect(view.container).not.toHaveTextContent(/preview|live|模型|配置|验证|内部|PC-010/);
 fireEvent.click(screen.getByText('提交检查'));expect(screen.getByRole('alert')).toHaveTextContent(expected as string);
 expect(apiClient.post).not.toHaveBeenCalled();
});
it('fails closed when status is unavailable without displaying diagnostics',async()=>{
 vi.mocked(apiClient.get).mockRejectedValue(new Error('offline'));
 await refreshCapabilities();render(<Action/>);
 expect(screen.getByRole('button',{name:'生成'})).toBeDisabled();
 expect(screen.queryByRole('status')).not.toBeInTheDocument();
});
it.each(['preview','live 授权ID abc','PC-010 验收','模型配置未就绪','供应商请求超时','输入检查未通过','预算停止线已到','API Key missing',
 'not ready','runtime unavailable','route /api/internal','path D:\\private\\config.json','credits: 4; token: unknown',
 'authorization_id: private','TypeError: stack trace','MinerU token expired','Beta 即将开放',
 '请求失败：/api/internal/config','无法读取 /opt/service/config.json'])(
 'redacts internal failure %s',message=>expect(publicFailure(message)).toBe(SERVICE_UNAVAILABLE));
it.each(['TRIAL_EXHAUSTED','trial_exhausted','TRIAL_UNCONFIGURED','budget_exhausted'])(
 'uses exact quota message for %s',code=>expect(publicFailure('内部信息',code)).toBe(QUOTA_UNAVAILABLE));
it('preserves business validation and hides technical progress',()=>{
 expect(publicFailure('请先填写标题')).toBe('请先填写标题');
 expect(publicFailure('当前额度不足，已有作品仍可使用。')).toBe(QUOTA_UNAVAILABLE);
 expect(publicProgress('正在调用模型')).toBe('正在处理，请稍候…');
 expect(publicProgress('正在保存第2页')).toBe('正在保存第2页');
});
it.each(['请先填写标题','保存成功','导出失败，请稍后重试。','达到单任务页数上限','你请求生成8页，本次已完成6页。'])(
 'preserves business feedback %s',message=>expect(publicFailure(message)).toBe(message));

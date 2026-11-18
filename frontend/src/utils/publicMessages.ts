import { SERVICE_UNAVAILABLE } from '@/config/product';

export const QUOTA_UNAVAILABLE = '当前可用额度不足，请联系管理员。';

// Display-only mapping. Do not change response codes, execution guards or data.
export function publicFailure(message: string, code = ''): string {
  if (['TRIAL_EXHAUSTED', 'TRIAL_UNCONFIGURED', 'BUDGET_EXHAUSTED'].includes(code.toUpperCase())
      || /(?:额度|积分|预算).*(?:不足|用完|耗尽|未配置|不够)|调用上限已到|(?:quota|budget)[_ ]exhausted/i.test(message)) {
    return QUOTA_UNAVAILABLE;
  }
  if (['QUEUE_FULL', 'PREVIEW', 'EXECUTION_BLOCKED', 'GENERATION_PAUSED', 'INTEGRATION_REQUIRED', 'CAPTION_UNAVAILABLE', 'CONFIGURATION_MISSING', 'PROVIDER_UNSUPPORTED', 'AUTHORIZATION_REQUIRED', 'AUTHORIZATION_EXPIRED', 'ADMIN_PAUSED', 'CONFIGURATION_APPLYING', 'PREVIOUS_FAILURE'].includes(code.toUpperCase())
      || /\b(?:live|preview|provider|api|csrf|tokenflux|deepseek|gemini|traceback|sqlite)\b|PC-\d|模型|预览模式|执行状态|授权|停止线|预留|预算|输入检查|配置|验收|验证|未接入|未开放|服务开放|暂停生成|生成已暂停|资源保护|失败阶段|请求次数|供应商|调用.*(?:失败|超时)|技术原因|错误代码|识别服务请求超时|(?<!\w)[A-Z]:[\\/]/i.test(message)
      || /service unavailable|internal server error|econnrefused|network error|request failed with status code/i.test(message)) {
    return SERVICE_UNAVAILABLE;
  }
  if (/\b(?:provider\w*|runtime|route|path|ready|not[ _-]?ready|token\w*|quota|credits|authorization[_ -]?id|call[_ -]?id|mineru|openai|codex|ocr|beta)\b|\b\w*(?:Error|Exception)\b|traceback|stack trace|https?:\/\/|\/(?:api|files|tmp|var|home|usr|app|workspace|Users|opt|etc|srv)\/|配置.*就绪|执行.*(?:开启|关闭)|累计.*停止线|调用次数|模型角色|生效版本|配置草稿|即将开放|待后续支持|服务测试/i.test(message)) {
    return SERVICE_UNAVAILABLE;
  }
  return message;
}

export function capabilityFailure(reason?: string | null): string {
  return ['budget_exhausted', 'trial_exhausted', 'trial_unconfigured'].includes(reason || '')
    ? QUOTA_UNAVAILABLE : SERVICE_UNAVAILABLE;
}

/** Accept only the saved completion sentence; never render diagnostic text. */
export function pageCompletion(message?: string | null): string | null {
  if (!message) return null;
  const match = message.match(/^(你请求生成|默认计划生成)(\d+)页，.*?本次已完成(\d+)页。$/);
  return match ? `${match[1]}${match[2]}页，本次已完成${match[3]}页。` : null;
}

export function publicProgress(message: string): string {
  return publicFailure(message) === message ? message : '正在处理，请稍候…';
}

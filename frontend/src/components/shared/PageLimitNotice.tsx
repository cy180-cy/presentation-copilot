import { pageCompletion } from '@/utils/publicMessages';
export function PageLimitNotice({message}: {message?: string | null}) {
  const completion = pageCompletion(message);
  if (!completion) return null;
  return <div role="status" className="mx-4 my-2 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-slate-700">{completion}</div>;
}

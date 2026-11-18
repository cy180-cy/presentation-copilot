import { Presentation } from 'lucide-react';

/** Existing Lucide icon, not an upstream logo or a generated image. */
export function ProductMark() {
  return <span className="inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-2xl bg-gradient-to-br from-amber-400 via-orange-400 to-rose-400 text-white shadow-sm" aria-hidden="true">
    <Presentation size={26} strokeWidth={1.8} />
  </span>;
}

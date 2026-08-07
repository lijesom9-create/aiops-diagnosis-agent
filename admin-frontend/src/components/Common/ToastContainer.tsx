/**
 * Toast 通知容器
 */

import { useToastStore } from '@/store/toastStore';
import { CheckCircle2, XCircle, Info, X } from 'lucide-react';

export default function ToastContainer() {
  const { toasts, removeToast } = useToastStore();

  if (toasts.length === 0) return null;

  return (
    <div className="fixed bottom-4 right-4 z-50 space-y-2">
      {toasts.map((t) => {
        const Icon = t.type === 'success' ? CheckCircle2 : t.type === 'error' ? XCircle : Info;
        const color =
          t.type === 'success'
            ? 'text-green-600'
            : t.type === 'error'
            ? 'text-red-600'
            : 'text-blue-600';

        return (
          <div
            key={t.id}
            className="flex items-start gap-2.5 bg-white border border-zinc-200 rounded-lg px-3.5 py-2.5 shadow-sm animate-slide-up min-w-[280px] max-w-md"
          >
            <Icon size={16} className={`${color} flex-shrink-0 mt-0.5`} />
            <span className="flex-1 text-[13px] text-zinc-700 leading-relaxed">{t.message}</span>
            <button
              onClick={() => removeToast(t.id)}
              className="text-zinc-400 hover:text-zinc-600 flex-shrink-0"
            >
              <X size={14} />
            </button>
          </div>
        );
      })}
    </div>
  );
}

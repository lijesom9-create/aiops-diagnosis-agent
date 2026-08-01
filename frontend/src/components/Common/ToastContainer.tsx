/**
 * Toast 通知容器
 * 固定在右上角，自动消失，点击可关闭
 * 在 App.tsx 中全局渲染一次即可
 */

import { useToastStore } from '@/store/toastStore';
import { CheckCircle, XCircle, Info, X } from 'lucide-react';

const _ICON_MAP = {
  success: CheckCircle,
  error: XCircle,
  info: Info,
};

const _STYLE_MAP = {
  success: 'bg-green-50 border-green-200 text-green-800',
  error: 'bg-red-50 border-red-200 text-red-800',
  info: 'bg-blue-50 border-blue-200 text-blue-800',
};

const _ICON_COLOR_MAP = {
  success: 'text-green-500',
  error: 'text-red-500',
  info: 'text-blue-500',
};

export default function ToastContainer() {
  const { toasts, removeToast } = useToastStore();

  if (toasts.length === 0) return null;

  return (
    <div
      className="fixed top-4 right-4 z-50 flex flex-col gap-2 max-w-sm"
      role="alert"
      aria-live="polite"
      aria-atomic="true"
    >
      {toasts.map((toast) => {
        const Icon = _ICON_MAP[toast.type];
        return (
          <div
            key={toast.id}
            className={`flex items-start gap-2 px-4 py-3 rounded-lg border shadow-md animate-in fade-in slide-in-from-top-2 ${_STYLE_MAP[toast.type]}`}
          >
            <Icon className={`w-5 h-5 flex-shrink-0 mt-0.5 ${_ICON_COLOR_MAP[toast.type]}`} aria-hidden="true" />
            <p className="text-sm flex-1 break-words">{toast.message}</p>
            <button
              onClick={() => removeToast(toast.id)}
              className="flex-shrink-0 text-current opacity-50 hover:opacity-100 transition-opacity"
              aria-label="关闭通知"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        );
      })}
    </div>
  );
}

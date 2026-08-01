/**
 * Toast 通知状态管理
 * 用法：
 *   import { useToastStore } from '@/store/toastStore';
 *   const toast = useToastStore();
 *   toast.error('操作失败');
 *   toast.success('操作成功');
 */

import { create } from 'zustand';

type ToastType = 'success' | 'error' | 'info';

interface ToastItem {
  id: string;
  type: ToastType;
  message: string;
}

interface ToastState {
  toasts: ToastItem[];
  addToast: (type: ToastType, message: string, duration?: number) => void;
  removeToast: (id: string) => void;
  // 便捷方法
  success: (message: string, duration?: number) => void;
  error: (message: string, duration?: number) => void;
  info: (message: string, duration?: number) => void;
}

let _toastId = 0;

export const useToastStore = create<ToastState>((set, get) => ({
  toasts: [],

  addToast: (type, message, duration = 3000) => {
    const id = `toast_${++_toastId}`;
    set((state) => ({
      toasts: [...state.toasts, { id, type, message }],
    }));
    // 自动移除
    if (duration > 0) {
      setTimeout(() => {
        get().removeToast(id);
      }, duration);
    }
  },

  removeToast: (id) => {
    set((state) => ({
      toasts: state.toasts.filter((t) => t.id !== id),
    }));
  },

  success: (message, duration) => get().addToast('success', message, duration),
  error: (message, duration) => get().addToast('error', message, duration ?? 5000),
  info: (message, duration) => get().addToast('info', message, duration),
}));

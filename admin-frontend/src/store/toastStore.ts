/**
 * Toast 通知状态管理
 */

import { create } from 'zustand';

export interface ToastItem {
  id: string;
  type: 'success' | 'error' | 'info';
  message: string;
}

interface ToastState {
  toasts: ToastItem[];
  addToast: (type: ToastItem['type'], message: string) => void;
  removeToast: (id: string) => void;
}

export const useToastStore = create<ToastState>((set) => ({
  toasts: [],

  addToast: (type, message) => {
    const id = `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
    set((state) => ({ toasts: [...state.toasts, { id, type, message }] }));
    // 3 秒后自动移除
    setTimeout(() => {
      set((state) => ({ toasts: state.toasts.filter((t) => t.id !== id) }));
    }, 3000);
  },

  removeToast: (id) =>
    set((state) => ({ toasts: state.toasts.filter((t) => t.id !== id) })),
}));

/** 快捷方法 */
export const toast = {
  success: (msg: string) => useToastStore.getState().addToast('success', msg),
  error: (msg: string) => useToastStore.getState().addToast('error', msg),
  info: (msg: string) => useToastStore.getState().addToast('info', msg),
};

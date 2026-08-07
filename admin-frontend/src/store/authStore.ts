/**
 * 认证状态管理 — 管理后台
 *
 * 与主前端不同：登录后强制校验 role === 'admin'，非管理员拒绝进入。
 * Token 由后端通过 httpOnly cookie 管理，前端不接触 token 明文。
 */

import { create } from 'zustand';
import { authApi } from '@/services/api';
import type { User, LoginRequest } from '@/types';

interface AuthState {
  user: User | null;
  isAuthenticated: boolean;
  isAdmin: boolean;
  isLoading: boolean;
  error: string | null;

  login: (data: LoginRequest) => Promise<void>;
  logout: () => Promise<void>;
  checkAuth: () => Promise<void>;
  clearError: () => void;
}

export const useAuthStore = create<AuthState>((set) => ({
  user: null,
  isAuthenticated: false,
  isAdmin: false,
  isLoading: true,
  error: null,

  login: async (data: LoginRequest) => {
    set({ isLoading: true, error: null });
    try {
      await authApi.login(data);
      const meResp = await authApi.getMe();
      const user = meResp.data;

      // 管理后台：非 admin 拒绝登录
      if (user.role !== 'admin') {
        await authApi.logout();
        set({
          error: '该账号无管理员权限，无法进入管理后台',
          isLoading: false,
        });
        return;
      }

      set({
        user,
        isAuthenticated: true,
        isAdmin: true,
        isLoading: false,
      });
    } catch (error: any) {
      const detail = error.response?.data?.detail;
      let message = '登录失败';
      if (typeof detail === 'string') {
        message = detail;
      }
      set({ error: message, isLoading: false });
      throw error;
    }
  },

  logout: async () => {
    try {
      await authApi.logout();
    } catch {
      // 即使后端调用失败也清除前端状态
    }
    set({
      user: null,
      isAuthenticated: false,
      isAdmin: false,
      error: null,
    });
  },

  checkAuth: async () => {
    try {
      const meResp = await authApi.getMe();
      const user = meResp.data;
      if (user.role !== 'admin') {
        // 已登录但非 admin，不允许进入
        set({
          user: null,
          isAuthenticated: false,
          isAdmin: false,
          isLoading: false,
        });
        return;
      }
      set({
        user,
        isAuthenticated: true,
        isAdmin: true,
        isLoading: false,
      });
    } catch {
      set({
        user: null,
        isAuthenticated: false,
        isAdmin: false,
        isLoading: false,
      });
    }
  },

  clearError: () => set({ error: null }),
}));

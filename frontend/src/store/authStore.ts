/**
 * 认证状态管理
 * Token 由后端通过 httpOnly cookie 管理，前端不再接触 token 明文
 */

import { create } from 'zustand';
import { authApi } from '@/services/api';
import type { User, LoginRequest, RegisterRequest } from '@/types';

interface AuthState {
  user: User | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  error: string | null;

  // Actions
  login: (data: LoginRequest) => Promise<void>;
  register: (data: RegisterRequest) => Promise<void>;
  logout: () => Promise<void>;
  checkAuth: () => Promise<void>;
  clearError: () => void;
}

export const useAuthStore = create<AuthState>((set) => ({
  user: null,
  isAuthenticated: false,
  isLoading: true,
  error: null,

  login: async (data: LoginRequest) => {
    set({ isLoading: true, error: null });

    try {
      // 登录成功后，后端会自动设置 httpOnly cookie，前端无需存储 token
      await authApi.login(data);

      // 获取用户信息（cookie 自动携带）
      const userResponse = await authApi.getMe();
      set({
        user: userResponse.data,
        isAuthenticated: true,
        isLoading: false,
      });
    } catch (error: any) {
      const detail = error.response?.data?.detail;
      let message = '登录失败';
      if (typeof detail === 'string') {
        message = detail;
      } else if (Array.isArray(detail)) {
        // FastAPI 422 验证错误格式: [{type, loc, msg, input}, ...]
        message = detail.map((e: any) => e?.msg || '').filter(Boolean).join('; ') || '登录失败';
      }
      set({ error: message, isLoading: false });
      throw error;
    }
  },

  register: async (data: RegisterRequest) => {
    set({ isLoading: true, error: null });

    try {
      // 注册成功后，后端会自动设置 httpOnly cookie
      await authApi.register(data);

      // 获取用户信息（cookie 自动携带）
      const userResponse = await authApi.getMe();
      set({
        user: userResponse.data,
        isAuthenticated: true,
        isLoading: false,
      });
    } catch (error: any) {
      const detail = error.response?.data?.detail;
      let message = '注册失败';
      if (typeof detail === 'string') {
        message = detail;
      } else if (Array.isArray(detail)) {
        message = detail.map((e: any) => e?.msg || '').filter(Boolean).join('; ') || '注册失败';
      }
      set({ error: message, isLoading: false });
      throw error;
    }
  },

  logout: async () => {
    try {
      // 调用后端清除 httpOnly cookie
      await authApi.logout();
    } catch {
      // 即使后端调用失败也清除前端状态（cookie 会自然过期）
    }
    set({
      user: null,
      isAuthenticated: false,
      error: null,
    });
  },

  checkAuth: async () => {
    // 不再检查 localStorage，直接用 cookie 调用 /auth/me 验证
    // 如果 cookie 不存在或过期，后端返回 401，前端状态设为未认证
    try {
      const response = await authApi.getMe();
      set({
        user: response.data,
        isAuthenticated: true,
        isLoading: false,
      });
    } catch {
      set({
        user: null,
        isAuthenticated: false,
        isLoading: false,
      });
    }
  },

  clearError: () => set({ error: null }),
}));

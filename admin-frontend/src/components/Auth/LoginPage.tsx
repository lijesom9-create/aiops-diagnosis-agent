/**
 * 登录页面 — 管理后台
 *
 * 与主前端登录页区别：登录后校验 admin 角色，非管理员拒绝进入。
 */

import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuthStore } from '@/store/authStore';
import { Eye, EyeOff, Loader2, ShieldCheck } from 'lucide-react';

export default function LoginPage() {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const { login, isLoading, error, clearError } = useAuthStore();
  const navigate = useNavigate();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    clearError();
    try {
      await login({ username, password });
      // login 成功后 store 中 isAuthenticated 为 true 才跳转
      if (useAuthStore.getState().isAuthenticated) {
        navigate('/');
      }
    } catch {
      // 错误已在 store 中处理
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-zinc-50 px-4">
      <div className="w-full max-w-sm">
        {/* 标题 */}
        <div className="mb-8">
          <div className="flex items-center gap-2 mb-1">
            <ShieldCheck size={20} className="text-zinc-900" />
            <h1 className="text-xl font-semibold text-zinc-900 tracking-tight">管理后台</h1>
          </div>
          <p className="mt-1.5 text-[13px] text-zinc-500">仅管理员可登录</p>
        </div>

        {/* 表单 */}
        <form onSubmit={handleSubmit} className="space-y-4">
          {error && (
            <div className="px-3 py-2 bg-red-50 border border-red-200 rounded-lg text-[13px] text-red-600">
              {error}
            </div>
          )}

          <div>
            <label htmlFor="username" className="block text-[13px] font-medium text-zinc-700 mb-1.5">
              用户名
            </label>
            <input
              id="username"
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              className="input"
              placeholder="请输入用户名"
              required
              disabled={isLoading}
              autoFocus
            />
          </div>

          <div>
            <label htmlFor="password" className="block text-[13px] font-medium text-zinc-700 mb-1.5">
              密码
            </label>
            <div className="relative">
              <input
                id="password"
                type={showPassword ? 'text' : 'password'}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="input pr-9"
                placeholder="请输入密码"
                required
                disabled={isLoading}
              />
              <button
                type="button"
                onClick={() => setShowPassword(!showPassword)}
                aria-label={showPassword ? '隐藏密码' : '显示密码'}
                className="absolute right-2.5 top-1/2 -translate-y-1/2 text-zinc-400 hover:text-zinc-600"
              >
                {showPassword ? <EyeOff size={16} /> : <Eye size={16} />}
              </button>
            </div>
          </div>

          <button
            type="submit"
            className="btn-primary w-full"
            disabled={isLoading}
          >
            {isLoading ? (
              <>
                <Loader2 size={15} className="animate-spin" />
                <span>登录中</span>
              </>
            ) : (
              <span>登录</span>
            )}
          </button>
        </form>
      </div>
    </div>
  );
}

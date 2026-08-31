/**
 * 注册页面 — 极简风格
 */

import { useState } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import { useAuthStore } from '@/store/authStore';
import { Eye, EyeOff, Loader2 } from 'lucide-react';

export default function RegisterPage() {
  const [username, setUsername] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const { register, isLoading, error, clearError } = useAuthStore();
  const navigate = useNavigate();
  const [localError, setLocalError] = useState('');

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    clearError();
    setLocalError('');

    if (password !== confirmPassword) {
      setLocalError('两次输入的密码不一致');
      return;
    }

    if (password.length < 6) {
      setLocalError('密码长度不能少于6位');
      return;
    }

    try {
      await register({ username, password, email, org_name: `个人_${username}` });
      navigate('/');
    } catch {
      // 错误已在 store 中处理
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-zinc-50 px-4">
      <div className="w-full max-w-sm">
        {/* 标题 */}
        <div className="mb-8">
          <h1 className="text-xl font-semibold text-zinc-900 tracking-tight">创建账号</h1>
          <p className="mt-1.5 text-[13px] text-zinc-500">注册以开始使用</p>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          {(error || localError) && (
            <div className="px-3 py-2 bg-red-50 border border-red-200 rounded-lg text-[13px] text-red-600">
              {error || localError}
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
            <label htmlFor="email" className="block text-[13px] font-medium text-zinc-700 mb-1.5">
              邮箱
            </label>
            <input
              id="email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="input"
              placeholder="请输入邮箱"
              required
              disabled={isLoading}
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
                placeholder="至少 6 位"
                required
                minLength={6}
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

          <div>
            <label htmlFor="confirmPassword" className="block text-[13px] font-medium text-zinc-700 mb-1.5">
              确认密码
            </label>
            <input
              id="confirmPassword"
              type="password"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              className="input"
              placeholder="再次输入密码"
              required
              disabled={isLoading}
            />
          </div>

          <button
            type="submit"
            className="btn-primary w-full"
            disabled={isLoading}
          >
            {isLoading ? (
              <>
                <Loader2 size={15} className="animate-spin" />
                <span>注册中</span>
              </>
            ) : (
              <span>注册</span>
            )}
          </button>
        </form>

        <p className="mt-6 text-center text-[13px] text-zinc-500">
          已有账号？{' '}
          <Link to="/login" className="text-zinc-900 font-medium hover:underline underline-offset-2">
            登录
          </Link>
        </p>
      </div>
    </div>
  );
}

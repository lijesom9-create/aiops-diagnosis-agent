/**
 * 布局组件 — 极简顶部导航
 */

import { Outlet, useNavigate, useLocation, Link } from 'react-router-dom';
import { useAuthStore } from '@/store/authStore';
import { LogOut, User, MessageSquare, FileText } from 'lucide-react';

export default function Layout() {
  const { user, logout } = useAuthStore();
  const navigate = useNavigate();
  const location = useLocation();

  const handleLogout = async () => {
    await logout();
    navigate('/login');
  };

  const navLinks = [
    { path: '/', label: '问答', icon: MessageSquare },
    { path: '/documents', label: '文档', icon: FileText },
  ];

  return (
    <div className="h-screen flex flex-col bg-white">
      {/* 顶部导航 */}
      <header className="h-14 border-b border-zinc-200 flex-shrink-0">
        <div className="h-full px-6 flex items-center justify-between">
          {/* Logo + 导航 */}
          <div className="flex items-center gap-8">
            <Link to="/" className="text-sm font-semibold text-zinc-900 tracking-tight">
              技术知识库
            </Link>

            <nav className="flex items-center gap-1">
              {navLinks.map(({ path, label, icon: Icon }) => {
                const isActive = location.pathname === path;
                return (
                  <Link
                    key={path}
                    to={path}
                    className={`flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[13px] transition-colors ${
                      isActive
                        ? 'bg-zinc-100 text-zinc-900 font-medium'
                        : 'text-zinc-500 hover:text-zinc-900 hover:bg-zinc-50'
                    }`}
                  >
                    <Icon size={15} />
                    <span>{label}</span>
                  </Link>
                );
              })}
            </nav>
          </div>

          {/* 用户区 */}
          <div className="flex items-center gap-3">
            <div className="flex items-center gap-1.5 text-[13px] text-zinc-500">
              <User size={14} />
              <span>{user?.username}</span>
            </div>
            <div className="w-px h-4 bg-zinc-200" />
            <button
              onClick={handleLogout}
              className="flex items-center gap-1.5 px-2 py-1.5 text-[13px] text-zinc-500 hover:text-zinc-900 rounded-md transition-colors"
            >
              <LogOut size={14} />
              <span>退出</span>
            </button>
          </div>
        </div>
      </header>

      {/* 主内容 */}
      <main className="flex-1 overflow-hidden">
        <Outlet />
      </main>
    </div>
  );
}

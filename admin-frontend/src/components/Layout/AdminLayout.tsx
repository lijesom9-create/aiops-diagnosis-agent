/**
 * 管理后台布局 — 侧边栏导航
 *
 * 结构：
 *   ┌──────┬──────────────────────┐
 *   │ 侧   │   顶部栏（用户/退出） │
 *   │ 边   ├──────────────────────┤
 *   │ 栏   │                      │
 *   │      │   主内容区（Outlet） │
 *   │      │                      │
 *   └──────┴──────────────────────┘
 */

import { Outlet, useNavigate, useLocation, Link } from 'react-router-dom';
import { useAuthStore } from '@/store/authStore';
import { Activity, Bell, ScrollText, FileText, ListChecks, Users, LogOut } from 'lucide-react';

const NAV_ITEMS = [
  { path: '/', label: '监控看板', icon: Activity },
  { path: '/alerts', label: '告警管理', icon: Bell },
  { path: '/logs', label: '日志查询', icon: ScrollText },
  { path: '/documents', label: '知识库', icon: FileText },
  { path: '/tasks', label: '任务监控', icon: ListChecks },
  { path: '/users', label: '用户管理', icon: Users },
];

export default function AdminLayout() {
  const { user, logout } = useAuthStore();
  const navigate = useNavigate();
  const location = useLocation();

  const handleLogout = async () => {
    await logout();
    navigate('/login');
  };

  return (
    <div className="h-screen flex bg-zinc-50">
      {/* 侧边栏 */}
      <aside className="w-56 flex-shrink-0 bg-white border-r border-zinc-200 flex flex-col">
        {/* Logo */}
        <div className="h-14 px-5 flex items-center border-b border-zinc-200">
          <span className="text-sm font-semibold text-zinc-900 tracking-tight">智能运维</span>
        </div>

        {/* 导航 */}
        <nav className="flex-1 px-3 py-4 space-y-0.5">
          {NAV_ITEMS.map(({ path, label, icon: Icon }) => {
            const isActive = path === '/' ? location.pathname === '/' : location.pathname.startsWith(path);
            return (
              <Link
                key={path}
                to={path}
                className={`flex items-center gap-2.5 px-3 py-2 rounded-lg text-[13px] transition-colors ${
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

        {/* 底部用户区 */}
        <div className="px-3 py-3 border-t border-zinc-200">
          <div className="flex items-center justify-between px-2">
            <div className="min-w-0">
              <p className="text-[13px] font-medium text-zinc-900 truncate">{user?.username}</p>
              <p className="text-[11px] text-zinc-400">管理员</p>
            </div>
            <button
              onClick={handleLogout}
              aria-label="退出登录"
              className="text-zinc-400 hover:text-zinc-900 p-1.5 rounded-md hover:bg-zinc-100 transition-colors"
            >
              <LogOut size={15} />
            </button>
          </div>
        </div>
      </aside>

      {/* 主内容区 */}
      <div className="flex-1 flex flex-col overflow-hidden">
        <Outlet />
      </div>
    </div>
  );
}

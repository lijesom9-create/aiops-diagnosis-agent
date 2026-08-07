/**
 * 应用主组件 — 智能运维系统
 *
 * 路由结构：
 *   /login        公开，登录页
 *   /             受保护（admin 角色），侧边栏布局
 *     ├─ /           监控看板（CPU/内存/磁盘/网络 + 服务健康）
 *     ├─ /alerts     告警管理（当前告警列表）
 *     ├─ /logs       日志查询（Loki 日志检索）
 *     ├─ /documents  知识库
 *     ├─ /tasks      任务监控
 *     └─ /users      用户管理
 */

import { useEffect } from 'react';
import { BrowserRouter as Router, Routes, Route, Navigate } from 'react-router-dom';
import { useAuthStore } from '@/store/authStore';
import AdminLayout from '@/components/Layout/AdminLayout';
import LoginPage from '@/components/Auth/LoginPage';
import MonitoringDashboard from '@/components/Monitoring/MonitoringDashboard';
import AlertsPage from '@/components/Alerts/AlertsPage';
import LogsPage from '@/components/Logs/LogsPage';
import DocumentsPage from '@/components/Documents/DocumentsPage';
import TasksPage from '@/components/Tasks/TasksPage';
import UsersPage from '@/components/Users/UsersPage';
import ToastContainer from '@/components/Common/ToastContainer';

/** 管理后台路由守卫：未认证 → 登录页；非 admin → 登录页 */
function AdminRoute({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, isAdmin, isLoading } = useAuthStore();

  if (isLoading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-zinc-900"></div>
      </div>
    );
  }

  if (!isAuthenticated || !isAdmin) {
    return <Navigate to="/login" replace />;
  }

  return <>{children}</>;
}

function App() {
  const { checkAuth } = useAuthStore();

  useEffect(() => {
    checkAuth();
  }, [checkAuth]);

  return (
    <Router>
      <Routes>
        {/* 公开路由 */}
        <Route path="/login" element={<LoginPage />} />

        {/* 受保护路由（admin 角色） */}
        <Route
          path="/"
          element={
            <AdminRoute>
              <AdminLayout />
            </AdminRoute>
          }
        >
          <Route index element={<MonitoringDashboard />} />
          <Route path="alerts" element={<AlertsPage />} />
          <Route path="logs" element={<LogsPage />} />
          <Route path="documents" element={<DocumentsPage />} />
          <Route path="tasks" element={<TasksPage />} />
          <Route path="users" element={<UsersPage />} />
        </Route>

        {/* 404 → 首页 */}
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>

      <ToastContainer />
    </Router>
  );
}

export default App;

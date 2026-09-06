/**
 * 应用主组件
 * 知识助手 - RAG 问答 + 文档管理 + 记忆系统
 */

import { useEffect } from 'react';
import { BrowserRouter as Router, Routes, Route, Navigate } from 'react-router-dom';
import { useAuthStore } from '@/store/authStore';
import Layout from '@/components/Layout/Layout';
import LoginPage from '@/components/Auth/LoginPage';
import RegisterPage from '@/components/Auth/RegisterPage';
import ChatPage from '@/components/Chat/ChatPage';
import DocumentsPage from '@/components/Documents/DocumentsPage';
import ToastContainer from '@/components/Common/ToastContainer';

// 受保护的路由
function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, isLoading } = useAuthStore();

  if (isLoading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-blue-500"></div>
      </div>
    );
  }

  if (!isAuthenticated) {
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
        <Route path="/register" element={<RegisterPage />} />

        {/* 受保护的路由 */}
        <Route
          path="/"
          element={
            <ProtectedRoute>
              <Layout />
            </ProtectedRoute>
          }
        >
          <Route index element={<ChatPage />} />
          <Route path="documents" element={<DocumentsPage />} />
        </Route>

        {/* 404 */}
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
      {/* 全局 Toast 通知 */}
      <ToastContainer />
    </Router>
  );
}

export default App;

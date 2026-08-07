/**
 * 用户管理页 — 分页列表 / 角色变更
 *
 * 调用：
 *   GET   /api/admin/users             分页列表（已过滤 hashed_password）
 *   PATCH /api/admin/users/{id}/role   修改角色
 *
 * 安全约束：不能降级自己的 admin 角色（后端校验）。
 */

import { useEffect, useState, useCallback } from 'react';
import { adminApi } from '@/services/api';
import { useAuthStore } from '@/store/authStore';
import { toast } from '@/store/toastStore';
import type { User } from '@/types';
import { RefreshCw, Loader2, Users as UsersIcon, ChevronLeft, ChevronRight } from 'lucide-react';

const ROLES = ['admin', 'teacher', 'student'] as const;
type Role = (typeof ROLES)[number];

const ROLE_TAG: Record<string, string> = {
  admin: 'tag-error',
  teacher: 'tag-info',
  student: 'tag-default',
};

const ROLE_LABEL: Record<string, string> = {
  admin: '管理员',
  teacher: '教师',
  student: '学生',
};

const PAGE_SIZE = 20;

export default function UsersPage() {
  const { user: currentUser } = useAuthStore();
  const [users, setUsers] = useState<User[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [updatingId, setUpdatingId] = useState<string | null>(null);

  const fetchUsers = useCallback(async (p: number, isRefresh = false) => {
    if (isRefresh) setRefreshing(true);
    else setLoading(true);
    try {
      const data = await adminApi.listUsers(p, PAGE_SIZE);
      setUsers(data.users);
      setTotal(data.total);
    } catch (err) {
      console.error('获取用户列表失败:', err);
      toast.error('获取用户列表失败');
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    fetchUsers(page);
  }, [page, fetchUsers]);

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const handleRoleChange = async (userId: string, newRole: Role) => {
    setUpdatingId(userId);
    try {
      await adminApi.updateUserRole(userId, newRole);
      toast.success(`角色已更新为 ${ROLE_LABEL[newRole]}`);
      // 更新本地列表
      setUsers((prev) =>
        prev.map((u) => (u.user_id === userId ? { ...u, role: newRole } : u))
      );
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      toast.error(typeof detail === 'string' ? detail : '角色更新失败');
    } finally {
      setUpdatingId(null);
    }
  };

  return (
    <div className="flex-1 overflow-auto">
      {/* 页头 */}
      <header className="h-14 px-6 border-b border-zinc-200 bg-white flex items-center justify-between flex-shrink-0">
        <div className="flex items-center gap-2">
          <h1 className="text-sm font-semibold text-zinc-900">用户管理</h1>
          <span className="text-[13px] text-zinc-400">共 {total} 人</span>
        </div>
        <button onClick={() => fetchUsers(page, true)} disabled={refreshing} className="btn-ghost">
          {refreshing ? <Loader2 size={14} className="animate-spin" /> : <RefreshCw size={14} />}
          <span>刷新</span>
        </button>
      </header>

      {/* 内容 */}
      <div className="p-6">
        {loading ? (
          <div className="flex items-center justify-center py-20">
            <Loader2 size={24} className="animate-spin text-zinc-400" />
          </div>
        ) : users.length === 0 ? (
          <div className="text-center py-20">
            <UsersIcon size={32} className="mx-auto text-zinc-300 mb-3" />
            <p className="text-sm text-zinc-400">暂无用户</p>
          </div>
        ) : (
          <div className="card overflow-hidden">
            <table className="w-full">
              <thead>
                <tr className="border-b border-zinc-200 bg-zinc-50">
                  <th className="text-left px-4 py-2.5 text-[13px] font-medium text-zinc-500">用户名</th>
                  <th className="text-left px-4 py-2.5 text-[13px] font-medium text-zinc-500">邮箱</th>
                  <th className="text-left px-4 py-2.5 text-[13px] font-medium text-zinc-500">当前角色</th>
                  <th className="text-left px-4 py-2.5 text-[13px] font-medium text-zinc-500">创建时间</th>
                  <th className="text-left px-4 py-2.5 text-[13px] font-medium text-zinc-500">角色操作</th>
                </tr>
              </thead>
              <tbody>
                {users.map((u) => {
                  const isSelf = u.user_id === currentUser?.user_id;
                  return (
                    <tr key={u.user_id} className="border-b border-zinc-100 last:border-0 hover:bg-zinc-50">
                      <td className="px-4 py-3">
                        <div className="flex items-center gap-2">
                          <span className="text-[13px] font-medium text-zinc-900">{u.username}</span>
                          {isSelf && (
                            <span className="text-[10px] text-zinc-400 bg-zinc-100 px-1.5 py-0.5 rounded">你</span>
                          )}
                        </div>
                      </td>
                      <td className="px-4 py-3 text-[13px] text-zinc-500">{u.email}</td>
                      <td className="px-4 py-3">
                        <span className={ROLE_TAG[u.role] || 'tag-default'}>
                          {ROLE_LABEL[u.role] || u.role}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-[13px] text-zinc-500">
                        {u.created_at ? new Date(u.created_at).toLocaleString('zh-CN') : '-'}
                      </td>
                      <td className="px-4 py-3">
                        <div className="flex items-center gap-1">
                          {ROLES.map((role) => {
                            const isCurrent = u.role === role;
                            const disabled = updatingId === u.user_id || (isSelf && role !== 'admin');
                            return (
                              <button
                                key={role}
                                onClick={() => !isCurrent && handleRoleChange(u.user_id, role)}
                                disabled={disabled || isCurrent}
                                className={`px-2.5 py-1 rounded-md text-[12px] transition-colors ${
                                  isCurrent
                                    ? 'bg-zinc-900 text-white font-medium'
                                    : 'text-zinc-500 hover:text-zinc-900 hover:bg-zinc-100'
                                } ${disabled && !isCurrent ? 'opacity-40 cursor-not-allowed' : ''}`}
                              >
                                {ROLE_LABEL[role]}
                              </button>
                            );
                          })}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>

            {/* 分页 */}
            {totalPages > 1 && (
              <div className="flex items-center justify-between px-4 py-3 border-t border-zinc-200">
                <span className="text-[13px] text-zinc-400">
                  第 {page} / {totalPages} 页
                </span>
                <div className="flex items-center gap-2">
                  <button
                    onClick={() => setPage((p) => Math.max(1, p - 1))}
                    disabled={page <= 1}
                    className="btn-secondary px-2.5 py-1.5"
                  >
                    <ChevronLeft size={14} />
                  </button>
                  <button
                    onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                    disabled={page >= totalPages}
                    className="btn-secondary px-2.5 py-1.5"
                  >
                    <ChevronRight size={14} />
                  </button>
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

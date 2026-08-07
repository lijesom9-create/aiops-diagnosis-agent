/**
 * 任务监控页 — 文档处理任务列表
 *
 * 调用 GET /api/admin/tasks，展示有 task_id 的文档处理任务。
 * 支持按状态过滤、自动轮询刷新（有 pending/processing 任务时）。
 */

import { useEffect, useState, useCallback } from 'react';
import { adminApi } from '@/services/api';
import type { TaskInfo } from '@/types';
import { RefreshCw, Loader2, ListChecks, AlertCircle } from 'lucide-react';

const STATUS_TAG: Record<string, { label: string; cls: string }> = {
  completed: { label: '已完成', cls: 'tag-success' },
  processing: { label: '处理中', cls: 'tag-info' },
  pending: { label: '等待中', cls: 'tag-warning' },
  failed: { label: '失败', cls: 'tag-error' },
};

const FILTERS = [
  { value: '', label: '全部' },
  { value: 'pending', label: '等待中' },
  { value: 'processing', label: '处理中' },
  { value: 'completed', label: '已完成' },
  { value: 'failed', label: '失败' },
];

export default function TasksPage() {
  const [tasks, setTasks] = useState<TaskInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [filter, setFilter] = useState('');

  const fetchTasks = useCallback(async (isRefresh = false) => {
    if (isRefresh) setRefreshing(true);
    else setLoading(true);
    try {
      const data = await adminApi.listTasks(filter || undefined);
      setTasks(data.tasks);
    } catch (err) {
      console.error('获取任务列表失败:', err);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [filter]);

  useEffect(() => {
    fetchTasks();
  }, [fetchTasks]);

  // 轮询：有 pending/processing 任务时自动刷新
  useEffect(() => {
    const hasActive = tasks.some((t) => t.status === 'pending' || t.status === 'processing');
    if (!hasActive) return;
    const timer = setInterval(() => fetchTasks(true), 5000);
    return () => clearInterval(timer);
  }, [tasks, fetchTasks]);

  return (
    <div className="flex-1 overflow-auto">
      {/* 页头 */}
      <header className="h-14 px-6 border-b border-zinc-200 bg-white flex items-center justify-between flex-shrink-0">
        <h1 className="text-sm font-semibold text-zinc-900">任务监控</h1>
        <button onClick={() => fetchTasks(true)} disabled={refreshing} className="btn-ghost">
          {refreshing ? <Loader2 size={14} className="animate-spin" /> : <RefreshCw size={14} />}
          <span>刷新</span>
        </button>
      </header>

      {/* 内容 */}
      <div className="p-6 space-y-4">
        {/* 状态过滤 */}
        <div className="flex items-center gap-1">
          {FILTERS.map((f) => (
            <button
              key={f.value}
              onClick={() => setFilter(f.value)}
              className={`px-3 py-1.5 rounded-lg text-[13px] transition-colors ${
                filter === f.value
                  ? 'bg-zinc-900 text-white font-medium'
                  : 'text-zinc-500 hover:text-zinc-900 hover:bg-zinc-100'
              }`}
            >
              {f.label}
            </button>
          ))}
        </div>

        {/* 任务列表 */}
        {loading ? (
          <div className="flex items-center justify-center py-20">
            <Loader2 size={24} className="animate-spin text-zinc-400" />
          </div>
        ) : tasks.length === 0 ? (
          <div className="text-center py-20">
            <ListChecks size={32} className="mx-auto text-zinc-300 mb-3" />
            <p className="text-sm text-zinc-400">暂无任务</p>
          </div>
        ) : (
          <div className="card overflow-hidden">
            <table className="w-full">
              <thead>
                <tr className="border-b border-zinc-200 bg-zinc-50">
                  <th className="text-left px-4 py-2.5 text-[13px] font-medium text-zinc-500">文件名</th>
                  <th className="text-left px-4 py-2.5 text-[13px] font-medium text-zinc-500">状态</th>
                  <th className="text-left px-4 py-2.5 text-[13px] font-medium text-zinc-500">Task ID</th>
                  <th className="text-left px-4 py-2.5 text-[13px] font-medium text-zinc-500">创建时间</th>
                  <th className="text-left px-4 py-2.5 text-[13px] font-medium text-zinc-500">错误信息</th>
                </tr>
              </thead>
              <tbody>
                {tasks.map((task) => {
                  const st = STATUS_TAG[task.status] || STATUS_TAG.pending;
                  return (
                    <tr key={task.document_id} className="border-b border-zinc-100 last:border-0 hover:bg-zinc-50">
                      <td className="px-4 py-3">
                        <p className="text-[13px] font-medium text-zinc-900 truncate max-w-[200px]">
                          {task.title || task.filename}
                        </p>
                        <p className="text-[11px] text-zinc-400 truncate max-w-[200px]">{task.filename}</p>
                      </td>
                      <td className="px-4 py-3">
                        <span className={st.cls}>{st.label}</span>
                      </td>
                      <td className="px-4 py-3">
                        <code className="text-[11px] text-zinc-400 font-mono">
                          {task.task_id ? task.task_id.slice(0, 12) + '…' : '-'}
                        </code>
                      </td>
                      <td className="px-4 py-3 text-[13px] text-zinc-500">
                        {task.created_at ? new Date(task.created_at).toLocaleString('zh-CN') : '-'}
                      </td>
                      <td className="px-4 py-3 max-w-[240px]">
                        {task.error_message ? (
                          <div className="flex items-start gap-1.5">
                            <AlertCircle size={13} className="text-red-400 flex-shrink-0 mt-0.5" />
                            <span className="text-[12px] text-red-600 line-clamp-2">{task.error_message}</span>
                          </div>
                        ) : (
                          <span className="text-[13px] text-zinc-300">-</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

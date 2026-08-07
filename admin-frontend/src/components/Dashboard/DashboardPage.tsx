/**
 * 概览页 — 知识库统计看板
 *
 * 调用 GET /api/admin/stats，展示：
 *   - 文档总数 + 状态分布（completed/processing/pending/failed）
 *   - 用户总数
 *   - 向量总数
 */

import { useEffect, useState, useCallback } from 'react';
import { adminApi } from '@/services/api';
import type { AdminStats } from '@/types';
import { FileText, Users, Database, RefreshCw, Loader2, type LucideIcon } from 'lucide-react';

export default function DashboardPage() {
  const [stats, setStats] = useState<AdminStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  const fetchStats = useCallback(async (isRefresh = false) => {
    if (isRefresh) setRefreshing(true);
    else setLoading(true);
    try {
      const data = await adminApi.getStats();
      setStats(data);
    } catch (err) {
      console.error('获取统计失败:', err);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    fetchStats();
  }, [fetchStats]);

  const docStats = stats?.documents;
  const docTotal = docStats?.total ?? 0;

  // 状态分布条形图数据
  const statusBars = [
    { label: '已完成', value: docStats?.completed ?? 0, color: 'bg-green-500' },
    { label: '处理中', value: docStats?.processing ?? 0, color: 'bg-blue-500' },
    { label: '等待中', value: docStats?.pending ?? 0, color: 'bg-amber-500' },
    { label: '失败', value: docStats?.failed ?? 0, color: 'bg-red-500' },
  ];

  return (
    <div className="flex-1 overflow-auto">
      {/* 页头 */}
      <header className="h-14 px-6 border-b border-zinc-200 bg-white flex items-center justify-between flex-shrink-0">
        <h1 className="text-sm font-semibold text-zinc-900">概览</h1>
        <button
          onClick={() => fetchStats(true)}
          disabled={refreshing}
          className="btn-ghost"
        >
          {refreshing ? <Loader2 size={14} className="animate-spin" /> : <RefreshCw size={14} />}
          <span>刷新</span>
        </button>
      </header>

      {/* 内容 */}
      <div className="p-6 space-y-6">
        {loading ? (
          <div className="flex items-center justify-center py-20">
            <Loader2 size={24} className="animate-spin text-zinc-400" />
          </div>
        ) : stats ? (
          <>
            {/* 数字卡片 */}
            <div className="grid grid-cols-3 gap-4">
              <StatCard
                icon={FileText}
                label="文档总数"
                value={docTotal}
              />
              <StatCard
                icon={Users}
                label="用户总数"
                value={stats.users.total}
              />
              <StatCard
                icon={Database}
                label="向量总数"
                value={stats.vectors.total}
              />
            </div>

            {/* 文档状态分布 */}
            <div className="card p-5">
              <h2 className="text-sm font-medium text-zinc-900 mb-4">文档状态分布</h2>
              <div className="space-y-3">
                {statusBars.map((bar) => {
                  const pct = docTotal > 0 ? (bar.value / docTotal) * 100 : 0;
                  return (
                    <div key={bar.label}>
                      <div className="flex items-center justify-between mb-1">
                        <span className="text-[13px] text-zinc-600">{bar.label}</span>
                        <span className="text-[13px] font-medium text-zinc-900">{bar.value}</span>
                      </div>
                      <div className="h-2 bg-zinc-100 rounded-full overflow-hidden">
                        <div
                          className={`h-full ${bar.color} rounded-full transition-all duration-500`}
                          style={{ width: `${pct}%` }}
                        />
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          </>
        ) : (
          <div className="text-center py-20 text-sm text-zinc-400">获取数据失败</div>
        )}
      </div>
    </div>
  );
}

/** 数字统计卡片 */
function StatCard({
  icon: Icon,
  label,
  value,
}: {
  icon: LucideIcon;
  label: string;
  value: number;
}) {
  return (
    <div className="card p-5">
      <div className="flex items-center gap-2 text-zinc-400 mb-2">
        <Icon size={15} />
        <span className="text-[13px]">{label}</span>
      </div>
      <p className="text-2xl font-semibold text-zinc-900 tabular-nums">{value.toLocaleString()}</p>
    </div>
  );
}

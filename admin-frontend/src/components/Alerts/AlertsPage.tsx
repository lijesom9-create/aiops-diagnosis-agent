/**
 * 告警管理页 — 展示 Alertmanager 当前告警
 *
 * 调用 monitoringApi.getAlerts()，展示：
 *   - 状态过滤（全部 / firing / pending）
 *   - 告警卡片列表（severity 颜色条 + 标签 + 摘要）
 *   - 每 30 秒自动刷新
 */

import { useEffect, useState, useCallback } from 'react';
import { monitoringApi } from '@/services/api';
import type { AlertInfo, AlertListResponse } from '@/types';
import { RefreshCw, Loader2, CheckCircle2 } from 'lucide-react';

// 状态过滤选项
const STATE_FILTERS = ['全部', 'firing', 'pending'] as const;
type StateFilter = (typeof STATE_FILTERS)[number];

export default function AlertsPage() {
  const [data, setData] = useState<AlertListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [filter, setFilter] = useState<StateFilter>('全部');

  const fetchAlerts = useCallback(async (isRefresh = false) => {
    if (isRefresh) setRefreshing(true);
    else setLoading(true);
    try {
      const state = filter === '全部' ? undefined : filter;
      const res = await monitoringApi.getAlerts(state);
      setData(res);
    } catch (err) {
      console.error('获取告警失败:', err);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [filter]);

  // 初始加载 + 过滤切换时重新拉取
  useEffect(() => {
    fetchAlerts();
  }, [fetchAlerts]);

  // 自动刷新（30 秒）
  useEffect(() => {
    const timer = setInterval(() => fetchAlerts(true), 30000);
    return () => clearInterval(timer);
  }, [fetchAlerts]);

  const alerts = data?.alerts ?? [];

  return (
    <div className="flex-1 overflow-auto">
      {/* 页头 */}
      <header className="h-14 px-6 border-b border-zinc-200 bg-white flex items-center justify-between flex-shrink-0">
        <h1 className="text-sm font-semibold text-zinc-900">告警管理</h1>
        <button
          onClick={() => fetchAlerts(true)}
          disabled={refreshing}
          className="btn-ghost"
        >
          {refreshing ? <Loader2 size={14} className="animate-spin" /> : <RefreshCw size={14} />}
          <span>刷新</span>
        </button>
      </header>

      {/* 内容 */}
      <div className="p-6 space-y-4 bg-zinc-50 min-h-full">
        {/* 状态过滤 + 数量统计 */}
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-1">
            {STATE_FILTERS.map((s) => (
              <button
                key={s}
                onClick={() => setFilter(s)}
                className={`px-3 py-1.5 text-[13px] rounded-md border transition-colors ${
                  filter === s
                    ? 'bg-white border-zinc-300 text-zinc-900 font-medium'
                    : 'border-transparent text-zinc-500 hover:text-zinc-900'
                }`}
              >
                {s}
              </button>
            ))}
          </div>
          <span className="text-[13px] text-zinc-500">
            共 <span className="font-medium text-zinc-900 tabular-nums">{data?.count ?? 0}</span> 条告警
          </span>
        </div>

        {/* 告警列表 */}
        {loading ? (
          <div className="flex items-center justify-center py-20">
            <Loader2 size={24} className="animate-spin text-zinc-400" />
          </div>
        ) : alerts.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-20 text-zinc-500">
            <CheckCircle2 size={32} className="text-green-500 mb-2" />
            <span className="text-sm">暂无告警</span>
          </div>
        ) : (
          <div className="space-y-3">
            {alerts.map((alert) => (
              <AlertCard key={alert.fingerprint} alert={alert} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

/** 告警卡片 */
function AlertCard({ alert }: { alert: AlertInfo }) {
  const severity = alert.severity;
  const state = alert.state;

  // severity 左侧颜色条
  const barColor =
    severity === 'critical'
      ? 'bg-red-500'
      : severity === 'warning'
        ? 'bg-amber-500'
        : severity === 'info'
          ? 'bg-blue-500'
          : 'bg-zinc-400';

  // severity 标签
  const severityClass =
    severity === 'critical'
      ? 'bg-red-50 text-red-700'
      : severity === 'warning'
        ? 'bg-amber-50 text-amber-700'
        : severity === 'info'
          ? 'bg-blue-50 text-blue-700'
          : 'bg-zinc-100 text-zinc-700';

  // state 标签
  const stateClass =
    state === 'firing'
      ? 'bg-red-500 text-white'
      : state === 'pending'
        ? 'bg-amber-500 text-white'
        : 'bg-zinc-200 text-zinc-700';

  // 时间格式化
  const startTime = alert.starts_at ? new Date(alert.starts_at).toLocaleString('zh-CN') : '-';

  return (
    <div className="bg-white rounded-lg border border-zinc-200 flex overflow-hidden">
      {/* severity 颜色条 */}
      <div className={`w-1 ${barColor} flex-shrink-0`} />

      <div className="flex-1 p-4">
        {/* 标题行 */}
        <div className="flex items-center gap-2 mb-2 flex-wrap">
          <span className="text-sm font-semibold text-zinc-900">{alert.alertname}</span>
          <span className={`px-1.5 py-0.5 text-[11px] rounded ${severityClass}`}>
            {severity}
          </span>
          <span className={`px-1.5 py-0.5 text-[11px] rounded ${stateClass}`}>
            {state}
          </span>
          {alert.category && (
            <span className="text-[11px] text-zinc-400">{alert.category}</span>
          )}
        </div>

        {/* 摘要 */}
        {alert.summary && (
          <p className="text-[13px] text-zinc-600 mb-2">{alert.summary}</p>
        )}

        {/* 元信息 */}
        <div className="flex items-center gap-4 text-[12px] text-zinc-400">
          {alert.instance && <span>实例: {alert.instance}</span>}
          <span>触发: {startTime}</span>
        </div>
      </div>
    </div>
  );
}

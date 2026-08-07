/**
 * 监控看板 — 系统指标概览 + 服务健康
 *
 * 调用 GET /api/monitoring/overview + /api/monitoring/health，展示：
 *   - 监控服务健康状态（Prometheus/Loki/Alertmanager）
 *   - CPU 使用率 + 负载 + 核数
 *   - 内存使用率 + 总量/可用
 *   - 磁盘使用率
 *   - 网络入/出流量
 *   - 自动刷新（30 秒）
 */

import { useEffect, useState, useCallback } from 'react';
import { monitoringApi } from '@/services/api';
import type { SystemOverview, MonitoringHealth } from '@/types';
import {
  Activity,
  Cpu,
  MemoryStick,
  HardDrive,
  Network,
  Server,
  RefreshCw,
  Loader2,
  CheckCircle2,
  XCircle,
} from 'lucide-react';

/** 格式化字节为人类可读（KB/MB/GB） */
function formatBytes(bytes: number): string {
  if (!bytes || bytes <= 0) return '-';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let val = bytes;
  let i = 0;
  while (val >= 1024 && i < units.length - 1) {
    val /= 1024;
    i++;
  }
  return `${val.toFixed(1)} ${units[i]}`;
}

/** 使用率对应颜色（绿/黄/红） */
function usageColor(percent: number): string {
  if (percent >= 90) return 'text-red-600';
  if (percent >= 75) return 'text-amber-600';
  return 'text-green-600';
}

/** 使用率进度条颜色 */
function barColor(percent: number): string {
  if (percent >= 90) return 'bg-red-500';
  if (percent >= 75) return 'bg-amber-500';
  return 'bg-green-500';
}

/** 指标卡片 */
function MetricCard({
  icon: Icon,
  title,
  value,
  unit,
  subtitle,
  percent,
}: {
  icon: React.ElementType;
  title: string;
  value: string | number;
  unit?: string;
  subtitle?: string;
  percent?: number;
}) {
  return (
    <div className="bg-white rounded-lg border border-zinc-200 p-4">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2 text-zinc-500">
          <Icon size={15} />
          <span className="text-[12px] font-medium">{title}</span>
        </div>
        {percent !== undefined && (
          <span className={`text-[13px] font-semibold ${usageColor(percent)}`}>
            {percent.toFixed(1)}%
          </span>
        )}
      </div>
      <div className="flex items-baseline gap-1">
        <span className="text-2xl font-semibold text-zinc-900 tabular-nums">{value}</span>
        {unit && <span className="text-[12px] text-zinc-400">{unit}</span>}
      </div>
      {subtitle && <p className="text-[11px] text-zinc-400 mt-1">{subtitle}</p>}
      {percent !== undefined && (
        <div className="mt-3 h-1.5 bg-zinc-100 rounded-full overflow-hidden">
          <div
            className={`h-full rounded-full transition-all ${barColor(percent)}`}
            style={{ width: `${Math.min(percent, 100)}%` }}
          />
        </div>
      )}
    </div>
  );
}

export default function MonitoringDashboard() {
  const [overview, setOverview] = useState<SystemOverview | null>(null);
  const [health, setHealth] = useState<MonitoringHealth | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchData = useCallback(async (isRefresh = false) => {
    if (isRefresh) setRefreshing(true);
    else setLoading(true);
    setError(null);
    try {
      const [ov, hl] = await Promise.all([
        monitoringApi.getOverview(),
        monitoringApi.getHealth(),
      ]);
      setOverview(ov);
      setHealth(hl);
    } catch (err) {
      console.error('获取监控数据失败:', err);
      setError(err instanceof Error ? err.message : '获取数据失败');
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    fetchData();
    // 自动刷新（30 秒）
    const timer = setInterval(() => fetchData(true), 30000);
    return () => clearInterval(timer);
  }, [fetchData]);

  // 提取指标值（单值）
  const metricVal = (key: string): number | null => {
    const m = overview?.metrics?.[key];
    if (!m || 'error' in m) return null;
    return m.value ?? null;
  };

  const cpuPercent = metricVal('cpu_usage_percent') ?? 0;
  const cpuLoad1 = metricVal('cpu_load1');
  const cpuLoad5 = metricVal('cpu_load5');
  const cpuCores = metricVal('cpu_cores');
  const memPercent = metricVal('memory_usage_percent') ?? 0;
  const memTotal = metricVal('memory_total_bytes');
  const memAvail = metricVal('memory_available_bytes');
  const swapPercent = metricVal('swap_usage_percent');
  const diskPercent = metricVal('disk_root_usage_percent') ?? 0;

  // 网络流量（多值，取总和）
  const netRx = overview?.metrics?.network_rx_bytes_rate?.values;
  const netTx = overview?.metrics?.network_tx_bytes_rate?.values;
  const totalRx = netRx?.reduce((s, v) => s + v.value, 0) ?? 0;
  const totalTx = netTx?.reduce((s, v) => s + v.value, 0) ?? 0;

  if (loading) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <Loader2 size={24} className="animate-spin text-zinc-400" />
      </div>
    );
  }

  return (
    <div className="flex-1 flex flex-col overflow-hidden">
      {/* 页头 */}
      <header className="h-14 px-6 border-b border-zinc-200 bg-white flex items-center justify-between flex-shrink-0">
        <div className="flex items-center gap-2">
          <h1 className="text-sm font-semibold text-zinc-900">监控看板</h1>
          {overview?.timestamp && (
            <span className="text-[11px] text-zinc-400">
              更新于 {overview.timestamp}
            </span>
          )}
        </div>
        <button onClick={() => fetchData(true)} disabled={refreshing} className="btn-ghost">
          {refreshing ? <Loader2 size={14} className="animate-spin" /> : <RefreshCw size={14} />}
          <span>刷新</span>
        </button>
      </header>

      {/* 主内容区 */}
      <div className="flex-1 overflow-auto p-6 space-y-6">
        {error && (
          <div className="bg-red-50 border border-red-200 rounded-lg p-4 text-[13px] text-red-700">
            获取监控数据失败: {error}
          </div>
        )}

        {/* 服务健康状态 */}
        <section>
          <h2 className="text-[12px] font-medium text-zinc-500 mb-3 flex items-center gap-1.5">
            <Server size={13} />
            <span>监控服务</span>
          </h2>
          <div className="grid grid-cols-3 gap-3">
            {health?.services.map((svc) => (
              <div
                key={svc.name}
                className="bg-white rounded-lg border border-zinc-200 p-3 flex items-center gap-3"
              >
                {svc.healthy ? (
                  <CheckCircle2 size={18} className="text-green-500 flex-shrink-0" />
                ) : (
                  <XCircle size={18} className="text-red-500 flex-shrink-0" />
                )}
                <div className="min-w-0">
                  <p className="text-[13px] font-medium text-zinc-900">{svc.name}</p>
                  <p className="text-[11px] text-zinc-400 truncate">{svc.url}</p>
                </div>
              </div>
            )) ?? <p className="text-[12px] text-zinc-400">加载中...</p>}
          </div>
        </section>

        {/* 系统指标 */}
        <section>
          <h2 className="text-[12px] font-medium text-zinc-500 mb-3 flex items-center gap-1.5">
            <Activity size={13} />
            <span>系统指标</span>
          </h2>
          <div className="grid grid-cols-4 gap-3">
            <MetricCard
              icon={Cpu}
              title="CPU 使用率"
              value={cpuPercent.toFixed(1)}
              unit="%"
              percent={cpuPercent}
              subtitle={`负载 ${cpuLoad1?.toFixed(2) ?? '-'} / ${cpuLoad5?.toFixed(2) ?? '-'}  |  ${cpuCores?.toFixed(0) ?? '-'} 核`}
            />
            <MetricCard
              icon={MemoryStick}
              title="内存使用率"
              value={memPercent.toFixed(1)}
              unit="%"
              percent={memPercent}
              subtitle={`可用 ${formatBytes(memAvail ?? 0)} / 总 ${formatBytes(memTotal ?? 0)}`}
            />
            <MetricCard
              icon={HardDrive}
              title="磁盘使用率"
              value={diskPercent ? diskPercent.toFixed(1) : '-'}
              unit={diskPercent ? '%' : ''}
              percent={diskPercent || undefined}
              subtitle={swapPercent !== null ? `Swap ${swapPercent.toFixed(1)}%` : '根分区'}
            />
            <MetricCard
              icon={Network}
              title="网络流量"
              value={formatBytes(totalRx)}
              unit="/s"
              subtitle={`入 ${formatBytes(totalRx)}/s  |  出 ${formatBytes(totalTx)}/s`}
            />
          </div>
        </section>

        {/* 指标明细表 */}
        <section>
          <h2 className="text-[12px] font-medium text-zinc-500 mb-3">指标明细</h2>
          <div className="bg-white rounded-lg border border-zinc-200 overflow-hidden">
            <table className="w-full text-[13px]">
              <thead className="bg-zinc-50 border-b border-zinc-200">
                <tr>
                  <th className="px-4 py-2 text-left font-medium text-zinc-500">指标</th>
                  <th className="px-4 py-2 text-right font-medium text-zinc-500">值</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-100">
                {overview &&
                  Object.entries(overview.metrics).map(([key, val]) => {
                    if (!val) return null;
                    const displayVal =
                      'error' in val
                        ? `错误: ${val.error?.slice(0, 50)}`
                        : 'value' in val
                        ? val.value !== null && val.value !== undefined
                          ? val.value.toFixed(2)
                          : '-'
                        : `${val.values?.length ?? 0} 条序列`;
                    return (
                      <tr key={key}>
                        <td className="px-4 py-2 text-zinc-700 font-mono text-[12px]">{key}</td>
                        <td className="px-4 py-2 text-right tabular-nums text-zinc-900">
                          {displayVal}
                        </td>
                      </tr>
                    );
                  })}
              </tbody>
            </table>
          </div>
        </section>
      </div>
    </div>
  );
}

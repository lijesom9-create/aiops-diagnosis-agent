/**
 * 日志查询页 — 容器日志查询（Loki）
 *
 * 调用 GET /api/monitoring/logs，按容器名/关键词/时间范围查询日志。
 * 按需查询，不自动刷新。
 */

import { useState, useCallback } from 'react';
import { monitoringApi } from '@/services/api';
import type { LogQueryResponse } from '@/types';
import { Search, Loader2, Terminal } from 'lucide-react';

// 时间范围选项
const RANGES = [
  { value: 5, label: '5分钟' },
  { value: 30, label: '30分钟' },
  { value: 60, label: '1小时' },
  { value: 360, label: '6小时' },
];

// 根据日志内容判断级别颜色：ERROR/Exception/Traceback 红色，WARN/Warning 黄色
function getLogColor(content: string): string {
  const lower = content.toLowerCase();
  if (lower.includes('error') || lower.includes('exception') || lower.includes('traceback')) {
    return 'text-red-600';
  }
  if (lower.includes('warn')) {
    return 'text-amber-600';
  }
  return 'text-zinc-700';
}

// 格式化时间，无法解析时原样返回
function formatTime(time: string): string {
  const d = new Date(time);
  if (!isNaN(d.getTime())) {
    return d.toLocaleString('zh-CN', { hour12: false });
  }
  return time;
}

export default function LogsPage() {
  const [container, setContainer] = useState('');
  const [keyword, setKeyword] = useState('');
  const [rangeMinutes, setRangeMinutes] = useState(30);
  const [loading, setLoading] = useState(false);
  const [data, setData] = useState<LogQueryResponse | null>(null);
  const [hasQueried, setHasQueried] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleQuery = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await monitoringApi.getLogs({
        container: container.trim() || undefined,
        keyword: keyword.trim() || undefined,
        minutes: rangeMinutes,
      });
      setData(res);
      setHasQueried(true);
    } catch (err) {
      console.error('日志查询失败:', err);
      setError('查询失败，请稍后重试');
      setData(null);
      setHasQueried(true);
    } finally {
      setLoading(false);
    }
  }, [container, keyword, rangeMinutes]);

  return (
    <div className="flex-1 flex flex-col overflow-hidden min-h-0">
      {/* 页头 */}
      <header className="h-14 px-6 border-b border-zinc-200 bg-white flex items-center justify-between flex-shrink-0">
        <h1 className="text-sm font-semibold text-zinc-900">日志查询</h1>
        <button onClick={handleQuery} disabled={loading} className="btn-primary">
          {loading ? <Loader2 size={14} className="animate-spin" /> : <Search size={14} />}
          <span>查询</span>
        </button>
      </header>

      {/* 内容 */}
      <div className="flex-1 flex flex-col overflow-hidden p-6 gap-4 min-h-0">
        {/* 查询表单 */}
        <div className="flex items-center gap-3 flex-shrink-0">
          <input
            type="text"
            value={container}
            onChange={(e) => setContainer(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleQuery()}
            placeholder="容器名，如 backend"
            className="input flex-1 max-w-[240px]"
          />
          <input
            type="text"
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleQuery()}
            placeholder="关键词，如 ERROR"
            className="input flex-1 max-w-[240px]"
          />
          <select
            value={rangeMinutes}
            onChange={(e) => setRangeMinutes(Number(e.target.value))}
            className="input w-[140px]"
          >
            {RANGES.map((r) => (
              <option key={r.value} value={r.value}>
                {r.label}
              </option>
            ))}
          </select>
          <button onClick={handleQuery} disabled={loading} className="btn-primary">
            {loading ? <Loader2 size={14} className="animate-spin" /> : <Search size={14} />}
            <span>查询</span>
          </button>
        </div>

        {/* 结果区 */}
        {loading ? (
          <div className="flex-1 flex items-center justify-center min-h-0">
            <Loader2 size={24} className="animate-spin text-zinc-400" />
          </div>
        ) : error ? (
          <div className="flex-1 flex items-center justify-center min-h-0">
            <p className="text-sm text-red-500">{error}</p>
          </div>
        ) : data && data.lines.length > 0 ? (
          <>
            {/* 统计 */}
            <div className="flex items-center justify-between flex-shrink-0">
              <span className="text-[13px] text-zinc-500">
                共 <span className="font-medium text-zinc-900">{data.count}</span> 条日志
              </span>
              <span className="text-[12px] text-zinc-400">范围：最近 {data.range_minutes} 分钟</span>
            </div>
            {/* 日志列表 */}
            <div className="flex-1 overflow-auto min-h-0 card font-mono text-[13px]">
              {data.lines.map((line, idx) => (
                <div
                  key={idx}
                  className="flex items-start gap-2 px-3 py-1 border-b border-zinc-100 last:border-0 hover:bg-zinc-50"
                >
                  <span className="text-zinc-400 flex-shrink-0">{formatTime(line.time)}</span>
                  <span className="tag-info flex-shrink-0">{line.container}</span>
                  <span
                    className={`flex-1 whitespace-pre-wrap break-words ${getLogColor(line.content)}`}
                  >
                    {line.content}
                  </span>
                </div>
              ))}
            </div>
          </>
        ) : (
          <div className="flex-1 flex items-center justify-center min-h-0">
            <div className="text-center">
              <Terminal size={32} className="mx-auto text-zinc-300 mb-3" />
              <p className="text-sm text-zinc-400">
                {hasQueried ? '暂无日志' : '输入查询条件后点击查询'}
              </p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

/**
 * 类型定义 — 管理后台
 */

/** 用户信息 */
export interface User {
  user_id: string;
  username: string;
  email: string;
  role: 'admin' | 'teacher' | 'student';
  org_id?: string;
  org_name?: string;
  created_at?: string;
}

/** 登录请求 */
export interface LoginRequest {
  username: string;
  password: string;
}

/** 管理后台统计 */
export interface AdminStats {
  documents: {
    total: number;
    completed: number;
    processing: number;
    pending: number;
    failed: number;
  };
  users: {
    total: number;
  };
  vectors: {
    total: number;
  };
}

/** 文档信息 */
export interface DocumentInfo {
  document_id: string;
  filename: string;
  title: string;
  status: 'pending' | 'processing' | 'completed' | 'failed';
  chunk_count: number;
  char_count: number;
  created_at: string;
  category: string;
  tags: string[];
}

/** 文档列表响应 */
export interface DocumentListResponse {
  documents: DocumentInfo[];
  total: number;
}

/** 任务信息（有 task_id 的文档） */
export interface TaskInfo {
  document_id: string;
  filename: string;
  title: string;
  status: string;
  task_id: string;
  error_message: string;
  created_at: string;
  finished_at: string | null;
}

/** 任务列表响应 */
export interface TaskListResponse {
  tasks: TaskInfo[];
  total: number;
}

/** 用户列表响应 */
export interface UserListResponse {
  users: User[];
  total: number;
  page: number;
  page_size: number;
}

/** 批量上传结果 */
export interface BatchUploadResult {
  total: number;
  success: number;
  skipped: number;
  failed: number;
  results: Array<{
    filename: string;
    status: 'pending' | 'skipped' | 'failed';
    document_id?: string;
    reason?: string;
  }>;
}

// ========== 监控数据类型 ==========

/** 系统指标概览 */
export interface SystemOverview {
  prometheus_url: string;
  timestamp: string;
  metrics: {
    cpu_usage_percent?: MetricValue;
    cpu_load1?: MetricValue;
    cpu_load5?: MetricValue;
    cpu_cores?: MetricValue;
    memory_total_bytes?: MetricValue;
    memory_available_bytes?: MetricValue;
    memory_usage_percent?: MetricValue;
    swap_usage_percent?: MetricValue;
    disk_root_usage_percent?: MetricValue;
    network_rx_bytes_rate?: MetricValue;
    network_tx_bytes_rate?: MetricValue;
    [key: string]: MetricValue | undefined;
  };
}

/** 指标值（单值或多值） */
export interface MetricValue {
  value?: number | null;
  values?: Array<{ labels: Record<string, string>; value: number }>;
  error?: string;
  note?: string;
}

/** PromQL 查询结果 */
export interface PrometheusQueryResult {
  query: string;
  count: number;
  values: Array<{ labels: Record<string, string>; value: number }>;
  timestamp: string;
}

/** PromQL 范围查询结果（时序趋势） */
export interface PrometheusRangeResult {
  query: string;
  range_minutes: number;
  step: string;
  series_count: number;
  series: Array<{
    labels: Record<string, string>;
    points: Array<{ time: string; value: number }>;
  }>;
}

/** 告警信息 */
export interface AlertInfo {
  alertname: string;
  severity: 'critical' | 'warning' | 'info' | string;
  category: string;
  instance: string;
  state: 'firing' | 'pending' | 'suppressed' | 'inactive' | string;
  summary: string;
  description: string;
  starts_at: string;
  ends_at: string;
  fingerprint: string;
}

/** 告警列表响应 */
export interface AlertListResponse {
  count: number;
  alerts: AlertInfo[];
  timestamp: string;
}

/** 日志行 */
export interface LogLine {
  time: string;
  container: string;
  content: string;
}

/** 日志查询响应 */
export interface LogQueryResponse {
  query: string;
  count: number;
  lines: LogLine[];
  range_minutes: number;
}

/** 监控服务健康状态 */
export interface MonitoringHealth {
  services: Array<{
    name: string;
    url: string;
    healthy: boolean;
  }>;
  timestamp: string;
}

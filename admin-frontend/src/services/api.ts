/**
 * API 服务 — 管理后台
 *
 * 认证：httpOnly cookie（withCredentials: true），与主前端共享同一后端。
 * 所有 /api/admin/* 端点要求 admin 角色（后端 require_admin 守卫）。
 */

import axios, { AxiosInstance } from 'axios';
import type {
  AdminStats,
  DocumentListResponse,
  TaskListResponse,
  UserListResponse,
  BatchUploadResult,
  User,
  SystemOverview,
  PrometheusQueryResult,
  PrometheusRangeResult,
  AlertListResponse,
  LogQueryResponse,
  MonitoringHealth,
} from '@/types';

const api: AxiosInstance = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || '/api',
  timeout: 120000,
  withCredentials: true, // 自动携带 httpOnly cookie
  headers: {
    'Content-Type': 'application/json',
  },
});

// 响应拦截器：401 跳登录，403 提示无权限
api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      if (!window.location.pathname.startsWith('/login')) {
        window.location.href = '/login';
      }
    }
    return Promise.reject(error);
  }
);

// ========== 认证 API ==========

export const authApi = {
  login: (data: { username: string; password: string }) =>
    api.post('/auth/login', data),

  logout: () => api.post('/auth/logout'),

  getMe: () => api.get<User>('/auth/me'),
};

// ========== 管理后台 API ==========

export const adminApi = {
  /** 知识库统计 */
  getStats: () => api.get<AdminStats>('/admin/stats').then((r) => r.data),

  /** 用户列表（分页） */
  listUsers: (page = 1, pageSize = 20) =>
    api
      .get<UserListResponse>('/admin/users', { params: { page, page_size: pageSize } })
      .then((r) => r.data),

  /** 修改用户角色 */
  updateUserRole: (userId: string, role: string) =>
    api
      .patch(`/admin/users/${userId}/role`, { role })
      .then((r) => r.data),

  /** 任务监控列表 */
  listTasks: (statusFilter?: string) =>
    api
      .get<TaskListResponse>('/admin/tasks', {
        params: statusFilter ? { status_filter: statusFilter } : {},
      })
      .then((r) => r.data),
};

// ========== 文档管理 API（复用 /api/documents/*，后端 require_admin 守卫） ==========

export const documentsApi = {
  /** 文档列表（admin 看全部） */
  list: (category?: string) =>
    api
      .get<DocumentListResponse>('/documents/', {
        params: category ? { category } : {},
      })
      .then((r) => r.data),

  /** 上传单个文档 */
  upload: (file: File, category = 'other', title?: string) => {
    const formData = new FormData();
    formData.append('file', file);
    formData.append('category', category);
    if (title) formData.append('title_utf8', title);
    return api
      .post('/documents/upload', formData, {
        headers: { 'Content-Type': 'multipart/form-data' },
      })
      .then((r) => r.data);
  },

  /** 批量上传 */
  batchUpload: (files: File[], skipDuplicate = true) => {
    const formData = new FormData();
    files.forEach((f) => formData.append('files', f));
    formData.append('skip_duplicate', String(skipDuplicate));
    return api
      .post<BatchUploadResult>('/documents/batch-upload', formData, {
        headers: { 'Content-Type': 'multipart/form-data' },
      })
      .then((r) => r.data);
  },

  /** 删除文档 */
  remove: (documentId: string) =>
    api.delete(`/documents/${documentId}`).then((r) => r.data),

  /** 重试失败文档 */
  retry: (documentId: string) =>
    api.post(`/documents/${documentId}/retry`).then((r) => r.data),

  /** 查询文档处理状态 */
  getStatus: (documentId: string) =>
    api.get(`/documents/${documentId}/status`).then((r) => r.data),
};

export default api;

// ========== 监控数据 API（智能运维看板） ==========

export const monitoringApi = {
  /** 系统指标概览（CPU/内存/磁盘/网络） */
  getOverview: () =>
    api.get<SystemOverview>('/monitoring/overview').then((r) => r.data),

  /** PromQL 瞬时查询 */
  query: (query: string) =>
    api
      .get<PrometheusQueryResult>('/monitoring/query', { params: { query } })
      .then((r) => r.data),

  /** PromQL 范围查询（时序趋势） */
  queryRange: (query: string, minutes = 30) =>
    api
      .get<PrometheusRangeResult>('/monitoring/range', {
        params: { query, minutes },
      })
      .then((r) => r.data),

  /** 当前告警列表 */
  getAlerts: (state?: string) =>
    api
      .get<AlertListResponse>('/monitoring/alerts', {
        params: state ? { state } : {},
      })
      .then((r) => r.data),

  /** 日志查询（Loki） */
  getLogs: (params: {
    container?: string;
    keyword?: string;
    minutes?: number;
    limit?: number;
  }) => api.get<LogQueryResponse>('/monitoring/logs', { params }).then((r) => r.data),

  /** 监控服务健康状态 */
  getHealth: () =>
    api.get<MonitoringHealth>('/monitoring/health').then((r) => r.data),
};

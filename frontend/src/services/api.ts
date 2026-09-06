/**
 * API服务
 * 知识助手 - RAG 问答 + 文档管理 + 记忆系统
 */

import axios, { AxiosInstance, AxiosResponse } from 'axios';
import type {
  User,
  Token,
  LoginRequest,
  RegisterRequest,
} from '@/types';

// 创建axios实例
// baseURL 通过 Vite 环境变量配置：
//   - 开发环境 (.env.development): VITE_API_BASE_URL=/api（走 vite 代理）
//   - 生产环境 (.env.production): VITE_API_BASE_URL=/api（走 nginx 反代）或完整域名
// withCredentials: true 让浏览器自动发送 httpOnly cookie（认证 token）
const api: AxiosInstance = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || '/api',
  timeout: 120000,  // Teaching Agent 可能需要较长时间
  withCredentials: true,  // 自动携带 httpOnly cookie
  headers: {
    'Content-Type': 'application/json',
  },
});

// 请求拦截器：cookie 由浏览器自动管理，无需手动添加 Authorization 头

// 响应拦截器：处理 401 未授权（cookie 过期或无效）
api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      // cookie 由后端管理，前端只需跳转到登录页
      // 页面刷新后 authStore 会重新初始化，checkAuth 会验证 cookie 有效性
      if (!window.location.pathname.startsWith('/login')) {
        window.location.href = '/login';
      }
    }
    return Promise.reject(error);
  }
);

// 认证API
export const authApi = {
  register: (data: RegisterRequest): Promise<AxiosResponse<Token>> =>
    api.post('/auth/register', data),

  login: (data: LoginRequest): Promise<AxiosResponse<Token>> =>
    api.post('/auth/login', data),

  logout: (): Promise<AxiosResponse<{ message: string }>> =>
    api.post('/auth/logout'),

  getMe: (): Promise<AxiosResponse<User>> =>
    api.get('/auth/me'),

  verifyToken: (): Promise<AxiosResponse<{ valid: boolean; user: User }>> =>
    api.get('/auth/verify'),
};

export interface LearningInsight {
  id?: string;
  eval_ids?: string[];
  user_id?: string;
  category: string;
  finding: string;
  suggestion: string;
  confidence: number;
}

export interface LearningReport {
  user_id: string;
  summary: string;
  insights: LearningInsight[];
  recommendations: string[];
  metrics_snapshot?: Record<string, unknown>;
  generated_at?: string;
}


// ========== 学习主题 API ==========

export interface Topic {
  topic_id: string;
  title: string;
  description?: string;
  level: string;
  daily_minutes: number;
  status: string;
  target_date?: string;
  created_at: string;
  updated_at: string;
}

export interface CreateTopicRequest {
  title: string;
  description?: string;
  level?: string;
  daily_minutes?: number;
  target_date?: string;
}


// ========== 学习资料 API ==========

export interface Document {
  document_id: string;
  filename: string;
  doc_type: string;
  status: string;
  chunk_count: number;
  created_at: string;
}


// ========== 学习计划 API ==========

export interface StudyPlan {
  plan_id: string;
  title: string;
  description?: string;
  level: string;
  daily_minutes: number;
  status: string;
  phases: PlanPhase[];
  total_days: number;
  total_tasks: number;
  created_at: string;
}

export interface PlanPhase {
  title: string;
  description?: string;
  duration_days: number;
  tasks: PlanTask[];
}

export interface PlanTask {
  title: string;
  description?: string;
  estimated_minutes: number;
  type: string;
}


// ========== 评估/学习报告 API ==========

export interface EvaluationSummary {
  total_evaluations: number;
  average_score: number;
  latest_score: number | null;
  improvement_trend: string;
}


// ========== 飞书集成 API ==========

export interface FeishuConfig {
  webhook_url: string | null;
  has_secret: boolean;
  user_open_id: string | null;
  cli_available: boolean;
}

export const feishuApi = {
  getConfig: (): Promise<AxiosResponse<FeishuConfig>> =>
    api.get('/users/me/feishu'),

  updateConfig: (data: Partial<FeishuConfig>): Promise<AxiosResponse<FeishuConfig>> =>
    api.put('/users/me/feishu', data),

  sendTest: (message: string, sendTo?: string): Promise<AxiosResponse<any>> =>
    api.post('/feishu/test', { message, send_to: sendTo || 'personal' }),

  createReminder: (data: {
    topic: string;
    task: string;
    reminder_hour?: number;
    recurrence?: string;
  }): Promise<AxiosResponse<any>> =>
    api.post('/feishu/calendar/reminder', data),

  generateSummary: (summaryType: string): Promise<AxiosResponse<any>> =>
    api.post('/feishu/summary/generate', { summary_type: summaryType }),
};

// ========== 知识库 API ==========

export interface KnowledgeDocument {
  id: string;
  title: string;
  content: string;
  type: string;
  source: string;
  metadata: Record<string, any>;
  chunks: string[];
  created_at: string;
  updated_at: string;
}

export interface KnowledgeSearchRequest {
  query: string;
  type_filter?: string[];
  limit?: number;
}

export const knowledgeApi = {
  list: (type?: string): Promise<AxiosResponse<KnowledgeDocument[]>> =>
    api.get('/knowledge/documents', { params: type ? { type } : {} }),

  get: (docId: string): Promise<AxiosResponse<KnowledgeDocument>> =>
    api.get(`/knowledge/documents/${docId}`),

  create: (data: {
    title: string;
    content: string;
    type?: string;
    source?: string;
    metadata?: Record<string, any>;
  }): Promise<AxiosResponse<KnowledgeDocument>> =>
    api.post('/knowledge/documents', data),

  update: (docId: string, data: {
    title?: string;
    content?: string;
    metadata?: Record<string, any>;
  }): Promise<AxiosResponse<KnowledgeDocument>> =>
    api.put(`/knowledge/documents/${docId}`, data),

  delete: (docId: string): Promise<AxiosResponse<void>> =>
    api.delete(`/knowledge/documents/${docId}`),

  search: (data: KnowledgeSearchRequest): Promise<AxiosResponse<{ results: KnowledgeDocument[]; total: number }>> =>
    api.post('/knowledge/search', data),

  statistics: (): Promise<AxiosResponse<{ total: number; by_type: Record<string, number> }>> =>
    api.get('/knowledge/statistics'),
};

// 博客生成API
export interface BlogGenerateRequest {
  topic: string;
  article_type?: string;
  requirements?: string;
  urls?: string[];
}

export interface OutlineRequest {
  topic: string;
  article_type?: string;
  knowledge_context?: string;
}

export interface OutlineApproveRequest {
  outline: string;
  approved: boolean;
  adjustments?: string;
}

export interface ContentGenerateRequest {
  topic: string;
  outline: string;
  knowledge_context?: string;
  style?: string;
}

export interface BlogGenerateResponse {
  task_id: string;
  status: string;
  message: string;
}

export interface OutlineResponse {
  topic: string;
  article_type: string;
  outline: string;
  knowledge_context?: string;
}

export interface ContentResponse {
  topic: string;
  content: string;
  word_count: number;
}


// 工作流API
export interface WorkflowGenerateRequest {
  topic: string;
  article_type?: string;
  requirements?: string;
  urls?: string[];
  use_workflow?: boolean;
}

export interface TaskState {
  task_id: string;
  task_name: string;
  task_type: string;
  status: 'pending' | 'running' | 'completed' | 'failed' | 'skipped';
  result?: string;
  error?: string;
  started_at?: string;
  completed_at?: string;
  duration?: number;
}

export interface WorkflowState {
  workflow_id: string;
  status: 'pending' | 'running' | 'completed' | 'failed' | 'paused';
  tasks: Record<string, TaskState>;
  progress: {
    total: number;
    completed: number;
    failed: number;
    running: number;
    pending: number;
    progress: number;
  };
  errors: Record<string, string>;
  created_at: string;
  updated_at: string;
}

export interface WorkflowGenerateResponse {
  workflow_id: string;
  status: string;
  progress: {
    total: number;
    completed: number;
    failed: number;
  };
  result?: {
    topic: string;
    outline: string;
    content: string;
    word_count: number;
  };
  trace: Array<{
    event_type: string;
    timestamp: string;
    task_id?: string;
    task_name?: string;
    duration?: number;
    error?: string;
  }>;
}


// ========== RAG 问答 API ==========

export interface ChatRequest {
  message: string;
  session_id?: string;
  use_memory?: boolean;
  use_rag?: boolean;
}

// ========== 文档管理 API (新版) ==========

export interface DocumentInfo {
  document_id: string;
  filename: string;
  title: string;
  status: string;
  chunk_count: number;
  char_count: number;
  created_at: string;
  category: string;
  tags: string[];
}

export const documentsApiNew = {
  list: (category?: string): Promise<{ documents: DocumentInfo[]; total: number }> =>
    api.get('/documents/', { params: category ? { category } : {} }).then(res => res.data),

  upload: (file: File, category: string = 'other'): Promise<{ document_id: string; filename: string; status: string }> => {
    const formData = new FormData();
    formData.append('file', file);
    formData.append('category', category);
    return api.post('/documents/upload', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    }).then(res => res.data);
  },

  delete: (documentId: string): Promise<{ message: string }> =>
    api.delete(`/documents/${documentId}`).then(res => res.data),

  getStatus: (documentId: string): Promise<DocumentInfo> =>
    api.get(`/documents/${documentId}/status`).then(res => res.data),
};

// ========== 记忆管理 API ==========

export interface UserProfile {
  user_id: string;
  name: string;
  learning_style: string;
  weak_topics: string[];
  strong_topics: string[];
  preferences: Record<string, any>;
}

export interface MemoryStats {
  user_id: string;
  has_profile: boolean;
  weak_topics: number;
  strong_topics: number;
  sessions: number;
  total_turns: number;
  archival_entries: number;
}


// ========== LangGraph Agent API（流式聊天 + 会话管理） ==========

/** 引用溯源数据结构（匹配后端 citations） */
export interface Citation {
  index: number;
  doc_id: string;
  title: string;
  heading_path: string;
  score: number;
  source: string;
  image_path?: string | null;
}

/** 会话列表项（匹配后端 SessionItem） */
export interface LangGraphSession {
  session_id: string;
  user_id: string;
  title: string;
  created_at: string;
  updated_at: string;
  message_count: number;
}

/** 消息项（匹配后端 MessageItem） */
export interface LangGraphMessage {
  role: string;
  content: string;
  tools_used?: string[] | null;
  timestamp?: string | null;
}

/** SSE 流式事件回调 */
export interface LangGraphStreamCallbacks {
  onStart?: (sessionId: string) => void;
  onToken?: (content: string) => void;
  onToolCalls?: (tools: string[]) => void;
  onToolResult?: (name: string, content: string) => void;
  onReflection?: (content: string) => void;
  onDone?: (data: { tools_used: string[]; step_count: number; citations: Citation[]; session_id: string; sanitized_content?: string }) => void;
  onError?: (message: string) => void;
}

/**
 * LangGraph 流式聊天
 * 调用 /api/langgraph/chat/stream，消费 SSE 事件
 */
export async function langgraphChatStream(
  message: string,
  sessionId: string | undefined,
  useWebSearch: boolean,
  callbacks: LangGraphStreamCallbacks,
): Promise<void> {
  // 使用 AbortController 实现超时（fetch 不继承 axios 的 timeout 配置）
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 120000);

  let response: Response;
  try {
    response = await fetch('/api/langgraph/chat/stream', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      credentials: 'include',  // 自动携带 httpOnly cookie
      body: JSON.stringify({
        message,
        session_id: sessionId,
        use_web_search: useWebSearch,
      }),
      signal: controller.signal,
    });
  } catch (err) {
    clearTimeout(timeoutId);
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw new Error('请求超时，请稍后重试');
    }
    throw err;
  }

  if (!response.ok) {
    clearTimeout(timeoutId);
    throw new Error(`HTTP ${response.status}`);
  }

  // 校验 body 是否存在（非空断言不安全）
  if (!response.body) {
    clearTimeout(timeoutId);
    throw new Error('响应体为空');
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';

      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed.startsWith('data: ')) continue;

        try {
          const data = JSON.parse(trimmed.slice(6));
          switch (data.type) {
            case 'start':
              callbacks.onStart?.(data.session_id);
              break;
            case 'token':
              callbacks.onToken?.(data.content || '');
              break;
            case 'tool_calls':
              callbacks.onToolCalls?.(data.tools || []);
              break;
            case 'tool_result':
              callbacks.onToolResult?.(data.name || '', data.content || '');
              break;
            case 'reflection':
              callbacks.onReflection?.(data.content || '');
              break;
            case 'done':
              callbacks.onDone?.({
                tools_used: data.tools_used || [],
                step_count: data.step_count || 0,
                citations: data.citations || [],
                session_id: data.session_id || '',
                sanitized_content: data.sanitized_content,
              });
              break;
            case 'error':
              callbacks.onError?.(data.content || '未知错误');
              break;
          }
        } catch {
          // 忽略 JSON 解析错误
        }
      }
    }
  } finally {
    clearTimeout(timeoutId);
    reader.releaseLock();
  }
}

/** LangGraph 会话管理 API */
export const langgraphApi = {
  /** 创建新会话 */
  createSession: (title?: string): Promise<LangGraphSession> =>
    api.post('/langgraph/sessions', { title: title || '新对话' }).then(res => res.data),

  /** 列出当前用户所有会话 */
  listSessions: (limit?: number): Promise<{ sessions: LangGraphSession[]; total: number }> =>
    api.get('/langgraph/sessions', { params: limit ? { limit } : {} }).then(res => res.data),

  /** 获取会话消息历史 */
  getSessionMessages: (sessionId: string, limit?: number): Promise<{ session_id: string; messages: LangGraphMessage[]; total: number }> =>
    api.get(`/langgraph/sessions/${sessionId}/messages`, { params: { limit: limit || 100 } }).then(res => res.data),

  /** 重命名会话标题 */
  updateSession: (sessionId: string, title: string): Promise<LangGraphSession> =>
    api.patch(`/langgraph/sessions/${sessionId}`, { title }).then(res => res.data),

  /** 删除会话 */
  deleteSession: (sessionId: string): Promise<{ message: string; session_id: string }> =>
    api.delete(`/langgraph/sessions/${sessionId}`).then(res => res.data),

  /** 提交答案反馈 */
  submitFeedback: (data: { session_id: string; message_content: string; rating: 'positive' | 'negative'; comment?: string }): Promise<{ feedback_id: string; message: string }> =>
    api.post('/langgraph/feedback', data).then(res => res.data),

  /** 获取反馈统计 */
  getFeedbackStats: (): Promise<{ total: number; positive: number; negative: number; satisfaction_rate: number }> =>
    api.get('/langgraph/feedback/stats').then(res => res.data),
};

export default api;

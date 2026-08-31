/**
 * 类型定义
 * Teaching Agent 架构
 */

// 用户相关
export interface User {
  user_id: string;
  username: string;
  email: string;
  role: 'student' | 'teacher' | 'admin';
}

export interface Token {
  access_token: string;
  token_type: string;
}

export interface LoginRequest {
  username: string;
  password: string;
}

export interface RegisterRequest {
  username: string;
  password: string;
  email: string;
  // 安全约束：后端不接受客户端角色（注册固定 student，仅超管用户名例外）
  org_name?: string;
}

// API响应
export interface ApiResponse<T> {
  data?: T;
  error?: string;
  message?: string;
}

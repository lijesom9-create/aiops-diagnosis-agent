/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 前端请求的 baseURL（开发环境 /api 走代理，生产环境可配置完整域名） */
  readonly VITE_API_BASE_URL: string;
  /** 开发环境后端地址（vite 代理 target） */
  readonly VITE_BACKEND_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

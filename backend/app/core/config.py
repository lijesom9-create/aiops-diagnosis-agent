"""
应用配置模块
支持多环境配置和AI模型切换
"""

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator
from typing import Optional, List
from functools import lru_cache
import secrets


class Settings(BaseSettings):
    """应用设置"""

    # 应用基础配置
    APP_NAME: str = "教育培训Agent"
    APP_VERSION: str = "1.0.0"
    # 运行环境：development | production
    # production 模式下会强制校验 SECRET_KEY、收窄 CORS、禁用 DEBUG
    ENV: str = "development"
    DEBUG: bool = False
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # 数据库配置
    MONGODB_URL: str = "mongodb://localhost:27017"
    MONGODB_DB_NAME: str = "education_agent"

    # JWT配置
    SECRET_KEY: str = ""  # development 模式下自动生成；production 模式下必须显式设置
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30

    # AI模型配置（统一配置）
    # 模型名称格式：deepseek/chat, zhipu/glm-4-flash, openai/gpt-4o-mini, anthropic/claude-sonnet-4-20250514, qwen/qwen-turbo
    AI_MODEL: str = "deepseek/chat"
    AI_API_KEY: Optional[str] = None
    AI_BASE_URL: Optional[str] = None  # 可选，自定义API地址

    # Embedding 配置
    EMBEDDING_API_KEY: Optional[str] = None
    EMBEDDING_MODEL: Optional[str] = None  # 例如 text-embedding-3-small
    EMBEDDING_BASE_URL: Optional[str] = None
    USE_LOCAL_EMBEDDING: bool = True
    LOCAL_EMBEDDING_MODEL: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

    # ChromaDB 配置
    CHROMA_HOST: Optional[str] = None
    CHROMA_PORT: Optional[int] = None
    CHROMA_PERSIST_DIR: Optional[str] = None

    # 向量存储后端选择：chroma | qdrant
    VECTOR_STORE_BACKEND: str = "chroma"

    # Qdrant 配置（local mode 无需 host/port）
    QDRANT_HOST: Optional[str] = None
    QDRANT_PORT: Optional[int] = None
    QDRANT_PERSIST_DIR: Optional[str] = None

    # 重排器配置
    RERANKER_ENABLED: bool = True
    RERANKER_MODEL_NAME: str = "BAAI/bge-reranker-base"

    # ========== RAG 检索链路推荐参数（基于参数扫描实验） ==========
    # 检索 top_k：召回与 LLM 上下文成本的平衡
    # 扩大数据集实验：top_k=8 recall +0.094（0.712→0.805），NDCG +0.015，MRR +0.003
    RAG_TOP_K: int = 8
    # 候选集倍数：向量/BM25 各返回 top_k * multiplier 个候选（3 最佳）
    RAG_CANDIDATE_MULTIPLIER: int = 3
    # RRF 融合参数 k 值（60 业界默认，有 reranker 时调参无效）
    RAG_RRF_K: int = 60
    # RRF 权重（vector : bm25，1:1 默认，有 reranker 时调参无效）
    RAG_VECTOR_WEIGHT: float = 1.0
    RAG_BM25_WEIGHT: float = 1.0
    # 查询重写模式：basic | enhanced | llm | enhanced_llm
    # enhanced=规则重写（默认）；enhanced_llm=规则+LLM MultiQuery（可选）
    RAG_REWRITE_MODE: str = "enhanced"
    # 父子分离存储（推荐 True：BM25 精度 +29pp）
    RAG_SEPARATE_PARENT_CHILD: bool = True

    # ========== P1-2: 上下文 Token 预算控制 ==========
    # LLM 上下文窗口 token 预算（不含用户查询和系统提示）
    # DeepSeek/Qwen 32k 模型推荐 6000（给 RAG 留余地，剩余留给 LLM 输出）
    # GPT-4o 128k 模型可放宽到 12000
    # 设为 -1 表示不限制
    RAG_MAX_CONTEXT_TOKENS: int = 6000

    # ========== 多轮对话查询改写 (Conversation Query Rewriting) ==========
    # 当 rewrite_mode=conversation 时生效，三层判断：
    # 1) 规则判断（指代词/上下文依赖词/省略主语）→ 2) 历史判断（chat_history 存在性）
    # → 3) LLM 改写（指代消解，生成独立完整查询）
    # 最多使用多少轮对话历史（3 表示最近 3 轮 user+assistant 共 6 条消息）
    CONVERSATION_REWRITE_MAX_HISTORY_TURNS: int = 3
    # LLM 改写调用超时（秒），超时降级到原始 query
    CONVERSATION_REWRITE_TIMEOUT: float = 10.0
    # LLM 改写温度（低温度保证稳定性）
    CONVERSATION_REWRITE_TEMPERATURE: float = 0.1

    # ========== 熔断器 + 限流器（保护 LLM/VLM API 调用）==========
    # 熔断阈值：连续失败多少次后熔断（CLOSED → OPEN）
    CIRCUIT_FAILURE_THRESHOLD: int = 5
    # 熔断冷却秒数（OPEN → HALF_OPEN 探测）
    CIRCUIT_RESET_TIMEOUT: float = 30.0
    # 限流速率：每秒最多多少个请求（令牌生成速率）
    RATE_LIMIT_RPS: float = 2.0
    # 限流桶容量：允许的瞬时突发量
    RATE_LIMIT_CAPACITY: int = 5
    # API 请求限流：每用户每分钟最大请求数（聊天接口）
    RATE_LIMIT_RPM: int = 30

    # ========== 多模态 RAG ==========
    # 总开关：是否启用多模态（图片 caption + 表格 summary）
    # 关闭时上传管道跳过 VLM 调用，图片元素仅做 OCR
    MULTIMODAL_ENABLED: bool = False

    # VLM 提供商：openai | qwen | zhipu
    # 留空时若 AI_MODEL 是 vision 模型则自动推断
    VLM_PROVIDER: Optional[str] = None
    # VLM 模型名（None 时使用 provider 默认值）
    # 推荐：gpt-4o-mini（便宜）/ qwen-vl-max（中文最好）/ glm-4v-flash（智谱免费）
    VLM_MODEL: Optional[str] = None
    # VLM API Key（None 时降级到 AI_API_KEY）
    VLM_API_KEY: Optional[str] = None
    # VLM API base URL（None 时降级到 AI_BASE_URL 或 provider 默认）
    VLM_BASE_URL: Optional[str] = None

    # 是否在多模态上传时同时跑 OCR（图片中的文字提取）
    # 推荐 True：截图/表格图片中的文字对检索很有价值
    MULTIMODAL_USE_OCR: bool = True

    # 图片 VLM 描述失败时是否阻断上传（False 时降级为 OCR-only）
    MULTIMODAL_VLM_REQUIRED: bool = False

    # 是否对表格元素也生成 LLM summary（与图片 caption 类似的策略）
    # 启用后：父块保留原表格 Markdown/HTML，子块用 summary 提升召回
    MULTIMODAL_TABLE_SUMMARY_ENABLED: bool = True

    # ========== 多模态向量（CLIP 图文对齐）==========
    # 总开关：是否启用 CLIP 图像向量检索（与 VLM caption 文本检索并行，RRF 融合）
    # 启用条件：本地已下载 CLIP 模型；未下载时自动降级为纯文本检索
    MULTIMODAL_VECTOR_ENABLED: bool = False
    # CLIP 模型名（推荐 OFFA-Sys/chinese-clip-vit-base-patch16 中文场景）
    # 英文场景可用 openai/clip-vit-base-patch32
    CLIP_MODEL_NAME: str = "OFA-Sys/chinese-clip-vit-base-patch16"
    # CLIP 检索结果在 RRF 融合中的权重（0-1，越高越偏向图像召回）
    CLIP_FUSION_WEIGHT: float = 0.3

    # 外部搜索配置
    TAVILY_API_KEY: Optional[str] = None
    JINA_API_KEY: Optional[str] = None

    # 飞书消息推送配置（群机器人 webhook，旧方式）
    FEISHU_WEBHOOK_URL: Optional[str] = None
    FEISHU_WEBHOOK_SECRET: Optional[str] = None

    # 飞书 CLI 配置（新方式）
    FEISHU_CLI_PATH: str = "lark"  # 飞书 CLI 可执行文件路径
    FEISHU_USER_OPEN_ID: Optional[str] = None  # 用户 open_id（ou_xxx）

    # 意图分类器配置
    INTENT_CLASSIFIER_THRESHOLD: float = 0.7
    INTENT_USE_LLM: bool = True

    # ReAct 配置
    USE_REACT: bool = False
    REACT_MAX_STEPS: int = 3
    REACT_SKILLS: str = "concept_lesson,practice_session"

    # Redis配置（可选，用于缓存）
    REDIS_URL: Optional[str] = None

    # CORS配置（逗号分隔的字符串）
    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:5173"

    @field_validator("ENV", mode="before")
    @classmethod
    def normalize_env(cls, v: str) -> str:
        """规范化 ENV 字段"""
        v = (v or "development").strip().lower()
        if v not in ("development", "production", "test"):
            raise ValueError(f"ENV 必须是 development/production/test，当前值: {v}")
        return v

    @field_validator("VECTOR_STORE_BACKEND", mode="after")
    @classmethod
    def validate_vector_store(cls, v: str, info) -> str:
        """生产环境强制使用 qdrant（项目硬约束）；开发环境允许 chroma/qdrant"""
        env = info.data.get("ENV", "development")
        v = (v or "").strip().lower()
        if v not in ("chroma", "qdrant"):
            raise ValueError(f"VECTOR_STORE_BACKEND 必须是 chroma 或 qdrant，当前值: {v}")
        if env == "production" and v != "qdrant":
            raise ValueError("生产环境必须使用 qdrant 作为向量存储后端（项目硬约束）")
        return v

    @field_validator("SECRET_KEY", mode="after")
    @classmethod
    def validate_secret_key(cls, v: str, info) -> str:
        """SECRET_KEY 校验：
        - production: 必须显式设置且长度 >= 32 字节，禁止使用占位符
        - development: 为空或占位符时自动生成随机密钥（每次启动不同，仅本地用）
        """
        env = info.data.get("ENV", "development")
        placeholder = "your-secret-key-change-in-production"

        if env == "production":
            if not v or v == placeholder:
                raise ValueError(
                    "生产环境必须通过 SECRET_KEY 环境变量显式设置一个固定密钥，"
                    "不能为空或使用占位符（否则每次重启会导致所有 JWT 失效）"
                )
            if len(v) < 32:
                raise ValueError("生产环境 SECRET_KEY 至少 32 字节，建议用 `python -c \"import secrets; print(secrets.token_urlsafe(48))\"` 生成")
            return v

        # development: 自动生成
        if not v or v == placeholder:
            return secrets.token_urlsafe(48)
        return v

    @property
    def is_production(self) -> bool:
        """是否为生产环境"""
        return self.ENV == "production"

    @property
    def is_development(self) -> bool:
        """是否为开发环境（含测试环境，二者安全策略一致）"""
        return self.ENV in ("development", "test")

    @property
    def cors_origins_list(self) -> List[str]:
        """将逗号分隔的字符串转换为列表"""
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
    )


@lru_cache()
def get_settings() -> Settings:
    """获取配置（带缓存）"""
    return Settings()


# 全局配置实例
settings = get_settings()

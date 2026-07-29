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
    DEBUG: bool = False
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # 数据库配置
    MONGODB_URL: str = "mongodb://localhost:27017"
    MONGODB_DB_NAME: str = "education_agent"

    # JWT配置
    SECRET_KEY: str = ""  # 将在运行时自动生成
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
    # 检索 top_k：召回与 LLM 上下文成本的平衡（5 推荐，8 可获更高 recall）
    RAG_TOP_K: int = 5
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

    @field_validator("SECRET_KEY", mode="before")
    @classmethod
    def generate_secret_key(cls, v: str) -> str:
        """如果 SECRET_KEY 为空，则自动生成随机密钥"""
        if not v or v == "your-secret-key-change-in-production":
            # 自动生成随机密钥（每次启动都不同，适合开发环境）
            # 生产环境必须通过环境变量设置固定密钥
            # HS256 要求密钥至少 32 字节，token_urlsafe(48) 解码后约 36 字节
            return secrets.token_urlsafe(48)
        return v

    @property
    def cors_origins_list(self) -> List[str]:
        """将逗号分隔的字符串转换为列表"""
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",")]

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

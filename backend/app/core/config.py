"""
应用配置模块
支持多环境配置和AI模型切换
"""

import secrets
from functools import lru_cache
from typing import List, Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # 管理后台：超管账号初始化
    # 全新部署时，首次注册该用户名会自动赋予 admin 角色（仅注册时生效，不回填已存在用户）
    # 留空则不启用；已存在用户的提权请用 backend/promote_admin.py 脚本
    SUPER_ADMIN_USERNAME: str = ""

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

    # ========== MCP (Model Context Protocol) 监控工具集成 ==========
    # 启用后 Agent 会在 lifespan 阶段加载 MCP server，支撑运维诊断"查监控"环节
    MCP_ENABLED: bool = False
    # MCP server 类型（决定加载哪个子进程）：
    #   ops_monitoring:    模拟 Prometheus+Loki 的 mock 数据（payment-service 故障场景）
    #   system_monitoring: 基于 psutil 查本机真实 CPU/内存/磁盘/进程指标（接你自己的电脑）
    MCP_SERVER_TYPE: str = "ops_monitoring"
    # MCP 工具加载超时（秒）：stdio 启动子进程 + get_tools() 的总超时
    # 超时后降级为纯知识库模式，不阻塞应用启动（生产化保护）
    MCP_LOAD_TIMEOUT: float = 30.0

    # Loki 日志查询地址（prometheus 模式下双 server 之一）
    # 与 Prometheus 配合：prometheus 管指标，loki 管日志
    LOKI_URL: str = "http://loki:3100"

    # Alertmanager 告警查询地址（prometheus 模式下三 server 之一）
    # 与 Prometheus/Loki 配合：prometheus 管指标，loki 管日志，alertmanager 管告警
    ALERTMANAGER_URL: str = "http://alertmanager:9093"

    # Prometheus 时序指标查询地址（前端监控看板 + Agent 诊断共用）
    # 容器内通过服务名访问，宿主机通过 localhost:9090
    PROMETHEUS_URL: str = "http://prometheus:9090"

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
    # 认证端点限流：每 IP 每分钟最大请求数（登录/注册，防暴力破解）
    AUTH_RATE_LIMIT_PER_MIN: int = 20

    # ========== LLM 请求超时（生产化保护）==========
    # LLM 单次请求超时（秒）：覆盖 Agent 主调用、反思、查询重写等所有 ChatOpenAI 调用
    # 超时后抛 TimeoutError，由上层 try/except 捕获并降级
    # DeepSeek/Qwen 常规对话 60s 足够；复杂工具调用 + 长上下文可适当放宽
    LLM_REQUEST_TIMEOUT: float = 60.0

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

    # 飞书自建应用（告警通知 Bridge：Alertmanager webhook → 飞书卡片消息）
    FEISHU_APP_ID: Optional[str] = None  # 自建应用 app_id（cli_xxx）
    FEISHU_APP_SECRET: Optional[str] = None  # 自建应用 app_secret
    FEISHU_ALERT_OPEN_ID: Optional[str] = None  # 接收告警的用户 open_id
    # 告警 webhook 共享密钥：Alertmanager 通过 http_config.authorization 携带 Bearer 头。
    # 未配置时 /api/alerts/webhook 拒绝处理（503），防伪造告警
    ALERT_WEBHOOK_SECRET: Optional[str] = None
    # 告警自动诊断：webhook 收到 firing 告警后自动调用 Agent 诊断并推送飞书
    ALERT_AUTO_DIAGNOSIS_ENABLED: bool = True
    # 同一事故重诊最小间隔（秒），兼作告警冷却窗口
    ALERT_DIAG_COOLDOWN_SECONDS: int = 900
    # ==== 事故生命周期（初诊 → 重诊 → 恢复摘要）====
    # 每个 incident 重诊次数上限（含升级/心跳触发），成本硬护栏
    DIAG_MAX_REDIAG_PER_INCIDENT: int = 3
    # 触发诊断的最低告警级别（低于此级别只推告警卡片、不进 Agent）
    ALERT_MIN_SEVERITY: str = "warning"
    # 抖动复用窗口：fingerprint resolved 后 N 秒内复燃 → 重新打开原事故（不重新初诊）
    INCIDENT_FLAPPING_WINDOW: int = 1800
    # 恢复摘要安静期：全部 fingerprint resolved 后等 N 秒确认不复燃再闭案生成摘要
    INCIDENT_RESOLVE_QUIET_PERIOD: int = 600
    # 新告警归入活跃事故的服务关联窗口（秒）
    INCIDENT_SERVICE_JOIN_WINDOW: int = 1800
    # 事故卡死保护：active 事故 last_seen_at 超过 N 小时无新告警 → 自动闭案（B6）
    # 成员告警在源头被删 / AM 重启丢状态 / 手动 webhook 测试时避免事故永远停留 active
    INCIDENT_STALE_AUTO_CLOSE_HOURS: int = 6
    # 告警 → 服务名静态映射（JSON 字符串，如 '{"node-exporter": "host-infra"}'），
    # 优先级高于 labels 自动提取；用于告警规则无 service 标签的环境
    ALERT_SERVICE_MAP: Optional[str] = None
    # B3 服务重要性矩阵：影响度 × 紧急度 → 优先级 P1-P4（ITIL 共识）
    # JSON 字符串，如 '{"payment-sim": "critical", "user-service": "normal"}'
    # critical=核心支付/认证、normal=辅助业务、low=非业务（默认 normal）
    # 消费点：诊断卡片显示影响等级、escalation 判定用影响等级而非原始 severity
    SERVICE_CRITICALITY: Optional[str] = None
    # ==== 执行层可靠性 ====
    # 诊断任务表：每任务最大尝试次数（超过标 dead）
    DIAG_TASK_MAX_ATTEMPTS: int = 3
    # 失败重试退避：回队后 N 秒内不再被认领（防 drain 循环瞬时烧光重试次数）
    DIAG_TASK_RETRY_BACKOFF_SECONDS: int = 60
    # running 任务超过 N 秒未完成视为处理实例已死，回队列（僵尸回收窗口）
    DIAG_TASK_STALE_SECONDS: int = 900
    # worker 空闲轮询间隔（秒）；入队会主动唤醒，此值只影响兜底延迟
    DIAG_TASK_POLL_SECONDS: float = 5.0
    # 诊断服务的组织 ID：自动诊断检索时以此 org 过滤（公共 + 该组织文档可见），
    # 解决"自动诊断看不到团队私有运维文档"问题
    DIAGNOSIS_ORG_ID: str = ""
    # 会话 checkpoint 后端：sqlite（单机开发）/ mongodb（多副本共享，生产推荐）
    CHECKPOINT_BACKEND: str = "sqlite"

    # 意图分类器配置
    INTENT_CLASSIFIER_THRESHOLD: float = 0.7
    INTENT_USE_LLM: bool = True

    # ReAct 配置
    USE_REACT: bool = False
    REACT_MAX_STEPS: int = 3
    REACT_SKILLS: str = "concept_lesson,practice_session"

    # Redis配置（可选，用于缓存）
    REDIS_URL: Optional[str] = None

    # Celery 异步任务队列（文档导入等耗时任务）
    # USE_CELERY=True 时走 Celery worker（需单独启动 worker）；False 时降级到 BackgroundTasks 同步处理
    USE_CELERY: bool = False
    CELERY_BROKER_URL: Optional[str] = None  # 留空则复用 REDIS_URL
    CELERY_RESULT_BACKEND: Optional[str] = None  # 留空则复用 REDIS_URL

    # CORS配置（逗号分隔的字符串）
    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:5173"

    # ========== C2: 敏感信息脱敏代码块策略 ==========
    # 控制 sanitizer 对 ``` 代码块 / 行内代码 的脱敏行为：
    #   strict（默认/生产）：代码块内的高置信凭据（API key/AWS key/私钥）仍脱敏，
    #       普通文本模式（手机号/身份证/邮箱/内网IP）豁免（代码块中可能是测试数据）
    #   loose（开发调试）：代码块完全豁免，保留代码示例原样
    # 背景：原实现无条件豁免代码块，LLM 把敏感数据放进代码块即可绕过脱敏
    SANITIZER_SANITIZE_CODE: str = "strict"

    @field_validator("ENV", mode="before")
    @classmethod
    def normalize_env(cls, v: str) -> str:
        """规范化 ENV 字段"""
        v = (v or "development").strip().lower()
        if v not in ("development", "production", "test"):
            raise ValueError(f"ENV 必须是 development/production/test，当前值: {v}")
        return v

    @field_validator("SANITIZER_SANITIZE_CODE", mode="after")
    @classmethod
    def validate_sanitizer_code_mode(cls, v: str) -> str:
        """C2: 代码块脱敏策略只允许 strict/loose，统一小写"""
        v = (v or "strict").strip().lower()
        if v not in ("strict", "loose"):
            raise ValueError(f"SANITIZER_SANITIZE_CODE 必须是 strict 或 loose，当前值: {v}")
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

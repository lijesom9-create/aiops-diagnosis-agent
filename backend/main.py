"""
个人知识助手 - 主应用入口
基于 RAG + LangGraph 的智能问答系统
"""

# 必须在所有 import 之前设置，否则 sentence-transformers 仍会联网检查更新
import os
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_DATASETS_OFFLINE"] = "1"

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from loguru import logger

from app.core.config import settings
from app.core.database import db
from app.api import auth, documents, memory, langgraph


# 应用生命周期管理
@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期"""

    # 启动时
    logger.info(f"启动 {settings.APP_NAME} v{settings.APP_VERSION}")

    # 连接数据库
    await db.connect()

    # 初始化统一知识存储
    from app.knowledge.unified_store import UnifiedKnowledgeStore
    from app.api.documents import set_knowledge_store
    from app.shared_services import get_embedding_model, set_knowledge_store as set_shared_store
    from app.retrieval.reranker import CrossEncoderReranker

    embedding_model = get_embedding_model()

    # 创建 Reranker（可配置开关，加载失败时优雅降级）
    reranker = None
    if settings.RERANKER_ENABLED:
        try:
            reranker = CrossEncoderReranker(model_name=settings.RERANKER_MODEL_NAME)
            # 触发一次加载，确保模型可用
            reranker._load_model()
            logger.info(f"Reranker 初始化完成: {settings.RERANKER_MODEL_NAME}")
        except Exception as e:
            logger.warning(f"Reranker 加载失败，将禁用重排: {e}")
            reranker = None

    knowledge_store = UnifiedKnowledgeStore(
        embedding_model=embedding_model,
        reranker=reranker,
    )
    set_knowledge_store(knowledge_store)
    set_shared_store(knowledge_store)

    logger.info(f"应用启动完成, 知识库: {knowledge_store.size()} 条")

    yield

    # 关闭时
    from app.core.ai_service import ai_service
    if hasattr(ai_service, 'close'):
        await ai_service.close()
    await db.disconnect()
    logger.info("应用已关闭")


# 创建FastAPI应用
app = FastAPI(
    title="知识助手",
    version=settings.APP_VERSION,
    description="个人知识助手 - 支持 RAG 问答、文档管理、记忆系统",
    lifespan=lifespan
)

# 配置CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(auth.router)
app.include_router(documents.router)
app.include_router(memory.router)
app.include_router(langgraph.router)  # LangGraph Agent (替代 chat.router)


# 根端点
@app.get("/")
async def root():
    """根端点"""
    return {
        "name": "知识助手",
        "version": settings.APP_VERSION,
        "status": "running",
        "architecture": "RAG + Memory"
    }


# 健康检查
@app.get("/api/health")
async def health_check():
    """健康检查"""
    return {
        "status": "healthy",
        "version": settings.APP_VERSION,
        "architecture": "RAG + Memory"
    }


# 配置信息（仅开发环境可用）
@app.get("/api/config")
async def get_config():
    """获取配置信息（仅显示非敏感信息，仅DEBUG模式可用）"""
    if not settings.DEBUG:
        raise HTTPException(status_code=404, detail="Not found")
    return {
        "app_name": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "debug": settings.DEBUG,
        "ai_model": settings.AI_MODEL,
        "architecture": "RAG + LangGraph"
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=False
    )

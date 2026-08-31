"""
共享服务模块

统一管理 embedding model、memory manager、knowledge store 等全局实例，
避免各模块独立创建导致的配置不一致问题。
"""

from typing import Any, Dict, List, Optional

from loguru import logger


class RAGRetrieverAdapter:
    """
    RAG 检索器适配器

    将 UnifiedKnowledgeStore 的 hybrid_search_parent_child 接口适配为
    MemoryManager 期望的 rag_retriever 接口。

    P0-1 修复：之前调用纯向量 search()，所有 RAG 优化（BM25/RRF/parent-child/reranker）均失效。
    现在改调 hybrid_search_parent_child，让生产 RAG 链路用上完整优化。
    """

    def __init__(self, knowledge_store):
        self.knowledge_store = knowledge_store
        # 从 config 读取推荐参数
        from app.core.config import settings
        self._rewrite_mode = settings.RAG_REWRITE_MODE
        self._candidate_multiplier = settings.RAG_CANDIDATE_MULTIPLIER
        self._rrf_k = settings.RAG_RRF_K
        self._vector_weight = settings.RAG_VECTOR_WEIGHT
        self._bm25_weight = settings.RAG_BM25_WEIGHT
        self._top_k = settings.RAG_TOP_K

    def search(self, query: str, top_k: Optional[int] = None, chat_history: Optional[List[Dict]] = None) -> List[Any]:
        """
        搜索知识库（使用 hybrid_search_parent_child 完整链路）

        Args:
            top_k: 返回结果数，None 时从 settings.RAG_TOP_K 读取
            chat_history: 对话历史（用于多轮对话改写）

        Returns:
            带有 to_dict() 方法的对象列表
        """
        if top_k is None:
            top_k = self._top_k
        # 优先用 hybrid_search_parent_child（完整 RAG 链路）
        # 失败时降级到纯向量 search
        try:
            results = self.knowledge_store.hybrid_search_parent_child(
                query=query,
                top_k=top_k,
                rewrite_query=True,
                rewrite_mode=self._rewrite_mode,
                candidate_multiplier=self._candidate_multiplier,
                rrf_k=self._rrf_k,
                vector_weight=self._vector_weight,
                bm25_weight=self._bm25_weight,
                chat_history=chat_history,
            )
        except Exception as e:
            logger.warning(f"hybrid_search_parent_child 失败，降级到纯向量检索: {e}")
            results = self.knowledge_store.search(query=query, top_k=top_k)

        # 将 dict 转换为带 to_dict() 方法的对象
        class ResultItem:
            def __init__(self, data: Dict):
                self.data = data
                self.id = data.get("id", "")
                self.score = data.get("score", 0.0)
                self.title = data.get("title", "")
                self.content = data.get("content", "")
                self.source = data.get("source", "")
                self.metadata = data.get("metadata", {})

            def to_dict(self):
                return self.data

        return [ResultItem(r) for r in results]


# 全局单例
_embedding_model = None
_memory_manager = None
_knowledge_store = None


def get_embedding_model():
    """获取统一的 embedding model 实例"""
    global _embedding_model
    if _embedding_model is None:
        from app.core.config import settings
        from app.retrieval.embeddings import create_embedding_model

        _embedding_model = create_embedding_model(
            api_key=settings.EMBEDDING_API_KEY or None,
            model_name=settings.EMBEDDING_MODEL or None,
            base_url=settings.EMBEDDING_BASE_URL or None,
            use_local_embedding=True,
            local_model_name="BAAI/bge-m3",
        )
        logger.info("Embedding model 初始化完成: BAAI/bge-m3 (dense+sparse 同源)")
    return _embedding_model


def get_knowledge_store():
    """获取统一的知识库存储实例"""
    return _knowledge_store


def set_knowledge_store(store):
    """设置知识库存储实例"""
    global _knowledge_store
    _knowledge_store = store
    logger.info("Knowledge store 已设置")


def init_knowledge_store():
    """初始化统一知识库（web lifespan 和 celery worker 复用）

    创建 embedding model + reranker + UnifiedKnowledgeStore，并设置到全局单例。
    worker 进程不跑 FastAPI lifespan，调用本函数完成初始化。
    幂等：已初始化则直接返回现有实例。
    """
    global _knowledge_store
    if _knowledge_store is not None:
        return _knowledge_store

    from app.core.config import settings
    from app.knowledge.unified_store import UnifiedKnowledgeStore
    from app.retrieval.reranker import CrossEncoderReranker

    embedding_model = get_embedding_model()

    # 创建 Reranker（可配置开关，加载失败时优雅降级）
    reranker = None
    if settings.RERANKER_ENABLED:
        try:
            reranker = CrossEncoderReranker(model_name=settings.RERANKER_MODEL_NAME)
            reranker._load_model()
            logger.info(f"Reranker 初始化完成: {settings.RERANKER_MODEL_NAME}")
        except Exception as e:
            logger.warning(f"Reranker 加载失败，将禁用重排: {e}")
            reranker = None

    knowledge_store = UnifiedKnowledgeStore(
        embedding_model=embedding_model,
        reranker=reranker,
        separate_parent_child=settings.RAG_SEPARATE_PARENT_CHILD,
        vector_store_backend=settings.VECTOR_STORE_BACKEND,
        sparse_embedding_model=embedding_model,  # BGE-M3 同源 sparse（替代自研 BM25）
    )
    set_knowledge_store(knowledge_store)
    logger.info(f"Knowledge store 初始化完成: {knowledge_store.size()} 条")
    return knowledge_store


def get_memory_manager():
    """获取统一的 memory manager 实例"""
    global _memory_manager
    if _memory_manager is None:
        from app.memory import MemoryManager

        embedding_model = get_embedding_model()
        knowledge_store = get_knowledge_store()

        # 创建 RAG 检索器适配器
        rag_retriever = None
        if knowledge_store:
            rag_retriever = RAGRetrieverAdapter(knowledge_store)
            logger.info("RAG retriever 适配器已创建")

        _memory_manager = MemoryManager(
            embedding_model=embedding_model,
            vector_store=knowledge_store.vector_store if knowledge_store else None,
            rag_retriever=rag_retriever,
        )
        logger.info("MemoryManager 初始化完成")
    return _memory_manager

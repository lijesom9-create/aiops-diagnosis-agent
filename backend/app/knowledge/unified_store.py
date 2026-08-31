"""
Unified Knowledge Store - 统一知识存储

将所有知识统一存储到 ChromaDB：
- 课程知识（course）
- 教学经验（teaching）
- 用户文档（user_document）

MongoDB 只存储元数据和状态，不存储知识内容。
"""

import os
import re
from typing import List, Dict, Any, Optional, Set
from dataclasses import dataclass, field
from datetime import datetime
from loguru import logger

from ..retrieval.chroma_store import ChromaDBVectorStore
from ..retrieval.qdrant_store import QdrantVectorStore
from ..retrieval.embeddings import EmbeddingModel
from ..observability.metrics import get_metrics


# ========== 领域词典 ==========

# 技术术语词典：jieba 不认识的专业词汇，手动添加
DOMAIN_DICTIONARY = {
    # 编程语言
    "Python", "Java", "JavaScript", "TypeScript", "Go", "Rust", "C++", "SQL",
    # Python 概念
    "装饰器", "生成器", "迭代器", "列表推导式", "字典推导式",
    "上下文管理器", "元类", "协程", "异步", "闭包",
    "递归", "回调", "柯里化", "高阶函数", "匿名函数",
    "面向对象", "多态", "封装", "继承", "抽象",
    # 数据结构
    "链表", "栈", "队列", "哈希表", "二叉树", "红黑树", "堆", "图",
    # 算法
    "动态规划", "贪心算法", "分治", "回溯", "二分查找", "深度优先", "广度优先",
    "冒泡排序", "快速排序", "归并排序", "插入排序", "选择排序",
    # 框架
    "FastAPI", "Flask", "Django", "React", "Vue", "Next.js",
    "LangChain", "LangGraph", "ChromaDB", "Redis", "MongoDB",
    # AI/ML
    "Embedding", "向量数据库", "RAG", "大语言模型", "提示词工程",
    "微调", "推理", "Tokenizer", "Transformer", "注意力机制",
    # Web
    "RESTful", "GraphQL", "WebSocket", "HTTP", "HTTPS", "API",
    "中间件", "路由", "控制器", "ORM", "JWT",
}


def _init_jieba():
    """初始化 jieba 领域词典"""
    try:
        import jieba
        for word in DOMAIN_DICTIONARY:
            jieba.add_word(word, freq=10000, tag="nz")
        logger.info(f"领域词典加载完成: {len(DOMAIN_DICTIONARY)} 个术语")
    except ImportError:
        logger.debug("jieba 未安装，跳过领域词典")


# 模块加载时初始化
_init_jieba()


@dataclass
class KnowledgeItem:
    """知识条目"""
    id: str
    title: str
    content: str
    source: str  # course | teaching | user_document
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    org_id: str = ""

    def to_chroma(self) -> Dict[str, Any]:
        """转换为 ChromaDB 存储格式"""
        return {
            "id": self.id,
            "content": self.content,
            "metadata": {
                "title": self.title,
                "source": self.source,
                "created_at": self.created_at,
                "org_id": self.org_id,
                **self.metadata,
            }
        }


class UnifiedKnowledgeStore:
    """
    统一知识存储

    所有知识都存储在同一个 ChromaDB Collection 中，
    通过 metadata.source 区分类型，通过 metadata.user_id 隔离用户。
    """

    def __init__(
        self,
        embedding_model: EmbeddingModel,
        collection_name: str = "knowledge",
        persist_directory: Optional[str] = None,
        reranker=None,
        vector_store_backend: Optional[str] = None,
        separate_parent_child: bool = True,
        sparse_embedding_model=None,
    ):
        self.embedding_model = embedding_model
        self._separate_parent_child = separate_parent_child
        # 保存参数供 CLIP image_store 懒加载时复用
        self._collection_name = collection_name
        self._persist_directory = persist_directory
        self._vector_store_backend = vector_store_backend
        # sparse embedding model（BGE-M3 lexical_weights，传给 child store 启用 sparse 检索）
        self._sparse_model = sparse_embedding_model

        # 子块向量库（向后兼容：self.vector_store 始终指向 child store）
        # child store 启用 sparse vector（sparse_embedding_model），parent store 不需要
        self.vector_store = self._create_vector_store(
            embedding_model=embedding_model,
            collection_name=collection_name,
            persist_directory=persist_directory,
            backend=vector_store_backend,
            sparse_embedding_model=sparse_embedding_model,
        )
        # 父块向量库（分离存储：独立 collection，不参与 ANN 检索，仅按 ID 取回）
        if separate_parent_child:
            self._parent_store = self._create_vector_store(
                embedding_model=embedding_model,
                collection_name=f"{collection_name}_parent",
                persist_directory=persist_directory,
                backend=vector_store_backend,
                sparse_embedding_model=None,
            )
        else:
            # 兼容模式：父子同库（旧逻辑，不推荐）
            self._parent_store = self.vector_store

        self.reranker = reranker

        # CLIP 图像向量库（懒加载，仅当 MULTIMODAL_VECTOR_ENABLED=True 且模型可用时创建）
        # 与文本向量库（self.vector_store）独立，存 CLIP 图像向量，供多模态检索
        self._clip_image_store = None
        self._clip_embedder = None
        self._clip_enabled: Optional[bool] = None  # None=未探测, True/False=已探测

        logger.info(
            f"UnifiedKnowledgeStore 初始化完成 (parent_child_separated={separate_parent_child}), "
            f"child_size={self.vector_store.size()}, parent_size={self._parent_store.size()}"
        )

        # P0-2: 查询结果缓存（LRU + TTL，避免相同 query 重复检索）
        self._query_cache: Dict[str, Dict[str, Any]] = {}  # key -> {"results": ..., "ts": ...}
        self._query_cache_ttl: int = 300  # 5 分钟
        self._query_cache_max: int = 128  # 最多缓存 128 个查询

        # 查询重写缓存（LLM 重写结果，相同 query+mode 命中）
        self._rewrite_cache: Dict[str, List[str]] = {}

    def _cache_get(self, key: str) -> Optional[List[Dict]]:
        """从缓存获取查询结果（底层用可插拔缓存层）"""
        from ..core.cache import get_cache
        cache = get_cache(self._query_cache_ttl)
        return cache.get("unified_store_query", key)

    def _cache_put(self, key: str, results: List[Dict]):
        """写入缓存结果"""
        from ..core.cache import get_cache
        cache = get_cache(self._query_cache_ttl)
        cache.set("unified_store_query", results, key)

    @staticmethod
    def _create_vector_store(
        embedding_model: EmbeddingModel,
        collection_name: str,
        persist_directory: Optional[str],
        backend: Optional[str] = None,
        sparse_embedding_model=None,
    ):
        """
        工厂方法：根据 backend 选择向量存储后端

        Args:
            backend: "chroma" | "qdrant" | None（None 时从 settings 读取）
            sparse_embedding_model: sparse embedding 模型（仅 Qdrant 支持，ChromaDB 忽略）
        """
        # 延迟导入避免循环依赖
        from ..core.config import settings

        backend = (backend or settings.VECTOR_STORE_BACKEND or "chroma").lower()

        if backend == "qdrant":
            host = settings.QDRANT_HOST
            port = settings.QDRANT_PORT
            path = persist_directory or settings.QDRANT_PERSIST_DIR
            logger.info(f"使用 Qdrant 向量存储后端 (host={host}, path={path})")
            return QdrantVectorStore(
                embedding_model=embedding_model,
                collection_name=collection_name,
                persist_directory=path,
                host=host,
                port=port,
                sparse_embedding_model=sparse_embedding_model,
            )

        # 默认 ChromaDB（不支持 sparse vector，忽略 sparse_embedding_model）
        host = settings.CHROMA_HOST
        port = settings.CHROMA_PORT
        path = persist_directory or settings.CHROMA_PERSIST_DIR
        return ChromaDBVectorStore(
            embedding_model=embedding_model,
            collection_name=collection_name,
            persist_directory=path,
            host=host,
            port=port,
        )

    # ========== CLIP 多模态向量（懒加载 + 优雅降级）==========

    def _ensure_clip(self) -> bool:
        """
        懒加载 CLIP embedder + 图像向量库

        Returns:
            True 表示 CLIP 可用；False 表示不可用（已降级，后续不再尝试）
        """
        if self._clip_enabled is not None:
            return self._clip_enabled

        # 首次探测
        try:
            from ..core.config import settings
            if not getattr(settings, "MULTIMODAL_VECTOR_ENABLED", False):
                self._clip_enabled = False
                return False
        except Exception:
            self._clip_enabled = False
            return False

        try:
            from ..retrieval.clip_embedder import get_default_clip_embedder
            self._clip_embedder = get_default_clip_embedder()
            if not self._clip_embedder.is_available():
                logger.info("CLIP 不可用（模型未下载），多模态向量检索已降级")
                self._clip_enabled = False
                return False

            # 创建独立的 CLIP 图像向量库（collection_name 加 _clip_image 后缀）
            # 注意：image_store 的 embedding_model 实际不会被用到
            # （add_with_vector/search_by_vector 都直接传预计算向量）
            self._clip_image_store = self._create_vector_store(
                embedding_model=self.embedding_model,  # 占位，实际不用
                collection_name=f"{self._collection_name}_clip_image",
                persist_directory=self._persist_directory,
                backend=self._vector_store_backend,
            )
            # 确保维度匹配（CLIP 模型维度 vs image_store 创建时的维度）
            # 注意：如果维度不匹配，需要重建 collection
            self._clip_enabled = True
            logger.info(
                f"CLIP 多模态向量已启用: dim={self._clip_embedder.dimension}, "
                f"image_store_size={self._clip_image_store.size()}"
            )
            return True
        except Exception as e:
            logger.warning(f"CLIP 初始化失败，降级为纯文本检索: {type(e).__name__}: {e}")
            self._clip_enabled = False
            return False

    def _index_image_clip_vectors(self, items: List[KnowledgeItem]) -> int:
        """
        为图片子块生成 CLIP 图像向量并写入 image_store

        在 add_batch 之后调用。遍历 items，找出 element_type=image 且有 image_path 的子块，
        加载图片，用 CLIP 生成向量，写入 _clip_image_store。

        Returns:
            成功索引的图片数量
        """
        if not self._ensure_clip():
            return 0

        # 收集需要处理的图片子块
        image_items = []
        for item in items:
            meta = item.metadata
            if meta.get("chunk_type") != "parent" and meta.get("element_type") == "image":
                image_path = meta.get("image_path")
                if image_path:
                    image_items.append((item, image_path))

        if not image_items:
            return 0

        indexed = 0
        for item, image_path in image_items:
            try:
                # 读取图片文件
                if not os.path.exists(image_path):
                    logger.debug(f"图片文件不存在，跳过 CLIP 索引: {image_path}")
                    continue
                with open(image_path, "rb") as f:
                    image_bytes = f.read()

                # 生成 CLIP 图像向量
                vector = self._clip_embedder.embed_image(image_bytes)
                if vector is None:
                    continue

                # 写入 image_store（用与子块相同的 doc_id，便于关联）
                # content 用 caption 文本（便于调试和 fallback 显示）
                meta = dict(item.metadata)
                self._clip_image_store.add_with_vector(
                    doc_id=item.id,
                    vector=vector,
                    content=item.content,
                    metadata=meta,
                )
                indexed += 1
            except Exception as e:
                logger.warning(f"CLIP 索引图片失败 {image_path}: {type(e).__name__}: {e}")

        if indexed > 0:
            logger.info(f"CLIP 索引完成: {indexed}/{len(image_items)} 张图片")
        return indexed

    def _clip_search(
        self, query: str, top_k: int, filters: Optional[Dict[str, Any]] = None
    ) -> List[tuple]:
        """
        CLIP 向量检索：query → CLIP text embedding → 查 image_store

        Args:
            filters: Qdrant 风格 filter（与向量/BM25 路径一致，含 chunk_type/source/可见性）

        Returns:
            List[(doc_id, score, metadata)]，score 已归一化到 [0,1]
        """
        if not self._ensure_clip():
            return []

        try:
            query_vector = self._clip_embedder.embed_text(query)
            if query_vector is None:
                return []
            return self._clip_image_store.search_by_vector(
                query_vector=query_vector,
                top_k=top_k,
                min_score=0.0,
                filters=filters,
            )
        except Exception as e:
            logger.warning(f"CLIP 检索失败: {type(e).__name__}: {e}")
            return []

    def _rewrite_query(
        self,
        query: str,
        rewrite_query: bool,
        rewrite_mode: str,
        chat_history: Optional[List[Dict]] = None,
    ) -> List[str]:
        """
        统一查询重写入口

        支持模式：
        - basic: 仅规则重写（去后缀、关键词组合）
        - enhanced: 规则重写 + 同义词/缩写扩展（默认）
        - llm: 仅 LLM MultiQuery 重写，失败降级到 enhanced
        - enhanced_llm: 规则重写 + LLM MultiQuery 叠加（去重），失败降级到 enhanced
        - conversation: 多轮对话改写（三层判断+LLM指代消解）→ 再走 enhanced

        缓存策略：
        - LLM 重写结果缓存（相同 query+mode 命中，省几百毫秒）
        - 规则重写不缓存（本身微秒级）
        - conversation 模式带 chat_history 不缓存（上下文不同）
        """
        if not rewrite_query:
            return [query]

        # 多轮对话改写：在 enhanced 之前做指代消解
        if rewrite_mode == "conversation" and chat_history:
            try:
                from ..retrieval.conversation_rewriter import get_default_conversation_rewriter
                rewriter = get_default_conversation_rewriter()
                if rewriter.needs_rewrite(query, chat_history):
                    query = rewriter.rewrite(query, chat_history)
                    logger.debug(f"对话改写后查询: {query}")
            except Exception as e:
                logger.warning(f"对话改写失败，使用原始查询: {e}")
            # 改写后继续走 enhanced
            return QueryRewriter.enhanced_rewrite(query)

        if rewrite_mode in ("llm", "enhanced_llm"):
            # LLM 重写结果缓存（chat_history 为 None 时才缓存）
            cache_key = f"rewrite:{rewrite_mode}:{hash(query)}"
            cached = self._rewrite_cache.get(cache_key)
            if cached is not None:
                logger.debug(f"查询重写缓存命中: {query[:30]}")
                return cached

            try:
                from ..retrieval.llm_query_rewriter import get_default_llm_rewriter
                rewriter = get_default_llm_rewriter()
                # 注入 embedding_model（用于变体语义过滤）
                if self.embedding_model is not None:
                    rewriter.set_embedding_model(self.embedding_model)
                original_mode = rewriter.mode
                rewriter.mode = rewrite_mode
                try:
                    result = rewriter.rewrite(query)
                finally:
                    rewriter.mode = original_mode
                # 缓存（限制大小，避免内存膨胀）
                if len(self._rewrite_cache) < 500:
                    self._rewrite_cache[cache_key] = result
                return result
            except Exception as e:
                logger.warning(f"LLM MultiQuery 重写失败，降级到 enhanced: {type(e).__name__}: {e}")
                return QueryRewriter.enhanced_rewrite(query)

        if rewrite_mode == "enhanced":
            return QueryRewriter.enhanced_rewrite(query)
        return QueryRewriter.rewrite(query)

    # ========== 添加知识 ==========

    def add(self, item: KnowledgeItem) -> None:
        """添加单条知识（按 chunk_type 分流到 parent/child store）

        sparse vector 由 QdrantVectorStore.add 内部自动处理，无需额外操作。
        """
        chroma_data = item.to_chroma()
        meta = chroma_data["metadata"]
        target_store = self._parent_store if meta.get("chunk_type") == "parent" else self.vector_store
        target_store.add(
            doc_id=item.id,
            content=item.content,
            metadata=meta,
        )
        logger.debug(f"添加知识: {item.id} ({item.source}, chunk_type={meta.get('chunk_type')})")

    def add_batch(self, items: List[KnowledgeItem]) -> None:
        """批量添加知识（按 chunk_type 分流到 parent/child store）"""
        if not items:
            return

        doc_ids = [item.id for item in items]
        contents = [item.content for item in items]
        metadatas = [item.to_chroma()["metadata"] for item in items]

        # 父子分流
        if self._separate_parent_child:
            parent_idx, child_idx = [], []
            for i, meta in enumerate(metadatas):
                if meta.get("chunk_type") == "parent":
                    parent_idx.append(i)
                else:
                    child_idx.append(i)
            if child_idx:
                self.vector_store.add_batch(
                    [doc_ids[i] for i in child_idx],
                    [contents[i] for i in child_idx],
                    [metadatas[i] for i in child_idx],
                )
            if parent_idx:
                self._parent_store.add_batch(
                    [doc_ids[i] for i in parent_idx],
                    [contents[i] for i in parent_idx],
                    [metadatas[i] for i in parent_idx],
                )
        else:
            self.vector_store.add_batch(doc_ids, contents, metadatas)

        # sparse vector 由 QdrantVectorStore.add_batch 内部自动处理，无需额外操作

        # CLIP 多模态向量索引（仅对图片子块，CLIP 不可用时自动跳过）
        try:
            self._index_image_clip_vectors(items)
        except Exception as e:
            logger.debug(f"CLIP 索引跳过（不影响主流程）: {type(e).__name__}: {e}")

        logger.info(f"批量添加知识: {len(items)} 条 (parent={sum(1 for m in metadatas if m.get('chunk_type')=='parent')}, child={sum(1 for m in metadatas if m.get('chunk_type')!='parent')})")

    def add_batch_with_dedup(
        self,
        items: List[KnowledgeItem],
        batch_size: int = 100,
        use_content_hash: bool = True,
    ) -> Dict[str, int]:
        """
        带去重的批量添加知识

        Args:
            items: 知识条目列表
            batch_size: 批量大小
            use_content_hash: 是否使用内容哈希去重

        Returns:
            Dict: {"added": 10, "skipped": 2, "failed": 1}
        """
        if not items:
            return {"added": 0, "skipped": 0, "failed": 0, "updated": 0}

        doc_ids = [item.id for item in items]
        contents = [item.content for item in items]
        metadatas = [item.to_chroma()["metadata"] for item in items]

        stats = self.vector_store.add_with_dedup(
            doc_ids=doc_ids,
            contents=contents,
            metadatas=metadatas,
            batch_size=batch_size,
            use_content_hash=use_content_hash,
        )

        logger.info(f"带去重批量添加完成: {stats}")
        return stats

    # ========== 搜索知识 ==========

    def search(
        self,
        query: str,
        top_k: int = 5,
        min_score: float = 0.5,  # 提高阈值，过滤不相关结果
        source: Optional[str] = None,
        user_id: Optional[str] = None,
        topic_id: Optional[str] = None,
        org_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        搜索知识

        Args:
            query: 搜索查询
            top_k: 返回数量
            min_score: 最小相似度（0-1），越高越严格
            source: 来源过滤 (course/teaching/user_document)
            user_id: 用户过滤（只搜该用户的文档 + 公共知识）
            topic_id: 主题过滤

        Returns:
            List[Dict]: 搜索结果列表
        """
        # 构建过滤条件
        filters = {}
        if source:
            filters["source"] = source

        # 搜索
        results = self.vector_store.search(
            query=query,
            top_k=top_k,
            min_score=min_score,
            filters=filters if filters else None,
        )

        # 后处理：组织隔离 + 用户隔离（与 _build_visibility_filter 语义一致：
        # shared_to_diagnosis="true" 的文档旁路隔离，对诊断服务可见）
        output = []
        for doc_id, score, metadata in results:
            shared_to_diagnosis = metadata.get("shared_to_diagnosis") == "true"

            # 组织隔离：只返回同一组织的数据
            if org_id:
                doc_org_id = metadata.get("org_id")
                if doc_org_id and doc_org_id != org_id and not shared_to_diagnosis:
                    continue  # 跳过其他组织的数据

            # 用户隔离：如果指定了 user_id，只返回公共知识或该用户的私有知识
            if user_id:
                doc_user_id = metadata.get("user_id")
                if doc_user_id and doc_user_id != user_id and not shared_to_diagnosis:
                    continue  # 跳过其他用户的私有知识

            output.append({
                "id": doc_id,
                "score": score,
                "title": metadata.get("title", ""),
                "content": metadata.get("content", ""),
                "source": metadata.get("source", ""),
                "metadata": metadata,
            })

        # Rerank（如果有 reranker）
        if self.reranker and output:
            try:
                from ..retrieval.base import RetrievalResult
                retrieval_results = [
                    RetrievalResult(
                        doc_id=item["id"],
                        content=item["content"],
                        score=item["score"],
                        metadata=item["metadata"],
                        source=item["source"],
                    )
                    for item in output
                ]
                reranked = self.reranker.rerank(query, retrieval_results, limit=top_k)
                output = [
                    {
                        "id": r.doc_id,
                        "score": r.score,
                        "title": r.metadata.get("title", ""),
                        "content": r.content,
                        "source": r.source,
                        "metadata": r.metadata,
                    }
                    for r in reranked
                ]
            except Exception as e:
                logger.warning(f"Rerank 失败，使用原始结果: {e}")

        return output

    def search_by_type(
        self,
        query: str,
        source: str,
        top_k: int = 5,
        user_id: Optional[str] = None,
        org_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """按类型搜索"""
        return self.search(query=query, top_k=top_k, source=source, user_id=user_id, org_id=org_id)

    # ========== Metadata Filter 辅助方法（运维场景：按 service/doc_type 等精准过滤）==========

    @staticmethod
    def _merge_filters(
        base: Dict[str, Any], extra: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """合并两个 Qdrant 风格 filter（支持 $and 嵌套）

        用于把 metadata_filter（如 {"service":"payment-service"}）合并到基础 filter
        （如 {"chunk_type":"child"}），生成 {"$and":[...]} 传给 Qdrant。
        """
        if not extra:
            return base or {}
        if not base:
            return extra
        conditions: List[Dict[str, Any]] = []
        if "$and" in base:
            conditions.extend(base["$and"])
        else:
            conditions.append(base)
        if "$and" in extra:
            conditions.extend(extra["$and"])
        else:
            conditions.append(extra)
        return {"$and": conditions}

    @staticmethod
    def _match_metadata_filter(
        metadata: Dict[str, Any], meta_filter: Optional[Dict[str, Any]]
    ) -> bool:
        """检查 metadata 是否满足 meta_filter（Python 层过滤，给 BM25 内存索引用）

        BM25 是内存索引不支持原生 filter，检索后在 Python 层按 meta_filter 过滤。
        支持：
        - {"key": "value"} 精确匹配
        - {"$and": [...]} 全部满足
        - {"$or": [...]} 任一满足
        - {"$or_empty": {"key": k, "value": v}} 字段为空 / 等于 v（可见性过滤，字段一定存在）
        - {"$or_missing": {"key": k, "value": v}} / {"$or_null": ...} 字段不存在 / 为空 / 等于 v（可见性过滤）
        """
        if not meta_filter:
            return True
        for k, v in meta_filter.items():
            if k == "$and":
                for sub in v:
                    if not UnifiedKnowledgeStore._match_metadata_filter(metadata, sub):
                        return False
            elif k == "$or":
                if not any(UnifiedKnowledgeStore._match_metadata_filter(metadata, sub) for sub in v):
                    return False
            elif k in ("$or_empty", "$or_missing", "$or_null"):
                # Python 层三者语义一致：字段不存在(None) / 空串 / 等于目标值 → 可见
                key = v["key"]
                val = v["value"]
                actual = metadata.get(key)
                if actual not in (None, "", val):
                    return False
            else:
                if metadata.get(k) != v:
                    return False
        return True

    def _build_visibility_filter(
        self,
        source: Optional[str] = None,
        metadata_filter: Optional[Dict[str, Any]] = None,
        org_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """构造可见性 + 业务过滤条件（三路检索共用，保证过滤行为一致）

        合并：
        - source: 来源过滤
        - metadata_filter: 业务元数据过滤（如 service/doc_type）
        - 可见性语义：shared OR (org 条件 AND user 条件)
          shared_to_diagnosis="true" 的文档对诊断服务可见（显式共享，旁路隔离）；
          未共享的文档保持原有 AND 语义——不能把 org/user 条件彼此 OR
          （否则"别人组织下的他人私有文档"会漏出来）

        存储约定差异：
        - org_id: KnowledgeItem.to_chroma 强制写入（公共文档 org_id=""），故用 $or_empty
        - user_id: uploader._store_chunks 的 cleaned 会移除空值（公共文档无 user_id 字段），故用 $or_missing
        - shared_to_diagnosis: 字符串 "true"/"false" 显式共享标记（缺失 = 未共享）

        Returns:
            filter dict，可能为 {}（无条件）。供 Qdrant pre-filter 和 BM25 Python post-filter 共用。
        """
        f: Dict[str, Any] = {}
        if source:
            f = self._merge_filters(f, {"source": source})
        if metadata_filter:
            f = self._merge_filters(f, metadata_filter)
        visibility_conds: List[Dict[str, Any]] = []
        if org_id:
            visibility_conds.append({"$or_empty": {"key": "org_id", "value": org_id}})
        if user_id:
            visibility_conds.append({"$or_missing": {"key": "user_id", "value": user_id}})
        if visibility_conds:
            f = self._merge_filters(f, {"$or": [
                {"shared_to_diagnosis": "true"},
                {"$and": visibility_conds},
            ]})
        return f

    def _build_child_filter(
        self,
        source: Optional[str] = None,
        metadata_filter: Optional[Dict[str, Any]] = None,
        org_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """构造子块检索 filter = chunk_type=child + 可见性过滤"""
        visibility = self._build_visibility_filter(source, metadata_filter, org_id, user_id)
        return self._merge_filters({"chunk_type": "child"}, visibility)

    # ========== 知识新鲜度衰减 ==========

    # 已过期文档的分数衰减系数（软降权不是硬过滤：过期文档仍可召回，只是排在有效文档后）
    FRESHNESS_EXPIRED_FACTOR = 0.5

    @staticmethod
    def _is_expired(metadata: Dict[str, Any], now: Optional[Any] = None) -> bool:
        """判断文档是否已过 valid_until（缺失/格式非法 → 视为长期有效）"""
        if not metadata:
            return False
        valid_until = metadata.get("valid_until")
        if not valid_until:
            return False
        if isinstance(valid_until, (int, float)):
            import datetime as _dt
            return now is not None and _dt.datetime.now().timestamp() > float(valid_until)
        try:
            from datetime import datetime as _dt
            parsed = _dt.fromisoformat(str(valid_until).replace("Z", "").replace("/", "-"))
            ref = now or _dt.now()
            if isinstance(ref, str):
                ref = _dt.fromisoformat(ref)
            return parsed < ref
        except Exception:
            return False

    @classmethod
    def _apply_freshness_decay(cls, results: List[Dict[str, Any]],
                               now: Optional[Any] = None) -> List[Dict[str, Any]]:
        """对已过 valid_until 的文档做分数软降权，并在 metadata 打 _expired 标记

        过期知识仍可召回（覆盖优先），但排名让位给有效文档；
        _expired 标记供引用卡片展示"已于 X 过期，结论仅供参考"。
        """
        for r in results or []:
            meta = r.get("metadata") or {}
            if cls._is_expired(meta, now):
                r["score"] = r.get("score", 0) * cls.FRESHNESS_EXPIRED_FACTOR
                r["metadata"] = {**meta, "_expired": True}
        return results

    # ========== 混合检索 ==========

    def hybrid_search(
        self,
        query: str,
        top_k: int = 5,
        min_score: float = 0.0,
        source: Optional[str] = None,
        user_id: Optional[str] = None,
        org_id: Optional[str] = None,
        rewrite_query: bool = True,
        rewrite_mode: str = "enhanced",
        rrf_k: int = 60,
        candidate_multiplier: int = 3,
    ) -> List[Dict[str, Any]]:
        """
        混合检索：Sparse（关键词）+ Vector（语义）+ RRF 融合

        Args:
            query: 搜索查询
            top_k: 返回数量
            min_score: 最小相似度
            source: 来源过滤
            user_id: 用户过滤
            org_id: 组织过滤
            rewrite_query: 是否重写查询
            rewrite_mode: 重写模式，"basic" / "enhanced" / "llm" / "enhanced_llm"，默认 enhanced
            rrf_k: RRF 融合参数 k，默认 60
            candidate_multiplier: vector/sparse 候选数量相对于 top_k 的倍数，默认 3

        Returns:
            List[Dict]: 搜索结果
        """
        import time
        _metrics = get_metrics()
        _start = time.perf_counter()

        # 查询结果缓存（与 hybrid_search_parent_child 一致的 LRU+TTL 策略）
        cache_key = f"hs:{query}:{top_k}:{source or ''}:{user_id or ''}:{org_id or ''}:{rewrite_mode}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            _metrics.increment("hybrid_search_cache_hits")
            return cached

        try:
            result = self._hybrid_search_inner(
                query, top_k, min_score, source, user_id, org_id,
                rewrite_query, rewrite_mode, rrf_k, candidate_multiplier,
            )
            self._cache_put(cache_key, result)
            return result
        finally:
            _latency_ms = (time.perf_counter() - _start) * 1000
            _metrics.increment("hybrid_search_total")
            _metrics.observe("hybrid_search_duration_ms", _latency_ms)

    def _hybrid_search_inner(
        self,
        query: str,
        top_k: int = 5,
        min_score: float = 0.0,
        source: Optional[str] = None,
        user_id: Optional[str] = None,
        org_id: Optional[str] = None,
        rewrite_query: bool = True,
        rewrite_mode: str = "enhanced",
        rrf_k: int = 60,
        candidate_multiplier: int = 3,
    ) -> List[Dict[str, Any]]:
        """hybrid_search 的内部实现"""
        # 0. 查询重写
        queries = self._rewrite_query(query, rewrite_query, rewrite_mode)

        # 1-2. 向量检索 + sparse 检索（并行执行，省 ~30ms）
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            # P3 修复：向量路使用 queries[0]（conversation 模式下即指代消解后的 query）
            vector_query = queries[0] if queries else query
            vector_future = pool.submit(
                self._vector_search,
                vector_query, top_k * candidate_multiplier, source, user_id, org_id
            )
            sparse_future = pool.submit(
                self._sparse_search,
                queries, top_k * candidate_multiplier, source, user_id, org_id
            )
            vector_results = vector_future.result()
            sparse_results = sparse_future.result()

        # 3. RRF 融合
        if sparse_results and vector_results:
            fused = self._rrf_fuse(sparse_results, vector_results, k=rrf_k)
        elif vector_results:
            fused = vector_results
        else:
            fused = sparse_results

        # 4. Rerank（P1-3: 与 parent_child 一致的候选数限制，避免 rerank 全量候选）
        if self.reranker and fused:
            try:
                from ..retrieval.base import RetrievalResult
                # 动态 rerank：基于分数分布决定送入 reranker 的候选数
                rerank_input = self._select_rerank_candidates(fused, top_k)
                # P0-1 优化：限制 rerank 候选上限，减少 ONNX 推理时间
                # 上限与 top_k 关联：保证返回数量 ≥ top_k，同时限制总候选数
                # 注：硬上限需 > top_k*2（_select_rerank_candidates 清晰查询分支的返回数），
                # 否则动态选择被覆盖。max(top_k*3, 18) 保证清晰查询的 2*top_k 候选不被截断，
                # 同时为模糊查询的全量候选提供安全兜底
                MAX_RERANK_CANDIDATES = max(top_k * 3, 18)
                if len(rerank_input) > MAX_RERANK_CANDIDATES:
                    rerank_input = rerank_input[:MAX_RERANK_CANDIDATES]
                retrieval_results = [
                    RetrievalResult(
                        doc_id=item["id"],
                        content=item["content"],
                        score=item["score"],
                        metadata=item["metadata"],
                        source=item.get("source", ""),
                    )
                    for item in rerank_input
                ]
                rerank_limit = min(len(rerank_input), max(top_k * 2, top_k + 5))
                reranked = self.reranker.rerank(query, retrieval_results, limit=rerank_limit)
                fused = [
                    {
                        "id": r.doc_id,
                        "score": r.score,
                        "title": r.metadata.get("title", ""),
                        "content": r.content,
                        "source": r.source,
                        "metadata": r.metadata,
                    }
                    for r in reranked
                ]
            except Exception as e:
                logger.warning(f"Rerank 失败，使用原始结果: {e}")

        # 5. 过滤低分 + 截断
        result = [r for r in fused if r.get("score", 0) >= min_score][:top_k]
        return result

    def hybrid_search_parent_child(
        self,
        query: str,
        top_k: int = 5,
        min_score: float = 0.0,
        source: Optional[str] = None,
        user_id: Optional[str] = None,
        org_id: Optional[str] = None,
        rewrite_query: bool = True,
        rewrite_mode: str = "enhanced",
        child_top_k: int = 15,
        rrf_k: int = 60,
        candidate_multiplier: int = 3,
        vector_weight: float = 1.0,
        bm25_weight: float = 1.0,
        chat_history: Optional[List[Dict]] = None,
        metadata_filter: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        父子文档混合检索

        流程：
        1. 向量检索子块（chunk_type=child）
        2. Sparse 检索子块
        3. RRF 融合子块结果
        4. 按 parent_id 去重
        5. 批量取回父块
        6. 返回父块作为 LLM 生成上下文

        Args:
            query: 搜索查询
            top_k: 返回父块数量
            min_score: 最小 RRF 分数
            source: 来源过滤
            user_id: 用户过滤
            org_id: 组织过滤
            rewrite_query: 是否重写查询
            child_top_k: 检索的子块数量（去重后得到父块）

        Returns:
            List[Dict]: 父块搜索结果
        """
        # P0-2: 查询缓存检查
        import hashlib
        import time as _time
        from ..observability.metrics import get_metrics as _get_metrics

        _metrics = _get_metrics()
        _t_start = _time.time()

        # conversation 模式下需将 chat_history 纳入 cache key，否则不同对话历史会错误命中缓存
        _history_hash = ""
        if rewrite_mode == "conversation" and chat_history:
            _history_str = "|".join(
                f"{t.get('role','')}:{t.get('content','')[:80]}"
                for t in chat_history[-6:]  # 只取最近 6 条避免 hash 过长
            )
            _history_hash = hashlib.md5(_history_str.encode()).hexdigest()[:8]

        _meta_filter_str = str(sorted(metadata_filter.items())) if metadata_filter else ""
        cache_key_str = f"{query}|{top_k}|{rewrite_mode}|{candidate_multiplier}|{rrf_k}|{vector_weight}|{bm25_weight}|{source}|{user_id}|{org_id}|{_history_hash}|{_meta_filter_str}"
        cache_key = hashlib.md5(cache_key_str.encode()).hexdigest()
        cached = self._cache_get(cache_key)
        if cached is not None:
            _metrics.increment("rag_query_total")
            _metrics.increment("rag_cache_hit_total")
            logger.debug(f"查询缓存命中: {query[:30]}...")
            return cached

        # 0. 查询重写（支持 basic / enhanced / llm / enhanced_llm / conversation）
        _t0 = _time.time()
        queries = self._rewrite_query(query, rewrite_query, rewrite_mode, chat_history=chat_history)
        _rewrite_ms = (_time.time() - _t0) * 1000

        # P0-3: 并行检索（向量 + sparse 同时执行）
        from concurrent.futures import ThreadPoolExecutor

        def _vector_search():
            """向量检索子块"""
            # 统一构造 filter：chunk_type=child + source + metadata_filter + 可见性(org/user)
            # org_id/user_id 走 Qdrant pre-filter，避免 top_k 内被其他组织/用户占满导致召回下降
            vector_filters = self._build_child_filter(source, metadata_filter, org_id, user_id)
            # P3 修复：向量路使用 queries[0]（conversation 模式下即指代消解后的 query），
            # 避免多轮对话中"它/这个/当时"等指代词直接 embedding 导致召回失效。
            # 普通模式下 queries[0] 与原 query 等价，行为不变。
            vector_query = queries[0] if queries else query
            vector_results = self.vector_store.search(
                query=vector_query,
                top_k=child_top_k * candidate_multiplier,
                min_score=0.0,
                filters=vector_filters,
            )
            # 调试日志：排查向量检索为空的原因
            if not vector_results:
                logger.warning(
                    f"[DEBUG] 向量检索返回0条! query={vector_query[:30]!r}, "
                    f"filters={vector_filters}, embedding_dim={getattr(self.embedding_model, 'dim', 'unknown')}"
                )
            children = []
            for doc_id, score, metadata in vector_results:
                # org_id/user_id 已由 pre-filter 保证，此处保留兜底（防御性，pre-filter 降级时仍正确）
                if org_id and metadata.get("org_id") and metadata.get("org_id") != org_id:
                    continue
                if user_id and metadata.get("user_id") and metadata.get("user_id") != user_id:
                    continue
                children.append({"id": doc_id, "score": score, "metadata": metadata})
            return children

        def _sparse_search():
            """Sparse vector 检索子块（BGE-M3 lexical_weights，替代 BM25）

            sparse_search 支持 Qdrant 原生 pre-filter，直接传入完整 filter，
            不需要 get_by_ids 回查 + Python 层过滤。
            """
            # 统一构造 filter：chunk_type=child + source + metadata_filter + 可见性(org/user)
            sparse_filter = self._build_child_filter(source, metadata_filter, org_id, user_id)
            all_scores: Dict[str, float] = {}
            all_meta: Dict[str, Dict] = {}
            for q in queries:
                results = self.vector_store.sparse_search(
                    query=q,
                    top_k=child_top_k * candidate_multiplier,
                    filters=sparse_filter,
                )
                for doc_id, score, metadata in results:
                    all_scores[doc_id] = max(all_scores.get(doc_id, 0), score)
                    all_meta[doc_id] = metadata
            sorted_docs = sorted(all_scores.items(), key=lambda x: x[1], reverse=True)[:child_top_k]
            children = []
            for doc_id, score in sorted_docs:
                meta = all_meta.get(doc_id, {})
                content = meta.get("content", "")
                children.append({"id": doc_id, "score": score, "metadata": meta, "content": content})
            return children

        # 并行执行向量检索和 sparse 检索
        _t1 = _time.time()
        with ThreadPoolExecutor(max_workers=3) as executor:
            future_v = executor.submit(_vector_search)
            future_s = executor.submit(_sparse_search)
            vector_children = future_v.result()
            sparse_children = future_s.result()
        _retrieval_ms = (_time.time() - _t1) * 1000

        # CLIP 多模态向量检索（第三路，CLIP 不可用时返回空）
        clip_children = []
        if self._ensure_clip():
            try:
                # P3 修复：与向量路一致，CLIP 检索也用消解后 query（conversation 模式）
                clip_query = queries[0] if queries else query
                # 统一 filter：chunk_type=child + source + metadata_filter + 可见性（与向量路一致）
                clip_filters = self._build_child_filter(source, metadata_filter, org_id, user_id)
                clip_results = self._clip_search(clip_query, top_k=child_top_k, filters=clip_filters)
                # 转换为 children 格式（与 vector/sparse children 一致）
                for doc_id, score, metadata in clip_results:
                    # pre-filter 已保证可见性，保留兜底（防御性）
                    if org_id and metadata.get("org_id") and metadata.get("org_id") != org_id:
                        continue
                    if user_id and metadata.get("user_id") and metadata.get("user_id") != user_id:
                        continue
                    clip_children.append({
                        "id": doc_id,
                        "score": score,
                        "metadata": metadata,
                    })
            except Exception as e:
                logger.warning(f"CLIP 检索失败（跳过）: {type(e).__name__}: {e}")

        # 3. 加权 RRF 融合子块结果（vector + sparse → text_fused）
        _t2 = _time.time()
        if sparse_children and vector_children:
            text_fused = self._rrf_fuse(
                vector_children, sparse_children, k=rrf_k,
                weight_a=vector_weight, weight_b=bm25_weight,
            )
        elif vector_children:
            text_fused = vector_children
        else:
            text_fused = sparse_children

        # CLIP 结果融合：用 CLIP_FUSION_WEIGHT 加权，与 text_fused 二次 RRF
        if clip_children and text_fused:
            try:
                from ..core.config import settings
                clip_weight = getattr(settings, "CLIP_FUSION_WEIGHT", 0.3)
            except Exception:
                clip_weight = 0.3
            # 文本权重 = 1 - clip_weight，确保 CLIP 不会主导排序
            fused_children = self._rrf_fuse(
                text_fused, clip_children, k=rrf_k,
                weight_a=1.0 - clip_weight, weight_b=clip_weight,
            )
        else:
            fused_children = text_fused if text_fused else clip_children
        _fuse_ms = (_time.time() - _t2) * 1000

        if not fused_children:
            return []

        # 4. 按 parent_id 聚合：记录每个父块的命中子块列表（用于投票加权）
        parent_id_to_children: Dict[str, List[Dict[str, Any]]] = {}
        for child in fused_children:
            parent_id = child["metadata"].get("parent_id")
            if not parent_id:
                continue
            parent_id_to_children.setdefault(parent_id, []).append(child)

        parent_ids = list(parent_id_to_children.keys())
        if not parent_ids:
            return []

        # 5. 批量从 parent_store 取回父块（父子分离存储）
        parent_records = self._parent_store.get_by_ids(parent_ids)
        parent_results = []
        for record in parent_records:
            parent_id = record["id"]
            meta = record["metadata"]
            if org_id and meta.get("org_id") and meta.get("org_id") != org_id:
                continue
            if user_id and meta.get("user_id") and meta.get("user_id") != user_id:
                continue

            children = parent_id_to_children.get(parent_id, [])
            if not children:
                continue
            # P1: 投票加权 —— 分数 = 最高子块分数 + 0.1 * (命中子块数 - 1)
            best_child = max(children, key=lambda c: c.get("score", 0))
            hit_count = len(children)
            vote_boost = 0.1 * (hit_count - 1)
            parent_score = best_child.get("score", 0) + vote_boost

            parent_results.append({
                "id": parent_id,
                "score": parent_score,
                "title": meta.get("title", ""),
                "content": record["content"],
                "source": meta.get("source", ""),
                "metadata": meta,
                "_hit_count": hit_count,  # 调试用
            })

        # 6. heading_path 相关性过滤与加权
        parent_results = self._apply_heading_path_filter(parent_results, query)

        # 7. Rerank（P0-2 修复：对父块重排，之前缺失）
        _t3 = _time.time()
        _reranker_used = False
        if self.reranker and parent_results:
            try:
                from ..retrieval.base import RetrievalResult

                # 动态 rerank：基于分数分布决定送入 reranker 的候选数
                # 思路：如果 top 分数远高于尾部（清晰查询），只 rerank 少量候选；
                #       如果分数接近（模糊查询），rerank 全部候选。
                # 收益：清晰查询省 30-50% rerank 时间，精度无损
                rerank_input = self._select_rerank_candidates(
                    parent_results, top_k
                )
                # P0-1 优化：限制 rerank 候选上限，减少 ONNX 推理时间
                # 候选已按融合分数降序排列，截断尾部低质量候选不影响精度
                # 上限与 top_k 关联：保证返回数量 ≥ top_k，同时限制总候选数
                # 注：硬上限需 > top_k*2（_select_rerank_candidates 清晰查询分支的返回数），
                # 否则动态选择被覆盖。max(top_k*3, 18) 保证清晰查询的 2*top_k 候选不被截断，
                # 同时为模糊查询的全量候选提供安全兜底
                MAX_RERANK_CANDIDATES = max(top_k * 3, 18)
                if len(rerank_input) > MAX_RERANK_CANDIDATES:
                    rerank_input = rerank_input[:MAX_RERANK_CANDIDATES]

                retrieval_results = [
                    RetrievalResult(
                        doc_id=item["id"],
                        content=item["content"],
                        score=item["score"],
                        metadata=item["metadata"],
                        source=item.get("source", ""),
                    )
                    for item in rerank_input
                ]
                # 取 top_k * 2 给 reranker，重排后截断 top_k
                rerank_limit = min(len(rerank_input), max(top_k * 2, top_k + 5))
                # P3 修复：reranker 必须用重写后的 query（queries[0]），
                # 与向量/CLIP 检索一致。conversation 模式下 queries[0] 是指代消解后的 query，
                # 若用原始 query 会让"它/这个"等指代词进入 CrossEncoder 导致打分失真
                rerank_query = queries[0] if queries else query
                reranked = self.reranker.rerank(rerank_query, retrieval_results, limit=rerank_limit)
                parent_results = [
                    {
                        "id": r.doc_id,
                        "score": r.score,
                        "title": r.metadata.get("title", ""),
                        "content": r.content,
                        "source": r.source,
                        "metadata": r.metadata,
                    }
                    for r in reranked
                ]
                _reranker_used = True
            except Exception as e:
                logger.warning(f"Parent-child rerank 失败，使用原始排序: {e}")
        _rerank_ms = (_time.time() - _t3) * 1000

        # 8. 新鲜度衰减 + 按分数排序、过滤、截断
        parent_results = self._apply_freshness_decay(parent_results)
        parent_results.sort(key=lambda x: x["score"], reverse=True)
        result = [r for r in parent_results if r.get("score", 0) >= min_score][:top_k]

        # P1-3: 结构化日志 + 指标采集
        _total_ms = (_time.time() - _t_start) * 1000
        logger.info(
            f"RAG 检索完成 | query={query[:30]!r} "
            f"| rewrite={_rewrite_ms:.1f}ms retrieval={_retrieval_ms:.1f}ms "
            f"fuse={_fuse_ms:.1f}ms rerank={_rerank_ms:.1f}ms total={_total_ms:.1f}ms "
            f"| vector_hits={len(vector_children)} sparse_hits={len(sparse_children)} "
            f"fused_children={len(fused_children)} parent_candidates={len(parent_results)} "
            f"returned={len(result)} reranker={'on' if _reranker_used else 'off'}"
        )
        # 写入指标（便于后续聚合分析）
        _metrics.increment("rag_query_total")
        _metrics.observe("rag_rewrite_duration_ms", _rewrite_ms)
        _metrics.observe("rag_retrieval_duration_ms", _retrieval_ms)
        _metrics.observe("rag_fuse_duration_ms", _fuse_ms)
        _metrics.observe("rag_rerank_duration_ms", _rerank_ms)
        _metrics.observe("rag_total_duration_ms", _total_ms)
        _metrics.observe("rag_vector_hits", len(vector_children))
        _metrics.observe("rag_sparse_hits", len(sparse_children))
        _metrics.observe("rag_parent_results", len(result))
        if _reranker_used:
            _metrics.increment("rag_reranker_used_total")
        else:
            _metrics.increment("rag_reranker_skipped_total")

        return result

    def _apply_heading_path_filter(
        self,
        parent_results: List[Dict[str, Any]],
        query: str,
        heading_path_boost: float = 0.05,
    ) -> List[Dict[str, Any]]:
        """
        过滤与查询无关的父块，并对 heading_path 命中的父块加权。

        匹配词包括：
        - 原始查询分词
        - QueryRewriter 扩展出的同义词 / 缩写

        若过滤后结果为空，则回退到原始结果，避免过度过滤导致召回下降。
        """
        if not parent_results:
            return parent_results

        query_terms = set(t.lower() for t in self._tokenize(query) if t)
        expansion_terms = set(QueryRewriter.expand_terms(query))
        all_terms = sorted(query_terms | expansion_terms)

        if not all_terms:
            return parent_results

        filtered_results = []
        for r in parent_results:
            content_lower = r["content"].lower()
            heading_path_str = " ".join(
                r["metadata"].get("heading_path", [])
            ).lower()

            content_match = any(term in content_lower for term in all_terms)
            heading_match = any(term in heading_path_str for term in all_terms)

            if content_match or heading_match:
                if heading_match:
                    r["score"] = r.get("score", 0) + heading_path_boost
                filtered_results.append(r)

        # 如果严格过滤导致无结果，回退到原始候选集
        if not filtered_results:
            logger.debug(
                f"heading_path 过滤后无结果，回退原始父块: query={query}"
            )
            return parent_results

        return filtered_results

    def _vector_search(
        self, query: str, top_k: int = 10,
        source: Optional[str] = None, user_id: Optional[str] = None,
        org_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """向量检索"""
        filters = {}
        if source:
            filters["source"] = source

        results = self.vector_store.search(
            query=query, top_k=top_k, min_score=0.0,
            filters=filters if filters else None,
        )

        output = []
        for doc_id, score, metadata in results:
            if org_id:
                doc_org_id = metadata.get("org_id")
                if doc_org_id and doc_org_id != org_id:
                    continue
            if user_id:
                doc_user_id = metadata.get("user_id")
                if doc_user_id and doc_user_id != user_id:
                    continue
            output.append({
                "id": doc_id,
                "score": score,
                "title": metadata.get("title", ""),
                "content": metadata.get("content", ""),
                "source": metadata.get("source", ""),
                "metadata": metadata,
            })
        return output

    def _sparse_search(
        self, queries: List[str], top_k: int = 10,
        source: Optional[str] = None, user_id: Optional[str] = None,
        org_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Sparse vector 检索（BGE-M3 lexical_weights，替代 BM25）

        利用 Qdrant 原生 pre-filter，不需要 get_by_ids 回查 + Python 层过滤。
        sparse_search 返回 [(doc_id, score, metadata), ...]，score 为 Qdrant sparse 原始打分。
        """
        # 构造子块 filter：chunk_type=child + source + 可见性（org/user）
        sparse_filter = self._build_child_filter(source, None, org_id, user_id)

        # 合并多个查询变体的 sparse 结果（取最大分）
        all_scores: Dict[str, float] = {}
        all_meta: Dict[str, Dict] = {}
        for query in queries:
            results = self.vector_store.sparse_search(
                query=query,
                top_k=top_k * 2,
                filters=sparse_filter,
            )
            for doc_id, score, metadata in results:
                all_scores[doc_id] = max(all_scores.get(doc_id, 0), score)
                all_meta[doc_id] = metadata

        if not all_scores:
            return []

        # 排序
        sorted_docs = sorted(all_scores.items(), key=lambda x: x[1], reverse=True)[:top_k]

        # 构建结果（metadata 已由 sparse_search 返回，无需回查向量库）
        output = []
        for doc_id, score in sorted_docs:
            meta = all_meta.get(doc_id, {})
            content = meta.get("content", "")
            output.append({
                "id": doc_id,
                "score": score,
                "title": meta.get("title", ""),
                "content": content or meta.get("content", ""),
                "source": meta.get("source", ""),
                "metadata": meta,
            })

        return output

    @staticmethod
    def _rrf_fuse(
        results_a: List[Dict], results_b: List[Dict], k: int = 60,
        weight_a: float = 1.0, weight_b: float = 1.0,
    ) -> List[Dict]:
        """加权 RRF (Reciprocal Rank Fusion) 融合两路结果

        公式: score(d) = weight_a / (k + rank_a + 1) + weight_b / (k + rank_b + 1)
        weight_a / weight_b 控制两路的相对重要性（默认 1:1 等价标准 RRF）
        """
        scores: Dict[str, float] = {}
        doc_map: Dict[str, Dict] = {}

        for rank, doc in enumerate(results_a):
            doc_id = doc["id"]
            scores[doc_id] = scores.get(doc_id, 0) + weight_a / (k + rank + 1)
            if doc_id not in doc_map:
                doc_map[doc_id] = doc

        for rank, doc in enumerate(results_b):
            doc_id = doc["id"]
            scores[doc_id] = scores.get(doc_id, 0) + weight_b / (k + rank + 1)
            if doc_id not in doc_map:
                doc_map[doc_id] = doc

        sorted_ids = sorted(scores.items(), key=lambda x: x[1], reverse=True)

        result = []
        for doc_id, score in sorted_ids:
            entry = dict(doc_map[doc_id])
            entry["score"] = score
            result.append(entry)

        return result

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        """中英文分词"""
        if not text:
            return []
        try:
            import jieba
            english_words = re.findall(r'[a-zA-Z]+', text.lower())
            chinese_text = re.sub(r'[a-zA-Z]+', '', text.lower())
            chinese_words = list(jieba.cut(chinese_text))
            stop_words = {"的", "了", "是", "在", "我", "有", "和", "就", "不", "人", "都", "一", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着", "没有", "看", "好", "自己", "这"}
            return english_words + [w for w in chinese_words if len(w) > 1 and w not in stop_words]
        except ImportError:
            return re.findall(r'[\w一-鿿]+', text.lower())

    # ========== 删除知识 ==========

    def delete(self, doc_id: str) -> bool:
        """删除知识（同时尝试删父子两个 store）

        Qdrant 的删除会自动处理 sparse vector 数据，无需额外操作。
        """
        r1 = self.vector_store.delete(doc_id=doc_id)
        r2 = True
        if self._separate_parent_child:
            r2 = self._parent_store.delete(doc_id=doc_id)
        return r1 or r2

    def delete_by_user(self, user_id: str) -> int:
        """删除用户的所有知识（同时删父子两个 store）"""
        n1 = self.vector_store.delete_by_filter({"user_id": user_id})
        if self._separate_parent_child:
            n2 = self._parent_store.delete_by_filter({"user_id": user_id})
        return n1

    def delete_by_topic(self, topic_id: str) -> int:
        """删除主题的所有知识（同时删父子两个 store）"""
        n1 = self.vector_store.delete_by_filter({"topic_id": topic_id})
        if self._separate_parent_child:
            n2 = self._parent_store.delete_by_filter({"topic_id": topic_id})
        return n1

    def delete_by_document(self, document_id: str) -> int:
        """删除文档的所有知识（同时删父子两个 store）"""
        n1 = self.vector_store.delete_by_document(document_id)
        n2 = self._parent_store.delete_by_document(document_id) if self._separate_parent_child else 0
        # 失效缓存：文档删除后，旧查询结果可能引用已删除内容
        self._invalidate_caches()
        return n1 if n1 >= 0 else n2

    def _invalidate_caches(self) -> None:
        """失效所有查询相关缓存（文档增删改时调用）

        同时失效：
        - unified_store 的查询结果缓存
        - ToolCache 的 search_knowledge 工具缓存
        - LLM 响应缓存（文档变了，旧答案可能过时）
        - 进程内的查询重写缓存
        """
        from ..core.cache import get_cache
        cache = get_cache()
        cache.clear("unified_store_query")
        cache.clear("search_knowledge")
        cache.clear("llm_response")
        n_rewrite = len(self._rewrite_cache)
        self._rewrite_cache.clear()
        if n_rewrite:
            logger.info(f"缓存失效: rewrite_cache={n_rewrite}")

    def _select_rerank_candidates(
        self,
        parent_results: List[Dict[str, Any]],
        top_k: int,
    ) -> List[Dict[str, Any]]:
        """
        动态选择送入 reranker 的候选数量

        基于分数分布的置信度判断：
        - 计算前 top_k 个分数的均值 mean_top 和尾部分数的均值 mean_tail
        - 如果均值差异大（清晰查询）：只 rerank top_k * 2 个
        - 如果均值差异小（模糊查询）：rerank 全部候选

        业界参考：DynamicRAG (arxiv 2505.07233) 的动态调整思路
        简化实现：用变异系数（CV = std/mean）作为置信度指标

        Args:
            parent_results: 父块候选（已按融合分数排序）
            top_k: 最终返回数量

        Returns:
            送入 reranker 的候选列表
        """
        if len(parent_results) <= top_k * 2:
            # 候选数本来就不多，全部送入
            return parent_results

        # 取分数（parent_results 已按 score 降序）
        scores = [r.get("score", 0.0) for r in parent_results]
        if not scores:
            return parent_results

        # 计算 top_k 个高分区和尾部低分区的分数均值
        top_scores = scores[:top_k]
        tail_scores = scores[top_k * 2:]  # 尾部（跳过中间区）

        if not tail_scores or not top_scores:
            return parent_results

        mean_top = sum(top_scores) / len(top_scores)
        mean_tail = sum(tail_scores) / len(tail_scores)

        # 分数归一化的差距（避免不同检索器分数量纲不同）
        # 如果 mean_top 接近 0，说明所有分数都很低，无法判断，保守 rerank 全部
        if abs(mean_top) < 1e-6:
            return parent_results

        relative_gap = abs(mean_top - mean_tail) / abs(mean_top)

        # 阈值 0.15：top 和 tail 的相对差距 > 15% 视为清晰查询
        # 这种情况下，尾部候选几乎不可能进入 top_k，可以安全跳过
        CLEAR_QUERY_THRESHOLD = 0.15

        if relative_gap > CLEAR_QUERY_THRESHOLD:
            # 清晰查询：只 rerank top_k * 2 个候选
            selected = parent_results[:top_k * 2]
            logger.debug(
                f"动态 rerank: 清晰查询 (relative_gap={relative_gap:.3f}), "
                f"rerank {len(selected)}/{len(parent_results)} 候选"
            )
            return selected
        else:
            # 模糊查询：rerank 全部候选
            logger.debug(
                f"动态 rerank: 模糊查询 (relative_gap={relative_gap:.3f}), "
                f"rerank 全部 {len(parent_results)} 候选"
            )
            return parent_results

    def get_by_document(self, document_id: str) -> List[Dict[str, Any]]:
        """获取文档的所有知识（合并父子两个 store）"""
        children = self.vector_store.get_by_document(document_id)
        if self._separate_parent_child:
            parents = self._parent_store.get_by_document(document_id)
            return children + parents
        return children

    def update_document(
        self,
        document_id: str,
        items: List[KnowledgeItem],
        batch_size: int = 100,
    ) -> Dict[str, int]:
        """
        更新文档知识（先删父子两个 store 旧数据，再按 chunk_type 分流添加）
        """
        if not items:
            return {"deleted": 0, "added": 0, "failed": 0}

        doc_ids = [item.id for item in items]
        contents = [item.content for item in items]
        metadatas = [item.to_chroma()["metadata"] for item in items]

        # 先删两个 store 的旧数据
        self.vector_store.delete_by_document(document_id)
        if self._separate_parent_child:
            self._parent_store.delete_by_document(document_id)

        # 按 chunk_type 分流添加
        stats = {"deleted": -1, "added": 0, "failed": 0}
        if self._separate_parent_child:
            parent_idx, child_idx = [], []
            for i, meta in enumerate(metadatas):
                if meta.get("chunk_type") == "parent":
                    parent_idx.append(i)
                else:
                    child_idx.append(i)
            if child_idx:
                s = self.vector_store.add_with_dedup(
                    [doc_ids[i] for i in child_idx],
                    [contents[i] for i in child_idx],
                    [metadatas[i] for i in child_idx],
                    batch_size=batch_size,
                )
                stats["added"] += s.get("added", 0) + s.get("updated", 0)
                stats["failed"] += s.get("failed", 0)
            if parent_idx:
                s = self._parent_store.add_with_dedup(
                    [doc_ids[i] for i in parent_idx],
                    [contents[i] for i in parent_idx],
                    [metadatas[i] for i in parent_idx],
                    batch_size=batch_size,
                )
                stats["added"] += s.get("added", 0) + s.get("updated", 0)
                stats["failed"] += s.get("failed", 0)
        else:
            s = self.vector_store.add_with_dedup(doc_ids, contents, metadatas, batch_size=batch_size)
            stats["added"] = s.get("added", 0) + s.get("updated", 0)
            stats["failed"] = s.get("failed", 0)

        # sparse vector 由 QdrantVectorStore 在 add 时自动处理，无需额外操作
        # 失效缓存：文档更新后内容变化，旧查询结果不再适用
        self._invalidate_caches()
        logger.info(f"文档 {document_id} 更新完成: {stats}")
        return stats

    def incremental_update(
        self,
        document_id: str,
        items: List[KnowledgeItem],
        batch_size: int = 100,
    ) -> Dict[str, int]:
        """
        增量更新文档知识（按 chunk_type 分流到父子 store）
        """
        if not items:
            return {"added": 0, "deleted": 0, "updated": 0, "unchanged": 0, "failed": 0}

        # 转换为分块格式
        chunks = []
        for item in items:
            chunks.append({
                "id": item.id,
                "text": item.content,
                "metadata": item.to_chroma()["metadata"],
            })

        # 分离模式下按 chunk_type 分流
        if self._separate_parent_child:
            child_chunks = [c for c in chunks if c["metadata"].get("chunk_type") != "parent"]
            parent_chunks = [c for c in chunks if c["metadata"].get("chunk_type") == "parent"]
            stats = {"added": 0, "deleted": 0, "updated": 0, "unchanged": 0, "failed": 0}
            if child_chunks:
                s = self.vector_store.incremental_update(
                    document_id=document_id, new_chunks=child_chunks, batch_size=batch_size,
                )
                for k in stats:
                    stats[k] += s.get(k, 0)
            if parent_chunks:
                s = self._parent_store.incremental_update(
                    document_id=document_id, new_chunks=parent_chunks, batch_size=batch_size,
                )
                for k in stats:
                    stats[k] += s.get(k, 0)
        else:
            stats = self.vector_store.incremental_update(
                document_id=document_id, new_chunks=chunks, batch_size=batch_size,
            )

        # sparse vector 由 QdrantVectorStore 在 add 时自动处理，无需额外操作
        logger.info(f"文档 {document_id} 增量更新完成: {stats}")
        return stats

    # ========== 统计 ==========

    def size(self) -> int:
        """记录数量（父子 store 合计）"""
        n = self.vector_store.size()
        if self._separate_parent_child:
            n += self._parent_store.size()
        return n

    def get_statistics(self) -> Dict[str, Any]:
        """获取统计信息"""
        all_data = self.vector_store.get_all(include=["metadatas"])
        metadatas = all_data.get("metadatas", [])
        if self._separate_parent_child:
            parent_data = self._parent_store.get_all(include=["metadatas"])
            metadatas = metadatas + parent_data.get("metadatas", [])

        # 按来源统计
        source_counts = {}
        for meta in metadatas:
            source = meta.get("source", "unknown")
            source_counts[source] = source_counts.get(source, 0) + 1

        return {
            "total": self.size(),
            "by_source": source_counts,
        }


# ========== 查询重写 ==========

def _build_expansion_map(
    base_synonyms: Dict[str, List[str]],
    abbreviations: Dict[str, List[str]],
) -> Dict[str, List[str]]:
    """构建双向同义词扩展表（包含术语和缩写）"""
    raw: Dict[str, Set[str]] = {}
    sources = [base_synonyms, abbreviations]
    for source in sources:
        for term, synonyms in source.items():
            key = term.lower()
            raw.setdefault(key, set())
            for syn in synonyms:
                syn_key = syn.lower()
                raw[key].add(syn_key)
                # 反向映射：同义词 -> 原词及其他同义词
                raw.setdefault(syn_key, set())
                raw[syn_key].add(key)
                raw[syn_key].update(
                    s.lower() for s in synonyms if s.lower() != syn_key
                )
    # 移除自身并排序，保证输出稳定
    return {
        k: sorted(v - {k})
        for k, v in raw.items()
        if v - {k}
    }


class QueryRewriter:
    """
    查询重写器

    对用户原始查询进行优化，提高检索质量。
    """

    # 常见后缀，可以去掉以获取核心关键词
    SUFFIXES = ["怎么学", "怎么用", "是什么", "怎么理解", "如何", "为什么", "怎么办", "什么意思", "怎么实现"]

    # 停用词
    STOP_WORDS = {"的", "了", "是", "在", "我", "有", "和", "就", "不", "人", "都", "一", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着", "没有", "看", "好", "自己", "这", "那", "吗", "呢", "吧", "啊", "请", "帮", "我"}

    # 技术术语中英文对照 / 同义词（key 为小写，中文按字面匹配，英文按单词边界匹配）
    BASE_TECH_SYNONYMS: Dict[str, List[str]] = {
        # Python
        "装饰器": ["decorator"],
        "迭代器": ["iterator"],
        "生成器": ["generator"],
        "生成器表达式": ["generator expression"],
        "列表推导式": ["list comprehension"],
        "高阶函数": ["higher-order function"],
        "闭包": ["closure"],
        "递归": ["recursion"],
        "动态规划": ["dynamic programming", "dp"],
        "记忆化": ["memoization", "lru_cache"],
        # Web / FastAPI
        "fastapi": ["fast api"],
        "依赖注入": ["dependency injection", "depends"],
        "路由": ["routing", "route"],
        "restful": ["rest", "api design"],
        "get": ["http get"],
        "post": ["http post"],
        "状态码": ["status code"],
        # 数据库 / SQLAlchemy
        "sqlalchemy": ["sqlalchemy", "orm"],
        "orm": ["对象关系映射", "模型映射"],
        "session": ["会话", "事务"],
        "crud": ["增删改查", "create", "read", "update", "delete"],
        "表连接": ["join", "sql join"],
        # 缓存 / Redis
        "redis": ["key-value", "缓存数据库"],
        "缓存": ["cache"],
        "持久化": ["persistence"],
        # 消息队列 / Celery
        "celery": ["任务队列", "distributed task queue"],
        "broker": ["消息中间件", "消息代理"],
        "worker": ["工作进程", "任务执行进程"],
        "backend": ["结果后端", "result backend"],
        "定时任务": ["periodic task", "scheduled task"],
        # 异步
        "async": ["异步", "协程"],
        "await": ["等待", "挂起"],
        "asyncio": ["事件循环", "event loop"],
        "gather": ["并发执行", "同时运行"],
        "to_thread": ["线程池", "thread pool"],
        # 版本控制 / Docker
        "git": ["version control", "版本控制"],
        "分支": ["branch"],
        "提交": ["commit"],
        "docker": ["container", "容器化"],
        "镜像": ["image"],
        "容器": ["container"],
        # 通用
        "sql": ["database", "数据库"],
        "api": ["接口"],
        "url": ["统一资源定位符"],
        "lru": ["lru_cache"],
    }

    # 缩写 / 首字母缩写词补全
    ABBREVIATIONS: Dict[str, List[str]] = {
        "orm": ["对象关系映射", "sqlalchemy"],
        "crud": ["增删改查", "创建", "读取", "更新", "删除"],
        "api": ["接口", "application programming interface"],
        "url": ["统一资源定位符"],
        "jwt": ["token", "认证"],
        "rdb": ["redis rdb", "内存快照"],
        "aof": ["append only file", "写操作日志"],
        "sql": ["structured query language", "数据库"],
    }

    _EXPANSION_MAP: Dict[str, List[str]] = _build_expansion_map(
        BASE_TECH_SYNONYMS, ABBREVIATIONS
    )

    @staticmethod
    def _is_english_term(term: str) -> bool:
        """判断是否主要由英文/数字/下划线组成的术语"""
        return bool(re.fullmatch(r"[a-z0-9_.]+|[a-z]+\s+[a-z]+", term))

    @staticmethod
    def _term_in_query(query_lower: str, term: str) -> bool:
        """判断术语是否出现在查询中（英文使用单词边界，中文使用子串）"""
        if not term:
            return False
        if QueryRewriter._is_english_term(term):
            return re.search(r"\b" + re.escape(term) + r"\b", query_lower) is not None
        return term in query_lower

    @staticmethod
    def expand_terms(query: str) -> List[str]:
        """
        扩展查询中的技术术语，返回应补充的同义词列表

        Args:
            query: 原始查询

        Returns:
            List[str]: 需要补充的术语列表（已过滤掉查询中已有的词）
        """
        if not query or not query.strip():
            return []

        query_lower = query.lower().strip()
        expansions: Set[str] = set()
        for term, synonyms in QueryRewriter._EXPANSION_MAP.items():
            if QueryRewriter._term_in_query(query_lower, term):
                for syn in synonyms:
                    if not QueryRewriter._term_in_query(query_lower, syn):
                        expansions.add(syn)
        return sorted(expansions)

    @staticmethod
    def rewrite(query: str) -> List[str]:
        """
        重写查询，返回多个查询变体（基础模式）

        Args:
            query: 原始查询

        Returns:
            List[str]: 查询变体列表（包含原始查询）
        """
        if not query or not query.strip():
            return [query]

        queries = [query.strip()]

        # 1. 去掉常见后缀，提取核心关键词
        for suffix in QueryRewriter.SUFFIXES:
            if query.endswith(suffix) and len(query) > len(suffix):
                core = query[:-len(suffix)].strip()
                if core and core not in queries:
                    queries.append(core)

        # 2. 分词后提取关键词组合
        terms = UnifiedKnowledgeStore._tokenize(query)
        if len(terms) >= 2:
            # 去掉停用词后的关键词
            keywords = [t for t in terms if t not in QueryRewriter.STOP_WORDS]
            if keywords and keywords != terms:
                keyword_query = " ".join(keywords)
                if keyword_query not in queries:
                    queries.append(keyword_query)

        # 3. 去重
        return list(dict.fromkeys(queries))  # 保持顺序去重

    @staticmethod
    def enhanced_rewrite(query: str) -> List[str]:
        """
        增强重写查询，返回多个查询变体

        在基础模式上增加：
        - 技术术语中英文同义词扩展（双向映射）
        - 缩写补全
        - 核心关键词组合

        Args:
            query: 原始查询

        Returns:
            List[str]: 查询变体列表（包含原始查询）
        """
        if not query or not query.strip():
            return [query]

        queries = QueryRewriter.rewrite(query)
        base_query = query.strip()
        base_query_lower = base_query.lower()

        # 1. 同义词 / 缩写扩展查询
        expanded_terms = QueryRewriter.expand_terms(base_query)
        if expanded_terms:
            expanded_query = base_query_lower + " " + " ".join(expanded_terms)
            if expanded_query not in queries:
                queries.append(expanded_query)

        # 2. 生成仅含核心关键词的英文/中文混合查询
        terms = UnifiedKnowledgeStore._tokenize(base_query_lower)
        keywords = [t for t in terms if t not in QueryRewriter.STOP_WORDS]
        keyword_queries: List[str] = []
        for term in keywords:
            term_lower = term.lower()
            keyword_queries.append(term_lower)
            # 追加该词的同义词（如果有）
            for syn in QueryRewriter._EXPANSION_MAP.get(term_lower, []):
                keyword_queries.append(syn)

        if keyword_queries:
            keyword_query = " ".join(list(dict.fromkeys(keyword_queries)))
            if keyword_query not in queries:
                queries.append(keyword_query)

        # 3. 去重
        return list(dict.fromkeys(queries))


# ========== 知识迁移工具 ==========

async def migrate_knowledge_to_vector_db(
    db,
    embedding_model: EmbeddingModel,
    persist_directory: Optional[str] = None,
) -> Dict[str, int]:
    """
    将 MongoDB 中的知识迁移到 ChromaDB

    Args:
        db: 数据库连接
        embedding_model: 嵌入模型
        persist_directory: ChromaDB 持久化目录

    Returns:
        Dict[str, int]: 迁移统计
    """
    store = UnifiedKnowledgeStore(
        embedding_model=embedding_model,
        persist_directory=persist_directory,
    )

    stats = {"course": 0, "teaching": 0, "total": 0}

    # 1. 迁移课程知识（从 knowledge_base collection）
    try:
        # 获取所有课程
        courses = await db.get_all_courses()
        items = []
        for course in courses:
            # 获取课程下的知识点
            course_id = course.get("course_id", "")
            knowledge_points = await db.get_knowledge_by_course(course_id)
            for kp in knowledge_points:
                item = KnowledgeItem(
                    id=f"kp_{kp.get('knowledge_id', '')}",
                    title=kp.get("title", ""),
                    content=kp.get("content", ""),
                    source="course",
                    metadata={
                        "course_id": course_id,
                        "course_name": course.get("title", ""),
                        "topic": kp.get("topic", ""),
                        "difficulty": kp.get("difficulty", "medium"),
                        "key_points": kp.get("key_points", []),
                    }
                )
                items.append(item)

        if items:
            store.add_batch(items)
            stats["course"] = len(items)
            logger.info(f"迁移课程知识: {len(items)} 条")
    except Exception as e:
        logger.error(f"迁移课程知识失败: {e}")

    # 2. 迁移教学经验
    try:
        experiences = await db.get_teaching_experiences()
        items = []
        for exp in experiences:
            item = KnowledgeItem(
                id=f"exp_{exp.get('experience_id', '')}",
                title=exp.get("title", ""),
                content=exp.get("content", ""),
                source="teaching",
                metadata={
                    "category": exp.get("category", ""),
                    "effectiveness": exp.get("effectiveness", 0.5),
                    "examples": exp.get("examples", []),
                    "tags": exp.get("tags", []),
                }
            )
            items.append(item)

        if items:
            store.add_batch(items)
            stats["teaching"] = len(items)
            logger.info(f"迁移教学经验: {len(items)} 条")
    except Exception as e:
        logger.error(f"迁移教学经验失败: {e}")

    stats["total"] = stats["course"] + stats["teaching"]
    logger.info(f"知识迁移完成: {stats}")
    return stats

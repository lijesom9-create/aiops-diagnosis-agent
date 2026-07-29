"""
Unified Knowledge Store - 统一知识存储

将所有知识统一存储到 ChromaDB：
- 课程知识（course）
- 教学经验（teaching）
- 用户文档（user_document）

MongoDB 只存储元数据和状态，不存储知识内容。
"""

import math
import re
import threading
from typing import List, Dict, Any, Optional, Set
from dataclasses import dataclass, field
from datetime import datetime
from loguru import logger

from ..retrieval.chroma_store import ChromaDBVectorStore
from ..retrieval.qdrant_store import QdrantVectorStore
from ..retrieval.embeddings import EmbeddingModel


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


# ========== BM25 倒排索引 ==========

class BM25Index:
    """
    BM25 倒排索引（内存持久化）

    在文档添加/删除时增量更新，搜索时直接使用，不需要每次重建。
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b

        # 倒排索引核心数据结构
        self._doc_contents: Dict[str, str] = {}       # doc_id → content
        self._doc_lengths: Dict[str, int] = {}         # doc_id → 文档长度（词数）
        self._term_freqs: Dict[str, Dict[str, int]] = {}  # term → {doc_id: 频率}
        self._doc_freqs: Dict[str, int] = {}           # term → 出现在几个文档中

        self._lock = threading.Lock()
        self._avg_doc_length: float = 0.0
        self._total_docs: int = 0

    def add_document(self, doc_id: str, content: str) -> None:
        """添加文档到倒排索引"""
        terms = self._tokenize(content)
        if not terms:
            return

        with self._lock:
            # 如果文档已存在，先移除旧的
            if doc_id in self._doc_contents:
                self._remove_doc_internal(doc_id)

            # 存储文档内容和长度
            self._doc_contents[doc_id] = content
            self._doc_lengths[doc_id] = len(terms)
            self._total_docs = len(self._doc_contents)

            # 统计词频
            tf = {}
            for term in terms:
                tf[term] = tf.get(term, 0) + 1

            # 更新倒排索引
            for term, freq in tf.items():
                if term not in self._term_freqs:
                    self._term_freqs[term] = {}
                self._term_freqs[term][doc_id] = freq
                self._doc_freqs[term] = self._doc_freqs.get(term, 0) + 1

            # 更新平均文档长度
            total_length = sum(self._doc_lengths.values())
            self._avg_doc_length = total_length / self._total_docs if self._total_docs else 0

    def add_batch(self, doc_ids: List[str], contents: List[str]) -> None:
        """批量添加文档"""
        for doc_id, content in zip(doc_ids, contents):
            self.add_document(doc_id, content)

    def remove_document(self, doc_id: str) -> None:
        """从倒排索引中移除文档"""
        with self._lock:
            self._remove_doc_internal(doc_id)

    def _remove_doc_internal(self, doc_id: str) -> None:
        """内部移除（需要已持有锁）"""
        if doc_id not in self._doc_contents:
            return

        # 从倒排索引中移除
        for term in list(self._term_freqs.keys()):
            if doc_id in self._term_freqs[term]:
                freq = self._term_freqs[term].pop(doc_id)
                self._doc_freqs[term] -= freq
                if self._doc_freqs[term] <= 0:
                    del self._doc_freqs[term]
                    del self._term_freqs[term]

        del self._doc_contents[doc_id]
        del self._doc_lengths[doc_id]
        self._total_docs = len(self._doc_contents)

        total_length = sum(self._doc_lengths.values())
        self._avg_doc_length = total_length / self._total_docs if self._total_docs else 0

    def search(self, query: str, top_k: int = 10) -> List[tuple]:
        """
        BM25 搜索

        Returns:
            List[(doc_id, score)]: 按分数排序的结果
        """
        query_terms = self._tokenize(query)
        if not query_terms or not self._doc_contents:
            return []

        with self._lock:
            scores = {}
            for doc_id in self._doc_contents:
                score = self._calculate_score(query_terms, doc_id)
                if score > 0:
                    scores[doc_id] = score

        sorted_results = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return sorted_results[:top_k]

    def _calculate_score(self, query_terms: List[str], doc_id: str) -> float:
        """计算单个文档的 BM25 分数"""
        score = 0.0
        doc_len = self._doc_lengths.get(doc_id, 0)

        for term in query_terms:
            tf = self._term_freqs.get(term, {}).get(doc_id, 0)
            if tf == 0:
                continue

            df = self._doc_freqs.get(term, 0)
            idf = max(0.0, math.log((self._total_docs - df + 0.5) / (df + 0.5) + 1))

            if self._avg_doc_length == 0:
                tf_norm = 0.0
            else:
                tf_norm = (tf * (self.k1 + 1)) / (
                    tf + self.k1 * (1 - self.b + self.b * doc_len / self._avg_doc_length)
                )

            score += idf * tf_norm

        return score

    @property
    def size(self) -> int:
        return self._total_docs

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        """中英文分词（使用领域词典）"""
        if not text:
            return []
        try:
            import jieba
            # 英文按空格和标点分词
            english_words = re.findall(r'[a-zA-Z_][a-zA-Z0-9_]*', text)
            # 中文使用 jieba 分词
            chinese_text = re.sub(r'[a-zA-Z0-9_]+', ' ', text)
            chinese_words = list(jieba.cut(chinese_text))
            # 合并，过滤停用词和单字符
            stop_words = {"的", "了", "是", "在", "我", "有", "和", "就", "不", "人", "都", "一", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着", "没有", "看", "好", "自己", "这", "那", "吗", "呢", "吧", "啊", "请", "帮", "能", "可以", "什么", "怎么", "如何"}
            all_words = [w.lower() for w in english_words] + [w for w in chinese_words if len(w) > 1 and w not in stop_words]
            return all_words
        except ImportError:
            return re.findall(r'[\w一-鿿]+', text.lower())


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
    ):
        self.embedding_model = embedding_model
        self._separate_parent_child = separate_parent_child

        # 子块向量库（向后兼容：self.vector_store 始终指向 child store）
        self.vector_store = self._create_vector_store(
            embedding_model=embedding_model,
            collection_name=collection_name,
            persist_directory=persist_directory,
            backend=vector_store_backend,
        )
        # 父块向量库（分离存储：独立 collection，不参与 ANN 检索，仅按 ID 取回）
        if separate_parent_child:
            self._parent_store = self._create_vector_store(
                embedding_model=embedding_model,
                collection_name=f"{collection_name}_parent",
                persist_directory=persist_directory,
                backend=vector_store_backend,
            )
        else:
            # 兼容模式：父子同库（旧逻辑，不推荐）
            self._parent_store = self.vector_store

        self.reranker = reranker

        # BM25 倒排索引（内存持久化，启动时从子块向量库加载）
        self._bm25 = BM25Index()
        self._bm25_initialized = False

        logger.info(
            f"UnifiedKnowledgeStore 初始化完成 (parent_child_separated={separate_parent_child}), "
            f"child_size={self.vector_store.size()}, parent_size={self._parent_store.size()}"
        )

        # P0-2: 查询结果缓存（LRU + TTL，避免相同 query 重复检索）
        self._query_cache: Dict[str, Dict[str, Any]] = {}  # key -> {"results": ..., "ts": ...}
        self._query_cache_ttl: int = 300  # 5 分钟
        self._query_cache_max: int = 128  # 最多缓存 128 个查询

    def _cache_get(self, key: str) -> Optional[List[Dict]]:
        """从缓存获取查询结果"""
        import time
        entry = self._query_cache.get(key)
        if entry is None:
            return None
        if time.time() - entry["ts"] > self._query_cache_ttl:
            del self._query_cache[key]
            return None
        return entry["results"]

    def _cache_put(self, key: str, results: List[Dict]):
        """写入缓存结果"""
        import time
        if len(self._query_cache) >= self._query_cache_max:
            # 淘汰最旧的
            oldest = min(self._query_cache.items(), key=lambda x: x[1]["ts"])
            del self._query_cache[oldest[0]]
        self._query_cache[key] = {"results": results, "ts": time.time()}

    @staticmethod
    def _create_vector_store(
        embedding_model: EmbeddingModel,
        collection_name: str,
        persist_directory: Optional[str],
        backend: Optional[str] = None,
    ):
        """
        工厂方法：根据 backend 选择向量存储后端

        Args:
            backend: "chroma" | "qdrant" | None（None 时从 settings 读取）
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
            )

        # 默认 ChromaDB
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

    def _ensure_bm25_index(self) -> None:
        """确保 BM25 索引已加载（懒加载）"""
        if self._bm25_initialized:
            return
        self._rebuild_bm25_index()
        self._bm25_initialized = True

    def _rewrite_query(self, query: str, rewrite_query: bool, rewrite_mode: str) -> List[str]:
        """
        统一查询重写入口

        支持模式：
        - basic: 仅规则重写（去后缀、关键词组合）
        - enhanced: 规则重写 + 同义词/缩写扩展（默认）
        - llm: 仅 LLM MultiQuery 重写，失败降级到 enhanced
        - enhanced_llm: 规则重写 + LLM MultiQuery 叠加（去重），失败降级到 enhanced
        """
        if not rewrite_query:
            return [query]

        if rewrite_mode in ("llm", "enhanced_llm"):
            try:
                from ..retrieval.llm_query_rewriter import get_default_llm_rewriter
                rewriter = get_default_llm_rewriter()
                # 注入 embedding_model（用于变体语义过滤）
                if self.embedding_model is not None:
                    rewriter.set_embedding_model(self.embedding_model)
                original_mode = rewriter.mode
                rewriter.mode = rewrite_mode
                try:
                    return rewriter.rewrite(query)
                finally:
                    rewriter.mode = original_mode
            except Exception as e:
                logger.warning(f"LLM MultiQuery 重写失败，降级到 enhanced: {type(e).__name__}: {e}")
                return QueryRewriter.enhanced_rewrite(query)

        if rewrite_mode == "enhanced":
            return QueryRewriter.enhanced_rewrite(query)
        return QueryRewriter.rewrite(query)

    def _rebuild_bm25_index(self) -> None:
        """从 ChromaDB 重建 BM25 索引（只索引子块，父块不进入 BM25）"""
        all_data = self.vector_store.get_all(include=["documents", "metadatas"])
        if not all_data or not all_data.get("ids"):
            return
        ids = all_data["ids"]
        documents = all_data.get("documents", [])
        metadatas = all_data.get("metadatas", []) or [{}] * len(ids)

        self._bm25 = BM25Index()
        child_ids = []
        child_documents = []
        for i, doc_id in enumerate(ids):
            meta = metadatas[i] if i < len(metadatas) else {}
            # 分离模式下 child_store 已无 parent；兼容模式下仍需过滤
            if meta.get("chunk_type") == "parent":
                continue
            child_ids.append(doc_id)
            child_documents.append(documents[i])

        self._bm25.add_batch(child_ids, child_documents)
        logger.info(f"BM25 索引重建完成: {self._bm25.size} 篇文档 (从 child_store 加载)")

    # ========== 添加知识 ==========

    def add(self, item: KnowledgeItem) -> None:
        """添加单条知识（按 chunk_type 分流到 parent/child store）"""
        chroma_data = item.to_chroma()
        meta = chroma_data["metadata"]
        target_store = self._parent_store if meta.get("chunk_type") == "parent" else self.vector_store
        target_store.add(
            doc_id=item.id,
            content=item.content,
            metadata=meta,
        )
        # 同步更新 BM25 索引：只索引子块
        self._ensure_bm25_index()
        if meta.get("chunk_type") != "parent":
            self._bm25.add_document(item.id, item.content)
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

        # 同步更新 BM25 索引：只索引子块
        self._ensure_bm25_index()
        child_ids, child_contents = [], []
        for i, meta in enumerate(metadatas):
            if meta.get("chunk_type") == "parent":
                continue
            child_ids.append(doc_ids[i])
            child_contents.append(contents[i])
        if child_ids:
            self._bm25.add_batch(child_ids, child_contents)
        logger.info(f"批量添加知识: {len(items)} 条 (parent={sum(1 for m in metadatas if m.get('chunk_type')=='parent')}, child={len(child_ids)})")

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

        # 后处理：组织隔离 + 用户隔离
        output = []
        for doc_id, score, metadata in results:
            # 组织隔离：只返回同一组织的数据
            if org_id:
                doc_org_id = metadata.get("org_id")
                if doc_org_id and doc_org_id != org_id:
                    continue  # 跳过其他组织的数据

            # 用户隔离：如果指定了 user_id，只返回公共知识或该用户的私有知识
            if user_id:
                doc_user_id = metadata.get("user_id")
                if doc_user_id and doc_user_id != user_id:
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
        混合检索：BM25（关键词）+ Vector（语义）+ RRF 融合

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
            candidate_multiplier: vector/BM25 候选数量相对于 top_k 的倍数，默认 3

        Returns:
            List[Dict]: 搜索结果
        """
        # 0. 查询重写
        queries = self._rewrite_query(query, rewrite_query, rewrite_mode)

        # 1. 向量检索（主查询）
        vector_results = self._vector_search(
            query, top_k=top_k * candidate_multiplier, source=source, user_id=user_id, org_id=org_id
        )

        # 2. BM25 检索
        bm25_results = self._bm25_search(
            queries, top_k=top_k * candidate_multiplier, source=source, user_id=user_id, org_id=org_id
        )

        # 3. RRF 融合
        if bm25_results and vector_results:
            fused = self._rrf_fuse(bm25_results, vector_results, k=rrf_k)
        elif vector_results:
            fused = vector_results
        else:
            fused = bm25_results

        # 4. Rerank
        if self.reranker and fused:
            try:
                from ..retrieval.base import RetrievalResult
                retrieval_results = [
                    RetrievalResult(
                        doc_id=item["id"],
                        content=item["content"],
                        score=item["score"],
                        metadata=item["metadata"],
                        source=item.get("source", ""),
                    )
                    for item in fused
                ]
                reranked = self.reranker.rerank(query, retrieval_results, limit=top_k)
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
    ) -> List[Dict[str, Any]]:
        """
        父子文档混合检索

        流程：
        1. 向量检索子块（chunk_type=child）
        2. BM25 检索子块
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

        cache_key_str = f"{query}|{top_k}|{rewrite_mode}|{candidate_multiplier}|{rrf_k}|{vector_weight}|{bm25_weight}|{source}|{user_id}|{org_id}"
        cache_key = hashlib.md5(cache_key_str.encode()).hexdigest()
        cached = self._cache_get(cache_key)
        if cached is not None:
            _metrics.increment("rag_query_total")
            _metrics.increment("rag_cache_hit_total")
            logger.debug(f"查询缓存命中: {query[:30]}...")
            return cached

        # 0. 查询重写（支持 basic / enhanced / llm / enhanced_llm）
        _t0 = _time.time()
        queries = self._rewrite_query(query, rewrite_query, rewrite_mode)
        _rewrite_ms = (_time.time() - _t0) * 1000

        # P0-3: 并行检索（向量 + BM25 同时执行）
        from concurrent.futures import ThreadPoolExecutor

        def _vector_search():
            """向量检索子块"""
            vector_filters: Dict[str, Any] = {"chunk_type": "child"}
            if source:
                vector_filters = {"$and": [{"chunk_type": "child"}, {"source": source}]}
            vector_results = self.vector_store.search(
                query=query,
                top_k=child_top_k * candidate_multiplier,
                min_score=0.0,
                filters=vector_filters,
            )
            children = []
            for doc_id, score, metadata in vector_results:
                if org_id and metadata.get("org_id") and metadata.get("org_id") != org_id:
                    continue
                if user_id and metadata.get("user_id") and metadata.get("user_id") != user_id:
                    continue
                children.append({"id": doc_id, "score": score, "metadata": metadata})
            return children

        def _bm25_search():
            """BM25 检索子块"""
            self._ensure_bm25_index()
            if self._bm25.size == 0:
                return []
            all_scores: Dict[str, float] = {}
            for q in queries:
                results = self._bm25.search(q, top_k=child_top_k * candidate_multiplier)
                for doc_id, score in results:
                    all_scores[doc_id] = max(all_scores.get(doc_id, 0), score)
            sorted_docs = sorted(all_scores.items(), key=lambda x: x[1], reverse=True)[:child_top_k]
            children = []
            for doc_id, score in sorted_docs:
                try:
                    records = self.vector_store.get_by_ids([doc_id])
                    if records:
                        meta = records[0].get("metadata", {})
                        content = records[0].get("content", "")
                        children.append({"id": doc_id, "score": score, "metadata": meta, "content": content})
                except Exception:
                    continue
            return children

        # 并行执行向量检索和 BM25 检索
        _t1 = _time.time()
        with ThreadPoolExecutor(max_workers=2) as executor:
            future_v = executor.submit(_vector_search)
            future_b = executor.submit(_bm25_search)
            vector_children = future_v.result()
            bm25_children = future_b.result()
        _retrieval_ms = (_time.time() - _t1) * 1000

        # 3. 加权 RRF 融合子块结果
        _t2 = _time.time()
        if bm25_children and vector_children:
            fused_children = self._rrf_fuse(
                vector_children, bm25_children, k=rrf_k,
                weight_a=vector_weight, weight_b=bm25_weight,
            )
        elif vector_children:
            fused_children = vector_children
        else:
            fused_children = bm25_children
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
                retrieval_results = [
                    RetrievalResult(
                        doc_id=item["id"],
                        content=item["content"],
                        score=item["score"],
                        metadata=item["metadata"],
                        source=item.get("source", ""),
                    )
                    for item in parent_results
                ]
                # 取 top_k * 2 给 reranker，重排后截断 top_k
                rerank_limit = min(len(parent_results), max(top_k * 2, top_k + 5))
                reranked = self.reranker.rerank(query, retrieval_results, limit=rerank_limit)
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

        # 8. 按分数排序、过滤、截断
        parent_results.sort(key=lambda x: x["score"], reverse=True)
        result = [r for r in parent_results if r.get("score", 0) >= min_score][:top_k]

        # P1-3: 结构化日志 + 指标采集
        _total_ms = (_time.time() - _t_start) * 1000
        logger.info(
            f"RAG 检索完成 | query={query[:30]!r} "
            f"| rewrite={_rewrite_ms:.1f}ms retrieval={_retrieval_ms:.1f}ms "
            f"fuse={_fuse_ms:.1f}ms rerank={_rerank_ms:.1f}ms total={_total_ms:.1f}ms "
            f"| vector_hits={len(vector_children)} bm25_hits={len(bm25_children)} "
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
        _metrics.observe("rag_bm25_hits", len(bm25_children))
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

        query_terms = set(t.lower() for t in BM25Index._tokenize(query) if t)
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

    def _bm25_search(
        self, queries: List[str], top_k: int = 10,
        source: Optional[str] = None, user_id: Optional[str] = None,
        org_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """BM25 关键词检索（使用持久化倒排索引）"""
        self._ensure_bm25_index()

        if self._bm25.size == 0:
            return []

        # 合并多个查询变体的 BM25 结果
        all_scores: Dict[str, float] = {}
        for query in queries:
            results = self._bm25.search(query, top_k=top_k * 2)
            for doc_id, score in results:
                all_scores[doc_id] = max(all_scores.get(doc_id, 0), score)

        if not all_scores:
            return []

        # 排序
        sorted_docs = sorted(all_scores.items(), key=lambda x: x[1], reverse=True)[:top_k]

        # 构建结果（从向量库获取元数据）
        output = []
        for doc_id, score in sorted_docs:
            # 获取元数据（通过公开 API，避免依赖具体后端的 _collection）
            try:
                records = self.vector_store.get_by_ids([doc_id])
                if records:
                    meta = records[0]["metadata"]
                    content = records[0]["content"]
                else:
                    meta = {}
                    content = ""
            except Exception:
                meta = {}
                content = ""

            # 过滤
            if source and meta.get("source") != source:
                continue
            if user_id:
                doc_user_id = meta.get("user_id")
                if doc_user_id and doc_user_id != user_id:
                    continue

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
        """删除知识（同时尝试删父子两个 store）"""
        r1 = self.vector_store.delete(doc_id=doc_id)
        r2 = True
        if self._separate_parent_child:
            r2 = self._parent_store.delete(doc_id=doc_id)
        # 同步更新 BM25 索引
        self._ensure_bm25_index()
        self._bm25.remove_document(doc_id)
        return r1 or r2

    def delete_by_user(self, user_id: str) -> int:
        """删除用户的所有知识（同时删父子两个 store）"""
        n1 = self.vector_store.delete_by_filter({"user_id": user_id})
        if self._separate_parent_child:
            n2 = self._parent_store.delete_by_filter({"user_id": user_id})
        self._ensure_bm25_index()
        return n1

    def delete_by_topic(self, topic_id: str) -> int:
        """删除主题的所有知识（同时删父子两个 store）"""
        n1 = self.vector_store.delete_by_filter({"topic_id": topic_id})
        if self._separate_parent_child:
            n2 = self._parent_store.delete_by_filter({"topic_id": topic_id})
        self._ensure_bm25_index()
        return n1

    def delete_by_document(self, document_id: str) -> int:
        """删除文档的所有知识（同时删父子两个 store）"""
        n1 = self.vector_store.delete_by_document(document_id)
        n2 = self._parent_store.delete_by_document(document_id) if self._separate_parent_child else 0
        # 同步更新 BM25
        self._ensure_bm25_index()
        return n1 if n1 >= 0 else n2

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

        # 重建 BM25
        self._bm25_initialized = False
        self._ensure_bm25_index()
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

        # 重建 BM25
        self._bm25_initialized = False
        self._ensure_bm25_index()
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

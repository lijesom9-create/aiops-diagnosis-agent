"""
混合检索器

实现稀疏检索 + 稠密检索的完整架构：
- 稀疏检索: BM25 (关键词匹配)
- 稠密检索: Vector (语义匹配)
- 融合: RRF / Weighted Sum / Convex Combination
- 重排序: CrossEncoder / LLM
"""

import math
import re
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass, field
from loguru import logger

from .fusion import RRFFusion, FusionResult, create_fusion


@dataclass
class RetrievalResult:
    """检索结果"""
    doc_id: str
    content: str
    score: float
    source: str  # bm25, vector, hybrid
    metadata: Dict[str, Any] = field(default_factory=dict)
    chunk_index: int = 0

    def to_dict(self) -> Dict:
        return {
            "doc_id": self.doc_id,
            "content": self.content[:200],
            "score": self.score,
            "source": self.source,
            "metadata": self.metadata,
        }


class SparseRetriever:
    """
    稀疏检索器 (BM25)

    特点:
    - 基于词频统计
    - 精确匹配关键词
    - 不理解语义
    - 速度快
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        """
        Args:
            k1: 词频饱和参数
            b: 文档长度归一化参数
        """
        self.k1 = k1
        self.b = b
        self._documents: Dict[str, Dict] = {}
        self._doc_lengths: Dict[str, int] = {}
        self._avg_doc_length: float = 0.0
        self._term_freqs: Dict[str, Dict[str, int]] = {}
        self._doc_freqs: Dict[str, int] = {}
        self._idf_cache: Dict[str, float] = {}

    def add_document(self, doc_id: str, content: str, metadata: Dict = None):
        """添加文档"""
        self._documents[doc_id] = {
            "content": content,
            "metadata": metadata or {},
        }

        # 分词
        terms = self._tokenize(content)
        self._doc_lengths[doc_id] = len(terms)

        # 统计词频
        term_freq = {}
        for term in terms:
            term_freq[term] = term_freq.get(term, 0) + 1

        # 更新索引
        for term, freq in term_freq.items():
            if term not in self._term_freqs:
                self._term_freqs[term] = {}
            self._term_freqs[term][doc_id] = freq

            if term not in self._doc_freqs:
                self._doc_freqs[term] = 0
            self._doc_freqs[term] += 1

        # 更新平均文档长度
        total_length = sum(self._doc_lengths.values())
        self._avg_doc_length = total_length / len(self._documents) if self._documents else 0

        # 清除 IDF 缓存
        self._idf_cache.clear()

    def add_batch(self, documents: List[Dict[str, Any]]):
        """批量添加文档"""
        for doc in documents:
            self.add_document(
                doc_id=doc.get("id", ""),
                content=doc.get("content", ""),
                metadata=doc.get("metadata", {}),
            )

    def search(self, query: str, top_k: int = 10) -> List[RetrievalResult]:
        """
        BM25 搜索

        Args:
            query: 查询文本
            top_k: 返回数量

        Returns:
            List[RetrievalResult]: 检索结果
        """
        if not self._documents:
            return []

        query_terms = self._tokenize(query)
        scores = {}

        for doc_id in self._documents:
            score = self._calculate_bm25_score(query_terms, doc_id)
            if score > 0:
                scores[doc_id] = score

        # 排序取 top_k
        sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_k]

        # 构建结果
        results = []
        for doc_id, score in sorted_scores:
            doc = self._documents[doc_id]
            results.append(RetrievalResult(
                doc_id=doc_id,
                content=doc["content"],
                score=score,
                source="bm25",
                metadata=doc["metadata"],
            ))

        return results

    def _calculate_bm25_score(self, query_terms: List[str], doc_id: str) -> float:
        """计算 BM25 分数"""
        score = 0.0
        doc_length = self._doc_lengths.get(doc_id, 0)

        for term in query_terms:
            if term not in self._term_freqs:
                continue

            # 词频
            tf = self._term_freqs[term].get(doc_id, 0)
            if tf == 0:
                continue

            # IDF (带缓存)
            if term not in self._idf_cache:
                df = self._doc_freqs.get(term, 0)
                n = len(self._documents)
                self._idf_cache[term] = max(0.0, math.log((n - df + 0.5) / (df + 0.5) + 1))
            idf = self._idf_cache[term]

            # BM25 公式（防止除零）
            if self._avg_doc_length == 0:
                tf_norm = 0.0
            else:
                tf_norm = (tf * (self.k1 + 1)) / (
                    tf + self.k1 * (1 - self.b + self.b * doc_length / self._avg_doc_length)
                )

            score += idf * tf_norm

        return score

    def _tokenize(self, text: str) -> List[str]:
        """
        分词（支持中英文）

        使用 jieba 分词（如果可用），否则使用正则分词
        """
        try:
            import jieba

            # 英文按空格分词
            english_words = re.findall(r'[a-z]+', text.lower())

            # 中文使用 jieba 分词
            chinese_text = re.sub(r'[a-z]+', '', text.lower())
            chinese_words = list(jieba.cut(chinese_text))

            # 合并，过滤停用词
            stop_words = {"的", "了", "是", "在", "我", "有", "和", "就", "不", "人", "都", "一", "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着", "没有", "看", "好", "自己", "这"}
            all_words = english_words + [w for w in chinese_words if len(w) > 1 and w not in stop_words]

            return all_words

        except ImportError:
            # jieba 未安装，使用正则分词
            return re.findall(r'[\w一-鿿]+', text.lower())

    def remove(self, doc_id: str):
        """移除文档"""
        if doc_id not in self._documents:
            return

        del self._documents[doc_id]
        del self._doc_lengths[doc_id]

        # 更新索引
        for term in list(self._term_freqs.keys()):
            if doc_id in self._term_freqs[term]:
                del self._term_freqs[term][doc_id]
                self._doc_freqs[term] -= 1
                if self._doc_freqs[term] == 0:
                    del self._doc_freqs[term]
                    del self._term_freqs[term]

        # 清除 IDF 缓存
        self._idf_cache.clear()

    def clear(self):
        """清空索引"""
        self._documents.clear()
        self._doc_lengths.clear()
        self._term_freqs.clear()
        self._doc_freqs.clear()
        self._idf_cache.clear()

    @property
    def size(self) -> int:
        return len(self._documents)


class DenseRetriever:
    """
    稠密检索器 (Vector)

    特点:
    - 基于语义相似度
    - 使用 Embedding 模型
    - 支持模糊匹配
    - 需要向量数据库
    """

    def __init__(self, embedding_model=None, vector_store=None):
        """
        Args:
            embedding_model: Embedding 模型 (支持 embed/encode 方法)
            vector_store: 向量数据库
        """
        self.embedding_model = embedding_model
        self.vector_store = vector_store
        self._documents: Dict[str, Dict] = {}

    def _get_embedding(self, text: str) -> List[float]:
        """获取文本的 embedding（适配不同模型）"""
        if not self.embedding_model:
            return []

        # 尝试 embed 方法
        if hasattr(self.embedding_model, 'embed'):
            return self.embedding_model.embed(text)

        # 尝试 encode 方法 (SentenceTransformer)
        if hasattr(self.embedding_model, 'encode'):
            result = self.embedding_model.encode(text, normalize_embeddings=True)
            if hasattr(result, 'tolist'):
                return result.tolist()
            return list(result)

        return []

    def _get_embeddings_batch(self, texts: List[str]) -> List[List[float]]:
        """批量获取 embeddings"""
        if not self.embedding_model:
            return []

        # 尝试 embed_batch 方法
        if hasattr(self.embedding_model, 'embed_batch'):
            return self.embedding_model.embed_batch(texts)

        # 尝试 encode 方法 (SentenceTransformer)
        if hasattr(self.embedding_model, 'encode'):
            results = self.embedding_model.encode(texts, normalize_embeddings=True)
            if hasattr(results, 'tolist'):
                return results.tolist()
            return [list(r) for r in results]

        return []

    def add_document(self, doc_id: str, content: str, metadata: Dict = None):
        """添加文档"""
        self._documents[doc_id] = {
            "content": content,
            "metadata": metadata or {},
        }

        # 如果有向量数据库，添加向量
        if self.vector_store and self.embedding_model:
            vector = self._get_embedding(content)
            self.vector_store.add(doc_id, vector, metadata)

    def add_batch(self, documents: List[Dict[str, Any]]):
        """批量添加文档"""
        for doc in documents:
            self.add_document(
                doc_id=doc.get("id", ""),
                content=doc.get("content", ""),
                metadata=doc.get("metadata", {}),
            )

    def search(self, query: str, top_k: int = 10, min_score: float = 0.3) -> List[RetrievalResult]:
        """
        向量搜索

        Args:
            query: 查询文本
            top_k: 返回数量
            min_score: 最小相似度

        Returns:
            List[RetrievalResult]: 检索结果
        """
        if not self.embedding_model:
            return []

        # 生成查询向量
        query_vector = self._get_embedding(query)

        # 向量搜索
        if self.vector_store:
            # 使用向量数据库
            results = self.vector_store.search(query_vector, top_k=top_k)
            return [
                RetrievalResult(
                    doc_id=r.get("id", ""),
                    content=r.get("content", ""),
                    score=r.get("score", 0),
                    source="vector",
                    metadata=r.get("metadata", {}),
                )
                for r in results
                if r.get("score", 0) >= min_score
            ]
        else:
            # 内存中的向量搜索
            scores = []
            for doc_id, doc in self._documents.items():
                if "vector" not in doc:
                    # 生成向量
                    doc["vector"] = self._get_embedding(doc["content"])

                # 计算余弦相似度
                score = self._cosine_similarity(query_vector, doc["vector"])
                if score >= min_score:
                    scores.append((score, doc_id))

            # 排序取 top_k
            scores.sort(reverse=True)
            scores = scores[:top_k]

            # 构建结果
            results = []
            for score, doc_id in scores:
                doc = self._documents[doc_id]
                results.append(RetrievalResult(
                    doc_id=doc_id,
                    content=doc["content"],
                    score=score,
                    source="vector",
                    metadata=doc["metadata"],
                ))

            return results

    def _cosine_similarity(self, vec1: List[float], vec2: List[float]) -> float:
        """计算余弦相似度"""
        if len(vec1) != len(vec2):
            return 0.0

        dot_product = sum(a * b for a, b in zip(vec1, vec2))
        norm1 = sum(a * a for a in vec1) ** 0.5
        norm2 = sum(b * b for b in vec2) ** 0.5

        if norm1 == 0 or norm2 == 0:
            return 0.0

        return dot_product / (norm1 * norm2)

    def remove(self, doc_id: str):
        """移除文档"""
        self._documents.pop(doc_id, None)
        if self.vector_store:
            self.vector_store.delete(doc_id)

    def clear(self):
        """清空索引"""
        self._documents.clear()
        if self.vector_store:
            self.vector_store.clear()

    @property
    def size(self) -> int:
        return len(self._documents)


class HybridRetriever:
    """
    混合检索器

    组合稀疏检索 (BM25) 和稠密检索 (Vector)，
    使用融合算法合并结果。

    架构:
    ┌─────────────────────────────────────────┐
    │           HybridRetriever               │
    ├─────────────────────────────────────────┤
    │  ┌─────────────┐  ┌─────────────┐      │
    │  │ Sparse      │  │ Dense       │      │
    │  │ (BM25)      │  │ (Vector)    │      │
    │  └─────────────┘  └─────────────┘      │
    │         │                │              │
    │         └───────┬────────┘              │
    │                 ↓                       │
    │         ┌─────────────┐                 │
    │         │ Fusion      │                 │
    │         │ (RRF/...)   │                 │
    │         └─────────────┘                 │
    │                 ↓                       │
    │         ┌─────────────┐                 │
    │         │ Reranker    │                 │
    │         │ (可选)      │                 │
    │         └─────────────┘                 │
    └─────────────────────────────────────────┘
    """

    def __init__(
        self,
        sparse_retriever: Optional[SparseRetriever] = None,
        dense_retriever: Optional[DenseRetriever] = None,
        fusion_method: str = "rrf",
        fusion_kwargs: Optional[Dict] = None,
        reranker=None,
    ):
        """
        Args:
            sparse_retriever: 稀疏检索器
            dense_retriever: 稠密检索器
            fusion_method: 融合方法 ("rrf", "weighted", "convex", "combmnz")
            fusion_kwargs: 融合算法参数
            reranker: 重排序器
        """
        self.sparse_retriever = sparse_retriever
        self.dense_retriever = dense_retriever
        self.fusion = create_fusion(fusion_method, **(fusion_kwargs or {}))
        self.reranker = reranker

    def add_documents(self, documents: List[Dict[str, Any]]):
        """添加文档到两个检索器"""
        if self.sparse_retriever:
            self.sparse_retriever.add_batch(documents)
        if self.dense_retriever:
            self.dense_retriever.add_batch(documents)

    def search(
        self,
        query: str,
        top_k: int = 10,
        sparse_top_k: int = 20,
        dense_top_k: int = 20,
        min_score: float = 0.0,
    ) -> List[RetrievalResult]:
        """
        混合搜索

        Args:
            query: 查询文本
            top_k: 最终返回数量
            sparse_top_k: 稀疏检索数量
            dense_top_k: 稠密检索数量
            min_score: 最小分数

        Returns:
            List[RetrievalResult]: 检索结果
        """
        all_results = []

        # 1. 稀疏检索 (BM25)
        sparse_results = []
        if self.sparse_retriever:
            sparse_results = self.sparse_retriever.search(query, top_k=sparse_top_k)
            all_results.append([r.to_dict() for r in sparse_results])
            logger.debug(f"稀疏检索: {len(sparse_results)} 条结果")

        # 2. 稠密检索 (Vector)
        dense_results = []
        if self.dense_retriever:
            dense_results = self.dense_retriever.search(query, top_k=dense_top_k)
            all_results.append([r.to_dict() for r in dense_results])
            logger.debug(f"稠密检索: {len(dense_results)} 条结果")

        # 3. 融合
        if len(all_results) == 0:
            return []
        elif len(all_results) == 1:
            # 只有一路检索，直接返回
            results = sparse_results if sparse_results else dense_results
            return results[:top_k]
        else:
            # 多路检索，需要融合
            fused = self.fusion.fuse(
                all_results,
                source_names=["bm25", "vector"][:len(all_results)],
            )

            # 转换为 RetrievalResult
            results = []
            for r in fused:
                if r.score >= min_score:
                    results.append(RetrievalResult(
                        doc_id=r.doc_id,
                        content=r.content,
                        score=r.score,
                        source="hybrid",
                        metadata=r.metadata,
                    ))

            # 4. 重排序（可选）
            if self.reranker and results:
                results = self.reranker.rerank(query, results)

            return results[:top_k]

    def remove(self, doc_id: str):
        """移除文档"""
        if self.sparse_retriever:
            self.sparse_retriever.remove(doc_id)
        if self.dense_retriever:
            self.dense_retriever.remove(doc_id)

    def clear(self):
        """清空索引"""
        if self.sparse_retriever:
            self.sparse_retriever.clear()
        if self.dense_retriever:
            self.dense_retriever.clear()

    @property
    def size(self) -> Dict[str, int]:
        """获取索引大小"""
        return {
            "sparse": self.sparse_retriever.size if self.sparse_retriever else 0,
            "dense": self.dense_retriever.size if self.dense_retriever else 0,
        }


class QueryRewriter:
    """
    查询重写器

    支持:
    - 中文分词 (jieba)
    - 查询扩展
    - 同义词替换
    """

    def __init__(self, use_jieba: bool = True):
        self.use_jieba = use_jieba
        self._stop_words = {"的", "了", "是", "在", "我", "有", "和", "就", "不", "人", "都", "一", "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着", "没有", "看", "好", "自己", "这"}

    def rewrite(self, query: str) -> List[str]:
        """
        重写查询

        Args:
            query: 原始查询

        Returns:
            List[str]: 查询变体列表
        """
        queries = [query]

        # 1. 分词
        tokens = self._tokenize(query)
        if len(tokens) > 1:
            queries.extend(tokens)

        # 2. 去掉常见后缀
        for suffix in ["怎么学", "怎么用", "是什么", "怎么理解", "如何", "为什么", "怎么办"]:
            if query.endswith(suffix):
                core = query[:-len(suffix)]
                if core:
                    queries.append(core)

        # 3. 去重
        return list(set(queries))

    def _tokenize(self, text: str) -> List[str]:
        """分词"""
        if self.use_jieba:
            try:
                import jieba

                # 英文按空格分词
                english_words = re.findall(r'[a-zA-Z]+', text)

                # 中文使用 jieba 分词
                chinese_text = re.sub(r'[a-zA-Z]+', '', text)
                chinese_words = list(jieba.cut(chinese_text))

                # 合并，过滤停用词
                all_words = english_words + [w for w in chinese_words if len(w) > 1 and w not in self._stop_words]

                return all_words

            except ImportError:
                pass

        # 回退到正则分词
        return re.findall(r'[\w一-鿿]+', text.lower())
